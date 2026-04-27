# Spintax API — Team Quickstart

Hands-on guide for Prospeqt teammates calling the spintax API from Claude Code or the terminal.

**Production URL:** https://prospeqt-spintax.onrender.com  
**Web UI:** open the URL, log in with `ADMIN_PASSWORD`  
**API auth:** `Authorization: Bearer <BATCH_API_KEY>` header  
**Get the secrets:** ask Mihajlo (1Password)

---

## 1. One-time setup

```bash
# 1) Clone the repo (or pull if you already have it)
cd ~
git clone git@github.com:mihajlo-133/prospeqt-spintax-web.git

# 2) Add to your shell profile (~/.zshrc or ~/.bashrc)
export SPINTAX_API_URL="https://prospeqt-spintax.onrender.com"
export SPINTAX_API_KEY="sk_batch_..."   # from 1Password

# 3) Reload
source ~/.zshrc
```

That's it. The CLI uses Python stdlib only — no `pip install` needed.

---

## 2. Spintax a file

### Single email body
Paste plain email copy into a file (or use stdin):

```bash
python3 ~/prospeqt-spintax-web/scripts/spintax_cli.py path/to/email.md
```

Output: `.zip` next to your input with a single `01_*.md` containing 5 spintax variations per block.

### Multi-segment markdown (Enavra, HeyReach, etc.)
```bash
python3 ~/prospeqt-spintax-web/scripts/spintax_cli.py path/to/segments.md
```

The CLI auto-detects multi-segment markdown. Output: `.zip` with one paste-ready `.md` per segment.

### From Claude Code
Just say:
> "spintax this file using the Prospeqt API: `~/Downloads/email_sequences.md`"

Claude runs the CLI for you and points you to the output `.zip`.

### Useful flags
```bash
--dry-run              # parse only, show structure, don't fire OpenAI jobs
--platform emailbison  # default is instantly
--model o4-mini        # default is o3 (also: o3-pro — needs org verification)
--concurrency 10       # default is 5
--output /tmp/x.zip    # custom zip path
--no-confirm           # skip the y/N prompt — useful for scripts
```

---

## 3. What the API does (and doesn't do)

### ✅ What it does
- Parses messy `.md` files (mixed heading levels, escaped backslashes, Google Docs export quirks) into structured segments
- Spintaxes the **body** of Email 1 (and Email 1 sub-variations like Var A/B) with 5 variations per sentence block
- Lints every variation against length tolerance, banned AI words, em-dash ban, etc.
- QA-checks the output against the original
- Auto-retries failed bodies up to 3x with backoff
- Returns a `.zip` with one paste-ready `.md` per segment

### ❌ What it DOESN'T do — important to know
- **Subjects are NEVER spintaxed.** Whatever you wrote stays exactly as-is, including hand-written spintax (HeyReach `{{a|b|c}}` pattern).
- **Email 2 is NEVER spintaxed.** Email 2 follow-ups pass through verbatim. Output `.md` still includes them, but their body is the original input plain text.
- Doesn't create campaigns in Instantly/EmailBison — that's the campaign-creator skill's job.
- Doesn't track who used the API (single shared bearer token).
- Doesn't survive browser close in the web UI yet (CLI is fine — it polls).

### Why these defaults?
1. **Subject ban**: Strategists hand-write subjects. AI rewrites lose the punch.
2. **Email 2 skip**: Follow-ups are short, contextual, and easier to hand-tune. Spintaxing them adds cost without adding much variation value.

---

## 4. Cost expectations

Daily cap: **$50 USD** (shared across the team, resets at midnight UTC).

| Doc size | Bodies | Cost | Wall time |
|---|---|---|---|
| Single email | 1 | ~$0.20-0.50 | 60-180s |
| Enavra-style (5 segments × 2 emails) | 10 (5 spun) | ~$1-1.50 | 4-5 min |
| HeyReach-style (33 segments × 2 emails, some Var A/B) | 76 (~33 spun) | ~$3-4 | 8-15 min |

**Why HeyReach time is high:** wall time is dominated by the slowest single job in parallel. Even with 5 concurrent workers, a few "expensive" Email 1 bodies pull the total up.

---

## 5. The output `.zip`

```
spintax_<filename>_2026-04-27_184557.zip
├── 01_segment_a_recent_large_fixed_price_awards.md
├── 02_segment_b_multi_year_long_performance_period_award.md
├── 03_segment_c_contract_modifications_scope_increases.md
├── 04_segment_d_small_business_set_aside_winners_sub_250.md
├── 05_segment_e_general_government_contractors_catch_all.md
├── _summary.md          ← cost, time, lint/QA per segment
└── _failed.md           ← only if anything failed all 3 retries
```

Each segment `.md` looks like:

```markdown
# Segment A: Recent Large Fixed-Price Awards

## Email 1
Subject: gov/{{contract_amount}}

Email Body:

{{RANDOM | Hey {{firstName}}, | Hi {{firstName}}, | Hello {{firstName}}, | ...}}

{{RANDOM | block 2 v1 | block 2 v2 | ... }}

...

## Email 2
Subject:

Email Body:

{{firstName}},
The {{awarding_agency}} contract you won...
- {{pain_one}}
- {{pain_two}}
...
```

**Naming rule:**
- Single-section docs (Enavra) → `01_segment_a_*.md` (no section prefix — section is variable across runs)
- Multi-section docs (HeyReach) → `01_copy_agencies_segment_1.md` / `09_copy_sales_teams_segment_1.md` (section disambiguates the segment numbers)

---

## 6. Chain with campaign creator skills

**Today: a 3-step manual chain.** In Claude Code:

> "spintax this file using the Prospeqt API and create draft Instantly campaigns for Enavra: `path/to/email_sequences.md`"

Claude will:
1. Run `spintax_cli.py` → get the `.zip`
2. `unzip` the output
3. For each `0N_segment_*.md`: read it, then call the `campaign-creator-instantly` skill with that segment's content

This works today. No new code needed.

**Future: an orchestrator skill** that chains the API → unzip → campaign creator automatically. Worth building after we use the manual chain for a few weeks and see what the actual workflow needs are.

---

## 7. Common issues

| Symptom | Fix |
|---|---|
| `401 Unauthorized` from CLI | `echo $SPINTAX_API_KEY` — make sure it's set in your shell |
| `daily_cap_hit` 429 response | Hit $50 daily cap. Wait until midnight UTC or ask Mihajlo to bump |
| `openai_org_not_verified` (using o3-pro) | Pick `o3` until org verification completes at platform.openai.com |
| Parse returns 0 segments | Doc has no `Segment N` markers. Add explicit `## Segment 1` headings or paste a single email body for single-mode |
| Expected 5 bodies, only 4 extracted | Heading levels inconsistent in source. Open issue, send Mihajlo a copy |
| .zip download 409 Conflict | Batch is still running. Wait for `status: done` |

---

## 8. Where to file bugs / requests

GitHub: https://github.com/mihajlo-133/prospeqt-spintax-web/issues  
Or ping Mihajlo directly with the `batch_id` (in the `_summary.md` of any zip).

---

## 9. Versioning

Auto-deploys on every push to `main`. To check the deployed version:

```bash
curl -s https://prospeqt-spintax.onrender.com/health
# returns {"status":"ok"} — health check only, doesn't expose version yet
```

Render dashboard shows the most recent commit hash. Mihajlo can roll back via the Render UI if a deploy breaks.
