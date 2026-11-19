"""B9a dashboard (PRD §14, §21): a small website on the server, standard library only.

Screens: Pipeline (home) · Leads · Lead detail · Needs manual · Users (owner) · Activity (owner).
Login: pick your name + the 6-digit authenticator code (TOTP). No email accounts, no passwords.
  - one secret / QR code per person; the owner creates users, shows QR codes, resets and revokes them,
  - 5 wrong codes → locked 15 minutes; a code works once; sessions last HUNT_SESSION_HOURS (5–24, default 24),
  - revoke = that person's QR stops working and their open sessions end at once,
  - every login, failed attempt and action goes to the activity log with who did it.
Sync with Telegram: approve / skip / edit here use the same functions as the Telegram buttons and redraw the
Telegram card; a tap on Telegram shows here on the next page load.

Run:  python -m hunt dashboard            (listens on HUNT_DASH_HOST:HUNT_DASH_PORT, default 127.0.0.1:8090)
      python -m hunt adduser NAME --owner  (prints a one-time setup link to scan the QR code)
Put it behind HTTPS (e.g. Caddy) before opening it to the internet and set HUNT_DASH_HTTPS=1.
"""
import hashlib
import html
import json
import os
import re
import secrets
import time
import urllib.parse
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from . import contacts, followups, outreach, pitch, registry, sender, settings, store, totp

SESSION_HOURS = max(5, min(24, int(os.environ.get("HUNT_SESSION_HOURS", "24"))))
HTTPS = os.environ.get("HUNT_DASH_HTTPS", "0") == "1"
TZ = ZoneInfo(os.environ.get("HUNT_TZ", "UTC"))
MAX_FAILS, LOCK_SECONDS, SETUP_HOURS = 5, 15 * 60, 24
COOKIE = "hunt_session"
PAGE = 50
STAGES = ["found", "researched", "drafted", "approved", "sent", "replied", "call_booked", "won"]
SIDE = ["needs_manual", "skipped", "lost"]
STAGE_LABEL = {"found": "Found", "researched": "Researched", "drafted": "Drafted", "approved": "Approved",
               "sent": "Sent", "replied": "Replied", "call_booked": "Call booked", "won": "Won",
               "needs_manual": "Needs manual", "skipped": "Skipped", "lost": "Lost"}
HISTORY_LABEL = {"new": "New", "contacted": "Previously contacted", "replied": "Replied",
                 "past_client": "Past client", "unsubscribed": "Unsubscribed"}
MANUAL = ("needs_manual", "bounced")
EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def when(ts) -> str:
    return datetime.fromtimestamp(float(ts), TZ).strftime("%d %b %H:%M") if ts else ""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------- users & sessions ----------

def add_user(con, name: str, role: str = "member") -> tuple[int, str]:
    """Create a user. Returns (user id, one-time setup token for the QR page)."""
    name = name.strip()[:40]
    if not name:
        raise ValueError("name is empty")
    cur = con.execute("INSERT INTO users (name,secret,role,created_at) VALUES (?,?,?,?)",
                      (name, totp.new_secret(), "owner" if role == "owner" else "member", time.time()))
    con.commit()
    return cur.lastrowid, setup_token(con, cur.lastrowid)


def setup_token(con, uid: int) -> str:
    token = secrets.token_urlsafe(24)
    store.meta_set(con, "setup:" + _hash(token), json.dumps({"uid": uid, "exp": time.time() + SETUP_HOURS * 3600}))
    return token


def _setup_user(con, token: str):
    raw = store.meta_get(con, "setup:" + _hash(token))
    if not raw:
        return None
    d = json.loads(raw)
    u = con.execute("SELECT * FROM users WHERE id=? AND active=1", (d["uid"],)).fetchone()
    return u if u and d["exp"] > time.time() else None


def _drop_setup_tokens(con, uid: int) -> None:
    for r in con.execute("SELECT k, v FROM meta WHERE k LIKE 'setup:%'").fetchall():
        if json.loads(r["v"]).get("uid") == uid:
            con.execute("DELETE FROM meta WHERE k=?", (r["k"],))
    con.commit()


def login(con, name: str, code: str, now: float | None = None) -> tuple[str, str]:
    """Returns (session token, "") on success, or ("", message)."""
    now = time.time() if now is None else now
    u = con.execute("SELECT * FROM users WHERE name=? COLLATE NOCASE", (name.strip(),)).fetchone()
    if not u or not u["active"]:
        store.log(con, name.strip()[:40] or "?", "login failed", "", "unknown or revoked user")
        return "", "Wrong name or code."
    if u["locked_until"] > now:
        store.log(con, u["name"], "login blocked", "", "locked")
        return "", f"Too many wrong codes. Try again in {int((u['locked_until'] - now) // 60) + 1} min."
    counter = totp.verify(u["secret"], code, u["last_counter"], now)
    # Claim the code atomically: only one of two simultaneous logins with the same code can move last_counter.
    if counter >= 0 and con.execute(
            "UPDATE users SET fails=0, locked_until=0, last_counter=? WHERE id=? AND active=1 AND secret=? "
            "AND last_counter<? AND locked_until<=?", (counter, u["id"], u["secret"], counter, now)).rowcount != 1:
        counter = -1
    if counter < 0:
        # count in SQL (not from the row read above) so simultaneous wrong guesses all count toward the lock
        con.execute("UPDATE users SET fails=fails+1 WHERE id=?", (u["id"],))
        locked = con.execute("UPDATE users SET fails=0, locked_until=? WHERE id=? AND fails>=?",
                             (now + LOCK_SECONDS, u["id"], MAX_FAILS)).rowcount == 1
        con.commit()
        store.log(con, u["name"], "login failed", "", "locked 15 min" if locked else "wrong code")
        return "", ("Too many wrong codes. Locked for 15 minutes." if locked else "Wrong name or code.")
    token = secrets.token_urlsafe(32)
    con.execute("INSERT INTO sessions (token_hash,user_id,expires,created_at) VALUES (?,?,?,?)",
                (_hash(token), u["id"], now + SESSION_HOURS * 3600, now))
    con.commit()
    _drop_setup_tokens(con, u["id"])  # the QR code has been scanned and used
    store.log(con, u["name"], "login")
    return token, ""


