"""Tiny Telegram Bot API client: sends lead cards with buttons and reads your taps/replies."""
import html
import json
import re
import urllib.parse
import urllib.request

import yaml

from .config import DATA, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

PROOF = {p["id"]: p["title"] for p in yaml.safe_load((DATA / "portfolio.yaml").read_text())["projects"]}

ICON = {"funding": "💰", "job": "🧑‍💼", "ask": "🙋"}


def enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def call(method: str, *, http_timeout: int = 70, **params):  # params go to Telegram (incl. its own "timeout")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=http_timeout) as r:
        body = json.load(r)
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body}")
    return body["result"]


LABEL = {"approved": "🚀 QUEUED FOR AUTO-APPLY", "applying": "⏳ APPLYING…", "applied": "✅ APPLIED",
         "needs_you": "👋 NEEDS YOU"}


def buttons(lead_id: str, status: str = "to_phone", url: str = "", mail: str = "") -> dict:
    if status == "to_phone":
        rows = [[("🚀 Auto-apply", "approve"), ("⏭ Skip", "skipped")], [("✅ I sent it myself", "sent")]]
    elif status in ("approved", "needs_you"):
        rows = [[(f"• {LABEL[status]} •", "noop")], [("✅ I sent it myself", "sent"), ("⏭ Skip", "skipped")]]
    elif status == "applying":
        rows = [[(f"• {LABEL[status]} •", "noop")]]
    else:
        rows = [[(f"• {LABEL.get(status, status.upper())} •", "noop")],
                [("💬 Replied", "replied"), ("🏆 Won", "won"), ("❌ Lost", "lost")]]
    kb = [[{"text": t, "callback_data": f"{a}:{lead_id}"} for t, a in row] for row in rows]
    links = []
    url = _safe_url(url)[0]
    if url:
        links.append({"text": "Open ↗", "url": url})
    if mail:
        links.append({"text": "✉️ Email draft", "url": mail})
    if links:
        kb.append(links)
    return {"inline_keyboard": kb}


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SKIP_EMAIL = re.compile(r"(noreply|no-reply|donotreply|example\.(com|org)|\.(png|jpg|gif)$)", re.I)


def lead_emails(lead: dict) -> list[str]:
    """Email addresses that appear in the lead itself (never ones the AI made up)."""
    text = f"{lead.get('summary', '')} {lead.get('title', '')}"
    out = []
    for e in EMAIL_RE.findall(text):
        if not SKIP_EMAIL.search(e) and e.lower() not in out:
            out.append(e.lower())
    return out[:2]


def gmail_compose(lead: dict, d: dict) -> str:
    """Prefilled Gmail draft link; the owner reviews and taps Send in Gmail."""
    to = lead_emails(lead)
    if not to:
        return ""
    q = urllib.parse.urlencode({"view": "cm", "fs": "1", "to": ",".join(to),
                                "su": _clip(d.get("email_subject"), 150), "body": _clip(d.get("email_body"), 1500)})
    return "https://mail.google.com/mail/?" + q


MAX_TEXT = 3900  # Telegram's limit is 4096 characters per message


def _clip(s, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"


def _safe_url(url: str) -> tuple[str, str]:
    """Only http(s) links are clickable; returns (url or '', domain)."""
    p = urllib.parse.urlparse(str(url or ""))
    if p.scheme in ("http", "https") and p.netloc:
        return url, p.netloc.lower()
    return "", "unknown source"


def card_text(lead: dict, d: dict) -> str:
    e = html.escape
    dot = "🟢" if d["score"] >= 75 else "🟡"
    url, domain = _safe_url(lead["url"])
    warn = "".join(f"⚠️ {e(_clip(w, 200))}\n" for w in d.get("_warnings", [])[:4])
    link = f"<a href=\"{e(url)}\">Open source ↗</a> ({e(domain)})" if url else f"(no safe link · {e(domain)})"
    return (f"{dot} <b>{d['score']}/100</b> · {ICON.get(lead['kind'], '•')} {e(lead['kind'].upper())} · "
            f"{e(_clip(d['market'], 30))}\n<b>{e(_clip(d['company'] or lead['company'], 120))}</b>: {e(_clip(d['signal'], 120))}\n\n"
            f"{warn}"
            f"<b>Why now:</b> {e(_clip(d['why_now'], 400))}\n<b>Pitch:</b> {e(_clip(d['offer'], 300))}\n"
            f"<b>Contact:</b> {e(_clip(d['contact_hint'], 300))}\n"
            f"<b>Proof:</b> {e(_clip('; '.join(PROOF.get(i, i) for i in d['proof_ids'][:4]), 400))}\n"
            f"{link}")


def draft_text(d: dict) -> str:
    return _clip(f"Subject: {_clip(d['email_subject'], 200)}\n\n{_clip(d['email_body'], 1800)}\n\n— — —\n"
                 f"DM version:\n{_clip(d['dm'], 800)}\n\n"
                 f"(Long-press to copy. Check every link and email before sending. "
                 f"Reply to this message with changes, e.g. 'shorter'.)", MAX_TEXT)


def send_lead(lead: dict, d: dict) -> tuple[int, int]:
    card = call("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=card_text(lead, d), parse_mode="HTML",
                disable_web_page_preview=True, reply_markup=buttons(lead["id"], url=lead["url"], mail=gmail_compose(lead, d)))
    draft = call("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=draft_text(d),
                 reply_to_message_id=card["message_id"])
    return card["message_id"], draft["message_id"]


def say(text: str, **extra) -> dict:
    return call("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=text, **extra)
