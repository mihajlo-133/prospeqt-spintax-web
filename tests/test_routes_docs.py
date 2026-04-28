"""Tests for the public documentation surfaces.

Routes covered:
    GET /docs           -> HTML reference (templates/docs.html)
    GET /llms.txt       -> LLM-optimized markdown (static/llms.txt)
    GET /openapi.json   -> Hand-built OpenAPI 3.1 spec

All three routes are PUBLIC (no auth). The spec content is hand-written
in app/routes/docs.py - if you change that, update the assertions here.

This file deliberately uses a plain TestClient (no auth) to confirm the
routes are reachable without a session cookie or bearer token.
"""

from fastapi.testclient import TestClient

from app.main import app


# ---------------------------------------------------------------------------
# GET /docs
# ---------------------------------------------------------------------------


def test_docs_html_returns_200_unauthenticated() -> None:
    """The /docs HTML page must be public."""
    with TestClient(app) as c:
        r = c.get("/docs")
    assert r.status_code == 200, r.text


def test_docs_html_content_type_is_html() -> None:
    """The /docs response must declare HTML content type."""
    with TestClient(app) as c:
        r = c.get("/docs")
    ctype = r.headers.get("content-type", "")
    assert "text/html" in ctype, f"unexpected content-type: {ctype!r}"


def test_docs_html_contains_branding_and_api_label() -> None:
    """Smoke-check that the rendered HTML carries the branding."""
    with TestClient(app) as c:
        r = c.get("/docs")
    body = r.text
    # Generic API mention
    assert "API" in body
    # Spintax-specific branding (page title plus visible heading)
    assert "Spintax" in body


def test_docs_html_lists_all_eight_endpoints() -> None:
    """The HTML page must reference every documented /api/* path."""
    with TestClient(app) as c:
        r = c.get("/docs")
    body = r.text
    for path in (
        "/api/spintax",
        "/api/status",
        "/api/spintax/batch",
        "/api/lint",
        "/api/qa",
    ):
        assert path in body, f"missing endpoint reference: {path}"


# ---------------------------------------------------------------------------
# GET /llms.txt
# ---------------------------------------------------------------------------


def test_llms_txt_returns_200_unauthenticated() -> None:
    """The /llms.txt route must be public."""
    with TestClient(app) as c:
        r = c.get("/llms.txt")
    assert r.status_code == 200, r.text


def test_llms_txt_content_type_is_plaintext() -> None:
    """The /llms.txt response must declare text/plain content type."""
    with TestClient(app) as c:
        r = c.get("/llms.txt")
    ctype = r.headers.get("content-type", "")
    assert "text/plain" in ctype, f"unexpected content-type: {ctype!r}"


def test_llms_txt_has_expected_section_headers() -> None:
    """Verify the LLM-optimized markdown structure.

    Section headers and key field names that AI agents grep for must
    survive the build pipeline intact.
    """
    with TestClient(app) as c:
        r = c.get("/llms.txt")
    body = r.text
    # Top-level headings
    assert "# Prospeqt Spintax API" in body
    # Section markers
    assert "## Endpoints" in body
    assert "## Models" in body
    assert "## Drift revision loop" in body
    assert "## Error codes" in body
    # Endpoint markers
    assert "POST /api/spintax" in body
    assert "GET /api/status" in body
    assert "POST /api/spintax/batch" in body
    assert "POST /api/lint" in body
    assert "POST /api/qa" in body
    # Result-field name agents key off
    assert "drift_revisions" in body
    assert "drift_unresolved" in body


def test_llms_txt_preserves_jinja_like_placeholders() -> None:
    """The literal {{firstName}} placeholder must NOT be Jinja-rendered.

    The llms.txt content uses {{firstName}} and {{companyName}} as
    documentation examples for the spintax variable syntax. Serving
    through Jinja would either crash or silently strip these.
    """
    with TestClient(app) as c:
        r = c.get("/llms.txt")
    assert "{{firstName}}" in r.text
    assert "{{companyName}}" in r.text


# ---------------------------------------------------------------------------
# GET /openapi.json
# ---------------------------------------------------------------------------


def test_openapi_json_returns_200_unauthenticated() -> None:
    """The /openapi.json route must be public."""
    with TestClient(app) as c:
        r = c.get("/openapi.json")
    assert r.status_code == 200, r.text


def test_openapi_json_content_type_is_json() -> None:
    """The /openapi.json response must declare JSON content type."""
    with TestClient(app) as c:
        r = c.get("/openapi.json")
    ctype = r.headers.get("content-type", "")
    assert "application/json" in ctype, f"unexpected content-type: {ctype!r}"


def test_openapi_json_has_required_top_level_keys() -> None:
    """The OpenAPI spec must have info, paths, and the custom x- extensions."""
    with TestClient(app) as c:
        r = c.get("/openapi.json")
    spec = r.json()

    # Standard OpenAPI keys
    assert "openapi" in spec
    assert spec["openapi"].startswith("3.1")
    assert "info" in spec
    assert "paths" in spec
    assert "components" in spec

    # Custom AI-agent extensions
    assert "x-agent-guidance" in spec
    assert "x-drift-revision" in spec
    assert "x-error-codes" in spec


def test_openapi_json_info_block_is_correct() -> None:
    """Verify info.title and info.version are wired up."""
    with TestClient(app) as c:
        r = c.get("/openapi.json")
    info = r.json()["info"]
    assert info["title"] == "Prospeqt Spintax API"
    assert info["version"] == "0.3.0"


def test_openapi_json_documents_all_eight_api_paths() -> None:
    """Every documented /api/* path must appear in the spec."""
    with TestClient(app) as c:
        r = c.get("/openapi.json")
    paths = r.json()["paths"]
    expected = {
        "/api/spintax",
        "/api/status/{job_id}",
        "/api/spintax/batch",
        "/api/spintax/batch/{batch_id}",
        "/api/spintax/batch/{batch_id}/cancel",
        "/api/spintax/batch/{batch_id}/download",
        "/api/lint",
        "/api/qa",
    }
    missing = expected - set(paths.keys())
    assert not missing, f"missing paths in openapi.json: {missing}"


def test_openapi_json_does_not_document_admin_paths() -> None:
    """Per design decision, /admin/* must NOT appear in the public spec."""
    with TestClient(app) as c:
        r = c.get("/openapi.json")
    paths = r.json()["paths"]
    for path in paths.keys():
        assert not path.startswith("/admin"), (
            f"admin path leaked into public spec: {path}"
        )


def test_openapi_json_x_error_codes_covers_all_nine_keys() -> None:
    """All 9 documented error keys must be present in x-error-codes."""
    with TestClient(app) as c:
        r = c.get("/openapi.json")
    codes = r.json()["x-error-codes"]
    expected = {
        "openai_timeout",
        "openai_quota",
        "max_tool_calls",
        "malformed_response",
        "auth_failed",
        "low_balance",
        "bad_request",
        "model_not_found",
        "internal_error",
    }
    missing = expected - set(codes.keys())
    assert not missing, f"missing error codes: {missing}"


def test_openapi_json_security_uses_bearer_auth() -> None:
    """The spec must declare bearer auth as the global security scheme."""
    with TestClient(app) as c:
        r = c.get("/openapi.json")
    spec = r.json()
    assert spec.get("security") == [{"bearerAuth": []}]
    schemes = spec["components"]["securitySchemes"]
    assert "bearerAuth" in schemes
    assert schemes["bearerAuth"]["type"] == "http"
    assert schemes["bearerAuth"]["scheme"] == "bearer"