def session_user(con, token: str):
    if not token:
        return None
    return con.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? "
                       "AND s.expires>? AND u.active=1", (_hash(token), time.time())).fetchone()


def revoke(con, uid: int, who: str) -> None:
    con.execute("UPDATE users SET active=0, revoked_at=? WHERE id=?", (time.time(), uid))
    con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    con.commit()
    _drop_setup_tokens(con, uid)
    store.log(con, who, "revoked user", str(uid))


def reset_qr(con, uid: int, who: str) -> str:
    """New secret (old QR stops working, sessions end). Returns a fresh setup token."""
    con.execute("UPDATE users SET secret=?, last_counter=-1, fails=0, locked_until=0, active=1, revoked_at=NULL "
                "WHERE id=?", (totp.new_secret(), uid))
    con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    con.commit()
    _drop_setup_tokens(con, uid)  # an older setup link must not reveal the new QR
    store.log(con, who, "reset QR", str(uid))
    return setup_token(con, uid)


# ---------- page parts ----------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--mute:#667085;--line:#e4e7ec;--acc:#1f6feb;--ok:#13804b;--warn:#b54708;--bad:#b42318}
@media (prefers-color-scheme: dark){:root{--bg:#0e1116;--card:#161b22;--ink:#e6edf3;--mute:#8b949e;--line:#30363d;--acc:#58a6ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:var(--acc);text-decoration:none}header{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:center;padding:10px 16px;
background:var(--card);border-bottom:1px solid var(--line)}header b{margin-right:8px}header .me{margin-left:auto;color:var(--mute)}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:20px;margin:4px 0 14px}h2{font-size:16px;margin:22px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.pipe{display:flex;gap:8px;overflow-x:auto;padding-bottom:6px}.stage{flex:1 0 96px;text-align:center;background:var(--card);
border:1px solid var(--line);border-radius:10px;padding:12px 6px;color:var(--ink)}.stage .n{font-size:24px;font-weight:700;display:block}
.stage small{color:var(--mute)}.arrow{align-self:center;color:var(--mute)}.side{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid var(--line);font-size:12px;color:var(--mute)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:12px;color:var(--mute);font-weight:600}
.scroll{overflow-x:auto}form.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
input,select,textarea,button{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
button{background:var(--acc);color:#fff;border-color:var(--acc);cursor:pointer}button.ghost{background:transparent;color:var(--ink)}
button.bad{background:var(--bad);border-color:var(--bad)}textarea{width:100%;min-height:70px}
pre{white-space:pre-wrap;font:inherit;margin:6px 0}.mute{color:var(--mute)}.warn{color:var(--warn)}.err{color:var(--bad)}.ok{color:var(--ok)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.login{max-width:360px;margin:12vh auto}.login input{width:100%;margin-bottom:10px}
#qr{background:#fff;padding:12px;display:inline-block;border-radius:8px}
"""

QR_JS = """document.addEventListener('DOMContentLoaded',function(){var el=document.getElementById('qr');
if(el&&window.QRCode){new QRCode(el,{text:el.dataset.uri,width:220,height:220});}});"""
QR_LIB = "https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"
QR_SCRIPTS = f"<script src='{QR_LIB}'></script><script>{QR_JS}</script>"


def layout(title: str, body: str, user=None, scripts: str = "") -> str:
    menu = ""
    if user:
        links = [("/", "Pipeline"), ("/leads", "Leads"), ("/manual", "Needs manual"), ("/metrics", "Metrics")]
        if user["role"] == "owner":
            links += [("/sources", "Sources"), ("/settings", "Settings"), ("/rules", "Style rules"),
                      ("/users", "Users"), ("/activity", "Activity")]
        menu = (f"<header><b>Hunt</b>{''.join(f'<a href={e(h)}>{e(t)}</a>' for h, t in links)}"
                f"<span class=me>{e(user['name'])}</span><form method=post action=/logout>"
                f"<button class=ghost>Log out</button></form></header>")
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
            f"initial-scale=1'><title>{e(title)} · Hunt</title><style>{CSS}</style>{scripts}</head><body>{menu}"
            f"<main>{body}</main></body></html>")


def _opts(name: str, values, current: str, label: str) -> str:
    o = f"<option value=''>{e(label)}</option>" + "".join(
        f"<option value='{e(v)}'{' selected' if v == current else ''}>{e(t)}</option>" for v, t in values)
    return f"<select name={name} onchange='this.form.submit()'>{o}</select>"


def _qr_block(u) -> str:
    uri = totp.uri(u["secret"], u["name"])
    return (f"<div class=card><h1>Authenticator for {e(u['name'])}</h1>"
            f"<p>Scan this with Google Authenticator, Microsoft Authenticator or 1Password. One QR per person — "
            f"don't share it.</p><div id=qr data-uri='{e(uri)}'></div>"
            f"<p class=mute>On the phone itself: <a href='{e(uri)}'>open in authenticator app</a>. "
            f"Or type this key: <code>{e(u['secret'])}</code></p>"
            f"<p><a href=/login>Then log in →</a></p></div>")


# ---------- screens ----------

def home(con, user) -> str:
    counts = {r["stage"]: r["n"] for r in con.execute("SELECT stage, COUNT(*) n FROM leads GROUP BY stage")}
    tiles = "<span class=arrow>→</span>".join(
        f"<a class=stage href='/leads?stage={s}'><span class=n>{counts.get(s, 0)}</span><small>{STAGE_LABEL[s]}</small></a>"
        for s in STAGES)
    side = "".join(f"<a class=pill href='/leads?stage={s}'>{STAGE_LABEL[s]}: {counts.get(s, 0)}</a>" for s in SIDE)
    waiting = con.execute("SELECT COUNT(*) FROM leads WHERE status IN ('to_approve','fu_to_approve')").fetchone()[0]
    manual_n = con.execute("SELECT COUNT(*) FROM leads WHERE status IN ('needs_manual','bounced')").fetchone()[0]
    replies = con.execute("SELECT COUNT(*) FROM leads WHERE status='replied'").fetchone()[0]
    todo = (f"<div class=card><h2 style='margin-top:0'>Waiting for you</h2>"
            f"<p><a href='/leads?status=to_approve'>{waiting} draft(s) to approve</a> · "
            f"<a href=/manual>{manual_n} need a contact or a check</a> · "
            f"<a href='/leads?status=replied'>{replies} replied</a></p></div>")
    return layout("Pipeline", f"<h1>Pipeline</h1>{todo}<div class=pipe>{tiles}</div><div class=side>{side}</div>", user)


def _lead_filters(q: dict) -> tuple[str, list]:
    where, args = ["1=1"], []
    if q.get("stage"):
        where.append("l.stage=?")
        args.append(q["stage"])
    if q.get("status"):
        statuses = {"to_approve": ("to_approve", "fu_to_approve")}.get(q["status"], (q["status"],))
        where.append(f"l.status IN ({','.join('?' * len(statuses))})")
        args.extend(statuses)
    if q.get("source"):
        where.append("l.source=?")
        args.append(q["source"])
    if q.get("lane"):
        where.append("(',' || COALESCE(l.lanes,'') || ',') LIKE ?")
        args.append(f"%,{q['lane']},%")
    if q.get("channel"):
        where.append("json_extract(l.data,'$.contacts.route')=?")
        args.append(q["channel"])
    if q.get("history"):
        where.append("COALESCE(p.history,'new')=?")
        args.append(q["history"])
    if q.get("q"):
        where.append("(l.title LIKE ? OR l.company LIKE ?)")
        args.extend([f"%{q['q']}%"] * 2)
    return " AND ".join(where), args


def leads(con, user, q: dict) -> str:
    where, args = _lead_filters(q)
    try:
        page = max(0, int(q.get("page") or 0))
    except ValueError:
        page = 0
    base = f"FROM leads l LEFT JOIN prospects p ON p.id=l.prospect_id WHERE {where}"
    total = con.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
    rows = con.execute(f"SELECT l.*, COALESCE(p.history,'new') history, json_extract(l.data,'$.contacts.route') route "
                       f"{base} ORDER BY l.updated_at DESC LIMIT ? OFFSET ?", (*args, PAGE, page * PAGE)).fetchall()
    sources = [(r[0], r[0]) for r in con.execute("SELECT DISTINCT source FROM leads ORDER BY source")]
    lanes = [(x, x) for x in ("writing", "signals", "design", "product", "development", "coaching")]
    chans = [(x, x) for x in ("email", "x", "instagram", "telegram", "linkedin", "whatsapp")]
    filt = (f"<form class=filters><input name=q placeholder='Search name' value='{e(q.get('q', ''))}'>"
            + _opts("stage", [(s, STAGE_LABEL[s]) for s in STAGES + SIDE], q.get("stage", ""), "Any stage")
            + _opts("source", sources, q.get("source", ""), "Any source")
            + _opts("lane", lanes, q.get("lane", ""), "Any lane")
            + _opts("channel", chans, q.get("channel", ""), "Any channel")
            + _opts("history", list(HISTORY_LABEL.items()), q.get("history", ""), "Any history")
            + (f"<input type=hidden name=status value='{e(q['status'])}'>" if q.get("status") else "")
            + "<button>Filter</button> <a href=/leads>Clear</a></form>")
    body = "".join(
        f"<tr><td><a href='/lead/{e(urllib.parse.quote(r['id']))}'>{e((r['company'] or r['title'] or '')[:70])}</a></td>"
        f"<td>{e(r['source'])}</td><td>{e(STAGE_LABEL.get(r['stage'], r['stage']))}<br><small class=mute>"
        f"{e(r['status'])}</small></td><td>{e(r['score'] if r['score'] is not None else '')}</td>"
        f"<td>{e(r['lanes'] or '')}</td><td>{e(r['route'] or '')}</td>"
        f"<td>{e(HISTORY_LABEL.get(r['history'], r['history']))}</td><td>{when(r['updated_at'])}</td></tr>" for r in rows)
    more = urllib.parse.urlencode({**{k: v for k, v in q.items() if v and k != "page"}, "page": page + 1})
    nav = (f"<p class=mute>{total} lead(s) · page {page + 1}"
           + (f" · <a href='/leads?{e(more)}'>next →</a>" if (page + 1) * PAGE < total else "") + "</p>")
    return layout("Leads", f"<h1>Leads</h1>{filt}<div class=scroll><table><tr><th>Name</th><th>Source</th><th>Stage"
                           f"</th><th>Fit</th><th>Lanes</th><th>Channel</th><th>History</th><th>Updated</th></tr>{body}"
                           f"</table></div>{nav}", user)


def _safe_links(pairs) -> str:
    return " · ".join(f"<a href='{e(u)}' target=_blank rel=noreferrer>{e(t)}</a>" for t, u in pairs
                      if str(u).startswith(("http://", "https://")))


def lead(con, user, lead_id: str, note: str = "") -> str:
    r = store.get(con, lead_id)
    if not r:
        return layout("Not found", "<h1>Lead not found</h1>", user)
    data = json.loads(r["data"] or "{}")
    c = data.get("contacts") or {}
    brief = con.execute("SELECT * FROM briefs WHERE lead_id=? ORDER BY created_at DESC", (lead_id,)).fetchone()
    drafts = con.execute("SELECT * FROM drafts WHERE lead_id=? ORDER BY version DESC", (lead_id,)).fetchall()
    msgs = con.execute("SELECT * FROM messages WHERE lead_id=? ORDER BY at", (lead_id,)).fetchall()
    lid = e(urllib.parse.quote(lead_id))
    actions = ""
    if r["status"] in outreach.WAITINGS or r["status"] in outreach.WAIT_OF:  # incl. drafts below the phone cut-off
        actions = (f"<div class=card><h2 style='margin-top:0'>Decide</h2><form method=post action='/lead/{lid}/act' "
                   f"class=row><button name=a value=pok>✅ Approve</button><button class=ghost name=a value=pskip>⏭ Skip"
                   f"</button></form><form method=post action='/lead/{lid}/act'><input type=hidden name=a value=edit>"
                   f"<p class=mute>Change it: write a comment (e.g. 'shorter'), or start with TEXT: for your own words."
                   f"</p><textarea name=text required></textarea><button>✏️ Rewrite</button></form></div>")
    elif r["status"] in outreach.APPROVEDS:
        actions = (f"<div class=card><form method=post action='/lead/{lid}/act' class=row><span>🔒 Approved.</span>"
                   f"<button name=a value=psent>✅ I sent it</button><button class=ghost name=a value=pskip>⏭ Skip"
                   f"</button></form></div>")
    warn = c.get("note") or data.get("send_note") or ""
    info = (f"<div class=card><h1>{e(r['company'] or r['title'])}</h1><p><span class=pill>"
            f"{e(STAGE_LABEL.get(r['stage'], r['stage']))}</span> <span class=pill>{e(r['status'])}</span> "
            f"<span class=pill>fit {e(r['score'] or 0)}</span> <span class=pill>{e(r['lanes'] or 'no lanes')}</span></p>"
            f"<p>{_safe_links([('Open source ↗', r['url'])])} · source {e(r['source'])}</p>"
            f"<p>Contact: {e(c.get('route') or '—')} {e(c.get('email') or '')} "
            f"{_safe_links((c.get('socials') or {}).items())}"
            + (f"<br><span class=warn>{e(warn)}</span>" if warn else "") + "</p></div>")
    b = ""
    if brief:
        b = "<div class=card><h2 style='margin-top:0'>Research brief</h2>" + "".join(
            f"<p><b>{k.replace('_', ' ').title()}:</b> {e(brief[k])}</p>" for k in
            ("who", "doing", "problem", "fit", "value", "why_me")) + f"<p class=mute>{when(brief['created_at'])}</p></div>"
    d = "".join(f"<div class=card><b>v{x['version']}</b> · {e(x['channel'])} · {when(x['created_at'])}"
                + (f" · edit: <i>{e(x['edit_comment'])}</i>" if x["edit_comment"] else "")
                + (f" · <span class=ok>approved by {e(x['approved_by'])} {when(x['approved_at'])}</span>"
                   if x["approved_by"] else "")
                + (f"<p><b>{e(x['subject'])}</b></p>" if x["subject"] else "") + f"<pre>{e(x['body'])}</pre></div>"
                for x in drafts)
    m = "".join(f"<div class=card><b>{'→ sent' if x['direction'] == 'out' else '← received'}</b> · {e(x['channel'])}"
                f" · {e(x['inbox'] or '')} {e(x['addr'] or '')} · {when(x['at'])}"
                + (f"<p><b>{e(x['subject'])}</b></p>" if x["subject"] else "") + f"<pre>{e(x['body'])}</pre></div>"
                for x in msgs)
    flash = f"<div class=card>{e(note)}</div>" if note else ""
    return layout(r["title"] or "Lead", flash + info + actions + b + "<h2>Drafts</h2>"
                  + (d or "<p class=mute>None yet.</p>") + "<h2>Messages</h2>" + (m or "<p class=mute>None yet.</p>"),
                  user)


def lead_action(con, user, lead_id: str, form: dict) -> str:
    a = form.get("a", "")
    row = store.get(con, lead_id)
    if not row:
        return "Lead not found."
    if a in ("pok", "pskip", "edit") and row["status"] in outreach.WAIT_OF:
        # a draft below the phone's fit cut-off never got a card: seeing it here counts as showing it
        store.update(con, lead_id, status=outreach.WAIT_OF[row["status"]])
        row = store.get(con, lead_id)
    if a in ("pok", "pskip", "psent"):
        return outreach.tap(con, a, lead_id, who=user["name"])
    if a == "edit" and form.get("text", "").strip():
        if row["status"] not in outreach.WAITINGS:
            return "Only drafts waiting for approval can be changed."
        store.log(con, user["name"], "edited draft", lead_id, form["text"][:200])
        outreach.reply(con, row, form["text"].strip())
        return "New version saved (and sent to Telegram)."
    return "Nothing done."


def manual(con, user, note: str = "") -> str:
    rows = con.execute("SELECT * FROM leads WHERE status IN ('needs_manual','bounced') ORDER BY score DESC, "
                       "updated_at DESC LIMIT 200").fetchall()
    items = []
    for r in rows:
        data = json.loads(r["data"] or "{}")
        c, d = data.get("contacts") or {}, data.get("draft") or {}
        why = ("Email bounced — find another contact" if r["status"] == "bounced"
               else data.get("send_note") or c.get("note") or "No contact found automatically")
        where = [("YouTube About page", r["url"].rstrip("/") + "/about")] if "youtube.com" in (r["url"] or "") else []
        if c.get("website"):
            where.append(("website", c["website"]))
        where += list((c.get("socials") or {}).items())
        lid = e(r["id"])
        items.append(
            f"<div class=card><label class=row><input type=checkbox name=ids value='{lid}'> "
            f"<b><a href='/lead/{e(urllib.parse.quote(r['id']))}'>{e(r['company'] or r['title'])}</a></b>"
            f"<span class=pill>fit {e(r['score'] or 0)}</span></label><p class=warn>{e(why)}</p><p>{_safe_links(where)}</p>"
            + (f"<details><summary>Ready draft</summary><pre>{e(d.get('body', ''))}</pre></details>" if d.get("body") else "")
            + f"<input name='contact_{lid}' placeholder='Paste email or profile link found' style='width:100%'></div>")
    body = ("<h1>Needs manual</h1><p class=mute>Tick the ones you handled, paste what you found, then push them back "
            "into the pipeline.</p>" + (f"<div class=card>{e(note)}</div>" if note else "")
            + "<form method=post action=/manual>" + ("".join(items) or "<p>Nothing waiting. 🎉</p>")
            + ("<div class='row card'><button name=a value=push>Push back with contact</button>"
               "<button class=ghost name=a value=sent>It was sent</button>"
               "<button class=bad name=a value=skip>Skip</button></div>" if items else "") + "</form>")
    return layout("Needs manual", body, user)


def apply_contact(data: dict, found: str) -> tuple[str, str]:
    """Put a pasted email / profile link into the lead's contacts. Returns (route, value) or ("", "")."""
    found = found.strip()
    c = data.setdefault("contacts", {})
    if EMAIL.match(found):
        c.update(email=found.lower(), route="email", email_from="owner")
        return "email", found.lower()
    if found.startswith(("http://", "https://")):
        nets = contacts.socials_in([found])
        if nets:
            net, url = next(iter(nets.items()))
            c.setdefault("socials", {})[net] = url
            c["route"] = net
            return net, url
    return "", ""


def manual_action(con, user, form: dict, ids: list[str]) -> str:
    a, done, skipped = form.get("a", ""), 0, 0
    for lid in ids:
        row = store.get(con, lid)
        if not row or row["status"] not in MANUAL:
            continue
        data = json.loads(row["data"] or "{}")
        if a == "push":
            route, value = apply_contact(data, form.get(f"contact_{lid}", ""))
            if not route:
                skipped += 1
                continue
            data.pop("send_note", None)
            researched = bool(data.get("research")) and not (data.get("research") or {}).get("skipped")
            # researched before → back to `researched` so the pitch is rewritten for this channel
            store.update(con, lid, status="researched" if researched else "prospect_ready", data=data)
            if row["prospect_id"]:
                store.prospect_add_handles(con, row["prospect_id"], {route: value})
        elif a == "sent":
            outreach.mark_sent(con, row, by=user["name"])
        elif a == "skip":
            store.update(con, lid, status="skipped")
        else:
            continue
        store.log(con, user["name"], f"manual {a}", lid, form.get(f"contact_{lid}", "")[:200])
        done += 1
    return f"{done} updated" + (f", {skipped} skipped (paste an email or a profile link)" if skipped else "") + "."


def users_page(con, user, note: str = "") -> str:
    rows = con.execute("SELECT * FROM users ORDER BY active DESC, name").fetchall()
    trs = "".join(
        f"<tr><td>{e(u['name'])}</td><td>{e(u['role'])}</td><td>{'active' if u['active'] else 'revoked'}"
        + (" · <span class=warn>locked</span>" if u["locked_until"] > time.time() else "") + "</td><td class=row>"
        + (f"<a href='/users/{u['id']}/qr'>Show QR</a>" if u["active"] else "")
        + f"<form method=post action='/users/{u['id']}/reset'><button class=ghost>New QR</button></form>"
        + (f"<form method=post action='/users/{u['id']}/revoke'><button class=bad>Revoke</button></form>"
           if u["active"] and u["id"] != user["id"] else "") + "</td></tr>" for u in rows)
    return layout("Users", (f"<div class=card>{e(note)}</div>" if note else "")
                  + f"<h1>Users</h1><table><tr><th>Name</th><th>Role</th><th>State</th><th></th></tr>{trs}</table>"
                    "<h2>Add a person</h2><form method=post action=/users class=row><input name=name required "
                    "maxlength=40 placeholder=Name><button>Create + show QR</button></form>", user)


def activity(con, user) -> str:
    rows = con.execute("SELECT * FROM activity ORDER BY at DESC LIMIT 300").fetchall()
    trs = "".join(f"<tr><td>{when(r['at'])}</td><td>{e(r['who'])}</td><td>{e(r['action'])}</td>"
                  f"<td>{e(r['target'])}</td><td>{e((r['detail'] or '')[:160])}</td></tr>" for r in rows)
    return layout("Activity", f"<h1>Activity</h1><div class=scroll><table><tr><th>When</th><th>Who</th><th>What</th>"
                              f"<th>Lead / user</th><th>Detail</th></tr>{trs}</table></div>", user)


# ---------- B9b: metrics, sources, settings, style rules ----------

FUNNEL = ["found", "researched", "drafted", "approved", "sent", "replied", "won"]


def _reached(con, days: int) -> list[tuple[dict, int]]:
    """(lead, furthest funnel step reached) — side exits (skipped, lost, needs manual) keep the credit for
    the steps they passed, judged from their drafts and messages."""
    cutoff = time.time() - days * 86400 if days else 0
    drafted = {r[0] for r in con.execute("SELECT DISTINCT lead_id FROM drafts")}
    approved = {r[0] for r in con.execute("SELECT DISTINCT lead_id FROM drafts WHERE approved_at IS NOT NULL")}
    sent = {r[0] for r in con.execute("SELECT DISTINCT lead_id FROM messages WHERE direction='out'")}
    out = []
    for r in con.execute("SELECT id, source, stage, stage_at, lanes, json_extract(data,'$.contacts.route') route "
                         "FROM leads WHERE created_at>=?", (cutoff,)):
        try:  # every stage the lead ever reached (kept by store.update), not just where it is now
            seen = set(json.loads(r["stage_at"] or "{}")) | {r["stage"]}
        except ValueError:
            seen = {r["stage"]}
        seen = {{"call_booked": "replied"}.get(s, s) for s in seen}
        i = max([FUNNEL.index(s) for s in seen if s in FUNNEL] or [0])
        for ids, step in ((drafted, "drafted"), (approved, "approved"), (sent, "sent")):
            if r["id"] in ids:
                i = max(i, FUNNEL.index(step))
        out.append((dict(r), i))
    return out


def metrics_data(con, days: int = 30) -> dict:
    """Funnel counts overall and split by source, lane and channel (PRD §3, §14)."""
    blank = lambda: [0] * len(FUNNEL)  # noqa: E731
    total, split = blank(), {"source": {}, "lane": {}, "channel": {}}
    for r, i in _reached(con, days):
        keys = {"source": [(r["source"] or "?").split(":")[0]],
                "lane": [x for x in (r["lanes"] or "").split(",") if x] or ["—"],
                "channel": [r["route"] or "—"]}
        for n in range(i + 1):
            total[n] += 1
        for dim, names in keys.items():
            for k in names:
                row = split[dim].setdefault(k, blank())
                for n in range(i + 1):
                    row[n] += 1
    return {"total": total, "split": split}


def _rate(row) -> str:
    s, rep = row[FUNNEL.index("sent")], row[FUNNEL.index("replied")]
    return f"{rep / s * 100:.0f}%" if s else "—"


def metrics(con, user, q: dict) -> str:
    try:
        days = int(q.get("days") or 30)
    except ValueError:
        days = 30
    m = metrics_data(con, days)
    t = m["total"]
    period = "".join(f"<a class=pill href='/metrics?days={d}'{' style=font-weight:700' if d == days else ''}>{lbl}</a> "
                     for d, lbl in ((7, "7 days"), (30, "30 days"), (90, "90 days"), (0, "All time")))
    head = "".join(f"<th>{STAGE_LABEL[s]}</th>" for s in FUNNEL) + "<th>Reply rate</th>"

    def table(title, rows):
        trs = "".join(f"<tr><td>{e(k)}</td>" + "".join(f"<td>{n}</td>" for n in v) + f"<td><b>{_rate(v)}</b></td></tr>"
                      for k, v in sorted(rows.items(), key=lambda kv: (-kv[1][FUNNEL.index('sent')], kv[0])))
        return (f"<h2>By {title}</h2><div class=scroll><table><tr><th>{title.title()}</th>{head}</tr>"
                f"{trs or '<tr><td colspan=9 class=mute>No leads yet.</td></tr>'}</table></div>")
    body = (f"<h1>Metrics</h1><p>{period}</p><div class=card><span class=n style='font-size:28px;font-weight:700'>"
            f"{_rate(t)}</span> reply rate <span class=mute>({t[FUNNEL.index('replied')]} replied of "
            f"{t[FUNNEL.index('sent')]} sent · good cold outreach ≈ 5%)</span></div>"
            f"<div class=scroll><table><tr>{head}</tr><tr>" + "".join(f"<td>{n}</td>" for n in t)
            + f"<td><b>{_rate(t)}</b></td></tr></table></div>"
            + table("source", m["split"]["source"]) + table("lane", m["split"]["lane"])
            + table("channel", m["split"]["channel"])
            + "<p class=mute>A lead counts at every step it passed. Leads found in the period.</p>")
    return layout("Metrics", body, user)


def sources_page(con, user, note: str = "") -> str:
    base, live = registry.load(overrides={}), registry.load()
    runs = {r["name"]: r for r in store.sources_list(con)}
    trs = []
    for name in sorted(live, key=lambda n: (live[n]["layer"], n)):
        b, s, run = base[name], live[name], runs.get(name, {})
        tiers = "".join(f"<option value={t}{' selected' if t == s['tier'] else ''}>{t}</option>"
                        for t in registry.TIERS[registry.TIERS.index(b["tier"]):])  # stricter only
        auto = (f"<select name=auto_send><option value=draft>Draft only</option><option value=on"
                f"{' selected' if s['auto_send'] == 'on' else ''}>Auto-send</option></select>"
                if s["tier"] == "open" else "<span class=mute>Draft only</span>")
        last = (f"{when(run['last_run'])} · {run.get('last_count') or 0} found"
                + (f"<br><span class=err>{e((run.get('last_error') or '')[:120])}</span>" if run.get("last_error") else "")
                if run.get("last_run") else "<span class=mute>not run yet</span>")
        trs.append(f"<tr><td><b>{e(name)}</b><br><small class=mute>layer {s['layer']} · {e(s['group'])}</small></td>"
                   f"<td><form method=post action=/sources class=row><input type=hidden name=name value='{e(name)}'>"
                   f"<label><input type=checkbox name=enabled value=on{' checked' if s['enabled'] else ''}> on</label>"
                   f"<select name=tier>{tiers}</select>{auto}<button class=ghost>Save</button></form></td>"
                   f"<td>{last}</td></tr>")
    return layout("Sources", (f"<div class=card>{e(note)}</div>" if note else "")
                  + "<h1>Sources</h1><p class=mute>Switch a source on or off, make its tier stricter, choose auto-send. "
                    "Only <b>open</b> sources can auto-send, and emails go out only once sending is switched on. "
                    "Platforms that ban automation stay manual.</p><div class=scroll><table><tr><th>Source</th>"
                    f"<th>Switches</th><th>Last run</th></tr>{''.join(trs)}</table></div>", user)


def sources_action(con, user, form: dict) -> str:
    name, live = form.get("name", ""), registry.load()
    if name not in live:
        return "Unknown source."
    saved = registry.saved_overrides()
    want = {"enabled": form.get("enabled") == "on", "tier": form.get("tier", live[name]["tier"]),
            "auto_send": "on" if form.get("auto_send") == "on" else "draft"}
    saved[name] = want
    store.meta_set(con, registry.DASH_KEY, json.dumps(saved))
    now = registry.load()[name]
    store.sync_sources(con, registry.load())
    store.log(con, user["name"], "changed source", name,
              f"{'on' if now['enabled'] else 'off'}, {now['tier']}, {now['auto_send']}")
    note = f"{name}: {'on' if now['enabled'] else 'off'} · {now['tier']} · " + \
           ("auto-send" if now["auto_send"] == "on" else "draft only")
    if want["tier"] != now["tier"] or want["auto_send"] != now["auto_send"]:
        note += " (a tier can only be made stricter; auto-send needs the open tier)"
    return note


def _current(con) -> dict:
    return {"approval_mode": settings.get(con, "approval_mode", "one"),
            "followups": bool(settings.get(con, "followups", followups.ON)),
            "followup_days": list(settings.get(con, "followup_days", followups.DAYS)),
            "followup_lanes": list(settings.get(con, "followup_lanes", sorted(followups.LANES))),
            "inbox_daily": int(settings.get(con, "inbox_daily", sender.PER_INBOX)),
            "active_lanes": list(settings.get(con, "active_lanes", list(settings.LANES))),
            "learn_edits": bool(settings.get(con, "learn_edits", pitch.LEARN))}


def _onoff(name: str, value: bool) -> str:
    return (f"<select name={name}><option value=on{' selected' if value else ''}>On</option>"
            f"<option value=off{'' if value else ' selected'}>Off</option></select>")


def _lane_boxes(prefix: str, chosen: list) -> str:
    return "".join(f"<label><input type=checkbox name={prefix}_{x} value=on{' checked' if x in chosen else ''}> {x}"
                   f"</label> " for x in settings.LANES)


def settings_page(con, user, note: str = "") -> str:
    c = _current(con)
    rows = [
        ("Approval mode", "<select name=approval_mode>" + "".join(
            f"<option value={v}{' selected' if v == c['approval_mode'] else ''}>{t}</option>"
            for v, t in (("one", "One-by-one"), ("bulk", "Bulk (/approveall allowed)"))) + "</select>"),
        ("Auto follow-ups", _onoff("followups", c["followups"])),
        ("Follow-up timing (days of silence)", f"<input name=followup_days value='{e(','.join(map(str, c['followup_days'])))}'>"),
        ("Follow-ups only for lanes <small class=mute>(none ticked = all)</small>", _lane_boxes("fl", c["followup_lanes"])),
        ("Daily email limit per inbox", f"<input name=inbox_daily type=number min=1 max=200 value={c['inbox_daily']}>"),
        ("Active lanes <small class=mute>(pitches are written only for these)</small>", _lane_boxes("al", c["active_lanes"])),
        ("Learn from edits", _onoff("learn_edits", c["learn_edits"])),
    ]
    fixed = [("Auto-send per source", "<a href=/sources>On the Sources screen</a>"),
             ("Signals outside India only", "On — fixed rule"),
             ("Send in prospect's business hours", "<span class=mute>Comes with real sending (after the inbox is set up)</span>"),
             ("Intent search phrases", "<span class=mute>Comes with Layer 2 intent search</span>")]
    trs = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
    trs2 = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in fixed)
    return layout("Settings", (f"<div class=card>{e(note)}</div>" if note else "")
                  + f"<h1>Settings</h1><form method=post action=/settings><table>{trs}</table>"
                    f"<p><button>Save settings</button></p></form><h2>Elsewhere / later</h2><table>{trs2}</table>", user)


def settings_action(con, user, form: dict) -> str:
    raw = {"approval_mode": form.get("approval_mode", ""), "followups": form.get("followups", ""),
           "followup_days": form.get("followup_days", ""), "inbox_daily": form.get("inbox_daily", ""),
           "learn_edits": form.get("learn_edits", ""),
           "followup_lanes": ",".join(x for x in settings.LANES if form.get(f"fl_{x}") == "on"),
           "active_lanes": ",".join(x for x in settings.LANES if form.get(f"al_{x}") == "on")}
    cur, changed, errors = _current(con), [], []
    for name, value in raw.items():
        try:
            v = settings.clean(name, value)
        except ValueError as ex:
            errors.append(str(ex) if "invalid literal" not in str(ex) else f"{name}: not a number")
            continue
        if v != cur[name]:
            settings.save(con, name, v, user["name"])
            changed.append(name.replace("_", " "))
    return ("Saved: " + ", ".join(changed) + "." if changed else "Nothing changed.") + \
           (" Not saved — " + "; ".join(errors) if errors else "")


def rules_page(con, user, note: str = "") -> str:
    rows = con.execute("SELECT * FROM style_rules ORDER BY active DESC, id DESC").fetchall()
    learn = _current(con)["learn_edits"]
    trs = "".join(
        f"<tr><td>{e(r['rule'])}</td><td>{'from an edit' if r['from_draft_id'] else 'added by hand'}<br>"
        f"<small class=mute>{when(r['created_at'])}</small></td><td><form method=post action=/rules>"
        f"<input type=hidden name=id value={r['id']}><button class={'bad' if r['active'] else 'ghost'} name=a "
        f"value={'off' if r['active'] else 'on'}>{'Switch off' if r['active'] else 'Switch on'}</button></form></td></tr>"
        for r in rows)
    return layout("Style rules", (f"<div class=card>{e(note)}</div>" if note else "")
                  + "<h1>Learned style rules</h1><p class=mute>Every new pitch follows the rules that are on. "
                    f"Learning from your edits is <b>{'on' if learn else 'off'}</b> (<a href=/settings>change</a>).</p>"
                    "<form method=post action=/rules class=row><input name=rule maxlength=200 required "
                    "placeholder='e.g. Never open with a compliment' style='flex:1'><button name=a value=add>Add rule"
                    f"</button></form><h2>Rules</h2><table><tr><th>Rule</th><th>Where from</th><th></th></tr>"
                    f"{trs or '<tr><td colspan=3 class=mute>No rules yet.</td></tr>'}</table>", user)


def rules_action(con, user, form: dict) -> str:
    a = form.get("a", "")
    if a == "add":
        if pitch.add_rule(con, form.get("rule", "")):
            store.log(con, user["name"], "added style rule", "", form.get("rule", "")[:200])
            return "Rule added."
        return "Empty, or that rule is already on."
    if a in ("on", "off") and str(form.get("id", "")).isdigit():
        r = con.execute("SELECT rule FROM style_rules WHERE id=?", (int(form["id"]),)).fetchone()
        if r:
            pitch.drop_rule(con, int(form["id"]), active=1 if a == "on" else 0)
            store.log(con, user["name"], f"style rule {a}", form["id"], r["rule"][:200])
            return f"Rule switched {a}."
    return "Nothing done."


def login_page(msg: str = "") -> str:
    return layout("Log in", f"<div class='card login'><h1>Hunt</h1><form method=post action=/login>"
                            f"<input name=name placeholder='Your name' autocomplete=username required>"
                            f"<input name=code placeholder='6-digit code' inputmode=numeric autocomplete=one-time-code "
                            f"maxlength=6 required>{f'<p class=err>{e(msg)}</p>' if msg else ''}<button>Log in</button>"
                            f"</form></div>")


# ---------- routing (pure function: easy to test) ----------

def app(con, method: str, path: str, query: dict, form: dict, cookies: dict, headers: dict):
    """Returns (status, extra headers, html)."""
    if method == "POST" and headers.get("origin") and \
            urllib.parse.urlparse(headers["origin"]).netloc != headers.get("host", ""):
        return 403, [], "Cross-site request refused."
    user = session_user(con, cookies.get(COOKIE, ""))
    m = re.fullmatch(r"/setup/([\w-]+)", path)
    if m:
        u = _setup_user(con, m.group(1))
        if not u:
            return 404, [], layout("Expired", "<h1>This setup link has expired or was used.</h1>")
        return 200, [], layout("Set up", _qr_block(u), scripts=QR_SCRIPTS)
    if path == "/login":
        if method == "POST":
            token, msg = login(con, form.get("name", ""), form.get("code", ""))
            if token:
                c = (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}"
                     + ("; Secure" if HTTPS else ""))
                return 303, [("Set-Cookie", c), ("Location", "/")], ""
            return 200, [], login_page(msg)
        return 200, [], login_page()
    if not user:
        return 303, [("Location", "/login")], ""
    if path == "/logout" and method == "POST":
        con.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(cookies.get(COOKIE, "")),))
        con.commit()
        store.log(con, user["name"], "logout")
        return 303, [("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0"), ("Location", "/login")], ""
    if path == "/":
        return 200, [], home(con, user)
    if path == "/leads":
        return 200, [], leads(con, user, query)
    m = re.fullmatch(r"/lead/([^/]+)(/act)?", path)
    if m:
        lid = urllib.parse.unquote(m.group(1))
        if m.group(2) and method == "POST":  # redirect after a press, so a page refresh can't repeat it
            note = lead_action(con, user, lid, form)
            return 303, [("Location", f"/lead/{urllib.parse.quote(lid)}?" + urllib.parse.urlencode({"note": note}))], ""
        return 200, [], lead(con, user, lid, query.get("note", "")[:200])
    if path == "/manual":
        if method == "POST":
            note = manual_action(con, user, form, form.get("_ids", []))
            return 303, [("Location", "/manual?" + urllib.parse.urlencode({"note": note}))], ""
        return 200, [], manual(con, user, query.get("note", "")[:200])
    if path == "/metrics":
        return 200, [], metrics(con, user, query)
    if user["role"] != "owner":
        return 403, [], layout("Owner only", "<h1>Only the owner can open this page.</h1>", user)
    if path == "/users":
        if method == "POST":
            try:
                uid, _ = add_user(con, form.get("name", ""))
            except Exception as ex:  # noqa: BLE001 — e.g. the name is taken
                return 200, [], users_page(con, user, f"Could not add: {ex}")
            store.log(con, user["name"], "created user", str(uid), form.get("name", ""))
            return 303, [("Location", f"/users/{uid}/qr")], ""
        return 200, [], users_page(con, user)
    m = re.fullmatch(r"/users/(\d+)/(qr|reset|revoke)", path)
    if m:
        uid, what = int(m.group(1)), m.group(2)
        if what == "qr":
            u = con.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
            if not u:
                return 404, [], layout("Not found", "<h1>No such active user</h1>", user)
            store.log(con, user["name"], "showed QR", str(uid))
            return 200, [], layout("QR", _qr_block(u), user, scripts=QR_SCRIPTS)
        if method == "POST" and what == "revoke" and uid != user["id"]:
            revoke(con, uid, user["name"])
            return 200, [], users_page(con, user, "Revoked. Their QR no longer works and they were logged out.")
        if method == "POST" and what == "reset":
            token = reset_qr(con, uid, user["name"])
            # resetting your own QR ends your session too: go straight to the new QR instead of a locked page
            return 303, [("Location", f"/setup/{token}" if uid == user["id"] else f"/users/{uid}/qr")], ""
    for p, page, act in (("/sources", sources_page, sources_action), ("/settings", settings_page, settings_action),
                         ("/rules", rules_page, rules_action)):
        if path == p:
            if method == "POST":  # redirect after a press, so a refresh can't repeat it
                return 303, [("Location", f"{p}?" + urllib.parse.urlencode({"note": act(con, user, form)[:300]}))], ""
            return 200, [], page(con, user, query.get("note", "")[:300])
    if path == "/activity":
        return 200, [], activity(con, user)
    return 404, [], layout("Not found", "<h1>Not found</h1>", user)


