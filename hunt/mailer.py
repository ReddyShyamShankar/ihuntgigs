"""Microsoft Graph mail client for the outreach inbox(es) (decision #22: Microsoft 365, OAuth, no passwords).

App-only sign-in (client credentials) from a Microsoft Entra app registration with the Graph application
permissions Mail.Send and Mail.Read, limited to the outreach mailboxes by an Exchange application access
policy. Secrets live only in the server's .env:
  MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET   (secret at the longest expiry Microsoft allows: 24 months)
  HUNT_INBOXES = a@example.com,b@example.com

Mail is sent as MIME so Hunt can set its own Message-ID (to match replies) and the List-Unsubscribe header
(Graph's JSON send only allows custom headers that start with X-). Reading never changes the mailbox:
nothing is marked read or moved; Hunt remembers which messages it has seen.
"""
import base64
import email.utils
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

GRAPH = "https://graph.microsoft.com/v1.0"
MAX_PAGES = 20  # 50 messages a page → up to 1,000 new messages per inbox per cycle


class NotSent(Exception):
    """Microsoft definitely did not accept the email (sign-in failed, or a 4xx refusal): safe to retry."""
_token = {"value": "", "exp": 0.0}


def inboxes() -> list[str]:
    return [i.strip().lower() for i in os.environ.get("HUNT_INBOXES", "").split(",") if i.strip()]


def enabled() -> bool:
    return bool(os.environ.get("MS_TENANT_ID") and os.environ.get("MS_CLIENT_ID")
                and os.environ.get("MS_CLIENT_SECRET") and inboxes())


def _http(req: urllib.request.Request, timeout: int = 30):
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def token() -> str:
    if _token["value"] and time.time() < _token["exp"] - 120:
        return _token["value"]
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": os.environ["MS_CLIENT_ID"],
        "client_secret": os.environ["MS_CLIENT_SECRET"], "scope": "https://graph.microsoft.com/.default"}).encode()
    url = f"https://login.microsoftonline.com/{urllib.parse.quote(os.environ['MS_TENANT_ID'])}/oauth2/v2.0/token"
    body = _http(urllib.request.Request(url, data=data))
    _token.update(value=body["access_token"], exp=time.time() + int(body.get("expires_in", 3600)))
    return _token["value"]


def graph(method: str, path: str, body: bytes | None = None, content_type: str = "application/json",
          headers: dict | None = None):
    url = path if path.startswith("https://") else GRAPH + path  # paging links are full URLs
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Authorization": f"Bearer {token()}", "Content-Type": content_type,
                                          **(headers or {})})
    return _http(req)


def build_mime(inbox: str, to: str, subject: str, text: str, unsubscribe: str = "",
               in_reply_to: str = "") -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = inbox, to, subject
    m["Date"] = email.utils.formatdate(localtime=False)
    m["Message-ID"] = email.utils.make_msgid(domain=inbox.split("@")[-1])
    if unsubscribe:
        m["List-Unsubscribe"] = unsubscribe
    if in_reply_to:
        m["In-Reply-To"] = m["References"] = in_reply_to
    m.set_content(text)
    return m


def send(inbox: str, msg: EmailMessage, post=None) -> str:
    """Send one MIME message from `inbox`. Returns its Message-ID."""
    raw = base64.b64encode(msg.as_bytes())
    if post is None:
        try:
            token()  # sign in first: a failure here means nothing was sent
        except Exception as ex:  # noqa: BLE001
            raise NotSent(f"sign-in failed: {ex}") from ex
    try:
        (post or graph)("POST", f"/users/{urllib.parse.quote(inbox)}/sendMail", raw, "text/plain")
    except urllib.error.HTTPError as ex:
        if ex.code < 500:  # 4xx (incl. 429 throttling) = Microsoft refused it; 5xx/timeouts = unknown
            raise NotSent(f"HTTP {ex.code}") from ex
        raise
    return msg["Message-ID"]


def recent(inbox: str, since_iso: str, get=None) -> list[dict]:
    """Inbox messages received since `since_iso` (oldest first), as plain text with their headers."""
    q = urllib.parse.urlencode({
        "$filter": f"receivedDateTime ge {since_iso}", "$orderby": "receivedDateTime asc", "$top": "50",
        "$select": "id,internetMessageId,from,subject,body,receivedDateTime,internetMessageHeaders"})
    path, raw = f"/users/{urllib.parse.quote(inbox)}/mailFolders/inbox/messages?{q}", []
    for _ in range(MAX_PAGES):  # follow Graph's next-page links so a backlog never hides newer mail
        res = (get or graph)("GET", path, None, "application/json", {"Prefer": 'outlook.body-content-type="text"'})
        raw += res.get("value") or []
        path = res.get("@odata.nextLink")
        if not path:
            break
    out = []
    for m in raw:
        heads = {h.get("name", "").lower(): h.get("value", "") for h in m.get("internetMessageHeaders") or []}
        out.append({"id": m.get("internetMessageId") or m.get("id"), "inbox": inbox,
                    "from": ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower(),
                    "subject": m.get("subject") or "", "body": (m.get("body") or {}).get("content") or "",
                    "at": m.get("receivedDateTime") or "", "headers": heads})
    return out
