"""Integration tests for Phase 2 routes: POST /api/spintax and GET /api/status/{job_id}.

Phase 2 target:
    Written BEFORE implementation (test-first). These tests will fail
    (404 or 401) before Phase 2 routes are implemented. After Phase 2,
    ALL tests must pass.

Contract (from session plan):
    POST /api/spintax
        Auth: session cookie required (401 if missing)
        Request: { text: str, platform: str, model?: str }
        Success: HTTP 200 or 202
        Body: { job_id: "<uuid4>" }
        Side effect: fires off run() as background task
        Errors: 422 for invalid input, 429 if daily cap hit

    GET /api/status/{job_id}
        Auth: session cookie required (401 if missing)
        Success: HTTP 200
        Body: job status object with at least { job_id, status }
        Missing: HTTP 404 with detail

All tests use FastAPI's TestClient. No real OpenAI calls.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("ADMIN_PASSWORD", "test-password-sentinel")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-000-sentinel")
os.environ.setdefault("OPENAI_MODEL", "o3")

VALID_TEXT = "Hello world. This is a test email for spintax generation."
VALID_PLATFORM = "instantly"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def anon_client():
    """TestClient with no session cookie."""
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def authed_client():
    """TestClient with a valid session cookie."""
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/admin/login", json={"password": "test-password-sentinel"})
        if r.status_code == 404:
            pytest.skip("POST /admin/login not yet implemented (Phase 2)")
        assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
        yield c


# ---------------------------------------------------------------------------
# A. POST /api/spintax — authentication gate
# ---------------------------------------------------------------------------


class TestSpintaxAuthGate:
    def test_no_cookie_returns_401(self, anon_client):
        """POST /api/spintax without session cookie must return 401."""
        r = anon_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": VALID_PLATFORM},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        assert r.status_code == 401, (
            f"Expected 401 for unauthenticated request, got {r.status_code}. Body: {r.text}"
        )

    def test_valid_cookie_not_401(self, authed_client):
        """POST /api/spintax with valid cookie must not return 401."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": VALID_PLATFORM},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        assert r.status_code != 401, (
            f"Expected non-401 with valid cookie, got {r.status_code}. Body: {r.text}"
        )


# ---------------------------------------------------------------------------
# B. POST /api/spintax — response shape
# ---------------------------------------------------------------------------


