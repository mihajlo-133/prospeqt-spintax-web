# Pipeline Benchmark Corpus

Real emails used to exercise the beta block-first pipeline against the alpha
whole-email runner. Each fixture is a YAML file with the email body plus
ground-truth annotations the benchmark script (`scripts/benchmark.py`) and
integration tests check against.

## File format

Every fixture follows this shape:

```yaml
---
email_id: short_slug                 # filename matches: {email_id}.yaml
client: friendly client name         # for grouping in reports (Fox & Farmer, ZenHire, etc.)
plain_body: |
  Hi {{firstName}},

  ... full email body here ...

  Talk soon,
  {{senderName}}
expected_register: professional B2B  # or 'casual', 'consultative', etc.
expected_audience_hint: law firms    # or null if not inferrable
expected_locked_nouns:               # words the profiler MUST flag as locked
  - clients
  - matters
expected_proper_nouns:               # words that must be preserved exactly
  - Fox & Farmer
expected_block_count: 7              # how many sentences/blocks splitter should find
expected_lockable_count: 6           # how many of those are spintaxable
notes: |
  Free-form context. Why this fixture exists, what regressions it
  catches, prior bugs it covers, etc.
```

## Adding a new fixture

1. Pick a stable `email_id` (lowercase, underscores). The filename must
   match exactly: `{email_id}.yaml`.
2. Paste the plain email body verbatim (with placeholders preserved).
3. Annotate the expected values by reading the email yourself. The
   benchmark compares actual pipeline output against these labels, so
   wrong labels = wrong scoring.
4. Run `scripts/benchmark.py --fixture {email_id}` to confirm the
   pipeline can process the fixture.

## What goes in `recorded/`

`fixtures/recorded/` holds JSON files of pre-recorded LLM responses that
the integration test (`tests/pipeline/test_pipeline_integration.py`)
replays so the test never hits a live API. Format: one JSON file per
stage call, named `{email_id}_{stage}.json`. Capture these by running
the pipeline against a live API once and saving the responses.

## Coverage target

Beta v1 ship gate calls for 20-30 fixtures spanning:

- Multiple clients (Fox & Farmer, ZenHire, Continuum, Eden Smoothies, ...)
- Multiple registers (professional B2B, casual, consultative)
- Multiple audiences (law firms, BPOs, healthcare, real estate)
- Edge cases: bullets, embedded URLs, abbreviations, very short / very long emails
