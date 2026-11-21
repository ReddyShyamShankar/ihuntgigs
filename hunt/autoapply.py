"""Automatic applying (decision #29): sources marked `apply: auto` in sources.yaml skip the ✍️ tap.

  new job → AI writes the cover note at once (checker fixes it) → Telegram: "Applying in 2 h" with ✋ Stop
  → no Stop within the safety window → `apply_ready` → server robot (route 1)
  → if the robot can't reach the company's own form → `mac_ready` → Mac helper (route 2, applier/)
  → applied | needs_you (CAPTCHA, a question it can't answer) | failed.

Reply to the "Applying in…" message to change the text (the window restarts after the rewrite).
The window length is HUNT_SAFETY_HOURS (default 2). Mode B (fully automatic) = 0, after 30 days of results.
"""
import json
import os
import time

import yaml

from . import store, telegram
from .config import DATA, TELEGRAM_CHAT_ID

SAFETY_HOURS = float(os.environ.get("HUNT_SAFETY_HOURS", "2"))
WAIT, READY, ROBOT, MAC, STOPPED = "send_wait", "apply_ready", "applying_srv", "mac_ready", "stopped"
STATUSES = {WAIT, READY, ROBOT, MAC, STOPPED}


def auto_sources() -> set[str]:
    cfg = yaml.safe_load((DATA / "sources.yaml").read_text()) or {}
    return {s["name"] for g in ("rss", "json") for s in cfg.get(g, []) or [] if s.get("apply") == "auto"}


def is_auto(row) -> bool:
    return bool(row) and row["source"] in auto_sources()


def _data(row) -> dict:
    return json.loads(row["data"] or "{}") if row and row["data"] else {}


def notice_text(row, d: dict) -> str:
    p = d.get("policy") or {}
    when = time.strftime("%H:%M", time.localtime(d.get("send_after", time.time())))
    head = (f"🤖 Applying at {when} ({SAFETY_HOURS:g} h) · checker {p.get('score', '?')}%\n"
            f"{row['title'][:150]}{' · ' + row['company'][:60] if row['company'] else ''}\n\n")
    tail = "\n\n— — —\nTap ✋ Stop to cancel. Reply to this message to change the text (e.g. 'shorter')."
    return telegram._clip(head + d.get("bid", "") + tail, telegram.MAX_TEXT)


def queue(con, row, d: dict) -> None:
    """A note passed the checker: post it with ✋ Stop and start the safety window."""
    d["send_after"] = time.time() + SAFETY_HOURS * 3600
    d["locked_bid"] = d["bid"]
    old = row["tg_card_id"]
    m = telegram.call("sendMessage", chat_id=TELEGRAM_CHAT_ID, text=notice_text(row, d),
                      reply_markup=telegram.buttons(row["id"], WAIT, url=row["url"]),
                      **({"reply_to_message_id": old} if old else {}))
    if old:  # an earlier version loses its Stop button; the newest message is the one that counts
        _edit_kb(old, telegram.buttons(row["id"], "replaced", url=row["url"]))
    store.update(con, row["id"], status=WAIT, data=d, tg_card_id=m["message_id"], tg_draft_id=m["message_id"])


def _edit_kb(message_id, markup) -> None:
    try:
        telegram.call("editMessageReplyMarkup", chat_id=TELEGRAM_CHAT_ID, message_id=message_id, reply_markup=markup)
    except Exception:  # noqa: BLE001 — old or unchanged message
        pass


def rebutton(con, lead_id: str) -> None:
    row = store.get(con, lead_id)
    if row and row["tg_card_id"]:
        _edit_kb(row["tg_card_id"], telegram.buttons(lead_id, row["status"], url=row["url"]))


def robot_on() -> bool:
    """The server robot runs only once the owner's profile (+ CV) is on the server."""
    path = os.environ.get("HUNT_APPLIER_PROFILE", "")
    return bool(path) and os.path.exists(path)


def release_due(con, now: float | None = None) -> int:
    """Safety window over and nobody tapped Stop: the server robot may apply (or the Mac, if the robot is off)."""
    now = now or time.time()
    n = 0
    target = READY if robot_on() else MAC
    for r in store.with_status(con, WAIT):
        if _data(r).get("send_after", 0) <= now:
            store.update(con, r["id"], status=target)
            rebutton(con, r["id"])
            n += 1
    return n


def stop(con, row) -> str:
    if row["status"] not in (WAIT, READY, MAC):
        return "Too late — it's already being sent or done"
    store.update(con, row["id"], status=STOPPED)
    rebutton(con, row["id"])
    return "Stopped. Nothing will be sent."


def mac_queue(con, row, note: str) -> None:
    """Route 1 couldn't do it: hand it to the Mac helper (route 2)."""
    d = _data(row)
    d["robot_note"] = note[:300]
    store.update(con, row["id"], status=MAC, data=d)
    rebutton(con, row["id"])
