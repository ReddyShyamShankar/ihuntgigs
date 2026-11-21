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


_GLOBAL = re.compile(r"\b(any(where)?|world ?wide|global(ly)?|international|time ?zones?|all countries)\b", re.I)
# Regions a job may be limited to, and the countries inside them (only the ones that matter for matching).
REGIONS = {
    "emea": {"united arab emirates", "saudi arabia", "qatar", "egypt", "south africa", "nigeria", "united kingdom"},
    "middle east": {"united arab emirates", "saudi arabia", "qatar", "bahrain", "oman", "kuwait", "jordan", "israel"},
    "mena": {"united arab emirates", "saudi arabia", "qatar", "bahrain", "oman", "kuwait", "egypt", "morocco"},
    "gcc": {"united arab emirates", "saudi arabia", "qatar", "bahrain", "oman", "kuwait"},
    "apac": {"india", "singapore", "australia", "japan", "philippines", "indonesia", "vietnam", "malaysia"},
    "asia": {"india", "singapore", "japan", "philippines", "indonesia", "vietnam", "malaysia", "united arab emirates"},
    "asia pacific": {"india", "singapore", "australia", "japan", "philippines", "indonesia", "malaysia"},
    "south asia": {"india", "pakistan", "bangladesh", "sri lanka", "nepal"},
}
_LOCATION = re.compile(r"\bLocation:\s*([^\n]{1,160}?)(?:\.\s|\.$|\n|$)")
_NOT_PLACE = re.compile(r"\b(remote|hybrid|on-?site|office|hq|headquarters|home|based|first|only|friendly)\b|[()]", re.I)


def _places_in_location(loc: str) -> list[str]:
    """ "Remote - USA; Poland (Remote)" → ["united states", "poland"]. Empty = anywhere."""
    out = []
    for part in re.split(r"[;,/|]| or | and ", loc or ""):
        place = re.sub(r"\s+", " ", _NOT_PLACE.sub(" ", part)).strip(" -–.:").lower()
        if place and not _GLOBAL.search(place):
            out.append(_ALIAS.get(place, place))
    return out


def countries_in_text(text: str) -> list[str]:
    """Country limits written in the job text, e.g. "candidates must reside in the United States",
    or a job-page location such as "Location: Remote - USA"."""
    out = []
    for rx in _LIMIT:
        for m in rx.finditer(text or ""):
            place = m.group(1).strip(" .").lower()
            place = _ALIAS.get(place, place)
            if place and not re.search(r"\b(remote|any|world|global|time ?zone)", place):
                out.append(place)
    for m in _LOCATION.finditer(text or ""):
        places = _places_in_location(m.group(1))
        if places and len(places) == len(re.split(r"[;,/|]| or | and ", m.group(1))):
            out += places  # every part names a place: the job is limited to those
    return list(dict.fromkeys(out))


def _allowed_place(place: str, allowed: set) -> bool:
    return any(a in place or place in a for a in allowed) or bool(REGIONS.get(place, set()) & allowed)


# "UAE Nationals", "Emiratis only", "Saudi nationals only": a citizenship rule, not a place
NATIONALS = re.compile(r"\b(emirati(s|sation)?|[a-z]+ nationals?( only)?|citizens? only|nationals? only)\b", re.I)


def citizens_only(lead: dict) -> bool:
    """Only citizens of one country may apply. The owner is an Indian citizen, so Indian-only is fine."""
    m = NATIONALS.search(f"{lead.get('title', '')}\n{lead.get('summary', '')}")
    return bool(m) and "indian" not in m.group(0).lower()


def wrong_country(lead: dict, allowed: set) -> bool:
    """The job limits who may apply (site tag, job text, or citizens only) and the owner can't."""
    if not allowed:
        return False
    if citizens_only(lead):
        return True
    only = [c.lower() for c in (lead.get("extra") or {}).get("countries") or []]
    only += countries_in_text(f"{lead.get('title', '')}\n{lead.get('summary', '')}")
    return bool(only) and not any(_allowed_place(c, allowed) for c in only)


def blocked(title: str) -> bool:
    """Title names work the owner never does (JOB_EXCLUDE). Applies even to trusted searches."""
    title = (title or "").lower()
    return any(_has(title, w.strip()) for w in JOB_EXCLUDE)


REMOTE = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed)\b", re.I)
NOT_REMOTE = re.compile(r"\b(hybrid|on-?site|in[- ]office|office[- ]based|work(ing)? from (the )?office|wfo|relocat\w*)\b", re.I)


def is_remote(lead: dict) -> bool:
    """Remote job? Remote-only boards (`remote: always`) count as remote; elsewhere the post must say so."""
    if (lead.get("extra") or {}).get("remote_always"):
        return True
    text = f"{lead.get('title') or ''} {lead.get('summary') or ''}"
    return bool(REMOTE.search(text)) and not NOT_REMOTE.search(text)


def wanted(lead: dict) -> bool:
    """Decision #31 (2026-11-21): contract work anywhere, or a full-time job if it is remote."""
    return is_contract(lead) or is_remote(lead)


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
