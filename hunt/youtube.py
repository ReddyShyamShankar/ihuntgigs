"""Phase 1 source (PRD §18): finance YouTube channels, found through the official YouTube Data API.

Every channel found becomes a lead (kind `prospect`) and a prospect row. No filters on size or quality:
everyone is stored. The AI does not touch these here — research (B5) picks them up later.

Quota (resets at midnight US Pacific time; default allowance 10,000 units a day):
  search.list = 100 units (up to 50 channels) · channels.list = 1 unit per 50 channels ·
  playlistItems.list = 1 unit per channel (its last 10 uploads).
So one search that finds 50 channels costs about 151 units. Hunt counts every unit it spends and
stops before the daily budget (`daily_units` in sources.yaml, default 9,000 — 1,000 kept spare).

Settings live under `youtube:` in data/sources.yaml (queries, daily_units, searches_per_run).
Each query remembers its next results page, so every run digs deeper instead of repeating page 1.
"""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from .sources import UA, _lead

API = "https://www.googleapis.com/youtube/v3/"
COST = {"search": 100, "channels": 1, "playlistItems": 1}
SOURCE = "youtube"
DEFAULTS = {"daily_units": 9000, "searches_per_run": 2, "videos_per_channel": 10}


class QuotaSpent(Exception):
    """Today's YouTube budget is used up; try again after midnight Pacific."""


def api_key() -> str:
    return os.environ.get("YOUTUBE_API_KEY", "")


def enabled() -> bool:
    return bool(api_key())


def _today_pacific() -> str:
    return dt.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def units_used(con) -> int:
    from . import store
    return int(store.meta_get(con, "yt_units:" + _today_pacific(), "0"))


def _spend(con, units: int, budget: int) -> None:
    from . import store
    used = units_used(con)
    if used + units > budget:
        raise QuotaSpent(f"YouTube budget {budget} units/day reached ({used} used)")
    store.meta_set(con, "yt_units:" + _today_pacific(), used + units)


def _http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _call(con, endpoint: str, params: dict, budget: int, fetch=_http_get) -> dict:
    _spend(con, COST[endpoint], budget)  # counted before the call: a failed call still costs quota
    q = urllib.parse.urlencode({**params, "key": api_key()})
    return fetch(f"{API}{endpoint}?{q}")


def channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}"


def _channel_lead(ch: dict, videos: list[dict], query: str) -> dict:
    sn, st = ch.get("snippet", {}), ch.get("statistics", {})
    subs = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0) or 0)
    titles = "; ".join(v["title"] for v in videos[:10])
    summary = (f"Subscribers: {subs if subs is not None else 'hidden'} · videos: {st.get('videoCount', '?')} · "
               f"country: {sn.get('country', '?')}. {sn.get('description', '')} Recent videos: {titles}")
    lead = _lead(SOURCE, "prospect", sn.get("title", ""), channel_url(ch["id"]),
                 summary, sn.get("title", ""), sn.get("publishedAt", ""))
    lead["title_key"] = "youtube:" + ch["id"]  # two channels can share a display name; the ID is unique
    lead["extra"] = {"channel_id": ch["id"], "handle": sn.get("customUrl", ""), "country": sn.get("country", ""),
                     "subscribers": subs, "video_count": int(st.get("videoCount", 0) or 0),
                     "view_count": int(st.get("viewCount", 0) or 0),
                     "description": sn.get("description", "")[:5000], "recent_videos": videos[:10],
                     "found_by": query}
    return lead


def collect(con, cfg: dict, fetch=_http_get) -> tuple[list[dict], dict]:
    """Run up to `searches_per_run` searches within today's budget. Returns (leads, stats).
    Channels already in the database are skipped before any per-channel quota is spent."""
    from . import store
    ycfg = {**DEFAULTS, **(cfg or {})}
    queries = ycfg.get("queries") or []
    budget = int(ycfg["daily_units"])
    stats = {"count": 0, "error": None}
    if not queries or not enabled():
        return [], stats
    leads: list[dict] = []
    start = int(store.meta_get(con, "yt_next_query", "0")) % len(queries)
    try:
        for i in range(min(int(ycfg["searches_per_run"]), len(queries))):
            qi = (start + i) % len(queries)
            query = queries[qi]
            page_key = f"yt_page:{query}"
            params = {"part": "snippet", "type": "channel", "q": query, "maxResults": 50,
                      "relevanceLanguage": "en"}
            token = store.meta_get(con, page_key, "")
            if token:
                params["pageToken"] = token
            found = _call(con, "search", params, budget, fetch)

            def page_done(found=found, page_key=page_key, qi=qi):
                # Move the cursor only once every channel on this page is collected. If the budget runs out
                # or a call fails mid-page, the next run searches the same page again; channels already
                # saved are skipped before any per-channel quota is spent.
                store.meta_set(con, page_key, found.get("nextPageToken", ""))  # empty = start over next time
                store.meta_set(con, "yt_next_query", qi + 1)
            ids = list(dict.fromkeys(it["snippet"]["channelId"] for it in found.get("items", [])
                                     if it.get("snippet", {}).get("channelId")))
            seen = {l["url"] for l in leads}
            ids = [c for c in ids if channel_url(c) not in seen
                   and store.get(con, _lead(SOURCE, "prospect", "", channel_url(c))["id"]) is None]
            if not ids:
                page_done()
                continue
            chans = _call(con, "channels", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids),
                                            "maxResults": 50}, budget, fetch).get("items", [])
            for ch in chans:
                uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                videos = []
                if uploads:
                    try:
                        got = _call(con, "playlistItems", {"part": "snippet", "playlistId": uploads,
                                                           "maxResults": int(ycfg["videos_per_channel"])},
                                    budget, fetch)
                        videos = [{"title": v["snippet"].get("title", ""),
                                   "published": v["snippet"].get("publishedAt", ""),
                                   "description": v["snippet"].get("description", "")[:2000]}
                                  for v in got.get("items", [])]
                    except QuotaSpent:
                        raise
                    except Exception:  # noqa: BLE001 — a channel with no public uploads still counts
                        videos = []
                leads.append(_channel_lead(ch, videos, query))
            page_done()
    except QuotaSpent as ex:
        stats["note"] = str(ex)  # running out of budget is normal, not a breakage
    except Exception as ex:  # noqa: BLE001 — report on the Sources screen, keep what we have
        stats["error"] = str(ex)[:300]
    stats["count"] = len(leads)
    return leads, stats
