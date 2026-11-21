"""SQLite memory: every lead Hunt has seen, its status, and your Telegram actions."""
import json
import re
import sqlite3
import time

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY, title_key TEXT, source TEXT, kind TEXT, title TEXT, url TEXT,
  company TEXT, summary TEXT, published TEXT, markets TEXT,
  status TEXT, score INTEGER, data TEXT,
  tg_card_id INTEGER, tg_draft_id INTEGER,
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_title_key ON leads(title_key);
CREATE INDEX IF NOT EXISTS idx_status ON leads(status);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

-- PRD v1 (B1): one row per person/company, however many places they were found.
CREATE TABLE IF NOT EXISTS prospects (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, name TEXT, company TEXT, country TEXT,
  handles TEXT, history TEXT DEFAULT 'new', sources TEXT, first_found REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, prospect_id INTEGER,
  who TEXT, doing TEXT, problem TEXT, fit TEXT, value TEXT, why_me TEXT, inputs TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, version INTEGER, channel TEXT, subject TEXT,
  body TEXT, edit_comment TEXT, approved_by TEXT, approved_at REAL, created_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, prospect_id INTEGER, lead_id TEXT, direction TEXT,
  channel TEXT, inbox TEXT, subject TEXT, body TEXT, ext_id TEXT, at REAL, addr TEXT
);
CREATE TABLE IF NOT EXISTS followups (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, due_at REAL, status TEXT, draft_id INTEGER
);
CREATE TABLE IF NOT EXISTS style_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT, from_draft_id INTEGER, active INTEGER DEFAULT 1, created_at REAL
);
CREATE TABLE IF NOT EXISTS do_not_contact (value TEXT PRIMARY KEY, reason TEXT, at REAL);
CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY, grp TEXT, layer INTEGER, tier TEXT, enabled INTEGER, auto_send TEXT,
  last_run REAL, last_count INTEGER, last_error TEXT, fails INTEGER DEFAULT 0, updated_at REAL
);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT, who TEXT, action TEXT, target TEXT, detail TEXT, at REAL
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE COLLATE NOCASE, secret TEXT, role TEXT,
  active INTEGER DEFAULT 1, last_counter INTEGER DEFAULT -1, fails INTEGER DEFAULT 0, locked_until REAL DEFAULT 0,
  created_at REAL, revoked_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER, expires REAL, created_at REAL);
