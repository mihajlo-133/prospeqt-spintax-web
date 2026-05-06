# Beta Block-First Spintax Pipeline - Spec v1

**Status:** Draft
**Author:** Claude + Mihajlo (design conversation 2026-05-06)
**Scope:** Beta v1 only. v2 and future work in appendix.

---

## 1. Goal

Replace the current whole-email spintax generation with a block-first pipeline where every sentence is processed in isolation. The current path sends the entire email to one LLM call and asks for all blocks and all V2-V5 variants in one shot. Quality drops on later blocks: synonyms drift away from V1's register ("Cheerful clients" in a professional B2B email), domain nouns get swapped ("clients" -> "customers"), and the model loses focus across long emails.

Block-first solves this by:

- Splitting the email into sentences before any generation happens
- Running a profiler pass that captures tone and locks domain nouns
- Generating a per-email synonym pool the spintaxer must pick from (no inventing words)
- Spintaxing each block in parallel with its own focused LLM call
- Reassembling the spintaxed blocks into the final output

The Phase 1 Jaccard cleanup we shipped on 2026-05-06 already proved that block-level prompts produce noticeably better synonyms than email-level prompts. This spec promotes that pattern from a fallback to the primary path.

## 2. Non-goals (v1)

- Replacing the alpha (whole-email) path. Alpha stays live and is the default.
- Testing alternative synonym providers (Datamuse, WordsAPI, WordHippo). Deferred to v2.
- Code-only path for pure-synonym-swap blocks. Deferred to future work.
- New validators. We reuse Jaccard, length, lint, drift from alpha.
- New retry-tuning beyond what alpha already does.

## 3. Architecture

### 3.1 Pipeline diagram

```
                      [Plain email body]
                              |
                  +-----------+-----------+
                  |                       |
                  v                       v
           [Splitter LLM]          [Profiler LLM]
            (parallel)              (parallel)
                  |                       |
                  v                       v
         {block_1: "...",         {tone, locked_words,
          block_2: "...",          proper_nouns}
          block_N: "..."}                |
                  \                       /
                   +---------+-----------+
                             |
                             v
                  [Synonym Pool Generator]
                  (1 LLM call, batched across blocks)
                             |
                             v
            {block_N: {synonyms, syntax_options}}
                             |
                             v
                  [Block Spintaxer x N]
                  (parallel, one call per block)
                  Strict: picks from pool only
                  Output: V1-V5 for each block
                             |
                             v
                       [Assembler]
                       (pure code)
                             |
                             v
                       [Validators]
                       Jaccard within-block, length,
                       lint, drift (existing modules)
                             |
                             v
                     [Final spintax]
```

### 3.2 Stage-by-stage data contract

#### Stage 1: Block Splitter

**Input:** plain email body (string).

**LLM call:**
- Model: `gpt-5-mini` (or current default mini)
- Reasoning effort: `low`
- Structured output: JSON Schema enforcing the shape below

**Output JSON schema:**
```json
{
  "blocks": [
    {"id": "block_1", "text": "Hi {{firstName}},", "lockable": true},
    {"id": "block_2", "text": "I noticed your firm.", "lockable": true},
    {"id": "block_3", "text": "Visit {{customLink}}.", "lockable": false}
  ]
}
```

`lockable` is set in code (NOT by the LLM) AFTER the splitter returns:
- `lockable: false` if the block's text contains `{{...}}` placeholder AND nothing else of substance (i.e., the block IS a placeholder)
- `lockable: true` otherwise (block has spintaxable content)

**Code rule for lockable detection:**
```python
# pseudo
def is_lockable(block_text: str) -> bool:
    # Strip placeholders
    stripped = re.sub(r"\{\{[^}]+\}\}", "", block_text).strip()
    # If after stripping placeholders nothing meaningful remains,
    # the block is unlockable (pure placeholder)
    return len(stripped) >= MIN_SPINTAXABLE_CHARS
```

`MIN_SPINTAXABLE_CHARS = 8` (tunable).

**Failure mode:** if the splitter call fails (network, timeout, invalid JSON despite structured output), the job fails with `splitter_error`. No regex fallback (per design call). Caller retries.

