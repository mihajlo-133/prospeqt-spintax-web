"""Integration tests for POST /api/qa.

Contract (from ARCHITECTURE.md + api_models.py):
    POST /api/qa
        Request: { output_text: str, input_text: str, platform: str }
        Response: { passed: bool, error_count: int, warning_count: int,
                    errors: list[str], warnings: list[str],
                    block_count: int, input_paragraph_count: int }
        HTTP 422 on invalid platform, missing required fields, empty text fields
        HTTP 200 on all well-formed requests (QA errors surfaced in body)

Uses the shared session-scoped TestClient from conftest.py.
All tests are offline and deterministic. No real API calls.
"""


# ---------------------------------------------------------------------------
# Shared test fixtures (module-level constants)
# ---------------------------------------------------------------------------

# A valid Instantly output block with 5 balanced variations.
_OUTPUT_INSTANTLY = (
    "{{RANDOM | Just one prose paragraph here. | "
    "Just one clear paragraph here. | "
    "Just one brief paragraph here.. | "
    "Just one solid paragraph here. | "
    "Just one simple paragraph here. }}"
)

# The original input that the output above was spun from (V1 must match input).
_INPUT_INSTANTLY = "Just one prose paragraph here."

# Valid EmailBison output and matching input.
_OUTPUT_EMAILBISON = "{Hello friend!|Hello buddy!|Hello there!|Hello mate!!|Hello pal!}"
_INPUT_EMAILBISON = "Hello friend!"


# ---------------------------------------------------------------------------
# Happy path - platform variants
# ---------------------------------------------------------------------------


def test_qa_instantly_valid_returns_200(authed_client):
    """POST /api/qa with valid Instantly output returns HTTP 200."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}. Body: {r.text}"


def test_qa_instantly_valid_passed_true(authed_client):
    """Valid Instantly output yields passed=True with empty errors list."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    body = r.json()
    assert body["passed"] is True, f"Expected passed=True, got {body}"
    assert body["errors"] == [], f"Expected no errors, got {body['errors']}"


def test_qa_emailbison_valid_passed_true(authed_client):
    """Valid EmailBison output yields passed=True."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_EMAILBISON,
            "input_text": _INPUT_EMAILBISON,
            "platform": "emailbison",
        },
    )
    body = r.json()
    assert body["passed"] is True, f"Expected passed=True for emailbison, got {body}"


# ---------------------------------------------------------------------------
# Response shape - exact key set, types
# ---------------------------------------------------------------------------


def test_qa_response_has_all_required_keys(authed_client):
    """POST /api/qa response must contain exactly the keys defined in QAResponse."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    body = r.json()
    expected_keys = {
        "passed",
        "error_count",
        "warning_count",
        "errors",
        "warnings",
        "block_count",
        "input_paragraph_count",
        # Phase A diversity gate (added 2026-05-04)
        "diversity_block_scores",
        "diversity_corpus_avg",
        "diversity_floor_block_avg",
        "diversity_floor_pair",
        "diversity_gate_level",
    }
    assert set(body.keys()) == expected_keys, (
        f"Response keys mismatch. Expected {expected_keys}, got {set(body.keys())}"
    )


def test_qa_response_type_passed_is_bool(authed_client):
    """passed field must be a bool."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    body = r.json()
    assert isinstance(body["passed"], bool), f"passed must be bool, got {type(body['passed'])}"


def test_qa_response_type_errors_is_list(authed_client):
    """errors field must be a list."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    body = r.json()
    assert isinstance(body["errors"], list), f"errors must be list, got {type(body['errors'])}"


def test_qa_response_type_warnings_is_list(authed_client):
    """warnings field must be a list."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    body = r.json()
    assert isinstance(body["warnings"], list), (
        f"warnings must be list, got {type(body['warnings'])}"
    )


def test_qa_response_type_counts_are_ints(authed_client):
    """error_count, warning_count, block_count, input_paragraph_count must all be ints."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    body = r.json()
    assert isinstance(body["error_count"], int), "error_count must be int"
    assert isinstance(body["warning_count"], int), "warning_count must be int"
    assert isinstance(body["block_count"], int), "block_count must be int"
    assert isinstance(body["input_paragraph_count"], int), "input_paragraph_count must be int"


def test_qa_response_content_type_is_json(authed_client):
    """POST /api/qa must return Content-Type: application/json."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    ct = r.headers.get("content-type", "")
    assert ct.startswith("application/json"), (
        f"Expected content-type starting with 'application/json', got '{ct}'"
    )


def test_qa_block_count_value(authed_client):
    """block_count in response reflects number of spintax blocks in output_text."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    body = r.json()
    assert body["block_count"] == 1, f"Expected block_count=1, got {body['block_count']}"


def test_qa_input_paragraph_count_value(authed_client):
    """input_paragraph_count reflects spintaxable paragraphs in input_text."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    body = r.json()
    assert body["input_paragraph_count"] == 1, (
        f"Expected input_paragraph_count=1, got {body['input_paragraph_count']}"
    )


def test_qa_error_count_matches_errors_list(authed_client):
    """error_count must equal len(errors)."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": "a completely different paragraph",
            "platform": "instantly",
        },
    )
    body = r.json()
    assert body["error_count"] == len(body["errors"]), (
        f"error_count {body['error_count']} != len(errors) {len(body['errors'])}"
    )


