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
    markets = markets_in(text)
    if not markets:
        return False, []
    if lead["kind"] == "job":
        title = lead["title"].lower()
        if not has_role(title) or any(_has(title, w.strip()) for w in JOB_EXCLUDE):
            return False, markets
    if lead["kind"] == "funding" and (not SIGNAL.search(lead["title"]) or NOISE.search(lead["title"])):
        return False, markets
    return True, markets
