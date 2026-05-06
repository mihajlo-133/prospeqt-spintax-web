"""Stage 6 - Pipeline Runner (orchestrator).

End-to-end driver for the beta block-first pipeline. Chains stages 1-5
(splitter, profiler, synonym pool, block spintaxer, assembler) and runs
the existing alpha QA validators afterward. On per-block diversity
failures it retries the failing block(s) up to ``max_retries_per_block``
times before giving up.

Public API::

    from app.pipeline.pipeline_runner import run_pipeline

    assembled, diagnostics = await run_pipeline(plain_body)

The orchestrator does NOT import ``app.spintax_runner`` (the 3,200-line
alpha runner) at module import time. ``app.qa`` is imported lazily inside
``run_pipeline`` so the alpha QA module is only pulled in when actually
running the pipeline, keeping unrelated test imports cheap.

Stage timing (parallel where possible):

    t=0:   splitter || profiler   (asyncio.gather)
    t~2:   synonym_pool           (waits on splitter + profiler)
    t~4:   block_spintaxer        (internally parallel across blocks)
    t~10:  assembler              (sync, fast)
    t~10:  qa() validators
    t~10+: per-block retries on diversity failures (parallel)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.pipeline.assembler import assemble_spintax
from app.pipeline.block_spintaxer import (
    spintax_all_blocks,
    spintax_one_block,
)
from app.pipeline.contracts import (
    ERR_BLOCK_SPINTAX,
    AssembledSpintax,
    BlockSpintaxerDiagnostics,
    PipelineDiagnostics,
    PipelineStageError,
    VariantSet,
)
from app.pipeline.profiler import profile_email
from app.pipeline.splitter import split_email
from app.pipeline.synonym_pool import generate_synonym_pool

DEFAULT_PLATFORM = "instantly"
DEFAULT_MAX_RETRIES_PER_BLOCK = 2
DEFAULT_LINT_TOLERANCE = 0.05
DEFAULT_LINT_TOLERANCE_FLOOR = 3
DEFAULT_LINT_MAX_RETRIES_PER_BLOCK = 2

# Mirror app.qa floors so we don't import the 851-line qa module just for
# two constants. If the alpha floors ever move, update these too.
_BLOCK_AVG_FLOOR = 0.30
_BLOCK_PAIR_FLOOR = 0.20

# Relaxed floors for short blocks (greetings, P.S. lines, callouts).
# Short blocks have less variable surface area between locked nouns +
# placeholders + function words, so even maximally creative variants
# can land below the strict floor purely from arithmetic. Scaling by
# block char length lets us keep the strict gate where there's room
# to move while not penalising blocks where there isn't.
_LONG_BLOCK_CHAR_THRESHOLD = 60
_BLOCK_AVG_FLOOR_SHORT = 0.20
_BLOCK_PAIR_FLOOR_SHORT = 0.10


def _failing_block_indices(
    qa_result: dict[str, Any],
    block_lengths: list[int] | None = None,
) -> list[int]:
    """Return 0-indexed block positions that fail the diversity floors.

    Mirrors ``compute_jaccard_failing_blocks`` from spintax_runner.py
    without pulling that module in. A block fails when:

    - block-avg V1<->Vn distance < floor, OR
    - any single V1<->Vn pair distance < pair_floor.

    Floors scale with block char length when ``block_lengths`` is
    provided: blocks shorter than ``_LONG_BLOCK_CHAR_THRESHOLD`` chars
    get the relaxed floors (avg 0.20, pair 0.10) because their variable
    surface is too small to reliably hit the strict 0.30/0.20 gate.
    Long blocks keep the strict floors. When ``block_lengths`` is None
    (e.g. unit tests that exercise the helper directly), the strict
    floors apply to every block (backward-compatible).

    Greeting / short / unscorable blocks (score=None) are skipped.
    """
    block_scores = qa_result.get("diversity_block_scores") or []
    pair_distances_per_block = qa_result.get("diversity_pair_distances") or []
    failing: list[int] = []
    for idx, score in enumerate(block_scores):
        if score is None:
            continue
        if (
            block_lengths is not None
            and idx < len(block_lengths)
            and block_lengths[idx] < _LONG_BLOCK_CHAR_THRESHOLD
        ):
            avg_floor = _BLOCK_AVG_FLOOR_SHORT
            pair_floor = _BLOCK_PAIR_FLOOR_SHORT
        else:
            avg_floor = _BLOCK_AVG_FLOOR
            pair_floor = _BLOCK_PAIR_FLOOR
        if score < avg_floor:
            failing.append(idx)
            continue
        pairs = (
            pair_distances_per_block[idx]
            if idx < len(pair_distances_per_block)
            else []
        )
        if any(d is not None and d < pair_floor for d in pairs):
            failing.append(idx)
    return failing


async def run_pipeline(
    plain_body: str,
    *,
    platform: str = DEFAULT_PLATFORM,
    splitter_model: str = "gpt-5-mini",
    profiler_model: str = "gpt-5-mini",
    pool_model: str = "gpt-5-mini",
    spintaxer_model: str = "gpt-5",
    spintaxer_reasoning: str = "high",
    max_retries_per_block: int = DEFAULT_MAX_RETRIES_PER_BLOCK,
    tolerance: float = DEFAULT_LINT_TOLERANCE,
    tolerance_floor: int = DEFAULT_LINT_TOLERANCE_FLOOR,
    lint_max_retries_per_block: int = DEFAULT_LINT_MAX_RETRIES_PER_BLOCK,
    on_api_call: Callable[[Any], None] | None = None,
) -> tuple[AssembledSpintax, PipelineDiagnostics]:
    """Run the full beta block-first pipeline end-to-end.

    Args:
        plain_body: plain-text email body (with ``{{placeholders}}``).
        platform: ``"instantly"`` or ``"emailbison"`` - passed to ``qa()``
            and to the per-block lint check inside the spintaxer.
        splitter_model / profiler_model / pool_model: gpt-5.x model names
            for the deterministic reasoning stages.
        spintaxer_model / spintaxer_reasoning: model + reasoning effort
            for the per-block creative stage.
        max_retries_per_block: how many times a single block may be
            regenerated after a QA DIVERSITY failure before the runner
            raises ``PipelineStageError``.
        tolerance / tolerance_floor: lint length-tolerance bounds. Used
            for the per-block lint check inside the spintaxer (each
            block's variants must fit within these bounds OR a retry is
            triggered with the lint error fed into the prompt). Must
            match what the caller passes to the final ``lint()`` pass on
            the assembled body, otherwise the per-block check is
            inconsistent with the post-pipeline check.
        lint_max_retries_per_block: per-block lint retry budget,
            forwarded to ``spintax_one_block``. ``0`` disables the
            retry loop. Default 2.
        on_api_call: optional callback receiving ``response.usage`` from
            every LLM call, forwarded to ``call_llm_json``.

    Returns:
        ``(AssembledSpintax, PipelineDiagnostics)``. The diagnostics
        reflect the final successful run, including retry counts.

    Raises:
        ``PipelineStageError`` from any stage, or
        ``PipelineStageError(ERR_BLOCK_SPINTAX)`` when retries exhaust.
    """
    # Lazy import: keeps the heavy alpha qa module out of the import path
    # for tests that mock or skip the validator stage.
    from app.qa import qa as run_qa

    # ------------------------------------------------------------------
    # Stages 1+2: splitter || profiler in parallel
    # ------------------------------------------------------------------
    splitter_coro = split_email(
        plain_body, model=splitter_model, on_api_call=on_api_call
    )
    profiler_coro = profile_email(
        plain_body, model=profiler_model, on_api_call=on_api_call
    )
    (block_list, splitter_diag), (profile, profiler_diag) = await asyncio.gather(
        splitter_coro, profiler_coro
    )

    # ------------------------------------------------------------------
    # Stage 3: synonym pool
    # ------------------------------------------------------------------
    pool, pool_diag = await generate_synonym_pool(
        block_list, profile, model=pool_model, on_api_call=on_api_call
    )

    # ------------------------------------------------------------------
    # Stage 4: per-block spintaxer (internally parallel)
    # ------------------------------------------------------------------
    initial_variants, spintaxer_diag = await spintax_all_blocks(
        block_list,
        pool,
        profile,
        model=spintaxer_model,
        reasoning_effort=spintaxer_reasoning,
        on_api_call=on_api_call,
        platform=platform,
        tolerance=tolerance,
        tolerance_floor=tolerance_floor,
        max_lint_retries=lint_max_retries_per_block,
    )

    # spintax_all_blocks sorts by numeric block_id suffix, which matches
    # the splitter's natural order (block_1, block_2, ..., block_N). So
    # variant_sets[i] corresponds to block_list.blocks[i]. We need a
    # mutable list for retry surgery.
    variant_sets: list[VariantSet] = list(initial_variants)

    # ------------------------------------------------------------------
    # Stage 5: assembler (sync)
    # ------------------------------------------------------------------
    assembled = assemble_spintax(block_list, variant_sets, platform=platform)

    # ------------------------------------------------------------------
    # Stage 6: validators with per-block retries
    # ------------------------------------------------------------------
    blocks_retried_total = 0
    max_retry_count_for_any_block = 0
    retry_count_by_block_id: dict[str, int] = {
        vs.block_id: 0 for vs in variant_sets
    }

    # Char lengths feed _failing_block_indices so short blocks get the
    # relaxed diversity floor. Computed once because block_list doesn't
    # change shape across retries (only variant_sets get patched).
    block_char_lengths = [len(b.text) for b in block_list.blocks]

    qa_result = run_qa(assembled.spintax, plain_body, platform)

    while not qa_result["passed"]:
        failing = _failing_block_indices(qa_result, block_char_lengths)
        if not failing:
            # The failure is NOT block-localized (e.g. block-count mismatch
            # because the splitter went per-sentence while alpha QA counts
            # per-paragraph; greeting check; v1 fidelity; duplicate variants).
            # Per-block retry can't help. Mirror alpha's behaviour and ship
            # the assembled spintax with qa_passed=False rather than raising;
            # the caller surfaces qa.errors / qa.warnings on /api/status so
            # the operator sees what's wrong without losing the body.
            break

        # Filter to blocks that still have retry budget.
        retryable: list[int] = []
        exhausted: list[int] = []
        for b_idx in failing:
            if b_idx >= len(block_list.blocks):
                continue  # defensive: out-of-range index from QA
            block_id = block_list.blocks[b_idx].id
            if retry_count_by_block_id.get(block_id, 0) < max_retries_per_block:
                retryable.append(b_idx)
            else:
                exhausted.append(b_idx)

        if not retryable:
            # Every failing block has used up its retry budget.
            # Ship the best-effort assembled spintax with qa_passed=False
            # rather than raising. This mirrors the non-localized-failure
            # branch above and matches alpha's "ship and surface errors"
            # semantics: the operator sees the body, sees the qa.errors /
            # qa.warnings, and sees retry counts in the diagnostics so
            # they can judge severity. Raising here lost the body and
            # cost the user the full per-block spend with nothing to
            # inspect (observed during prompt-tightening live test, blocks
            # [0, 6] of the funding-niche email after 2 retries each).
            break

        # Run all retryable blocks in parallel.
        async def _retry_block(b_idx: int) -> tuple[int, VariantSet]:
            b = block_list.blocks[b_idx]
            entry = pool.blocks.get(b.id)
            new_vs = await spintax_one_block(
                b,
                entry,
                profile,
                model=spintaxer_model,
                reasoning_effort=spintaxer_reasoning,
                on_api_call=on_api_call,
                platform=platform,
                tolerance=tolerance,
                tolerance_floor=tolerance_floor,
                max_lint_retries=lint_max_retries_per_block,
            )
            return b_idx, new_vs

        retry_results = await asyncio.gather(
            *[_retry_block(b_idx) for b_idx in retryable]
        )

        for b_idx, new_vs in retry_results:
            # Sanity check: positions must still align between block_list
            # and variant_sets after our previous edits.
            if variant_sets[b_idx].block_id != block_list.blocks[b_idx].id:
                raise PipelineStageError(
                    ERR_BLOCK_SPINTAX,
                    detail=(
                        f"variant_sets/block_list ordering mismatch at "
                        f"index {b_idx}: "
                        f"vs.block_id={variant_sets[b_idx].block_id!r}, "
                        f"block.id={block_list.blocks[b_idx].id!r}"
                    ),
                )
            variant_sets[b_idx] = new_vs
            block_id = block_list.blocks[b_idx].id
            retry_count_by_block_id[block_id] += 1
            blocks_retried_total += 1
            max_retry_count_for_any_block = max(
                max_retry_count_for_any_block,
                retry_count_by_block_id[block_id],
            )

        # Re-assemble + re-validate with the patched variant sets.
        assembled = assemble_spintax(block_list, variant_sets, platform=platform)
        qa_result = run_qa(assembled.spintax, plain_body, platform)

    # ------------------------------------------------------------------
    # Final diagnostics: bake retry counts into block_spintaxer stage.
    # ------------------------------------------------------------------
    final_spintaxer_diag = BlockSpintaxerDiagnostics(
        blocks_completed=spintaxer_diag.blocks_completed,
        blocks_retried=blocks_retried_total,
        max_retries_per_block=max_retry_count_for_any_block,
        p95_block_duration_ms=spintaxer_diag.p95_block_duration_ms,
    )

    diagnostics = PipelineDiagnostics(
        pipeline="beta_v1",
        splitter=splitter_diag,
        profiler=profiler_diag,
        synonym_pool=pool_diag,
        block_spintaxer=final_spintaxer_diag,
    )

    return assembled, diagnostics
