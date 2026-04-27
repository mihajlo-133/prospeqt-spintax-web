# Batch Spintax API Specification
**Author:** Mihajlo + Claude | **Date:** 2026-04-27 | **Status:** LOCKED — ready to build

This document is the single source of truth for the batch spintax feature.
Engineer reads this before writing a single line of code.
QA audits against this when the feature ships.
No deviations without an explicit note in this file explaining the reason.

---

## 1. Why this exists

GTM strategists deliver email copy as messy `.md` files containing many segments
(Enavra: 5 segments × 2 emails = 10 bodies; HeyReach: 33 segments × ~2 emails = ~70 bodies).
The current tool spintaxes one body at a time. The team has been copy-pasting each segment
manually, which is the bottleneck.

This feature lets the team drop the `.md` file (or POST it from Claude Code) and get back
a `.zip` with one paste-ready `.md` per segment.

**Ultimate UX:** API-first. The web UI is a thin client. Claude Code drives the API
headlessly with a bearer token.

---

## 2. Scope

### In scope (Phase 1)
- `.md` parser using `o4-mini` to extract segment + email structure
- Pre-confirm response: segment count, names, total body count
- Batch runner: 5 concurrent jobs, auto-retry up to 3x per failed body
- `.zip` assembly: one `.md` per segment + `_summary.md` (+ `_failed.md` if any)
- Web UI: paste/upload `.md` → confirm → poll → download
- Daily spend cap: bump from $20 to $50

### In scope (Phase 2)
- Bearer-token auth on batch endpoints (separate from web session cookie)

### Out of scope (later)
- Slack notification when batch finishes
- Browser-close survival (currently: tab must stay open)
- Configurable concurrency per-batch

### Hard rules
- **Subjects are NEVER spintaxed.** Preserve verbatim from input.
- **One spintax model per batch.** Default `o3`. Configurable per request.
- **Parser flags ambiguity, never refuses.** User decides if the count is right.
- **Bodies that fail 3 retries get listed in `_failed.md`, batch still succeeds.**
- Don't ship spintax that fails the linter — same as single-body flow.

---

## 3. API contract

### 3.1 `POST /api/spintax/batch`

Submit a batch.

**Request body (JSON):**
```json
{
  "md": "<file content as string>",
  "platform": "instantly" | "emailbison",
  "model": "o3",                     // optional, default = settings.default_model
  "concurrency": 5,                  // optional, default = 5, max = 20
  "dry_run": true                    // optional, default = false
}
```

**Response when `dry_run=true` (returns in ~10s after parser pass, no spintax jobs fired):**
```json
{
  "batch_id": "bat_2026-04-27_a3b1c2",
  "parsed": {
    "segments": [
      { "name": "Agencies — Segment 1", "email_count": 2 },
      { "name": "Agencies — Segment 2", "email_count": 2 },
      { "name": "Sales — Segment 8 (Var A + Var B)", "email_count": 4, "warning": "sub_variations_split" }
    ],
    "total_bodies": 70,
    "warnings": [
      "Segment 8 had Variation A + Variation B — split into 4 emails"
    ]
  },
  "status": "parsed",
  "fired": false
}
```

**Response when `dry_run=false`:**
Same `parsed` block, plus:
```json
{
  "batch_id": "bat_2026-04-27_a3b1c2",
  "parsed": { ... },
  "status": "running",
  "fired": true,
  "total_jobs": 70
}
```

**Errors:**
- `400` — invalid platform / model / .md is empty
- `401` — bad/missing auth
- `429` — daily spend cap would be exceeded by this batch (estimate)
- `503` — OpenAI org not verified for requested model (e.g., o3-pro)

### 3.2 `GET /api/spintax/batch/{batch_id}`

Poll status.

**Response:**
```json
{
  "batch_id": "bat_2026-04-27_a3b1c2",
  "status": "running" | "done" | "failed" | "cancelled",
  "platform": "instantly",
  "model": "o3",
  "completed": 30,
  "failed": 1,
  "in_progress": 5,
  "queued": 34,
  "total": 70,
  "retries_used": 4,
  "elapsed_sec": 425,
  "eta_sec": 600,
  "cost_usd_so_far": 18.40,
  "cost_usd_estimated_total": 35.00,
  "download_url": "/api/spintax/batch/{batch_id}/download"  // present when status=done
}
```

