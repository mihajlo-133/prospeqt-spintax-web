"""Unit tests for ``app.pipeline.pipeline_runner.run_pipeline``.

Each pipeline stage is patched at the orchestrator's import path so we
can drive the stage outputs deterministically without hitting LLMs or
the real qa() validator.

The full end-to-end test with recorded LLM-shaped fixtures lives in
``test_pipeline_integration.py``. This file is for the orchestrator's
own logic - retry loops, diagnostics aggregation, error propagation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.pipeline.contracts import (
    ERR_BLOCK_SPINTAX,
    ERR_PROFILER,
    AssembledSpintax,
    Block,
    BlockList,
    BlockPoolEntry,
    BlockSpintaxerDiagnostics,
    PipelineStageError,
    Profile,
    ProfilerDiagnostics,
    SplitterDiagnostics,
    SynonymPool,
    SynonymPoolDiagnostics,
    VariantSet,
)
from app.pipeline.pipeline_runner import _failing_block_indices, run_pipeline


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_block_list() -> BlockList:
    """Three lockable blocks: block_1, block_2, block_3."""
    return BlockList(
        blocks=[
            Block(id="block_1", text="Hi {{firstName}}.", lockable=True),
            Block(id="block_2", text="We help firms.", lockable=True),
            Block(id="block_3", text="Talk soon.", lockable=True),
        ]
    )


def _make_profile() -> Profile:
    return Profile(
        tone="professional B2B",
        audience_hint="law firms",
        locked_common_nouns=["firms"],
        proper_nouns=[],
    )


def _make_pool() -> SynonymPool:
    return SynonymPool(
        blocks={
            "block_1": BlockPoolEntry(
                synonyms={"hi": ["hello", "hey"]},
                syntax_options=[],
            ),
            "block_2": BlockPoolEntry(
                synonyms={"help": ["assist", "support"]},
                syntax_options=[],
            ),
            "block_3": BlockPoolEntry(
                synonyms={"talk": ["chat", "connect"]},
                syntax_options=[],
            ),
        }
    )


def _make_initial_variants() -> list[VariantSet]:
    """One VariantSet per block, V1 verbatim, four other strings."""
    return [
        VariantSet(
            block_id="block_1",
            variants=[
                "Hi {{firstName}}.",
                "Hello {{firstName}}.",
                "Hey {{firstName}}.",
                "Greetings {{firstName}}.",
                "Howdy {{firstName}}.",
            ],
        ),
        VariantSet(
            block_id="block_2",
            variants=[
                "We help firms.",
                "We assist firms.",
                "We support firms.",
                "We back firms.",
                "We enable firms.",
            ],
        ),
        VariantSet(
            block_id="block_3",
            variants=[
                "Talk soon.",
                "Chat soon.",
                "Connect soon.",
                "Speak soon.",
                "Catch up soon.",
            ],
        ),
    ]


def _qa_pass() -> dict:
    return {
        "passed": True,
        "error_count": 0,
        "warning_count": 0,
        "errors": [],
        "warnings": [],
        "block_count": 3,
        "input_paragraph_count": 3,
        "diversity_block_scores": [0.5, 0.5, 0.5],
        "diversity_pair_distances": [
            [0.5, 0.5, 0.5, 0.5],
            [0.5, 0.5, 0.5, 0.5],
            [0.5, 0.5, 0.5, 0.5],
        ],
        "diversity_corpus_avg": 0.5,
        "diversity_floor_block_avg": 0.30,
        "diversity_floor_pair": 0.20,
        "diversity_gate_level": "warning",
    }


def _qa_fail_block(idx: int) -> dict:
    """QA result that fails diversity on a single 0-indexed block."""
    scores = [0.5, 0.5, 0.5]
    pairs: list[list[float]] = [
        [0.5, 0.5, 0.5, 0.5],
        [0.5, 0.5, 0.5, 0.5],
        [0.5, 0.5, 0.5, 0.5],
    ]
    scores[idx] = 0.10
    pairs[idx] = [0.05, 0.05, 0.05, 0.05]
    return {
        "passed": False,
        "error_count": 1,
        "warning_count": 0,
        "errors": [f"block {idx + 1}: diversity below floor"],
        "warnings": [],
        "block_count": 3,
        "input_paragraph_count": 3,
        "diversity_block_scores": scores,
        "diversity_pair_distances": pairs,
        "diversity_corpus_avg": 0.4,
        "diversity_floor_block_avg": 0.30,
        "diversity_floor_pair": 0.20,
        "diversity_gate_level": "error",
    }


def _qa_fail_global() -> dict:
    """QA fails on a non-block-localized error (no diversity dips)."""
    return {
        "passed": False,
        "error_count": 1,
        "warning_count": 0,
        "errors": ["block count mismatch: 3 vs 4"],
        "warnings": [],
        "block_count": 3,
        "input_paragraph_count": 4,
        "diversity_block_scores": [0.5, 0.5, 0.5],
        "diversity_pair_distances": [
            [0.5, 0.5, 0.5, 0.5],
            [0.5, 0.5, 0.5, 0.5],
            [0.5, 0.5, 0.5, 0.5],
        ],
        "diversity_corpus_avg": 0.5,
        "diversity_floor_block_avg": 0.30,
        "diversity_floor_pair": 0.20,
        "diversity_gate_level": "error",
    }


def _patch_stages(
    *,
    splitter_return=None,
    profiler_return=None,
    pool_return=None,
    spintaxer_return=None,
    spintax_one_return=None,
    qa_return_value=None,
    qa_side_effect=None,
):
    """Build the patcher stack used by every test.

    Returns a context manager that patches every external call point on
    the orchestrator at once. Defaults are happy-path values.
    """
    if splitter_return is None:
        splitter_return = (_make_block_list(), SplitterDiagnostics())
    if profiler_return is None:
        profiler_return = (_make_profile(), ProfilerDiagnostics(tone="x"))
    if pool_return is None:
        pool_return = (_make_pool(), SynonymPoolDiagnostics())
    if spintaxer_return is None:
        spintaxer_return = (
            _make_initial_variants(),
            BlockSpintaxerDiagnostics(blocks_completed=3, p95_block_duration_ms=100),
        )

    qa_mock = MagicMock()
    if qa_side_effect is not None:
        qa_mock.side_effect = qa_side_effect
    else:
        qa_mock.return_value = qa_return_value if qa_return_value is not None else _qa_pass()

    return _StagesPatcher(
        splitter_return,
        profiler_return,
        pool_return,
        spintaxer_return,
        spintax_one_return,
        qa_mock,
    )


class _StagesPatcher:
    def __init__(
        self,
        splitter_return,
        profiler_return,
        pool_return,
        spintaxer_return,
        spintax_one_return,
        qa_mock,
    ):
        self.splitter_return = splitter_return
        self.profiler_return = profiler_return
        self.pool_return = pool_return
        self.spintaxer_return = spintaxer_return
        self.spintax_one_return = spintax_one_return
        self.qa_mock = qa_mock
        self._patches: list = []

    def __enter__(self):
        self.split_mock = AsyncMock(return_value=self.splitter_return)
        self.profile_mock = AsyncMock(return_value=self.profiler_return)
        self.pool_mock = AsyncMock(return_value=self.pool_return)
        self.spintax_all_mock = AsyncMock(return_value=self.spintaxer_return)
        self.spintax_one_mock = AsyncMock()
        if self.spintax_one_return is not None:
            if isinstance(self.spintax_one_return, list):
                self.spintax_one_mock.side_effect = self.spintax_one_return
            else:
                self.spintax_one_mock.return_value = self.spintax_one_return

        self._patches = [
            patch("app.pipeline.pipeline_runner.split_email", self.split_mock),
            patch("app.pipeline.pipeline_runner.profile_email", self.profile_mock),
            patch(
                "app.pipeline.pipeline_runner.generate_synonym_pool",
                self.pool_mock,
            ),
            patch(
                "app.pipeline.pipeline_runner.spintax_all_blocks",
                self.spintax_all_mock,
            ),
            patch(
                "app.pipeline.pipeline_runner.spintax_one_block",
                self.spintax_one_mock,
            ),
            # Patch the lazy-imported qa() function at its source so the
            # orchestrator's `from app.qa import qa` picks up our mock.
            patch("app.qa.qa", self.qa_mock),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for p in self._patches:
            p.stop()
        return False


# ---------------------------------------------------------------------------
# 1. Helper: _failing_block_indices
# ---------------------------------------------------------------------------


class TestFailingBlockIndices:
    def test_no_failures(self):
        result = _failing_block_indices(_qa_pass())
        assert result == []

    def test_block_avg_below_floor(self):
        qa = _qa_pass()
        qa["diversity_block_scores"] = [0.5, 0.20, 0.5]  # block_2 below 0.30
        assert _failing_block_indices(qa) == [1]

    def test_pair_distance_below_floor(self):
        qa = _qa_pass()
        qa["diversity_block_scores"] = [0.5, 0.5, 0.5]  # avg fine
        qa["diversity_pair_distances"][2] = [0.5, 0.5, 0.10, 0.5]  # one pair < 0.20
        assert _failing_block_indices(qa) == [2]

    def test_score_none_skipped(self):
        qa = _qa_pass()
        qa["diversity_block_scores"] = [None, 0.20, 0.5]
        # idx 0 skipped because score=None even though "below floor"-ish
        assert _failing_block_indices(qa) == [1]

    def test_empty_inputs(self):
        qa = _qa_pass()
        qa["diversity_block_scores"] = []
        qa["diversity_pair_distances"] = []
        assert _failing_block_indices(qa) == []

    def test_short_block_gets_relaxed_avg_floor(self):
        """A short block (<60 chars) with score 0.25 passes (relaxed 0.20)."""
        qa = _qa_pass()
        qa["diversity_block_scores"] = [0.25, 0.5, 0.5]
        # Block 0 is short (under 60 chars), block_1 + block_2 are long.
        block_lengths = [40, 80, 80]
        assert _failing_block_indices(qa, block_lengths) == []

    def test_long_block_keeps_strict_avg_floor(self):
        """A long block (>=60 chars) with score 0.25 still fails strict 0.30."""
        qa = _qa_pass()
        qa["diversity_block_scores"] = [0.25, 0.5, 0.5]
        block_lengths = [80, 80, 80]
        assert _failing_block_indices(qa, block_lengths) == [0]

    def test_short_block_below_relaxed_floor_still_fails(self):
        """A short block with score 0.15 fails even the relaxed 0.20 floor."""
        qa = _qa_pass()
        qa["diversity_block_scores"] = [0.15, 0.5, 0.5]
        block_lengths = [40, 80, 80]
        assert _failing_block_indices(qa, block_lengths) == [0]

    def test_short_block_pair_floor_relaxed(self):
        """Short block with pair distance 0.15 passes (relaxed pair floor 0.10)."""
        qa = _qa_pass()
        qa["diversity_block_scores"] = [0.5, 0.5, 0.5]  # avgs all fine
        # Block 0 has one pair at 0.15 - would fail strict 0.20 but passes
        # relaxed 0.10.
        qa["diversity_pair_distances"][0] = [0.5, 0.5, 0.15, 0.5]
        block_lengths = [40, 80, 80]
        assert _failing_block_indices(qa, block_lengths) == []

    def test_block_lengths_none_uses_strict_floor(self):
        """Backward compat: when block_lengths is None, strict floors apply."""
        qa = _qa_pass()
        qa["diversity_block_scores"] = [0.25, 0.5, 0.5]
        # No block_lengths arg = backward-compatible strict gate.
        assert _failing_block_indices(qa) == [0]


# ---------------------------------------------------------------------------
# 2. Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_no_retries():
    """All stages succeed, qa passes on first try."""
    with _patch_stages() as p:
        assembled, diag = await run_pipeline("Hi.\n\nThanks.\n\nBye.\n")

    assert isinstance(assembled, AssembledSpintax)
    assert assembled.spintax  # not empty
    # QA called exactly once
    assert p.qa_mock.call_count == 1
    # Diagnostics
    assert diag.pipeline == "beta_v1"
    assert diag.block_spintaxer.blocks_completed == 3
    assert diag.block_spintaxer.blocks_retried == 0
    assert diag.block_spintaxer.max_retries_per_block == 0
    # Spintax_one_block never called (no retries)
    p.spintax_one_mock.assert_not_called()


@pytest.mark.asyncio
async def test_stages_run_in_correct_order():
    """Verify splitter+profiler called BEFORE pool, pool BEFORE spintaxer."""
    call_log: list[str] = []

    async def _split(*a, **kw):
        call_log.append("split")
        return _make_block_list(), SplitterDiagnostics()

    async def _profile(*a, **kw):
        call_log.append("profile")
        return _make_profile(), ProfilerDiagnostics(tone="x")

    async def _pool(*a, **kw):
        call_log.append("pool")
        return _make_pool(), SynonymPoolDiagnostics()

    async def _spin_all(*a, **kw):
        call_log.append("spin")
        return _make_initial_variants(), BlockSpintaxerDiagnostics(
            blocks_completed=3
        )

    qa_mock = MagicMock(return_value=_qa_pass())

    with patch("app.pipeline.pipeline_runner.split_email", _split), patch(
        "app.pipeline.pipeline_runner.profile_email", _profile
    ), patch(
        "app.pipeline.pipeline_runner.generate_synonym_pool", _pool
    ), patch(
        "app.pipeline.pipeline_runner.spintax_all_blocks", _spin_all
    ), patch("app.qa.qa", qa_mock):
        await run_pipeline("Hi.\n\nThanks.\n\nBye.\n")

    # split + profile both before pool; pool before spin
    assert "split" in call_log[:2]
    assert "profile" in call_log[:2]
    assert call_log.index("pool") < call_log.index("spin")
    assert call_log.index("split") < call_log.index("pool")
    assert call_log.index("profile") < call_log.index("pool")


# ---------------------------------------------------------------------------
# 3. Stage error propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_splitter_error_propagates():
    split_mock = AsyncMock(side_effect=PipelineStageError("splitter_error", "boom"))
    profile_mock = AsyncMock(
        return_value=(_make_profile(), ProfilerDiagnostics(tone="x"))
    )

    with patch("app.pipeline.pipeline_runner.split_email", split_mock), patch(
        "app.pipeline.pipeline_runner.profile_email", profile_mock
    ):
        with pytest.raises(PipelineStageError) as exc:
            await run_pipeline("body")

    assert exc.value.error_key == "splitter_error"


@pytest.mark.asyncio
async def test_profiler_error_propagates():
    split_mock = AsyncMock(
        return_value=(_make_block_list(), SplitterDiagnostics())
    )
    profile_mock = AsyncMock(
        side_effect=PipelineStageError(ERR_PROFILER, "boom")
    )

    with patch("app.pipeline.pipeline_runner.split_email", split_mock), patch(
        "app.pipeline.pipeline_runner.profile_email", profile_mock
    ):
        with pytest.raises(PipelineStageError) as exc:
            await run_pipeline("body")

    assert exc.value.error_key == ERR_PROFILER


# ---------------------------------------------------------------------------
# 4. QA retry logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_fail_then_retry_succeeds():
    """Block 1 fails diversity, retry produces variants that pass qa()."""
    # First qa() call: block_2 (idx=1) fails. Second call: pass.
    qa_results = [_qa_fail_block(1), _qa_pass()]

    new_variants = VariantSet(
        block_id="block_2",
        variants=[
            "We help firms.",
            "We aid firms.",
            "We back firms.",
            "We enable firms.",
            "We bolster firms.",
        ],
    )

    with _patch_stages(
        spintax_one_return=new_variants,
        qa_side_effect=qa_results,
    ) as p:
        assembled, diag = await run_pipeline("Hi.\n\nWe help.\n\nBye.\n")

    # qa() called twice (initial + after retry)
    assert p.qa_mock.call_count == 2
    # spintax_one_block called exactly once for the failing block
    assert p.spintax_one_mock.await_count == 1
    # The block re-spintaxed was block_2
    call_kwargs = p.spintax_one_mock.await_args
    block_arg = call_kwargs.args[0]
    assert block_arg.id == "block_2"
    # Diagnostics reflect the retry
    assert diag.block_spintaxer.blocks_retried == 1
    assert diag.block_spintaxer.max_retries_per_block == 1


@pytest.mark.asyncio
async def test_qa_fail_global_ships_without_retry():
    """Non-block-localized failure (block_count mismatch, greeting check,
    duplicate variants, v1 fidelity) cannot be helped by per-block retry,
    so the orchestrator MUST ship the assembled spintax instead of raising.
    The runner records qa_passed=False on the result so /api/status
    surfaces the failure without losing the generated body. This mirrors
    alpha's "ship best-effort body, mark qa_passed=False" semantics."""
    with _patch_stages(qa_return_value=_qa_fail_global()) as p:
        assembled, diag = await run_pipeline("body")

    # Spintax was assembled (not lost to a raised exception).
    assert assembled.spintax
    assert diag.pipeline == "beta_v1"
    # No retries attempted - non-localized errors don't trigger retry.
    p.spintax_one_mock.assert_not_called()


