"""Use the owner's ChatGPT plan as Hunt's AI, via OpenAI's official "Sign in with ChatGPT".

Follows https://developers.openai.com/siwc (open-source / self-hosted VM flow):
  1. `python -m hunt login` on your own computer opens the browser, you approve, credentials
     are saved to data/chatgpt.json (owner-only file, never committed).
  2. deploy/push-login.sh moves that file to the server, which then refreshes tokens itself.
  3. Every AI call streams one Responses API request with store=false, stream=true.
No API key and no client secret are involved; usage counts against the ChatGPT plan.
"""
import base64
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import config
from .config import DATA

AUTH = "https://auth.openai.com"
AUTHORIZE_URL = f"{AUTH}/api/accounts/authorize"
TOKEN_URL = f"{AUTH}/api/accounts/oauth/token"
API = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
APP_NAME = "Hunt"
PORT = 1455
REDIRECT = f"http://127.0.0.1:{PORT}/auth/callback"

CREDS = Path(os.environ.get("HUNT_CHATGPT_CREDS", str(DATA / "chatgpt.json")))
HOST_ID_FILE = Path(os.environ.get("HUNT_HOST_ID_FILE", str(DATA / "host_id")))
LOCK = CREDS.with_suffix(".lock")
DEAD_REFRESH = {"invalid_grant", "invalid_refresh_token", "token_expired", "refresh_token_expired",
                "refresh_token_invalidated", "refresh_token_reused"}


class LoginNeeded(RuntimeError):
    """The saved sign-in is missing or no longer valid: run `python -m hunt login` again."""


class UsageLimit(RuntimeError):
    """The ChatGPT plan (or Hunt's app limit) is used up for now. Pause, don't retry in a loop."""


class TryLater(RuntimeError):
    """Temporary or policy problem on OpenAI's side. Pause the cycle; keep leads queued and credentials."""


class AIOff(TryLater):
    """The owner's master switch (HUNT_AI) does not allow this AI. Nothing is sent; leads stay queued."""


class ChatGPTError(RuntimeError):
    pass


