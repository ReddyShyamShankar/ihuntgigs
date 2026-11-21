"""Collectors: pull fresh items from every source in data/sources.yaml and normalise them into leads."""
import hashlib
import html
import json
import re
import urllib.parse
import urllib.request

import feedparser
import yaml

from .filters import countries_in_text
from .config import DATA

UA = "Mozilla/5.0 (compatible; HuntBot/0.1)"


def _clean(text: str, limit: int = 1500) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return text[:limit]


def title_key(title: str) -> str:
    """Same story from 5 outlets → one lead. Google News adds ' - Publisher' at the end."""
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", title.lower())
    return re.sub(r"[^a-z0-9]+", "", t)[:70]


def _lead(source, kind, title, url, summary="", company="", published="") -> dict:
    title = _clean(title, 300)
    return {
        "id": hashlib.sha1(url.encode()).hexdigest()[:16],
        "title_key": title_key(title),
        "source": source,
        "kind": kind,
        "title": title,
        "url": url,
        "company": _clean(company, 120),
        "summary": _clean(summary),
        "published": str(published or ""),
    }


def _gnews_url(q: str, region: str) -> str:
    hl, gl, ceid = ("en-IN", "IN", "IN:en") if region == "IN" else ("en-US", "US", "US:en")
    return (f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}"
            f"&hl={hl}&gl={gl}&ceid={ceid}")


def _rss(name, url, kind, must=None) -> list[dict]:
    feed = feedparser.parse(url, agent=UA)
    if feed.bozo and not feed.entries:
        raise RuntimeError(f"feed error: {feed.bozo_exception}")
    out = []
    for e in feed.entries:
        title = e.get("title", "")
        if must and must.lower() not in title.lower():
            continue
        company = e.get("source", {}).get("title", "") if name.startswith("news") else e.get("author", "")
        if not company and " at " in title:
            title, company = title.rsplit(" at ", 1)
        out.append(_lead(name, kind, title, e.get("link", ""), e.get("summary", ""),
                         company, e.get("published", e.get("updated", ""))))
    return out


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _typed(lead: dict, job_type) -> dict:
    """Keep the site's own job-type label (e.g. "Full Time", "Contract") for the contract-only gate."""
    if isinstance(job_type, (list, tuple)):
        job_type = " ".join(str(x) for x in job_type)
    if job_type:
        lead["extra"] = {**(lead.get("extra") or {}), "job_type": str(job_type)[:60]}
    return lead


REMOTEOK_TYPES = ("contract", "contractor", "freelance", "full time", "full-time", "part time", "part-time")


def _json(name, url, kind, fmt=None, markets=None) -> list[dict]:
    data = _get_json(url)
    out = []
    name_fmt = fmt or name
    if name_fmt == "remoteok":
        for d in data:
            if isinstance(d, dict) and d.get("position"):
                tags = [str(x).lower() for x in d.get("tags") or [] if str(x).lower() in REMOTEOK_TYPES]
                out.append(_typed(_lead(name, kind, d["position"], d.get("url", ""), d.get("description", ""),
                                        d.get("company", ""), d.get("date", "")), tags))
    elif name_fmt == "remotive":
        for d in data.get("jobs", []):
            out.append(_typed(_lead(name, kind, d["title"], d["url"], d.get("description", ""),
                                    d.get("company_name", ""), d.get("publication_date", "")), d.get("job_type")))
    elif name_fmt == "freelancer":  # Freelancer.com public projects API (filtered by skill IDs in the URL)
        for d in (data.get("result") or {}).get("projects", []):
            budget = d.get("budget") or {}
            cur = (d.get("currency") or {}).get("code", "")
            pay = f"Budget {budget.get('minimum', '?')}-{budget.get('maximum', '?')} {cur} ({d.get('type', '')}). "
            lead = _lead(name, kind, d["title"], "https://www.freelancer.com/projects/" + d.get("seo_url", ""),
                         pay + (d.get("description") or d.get("preview_description") or ""), "",
                         d.get("time_submitted", ""))
            lead["extra"] = {"project_id": d.get("id"), "budget_min": budget.get("minimum"),
                             "budget_max": budget.get("maximum"), "currency": cur, "type": d.get("type", ""),
                             "bid_count": (d.get("bid_stats") or {}).get("bid_count")}
            out.append(lead)
    elif name_fmt == "braintrust":
        for d in data.get("results", []):
            budget = f"${d.get('budget_minimum_usd')}-{d.get('budget_maximum_usd')} {d.get('payment_type', '')}. "
            out.append(_lead(name, kind, d["title"], f"https://app.usebraintrust.com/jobs/{d['id']}/",
                             budget + str(d.get("introduction") or d.get("description") or ""),
                             (d.get("employer") or {}).get("name", ""), d.get("created", "")))
    elif name_fmt == "jobicy":
        for d in data.get("jobs", []):
            out.append(_typed(_lead(name, kind, d["jobTitle"], d["url"], d.get("jobExcerpt") or d.get("jobDescription", ""),
                                    d.get("companyName", ""), d.get("pubDate", "")), d.get("jobType")))
    elif name_fmt == "workingnomads":
        for d in data:
            out.append(_lead(name, kind, d["title"], d["url"], d.get("description", ""), d.get("company_name", ""),
                             d.get("pub_date", "")))
    elif name_fmt == "himalayas":
        for d in data.get("jobs", []):
            lead = _typed(_lead(name, kind, d["title"], d.get("applicationLink") or d.get("guid", ""),
                                d.get("excerpt") or d.get("description", ""), d.get("companyName", ""),
                                d.get("pubDate", "")), d.get("employmentType"))
            if d.get("companySlug"):  # helps the server robot find the company's own job page (hunt/robot.py)
                lead["extra"] = {**(lead.get("extra") or {}), "company_slug": str(d["companySlug"])[:80]}
            written = countries_in_text(d.get("description", ""))  # e.g. "must reside in the United States"
            if written and not d.get("locationRestrictions"):
                d["locationRestrictions"] = written
            if d.get("locationRestrictions"):  # e.g. ["United States"]: only people there may apply
                lead["extra"] = {**(lead.get("extra") or {}), "countries": list(d["locationRestrictions"])[:60]}
            out.append(lead)
    if markets:
        for lead in out:
            lead["market_hint"] = list(markets)
    return out


