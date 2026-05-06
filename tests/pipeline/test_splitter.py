"""Tests for app.pipeline.splitter (Stage 1 — Sentence Splitter).

Mocks ``call_llm_json`` at the splitter module level so no network calls
are made. All six required test cases are covered:

1. Happy path — multi-sentence email produces ordered blocks with sequential ids
2. Lockable=False — pure placeholder block (e.g. "{{firstName}}") is not lockable
3. Empty body — raises PipelineStageError BEFORE LLM is called
4. Malformed LLM response — raises PipelineStageError(ERR_SPLITTER)
5. Diagnostics — block_count, lockable_count, duration_ms are correct
6. Placeholder preservation — block text containing {{firstName}} is returned unchanged
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline.contracts import ERR_SPLITTER, PipelineStageError
from app.pipeline.splitter import split_email

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_TARGET = "app.pipeline.splitter.call_llm_json"

# Minimal 3-block LLM response used in most tests.
_THREE_BLOCKS_RESPONSE = {
    "blocks": [
        {"id": "block_1", "text": "Hi {{firstName}}, hope this finds you well."},
        {"id": "block_2", "text": "We help SaaS teams cut cloud costs by 20%."},
        {"id": "block_3", "text": "Open to a quick call next week?"},
    ]
}

# A response where the first block is a pure placeholder (not lockable).
_WITH_PLACEHOLDER_BLOCK_RESPONSE = {
    "blocks": [
        {"id": "block_1", "text": "{{firstName}}"},
        {"id": "block_2", "text": "We help SaaS teams cut cloud costs by 20%."},
    ]
}


# ---------------------------------------------------------------------------
# Test 1 — Happy path: ordered blocks with sequential ids
# ---------------------------------------------------------------------------


async def test_happy_path_blocks_in_order():
    """Multi-sentence email produces blocks in order with sequential ids."""
    plain_body = (
        "Hi {{firstName}}, hope this finds you well. "
        "We help SaaS teams cut cloud costs by 20%. "
        "Open to a quick call next week?"
    )

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=_THREE_BLOCKS_RESPONSE)):
        block_list, diag = await split_email(plain_body)

    blocks = block_list.blocks
    assert len(blocks) == 3
    assert [b.id for b in blocks] == ["block_1", "block_2", "block_3"]
    assert blocks[0].text == "Hi {{firstName}}, hope this finds you well."
    assert blocks[1].text == "We help SaaS teams cut cloud costs by 20%."
    assert blocks[2].text == "Open to a quick call next week?"


# ---------------------------------------------------------------------------
# Test 2 — Lockable=False for pure placeholder block
# ---------------------------------------------------------------------------


async def test_pure_placeholder_block_is_not_lockable():
    """A block whose text is only a placeholder token is not lockable."""
    plain_body = "{{firstName}}\nWe help SaaS teams cut cloud costs by 20%."

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=_WITH_PLACEHOLDER_BLOCK_RESPONSE)):
        block_list, _ = await split_email(plain_body)

    blocks = block_list.blocks
    assert blocks[0].text == "{{firstName}}"
    assert blocks[0].lockable is False
    # The second block has real content, so it must be lockable.
    assert blocks[1].lockable is True


# ---------------------------------------------------------------------------
# Test 3 — Empty body raises before calling LLM
# ---------------------------------------------------------------------------


async def test_empty_body_raises_before_llm():
    """Empty (or whitespace-only) body raises PipelineStageError without touching LLM."""
    mock_llm = AsyncMock(return_value=_THREE_BLOCKS_RESPONSE)

    with patch(_PATCH_TARGET, new=mock_llm):
        with pytest.raises(PipelineStageError) as exc_info:
            await split_email("   ")

    assert exc_info.value.error_key == ERR_SPLITTER
    mock_llm.assert_not_called()


async def test_none_body_raises_before_llm():
    """Falsy body (empty string) raises PipelineStageError without touching LLM."""
    mock_llm = AsyncMock(return_value=_THREE_BLOCKS_RESPONSE)

    with patch(_PATCH_TARGET, new=mock_llm):
        with pytest.raises(PipelineStageError) as exc_info:
            await split_email("")

    assert exc_info.value.error_key == ERR_SPLITTER
    mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4 — Malformed LLM response raises PipelineStageError(ERR_SPLITTER)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_response",
    [
        # Missing top-level 'blocks' key
        {"sentences": [{"id": "block_1", "text": "Hello."}]},
        # 'blocks' is not a list
        {"blocks": "not a list"},
        # Block missing 'id' field
        {"blocks": [{"text": "Hello."}]},
        # Block missing 'text' field
        {"blocks": [{"id": "block_1"}]},
        # 'id' is not a string
        {"blocks": [{"id": 1, "text": "Hello."}]},
        # 'text' is not a string
        {"blocks": [{"id": "block_1", "text": 42}]},
    ],
)
async def test_malformed_response_raises(bad_response):
    """Any structural problem in the LLM response raises PipelineStageError(ERR_SPLITTER)."""
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=bad_response)):
        with pytest.raises(PipelineStageError) as exc_info:
            await split_email("Hello, this is a test email.")

    assert exc_info.value.error_key == ERR_SPLITTER


# ---------------------------------------------------------------------------
# Test 5 — Diagnostics: block_count, lockable_count, duration_ms
# ---------------------------------------------------------------------------


async def test_diagnostics_counts_are_correct():
    """Diagnostics reflect the actual block and lockable counts."""
    plain_body = "Real sentence one. Real sentence two. {{placeholder}}"
    response = {
        "blocks": [
            {"id": "block_1", "text": "Real sentence one."},
            {"id": "block_2", "text": "Real sentence two."},
            # Pure placeholder — not lockable
            {"id": "block_3", "text": "{{placeholder}}"},
        ]
    }

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=response)):
        _, diag = await split_email(plain_body)

    assert diag.block_count == 3
    assert diag.lockable_count == 2
    assert diag.duration_ms >= 0


async def test_diagnostics_duration_ms_is_non_negative():
    """duration_ms is always >= 0 (wall-clock measurement)."""
    with patch(_PATCH_TARGET, new=AsyncMock(return_value=_THREE_BLOCKS_RESPONSE)):
        _, diag = await split_email("One sentence. Two sentences. Three sentences.")

    assert diag.duration_ms >= 0


# ---------------------------------------------------------------------------
# Test 6 — Placeholder preservation: {{firstName}} returned unchanged
# ---------------------------------------------------------------------------


async def test_placeholder_text_preserved_exactly():
    """Block texts containing {{placeholder}} tokens are returned unchanged."""
    response = {
        "blocks": [
            {"id": "block_1", "text": "Hi {{firstName}},"},
            {"id": "block_2", "text": "We help {{companyName}} cut costs."},
            {"id": "block_3", "text": "Regards, {{senderName}}"},
        ]
    }

    with patch(_PATCH_TARGET, new=AsyncMock(return_value=response)):
        block_list, _ = await split_email(
            "Hi {{firstName}}, We help {{companyName}} cut costs. Regards, {{senderName}}"
        )

    assert block_list.blocks[0].text == "Hi {{firstName}},"
    assert block_list.blocks[1].text == "We help {{companyName}} cut costs."
    assert block_list.blocks[2].text == "Regards, {{senderName}}"


# ---------------------------------------------------------------------------
# Test — on_api_call callback is forwarded
# ---------------------------------------------------------------------------


async def test_on_api_call_forwarded():
    """on_api_call callback is passed through to call_llm_json."""
    captured = []

    def _cb(usage):
        captured.append(usage)

    mock_llm = AsyncMock(return_value=_THREE_BLOCKS_RESPONSE)

    with patch(_PATCH_TARGET, new=mock_llm):
        await split_email("Test sentence. Another sentence.", on_api_call=_cb)

    # Verify call_llm_json was called with on_api_call kwarg
    call_kwargs = mock_llm.call_args.kwargs
    assert call_kwargs.get("on_api_call") is _cb