@pytest.mark.asyncio
async def test_retries_exhaust_ships_best_effort():
    """Block keeps failing through all retries; runner ships best-effort.

    After every failing block exhausts its retry budget, the orchestrator
    breaks out of the loop and returns the most recently assembled body
    with ``qa_passed=False`` rather than raising. This mirrors the
    non-localized-failure path and matches alpha's "ship and surface
    errors" semantics: the operator can inspect the body, see qa.errors
    in the API response, and judge severity. Raising would lose the body
    and waste the per-block spend.
    """
    # qa() always reports block_1 (idx=0) failing.
    failing = _qa_fail_block(0)

    # spintax_one_block always returns the same useless variants.
    bad_variants = VariantSet(
        block_id="block_1",
        variants=["Hi.", "Hi.", "Hi.", "Hi.", "Hi."],
    )

    with _patch_stages(
        spintax_one_return=bad_variants,
        qa_side_effect=[failing, failing, failing, failing],
    ) as p:
        assembled, diag = await run_pipeline(
            "Hi.\n\nThanks.\n\nBye.\n",
            max_retries_per_block=2,
        )

    # Body shipped with best-effort variants - no raise.
    assert isinstance(assembled, AssembledSpintax)
    assert assembled.spintax  # non-empty
    # Initial qa() + 2 retries = 3 qa() calls before bailing out
    assert p.qa_mock.call_count == 3
    assert p.spintax_one_mock.await_count == 2  # 2 retries used
    # Diagnostics reflect the exhausted retries.
    assert diag.block_spintaxer.blocks_retried == 2
    assert diag.block_spintaxer.max_retries_per_block == 2


