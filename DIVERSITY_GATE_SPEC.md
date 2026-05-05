# Diversity Gate + reshape_blocks-per-block Enforcement - Implementation Spec

**Status:** Draft v3 (incorporates spec-critic round 1 review of v2)
**Target repo:** `/Users/mihajlo/Desktop/prospeqt-spintax-web`
**Files in scope:** `app/qa.py`, `app/spintax_runner.py`, `app/api_models.py`, `tests/test_qa.py`, `tests/test_routes_qa.py`, `tests/test_spintax_runner_prompt.py` (new). All paths absolute under the spintax-web repo.

**Changes vs v2:** see `## Appendix A - v2 to v3 changelog` at the bottom. The major v3 shifts are: (a) shipping the gate as **warning-only on Day 1** with a measurable promotion criterion; (b) accepting that prompt nudges are weak and reframing Phase B as defensive prompt cleanup with the gate as the real enforcement; (c) special-casing CTA blocks because the standard tokenizer scores them poorly for principled reasons.

---

## 1. Background

On 2026-05-03 we ran two production tests of the same law-firms review-collection email through `https://prospeqt-spintax.onrender.com`: medium effort ($1.04, 16.8 min) and high effort ($1.92, 20.5 min). Both passed mechanical lint and the existing `app/qa.py` checks. We then audited each output against the v2 spintax skill at `tools/prospeqt-automation/skills/claude-code-spintax-v2/`, which mandates "60-80% different per variation" and explicit use of both Strategy A (synonym swaps) and Strategy B (sentence restructuring).

Per-block word-set Jaccard difference vs Variation 1, target 60-80% (audit data, computed by an external scorer):

| Block | Type | Medium (audit) | High (audit) |
|---|---|---|---|
| 1 | Greeting | 62% (whitelist) | 62% (whitelist) |
| 2 | Notice | 43% | 48% |
| 3 | Pain Q | 44% | **21%** |
| 4 | Pitch | 23% | **10%** |
| 5 | CTA | 71% | 58% |
| 6 | P.S. | 22% | 19% |

**Empirical replay under this spec's tokenizer** (Section 4.4 rules, run during v3 review on `/tmp/medium_run_body.md` and `/tmp/high_run_body.md`):

| Block | Type | Medium avg | Medium min | High avg | High min |
|---|---|---|---|---|---|
| 1 | Greeting | exempt | exempt | exempt | exempt |
| 2 | Notice | (block missing in extract) | - | - | - |
| 3 | Pain Q | 0.369 | 0.182 | 0.169 | 0.000 |
| 4 | Pitch | 0.244 | 0.062 | 0.092 | 0.000 |
| 5 | CTA | 1.000 | 1.000 | **0.667** | **0.000** |
| 6 | P.S. | 0.174 | 0.000 | 0.205 | 0.000 |

Two material findings from the empirical replay that did not exist in v2:

1. **The high run's block 5 (CTA) hits a pair distance of 0.000** - V1="Would you be curious to hear more?" vs V2="Curious to hear more about this?" both reduce to `{"curious", "hear"}` after `len >= 3` filtering and stopword removal of `{would, you, be, to, more, about, this}`. This is a **structural artifact**: CTAs share question-frame words that get stopworded, so identical content tokens emerge from different sentence shapes. Audit said 58% (using a less aggressive tokenizer); our gate would call it 0.000.
2. **The high run's block 4 V3 hits exact 0.000** - V1="We help law firms net 48 5-star Google reviews/month for 149 bucks/month. Plus, you'd choose..." vs V3="For 149 bucks/month, we help law firms net 48 5-star Google reviews/month. Plus, you'd choose..." produce **identical token sets** because V3 just fronts the price phrase. This is the smoking-gun case the gate must catch (Section 4.8 test 9).

This empirical replay reframes the rollout strategy. Both runs miss the diversity target on 4-5 of 6 blocks AND CTA structure produces false positives under our tokenizer. Flipping the gate to error-level on Day 1 would fail every production job. v3 ships the gate **warning-only** on Day 1 with a measurable promotion criterion (Section 6.4).

The spec adds two complementary enforcements that ship as **two phases, in order**:

- **Phase A (this spec, ships first):** a deterministic diversity gate in `qa.py`. **Day 1: warning-only.** Promoted to error after the criterion in Section 6.4 is met. Provides the **measurement** layer.
- **Phase B (this spec, ships after Phase A produces production signal):** a runner-level system-prompt mandate that the agent invoke `reshape_blocks` once per spintaxable non-greeting block. **Reframed in v3 as defensive prompt cleanup, not enforcement** - the gate is the real enforcement. Provides a behavioral nudge with low confidence in its impact.

Phase A is independently valuable. Phase B is sequenced second because the gate must be measurable in production before we can tell whether the prompt nudge moved anything.

**Calibration evidence:** the v3 review re-ran the spec's tokenizer against both audit bodies. The thresholds in Section 4.2 catch all four known-bad blocks AND produce one expected false positive (high block 5 CTA). Section 4.3.2 introduces a CTA carve-out using `app.lint.is_greeting_block` as a precedent for "structurally bounded" exempt categories. The first builder task (Section 6.1) re-runs this calibration to confirm thresholds reproduce these numbers before merge.

---

## 2. Goals

After this change, the following outputs **WARN** (Day 1, warning-only rollout) and **FAIL** (Day N+, after promotion):

1. Medium law-firms run, block 4 (Jaccard ~0.244 avg, ~0.062 min) - block-avg + pair floor breach.
2. High law-firms run, block 3 (Jaccard ~0.169 avg, 0.000 min) - block-avg + pair floor breach.
3. High law-firms run, block 4 (Jaccard ~0.092 avg, 0.000 min) - block-avg + pair floor breach.
4. High law-firms run, block 6 (Jaccard ~0.205 avg, 0.000 min) - block-avg + pair floor breach.
5. The specific high block 4 V3 = V1-with-fronted-price case (token-set identical, distance exactly 0.000) - pair floor breach.

After this change, the following outputs **continue to PASS at any stage**:

1. Greeting blocks (whitelist-driven exemption, NOT score-driven).
2. CTA blocks (structurally exempt, see Section 4.3.2; this protects high block 5 from the structural false positive identified in the empirical replay).
3. Variants with `>= 0.50` average pairwise Jaccard distance per block and no individual pair below the pair floor.

**Soft goals (Phase B, not gated):**

- The model's `agent_tool_breakdown.reshape_blocks` count rises in production runs. Not a hard contract - the gate catches output failures regardless of how the model got there.

