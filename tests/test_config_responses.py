"""Config tests for the Responses API model set.

Verifies invariants between RESPONSES_MODELS, MODEL_PRICES, and REASONING_MODELS:
- Every Responses-API model has a price entry.
- Every Responses-API model is also a reasoning model (they're gpt-5.x).
- Chat-only models (o3, o4-mini) are NOT in RESPONSES_MODELS.
"""

from __future__ import annotations

from app.config import MODEL_PRICES, REASONING_MODELS, RESPONSES_MODELS


def test_every_responses_model_has_a_price() -> None:
    """If we route a model to /v1/responses, we must be able to bill it."""
    for model in RESPONSES_MODELS:
        assert model in MODEL_PRICES, (
            f"{model} is in RESPONSES_MODELS but missing from MODEL_PRICES"
        )
        prices = MODEL_PRICES[model]
        assert prices["input"] > 0, f"{model} has zero input price"
        assert prices["output"] > 0, f"{model} has zero output price"


def test_every_responses_model_is_a_reasoning_model() -> None:
    """gpt-5.x models all support reasoning_effort, so they live in REASONING_MODELS."""
    for model in RESPONSES_MODELS:
        assert model in REASONING_MODELS, (
            f"{model} is in RESPONSES_MODELS but missing from REASONING_MODELS"
        )


def test_chat_only_models_not_in_responses_models() -> None:
    """o3 and o4-mini stay on /v1/chat/completions — verify they're not in the new set."""
    assert "o3" not in RESPONSES_MODELS
    assert "o4-mini" not in RESPONSES_MODELS
    assert "o3-mini" not in RESPONSES_MODELS
    assert "o3-pro" not in RESPONSES_MODELS


def test_responses_models_are_only_gpt5_family() -> None:
    """RESPONSES_MODELS should contain exactly the gpt-5.x family for now."""
    assert RESPONSES_MODELS == {"gpt-5", "gpt-5-mini", "gpt-5.5"}