@pytest.mark.asyncio
async def test_two_blocks_failing_retried_in_parallel():
    """When multiple blocks fail in one round, all retries fire in parallel."""
    qa_fail_two = _qa_fail_block(0)
    qa_fail_two["diversity_block_scores"][2] = 0.10
    qa_fail_two["diversity_pair_distances"][2] = [0.05, 0.05, 0.05, 0.05]

    qa_results = [qa_fail_two, _qa_pass()]

    # spintax_one returns valid variants for whichever block was passed.
    async def _retry_fn(block, *a, **kw):
        return VariantSet(
            block_id=block.id,
            variants=[block.text] + [f"alt {block.id} {i}" for i in range(1, 5)],
        )

    spintax_one_mock = AsyncMock(side_effect=_retry_fn)

    qa_mock = MagicMock(side_effect=qa_results)

    with patch(
        "app.pipeline.pipeline_runner.split_email",
        AsyncMock(return_value=(_make_block_list(), SplitterDiagnostics())),
    ), patch(
        "app.pipeline.pipeline_runner.profile_email",
        AsyncMock(
            return_value=(_make_profile(), ProfilerDiagnostics(tone="x"))
        ),
    ), patch(
        "app.pipeline.pipeline_runner.generate_synonym_pool",
        AsyncMock(return_value=(_make_pool(), SynonymPoolDiagnostics())),
    ), patch(
        "app.pipeline.pipeline_runner.spintax_all_blocks",
        AsyncMock(
            return_value=(
                _make_initial_variants(),
                BlockSpintaxerDiagnostics(blocks_completed=3),
            )
        ),
    ), patch(
        "app.pipeline.pipeline_runner.spintax_one_block", spintax_one_mock
    ), patch("app.qa.qa", qa_mock):
        _, diag = await run_pipeline("Hi.\n\nThanks.\n\nBye.\n")

    assert spintax_one_mock.await_count == 2  # both blocks retried in one round
    assert diag.block_spintaxer.blocks_retried == 2
    assert diag.block_spintaxer.max_retries_per_block == 1


