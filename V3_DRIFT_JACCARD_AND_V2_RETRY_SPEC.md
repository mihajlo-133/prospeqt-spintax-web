# V3 - Drift Jaccard Awareness + V2 Retry Budget + Hybrid B+D Selection

**Status:** spec, awaiting approval
**Author:** Claude (with Mihajlo)
**Date:** 2026-05-05
**Predecessors:**
- `DIVERSITY_GATE_SPEC.md` (Phase A diversity gate)
- `DIVERSITY_RETRY_FIX_PROPOSAL.md` (V2 per-block retry design)
- Session breadcrumbs: `Session_20260505_205600_gpt55_vs_o3_benchmark_complete.md`

## 1. Why

The gpt-5.5 vs o3 benchmark on the Fox & Farmer review-tracking email surfaced two independent failure modes:

1. **Drift_retry ships word-shuffle pseudo-variants.** On gpt-5.5, drift_retry produced block 6 with V4 and V5 using the *same content words as V1, just reordered* (Jaccard distance = 0.0, ~100% word overlap). Drift_retry's existing checks (length, lint, drift) all passed on these variants, so it handed off a block that was guaranteed to fail the diversity gate.
2. **V2 sub-call has no retry budget.** When a V2 sub-call produces variants that violate the length tolerance (Bug A failure mode: model ignores Rule 5 in prompt), Bug B reverts the block to its drift_retry source. On block 6 that revert *re-introduces* the 0.0-distance pseudo-variants that V2 was trying to fix.

The result: even when V2 fires correctly, a single Bug A miss on a block whose drift_retry output was bad means the email ships with a 0.0-distance variant set. The QA gate flags it loudly, and the user has to manually rework that block.

## 2. Goals

