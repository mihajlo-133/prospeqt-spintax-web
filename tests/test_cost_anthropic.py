"""Cost helper tests for Anthropic Messages API usage.

Verifies `_compute_cost` correctly bills Anthropic responses including:
- Plain (no cache) input + output.
- cache_creation_input_tokens (write).
- cache_read_input_tokens (read).
- Both cache fields together.

v1 multiplier policy: ALL input buckets bill at base input price (1.0x).
The real Anthropic multipliers (1.25x write, 0.10x read) are flagged for
a future spike against the live billing dashboard.

Also asserts that Anthropic dispatch does NOT mutate OpenAI cost behavior
- existing test_cost_helper.py covers that side, but the assertion in this
file pins the Anthropic-specific output keys (`cache_*_tokens`) so future
field-name drift surfaces immediately.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import MODEL_PRICES
from app.spintax_runner import _compute_cost


def _make_anthropic_usage(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> MagicMock:
    """Build a MagicMock that quacks like an Anthropic response.usage.

    Anthropic field names match OpenAI Responses-API (`input_tokens`,
    `output_tokens`) but add `cache_creation_input_tokens` and
    `cache_read_input_tokens` for prompt-caching billing.
    """
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = cache_creation
    usage.cache_read_input_tokens = cache_read
    # Explicitly mark Chat-shape and OpenAI Responses-detail fields as None
    # so the auto-MagicMock attribute machinery doesn't return a Mock object
    # that fools _is_int.
    usage.prompt_tokens = None
    usage.completion_tokens = None
    usage.output_tokens_details = None
    usage.completion_tokens_details = None
    return usage


# ---------------------------------------------------------------------------
# Plain (no cache) — basic input + output billing
# ---------------------------------------------------------------------------


def test_plain_no_cache_opus() -> None:
    """100 input + 50 output on opus-4-7 = $5/MTok input + $25/MTok output."""
    usage = _make_anthropic_usage(input_tokens=100, output_tokens=50)
    result = _compute_cost(usage, "claude-opus-4-7")

    expected = (100 / 1_000_000) * 5.00 + (50 / 1_000_000) * 25.00
    assert result["total_cost_usd"] == pytest.approx(expected)
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 50
    assert result["reasoning_tokens"] == 0  # thinking tokens fold into output_tokens
    assert result["cache_creation_tokens"] == 0
    assert result["cache_read_tokens"] == 0


def test_plain_no_cache_sonnet() -> None:
    """1000 input + 200 output on sonnet-4-6 = $3/MTok input + $15/MTok output."""
    usage = _make_anthropic_usage(input_tokens=1000, output_tokens=200)
    result = _compute_cost(usage, "claude-sonnet-4-6")

    expected = (1000 / 1_000_000) * 3.00 + (200 / 1_000_000) * 15.00
    assert result["total_cost_usd"] == pytest.approx(expected)


def test_real_billing_match_spike_opus() -> None:
    """Spike data: 1927 input + 132 output on opus-4-7 = $0.0129 (matches API).

    Real call from spike script returned this exact total. The check below
    pins the math against the published $5/$25 per MTok rates so price drift
    will surface as a test failure.
    """
    usage = _make_anthropic_usage(input_tokens=1927, output_tokens=132)
    result = _compute_cost(usage, "claude-opus-4-7")
    # 1927 * 5e-6 + 132 * 25e-6 = 0.009635 + 0.0033 = 0.012935
    assert result["total_cost_usd"] == pytest.approx(0.012935, rel=1e-4)


# ---------------------------------------------------------------------------
# Cache creation (write) only
# ---------------------------------------------------------------------------


def test_cache_creation_only_billed_at_base_input_price() -> None:
    """cache_creation_input_tokens=500 bills at 1.0x base input price (v1)."""
    usage = _make_anthropic_usage(
        input_tokens=100, output_tokens=50, cache_creation=500
    )
    result = _compute_cost(usage, "claude-opus-4-7")

    # v1: cache write multiplier is 1.0x (base) - TODO 1.25x post-spike
    expected = (
        (100 / 1_000_000) * 5.00
        + (500 / 1_000_000) * 5.00
        + (50 / 1_000_000) * 25.00
    )
    assert result["total_cost_usd"] == pytest.approx(expected)
    assert result["cache_creation_tokens"] == 500
    assert result["cache_read_tokens"] == 0
    # input_tokens stays separate from cache_creation - reviewer's correction.
    assert result["input_tokens"] == 100


# ---------------------------------------------------------------------------
# Cache read only
# ---------------------------------------------------------------------------


def test_cache_read_only_billed_at_base_input_price() -> None:
    """cache_read_input_tokens=2000 bills at 1.0x base input price (v1)."""
    usage = _make_anthropic_usage(
        input_tokens=100, output_tokens=50, cache_read=2000
    )
    result = _compute_cost(usage, "claude-opus-4-7")

    # v1: cache read multiplier is 1.0x (base) - TODO 0.10x post-spike
    expected = (
        (100 / 1_000_000) * 5.00
        + (2000 / 1_000_000) * 5.00
        + (50 / 1_000_000) * 25.00
    )
    assert result["total_cost_usd"] == pytest.approx(expected)
    assert result["cache_creation_tokens"] == 0
    assert result["cache_read_tokens"] == 2000


# ---------------------------------------------------------------------------
# Both caches together
# ---------------------------------------------------------------------------


def test_both_caches_summed_correctly() -> None:
    """Both cache_creation and cache_read present + base input + output."""
    usage = _make_anthropic_usage(
        input_tokens=200,
        output_tokens=100,
        cache_creation=300,
        cache_read=1500,
    )
    result = _compute_cost(usage, "claude-sonnet-4-6")

    # All input buckets at $3/MTok (sonnet input price), output at $15/MTok.
    expected = (
        (200 / 1_000_000) * 3.00
        + (300 / 1_000_000) * 3.00
        + (1500 / 1_000_000) * 3.00
        + (100 / 1_000_000) * 15.00
    )
    assert result["total_cost_usd"] == pytest.approx(expected)
    assert result["cache_creation_tokens"] == 300
    assert result["cache_read_tokens"] == 1500


# ---------------------------------------------------------------------------
# Defensive: missing cache fields default to 0 (no AttributeError)
# ---------------------------------------------------------------------------


def test_missing_cache_fields_default_to_zero() -> None:
    """An Anthropic usage object without cache_*_input_tokens still bills correctly."""
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50
    # Cache fields are explicitly None - emulates an SDK build that doesn't
    # populate them, or a hand-built test mock that omits the attributes.
    usage.cache_creation_input_tokens = None
    usage.cache_read_input_tokens = None
    usage.prompt_tokens = None
    usage.completion_tokens = None
    usage.output_tokens_details = None
    usage.completion_tokens_details = None

    result = _compute_cost(usage, "claude-opus-4-7")

    expected = (100 / 1_000_000) * 5.00 + (50 / 1_000_000) * 25.00
    assert result["total_cost_usd"] == pytest.approx(expected)
    assert result["cache_creation_tokens"] == 0
    assert result["cache_read_tokens"] == 0


# ---------------------------------------------------------------------------
# Anthropic models always include the new cache_*_tokens keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model", sorted({"claude-opus-4-7", "claude-sonnet-4-6"}))
def test_anthropic_result_dict_always_has_cache_keys(model: str) -> None:
    """The cache_*_tokens output keys exist on every Anthropic call.

    Downstream consumers can rely on `.get('cache_creation_tokens', 0)` /
    direct `result['cache_creation_tokens']` working without KeyError.
    """
    usage = _make_anthropic_usage(input_tokens=1, output_tokens=1)
    result = _compute_cost(usage, model)
    assert "cache_creation_tokens" in result
    assert "cache_read_tokens" in result
    assert "input_tokens" in result
    assert "output_tokens" in result
    assert "reasoning_tokens" in result
    assert "total_cost_usd" in result


# ---------------------------------------------------------------------------
# Sanity: prices come from MODEL_PRICES (not a hardcoded local literal)
# ---------------------------------------------------------------------------


def test_prices_sourced_from_model_prices() -> None:
    """Whatever MODEL_PRICES says is what the cost helper bills."""
    usage = _make_anthropic_usage(input_tokens=1_000_000, output_tokens=1_000_000)
    result = _compute_cost(usage, "claude-opus-4-7")

    p = MODEL_PRICES["claude-opus-4-7"]
    # 1M input + 1M output = exactly $input + $output rate.
    assert result["total_cost_usd"] == pytest.approx(p["input"] + p["output"])
