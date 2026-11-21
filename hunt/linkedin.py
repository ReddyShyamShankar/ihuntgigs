"""LinkedIn public job search → Hunt leads (decision #32).

LinkedIn's alerts only cover one country each and its new search has no "Worldwide". So Hunt reads
LinkedIn's PUBLIC job search (the logged-out pages anyone can open): no account, no login, nothing
done in the owner's LinkedIn. Every hour it takes the next few keyword × place pairs from
`linkedin_search:` in sources.yaml (Worldwide, UAE, India, USA, Europe, …), remote jobs from the
last 24 hours, slowly, and backs off when LinkedIn says "too many requests".

Only cards that pass the cheap checks (title not blocked, location open to the owner, not seen before)
get their full job page read (description + employment type), so few pages are fetched.
"""
from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

from . import store
from .config import COUNTRIES, DATA
from .filters import blocked, wrong_country
from .sources import _lead

NAME = "linkedin_search"
SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
PAUSE = 4  # seconds between requests: unhurried


def config() -> dict:
    return (yaml.safe_load((DATA / "sources.yaml").read_text()) or {}).get(NAME) or {}


def _get(url: str, tries: int = 3, wait: float = 20) -> str:
    for i in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            if ex.code != 429 or i == tries - 1:
                raise
            time.sleep(wait * (i + 1))  # "too many requests": wait, then try again
    return ""


def search_url(keyword: str, place: dict, start: int = 0, hours: int = 24) -> str:
    q = {"keywords": keyword, "location": place.get("name", "Worldwide"), "f_WT": "2",
         "f_TPR": f"r{hours * 3600}", "start": str(start)}
    if place.get("geo"):
        q["geoId"] = str(place["geo"])
    return SEARCH + "?" + urllib.parse.urlencode(q)


def _text(rx: str, s: str) -> str:
    m = re.search(rx, s, re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))).strip() if m else ""


def parse_cards(page: str) -> list[dict]:
    """One search page → [{id, title, company, location, url, posted}]."""
    out = []
    for card in page.split("<li")[1:]:
        # the job number: the card's urn, else the number at the END of the link (titles can hold numbers too)
        m = re.search(r'urn:li:jobPosting:(\d{6,})', card) or re.search(r'/jobs/view/[^"?]*?(\d{6,})[?"/]', card)
        if not m:
            continue
        job_id = m.group(1)
        posted = re.search(r'datetime="([\d-]+)"', card)
        out.append({"id": job_id,
                    "title": _text(r'base-search-card__title[^>]*>(.*?)</h3>', card),
                    "company": _text(r'base-search-card__subtitle[^>]*>(.*?)</h4>', card),
                    "location": _text(r'job-search-card__location[^>]*>(.*?)</span>', card),
                    "posted": posted.group(1) if posted else "",
                    "url": f"https://www.linkedin.com/jobs/view/{job_id}/"})
    return out


def parse_posting(page: str) -> dict:
    """Full job page → description + the criteria list (Employment type, Seniority level, …)."""
    desc = _text(r'show-more-less-html__markup[^>]*>(.*?)</div>', page)
    crit = dict(zip([html.unescape(x).strip() for x in
                     re.findall(r'description__job-criteria-subheader[^>]*>(.*?)<', page, re.S)],
                    [html.unescape(x).strip() for x in
                     re.findall(r'description__job-criteria-text[^>]*>(.*?)<', page, re.S)]))
    return {"description": desc[:6000], "employment_type": crit.get("Employment type", ""),
            "offsite_apply": "offsite-apply" in page or "apply-link-offsite" in page}


def to_lead(card: dict, posting: dict) -> dict:
    loc = card["location"] or "Worldwide"
    summary = f"{card['company']} on LinkedIn. Location: {loc}. {posting.get('description', '')}"
    lead = _lead(NAME, "job", card["title"], card["url"], summary, card["company"], card["posted"])
    lead["extra"] = {"remote_always": True, "li_location": loc, "li_offsite": bool(posting.get("offsite_apply"))}
    if posting.get("employment_type"):
        lead["extra"]["job_type"] = posting["employment_type"][:60]
    return lead


def plan(cfg: dict) -> list[tuple[str, dict]]:
    """Every keyword × place pair; the hourly runs walk through them in turn."""
    return [(k, p) for p in cfg.get("locations", []) or [] for k in cfg.get("keywords", []) or []]


def collect(con=None, per_run: int | None = None, get=_get, pause: float = PAUSE,
            cfg: dict | None = None) -> tuple[list[dict], dict]:
    """Next slice of keyword × place searches. Returns (leads, stats). con=None = dry run, cursor untouched."""
    cfg = config() if cfg is None else cfg
    pairs = plan(cfg)
    stats = {"count": 0, "error": None}
    if cfg.get("enabled", True) is False or not pairs:
        return [], stats
    per_run = min(per_run or int(cfg.get("per_run", 6)), len(pairs))
    cursor = int(store.meta_get(con, "linkedin_cursor", "0") or 0) if con is not None else 0
    seen, leads = set(), []
    try:
        for i in range(per_run):
            keyword, place = pairs[(cursor + i) % len(pairs)]
            for page in range(int(cfg.get("pages", 1))):
                cards = parse_cards(get(search_url(keyword, place, page * 10, int(cfg.get("hours", 24)))))
                time.sleep(pause)
                for c in cards:
                    if c["id"] in seen or not c["title"] or blocked(c["title"]):
                        continue
                    seen.add(c["id"])
                    probe = {"title": c["title"], "summary": f"Location: {c['location'] or 'Worldwide'}.", "extra": {}}
                    if COUNTRIES and wrong_country(probe, COUNTRIES):
                        continue  # e.g. "Miami, FL": only people there can take it
                    if con is not None and not store.is_new(con, {"id": _lead(NAME, "job", c["title"], c["url"])["id"],
                                                                  "title_key": ""}):
                        continue
                    posting = parse_posting(get(POSTING.format(c["id"])))
                    time.sleep(pause)
                    leads.append(to_lead(c, posting))
                if len(cards) < 10:
                    break
    except Exception as ex:  # noqa: BLE001 — LinkedIn trouble must never stop the other sources
        stats["error"] = str(ex)[:300]
    if con is not None:
        store.meta_set(con, "linkedin_cursor", (cursor + per_run) % len(pairs))
    stats["count"] = len(leads)
    return leads, stats