1. Drift_retry never hands off a block with V1 ↔ Vn Jaccard distance < 0.20 OR block-avg < 0.30 (matching the diversity gate's pair and block-avg floors).
2. V2 sub-call gets up to 3 retries on length failure, then a deterministic selection rule picks the best output (which may be a length-broken-but-diverse attempt, or the revert).
3. The combination eliminates the "reverted block ships 0.0-distance pseudo-variants" failure mode.
4. No regression on the existing happy paths (drift-clean + diversity-clean bodies).

## 3. Non-Goals

- Replacing the drift_retry concept-drift check. That stays.
- Replacing Bug A's prompt-side length constraint. Stays as a soft hint; defense is hard rules.
- Re-architecting the V2 sub-call schema. Same `_run_per_block_revision_subcall`, same JSON shape.
- Changing the diversity gate floors (0.20 pair, 0.30 block-avg) or the gate level promotion logic.

## 4. Workstream 1: Per-Block Jaccard Cleanup Phase

**Architecture (revised per Mihajlo, 2026-05-05 23:57):** Instead of drift_retry regenerating the whole email when Jaccard violations exist, add a NEW "Jaccard cleanup phase" that runs BETWEEN drift_retry and V2. This phase fires per-block sub-calls (same infrastructure as V2) with a Jaccard-focused prompt. Cap at 2 per block. After exhaustion, hand off to V2.

The original "drift_retry whole-email Jaccard re-prompt" design (sections 4.1-4.4 below) is preserved for reference but not implemented. See Section 4.9 for the per-block design.

### Original Design (Reference Only - Not Implemented)

### 4.1 Current Drift Loop (Reference)

`app/spintax_runner.py:1841-1942`. Pseudocode:

```
diversity_retries = 0
drift_revisions = 0
for attempt in range(MAX_DRIFT_REVISIONS + 1):  # currently 0..3
    outcome = run_tool_loop(current_user_content)
    qa_result = qa(outcome.final_body, plain_body, platform)
    drift_warnings = _extract_drift_warnings(qa_result)  # only "drift phrase" / "new content words"
    if not drift_warnings:
        break
    if attempt >= MAX_DRIFT_REVISIONS:
        unresolved_drift = drift_warnings
        break
    drift_revisions += 1
    current_user_content = _build_drift_revision_prompt(... drift_warnings ...)
```

The break condition is `not drift_warnings`. Jaccard violations live in `qa_result['errors']` (at error level) or `qa_result['warnings']` (at warning level) but are never extracted, so the loop happily exits with them present.

### 4.2 New Helper: `_extract_jaccard_warnings`

Add to `app/spintax_runner.py` next to `_extract_drift_warnings`:

```python
def _extract_jaccard_warnings(qa_result: dict[str, Any]) -> list[str]:
    """Pull diversity-related entries (pair-floor + block-avg) from BOTH
    errors and warnings, regardless of gate level.

    Returns full QA strings preserving 1-indexed block numbers. The drift
    loop merges these with drift_warnings to drive its break condition;
    _build_drift_revision_prompt formats them into the prompt.
    """
    matches: list[str] = []
    for w in qa_result.get("errors", []) + qa_result.get("warnings", []):
        if "pairwise diversity below floor" in w:
            matches.append(w)
        elif "diversity below floor (avg distance" in w:
            matches.append(w)
    return matches
```

### 4.3 Per-Block Re-prompt Counter

The user spec'd a cap of 2 Jaccard re-prompts per block. Track via dict:

```python
jaccard_reprompts_per_block: dict[int, int] = {}  # 1-indexed block -> count

# After QA, when extracting Jaccard warnings, parse 1-indexed block numbers:
for w in jaccard_warnings:
    m = re.match(r"block (\d+)", w)
    if m:
        bn = int(m.group(1))
        # Filter: only include warnings for blocks under cap.
        if jaccard_reprompts_per_block.get(bn, 0) < MAX_JACCARD_REPROMPTS_PER_BLOCK:
            jaccard_warnings_to_use.append(w)

# When we issue a drift revision that includes Jaccard warnings, increment the counter
# for each block whose warning made it into the prompt.
```

When all remaining Jaccard warnings are over-cap, the loop should exit (so V2 takes over for the remaining failing blocks).

### 4.4 Updated Loop Logic

```python
for attempt in range(MAX_DRIFT_REVISIONS + 1):
    outcome = run_tool_loop(current_user_content)
    qa_result = qa(outcome.final_body, plain_body, platform)
    drift_warnings = _extract_drift_warnings(qa_result)
    jaccard_warnings_all = _extract_jaccard_warnings(qa_result)
    # Filter to only blocks under the per-block cap
    jaccard_warnings_to_use = [
        w for w in jaccard_warnings_all
        if _block_num_for_warning(w) is not None
        and jaccard_reprompts_per_block.get(_block_num_for_warning(w), 0) < MAX_JACCARD_REPROMPTS_PER_BLOCK
    ]

    if not drift_warnings and not jaccard_warnings_to_use:
        break  # done; jaccard_warnings_all may still be non-empty (over-cap), V2 picks them up
    if attempt >= MAX_DRIFT_REVISIONS:
        unresolved_drift = drift_warnings
        unresolved_jaccard = jaccard_warnings_to_use  # NEW field
        break

    drift_revisions += 1
    current_user_content = _build_drift_revision_prompt(
        plain_body, outcome.final_body, drift_warnings, jaccard_warnings_to_use,
        platform, attempt=drift_revisions,
    )
    # Increment per-block counters for the Jaccard warnings we're about to send
    for w in jaccard_warnings_to_use:
        bn = _block_num_for_warning(w)
        if bn is not None:
            jaccard_reprompts_per_block[bn] = jaccard_reprompts_per_block.get(bn, 0) + 1
```

### 4.5 Updated `_build_drift_revision_prompt`

Add `jaccard_warnings: list[str]` parameter. New section in prompt body:

```
WORD-SET DUPLICATE WARNINGS (variations that share too many content words with V1):
- block 6 variation 4: pairwise diversity below floor (distance 0.00 < 0.2; ~100% word overlap)
- block 6 variation 5: pairwise diversity below floor (distance 0.00 < 0.2; ~100% word overlap)

For each flagged variation, you must use DIFFERENT CONTENT WORDS and a DIFFERENT
SENTENCE STRUCTURE. Do NOT just rearrange the words from V1 - the diversity check
counts shared words and ignores order. Real diversity comes from synonyms,
paraphrasing, and changing what's emphasized. Length tolerance and concept-drift
rules from above still apply.
```

If `jaccard_warnings` is empty, omit the section entirely (no behavior change for non-violating bodies).

### 4.6 New Constants

```python
# In app/spintax_runner.py near MAX_DRIFT_REVISIONS:
MAX_JACCARD_REPROMPTS_PER_BLOCK = 2  # after this, V2 takes over
```

### 4.7 New Result Fields

`SpintaxJobResult` already has `drift_unresolved`. Add:

```python
@dataclass
class SpintaxJobResult:
    ...
    jaccard_unresolved: list[str] = field(default_factory=list)
```

And mirror in `app/api_models.py:SpintaxJobResult` and `app/routes/spintax.py:_convert_result`.

### 4.9 Per-Block Jaccard Cleanup Phase (CHOSEN DESIGN)

A new phase between drift_retry exit and V2 retry entry. Runs at all gate levels (warning AND error), unlike V2 which only runs at error level.

**Pseudocode:**

```python
# In run() coroutine, AFTER drift loop exits, BEFORE V2 retry block:

# ---- NEW: Jaccard cleanup phase --------------------------------
# Detect blocks with Jaccard violations (any pair distance < BLOCK_PAIR_FLOOR
# or block-avg < BLOCK_AVG_FLOOR). For each, fire a per-block sub-call with
# a Jaccard-focused prompt. Cap at 2 attempts per block.
# ---------------------------------------------------------------
jaccard_reprompts_per_block: dict[int, int] = {}
jaccard_diags = JaccardCleanupDiagnostics()  # see 4.13

while True:
    failing_blocks = compute_jaccard_failing_blocks(qa_result)
    # Filter: only include blocks under per-block cap
    eligible = [b for b in failing_blocks
                if jaccard_reprompts_per_block.get(b, 0) < MAX_JACCARD_REPROMPTS_PER_BLOCK]
    if not eligible:
        break  # All clean, OR all over-cap (V2 will handle leftovers)

    replacements: dict[int, str] = {}
    for idx in eligible:
        # Build per-block sub-call with Jaccard-focused prompt
        prompt = _build_jaccard_cleanup_prompt(
            block_v1=v1,
            block_variants=v2_to_v5,
            offending_pairs=...,  # pairs with distance < BLOCK_PAIR_FLOOR
            overlap_words=...,    # the words that are duplicated (filtered)
            preserve_words=...,   # proper nouns + placeholders (must keep)
            tolerance=tolerance,
            tolerance_floor=tolerance_floor,
        )
        parsed = await _run_per_block_revision_subcall(...)
        # Validate output (length + Jaccard)
        # If improved: add to replacements
        jaccard_reprompts_per_block[idx] = jaccard_reprompts_per_block.get(idx, 0) + 1

    if replacements:
        new_body = reassemble(outcome.final_body, replacements, platform)
        outcome.final_body = new_body
        qa_result = qa(new_body, plain_body, platform)
    else:
        break  # No successful sub-calls this iteration; bail
```

### 4.10 New Helper: `_build_jaccard_cleanup_prompt`

Different prompt than V2's `_build_diversity_revision_prompt`. Specifically targets word-set duplication.

Key prompt sections:
1. **Context:** "Variations V<n> share <X>% of content words with V1. The diversity gate counts shared words ignoring order, so reordering is not enough."
2. **Specific overlap data:** "These words appear in both V1 and V<n>: [list]. To clear the diversity floor, replace at least <K> of them with synonyms or paraphrases."
3. **Preserve list:** "These must stay exact across all variations: [proper nouns from V1 + all `{{placeholders}}` and `{VARS}`]. Do not paraphrase or replace these."
4. **Length rule:** Same band-based language as Bug A (5% inner / 12% outer per Question B).
5. **Output format:** Same JSON schema as V2 sub-call (`v2`, `v3`, `v4`, `v5`, `strategies`).

**Proper-noun detection (heuristic):** Extract V1 tokens; mark token as "preserve" if:
- Token contains a `{{...}}` or `{VAR}` pattern, OR
- Token is capitalized AND not the first word of the sentence (avoid sentence-initial false positives), OR
- Token is part of a multi-word phrase where adjacent tokens are also capitalized (e.g., "Fox & Farmer", "Google", "5-stars" stays as a phrase).

This is approximate. The model still has the V1 text and can use judgment.

### 4.11 New Constant

```python
# In app/spintax_runner.py near MAX_DRIFT_REVISIONS:
MAX_JACCARD_REPROMPTS_PER_BLOCK = 2  # after this, V2 takes over
```

### 4.12 New Helper: `compute_jaccard_failing_blocks`

Similar to existing `compute_failing_blocks_from_errors` but works on QA's `diversity_block_scores` directly (not error strings) so it works at warning level too:

```python
def compute_jaccard_failing_blocks(qa_result: dict[str, Any]) -> list[int]:
    """Return 0-indexed block numbers with Jaccard violations.

    A block fails if either:
    - any V1<->Vn pair distance < BLOCK_PAIR_FLOOR (0.20), OR
    - block-avg V1<->Vn distance < BLOCK_AVG_FLOOR (0.30)
    Greeting/short blocks (score=None) are skipped.
    """
    block_scores = qa_result.get("diversity_block_scores", []) or []
    pair_distances_per_block = qa_result.get("diversity_pair_distances", []) or []
    failing = []
    for idx, score in enumerate(block_scores):
        if score is None:
            continue
        if score < BLOCK_AVG_FLOOR:
            failing.append(idx)
            continue
        # Check pair-floor
        pairs = pair_distances_per_block[idx] if idx < len(pair_distances_per_block) else []
        if any(d is not None and d < BLOCK_PAIR_FLOOR for d in pairs):
            failing.append(idx)
    return failing
```

This requires `qa_result` to expose per-block pair distances (currently only block-avg is exposed). **Pre-req: augment `qa.py:check_block_diversity` return signature to include pair distances per block.**

### 4.13 New Diagnostics Dataclass

```python
@dataclass
class JaccardCleanupDiagnostics:
    fired: bool = False
    blocks_attempted: list[int] = field(default_factory=list)
    sub_calls: list[JaccardSubCallRecord] = field(default_factory=list)
    blocks_at_cap: list[int] = field(default_factory=list)  # passed to V2
    cleanup_cost_usd: float = 0.0

@dataclass
class JaccardSubCallRecord:
    block_idx: int
    attempt_num: int  # 1 or 2 (per-block cap)
    outcome: str  # "improved", "no_improvement", "json_parse_error", "api_error"
    cost_usd: float
    pre_score: float
    post_score: float
```

### 4.14 Tests

- Unit: `_extract_jaccard_warnings` parses both errors and warnings correctly across gate levels
- Unit: per-block counter caps at 2 then excludes that block's warnings
- Unit: `_build_drift_revision_prompt` includes Jaccard section only when warnings present
- Integration: drift loop with all-passing body breaks immediately (no regression)
- Integration: drift loop with Jaccard-only warnings issues a revision and increments counter
- Integration: drift loop with Jaccard warnings over-cap exits and surfaces them via `jaccard_unresolved`
- Test fixture: a body where drift_retry produced Jaccard-failing variants; expect drift loop to re-prompt and shipping body to clear the floor

## 5. Workstream 2: V2 Retry Budget + Hybrid B+D Selection

### 5.1 Current V2 Logic (Reference)

`app/spintax_runner.py:2025-2145`. For each failing block: one sub-call → store `parsed['v2..v5']` → splice into body → regression revert → Bug B post-lint revert.

### 5.2 New Per-Block Retry Loop

Replace the single `_run_per_block_revision_subcall` call with a 3-attempt loop:

```python
MAX_V2_RETRIES_PER_BLOCK = 3  # so up to 4 total attempts (1 + 3 retries)

attempts: list[V2Attempt] = []  # see dataclass below
for retry_idx in range(MAX_V2_RETRIES_PER_BLOCK + 1):
    parsed = await _run_per_block_revision_subcall(...)
    attempt = _evaluate_v2_attempt(
        v1=v1,
        parsed=parsed,
        platform=platform,
        tolerance=tolerance,
        tolerance_floor=tolerance_floor,
    )
    attempts.append(attempt)
    if attempt.length_clean and not attempt.has_zero_pair:
        break  # got a clean attempt, no need to retry
    # Otherwise: retry if budget allows
```

### 5.3 New Dataclass: `V2Attempt`

```python
@dataclass
class V2Attempt:
    parsed: dict[str, Any]  # {v2, v3, v4, v5, strategies}
    cost_usd: float
    diversity_score: float  # block-avg V1<->Vn Jaccard
    length_clean: bool  # all of v2..v5 within tolerance band
    has_zero_pair: bool  # any V1<->Vn pair has Jaccard distance == 0.0
    pair_distances: list[float]  # for diagnostics
    length_violations: list[tuple[int, int, int]]  # (variant_idx, length, allowed_band)
```

### 5.4 New Helper: `_evaluate_v2_attempt`

Computes the four fields (`diversity_score`, `length_clean`, `has_zero_pair`, `pair_distances`) using `app.qa._diversity_tokens` and `_jaccard_distance`. No model call.

```python
def _evaluate_v2_attempt(
    v1: str, parsed: dict, platform: str, tolerance: float, tolerance_floor: int
) -> V2Attempt:
    variants = [parsed["v2"], parsed["v3"], parsed["v4"], parsed["v5"]]
    v1_tokens = _diversity_tokens(v1)
    pair_distances = []
    for v in variants:
        d = _jaccard_distance(v1_tokens, _diversity_tokens(v))
        pair_distances.append(d if d is not None else 1.0)
    avg = sum(pair_distances) / len(pair_distances)

    # Length check: same band logic as Bug A's prompt
    v1_len = len(v1)
    allowed_diff = max(int(round(v1_len * tolerance)), tolerance_floor)
    band_lo, band_hi = max(0, v1_len - allowed_diff), v1_len + allowed_diff
    violations = []
    for i, v in enumerate(variants):
        if not (band_lo <= len(v) <= band_hi):
            violations.append((i + 2, len(v), (band_lo, band_hi)))  # 1-indexed Vn

    return V2Attempt(
        parsed=parsed,
        cost_usd=...,  # passed in or set by caller
        diversity_score=avg,
        length_clean=not violations,
        has_zero_pair=any(d == 0.0 for d in pair_distances),
        pair_distances=pair_distances,
        length_violations=violations,
    )
```

### 5.4.1 Length Bands (Per Mihajlo's Question B Decision)

Two-tier length policy:
- **Inner band (length-clean):** existing tolerance (5%, 3-char floor). Score gets full diversity weight.
- **Outer band (length-broken-but-acceptable):** ±12%, 6-char floor. Score gets penalty multiplier.
- **Outside outer band:** disqualified.

Default symmetric (same policy for shorter and longer). New constants:

```python
LENGTH_OUTER_TOLERANCE = 0.12  # max allowed deviation in either direction
LENGTH_OUTER_FLOOR = 6  # minimum char tolerance even on short V1
LENGTH_BROKEN_PENALTY = 0.5  # multiplier when in outer band but not inner
```

Update `_evaluate_v2_attempt` to compute three flags instead of two:
```python
length_clean: bool      # within inner band
length_acceptable: bool # within outer band (includes inner)
disqualified_length: bool  # outside outer band
```

### 5.5 Hybrid B+D Selection Rule

After exhausting retries, build the candidate set: `[*v2_attempts, revert_candidate]`.

The `revert_candidate` is built by treating the original drift_retry block's V2-V5 as a "parsed dict" and running `_evaluate_v2_attempt` on it. Same scoring fields.

```python
def _select_winner(candidates: list[V2Attempt]) -> V2Attempt | None:
    """Hybrid B+D selection.

    - Disqualify any candidate with has_zero_pair=True.
    - Among non-disqualified: score = diversity_score * (1.0 if length_clean else 0.5).
    - Pick highest score. Tie-break by length_clean preference, then highest diversity.
    - If all disqualified, return None (caller falls back to revert + warn).
    """
    eligible = [a for a in candidates if not a.has_zero_pair]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda a: (
            a.diversity_score * (1.0 if a.length_clean else 0.5),
            a.length_clean,  # tie-break: prefer length-clean
            a.diversity_score,  # then prefer higher raw diversity
        ),
    )
```

### 5.6 Splice Path

If `_select_winner` returns:
- A V2 attempt: splice that into `replacements[idx]` (existing splice/reassemble code).
- The revert candidate: don't splice (don't include `idx` in `replacements`); the original block stays.
- `None` (all disqualified): don't splice + log loud warning + record in diags as new revert reason `all_disqualified`.

