"""Hunt command line.

  python -m hunt collect   # dry run: fetch + keyword-filter, print what would go to the AI (no cost)
  python -m hunt run       # one full cycle: collect → AI score + draft → Telegram (or print)
  python -m hunt bot       # always-on: runs a cycle every hour and handles your Telegram taps
  python -m hunt digest    # send the weekly per-source summary
  python -m hunt login     # sign in with ChatGPT (on your own computer), then deploy/push-login.sh
  python -m hunt models    # list the ChatGPT models your plan offers
  python -m hunt apply-queue [--peek] / apply-result ID STATUS [note] / apply-say TEXT   # used by applier/
"""
import json
import sys
import threading
import time
import traceback

from . import brain, chatgpt, gmail_alerts, pick, store, telegram
from .config import BID_RESERVE, MAX_AI_PER_DAY, MAX_AI_PER_RUN, MIN_SCORE, RUN_EVERY_MINUTES, TELEGRAM_CHAT_ID
from .filters import keep
from .sources import collect, pick_sources

PRIORITY = {"ask": 0, "funding": 1, "job": 2}
_lock = threading.Lock()


def gather(con, dry=False) -> list[dict]:
    leads, errors = collect()
    try:
        leads += gmail_alerts.collect(None if dry else con)
    except Exception as ex:  # noqa: BLE001 — Gmail trouble must not stop the other sources
        errors.append(f"linkedin_alerts (gmail): {ex}")
    fresh, seen_keys = [], set()
    picky = pick_sources()
    for lead in leads:
        if lead["title_key"] in seen_keys or not store.is_new(con, lead):
            continue
        seen_keys.add(lead["title_key"])
        ok, markets = keep(lead)
        lead["markets"] = markets
        if not dry:
            store.add(con, lead, markets, ("pick_new" if lead["source"] in picky else "queued") if ok else "filtered")
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
        picked = pick.send_new_cards(con)
        todo = store.queued(con)
        todo.sort(key=lambda l: (PRIORITY.get(l["kind"], 9), -l["created_at"]))
        sent = low = 0
        budget = min(MAX_AI_PER_RUN, store.lead_ai_left(con, MAX_AI_PER_DAY, BID_RESERVE))
        paused = ""
        for lead in todo[:budget]:
            if not store.lead_ai_left(con, MAX_AI_PER_DAY, BID_RESERVE):
                break  # the bid writer runs at the same time; its reserve is off-limits here
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
        try:
            gmail_alerts.mark_done(con)
        except Exception as ex:  # noqa: BLE001
            print("gmail mark-read error:", ex)
        summary = f"cycle done: {picked} pick cards, {sent} to phone, {low} below {MIN_SCORE}, {max(0, len(todo) - budget)} still queued (AI used today: {store.ai_used_today(con)}/{MAX_AI_PER_DAY}, bids {store.bid_ai_used_today(con)}, reserved for bids {BID_RESERVE})"
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
        shown += sum(st.get(k, 0) for k in pick.STATUSES - {"pick_new"})
        lines.append(f"{src}: shown {shown} · sent {st.get('sent', 0) + st.get('replied', 0) + st.get('won', 0) + st.get('lost', 0)}"
                     f" · replied {st.get('replied', 0) + st.get('won', 0) + st.get('lost', 0)} · won {st.get('won', 0)}")
    return "\n".join(lines)


BUTTON_ACTIONS = {"approve", "sent", "skipped", "replied", "won", "lost", "noop"} | set(pick.TAPS)
STATUS_OF = {"approve": "approved"}
APPLY_DONE = {"applied", "needs_you", "failed"}


def rebutton(con, lead_id: str) -> None:
    """Redraw a card's buttons for its current status (keeps the Open / Email links)."""
    row = store.get(con, lead_id)
    if not row or not row["tg_card_id"]:
        return
    lead, d = dict(row), json.loads(row["data"] or "{}")
    telegram.call("editMessageReplyMarkup", chat_id=TELEGRAM_CHAT_ID, message_id=row["tg_card_id"],
                  reply_markup=telegram.buttons(lead_id, row["status"], url=lead["url"],
                                                mail=telegram.gmail_compose(lead, d)))


def approve_all(con) -> int:
    rows = store.with_status(con, "to_phone", max_age_days=3)
    for r in rows:
        store.update(con, r["id"], status="approved")
        try:
            rebutton(con, r["id"])
        except Exception:  # noqa: BLE001 — an old card may be uneditable; the status still counts
            pass
    return len(rows)


def apply_payload(row: dict) -> dict:
    d = json.loads(row.get("data") or "{}")
    return {"id": row["id"], "kind": row["kind"], "source": row["source"], "url": row["url"],
            "title": row["title"], "company": d.get("company") or row["company"],
            "emails": telegram.lead_emails(row),
            "email_subject": d.get("email_subject", ""), "email_body": d.get("email_body", ""),
            "dm": d.get("dm", "")}


