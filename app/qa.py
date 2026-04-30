"""Quality checks beyond the deterministic linter.

What this does:
    Checks spintax output against the original input for fidelity issues
    that the structural linter cannot catch on its own:
        V1-fidelity   - Variation 1 of each block matches the corresponding
                        paragraph in the original input word-for-word.
        Block count   - Number of spintax blocks equals the number of
                        spintaxable paragraphs in the input.
        Greeting      - If the input starts with a greeting, only approved
                        professional greetings appear in the spun block.
        Duplicates    - No variation is repeated inside the same block.
        Smart quotes  - No curly quotes or apostrophes (warning).
        Doubled punct - No "!!", "??", or "..", etc. (warning).

What it depends on:
    - app/lint.py for extract_blocks and _split_variations
    - Python stdlib (argparse, json, re, sys, pathlib)

What depends on it:
    - Phase 1 route handler POST /api/qa will wrap qa()
    - app/spintax_runner.py (Phase 2) calls qa() after the lint loop succeeds

Source:
    Copied from
    /Users/mihajlo/Desktop/claude-code/tools/prospeqt-automation/scripts/qa_spintax.py
    on 2026-04-26 (Phase 0). The only adjustment from the source is the
    import path: `from spintax_lint import ...` becomes
    `from app.lint import ...` so the package import works in this repo.

Public API:
    qa(output_text, input_text, platform) -> dict
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from app.lint import extract_blocks, _split_variations

# Approved greeting variations. Variation 1 must match the input, the rest
# must be drawn from this whitelist.
APPROVED_GREETING_PATTERNS = [
    re.compile(r"^Hey\s+\{\{firstName\}\},?$"),
    re.compile(r"^Hi\s+\{\{firstName\}\},?$"),
    re.compile(r"^Hello\s+\{\{firstName\}\},?$"),
    re.compile(r"^Hey\s+there,?$"),
    re.compile(r"^\{\{firstName\}\},?$"),
]

# Informal greetings that fail QA.
INFORMAL_GREETING_WORDS = {
    "howdy",
    "heya",
    "hey y'all",
    "yo",
    "sup",
    "what's up",
    "dude",
    "greetings",
    "salutations",
    "good day",
    "cheers mate",
}

# Smart / curly quotes and apostrophes. Warning-level only.
SMART_QUOTE_CHARS = {"‘", "’", "“", "”", "′", "″"}

DOUBLED_PUNCTUATION_RE = re.compile(r"([!?.,])\1+")

# If the first non-empty line of the input matches any of these, treat it
# as the greeting paragraph.
GREETING_LINE_RE = re.compile(
    r"^\s*(hey|hi|hello|greetings|howdy)\b.*\{\{firstName\}\}.*$",
    re.IGNORECASE,
)

# Closing-line salutations that mark the start of an email signature block
# (e.g. "Best,", "Thanks,", "Regards,"). Matched against the FIRST line of a
# 1-2 line paragraph; line 2 (if present) is the sender's name and must be
# short + free of `{{variable}}` tokens.
CLOSING_SIGNATURE_LINE_RE = re.compile(
    r"^\s*(best|thanks|regards|cheers|warm\s+regards|sincerely|"
    r"kind\s+regards|thank\s+you)\s*,\s*$",
    re.IGNORECASE,
)


def _looks_like_closing_signature(lines: list[str]) -> bool:
    """True if `lines` is a closing email signature like ``Best,\\nDanica``.

    Strictly two non-empty lines: line 1 matches CLOSING_SIGNATURE_LINE_RE,
    line 2 is short (<=30 chars) and contains no `{{` tokens. A bare
    ``Best,`` on its own is NOT classified as a signature — in compact
    single-newline layouts that match would be a false positive (every
    instance of "Best," in mid-paragraph prose would get marked UNSPUN).

    Used by the validator to mark closing signatures as UNSPUN so a
    3-paragraph input like ``greeting / body / "Best,\\nDanica"`` yields 2
    spintax blocks rather than 3 — the signature stays verbatim.
    """
    if len(lines) != 2:
        return False
    if not CLOSING_SIGNATURE_LINE_RE.match(lines[0]):
        return False
    second = lines[1].strip()
    if len(second) > 30 or "{{" in second:
        return False
    return True


_VARIABLE_TOKEN_LINE_RE = re.compile(r"\s*\{\{[A-Za-z_][A-Za-z0-9_]*\}\}\s*")


def _classify_block(lines: list[str]) -> tuple[str, str]:
    """Classify a contiguous group of non-blank lines as PROSE or UNSPUN.

    The block has already had blank lines stripped; `lines` are the non-empty
    lines that survived. Returns (kind, joined_text).
    """
    p = "\n".join(lines)
    # All-bullet paragraph?
    if lines and all(line.lstrip().startswith("-") for line in lines):
        return ("UNSPUN", p)
    # Single-line variable token like `{{accountSignature}}`?
    if len(lines) == 1 and _VARIABLE_TOKEN_LINE_RE.fullmatch(lines[0]):
        return ("UNSPUN", p)
    # Closing email signature like "Best,\nDanica"?
    if _looks_like_closing_signature(lines):
        return ("UNSPUN", p)
    return ("PROSE", p)


def split_input_paragraphs(text: str) -> list[str]:
    """Split input into spintaxable paragraphs.

    Supports both classic and compact email layouts:

    - Classic: paragraphs separated by blank lines (``\\n\\n``).
    - Compact: each paragraph on its own line, no blank lines between
      (single-``\\n`` separators, common in cold-email writing).
    - Mixed: any combination of the above in the same input.

    Within a non-blank run, adjacent lines stay together ONLY when they
    form a recognized multi-line UNSPUN pattern:

    - All lines start with ``-`` (bullet list).
    - The lines look like a closing email signature
      (``Best,\\nDanica`` — closing word + short name).

    Otherwise each non-blank line becomes its own paragraph. This means
    a 6-line compact email yields 6 paragraphs; the same email with a
    ``Best,\\nDanica`` signature yields 5 PROSE + 1 UNSPUN.

    Returns a list of (kind, paragraph_text) tuples; ``kind`` is
    ``"PROSE"`` (must be spun) or ``"UNSPUN"`` (must stay verbatim).
    """
    # Normalize line endings.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    paragraphs: list[tuple[str, str]] = []
    pending: list[str] = []  # non-blank lines accumulating into one block

    def _flush_pending() -> None:
        # Decide whether the accumulated lines form a single multi-line
        # UNSPUN block (bullets / signature) or split into per-line PROSE.
        if not pending:
            return
        # Bullet group: all lines start with `-` -> single UNSPUN block.
        if len(pending) > 1 and all(ln.lstrip().startswith("-") for ln in pending):
            paragraphs.append(_classify_block(pending))
        # Closing-signature group (e.g. ``Best,\nDanica``): single UNSPUN block.
        elif len(pending) > 1 and _looks_like_closing_signature(pending):
            paragraphs.append(_classify_block(pending))
        else:
            # No multi-line pattern matched — emit each line as its own
            # paragraph so single-``\n`` layouts produce N paragraphs.
            for ln in pending:
                paragraphs.append(_classify_block([ln]))
        pending.clear()

    for raw_line in text.split("\n"):
        if raw_line.strip():
            pending.append(raw_line)
        else:
            # Blank line: flush whatever we've accumulated.
            _flush_pending()
    _flush_pending()
    return paragraphs


def spintaxable_input_paragraphs(text: str) -> list[str]:
    return [p for kind, p in split_input_paragraphs(text) if kind == "PROSE"]


def _normalize_whitespace(s: str) -> str:
    """Collapse all runs of whitespace (including newlines) to single spaces.
    Spintax blocks render on one line, so an input paragraph with an internal
    newline loses that newline in V1. We consider the text unchanged as long
    as the visible words/punctuation match when whitespace is normalized."""
    return re.sub(r"\s+", " ", s).strip()


def check_v1_fidelity(blocks_vars: list[list[str]], input_paragraphs: list[str]) -> list[str]:
    """Variation 1 of each block must match the corresponding input paragraph
    (whitespace-normalized - internal newlines in a paragraph collapse to
    spaces because spintax cannot preserve them)."""
    errors = []
    if len(blocks_vars) != len(input_paragraphs):
        # Count mismatch handled separately; skip this check in that case.
        return errors
    for i, (variations, original) in enumerate(zip(blocks_vars, input_paragraphs), start=1):
        if not variations:
            errors.append(f"block {i}: empty variations list")
            continue
        v1_norm = _normalize_whitespace(variations[0])
        orig_norm = _normalize_whitespace(original)
        if v1_norm != orig_norm:
            errors.append(
                f"block {i}: Variation 1 does not match original paragraph "
                f"(got {v1_norm[:60]!r}..., expected {orig_norm[:60]!r}...)"
            )
    return errors


def check_block_count(blocks_vars: list[list[str]], input_paragraphs: list[str]) -> list[str]:
    errors = []
    got = len(blocks_vars)
    expected = len(input_paragraphs)
    if got != expected:
        errors.append(
            f"block count mismatch: got {got} spintax blocks, expected {expected} "
            f"(one per spintaxable input paragraph)"
        )
    return errors


def _input_starts_with_greeting(text: str) -> bool:
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        return bool(GREETING_LINE_RE.match(line))
    return False


def check_greeting(blocks_vars: list[list[str]], input_text: str) -> list[str]:
    """If the input starts with a greeting, Block 1's variations must be
    approved greetings. Otherwise skip."""
    errors = []
    if not _input_starts_with_greeting(input_text):
        return errors
    if not blocks_vars:
        return errors
    first_block = blocks_vars[0]
    for i, v in enumerate(first_block, start=1):
        v_stripped = v.strip()
        lower = v_stripped.lower()
        matched = any(p.match(v_stripped) for p in APPROVED_GREETING_PATTERNS)
        hit_informal = next(
            (w for w in INFORMAL_GREETING_WORDS if re.search(r"\b" + re.escape(w) + r"\b", lower)),
            None,
        )
        if hit_informal:
            errors.append(
                f"block 1 variation {i}: informal greeting '{hit_informal}' not allowed ({v_stripped!r})"
            )
        elif not matched:
            errors.append(
                f"block 1 variation {i}: greeting not in approved whitelist ({v_stripped!r}). "
                f"Approved: 'Hey {{{{firstName}}}},', 'Hi {{{{firstName}}}},', "
                f"'Hello {{{{firstName}}}},', 'Hey there,', '{{{{firstName}}}},'"
            )
    return errors


def check_no_duplicate_variations(blocks_vars: list[list[str]]) -> list[str]:
    errors = []
    for i, variations in enumerate(blocks_vars, start=1):
        seen = {}
        for j, v in enumerate(variations, start=1):
            norm = re.sub(r"\s+", " ", v.strip()).lower()
            if norm in seen:
                errors.append(f"block {i}: variation {j} is a duplicate of variation {seen[norm]}")
            else:
                seen[norm] = j
    return errors


def check_no_smart_quotes(blocks_vars: list[list[str]]) -> list[str]:
    """Warning-level."""
    warnings = []
    for i, variations in enumerate(blocks_vars, start=1):
        for j, v in enumerate(variations, start=1):
            hits = sorted({c for c in v if c in SMART_QUOTE_CHARS})
            if hits:
                display = ", ".join(repr(c) for c in hits)
                warnings.append(f"block {i} variation {j}: smart quote(s) present ({display})")
    return warnings


# Concept-drift detection - added 2026-04-28.
# We observed gpt-5.5 inventing context that wasn't in the original (e.g. adding
# "in the first demo", "this quarter", "your team") when generating variations 2-5.
# This drift hurts cold email reply rates because the prospect can tell the
# variation is improvising rather than restating. The check flags variations
# that introduce too many new content words, OR contain hard-listed drift
# phrases. Warning-level - judgment territory, not strict like length tolerance.

# Hand-curated list of phrases we've actually seen the API hallucinate when
# spinning. Substring match (case-insensitive). If a phrase appears in V2-V5
# but NOT in V1, that's drift.
DRIFT_PHRASES = (
    # temporal markers (none of these are usually in the original V1)
    "this quarter",
    "this month",
    "this week",
    "this year",
    "next quarter",
    "next month",
    "next week",
    "next year",
    "first demo",
    "the demo",
    # hallucinated stakeholders the original didn't name
    "your team's",
    "your folks",
    "your people",
    "your reps",
)

# Common-word stoplist - words that don't carry concept weight even if they
# appear new in V2-V5. Kept short. Anything past these is meaningful drift.
_DRIFT_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "against",
        "also",
        "always",
        "another",
        "around",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "could",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "even",
        "ever",
        "every",
        "from",
        "further",
        "have",
        "having",
        "here",
        "into",
        "just",
        "like",
        "more",
        "most",
        "much",
        "must",
        "never",
        "only",
        "other",
        "over",
        "really",
        "same",
        "should",
        "some",
        "soon",
        "still",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "today",
        "under",
        "until",
        "very",
        "well",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "with",
        "would",
        "your",
        "yours",
        "you've",
        "we'll",
        "we've",
        "we're",
        "you'll",
        "we'd",
        "you'd",
    }
)

# How many net-new content words in a single variation before we warn.
# Tuned conservatively - synonym swaps usually introduce 1-2 new words.
# Real drift (gpt-5.5 examples we saw) added 4-5+ new content words.
_DRIFT_WORD_THRESHOLD = 4


def _content_words(text: str) -> set[str]:
    """Extract meaningful content words from a variation.

    Lowercased, alpha-only, len >= 4, not in the stoplist. Variables like
    {{firstName}} are stripped before tokenizing so they don't count.
    """
    # Strip {{variables}} entirely - they are anchors, not content.
    stripped = re.sub(r"\{\{[^}]+\}\}", " ", text)
    # Tokenize on non-letter characters.
    tokens = re.findall(r"[A-Za-z']+", stripped.lower())
    return {t for t in tokens if len(t) >= 4 and t not in _DRIFT_STOPWORDS}


def check_concept_drift(blocks_vars: list[list[str]]) -> list[str]:
    """Flag variations that introduce too many new concepts vs Variation 1.

    Two signals:
        1. Hard-list match: variation contains a phrase from DRIFT_PHRASES
           that does NOT also appear in V1.
        2. Word-set diff: variation introduces > _DRIFT_WORD_THRESHOLD content
           words that aren't in V1's content-word set.

    Both surface as warnings (not errors). The point is to make drift
    visible to the operator so they can decide if it crossed a line, not
    to gate the job.

    Returns warning strings. Skips blocks with fewer than 2 variations.
    """
    warnings = []
    for i, variations in enumerate(blocks_vars, start=1):
        if len(variations) < 2:
            continue
        v1 = variations[0]
        v1_lower = v1.lower()
        v1_words = _content_words(v1)

        for j, v in enumerate(variations[1:], start=2):
            v_lower = v.lower()

            # Signal 1: hard-list drift phrases
            for phrase in DRIFT_PHRASES:
                if phrase in v_lower and phrase not in v1_lower:
                    warnings.append(
                        f"block {i} variation {j}: drift phrase '{phrase}' not present in V1"
                    )

            # Signal 2: net-new content words above threshold
            v_words = _content_words(v)
            new_words = v_words - v1_words
            if len(new_words) > _DRIFT_WORD_THRESHOLD:
                sample = ", ".join(sorted(new_words)[:5])
                warnings.append(
                    f"block {i} variation {j}: {len(new_words)} new content "
                    f"words not in V1 (e.g. {sample})"
                )

    return warnings


def check_no_doubled_punctuation(blocks_vars: list[list[str]]) -> list[str]:
    """Warning-level. Some triple-dots are intentional; so only flag >= 2
    repeats for '!', '?' and 4+ for '.'."""
    warnings = []
    for i, variations in enumerate(blocks_vars, start=1):
        for j, v in enumerate(variations, start=1):
            for m in DOUBLED_PUNCTUATION_RE.finditer(v):
                seq = m.group(0)
                ch = seq[0]
                if ch in "!?" and len(seq) >= 2:
                    warnings.append(f"block {i} variation {j}: doubled '{ch}' ({seq!r})")
                elif ch == "." and len(seq) >= 4:
                    warnings.append(f"block {i} variation {j}: quadrupled '.' ({seq!r})")
                elif ch == "," and len(seq) >= 2:
                    warnings.append(f"block {i} variation {j}: doubled ',' ({seq!r})")
    return warnings


def qa(output_text: str, input_text: str, platform: str) -> dict[str, Any]:
    """Run all QA checks. Returns a structured result dict."""
    # Extract blocks from generator output.
    raw_blocks = extract_blocks(output_text, platform)
    blocks_vars = [_split_variations(block_text, platform) for _, block_text in raw_blocks]

    input_paragraphs = spintaxable_input_paragraphs(input_text)

    errors = []
    warnings = []

    errors += check_block_count(blocks_vars, input_paragraphs)
    errors += check_v1_fidelity(blocks_vars, input_paragraphs)
    errors += check_greeting(blocks_vars, input_text)
    errors += check_no_duplicate_variations(blocks_vars)

    warnings += check_no_smart_quotes(blocks_vars)
    warnings += check_no_doubled_punctuation(blocks_vars)
    warnings += check_concept_drift(blocks_vars)

    return {
        "passed": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "block_count": len(blocks_vars),
        "input_paragraph_count": len(input_paragraphs),
    }


def main():  # pragma: no cover
    p = argparse.ArgumentParser(description="Run QA checks on spintax output.")
    p.add_argument("--output", type=Path, required=True, help="Generated spintax file")
    p.add_argument("--input", type=Path, required=True, help="Original plain email input file")
    p.add_argument("--platform", choices=["instantly", "emailbison"], required=True)
    p.add_argument("--json", action="store_true", help="Emit result as JSON on stdout")
    p.add_argument("--quiet", action="store_true", help="Suppress warnings section")
    args = p.parse_args()

    if not args.output.exists():
        print(f"error: output file not found: {args.output}", file=sys.stderr)
        sys.exit(2)
    if not args.input.exists():
        print(f"error: input file not found: {args.input}", file=sys.stderr)
        sys.exit(2)

    output_text = args.output.read_text(encoding="utf-8")
    input_text = args.input.read_text(encoding="utf-8")

    result = qa(output_text, input_text, args.platform)

    if args.json:
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["passed"] else 1)

    if result["warnings"] and not args.quiet:
        print("QA WARNINGS:", file=sys.stderr)
        for w in result["warnings"]:
            print(f"  {w}", file=sys.stderr)
        print(file=sys.stderr)

    if result["errors"]:
        print("QA ERRORS:", file=sys.stderr)
        for e in result["errors"]:
            print(f"  {e}", file=sys.stderr)
        print(
            f"\nQA FAIL: {result['error_count']} error(s), {result['warning_count']} warning(s) "
            f"(blocks={result['block_count']}, expected={result['input_paragraph_count']})",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"QA PASS: 0 errors, {result['warning_count']} warning(s) "
        f"(blocks={result['block_count']}, expected={result['input_paragraph_count']})"
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