### 5.7 New Diagnostic Fields

Augment `DiversitySubCallRecord`:

```python
@dataclass
class DiversitySubCallRecord:
    block_idx: int
    outcome: str  # "success", "json_parse_error", "api_error", "skipped_short_block", "all_attempts_disqualified"
    cost_usd: float
    strategies: list[str] = field(default_factory=list)
    error_msg: str | None = None
    # NEW:
    attempts: int = 1  # total attempts including retries (1..4)
    selected_attempt_idx: int | None = None  # which attempt was selected (0-indexed); None if revert chosen
    selected_was_length_clean: bool | None = None
    selected_diversity_score: float | None = None
    all_attempts_diversity: list[float] = field(default_factory=list)  # for analysis
```

Augment `DiversityRevertRecord` reasons: add `"all_disqualified"`.

### 5.8 Splice/Lint/QA Pipeline Unchanged

The post-splice steps stay the same:
- Reassemble body
- Re-run QA on spliced body
- Per-block regression revert (post < pre - 0.05) — still triggers
- Bug B post-lint revert — still triggers (defense in depth, but should fire much less because winner selection already prefers length-clean)

The selection rule is upstream of these; the existing safety nets remain.

### 5.9 Tests

- Unit: `_evaluate_v2_attempt` correctly computes fields on synthetic inputs (length-broken, zero-pair, normal)
- Unit: `_select_winner` picks length-clean over length-broken at equal diversity
- Unit: `_select_winner` picks length-broken-high-diversity over length-clean-low-diversity per composite formula
- Unit: `_select_winner` returns None when all candidates have zero-pair
- Unit: revert candidate scoring uses the same evaluation function as V2 attempts
- Integration: 3 retries exhaust on a block where model never honors length, winner selection picks the most diverse attempt
- Integration: first retry succeeds, no further retries fire (cost discipline)
- Fixture: block 6 scenario (drift_retry produces 0.0-pair variants); winner selection picks a V2 attempt over the revert; final body clears the diversity gate