**Splitter prompt (draft):**
```
You are a sentence splitter for marketing email bodies.

Input: a plain email body. Output: a JSON array of sentence-level blocks
in order. Each block is a single sentence or a single bullet point.

Rules:
- Split on sentence boundaries: period/question-mark/exclamation followed
  by whitespace + capital letter, or followed by end of input.
- Do NOT split on abbreviations (Mr., Dr., e.g., i.e., U.S., etc.).
- Do NOT split on URLs even if they contain periods or weird spacing.
- Treat each bullet point as its own block.
- Preserve placeholders like {{firstName}} EXACTLY as written.
- Preserve all whitespace and punctuation that the original sentence had,
  including trailing punctuation.
- Return blocks in the order they appear in the email.

Output JSON shape:
{"blocks": [{"id": "block_1", "text": "..."}, ...]}
```

#### Stage 2: Profiler

**Input:** plain email body (string). Runs in parallel with splitter.

**LLM call:**
- Model: `gpt-5-mini`
- Reasoning effort: `low`
- Structured output: JSON Schema below

**Output JSON schema:**
```json
{
  "tone": "professional B2B, consultative, no jargon",
  "audience_hint": "law firms",
  "locked_common_nouns": ["clients", "matters", "cases"],
  "proper_nouns": ["Fox & Farmer", "JP Morgan"]
}
```

`locked_common_nouns` are domain-specific common nouns the spintaxer must NOT swap (e.g., "clients" stays "clients" in a law firm email).

`proper_nouns` are auto-detected by code BEFORE the profiler call (regex for capitalized multi-word phrases) and then handed to the profiler for verification. The profiler may add more if it spots obvious brand names the regex missed. Final list is union of both sources.

**Profiler prompt (draft):**
```
You are an email tone profiler. You read a marketing email and extract:

1. tone: a short phrase describing register and voice
   (e.g. "professional B2B, consultative" or "casual, friendly").
2. audience_hint: who this email is for, if inferrable
   (e.g. "law firms", "BPO operators"). Use null if unclear.
3. locked_common_nouns: common nouns that carry domain meaning
   and must NOT be swapped for synonyms
   (e.g. "clients" in legal/professional services,
   "patients" in healthcare, "tenants" in real estate).
4. proper_nouns: brand names, company names, product names
   that must be preserved exactly.

Be vague on tone, specific on locked nouns. Do NOT deduce block intent.
```

#### Stage 3: Synonym Pool Generator

**Input:** all blocks from splitter + profile from profiler. Runs in serial after both upstream stages complete.

**LLM call:**
- Model: `gpt-5-mini`
- Reasoning effort: `medium`
- One call, batched across all blocks
- Structured output: JSON Schema below

**Output JSON schema:**
```json
{
  "block_1": {
    "synonyms": {
      "happy": ["pleased", "glad", "satisfied"],
      "help": ["assist", "support", "enable"]
    },
    "syntax_options": [
      "I noticed your firm.",
      "Your firm caught my eye.",
      "Came across your firm."
    ]
  },
  "block_2": { ... }
}
```

**Rules baked into the prompt:**
- For each content word in a block, return 3-5 register-matched synonyms
- Synonyms must be within +/- 3 chars of the original word's length (max +/- 6)
- Skip locked_common_nouns and proper_nouns (do not generate synonyms for them)
- Skip function words (the, a, is, of, and, etc.)
- syntax_options are alternative phrasings of the entire sentence that preserve meaning
- Provide 2-4 syntax_options per block

**Synonym pool prompt (draft):**
```
You are a synonym pool generator for an email spintax system.

For each block (sentence) in the email, return a synonym pool the
spintaxer can pick from. The spintaxer is STRICT: it can only use
words from the pool you provide.

Profile:
  Tone: {tone}
  Audience: {audience_hint}
  Locked nouns: {locked_common_nouns}
  Proper nouns: {proper_nouns}

For each block:

1. synonyms: dict of {original_word: [synonym_options]}
   - Only for content words (skip 'the', 'a', 'is', 'of', etc.)
   - Skip any word in locked_common_nouns or proper_nouns
   - Each synonym must MATCH THE TONE (no "cheerful" in a professional email)
   - Each synonym must be within +/- 3 chars of the original word's length
     (max +/- 6 in edge cases)
   - 3 to 5 synonyms per word

2. syntax_options: 2-4 alternative phrasings of the entire sentence
   - Preserve meaning exactly
   - Preserve all placeholders ({{...}}) and locked nouns
   - Vary syntactic structure (clause order, voice, framing)
   - Do NOT introduce new content words; only reorder existing meaning

Blocks:
{blocks_json}
```

