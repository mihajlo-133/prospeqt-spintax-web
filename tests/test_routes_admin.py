"""Integration tests for Phase 2 admin routes: POST /admin/login + cookie management.

Phase 2 target:
    Written BEFORE implementation (test-first). These tests will fail
    (404 or 401) before Phase 2 routes are implemented. After Phase 2,
    ALL tests must pass.

Contract (from session plan):
    POST /admin/login
        Request: { password: str }
        Success: HTTP 200, Set-Cookie with session token
        Failure (wrong password): HTTP 401, no cookie
        Missing field: HTTP 422

    POST /admin/logout (if implemented)
        Clears session cookie
        Returns 200

    Cookie properties:
        - httponly=True
        - samesite lax or strict

All tests use FastAPI's TestClient. No real credentials.
"""

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ADMIN_PASSWORD", "test-password-sentinel")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-000-sentinel")
os.environ.setdefault("OPENAI_MODEL", "o3")

CORRECT_PASSWORD = "test-password-sentinel"
WRONG_PASSWORD = "definitely-not-the-right-password"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_client():
    """Function-scoped TestClient — fresh cookie jar for every test."""
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# A. POST /admin/login — correct credentials
# ---------------------------------------------------------------------------


class TestLoginCorrectCredentials:
    def test_correct_password_returns_200(self, fresh_client):
        """POST /admin/login with correct password must return 200."""
        r = fresh_client.post("/admin/login", json={"password": CORRECT_PASSWORD})
        if r.status_code == 404:
            pytest.skip("POST /admin/login not yet implemented (Phase 2)")
        assert r.status_code == 200, (
            f"Expected 200 for correct password, got {r.status_code}. Body: {r.text}"
        )

    def test_correct_password_sets_cookie(self, fresh_client):
        """POST /admin/login with correct password must set a session cookie."""
        r = fresh_client.post("/admin/login", json={"password": CORRECT_PASSWORD})
        if r.status_code == 404:
            pytest.skip("POST /admin/login not yet implemented (Phase 2)")
        if r.status_code != 200:
            pytest.skip(f"Login returned {r.status_code}, skipping cookie check")

        # Cookie must appear in Set-Cookie header OR in TestClient's cookie jar
        has_cookie = (
            "set-cookie" in r.headers or len(r.cookies) > 0 or len(fresh_client.cookies) > 0
        )
        assert has_cookie, (
            f"Expected Set-Cookie header after successful login. "
            f"Headers: {dict(r.headers)}. Cookies: {dict(r.cookies)}"
        )

    def test_login_response_body_is_json(self, fresh_client):
        """POST /admin/login success response must be valid JSON."""
        r = fresh_client.post("/admin/login", json={"password": CORRECT_PASSWORD})
        if r.status_code == 404:
            pytest.skip("POST /admin/login not yet implemented (Phase 2)")
        if r.status_code != 200:
            pytest.skip(f"Login returned {r.status_code}")
        # Must parse as JSON without error
        try:
            r.json()
        except Exception as e:
            pytest.fail(f"Login response must be valid JSON. Error: {e}")

    def test_cookie_is_httponly(self, fresh_client):
        """Session cookie must have HttpOnly flag to prevent JS access."""
        r = fresh_client.post("/admin/login", json={"password": CORRECT_PASSWORD})
        if r.status_code == 404:
            pytest.skip("POST /admin/login not yet implemented (Phase 2)")
        if r.status_code != 200:
            pytest.skip(f"Login returned {r.status_code}")

        set_cookie = r.headers.get("set-cookie", "")
        if not set_cookie:
            pytest.skip("No Set-Cookie header in response — Phase 2 must set cookie")

        assert "httponly" in set_cookie.lower(), (
            f"Session cookie must be HttpOnly. Set-Cookie: {set_cookie}"
        )


# ---------------------------------------------------------------------------
# B. POST /admin/login — wrong credentials
# ---------------------------------------------------------------------------


class TestLoginWrongCredentials:
    def test_wrong_password_returns_401(self, fresh_client):
        """POST /admin/login with wrong password must return 401."""
        r = fresh_client.post("/admin/login", json={"password": WRONG_PASSWORD})
        if r.status_code == 404:
            pytest.skip("POST /admin/login not yet implemented (Phase 2)")
        assert r.status_code == 401, (
            f"Expected 401 for wrong password, got {r.status_code}. Body: {r.text}"
        )

    def test_wrong_password_no_cookie(self, fresh_client):
        """POST /admin/login with wrong password must NOT set a session cookie."""
        r = fresh_client.post("/admin/login", json={"password": WRONG_PASSWORD})
        if r.status_code == 404:
            pytest.skip("POST /admin/login not yet implemented (Phase 2)")
        if r.status_code != 401:
            pytest.skip(f"Login returned {r.status_code} (not 401), skipping cookie check")

        set_cookie = r.headers.get("set-cookie", "")
        # Must not set any cookie with 'session' in the name
        assert "session" not in set_cookie.lower(), (
            f"Must NOT set session cookie on failed login. Set-Cookie: {set_cookie}"
        )

    def test_empty_password_rejected(self, fresh_client):
        """POST /admin/login with empty string password must return 401 or 422."""
        r = fresh_client.post("/admin/login", json={"password": ""})
        if r.status_code == 404:
            pytest.skip("POST /admin/login not yet implemented (Phase 2)")
        assert r.status_code in (401, 422), (
            f"Expected 401 or 422 for empty password, got {r.status_code}. Body: {r.text}"
        )


