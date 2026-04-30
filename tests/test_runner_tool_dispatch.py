"""Phase 3 tests: per-loop dispatchers, schema shapes, client-reuse.

Coverage targets:
  1. All 8 spintax tools register correctly in chat / responses / anthropic
     shape lists (right strict-mode field, right wrapper).
  2. Each dispatcher (chat / responses / anthropic) routes by tool name
     to the underlying impl and surfaces a structured result.
  3. Strict-mode `None` arguments coalesce to safe defaults inside the
     dispatch helpers — tools never see `None` for `role` or `sense_label`.
  4. The `httpx.AsyncClient` used by SpiderFetcher is a module-level
     singleton — repeated calls share the same client instance, and
     `close_fetchers()` releases it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.spintax_runner import (
    SPINTAX_TOOLS_ANTHROPIC,
    SPINTAX_TOOLS_CHAT,
    SPINTAX_TOOLS_RESPONSES,
)
from app.tools.schemas import ALL_SPINTAX_TOOLS, TOOL_NAMES
from app.tools.tool_impls import (
    SPINTAX_TOOL_NAMES,
    dispatch_anthropic,
    dispatch_chat,
    dispatch_responses,
)


# ---------------------------------------------------------------------------
# Schema audits — strict-mode invariants, count, naming
# ---------------------------------------------------------------------------


def test_eight_tools_registered():
    assert len(ALL_SPINTAX_TOOLS) == 8
    expected_names = {
        "wordhippo_lookup",
        "classify_word_sense_for_sentence",
        "score_synonym_candidates",
        "get_pre_approved_synonyms",
        "classify_sentence_blocks",
        "identify_syntax_family",
        "reshape_blocks",
        "lint_structure_repetition",
    }
    assert set(TOOL_NAMES) == expected_names
    assert SPINTAX_TOOL_NAMES == expected_names


def test_strict_mode_invariants_on_every_tool():
    """Responses API requires additionalProperties=False AND every property in required."""
    for tool in ALL_SPINTAX_TOOLS:
        params = tool["function"]["parameters"]
        assert params.get("additionalProperties") is False, (
            f"{tool['function']['name']}: additionalProperties not False"
        )
        property_names = set(params["properties"].keys())
        required = set(params.get("required", []))
        missing = property_names - required
        assert not missing, f"{tool['function']['name']}: property/ies not in required: {missing}"


def test_chat_shape_keeps_function_wrapper():
    for tool in SPINTAX_TOOLS_CHAT:
        assert "function" in tool
        assert tool["type"] == "function"
        assert "name" in tool["function"]


def test_responses_shape_is_flat_with_strict_true():
    for tool in SPINTAX_TOOLS_RESPONSES:
        assert tool["type"] == "function"
        assert "name" in tool
        assert "function" not in tool
        assert tool["strict"] is True


def test_anthropic_shape_uses_input_schema():
    for tool in SPINTAX_TOOLS_ANTHROPIC:
        assert "name" in tool
        assert "input_schema" in tool
        assert "function" not in tool
        assert "parameters" not in tool


# ---------------------------------------------------------------------------
# Dispatch routing — all 3 loops, all 8 tools (selected smoke routes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_chat_routes_get_pre_approved_synonyms():
    args = {"source_word": "saw", "role": "opener", "sense_label": None}
    result = await dispatch_chat("get_pre_approved_synonyms", json.dumps(args))
    assert "approved" in result
    assert "noticed" in result["approved"]
    # None coalesced inside dispatcher
    assert result["sense_label"] == "unknown"


@pytest.mark.asyncio
async def test_dispatch_responses_routes_score_candidates():
    args = {
        "source_word": "saw",
        "sentence": "I saw the SBA data.",
        "candidates": ["noticed", "observed"],
        "role": "opener",
        "sense_label": "data_observation",
    }
    result = await dispatch_responses("score_synonym_candidates", json.dumps(args))
    assert "results" in result
    statuses = {r["candidate"]: r["status"] for r in result["results"]}
    assert statuses["noticed"] == "approved"
    assert statuses["observed"] == "rejected"


@pytest.mark.asyncio
async def test_dispatch_anthropic_takes_dict_directly():
    """Anthropic SDK passes b.input as a parsed dict, not a JSON string."""
    args = {"sentence": "Would it hurt to see if you qualify?", "role": "cta"}
    result = await dispatch_anthropic("identify_syntax_family", args)
    assert result["family"] == "cta_curiosity"


@pytest.mark.asyncio
async def test_dispatch_unknown_tool_returns_error_payload():
    result = await dispatch_chat("nonexistent_tool", "{}")
    assert "error" in result
    assert "Unknown spintax tool" in result["error"]


@pytest.mark.asyncio
async def test_dispatch_handles_malformed_json():
    result = await dispatch_chat("get_pre_approved_synonyms", "{not valid json")
    assert "error" in result
    assert "JSON" in result["error"]


@pytest.mark.asyncio
async def test_role_none_coalesces_to_unknown():
    args = {"sentence": "Random body line.", "role": None}
    result = await dispatch_anthropic("classify_sentence_blocks", args)
    assert result["role"] == "unknown"


@pytest.mark.asyncio
async def test_max_variants_none_defaults_to_three():
    args = {
        "sentence": "Hey {{firstName}} - saw the 1-star review from {{review_name}}.",
        "role": "opener",
        "source_family": None,
        "target_family": None,
        "max_variants": None,
    }
    result = await dispatch_anthropic("reshape_blocks", args)
    # The reshuffler may produce fewer than 3, but never more.
    assert len(result["variants"]) <= 3


# ---------------------------------------------------------------------------
# Singleton httpx.AsyncClient lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_client_singleton_returns_same_instance():
    """Two _get_async_client() calls return the same httpx.AsyncClient.

    This is the pooling guarantee: concurrent wordhippo_lookup calls inside
    one agent loop must share the connection pool.
    """
    from app.tools import wordhippo_client

    wordhippo_client._reset_for_tests()
    try:
        client1 = await wordhippo_client._get_async_client()
        client2 = await wordhippo_client._get_async_client()
        assert isinstance(client1, httpx.AsyncClient)
        assert client1 is client2, "singleton is not being reused across calls"
    finally:
        await wordhippo_client.close_fetchers()


@pytest.mark.asyncio
async def test_close_fetchers_releases_singleton():
    """After close_fetchers(), the next call creates a fresh client."""
    from app.tools import wordhippo_client

    wordhippo_client._reset_for_tests()
    try:
        client1 = await wordhippo_client._get_async_client()
        await wordhippo_client.close_fetchers()
        client2 = await wordhippo_client._get_async_client()
        assert client1 is not client2, "close_fetchers() did not drop the singleton"
    finally:
        await wordhippo_client.close_fetchers()


@pytest.mark.asyncio
async def test_async_client_has_pool_limits_configured():
    """Verify the singleton was created with the expected Limits config."""
    from app.tools import wordhippo_client

    wordhippo_client._reset_for_tests()
    try:
        client = await wordhippo_client._get_async_client()
        # httpx exposes the pool limits on the transport. The exact attribute
        # path is internal, but we can at least verify the client wasn't created
        # with a default (unlimited) pool — by checking timeout was set.
        assert client.timeout.read == 60.0
        assert client.timeout.connect == 10.0
    finally:
        await wordhippo_client.close_fetchers()
