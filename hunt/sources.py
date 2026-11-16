"""Collectors: pull fresh items from every source in data/sources.yaml and normalise them into leads."""
import hashlib
import html
import json
import re
import urllib.parse
import urllib.request

import feedparser
import yaml

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


def _json(name, url, kind, fmt=None, markets=None) -> list[dict]:
    data = _get_json(url)
    out = []
    name_fmt = fmt or name
    if name_fmt == "remoteok":
        for d in data:
            if isinstance(d, dict) and d.get("position"):
                out.append(_lead(name, kind, d["position"], d.get("url", ""), d.get("description", ""),
                                 d.get("company", ""), d.get("date", "")))
    elif name_fmt == "remotive":
        for d in data.get("jobs", []):
            out.append(_lead(name, kind, d["title"], d["url"], d.get("description", ""),
                             d.get("company_name", ""), d.get("publication_date", "")))
    elif name_fmt == "freelancer":  # Freelancer.com public projects API (filtered by skill IDs in the URL)
        for d in (data.get("result") or {}).get("projects", []):
            budget = d.get("budget") or {}
            cur = (d.get("currency") or {}).get("code", "")
            pay = f"Budget {budget.get('minimum', '?')}-{budget.get('maximum', '?')} {cur} ({d.get('type', '')}). "
            out.append(_lead(name, kind, d["title"], "https://www.freelancer.com/projects/" + d.get("seo_url", ""),
                             pay + (d.get("description") or d.get("preview_description") or ""), "",
                             d.get("time_submitted", "")))
    elif name_fmt == "braintrust":
        for d in data.get("results", []):
            budget = f"${d.get('budget_minimum_usd')}-{d.get('budget_maximum_usd')} {d.get('payment_type', '')}. "
            out.append(_lead(name, kind, d["title"], f"https://app.usebraintrust.com/jobs/{d['id']}/",
                             budget + str(d.get("introduction") or d.get("description") or ""),
                             (d.get("employer") or {}).get("name", ""), d.get("created", "")))
    elif name_fmt == "jobicy":
        for d in data.get("jobs", []):
            out.append(_lead(name, kind, d["jobTitle"], d["url"], d.get("jobExcerpt") or d.get("jobDescription", ""),
                             d.get("companyName", ""), d.get("pubDate", "")))
    elif name_fmt == "workingnomads":
        for d in data:
            out.append(_lead(name, kind, d["title"], d["url"], d.get("description", ""), d.get("company_name", ""),
                             d.get("pub_date", "")))
    elif name_fmt == "himalayas":
        for d in data.get("jobs", []):
            out.append(_lead(name, kind, d["title"], d.get("applicationLink") or d.get("guid", ""),
                             d.get("excerpt") or d.get("description", ""), d.get("companyName", ""),
                             d.get("pubDate", "")))
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
                         d.get("updated_at", "")))
    elif ats == "lever":
        for d in data:
            cat = d.get("categories") or {}
            rows.append((d.get("text", ""), d.get("hostedUrl", ""),
                         f"{cat.get('location', '')} {d.get('workplaceType', '')}", d.get("createdAt", "")))
    elif ats == "ashby":
        for d in data.get("jobs", []):
            loc = f"{d.get('location', '')} {d.get('workplaceType', '')} {'remote' if d.get('isRemote') else ''}"
            rows.append((d["title"], d.get("jobUrl", ""), loc, d.get("publishedAt", "")))
    out = []
    for title, url, loc, published in rows:
        if remote_only and "remote" not in loc.lower():
            continue
        lead = _lead(name, "job", title, url, f"{company} careers page. Location: {loc}", company, published)
        lead["market_hint"] = list(markets)
        out.append(lead)
    return out


def collect() -> tuple[list[dict], list[str]]:
    """Run every collector. One broken source never stops the others."""
    cfg = yaml.safe_load((DATA / "sources.yaml").read_text())
    leads, errors = [], []
    jobs = [("news:" + n.get("region", "US"), _rss, (_gnews_url(n["q"], n.get("region", "US")), "funding"))
            for n in cfg.get("news", [])]
    jobs += [(s["name"], _rss, (s["url"], s["kind"], s.get("title_must_include"))) for s in cfg.get("rss", [])]
    jobs += [(s["name"], _json, (s["url"], s["kind"], s.get("format"), s.get("markets"))) for s in cfg.get("json", [])]
    jobs += [("careers:" + c["slug"], _careers, (c["ats"], c["slug"], c["company"], c.get("markets", []),
                                                 c.get("remote_only", True))) for c in cfg.get("careers", [])]
    for name, fn, args in jobs:
        try:
            leads += [l for l in fn(name, *args) if l["url"]]
        except Exception as ex:  # noqa: BLE001 — report and continue
            errors.append(f"{name}: {ex}")
    return leads, errors