### 3.3 `GET /api/spintax/batch/{batch_id}/download`

Get the `.zip`.

- Returns `application/zip` when `status == "done"`
- Returns `409 Conflict` if status is `running` or `queued`
- Returns `404` if batch_id unknown or expired (TTL: 24h after completion)

### 3.4 `POST /api/spintax/batch/{batch_id}/cancel`

Cancel an in-flight batch. In-progress jobs finish; queued jobs are dropped.

---

## 4. Parser (`o4-mini`)

### 4.1 What it does
Takes the raw `.md` content and returns structured JSON:

```python
[
  {
    "section": "Copy Agencies",        # optional top-level grouping (H1 in source)
    "segment_name": "Segment 1 — Follows Instantly + cold email + LinkedIn",
    "emails": [
      {
        "email_label": "Email 1",
        "subject_raw": "{{eat what you sell | per/seat margin | ...}}",
        "body_raw": "Hey {{firstName}},\n\nYou follow {{Instantly}}..."
      },
      { "email_label": "Email 2", "subject_raw": "...", "body_raw": "..." }
    ],
    "warnings": []
  }
]
```

### 4.2 Parser prompt (high-level)
- System: "You are extracting cold-email copy from a messy markdown file. The file
  is structured into segments. Each segment has 1-N emails. Each email has an
  optional subject line and a body. Return JSON matching the schema."
- User: "<the .md content>"
- Response format: JSON Schema (use OpenAI structured outputs)

### 4.3 Ambiguity handling
- If the parser is unsure about an email boundary → emit `warnings[]` on the
  segment and on the response top-level.
- Sub-variations (e.g., "Variation A" / "Variation B" inside a segment) →
  treat each as a separate email, name them `Segment N (Var A)` etc.,
  add warning `sub_variations_split`.
- Empty subjects → `subject_raw: ""` (preserve, do not infer).
- Bodies with already-spintaxed subjects (HeyReach pattern) → preserve as-is.

### 4.4 Cost estimate per parser pass
o4-mini at $1.10/M input + $4.40/M output. Average .md = 5k input tokens,
1k output tokens → ~$0.011 per parser pass. Negligible.

---

## 5. Job runner

### 5.1 Concurrency
Default 5 concurrent spintax jobs. Configurable via `concurrency` field
(min 1, max 20). Implemented as a bounded `asyncio.Semaphore`.

### 5.2 Retries
Each failed body retries up to 3x with exponential backoff (5s, 15s, 45s).
Failure types that trigger retry:
- `openai_timeout`
- `network_error`
- `transient_5xx`
- `max_tool_calls` (linter could not converge)
- `malformed_response`

Failure types that do NOT retry (escalate immediately):
- `openai_quota` — fail the whole batch, stop firing new jobs
- `openai_org_not_verified` — fail the whole batch
- `daily_spend_cap_exceeded` — fail the whole batch

### 5.3 Per-body output
Each body produces:
```python
{
  "segment_name": "...",
  "email_label": "Email 1",
  "subject_raw": "...",            # passed through verbatim
  "spintax_body": "{{RANDOM | v1 | v2 | ... }} ...",
  "lint_passed": True,
  "qa_passed": True,
  "qa_errors": [],
  "qa_warnings": [],
  "cost_usd": 0.42,
  "elapsed_sec": 87,
  "retries_used": 0
}
```

### 5.4 Spend cap enforcement
Before firing each job, check `current_daily_spend + estimated_job_cost <= cap`.
If would exceed → mark batch as `failed` with reason `daily_spend_cap_exceeded`.
Default cap: **$50/day** (bump from current $20). Configurable via env.

---

## 6. Output `.zip`

### 6.1 Filename convention
`spintax_batch_{batch_id}_{YYYY-MM-DD}.zip`

### 6.2 Contents
```
spintax_batch_bat_2026-04-27_a3b1c2_2026-04-27.zip
├── 01_agencies_segment_1.md
├── 02_agencies_segment_2.md
├── ...
├── 09_sales_segment_1.md
├── 10_sales_segment_8_var_a.md
├── 11_sales_segment_8_var_b.md
├── ...
├── _summary.md       ← always present
└── _failed.md        ← only if any body failed all 3 retries
```

