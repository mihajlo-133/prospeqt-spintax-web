"""Cost helper hardening tests.

Verifies that `_compute_cost` returns identical USD for identical token counts
regardless of whether the `usage` object follows the Chat Completions shape
(o3, o4-mini) or the Responses API shape (gpt-5.x).

Pattern mirrors `tests/test_failure_modes.py` — hand-built MagicMock objects,
no respx, no real network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.spintax_runner import _compute_cost


def _make_chat_usage(
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
) -> MagicMock:
    """Hand-build a MagicMock that quacks like a Chat Completions response.usage."""
    details = MagicMock()
    details.reasoning_tokens = reasoning_tokens

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.completion_tokens_details = details
    # Explicitly mark Responses-shape fields as absent (None, not MagicMock auto-attr).
    usage.input_tokens = None
    usage.output_tokens = None
    usage.output_tokens_details = None
    return usage


def _make_responses_usage(
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int = 0,
) -> MagicMock:
    """Hand-build a MagicMock that quacks like a Responses API response.usage."""
    details = MagicMock()
    details.reasoning_tokens = reasoning_tokens

    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.output_tokens_details = details
    # Explicitly mark Chat-shape fields as absent.
    usage.prompt_tokens = None
    usage.completion_tokens = None
    usage.completion_tokens_details = None
    return usage


# ---------------------------------------------------------------------------
# Equivalence tests: same token counts -> identical USD
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_tok,output_tok,reasoning_tok,model",
    [
        (100, 50, 10, "o3"),
        (1234, 567, 42, "o4-mini"),
        (135, 81, 42, "o3"),  # spike data
    ],
)
def test_chat_and_responses_shapes_produce_identical_usd(
    input_tok: int, output_tok: int, reasoning_tok: int, model: str
) -> None:
    """Same numbers, two shapes, identical total_cost_usd."""
    chat_usage = _make_chat_usage(input_tok, output_tok, reasoning_tok)
    resp_usage = _make_responses_usage(input_tok, output_tok, reasoning_tok)

    chat_result = _compute_cost(chat_usage, model)
    resp_result = _compute_cost(resp_usage, model)

    assert chat_result["total_cost_usd"] == resp_result["total_cost_usd"]
    assert chat_result["input_tokens"] == resp_result["input_tokens"] == input_tok
    assert chat_result["output_tokens"] == resp_result["output_tokens"] == output_tok
    assert chat_result["reasoning_tokens"] == resp_result["reasoning_tokens"] == reasoning_tok


# ---------------------------------------------------------------------------
# Shape-specific reads
# ---------------------------------------------------------------------------


def test_responses_shape_reads_reasoning_from_output_tokens_details() -> None:
    """Responses-shape: reasoning_tokens lives at output_tokens_details.reasoning_tokens."""
    usage = _make_responses_usage(input_tokens=200, output_tokens=100, reasoning_tokens=25)
    result = _compute_cost(usage, "o3")
    assert result["reasoning_tokens"] == 25


def test_chat_shape_reads_reasoning_from_completion_tokens_details() -> None:
    """Chat-shape: reasoning_tokens lives at completion_tokens_details.reasoning_tokens."""
    usage = _make_chat_usage(prompt_tokens=200, completion_tokens=100, reasoning_tokens=25)
    result = _compute_cost(usage, "o3")
    assert result["reasoning_tokens"] == 25


# ---------------------------------------------------------------------------
# Defensive: unknown model -> zero cost (but tokens still reported)
# ---------------------------------------------------------------------------


def test_unknown_model_returns_zero_cost_but_records_tokens() -> None:
    usage = _make_responses_usage(input_tokens=100, output_tokens=50, reasoning_tokens=5)
    result = _compute_cost(usage, "totally-fake-model-xyz")
    assert result["total_cost_usd"] == 0.0
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 50
    assert result["reasoning_tokens"] == 5


# ---------------------------------------------------------------------------
# Edge: missing details object -> reasoning_tokens defaults to 0
# ---------------------------------------------------------------------------


def test_missing_details_object_defaults_reasoning_to_zero() -> None:
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50
    usage.output_tokens_details = None
    usage.prompt_tokens = None
    usage.completion_tokens = None
    usage.completion_tokens_details = None
    result = _compute_cost(usage, "o3")
    assert result["reasoning_tokens"] == 0