def handle_update(con, u) -> None:
    if "callback_query" in u:
        q = u["callback_query"]
        if str(q.get("message", {}).get("chat", {}).get("id", "")) != str(TELEGRAM_CHAT_ID):
            return  # only the owner's chat may press buttons
        action, _, lead_id = str(q.get("data", "")).partition(":")
        if action not in BUTTON_ACTIONS or not lead_id:
            return
        row = store.get(con, lead_id)
        if action != "noop" and pick.owns(row, action):
            note = pick.tap(con, action, lead_id, q.get("message", {}).get("message_id"))
            telegram.call("answerCallbackQuery", callback_query_id=q["id"], text=note)
            return
        if action != "noop":
            status = STATUS_OF.get(action, action)
            store.update(con, lead_id, status=status)
            rebutton(con, lead_id)
        note = "Queued — your Mac will apply" if action == "approve" else f"Marked {action}"
        telegram.call("answerCallbackQuery", callback_query_id=q["id"], text=note)
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
    elif text.startswith("/applyall"):
        n = approve_all(con)
        telegram.say(f"🚀 {n} lead(s) queued for auto-apply. Your Mac applies them next time it checks in."
                     if n else "Nothing waiting — no new cards from the last 3 days.")
    elif text.startswith("/later"):
        n = pick.resend_later(con)
        telegram.say(f"⏰ {n} parked card(s) sent again below." if n else "Nothing parked.")
    elif text.startswith("/run"):
        telegram.say("Running a cycle now…")
        telegram.say(run_cycle())
    elif msg.get("reply_to_message"):
        row = store.by_message(con, msg["reply_to_message"]["message_id"])
        if not row:
            return
        if pick.owns(row):
            pick.reply(con, row, text)
            return
        lead, d = dict(row), json.loads(row["data"] or "{}")
        if not store.bid_ai_left(con, MAX_AI_PER_DAY, BID_RESERVE):
            telegram.say(f"⏸ Daily AI limit ({MAX_AI_PER_DAY}) reached; try the rewrite tomorrow.")
            return
        try:
            store.ai_count_add(con, bid=True)  # owner-requested: comes out of the reserve, not the queue's share
            new = brain.rewrite(lead, {k: d.get(k, "") for k in ("email_subject", "email_body", "dm")}, text)
        except (chatgpt.UsageLimit, chatgpt.LoginNeeded, chatgpt.TryLater) as ex:
            telegram.say(f"⏸ Can't rewrite right now: {ex}")
            return
        if new:
            d.update(new)
            m = telegram.say(telegram.draft_text(d), reply_to_message_id=row["tg_card_id"])
            store.update(con, lead["id"], data=d, tg_draft_id=m["message_id"])
            try:
                rebutton(con, lead["id"])  # the ✉️ Email draft link must carry the rewritten pitch
            except Exception:  # noqa: BLE001 — an old card may be uneditable
                pass


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

    def writer():  # writes bids the owner asked for (✍️ / 🔁 / reply) within seconds, not at the next hourly cycle
        con3 = store.db()
        while True:
            try:
                pick.process_writes(con3)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            time.sleep(10)

    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=writer, daemon=True).start()
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
    elif cmd == "apply-queue":  # used by the Mac helper (applier/) over SSH
        con = store.db()
        # Only one helper runs at a time, so anything still "applying" is left over from a run that
        # stopped part-way: offer it again (the helper skips URLs it already submitted).
        rows = store.with_status(con, ("approved", "applying"))
        if "--peek" not in sys.argv:
            for r in rows:
                if r["status"] != "applying":
                    store.update(con, r["id"], status="applying")
                    rebutton(con, r["id"])
        print(json.dumps([apply_payload(r) for r in rows]))
    elif cmd == "apply-result":  # apply-result LEAD_ID applied|needs_you|failed [note...]
        lead_id, status, note = sys.argv[2], sys.argv[3], " ".join(sys.argv[4:])[:300]
        if status not in APPLY_DONE:
            raise SystemExit(f"status must be one of {sorted(APPLY_DONE)}")
        con = store.db()
        row = store.get(con, lead_id)
        if not row:
            raise SystemExit("unknown lead")
        d = json.loads(row["data"] or "{}")
        d["apply_note"] = note
        store.update(con, lead_id, status="needs_you" if status == "failed" else status, data=d)
        rebutton(con, lead_id)
        if status != "applied" and note and telegram.enabled():
            telegram.say(f"👋 Needs you — {row['title'][:80]}\n{note}", reply_to_message_id=row["tg_card_id"])
    elif cmd == "apply-say":  # the Mac helper's end-of-batch summary
        telegram.say(" ".join(sys.argv[2:])[:3500])
    elif cmd == "digest":
        telegram.say(digest_text()) if telegram.enabled() else print(digest_text())
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
