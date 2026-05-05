# Diversity Retry Fix — V2 Proposal

**Status:** Design consensus reached by 4-agent team (prompt-engineer, harness-architect, data-analyst, critic). Implementation pending.
**Scope:** Replace the broken diversity retry mechanism that made block scores WORSE on the first real test (block 1 went from 0.479 to 0.083).
**Confidence:** MEDIUM-HIGH (per team consensus)
**Author note:** V3 prompt f-string was lost to context compaction during the team debate. Below is a reconstruction by the main session from the team's documented design constraints.

---

## 1. Root Cause

The diversity retry is failing because of a **cascade between the drift gate and the diversity retry**:

1. The outer drift loop runs to exhaustion FIRST, hammering "synonym swaps only, V1 word-for-word identical" into the model's working memory across up to 3 revisions.
2. The diversity retry then fires INTO that poisoned context. The model continues producing minimum-vocabulary edits — exactly what the gate is built to catch.
3. After the diversity retry produces new output, drift checks fire AGAIN on the new output and may revise it back toward "synonym swaps only," undoing any structural diversity gained.

The two QA axes are orthogonal in principle:
- **Drift** is a vocabulary-only filter (len>=4 content words, threshold 4 net-new tokens) — see `_extract_drift_warnings` at `spintax_runner.py:1295-1306`.
- **Diversity** is a lexical+structural form measure (Jaccard over len>=3 tokens, plus pairwise block similarity) — see `app/qa.py`.

The model CAN restructure sentences (voice change, clause reorder, question form) WITHOUT introducing new len>=4 vocabulary — satisfying both gates simultaneously. But V1's loop architecture prevented the model from accessing that orthogonality.

**Empirical evidence:** in `benchmark/local_e2e.json`, passing jobs show `drift_revisions=0` AND `qa_passed=true` co-occurring. The dual-gate path IS achievable when context isn't poisoned.

---

## 2. Architectural Fix — Per-Block Sub-Call

Replace the existing whole-document diversity retry loop (`spintax_runner.py:1586-1617`) with per-block sub-calls. Each failing block gets its own clean-context sub-call, isolated from the drift conversation.

### 2.1 New helper: `compute_failing_blocks_from_errors`

Auto-inherits the CTA pair-floor carve-out because `qa.py:600` only emits pair-floor errors for non-CTA blocks.

```python
import re

def compute_failing_blocks_from_errors(qa_result: dict[str, Any]) -> list[int]:
    """Return 0-indexed block indices that have diversity-related errors.

    Reads from qa_result['errors'] (NOT block scores) so the CTA pair-floor
    carve-out in qa.py:600 is auto-inherited (CTA blocks never appear here
    when they fail only the pair floor).
    """
    failing = set()
    pattern = re.compile(r"^block (\d+)\b")
    for err in qa_result.get("errors", []):
        if "diversity below floor" not in err and "pairwise diversity below floor" not in err:
            continue
        m = pattern.match(err)
        if m:
            failing.add(int(m.group(1)) - 1)  # qa.py uses 1-indexed; we use 0-indexed
    return sorted(failing)
```

### 2.2 New helper: `revert_single_block`

Two-direction post-revert invariant. On corruption, the caller ships the pre-retry body wholesale (P6 fallback).

```python
class SpliceCorruptionError(RuntimeError):
    """Raised when revert_single_block detects unintended mutations."""

def revert_single_block(
    post_body: str,
    pre_body: str,
    block_idx: int,
    platform: str,
) -> str:
    """Replace block_idx in post_body with the corresponding block from pre_body.

    Verifies invariants in both directions:
    1. The reverted block matches pre_body's block exactly
    2. All OTHER blocks in post_body remain untouched

    Raises SpliceCorruptionError if either invariant fails. Caller should
    fall back to shipping pre_body wholesale.
    """
    pre_blocks = extract_blocks(pre_body, platform)
    post_blocks = extract_blocks(post_body, platform)
    target_inner = pre_blocks[block_idx][1]

    new_body = reassemble(post_body, {block_idx: target_inner}, platform)
    new_blocks = extract_blocks(new_body, platform)

    if new_blocks[block_idx][1] != target_inner:
        raise SpliceCorruptionError(
            f"revert of block {block_idx} did not restore pre-retry content"
        )
    for i, (_, post_inner) in enumerate(post_blocks):
        if i == block_idx:
            continue
        if new_blocks[i][1] != post_inner:
            raise SpliceCorruptionError(
                f"revert of block {block_idx} corrupted block {i}"
            )
    return new_body
```

