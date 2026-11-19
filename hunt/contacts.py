"""B4 contact finder (PRD §8): for each new YouTube prospect, find a way to reach them.

Order of search, cheapest first:
  1. Email written in the channel description (including "name [at] site [dot] com" spellings).
  2. The channel's own website / link page (Linktree etc.) from the description: home page, then up
     to two contact/about pages on the same site. Plain web pages only — no browser, no logins.
  3. Social profiles from the description or the website: X, Instagram, Telegram, LinkedIn, WhatsApp.
     These are manual-tier: Hunt drafts, the owner sends by hand.
  4. Nothing found → status `needs_manual` with a note on exactly where to click
     (YouTube hides the business email behind a sign-in + "I'm not a robot" check on the About page).

Status after this step: `prospect_ready` (a contact route is known; waits for research, B5)
or `needs_manual`. Results live in the lead's data["contacts"] and in the prospect's handles.
"""
import html
import http.client
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from .sources import UA

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s)\]}\"'<>,]+", re.IGNORECASE)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.IGNORECASE)
CFEMAIL_RE = re.compile(r"""data-cfemail\s*=\s*["']([0-9a-fA-F]+)["']""")
# "name [at] site [dot] com", "name (at) site (dot) com"
AT_DOT_RE = re.compile(r"([A-Za-z0-9._%+\-]+)\s*[\[(\{]\s*(?:@|at)\s*[\])\}]\s*([A-Za-z0-9\-]+)"
                       r"\s*[\[(\{]\s*(?:\.|dot)\s*[\])\}]\s*([A-Za-z]{2,})", re.IGNORECASE)

SOCIAL = {  # handle name → (hosts, required path start)
    "x": (("twitter.com", "x.com"), ""),
    "instagram": (("instagram.com",), ""),
    "telegram": (("t.me", "telegram.me", "telegram.dog"), ""),
    "linkedin": (("linkedin.com",), ("in/", "company/")),
    "whatsapp": (("wa.me", "api.whatsapp.com", "chat.whatsapp.com"), ""),
}
LINK_PAGES = ("linktr.ee", "beacons.ai", "bio.link", "stan.store", "campsite.bio", "linkin.bio",
              "taplink.cc", "solo.to", "lnk.bio", "carrd.co", "hoo.be", "msha.ke")
# Never the prospect's own site: video sites, shops, brokers, exchanges, link shorteners, app stores.
NOT_A_SITE = ("youtube.com", "youtu.be", "google.", "facebook.com", "fb.com", "tiktok.com", "discord.",
              "patreon.com", "amazon.", "amzn.", "bit.ly", "tinyurl.", "goo.gl", "ow.ly", "rebrand.ly",
              "tradingview.com", "binance.", "okx.", "bitget.", "mexc.", "coinbase.",
              "kraken.", "etoro.", "exness.", "icmarkets.", "xm.com", "pepperstone.", "zerodha.", "upstox.",
              "angelone.", "groww.", "dhan.co", "apple.com", "play.google", "spotify.com", "paypal.",
              "buymeacoffee.", "ko-fi.com", "gumroad.com", "reddit.com", "threads.net", "medium.com",
              "substack.com", "github.com", "wikipedia.org", "ledger.com", "trezor.io", "nordvpn.",
              "skillshare.", "udemy.com", "coursera.org", "shopify.", "etsy.com", "skool.com", "whop.com",
              "deriv.", "justmarkets.", "hfm.com", "fxpro.", "exnesstrack.", "exnessonelink.", "myfxbook.",
              "myfundedfutures.", "onefunded.", "wfunded.", "ftmo.", "fundingpips.", "the5ers.", "delta.exchange",
              "coindcx.", "wazirx.", "affs.click", "taap.it", "myfxlink.", "linktw.in", "t.ly", "shorturl.",
              "cutt.ly", "is.gd", "tradelocker.", "metatrader", "mql5.com", "forexfactory.", "investing.com",
              "whatsapp.com", "googleapis.com", "gstatic.com", "forms.gle", "opensea.io", "spotware.com",
              "datagrail.io", "app.link", "cloudflare.com", "jsdelivr.net", "w3.org", "schema.org")
_extra: tuple | None = None


def not_a_site() -> tuple:
    """NOT_A_SITE plus `extra_not_a_site:` from data/sources.yaml (names kept out of the public code)."""
    global _extra
    if _extra is None:
        try:
            from .sources import config
            _extra = tuple(str(x).strip().lower() for x in (config().get("extra_not_a_site") or []) if x)
        except Exception:  # noqa: BLE001 — unreadable config: built-in list only
            _extra = ()
    return NOT_A_SITE + _extra


