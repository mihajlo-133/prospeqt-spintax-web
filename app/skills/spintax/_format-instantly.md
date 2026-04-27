---
name: spintax-format-instantly
description: Instantly platform format for spintax email copy. Double curly bracket variables and `{{RANDOM | ... }}` spintax wrapper.
type: reference
tags: [spintax, format, instantly]
status: active
created: "2026-04-23"
---

# Instantly Format

## Variable syntax

- Double curly brackets: `{{firstName}}`, `{{company}}`, `{{loan_amount}}`, `{{icp_companies}}`
- Keep exact casing as provided by the campaign doc. Do not convert case.
- Preserve bracket style verbatim. Never collapse to single braces.

## Spintax block syntax

```
{{RANDOM | Variation 1 (exact original) | Variation 2 | Variation 3 | Variation 4 | Variation 5}}
```

Rules:
- Wrapper is `{{RANDOM | ... }}` (capital RANDOM, double braces).
- Pipe separator with **spaces on both sides**: ` | `.
- Exactly 5 variations per block. Variation 1 = exact original.

## Body spacing — MANDATORY

Every element in the email body is separated by TWO blank lines (press Enter twice after each block).

```
{{firstName}},


{{RANDOM | sentence 1 v1 | v2 | v3 | v4 | v5}}


{{RANDOM | sentence 2 v1 | v2 | v3 | v4 | v5}}


{{RANDOM | CTA v1 | v2 | v3 | v4 | v5}}


{{accountSignature}}


{{RANDOM | opt-out v1 | ... | v28}}
```

Spacing rules:
- Salutation line (e.g. `{{firstName}},`) → TWO blank lines after
- Each `{{RANDOM ...}}` block → TWO blank lines after
- `{{accountSignature}}` → TWO blank lines after
- P.S. line (if present) → TWO blank lines after
- Opt-out spintax block → end of email, no trailing blank lines needed

## Example

```
{{firstName}},


{{RANDOM | Domains like {{redirect_domains}} (+{{redirect_count}} others) point to {{root_domain}}, so you're running cold email... | I spotted {{redirect_domains}} (+{{redirect_count}} more) pointing to {{root_domain}}, which tells me you're doing cold email... | {{redirect_domains}} and {{redirect_count}} other domains redirect to {{root_domain}} - clear sign cold email is already running... | Redirect domains like {{redirect_domains}} (+{{redirect_count}} others) trace back to {{root_domain}}, so cold email is in play... | Checked the records and {{redirect_domains}} (+{{redirect_count}} others) point to {{root_domain}} - you're running cold email...}}


{{RANDOM | Want me to send over the details? | Would details be useful? | Should I share more details? | Interested in the details? | Want the breakdown sent over?}}


{{accountSignature}}
```
