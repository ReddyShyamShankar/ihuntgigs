"""B7 approval on the phone (PRD §10): one Telegram card per drafted prospect pitch, with
✅ Approve · ✏️ Edit · ⏭ Skip. Nothing is sent from here; approval only locks the text.

Flow (prospect leads only, kind `prospect`):
  drafted (B6) ──card──▶ to_approve ──✅──▶ pitch_approved ──(B8 sender, or the owner by hand)──▶ sent
                              │  ▲                    └──⏭──▶ skipped
                              ✏️ │ reply to the card = edit comment → pitch.revise → new version card
                              └──⏭──▶ skipped
Only fit 60+ (HUNT_PHONE_MIN) reaches the phone; lower drafts wait in the database for the dashboard (B9).
Approve works only on the newest version's card. /approveall approves every waiting card whose
source is not manual-tier and whose draft has no warnings; everything else stays for a one-by-one look.
DM routes (X, Instagram, Telegram, LinkedIn, WhatsApp) are always sent by hand: the card links the profile.
`pitch_approved` is its own status so the job-lead auto-apply queue (`approved`) never picks these up.
"""
import html
import json
import os
import time

import urllib.parse

from . import mailer, pitch, registry, sender, store, telegram
from .config import TELEGRAM_CHAT_ID

PHONE_MIN = int(os.environ.get("HUNT_PHONE_MIN", "60"))
MAX_CARDS_PER_CYCLE = int(os.environ.get("HUNT_CARDS_PER_CYCLE", "10"))
TAPS = {"pok", "pedit", "pskip", "psent"}
WAITING, APPROVED = "to_approve", "pitch_approved"
FU_WAITING, FU_APPROVED = "fu_to_approve", "fu_approved"  # B8b follow-ups use the same cards
WAIT_OF = {"drafted": WAITING, "fu_drafted": FU_WAITING}
DRAFTED_OF = {v: k for k, v in WAIT_OF.items()}
APPROVE_OF = {WAITING: APPROVED, FU_WAITING: FU_APPROVED}
WAITINGS, APPROVEDS = set(APPROVE_OF), set(APPROVE_OF.values())
NET = {"x": "X", "instagram": "Instagram", "telegram": "Telegram", "linkedin": "LinkedIn", "whatsapp": "WhatsApp"}


def owns(row, action: str = "") -> bool:
    """Is this lead (or tap) handled here? All prospect leads, so the job flows never touch them."""
    return action in TAPS or (bool(row) and row["kind"] == "prospect")


def _data(row) -> dict:
    return json.loads(row["data"] or "{}")


