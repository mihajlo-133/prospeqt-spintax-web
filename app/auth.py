"""Cookie-based authentication helpers.

What this does:
    Signs and verifies session cookies using HMAC-SHA256 (stdlib).
    No external dependencies. Admin password compared constant-time.
    All /api/* routes use require_auth dependency (app/dependencies.py).

What it depends on:
    Python stdlib: hashlib, hmac, json, base64, secrets, datetime.
    app.config for ADMIN_PASSWORD and SESSION_SECRET env vars.

What depends on it:
    app/routes/admin.py (set_session_cookie, verify_password)
    app/dependencies.py (is_authenticated)

Cookie format:
    session=<base64url(json_payload)>.<hex_hmac>

Where:
    json_payload = {"login_at": "<iso8601 utc>", "expires_at": "<iso8601 utc>"}
    hex_hmac = hmac_sha256(SESSION_SECRET, json_payload_bytes).hexdigest()

Verification:
    1. Split on '.' (must have exactly 2 parts)
    2. Decode base64url payload
    3. Parse JSON
    4. Compute expected HMAC of the raw payload bytes
    5. Compare via hmac.compare_digest (constant-time)
    6. Check expires_at > now
"""

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response

from app.config import settings

SESSION_DURATION_DAYS = 7
SESSION_COOKIE_NAME = "session"


def _now() -> float:
    """Single time source. Patched by tests to simulate expiry."""
    return time.time()


def _now_utc() -> datetime:
    return datetime.fromtimestamp(_now(), tz=timezone.utc)


def _b64url_encode(data: bytes) -> str:
    """URL-safe base64 encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    """URL-safe base64 decode, restoring padding if missing."""
    pad = (-len(data)) % 4
    return base64.urlsafe_b64decode(data + ("=" * pad))


def _signing_secret() -> bytes:
    """Bytes used as the HMAC key.

    If SESSION_SECRET is empty (local dev), fall back to ADMIN_PASSWORD as
    a placeholder so the app still works in test/dev. Production must set
    SESSION_SECRET to a real high-entropy value.
    """
    secret = settings.session_secret or settings.admin_password or "dev-fallback"
    return secret.encode("utf-8")


def sign_cookie(login_at: datetime | None = None) -> str:
    """Build a signed session cookie value.

    The returned string is exactly what goes into the 'session' cookie's value
    (i.e., everything after 'session='). It does NOT include the cookie name.
    """
    if login_at is None:
        login_at = _now_utc()
    expires_at = login_at + timedelta(days=SESSION_DURATION_DAYS)
    payload = {
        "login_at": login_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    sig = hmac.new(_signing_secret(), payload_bytes, hashlib.sha256).hexdigest()
    return _b64url_encode(payload_bytes) + "." + sig


def verify_cookie(value: str) -> bool:
    """Return True if the cookie value is valid, unexpired, and HMAC matches."""
    if not value or "." not in value:
        return False
    try:
        b64_payload, sig = value.rsplit(".", 1)
    except ValueError:
        return False
    try:
        payload_bytes = _b64url_decode(b64_payload)
    except (ValueError, base64.binascii.Error):
        return False
    expected = hmac.new(
        _signing_secret(), payload_bytes, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    expires_str = payload.get("expires_at")
    if not isinstance(expires_str, str):
        return False
    try:
        expires_at = datetime.fromisoformat(expires_str)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        # Treat naive timestamps as UTC for safety
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > _now_utc()


def set_session_cookie(response: Response) -> None:
    """Attach a freshly signed session cookie to the response."""
    cookie_value = sign_cookie()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_value,
        max_age=SESSION_DURATION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=False,  # Render uses TLS termination but TestClient is plain http
        path="/",
    )


def is_authenticated(request: Request) -> bool:
    """Return True if the request carries a valid session cookie."""
    value = request.cookies.get(SESSION_COOKIE_NAME)
    if not value:
        return False
    return verify_cookie(value)


def verify_password(candidate: str) -> bool:
    """Constant-time compare candidate against ADMIN_PASSWORD setting.

    Returns False if ADMIN_PASSWORD is unset or candidate is empty -
    we never accept an empty password as a "match" against an empty config.
    """
    expected = settings.admin_password or ""
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
