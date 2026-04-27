---
name: spintax-format-emailbison
description: EmailBison platform format for spintax email copy. ALL CAPS variables in single curly braces and `{v1|v2|v3|v4|v5}` spintax wrapper with no spaces around pipes.
type: reference
tags: [spintax, format, emailbison]
status: active
created: "2026-04-23"
---

# EmailBison Format

## Variable syntax

- Single curly brackets, ALL CAPS inside: `{FIRST_NAME}`, `{COMPANY}`, `{LOAN_AMOUNT}`, `{NICHE1}`
- **Auto-convert any other casing to ALL CAPS.** Never keep lowercase or camelCase.

| Input | Converted |
|---|---|
| `{niche1}` | `{NICHE1}` |
| `{loan_amount}` | `{LOAN_AMOUNT}` |
| `{first_name}` | `{FIRST_NAME}` |
| `{companyName}` | `{COMPANYNAME}` |
| `{FirstName}` | `{FIRSTNAME}` |

## Spintax block syntax

```
{Variation 1 (exact original)|Variation 2|Variation 3|Variation 4|Variation 5}
```

Rules:
- Single curly braces (not double).
- Pipe separator with **NO spaces**: `|`.
- Exactly 5 variations per block. Variation 1 = exact original.

## Body spacing

Each sentence on its own line with a blank line between.

```
{FIRST_NAME},

{sentence 1 v1|v2|v3|v4|v5}

{sentence 2 v1|v2|v3|v4|v5}

{CTA v1|v2|v3|v4|v5}

{ACCOUNT_SIGNATURE}
```

## Critical difference from Instantly

- Instantly uses `{{double}}` braces for variables AND a `{{RANDOM | ... }}` wrapper.
- EmailBison uses `{single}` braces for both variables AND spintax blocks.
- The linter distinguishes spintax blocks from variables in EmailBison by looking for the pipe `|` character inside the braces.

This means: a single word in braces (e.g. `{FIRST_NAME}`) is a variable. Braces containing pipes (e.g. `{v1|v2|v3|v4|v5}`) is a spintax block.