---

## 3. Non-goals

We are NOT doing any of the following:

- Shipping a synonym-shuffle / mechanical reshuffler that bypasses the model. Variant generation stays model-driven.
- Changing `reasoning_effort` defaults. (Recommendation is medium based on the audit, but ships as separate config change.)
- Modifying the public HTTP API contract beyond additive field additions on `QAResponse` and `QAResultEmbed`. No renames.
- Touching anything outside `/Users/mihajlo/Desktop/prospeqt-spintax-web/app/` and the corresponding `tests/`.
- Adding new pip dependencies. Stdlib only for `qa.py`.
- Improving the linter (`app/lint.py`).
- Refactoring agent dispatch loops (`_run_tool_loop_chat`, `_run_tool_loop_responses`, `_run_tool_loop_anthropic`).
- ~~Auto-retrying on diversity failure.~~ **RETIRED in V2 (2026-05-05).** Phase A originally shipped without retry on grounds that re-running the same prompt was unlikely to fix low-diversity output. Empirically true for the V1 design (whole-email retry inside the drift conversation). V2 replaces this with per-block sub-calls in clean context (no drift conversation history). See Section 4.7 for the V2 retry spec. Retry still gated to `DIVERSITY_GATE_LEVEL=='error'`; warning-level Day 1 deployments are unchanged.
- Adding a runtime enforcement layer that intercepts the agent's final body until every block has been through `reshape_blocks` (Option B in Section 5.1, rejected on complexity grounds).
- **NEW v3:** changing the semantics of `qa()["passed"]` for any consumer of the existing QA endpoint without a rollout window. Day-1 diversity surfaces as warnings; the `passed` boolean only changes after the promotion criterion is met (Section 6.4).
- **NEW v3:** structure-aware semantic diff (e.g., embedding similarity, parse-tree comparison). Nice-to-have, out of scope.

---

## 4. Part 1: Diversity gate in `app/qa.py`

### 4.1 Algorithm

For each spintax block:

1. **Tokenize** each variation into a set of "content tokens": lowercase, strip `{{variables}}` first (rationale in Section 4.5), regex `[A-Za-z']+`, drop tokens with `len < 3`, drop tokens in `_DIVERSITY_STOPWORDS` (Section 4.4).
2. **Compute Jaccard distance** between V1 and each of V2..V5: `jaccard_distance(A, B) = 1 - |A ∩ B| / |A ∪ B|`. **Empty-set handling:** if both A and B are empty (each variation produced 0 content tokens after stopwording), the pair is **skipped**, not scored as 0.0. If exactly one is empty, distance is `1.0`. If both have any content tokens, score normally.
3. **Block diversity score** = mean of the V1<->Vn Jaccard distances over `n in {2,3,4,5}`, excluding skipped pairs. If 0 pairs were scored (all empty), the block score is `None` and a warning is recorded.
4. **Per-block floor check:** any scored pairwise distance below `BLOCK_PAIR_FLOOR` -> diagnostic emission (warning OR error depending on `DIVERSITY_GATE_LEVEL`, Section 6.4).
5. **Per-block average check:** block score below `BLOCK_AVG_FLOOR` -> diagnostic emission (same dispatch). Skipped if block score is `None`.
6. **Corpus average check:** mean of all non-`None` block scores below `CORPUS_AVG_FLOOR` -> warning (always warning regardless of gate level).

We anchor against V1 (not all-pairs) because: the skill text says "60-80% different from Variation 1"; the linter and existing QA already treat V1 as the source of truth for fidelity; all-pairs scales O(N^2) which is wasted computation when N=5.

### 4.2 Thresholds

```python
# Hard floors. Calibrated against the 2026-05-03 audit + spec-critic empirical replay.
# See Section 1 for empirical block-by-block scores under this tokenizer.
BLOCK_AVG_FLOOR = 0.30        # mean V1<->Vn distance per block
BLOCK_PAIR_FLOOR = 0.20       # any single V1<->Vn distance
CORPUS_AVG_FLOOR = 0.45       # whole-email soft signal

# NEW v3: gate-level dispatch. Day 1 = "warning"; promote to "error"
# after the criterion in Section 6.4 is met. Stored as a module-level
# string constant (NOT env var) so it's auditable in code review.
DIVERSITY_GATE_LEVEL = "warning"  # "warning" | "error"
```

Why these numbers (calibration evidence):

- **`BLOCK_AVG_FLOOR = 0.30`** catches: medium block 4 (0.244), high block 3 (0.169), high block 4 (0.092), high block 6 (0.205). Misses: medium block 3 (0.369, audit said 44% - acceptable), high block 5 (0.667 avg, but pair floor catches it - and CTA is exempt anyway per 4.3.2).
- **`BLOCK_PAIR_FLOOR = 0.20`** catches: medium block 4 V3 (0.062), medium block 6 V3 + V4 (0.000), high block 3 V2 (0.091), high block 4 V3 (0.000), high block 6 V5 (0.000), and the spec's smoking-gun case high block 4 V3 (0.000). Required because the average can stay above 0.30 while one variant is a near-copy.
- **`CORPUS_AVG_FLOOR = 0.45`** soft-flags whole-email blandness. Both audited runs averaged ~0.40 across prose; 0.45 fires consistently for those.

These constants live as module-level globals in `qa.py`. They are tuned on N=2 production runs - we adjust after 5-10 more samples land. Documented in code comments.

### 4.3 Block-level exemptions (whitelist-driven, NOT score-driven)

#### 4.3.1 Greeting blocks

**Greeting blocks are exempt from the diversity gate.** Detection: `app.lint.is_greeting_block(variations)` - all 5 variations must match the strict whitelist `{"Hey {{firstName}},", "Hi {{firstName}},", "Hello {{firstName}},", "Hey there,", "{{firstName}},"}`.