ATS_URL = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def _careers(name, ats, slug, company, markets, remote_only=True) -> list[dict]:
    """A trading/fintech company's own public job board (official Greenhouse/Lever/Ashby feeds)."""
    data = _get_json(ATS_URL[ats].format(slug=slug))
    rows = []
    if ats == "greenhouse":
        for d in data.get("jobs", []):
            rows.append((d["title"], d.get("absolute_url", ""), (d.get("location") or {}).get("name", ""),
                         d.get("updated_at", ""), ""))
    elif ats == "lever":
        for d in data:
            cat = d.get("categories") or {}
            rows.append((d.get("text", ""), d.get("hostedUrl", ""),
                         f"{cat.get('location', '')} {d.get('workplaceType', '')}", d.get("createdAt", ""),
                         cat.get("commitment", "")))
    elif ats == "ashby":
        for d in data.get("jobs", []):
            loc = f"{d.get('location', '')} {d.get('workplaceType', '')} {'remote' if d.get('isRemote') else ''}"
            rows.append((d["title"], d.get("jobUrl", ""), loc, d.get("publishedAt", ""), d.get("employmentType", "")))
    out = []
    for title, url, loc, published, job_type in rows:
        if remote_only and "remote" not in loc.lower():
            continue
        lead = _typed(_lead(name, "job", title, url, f"{company} careers page. Location: {loc}", company, published),
                      job_type)
        lead["market_hint"] = list(markets)
        out.append(lead)
    return out


def config() -> dict:
    return yaml.safe_load((DATA / "sources.yaml").read_text()) or {}


def pick_sources() -> set[str]:
    """Sources marked `flow: pick` in sources.yaml: the owner picks before the AI writes (Upwork-bot flow)."""
    cfg = yaml.safe_load((DATA / "sources.yaml").read_text())
    return {s["name"] for group in ("rss", "json") for s in cfg.get(group, []) or [] if s.get("flow") == "pick"}


def collect(stats: dict | None = None, cfg: dict | None = None) -> tuple[list[dict], list[str]]:
    """Run every switched-on collector. One broken source never stops the others.
    `stats` (if given) is filled with name → {"count": leads found, "error": text or None}."""
    from . import registry
    own = cfg is None
    if own:
        cfg = yaml.safe_load((DATA / "sources.yaml").read_text())
    over = registry.saved_overrides() if own else {}
    reg = registry.load(cfg, overrides=over or None)
    # per-line switch in sources.yaml, unless the dashboard set this source on or off
    on = lambda name, item: (bool(over[name].get("enabled")) if isinstance(over.get(name), dict) and  # noqa: E731
                             "enabled" in over[name] else item.get("enabled", True) is not False)
    leads, errors = [], []
    searches = {s["name"] for s in cfg.get("json", []) or [] if "/search?q=" in s.get("url", "")}
    always = {s["name"] for group in ("rss", "json") for s in cfg.get(group, []) or [] if s.get("contract") == "always"}
    remote = {s["name"] for group in ("rss", "json") for s in cfg.get(group, []) or [] if s.get("remote") == "always"}
    jobs = [("news:" + n.get("region", "US"), _rss, (_gnews_url(n["q"], n.get("region", "US")), "funding"))
            for n in cfg.get("news", []) if on("news:" + n.get("region", "US"), n)]
    jobs += [(s["name"], _rss, (s["url"], s["kind"], s.get("title_must_include"))) for s in cfg.get("rss", []) if on(s["name"], s)]
    jobs += [(s["name"], _json, (s["url"], s["kind"], s.get("format"), s.get("markets")))
             for s in cfg.get("json", []) if on(s["name"], s)]
    jobs += [("careers:" + c["slug"], _careers, (c["ats"], c["slug"], c["company"], c.get("markets", []),
                                                 c.get("remote_only", True))) for c in cfg.get("careers", []) if on("careers:" + c["slug"], c)]
    for name, fn, args in jobs:
        if not reg.get(name, {"enabled": True})["enabled"]:
            continue  # switched off under `overrides:`
        row = stats.setdefault(name, {"count": 0, "error": None}) if stats is not None else {}
        try:
            got = [l for l in fn(name, *args) if l["url"]]
            if name in always:
                for l in got:
                    l["extra"] = {**(l.get("extra") or {}), "contract_always": True}
            if name in remote:  # remote-only board, or a career page read with remote_only on
                for l in got:
                    l["extra"] = {**(l.get("extra") or {}), "remote_always": True}
            if name in searches:  # the owner's own search words found it: trusted as in the niche
                for l in got:
                    l["extra"] = {**(l.get("extra") or {}), "from_search": True}
            leads += got
            if stats is not None:
                row["count"] += len(got)
        except Exception as ex:  # noqa: BLE001 — report and continue
            errors.append(f"{name}: {ex}")
            if stats is not None:
                row["error"] = (f"{row['error']}; " if row["error"] else "") + str(ex)[:300]
    return leads, errors
