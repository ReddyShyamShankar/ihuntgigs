#!/usr/bin/env python3
"""Hunt auto-apply helper — runs on the owner's Mac, never on the server.

Flow: the owner taps 🚀 Auto-apply (or /applyall) in Telegram → the lead is "approved" on the
server → this helper fetches approved leads over SSH (no open port on the server) → opens each
job's application form in its own Chrome window → fills it from profile.yaml + the AI pitch →
submits (LIVE) or only fills and screenshots (DRY, the default) → reports back to Telegram.

Never used on Upwork, LinkedIn or MQL5 (account-ban risk; Upwork has its own tool).
Never solves "I'm not a robot" checks: those leads come back as 👋 NEEDS YOU.

Usage (from the repo root):
  python3 applier/hunt_applier.py               # one pass, DRY: fill + screenshot, submit nothing
  python3 applier/hunt_applier.py --live        # one pass, really submit
  python3 applier/hunt_applier.py --live --loop # keep checking every 10 minutes
  python3 applier/hunt_applier.py --url URL     # DRY test on one job page (no server needed)
  python3 applier/hunt_applier.py --setup       # sign in to job boards once in the helper's Chrome
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUNS = HERE / "runs"
PROFILE_FILE = Path(os.environ.get("HUNT_APPLIER_PROFILE", HERE / "profile.yaml"))
CHROME_DIR = Path.home() / ".hunt-applier" / "chrome"  # separate profile: never your main Chrome

NEVER = re.compile(r"(^|\.)(upwork\.com|linkedin\.com|mql5\.com)$", re.I)
ATS = {  # host pattern → name
    r"(^|\.)greenhouse\.io$": "greenhouse",
    r"(^|\.)lever\.co$": "lever",
    r"(^|\.)ashbyhq\.com$": "ashby",
    r"(^|\.)workable\.com$": "workable",
    r"(^|\.)breezy\.hr$": "breezy",
    r"(^|\.)recruitee\.com$": "recruitee",
    r"(^|\.)smartrecruiters\.com$": "smartrecruiters",
}
CAPTCHA = ("iframe[src*='recaptcha/api2/anchor']", "iframe[src*='hcaptcha.com']",
           "iframe[src*='challenges.cloudflare.com']", ".g-recaptcha", ".h-captcha")
SUCCESS = re.compile(r"(thank(s| you)|application (was )?(received|submitted|sent)|we('|’)ve received|successfully submitted)", re.I)


# ---------------- server link (SSH, same key as deploy) ----------------
def _target() -> tuple[str, str]:
    env = {}
    for line in (ROOT / "deploy" / "target.env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').replace("$HOME", str(Path.home()))
    return env["HUNT_HOST"], env.get("HUNT_KEY", str(Path.home() / ".ssh/id_ed25519"))


def hunt(*args: str) -> str:
    """Run `python -m hunt ARGS` on the server as the sandboxed hunt user, with its .env."""
    host, key = _target()
    remote = ("systemd-run --quiet --collect --pipe --wait --uid=hunt --gid=hunt "
              "-p EnvironmentFile=/opt/hunt/.env -p WorkingDirectory=/opt/hunt "
              "/opt/hunt/.venv/bin/python -m hunt " + " ".join(shlex.quote(a) for a in args))
    out = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", host, remote],
                         capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise RuntimeError(f"server said: {out.stderr.strip()[-300:]}")
    return out.stdout


# ---------------- form filling ----------------
def load_profile() -> dict:
    if not PROFILE_FILE.exists():
        sys.exit(f"Missing {PROFILE_FILE}. Copy profile.example.yaml to profile.yaml and fill it in.")
    p = yaml.safe_load(PROFILE_FILE.read_text())
    p["full_name"] = f"{p['first_name']} {p['last_name']}".strip()
    return p


# Boxes where the owner's profile beats anything a form's CV reader pre-filled (old phone, old email, …).
PROFILE_WINS = re.compile(r"\b(first|last|full|given|family|sur)?[\s_-]*name\b|e-?mail|phone|mobile|"
                          r"current (company|employer)|current (title|role|position)|job title", re.I)


# Rough USD → other currency rates, only for answering "expected salary" boxes (rounded anyway).
FX = {"USD": 1.0, "EUR": 0.87, "GBP": 0.76, "AED": 3.67, "INR": 88.0, "CAD": 1.40, "AUD": 1.53, "SGD": 1.30,
      "CHF": 0.80, "SAR": 3.75}
_CUR = [("EUR", r"\beur(o|os)?\b|€"), ("GBP", r"\bgbp\b|£|pounds?"), ("AED", r"\baed\b|dirham"),
        ("INR", r"\binr\b|₹|rupees?|\blpa\b|lakh"), ("CAD", r"\bcad\b"), ("AUD", r"\baud\b"),
        ("SGD", r"\bsgd\b"), ("CHF", r"\bchf\b"), ("SAR", r"\bsar\b|riyal"), ("USD", r"\busd\b|\$|dollars?")]


def pay_answer(p: dict, question: str) -> str:
    """Expected pay for a form box, from the owner's hourly rate (profile `rate`, e.g. "35 USD/hour") and
    weekly hours (`hours_per_week`, e.g. "40+"), in the period and currency the question asks for.
    No rate in the profile → "" (the question goes to the owner)."""
    m = re.search(r"\d+(\.\d+)?", str(p.get("rate", "")))
    if not m:
        return ""
    hourly = float(m.group())
    hours = int((re.search(r"\d+", str(p.get("hours_per_week", ""))) or re.search(r"\d+", "40")).group())
    q = question.lower()
    cur = next((c for c, rx in _CUR if re.search(rx, q)), "USD")
    if re.search(r"\b(annual|annually|yearly|per (year|annum)|a year|/ ?year|p\.?a\.?|ctc|lpa)\b", q):
        amount, period, step = hourly * hours * 52, "per year", 1000
    elif re.search(r"\b(month|monthly|per month|/ ?month)\b", q):
        amount, period, step = hourly * hours * 52 / 12, "per month", 100
    elif re.search(r"\b(week|weekly)\b", q):
        amount, period, step = hourly * hours, "per week", 10
    elif re.search(r"\b(day|daily)\b", q):
        amount, period, step = hourly * 8, "per day", 5
    elif re.search(r"\b(hour|hourly)\b", q):
        amount, period, step = hourly, "per hour", 1
    else:  # "salary expectations" with no period: say it as monthly, the usual default
        amount, period, step = hourly * hours * 52 / 12, "per month", 100
    value = round(amount * FX[cur] / step) * step
    gross = " gross" if "gross" in q or period in ("per month", "per year") else ""
    return f"{value:,.0f} {cur} {period}{gross}"


def owner_answers(p: dict) -> list[tuple[re.Pattern, list[str]]]:
    """profile.yaml `answers:` → [(question pattern, acceptable answers in order of preference)].
    Only these questions are ever answered on tick-one questions; anything else goes to the owner."""
    out = []
    for a in p.get("answers") or []:
        say = a.get("say")
        say = [say] if isinstance(say, str) else list(say or [])
        if a.get("ask") and say:
            out.append((re.compile(a["ask"], re.I), [str(s) for s in say]))
    return out


def answer_choice(p: dict, question: str, options: list[str]) -> int:
    """Index of the option to tick for `question`, or -1. Exact match first, then 'starts with'."""
    for rx, say in owner_answers(p):
        if not rx.search(question):
            continue
        norm = [" ".join(o.split()).lower() for o in options]
        for want in (s.lower() for s in say):
            if want in norm:
                return norm.index(want)
            for i, o in enumerate(norm):
                if o.startswith(want):
                    return i
        # matched, but none of these answers is offered: try the next pattern, else the owner decides
    return -1


def field_rules(p: dict, cover: str) -> list[tuple[re.Pattern, str]]:
    """Label/name/placeholder text → value. First match wins; empty values are skipped."""
    def R(s):
        return re.compile(s, re.I)
    # Owner's own standard answers (profile.yaml `answers:`) win over the built-in rules.
    return [(rx, choices[0]) for rx, choices in owner_answers(p)] + [
        (R(r"first[\s_-]*name|given name|forename"), p["first_name"]),
        (R(r"last[\s_-]*name|surname|family name"), p["last_name"]),
        (R(r"e-?mail"), p["email"]),
        (R(r"phone|mobile|telephone|whatsapp"), p.get("phone", "")),
        (R(r"linkedin"), p.get("linkedin", "")),
        (R(r"github"), p.get("github", "")),
        (R(r"portfolio|website|personal (site|url)|\burls?\b|link to"), p.get("website", "")),
        (R(r"twitter|\bx handle"), p.get("twitter", "")),
        (R(r"^\s*country\b|country of residence|current country|country you (live|reside)"), p.get("country", "")),
        (R(r"\blocation\b|\bcity\b|where are you (based|located)"), p.get("location", "")),
        (R(r"current (company|employer)|organi[sz]ation"), p.get("current_company", "")),
        (R(r"current (title|role|position)|job title"), p.get("current_title", "")),
        # Only total experience; "years with Rust / managing a team" is skill-specific → left for the owner.
        (R(r"(years of (professional |work |total |relevant |overall )?experience|how many years)"
           r"(?![^|]*\b(with|in|using|on|as|managing|leading|of (?!(professional|work|total|overall) )\w))"),
         str(p.get("years_experience", ""))),
        # An hourly-rate box gets the owner's rate; any other pay question (monthly/yearly salary, other
        # currency) is left blank so it goes to the owner instead of a wrong "35 USD/hour".
        # Pay questions: worked out from the owner's hourly rate, in the period + currency the form asks
        # (owner, 2026-11-21: "you give a number based on my profile"). See pay_answer().
        (R(r"salary|compensation|expected pay|pay expectation|remuneration|\bctc\b|hourly rate|rate per hour|"
           r"per hour|\brate\b"), lambda desc: pay_answer(p, desc)),
        (R(r"notice period|start date|when can you start|availability"), p.get("availability", "")),
        (R(r"how did you hear|referr"), p.get("heard_from", "")),
        (R(r"cover letter|additional info|anything else|why (do you|are you)|tell us|motivation|message|introduc"), cover),
        # last: a field whose label is just "Name" / "Full name" / "Your name" (not "Company name")
        (R(r"^\s*(your |full[\s_-]*|legal )?name\s*[*✱]?\s*(\||$)"), p["full_name"]),
    ]


DESCRIBE_JS = """el => {
  // Human-readable question text first (used in NEEDS YOU notes), then technical names.
  const t = [];
  if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t.push(l.innerText); }
  for (const sel of ['.application-question', 'li', '.field', '.form-group', '[class*=field]', '[class*=Field]', 'label']) {
    const wrap = el.closest(sel);
    if (!wrap) continue;
    const l = wrap.querySelector('.application-label, legend, label, .label, [class*=label], [class*=Label]');
    const txt = (l ? l.innerText : (wrap.tagName === 'LABEL' ? wrap.innerText : '')).trim();
    if (txt) { t.push(txt); break; }
  }
  t.push(el.getAttribute('aria-label'), el.placeholder, el.name, el.id, el.getAttribute('autocomplete'));
  return t.filter(Boolean).map(s => String(s).replace(/\\s+/g, ' ').trim()).join(' | ').slice(0, 300);
}"""


def _describe(el) -> str:
    try:
        return el.evaluate(DESCRIBE_JS)
    except Exception:  # noqa: BLE001
        return ""


def pick_option(page, el, value: str) -> bool:
    """Pick-from-list box (Country, City): click the list option that starts like our answer.
    If no option matches, the typed text is cleared so the field shows up as NEEDS YOU, not half-filled."""
    page.wait_for_timeout(1200)
    want = value.lower().split(",")[0].strip()
    for opt in page.query_selector_all("[role=option]"):
        try:
            if opt.is_visible() and (opt.inner_text() or "").strip().lower().startswith(want[:20]):
                opt.click()
                page.wait_for_timeout(400)
                return True
        except Exception:  # noqa: BLE001
            pass
    el.fill("")
    return False


def fill_form(page, p: dict, cover: str) -> dict:
    rules = field_rules(p, cover)
    filled, missing = [], []
    # 1) CV first: some forms (Lever, Ashby) read the CV and overwrite fields, so let that finish.
    #    File inputs often hide behind a styled button, so visibility isn't required.
    files = page.query_selector_all("input[type=file]")
    if files and p.get("resume") and Path(p["resume"]).exists():
        target = next((f for f in files if re.search(r"resume|cv|curriculum", _describe(f), re.I)), files[0])
        try:
            target.set_input_files(p["resume"])
            filled.append("CV")
            page.wait_for_timeout(6000)
        except Exception as ex:  # noqa: BLE001
            missing.append(f"CV upload ({str(ex)[:50]})")
    elif files:
        missing.append("CV upload (set resume: in profile.yaml)")
    # 1b) Cover letter hidden behind a button (Greenhouse: "Enter manually") → open the text box first.
    if cover:
        for btn in page.query_selector_all("button:has-text('Enter manually'), a:has-text('Enter manually')"):
            try:
                if btn.is_visible() and re.search(r"cover", _describe(btn) + (btn.evaluate(
                        "b => (b.closest('fieldset, .field, [class*=field], [class*=upload]') || b.parentElement).innerText") or ""), re.I):
                    btn.click()
                    page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass
    # 2) Text boxes. The FIRST rule that matches decides; if that answer is blank we leave the
    #    box empty rather than guessing (e.g. no GitHub link → don't put the website there).
    for el in page.query_selector_all("input, textarea"):
        try:
            kind = (el.get_attribute("type") or "text").lower()
            if kind in ("hidden", "submit", "button", "checkbox", "radio", "search", "file") or not el.is_visible():
                continue
            desc = _describe(el)
            required = (el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true"
                        or "*" in desc or "✱" in desc)
            value = next((v for rx, v in rules if rx.search(desc)), "")
            if callable(value):
                value = value(desc)
            if value and kind == "number":  # number-only boxes take digits only
                value = re.sub(r"[^\d.]", "", str(value).split(" ")[0])
            current = (el.input_value() or "").strip()
            if current:
                # The form (or its CV reader) filled this already. For who-you-are boxes the owner's
                # profile wins over what was read from the CV; everything else is left as it is.
                if not (value and PROFILE_WINS.search(desc) and current != str(value)):
                    filled.append(desc.split(" | ")[0][:40] + " (prefilled)")
                    continue
                el.fill("")
            if value:
                el.fill(str(value))
                listbox = el.get_attribute("role") == "combobox" or el.get_attribute("aria-autocomplete")
                if listbox and not pick_option(page, el, str(value)):  # never press Enter: it can submit the form
                    if required:
                        missing.append(desc or kind)
                    continue
                filled.append(desc.split(" | ")[0][:40] or kind)
            elif required:
                missing.append(desc or kind)
        except Exception as ex:  # noqa: BLE001 — one odd field must not stop the rest
            missing.append(f"(field error: {str(ex)[:60]})")
    # 3) Required tick-box / multiple-choice questions: never guessed. Tick-one (radio) questions the
    #    owner pre-answered in profile.yaml `answers:` are ticked; everything else goes to the owner.
    groups = page.evaluate("""() => {
      const out = [];
      const seen = new Set();
      for (const el of document.querySelectorAll('input[type=checkbox], input[type=radio]')) {
        if (!el.name || seen.has(el.name) || !el.offsetParent) continue;
        seen.add(el.name);
        const box = el.closest('.application-question, fieldset, [class*=question], [class*=Question]')
                 || el.closest('.field, .form-group, li');
        const all = [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)];
        const label = box ? (box.querySelector('.application-label, legend, label, [class*=label]') || box).innerText.split('\\n')[0] : el.name;
        const required = all.some(i => i.required || i.getAttribute('aria-required') === 'true') || /[*✱]/.test(label);
        const options = all.map(i => ((i.closest('label') && i.closest('label').innerText) || i.value || '').trim());
        if (required && !all.some(i => i.checked))
          out.push({name: el.name, radio: el.type === 'radio', label: label.trim().slice(0, 120), options});
      }
      return out;
    }""")
    for g in groups:
        pick = answer_choice(p, g["label"], g["options"]) if g["radio"] else -1
        if pick >= 0:
            try:
                page.query_selector_all(f'input[name="{g["name"]}"]')[pick].check()
                filled.append(f'{g["label"][:40]} → {g["options"][pick]}')
                continue
            except Exception:  # noqa: BLE001
                pass
        missing.append(g["label"])
    for sel in page.query_selector_all("select"):  # required dropdowns we can't answer safely
        try:
            if sel.is_visible() and (sel.get_attribute("required") is not None or "*" in _describe(sel)):
                if not (sel.input_value() or "").strip():
                    desc = _describe(sel)
                    labels = [o.inner_text() for o in sel.query_selector_all("option")]
                    pick = answer_choice(p, desc, labels)
                    if pick >= 0 and labels[pick].strip():
                        sel.select_option(index=pick)
                        filled.append(f"{desc.split(' | ')[0][:40]} → {labels[pick].strip()}")
                    else:
                        missing.append(desc or "dropdown")
        except Exception:  # noqa: BLE001
            pass
    return {"filled": filled, "missing": missing}


def has_captcha(page) -> bool:
    for sel in CAPTCHA:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:  # noqa: BLE001
            pass
    return False


def host_of(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().split(":")[0]


def ats_of(url: str) -> str:
    h = host_of(url)
    return next((name for rx, name in ATS.items() if re.search(rx, h)), "")


def go_to_form(ctx, page, url: str):
    """Open the job, follow its Apply button (up to 3 hops), land on the application form.
    Returns (page, where) where 'where' is the ATS name, 'form', 'email', 'blocked' or 'no-form'."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    for _ in range(4):
        page.wait_for_timeout(2500)
        if NEVER.search(host_of(page.url)):
            return page, "blocked"
        ats = ats_of(page.url)
        base = page.url.split("?")[0].split("#")[0].rstrip("/")
        if ats == "lever" and not base.endswith("/apply"):
            page.goto(base + "/apply", wait_until="domcontentloaded")
            continue
        if ats == "ashby" and not base.endswith("/application"):
            page.goto(base + "/application", wait_until="domcontentloaded")
            continue
        frame = page.query_selector("iframe[src*='greenhouse.io'], iframe[src*='lever.co'], iframe[src*='ashbyhq.com'], "
                                    "iframe[src*='workable.com'], iframe[src*='smartrecruiters.com']")
        if not ats and frame and frame.get_attribute("src"):  # form embedded from a job system
            page.goto(urllib.parse.urljoin(page.url, frame.get_attribute("src")), wait_until="domcontentloaded")
            continue
        # A real application form = on a known job system with an email box, or any form asking for a CV.
        # (Plain email boxes elsewhere are usually newsletter sign-ups — never treat those as applications.)
        if (ats and page.query_selector("input[type=email], input[name*=email i]")) or page.query_selector("input[type=file]"):
            return page, ats or "form"
        if re.search(r"just a moment|attention required|verify you are human", page.title(), re.I):
            return page, "robot-check"
        link = next((a for a in page.query_selector_all("a:has-text('Apply'), button:has-text('Apply')")
                     if a.is_visible()), None)
        if not link:
            return page, "no-form"
        href = link.get_attribute("href") or ""
        if href.startswith("mailto:"):
            return page, "email"
        if href and NEVER.search(host_of(urllib.parse.urljoin(page.url, href))):
            return page, "blocked"
        # Click like a person (job boards often open the employer's page in a new tab via a redirect).
        before, old_url = len(ctx.pages), page.url
        link.click()
        page.wait_for_timeout(4000)
        if len(ctx.pages) > before:
            page = ctx.pages[-1]
            try:
                page.wait_for_load_state("domcontentloaded", timeout=20000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(2000)
        if re.search(r"/(sign-?up|sign-?in|log-?in|register)\b", page.url, re.I):
            return page, "login-wall"
        if page.url == old_url and len(ctx.pages) == before:
            continue  # Apply may have opened a form on the same page (modal); look again
    return page, "no-form"


def submit(page) -> bool:
    btn = page.query_selector("button:has-text('Submit application'), button:has-text('Send application'), "
                              "button[type=submit], input[type=submit], button:has-text('Submit')")
    if not btn:
        return False
    before_url, before_text = page.url, page.inner_text("body")[:20000]
    btn.click()
    page.wait_for_timeout(6000)
    after_text = page.inner_text("body")[:20000]
    # Count it only if a NEW confirmation appeared and the form went away (or the page moved on);
    # a "thank you for your interest" that was already on the page proves nothing.
    new_success = bool(SUCCESS.search(after_text)) and len(SUCCESS.findall(after_text)) > len(SUCCESS.findall(before_text))
    form_gone = page.url != before_url or not page.query_selector("input[type=email], input[type=file]")
    return new_success and form_gone


def apply_one(ctx, lead: dict, p: dict, live: bool) -> tuple[str, str]:
    """Returns (status, note); status is applied | needs_you | dry."""
    if NEVER.search(host_of(lead["url"])):
        return "needs_you", "Upwork/LinkedIn/MQL5 — apply by hand (Hunt never automates these)"
    if lead.get("kind") == "funding":
        return "needs_you", "Cold outreach (funding news) — use ✉️ Email draft or DM by hand"
    page = ctx.new_page()
    shot = RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^A-Za-z0-9]', '', lead['id'])[:12]}.png"
    try:
        page, where = go_to_form(ctx, page, lead.get("ats_url") or lead["url"])  # company's own form first
        if where == "login-wall":
            return "needs_you", (f"{host_of(page.url)} wants you signed in first — run the helper once with "
                                 "--setup and sign in, it remembers you")
        if where == "robot-check":
            return "needs_you", "The job site shows an 'are you human' check to robots — apply by hand"
        if where == "blocked":
            return "needs_you", "Leads to Upwork/LinkedIn/MQL5 — apply by hand"
        if where == "email" or (where == "no-form" and lead.get("emails")):
            return "needs_you", "Applies by email — tap ✉️ Email draft, check it, press Send"
        if where == "no-form":
            return "needs_you", "Couldn't find an application form — open the link"
        cover = (lead.get("cover") or lead.get("email_body") or lead.get("dm") or "").strip()  # cover = locked note
        result = fill_form(page, p, cover)
        page.screenshot(path=str(shot), full_page=True)
        if has_captcha(page):
            return "needs_you", f"Filled, but it has an 'I'm not a robot' check — finish by hand ({where})"
        if result["missing"]:
            return "needs_you", f"Filled {len(result['filled'])} fields; needs your answer: " + "; ".join(
                m.split(" | ")[0].strip()[:60] for m in result["missing"][:4])
        if len(result["filled"]) < 3 or "CV" not in result["filled"]:
            return "needs_you", f"Form has no CV upload or looks unusual ({where}) — please check it by hand"
        if not live:
            return "dry", f"DRY: filled {len(result['filled'])} fields on {where}, not submitted ({shot.name})"
        if submit(page):
            page.screenshot(path=str(shot), full_page=True)
            return "applied", f"Submitted via {where}"
        if has_captcha(page):
            return "needs_you", "A robot check appeared after Submit — finish it by hand"
        return "needs_you", f"Pressed Submit but saw no confirmation — please check ({where})"
    except Exception as ex:  # noqa: BLE001
        return "needs_you", f"Helper error: {str(ex)[:150]}"
    finally:
        for pg in ctx.pages[1:]:
            pg.close()


