"""Dashboard settings (PRD §15): options the owner changes on the website, read by the hourly run.

Stored in the database (meta key `settings`, JSON). A setting nobody has changed falls back to the
default the code passes in, which comes from .env (e.g. HUNT_FOLLOWUPS) — so a fresh install behaves
exactly as before, and .env stays the place for secrets and server details.
"""
import json

KEY = "settings"
LANES = ("writing", "signals", "design", "product", "development", "coaching")
APPROVAL_MODES = ("one", "bulk")


def _all(con) -> dict:
    from . import store
    try:
        return json.loads(store.meta_get(con, KEY) or "{}")
    except ValueError:
        return {}


def get(con, name: str, default=None):
    """The saved value, or `default` (the .env / built-in value) if the owner never changed it."""
    v = _all(con).get(name)
    return default if v is None else v


def clean(name: str, raw: str):
    """Turn a form value into a stored value. Raises ValueError with a plain message if it is wrong."""
    raw = (raw or "").strip()
    if name in ("followups", "learn_edits", "business_hours"):
        return raw == "on"
    if name == "approval_mode":
        if raw not in APPROVAL_MODES:
            raise ValueError("approval mode must be one-by-one or bulk")
        return raw
    if name == "followup_days":
        days = [int(x) for x in raw.replace(" ", "").split(",") if x]
        if not days or len(days) > 4 or any(d < 1 or d > 60 for d in days):
            raise ValueError("follow-up days: 1 to 4 numbers between 1 and 60, e.g. 3,7")
        return days
    if name == "inbox_daily":
        n = int(raw)
        if not 1 <= n <= 200:
            raise ValueError("daily email limit per inbox must be 1–200")
        return n
    if name in ("followup_lanes", "active_lanes"):
        lanes = [x for x in raw.split(",") if x in LANES]
        if name == "active_lanes" and not lanes:
            raise ValueError("keep at least one lane active")
        return lanes
    raise ValueError(f"unknown setting {name}")


def save(con, name: str, value, who: str) -> None:
    from . import store
    data = _all(con)
    old = data.get(name)
    data[name] = value
    store.meta_set(con, KEY, json.dumps(data))
    store.log(con, who, "changed setting", name, f"{old!r} → {value!r}")
