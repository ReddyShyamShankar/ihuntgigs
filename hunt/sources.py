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
        out.append(_lead(name, kind, title, e.get("link", ""), e.get("summary", ""),
                         company, e.get("published", e.get("updated", ""))))
    return out


def _json(name, url, kind) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    out = []
    if name == "remoteok":
        for d in data:
            if isinstance(d, dict) and d.get("position"):
                out.append(_lead(name, kind, d["position"], d.get("url", ""), d.get("description", ""),
                                 d.get("company", ""), d.get("date", "")))
    elif name == "remotive":
        for d in data.get("jobs", []):
            out.append(_lead(name, kind, d["title"], d["url"], d.get("description", ""),
                             d.get("company_name", ""), d.get("publication_date", "")))
    elif name == "himalayas":
        for d in data.get("jobs", []):
            out.append(_lead(name, kind, d["title"], d.get("applicationLink") or d.get("guid", ""),
                             d.get("excerpt") or d.get("description", ""), d.get("companyName", ""),
                             d.get("pubDate", "")))
    return out


def collect() -> tuple[list[dict], list[str]]:
    """Run every collector. One broken source never stops the others."""
    cfg = yaml.safe_load((DATA / "sources.yaml").read_text())
    leads, errors = [], []
    jobs = [("news:" + n.get("region", "US"), _rss, (_gnews_url(n["q"], n.get("region", "US")), "funding"))
            for n in cfg.get("news", [])]
    jobs += [(s["name"], _rss, (s["url"], s["kind"], s.get("title_must_include"))) for s in cfg.get("rss", [])]
    jobs += [(s["name"], _json, (s["url"], s["kind"])) for s in cfg.get("json", [])]
    for name, fn, args in jobs:
        try:
            leads += [l for l in fn(name, *args) if l["url"]]
        except Exception as ex:  # noqa: BLE001 — report and continue
            errors.append(f"{name}: {ex}")
    return leads, errors