def run(live: bool, only_url: str = "") -> None:
    from playwright.sync_api import sync_playwright

    p = load_profile()
    RUNS.mkdir(exist_ok=True)
    if only_url:
        leads = [{"id": "test", "kind": "job", "url": only_url, "emails": [], "email_body": "Test cover note."}]
        live = False
    else:
        leads = json.loads(hunt("apply-queue", *([] if live else ["--peek"])) or "[]")
    if not leads:
        print("nothing approved")
        return
    print(f"{len(leads)} approved lead(s); mode = {'LIVE' if live else 'DRY'}")
    CHROME_DIR.mkdir(parents=True, exist_ok=True)
    counts = {"applied": 0, "needs_you": 0, "dry": 0}
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(CHROME_DIR), channel="chrome", headless=False, ignore_default_args=["--no-sandbox"],
                                                    viewport={"width": 1280, "height": 900})
        if not ctx.pages:
            ctx.new_page()  # keep one tab so the window stays open
        done_log = RUNS / "submitted.txt"  # leads this Mac already submitted (guards against re-offers)
        done = set(done_log.read_text().split()) if done_log.exists() else set()
        for lead in leads:
            if lead["id"] in done:
                status, note = "applied", "Already submitted earlier (re-confirmed)"
            else:
                status, note = apply_one(ctx, lead, p, live)
                if live and status == "applied" and not only_url:
                    with done_log.open("a") as f:
                        f.write(lead["id"] + "\n")
            counts[status] = counts.get(status, 0) + 1
            print(f"- {status:9} {lead.get('company') or ''} | {note}")
            if live and not only_url:
                hunt("apply-result", lead["id"], "applied" if status == "applied" else "needs_you", note)
            time.sleep(5)  # unhurried, human pace
        ctx.close()
    if live and not only_url:
        hunt("apply-say", f"🚀 Auto-apply done: {counts['applied']} applied, {counts['needs_you']} need you "
                          f"(their cards show 👋 NEEDS YOU).")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="really submit (default: DRY, fill only)")
    ap.add_argument("--loop", action="store_true", help="check every 10 minutes")
    ap.add_argument("--url", default="", help="DRY test on one job URL")
    ap.add_argument("--setup", action="store_true", help="open the helper's Chrome so you can sign in to job boards once")
    a = ap.parse_args()
    if a.setup:
        # Plain Chrome on the helper's profile, no automation attached:
        # Google refuses "Sign in with Google" in robot-controlled Chrome.
        CHROME_DIR.mkdir(parents=True, exist_ok=True)
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        proc = subprocess.Popen([chrome, f"--user-data-dir={CHROME_DIR}", "--no-first-run",
                                 "https://remoteok.com/login"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        input("Sign in to the job boards you use in this window, then press Enter here to save and close… ")
        proc.terminate()
        proc.wait(timeout=30)
        return
    while True:
        try:
            run(a.live, a.url)
        except Exception as ex:  # noqa: BLE001
            print("error:", ex)
        if not a.loop or a.url:
            break
        time.sleep(600)


if __name__ == "__main__":
    main()