## 6. Implementation Order

1. **Workstream 1** (drift_retry Jaccard) - root cause, biggest impact. Validate on the test email.
2. **Workstream 2** (V2 retry + selection) - layered on top. Validate on the same email; expect V2 to fire less often after Workstream 1.
3. Re-test gpt-5.5 and o3 on the Fox & Farmer email with both workstreams in place. Expect: lint_passed=True, qa.passed=True at error level, reverted_blocks=0 ideally.
4. If error-level QA passes consistently, commit the entire stack (Bug A + Bug B + V2 + observability + V3) as one PR. Then promote `DIVERSITY_GATE_LEVEL=error` on Render.

## 7. Constants Summary

Existing (unchanged):
- `MAX_DRIFT_REVISIONS = 3`
- `MAX_DIVERSITY_RETRIES = 1`
- `MAX_RETRY_COST_USD`, `MIN_REMAINING_BUDGET_FOR_RETRY`, `ESTIMATED_BLOCK_RETRY_COST_USD`

New:
- `MAX_JACCARD_REPROMPTS_PER_BLOCK = 2`
- `MAX_V2_RETRIES_PER_BLOCK = 3`
- `LENGTH_BROKEN_PENALTY = 0.5` (hybrid B scoring weight; constant for tunability)

## 8. Cost Estimate

