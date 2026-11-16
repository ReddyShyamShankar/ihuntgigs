"""LinkedIn job-alert emails → Hunt leads.

LinkedIn emails the owner new jobs for the alerts they set up. A Gmail filter files those emails
under one label (default "Hunt/LinkedIn Jobs") and skips the inbox, so the phone never buzzes.
Hunt reads that label over IMAP with a Gmail app password, turns each job into a lead, and marks
the email as read only once every job in it has been handled (card sent, or judged not a fit).
Reading never marks anything read by itself (BODY.PEEK).
"""
import email
import email.header
import html
import imaplib
import json
import os
import re
import time

from .filters import markets_in
from .sources import _lead

HOST = "imap.gmail.com"
LABEL = os.environ.get("HUNT_GMAIL_LABEL", "Hunt/LinkedIn Jobs")
JOB_ID = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
PENDING_KEY = "gmail_pending"  # meta: {email uid: [lead ids]}


def enabled() -> bool:
    return bool(os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"))


def _connect() -> imaplib.IMAP4_SSL:
    m = imaplib.IMAP4_SSL(HOST, timeout=60)
    m.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
    typ, _ = m.select(f'"{LABEL}"')
    if typ != "OK":
        raise RuntimeError(f'Gmail label "{LABEL}" not found')
    return m


def _decode(value: str) -> str:
    return str(email.header.make_header(email.header.decode_header(value or "")))


def _parts(msg) -> tuple[str, str]:
    plain = rich = ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        body = part.get_payload(decode=True) or b""
        text = body.decode(part.get_content_charset() or "utf-8", "replace")
        if ctype == "text/plain":
            plain += text
        else:
            rich += text
    return plain, rich


def parse_jobs(plain: str, rich: str) -> list[dict]:
    """Pull (title, company, location, url) for each job in a LinkedIn alert email."""
    jobs, seen = [], set()
    # Plain-text part: "<title>\n<company>\n<location>\nView job: <url>"
    lines = [l.strip() for l in plain.splitlines()]
    for i, line in enumerate(lines):
        m = JOB_ID.search(line)
        if not m or m.group(1) in seen:
            continue
        before = [l for l in lines[max(0, i - 6):i] if l and not l.startswith(("View job", "http", "---"))][-3:]
        if not before:
            continue
        title = before[0] if len(before) < 3 else before[-3]
        company = before[1] if len(before) == 3 else (before[-1] if len(before) == 2 else "")
        location = before[-1] if len(before) == 3 else ""
        seen.add(m.group(1))
        jobs.append({"id": m.group(1), "title": title, "company": company, "location": location})
    # HTML fallback: the link text of each job anchor is its title.
    if not jobs and rich:
        for m in re.finditer(r'<a[^>]+href="([^"]*jobs/view/(\d+)[^"]*)"[^>]*>(.*?)</a>', rich, re.S | re.I):
            jid, text = m.group(2), html.unescape(re.sub(r"<[^>]+>|\s+", " ", m.group(3))).strip()
            if jid in seen or len(text) < 4:
                continue
            seen.add(jid)
            jobs.append({"id": jid, "title": text, "company": "", "location": ""})
    for j in jobs:
        j["url"] = f"https://www.linkedin.com/jobs/view/{j['id']}/"
    return jobs


def collect(con=None) -> list[dict]:
    """Leads from unread alert emails in the label. Remembers which email each lead came from."""
    if not enabled():
        return []
    m = _connect()
    try:
        since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - 14 * 86400))
        _, data = m.uid("search", None, f"(UNSEEN SINCE {since})")
        uids = data[0].split() if data and data[0] else []
        leads, pending = [], {}
        for uid in uids[-50:]:
            _, got = m.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in got if isinstance(p, tuple)), b"")
            msg = email.message_from_bytes(raw)
            subject = _decode(msg.get("Subject", ""))
            plain, rich = _parts(msg)
            hint = markets_in(subject) or ["any"]
            ids = []
            for j in parse_jobs(plain, rich):
                summary = (f"LinkedIn job alert ({subject}). Company: {j['company'] or 'see link'}. "
                           f"Location: {j['location'] or 'see link'}.")
                lead = _lead("linkedin_alerts", "job", j["title"], j["url"], summary, j["company"],
                             msg.get("Date", ""))
                lead["market_hint"] = hint
                leads.append(lead)
                ids.append(lead["id"])
            pending[uid.decode()] = {"ids": ids, "t": time.time()}
        if con is not None:
            from . import store
            old = json.loads(store.meta_get(con, PENDING_KEY, "{}"))
            for k, v in pending.items():
                old.setdefault(k, v)  # keep the first-seen time
            store.meta_set(con, PENDING_KEY, json.dumps(old))
        return leads
    finally:
        m.logout()


def mark_done(con) -> int:
    """Mark alert emails read once none of their jobs is still waiting for the AI (or after 3 days)."""
    from . import store
    pending = json.loads(store.meta_get(con, PENDING_KEY, "{}"))
    if not pending or not enabled():
        return 0
    done = []
    for uid, item in pending.items():
        rows = [store.get(con, i) for i in item["ids"]]
        stale = time.time() - item["t"] > 3 * 86400  # Hunt stops trying queued leads after 3 days
        if stale or all(r is None or r["status"] != "queued" for r in rows):
            done.append(uid)
    if done:
        m = _connect()
        try:
            for uid in done:
                m.uid("store", uid, "+FLAGS", "(\\Seen)")
        finally:
            m.logout()
        for uid in done:
            pending.pop(uid, None)
        store.meta_set(con, PENDING_KEY, json.dumps(pending))
    return len(done)
