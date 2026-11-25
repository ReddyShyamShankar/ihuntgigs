"""Pick flow — the owner's Upwork-bot flow, used by sources marked `flow: pick` in sources.yaml.

  find → 🎯 card on Telegram (no AI yet) → owner taps ✍️ → AI writes the bid → rule checker (hunt/policy.py),
  automatic rewrites until it passes → owner reviews → ✅ Approve locks the exact text.

Sending the locked bid is Step 2 (Freelancer's official bid API). Until then the owner bids by hand
and taps "✅ I applied". Since 2026-11-20 every contract job source uses this flow.
"""
import json
import time

from . import autoapply, brain, chatgpt, config, policy, store, telegram
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
    "stop": {autoapply.WAIT, autoapply.READY, autoapply.MAC},
}
LOCKED = {"bid_approved", "sent", "replied", "won", "lost"}
STATUSES = {"pick_new", "pick", "later", "to_write", "drafted", "draft_failed", "bid_approved"} | autoapply.STATUSES
EDITABLE = {"drafted", "draft_failed", autoapply.WAIT, autoapply.READY, autoapply.MAC}  # replies may change the text


def owns(row, action: str = "") -> bool:
    """Is this lead (or tap) handled by the pick flow? Older cards from the same source keep the old flow."""
    return bool(row) and (row["status"] in STATUSES or action in TAPS)


def _ai_left(con) -> int:
    return store.bid_ai_left(con, MAX_AI_PER_DAY, BID_RESERVE) if config.ai_on() else 0


def _no_ai_note() -> str:
    return "today's bid AI allowance is used up" if config.ai_on() else "AI is switched off"


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
    style = "form" if autoapply.is_auto(row) else "bid"  # company forms: cover note, one own-portfolio link OK
    feedback: list[str] = []
    best = None
    for _ in range(1 + MAX_REDRAFTS):
        if not _ai_left(con):
            break
        store.ai_count_add(con, bid=True)
        out = brain.write_bid(lead, feedback, instruction, current, style=style)
        if not out:
            continue
        verdict = policy.assess(out["bid_text"], job_text=_job_text(row), owner_name=OWNER_NAME, style=style)
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
        auto = autoapply.is_auto(row)
        if auto and not _ai_left(con):
            continue  # automatic jobs wait (AI off, or today's allowance used) instead of failing
        saved = _data(row)
        where = row["tg_draft_id"] or row["tg_card_id"]
        name = "" if where else f" ({row['title'][:80]})"  # automatic jobs have no card to reply to
        current = "" if saved.get("fresh") else saved.get("bid", "")  # 🔁 Redo = fresh text, old bid kept as backup
        try:
            d = write(con, row, instruction=saved.get("instruction", ""), current=current)
        except Exception as ex:  # noqa: BLE001 — any failure ends this request; the writer must not retry it forever
            paused = isinstance(ex, (chatgpt.UsageLimit, chatgpt.LoginNeeded, chatgpt.TryLater))
            telegram.say(f"⏸ Can't write this bid{name} right now: {str(ex)[:200]}. Tap 🔁 to try again"
                         + (" later." if paused else "."), reply_to_message_id=where)
            saved.pop("instruction", None)
            saved.pop("fresh", None)
            kept = (autoapply.WAIT if auto else "drafted") if saved.get("bid") else "draft_failed"
            store.update(con, row["id"], status=kept, data=saved)
            rebutton(con, row["id"])
            continue
        d.pop("instruction", None)
        d.pop("fresh", None)
        attempt = d.get("attempt")
        if attempt and attempt["policy"]["accepted"]:
            autoapply.queue(con, row, d) if auto else _post_bid(con, row, d)
        else:
            why = "; ".join((attempt or {}).get("policy", {}).get("blockers", [])[:6]) or \
                  (_no_ai_note() if not _ai_left(con) else "the AI gave no answer")
            if d.get("bid"):  # a rewrite failed: keep the last good bid, like the Upwork bot
                telegram.say(f"⚠️ The rewrite didn't pass the checker ({why}). Your previous text is unchanged.",
                             reply_to_message_id=where)
                store.update(con, row["id"], status=autoapply.WAIT if auto else "drafted", data=d)
            else:
                m = telegram.say(f"⚠️ No bid{name} passed the checker after {1 + MAX_REDRAFTS} tries: {why}",
                                 reply_to_message_id=where)
                # An automatic job has no card yet: this message becomes its card (with 🔁 Try again / ⏭ Skip).
                store.update(con, row["id"], status="draft_failed", data=d,
                             **({} if where else {"tg_card_id": m["message_id"]}))
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
    if action == "stop":
        return autoapply.stop(con, row)
    if action == "skipped":
        if status in LOCKED:
            return "Already approved — use ✅ I applied or leave it"
        store.update(con, lead_id, status="skipped")
    elif action == "sent":
        if status not in ("bid_approved", autoapply.MAC, autoapply.READY, autoapply.WAIT):
            return "Approve the bid first"
        store.update(con, lead_id, status="sent")
    elif action not in TAPS or status not in TAPS[action]:
        return "Not possible right now"
    elif action in ("write", "redo"):
        if not _ai_left(con):
            note = _no_ai_note()
            return note[0].upper() + note[1:] + (" — try tomorrow" if config.ai_on() else "")
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
            "bidok": "🔒 Locked. Send it on the job's page, then tap ✅ I applied", "skipped": "Skipped",
            "sent": "Marked as applied"}.get(action, "OK")


def reply(con, row, text: str) -> None:
    """Owner replied to a bid: TEXT:… = their own words (checked), anything else = rewrite instruction."""
    if row["status"] in LOCKED:
        telegram.say("🔒 This bid is approved and locked; it can't be changed.", reply_to_message_id=row["tg_draft_id"])
        return
    if row["status"] not in EDITABLE:
        return
    auto = autoapply.is_auto(row)
    d = _data(row)
    if text[:5].upper() == "TEXT:":
        own = text[5:].strip()
        verdict = policy.assess(own, job_text=_job_text(row), owner_name=OWNER_NAME, style="form" if auto else "bid")
        if not verdict.accepted:
            telegram.say("⚠️ Your text breaks: " + "; ".join(verdict.blockers[:6]) + ". Nothing changed.",
                         reply_to_message_id=row["tg_draft_id"])
            return
        d.update({"bid": own, "policy": verdict.as_dict()})
        autoapply.queue(con, row, d) if auto else _post_bid(con, row, d)
        rebutton(con, row["id"])
        return
    if not _ai_left(con):
        telegram.say("⏸ Today's bid AI allowance is used up; try the rewrite tomorrow." if config.ai_on() else
                     "⏸ AI is switched off. Reply TEXT: + your own words to use your own text.")
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
