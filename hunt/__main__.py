"""Hunt command line.

  python -m hunt collect   # dry run: fetch + keyword-filter, print what would go to the AI (no cost)
  python -m hunt run       # one full cycle: collect → AI score + draft → Telegram (or print)
  python -m hunt bot       # always-on: runs a cycle every hour and handles your Telegram taps
  python -m hunt digest    # send the weekly per-source summary
  python -m hunt login     # sign in with ChatGPT (on your own computer), then deploy/push-login.sh
  python -m hunt models    # list the ChatGPT models your plan offers
"""
import json
import sys
import threading
import time
import traceback

from . import brain, chatgpt, store, telegram
from .config import MAX_AI_PER_DAY, MAX_AI_PER_RUN, MIN_SCORE, RUN_EVERY_MINUTES, TELEGRAM_CHAT_ID
from .filters import keep
from .sources import collect

PRIORITY = {"ask": 0, "funding": 1, "job": 2}
_lock = threading.Lock()


def gather(con, dry=False) -> list[dict]:
    leads, errors = collect()
    fresh, seen_keys = [], set()
    for lead in leads:
        if lead["title_key"] in seen_keys or not store.is_new(con, lead):
            continue
        seen_keys.add(lead["title_key"])
        ok, markets = keep(lead)
        lead["markets"] = markets
        if not dry:
            store.add(con, lead, markets, "queued" if ok else "filtered")
        if ok:
            fresh.append(lead)
    print(f"collected {len(leads)} items, {len(fresh)} new + relevant by keywords")
    for e in errors:
        print("  source error →", e)
    return fresh


def run_cycle() -> str:
    with _lock:
        con = store.db()
        gather(con)
        todo = store.queued(con)
        todo.sort(key=lambda l: (PRIORITY.get(l["kind"], 9), -l["created_at"]))
        sent = low = 0
        budget = max(0, min(MAX_AI_PER_RUN, MAX_AI_PER_DAY - store.ai_used_today(con)))
        paused = ""
        for lead in todo[:budget]:
            try:
                d = brain.assess(lead)
                store.ai_count_add(con)
            except (chatgpt.UsageLimit, chatgpt.LoginNeeded, chatgpt.TryLater) as ex:
                paused = str(ex)  # stop this cycle; leads stay queued for the next one
                _warn_once(con, type(ex).__name__, f"⏸ Hunt paused: {ex}")
                break
            except Exception as ex:  # noqa: BLE001
                store.ai_count_add(con)
                store.update(con, lead["id"], status="error", data={"error": str(ex)})
                print("AI error:", ex)
                continue
            if not d or not d["relevant"] or d["score"] < MIN_SCORE:
                store.update(con, lead["id"], status="low", score=(d or {}).get("score", 0), data=d or {})
                low += 1
                continue
            if store.company_recent(con, d["company"]):
                store.update(con, lead["id"], status="duplicate", score=d["score"], data=d)
                continue
            if telegram.enabled():
                try:
                    card_id, draft_id = telegram.send_lead(lead, d)
                except Exception as ex:  # noqa: BLE001 — one bad card must never block the queue
                    store.update(con, lead["id"], status="error", score=d["score"],
                                 data={**d, "error": f"telegram: {str(ex)[:300]}"})
                    print("Telegram send error:", str(ex)[:300])
                    continue
                store.update(con, lead["id"], status="to_phone", score=d["score"], data=d,
                             tg_card_id=card_id, tg_draft_id=draft_id)
            else:
                store.update(con, lead["id"], status="to_phone", score=d["score"], data=d)
                print("\n" + "=" * 70 + f"\n[{d['score']}] {lead['title']}\n{lead['url']}\n"
                      + telegram.draft_text(d))
            sent += 1
        summary = f"cycle done: {sent} to phone, {low} below {MIN_SCORE}, {max(0, len(todo) - budget)} still queued (AI used today: {store.ai_used_today(con)}/{MAX_AI_PER_DAY})"
        if paused:
            summary += f" — paused: {paused}"
        print(summary)
        return summary


def _warn_once(con, kind: str, text: str) -> None:
    """Tell the owner on Telegram at most once a day per problem kind."""
    key = f"warned_{kind}"
    today = time.strftime("%Y-%m-%d")
    if store.meta_get(con, key) != today:
        store.meta_set(con, key, today)
        if telegram.enabled():
            telegram.say(text)