#### Stage 4: Block Spintaxer

**Input:** one block + its pool entry + the profile. One LLM call PER block, run in parallel.

**LLM call:**
- Model: configurable, default `gpt-5` (matches alpha's default)
- Reasoning effort: `high` (matches alpha)
- Structured output: JSON Schema below

**Output JSON schema:**
```json
{
  "block_id": "block_1",
  "variants": ["V1 text", "V2 text", "V3 text", "V4 text", "V5 text"]
}
```

**Rules:**
- V1 must equal the original block text exactly (preserved)
- V2-V5 are generated by:
  - Picking a syntax_option from the pool
  - Substituting words using the synonym pool
  - Preserving placeholders, locked_nouns, proper_nouns exactly
- V2-V5 must each differ from V1 and from each other (Jaccard floor enforced post-hoc)
- Length stays within +/- 5% (3-char floor) of V1

**Block spintaxer prompt (draft):**
```
You are a sentence spintaxer. You take a single sentence (V1) and
produce 4 alternative phrasings (V2, V3, V4, V5) that say the same
thing in different words.

STRICT RULE: you may only use words from the synonym pool, plus
function words (the, a, is, of, and, etc.), plus the locked nouns
and proper nouns listed below. If a word is not in the pool and not
locked, you cannot use it.

V1 (original sentence):
{block_v1}

Synonym pool (the only content-word substitutions allowed):
{synonyms_dict}

Syntax options (alternative phrasings to start from):
{syntax_options_list}

Locked nouns (preserve exactly):
{locked_nouns_list}

Proper nouns (preserve exactly):
{proper_nouns_list}

Placeholders (preserve exactly):
{{firstName}}, {{companyName}}, {{customLink}}, etc. - any text in
double curly braces is a placeholder and must NOT be modified.

Profile:
  Tone: {tone}
  Audience: {audience_hint}

Output exactly 5 variants. V1 must match the input above word for word.
V2-V5 must each differ from V1 AND from each other in both word choice
and structure. Stay within +/- 5% of V1's character length.
```

#### Stage 5: Assembler

**Input:** N block-spintax results.

**Logic (pure code, no LLM):**
- Concatenate variants by position into the final spintax format
- Block formatting: `{V1|V2|V3|V4|V5}` per block, joined in original order with original whitespace/newlines
- For `lockable: false` blocks (pure placeholder blocks), pass through V1 only - no spintax wrapping

**Output:** the final spintax string ready for validators.

#### Stage 6: Validators

Reuses existing modules from alpha:
- `app/qa/jaccard.py` - within-block diversity check
- `app/qa/length.py` - per-variant length tolerance
- `app/qa/lint.py` - platform-specific syntax (Instantly vs EmailBison)
- `app/qa/drift.py` - drift detection

If a validator fails on a specific block:
- Jaccard fail -> retry that block's spintaxer call (max N reprompts, same as alpha)
- Length fail -> retry that block's spintaxer call
- Lint fail -> usually a global issue; surface as job error
- Drift fail -> retry that block's spintaxer call

If retries exhaust, the job fails with the same error keys alpha uses today.

### 3.3 Order of execution

```
t=0: splitter call + profiler call (parallel)
t=2: both return; build proper_nouns union, lockable tags
t=2: synonym pool call (waits on splitter + profiler)
t=6: synonym pool returns
t=6: N block spintaxer calls in parallel
t=11: slowest block returns; assembly + validators (synchronous, fast)
t=12: done OR retry failing block(s)
```

Estimated p50 latency: **~12s for a 6-block email** vs alpha's ~30s. Cost is roughly comparable (more calls, smaller each).

## 4. File layout

### 4.1 New files

```
app/
  spintax_runner_v2.py          # Beta entrypoint, mirrors run() interface
  pipeline/
    __init__.py
    splitter.py                 # Stage 1
    profiler.py                 # Stage 2
    synonym_pool.py             # Stage 3
    block_spintaxer.py          # Stage 4
    assembler.py                # Stage 5
    contracts.py                # Pydantic models for all stage outputs
    pipeline_runner.py          # Orchestrates stages 1-5 + validators

tests/
  pipeline/
    test_splitter.py
    test_profiler.py
    test_synonym_pool.py
    test_block_spintaxer.py
    test_assembler.py
    test_pipeline_integration.py
    fixtures/
      fox_farmer_email.txt
      zenhire_email.txt
      ... (benchmark corpus)
```

### 4.2 Modified files

```
app/
  spintax_runner.py             # Untouched (alpha stays as-is)
  routes/spintax.py             # Add pipeline selection logic based on env var
  routes/batch.py               # Add pipeline selection logic
  config.py                     # New SPINTAX_PIPELINE setting

```

### 4.3 Feature flag

**Env var:** `SPINTAX_PIPELINE`

**Values:**
- `alpha` (default) - current whole-email path
- `beta_v1` - new block-first path

**Where it's read:**
- `app/config.py` via pydantic-settings
- `app/routes/spintax.py` and `app/routes/batch.py` route to the correct runner based on this value
- Each request can override via `?pipeline=` query param (admin/QA only) for A/B testing

**UI exposure:**
- Add a hidden admin toggle in the web UI to flip pipelines per request (for teammate A/B testing)
- Default UI does NOT expose this; teammates use a separate admin flag or direct URL param

## 5. Testing strategy

### 5.1 Unit tests

Each pipeline module gets its own test file with:

- **Splitter:** mock LLM responses with various email shapes (single-sentence, multi-paragraph, bulleted, with placeholders, with URLs containing dots). Verify JSON contract, lockable detection rule.
- **Profiler:** mock LLM responses for different domains (law firms, BPOs, healthcare). Verify proper noun detection (regex pre-pass) merges correctly with LLM output.
- **Synonym pool:** mock LLM responses. Verify length-band rule, locked noun exclusion, function word exclusion.
- **Block spintaxer:** mock LLM responses. Verify strict pool-only enforcement (test that a response using an out-of-pool word triggers retry).
- **Assembler:** pure code, no mocks. Verify spintax wrapping for lockable blocks, passthrough for unlockable, whitespace preservation.

### 5.2 Integration test

Single end-to-end test using the full pipeline against a recorded LLM response set (saved fixtures):

- Input: Fox & Farmer review-tracking email (the canonical test case)
- Mock all LLM calls with pre-recorded fixtures
- Verify final spintax passes all validators

### 5.3 Benchmark corpus

A folder of 20-30 real emails from past clients, each annotated with:

```yaml
# tests/pipeline/fixtures/fox_farmer_email.yaml
---
email_id: fox_farmer_review
plain_body: |
  Hi {{firstName}}, ...
expected_register: professional B2B
expected_locked_nouns: [clients, matters]
expected_proper_nouns: [Fox & Farmer]
expected_block_count: 7
notes: |
  Used in production testing 2026-05-06. Watch for "Cheerful" drift.
```

A benchmark script runs alpha and beta_v1 on every corpus email and reports:
- Job success / failure
- Total LLM calls per pipeline
- Total cost per pipeline
- Total wall-clock latency per pipeline
- Validator pass rates per pipeline

This is for our internal sanity check. The actual sunset gate is teammate A/B verdict (see section 6).

### 5.4 No live API calls in tests

Per repo convention, all tests mock the LLM. The benchmark script may hit live LLMs but only when explicitly invoked (`python scripts/benchmark.py`), never in CI.

## 6. Sunset gate (alpha retirement)

Alpha is sunset (removed from production traffic) ONLY when:

1. Beta v1 (or v2 if shipped) has been live behind a feature flag for at least 14 days
2. Mihajlo's teammates have A/B tested both pipelines on real emails
3. Teammate consensus is that beta produces better output across the dimensions they care about (synonym register, domain noun preservation, overall feel)
4. Beta latency p95 is within 1.5x of alpha (degraded but acceptable)
5. Beta cost per email is within 2x of alpha (degraded but acceptable)

If teammates prefer alpha on any dimension they consider important, sunset does NOT happen. Iterate on beta first.

**"Sunset" definition:** alpha code stays in the GitHub repo permanently. It is removed from production traffic by setting `SPINTAX_PIPELINE=beta_v1` as the Render default and removing the alpha branch from the routing logic. Alpha can be reactivated by flipping the env var.

## 7. Errors and observability

### 7.1 New error keys

- `splitter_error` - splitter LLM call failed or returned invalid JSON
- `profiler_error` - profiler LLM call failed or returned invalid JSON
- `synonym_pool_error` - pool generation failed or returned invalid JSON
- `block_spintax_error` - a block's spintaxer call failed after retries

Each maps to a clear user-facing message in the existing error UI.

### 7.2 New diagnostics

Extend the existing job result with:

```json
{
  "pipeline": "beta_v1",
  "splitter": {
    "block_count": 7,
    "lockable_count": 6,
    "duration_ms": 1800
  },
  "profiler": {
    "tone": "professional B2B",
    "locked_nouns": ["clients", "matters"],
    "proper_nouns": ["Fox & Farmer"],
    "duration_ms": 1900
  },
  "synonym_pool": {
    "total_synonyms": 42,
    "blocks_covered": 6,
    "duration_ms": 4100
  },
  "block_spintaxer": {
    "blocks_completed": 6,
    "blocks_retried": 1,
    "max_retries_per_block": 2,
    "p95_block_duration_ms": 5200
  }
}
```

This appears in `/api/status/{job_id}` for any beta job, alongside existing alpha diagnostics (which are unchanged).

## 8. Out of scope for v1

- Synonym provider experimentation (Datamuse, WordsAPI, etc.) -> v2
- Code-only path for sentences that need no syntax change -> future
- Multi-language support -> future
- Per-block caching across emails (same sentence appearing in multiple emails) -> future
- Sub-block spintax (spintaxing within a phrase, not just whole sentences) -> not planned

## 9. Open questions

To resolve before or during implementation:

1. **Splitter granularity for compound sentences with semicolons.** "I help you A; you save B." - one block or two? Default: one block (semicolon does not split). Confirm with first benchmark run.
2. **Bullet point handling edge case.** A bullet that contains a colon: "Free strategy call: 30 minutes, no pitch." - one block or two? Default: one block. Confirm with corpus.
3. **Spintaxer freedom for tiny blocks.** A 3-word block ("Talk soon!" or "Best,") may have no useful synonym substitutions. Should the spintaxer be allowed to repeat V1 across V2-V5 in those cases, or do we force generation? Default: allow repeats for blocks with `<= 3 content words`, document this in the prompt. Will surface in Jaccard validator (it already handles "no content tokens" gracefully).
4. **Profiler model choice.** GPT-5-mini may be too small to nail tone on subtle emails. We'll start there; upgrade if benchmark shows it gets register wrong.
5. **Synonym pool batching cost.** A 10-block email may produce a 5KB pool JSON. If output token cost dominates, we may need to split into multiple calls. Measure on benchmark corpus first.

## Appendix A: Beta v2 - swap synonym provider

Beta v2 swaps the GPT-mini synonym pool generator for whichever provider wins a head-to-head test:

- **Datamuse** - free, no auth, JSON API. Strong on common synonyms, weaker on register.
- **WordsAPI** - paid free tier, cleaner data than WordHippo.
- **WordHippo scrape** - what Mihajlo knows, but no official API.
- **GPT-mini** - the v1 baseline. Strong on register and context, costs more.

The provider abstraction lives in `app/pipeline/synonym_providers/` with a uniform `generate_pool(blocks, profile) -> SynonymPool` interface. Selection driven by `SYNONYM_PROVIDER` env var.

A separate workstream runs the head-to-head: same benchmark corpus, all providers run, teammates rank outputs blind. Winner becomes the v2 default.

## Appendix B: Future work

- **Code-only path for pure-synonym blocks.** If a block has no useful syntax variation (short greeting, signature, simple phrase), skip the LLM and do template substitution from the synonym pool in code. Saves cost on the easy cases.
- **Cross-email synonym pool cache.** If the same sentence appears in multiple emails (greetings, sign-offs), cache the pool and reuse.
- **Prompt-level register learning.** Track which synonyms get rejected by Jaccard or human review, feed back into the pool generator's prompt.
- **Multi-language.** Splitter and profiler can already handle non-English; spintaxer prompts would need translation. Block-first makes this much cleaner than alpha would have.

---

**End of spec v1.**