- Workstream 1: each Jaccard re-prompt is a full-email regeneration (same cost as a drift revision: ~$0.10-0.30 per attempt depending on model). Capped at 2 per block, but in practice most emails will have 0-1 blocks needing this. Worst case +$0.60 per email.
- Workstream 2: each retry is a single sub-call (~$0.07-0.18). Capped at 3 retries per failing block. With 4 failing blocks worst case = 12 extra sub-calls = ~$1.50 worst case. In practice expect 0-1 retries per block once Workstream 1 reduces the failing-block count.
- Combined worst case: +$2.10 per email vs current. Realistic: +$0.30-0.60 per email.
- The existing budget gate (`MAX_RETRY_COST_USD`) still bounds total V2 spend; we may need to bump it.

## 9. Rollback Plan

Both workstreams are gated by constants. Setting `MAX_JACCARD_REPROMPTS_PER_BLOCK = 0` disables Workstream 1 (drift loop reverts to old behavior). Setting `MAX_V2_RETRIES_PER_BLOCK = 0` disables Workstream 2 (V2 reverts to single-shot with hybrid B+D over 1 V2 attempt + revert; this is a behavior change but a much smaller one).

For full rollback to current behavior, an env-var feature flag `V3_ENABLED=0` would short-circuit both. Decision: include the env var or rely on constants? **Default: rely on constants** for simplicity. The env var can be added later if needed.