# ---------------------------------------------------------------------------
# 5. Diagnostics aggregation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostics_carry_through_initial_stages():
    """Splitter/profiler/pool diagnostics show up in PipelineDiagnostics."""
    splitter_diag = SplitterDiagnostics(
        block_count=3, lockable_count=3, duration_ms=120
    )
    profiler_diag = ProfilerDiagnostics(
        tone="warm B2B",
        locked_nouns=["clients"],
        proper_nouns=["Acme"],
        duration_ms=80,
    )
    pool_diag = SynonymPoolDiagnostics(
        total_synonyms=12, blocks_covered=3, duration_ms=200
    )
    spintaxer_diag = BlockSpintaxerDiagnostics(
        blocks_completed=3, p95_block_duration_ms=400
    )

    with _patch_stages(
        splitter_return=(_make_block_list(), splitter_diag),
        profiler_return=(_make_profile(), profiler_diag),
        pool_return=(_make_pool(), pool_diag),
        spintaxer_return=(_make_initial_variants(), spintaxer_diag),
    ):
        _, diag = await run_pipeline("body")

    assert diag.splitter.block_count == 3
    assert diag.splitter.duration_ms == 120
    assert diag.profiler.tone == "warm B2B"
    assert diag.profiler.locked_nouns == ["clients"]
    assert diag.synonym_pool.total_synonyms == 12
    assert diag.block_spintaxer.p95_block_duration_ms == 400
    assert diag.block_spintaxer.blocks_retried == 0
