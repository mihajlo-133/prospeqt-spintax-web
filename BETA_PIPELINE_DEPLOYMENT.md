---
title: "Beta Block-First Pipeline Deployment & A/B Test Runbook"
type: reference
tags: [spintax, beta_v1, pipeline, deployment, render, ab_test]
status: active
created: 2026-05-06
---

# Beta Block-First Pipeline - Deployment & A/B Test Runbook

**Service:** prospeqt-spintax-web (https://prospeqt-spintax.onrender.com)
**Render service ID:** `srv-d7nosqf7f7vs73fu9pc0` (verify before editing - see Pre-flight)
**Spec:** `BETA_BLOCK_FIRST_SPEC.md`
**Last updated:** 2026-05-06

---

## What this covers

How to ship the beta block-first pipeline (`spintax_runner_v2`) into
production behind a per-request opt-in, run a 1-2 week teammate A/B
test, and decide whether to make it the default.

The four Wave 4 components in scope:

| Component | Path | Purpose |
|-----------|------|---------|
| Beta runner entrypoint | `app/spintax_runner_v2.py` | Wraps `pipeline_runner.run_pipeline()` with the alpha-compatible `run()` signature. Selected by the dispatcher. |
| Pipeline diagnostics on `/api/status` | `app/api_models.py` + `app/routes/spintax.py` | Surfaces `PipelineDiagnostics` (per-stage timing, lockable count, retry count) on completed beta jobs. |
| Admin pipeline toggle | `templates/index.html` + `static/main.js` | Hidden by default; `?admin=1` reveals an alpha / beta_v1 segmented button that threads the override through the request payload. |
| Server config | `app/config.py` env var `SPINTAX_PIPELINE` | Production default. Per-request override always wins. |

---

## Pre-flight (do every time before changing Render env vars)

1. Probe the live URL to confirm which service is which:

   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://prospeqt-spintax.onrender.com/
   ```

   Expect `200` (or whatever the dashboard's normal status is).

2. Verify the service ID matches by hitting Render's API:

   ```bash
   API_KEY=$(grep -E '^[A-Za-z0-9_-]+' tools/accounts/render/api_key.md | head -1)
   curl -s -H "Authorization: Bearer $API_KEY" \
     "https://api.render.com/v1/services/srv-d7nosqf7f7vs73fu9pc0" | jq '.service.name, .service.serviceDetails.url'
   ```

   Confirm the name matches what you expect before editing any env var.
   This guards against the multi-service-named-similarly trap.

3. Render's `PUT /env-vars` REPLACES the entire env-var set. **Always
   fetch the current set first, merge, then PUT the full list back.**
   Never PUT a partial set.

---

## Stage 1 - Ship beta as opt-in only (NOT default)

Beta lands in production but only fires when a teammate explicitly
selects it via the admin toggle. Default traffic stays on alpha.

### 1a. Confirm `SPINTAX_PIPELINE` stays at `alpha`

```bash
curl -s -H "Authorization: Bearer $API_KEY" \
  "https://api.render.com/v1/services/srv-d7nosqf7f7vs73fu9pc0/env-vars" \
  | jq '.[] | select(.envVar.key=="SPINTAX_PIPELINE")'
```

Either `SPINTAX_PIPELINE=alpha` or absent (the validator defaults to
`alpha`) is fine. Do NOT set it to `beta_v1` at this stage.

### 1b. Deploy

```bash
git push origin main
```

Render auto-deploys. Wait for build + `Live` status in the dashboard.

### 1c. Smoke test

```bash
# Alpha path stays default - no `pipeline` field
curl -X POST https://prospeqt-spintax.onrender.com/api/spintax \
  -b cookie.txt \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hi {{firstName}}.\n\nWe help small firms.\n\nTalk soon.","platform":"instantly"}'

# Beta opt-in - explicit `pipeline:"beta_v1"`
curl -X POST https://prospeqt-spintax.onrender.com/api/spintax \
  -b cookie.txt \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hi {{firstName}}.\n\nWe help small firms.\n\nTalk soon.","platform":"instantly","pipeline":"beta_v1"}'
```

Poll `/api/status/{job_id}` until `status:"done"`. The beta job's
`result` should contain a `pipeline_diagnostics` block with stage
timing and `pipeline:"beta_v1"`. Alpha jobs leave that field `null`.

### 1d. Send teammates the admin URL

Tell the testers:

> Visit `https://prospeqt-spintax.onrender.com/?admin=1` once. After
> that, the PIPELINE row will appear next to Reasoning and stay
> visible across navigations (persisted via `localStorage`). Click
> `beta_v1` to opt in for your next run.

Localstorage key for opt-out: `localStorage.removeItem('spintaxAdminMode')`.

---

## Stage 2 - Run the A/B test (1-2 weeks)

### 2a. Test plan

| Aspect | Value |
|--------|-------|
| Duration | 7-14 calendar days |
| Sample target | >= 30 jobs per pipeline per teammate |
| Test population | Mihajlo + 2-3 GTM teammates |
| Allocation | Self-selected (teammates flip the toggle when they want to test) |
| Success criteria | (1) Beta job-level pass rate >= alpha pass rate. (2) Beta cost-per-job within 20% of alpha. (3) Subjective copy quality vote: >= 60% prefer beta in blind A/B reviews. |

### 2b. Tracking

Each teammate logs every comparison in a shared sheet with these columns:

- `job_id_alpha`, `job_id_beta` (run the same input through both)
- `cost_usd_alpha`, `cost_usd_beta`
- `qa_passed_alpha`, `qa_passed_beta`
- `diversity_corpus_avg_alpha`, `diversity_corpus_avg_beta`
- `preferred` (alpha / beta / tie) - blind read by a different teammate
- `notes`

The first four columns come straight from `/api/status/{job_id}` JSON.

### 2c. Scripted comparison

For systematic batches, run `scripts/benchmark.py` against the corpus
(beta-only at v1; alpha comparison lands once `spintax_runner_v2.run`
is callable from a script context, which it now is - extending the
benchmark to call alpha is a small follow-up):

```bash
python scripts/benchmark.py --format markdown --output benchmark/results_$(date +%Y%m%d).md
```

---

## Stage 3 - Promote beta to default (only after success criteria pass)

### 3a. Decide

Review the A/B sheet. Promote ONLY when all three criteria in 2a are
met. Otherwise stay in Stage 2 or roll back.

### 3b. Flip the env var

Fetch current env vars, merge `SPINTAX_PIPELINE=beta_v1` in, PUT back:

```bash
API_KEY=$(grep -E '^[A-Za-z0-9_-]+' tools/accounts/render/api_key.md | head -1)
SVC=srv-d7nosqf7f7vs73fu9pc0

# Fetch + merge + PUT (PUT replaces the WHOLE env-var set, do not skip the merge)
curl -s -H "Authorization: Bearer $API_KEY" \
  "https://api.render.com/v1/services/$SVC/env-vars" \
  | jq '[.[] | .envVar | select(.key != "SPINTAX_PIPELINE")] + [{"key":"SPINTAX_PIPELINE","value":"beta_v1"}]' \
  > /tmp/envvars.json

curl -s -X PUT -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -d @/tmp/envvars.json \
  "https://api.render.com/v1/services/$SVC/env-vars"
```

Render redeploys automatically.

### 3c. Post-promotion smoke

After `Live`:

```bash
curl -X POST https://prospeqt-spintax.onrender.com/api/spintax \
  -b cookie.txt \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hi {{firstName}}.\n\nWe help small firms.\n\nTalk soon.","platform":"instantly"}'
```

The status response on this default-path job should have
`pipeline_diagnostics.pipeline == "beta_v1"`.

---

## Rollback

If anything goes wrong at Stage 3, rolling back is one env-var flip:

1. Run the same fetch + merge + PUT block from 3b but with
   `SPINTAX_PIPELINE=alpha`.
2. Render redeploys.
3. All new requests run alpha. Existing in-flight beta jobs finish on
   their own.

The beta runner stays in the codebase and stays available via the
admin toggle and `pipeline:"beta_v1"` override even after rollback.

---

## What's NOT in this runbook

- Adding alpha-vs-beta comparison to `scripts/benchmark.py` (deferred;
  alpha now has a clean callable runner via `spintax_runner_v2`-style
  wrapper if needed, but the existing benchmark intentionally stayed
  beta-only for v1).
- Per-account allocation gating (e.g. 10% of org X). Self-selected is
  fine for the small teammate pool we're testing with.
- Long-term cost dashboards. Per-job cost is on `/api/status` already;
  build aggregations later if useful.
