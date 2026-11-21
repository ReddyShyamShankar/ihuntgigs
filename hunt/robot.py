"""Server robot — route 1 of automatic applying (decision #29).

Himalayas job pages sit behind a "verify you're human" wall, and Hunt never gets past those by trickery.
So the robot skips Himalayas: it looks the job up on the company's OWN job system through that system's
public job list (Greenhouse, Lever, Ashby), opens the real application form in a hidden browser, fills it
from the owner's profile + the locked cover note, attaches the CV and submits.

Anything it can't finish (no company form found, CAPTCHA, a question it can't answer) goes to the Mac
helper (route 2, status `mac_ready`), which works in the owner's own Chrome when the laptop is on.

Runs as `python -m hunt robot` from a systemd timer with its own memory/CPU cap, a few jobs per run.
HUNT_ROBOT_LIVE=1 really submits; anything else fills the form but submits nothing (dry run).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import autoapply, store, telegram
from .config import DATA

ROOT = Path(__file__).resolve().parent.parent
LIVE = os.environ.get("HUNT_ROBOT_LIVE") == "1"
PER_RUN = int(os.environ.get("HUNT_ROBOT_PER_RUN", "3"))
UA = "Mozilla/5.0 (compatible; HuntBot/1.0)"

# Each job system's public job list: URL pattern + how to read (title, link) pairs from it.
BOARDS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{}/jobs",
                   lambda j: [(x.get("title", ""), x.get("absolute_url", "")) for x in j.get("jobs", [])]),
    "lever": ("https://api.lever.co/v0/postings/{}?mode=json",
              lambda j: [(x.get("text", ""), x.get("hostedUrl", "")) for x in j] if isinstance(j, list) else []),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{}",
              lambda j: [(x.get("title", ""), x.get("jobUrl") or x.get("applyUrl", "")) for x in j.get("jobs", [])]),
}
_SUFFIX = re.compile(r"\b(inc|llc|ltd|limited|gmbh|corp|corporation|plc|technologies|holdings|group)\b\.?")
_NOISE = {"remote", "contract", "contractor", "freelance", "part", "time", "full", "the", "a", "an", "and", "of", "for",
          "to", "in", "m", "f", "d", "x", "hybrid", "worldwide", "anywhere"}


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def slugs(company: str, hint: str = "") -> list[str]:
    """Names a company's job board is likely filed under, e.g. "Example FX Ltd" → examplefx, example-fx."""
    words = re.findall(r"[a-z0-9]+", _SUFFIX.sub("", (company or "").lower()))
    out = [hint.lower().strip()] if hint else []
    out += ["".join(words), "-".join(words)]
    return [s for s in dict.fromkeys(out) if len(s) >= 3]


def _tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (title or "").lower()) if w not in _NOISE}


def same_job(a: str, b: str) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.75


def find_form(company: str, title: str, hint: str = "", get=_get) -> tuple[str, str]:
    """(job system, job link) on the company's own board, or ("", "") if it isn't there."""
    for slug in slugs(company, hint):
        for ats, (pattern, read) in BOARDS.items():
            try:
                jobs = read(get(pattern.format(urllib.parse.quote(slug))))
            except Exception:  # noqa: BLE001 — not on this system (404) or the list is down
                continue
            for t, link in jobs:
                if link and same_job(t, title):
                    return ats, link
    return "", ""


def _applier():
    """The Mac helper's form filler, reused here so both routes fill forms the same way."""
    if str(ROOT / "applier") not in sys.path:
        sys.path.insert(0, str(ROOT / "applier"))
    import hunt_applier
    hunt_applier.RUNS = Path(os.environ.get("HUNT_ROBOT_RUNS", DATA / "robot-runs"))
    hunt_applier.RUNS.mkdir(parents=True, exist_ok=True)
    return hunt_applier


def fill_and_submit(lead: dict) -> tuple[str, str]:
    """Open the form in a hidden browser and apply. Returns (applied | needs_you | dry, note)."""
    h = _applier()
    p = h.load_profile()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            return h.apply_one(ctx, lead, p, LIVE)
        finally:
            browser.close()


def _note(row, text: str) -> None:
    if telegram.enabled():
        telegram.say(text, reply_to_message_id=row["tg_card_id"])


def run_one(con, row, apply=fill_and_submit, find=find_form) -> str:
    lead_id = row["id"]
    store.update(con, lead_id, status=autoapply.ROBOT)
    autoapply.rebutton(con, lead_id)
    d = autoapply._data(row)
    hint = (d.get("extra") or {}).get("company_slug", "")
    try:
        ats, link = find(row["company"], row["title"], hint)
        if not link and "linkedin.com" in (row["url"] or ""):
            d["robot_note"] = "Easy Apply on LinkedIn: open it, paste the note, send"
            store.update(con, lead_id, status="needs_you", data=d)
            autoapply.rebutton(con, lead_id)
            _note(row, "🔗 No company form found, so this one is LinkedIn Easy Apply. Open the job, paste the "
                       "note above, send, then tap ✅ I applied. (Hunt never works inside your LinkedIn account.)")
            return "linkedin"
        if not link:
            why = "The company's own job form wasn't found on Greenhouse, Lever or Ashby"
            autoapply.mac_queue(con, store.get(con, lead_id), why)
            _note(row, f"💻 {why}. Your Mac helper will apply through Himalayas when the laptop is on.")
            return "mac"
        d["ats_url"] = link
        store.update(con, lead_id, data=d)
        status, note = apply({"id": lead_id, "kind": "job", "url": link, "emails": [],
                              "cover": d.get("locked_bid", ""), "company": row["company"]})
    except Exception as ex:  # noqa: BLE001 — one bad job must never block the rest
        status, note = "needs_you", f"Robot error: {str(ex)[:150]}"
    row = store.get(con, lead_id)
    d = autoapply._data(row)
    d["robot_note"] = note[:300]
    if status == "applied":
        store.update(con, lead_id, status="applied", data=d)
        autoapply.rebutton(con, lead_id)
        _note(row, f"✅ Applied — {row['title'][:80]} ({note})")
        return "applied"
    if status == "dry":
        store.update(con, lead_id, status="needs_you", data=d)
        autoapply.rebutton(con, lead_id)
        _note(row, f"🧪 {note}. Robot is in test mode: apply by hand, then tap ✅ I applied.")
        return "dry"
    autoapply.mac_queue(con, row, note)
    _note(row, f"💻 Robot couldn't finish: {note}. Your Mac helper will try when the laptop is on.")
    return "mac"


def run(con=None, limit: int = PER_RUN) -> dict:
    con = con or store.db()
    counts: dict = {}
    for row in store.with_status(con, autoapply.READY)[:limit]:
        got = run_one(con, row)
        counts[got] = counts.get(got, 0) + 1
        time.sleep(3)
    return counts
