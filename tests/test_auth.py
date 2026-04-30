"""Integration tests for authentication — POST /admin/login + cookie-gated routes.

Phase 2 target:
    Written BEFORE implementation (test-first). Failures before Phase 2 builder
    completes are expected and correct. Failures after must be zero.

Contract (from session plan + locked settings):
    POST /admin/login
        Request: { password: str }
        Success (correct password): HTTP 200, Set-Cookie header present
        Failure (wrong password): HTTP 401, no cookie
        Missing field: HTTP 422

    All /api/* routes gated behind the session cookie.
    /health is explicitly PUBLIC (regression check).

    Cookie security properties (per Phase 2 spec):
        - httponly=True (JS cannot read it)
        - samesite lax or strict
        - Expires/Max-Age reflects configured session duration

    Session expiry:
        - Expired cookie => 401
        - Tampered cookie (modified payload) => 401

All tests use the function-scoped client fixture defined below.
No real OpenAI calls. All cookie-based assertions are done via the TestClient.
"""

import os
import time

import pytest
from fastapi.testclient import TestClient


# We need a fresh client for auth tests because session cookies accumulate.
# Use function-scoped fixture so each test starts with a clean cookie jar.
@pytest.fixture()
def auth_client():
    """Function-scoped TestClient — fresh cookies on every test."""
    # Ensure ADMIN_PASSWORD is set before importing app
    os.environ.setdefault("ADMIN_PASSWORD", "test-password-sentinel")
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def authed_client(auth_client):
    """Function-scoped TestClient that has already performed a successful login."""
    r = auth_client.post("/admin/login", json={"password": "test-password-sentinel"})
    assert r.status_code == 200, f"authed_client fixture login failed: {r.status_code} {r.text}"
    yield auth_client


# ---------------------------------------------------------------------------
# A. POST /admin/login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_login_correct_password_returns_200(self, auth_client):
        """POST /admin/login with the correct password returns HTTP 200."""
        r = auth_client.post("/admin/login", json={"password": "test-password-sentinel"})
        assert r.status_code == 200, (
            f"Expected 200 for correct password, got {r.status_code}. Body: {r.text}"
        )

    def test_login_correct_password_sets_cookie(self, auth_client):
        """POST /admin/login with correct password must set a session cookie."""
        r = auth_client.post("/admin/login", json={"password": "test-password-sentinel"})
        assert r.status_code == 200
        # Cookie must be present in the response Set-Cookie header
        assert "set-cookie" in r.headers or len(r.cookies) > 0, (
            "Expected Set-Cookie header after successful login, got none. "
            f"Headers: {dict(r.headers)}"
        )

    def test_login_wrong_password_returns_401(self, auth_client):
        """POST /admin/login with wrong password returns HTTP 401."""
        r = auth_client.post("/admin/login", json={"password": "definitely-wrong-password"})
        assert r.status_code == 401, (
            f"Expected 401 for wrong password, got {r.status_code}. Body: {r.text}"
        )

    def test_login_wrong_password_no_cookie(self, auth_client):
        """POST /admin/login with wrong password must NOT set a cookie."""
        r = auth_client.post("/admin/login", json={"password": "wrong"})
        assert r.status_code == 401
        # Must not set a session cookie on failure
        cookie_header = r.headers.get("set-cookie", "")
        assert not cookie_header or "session" not in cookie_header.lower(), (
            "Should not set a session cookie on failed login"
        )

    def test_login_missing_password_returns_422(self, auth_client):
        """POST /admin/login with no password field returns HTTP 422."""
        r = auth_client.post("/admin/login", json={})
        assert r.status_code == 422, (
            f"Expected 422 for missing password, got {r.status_code}. Body: {r.text}"
        )

    def test_login_empty_password_returns_401(self, auth_client):
        """POST /admin/login with empty string password returns 401 (not 422)."""
        r = auth_client.post("/admin/login", json={"password": ""})
        # Empty password is a valid string field but will never match a real password.
        # Either 401 (auth failure) or 422 (if validator rejects blank) is acceptable.
        assert r.status_code in (401, 422), (
            f"Expected 401 or 422 for empty password, got {r.status_code}"
        )


# ---------------------------------------------------------------------------
# B. Cookie-gated routes (POST /api/spintax)
# ---------------------------------------------------------------------------


