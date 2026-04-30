"""Integration tests for POST /api/lint.

Contract (from ARCHITECTURE.md + api_models.py):
    POST /api/lint
        Request: { text: str, platform: str, tolerance?: float, tolerance_floor?: int }
        Response: { errors: list[str], warnings: list[str], passed: bool,
                    error_count: int, warning_count: int }
        HTTP 422 on invalid platform, missing required field, empty text
        HTTP 200 on all well-formed requests (errors in body, not HTTP status)

Uses the shared session-scoped TestClient from conftest.py.
All tests are offline and deterministic. No real API calls.
"""


# ---------------------------------------------------------------------------
# Happy path - platform variants
# ---------------------------------------------------------------------------

# A minimal passing Instantly spintax block: 5 variations, roughly equal length.
_INSTANTLY_VALID = (
    "{{RANDOM | Hello there friend. | Hello there buddy. | "
    "Hello there mate. | Hello there pal. | Hello there dear. }}"
)

# A minimal passing EmailBison block: 5 variations with single-brace syntax.
_EMAILBISON_VALID = "{Hello friend!|Hello buddy!|Hello there!|Hello mate!!|Hello pal!}"


def test_lint_instantly_valid_returns_200(authed_client):
    """POST /api/lint with valid Instantly spintax returns HTTP 200."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "instantly"})
    assert r.status_code == 200, f"Expected 200, got {r.status_code}. Body: {r.text}"


def test_lint_instantly_valid_passed_true(authed_client):
    """Valid Instantly spintax yields passed=True with empty errors list."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "instantly"})
    body = r.json()
    assert body["passed"] is True, f"Expected passed=True, got {body}"
    assert body["errors"] == [], f"Expected no errors, got {body['errors']}"


def test_lint_emailbison_valid_passed_true(authed_client):
    """Valid EmailBison spintax yields passed=True with empty errors list."""
    r = authed_client.post("/api/lint", json={"text": _EMAILBISON_VALID, "platform": "emailbison"})
    body = r.json()
    assert body["passed"] is True, f"Expected passed=True for emailbison, got {body}"
    assert body["errors"] == [], f"Expected no errors for emailbison, got {body['errors']}"


# ---------------------------------------------------------------------------
# Response shape - exact key set, types
# ---------------------------------------------------------------------------


def test_lint_response_has_all_required_keys(authed_client):
    """POST /api/lint response must contain exactly the keys defined in LintResponse."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "instantly"})
    body = r.json()
    expected_keys = {"errors", "warnings", "passed", "error_count", "warning_count"}
    assert set(body.keys()) == expected_keys, (
        f"Response keys mismatch. Expected {expected_keys}, got {set(body.keys())}"
    )


def test_lint_response_type_errors_is_list(authed_client):
    """errors field must be a list[str]."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "instantly"})
    body = r.json()
    assert isinstance(body["errors"], list), f"errors must be list, got {type(body['errors'])}"


def test_lint_response_type_warnings_is_list(authed_client):
    """warnings field must be a list[str]."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "instantly"})
    body = r.json()
    assert isinstance(body["warnings"], list), (
        f"warnings must be list, got {type(body['warnings'])}"
    )


def test_lint_response_type_passed_is_bool(authed_client):
    """passed field must be a bool."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "instantly"})
    body = r.json()
    assert isinstance(body["passed"], bool), f"passed must be bool, got {type(body['passed'])}"


def test_lint_response_type_counts_are_ints(authed_client):
    """error_count and warning_count must be ints."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "instantly"})
    body = r.json()
    assert isinstance(body["error_count"], int), "error_count must be int"
    assert isinstance(body["warning_count"], int), "warning_count must be int"


def test_lint_response_content_type_is_json(authed_client):
    """POST /api/lint must return Content-Type: application/json."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "instantly"})
    ct = r.headers.get("content-type", "")
    assert ct.startswith("application/json"), (
        f"Expected content-type starting with 'application/json', got '{ct}'"
    )


def test_lint_error_count_matches_errors_list(authed_client):
    """error_count must equal len(errors)."""
    r = authed_client.post(
        "/api/lint",
        json={"text": "no spintax blocks here", "platform": "instantly"},
    )
    body = r.json()
    assert body["error_count"] == len(body["errors"]), (
        f"error_count {body['error_count']} != len(errors) {len(body['errors'])}"
    )


def test_lint_warning_count_matches_warnings_list(authed_client):
    """warning_count must equal len(warnings)."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "instantly"})
    body = r.json()
    assert body["warning_count"] == len(body["warnings"]), (
        f"warning_count {body['warning_count']} != len(warnings) {len(body['warnings'])}"
    )


# ---------------------------------------------------------------------------
# Platform validation
# ---------------------------------------------------------------------------


def test_lint_invalid_platform_returns_422(authed_client):
    """POST /api/lint with an unsupported platform must return HTTP 422."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "mailchimp"})
    assert r.status_code == 422, (
        f"Expected 422 for invalid platform, got {r.status_code}. Body: {r.text}"
    )