"""
# Columns added to `leads` for PRD v1. Added with ALTER so the live database keeps every row.
LEAD_COLUMNS = {"prospect_id": "INTEGER", "lanes": "TEXT", "stage": "TEXT", "stage_at": "TEXT",
                "prescore": "INTEGER"}

# PRD funnel: found → researched → drafted → approved → sent → replied → call_booked → won
# (side exits: skipped, lost, needs_manual). Every status maps to one stage; `error` keeps the old one.
STAGE_OF = {
    "filtered": "found", "queued": "found", "prospect_new": "found", "prospect_ready": "found", "researched": "researched", "other_language": "skipped", "pick_new": "found", "not_contract": "skipped", "off_niche": "skipped", "wrong_country": "skipped", "pick": "found", "later": "found",
    "to_write": "found", "low": "researched", "duplicate": "researched", "draft_failed": "researched",
    "to_phone": "drafted", "drafted": "drafted", "to_approve": "drafted", "pitch_approved": "approved", "sending": "approved", "fu_drafted": "sent", "fu_to_approve": "sent", "fu_approved": "sent", "fu_sending": "sent", "bounced": "needs_manual", "unsubscribed": "lost", "blocked": "skipped", "approved": "approved", "bid_approved": "approved",
    "send_wait": "drafted", "apply_ready": "approved", "applying_srv": "approved", "mac_ready": "approved", "stopped": "skipped",
    "applying": "approved", "applied": "sent", "sent": "sent", "replied": "replied",
    "call_booked": "call_booked", "won": "won", "lost": "lost", "skipped": "skipped",
    "needs_you": "needs_manual", "needs_manual": "needs_manual", "failed": "needs_manual",
}
SCHEMA_VERSION = "b8"  # b8: messages.addr (who an email went to / came from)
# status: filtered | queued | low | duplicate | to_phone | sent | skipped | replied | won | lost | error
#         + auto-apply: approved (owner tapped 🚀) → applying (Mac helper took it) → applied | needs_you
#         + pick flow (sources with `flow: pick`, Upwork-bot style):
#           pick_new → pick (card on phone, no AI yet) → to_write (owner tapped ✍️) → drafted | draft_failed
#           → bid_approved (owner tapped ✅; bid text locked in data.locked_bid). later = parked by owner.


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=60)  # threads wait for each other's writes instead of failing
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    if meta_get(con, "schema") != SCHEMA_VERSION:
        _migrate(con)
    return con


def _migrate(con) -> None:
    """Bring an older database up to PRD v1 without losing a row: add columns, fill stage + pre-score.
    Runs inside one write-locked transaction, so when several threads open the database at start-up
    only the first one migrates; the others wait, then see the new version and skip."""
    from .filters import prescore
    con.execute("BEGIN IMMEDIATE")
    try:
        if meta_get(con, "schema") == SCHEMA_VERSION:  # another thread finished it while we waited
            con.rollback()
            return
        _migrate_locked(con, prescore)
        con.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('schema',?)", (SCHEMA_VERSION,))
        con.commit()
    except BaseException:
        con.rollback()
        raise


def _migrate_locked(con, prescore) -> None:
    have = {r["name"] for r in con.execute("PRAGMA table_info(leads)")}
    for col, kind in LEAD_COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE leads ADD COLUMN {col} {kind}")
    if "addr" not in {r["name"] for r in con.execute("PRAGMA table_info(messages)")}:
        con.execute("ALTER TABLE messages ADD COLUMN addr TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_stage ON leads(stage)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_addr ON messages(addr)")
    rows = con.execute("SELECT id, status, kind, title, company, summary, markets, created_at, updated_at "
                       "FROM leads WHERE stage IS NULL OR prescore IS NULL").fetchall()
    for r in rows:
        stage = STAGE_OF.get(r["status"], "found")
        markets = [m for m in (r["markets"] or "").split(",") if m]
        ps = prescore(dict(r), r["status"] != "filtered", markets)
        con.execute("UPDATE leads SET stage=COALESCE(stage,?), stage_at=COALESCE(stage_at,?), "
                    "prescore=COALESCE(prescore,?) WHERE id=?",
                    (stage, json.dumps({stage: r["updated_at"], "found": r["created_at"]}), ps, r["id"]))
    for r in con.execute("SELECT id, kind, source, company, data FROM leads WHERE prospect_id IS NULL").fetchall():
        # News items carry the publisher in `company`; only the AI's reading names the business itself.
        company = "" if r["kind"] == "funding" else (r["company"] or "")
        if not company and r["data"]:
            try:
                company = json.loads(r["data"]).get("company") or ""
            except (ValueError, AttributeError):
                company = ""
        pid = upsert_prospect(con, company=company, source=r["source"], commit=False) if company else None
        if pid:
            con.execute("UPDATE leads SET prospect_id=? WHERE id=?", (pid, r["id"]))


def is_new(con, lead) -> bool:
    row = con.execute("SELECT 1 FROM leads WHERE id=? OR (title_key=? AND title_key!='')",
                      (lead["id"], lead["title_key"])).fetchone()
    return row is None


def add(con, lead, markets, status, prescore: int = 0) -> None:
    now = time.time()
    stage = STAGE_OF.get(status, "found")
    # News items carry the publisher in `company`, not the business; those are linked after the AI reads them.
    company = "" if lead["kind"] == "funding" else lead.get("company", "")
    extra = lead.get("extra") or {}
    handles = {k: extra[v] for k, v in (("youtube", "channel_id"), ("youtube_handle", "handle")) if extra.get(v)}
    # Two YouTube channels can share a display name: key them on the channel id, never the name.
    pkey = "youtube:" + extra["channel_id"] if extra.get("channel_id") else ""
    pid = upsert_prospect(con, company=company, source=lead["source"], country=extra.get("country", ""),
                          handles=handles or None, key=pkey) if company else None
    con.execute(
        "INSERT OR IGNORE INTO leads (id,title_key,source,kind,title,url,company,summary,published,"
        "markets,status,data,created_at,updated_at,stage,stage_at,prescore,prospect_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead["id"], lead["title_key"], lead["source"], lead["kind"], lead["title"], lead["url"],
         lead["company"], lead["summary"], lead["published"], ",".join(markets), status,
         json.dumps({"extra": lead["extra"]}) if lead.get("extra") else None, now, now,
         stage, json.dumps({"found": now, stage: now}), prescore, pid))
    con.commit()


def update(con, lead_id, **fields) -> None:
    if "data" in fields and not isinstance(fields["data"], str):
        fields["data"] = json.dumps(fields["data"])
    fields["updated_at"] = time.time()
    stage = STAGE_OF.get(fields.get("status", ""))
    if stage:  # record the stage and the first time the lead reached it
        row = con.execute("SELECT stage_at FROM leads WHERE id=?", (lead_id,)).fetchone()
        times = json.loads(row["stage_at"] or "{}") if row else {}
        times.setdefault(stage, fields["updated_at"])
        fields["stage"], fields["stage_at"] = stage, json.dumps(times)
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE leads SET {cols} WHERE id=?", (*fields.values(), lead_id))
    con.commit()


def get(con, lead_id):
    return con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()


def by_message(con, message_id):
    return con.execute("SELECT * FROM leads WHERE tg_card_id=? OR tg_draft_id=?",
                       (message_id, message_id)).fetchone()


AGE_PENALTY_PER_DAY = 2  # pre-score points a waiting lead loses per day, so fresh posts go first


def queued(con, exclude_sources=()) -> list[dict]:
    """The AI's waiting line, best first. Nothing is dropped: keyword misses (`filtered`) wait too,
    just lower down, and there is no age cut-off. Pick-flow sources are left out (they wait for ✍️)."""
    now = time.time()
    rows = [dict(r) for r in con.execute("SELECT * FROM leads WHERE status IN ('queued','filtered')")]
    rows = [r for r in rows if r["source"] not in set(exclude_sources)]
    rows.sort(key=lambda r: ((r["prescore"] or 0) - AGE_PENALTY_PER_DAY * (now - r["created_at"]) / 86400,
                             r["created_at"]), reverse=True)
    return rows


def retire_old_queue(con, is_contract, pick_sources=()) -> int:
    """Contract-only switch (2026-11-20): nothing uses the old AI queue any more. Funding news is retired,
    full-time jobs become `not_contract`, and contract jobs move to the pick flow (cards only go out for
    leads from the last 3 days; older ones stay stored)."""
    rows = con.execute("SELECT id, kind, source, status, title, summary, data FROM leads "
                       "WHERE status IN ('queued','filtered')").fetchall()
    for r in rows:
        lead = {"kind": r["kind"], "title": r["title"], "summary": r["summary"],
                "extra": (json.loads(r["data"]) if r["data"] else {}).get("extra") or {}}
        if r["kind"] not in ("job", "ask"):
            status = "skipped"
        elif not is_contract(lead):
            status = "not_contract"
        else:  # `filtered` = the keyword check missed it (outside the niche), unless the site already filters
            status = "off_niche" if r["status"] == "filtered" and r["source"] not in set(pick_sources) else "pick_new"
        update(con, r["id"], status=status)
    return len(rows)


def requeue_pick(con, pick_sources) -> int:
    """Pick-flow leads (Freelancer) waiting in the AI queue from before the pick flow move to `pick_new`,
    so the pick sender owns them (new pick cards only go out for the last 3 days; older ones stay stored)."""
    names = list(pick_sources)
    if not names:
        return 0
    marks = ",".join("?" * len(names))
    rows = con.execute(f"SELECT id FROM leads WHERE status IN ('queued','filtered') AND source IN ({marks})",
                       names).fetchall()
    for r in rows:
        update(con, r["id"], status="pick_new")
    return len(rows)


def _norm(text: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split()
    while words and words[-1] in COMPANY_SUFFIXES:
        words.pop()
    return " ".join(words)


COMPANY_SUFFIXES = {"inc", "ltd", "llc", "limited", "corp", "co", "gmbh", "plc", "pvt", "private", "sa", "ag"}


def upsert_prospect(con, *, company: str = "", name: str = "", country: str = "", source: str = "",
                    handles: dict | None = None, commit: bool = True, key: str = "") -> int | None:
    """One prospect per person/company. Found again elsewhere → same row, new source and handles merged.
    `key` overrides the name-based key when the source has a unique id (YouTube channel id)."""
    key = key or _norm(company) or _norm(name)
    if not key:
        return None
    now = time.time()
    row = con.execute("SELECT * FROM prospects WHERE key=?", (key,)).fetchone()
    if row is None:
        cur = con.execute("INSERT INTO prospects (key,name,company,country,handles,sources,first_found,updated_at) "
                          "VALUES (?,?,?,?,?,?,?,?)",
                          (key, name, company, country, json.dumps(handles or {}),
                           json.dumps([source] if source else []), now, now))
        if commit:
            con.commit()
        return cur.lastrowid
    srcs = json.loads(row["sources"] or "[]")
    if source and source not in srcs:
        srcs.append(source)
    merged = {**json.loads(row["handles"] or "{}"), **(handles or {})}
    con.execute("UPDATE prospects SET sources=?, handles=?, name=COALESCE(NULLIF(name,''),?), "
                "country=COALESCE(NULLIF(country,''),?), updated_at=? WHERE id=?",
                (json.dumps(srcs), json.dumps(merged), name, country, now, row["id"]))
    if commit:
        con.commit()
    return row["id"]


def prospect_add_handles(con, prospect_id: int, handles: dict) -> None:
    """Merge contact handles into one known prospect row."""
    row = con.execute("SELECT handles FROM prospects WHERE id=?", (prospect_id,)).fetchone()
    if row is None:
        return
    merged = {**json.loads(row["handles"] or "{}"), **handles}
    con.execute("UPDATE prospects SET handles=?, updated_at=? WHERE id=?", (json.dumps(merged), time.time(), prospect_id))
    con.commit()


def dnc_add(con, value: str, reason: str) -> None:
    """Do-not-contact: unsubscribes and bounces. Never deleted, checked before every send."""
    con.execute("INSERT OR IGNORE INTO do_not_contact (value,reason,at) VALUES (?,?,?)",
                (value.strip().lower(), reason, time.time()))
    con.commit()


def dnc_has(con, value: str) -> bool:
    return con.execute("SELECT 1 FROM do_not_contact WHERE value=?", (value.strip().lower(),)).fetchone() is not None


def log(con, who: str, action: str, target: str = "", detail: str = "") -> None:
    """Activity log: who did what, when."""
    con.execute("INSERT INTO activity (who,action,target,detail,at) VALUES (?,?,?,?,?)",
                (who, action, target, detail, time.time()))
    con.commit()


def with_status(con, statuses, max_age_days=None) -> list[dict]:
    """Leads in any of `statuses`; max_age_days=None means no age limit."""
    statuses = [statuses] if isinstance(statuses, str) else list(statuses)
    cutoff = time.time() - max_age_days * 86400 if max_age_days else 0
    marks = ",".join("?" * len(statuses))
    return [dict(r) for r in con.execute(
        f"SELECT * FROM leads WHERE status IN ({marks}) AND created_at>? ORDER BY score DESC", (*statuses, cutoff))]


def meta_get(con, key, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def meta_set(con, key, value) -> None:
    con.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)", (key, str(value)))
    con.commit()


def stats(con, days=7) -> dict:
    cutoff = time.time() - days * 86400
    rows = con.execute(
        "SELECT source, status, COUNT(*) n FROM leads WHERE created_at>? GROUP BY source, status",
        (cutoff,)).fetchall()
    out: dict = {}
    for r in rows:
        src = r["source"].split(":")[0]
        out.setdefault(src, {}).setdefault(r["status"], 0)
        out[src][r["status"]] += r["n"]
    return out


def company_recent(con, company: str, days=14) -> bool:
    """Same company already on your phone recently (e.g. 6 outlets covering one raise)?"""
    if not company:
        return False
    cutoff = time.time() - days * 86400
    for r in con.execute("SELECT data FROM leads WHERE status NOT IN ('filtered','queued','low','error') "
                         "AND updated_at>? AND data IS NOT NULL", (cutoff,)):
        try:
            if json.loads(r["data"]).get("company", "").strip().lower() == company.strip().lower():
                return True
        except ValueError:
            continue
    return False


def ai_used_today(con) -> int:
    return int(meta_get(con, "ai:" + time.strftime("%Y-%m-%d"), "0"))


def bid_ai_used_today(con) -> int:
    return int(meta_get(con, "aibid:" + time.strftime("%Y-%m-%d"), "0"))


def ai_count_add(con, bid: bool = False) -> None:
    """Every AI call counts toward the daily total. Calls the owner asked for (bids, pitch rewrites)
    pass bid=True and are also counted on their own, so the automatic queue's share can be worked out."""
    meta_set(con, "ai:" + time.strftime("%Y-%m-%d"), ai_used_today(con) + 1)
    if bid:
        meta_set(con, "aibid:" + time.strftime("%Y-%m-%d"), bid_ai_used_today(con) + 1)