# ---------- server ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "Hunt"

    def _go(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        form = {}
        if method == "POST":
            n = min(int(self.headers.get("Content-Length") or 0), 200_000)
            raw = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            form = {k: v[0] for k, v in raw.items()}
            form["_ids"] = raw.get("ids", [])
        jar = SimpleCookie(self.headers.get("Cookie") or "")
        cookies = {k: v.value for k, v in jar.items()}
        heads = {k.lower(): v for k, v in self.headers.items()}
        con = store.db()
        try:
            status, extra, body = app(con, method, url.path, query, form, cookies, heads)
        except Exception as ex:  # noqa: BLE001 — never show a stack trace to the browser
            print("dashboard error:", repr(ex))
            status, extra, body = 500, [], layout("Error", "<h1>Something went wrong.</h1>")
        finally:
            con.close()
        data = body.encode()
        self.send_response(status)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")  # no-referrer makes browsers send Origin: null on posts
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; "
                                                    "script-src 'unsafe-inline' https://cdnjs.cloudflare.com; "
                                                    "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._go("GET")

    def do_POST(self):  # noqa: N802
        self._go("POST")

    def log_message(self, fmt, *args):  # setup links carry a secret: never write the path after /setup/
        path = urllib.parse.urlsplit(self.path).path
        print(f"dashboard {self.command} {'/setup/…' if path.startswith('/setup/') else path}")


def serve() -> None:
    host, port = os.environ.get("HUNT_DASH_HOST", "127.0.0.1"), int(os.environ.get("HUNT_DASH_PORT", "8090"))
    print(f"dashboard on http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