### 2.3 `joint_score` with block-length-scaled drift clamp

Long body blocks (14-18 content words) with drift_count=6+ otherwise floor `drift_inverse` to 0, causing always-revert on exactly the blocks the V2 path is meant to recover.

```python
def joint_score(diversity_avg: float, drift_count: int, content_word_count: int) -> float:
    """Combined drift+diversity score for revert decisioning.

    The drift inverse is scaled by block length so long body blocks aren't
    unfairly penalized vs short CTA/p.s. blocks.
    """
    drift_denom = max(5, content_word_count // 2)
    drift_inverse = max(0.0, 1.0 - drift_count / drift_denom)
    return 0.7 * diversity_avg + 0.3 * drift_inverse
```

### 2.4 Constants

```python
MAX_DIVERSITY_RETRIES = 1               # locked conservative; bump to 2 in V1.1 if pass rate is too low
MAX_RETRY_COST_USD = 4.00               # hard ceiling per job (proportional formula in V2.1)
MIN_REMAINING_BUDGET_FOR_RETRY = 0.50   # skip retry if budget below this
```

### 2.5 Block ID parse-time normalization

Guards against silent no-op when the model returns string keys but the splicer expects ints:

```python
parsed_revisions = {
    k.strip(): v
    for k, v in raw_response.items()
    if k.strip() in {str(i) for i in failing_block_indices}
}
if not parsed_revisions:
    revision_attempts += 1
    continue  # don't splice, don't revert, just count the attempt
```

### 2.6 Pre-loop cost cap enforcement

Check cost cap **BEFORE** the sub-call loop starts, not after. Partial runs (some blocks improved, others not) confuse P6 revert logic.

```python
estimated_block_cost = 0.05  # tune from telemetry
remaining = MAX_RETRY_COST_USD - cost_box[0]
if remaining < len(failing_blocks) * estimated_block_cost:
    logging.warning(
        "spintax_runner: skipping diversity retry, insufficient budget "
        "(need ~%.2f, have %.2f)",
        len(failing_blocks) * estimated_block_cost,
        remaining,
    )
    # Skip retry; ship pre-retry body
```

---

## 3. New Prompt — `_build_diversity_revision_prompt`

The team's design constraints for the prompt:

- **Per-block scope** — model receives ONE block at a time, not the whole email body
- **Three-lever priority** — structural > lexical > combined; `lexical_swap` dropped from standalone enum
- **Strategy selection forced as JSON output field** — for auditability
- **Worked examples use abstract placeholders** (`{{company_name}}`, `{{trigger_event}}`, `{{time_period}}`, `{{outcome_metric}}`) — concrete domain tokens dropped to prevent imitation bleed
- **40-50% relative swap percentage target** — handles the bimodal block-length distribution without separate rules

Below is the V3 prompt reconstructed from these constraints:

```python
def _build_diversity_revision_prompt(
    block_v1: str,
    block_variants: list[str],
    block_score: float,
    block_pairwise_diagnostics: list[str],
    block_position: int,
    platform: str,
) -> str:
    """Build a per-block diversity-revision prompt.

    Sent to the model in a CLEAN sub-call (no drift conversation history,
    no other-block context). Returns instructions to revise V2-V5 of a
    single block to clear the diversity floor.

    The clean context is the load-bearing fix: without the drift loop's
    'synonym swaps only' instructions in working memory, the model is
    free to pick structural revisions.
    """
    diagnostics_block = "\n".join(f"  - {d}" for d in block_pairwise_diagnostics)
    variants_block = "\n".join(
        f"  V{i + 2}: {v}" for i, v in enumerate(block_variants)
    )

    return (
        f"You are revising one paragraph of a cold email to fix a diversity "
        f"failure. The block scored {block_score:.2f} Jaccard distance "
        f"average, below the 0.30 floor. Your variants 2-5 read as near-"
        f"duplicates of V1.\n\n"
        f"BLOCK POSITION: {block_position} (1=greeting, 2=opener, "
        f"middle=body, last-1=CTA, last=signature/p.s.)\n"
        f"PLATFORM: {platform}\n\n"
        f"V1 (must be preserved word-for-word):\n  {block_v1}\n\n"
        f"Your previous V2-V5 (failing):\n{variants_block}\n\n"
        f"Pairwise issues:\n{diagnostics_block}\n\n"
        f"---\n\n"
        f"REVISION STRATEGY — pick ONE per variant, in priority order:\n\n"
        f"1. **structural** (PREFERRED): Change sentence shape. Voice shift "
        f"(active<->passive), clause reorder, statement<->question flip, "
        f"lead with object instead of subject, split into two clauses, "
        f"merge two clauses. The CONTENT stays the same; the SHAPE "
        f"changes.\n\n"
        f"2. **lexical**: Swap individual content words for synonyms while "
        f"preserving sentence shape. Use only if structural change is not "
        f"viable for this block (e.g., one-clause greeting).\n\n"
        f"3. **combined**: Both shape change AND synonym swaps. Most "
        f"variation per variant. Use sparingly — high risk of drift.\n\n"
        f"---\n\n"
        f"REVISION RULES — non-negotiable:\n\n"
        f"1. V1 must remain word-for-word identical to what's shown above.\n"
        f"2. Aim for **40-50% relative word change** between V1 and each of "
        f"V2-V5 (after stopwording short function words). NOT 80-90% same; "
        f"NOT 100% different. Mid-range diversity reads as natural rewriting.\n"
        f"3. Across V2-V5, use AT LEAST 2 different strategies. If you "
        f"only do synonym swaps, the gate will fail again.\n"
        f"4. Do NOT invent new concepts (no drift). The model output is "
        f"checked separately for content drift; new ideas/stakeholders/"
        f"time horizons not in V1 are forbidden.\n"
        f"5. All `{{{{variables}}}}` preserved exactly with double-brace "
        f"syntax.\n\n"
        f"---\n\n"
        f"WORKED EXAMPLES (abstract placeholders to prevent imitation bleed):\n\n"
        f"INPUT V1: \"At {{{{company_name}}}}, {{{{trigger_event}}}} happened "
        f"in {{{{time_period}}}} and we saw {{{{outcome_metric}}}}.\"\n\n"
        f"GOOD V2 (structural — clause-first reorder):\n"
        f"  \"{{{{outcome_metric}}}} came after {{{{trigger_event}}}} at "
        f"{{{{company_name}}}} in {{{{time_period}}}}.\"\n\n"
        f"GOOD V3 (combined — voice flip + synonym):\n"
        f"  \"In {{{{time_period}}}}, {{{{trigger_event}}}} drove "
        f"{{{{outcome_metric}}}} for {{{{company_name}}}}.\"\n\n"
        f"GOOD V4 (lexical — pure synonyms):\n"
        f"  \"{{{{company_name}}}} hit {{{{outcome_metric}}}} once "
        f"{{{{trigger_event}}}} occurred during {{{{time_period}}}}.\"\n\n"
        f"BAD V5 (single verb swap — what the gate catches):\n"
        f"  \"At {{{{company_name}}}}, {{{{trigger_event}}}} took place "
        f"in {{{{time_period}}}} and we saw {{{{outcome_metric}}}}.\"\n"
        f"  ← only 'happened'->'took place'; ~92% word overlap; FAILS gate.\n\n"
        f"---\n\n"
        f"OUTPUT FORMAT (JSON):\n\n"
        f"{{\n"
        f'  "v2": "<revised variant>",\n'
        f'  "v3": "<revised variant>",\n'
        f'  "v4": "<revised variant>",\n'
        f'  "v5": "<revised variant>",\n'
        f'  "strategies": ["structural", "combined", "structural", "lexical"]\n'
        f"}}\n\n"
        f"`strategies` must be a 4-element array; one of structural / "
        f"lexical / combined per variant; AT LEAST 2 distinct values.\n\n"
        f"Produce the JSON now. No prose before or after."
    )
```