**Justification (NEW v3, addressing critic finding #1):** the exemption is principled, not arithmetic. Under our tokenizer the 5 whitelisted greetings produce token sets `{"hey"}, {}, {"hello"}, {"hey"}, {}` - V1<->V2 = 1.0 (empty pair), V1<->V4 = 0.0 (identical). These scores are noise from the variable-stripping rule, not a meaningful diversity measurement. The skill bounds the variation set to 5 fixed strings; lexical overlap between them is structurally bounded; scoring them with Jaccard produces nonsense. Exempting via whitelist match is the only correct choice.

If a block looks like a greeting attempt but does NOT match the strict whitelist (informal greetings, case drift), the existing `_looks_like_greeting_attempt` path in `lint.py` already errors out before QA runs. Diversity scoring on a botched greeting is harmless noise - the run fails for the lint reason regardless.

#### 4.3.2 CTA blocks (NEW v3, addressing critic finding #7)

**CTA blocks (the last spintax block before the signature, when it ends with `?` in V1) are exempt from the per-pair floor and use a relaxed `BLOCK_AVG_FLOOR_CTA = 0.20`.**

**Justification:** the empirical replay showed high block 5 (CTA) hits a 0.000 pair distance under our tokenizer despite being genuinely diverse copy. V1="Would you be curious to hear more?" and V2="Curious to hear more about this?" both reduce to `{"curious", "hear"}` after stopwording removes `{would, you, be, to, more, about, this}`. The skill mandates CTA must be a question in all 5 variations (`_rules-ai-patterns.md` Section 11), so question-frame words dominate. After stopwording those out, only the action verb ("hear", "send", "know") and the object ("more", "details", "info") remain, and these often coincide across variants for legitimate copy reasons.

CTA detection (deterministic, no role-classification heuristic):
1. The block is the **last spintax block** in the output.
2. Variation 1 ends with `?` after stripping trailing whitespace and the closing block syntax.
3. Variations 2-5 all end with `?` (the linter already enforces "CTA must be a question in all 5"; we re-check defensively).

If all three conditions hold, the block is classified CTA-exempt:

- The pair floor (`BLOCK_PAIR_FLOOR = 0.20`) is **not enforced** for CTA blocks.
- The average floor uses `BLOCK_AVG_FLOOR_CTA = 0.20` instead of `BLOCK_AVG_FLOOR = 0.30`.
- The block's score is still recorded in `diversity_block_scores` (not `None`) so the operator can see the score; only the **error/warning emission** is gated by the relaxed thresholds.

This is a deliberate special case, not a generic "role-aware" framework. We only need it for CTA today. If body/proof/PS blocks turn out to need their own carve-outs, we add them one at a time with the same justification pattern.

#### 4.3.3 UNSPUN blocks

UNSPUN blocks (bullet lists, single-line variable tokens like `{{accountSignature}}`, closing signatures) are not spintax blocks - `extract_blocks()` returns 0 wrappers for them, so they never enter the diversity check.

#### 4.3.4 Short variation blocks

If a block has fewer than 2 variations, the diversity gate skips it (records `None` in `diversity_block_scores`). The existing variation-count check in lint already fails the run.

### 4.4 Stopword set

`_DIVERSITY_STOPWORDS` is a **standalone frozenset** (NOT derived from `_DRIFT_STOPWORDS`) so the two evolve independently. Practical content: a copy of `_DRIFT_STOPWORDS` plus short common words that drift's `len >= 4` rule excludes. Full list in v2 spec Section 4.4 (unchanged in v3). Code comment notes the intentional duplication.

### 4.5 Variable handling (NEW v3 explicit rationale, addressing critic finding #4)

**Variables (`{{firstName}}`, `{COMPANY}`, etc.) are stripped to a single space BEFORE tokenization.** The regex `re.sub(r"\{\{[^}]+\}\}", " ", text)` runs first; the EmailBison single-brace `{VAR}` form would need a separate regex but is not relevant to the medium/high audit data which is Instantly-format only. The builder adds a second regex `re.sub(r"\{[A-Z_][A-Z0-9_]*\}", " ", text)` to handle EmailBison.

**Why strip, not normalize?**

- **Strip** (chosen): variable becomes whitespace; downstream tokenizer ignores it. Two blocks with identical text except different variables score identically. Pro: stable across platforms (Instantly's `{{firstName}}` and EmailBison's `{FIRSTNAME}` both vanish). Con: very short blocks (e.g., a hypothetical greeting `"Hi {{firstName}},"` not in the whitelist) reduce to near-empty token sets.
- **Keep as opaque string** (rejected): variable becomes `firstName` in the token set. Pro: contributes a content token. Con: variable names are V1 anchors per `_rules-ai-patterns.md` Section 13, identical across all 5 variations; including them inflates intersection size and depresses Jaccard distance for legitimate diversity.
- **Normalize to common token** (rejected): replace all variables with `__VAR__`. Pro: variables don't inflate intersection. Con: extra logic for no clear benefit over stripping; doesn't change the score relative to stripping.

The empty-token-set policy in 4.1 step 2 is the safety net for the "very short block" downside of stripping.

### 4.6 Output shape

`qa()` return dict gains FIVE new keys (one more than v2 to expose the gate level); existing keys unchanged:

```python
{
    # Existing - unchanged
    "passed": bool,
    "error_count": int,
    "warning_count": int,
    "errors": list[str],
    "warnings": list[str],
    "block_count": int,
    "input_paragraph_count": int,

    # NEW
    "diversity_block_scores": list[float | None],
    "diversity_corpus_avg": float | None,
    "diversity_floor_block_avg": float,
    "diversity_floor_pair": float,
    "diversity_gate_level": str,             # NEW v3: "warning" | "error"
}
```

**Invariant:** `len(diversity_block_scores) == block_count` always.

**Day 1 rollout (`DIVERSITY_GATE_LEVEL = "warning"`):** diversity diagnostics go to `warnings` list. `errors` and `passed` are unchanged for diversity-only failures. `qa_passed=True` for an output that would fail-on-promotion.

**Post-promotion (`DIVERSITY_GATE_LEVEL = "error"`):** diversity diagnostics go to `errors` list. `passed=False` if any diversity error fires.

The corpus warning (CORPUS_AVG_FLOOR) is **always a warning** regardless of gate level - it's an informational signal, not a gate.

Diagnostic message templates:

```
block 4: diversity below floor (avg distance 0.23 < 0.30; variations are too similar to V1, avg word overlap 77%)
block 4 variation 3: pairwise diversity below floor (distance 0.10 < 0.20; variation reads as a near-duplicate of V1, ~90% word overlap)
corpus diversity below soft floor (avg 0.40 < 0.45; whole email reads bland - consider reshape_blocks per block)
block 3: all variations had < 2 content tokens after stopwording; cannot score diversity
```

`api_models.QAResponse` and `api_models.QAResultEmbed` both gain matching fields:

```python
diversity_block_scores: list[float | None] = Field(default_factory=list)
diversity_corpus_avg: float | None = None
diversity_floor_block_avg: float | None = None
diversity_floor_pair: float | None = None
diversity_gate_level: str | None = None  # NEW v3
```

### 4.7 Behavior when diversity fails (post-promotion)

**V2 (2026-05-05): per-block diversity retry.** Replaces the V1 "no retry" stance. The V1 design (warning-level only) shipped because the original whole-email retry produced WORSE output (block scores 0.479 -> 0.083 in benchmark job 2). Root cause was the drift loop poisoning the model's working context with "synonym swaps only, V1 word-for-word identical" instructions, then the diversity retry firing into that poisoned context. V2 fixes this with per-block sub-calls in CLEAN context.

A diversity error adds to `errors`, which means:

- `qa(...)["passed"]` becomes `False`.
- `spintax_runner.run()` triggers the V2 per-block retry path before recording the final `qa_passed` on `SpintaxJobResult`.

#### V2 retry architecture

Implemented in `app/spintax_runner.py`. Helpers:

- `compute_failing_blocks_from_errors(qa_result)` - returns 0-indexed block indices from `qa_result["errors"]`. Reads errors (not block scores) so the CTA pair-floor carve-out from `qa.py` line 600 is auto-inherited.
- `_build_diversity_revision_prompt(block_v1, block_variants, ...)` - per-block prompt with abstract worked examples (`{{company_name}}`, `{{trigger_event}}`, etc.) to prevent imitation bleed. Demands JSON output `{v2, v3, v4, v5, strategies}`.
- `_run_per_block_revision_subcall(client, model, prompt, on_api_call)` - single LLM call in CLEAN context (no system prompt, no drift conversation, no other-block context). Returns parsed JSON. Per-API dispatch (Responses, Chat, Anthropic).
- `reassemble(body, replacements, platform)` (in `app/lint.py`) - splices new inner-text into specific block positions, preserving all other blocks byte-for-byte.
- `revert_single_block(post, pre, idx, platform)` + `SpliceCorruptionError` - per-block revert when a retry made things worse. Two-direction post-revert invariant; on corruption the caller ships pre-retry body wholesale (P6 fallback).
- `joint_score(diversity_avg, drift_count, content_word_count)` - block-length-scaled drift+diversity score for revert decisioning. Long body blocks aren't penalized into always-revert vs short CTA blocks.

#### Retry flow (single pass, MAX_DIVERSITY_RETRIES=1)

1. After drift loop completes, check `qa_result["diversity_gate_level"] == "error"` and `diversity_retries < MAX_DIVERSITY_RETRIES`. If either fails, no retry.
2. `compute_failing_blocks_from_errors(qa_result)` - empty list -> no retry.
3. **Pre-loop budget check.** `MAX_RETRY_COST_USD = 4.00`, `MIN_REMAINING_BUDGET_FOR_RETRY = 0.50`. If remaining budget < `len(failing_blocks) * ESTIMATED_BLOCK_RETRY_COST_USD` or < min, skip retry. Partial runs confuse revert logic.
4. For each failing block: build per-block prompt, call `_run_per_block_revision_subcall` (clean context), parse JSON, format new inner as `" V1 | V2 | V3 | V4 | V5"`. Sub-call failures (parse error, API error) are skipped per-block and counted - they do NOT abort the whole retry.
5. `reassemble(pre_body, replacements, platform)` - splice all successful replacements at once.
6. Re-run `qa()` on spliced body.
7. **P6 per-block revert.** For each replaced block, if post-retry score < pre-retry score - 0.05 (regression threshold), revert that block via `revert_single_block`. `SpliceCorruptionError` -> ship pre-retry body wholesale.
8. Final `outcome.final_body` and `qa_result` reflect the post-revert state.

#### Constants

| Constant | Value | Rationale |
|---|---|---|
| `MAX_DIVERSITY_RETRIES` | 1 | Conservative; bump to 2 in V2.1 if pass rate too low |
| `MAX_RETRY_COST_USD` | 4.00 | Hard ceiling per job; 13-40x typical cost. V2.1 fix: proportional formula |
| `MIN_REMAINING_BUDGET_FOR_RETRY` | 0.50 | Skip retry if cumulative cost already too high |
| `ESTIMATED_BLOCK_RETRY_COST_USD` | 0.05 | Tune from telemetry; current estimate from gpt-5.5-pro Responses API |

#### Invalidators (kill V2 if any of these holds)

- P6 revert rate >= 50% of blocks across test runs - V2 is net-negative
- `SpliceCorruptionError` in production - harness bug, fix before re-shipping
- Combined-strategy-without-structure rate > 30% - prompt re-anchoring is still live, harden prompt

#### Filter compatibility

`_extract_drift_warnings()` filter is unchanged - diversity diagnostics carry distinctive prefix strings (`"diversity below"`, `"pairwise diversity"`, `"corpus diversity"`) that don't match drift filter patterns. The drift loop runs FIRST and to completion; V2 only fires after drift converges.

### 4.7b Backward compatibility statement (NEW v3, addressing critic finding #8)

`qa()` is consumed by three surfaces:
1. `app/routes/qa.py` -> POST `/api/qa` HTTP endpoint (web UI + external callers).
2. `app/spintax_runner.py` line ~1473 - calls `qa()` after the lint loop succeeds, plumbs the result into `SpintaxJobResult`.
3. `app/qa.py` `main()` - CLI entrypoint with `--json` flag.

**v3 contract:**
- New keys are additive on all three surfaces. No renames. No removals.
- Day 1 (`DIVERSITY_GATE_LEVEL = "warning"`): `passed` semantics are unchanged - a diversity-only failure has `passed=True`. Existing consumers see new warnings in the `warnings` list. No consumer breaks.
- Post-promotion (`DIVERSITY_GATE_LEVEL = "error"`): `passed` semantics change for diversity-failing outputs. **Promotion is treated as a breaking change to the runner's `qa_passed` semantics** and announced explicitly. Section 6.4 lists the rollout sequence including operator notification.

### 4.7c Exception handling (NEW v3, addressing critic finding #6)

The new `check_block_diversity()` function MUST not raise for any input that the existing extractor accepts. Defensive contract in `qa()`:

```python
try:
    diversity_errors, diversity_warnings, per_block_scores = check_block_diversity(blocks_vars)
except Exception as exc:  # noqa: BLE001 - defensive; never let diversity break QA
    diversity_errors = []
    diversity_warnings = [f"diversity check failed internally: {type(exc).__name__}: {exc}"]
    per_block_scores = [None] * len(blocks_vars)
```

The contract: a bug in `check_block_diversity` produces a single warning and falls through. It does NOT add to `errors`, does NOT change `passed`, does NOT crash the QA call. The result still has the new keys present (with `None` placeholders) so consumers don't see schema drift on a diversity bug.

Test 15 (Section 4.8) covers this with a mocked function that raises.

### 4.8 Tests to write

**File: `tests/test_qa.py`** (additions). All stdlib, no new fixtures unless inline strings.

1. **`test_diversity_all_identical_fails`** - 5 identical variations -> per-block-floor and per-pair-floor diagnostics. Day 1: in `warnings`. Post-promotion: in `errors`.
2. **`test_diversity_well_distributed_passes`** - 5 hand-crafted variations with `> 0.50` pairwise distance -> all `diversity_block_scores >= 0.50`, no diagnostics.
3. **`test_diversity_one_variant_below_pair_floor`** - 4 of 5 diverse, 1 near-copy -> `block N variation M: pairwise diversity below floor` diagnostic; avg may still pass.
4. **`test_diversity_block_below_avg_floor`** - every pair in 0.21..0.28 range -> avg-floor diagnostic; no individual pair triggers.
5. **`test_diversity_greeting_block_skipped`** - greeting block matching `is_greeting_block` -> `None` in `diversity_block_scores`, no diagnostics. **Asserts exemption is whitelist-driven, not score-driven** by also testing a contrived greeting variant set whose Jaccard scores would FAIL the floor if scored - and confirming no diagnostic fires (NEW v3, addressing critic finding #1).
6. **`test_diversity_corpus_warning_only`** - all blocks 0.32..0.40 -> warning, no error. Corpus warning always emitted as warning regardless of gate level.
7. **`test_diversity_short_variations_skipped`** - block with 1 variation -> score `None`, no diagnostic.
8. **`test_diversity_score_fields_present`** - confirm 5 new keys in return dict; types match.
9. **`test_diversity_high_run_block_4_v3_smoking_gun`** - inline V1 and V3 from the high run block 4 (NEW v3, addressing critic finding #3):
    ```python
    V1 = "We help law firms net 48 5-star Google reviews/month for 149 bucks/month. Plus, you'd choose which reviews go public - letting you block bad ones."
    V3 = "For 149 bucks/month, we help law firms net 48 5-star Google reviews/month. Plus, you'd choose which reviews go public - letting you block bad ones."
    ```
   Build a synthetic 5-variant block where V2/V4/V5 are similarly close. Assert `_jaccard_distance(_diversity_tokens(V1), _diversity_tokens(V3)) == 0.0` directly. Assert the gate emits a pair-floor diagnostic for V3.
10. **`test_diversity_medium_run_block_5_cta_passes_via_exemption`** - inline the medium block 5 CTA variants (5 questions). Assert the block scores at the CTA-exempt thresholds, no diagnostic fires. Cover both `DIVERSITY_GATE_LEVEL` values (parametrized).
11. **`test_diversity_high_run_block_5_cta_passes_via_exemption`** - inline the high block 5 CTA. Without exemption it would fail (avg 0.667 but pair 0.000). With exemption it passes. NEW v3, addressing critic finding #7 directly.
12. **`test_diversity_empty_tokens_warning`** - block where every variation reduces to 0 content tokens after stopwording -> warning emitted, score `None`, no error.
13. **`test_diversity_one_empty_one_full_pair_distance`** - confirm `_jaccard_distance(set(), {"x"}) == 1.0`.
14. **`test_diversity_invariant_block_count`** - confirm `len(result["diversity_block_scores"]) == result["block_count"]`.
15. **`test_diversity_internal_exception_isolated`** - NEW v3. Monkeypatch `check_block_diversity` to raise. Confirm `qa()` returns successfully with a `"diversity check failed internally"` warning and does NOT add to `errors`. Confirm `diversity_block_scores` is `[None] * block_count`.
16. **`test_diversity_gate_level_warning_does_not_change_passed`** - NEW v3. With `DIVERSITY_GATE_LEVEL = "warning"`, a diversity-failing block emits warnings but `passed` stays `True` (assuming no other errors). Critical for backward-compat (4.7b).
17. **`test_diversity_gate_level_error_changes_passed`** - NEW v3. With `DIVERSITY_GATE_LEVEL = "error"`, a diversity-failing block produces `passed=False`.
18. **`test_diversity_variable_stripping`** - NEW v3 (critic finding #4). Block where V1=`"Hi {{firstName}}, your account..."` and V2=`"Hi {{firstName}}, the account..."` - confirm `{{firstName}}` doesn't appear in token sets and doesn't inflate intersection.

**File: `tests/test_routes_qa.py`** (additions):

19. **`test_qa_route_includes_diversity_fields`** - POST `/api/qa` with a 5-variation single-block body, response JSON contains all 5 new keys with correct types and the `diversity_gate_level` value matches `qa.DIVERSITY_GATE_LEVEL`.

All tests use only stdlib + existing pytest infra. Inline strings for tests 9, 10, 11 are committed to the test file (no `/tmp` dependency).

### 4.9 Public API impact summary

- `app.qa.qa()`: adds 5 keys; no existing keys change. Day 1 `passed` semantics unchanged for diversity-only failures.
- `app.api_models.QAResponse`: adds 5 optional fields.
- `app.api_models.QAResultEmbed`: adds 5 optional fields.
- `app.spintax_runner.SpintaxJobResult`: no signature change. Builder must verify the dataclass-to-pydantic conversion path (likely in routes) propagates new fields. Phase A test 19 covers this for the QA endpoint; an analogous smoke check belongs in `tests/test_routes_spintax.py` (added in Phase A step 4 below).

---

## 5. Part 2: `reshape_blocks`-per-block enforcement in `app/spintax_runner.py`

### 5.1 The two options

**Option A - System-prompt mandate (soft enforcement).** Rewrite the runner's hard-rules block in `_build_hard_rules`, lines 1063-1224, to require one `reshape_blocks` call per spintaxable non-greeting block before finalizing.

- Pro: zero changes to dispatch loops; ships in one prompt edit; works across all three API surfaces uniformly.
- Pro: cheapest implementation. Smallest blast radius.
- Con: **unenforceable.** The model already had a system prompt saying "REQUIRED - make at least ONE synonym/syntax tool call BEFORE the first lint_spintax call" and produced 1-2 calls across 6 blocks. More prompt text on top of weak prompt text does not force compliance.

**Option B - Runtime gating (hard enforcement).** Track which spintax blocks have been through `reshape_blocks`. After `lint_spintax` passes, before allowing the model to emit a final body, force a turn for any non-reshaped block.

- Pro: deterministic.
- Pro: auditable.
- Con: complexity. (a) `reshape_blocks` takes a sentence string; we need fuzzy sentence-to-block matching; (b) the model may pass a paraphrased sentence, requiring approximate matching; (c) three-place change across the loop adapters; (d) layer violation - dispatchers are tool-agnostic today.

### 5.2 Recommendation (REVISED v3, addressing critic finding #5)

**v2 said: ship Option A and escalate to Option B if Option A is leaky.** v3 reframes: **prompt-only nudges are weak by evidence, the diversity gate is the real enforcement, and Phase B becomes defensive prompt cleanup with low confidence in its impact.**

Rationale:
- The current prompt already says "REQUIRED" for one tool call; the model produced 1-2 across 6 blocks. Adding more "REQUIRED" text is exactly the kind of fix that has been measured not to work.
- The diversity gate (Phase A) catches the bad output regardless of how many `reshape_blocks` calls the model made. Bad output is the thing we care about.
- Option B's complexity is real and not justified by Phase A's expected hit rate.

**v3 ships Phase B as defensive prompt cleanup:**

- The prompt edit (Section 5.3) makes the per-block reshape expectation **clearer**, references the gate explicitly so the model knows the consequence of skipping reshape, and surfaces the connection between Strategy B and diversity.
- We ship Phase B alongside Phase A (no measurement gate between them) because we no longer claim the prompt edit moves the metric. The edit costs ~30 minutes, is independently revertable, and adding clarity to a system prompt is rarely net-negative.
- We do NOT escalate to Option B unless: (a) Phase A is in production for `>= 4 weeks`, (b) the diversity gate (post-promotion) is failing in `>= 30%` of jobs, AND (c) the operator confirms the failures are real diversity issues not threshold mis-calibration. Phase C (Option B) is a separate spec.

### 5.3 Prompt changes (Phase B = defensive cleanup)

In `_build_hard_rules()` (`spintax_runner.py` lines 1063-1224), replace the existing step 1.5 with the text below. Surrounding workflow steps stay unchanged.

```
1.5. CALL `reshape_blocks` for every non-greeting prose block before
   the first `lint_spintax` call. Block 1 is usually the greeting,
   which uses a fixed 5-string whitelist - it does NOT need
   reshape_blocks. Every OTHER prose paragraph gets ONE
   reshape_blocks call.

   Why: reshape_blocks is the Strategy B tool (sentence
   restructuring, clause reorder, question/statement flip,
   opener change). Without it, your variations 2-5 are word-swap
   reskins (Strategy A only), which produce 20-30% Jaccard
   diversity. The skill target is 60-80%.

   The qa.py diversity gate (BLOCK_AVG_FLOOR = 0.30,
   BLOCK_PAIR_FLOOR = 0.20) WILL flag low-diversity blocks (Day 1:
   warnings; later: errors). Skipping reshape_blocks on a block
   is a near-guaranteed gate flag for that block.

   For each non-greeting prose block:
     a. Identify the role (opener / body / proof / cta / ps).
     b. Call identify_syntax_family(sentence, role) — FREE.
     c. Call reshape_blocks(sentence, role, source_family,
        target_family, max_variants=3) — FREE. Use the returned
        variants as Strategy B seeds. If reshape_blocks returns
        zero variants for a family it does not handle, that is
        OK — make the call, then use your own restructuring
        (clause reorder, question/statement flip,
        because/so/since flip).
     d. THEN draft variations 2-5 combining Strategy A (synonyms
        via get_pre_approved_synonyms / score_synonym_candidates /
        wordhippo_lookup) AND Strategy B (the reshape_blocks
        output).

   The P.S. line and the pitch line have historically had the
   lowest diversity scores precisely because the model decides
   they are "fine as-is" and synonym-swaps them. They are NOT
   fine as-is. They get reshape_blocks like every other prose
   block.

   The agent-tool budget (DEFAULT_MAX_AGENT_TOOL_CALLS = 30) is
   sized to support: N reshape_blocks calls + N
   identify_syntax_family calls + 5-10 synonym lookups + headroom.
   Use it. These tools are FREE locally; only wordhippo_lookup
   costs.
```

The existing step 5b ("STUCK?") stays. The post-generation `lint_structure_repetition` hint stays.

**Wording note (NEW v3):** v2 used the word "REQUIRED" in caps. v3 drops it because it's the same word the current prompt uses unsuccessfully. v3 leads with "CALL" (imperative) and connects to the consequence (the gate). If the model treats this any differently than v2's "REQUIRED" we will see it in the `agent_tool_breakdown` post-deploy. We are not betting on it.

### 5.4 New diversity-failure feedback (works with Phase A)

When the diversity gate added in Phase A flags a block (warning or error), the runner does NOT retry. The diagnostic messages land in `qa_warnings` (Day 1) or `qa_errors` (post-promotion) on `SpintaxJobResult`; the operator sees them in the admin UI. No prompt-feedback plumbing needed.

`_extract_drift_warnings()` filter is unchanged - diversity diagnostic prefixes don't match drift filter patterns.

### 5.5 Code sketch (prompt edit)

Pure string-edit inside `_build_hard_rules()`. No function signatures change. No new imports. No changes to dispatch loops. `DEFAULT_MAX_AGENT_TOOL_CALLS = 30` (line 172) stays at 30.

### 5.6 How to test

1. **Snapshot test on prompt content.** New file `tests/test_spintax_runner_prompt.py` (~40 lines):
   - Calls `build_system_prompt("instantly", _skills_dir())` and `build_system_prompt("emailbison", _skills_dir())`.
   - Asserts the string contains key phrases: `"reshape_blocks for every non-greeting prose block"`, `"identify_syntax_family"`, `"BLOCK_AVG_FLOOR"`, `"diversity gate"`.
   - Asserts ordering: the new step 1.5 text appears between the literal markers `"\n1. "` (step 1) and `"\n2. "` (step 2). Catches the "step numbering broke" regression.

2. **No CI live-API test.** Cost + flake + low coverage value.

3. **Manual post-merge smoke (optional).** Resubmit the law-firms payload at medium effort. Inspect `agent_tool_breakdown.reshape_blocks` count. Even if it stays at 1-2, the gate (Phase A) is the real enforcement. Cost: ~$1-2.

### 5.7 Backward compatibility

Same as v2: prompt edit is internal; no public API changes. Old jobs in flight at deploy time complete with the old prompt (system prompt is captured per-job).

---

## 6. Implementation phases

### 6.1 Phase A.0 - Calibration check (FIRST builder task, ~30 min)

Before writing any production code, run a one-shot calibration:

1. Create `/tmp/diversity_calibration.py` (or a `@pytest.mark.skip`-decorated test in the repo for re-runs).
2. Inline-paste medium and high run prose blocks (5 blocks each, skip greeting and signature passthrough).
3. Run `_diversity_tokens()` and `_jaccard_distance()` exactly as specced in 4.6.
4. Print per-block average and minimum distances.
5. **Pass criteria:** scores match the empirical replay table in Section 1 (medium block 4 ~0.244, high block 3 ~0.169, high block 4 ~0.092, high block 6 ~0.205, high block 5 ~0.667 with min 0.000). If the implementation is correct, the script produces the same numbers.

If pass criteria do not hold, the tokenizer or stopword set has a bug; fix BEFORE merge. Acceptable threshold adjustments without re-spec:
- `BLOCK_AVG_FLOOR` between 0.25 and 0.35.
- `BLOCK_PAIR_FLOOR` between 0.15 and 0.25.
- Adding 5-10 stopwords to `_DIVERSITY_STOPWORDS`.

Anything outside those ranges -> back to spec review.

### 6.2 Phase A - Diversity gate (~3-4 hours after calibration passes)

1. Edit `app/qa.py`: add constants (`BLOCK_AVG_FLOOR`, `BLOCK_PAIR_FLOOR`, `BLOCK_AVG_FLOOR_CTA`, `CORPUS_AVG_FLOOR`, `DIVERSITY_GATE_LEVEL`), `_DIVERSITY_STOPWORDS`, `_diversity_tokens`, `_jaccard_distance`, `_is_cta_block`, `check_block_diversity`. Wire into `qa()` with the defensive try/except (4.7c). Update CLI human output.
2. Update import at top: add `is_greeting_block` to `from app.lint import ...`.
3. Edit `app/api_models.py`: add 5 new fields to `QAResponse` AND `QAResultEmbed`.
4. Verify `app/spintax_runner.py`'s `SpintaxJobResult` -> `QAResultEmbed` propagation. If the route layer hand-lists fields when constructing the pydantic model, add the 5 new fields explicitly. Add a smoke test in `tests/test_routes_spintax.py` confirming the new fields land on `JobStatusResponse.result.qa`.
5. Add tests 1-18 to `tests/test_qa.py`. Add test 19 to `tests/test_routes_qa.py`.
6. Run `pytest tests/test_qa.py tests/test_routes_qa.py` - all pass.
7. Run `pytest tests/` - confirm no regression.

**Phase A done when:**
- All new tests pass.
- All existing tests pass.
- Manual `qa()` call on inlined high run body produces `passed=True` (Day 1, warning level) with diversity warnings on blocks 3, 4, 6 AND CTA block 5 NOT flagged (CTA exemption working).
- `DIVERSITY_GATE_LEVEL = "warning"` in code at deploy.

### 6.3 Phase B - reshape_blocks prompt cleanup (~30-60 min, ships with Phase A)

1. Edit `app/spintax_runner.py`: replace step 1.5 in `_build_hard_rules()` per Section 5.3.
2. Add `tests/test_spintax_runner_prompt.py` per 5.6.1.
3. Snapshot test passes.

**Phase B done when:** snapshot test passes. No production smoke required - the gate is the enforcement.

### 6.4 Phase A.1 - Promotion criterion (NEW v3)

After Phase A ships Day 1 with `DIVERSITY_GATE_LEVEL = "warning"`:

**Daily review for `>= 7` consecutive days:**
- Pull `qa_warnings` from production jobs.
- Count: how many jobs would have failed if `DIVERSITY_GATE_LEVEL = "error"` (i.e., have any `"diversity below floor"` or `"pairwise diversity below floor"` warning)?
- Count: how many jobs would have passed?

**Promotion criterion:** `>= 70%` of production jobs pass the warning gate (would still pass if promoted to error) for 7 consecutive days. AND Mihajlo confirms the remaining `<= 30%` failures are real diversity issues (not threshold mis-calibration).

**On promotion:**
1. Single-line edit: `DIVERSITY_GATE_LEVEL = "error"` in `app/qa.py`.
2. Deploy.
3. Operator notification: post in `#prospeqt` Slack that `qa_passed=False` is now possible for diversity reasons; the admin UI's QA badge will turn red on diversity failures.

**On NOT meeting the criterion:**
- If `< 70%` pass rate after 7 days: the thresholds are too tight OR the model is genuinely producing too many low-diversity outputs. Tune thresholds (within Section 6.1 ranges) and reset the 7-day clock.
- If pass rate stalls below 70% across multiple tuning rounds: open Phase C (Option B runtime gating) as a separate spec.

**Rollback from promotion:** revert `DIVERSITY_GATE_LEVEL` to `"warning"` (one-line). No data migration; no prior outputs reprocessed.

---

## 7. Risk / rollback

### Risks

1. **Thresholds too tight, blocking legit copy.** Mitigated three ways: (a) Phase A.0 calibration check; (b) Day 1 ships warning-only; (c) promotion criterion explicitly verifies false-positive rate before flipping.
2. **CTA exemption bypasses real failures.** If a CTA block genuinely has 5 near-identical variations (not just 5 questions sharing structure), the exemption hides it. Mitigation: the score is still recorded in `diversity_block_scores`; the operator sees the number even if no diagnostic fires. If we observe CTA-exempt blocks scoring below 0.05 consistently, we tighten `BLOCK_AVG_FLOOR_CTA`.
3. **Greeting detection drift.** `is_greeting_block` is whitelist-based. Same risk as today's lint/qa overlap. Single source of truth via import.
4. **Prompt change has no effect.** v3 explicitly does NOT bet on the prompt change. The gate is the real enforcement. Worst case Phase B is a no-op.
5. **Cost increase from prompt change.** Negligible (string-only edit; no extra API calls implied).
6. **Diversity gate interacts poorly with drift revisions.** A drift revision might pass drift but fail diversity. Result lands at `qa_passed=False` (post-promotion) with both error categories visible. No infinite loops. No mitigation needed.
7. **`SpintaxJobResult` -> `QAResultEmbed` propagation bug.** Mitigated by Phase A step 4 explicit verification + integration test 19 + new smoke test in `test_routes_spintax.py`.
8. **Promotion happens prematurely.** Mitigation: 7 consecutive days + `>= 70%` pass rate + Mihajlo confirmation. Three independent checks.
9. **Internal exception in `check_block_diversity`.** Mitigated by 4.7c defensive try/except + test 15.
10. **CTA detection false positive.** A non-CTA block whose V1 ends with `?` and is the last block (e.g., a final pain question) gets the relaxed thresholds. Acceptable risk - the relaxed thresholds (0.20 avg, no pair floor) still catch the worst cases (all-identical) and the cost of a false-positive exemption is one missed diversity flag, not a blocked job.

### Rollback

**Phase A:**
1. Soft rollback (single-line): set `DIVERSITY_GATE_LEVEL = "warning"`. Gate runs; never errors.
2. Softer rollback: set `BLOCK_AVG_FLOOR = 0.0` and `BLOCK_PAIR_FLOOR = 0.0`. Gate runs but never fires.
3. Hard rollback: revert qa.py and api_models.py commits.

**Phase B:** revert the prompt-edit commit. Old step 1.5 restored.

**Promotion (post-Phase A.1):** revert `DIVERSITY_GATE_LEVEL` to `"warning"`. One line.

All four levels are independently revertable.

---

## 8. Open questions for Mihajlo

1. **Phase A.0 calibration.** Commit a calibration script as a `@pytest.mark.calibration`-decorated test (CI-skipped by default), or one-shot in `/tmp` during builder implementation?
2. **Auto-retry on diversity failure.** Spec says no retry. Future option after failure-rate data.
3. **Promotion criterion thresholds.** 7 days + 70% pass rate. Tighter (90%) or looser (60%)? Want to be more conservative?
4. **CTA exemption boundary.** Spec uses "last spintax block + V1 ends with '?'" as deterministic detection. False-positive case: an email that ends with a final pain question, not a CTA. Acceptable, or do we need a stricter detector (role classifier)?
5. **Prompt edit phrasing.** New step 1.5 references the constant `BLOCK_AVG_FLOOR` by name. If renamed in a future refactor, prompt drifts. Acceptable, or use literal `0.30`?
6. **Promotion notification mechanism.** Spec says "post in `#prospeqt` Slack." Want me to add a structured changelog entry to the spintax-web repo's CHANGELOG.md too, or is Slack enough?

---

## 9. Summary for builder agent

**Phase A.0 (calibration, first):** confirm the spec's tokenizer reproduces the empirical replay numbers in Section 1. Tune within ranges if needed.

**Phase A (diversity gate, Day 1 = warning level):**
- Add constants, `_diversity_tokens`, `_jaccard_distance`, `_is_cta_block`, `check_block_diversity` to `app/qa.py`.
- Wire into `qa()` with defensive try/except.
- Add 5 new fields to BOTH `QAResponse` and `QAResultEmbed`.
- Verify dataclass-to-pydantic propagation in routes.
- 19 tests across `test_qa.py`, `test_routes_qa.py`, `test_routes_spintax.py`. Stdlib only.
- Update CLI human output with diversity summary.
- `DIVERSITY_GATE_LEVEL = "warning"` at deploy.

**Phase B (prompt cleanup, ships with Phase A):**
- Edit `_build_hard_rules()` step 1.5 in `app/spintax_runner.py`.
- Snapshot test. No live-API test.

**Phase A.1 (promotion, day 7+):**
- Daily review production warnings.
- After 7 days + 70% pass rate + operator confirmation: flip `DIVERSITY_GATE_LEVEL = "error"`.
- Operator notification.

---

## Appendix A - v2 to v3 changelog

Items addressed from spec-critic round 1 review:

1. **Critic finding #1 (greeting Jaccard arithmetic):** v2 already exempted greetings via `is_greeting_block` (Section 4.3). v3 makes the **principled justification explicit** in 4.3.1 (whitelist-bounded, not score-driven) AND adds test 5 assertion that exemption is whitelist-driven, not score-driven.
2. **Critic finding #2 (block 5 V2 at 44% is a fine variant):** v3 anchors against V1 (not all-pairs) and uses CTA-specific thresholds (4.3.2). The "44% is fine" case is the V1<->Vn distance the gate evaluates. Test 11 covers this.
3. **Critic finding #3 (block 4 high V3 at 0% is the smoking gun):** v3 adds test 9 (`test_diversity_high_run_block_4_v3_smoking_gun`) with the exact V1 and V3 strings; asserts `_jaccard_distance` returns `0.000` and the gate emits a pair-floor diagnostic.
4. **Critic finding #4 (variable handling):** v3 adds Section 4.5 with explicit rationale for the strip-not-keep-not-normalize choice, and test 18 asserts variables don't appear in token sets.
5. **Critic finding #5 (Option A unenforceable - BLOCKER):** v3 reframes Phase B as "defensive prompt cleanup, not enforcement" (Section 5.2). The diversity gate is the real enforcement. Phase B no longer needs Phase A signal as a trigger; ships alongside Phase A. Phase C (Option B runtime gating) explicitly deferred to a separate spec with a 4-week + 30% failure trigger.
6. **Critic finding #6 (failure-mode gap):** v3 adds Section 4.7c with explicit defensive try/except contract and test 15.
7. **Critic finding #7 (CTA exemption fragility):** v3 adds Section 4.3.2 with a deterministic CTA detector and CTA-specific thresholds (`BLOCK_AVG_FLOOR_CTA = 0.20`, no pair floor). Empirical replay confirmed high block 5 needs this. Tests 10 and 11 cover both audited CTA blocks.
8. **Critic finding #8 (backward compat):** v3 adds Section 4.7b documenting the three consumer surfaces and the v3 contract (additive only Day 1; `passed` semantics change only after promotion). Tests 16 and 17 cover both gate levels.
9. **Critic finding #9 (rollout strategy - HIGH):** v3 adds `DIVERSITY_GATE_LEVEL` constant defaulting to `"warning"` (Section 4.2), Section 6.4 promotion criterion (7 days + 70% pass rate + operator confirmation), and explicit promotion / rollback procedures.
10. **Empirical replay table** added to Section 1 with per-block scores under this spec's tokenizer (replaces v2's reliance on the audit's external scorer).
11. **`diversity_gate_level` key** added to `qa()` return dict and Pydantic models so consumers can detect the current rollout stage (Section 4.6).
12. **Phase A test count** raised from 14 to 19.

End of spec v3.
