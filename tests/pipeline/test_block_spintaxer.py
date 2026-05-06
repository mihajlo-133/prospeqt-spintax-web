"""Unit tests for ``app/pipeline/block_spintaxer.py`` (Stage 4).

The module is wrapped around ``call_llm_json`` from
``app.pipeline.llm_client``; we patch ``app.pipeline.block_spintaxer.call_llm_json``
(the import-site reference) so no real network ever happens.

Coverage:
  * ``spintax_one_block`` — bypass paths, happy path, V1 force-substitution,
    structural validation failures.
  * ``spintax_all_blocks`` — parallel happy path, numeric-suffix ordering,
    mixed lockable/unlockable, diagnostics, error propagation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline.block_spintaxer import spintax_all_blocks, spintax_one_block
from app.pipeline.contracts import (
    ERR_BLOCK_SPINTAX,
    Block,
    BlockList,
    BlockPoolEntry,
    PipelineStageError,
    Profile,
    SynonymPool,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _profile() -> Profile:
    return Profile(
        tone="professional B2B, consultative",
        audience_hint="law firms",
        locked_common_nouns=["clients"],
        proper_nouns=["Prospeqt"],
    )


def _full_pool_entry() -> BlockPoolEntry:
    return BlockPoolEntry(
        synonyms={"help": ["assist", "support"], "build": ["create", "make"]},
        syntax_options=["alt phrasing one", "alt phrasing two"],
    )


def _llm_ok(block_id: str, v1_text: str) -> dict:
    """Return a well-formed LLM response for *block_id* with V1=*v1_text*."""
    return {
        "block_id": block_id,
        "variants": [
            v1_text,
            "second variant text",
            "third variant text",
            "fourth variant text",
            "fifth variant text",
        ],
    }


# ---------------------------------------------------------------------------
# spintax_one_block — bypass paths
# ---------------------------------------------------------------------------


class TestSpintaxOneBlockBypass:
    async def test_lockable_false_returns_5_copies_of_v1_no_llm_call(self):
        block = Block(id="block_1", text="Hello {{firstName}}", lockable=False)
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
        ) as mock_llm:
            result = await spintax_one_block(block, _full_pool_entry(), _profile())

        assert result.block_id == "block_1"
        assert result.variants == ["Hello {{firstName}}"] * 5
        mock_llm.assert_not_called()

    async def test_lockable_true_but_pool_entry_none_bypasses_llm(self):
        block = Block(id="block_1", text="We help clients win.", lockable=True)
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
        ) as mock_llm:
            result = await spintax_one_block(block, None, _profile())

        assert result.variants == ["We help clients win."] * 5
        mock_llm.assert_not_called()

    async def test_empty_pool_entry_bypasses_llm(self):
        """Lockable block + pool entry with zero synonyms AND zero syntax_options."""
        block = Block(id="block_1", text="We help clients win.", lockable=True)
        empty_entry = BlockPoolEntry(synonyms={}, syntax_options=[])
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
        ) as mock_llm:
            result = await spintax_one_block(block, empty_entry, _profile())

        assert result.variants == ["We help clients win."] * 5
        mock_llm.assert_not_called()

    async def test_synonyms_empty_but_syntax_options_present_does_call_llm(self):
        """Defensive: only one of the two pool-entry fields populated still triggers LLM."""
        block = Block(id="block_1", text="We help clients win.", lockable=True)
        partial = BlockPoolEntry(synonyms={}, syntax_options=["alt phrasing"])
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=_llm_ok("block_1", "We help clients win."),
        ) as mock_llm:
            result = await spintax_one_block(block, partial, _profile())

        assert result.variants[0] == "We help clients win."
        mock_llm.assert_awaited_once()


# ---------------------------------------------------------------------------
# spintax_one_block — happy path + V1 enforcement
# ---------------------------------------------------------------------------


class TestSpintaxOneBlockHappyPath:
    async def test_happy_path_returns_5_variants(self):
        block = Block(id="block_1", text="We help clients win.", lockable=True)
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=_llm_ok("block_1", "We help clients win."),
        ) as mock_llm:
            result = await spintax_one_block(
                block, _full_pool_entry(), _profile()
            )

        mock_llm.assert_awaited_once()
        assert result.block_id == "block_1"
        assert len(result.variants) == 5
        assert result.variants[0] == "We help clients win."
        assert result.variants[1] == "second variant text"
        assert result.variants[4] == "fifth variant text"

    async def test_v1_force_substitution_overrides_llm(self):
        """Even if the LLM returns a different V1, we substitute block.text."""
        block = Block(id="block_1", text="We help clients win.", lockable=True)
        bad_llm = {
            "block_id": "block_1",
            "variants": [
                "WRONG V1 from model",  # the LLM lied about V1
                "v2 alt",
                "v3 alt",
                "v4 alt",
                "v5 alt",
            ],
        }
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=bad_llm,
        ):
            result = await spintax_one_block(
                block, _full_pool_entry(), _profile()
            )

        # V1 force-substituted with original block.text.
        assert result.variants[0] == "We help clients win."
        # V2-V5 came through from the model.
        assert result.variants[1] == "v2 alt"
        assert result.variants[2] == "v3 alt"
        assert result.variants[3] == "v4 alt"
        assert result.variants[4] == "v5 alt"


# ---------------------------------------------------------------------------
# spintax_one_block — validation failures
# ---------------------------------------------------------------------------


class TestSpintaxOneBlockValidation:
    async def test_wrong_block_id_raises(self):
        block = Block(id="block_1", text="We help clients win.", lockable=True)
        bad = {
            "block_id": "block_99",  # mismatched
            "variants": ["a", "b", "c", "d", "e"],
        }
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=bad,
        ):
            with pytest.raises(PipelineStageError) as exc_info:
                await spintax_one_block(block, _full_pool_entry(), _profile())

        assert exc_info.value.error_key == ERR_BLOCK_SPINTAX
        assert "block_id" in exc_info.value.detail.lower()

    async def test_wrong_variant_count_raises(self):
        block = Block(id="block_1", text="We help clients win.", lockable=True)
        bad = {
            "block_id": "block_1",
            "variants": ["a", "b", "c", "d"],  # only 4
        }
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=bad,
        ):
            with pytest.raises(PipelineStageError) as exc_info:
                await spintax_one_block(block, _full_pool_entry(), _profile())

        assert exc_info.value.error_key == ERR_BLOCK_SPINTAX
        assert "5" in exc_info.value.detail

    async def test_empty_variant_string_raises(self):
        block = Block(id="block_1", text="We help clients win.", lockable=True)
        bad = {
            "block_id": "block_1",
            "variants": ["a", "b", "", "d", "e"],
        }
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=bad,
        ):
            with pytest.raises(PipelineStageError) as exc_info:
                await spintax_one_block(block, _full_pool_entry(), _profile())

        assert exc_info.value.error_key == ERR_BLOCK_SPINTAX

    async def test_variants_not_a_list_raises(self):
        block = Block(id="block_1", text="We help clients win.", lockable=True)
        bad = {"block_id": "block_1", "variants": "not a list"}
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=bad,
        ):
            with pytest.raises(PipelineStageError) as exc_info:
                await spintax_one_block(block, _full_pool_entry(), _profile())
        assert exc_info.value.error_key == ERR_BLOCK_SPINTAX

    async def test_non_string_variant_raises(self):
        block = Block(id="block_1", text="We help clients win.", lockable=True)
        bad = {"block_id": "block_1", "variants": ["a", 42, "c", "d", "e"]}
        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=bad,
        ):
            with pytest.raises(PipelineStageError) as exc_info:
                await spintax_one_block(block, _full_pool_entry(), _profile())
        assert exc_info.value.error_key == ERR_BLOCK_SPINTAX


# ---------------------------------------------------------------------------
# spintax_all_blocks — parallel happy path
# ---------------------------------------------------------------------------


class TestSpintaxAllBlocksHappyPath:
    async def test_three_lockable_blocks_returns_three_variant_sets(self):
        blocks = BlockList(
            blocks=[
                Block(id="block_1", text="V1 of block one.", lockable=True),
                Block(id="block_2", text="V1 of block two.", lockable=True),
                Block(id="block_3", text="V1 of block three.", lockable=True),
            ]
        )
        pool = SynonymPool(
            blocks={
                "block_1": _full_pool_entry(),
                "block_2": _full_pool_entry(),
                "block_3": _full_pool_entry(),
            }
        )

        async def fake_llm(*, prompt, model, error_key, **kwargs):
            # The prompt embeds the block_id in the JSON shape line. We
            # parse it back so each call returns its own block_id.
            for line in prompt.splitlines():
                if '"block_id":' in line:
                    bid = line.split('"block_id":')[1].split('"')[1]
                    return _llm_ok(bid, f"V1 of block {bid[-1]}")
            raise AssertionError("block_id not found in prompt")

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            side_effect=fake_llm,
        ) as mock_llm:
            # See test_mixed_lockable_and_unlockable for max_lint_retries=0 rationale.
            results, diag = await spintax_all_blocks(
                blocks, pool, _profile(), max_lint_retries=0
            )

        assert mock_llm.await_count == 3
        assert len(results) == 3
        assert [vs.block_id for vs in results] == ["block_1", "block_2", "block_3"]
        for vs in results:
            assert len(vs.variants) == 5

    async def test_numeric_suffix_ordering_not_lexicographic(self):
        """block_1, block_2, block_10 must sort numerically, not lexicographically."""
        blocks = BlockList(
            blocks=[
                Block(id="block_1", text="t1", lockable=True),
                Block(id="block_2", text="t2", lockable=True),
                Block(id="block_10", text="t10", lockable=True),
            ]
        )
        pool = SynonymPool(
            blocks={
                "block_1": _full_pool_entry(),
                "block_2": _full_pool_entry(),
                "block_10": _full_pool_entry(),
            }
        )

        async def fake_llm(*, prompt, model, error_key, **kwargs):
            for line in prompt.splitlines():
                if '"block_id":' in line:
                    bid = line.split('"block_id":')[1].split('"')[1]
                    # V1 must match input block.text for force-sub; we set
                    # input texts above to "t1", "t2", "t10", so we recover
                    # them from the suffix.
                    suffix = bid.rsplit("_", 1)[1]
                    return _llm_ok(bid, f"t{suffix}")
            raise AssertionError("block_id not in prompt")

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            side_effect=fake_llm,
        ):
            results, _ = await spintax_all_blocks(blocks, pool, _profile())

        # Numeric: 1, 2, 10. Lexicographic would yield 1, 10, 2.
        assert [vs.block_id for vs in results] == [
            "block_1",
            "block_2",
            "block_10",
        ]

    async def test_mixed_lockable_and_unlockable(self):
        """Two lockable + one unlockable: LLM called twice, output has 3 entries."""
        blocks = BlockList(
            blocks=[
                Block(id="block_1", text="lockable one", lockable=True),
                Block(
                    id="block_2",
                    text="{{customLink}}",  # pure placeholder
                    lockable=False,
                ),
                Block(id="block_3", text="lockable three", lockable=True),
            ]
        )
        pool = SynonymPool(
            blocks={
                "block_1": _full_pool_entry(),
                # block_2 absent because unlockable.
                "block_3": _full_pool_entry(),
            }
        )

        async def fake_llm(*, prompt, model, error_key, **kwargs):
            for line in prompt.splitlines():
                if '"block_id":' in line:
                    bid = line.split('"block_id":')[1].split('"')[1]
                    text = "lockable one" if bid == "block_1" else "lockable three"
                    return _llm_ok(bid, text)
            raise AssertionError("block_id not in prompt")

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            side_effect=fake_llm,
        ) as mock_llm:
            # max_lint_retries=0: this test checks call counts, not lint
            # retry behaviour. The _llm_ok fixture intentionally produces
            # length-mismatched variants which would otherwise trigger the
            # lint-feedback retry loop and inflate await_count.
            results, diag = await spintax_all_blocks(
                blocks, pool, _profile(), max_lint_retries=0
            )

        assert mock_llm.await_count == 2  # only the two lockable blocks
        assert len(results) == 3
        ids = [vs.block_id for vs in results]
        assert ids == ["block_1", "block_2", "block_3"]
        # block_2 (unlockable) must be 5 copies of the placeholder.
        block_2 = next(vs for vs in results if vs.block_id == "block_2")
        assert block_2.variants == ["{{customLink}}"] * 5

    async def test_diagnostics_populated(self):
        blocks = BlockList(
            blocks=[
                Block(id="block_1", text="t1", lockable=True),
                Block(id="block_2", text="t2", lockable=True),
                Block(id="block_3", text="t3", lockable=True),
            ]
        )
        pool = SynonymPool(
            blocks={
                "block_1": _full_pool_entry(),
                "block_2": _full_pool_entry(),
                "block_3": _full_pool_entry(),
            }
        )

        async def fake_llm(*, prompt, model, error_key, **kwargs):
            for line in prompt.splitlines():
                if '"block_id":' in line:
                    bid = line.split('"block_id":')[1].split('"')[1]
                    suffix = bid.rsplit("_", 1)[1]
                    return _llm_ok(bid, f"t{suffix}")
            raise AssertionError

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            side_effect=fake_llm,
        ):
            results, diag = await spintax_all_blocks(blocks, pool, _profile())

        assert diag.blocks_completed == 3
        assert diag.blocks_retried == 0
        assert diag.max_retries_per_block == 0
        assert diag.p95_block_duration_ms >= 0


# ---------------------------------------------------------------------------
# spintax_all_blocks — error propagation
# ---------------------------------------------------------------------------


class TestSpintaxAllBlocksErrors:
    async def test_first_error_propagates_through_gather(self):
        blocks = BlockList(
            blocks=[
                Block(id="block_1", text="t1", lockable=True),
                Block(id="block_2", text="t2", lockable=True),
            ]
        )
        pool = SynonymPool(
            blocks={
                "block_1": _full_pool_entry(),
                "block_2": _full_pool_entry(),
            }
        )

        async def fake_llm(*, prompt, model, error_key, **kwargs):
            # Always raise so gather propagates immediately.
            raise PipelineStageError(
                ERR_BLOCK_SPINTAX, detail="forced failure for test"
            )

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            side_effect=fake_llm,
        ):
            with pytest.raises(PipelineStageError) as exc_info:
                await spintax_all_blocks(blocks, pool, _profile())

        assert exc_info.value.error_key == ERR_BLOCK_SPINTAX


# ---------------------------------------------------------------------------
# spintax_one_block - lint-feedback retry
# ---------------------------------------------------------------------------


class TestLintFeedbackRetry:
    """Per-block lint retry: spintax_one_block re-prompts the model up to
    max_lint_retries times when the deterministic linter reports model-fixable
    errors (length, em-dash, banned words, invisible chars) on the variants
    it just produced.
    """

    async def test_first_attempt_clean_no_retry(self):
        """Happy path: first attempt's variants pass the per-block linter.

        tolerance=0.5 keeps the length check generous so tiny wording
        differences in the fixture don't accidentally trip the retry.
        """
        v1 = "We help small accounting firms grow their practice."
        block = Block(id="block_1", text=v1, lockable=True)

        clean = {
            "block_id": "block_1",
            "variants": [
                v1,
                "We support small accounting firms grow practice.",
                "We assist small accounting firms scale practice.",
                "We help small accounting firms expand practice.",
                "We help small accounting firms build practice.",
            ],
        }

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=clean,
        ) as mock_llm:
            result = await spintax_one_block(
                block,
                _full_pool_entry(),
                _profile(),
                platform="instantly",
                tolerance=0.5,
                tolerance_floor=3,
                max_lint_retries=2,
            )

        assert mock_llm.await_count == 1
        assert result.variants[0] == v1

    async def test_length_violation_triggers_retry_with_feedback(self):
        """First attempt produces a too-long variant; second attempt clean.

        Asserts the retry happens AND the second attempt's prompt contains
        the lint-feedback section. tolerance=0.3 widens the allowed band
        so only the deliberately-egregious variant trips it; the
        "second attempt" variants (close to V1 length) pass cleanly.
        """
        v1 = "We help small accounting firms grow their practice."  # 51 chars
        block = Block(id="block_1", text=v1, lockable=True)

        # tolerance=0.3 -> max(51*0.3, 3) = 15.3 chars allowed.
        # too_long has 31-char diff vs V1 -> fails.
        too_long = (
            "We help small accounting firms grow their practice substantially today right now."
        )  # 82 chars, +31 vs V1
        too_long_first = {
            "block_id": "block_1",
            "variants": [
                v1,
                too_long,
                "We support small accounting firms grow practice.",
                "We assist small accounting firms scale practice.",
                "We help small accounting firms expand practice.",
            ],
        }
        clean_second = {
            "block_id": "block_1",
            "variants": [
                v1,
                "We help small accounting firms scale practice.",
                "We support small accounting firms grow practice.",
                "We assist small accounting firms scale practice.",
                "We help small accounting firms expand practice.",
            ],
        }

        prompts_seen: list[str] = []
        call_count = [0]

        async def fake_llm(*, prompt, model, error_key, **kwargs):
            prompts_seen.append(prompt)
            call_count[0] += 1
            return too_long_first if call_count[0] == 1 else clean_second

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            side_effect=fake_llm,
        ):
            result = await spintax_one_block(
                block,
                _full_pool_entry(),
                _profile(),
                platform="instantly",
                tolerance=0.3,
                tolerance_floor=3,
                max_lint_retries=2,
            )

        assert call_count[0] == 2
        # First prompt has no feedback section.
        assert "LINT FEEDBACK" not in prompts_seen[0]
        # Second prompt has the feedback section AND mentions length.
        assert "LINT FEEDBACK" in prompts_seen[1]
        assert "length" in prompts_seen[1].lower()
        # Final result is the clean second attempt.
        assert result.variants[0] == v1
        assert result.variants[1] == "We help small accounting firms scale practice."

    async def test_banned_word_triggers_retry(self):
        """Banned-AI-word violation triggers retry with feedback.

        tolerance=0.5 prevents accidental length-tolerance trips so the
        only thing the linter can flag is the banned word.
        """
        v1 = "We help small accounting firms grow their practice."
        block = Block(id="block_1", text=v1, lockable=True)

        # "leverage" is on BANNED_AI_WORDS.
        first = {
            "block_id": "block_1",
            "variants": [
                v1,
                "We leverage small accounting firms grow practice.",  # banned
                "We support small accounting firms grow practice.",
                "We assist small accounting firms scale practice.",
                "We help small accounting firms expand practice.",
            ],
        }
        second = {
            "block_id": "block_1",
            "variants": [
                v1,
                "We help small accounting firms scale practice.",
                "We support small accounting firms grow practice.",
                "We assist small accounting firms scale practice.",
                "We help small accounting firms expand practice.",
            ],
        }

        prompts_seen: list[str] = []
        call_count = [0]

        async def fake_llm(*, prompt, model, error_key, **kwargs):
            prompts_seen.append(prompt)
            call_count[0] += 1
            return first if call_count[0] == 1 else second

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            side_effect=fake_llm,
        ):
            result = await spintax_one_block(
                block,
                _full_pool_entry(),
                _profile(),
                platform="instantly",
                tolerance=0.5,
                tolerance_floor=3,
                max_lint_retries=2,
            )

        assert call_count[0] == 2
        assert "LINT FEEDBACK" in prompts_seen[1]
        assert "banned word" in prompts_seen[1].lower() or "leverage" in prompts_seen[1]
        assert "leverage" not in result.variants[1]

    async def test_retries_exhausted_returns_last_attempt_no_raise(self):
        """All attempts violate lint; function returns the last attempt
        without raising. The pipeline-level lint pass surfaces the issue."""
        v1 = "We help small accounting firms grow their practice."
        block = Block(id="block_1", text=v1, lockable=True)

        always_bad = {
            "block_id": "block_1",
            "variants": [
                v1,
                "We help small accounting firms grow their practice substantially today right now.",
                "We help small accounting firms grow their practice substantially today right now.",
                "We help small accounting firms grow their practice substantially today right now.",
                "We help small accounting firms grow their practice substantially today right now.",
            ],
        }

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=always_bad,
        ) as mock_llm:
            result = await spintax_one_block(
                block,
                _full_pool_entry(),
                _profile(),
                platform="instantly",
                tolerance=0.05,
                tolerance_floor=3,
                max_lint_retries=2,
            )

        # 1 + 2 retries = 3 attempts total.
        assert mock_llm.await_count == 3
        # Returned without raising even though lint kept failing.
        assert result.variants[0] == v1
        assert len(result.variants) == 5

    async def test_max_lint_retries_zero_disables_retry(self):
        """max_lint_retries=0 means one call only, even if lint fails."""
        v1 = "We help small accounting firms grow their practice."
        block = Block(id="block_1", text=v1, lockable=True)

        always_bad = {
            "block_id": "block_1",
            "variants": [
                v1,
                "We help small accounting firms grow their practice substantially today right now.",
                "We help small accounting firms grow their practice substantially today right now.",
                "We help small accounting firms grow their practice substantially today right now.",
                "We help small accounting firms grow their practice substantially today right now.",
            ],
        }

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=always_bad,
        ) as mock_llm:
            result = await spintax_one_block(
                block,
                _full_pool_entry(),
                _profile(),
                platform="instantly",
                tolerance=0.05,
                tolerance_floor=3,
                max_lint_retries=0,
            )

        assert mock_llm.await_count == 1
        assert result.variants[0] == v1

    async def test_v1_only_lint_error_does_not_trigger_retry(self):
        """If V1 (block.text) contains a banned word, the retry can't fix
        it (force-substituted). Must NOT retry forever - one call only."""
        # V1 contains "leverage" (banned). Force-substitution preserves it.
        v1 = "We leverage tech for accounting firms."
        block = Block(id="block_1", text=v1, lockable=True)

        response = {
            "block_id": "block_1",
            "variants": [
                v1,  # banned word here, but force-sub preserves it anyway
                "We help small accounting firms scale.",
                "We support small accounting firms grow.",
                "We assist small accounting firms expand.",
                "We back small accounting firms build.",
            ],
        }

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            new_callable=AsyncMock,
            return_value=response,
        ) as mock_llm:
            result = await spintax_one_block(
                block,
                _full_pool_entry(),
                _profile(),
                platform="instantly",
                tolerance=0.05,
                tolerance_floor=3,
                max_lint_retries=2,
            )

        # V1-only banned-word error must be filtered: no retry.
        assert mock_llm.await_count == 1
        assert result.variants[0] == v1

    async def test_lint_feedback_with_brace_variants_does_not_break_format(self):
        """Variants containing literal `{` `}` (placeholders) must not break
        the prompt template's .format() call when fed back as feedback."""
        v1 = "Hi {{firstName}}, we help small accounting firms grow."
        block = Block(id="block_1", text=v1, lockable=True)

        # First attempt: too-long variant that contains literal `{{firstName}}`.
        too_long = (
            "Hi {{firstName}}, we help small accounting firms grow their "
            "practice substantially today right now and forever."
        )
        first = {
            "block_id": "block_1",
            "variants": [
                v1,
                too_long,
                "Hi {{firstName}}, we support small accounting firms grow.",
                "Hi {{firstName}}, we assist small accounting firms grow.",
                "Hi {{firstName}}, we back small accounting firms grow.",
            ],
        }
        second = {
            "block_id": "block_1",
            "variants": [
                v1,
                "Hi {{firstName}}, we back small accounting firms grow.",
                "Hi {{firstName}}, we support small accounting firms grow.",
                "Hi {{firstName}}, we assist small accounting firms grow.",
                "Hi {{firstName}}, we help small accounting firms scale.",
            ],
        }

        call_count = [0]

        async def fake_llm(*, prompt, model, error_key, **kwargs):
            call_count[0] += 1
            return first if call_count[0] == 1 else second

        with patch(
            "app.pipeline.block_spintaxer.call_llm_json",
            side_effect=fake_llm,
        ):
            # If .format() were called on the feedback bullets, the literal
            # `{{firstName}}` would either be treated as a literal `{firstName}`
            # placeholder OR raise KeyError. This test fails if either happens.
            result = await spintax_one_block(
                block,
                _full_pool_entry(),
                _profile(),
                platform="instantly",
                tolerance=0.05,
                tolerance_floor=3,
                max_lint_retries=2,
            )

        assert call_count[0] == 2
        assert result.variants[0] == v1