def lead_ai_left(con, max_per_day: int, reserve: int) -> int:
    """AI calls the automatic lead queue may still make today (the bid reserve is off-limits)."""
    total, bids = ai_used_today(con), bid_ai_used_today(con)
    return max(0, min(max_per_day - reserve - (total - bids), max_per_day - total))


def bid_ai_left(con, max_per_day: int, reserve: int) -> int:
    """AI calls the owner's requests (bids, pitch rewrites) may still make today. Never past the daily
    cap; the reserve is guaranteed because the automatic queue stops at max_per_day - reserve."""
    return max(0, max_per_day - ai_used_today(con))


def sync_sources(con, reg: dict) -> None:
    """Copy the registry (from sources.yaml) into the database so the dashboard can show it.
    Rows for sources no longer in the registry (removed or renamed) are dropped."""
    now = time.time()
    names = list(reg)
    marks = ",".join("?" * len(names)) or "''"
    con.execute(f"DELETE FROM sources WHERE name NOT IN ({marks})", names)
    for e in reg.values():
        con.execute("INSERT INTO sources (name,grp,layer,tier,enabled,auto_send,updated_at) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET grp=excluded.grp, layer=excluded.layer, tier=excluded.tier, "
                    "enabled=excluded.enabled, auto_send=excluded.auto_send, updated_at=excluded.updated_at",
                    (e["name"], e["group"], e["layer"], e["tier"], int(e["enabled"]), e["auto_send"], now))
    con.commit()


def source_ran(con, name: str, count: int, error: str | None) -> None:
    """Record one run of a source: how many leads, and the error if it broke (fails counts a streak)."""
    con.execute("INSERT OR IGNORE INTO sources (name) VALUES (?)", (name,))
    con.execute("UPDATE sources SET last_run=?, last_count=?, last_error=?, "
                "fails=CASE WHEN ? IS NULL THEN 0 ELSE COALESCE(fails,0)+1 END WHERE name=?",
                (time.time(), count, error, error, name))
    con.commit()


def sources_list(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM sources ORDER BY layer, grp, name")]