def _fmt_subs(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e3 else str(n)


def contact_link(data: dict) -> tuple[str, str]:
    """(label, url) of where the pitch goes. Email has no link (B8 sends it); DMs link the profile."""
    c = data.get("contacts") or {}
    route = (data.get("draft") or {}).get("channel") or c.get("route") or "email"
    if route == "email":
        return f"✉️ email {c.get('email', '')}".strip(), ""
    url = telegram._safe_url((c.get("socials") or {}).get(route, ""))[0]
    return f"💬 {NET.get(route, route)} DM — you send it by hand", url


def compose_link(data: dict) -> str:
    """Prefilled draft for an email pitch the owner sends by hand, with the same footer the sender adds.
    Opens in the outreach inbox (Outlook) when one is connected, so replies land where Hunt reads them;
    before that, a Gmail draft."""
    to, d = (data.get("contacts") or {}).get("email", ""), data.get("draft") or {}
    if not to:
        return ""
    body = (d.get("body") or "")[:1500] + sender.footer()
    if mailer.inboxes():
        return "https://outlook.office.com/mail/deeplink/compose?" + urllib.parse.urlencode(
            {"to": to, "subject": d.get("subject", ""), "body": body}, quote_via=urllib.parse.quote)
    return "https://mail.google.com/mail/?" + urllib.parse.urlencode(
        {"view": "cm", "fs": "1", "to": to, "su": d.get("subject", ""), "body": body})


def card_text(con, row) -> str:
    e = html.escape
    data = _data(row)
    r, d, extra = data.get("research") or {}, data.get("draft") or {}, data.get("extra") or {}
    score = row["score"] or 0
    dot = "🟢" if score >= 75 else "🟡" if score >= PHONE_MIN else "⚪"
    hist = pitch._history(con, row)[0]
    subs = _fmt_subs(extra.get("subscribers"))
    label, _ = contact_link(data)
    warn = "".join(f"⚠️ {e(telegram._clip(w, 160))}\n" for w in (d.get("warnings") or [])[:4])
    subject = f"<b>Subject:</b> {e(telegram._clip(d.get('subject'), 150))}\n" if d.get("subject") else ""
    fu_line = f"🔁 <b>Follow-up {d['followup']}</b> — no reply yet\n" if d.get("followup") else ""
    return telegram._clip(
        f"{dot} <b>{score}/100</b> · 📺 {e(row['source'].upper())} · {e(hist)}\n"
        f"<b>{e(telegram._clip(row['company'] or row['title'], 100))}</b>{f' ({subs} subs)' if subs else ''}\n"
        f"<b>Problem:</b> {e(telegram._clip(r.get('problem'), 300))}\n"
        f"<b>Lanes:</b> {e(row['lanes'] or '—')}\n"
        f"<b>Contact:</b> {e(telegram._clip(label, 150))}\n"
        f"{warn}\n{fu_line}<b>— Draft v{d.get('version', 1)} —</b>\n{subject}{e(telegram._clip(d.get('body'), 1500))}\n\n"
        f"<i>Reply to this card with changes (e.g. 'shorter'), or start with TEXT: to use your own words.</i>",
        telegram.MAX_TEXT)


LABEL = {APPROVED: "🔒 APPROVED", FU_APPROVED: "🔒 FOLLOW-UP APPROVED", "sent": "✅ SENT", "skipped": "⏭ SKIPPED"}


def buttons(row) -> dict:
    lid, status, data = row["id"], row["status"], _data(row)
    if status in WAITINGS:
        rows = [[("✅ Approve", "pok"), ("✏️ Edit", "pedit"), ("⏭ Skip", "pskip")]]
    elif status in APPROVEDS:
        email = (data.get("draft") or {}).get("channel", "email") == "email"
        note = "sender emails it" if email and sender.auto_send(row["source"]) else "send it by hand"
        rows = [[(f"• {LABEL[status]} — {note} •", "noop")], [("✅ I sent it", "psent"), ("⏭ Skip", "pskip")]]
    else:
        rows = [[(f"• {LABEL.get(status, status.upper())} •", "noop")]]
    kb = [[{"text": t, "callback_data": f"{a}:{lid}"} for t, a in r] for r in rows]
    links = []
    url = telegram._safe_url(row["url"])[0]
    if url:
        links.append({"text": "Channel ↗", "url": url})
    _, dm = contact_link(data)
    if dm:
        links.append({"text": "Contact ↗", "url": dm})
    elif status in APPROVEDS and not sender.auto_send(row["source"]):
        mail = compose_link(data)
        if mail:
            links.append({"text": "✉️ Email draft", "url": mail})
    if links:
        kb.append(links)
    return {"inline_keyboard": kb}


def _post(con, row) -> int:
    m = telegram.call("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=card_text(con, row), parse_mode="HTML",
                      disable_web_page_preview=True, reply_markup=buttons(row))
    return m["message_id"]


def rebutton(con, lead_id: str) -> None:
    row = store.get(con, lead_id)
    if row and row["tg_card_id"]:
        telegram.call("editMessageReplyMarkup", chat_id=TELEGRAM_CHAT_ID, message_id=row["tg_card_id"],
                      reply_markup=buttons(row))


def send_new_cards(con, limit: int | None = None) -> int:
    """Post cards for fresh fit-60+ drafts, best first. Lower ones wait (nothing is dropped)."""
    if not telegram.enabled():
        return 0
    rows = con.execute("SELECT * FROM leads WHERE kind='prospect' AND score>=? AND (status IN ('drafted','fu_drafted') "
                       "OR (status IN ('to_approve','fu_to_approve') AND tg_card_id IS NULL)) "  # restart cut-offs
                       "ORDER BY score DESC, created_at LIMIT ?",
                       (PHONE_MIN, MAX_CARDS_PER_CYCLE if limit is None else limit)).fetchall()
    sent = 0
    for row in rows:
        waiting = WAIT_OF.get(row["status"], row["status"])
        store.update(con, row["id"], status=waiting)  # before posting: a crash can't post the card twice
        try:
            card = _post(con, store.get(con, row["id"]))
        except Exception as ex:  # noqa: BLE001 — one bad card never blocks the rest
            store.update(con, row["id"], status=DRAFTED_OF[waiting], data={**_data(row), "card_error": str(ex)[:300]})
            continue
        store.update(con, row["id"], tg_card_id=card, tg_draft_id=None)
        sent += 1
    return sent


def _approve(con, row, by: str = "owner") -> None:
    data = _data(row)
    d = data.get("draft") or {}
    now = time.time()
    d.update(approved_at=now, approved_by=by)
    data["draft"] = d
    con.execute("UPDATE drafts SET approved_by=?, approved_at=? WHERE id=?", (by, now, d.get("draft_id")))
    if row["status"] == FU_WAITING:
        _fu_status(con, row["id"], "approved")
    store.update(con, row["id"], status=APPROVE_OF[row["status"]], data=data)


def _fu_status(con, lead_id: str, status: str) -> None:
    """Move the lead's open follow-up row (drafted / approved) to `status`."""
    con.execute("UPDATE followups SET status=? WHERE lead_id=? AND status IN ('drafted','approved')",
                (status, lead_id))
    con.commit()


def mark_sent(con, row, by: str = "owner") -> None:
    """A person sent the pitch or follow-up by hand (Telegram ✅ I sent it, or the dashboard; `by` = who).
    The automatic sender records its own emails (with their Message-ID) and never calls this."""
    data = _data(row)
    d = data.get("draft") or {}
    if d.get("followup"):  # also covers a follow-up handed over after an uncertain send (needs_manual)
        _fu_status(con, row["id"], "sent")
    channel = d.get("channel") or "email"
    addr = ((data.get("contacts") or {}).get("email") or "").lower() if channel == "email" else ""
    sender._record(con, row, "out", "by hand", addr, d.get("subject", ""), d.get("body", ""), "", channel)
    data["last_sent_at"] = time.time()
    store.update(con, row["id"], status="sent", data=data)


def tap(con, action: str, lead_id: str, message_id=None, who: str = "owner") -> str:
    """A prospect-card button was pressed — on Telegram, or on the dashboard (`who` = the logged-in user).
    Returns the short note to show."""
    row = store.get(con, lead_id)
    if not row or row["kind"] != "prospect":
        return "Unknown lead"
    status = row["status"]
    if message_id is not None and message_id != row["tg_card_id"] and action in ("pok", "pedit"):
        return "That's an old version — use the newest card"
    if action == "pok":
        if status not in WAITINGS:
            return "Not waiting for approval"
        _approve(con, row, by=who)
        note = "🔒 Approved"
    elif action == "pedit":
        return ("Reply to this card with what to change, or TEXT: + your own words" if status in WAITINGS
                else "Only waiting drafts can be edited")
    elif action == "pskip":
        if status in (FU_WAITING, FU_APPROVED):  # skipping a follow-up stops follow-ups, not the lead
            _fu_status(con, lead_id, "skipped")
            data = _data(row)
            data["followups_stopped"] = True
            store.update(con, lead_id, status="sent", data=data)
            note = "Follow-ups stopped"
        elif status in (WAITING, APPROVED):
            store.update(con, lead_id, status="skipped")
            note = "Skipped"
        else:
            return "Not possible now"
    elif action == "psent":
        if status not in APPROVEDS:
            return "Approve it first"
        mark_sent(con, row, by=who)
        note = "Marked as sent"
    else:
        return "Not possible now"
    store.log(con, who, {"pok": "approved", "pskip": "skipped", "psent": "marked sent"}.get(action, action), lead_id)
    try:
        rebutton(con, lead_id)  # keeps the Telegram card in step with a dashboard tap
    except Exception:  # noqa: BLE001 — the change is saved; an old/unreachable card can't block it
        pass
    return note


def _replace_card(con, old_card, lead_id: str) -> None:
    """Post the new version as a fresh card; the old card loses its buttons."""
    row = store.get(con, lead_id)
    new = _post(con, row)
    store.update(con, lead_id, tg_card_id=new)
    if old_card:
        version = (_data(row).get("draft") or {}).get("version", "")
        try:
            telegram.call("editMessageReplyMarkup", chat_id=TELEGRAM_CHAT_ID, message_id=old_card,
                          reply_markup={"inline_keyboard": [[{"text": f"• replaced by v{version} •",
                                                              "callback_data": f"noop:{lead_id}"}]]})
        except Exception:  # noqa: BLE001 — an old card may be uneditable
            pass


def reply(con, row, text: str, ask=None) -> None:
    """Owner replied to a card: TEXT:… = own words (checked, kept with warnings), else an edit comment."""
    card = row["tg_card_id"]
    if row["status"] not in WAITINGS:
        telegram.say("🔒 Only drafts waiting for approval can be changed.", reply_to_message_id=card)
        return
    data = _data(row)
    channel = (data.get("draft") or {}).get("channel") or pitch.channel_of(data.get("contacts") or {})
    if text[:5].upper() == "TEXT:":
        own = text[5:].strip()
        subject = (data.get("draft") or {}).get("subject", "")
        if own.lower().startswith("subject:") and "\n" in own:
            subject, own = own.split("\n", 1)
            subject = subject[8:].strip()
        d = pitch._clean({"subject": subject, "body": own}, channel)
        rule, fu = "", int((data.get("draft") or {}).get("followup") or 0)
        pitch._save(con, row, data, d, channel, pitch.problems(d, channel, row, data, followup=bool(fu)),
                    "owner's own text", followup=fu)
    else:
        telegram.say("✏️ Rewriting…", reply_to_message_id=card)
        try:
            rule = pitch.revise(con, row["id"], text, ask=ask).get("learned_rule", "")
        except Exception as ex:  # noqa: BLE001 — usage limits, refusals: the old version stays
            telegram.say(f"⏸ Can't rewrite right now: {str(ex)[:200]}", reply_to_message_id=card)
            return
    waiting = row["status"]
    store.update(con, row["id"], status=waiting)  # _save set (fu_)drafted; the card is already on the phone
    try:
        _replace_card(con, card, row["id"])
    except Exception as ex:  # noqa: BLE001 — card not posted: unlink the old one so it can't approve unseen text
        store.update(con, row["id"], status=DRAFTED_OF[waiting], tg_card_id=None)
        telegram.say(f"⚠️ New version saved but the card failed ({str(ex)[:150]}); it comes back next cycle.")
        return
    if rule:
        telegram.say(f"📝 Learned for future pitches: {rule}")


def _tier(source: str, reg: dict) -> str:
    e = reg.get(source)
    return e["tier"] if e else registry.default_tier(source)


def approve_all(con, reg: dict | None = None) -> tuple[int, int]:
    """Approve every waiting card that is safe to bulk-approve. Returns (approved, left for one-by-one)."""
    if reg is None:
        try:
            reg = registry.load()
        except Exception:  # noqa: BLE001 — unreadable config: fall back to name-based tiers
            reg = {}
    rows = con.execute("SELECT * FROM leads WHERE kind='prospect' AND status IN (?,?) AND tg_card_id IS NOT NULL",
                       (WAITING, FU_WAITING)).fetchall()  # only cards the owner could actually see
    done = held = 0
    for row in rows:
        warnings = (_data(row).get("draft") or {}).get("warnings")
        if _tier(row["source"], reg) == "manual" or warnings or (row["score"] or 0) < PHONE_MIN:
            held += 1
            continue
        _approve(con, row, by="owner (approve all)")
        try:
            rebutton(con, row["id"])
        except Exception:  # noqa: BLE001 — an old card may be uneditable; the status still counts
            pass
        done += 1
    return done, held
