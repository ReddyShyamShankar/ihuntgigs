"""B8b follow-ups (PRD §12): a short nudge when a sent pitch gets no reply. OFF by default.

Settings (server .env):
  HUNT_FOLLOWUPS=on            switch on (default off)
  HUNT_FOLLOWUP_DAYS=3,7       days of silence before follow-up 1, then before follow-up 2 (counted from the
                               last message sent); the list length is how many follow-ups there are
  HUNT_FOLLOWUP_LANES=         only these lanes (comma list); empty = all six lanes

Flow, per prospect lead with status `sent` and no reply:
  enough silent days → AI writes follow-up n (pitch.write_followup, 20–80 words, same thread)
  → status fu_drafted + a `followups` row (due date, status drafted, linked draft)
  → the same Telegram card as a pitch (B7): ✅ Approve / ✏️ Edit / ⏭ Skip (skip = stop follow-ups for this lead)
  → approved: the sender (B8) emails it as a reply in the same thread, or the owner sends a DM by hand
  → back to `sent`; the clock restarts for the next one.
Any reply, bounce or unsubscribe (sender.read_run) ends it: the lead leaves `sent` and open rows are cancelled.
Follow-ups share the pitch writer's daily AI budget.
"""
import json
import os
import time

from . import chatgpt, config, pitch, settings, store

ON = os.environ.get("HUNT_FOLLOWUPS", "off").lower() == "on"
DAYS = [int(x) for x in os.environ.get("HUNT_FOLLOWUP_DAYS", "3,7").split(",") if x.strip()]
LANES = {x.strip() for x in os.environ.get("HUNT_FOLLOWUP_LANES", "").split(",") if x.strip()}
PER_RUN = int(os.environ.get("HUNT_FOLLOWUPS_PER_RUN", "6"))
MAX_TRIES = 3


def done_count(con, lead_id: str) -> int:
    return con.execute("SELECT COUNT(*) FROM followups WHERE lead_id=? AND status='sent'", (lead_id,)).fetchone()[0]


def on(con) -> bool:  # dashboard setting first, else HUNT_FOLLOWUPS
    return bool(settings.get(con, "followups", ON))


def days(con) -> list[int]:
    return list(settings.get(con, "followup_days", DAYS))


def _lane_ok(row, only: set) -> bool:
    lanes = {x for x in (row["lanes"] or "").split(",") if x}
    return not only or bool(lanes & only)


def _earlier(con, row, data: dict) -> tuple[str, str]:
    """(subject, text) of the last message sent to this lead."""
    m = con.execute("SELECT subject, body FROM messages WHERE lead_id=? AND direction='out' ORDER BY at DESC",
                    (row["id"],)).fetchone()
    if m:
        return m["subject"] or "", m["body"] or ""
    d = data.get("draft") or {}
    return d.get("subject", ""), d.get("body", "")


def due(con, now: float | None = None) -> list:
    """(lead, follow-up number, due time) for every lead whose next follow-up is due now."""
    now = time.time() if now is None else now
    out, gaps, only = [], days(con), set(settings.get(con, "followup_lanes", LANES))
    for row in con.execute("SELECT * FROM leads WHERE kind='prospect' AND status='sent' ORDER BY score DESC"):
        data = json.loads(row["data"] or "{}")
        n = done_count(con, row["id"])
        last = data.get("last_sent_at") or (data.get("draft") or {}).get("sent_at")
        if data.get("followups_stopped") or n >= len(gaps) or not last or not _lane_ok(row, only):
            continue
        if now >= float(last) + gaps[n] * 86400:
            out.append((row, n + 1, float(last) + gaps[n] * 86400))
    return out


def run(con, ask=None, now: float | None = None) -> dict:
    counts = {"drafted": 0, "errors": 0, "paused": ""}
    if not on(con) or (ask is None and not config.ai_on()):
        return counts
    for row, n, due_at in due(con, now)[:PER_RUN]:
        if pitch.used_today(con) >= pitch.PER_DAY:
            break
        data = json.loads(row["data"] or "{}")
        subject, earlier = _earlier(con, row, data)
        try:
            draft_id = pitch.write_followup(con, row, n, len(days(con)), subject, earlier, ask)
        except (chatgpt.UsageLimit, chatgpt.LoginNeeded, chatgpt.TryLater) as ex:
            counts["paused"] = str(ex)
            break
        except Exception as ex:  # noqa: BLE001 — one bad lead never blocks the rest
            counts["errors"] += 1
            tries = int(data.get("followup_tries", 0)) + 1
            data.update(followup_tries=tries, followup_error=str(ex)[:300])
            if tries >= MAX_TRIES:
                data["followups_stopped"] = True  # stop spending AI on a lead that keeps failing
            store.update(con, row["id"], data=data)
            continue
        con.execute("INSERT INTO followups (lead_id,due_at,status,draft_id) VALUES (?,?,?,?)",
                    (row["id"], due_at, "drafted", draft_id))
        con.commit()
        counts["drafted"] += 1
    return counts