FREE_MAIL = ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "yahoo.in",
             "icloud.com", "proton.me", "protonmail.com", "pm.me", "aol.com", "zoho.com", "zohomail.in",
             "gmx.com", "gmx.de", "yandex.com", "rediffmail.com", "mail.ru")
# Links with these in them are referral/affiliate links, never the creator's own site.
AFFILIATE_WORDS = ("ref=", "refid", "ref_", "/ref/", "aff", "partner", "utm_medium=aff", "invite", "/a/", "/go/",
                   "promo", "bonus", "signup", "sign-up", "register")
JUNK_EMAIL = ("example.", "sentry", "wixpress", "domain.com", "email.com", "yourname", "noreply", "no-reply",
              "@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", "youtube.com", "godaddy",
              "wordpress", "cloudflare", "schema.org", "w3.org", ".js@", ".css@", ".mjs", "fourthwall",
              "@sentry", "@wix", "@mailchimp", "@sendgrid", "company.com", "yourdomain", "mysite.",
              "website.com", "test.com", "linktr.ee", "linklyhq")
CONTACT_WORDS = ("contact", "about", "business", "collab", "work-with", "workwithme", "partner",
                 "sponsor", "advertis", "media-kit", "press", "hire")
MAX_PAGES = 5  # web pages fetched per channel, at most
# Words near an email in a video description that say "this is my contact" vs "this is a sponsor".
CONTACT_CUES = ("business", "contact", "inquir", "enquir", "collab", "email me", "mail me", "reach me",
                "reach out", "partnership", "for work", "work with me", "email:", "mail:")
SPONSOR_CUES = ("sponsor", "promo code", "use code", "affiliate", "discount", "brought to you", "powered by")
TRAILING = ".,;:!?)'\"]}>"


def _public(url: str) -> bool:
    """Only plain web addresses that resolve to public IPs. Links come from strangers' channel pages, so
    never let them make the server fetch localhost, the private network or cloud metadata addresses."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except (OSError, ValueError):
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _public(newurl):
            raise urllib.error.URLError("redirect to a non-public address blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _connect_public(host: str, port: int, timeout) -> socket.socket:
    """Resolve and connect in one step, refusing any non-public address. Checking and connecting with the
    same lookup closes the DNS-rebinding gap (a name that is public when checked, private when used)."""
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    if not infos or not all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos):
        raise urllib.error.URLError(f"non-public address blocked: {host}")
    fam, typ, proto, _, addr = infos[0]
    sock = socket.socket(fam, typ, proto)
    if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
        sock.settimeout(timeout)
    sock.connect(addr)
    return sock


class _PublicHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = _connect_public(self.host, self.port, self.timeout)


class _PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):
        self.sock = self._context.wrap_socket(_connect_public(self.host, self.port, self.timeout),
                                              server_hostname=self.host)


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTP, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPS, req, context=self._context)


# No proxies (a proxy would connect for us, unchecked); every connection goes through _connect_public.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _PublicHTTPHandler, _PublicHTTPSHandler,
                                      _SafeRedirect)


def _http_get(url: str) -> str:
    if not _public(url):
        raise urllib.error.URLError("non-public address blocked")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with _OPENER.open(req, timeout=8) as r:
        if "html" not in (r.headers.get("Content-Type") or "html"):
            return ""
        return r.read(400_000).decode(r.headers.get_content_charset() or "utf-8", "replace")


def _cf_decode(hexstr: str) -> str:
    """Cloudflare hides emails as data-cfemail="…" (XOR with the first byte)."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def emails_in(text: str) -> list[str]:
    text = urllib.parse.unquote(html.unescape(text or ""))
    found = [m.group(0) for m in EMAIL_RE.finditer(text)]
    found += [f"{a}@{b}.{c}" for a, b, c in AT_DOT_RE.findall(text)]
    found += [_cf_decode(h) for h in CFEMAIL_RE.findall(text)]
    out = []
    for e in found:
        e = e.strip(".").lower()
        if "@" in e and not any(j in e for j in JUNK_EMAIL) and len(e.split("@")[1].split(".")[-2]) >= 2 \
                and e not in out:
            out.append(e)
    return out


def urls_in(text: str) -> list[str]:
    """Links in free text, without sentence punctuation stuck to the end."""
    return [u.rstrip(TRAILING) for u in URL_RE.findall(text or "")]


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")


def _blocked(host: str) -> bool:
    """True if the host is (a subdomain of) a NOT_A_SITE entry. Matched on whole host parts,
    so 'xm.com' blocks xm.com and my.xm.com but not fxm.com."""
    h = "." + host
    return any(("." + w) in h for w in not_a_site())


