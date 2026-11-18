"""Pick flow — the owner's Upwork-bot flow, used by sources marked `flow: pick` in sources.yaml.

  find → 🎯 card on Telegram (no AI yet) → owner taps ✍️ → AI writes the bid → rule checker (hunt/policy.py),
  automatic rewrites until it passes → owner reviews → ✅ Approve locks the exact text.

Sending the locked bid is Step 2 (Freelancer's official bid API). Until then the owner bids by hand
and taps "✅ I bid myself".
"""
import json
import time

from . import brain, chatgpt, policy, store, telegram
from .config import BID_RESERVE, MAX_AI_PER_DAY, TELEGRAM_CHAT_ID

MAX_CARDS_PER_CYCLE = 15
MAX_REDRAFTS = 3  # rewrites after the first attempt, like the Upwork bot's bounded redrafts
OWNER_NAME = brain.NAME

# Allowed taps per current status. Anything else is ignored (e.g. a double tap, or a tap on a locked bid).
TAPS = {
    "write": {"pick", "later"},
    "later": {"pick"},
    "redo": {"drafted", "draft_failed"},
    "bidok": {"drafted"},
}
LOCKED = {"bid_approved", "sent", "replied", "won", "lost"}
STATUSES = {"pick_new", "pick", "later", "to_write", "drafted", "draft_failed", "bid_approved"}


def owns(row, action: str = "") -> bool:
    """Is this lead (or tap) handled by the pick flow? Older cards from the same source keep the old flow."""
    return bool(row) and (row["status"] in STATUSES or action in TAPS)


def _ai_left(con) -> int:
    return store.bid_ai_left(con, MAX_AI_PER_DAY, BID_RESERVE)


def _data(row) -> dict:
    return json.loads(row["data"] or "{}") if row and row["data"] else {}


def _job_text(row) -> str:
    return f"{row['title']}\n{row['summary']}"


def rebutton(con, lead_id: str) -> None:
    """Show the buttons for the current status on the card and on the bid message."""
    row = store.get(con, lead_id)
    if not row:
        return
    kb = telegram.buttons(lead_id, row["status"], url=row["url"])
    card_kb = _label_kb(lead_id, "✍️ Bid ready below — approve it there", row["url"]) \
        if row["status"] == "drafted" else kb  # Approve lives only next to the text it locks
    for mid, markup in ((row["tg_card_id"], card_kb), (row["tg_draft_id"], kb)):
        if mid:
            _edit_kb(mid, markup)


def _label_kb(lead_id: str, label: str, url: str = "") -> dict:
    kb = [[{"text": f"• {label} •", "callback_data": f"noop:{lead_id}"}]]
    if url:
        kb.append([{"text": "Open ↗", "url": url}])
    return {"inline_keyboard": kb}


def _edit_kb(message_id, markup) -> None:
    try:
        telegram.call("editMessageReplyMarkup", chat_id=TELEGRAM_CHAT_ID, message_id=message_id, reply_markup=markup)
    except Exception:  # noqa: BLE001 — old or unchanged message; status in the database still counts
        pass


def _post_bid(con, row, d: dict) -> None:
    """Send the bid as a new message; the old version loses its buttons so it can never be approved."""
    if row["tg_draft_id"]:
        _edit_kb(row["tg_draft_id"], _label_kb(row["id"], "Replaced by a newer version below"))
    m = telegram.call("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=telegram.bid_text(d),
                      reply_to_message_id=row["tg_card_id"],
                      reply_markup=telegram.buttons(row["id"], "drafted", url=row["url"]))
    store.update(con, row["id"], status="drafted", data=d, tg_draft_id=m["message_id"])


def send_new_cards(con, rows=None) -> int:
    """New pick-flow leads go to the phone as short cards. No AI is spent until the owner taps ✍️."""
    if not telegram.enabled():
        return 0
    if rows is None:
        rows = store.with_status(con, "pick_new", max_age_days=3)
        rows.sort(key=lambda r: -r["created_at"])
    sent = 0
    for r in rows[:MAX_CARDS_PER_CYCLE]:
        try:
            m = telegram.call("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=telegram.pick_card_text(r),
                              parse_mode="HTML", disable_web_page_preview=True,
                              reply_markup=telegram.buttons(r["id"], "pick", url=r["url"]))
        except Exception as ex:  # noqa: BLE001 — one bad card must never block the rest
            store.update(con, r["id"], status="error", data={**_data(r), "error": f"telegram: {str(ex)[:300]}"})
            continue
        store.update(con, r["id"], status="pick", tg_card_id=m["message_id"])
        sent += 1
    return sent


def write(con, row, instruction: str = "", current: str = "") -> dict:
    """Write (or rewrite) a bid until the checker accepts it or attempts run out. Returns the data saved."""
    lead = dict(row)
    d = _data(row)
    feedback: list[str] = []
    best = None
    for _ in range(1 + MAX_REDRAFTS):
        if not _ai_left(con):
            break
        store.ai_count_add(con, bid=True)
        out = brain.write_bid(lead, feedback, instruction, current)
        if not out:
            continue
        verdict = policy.assess(out["bid_text"], job_text=_job_text(row), owner_name=OWNER_NAME)
        attempt = {"bid": out["bid_text"].strip(), "days": out.get("days"), "proof_ids": out.get("proof_ids", []),
                   "policy": verdict.as_dict()}
        if best is None or verdict.accepted or verdict.score > best["policy"]["score"]:
            best = attempt
        if verdict.accepted:
            break
        feedback = list(verdict.violations)
    d["attempt"] = best
    if best and best["policy"]["accepted"]:
        d.update(best)
    return d


