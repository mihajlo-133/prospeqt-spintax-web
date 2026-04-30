"""Unit tests for app/spend.py — daily USD spend cap enforcement.

Phase 2 target:
    Written BEFORE implementation (test-first).

Contract (from locked settings + session plan):
    - Daily cap: $50 USD (configurable via DAILY_SPEND_CAP_USD env var)
    - Resets at midnight UTC
    - API call that would exceed the cap returns 429 with exact envelope:
        { "error": "daily_cap_hit", "cap_usd": 50, "spent_usd": <n>, "resets_at": "<iso>" }
    - Increment is called AFTER a successful generation (not before)
    - Module must expose: check_cap() and add_cost(amount_usd) functions
    - check_cap() returns None if under cap, raises HTTPException(429) if at/over cap

Isolation strategy:
    - All tests reset the spend module's internal counter before running
    - Use monkeypatch or direct module-level attribute patching to control date
    - No real OpenAI calls
    - No real HTTP calls
"""

import importlib
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_spend():
    """Reset the spend module to a clean daily state."""
    import app.spend as spend

    importlib.reload(spend)
    return spend


def _get_spend():
    try:
        import app.spend as spend

        return spend
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# A. Under cap — requests allowed
# ---------------------------------------------------------------------------


class TestUnderCap:
    def test_under_cap_allows_request(self):
        """When spent_usd=$5 and cap=$50, check_cap() must not raise."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet (Phase 2 builder must create it)")

        # Set up: $5 spent
        spend._reset_for_test(5.0)

        # Must not raise
        try:
            spend.check_cap()
        except HTTPException:
            pytest.fail("check_cap() raised 429 when under cap ($5 < $50)")

    def test_add_cost_increments_counter(self):
        """add_cost(amount) must increase the spend counter by exactly amount."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(0.0)
        spend.add_cost(3.50)
        assert abs(spend.get_spent_today() - 3.50) < 1e-9, (
            f"Expected spent_usd=3.50, got {spend.get_spent_today()}"
        )

    def test_add_cost_accumulates(self):
        """Multiple add_cost() calls must accumulate."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(0.0)
        spend.add_cost(5.0)
        spend.add_cost(3.0)
        spend.add_cost(2.0)
        assert abs(spend.get_spent_today() - 10.0) < 1e-9


# ---------------------------------------------------------------------------
# B. At cap — requests blocked with 429
# ---------------------------------------------------------------------------


class TestAtCap:
    def test_at_cap_raises_http_429(self):
        """When spent_usd=$50 (= cap), check_cap() must raise HTTPException(429)."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(50.0)  # exactly at cap
        with pytest.raises(HTTPException) as exc_info:
            spend.check_cap()
        assert exc_info.value.status_code == 429

    def test_over_cap_raises_http_429(self):
        """When spent_usd=$25 (> cap), check_cap() must raise HTTPException(429)."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(55.0)
        with pytest.raises(HTTPException) as exc_info:
            spend.check_cap()
        assert exc_info.value.status_code == 429

    def test_429_response_has_correct_shape(self):
        """The 429 exception detail must match the exact envelope shape."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(50.0)
        with pytest.raises(HTTPException) as exc_info:
            spend.check_cap()

        detail = exc_info.value.detail
        assert isinstance(detail, dict), f"429 detail must be a dict, got {type(detail)}: {detail}"
        required_keys = {"error", "cap_usd", "spent_usd", "resets_at"}
        missing = required_keys - set(detail.keys())
        assert not missing, f"429 detail missing keys: {missing}. Got keys: {set(detail.keys())}"

    def test_429_error_field_value(self):
        """The 'error' key in 429 detail must be 'daily_cap_hit'."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(50.0)
        with pytest.raises(HTTPException) as exc_info:
            spend.check_cap()
        detail = exc_info.value.detail
        assert detail["error"] == "daily_cap_hit", (
            f"Expected error='daily_cap_hit', got {detail['error']!r}"
        )

    def test_429_cap_usd_field_matches_config(self):
        """The 'cap_usd' key in 429 detail must match the configured cap."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(50.0)
        with pytest.raises(HTTPException) as exc_info:
            spend.check_cap()
        detail = exc_info.value.detail
        assert detail["cap_usd"] == 50.0, f"Expected cap_usd=50.0, got {detail['cap_usd']}"

    def test_429_spent_usd_reflects_current_spend(self):
        """The 'spent_usd' key must reflect the actual spent amount."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(52.5)
        with pytest.raises(HTTPException) as exc_info:
            spend.check_cap()
        detail = exc_info.value.detail
        assert abs(detail["spent_usd"] - 52.5) < 1e-9, (
            f"Expected spent_usd=52.5, got {detail['spent_usd']}"
        )

    def test_429_resets_at_is_iso8601_string(self):
        """The 'resets_at' key must be a valid ISO 8601 UTC string."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(50.0)
        with pytest.raises(HTTPException) as exc_info:
            spend.check_cap()
        detail = exc_info.value.detail
        resets_at = detail["resets_at"]
        # Must parse as a datetime
        parsed = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        assert parsed.tzinfo is not None, "resets_at must be timezone-aware"

    def test_429_resets_at_is_next_midnight_utc(self):
        """The 'resets_at' must point to the next midnight UTC."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(50.0)
        with pytest.raises(HTTPException) as exc_info:
            spend.check_cap()
        detail = exc_info.value.detail
        resets_at = datetime.fromisoformat(detail["resets_at"].replace("Z", "+00:00"))
        # Must be at midnight (hour=0, minute=0, second=0)
        assert resets_at.hour == 0, f"resets_at must be at midnight UTC, got hour={resets_at.hour}"
        assert resets_at.minute == 0
        assert resets_at.second == 0
        # Must be in the future
        assert resets_at > datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# C. Midnight UTC reset
# ---------------------------------------------------------------------------


class TestMidnightReset:
    def test_resets_at_midnight_utc(self):
        """If the stored counter date is yesterday, add_cost() or check_cap() resets to 0."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        # Simulate: yesterday's date stored with a high spend amount
        spend._reset_for_test(49.99, date_override="yesterday")
        # Now call check_cap() — the date has changed, so counter resets to 0
        # This should NOT raise (counter resets to 0, which is under cap)
        try:
            spend.check_cap()
        except HTTPException:
            pytest.fail(
                "check_cap() raised 429 after midnight reset — "
                "counter should have been reset to 0 before checking cap"
            )
        # After reset, spent should be 0
        assert spend.get_spent_today() == 0.0, (
            f"After midnight reset, spent_usd must be 0.0, got {spend.get_spent_today()}"
        )

    def test_same_day_does_not_reset(self):
        """If the stored date is today, the counter must NOT reset."""
        spend = _get_spend()
        if spend is None:
            pytest.fail("app.spend module does not exist yet")

        spend._reset_for_test(5.0, date_override="today")
        # check_cap() must keep the $5 amount
        try:
            spend.check_cap()
        except HTTPException:
            pass  # Fine if we're over cap, but it means $5 stayed (not reset to 0)
        # The spend should remain at $5, not 0
        spent = spend.get_spent_today()
        assert abs(spent - 5.0) < 1e-9, (
            f"Same-day counter must not be reset, expected 5.0, got {spent}"
        )


