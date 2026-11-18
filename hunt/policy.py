"""Rule checker for bids, adapted from the owner's Upwork bot (upwork-auto-live/proposal_policy.py).

Every bid is checked before the owner sees it. Each broken rule costs an equal share of the score
(RULE_COUNT rules, PASS_PERCENT to pass). A few rules are hard blockers: break one and the bid fails,
whatever the score.

Freelancer-specific changes from the Upwork version: no links at all, no contact details and no
other platform names in a bid (Freelancer bans these before a project is awarded). The bid amount is
set separately, so the text must not name a price.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import DATA

RULES_VERSION = "2026-11-16-v1"
RULE_COUNT = 22
PASS_PERCENT = 80.0
MIN_WORDS, MAX_WORDS = 60, 200

HARD = (
    "empty bid",
    "mentions a price",
    "offers free sample/trial",
    "unverified number",
    "contains a link",
    "contains contact details",
    "mentions another platform",
    "missing client-required ending",
)

PORTFOLIO_TEXT = (DATA / "portfolio.yaml").read_text()

_EMOJI_START = re.compile(r"^[\U0001F300-\U0001FAFF☀-➿]")
_BANNED_OPENING = re.compile(
    r"^(?:hi[,!]?\s+|hello[,!]?\s+|dear\s+\w+[,!]?\s+)?"
    r"(?:you(?:'re| are) looking for|you need someone|i understand you need|"
    r"i(?:'ve| have) read your (?:job|project) description|i can help you with|hire me\b|"
    r"i am (?:very )?interested in your project)",
    re.I,
)
_OFFER = re.compile(
    r"\b(?:free|unpaid|complimentary)\b(?:\W+\w+){0,5}\W+\b(?:sample|pilot|trial|test|audit|review|mockup)\b|"
    r"\b(?:sample|pilot|trial|test)\b(?:\W+\w+){0,3}\W+\b(?:for free|free of charge|at no cost|on me)\b|"
    r"\b(?:unlimited\s+revisions?|money-back\s+guarantee)\b",
    re.I,
)
_CLIENT_REQUIRES_SAMPLE = re.compile(
    r"\b(?:must|required?|please)\b(?:\W+\w+){0,5}\W+\b(?:complete|do|take|create|send|provide)\b"
    r"(?:\W+\w+){0,5}\W+\b(?:sample|test|trial)\b",
    re.I,
)
_AI_FILLER = re.compile(
    r"\b(?:delve|utilize|cutting-edge|game-changer|seamless(?:ly)?|meticulous(?:ly)?|"
    r"passionate(?: about)?|excited to apply|thrilled to|perfect fit|ideal candidate|"
    r"high-quality|robust|look no further|i am confident that|unlock the potential(?: of)?|"
    r"in today['’]s fast-paced world|ever-evolving landscape|dive into|at the end of the day)\b",
    re.I,
)
_GENERIC_CTA = re.compile(
    r"^(?:i look forward to hearing from you|looking forward to hearing from you|let'?s discuss|"
    r"feel free to reach out|hope to hear from you|let me know|happy to chat|"
    r"i am ready to get started|i can start right away|i am available immediately|ready when you are)[.!]?$",
    re.I,
)
_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|io|net|org|app|site|co|me|xyz)(?:/\S*)?\b", re.I)
_CONTACT = re.compile(
    r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\+?\d[\d\s().-]{8,}\d|"
    r"\b(?:whatsapp|telegram|skype|discord|signal app|wechat|call me|email me)\b",
    re.I,
)
_OTHER_PLATFORM = re.compile(r"\b(?:upwork|fiverr|toptal|peopleperhour|guru\.com|contra)\b", re.I)
_PRICE = re.compile(r"[$€£₹]\s*\d|\b\d[\d,.]*\s*(?:usd|eur|gbp|inr|dollars?|per hour|/hr|/hour)\b", re.I)
_CLIENT_ASKS_PRICE = re.compile(
    r"\b(?:quote|your rate|hourly rate|price|cost|budget)\b(?:\W+\w+){0,6}\W+\b(?:include|state|mention|tell|share|provide)\b|"
    r"\b(?:include|state|mention|tell|share|provide)\b(?:\W+\w+){0,6}\W+\b(?:quote|rate|price|cost|budget)\b",
    re.I,
)


@dataclass(frozen=True)
class Decision:
    score: float
    accepted: bool
    violations: tuple[str, ...]
    blockers: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"version": RULES_VERSION, "score": self.score, "accepted": self.accepted,
                "violations": list(self.violations), "blockers": list(self.blockers)}


def _words(text: str) -> list[str]:
    return re.findall(r"\b[\w’'-]+\b", text or "")


def _numbers(text: str) -> set[str]:
    clean = _URL.sub("", text or "")
    return {m.group(0).lower().replace(",", "").rstrip(".") for m in re.finditer(
        r"(?<![\w])\d[\d,]*(?:\.\d+)?(?:[kmb%+]|\s*(?:million|billion))?", clean, re.I)}


def _meaningful(text: str) -> set[str]:
    stop = {"about", "after", "also", "before", "could", "first", "from", "have", "into", "their", "there",
            "these", "this", "those", "want", "with", "would", "your", "ready", "start", "send", "project",
            "work", "deliver", "complete", "discuss", "available", "help", "client", "apply", "proposal",
            "need", "needs", "looking", "will", "what", "when", "which", "that", "they", "them"}
    out = set()
    for raw in re.findall(r"\b[A-Za-z]{4,}\b", text or ""):
        w = raw.lower()
        for suffix in ("ing", "ed", "es", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                w = w[: -len(suffix)]
                break
        if w not in stop:
            out.add(w)
    return out


def _client_max_words(job: str) -> int | None:
    m = re.search(r"\b(?:under|max(?:imum)?(?:\s+of)?|no\s+more\s+than|within)\s+(\d{1,3})\s+words?\b", job or "", re.I)
    return int(m.group(1)) if m else None


def _client_required_ending(job: str) -> str | None:
    m = re.search(r"\b(?:end|finish|start|begin)\s+(?:your\s+|the\s+)?(?:bid|proposal|application|reply)"
                  r"\s+with\s+(?:the\s+word\s+)?[\"'“‘]?([^\n\"'”’.,;]{1,40})[\"'”’]?", job or "", re.I)
    if not m or not re.match(r"(?:end|finish)", m.group(0), re.I):
        return None
    return m.group(1).strip()


def validate(text: str, *, job_text: str = "", owner_name: str = "") -> list[str]:
    """Return the list of broken rules. Empty means every rule passed."""
    text = (text or "").strip()
    if not text:
        return ["empty bid"]
    v: list[str] = []
    words = _words(text)
    client_max = _client_max_words(job_text)
    if len(words) < MIN_WORDS and not (client_max and client_max < MIN_WORDS):
        v.append(f"under {MIN_WORDS} words")
    limit = client_max or MAX_WORDS
    if len(words) > limit:
        v.append(f"over {limit} words")
    if _EMOJI_START.match(text):
        v.append("opens with emoji")
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    if _BANNED_OPENING.search(first):
        v.append("opens by restating the job")
    if len(_words(re.split(r"(?<=[.!?])\s", first)[0])) > 25:
        v.append("opening sentence too long")
    if "—" in text or "–" in text:
        v.append("contains long dash")
    if _PRICE.search(text) and not _CLIENT_ASKS_PRICE.search(job_text or ""):
        v.append("mentions a price")
    if re.search(r"\b(?:we|our|us)\b", re.sub(r"\blet['’]?s\b", "", text, flags=re.I)):
        v.append("uses we/our/us")
    if re.search(r"\bmy\s+(?:team|company|agency|studio)\b", text, re.I):
        v.append("mentions team/agency")
    if owner_name and owner_name.lower() not in text.splitlines()[-1].lower():
        v.append("missing sign-off with name")
    if _OFFER.search(text) and not _CLIENT_REQUIRES_SAMPLE.search(job_text or ""):
        v.append("offers free sample/trial")
    if _AI_FILLER.search(text):
        v.append("contains AI filler words")
    unknown = sorted(_numbers(text) - _numbers(job_text + "\n" + PORTFOLIO_TEXT))
    if unknown:
        v.append("unverified number: " + ", ".join(unknown))
    if _URL.search(text):
        v.append("contains a link")
    if _CONTACT.search(text):
        v.append("contains contact details")
    if _OTHER_PLATFORM.search(text):
        v.append("mentions another platform")
    for para in re.split(r"\n\s*\n", text):
        if len(re.findall(r"[.!?]+(?:\s|$)", para.strip())) > 3:
            v.append("paragraph over 3 sentences")
            break
    if any(len(_words(s)) > 35 for s in re.split(r"(?<=[.!?])\s+", text)):
        v.append("sentence over 35 words")
    body_lines = [l.strip() for l in text.splitlines() if l.strip()]
    if owner_name and body_lines and owner_name.lower() in body_lines[-1].lower():
        body_lines = body_lines[:-1]
    if body_lines and body_lines[-1].lower().rstrip(",") in ("best", "thanks", "regards", "best regards", "cheers"):
        body_lines = body_lines[:-1]
    last = body_lines[-1] if body_lines else ""
    if _GENERIC_CTA.match(last):
        v.append("ends with generic line")
    ending = _client_required_ending(job_text)
    if ending:
        if ending.lower() not in text.lower():
            v.append("missing client-required ending")
    elif len(_meaningful(job_text)) >= 6 and not (_meaningful(job_text) & _meaningful(last)):
        v.append("closing not about their project")
    return v


def assess(text: str, *, job_text: str = "", owner_name: str = "") -> Decision:
    unique = tuple(dict.fromkeys(validate(text, job_text=job_text, owner_name=owner_name)))
    failed = min(len(unique), RULE_COUNT)
    score = round(100.0 * (RULE_COUNT - failed) / RULE_COUNT, 1)
    hard = tuple(x for x in unique if x.startswith(HARD))
    accepted = score >= PASS_PERCENT and not hard
    return Decision(score, accepted, unique, () if accepted else (hard or unique))
