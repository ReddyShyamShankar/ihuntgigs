"""Authenticator codes (TOTP, RFC 6238): the 6-digit codes Google Authenticator & co. show every 30 seconds.
Standard library only. Each user has their own secret; it is shown once as a QR code / setup link."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

STEP, DIGITS = 30, 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS
    return str(n).zfill(DIGITS)


def code(secret: str, at: float | None = None) -> str:
    return _code(secret, int((time.time() if at is None else at) // STEP))


def verify(secret: str, entered: str, last_counter: int = -1, at: float | None = None) -> int:
    """Returns the matching time step (to store, so a code can't be used twice), or -1.
    Accepts the previous, current and next 30-second window (phone clocks drift)."""
    entered = "".join(c for c in str(entered) if c.isdigit())
    if len(entered) != DIGITS:
        return -1
    now = int((time.time() if at is None else at) // STEP)
    for counter in (now - 1, now, now + 1):
        if counter > last_counter and hmac.compare_digest(_code(secret, counter), entered):
            return counter
    return -1


def uri(secret: str, name: str, issuer: str = "Hunt") -> str:
    """otpauth:// link — what the QR code holds; tapping it on a phone opens the authenticator app."""
    label = urllib.parse.quote(f"{issuer}:{name}")
    return f"otpauth://totp/{label}?" + urllib.parse.urlencode(
        {"secret": secret, "issuer": issuer, "digits": DIGITS, "period": STEP})
