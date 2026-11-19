"""Source registry (PRD §6): every place Hunt looks, with its layer, automation tier, on/off switch
and auto-send mode.

Nothing has to be filled in: defaults come from the source's group and name. Any line in
data/sources.yaml may override them with `layer:`, `tier:`, `enabled: false` or `auto_send: on`.
Sources that are not lines of their own (Google News regions, LinkedIn alert emails) are overridden
under a top-level `overrides:` map, e.g.  overrides: {linkedin_alerts: {enabled: false}}.

The dashboard's Sources screen saves its switches in the database (meta `source_overrides`); they are
applied last. A tier can only be made stricter there, never looser than sources.yaml / the default —
so a platform that bans automation can never be switched to auto-send from the website.

Layers: 1 = fixed job feeds, 2 = intent search on the open web, 3 = prospect hunting.
Tiers:  open = official API/RSS, safe to read and (if allowed) auto-send;
        limited = official API only, within its rate limits;
        manual = platform bans automation: Hunt drafts, the owner sends by hand.
"""
import json

import yaml

from .config import DATA

TIERS = ("open", "limited", "manual")
GROUP_LAYER = {"news": 3, "rss": 1, "json": 1, "careers": 1, "gmail": 1, "youtube": 3}
MANUAL_WORDS = ("linkedin", "tradingview", "mql5", "upwork", "instagram", "telegram_channel")
LIMITED_WORDS = ("reddit", "discord", "producthunt")


def default_tier(name: str) -> str:
    n = name.lower()
    if any(w in n for w in MANUAL_WORDS):
        return "manual"
    if any(n.startswith(w) for w in LIMITED_WORDS):
        return "limited"
    return "open"


def _entry(name: str, group: str, item: dict) -> dict:
    tier = str(item.get("tier") or default_tier(name)).lower()
    if tier not in TIERS:
        raise ValueError(f"source {name}: tier must be one of {TIERS}, got {tier!r}")
    wants_auto = item.get("auto_send") in (True, "on", "On", "ON")  # YAML reads a bare `on` as True
    return {
        "name": name,
        "group": group,
        "layer": int(item.get("layer") or GROUP_LAYER[group]),
        "tier": tier,
        "enabled": item.get("enabled", True) is not False,
        # The tier decides whether auto-send is even possible; the owner's setting decides if it is used.
        "auto_send": "on" if wants_auto and tier == "open" else "draft",
    }


def _merge_region(entries: list[dict]) -> dict:
    """Several Google News searches share one region name. Only the switched-on searches count:
    the region is on if any is on, takes the strictest tier among them, the first one's layer,
    and auto-sends only if every one of them does."""
    live = [e for e in entries if e["enabled"]] or entries
    merged = dict(live[0])
    merged["enabled"] = any(e["enabled"] for e in entries)
    merged["tier"] = max((e["tier"] for e in live), key=TIERS.index)
    merged["auto_send"] = "on" if all(e["auto_send"] == "on" for e in live) and merged["tier"] == "open" else "draft"
    return merged


DASH_KEY = "source_overrides"


def saved_overrides() -> dict:
    """The dashboard's switches: name → {enabled, tier, auto_send}. Empty if the database can't be read."""
    try:
        from . import store
        con = store.db()
        try:
            return json.loads(store.meta_get(con, DASH_KEY) or "{}")
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — never block collecting because of the dashboard
        return {}


def apply_overrides(reg: dict, over: dict) -> dict:
    for name, o in (over or {}).items():
        e = reg.get(name)
        if not e or not isinstance(o, dict):
            continue
        tier = o.get("tier") if o.get("tier") in TIERS else e["tier"]
        if TIERS.index(tier) < TIERS.index(e["tier"]):
            tier = e["tier"]  # stricter only
        auto = o.get("auto_send", e["auto_send"])
        reg[name] = {**e, "tier": tier, "enabled": bool(o.get("enabled", e["enabled"])),
                     "auto_send": "on" if auto == "on" and tier == "open" else "draft"}
    return reg


def load(cfg: dict | None = None, overrides: dict | None = None) -> dict[str, dict]:
    """name → registry entry, for every source in sources.yaml plus the built-in ones.
    With no `cfg` (normal use) the dashboard's saved switches are applied too; pass `overrides` to
    apply them to a given cfg."""
    if overrides is None and cfg is None:
        overrides = saved_overrides()
    if cfg is None:
        cfg = yaml.safe_load((DATA / "sources.yaml").read_text()) or {}
    out: dict[str, dict] = {}
    regions: dict[str, list[dict]] = {}
    for q in cfg.get("news", []) or []:
        name = "news:" + q.get("region", "US")
        regions.setdefault(name, []).append(_entry(name, "news", q))
    for name, entries in regions.items():
        out[name] = _merge_region(entries)
    for group in ("rss", "json"):
        for s in cfg.get(group, []) or []:
            out[s["name"]] = _entry(s["name"], group, s)
    for c in cfg.get("careers", []) or []:
        out["careers:" + c["slug"]] = _entry("careers:" + c["slug"], "careers", c)
    out["linkedin_alerts"] = _entry("linkedin_alerts", "gmail", {})
    if cfg.get("youtube"):  # Phase 1: finance YouTube channels (official API)
        out["youtube"] = _entry("youtube", "youtube", cfg["youtube"])
    for name, over in (cfg.get("overrides") or {}).items():
        if name in out:
            merged = {k: v for k, v in out[name].items() if k in ("layer", "tier", "enabled")}
            merged["auto_send"] = "on" if out[name]["auto_send"] == "on" else None
            merged.update(over or {})
            out[name] = _entry(name, out[name]["group"], merged)
    return apply_overrides(out, overrides) if overrides else out


def enabled(name: str, reg: dict | None = None) -> bool:
    reg = load() if reg is None else reg
    return reg.get(name, {"enabled": True})["enabled"]


def can_auto_send(name: str, reg: dict | None = None) -> bool:
    reg = load() if reg is None else reg
    e = reg.get(name)
    return bool(e) and e["auto_send"] == "on" and e["tier"] == "open"
