---
name: spintax
description: Pipeline for creating spintax email variations for cold outreach. Orchestrates generation, AI-pattern scrubbing, length balancing, spam check, and platform formatting. Invoke when asked to run the new spintax pipeline or "spin v2".
title: "Spintax Pipeline"
type: reference
tags: [automation, spintax, pipeline]
status: active
created: "2026-04-23"
---

# Spintax Pipeline

Takes an email body and produces spintaxed variations that pass mechanical checks and read like a real human wrote them.

This is the V2 pipeline. The older single-file skills (`spintax-instantly`, `spintax-emailbison`) remain in place for head-to-head comparison. Do not remove them until this pipeline is validated on live campaigns.

## When to use

Triggers:
- "spin this email v2"
- "spin v2"
- "run spintax pipeline"
- Explicit request for pipelined / linted spintax

If the user just says "spin this email" without v2, use the old `spintax-instantly` or `spintax-emailbison` skill instead.

## Required inputs

1. **Email body** — plain text with `{{variables}}` (Instantly) or `{VARIABLES}` (EmailBison) intact.
2. **Platform** — `instantly` or `emailbison`. If not supplied, ask.

## Pipeline

Run these steps in order. Do not skip. Do not reorder.

### Step 1 — Load rules

Read these reference files from this skill directory:
- `_rules-ai-patterns.md` — banned AI openers, filler phrases, transitions, writing-style rules
- `_rules-length.md` — the 5% character-count constraint (HARD RULE)
- `_format-{platform}.md` — platform-specific syntax and spacing

Do NOT load `_rules-spam-words.md` yet. That list is large. Only load when you reach Step 4.

### Step 2 — Generate variations

For each sentence in the email, produce a spintax block with exactly 5 variations:

- Variation 1 = exact original, word for word. No changes.
- Variations 2-5 = 60-80% different from Variation 1 using BOTH:
  - **Strategy A: Synonym swaps.** Swap 40-50% of words for shorter or more direct synonyms.
  - **Strategy B: Restructuring.** Change sentence shape — statement to question, clause reorder, different opener, because/so/since flip.

While generating, actively apply all constraints from:
- `_rules-ai-patterns.md` — no banned words, no AI openers, no summary sentences, contractions mandatory, sentences under 25 words
- `_rules-length.md` — aim each variation at Variation 1's character count (5% tolerance)

### Step 3 — Self-check before linting

Before running the mechanical linter, re-read your output. Check:
- Every variable kept verbatim with correct brackets
- "Help" never swapped for assist / aid / support
- Financial / medical / legal terms preserved
- CTA (last line before signature) is a question in all 5 variations
- No em-dashes anywhere (hard ban)
- Variation 1 matches the original word for word

Fix anything obvious before moving on.

### Step 4 — Spam word check

Load `_rules-spam-words.md`. Scan every variation.

- If a spam word is **not** operationally required → replace with a neutral equivalent.
- If a spam word **is** load-bearing (e.g., "loan" for a fintech client, "mortgage" for a lender) → keep it, but:
  - Never in the subject line
  - Only once per email
  - Never combine two triggers in the same sentence
  - Flag it in your Step 6 delivery note

### Step 5 — Run the linter (MANDATORY)

Write the spintaxed output to a temp file, then run:

```bash
python tools/prospeqt-automation/scripts/spintax_lint.py \
    --platform {instantly|emailbison} \
    --file /tmp/spintax_draft.md
```

The linter checks:
1. **Length tolerance** — each variation within 5% of Variation 1 character count
2. **Em-dashes** — any occurrence fails
3. **Banned AI words** — hard list (utilize, leverage, etc.)
4. **Spam triggers** — warning only, not a fail
5. **Variable format** — platform-specific (ALL CAPS for EmailBison)
6. **Variation count** — exactly 5 per block

Exit code 0 means pass. Non-zero means errors — fix the flagged blocks and re-run. Repeat until the linter reports `PASS`.

**Do not ship spintax that has not been linted.** The linter is the enforcement mechanism. LLM judgment alone is not sufficient.

### Step 6 — Apply platform formatting

Re-read `_format-{platform}.md`. Confirm:
- Correct wrapper syntax (`{{RANDOM | ... }}` for Instantly, `{v1|v2|...}` for EmailBison)
- Correct blank-line spacing between blocks
- Correct variable casing (ALL CAPS for EmailBison)
- Correct salutation and sign-off placement

### Step 7 — Deliver

Output the final copy. Include a short report at the end:

- Number of sentence blocks spintaxed
- Linter status: `PASS` with char-count tolerance used
- Spam triggers deliberately kept (if any) with justification
- Any variations that hit the 5% tolerance boundary (so Mihajlo knows where the copy is tight)

## Absolute constraints

These are non-negotiable and override everything else:

- Variation 1 = exact original. Not "close to." Exact.
- Variables kept verbatim with correct brackets and casing.
- CTA must be a question in all 5 variations.
- Em-dashes (—) banned in every variation.
- Linter must pass before delivery.
- 5% character count tolerance per `_rules-length.md`.

## Why this shape

The old single-file skills put every rule in one 400-line doc and relied on LLM attention to enforce everything in one pass. That works for the easy rules (tone, style) but fails for the mechanical ones (char count, banned words, em-dashes) because LLMs drift on counting and list-matching.

This pipeline splits the work: the LLM handles judgment (flow, meaning, tone), and a Python linter handles mechanics (length, banned words, format). Shared rules live in one place, so Instantly and EmailBison no longer drift from each other.