def digest_text(days=7) -> str:
    s = store.stats(store.db(), days)
    lines = [f"📊 Hunt — last {days} days (per source)"]
    for src, st in sorted(s.items()):
        shown = st.get("to_phone", 0) + sum(st.get(k, 0) for k in ("sent", "skipped", "replied", "won", "lost"))
        lines.append(f"{src}: shown {shown} · sent {st.get('sent', 0) + st.get('replied', 0) + st.get('won', 0) + st.get('lost', 0)}"
                     f" · replied {st.get('replied', 0) + st.get('won', 0) + st.get('lost', 0)} · won {st.get('won', 0)}")
    return "\n".join(lines)


BUTTON_ACTIONS = {"sent", "skipped", "replied", "won", "lost", "noop"}


def handle_update(con, u) -> None:
    if "callback_query" in u:
        q = u["callback_query"]
        if str(q.get("message", {}).get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
            return  # only the owner's chat may press buttons
        action, _, lead_id = str(q.get("data", "")).partition(":")
        if action not in BUTTON_ACTIONS or not lead_id:
            return
        if action != "noop":
            store.update(con, lead_id, status=action)
            telegram.call("editMessageReplyMarkup", chat_id=q["message"]["chat"]["id"],
                          message_id=q["message"]["message_id"], reply_markup=telegram.buttons(lead_id, action))
        telegram.call("answerCallbackQuery", callback_query_id=q["id"], text=f"Marked {action}")
        return
    msg = u.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not text:
        return
    if text.startswith("/start") and not TELEGRAM_CHAT_ID and msg.get("chat", {}).get("type") == "private":
        telegram.call("sendMessage", chat_id=chat_id,
                      text=f"Hunt here. Your chat id is {chat_id}. Put it in TELEGRAM_CHAT_ID.")
        return
    if chat_id != str(TELEGRAM_CHAT_ID):
        return  # ignore strangers (including /start once the owner is set)
    if text.startswith("/stats"):
        telegram.say(digest_text())
    elif text.startswith("/run"):
        telegram.say("Running a cycle now…")
        telegram.say(run_cycle())
    elif msg.get("reply_to_message"):
        row = store.by_message(con, msg["reply_to_message"]["message_id"])
        if not row:
            return
        lead, d = dict(row), json.loads(row["data"] or "{}")
        if store.ai_used_today(con) >= MAX_AI_PER_DAY:
            telegram.say(f"⏸ Daily AI limit ({MAX_AI_PER_DAY}) reached; try the rewrite tomorrow.")
            return
        try:
            store.ai_count_add(con)
            new = brain.rewrite(lead, {k: d.get(k, "") for k in ("email_subject", "email_body", "dm")}, text)
        except (chatgpt.UsageLimit, chatgpt.LoginNeeded, chatgpt.TryLater) as ex:
            telegram.say(f"⏸ Can't rewrite right now: {ex}")
            return
        if new:
            d.update(new)
            m = telegram.say(telegram.draft_text(d), reply_to_message_id=row["tg_card_id"])
            store.update(con, lead["id"], data=d, tg_draft_id=m["message_id"])


def bot() -> None:
    def scheduler():
        while True:
            try:
                run_cycle()
                week = time.strftime("%G-W%V")
                con2 = store.db()
                if time.strftime("%a") == "Sun" and store.meta_get(con2, "digest_week") != week:
                    telegram.say(digest_text())
                    store.meta_set(con2, "digest_week", week)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            time.sleep(RUN_EVERY_MINUTES * 60)

    threading.Thread(target=scheduler, daemon=True).start()
    con = store.db()
    offset = int(store.meta_get(con, "tg_offset", "0"))
    print("bot listening…")
    while True:
        try:
            updates = telegram.call("getUpdates", offset=offset, timeout=50)
            for u in updates:
                offset = u["update_id"] + 1
                store.meta_set(con, "tg_offset", offset)
                try:
                    handle_update(con, u)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            time.sleep(10)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    if cmd == "collect":
        fresh = gather(store.db(), dry=True)
        for l in fresh[:60]:
            print(f"- [{l['kind']}/{l['source']}] {','.join(l['markets'])} | {l['title'][:110]}")
    elif cmd == "run":
        run_cycle()
    elif cmd == "bot":
        bot()
    elif cmd == "login":
        chatgpt.login()
        for m in chatgpt.models()[:15]:
            print(f"  model: {m['slug']}  {m['name']}")
    elif cmd == "models":
        for m in chatgpt.models():
            print(f"{m['slug']}  {m['name']}")
    elif cmd == "digest":
        telegram.say(digest_text()) if telegram.enabled() else print(digest_text())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