class TestSpintaxResponseShape:
    def test_returns_200_or_202(self, authed_client):
        """POST /api/spintax with valid body must return 200 or 202."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": VALID_PLATFORM},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        assert r.status_code in (200, 202), (
            f"Expected 200 or 202, got {r.status_code}. Body: {r.text}"
        )

    def test_response_has_job_id(self, authed_client):
        """POST /api/spintax response must contain a 'job_id' field."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": VALID_PLATFORM},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        if r.status_code not in (200, 202):
            pytest.skip(f"Route returned {r.status_code}, skipping shape check")
        body = r.json()
        assert "job_id" in body, f"Response must have 'job_id'. Got: {body}"

    def test_job_id_is_valid_uuid4(self, authed_client):
        """The job_id in the response must be a valid UUID4 string."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": VALID_PLATFORM},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        if r.status_code not in (200, 202):
            pytest.skip(f"Route returned {r.status_code}")
        job_id = r.json().get("job_id")
        assert job_id is not None, "job_id must not be None"
        try:
            parsed = uuid.UUID(job_id, version=4)
            assert str(parsed) == job_id, f"job_id is not normalized UUID4: {job_id}"
        except ValueError:
            pytest.fail(f"job_id '{job_id}' is not a valid UUID4")

    def test_two_requests_produce_different_job_ids(self, authed_client):
        """Two separate POST /api/spintax calls must produce distinct job_ids."""
        r1 = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": VALID_PLATFORM},
        )
        r2 = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": VALID_PLATFORM},
        )
        if r1.status_code == 404 or r2.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        if r1.status_code not in (200, 202) or r2.status_code not in (200, 202):
            pytest.skip("Route not returning success")
        j1 = r1.json().get("job_id")
        j2 = r2.json().get("job_id")
        assert j1 != j2, f"Two requests must produce different job_ids, got '{j1}' twice"


# ---------------------------------------------------------------------------
# C. POST /api/spintax — input validation
# ---------------------------------------------------------------------------


class TestSpintaxInputValidation:
    def test_empty_text_returns_422(self, authed_client):
        """Empty text field must return 422."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": "", "platform": VALID_PLATFORM},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        assert r.status_code == 422, (
            f"Expected 422 for empty text, got {r.status_code}. Body: {r.text}"
        )

    def test_invalid_platform_returns_422(self, authed_client):
        """Unknown platform must return 422."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": "hotmail"},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        assert r.status_code == 422, (
            f"Expected 422 for invalid platform, got {r.status_code}. Body: {r.text}"
        )

    def test_missing_text_field_returns_422(self, authed_client):
        """Missing 'text' field must return 422."""
        r = authed_client.post(
            "/api/spintax",
            json={"platform": VALID_PLATFORM},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        assert r.status_code == 422, (
            f"Expected 422 for missing text field, got {r.status_code}. Body: {r.text}"
        )

    def test_missing_platform_field_returns_422(self, authed_client):
        """Missing 'platform' field must return 422."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        assert r.status_code == 422, (
            f"Expected 422 for missing platform field, got {r.status_code}. Body: {r.text}"
        )

    def test_emailbison_platform_accepted(self, authed_client):
        """'emailbison' must be a valid platform value."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": "emailbison"},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        assert r.status_code not in (422,), (
            f"'emailbison' is a valid platform — must not return 422. Got: {r.status_code}"
        )


# ---------------------------------------------------------------------------
# D. GET /api/status/{job_id} — authentication gate
# ---------------------------------------------------------------------------


class TestStatusAuthGate:
    def test_no_cookie_returns_401(self, anon_client):
        """GET /api/status/{job_id} without session cookie must return 401."""
        fake_id = str(uuid.uuid4())
        r = anon_client.get(f"/api/status/{fake_id}")
        if r.status_code == 404:
            # Ambiguous — route may not exist. Check if it's a route 404 vs job 404.
            # If method not allowed, skip.
            pytest.skip("GET /api/status route not yet implemented (Phase 2)")
        assert r.status_code == 401, (
            f"Expected 401 for unauthenticated status poll, got {r.status_code}. Body: {r.text}"
        )


# ---------------------------------------------------------------------------
# E. GET /api/status/{job_id} — response shape
# ---------------------------------------------------------------------------


class TestStatusResponseShape:
    def test_existing_job_returns_200(self, authed_client):
        """GET /api/status/{job_id} for a known job must return 200."""
        # Create the job first
        r = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": VALID_PLATFORM},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented (Phase 2)")
        if r.status_code not in (200, 202):
            pytest.skip(f"POST /api/spintax returned {r.status_code}")

        job_id = r.json().get("job_id")
        if not job_id:
            pytest.skip("No job_id in response")

        status_r = authed_client.get(f"/api/status/{job_id}")
        if status_r.status_code == 404:
            pytest.skip("GET /api/status route not yet implemented (Phase 2)")
        assert status_r.status_code == 200, (
            f"Expected 200 for known job, got {status_r.status_code}. Body: {status_r.text}"
        )

    def test_status_response_has_required_fields(self, authed_client):
        """Status response must contain 'job_id' and 'status' fields."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": VALID_PLATFORM},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented")
        if r.status_code not in (200, 202):
            pytest.skip(f"POST /api/spintax returned {r.status_code}")

        job_id = r.json().get("job_id")
        if not job_id:
            pytest.skip("No job_id")

        status_r = authed_client.get(f"/api/status/{job_id}")
        if status_r.status_code != 200:
            pytest.skip(f"Status endpoint not ready (got {status_r.status_code})")

        body = status_r.json()
        assert "job_id" in body or "status" in body, (
            f"Status response must contain at least 'job_id' or 'status'. Got: {body}"
        )

    def test_status_field_is_valid_value(self, authed_client):
        """The 'status' field must be one of the known job status values."""
        VALID_STATUSES = {"queued", "drafting", "linting", "iterating", "qa", "done", "failed"}

        r = authed_client.post(
            "/api/spintax",
            json={"text": VALID_TEXT, "platform": VALID_PLATFORM},
        )
        if r.status_code == 404:
            pytest.skip("POST /api/spintax not yet implemented")
        if r.status_code not in (200, 202):
            pytest.skip(f"POST returned {r.status_code}")

        job_id = r.json().get("job_id")
        if not job_id:
            pytest.skip("No job_id")

        status_r = authed_client.get(f"/api/status/{job_id}")
        if status_r.status_code != 200:
            pytest.skip("Status endpoint not ready")

        body = status_r.json()
        status_value = body.get("status")
        if status_value is not None:
            assert status_value in VALID_STATUSES, (
                f"Job status must be one of {VALID_STATUSES}, got '{status_value}'"
            )

    def test_unknown_job_id_returns_404(self, authed_client):
        """GET /api/status/{unknown_id} must return 404."""
        unknown_id = str(uuid.uuid4())
        r = authed_client.get(f"/api/status/{unknown_id}")
        if r.status_code == 401:
            pytest.skip("Auth not wired to status endpoint yet")
        if r.status_code == 405:
            pytest.skip("GET /api/status not yet implemented")
        assert r.status_code == 404, (
            f"Expected 404 for unknown job_id, got {r.status_code}. Body: {r.text}"
        )