Naming rule: `{ordinal:02d}_{section_slug}_{segment_slug}.md`,
where section_slug + segment_slug come from the parser output, lowercased,
non-alphanumeric → underscore.

### 6.3 Per-segment `.md` format

Rules:
- `Subject:` line is always present, even when empty.
- Subjects are passed through **verbatim from the source `.md`** — the spintax
  engine never touches them. If the strategist hand-wrote spintax in the subject
  (e.g., HeyReach pattern `{{a | b | c}}`), it's preserved exactly as written.
- Email 2 typically has an empty subject. The output reflects that — `Subject:`
  with nothing after the colon.
- `Email Body:` tag introduces the spintaxed body. Body is the only thing the
  spintax engine generates.

```markdown
# {segment_name}

_Section: {section}_
_Generated: {timestamp ISO 8601 UTC}_
_Model: {model}_

---

## Email 1

Subject: {subject_raw verbatim, may be empty}

Email Body:

{spintax_body}

---

## Email 2

Subject:

Email Body:

{spintax_body}
```

### 6.4 `_summary.md` format
```markdown
# Spintax Batch Summary

- Batch ID: bat_2026-04-27_a3b1c2
- Generated: 2026-04-27T13:42:00Z
- Platform: instantly
- Model: o3
- Concurrency: 5
- Total segments: 33
- Total bodies: 70
- Bodies completed: 69
- Bodies failed (after 3 retries): 1
- Total cost: $34.82
- Total elapsed: 14m 22s

## Per-segment results

| # | Segment | Bodies | Cost | Lint | QA |
|---|---------|--------|------|------|----|
| 01 | Agencies — Segment 1 | 2 | $1.05 | PASS | PASS |
| 02 | Agencies — Segment 2 | 2 | $0.92 | PASS | 1 warning |
| ... |
```

### 6.5 `_failed.md` format (only if failures)
```markdown
# Failed Bodies

The following bodies failed all 3 retries. Fix the source `.md` and re-submit
just those segments.

## Sales — Segment 12, Email 2
**Reason:** linter could not converge after 10 iterations
**Attempted retries:** 3
**Last error:** "5% character tolerance exceeded on Variation 3"

Source body:
```
[original body text]
```
```

---

## 7. Auth

### 7.1 Phase 1 (web UI only)
Uses existing session cookie. Same as current `/api/spintax`.

### 7.2 Phase 2 (Claude Code / headless)
Add `Authorization: Bearer <token>` support on batch endpoints.

- Tokens stored server-side as bcrypt-hashed strings in a single config file
  (`tokens.json` or env var `BATCH_API_TOKENS=tok1,tok2,...`).
- Each token has: `name`, `created`, `last_used` (no per-token rate limits in v1).
- Admin generates a token via `python -m app.tokens new --name "mihajlo-cli"`.
- Distribute via secure channel (1Password, Bitwarden, etc.) — never commit.

### 7.3 Auth fallback order
1. `Authorization: Bearer ...` header → check token table
2. Session cookie → check session validity
3. Else 401

---

## 8. Web UI

### 8.1 New page: `/batch`
- File upload OR textarea for `.md` content
- Platform selector (instantly / emailbison)
- Model selector (o4-mini / o3 / o3-pro)
- Concurrency selector (default 5)
- "Parse" button → POST `/api/spintax/batch` with `dry_run=true`

### 8.2 Confirm screen (after parse)
```
Parsed: 33 segments, 70 bodies

Segments:
  Copy Agencies (8 segments, 16 bodies)
    1. Segment 1 — 2 bodies
    2. Segment 2 — 2 bodies
    ...
  Copy Sales Teams (25 segments, 54 bodies)
    1. Segment 1 — 2 bodies
    ...
    8. Segment 8 (Var A + Var B) — 4 bodies   ⚠ split

[← Back]   [Spin all 70 →]
```

### 8.3 Progress screen (after firing)
- Top: status + ETA + cost so far
- Per-segment progress bar with color (queued/running/done/failed)
- "Cancel batch" button
- "Download .zip" button (enabled only when `status == done`)