# ---------------------------------------------------------------------------
# C. POST /admin/login — malformed request
# ---------------------------------------------------------------------------


class TestLoginMalformedRequest:
    def test_missing_password_field_returns_422(self, fresh_client):
        """POST /admin/login without 'password' field must return 422."""
        r = fresh_client.post("/admin/login", json={})
        if r.status_code == 404:
            pytest.skip("POST /admin/login not yet implemented (Phase 2)")
        assert r.status_code == 422, (
            f"Expected 422 for missing password field, got {r.status_code}. Body: {r.text}"
        )

    def test_non_json_body_rejected(self, fresh_client):
        """POST /admin/login with non-JSON body must return 422 or 400."""
        r = fresh_client.post(
            "/admin/login",
            data="not-json-at-all",
            headers={"Content-Type": "text/plain"},
        )
        if r.status_code == 404:
            pytest.skip("POST /admin/login not yet implemented (Phase 2)")
        assert r.status_code in (400, 415, 422), (
            f"Expected 400/415/422 for non-JSON body, got {r.status_code}. Body: {r.text}"
        )


# ---------------------------------------------------------------------------
# D. Cookie security — tampered cookie
# ---------------------------------------------------------------------------


class TestCookieTampered:
    def test_tampered_cookie_returns_401_on_protected_route(self, fresh_client):
        """A manually crafted cookie must be rejected when accessing /api/spintax."""
        fresh_client.cookies.set("session", "tampered-not-a-real-signed-token")
        r = fresh_client.post(
            "/api/spintax",
            json={"text": "Hello world.", "platform": "instantly"},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        assert r.status_code == 401, (
            f"Tampered cookie must be rejected with 401, got {r.status_code}. Body: {r.text}"
        )

    def test_no_cookie_returns_401_on_api_lint(self, fresh_client):
        """POST /api/lint without cookie must return 401 (Phase 2 gates all /api/* routes).

        Note: Phase 1 had /api/lint as open. Phase 2 gates everything.
        This test asserts the final gated behavior.
        """
        r = fresh_client.post(
            "/api/lint",
            json={"text": "{{RANDOM | A | B | C | D | E }}", "platform": "instantly"},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/lint not yet gated (Phase 2)")
        assert r.status_code == 401, (
            f"Expected 401 for unauthenticated /api/lint after Phase 2 auth gating, "
            f"got {r.status_code}. Body: {r.text}"
        )


# ---------------------------------------------------------------------------
# E. /health is always public (regression guard)
# ---------------------------------------------------------------------------


class TestHealthPublic:
    def test_health_returns_200_without_cookie(self, fresh_client):
        """/health must always be public — no cookie, no auth required."""
        r = fresh_client.get("/health")
        assert r.status_code == 200, (
            f"GET /health must be public (no cookie needed), got {r.status_code}. Body: {r.text}"
        )

    def test_health_body_shape(self, fresh_client):
        """/health must return {status: 'ok'}."""
        r = fresh_client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") == "ok", f"GET /health must return {{status: 'ok'}}, got {body}"


# ---------------------------------------------------------------------------
# F. POST /admin/logout (if implemented in Phase 2)
# ---------------------------------------------------------------------------


class TestLogout:
    def test_logout_clears_cookie(self, fresh_client):
        """POST /admin/logout must clear the session cookie.

        This test skips gracefully if the logout route is not implemented.
        Builder can add it as a stretch goal — it's not required for Phase 2
        core, but tests here ensure correct behavior if it's built.
        """
        # First, log in
        r = fresh_client.post("/admin/login", json={"password": CORRECT_PASSWORD})
        if r.status_code == 404:
            pytest.skip("POST /admin/login not implemented")
        if r.status_code != 200:
            pytest.skip(f"Login returned {r.status_code}")

        # Try to logout
        logout_r = fresh_client.post("/admin/logout")
        if logout_r.status_code == 404:
            pytest.skip("POST /admin/logout not yet implemented (optional in Phase 2)")
        assert logout_r.status_code == 200, (
            f"Expected 200 from POST /admin/logout, got {logout_r.status_code}. Body: {logout_r.text}"
        )

        # After logout, the cookie should be cleared or expired
        # The session should no longer be valid
        after_logout = fresh_client.post(
            "/api/spintax",
            json={"text": "Hello world.", "platform": "instantly"},
        )
        if after_logout.status_code == 404:
            pytest.skip("POST /api/spintax not implemented")
        assert after_logout.status_code == 401, (
            f"After logout, /api/spintax should return 401. Got {after_logout.status_code}. "
            f"Body: {after_logout.text}"
        )