### Drift prompt minor wording fix

`_build_drift_revision_prompt` in `spintax_runner.py:1309-1345` re-anchors the model on synonym-only behavior with the line:

> "Restructure or swap synonyms only."

Replace with language that doesn't pre-emptively shut down structural variation:

```python
        f"2. Use synonym swaps OR sentence-shape changes (voice, clause "
        f"order, question form). Do NOT add new framings, stakeholders, "
        f"time horizons, or actors.\n"
```

This is a 1-line wording fix at `spintax_runner.py:1337-1338`. It doesn't change drift's vocabulary check; it just stops the prompt from biasing the model AWAY from the structural diversity that the diversity gate needs.

---

## 4. Confidence + Invalidators

**Confidence: MEDIUM-HIGH**

This fix is invalidated if:
1. **Test criterion 5 fails** ("no new drift warnings introduced: drift_count_post <= drift_count_pre"). If the per-block sub-calls genuinely decouple diversity from drift, this should be trivially satisfiable. If not, the cascade is still present in subtler form.
2. **P6 revert rate exceeds 50%** of blocks across test runs. The retry is net-negative — costs tokens for no improvement. Kill switch.
3. **>30% of blocks pick `combined` strategy with no detectable structural change.** Prompt re-anchoring is still live; needs further hardening.
4. **The 4 passing benchmark jobs were structurally easier than failing recovery cases.** No per-block token-diff analysis was run on a FAILING job (corpus_avg=0.083) to prove the dual-gate path holds in the recovery case. Acknowledged sampling bias.

---

## 5. Logged Risks (NOT blocking ship)

1. **Sampling bias on empirical evidence.** Data-analyst's "drift_count=2-3 per variant, never 4+" came from PASSING benchmark jobs. The hard recovery case (corpus_avg=0.083 -> 0.45) was not directly validated. Mitigation: instrument P6 revert rate as first-class metric; if revert rate > 50% on real recovery cases, pull V2 and recalibrate `_DRIFT_WORD_THRESHOLD` from 4 to 6.
2. **Inter-block semantic coherence.** Per-block revision can produce locally-correct, globally-incoherent blocks (opener promises X, CTA delivers Y). Inherent to per-block architecture. Mitigation: pass surrounding blocks as read-only context in sub-call prompt (not implemented in V2; defer to V2.1 if needed). Monitor via human QA spot-check on first 20 production retries.
3. **`_DRIFT_WORD_THRESHOLD=4` may flag valid diverse revisions.** V2 removes the cascade that made this catastrophic in V1, so this is now an accuracy issue (not correctness). Recalibrate to 6 if test 5 fails.
4. **$4.00 retry cost ceiling is 13-40x typical job cost.** Internally consistent but absurdly high for cheap jobs. V2.1 fix: proportional formula `MAX_RETRY_COST_USD = min(4.00, max(0.50, 5 * initial_job_cost_usd))`.

---

## 6. Spec Deviation Required

`DIVERSITY_GATE_SPEC.md` Section 4.7 states "NO auto-retry on diversity failure" as a Phase B non-goal. V2 implements auto-retry. **Before promoting `DIVERSITY_GATE_LEVEL` from "warning" to "error", update Section 4.7 to retire the non-goal language and replace with the V2 retry spec.** Otherwise a future engineer reading the spec will roll back V2 as spec-violating.

---

## 7. Test Plan

Run V2 against the same email used in benchmark jobs 1+2 (the failing-recovery case with corpus_avg=0.323 -> 0.138 after broken retry; we want post-V2 corpus_avg >= 0.45).