### 8.4 Polling
Web UI polls `GET /api/spintax/batch/{id}` every 5s while `status == running`.

---

## 9. Test plan

### 9.1 Parser tests (unit)
- HeyReach `.md` (33 segments, ~70 bodies) → asserts segment count = 33,
  bodies = 70 ± 4 (parser variance), warnings include sub-variation flag for
  segment 8 of Sales.
- Enavra `.md` (5 segments × 2 emails = 10 bodies) → asserts segments = 5, bodies = 10.
- Empty `.md` → returns `parsed.total_bodies = 0` with warning.
- Malformed `.md` (no segments, just one body) → returns
  `parsed.segments = [{name: "Segment 1", email_count: 1}]` and a warning.

### 9.2 Job runner tests (mocked OpenAI)
- 70 bodies, 5 concurrent → at most 5 in flight at any time
- 1 body fails 3x → ends in `_failed.md`, batch still succeeds
- Daily cap exceeded mid-batch → batch fails with `daily_spend_cap_exceeded`
- Cancel mid-batch → in-flight finish, queued drop, status = cancelled

### 9.3 Integration tests
- POST batch with Enavra .md (real OpenAI) → completes in <5 min, .zip has 5 .md files
- Auth rejected with bad bearer token
- `_summary.md` matches actual job stats

### 9.4 Live validation
First real run = Enavra (10 bodies, ~$5 cost). Inspect:
- All 5 segment `.md` files present
- Subjects preserved verbatim (check escaped backslashes survive)
- Body spintax passes lint + QA
- `_summary.md` cost matches OpenAI dashboard

---

## 10. Phasing

### Phase 1 — Core batch (target: 2-3 days)
- [ ] `app/parser.py` — o4-mini parser with structured output
- [ ] `app/batch.py` — batch state, semaphore, retries
- [ ] `app/zip_builder.py` — assemble .zip + summary + failed
- [ ] `app/routes/batch.py` — three endpoints (POST, GET, download)
- [ ] Bump daily cap to $50 in `.env`
- [ ] Web UI: `/batch` page, confirm screen, progress screen
- [ ] Tests for parser + runner + zip
- [ ] Live test on Enavra .md

### Phase 2 — Bearer-token auth (target: 0.5 day)
- [ ] `app/tokens.py` — bcrypt-hashed token store
- [ ] CLI: `python -m app.tokens new --name X`
- [ ] Auth middleware update — bearer token first, session cookie fallback
- [ ] Doc: how to use from Claude Code

### Phase 3 (later)
- Slack notification when batch done (Slack incoming webhook URL in env)
- Browser-close survival via persistent state (SQLite or simple JSON)
- Configurable concurrency from web UI (currently API-only)

---

## 11. Open questions

None as of 2026-04-27. All decisions locked. Update this section if anything
changes during build.

---

## 12. Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-04-27 | Subjects never spintaxed | Always hand-written by GTM strategist |
| 2026-04-27 | Output uses `Subject:` and `Email Body:` tags | Predictable structure for the GTM engineer pasting into Instantly |
| 2026-04-27 | Email 2 subject is always empty by convention | Strategist convention; parser preserves whatever the source has, output reflects empty cleanly |
| 2026-04-27 | Output = .zip with 1 .md per segment | Matches GTM engineer's paste-into-Instantly workflow |
| 2026-04-27 | Parser = o4-mini | Cheap, fast, structured outputs work great |
| 2026-04-27 | Concurrency = 5 default | Conservative start, dial up after measuring rate-limit headroom |
| 2026-04-27 | Daily cap = $50 | Allows full HeyReach batch (~$35) + buffer |
| 2026-04-27 | Auto-retry 3x | Handles transient OpenAI flakiness without user intervention |
| 2026-04-27 | Parser flags, never refuses | User judges if count is right; AI shouldn't gatekeep |
| 2026-04-27 | Auth = bearer token (Phase 2) | Required for headless Claude Code use; web UI keeps session cookie |
| 2026-04-27 | Browser tab must stay open (Phase 1) | Slack delivery is Phase 3 polish |