# ---------------------------------------------------------------------------
# D. Route-level 429 shape (via HTTP route, not just module)
# ---------------------------------------------------------------------------


class TestRouteLevel429:
    """Verify the 429 shape comes through correctly at the HTTP route level
    (as opposed to just the module level checked above).
    """

    def test_post_spintax_returns_429_when_at_cap(self, authed_client_factory):
        """POST /api/spintax when cap is hit must return 429 with exact envelope."""
        # This test uses a fixture that provides an authed client with the spend
        # module pre-set to cap. Builder must wire spend.check_cap() into the route.
        client = authed_client_factory(spent_usd=50.0)
        r = client.post(
            "/api/spintax",
            json={"text": "Hello world.", "platform": "instantly"},
        )
        assert r.status_code == 429, (
            f"Expected 429 when at cap, got {r.status_code}. Body: {r.text}"
        )
        body = r.json()
        required_keys = {"error", "cap_usd", "spent_usd", "resets_at"}
        missing = required_keys - set(body.keys())
        assert not missing, f"429 body missing keys: {missing}. Got: {body}"
        assert body["error"] == "daily_cap_hit"


@pytest.fixture()
def authed_client_factory():
    """Factory fixture: returns a TestClient that is pre-authed
    and has the spend module set to a specific spent_usd value.
    """
    import os

    os.environ.setdefault("ADMIN_PASSWORD", "test-password-sentinel")

    def _make(spent_usd: float):
        spend = _get_spend()
        if spend is not None:
            spend._reset_for_test(spent_usd)

        from app.main import app as fastapi_app

        with TestClient(fastapi_app, raise_server_exceptions=False) as c:
            # Login
            c.post("/admin/login", json={"password": "test-password-sentinel"})
            return c

    from fastapi.testclient import TestClient

    return _make
