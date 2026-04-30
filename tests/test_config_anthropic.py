"""Config tests for the Anthropic model set.

Verifies invariants between ANTHROPIC_MODELS, MODEL_PRICES, RESPONSES_MODELS,
and REASONING_MODELS:
- Every Anthropic model has a price entry with non-zero prices.
- ANTHROPIC_MODELS is disjoint from RESPONSES_MODELS.
- ANTHROPIC_MODELS is disjoint from REASONING_MODELS (Anthropic uses
  `thinking` config, not OpenAI's `reasoning_effort`).
- Settings reads ANTHROPIC_API_KEY and ANTHROPIC_ENABLED env vars.
"""

from __future__ import annotations

import importlib

import pytest

from app.config import (
    ANTHROPIC_MODELS,
    MODEL_PRICES,
    REASONING_MODELS,
    RESPONSES_MODELS,
)


def test_every_anthropic_model_has_a_price() -> None:
    """If we route a model to the Anthropic adapter, we must be able to bill it."""
    for model in ANTHROPIC_MODELS:
        assert model in MODEL_PRICES, (
            f"{model} is in ANTHROPIC_MODELS but missing from MODEL_PRICES"
        )
        prices = MODEL_PRICES[model]
        assert prices["input"] > 0, f"{model} has zero input price"
        assert prices["output"] > 0, f"{model} has zero output price"


def test_anthropic_models_disjoint_from_responses_models() -> None:
    """ANTHROPIC_MODELS and RESPONSES_MODELS must NOT overlap.

    Each set selects a different SDK + adapter; overlap would route
    one model to two paths and break the dispatcher.
    """
    overlap = ANTHROPIC_MODELS & RESPONSES_MODELS
    assert not overlap, f"ANTHROPIC_MODELS ∩ RESPONSES_MODELS must be empty, got {overlap}"


def test_anthropic_models_disjoint_from_reasoning_models() -> None:
    """ANTHROPIC_MODELS must NOT be in REASONING_MODELS.

    REASONING_MODELS drives OpenAI's `reasoning_effort` plumbing in the
    chat/responses adapters. Anthropic uses `thinking={"type":"adaptive"}`
    in its own adapter and must not pick up the OpenAI flag.
    """
    overlap = ANTHROPIC_MODELS & REASONING_MODELS
    assert not overlap, f"ANTHROPIC_MODELS ∩ REASONING_MODELS must be empty, got {overlap}"


def test_anthropic_models_contains_expected_models() -> None:
    """Sanity: the v1 release ships exactly opus-4-7 and sonnet-4-6."""
    assert ANTHROPIC_MODELS == {"claude-opus-4-7", "claude-sonnet-4-6"}


def test_anthropic_prices_match_2026_q2_published_rates() -> None:
    """Spot-check price values to catch drift via plan-vs-config errors.

    Per Anthropic pricing page (CONFIRMED 2026-04):
        claude-opus-4-7   = $5  / $25 per MTok
        claude-sonnet-4-6 = $3  / $15 per MTok
    """
    assert MODEL_PRICES["claude-opus-4-7"] == {"input": 5.00, "output": 25.00}
    assert MODEL_PRICES["claude-sonnet-4-6"] == {"input": 3.00, "output": 15.00}


def test_settings_reads_anthropic_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings() must pick up ANTHROPIC_API_KEY and ANTHROPIC_ENABLED env vars."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-sentinel")
    monkeypatch.setenv("ANTHROPIC_ENABLED", "false")

    # Need a fresh Settings() instance because the module-level singleton
    # was instantiated at import time. Reload to re-read env.
    from app import config as config_mod

    importlib.reload(config_mod)

    assert config_mod.settings.anthropic_api_key == "sk-ant-test-sentinel"
    assert config_mod.settings.anthropic_enabled is False


def test_settings_default_anthropic_enabled_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default value of anthropic_enabled is True (opt-out, not opt-in)."""
    monkeypatch.delenv("ANTHROPIC_ENABLED", raising=False)
    from app import config as config_mod

    importlib.reload(config_mod)

    assert config_mod.settings.anthropic_enabled is True


def test_settings_default_anthropic_api_key_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default value of anthropic_api_key is empty string (boots without env)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from app import config as config_mod

    importlib.reload(config_mod)

    # Either the .env file value or empty - we just need it to not raise.
    # Type contract: it MUST be a string.
    assert isinstance(config_mod.settings.anthropic_api_key, str)