def test_qa_warning_count_matches_warnings_list(authed_client):
    """warning_count must equal len(warnings)."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    body = r.json()
    assert body["warning_count"] == len(body["warnings"]), (
        f"warning_count {body['warning_count']} != len(warnings) {len(body['warnings'])}"
    )


# ---------------------------------------------------------------------------
# Platform validation
# ---------------------------------------------------------------------------


def test_qa_invalid_platform_returns_422(authed_client):
    """POST /api/qa with an unsupported platform must return HTTP 422."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "sendgrid",
        },
    )
    assert r.status_code == 422, (
        f"Expected 422 for invalid platform, got {r.status_code}. Body: {r.text}"
    )


def test_qa_invalid_platform_422_has_detail(authed_client):
    """HTTP 422 for invalid platform should include 'detail' field in body."""
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": _OUTPUT_INSTANTLY,
            "input_text": _INPUT_INSTANTLY,
            "platform": "sendgrid",
        },
    )
    detail = r.json()
    assert "detail" in detail, f"Expected 'detail' in 422 body, got keys: {list(detail.keys())}"


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------


def test_qa_missing_output_text_returns_422(authed_client):
    """POST /api/qa without 'output_text' must return HTTP 422."""
    r = authed_client.post(
        "/api/qa",
        json={"input_text": _INPUT_INSTANTLY, "platform": "instantly"},
    )
    assert r.status_code == 422, f"Expected 422 for missing output_text, got {r.status_code}"


def test_qa_missing_input_text_returns_422(authed_client):
    """POST /api/qa without 'input_text' must return HTTP 422."""
    r = authed_client.post(
        "/api/qa",
        json={"output_text": _OUTPUT_INSTANTLY, "platform": "instantly"},
    )
    assert r.status_code == 422, f"Expected 422 for missing input_text, got {r.status_code}"


def test_qa_missing_platform_returns_422(authed_client):
    """POST /api/qa without 'platform' must return HTTP 422."""
    r = authed_client.post(
        "/api/qa",
        json={"output_text": _OUTPUT_INSTANTLY, "input_text": _INPUT_INSTANTLY},
    )
    assert r.status_code == 422, f"Expected 422 for missing platform, got {r.status_code}"


# ---------------------------------------------------------------------------
# Empty text field handling
# ---------------------------------------------------------------------------


def test_qa_empty_output_text_returns_422(authed_client):
    """POST /api/qa with whitespace-only output_text must return HTTP 422."""
    r = authed_client.post(
        "/api/qa",
        json={"output_text": "   ", "input_text": _INPUT_INSTANTLY, "platform": "instantly"},
    )
    assert r.status_code == 422, (
        f"Expected 422 for empty output_text, got {r.status_code}. Body: {r.text}"
    )


def test_qa_empty_input_text_returns_422(authed_client):
    """POST /api/qa with whitespace-only input_text must return HTTP 422."""
    r = authed_client.post(
        "/api/qa",
        json={"output_text": _OUTPUT_INSTANTLY, "input_text": "  ", "platform": "instantly"},
    )
    assert r.status_code == 422, (
        f"Expected 422 for empty input_text, got {r.status_code}. Body: {r.text}"
    )


# ---------------------------------------------------------------------------
# Non-JSON body
# ---------------------------------------------------------------------------


def test_qa_non_json_body_returns_422(authed_client):
    """POST /api/qa with a non-JSON body must return HTTP 422."""
    r = authed_client.post(
        "/api/qa",
        content=b"this is not json",
        headers={"Content-Type": "text/plain"},
    )
    assert r.status_code == 422, (
        f"Expected 422 for non-JSON body, got {r.status_code}. Body: {r.text}"
    )


# ---------------------------------------------------------------------------
# QA logic - errors surfaced in response body (HTTP 200)
# ---------------------------------------------------------------------------


def test_qa_v1_fidelity_failure_returns_200_with_errors(authed_client):
    """When V1 variation does not match input, returns 200 with errors, passed=False."""
    output_text = (
        "{{RANDOM | A COMPLETELY DIFFERENT TEXT. | "
        "Just one clear paragraph here. | "
        "Just one brief paragraph here.. | "
        "Just one solid paragraph here. | "
        "Just one simple paragraph here. }}"
    )
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": output_text,
            "input_text": _INPUT_INSTANTLY,
            "platform": "instantly",
        },
    )
    body = r.json()
    assert r.status_code == 200
    assert body["passed"] is False, f"Expected passed=False for V1 mismatch, got {body}"
    assert body["error_count"] > 0, f"Expected errors for V1 mismatch, got {body}"


def test_qa_informal_greeting_returns_200_with_errors(authed_client):
    """Informal greeting variation produces errors in the response body, passed=False."""
    output_text = (
        "{{RANDOM | Hey {{firstName}}, | Hi {{firstName}}, | "
        "Hello {{firstName}}, | Heya {{firstName}}, | Howdy {{firstName}}, }}"
    )
    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": output_text,
            "input_text": "Hey {{firstName}},\n",
            "platform": "instantly",
        },
    )
    body = r.json()
    assert r.status_code == 200
    assert body["passed"] is False, f"Expected passed=False for informal greetings, got {body}"
    assert any("informal greeting" in e for e in body["errors"]), (
        f"Expected 'informal greeting' error, got {body['errors']}"
    )
