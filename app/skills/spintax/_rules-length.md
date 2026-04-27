---
name: spintax-rules-length
description: Character-count constraint for spintax variations. Every variation inside a spintax block must be within 5% character count of Variation 1.
type: reference
tags: [spintax, rules, length]
status: active
created: "2026-04-23"
---

# Length Rule — HARD CONSTRAINT

Every variation inside a spintax block must be within **5% character count** of Variation 1.

## Why this rule exists

Consistent rendered length makes the spintax invisible to two detection mechanisms:

1. **Length-fingerprinting spam filters.** When a sender fires the same sequence to 100 leads and the rendered email length varies by 40%, that variance is a weak but real signal the copy is templated. Keeping variation lengths tight removes that signal.
2. **Human pattern recognition.** If a recipient forwards two emails from the same campaign to a colleague, keeping rendered length consistent makes them look like identical writing rather than obvious templates with swapped synonyms.

This rule is new in the V2 pipeline. Old spintax copy does not follow it.

## How to count characters

- Count literal characters of each variation between pipes.
- Variables count as their literal placeholder text:
  - `{{icp_companies}}` = 18 characters (not the rendered value)
  - `{COMPANY}` = 9 characters
- Strip leading/trailing whitespace from each variation before counting.
- Apply the rule per sentence block, not per whole email.
- The wrapper itself (`{{RANDOM | }}` or `{ }`) is NOT counted — only the variation text.

## Tolerance

**5% of Variation 1 character count.** Examples:

| Variation 1 length | Allowed range for variations 2-5 |
|---|---|
| 40 chars | 38-42 |
| 80 chars | 76-84 |
| 120 chars | 114-126 |
| 200 chars | 190-210 |

The tolerance is set as a default in `spintax_lint.py` and can be adjusted via the `--tolerance` CLI flag if a specific campaign needs looser or tighter.

## How to hit the target

The approach that works in practice:

1. **Draft Variation 1 first.** It is the exact original. Lock its length.
2. **Draft Variations 2-5 one at a time, aiming at Variation 1's length.** Do not write all 4 quickly then adjust.
3. **If a variation runs long:** tighten with contractions (we'd / I've / you're), drop filler words ("just", "really", "actually"), cut adjectives that don't change the meaning.
4. **If a variation runs short:** add specifics — a number, a noun, a clarifying clause. Never pad with filler words like "basically" or "essentially."
5. **Count as you go.** Do not wait until all 5 are written to check. A long variation is easier to tighten than a short one is to extend naturally.

## What NOT to do

- **Do not pad with filler words** to hit the count. "Hey {{firstName}}, I just wanted to really quickly mention..." is worse than "Hey {{firstName}}, a quick note:" even if the former hits the target.
- **Do not stretch meaning.** Adding "for your company" when the sentence never discussed the company breaks variation equivalence.
- **Do not squeeze Variation 1.** Variation 1 is the exact original. It sets the target; it does not get edited to make other variations easier to balance.

## Verification

Every spintax output must pass the linter before delivery:

```bash
python tools/prospeqt-automation/scripts/spintax_lint.py \
    --platform {instantly|emailbison} \
    --file /tmp/spintax_draft.md
```

If the linter flags a block as out of tolerance, the output format is:

```
block 3 (line 18): variation 4 length 62 vs base 78 (diff 16 chars = 20.5%, limit 5%)
```

Fix the specific variation flagged. Do not just add words to pass the count. Rewrite so the variation naturally matches the length while preserving meaning and voice.