def _is_link_page(url: str) -> bool:
    h = _host(url)
    return any(h == p or h.endswith("." + p) for p in LINK_PAGES)


def _social(url: str) -> str:
    """Which social network a link points at ('' if none)."""
    h = _host(url)
    for name, (hosts, starts) in SOCIAL.items():
        if any(h == s or h.endswith("." + s) for s in hosts):
            path = urllib.parse.urlparse(url).path.strip("/").lower()
            if starts and not path.startswith(starts):
                return "other"
            return name
    return ""


def socials_in(urls: list[str]) -> dict:
    out = {}
    for u in urls:
        name = _social(u)
        if name in ("", "other") or name in out:
            continue
        path = urllib.parse.urlparse(u).path.strip("/").lower()
        if path and not path.startswith(("intent", "share", "home", "p/", "reel", "explore")):
            out[name] = u if name == "whatsapp" else u.split("?")[0]  # wa links carry the number in ?phone=
    return out


GENERIC = {"with", "trading", "trader", "traders", "trade", "trades", "forex", "crypto", "official", "channel",
           "academy", "global", "tips", "daily", "signals", "live", "market", "markets", "money", "bitcoin",
           "stocks", "stock", "invest", "investing", "learn", "room", "zone", "show", "insights", "india", "bangla",
           "gold", "best", "tutorial", "strategy", "strategies", "course", "news", "hindi", "smart", "empire",
           "level", "challenge", "terminal", "arena", "network", "networks", "group", "team", "club", "world"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{4,}", (text or "").lower()) if w not in GENERIC}


def _owned(site: str, name: str) -> bool:
    """A link page (Linktree etc.) or a site whose name matches the channel name: clearly theirs."""
    return _is_link_page(site) or any(w in _host(site) for w in _words(name))


def _email_ok(email: str, site: str) -> bool:
    """An email found on a website counts only if it is on the site's own domain or a personal mailbox."""
    dom, host = email.split("@")[1], _host(site)
    return dom in FREE_MAIL or host == dom or host.endswith("." + dom) or dom.endswith("." + host)


def sites_in(urls: list[str], name: str = "") -> list[str]:
    """Candidate own-websites, best first: link pages (Linktree etc.), then sites whose name matches the
    channel name, then the most-linked. Brokers, exchanges, shorteners and referral links are skipped."""
    links, count = [], {}
    for u in urls:
        h = _host(u)
        if not h or not re.search(r"\.[a-z]{2,}$", h) or _blocked(h) or _social(u):
            continue
        if _is_link_page(u):
            links.append(u.split("?")[0])
        elif not any(w in u.lower() for w in AFFILIATE_WORDS):
            p = urllib.parse.urlparse(u)
            site = f"{p.scheme}://{p.netloc}/"
            count[site] = count.get(site, 0) + 1
    want = _words(name)
    def rank(site):
        host = _host(site)
        return (-any(w in host for w in want), -count[site])
    return list(dict.fromkeys(links + sorted(count, key=rank)))


def _crawl(site: str, fetch, budget: list[int], direct: list[str] = ()) -> tuple[list[str], list[str]]:
    """Home page, any contact page the channel linked directly, plus up to two contact/about pages found
    on the home page → (emails, links)."""
    emails, links = [], []
    queue, seen = [site] + list(direct)[:1], set()
    while queue and budget[0] > 0:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        budget[0] -= 1
        try:
            page = fetch(url)
        except Exception:  # noqa: BLE001 — a dead website is normal; move on
            continue
        emails += [e for e in emails_in(page) if e not in emails]
        hrefs = [urllib.parse.urljoin(url, h.strip()) for h in HREF_RE.findall(page)]
        links += hrefs
        if url == site:
            inner = [h for h in hrefs if _host(h) == _host(site) and h.rstrip("/") != site.rstrip("/")
                     and any(w in h.lower() for w in CONTACT_WORDS)]
            queue += list(dict.fromkeys(inner))[:2]
    return emails, links


def _video_email_ok(email: str, text: str, nm: str) -> bool:
    """An email seen only in a video description may be a sponsor's. Trust it only when it carries the
    channel's name, or the words just before it say it is the creator's contact (and not a sponsor)."""
    local, dom = email.split("@", 1)
    if _owned("https://" + dom, nm) or any(w in local for w in _words(nm)):
        return True
    low = html.unescape(text).lower()
    i = low.find(email)
    before = low[max(0, i - 60):i] if i >= 0 else ""
    return any(c in before for c in CONTACT_CUES) and not any(c in before for c in SPONSOR_CUES)


def _video_socials(videos: list[str], nm: str) -> dict:
    """Social links from video descriptions: trusted if the handle carries the channel's name or the same
    link appears in at least two videos (the creator's own footer); one-off links may be a sponsor's."""
    seen: dict[str, int] = {}
    for text in videos:
        for u in dict.fromkeys(urls_in(text)):
            if _social(u) not in ("", "other"):
                seen[u.split("?")[0]] = seen.get(u.split("?")[0], 0) + 1
    want = _words(nm)
    ok = [u for u, n in seen.items() if n >= 2 or any(w in urllib.parse.urlparse(u).path.lower() for w in want)]
    return socials_in(ok)


def find(extra: dict, channel_url: str, fetch=_http_get, name: str = "") -> dict:
    """Search one channel. Returns the contacts dict stored on the lead."""
    nm = name + " " + extra.get("handle", "")
    desc = extra.get("description", "") or ""
    video_texts = [v.get("description", "") or "" for v in extra.get("recent_videos") or []]
    videos = " \n".join(video_texts)
    urls = urls_in(desc) + urls_in(videos)
    socials = socials_in(urls_in(desc))
    for k, v in _video_socials(video_texts, nm).items():
        socials.setdefault(k, v)
    out = {"email": "", "email_from": "", "website": "", "socials": socials, "checked": []}
    found = emails_in(desc)
    if found:
        out["email"], out["email_from"] = found[0], "channel description"
    for e in emails_in(videos):
        if out["email"]:
            break
        if _video_email_ok(e, videos, nm):
            out["email"], out["email_from"] = e, "video description"
        else:
            out.setdefault("maybe_emails", []).append(f"{e} (in a video description)")
    # Crawl only sites that are clearly theirs, or linked from the channel description itself.
    # Sites linked only in videos are mostly sponsors; their emails would reach the wrong person.
    from_desc = set(sites_in(urls_in(desc)))
    budget = [MAX_PAGES]
    todo = [s for s in sites_in(urls, nm) if _owned(s, nm) or s in from_desc][:2]
    while todo and budget[0] > 0:
        site = todo.pop(0)
        if site in out["checked"]:
            continue
        out["checked"].append(site)
        owned = _owned(site, nm)
        direct = [u for u in urls if _host(u) == _host(site) and u.rstrip("/") != site.rstrip("/")
                  and any(w in u.lower() for w in CONTACT_WORDS)]
        emails, links = _crawl(site, fetch, budget, direct)
        if owned and not out["website"] and not _is_link_page(site):
            out["website"] = site
        if owned:
            for k, v in socials_in(links).items():
                out["socials"].setdefault(k, v)
        if owned and not out["website"]:  # a link page often points at the real website
            own = [s for s in sites_in(links, nm) if not _is_link_page(s) and _owned(s, nm)]
            if own:
                out["website"] = own[0]
                if not out["email"]:
                    todo.append(own[0])  # read the real website too, with the pages left in the budget
        good = [e for e in emails if owned and (_email_ok(e, site) or _owned("https://" + e.split("@")[1], nm))]
        if good and not out["email"]:
            out["email"], out["email_from"] = good[0], site
        maybe = [e for e in emails if e not in good and e != out["email"]]
        if maybe:  # shown to the owner, never used as the send address
            out.setdefault("maybe_emails", []).extend(f"{e} (on {site})" for e in maybe[:3])
    if out["email"]:
        out["route"] = "email"
    else:
        out["route"] = next((k for k in ("x", "instagram", "telegram", "linkedin", "whatsapp")
                             if k in out["socials"]), "")
    if not out["route"]:
        out["note"] = (f"No email or social link found. Open {channel_url}/about and tap 'View email address' "
                       "(needs a Google sign-in and the 'I'm not a robot' tick).")
    return out


def run(con, limit: int = 25, fetch=_http_get) -> dict:
    """Find contacts for up to `limit` new YouTube prospects, best first. Returns counts."""
    from . import store
    rows = con.execute("SELECT * FROM leads WHERE status='prospect_new' ORDER BY prescore DESC, created_at "
                       "LIMIT ?", (limit,)).fetchall()
    counts = {"email": 0, "social": 0, "manual": 0}
    for r in rows:
        data = json.loads(r["data"] or "{}")
        c = find(data.get("extra") or {}, r["url"], fetch, name=r["title"])
        data["contacts"] = c
        counts["email" if c["route"] == "email" else "social" if c["route"] else "manual"] += 1
        store.update(con, r["id"], status="prospect_ready" if c["route"] else "needs_manual", data=data)
        handles = {k: v for k, v in {"email": c["email"], "website": c["website"], **c["socials"]}.items() if v}
        if r["prospect_id"] and handles:
            store.prospect_add_handles(con, r["prospect_id"], handles)
    return counts
