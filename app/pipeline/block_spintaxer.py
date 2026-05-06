"""Stage 4 — Block Spintaxer (parallel, per-block).

For every lockable block, generate four alternative phrasings (V2-V5)
constrained to the block's synonym pool plus locked / proper nouns plus
function words. V1 is preserved verbatim.

Two public entry points:

* ``spintax_one_block`` — runs one LLM call for one block.
* ``spintax_all_blocks`` — runs every lockable block in parallel via
  ``asyncio.gather``.

Defense-in-depth: even though the prompt asks the model to copy V1 word
for word, the function force-substitutes ``block.text`` at index 0 of
the returned variants. The orchestrator (Stage 6) handles retries on
failure; this stage fails fast on the first error.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from typing import Any

from app.lint import lint as run_lint
from app.pipeline.contracts import (
    ERR_BLOCK_SPINTAX,
    Block,
    BlockList,
    BlockPoolEntry,
    BlockSpintaxerDiagnostics,
    PipelineStageError,
    Profile,
    SynonymPool,
    VariantSet,
)
from app.pipeline.llm_client import call_llm_json

# Defaults for the per-block lint retry loop. Match app.lint.DEFAULT_TOLERANCE
# / DEFAULT_TOLERANCE_FLOOR but kept literal here to avoid import-time
# coupling - if app.lint constants drift, we want this stage to keep
# whatever tolerance the orchestrator passes in.
_DEFAULT_LINT_TOLERANCE = 0.05
_DEFAULT_LINT_TOLERANCE_FLOOR = 3
_DEFAULT_LINT_MAX_RETRIES = 2

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_SPINTAXER_PROMPT_TMPL = """\
You are a block spintaxer. You take a single block of email copy
(V1, which is one paragraph - may be one sentence or multiple
sentences that belong together) and produce 4 alternative
phrasings (V2, V3, V4, V5) that say the same thing in natural,
fluent English, using different words. Multi-sentence blocks
must be rephrased as a coherent whole; do not pick only one
sentence to rewrite.

STRICT RULES:
1. You may ONLY use words from the synonym pool below, plus function
   words (the, a, is, are, of, and, or, but, to, in, on, at, for, with,
   by, from, as, it, this, that), plus the locked nouns and proper nouns
   listed. If a content word is not in the pool and not locked, you
   cannot use it.
2. Preserve every placeholder (any token wrapped in double curly braces
   in V1) EXACTLY, including its braces.
3. Preserve every locked noun and proper noun EXACTLY.
4. V1 must be the original sentence WORD FOR WORD. Copy it.
5. V2-V5 must each differ from V1 AND from each other. Use different
   synonyms from the pool, reorder whole clauses, or change voice. Do
   NOT produce two identical variants.
6. Stay within +/- 5% of V1's character length (3-char floor on tolerance).