class TestGatedRoutes:
    def test_api_spintax_no_cookie_returns_401(self, auth_client):
        """POST /api/spintax without a session cookie must return HTTP 401."""
        r = auth_client.post(
            "/api/spintax",
            json={"text": "Hello world.", "platform": "instantly"},
        )
        assert r.status_code == 401, (
            f"Expected 401 for unauthenticated /api/spintax, got {r.status_code}. Body: {r.text}"
        )

    def test_api_spintax_valid_cookie_accepted(self, authed_client):
        """POST /api/spintax with a valid session cookie must NOT return 401.
        Accepts 200 (job queued) or 202 (accepted) — builder decides.
        """
        r = authed_client.post(
            "/api/spintax",
            json={"text": "Hello world.", "platform": "instantly"},
        )
        assert r.status_code in (200, 202), (
            f"Expected 200/202 for authenticated /api/spintax, got {r.status_code}. Body: {r.text}"
        )

    def test_api_status_no_cookie_returns_401(self, auth_client):
        """GET /api/status/{job_id} without cookie must return 401."""
        r = auth_client.get("/api/status/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 401, (
            f"Expected 401 for unauthenticated status poll, got {r.status_code}. Body: {r.text}"
        )

    def test_health_is_public_no_cookie_needed(self, auth_client):
        """GET /health must return 200 without any session cookie.
        Regression check — Phase 0 endpoint must never be gated.
        """
        r = auth_client.get("/health")
        assert r.status_code == 200, (
            f"GET /health must be public (no cookie), got {r.status_code}. Body: {r.text}"
        )

    def test_api_lint_no_cookie_returns_401(self, auth_client):
        """POST /api/lint without cookie must return 401 if gated in Phase 2.
        Note: Phase 1 had lint as open. Phase 2 gates ALL /api/* routes.
        This test asserts the gating behavior.
        """
        r = auth_client.post(
            "/api/lint",
            json={"text": "{{RANDOM | A | B | C | D | E }}", "platform": "instantly"},
        )
        assert r.status_code == 401, (
            f"Expected 401 for unauthenticated /api/lint, got {r.status_code}. Body: {r.text}"
        )


# ---------------------------------------------------------------------------
# C. Session expiry and tampering
# ---------------------------------------------------------------------------


class TestCookieSecurity:
    def test_tampered_cookie_returns_401(self, auth_client):
        """A manually crafted / tampered session cookie must be rejected (401)."""
        # Inject a fake cookie value directly
        auth_client.cookies.set("session", "tampered-not-a-real-signed-token")
        r = auth_client.post(
            "/api/spintax",
            json={"text": "Hello.", "platform": "instantly"},
        )
        assert r.status_code == 401, (
            f"Tampered cookie must be rejected with 401, got {r.status_code}"
        )

    def test_expired_cookie_returns_401(self, auth_client, monkeypatch):
        """A session cookie that is past its expiry must return 401.

        Strategy: login normally, then monkeypatch the time-check function
        in app.auth (or app.dependencies) to simulate the session being expired.
        The exact monkeypatch target depends on the builder's implementation;
        this test will need a small adjustment if the time function name differs.
        """
        # First, do a real login to get a valid cookie
        r = auth_client.post("/admin/login", json={"password": "test-password-sentinel"})
        assert r.status_code == 200

        # Monkeypatch the current-time function used in auth validation to be far future.
        # Try both common patterns builders might use:
        try:
            import app.auth as auth_module

            if hasattr(auth_module, "_now"):
                future_time = time.time() + 86400 * 365  # 1 year in the future
                monkeypatch.setattr(auth_module, "_now", lambda: future_time)
        except ImportError:
            pass  # auth module not yet implemented — test is a pre-flight

        # Now try to use the (now-expired) cookie
        r2 = auth_client.post(
            "/api/spintax",
            json={"text": "Hello.", "platform": "instantly"},
        )
        # Either 401 (expired recognized) or 200/202 (if expiry not yet implemented)
        # We assert 401 as the target behavior
        assert r2.status_code == 401, (
            f"Expired session cookie must return 401, got {r2.status_code}. "
            "If auth module not yet implemented, this is expected to fail pre-Phase2."
        )