## 10. Resolved Open Questions (Per Mihajlo, 2026-05-05 23:57)

- **Question A: Specific words in re-prompt.** RESOLVED: yes, be specific. Pass the prompt BOTH the overlapping words to change AND the proper nouns / placeholders that must stay exact. Example: "Fox & Farmer" must stay because it's a real client we helped. Heuristic = capitalized tokens + placeholder patterns. (See section 4.10.)
- **Question B: Length penalty band.** RESOLVED: two-tier policy. Inner band (5% inner / current tolerance) = no penalty. Outer band (5% to 12%) = penalty 0.5. Outside outer band (>12%) = disqualified. Symmetric (same for shorter and longer). (See section 5.4.1.)
- **Question C: All-disqualified fallback.** RESOLVED: ship the original drift_retry block + log loud warning. Don't fail the job.

## 11. Implementation Order (Confirmed: Option A - Drift First)

1. **Phase 3 prep (next session):** test fixtures for the Fox & Farmer block 6 scenario. Synthetic spintax body where drift_retry produced a 0.0-pair Jaccard violation. Used to smoke-test Workstream 1 in isolation.
2. **Workstream 1 implementation (Jaccard cleanup phase):**
   - Augment `qa.check_block_diversity` to expose per-block pair distances
   - Add `compute_jaccard_failing_blocks` helper
   - Add `_build_jaccard_cleanup_prompt` with proper-noun preserve list
   - Add `JaccardCleanupDiagnostics` dataclass + record types
   - Wire phase between drift_retry exit and V2 entry in `run()`
   - Unit tests for each helper
   - Integration test on the Fox & Farmer fixture
3. **Live re-test on Fox & Farmer email** (gpt-5.5 + o3) with Workstream 1 only:
   - Expectation: V2 fires on fewer blocks, block 6 ships clean
   - Decision point: does V2 still fire at all? If not, Workstream 2 may be unnecessary
4. **Workstream 2 implementation (V2 retry budget + Hybrid B+D):**
   - Conditional on Workstream 1 results (only build if needed)
   - V2Attempt dataclass, retry loop, hybrid B+D selection
   - Unit tests + integration test
5. **Live re-test** on the same email with both workstreams
6. **Commit** Bug A + Bug B + V2 + observability + V3 (Workstream 1 + maybe 2) as one PR