QUALITY RULES (every V2-V5 must satisfy ALL of these):
7. Natural English word order. Use ordinary subject-verb-object phrasing.
   NEVER invert to non-standard order such as: putting the object first
   and the verb last ("Funding we offer."), trailing a "we [verb]" tag
   after the object ("Sales funding for the audience, we present."), or
   ending on a noun phrase that should have led the sentence ("Your
   options to view, no hard pull"). Allowed reorderings move whole
   clauses (e.g. a trailing prepositional phrase moves to the front);
   they do NOT swap subject and verb position.
8. Complete sentences only. Each variant must have a subject, a finite
   verb, and a clear object when the verb requires one. No fragments.
   Forbidden example: "We deliver for the audience income-based funding."
   is wrong because the verb "deliver" has no proper object - it is a
   broken sentence with a trailing noun phrase.
9. Match V1's spelling, capitalization, and abbreviation form for any
   carried-over words.
   - If V1 says "hours", do NOT write "hrs". If V1 says "hrs", keep "hrs".
   - If V1 says "4-min", do NOT write "4min" or "4 min".
   - If V1 says "P.S.", keep "P.S." (not "PS").
   - If V1 starts with a capital letter, V2-V5 must start with a capital
     letter too.
   - If V1 begins with a placeholder followed by a capitalized word,
     V2-V5 must follow the same capitalization for the word after the
     placeholder.
10. Preserve compound domain phrases. Multi-word noun phrases that act
    as terms-of-art must be kept intact even if individual words appear
    in the synonym pool. Examples of phrases to keep verbatim from V1
    when V1 contains them: "hard pull", "credit check", "wire transfer",
    "revenue-based funding". When in doubt, keep V1's wording for the
    phrase rather than substituting a pool synonym.
11. Idiomatic, fluent English only. Forbidden:
    - Archaic constructions ("on that which you use").
    - Broken-grammar mutations ("don't got", "covering upfront cuts
      final costs").
    - Word salad from blindly slotting pool synonyms.
    If a pool synonym produces awkward English when slotted in, pick a
    different pool option or restructure the sentence.

V1 (original sentence):
{block_v1}

Synonym pool (the only content-word substitutions allowed):
{synonyms_dict_json}

Syntax options (alternative phrasings to start from):
{syntax_options_list_json}

Locked nouns (preserve exactly): {locked_nouns_list_json}
Proper nouns (preserve exactly): {proper_nouns_list_json}

Profile:
  Tone: {tone}
  Audience: {audience_hint_or_unknown}

Output JSON shape (the ONLY allowed shape):
{{
  "block_id": "{block_id}",
  "variants": ["V1 exact copy", "V2", "V3", "V4", "V5"]
}}\
"""


_LINT_FEEDBACK_TMPL = """\


LINT FEEDBACK FROM YOUR PREVIOUS ATTEMPT:
The variants you produced for this block failed these checks:
{errors_bulleted}

Address every error above and produce a fresh set of 5 variants. Specifically:
- Length errors mean a variant's character count strayed too far from V1.
  Allowed range: V1 length is {v1_len} chars; each of V2-V5 must be within
  +/- {tolerance_pct:.0f}% (or {tolerance_floor} chars, whichever is larger),
  i.e. between {min_len} and {max_len} chars inclusive.
- Banned-word errors mean a variant uses a word from the AI-cliche blocklist.
  Pick a different content word from the synonym pool above.
- Em-dash errors ("contains em-dash") mean the variant contains the "{em_dash}"
  character. Replace it with a regular hyphen "-" or restructure the sentence.
- Invisible-character errors mean the variant contains zero-width / soft-hyphen
  Unicode. Strip them. Do NOT pad lengths with invisible whitespace.

V1 is fixed and must remain word-for-word identical. Only adjust V2-V5.\
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_prompt(
    block: Block,
    pool_entry: BlockPoolEntry,
    profile: Profile,
    *,
    lint_feedback: list[str] | None = None,
    tolerance: float = _DEFAULT_LINT_TOLERANCE,
    tolerance_floor: int = _DEFAULT_LINT_TOLERANCE_FLOOR,
) -> str:
    """Render the spintaxer prompt for one block.

    If ``lint_feedback`` is non-empty, append a feedback section listing the
    lint errors from the previous attempt so the model can address them in
    the next call.
    """
    base = _SPINTAXER_PROMPT_TMPL.format(
        block_v1=block.text,
        synonyms_dict_json=json.dumps(pool_entry.synonyms, ensure_ascii=False),
        syntax_options_list_json=json.dumps(
            pool_entry.syntax_options, ensure_ascii=False
        ),
        locked_nouns_list_json=json.dumps(
            profile.locked_common_nouns, ensure_ascii=False
        ),
        proper_nouns_list_json=json.dumps(
            profile.proper_nouns, ensure_ascii=False
        ),
        tone=profile.tone,
        audience_hint_or_unknown=profile.audience_hint or "unknown",
        block_id=block.id,
    )
    if not lint_feedback:
        return base

    v1_len = len(block.text)
    allowed = max(int(round(v1_len * tolerance)), tolerance_floor)
    bulleted = "\n".join(f"  - {e}" for e in lint_feedback)
    return base + _LINT_FEEDBACK_TMPL.format(
        errors_bulleted=bulleted,
        v1_len=v1_len,
        tolerance_pct=tolerance * 100,
        tolerance_floor=tolerance_floor,
        min_len=max(0, v1_len - allowed),
        max_len=v1_len + allowed,
        em_dash="—",
    )


def _check_block_lint_errors(
    variants: list[str],
    platform: str,
    tolerance: float,
    tolerance_floor: int,
) -> list[str]:
    """Return per-block lint errors that the spintaxer can fix on retry.

    Wraps the 5 variants in a single-block spintax string, runs the
    deterministic linter, and filters the results to errors that targeting
    a single retry of THIS block can plausibly fix:

    * V1-only errors are dropped (V1 is force-substituted from ``block.text``;
      a retry can't change it).
    * EmailBison variable-casing errors are dropped (they originate from the
      input body, not the variants).
    * "no spintax blocks found" is dropped (defensive; means our wrapper
      itself wasn't recognised, which would be a bug in the wrapper not in
      the variants).
    * Spam-trigger entries are warnings, not errors, so they never appear in
      this list.
    """
    if platform == "instantly":
        body = "{{RANDOM | " + " | ".join(variants) + "}}"
    elif platform == "emailbison":
        body = "{" + "|".join(variants) + "}"
    else:
        return []

    errors, _warnings = run_lint(body, platform, tolerance, tolerance_floor)

    fixable: list[str] = []
    for err in errors:
        if "no spintax blocks found" in err:
            continue
        if "should be ALL CAPS" in err:
            continue
        # V1-only errors: "...: variation 1 contains ..." or
        # "...: variation 1 is empty". Strip these because V1 is the
        # force-substituted block.text - the model cannot change it.
        if " variation 1 " in err or err.endswith(" variation 1"):
            continue
        fixable.append(err)
    return fixable


def _validate_variants_response(
    raw: dict[str, Any], expected_block_id: str
) -> list[str]:
    """Validate the LLM's JSON shape and return the variants list.

    Raises ``PipelineStageError(ERR_BLOCK_SPINTAX, ...)`` on any
    structural problem. The caller still needs to force-substitute V1
    after this passes.
    """
    block_id = raw.get("block_id")
    if not isinstance(block_id, str) or block_id != expected_block_id:
        raise PipelineStageError(
            ERR_BLOCK_SPINTAX,
            detail=(
                f"LLM returned block_id={block_id!r}, "
                f"expected {expected_block_id!r}"
            ),
        )

    variants = raw.get("variants")
    if not isinstance(variants, list):
        raise PipelineStageError(
            ERR_BLOCK_SPINTAX,
            detail=(
                f"'variants' must be a list, got {type(variants).__name__}"
            ),
        )
    if len(variants) != 5:
        raise PipelineStageError(
            ERR_BLOCK_SPINTAX,
            detail=f"'variants' must have exactly 5 entries, got {len(variants)}",
        )

    for i, v in enumerate(variants):
        if not isinstance(v, str):
            raise PipelineStageError(
                ERR_BLOCK_SPINTAX,
                detail=(
                    f"variants[{i}] must be a string, "
                    f"got {type(v).__name__}"
                ),
            )
        if not v.strip():
            raise PipelineStageError(
                ERR_BLOCK_SPINTAX,
                detail=f"variants[{i}] is empty",
            )

    return variants


def _block_id_sort_key(block_id: str) -> tuple[int, str]:
    """Sort key that orders ``block_1 < block_2 < block_10`` numerically.

    Falls back to lexicographic on the original id for any block id that
    doesn't follow the ``block_<N>`` shape, so non-conforming ids still
    sort deterministically without raising.
    """
    if "_" in block_id:
        suffix = block_id.rsplit("_", 1)[1]
        if suffix.isdigit():
            return (int(suffix), block_id)
    return (10**9, block_id)


def _p95_ms(durations_sec: list[float]) -> int:
    """Return the 95th percentile of *durations_sec* converted to ms.

    Uses ``ceil(0.95 * N) - 1`` as the index into the sorted list so we
    behave well for the tiny N (one entry per block) where
    ``statistics.quantiles`` is awkward.
    """
    if not durations_sec:
        return 0
    sorted_secs = sorted(durations_sec)
    n = len(sorted_secs)
    # ceil(0.95 * n) gives the rank; subtract 1 for 0-based index.
    idx = max(0, min(n - 1, math.ceil(0.95 * n) - 1))
    return int(sorted_secs[idx] * 1000)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def spintax_one_block(
    block: Block,
    pool_entry: BlockPoolEntry | None,
    profile: Profile,
    *,
    model: str = "gpt-5",
    reasoning_effort: str = "high",
    on_api_call: Callable[[Any], None] | None = None,
    platform: str = "instantly",
    tolerance: float = _DEFAULT_LINT_TOLERANCE,
    tolerance_floor: int = _DEFAULT_LINT_TOLERANCE_FLOOR,
    max_lint_retries: int = _DEFAULT_LINT_MAX_RETRIES,
) -> VariantSet:
    """Generate V2-V5 for one block. V1 is preserved verbatim.

    Pure-passthrough cases (no LLM call):
      * ``block.lockable`` is False
      * ``pool_entry`` is None
      * ``pool_entry`` has zero synonyms AND zero syntax_options

    In all three cases the result is ``[block.text] * 5`` and the
    assembler is responsible for collapsing duplicate variants later.

    Lint-feedback retry: after each LLM call the function runs the
    deterministic linter on this single block's variants. If it returns
    model-fixable errors (length-tolerance, banned words, em-dash,
    invisible chars), the next attempt repeats the call with those
    errors fed back into the prompt. After ``max_lint_retries`` retries
    the function returns the final attempt without raising; the
    pipeline-level lint pass at the end of ``run_pipeline`` is what
    surfaces remaining issues to the operator.

    Args:
        block: Block from the splitter.
        pool_entry: Synonym pool entry for this block (or None for
            unlockable blocks / blocks the pool generator skipped).
        profile: Profile from the profiler (tone, locked / proper nouns).
        model: OpenAI Responses-API model name.
        reasoning_effort: "low" | "medium" | "high".
        on_api_call: Optional callback receiving ``response.usage`` for
            cost tracking.
        platform: ``"instantly"`` or ``"emailbison"`` - determines the
            spintax wrapper used when running the per-block lint check.
        tolerance: Length-tolerance fraction for the per-block lint check
            (default 0.05 = 5%).
        tolerance_floor: Minimum absolute char tolerance, protects short
            blocks (default 3).
        max_lint_retries: Maximum number of additional LLM calls after
            the first attempt fails per-block lint. ``0`` disables the
            retry (one call only). Default 2.

    Returns:
        ``VariantSet`` with exactly 5 entries; ``variants[0] == block.text``.

    Raises:
        ``PipelineStageError(ERR_BLOCK_SPINTAX, ...)`` on LLM error or
        structural validation failure (block_id mismatch, wrong variant
        count, etc.). Lint failures do NOT raise - they trigger retry
        and, if retries exhaust, the last attempt is returned.
    """
    # Step 1 - pure-passthrough: unlockable block.
    if not block.lockable:
        return VariantSet(block_id=block.id, variants=[block.text] * 5)

    # Step 2 - empty pool: lockable but nothing to vary with.
    if pool_entry is None or (
        not pool_entry.synonyms and not pool_entry.syntax_options
    ):
        return VariantSet(block_id=block.id, variants=[block.text] * 5)

    # Step 3 - retry loop. Attempt 0 is the first call (no feedback);
    # attempts 1..max_lint_retries replay with the previous attempt's
    # lint errors fed back into the prompt.
    last_lint_errors: list[str] = []
    forced: list[str] = [block.text] * 5  # placeholder until first call resolves

    for attempt in range(1 + max_lint_retries):
        prompt = _build_prompt(
            block,
            pool_entry,
            profile,
            lint_feedback=last_lint_errors if attempt > 0 else None,
            tolerance=tolerance,
            tolerance_floor=tolerance_floor,
        )

        raw = await call_llm_json(
            prompt=prompt,
            model=model,
            error_key=ERR_BLOCK_SPINTAX,
            reasoning_effort=reasoning_effort,
            on_api_call=on_api_call,
        )

        # Validate structure (block_id match, exactly 5 strings, no empties).
        variants = _validate_variants_response(raw, expected_block_id=block.id)

        # Force V1 fidelity. The prompt asks the model to copy V1, but we
        # never trust that and always overwrite index 0 with the original
        # block text. Defense-in-depth.
        forced = [block.text, variants[1], variants[2], variants[3], variants[4]]

        # Per-block lint check. If clean, ship. Otherwise feed back into
        # the next attempt's prompt.
        last_lint_errors = _check_block_lint_errors(
            forced, platform, tolerance, tolerance_floor
        )
        if not last_lint_errors:
            break

    return VariantSet(block_id=block.id, variants=forced)


async def spintax_all_blocks(
    block_list: BlockList,
    pool: SynonymPool,
    profile: Profile,
    *,
    model: str = "gpt-5",
    reasoning_effort: str = "high",
    on_api_call: Callable[[Any], None] | None = None,
    platform: str = "instantly",
    tolerance: float = _DEFAULT_LINT_TOLERANCE,
    tolerance_floor: int = _DEFAULT_LINT_TOLERANCE_FLOOR,
    max_lint_retries: int = _DEFAULT_LINT_MAX_RETRIES,
) -> tuple[list[VariantSet], BlockSpintaxerDiagnostics]:
    """Run all blocks in parallel and return ordered ``VariantSet`` list.

    Each block becomes one coroutine; the orchestrator awaits all of
    them via ``asyncio.gather`` and lets the first exception propagate.
    Output ``VariantSet`` list is sorted by the numeric suffix of the
    block id (``block_1 < block_2 < block_10``), not lexicographic.

    The ``blocks_retried`` and ``max_retries_per_block`` diagnostics
    fields stay at zero here - Stage 6 (the orchestrator) is what
    retries individual blocks on QA diversity failures and overwrites
    those fields. The per-block LINT retry that lives inside
    ``spintax_one_block`` is invisible to the diagnostics.

    Args:
        block_list: Splitter output.
        pool: Synonym pool generator output.
        profile: Profile output.
        model: OpenAI Responses-API model name.
        reasoning_effort: "low" | "medium" | "high".
        on_api_call: Optional cost-tracking callback.
        platform: ``"instantly"`` or ``"emailbison"`` - forwarded to the
            per-block lint check inside ``spintax_one_block``.
        tolerance / tolerance_floor: Length-tolerance bounds for the
            per-block lint check.
        max_lint_retries: Per-block lint retry budget (forwarded).

    Returns:
        ``(list[VariantSet], BlockSpintaxerDiagnostics)`` tuple.

    Raises:
        ``PipelineStageError(ERR_BLOCK_SPINTAX, ...)`` if any block call
        fails - the first exception from ``gather`` propagates.
    """
    blocks = list(block_list.blocks)
    durations: list[float] = []

    async def _timed_call(b: Block) -> VariantSet:
        entry = pool.blocks.get(b.id)
        t0 = time.perf_counter()
        try:
            return await spintax_one_block(
                b,
                entry,
                profile,
                model=model,
                reasoning_effort=reasoning_effort,
                on_api_call=on_api_call,
                platform=platform,
                tolerance=tolerance,
                tolerance_floor=tolerance_floor,
                max_lint_retries=max_lint_retries,
            )
        finally:
            durations.append(time.perf_counter() - t0)

    coros = [_timed_call(b) for b in blocks]
    # return_exceptions=False: first error propagates immediately.
    results: list[VariantSet] = await asyncio.gather(*coros)

    # Sort by numeric block-id suffix so block_10 lands after block_2.
    results.sort(key=lambda vs: _block_id_sort_key(vs.block_id))

    diagnostics = BlockSpintaxerDiagnostics(
        blocks_completed=len(results),
        blocks_retried=0,
        max_retries_per_block=0,
        p95_block_duration_ms=_p95_ms(durations),
    )

    return results, diagnostics
