"""Tests for the Phase 3 page routes (GET / and GET /login).

What this covers:
    - GET / without a cookie redirects to /login (302)
    - GET / with a valid cookie returns 200 + HTML
    - GET / with an expired cookie redirects to /login (302)
    - GET /login without a cookie returns 200 + HTML
    - GET /login with a valid cookie redirects to / (302)
    - GET /login with an expired cookie returns 200 + HTML
    - Static files served at /static/main.css and /static/main.js
      with correct content types
    - Templates exist and contain expected data-state markers / form
      structure (smoke tests on the rendered HTML)

Auth flow used in tests:
    - Valid cookie obtained via POST /admin/login (mirrors authed_client
      fixture from conftest.py)
    - Expired cookie crafted by signing a payload with a past expires_at
      using app.auth.sign_cookie + a custom signing time
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE_NAME, sign_cookie
from app.main import app


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def page_client() -> TestClient:
    """A fresh TestClient that does NOT follow redirects.

    We need to assert the 302 status + Location header, so we must turn
    off the default follow-redirects behavior.
    """
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def authed_page_client() -> TestClient:
    """A TestClient pre-authenticated via /admin/login, no redirect-follow."""
    client = TestClient(app, follow_redirects=False)
    resp = client.post(
        "/admin/login",
        json={"password": os.environ["ADMIN_PASSWORD"]},
    )
    assert resp.status_code == 200, f"login setup failed: {resp.status_code} {resp.text}"
    return client


def _expired_cookie_value() -> str:
    """Build a cookie whose payload is signed but already expired.

    Signs with login_at one year ago so expires_at = login_at + 7 days
    is firmly in the past.
    """
    one_year_ago = datetime.now(tz=timezone.utc) - timedelta(days=365)
    return sign_cookie(login_at=one_year_ago)


# ---------------------------------------------------------------------------
# Template + static file existence smoke tests
# ---------------------------------------------------------------------------


def test_templates_directory_exists() -> None:
    assert TEMPLATES_DIR.is_dir(), "templates/ directory must exist"


def test_index_template_exists_and_non_empty() -> None:
    index_path = TEMPLATES_DIR / "index.html"
    assert index_path.is_file(), "templates/index.html must exist"
    content = index_path.read_text(encoding="utf-8")
    assert len(content) > 0, "templates/index.html must not be empty"
    # Smoke-check: spec mandates data-state on #progress
    assert 'id="progress"' in content
    assert "data-state" in content


def test_login_template_exists_and_non_empty() -> None:
    login_path = TEMPLATES_DIR / "login.html"
    assert login_path.is_file(), "templates/login.html must exist"
    content = login_path.read_text(encoding="utf-8")
    assert len(content) > 0, "templates/login.html must not be empty"
    assert "<form" in content
    assert 'id="password"' in content


def test_static_main_css_exists() -> None:
    css = STATIC_DIR / "main.css"
    assert css.is_file()
    assert css.stat().st_size > 0


def test_static_main_js_exists() -> None:
    js = STATIC_DIR / "main.js"
    assert js.is_file()
    assert js.stat().st_size > 0


# ---------------------------------------------------------------------------
# GET / behavior
# ---------------------------------------------------------------------------


def test_get_root_no_cookie_redirects_to_login(page_client: TestClient) -> None:
    resp = page_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_get_root_with_valid_cookie_returns_html(
    authed_page_client: TestClient,
) -> None:
    resp = authed_page_client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # Verify Jinja rendered the shell + key data-state hooks
    assert 'id="progress"' in body
    assert "data-state" in body
    assert 'id="email-body"' in body
    assert 'id="generate-btn"' in body
    # Static assets resolved through url_for
    assert "/static/main.css" in body
    assert "/static/main.js" in body


def test_get_root_with_expired_cookie_redirects_to_login(
    page_client: TestClient,
) -> None:
    expired = _expired_cookie_value()
    page_client.cookies.set(SESSION_COOKIE_NAME, expired)
    resp = page_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_get_root_with_tampered_cookie_redirects_to_login(
    page_client: TestClient,
) -> None:
    """Defensive: a cookie that does not verify is treated as missing."""
    page_client.cookies.set(SESSION_COOKIE_NAME, "garbage.value")
    resp = page_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# GET /login behavior
# ---------------------------------------------------------------------------


def test_get_login_no_cookie_returns_form(page_client: TestClient) -> None:
    resp = page_client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "<form" in body
    assert 'id="password"' in body
    assert 'id="login-btn"' in body
    assert "/static/main.css" in body


def test_get_login_with_valid_cookie_redirects_home(
    authed_page_client: TestClient,
) -> None:
    resp = authed_page_client.get("/login")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_get_login_with_expired_cookie_returns_form(
    page_client: TestClient,
) -> None:
    expired = _expired_cookie_value()
    page_client.cookies.set(SESSION_COOKIE_NAME, expired)
    resp = page_client.get("/login")
    assert resp.status_code == 200
    body = resp.text
    assert "<form" in body
    assert 'id="password"' in body


# ---------------------------------------------------------------------------
# Static asset serving
# ---------------------------------------------------------------------------


def test_static_main_css_served(page_client: TestClient) -> None:
    resp = page_client.get("/static/main.css")
    assert resp.status_code == 200
    assert "text/css" in resp.headers["content-type"]
    body = resp.text
    # Spot check: design tokens block must be present (per DESIGN.md sec 1)
    assert "--bg: #f5f5f7" in body
    assert "--blue: #2756f7" in body


def test_static_main_js_served(page_client: TestClient) -> None:
    resp = page_client.get("/static/main.js")
    assert resp.status_code == 200
    ct = resp.headers["content-type"]
    # Browsers / starlette serve as application/javascript or text/javascript
    assert "javascript" in ct
    body = resp.text
    # Spot check: state machine + polling functions must be present
    assert "startGeneration" in body
    assert "pollStatus" in body


# ---------------------------------------------------------------------------
# Misc - no-cache regressions, content type
# ---------------------------------------------------------------------------


def test_index_html_response_is_text_html(authed_page_client: TestClient) -> None:
    resp = authed_page_client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


def test_login_html_response_is_text_html(page_client: TestClient) -> None:
    resp = page_client.get("/login")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")


# ---------------------------------------------------------------------------
# Bug-fix regressions (BUG-02, BUG-03, BUG-04 from playwright QA)
# ---------------------------------------------------------------------------


def test_generate_button_renders_disabled_initially(
    authed_page_client: TestClient,
) -> None:
    """BUG-02: the Generate button must be rendered with the `disabled`
    attribute on initial page load. The empty textarea has no content,
    so the button cannot be valid. JS keeps the state in sync; this
    test pins the server-rendered baseline so a JS bug cannot leave
    the user with a falsely-enabled button.
    """
    resp = authed_page_client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Find the <button id="generate-btn" ...> opening tag (multi-line)
    # and confirm `disabled` appears inside it. We look for the literal
    # token, not a substring of "disabled" so we match the HTML
    # boolean attribute and not e.g. an aria-disabled value.
    import re

    btn_match = re.search(
        r'<button\s+id="generate-btn"[^>]*?>',
        body,
        flags=re.DOTALL,
    )
    assert btn_match is not None, "generate-btn opening tag not found in HTML"
    assert re.search(r"\bdisabled\b", btn_match.group(0)), (
        "generate-btn must render with `disabled` attribute on initial load. "
        "Got: " + btn_match.group(0)
    )


def test_index_suppresses_favicon_404(authed_page_client: TestClient) -> None:
    """BUG-03: index.html declares a `<link rel="icon">` so browsers
    do not fire a 404 on /favicon.ico for every page load.
    """
    resp = authed_page_client.get("/")
    assert resp.status_code == 200
    assert 'rel="icon"' in resp.text


def test_login_suppresses_favicon_404(page_client: TestClient) -> None:
    """BUG-03: login.html also suppresses the favicon 404."""
    resp = page_client.get("/login")
    assert resp.status_code == 200
    assert 'rel="icon"' in resp.text


def test_login_form_has_username_autocomplete(page_client: TestClient) -> None:
    """BUG-04: password forms should have an associated username field
    so password managers and screen readers can pair the credential.
    The shared-password tool ships a hidden username input with
    autocomplete="username".
    """
    resp = page_client.get("/login")
    assert resp.status_code == 200
    body = resp.text
    assert 'autocomplete="username"' in body
    assert 'autocomplete="current-password"' in body


def test_static_main_js_has_format_agnostic_resolver(
    page_client: TestClient,
) -> None:
    """BUG-01: main.js must ship the tokenizer-based resolver. We pin
    a few load-bearing strings so a refactor that drops the new
    behavior gets caught.
    """
    resp = page_client.get("/static/main.js")
    assert resp.status_code == 200
    body = resp.text
    # The shared tokenizer that handles BOTH formats
    assert "function tokenizeSpintax" in body
    # Variables must pass through unchanged
    assert "'variable'" in body
    # Both spintax kinds must be wired up
    assert "'double'" in body
    assert "'single'" in body