def process_writes(con) -> int:
    """Called by the writer thread: write every bid the owner asked for."""
    done = 0
    for row in store.with_status(con, "to_write"):
        saved = _data(row)
        current = "" if saved.get("fresh") else saved.get("bid", "")  # 🔁 Redo = fresh text, old bid kept as backup
        try:
            d = write(con, row, instruction=saved.get("instruction", ""), current=current)
        except Exception as ex:  # noqa: BLE001 — any failure ends this request; the writer must not retry it forever
            paused = isinstance(ex, (chatgpt.UsageLimit, chatgpt.LoginNeeded, chatgpt.TryLater))
            telegram.say(f"⏸ Can't write this bid right now: {str(ex)[:200]}. Tap 🔁 to try again"
                         + (" later." if paused else "."), reply_to_message_id=row["tg_card_id"])
            saved.pop("instruction", None)
            saved.pop("fresh", None)
            store.update(con, row["id"], status="drafted" if saved.get("bid") else "draft_failed", data=saved)
            rebutton(con, row["id"])
            continue
        d.pop("instruction", None)
        d.pop("fresh", None)
        attempt = d.get("attempt")
        if attempt and attempt["policy"]["accepted"]:
            _post_bid(con, row, d)
        else:
            why = "; ".join((attempt or {}).get("policy", {}).get("blockers", [])[:6]) or \
                  ("today's bid AI allowance is used up" if not _ai_left(con) else "the AI gave no answer")
            if d.get("bid"):  # a rewrite failed: keep the last good bid, like the Upwork bot
                telegram.say(f"⚠️ The rewrite didn't pass the checker ({why}). Your previous bid is unchanged.",
                             reply_to_message_id=row["tg_draft_id"] or row["tg_card_id"])
                store.update(con, row["id"], status="drafted", data=d)
            else:
                telegram.say(f"⚠️ No bid passed the checker after {1 + MAX_REDRAFTS} tries: {why}",
                             reply_to_message_id=row["tg_card_id"])
                store.update(con, row["id"], status="draft_failed", data=d)
        rebutton(con, row["id"])
        done += 1
    return done


def tap(con, action: str, lead_id: str, message_id=None) -> str:
    """Owner tapped a pick-flow button. Returns the short note Telegram shows."""
    row = store.get(con, lead_id)
    if not row:
        return "Unknown lead"
    status = row["status"]
    if action == "bidok" and message_id != row["tg_draft_id"]:
        return "That's an old version — approve the newest bid"
    if action == "skipped":
        if status in LOCKED:
            return "Already approved — use ✅ I bid myself or leave it"
        store.update(con, lead_id, status="skipped")
    elif action == "sent":
        if status != "bid_approved":
            return "Approve the bid first"
        store.update(con, lead_id, status="sent")
    elif action not in TAPS or status not in TAPS[action]:
        return "Not possible right now"
    elif action in ("write", "redo"):
        if not _ai_left(con):
            return "Today's bid AI allowance is used up — try tomorrow"
        d = _data(row)
        if action == "redo":
            d["fresh"] = True  # write new text, but keep the old bid in case every new attempt fails
        store.update(con, lead_id, status="to_write", data=d)
    elif action == "later":
        store.update(con, lead_id, status="later")
    elif action == "bidok":
        d = _data(row)
        verdict = policy.assess(d.get("bid", ""), job_text=_job_text(row), owner_name=OWNER_NAME)
        if not verdict.accepted:
            return "This text no longer passes the checker — tap 🔁 Redo"
        d["locked_bid"] = d["bid"]
        d["locked_at"] = time.time()
        d["locked_policy"] = verdict.as_dict()
        store.update(con, lead_id, status="bid_approved", data=d)
    rebutton(con, lead_id)
    return {"write": "Writing — the bid arrives in a minute", "redo": "Rewriting…", "later": "Parked",
            "bidok": "🔒 Locked. Bid it on Freelancer, then tap ✅ I bid myself", "skipped": "Skipped",
            "sent": "Marked as bid"}.get(action, "OK")


def reply(con, row, text: str) -> None:
    """Owner replied to a bid: TEXT:… = their own words (checked), anything else = rewrite instruction."""
    if row["status"] in LOCKED:
        telegram.say("🔒 This bid is approved and locked; it can't be changed.", reply_to_message_id=row["tg_draft_id"])
        return
    if row["status"] not in ("drafted", "draft_failed"):
        return
    d = _data(row)
    if text[:5].upper() == "TEXT:":
        own = text[5:].strip()
        verdict = policy.assess(own, job_text=_job_text(row), owner_name=OWNER_NAME)
        if not verdict.accepted:
            telegram.say("⚠️ Your text breaks: " + "; ".join(verdict.blockers[:6]) + ". Nothing changed.",
                         reply_to_message_id=row["tg_draft_id"])
            return
        d.update({"bid": own, "policy": verdict.as_dict()})
        _post_bid(con, row, d)
        rebutton(con, row["id"])
        return
    if not _ai_left(con):
        telegram.say("⏸ Today's bid AI allowance is used up; try the rewrite tomorrow.")
        return
    d["instruction"] = text
    store.update(con, row["id"], status="to_write", data=d)
    rebutton(con, row["id"])
    telegram.say("Rewriting…", reply_to_message_id=row["tg_draft_id"] or row["tg_card_id"])


def resend_later(con) -> int:
    """/later: send parked cards again as fresh cards at the bottom of the chat."""
    rows = store.with_status(con, "later", max_age_days=14)
    for r in rows:
        store.update(con, r["id"], status="pick_new")
    return send_new_cards(con, [dict(store.get(con, r["id"])) for r in rows]) if rows else 0
