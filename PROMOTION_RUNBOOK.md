# Diversity Gate Promotion Runbook

**Service:** prospeqt-spintax-web (https://prospeqt-spintax.onrender.com)
**Render service ID:** `srv-d7nosqf7f7vs73fu9pc0`
**Spec:** `DIVERSITY_GATE_SPEC.md` Section 6.4
**Last updated:** 2026-05-04

---

## What promotion does

The diversity gate ships at **warning level** on Day 1. It scores variant
diversity, logs failures in the API response, but does NOT block bad
output from being returned to callers.

**Promotion** = flipping the gate from `warning` to `error` so:

- Block-level diversity failures count as errors (not warnings)
- The API response shows `passed: false` when diversity is below floor
- The runner triggers up to 1 auto-retry on diversity failure (up from 0)

This runbook covers the operator-side procedure for promoting (and
rolling back).

---

## Eligibility criterion (before promoting)

Per spec Section 6.4, promotion is eligible only after:

| Criterion | Target |
|-----------|--------|
| Consecutive days of pass-rate data | >= 7 |
| Daily diversity-pass rate | >= 70% |
| Operator confirmation | required |

If pass rate drops below 70% on any day in the window, the 7-day clock
resets. Promotion can only happen after a clean window.

To check pass rate, query the production logs (or the future admin UI)
for `diversity_passed: true` vs `diversity_passed: false` over the last
7 calendar days, grouped by day.

---

## Promotion procedure

### Step 1: Confirm the env var change point

The gate level is read from the `DIVERSITY_GATE_LEVEL` env var at process
startup time (in `app/qa.py`). Default is `"warning"` if unset.

To promote, set `DIVERSITY_GATE_LEVEL=error` on the Render service.
**Render env var changes trigger a redeploy automatically** so this is
the only required action on the deploy side.

### Step 2: Set the env var on Render

Two options:

#### Option A: Render dashboard (manual)

1. Open https://dashboard.render.com -> service `srv-d7nosqf7f7vs73fu9pc0`
2. Navigate to Environment -> Add Environment Variable
3. Set `DIVERSITY_GATE_LEVEL` = `error`
4. Save (this triggers a redeploy)
5. Wait for deploy to complete (~2-3 min)

#### Option B: Render API (scripted, preferred for audit trail)

Render's PUT `/env-vars` is destructive (replaces ALL vars), so always
fetch the current set, merge, then PUT the full list back. Reference:
`tools/accounts/render/api_key.md` for the API key.

```bash
# Fetch current vars
curl -H "Authorization: Bearer $RENDER_API_KEY" \
  https://api.render.com/v1/services/srv-d7nosqf7f7vs73fu9pc0/env-vars

# Add DIVERSITY_GATE_LEVEL=error to the list, then PUT back the merged set.
# Do NOT use partial PUT - it will wipe other env vars.
```

### Step 3: Verify the deploy

Once the deploy completes, hit `/api/qa` and check the `diversity_gate_level`
field in the response:

```bash
curl -X POST https://prospeqt-spintax.onrender.com/api/qa \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"output_text":"...","input_text":"...","platform":"instantly"}' \
  | jq '.diversity_gate_level'
```

Expected: `"error"`. If still `"warning"`, the env var did not take
effect - check the Render dashboard for the value, redeploy if needed.

The startup log line `app.qa: DIVERSITY_GATE_LEVEL resolved to '<value>'`
appears in the Render deploy log at process start. Confirm there too.

### Step 4: Notify the team

Per spec Section 6.4 promotion notification:

1. **Telegram:** post to the personal bot (`tools/accounts/telegram.md`,
   bot `@claude_code_386_bot`, chat ID `8052848572`)

   Message template:
   > Diversity gate promoted to ERROR on YYYY-MM-DD. Floors: BLOCK_AVG=0.30, BLOCK_PAIR=0.20, CTA_AVG=0.20, CORPUS_AVG=0.45 (warning only). Service redeploy complete.

2. **CHANGELOG.md:** append a line to `CHANGELOG.md` in the repo root
   and commit:

   ```
   ## YYYY-MM-DD
   - Diversity gate promoted from warning to error level. Floors unchanged.
   ```

   Commit message: `feat(qa): promote diversity gate from warning to error`

---

## Rollback procedure

If the promotion produces too many false positives (legitimate output
gets blocked) or surfaces a real issue:

### Step 1: Unset or revert the env var

On Render, either:
- Delete the `DIVERSITY_GATE_LEVEL` env var entirely (defaults back to `"warning"`)
- Or set `DIVERSITY_GATE_LEVEL=warning` explicitly

Either action triggers a redeploy.

### Step 2: Verify rollback

Same `/api/qa` check as Step 3 of promotion. Expected: `"warning"`.

### Step 3: Notify the team

Telegram message:
> Diversity gate rolled back to WARNING level on YYYY-MM-DD. Reason: <one-line>.

CHANGELOG.md append:
```
## YYYY-MM-DD
- Diversity gate rolled back from error to warning. Reason: <one-line>.
```

### Step 4: Investigate the trigger

Don't re-promote until the cause is understood and a fix is shipped.
Re-arm the 7-day clock from the rollback date.

---

## Promotion history

(append entries here as promotions/rollbacks happen)

| Date | Action | Operator | Notes |
|------|--------|----------|-------|
| 2026-05-04 | Phase A shipped at warning level | builder team | initial gate ship |

---

## Internals reference

- Env var: `DIVERSITY_GATE_LEVEL` (values: `"warning"` | `"error"`)
- Default: `"warning"` (set in `app/qa.py` via `os.environ.get(..., "warning")`)
- Read time: process startup
- Effect on `qa()`: dispatch logic at `app/qa.py` (search for `if DIVERSITY_GATE_LEVEL == "error":`)
- Effect on runner retry: `app/spintax_runner.py` (search for `should_retry_diversity`)
- Retry budget: `MAX_DIVERSITY_RETRIES = 1` in `app/spintax_runner.py`
- Floors (constants in `app/qa.py`): `BLOCK_AVG_FLOOR=0.30`, `BLOCK_PAIR_FLOOR=0.20`, `BLOCK_AVG_FLOOR_CTA=0.20`, `CORPUS_AVG_FLOOR=0.45`