# ---------- small helpers ----------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_private(path: Path, data: dict) -> None:
    """Atomic write with owner-only permissions (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _load() -> dict:
    if not CREDS.exists():
        raise LoginNeeded("No ChatGPT sign-in saved yet. Run: python -m hunt login")
    return json.loads(CREDS.read_text())


def host_id() -> str:
    """Stable, opaque id for this machine (laptop or server). Not a credential."""
    if HOST_ID_FILE.exists():
        return HOST_ID_FILE.read_text().strip()
    value = f"urn:uuid:{uuid.uuid4()}"
    HOST_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOST_ID_FILE.write_text(value + "\n")
    return value


def _post_form(url: str, fields: dict) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except ValueError:
            return e.code, {"detail": body.decode(errors="replace")[:300]}


def _validate_id_token(id_token: str, client_id: str, nonce: str | None) -> dict:
    """Check signature (OpenAI's published keys), issuer, audience, expiry and nonce."""
    import jwt  # PyJWT, only needed at sign-in

    with urllib.request.urlopen(f"{AUTH}/.well-known/openid-configuration", timeout=30) as r:
        jwks_uri = json.loads(r.read())["jwks_uri"]
    key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(id_token, key, algorithms=["RS256"], audience=client_id, issuer=AUTH,
                        options={"require": ["exp", "iat", "sub"]})
    if nonce is not None and claims.get("nonce") != nonce:
        raise ChatGPTError("ID token nonce mismatch; sign-in rejected.")
    return claims


# ---------- sign-in (run on your own computer) ----------

def login() -> None:
    _require_on()
    saved = json.loads(CREDS.read_text()) if CREDS.exists() else {}
    client_id = saved.get("client_id")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    params = {
        "client_id": client_id or "dynamic_agent_client",
        "ext_agent_host_id": host_id(),
        "response_type": "code",
        "redirect_uri": REDIRECT,
        "scope": SCOPES,
        "resource": API,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
    }
    if client_id:  # returning sign-in: skip the account picker
        if saved.get("id_token"):
            params["id_token_hint"] = saved["id_token"]
        if saved.get("email"):
            params["login_hint"] = saved["email"]
    else:  # first registration: name the app
        params["agent_name_hint"] = APP_NAME

    result: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            if url.path != "/auth/callback":
                self.send_response(404)
                self.end_headers()
                return
            got = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            if got.get("state") != state:  # stray or hostile request: ignore, keep waiting
                self.send_response(400)
                self.end_headers()
                return
            result.update(got)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>Hunt: sign-in received. You can close this tab.</h2>".encode())

        def log_message(self, *a):  # keep codes out of the terminal
            pass

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)
    print("Opening your browser to sign in with ChatGPT…")
    webbrowser.open(url)
    deadline = time.time() + 600
    server.timeout = 5
    while not result and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if not result:
        raise ChatGPTError("No answer from the browser within 10 minutes.")
    if result.get("state") != state:
        raise ChatGPTError("Sign-in state mismatch; ignored for safety. Try again.")
    if result.get("error"):
        raise ChatGPTError(f"Sign-in stopped: {result['error']}. "
                           "Hunt needs the 'use your ChatGPT plan' permission to work.")
    issued = result.get("client_id") or client_id
    if client_id and result.get("client_id") and result["client_id"] != client_id:
        raise ChatGPTError("Browser returned a different app registration; rejected.")
    if not issued or issued == "dynamic_agent_client":
        raise ChatGPTError("Registration incomplete (no app id returned). Try again.")

    status, tok = _post_form(TOKEN_URL, {
        "grant_type": "authorization_code", "client_id": issued, "code": result["code"],
        "code_verifier": verifier, "redirect_uri": REDIRECT, "resource": API})
    if status != 200 or "access_token" not in tok:
        raise ChatGPTError(f"Token exchange failed ({status}): {tok.get('error') or tok.get('detail')}")

    claims = _validate_id_token(tok["id_token"], issued, nonce)
    if saved.get("subject") and saved["subject"] != claims["sub"]:
        raise ChatGPTError("Signed in as a different ChatGPT account than before; not replacing it.")
    scopes = sorted(str(tok.get("scope", "")).split())  # trust only the token response
    if PLAN_SCOPE not in scopes:
        raise ChatGPTError("Signed in, but the 'use your ChatGPT plan' permission was not granted. "
                           "Run login again and allow it.")

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _save_login(claims, issued, tok, scopes)
    print(f"Signed in as {claims.get('email', 'your ChatGPT account')}. Saved to {CREDS} (owner-only).")


def _save_login(claims: dict, issued: str, tok: dict, scopes: list) -> None:
    _write_private(CREDS, {
        "email": claims.get("email", ""),
        "issuer": AUTH,
        "subject": claims["sub"],
        "client_id": issued,
        "ext_agent_host_id": host_id(),
        "id_token": tok["id_token"],
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "token_type": tok.get("token_type", "Bearer"),
        "expires_in": int(tok.get("expires_in", 3600)),
        "earliest_refresh_at": tok.get("earliest_refresh_at"),
        "scopes": scopes,
        "saved_at": _now_iso(),
        "saved_epoch": time.time(),
    })


# ---------- tokens (server keeps them fresh) ----------

def _require_on() -> None:
    if config.AI != "chatgpt":
        raise AIOff("AI is switched off (HUNT_AI)")


def access_token() -> str:
    """Return a valid access token, refreshing it near expiry. Serialised across threads/processes."""
    _require_on()  # models() and respond() both need a token, so nothing is sent while AI is off
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        c = _load()
        if not c.get("access_token") or not c.get("refresh_token"):
            raise LoginNeeded("ChatGPT sign-in has no tokens here. Run login (and push-login for the server).")
        expires = float(c.get("saved_epoch", 0)) + float(c.get("expires_in", 3600))
        earliest = _epoch(c.get("earliest_refresh_at"))
        now = time.time()
        if now < expires - 300 or (now < earliest and now < expires - 30):
            return c["access_token"]  # never hand out a token that is about to expire
        status, tok = _post_form(TOKEN_URL, {
            "grant_type": "refresh_token", "client_id": c["client_id"],
            "refresh_token": c["refresh_token"], "resource": API})
        if status != 200 or "access_token" not in tok:
            code = tok.get("error") or tok.get("code") or ""
            if code in DEAD_REFRESH:
                c.update(access_token="", refresh_token="")
                _write_private(CREDS, c)
                raise LoginNeeded(f"ChatGPT sign-in expired ({code}). Run login again.")
            if code == "invalid_client":
                raise LoginNeeded("ChatGPT app registration is invalid. Run login again.")
            raise ChatGPTError(f"Token refresh failed ({status}): {code or tok.get('detail')}")
        c.update(
            access_token=tok["access_token"],
            refresh_token=tok.get("refresh_token", c["refresh_token"]),
            expires_in=int(tok.get("expires_in", 3600)),
            earliest_refresh_at=tok.get("earliest_refresh_at"),
            saved_at=_now_iso(), saved_epoch=time.time(),
        )
        if tok.get("scope"):
            c["scopes"] = sorted(tok["scope"].split())
        _write_private(CREDS, c)
        return c["access_token"]


