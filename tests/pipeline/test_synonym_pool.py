"""Unit tests for app.pipeline.synonym_pool.

The LLM call is mocked at the module's import path so no network is
hit. Each test exercises one rule from BETA_BLOCK_FIRST_SPEC.md §3.2
Stage 3.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline.contracts import (
    ERR_SYNONYM_POOL,
    Block,
    BlockList,
    PipelineStageError,
    Profile,
)
from app.pipeline.synonym_pool import generate_synonym_pool


def _make_block_list(*pairs: tuple[str, str, bool]) -> BlockList:
    """Build a BlockList from (id, text, lockable) tuples."""
    return BlockList(
        blocks=[Block(id=bid, text=text, lockable=lockable) for bid, text, lockable in pairs]
    )


def _make_profile(
    *,
    tone: str = "professional B2B",
    locked: list[str] | None = None,
    proper: list[str] | None = None,
) -> Profile:
    return Profile(
        tone=tone,
        audience_hint="law firms",
        locked_common_nouns=locked or [],
        proper_nouns=proper or [],
    )


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_happy_path_two_lockable_blocks(mock_llm):
    """Two lockable blocks with valid LLM output produce both pool entries."""
    mock_llm.return_value = {
        "block_1": {
            "synonyms": {"happy": ["pleased", "glad", "content"]},
            "syntax_options": ["Hi there.", "Hello there."],
        },
        "block_2": {
            "synonyms": {"help": ["assist", "support"]},
            "syntax_options": ["We help you.", "We support you."],
        },
    }
    block_list = _make_block_list(
        ("block_1", "I am happy.", True),
        ("block_2", "We help.", True),
    )
    pool, diag = await generate_synonym_pool(block_list, _make_profile())

    assert "block_1" in pool.blocks
    assert "block_2" in pool.blocks
    assert pool.blocks["block_1"].synonyms == {"happy": ["pleased", "glad", "content"]}
    assert pool.blocks["block_2"].syntax_options == ["We help you.", "We support you."]
    assert diag.blocks_covered == 2
    assert diag.total_synonyms == 5
    assert diag.duration_ms >= 0
    mock_llm.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. No-LLM short-circuit
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_all_unlockable_skips_llm(mock_llm):
    """Block list with zero lockable blocks returns empty pool, no LLM call."""
    block_list = _make_block_list(
        ("block_1", "{{firstName}}", False),
        ("block_2", "{{senderName}}", False),
    )
    pool, diag = await generate_synonym_pool(block_list, _make_profile())

    assert pool.blocks == {}
    assert diag.blocks_covered == 0
    assert diag.total_synonyms == 0
    mock_llm.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Locked-noun filter
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_locked_noun_synonyms_stripped(mock_llm):
    """LLM-returned synonyms whose KEY is a locked noun are dropped."""
    mock_llm.return_value = {
        "block_1": {
            "synonyms": {
                "clients": ["customers"],
                "happy": ["pleased", "glad"],
            },
            "syntax_options": [],
        },
    }
    block_list = _make_block_list(("block_1", "Clients are happy.", True))
    profile = _make_profile(locked=["clients"])

    pool, _ = await generate_synonym_pool(block_list, profile)

    assert "clients" not in pool.blocks["block_1"].synonyms
    assert pool.blocks["block_1"].synonyms == {"happy": ["pleased", "glad"]}


# ---------------------------------------------------------------------------
# 4. Proper-noun filter
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_proper_noun_synonyms_stripped(mock_llm):
    """LLM-returned synonyms whose KEY is a proper noun are dropped."""
    mock_llm.return_value = {
        "block_1": {
            "synonyms": {
                "Continuum": ["Endless"],
                "great": ["excellent", "wonderful"],
            },
            "syntax_options": [],
        },
    }
    block_list = _make_block_list(("block_1", "Continuum is great.", True))
    profile = _make_profile(proper=["Continuum"])

    pool, _ = await generate_synonym_pool(block_list, profile)

    assert "Continuum" not in pool.blocks["block_1"].synonyms
    assert "great" in pool.blocks["block_1"].synonyms


# ---------------------------------------------------------------------------
# 5. Function-word filter
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_function_word_synonyms_stripped(mock_llm):
    """Function words like 'the' / 'and' are stripped from synonym keys."""
    mock_llm.return_value = {
        "block_1": {
            "synonyms": {
                "the": ["a"],
                "and": ["plus"],
                "review": ["assessment", "evaluation"],
            },
            "syntax_options": [],
        },
    }
    block_list = _make_block_list(("block_1", "The review and notes.", True))

    pool, _ = await generate_synonym_pool(block_list, _make_profile())

    syns = pool.blocks["block_1"].synonyms
    assert "the" not in syns
    assert "and" not in syns
    assert "review" in syns


# ---------------------------------------------------------------------------
# 6. Length-band boundary cases
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_length_band_boundary(mock_llm):
    """Length delta <= 6 chars is kept; > 6 is dropped."""
    # 'help' is 4 chars. 10-char synonym (delta 6) kept; 11-char (delta 7) dropped.
    mock_llm.return_value = {
        "block_1": {
            "synonyms": {
                "help": [
                    "assist",      # delta 2 — keep
                    "facilitate",  # delta 6 — keep (boundary)
                    "accommodate", # delta 7 — drop
                ],
            },
            "syntax_options": [],
        },
    }
    block_list = _make_block_list(("block_1", "We help firms.", True))

    pool, _ = await generate_synonym_pool(block_list, _make_profile())

    assert pool.blocks["block_1"].synonyms["help"] == ["assist", "facilitate"]


# ---------------------------------------------------------------------------
# 7. Missing block id raises
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_missing_block_id_raises(mock_llm):
    """LLM returns only block_1 but block_2 was lockable -> raise."""
    mock_llm.return_value = {
        "block_1": {"synonyms": {}, "syntax_options": []},
    }
    block_list = _make_block_list(
        ("block_1", "First.", True),
        ("block_2", "Second.", True),
    )

    with pytest.raises(PipelineStageError) as exc_info:
        await generate_synonym_pool(block_list, _make_profile())

    assert exc_info.value.error_key == ERR_SYNONYM_POOL
    assert "block_2" in exc_info.value.detail


# ---------------------------------------------------------------------------
# 8. Extra block id silently dropped
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_extra_block_id_dropped(mock_llm):
    """LLM returns block_99 not in input -> dropped, no error."""
    mock_llm.return_value = {
        "block_1": {
            "synonyms": {"good": ["fine", "decent"]},
            "syntax_options": [],
        },
        "block_99": {
            "synonyms": {"junk": ["trash"]},
            "syntax_options": [],
        },
    }
    block_list = _make_block_list(("block_1", "Good day.", True))

    pool, _ = await generate_synonym_pool(block_list, _make_profile())

    assert "block_1" in pool.blocks
    assert "block_99" not in pool.blocks


# ---------------------------------------------------------------------------
# 9. Diagnostics totals
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_diagnostics_totals(mock_llm):
    """total_synonyms sums kept lists; blocks_covered counts non-empty entries."""
    mock_llm.return_value = {
        "block_1": {
            "synonyms": {"good": ["fine", "decent", "solid"]},
            "syntax_options": ["Looks fine."],
        },
        "block_2": {
            "synonyms": {"fast": ["quick", "swift"]},
            "syntax_options": [],
        },
    }
    block_list = _make_block_list(
        ("block_1", "Good thing.", True),
        ("block_2", "Fast work.", True),
    )

    _, diag = await generate_synonym_pool(block_list, _make_profile())

    assert diag.total_synonyms == 5
    assert diag.blocks_covered == 2


# ---------------------------------------------------------------------------
# 10. Empty-after-filter block is pruned
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_empty_block_pruned(mock_llm):
    """A block whose synonyms all get filtered AND has no syntax_options is pruned."""
    mock_llm.return_value = {
        "block_1": {
            # The only synonym key is a function word, which gets filtered out.
            "synonyms": {"the": ["a"]},
            "syntax_options": [],
        },
        "block_2": {
            "synonyms": {"good": ["fine", "decent"]},
            "syntax_options": [],
        },
    }
    block_list = _make_block_list(
        ("block_1", "The thing.", True),
        ("block_2", "Good day.", True),
    )

    pool, _ = await generate_synonym_pool(block_list, _make_profile())

    assert "block_1" not in pool.blocks
    assert "block_2" in pool.blocks


# ---------------------------------------------------------------------------
# 11. Mixed lockable/unlockable: only lockable blocks expected from LLM
# ---------------------------------------------------------------------------


@patch("app.pipeline.synonym_pool.call_llm_json", new_callable=AsyncMock)
async def test_mixed_lockable_unlockable(mock_llm):
    """Unlockable blocks are excluded from prompt and result; LLM only sees lockable."""
    mock_llm.return_value = {
        "block_1": {
            "synonyms": {"good": ["fine", "decent"]},
            "syntax_options": [],
        },
    }
    block_list = _make_block_list(
        ("block_1", "Good day.", True),
        ("block_2", "{{firstName}}", False),
    )

    pool, _ = await generate_synonym_pool(block_list, _make_profile())

    assert "block_1" in pool.blocks
    assert "block_2" not in pool.blocks
    # Verify the LLM only saw block_1 — the prompt is the first positional arg
    # to call_llm_json, but it's keyword-only here. Check via call_args.
    call_kwargs = mock_llm.await_args.kwargs
    assert "block_1" in call_kwargs["prompt"]
    assert "block_2" not in call_kwargs["prompt"]