**Acceptance criteria — ALL must hold:**

1. `min(post_scores[failing_blocks]) >= BLOCK_AVG_FLOOR (0.30)` — every failing block now passes its floor
2. `corpus_avg(post_scores) >= CORPUS_AVG_FLOOR (0.45)` — corpus-level gate holds
3. Zero `SpliceCorruptionError` events in the test run
4. At least 1 block in revision set has structural strategy change (verified from JSON `strategies` field) — confirms model is doing structural work, not synonym-swapping
5. `drift_count_post <= drift_count_pre` for all blocks — no new drift warnings introduced

**Kill criteria (across 10 test runs):**
- P6 revert rate >= 50% of blocks -> V2 is net-negative, do not promote
- Combined-strategy-without-structure rate > 30% -> prompt re-anchoring is live, harden prompt
- `SpliceCorruptionError` appears even once -> harness implementation has a bug, fix before ship

If criterion 5 fails: first check whether flagged blocks have GENUINE semantic drift or just lexical diversity. If just lexical diversity, threshold recalibration (4 -> 6) is the right answer, not pulling V2.

---

## 8. Implementation Steps

In order:

1. Add helpers `compute_failing_blocks_from_errors`, `revert_single_block`, `joint_score`, `SpliceCorruptionError` near top of `spintax_runner.py` (around line 200, before the existing helpers).
2. Add constants `MAX_RETRY_COST_USD`, `MIN_REMAINING_BUDGET_FOR_RETRY` (line ~50, with the other constants).
3. Replace `_build_diversity_revision_prompt` (lines 1372-1408) with the new per-block version above. **Note the signature change** — it now takes one block, not the whole body.
4. Apply the 1-line wording fix in `_build_drift_revision_prompt` at lines 1337-1338.
5. Replace the diversity retry block (lines 1586-1617) with per-block sub-call logic:
   - After drift loop completes, call `compute_failing_blocks_from_errors(qa_result)`
   - Check `MAX_RETRY_COST_USD` budget BEFORE entering sub-call loop
   - For each failing block: spawn a clean sub-call (new conversation, no drift history) with the new per-block prompt
   - Splice the response back via the existing `extract_blocks` + `reassemble` helpers
   - On `SpliceCorruptionError`: fall through to `revert_single_block` to ship pre-retry body
   - Re-run `qa()` on the spliced body; this becomes the final `qa_result`
6. Update unit tests in `tests/test_diversity_gate.py` and add new tests:
   - `test_compute_failing_blocks_from_errors_excludes_cta_pair_floor`
   - `test_revert_single_block_invariants_both_directions`
   - `test_joint_score_block_length_scaling`
   - `test_diversity_retry_per_block_isolation`
7. Run `pytest -x` until all tests pass.
8. Run a real test against `gpt-5.5-pro` with the same email used in benchmark jobs 1+2. Verify all 5 acceptance criteria hold.
9. Update `DIVERSITY_GATE_SPEC.md` Section 4.7 (spec deviation).
10. Commit + ship.

**Estimated effort:** 4-6 hours of careful implementation + 30-50 min for the real test run.

---

## 9. Team Process Notes

The team ran ~110 minutes, 4 agents, 3 rounds of debate. Two compactions destroyed the in-flight V3 prompt f-string text but preserved the design constraints, harness Python, and risk analysis. The prompt above is a reconstruction by the main session from those constraints.

Original team:
- **prompt-engineer** (Opus, lead synthesizer) — drafted V1/V2/V3 prompts; lost to compaction
- **harness-architect** (Opus) — three-lever priority + 5 polish notes + harness Python
- **data-analyst** (Sonnet) — bimodal block-length finding; benchmark validation
- **critic** (Sonnet) — 8-lens pre-mortem; caught FM12 cascade root cause; 4-blocker convergence

The team's design is sound. The prompt below has not been independently reviewed (the team is disbanded). Recommend a single-pass review by a fresh prompt-engineer or by Mihajlo before pasting into production.