def _epoch(value) -> float:
    """earliest_refresh_at may be seconds, milliseconds or an ISO date; unknown -> 0 (no wait)."""
    if value in (None, ""):
        return 0.0
    try:
        v = float(value)
        return v / 1000 if v > 1e12 else v
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0


# ---------- inference ----------

def models() -> list[dict]:
    req = urllib.request.Request(f"{API}/models", headers={"Authorization": f"Bearer {access_token()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    return [{"slug": m.get("slug") or m.get("id"), "name": m.get("display_name", "")}
            for m in data.get("models", data.get("data", [])) if m.get("visibility", "list") == "list"]


PAUSE_CODES = {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable",
               "subscription_sharing_user_not_eligible", "chatpass_v2_scope_not_authorized",
               "chatpass_v2_invalid_authorization_context", "subscription_sharing_route_not_supported"}


def _raise_for(status: int, body: dict, request_id: str = "") -> None:
    raw = body.get("error")
    err = raw if isinstance(raw, dict) else {"message": str(raw or "")}
    code = str(err.get("code") or "")
    msg = str(err.get("message") or body.get("detail") or "")[:300]
    rid = f" [request {request_id}]" if request_id else ""
    if code == "subscription_sharing_usage_limit_exceeded" or status == 429:
        raise UsageLimit("ChatGPT plan limit reached. Check chatgpt.com/settings/usage." + rid)
    if code == "subscription_sharing_invalid_user" or status == 401:
        raise LoginNeeded(f"ChatGPT rejected the sign-in ({code or status}). Run login again.{rid}")
    if code in PAUSE_CODES or status in (403, 500, 502, 503, 504):
        raise TryLater(f"ChatGPT unavailable for now ({status} {code}): {msg}{rid}")
    raise ChatGPTError(f"ChatGPT request failed ({status} {code}): {msg}{rid}")


def respond(instructions: str, user_text: str, schema: dict, model: str, effort: str | None = None) -> dict:
    """One streamed Responses API call that must return JSON matching `schema`."""
    body = {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": user_text}],
        "text": {"format": {"type": "json_schema", "name": "hunt_output", "schema": schema, "strict": True}},
        "store": False,
        "stream": True,
    }
    if effort:
        body["reasoning"] = {"effort": effort}
    req = urllib.request.Request(f"{API}/responses", data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {access_token()}", "Content-Type": "application/json",
        "Accept": "text/event-stream"})
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        raw, rid = e.read(), e.headers.get("x-request-id", "")
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            parsed = {"detail": raw.decode(errors="replace")}
        _raise_for(e.code, parsed if isinstance(parsed, dict) else {"detail": str(parsed)}, rid)
    text, done = [], False
    with resp:
        for line in resp:
            line = line.decode().strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            ev = json.loads(payload)
            kind = ev.get("type")
            if kind == "response.output_text.delta":
                text.append(ev.get("delta", ""))
            elif kind == "response.completed":
                done = True
            elif kind in ("response.failed", "error"):
                err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
                code = str(err.get("code", "")) if isinstance(err, dict) else str(err)
                _raise_for(429 if "usage_limit" in code else 400 if code.endswith("unsupported_capability") else 503,
                           {"error": err})
            elif kind == "response.incomplete":
                reason = ((ev.get("response") or {}).get("incomplete_details") or {}).get("reason", "unknown")
                raise ChatGPTError(f"ChatGPT answer was cut short ({reason}).")
    if not done:
        raise ChatGPTError("ChatGPT stream ended without completing.")
    return json.loads("".join(text))