def test_lint_invalid_platform_422_detail_mentions_platform(authed_client):
    """HTTP 422 for invalid platform should reference the validation error."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID, "platform": "mailchimp"})
    detail = r.json()
    # FastAPI/Pydantic 422 body has a 'detail' key with error descriptions.
    assert "detail" in detail, f"Expected 'detail' in 422 body, got keys: {list(detail.keys())}"


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


def test_lint_missing_text_returns_422(authed_client):
    """POST /api/lint without 'text' field must return HTTP 422."""
    r = authed_client.post("/api/lint", json={"platform": "instantly"})
    assert r.status_code == 422, f"Expected 422 for missing text, got {r.status_code}"


def test_lint_missing_platform_returns_422(authed_client):
    """POST /api/lint without 'platform' field must return HTTP 422."""
    r = authed_client.post("/api/lint", json={"text": _INSTANTLY_VALID})
    assert r.status_code == 422, f"Expected 422 for missing platform, got {r.status_code}"


# ---------------------------------------------------------------------------
# Empty text handling
# ---------------------------------------------------------------------------


def test_lint_empty_text_returns_422(authed_client):
    """POST /api/lint with whitespace-only text must return HTTP 422.

    The LintRequest.text_must_not_be_empty validator rejects empty text at the
    Pydantic level. The route never calls lint() for empty input.
    """
    r = authed_client.post("/api/lint", json={"text": "   ", "platform": "instantly"})
    assert r.status_code == 422, f"Expected 422 for empty text, got {r.status_code}. Body: {r.text}"


def test_lint_empty_string_text_returns_422(authed_client):
    """POST /api/lint with empty string text must return HTTP 422."""
    r = authed_client.post("/api/lint", json={"text": "", "platform": "instantly"})
    assert r.status_code == 422, (
        f"Expected 422 for empty string text, got {r.status_code}. Body: {r.text}"
    )


# ---------------------------------------------------------------------------
# Non-JSON body
# ---------------------------------------------------------------------------


def test_lint_non_json_body_returns_422(authed_client):
    """POST /api/lint with a non-JSON body must return HTTP 422."""
    r = authed_client.post(
        "/api/lint",
        content=b"this is not json",
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 422, (
        f"Expected 422 for non-JSON body, got {r.status_code}. Body: {r.text}"
    )


# ---------------------------------------------------------------------------
# Lint logic - errors surfaced in response body (HTTP 200 with errors in body)
# ---------------------------------------------------------------------------


def test_lint_em_dash_returns_200_with_errors(authed_client):
    """POST /api/lint with em-dash in text returns 200, errors list non-empty, passed=False."""
    em_dash = "—"
    em_text_with_dash = (
        f"{{{{RANDOM | Hello {em_dash} there friend. | Hello there buddy. | "
        "Hello there mate. | Hello there pal. | Hello there dear. }}"
    )
    r = authed_client.post("/api/lint", json={"text": em_text_with_dash, "platform": "instantly"})
    body = r.json()
    assert r.status_code == 200
    assert body["passed"] is False, f"Expected passed=False for em-dash text, got {body}"
    assert any("em-dash" in e for e in body["errors"]), (
        f"Expected 'em-dash' in errors, got {body['errors']}"
    )


def test_lint_banned_word_returns_200_with_errors(authed_client):
    """POST /api/lint with banned word returns 200, errors list mentions the word, passed=False."""
    banned_text = (
        "{{RANDOM | Hello there friend. | Hello there buddy. | "
        "Hello utilize there. | Hello there pal. | Hello there dear. }}"
    )
    r = authed_client.post("/api/lint", json={"text": banned_text, "platform": "instantly"})
    body = r.json()
    assert r.status_code == 200
    assert body["passed"] is False, f"Expected passed=False for banned word, got {body}"
    assert any("utilize" in e for e in body["errors"]), (
        f"Expected 'utilize' in errors, got {body['errors']}"
    )


def test_lint_no_spintax_blocks_returns_errors(authed_client):
    """POST /api/lint with plain text (no spintax) returns errors, passed=False."""
    r = authed_client.post(
        "/api/lint",
        json={"text": "Just plain text with no spintax blocks.", "platform": "instantly"},
    )
    body = r.json()
    assert r.status_code == 200
    assert body["passed"] is False
    assert body["error_count"] > 0, f"Expected errors for plain text, got {body}"


# ---------------------------------------------------------------------------
# Tolerance and tolerance_floor defaults
# ---------------------------------------------------------------------------


def test_lint_accepts_custom_tolerance(authed_client):
    """POST /api/lint accepts optional tolerance field (float in [0, 1])."""
    r = authed_client.post(
        "/api/lint",
        json={"text": _INSTANTLY_VALID, "platform": "instantly", "tolerance": 0.1},
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"


def test_lint_accepts_custom_tolerance_floor(authed_client):
    """POST /api/lint accepts optional tolerance_floor field (non-negative int)."""
    r = authed_client.post(
        "/api/lint",
        json={"text": _INSTANTLY_VALID, "platform": "instantly", "tolerance_floor": 5},
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"


def test_lint_tolerance_out_of_range_returns_422(authed_client):
    """POST /api/lint rejects tolerance > 1.0 with 422."""
    r = authed_client.post(
        "/api/lint",
        json={"text": _INSTANTLY_VALID, "platform": "instantly", "tolerance": 2.0},
    )
    assert r.status_code == 422, f"Expected 422 for tolerance>1, got {r.status_code}"


def test_lint_negative_tolerance_floor_returns_422(authed_client):
    """POST /api/lint rejects tolerance_floor < 0 with 422."""
    r = authed_client.post(
        "/api/lint",
        json={"text": _INSTANTLY_VALID, "platform": "instantly", "tolerance_floor": -1},
    )
    assert r.status_code == 422, f"Expected 422 for negative tolerance_floor, got {r.status_code}"
