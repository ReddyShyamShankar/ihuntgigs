"""SQLite memory: every lead Hunt has seen, its status, and your Telegram actions."""
import json
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
"""
# status: filtered | queued | low | duplicate | to_phone | sent | skipped | replied | won | lost | error


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def is_new(con, lead) -> bool:
    row = con.execute("SELECT 1 FROM leads WHERE id=? OR (title_key=? AND title_key!='')",
                      (lead["id"], lead["title_key"])).fetchone()
    return row is None


def add(con, lead, markets, status) -> None:
    now = time.time()
    con.execute(
        "INSERT OR IGNORE INTO leads (id,title_key,source,kind,title,url,company,summary,published,"
        "markets,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead["id"], lead["title_key"], lead["source"], lead["kind"], lead["title"], lead["url"],
         lead["company"], lead["summary"], lead["published"], ",".join(markets), status, now, now))
    con.commit()


def update(con, lead_id, **fields) -> None:
    if "data" in fields and not isinstance(fields["data"], str):
        fields["data"] = json.dumps(fields["data"])
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE leads SET {cols} WHERE id=?", (*fields.values(), lead_id))
    con.commit()


def get(con, lead_id):
    return con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()


def by_message(con, message_id):
    return con.execute("SELECT * FROM leads WHERE tg_card_id=? OR tg_draft_id=?",
                       (message_id, message_id)).fetchone()


def queued(con, max_age_days=3):
    cutoff = time.time() - max_age_days * 86400
    return [dict(r) for r in con.execute(
        "SELECT * FROM leads WHERE status='queued' AND created_at>? ORDER BY created_at DESC", (cutoff,))]


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


def ai_count_add(con) -> None:
    meta_set(con, "ai:" + time.strftime("%Y-%m-%d"), ai_used_today(con) + 1)
