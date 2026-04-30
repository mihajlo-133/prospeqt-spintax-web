"""Shared pytest fixtures for prospeqt-spintax-web tests.

Phase 0: client fixture + env var setup only.
Phase 1+: fixtures for mocked Instantly/OpenAI responses via respx.
Phase 2: job-state fixtures, spend-cap fixtures, session-secret env.
"""

import os

import pytest
from fastapi.testclient import TestClient

# Set required env vars before importing app so pydantic-settings reads them.
# These are test-only sentinel values - never real credentials.
os.environ.setdefault("ADMIN_PASSWORD", "test-password-sentinel")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-000-sentinel")
os.environ.setdefault("OPENAI_MODEL", "o3")
os.environ.setdefault("SESSION_SECRET", "test-secret-32-characters-minimum-x")

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    """Synchronous FastAPI test client.

    Reused across the session for speed. Phase 0 endpoints are read-only,
    so session scope is safe. Narrow to function scope if a test mutates
    app state (e.g., job dict, spend cap counter).
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed_client():
    """Function-scoped TestClient pre-authenticated via /admin/login.

    Phase 2 gates ALL /api/* routes (lint, qa, spintax, status) behind a
    session cookie. Phase 1 route tests use this fixture so they can hit
    /api/lint and /api/qa without 401.

    Function scope (not session) so the cookie jar is fresh per test
    and doesn't leak across tests that mutate cookies (auth tampering tests).
    """
    with TestClient(app) as c:
        r = c.post(
            "/admin/login",
            json={"password": os.environ["ADMIN_PASSWORD"]},
        )
        assert r.status_code == 200, f"authed_client login failed: {r.status_code} {r.text}"
        yield c


@pytest.fixture(autouse=True)
def _reset_spend_between_tests():
    """Auto-reset the spend module before every test.

    The spend module is process-singleton. Without this fixture,
    test order leaks state — e.g., a 'cap hit' test would block
    every subsequent /api/spintax test in the session.
    """
    try:
        from app import spend

        spend._reset_for_test(0.0)
    except ImportError:
        # Phase 0/1 didn't have spend.py
        pass
    yield


@pytest.fixture(autouse=True)
def _reset_wordhippo_singleton_between_tests():
    """Drop the shared httpx.AsyncClient singleton before every test.

    Phase 3 introduced a module-level singleton in `app/tools/wordhippo_client.py`
    for connection pooling. Without this reset, the client created during one
    test (against a real or mocked transport) persists into the next test —
    where respx may have re-patched the global httpx transport, causing the
    cached client to issue requests against the wrong mock graph.
    """
    try:
        from app.tools import wordhippo_client

        wordhippo_client._reset_for_tests()
    except ImportError:
        # Phase 0/1/2 didn't have the singleton
        pass
    yield
