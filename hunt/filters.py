"""Cheap keyword pre-filter: keeps the AI bill small by dropping obvious non-matches first."""
import re

from .config import JOB_EXCLUDE, MARKET_WORDS, ROLE_WORDS


def _has(text: str, word: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", text) is not None


def markets_in(text: str) -> list[str]:
    text = text.lower()
    return [m for m, words in MARKET_WORDS.items() if any(_has(text, w) for w in words)]


def has_role(text: str) -> bool:
    text = text.lower()
    return any(_has(text, w) for w in ROLE_WORDS)


# News must describe a buying moment, not market chatter.
SIGNAL = re.compile(r"\b(raise[sd]?|raising|funding|funded|seed|pre-seed|series [a-d]|launch(es|ed|ing)?|"
                    r"presale|ico|ido|tge|licen[cs]e[sd]?|approval|approved|backed|valuation|unveils|debuts|"
                    r"expands|hiring)\b", re.I)
NOISE = re.compile(r"\b(price prediction|stock price|price, news|hack(ed|s)?|heist|lawsuit|sues|arrest(ed)?|"
                   r"scam|fraud|says|explained|what is|how to|best crypto|vs\.?|shares by|acquires \$)", re.I)


def keep(lead: dict) -> tuple[bool, list[str]]:
    """Return (worth sending to AI?, markets matched)."""
    text = f"{lead['title']} {lead['company']} {lead['summary']}"
    markets = markets_in(text) or lead.get("market_hint", [])
    if not markets:
        return False, []
    if lead["kind"] == "job":
        title = lead["title"].lower()
        if not has_role(title) or any(_has(title, w.strip()) for w in JOB_EXCLUDE):
            return False, markets
    if lead["kind"] == "funding" and (not SIGNAL.search(lead["title"]) or NOISE.search(lead["title"])):
        return False, markets
    return True, markets


KIND_POINTS = {"ask": 15, "funding": 10, "job": 5}


def prescore(lead: dict, kept: bool, markets: list[str]) -> int:
    """Free 0–100 guess of how good a lead is, from keywords only. It only orders the AI's waiting
    line (best first); it never removes a lead."""
    title = (lead.get("title") or "").lower()
    score = 12 * min(len(markets), 3) + KIND_POINTS.get(lead.get("kind"), 0)
    if kept:
        score += 30
    if has_role(title):
        score += 15
    if any(_has(title, w.strip()) for w in JOB_EXCLUDE):
        score -= 15
    if lead.get("kind") == "funding" and SIGNAL.search(title):
        score += 5
    return max(0, min(100, score))


# ---- Contract-only gate (2026-11-20): full-time roles are dropped on arrival, before any AI. ----
# A site's own job-type label wins (Himalayas, Remotive, Jobicy, Lever, Ashby, RemoteOK tags).
# Without a label, the post's words decide. "smart contract" (crypto) never counts as a contract job.
TYPE_CONTRACT = re.compile(r"contract|freelanc|temporary|\btemp\b|fixed[- ]?term|project|gig|hourly", re.I)
TEXT_CONTRACT = re.compile(
    r"\bcontractors?\b|\bfreelanc(e|er|ers|ing)\b|\bcontract[- ]to[- ]hire\b|\bfixed[- ]term\b|\bproject[- ]based\b|"
    r"\b1099\b|\bhourly rate\b|\bper hour\b|\b(on a |short[- ]term |long[- ]term |paid |part[- ]time |remote )?"
    r"contract (role|position|basis|job|work|opportunity|engagement|gig|assignment)\b|\(contract\)|[-–|] contract\b",
    re.I)
SMART_CONTRACT = re.compile(r"\bsmart[- ]contracts?\b", re.I)
FULL_TIME = re.compile(r"\bfull[- ]?time\b|\bpermanent\b", re.I)


_ALIAS = {"us": "united states", "usa": "united states", "u.s.": "united states", "u.s.a.": "united states",
          "america": "united states", "uk": "united kingdom", "u.k.": "united kingdom", "uae": "united arab emirates"}
_PLACE = r"(?i:the\s+)?([A-Z][\w.]*(?:[ -][A-Z][\w.]*){0,3})"
_LIMIT = [re.compile(r, re.I if i else 0) for i, r in enumerate((
    r"(?i:must|need to|needs to|required to|should|have to)\s+(?i:be\s+)?(?i:currently\s+)?"
    r"(?i:reside|residing|live|living|located|based)\s+(?i:in|within)\s+" + _PLACE,
    r"(?:only|exclusively)\s+(?:open\s+)?(?:to\s+)?(?:candidates|applicants|residents|people)\s+(?:in|from|of|based in)\s+"
    r"(?:the\s+)?([a-z][\w.]*(?:[ -][a-z][\w.]*){0,3})",
    r"\b(us|usa|u\.s\.|uk|u\.k\.)[- ](?:only|based only|residents only)\b",
))]


def countries_in_text(text: str) -> list[str]:
    """Country limits written in the job text, e.g. "candidates must reside in the United States"."""
    out = []
    for rx in _LIMIT:
        for m in rx.finditer(text or ""):
            place = m.group(1).strip(" .").lower()
            place = _ALIAS.get(place, place)
            if place and not re.search(r"\b(any|world|global|remote|time ?zone|europe|emea|apac|latam)", place):
                out.append(place)
    return list(dict.fromkeys(out))


def wrong_country(lead: dict, allowed: set) -> bool:
    """The job limits who may apply (site tag or job text) and none of the owner's countries is allowed."""
    if not allowed:
        return False
    only = [c.lower() for c in (lead.get("extra") or {}).get("countries") or []]
    only += countries_in_text(f"{lead.get('title', '')}\n{lead.get('summary', '')}")
    return bool(only) and not any(a in c or c in a for c in only for a in allowed)


def blocked(title: str) -> bool:
    """Title names work the owner never does (JOB_EXCLUDE). Applies even to trusted searches."""
    title = (title or "").lower()
    return any(_has(title, w.strip()) for w in JOB_EXCLUDE)


def is_contract(lead: dict) -> bool:
    """Is this a contract / freelance job? Asks and `contract: always` sources are freelance by nature."""
    if lead.get("kind") == "ask" or (lead.get("extra") or {}).get("contract_always"):
        return True
    job_type = str((lead.get("extra") or {}).get("job_type") or "").strip()
    if job_type:
        return bool(TYPE_CONTRACT.search(job_type))
    title = lead.get("title") or ""
    text = SMART_CONTRACT.sub(" ", f"{title} {lead.get('summary') or ''}")
    if FULL_TIME.search(title):
        return False
    return bool(TEXT_CONTRACT.search(text))
