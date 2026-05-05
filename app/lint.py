"""Deterministic linter for spintax email copy.

What this does:
    Runs structural and quality checks on spintax email copy. Pure functions,
    no I/O, no external dependencies beyond the Python standard library.

    Checks:
        1. Length tolerance  - each variation within N% of Variation 1
        2. Em-dashes         - any occurrence is a hard fail
        3. Banned AI words   - hard list (utilize, leverage, etc.)
        4. Spam triggers     - warning only, not a fail
        5. Variable format   - ALL CAPS required for EmailBison
        6. Variation count   - exactly 5 per block
        7. Invisible chars   - zero-width / soft-hyphen padding rejected

What it depends on:
    Python stdlib only (argparse, re, sys, pathlib).

What depends on it:
    - app/qa.py imports extract_blocks and _split_variations
    - app/spintax_runner.py (Phase 2) imports lint() for the tool-call loop
    - Phase 1 route handler POST /api/lint will wrap lint()

Source:
    Copied verbatim from
    /Users/mihajlo/Desktop/claude-code/tools/prospeqt-automation/scripts/spintax_lint.py
    on 2026-04-26 (Phase 0). When the source is updated, copy the new
    version here and re-run pytest to catch regressions.

Public API:
    lint(text, platform, tolerance, tolerance_floor) -> (errors, warnings)
    extract_blocks(text, platform) -> list[(offset, inner_text)]
    _split_variations(block_inner, platform) -> list[str]
    is_greeting_block(variations) -> bool
"""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_TOLERANCE = 0.05  # 5%
DEFAULT_TOLERANCE_FLOOR = 3  # minimum absolute char tolerance (protects short blocks)

INSTANTLY_BLOCK_OPEN_RE = re.compile(r"\{\{RANDOM\s*\|")
EMAILBISON_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

EM_DASH = "—"

# Professional greeting whitelist. If every variation of a block matches one
# of these patterns, the block is treated as a greeting and exempt from the
# length tolerance check. Greetings naturally have different lengths
# (e.g. "Hey there," vs "Hello {{firstName}},").
GREETING_PATTERNS = [
    re.compile(r"^Hey\s+\{\{firstName\}\},$"),
    re.compile(r"^Hi\s+\{\{firstName\}\},$"),
    re.compile(r"^Hello\s+\{\{firstName\}\},$"),
    re.compile(r"^Hey\s+there,$"),
    re.compile(r"^\{\{firstName\}\},$"),
]


def is_greeting_block(variations: list[str]) -> bool:
    """True if every variation matches an approved greeting pattern."""
    if not variations:
        return False
    for v in variations:
        stripped = v.strip()
        if not any(p.match(stripped) for p in GREETING_PATTERNS):
            return False
    return True


_GREETING_LOOKAHEAD_RE = re.compile(
    r"^(Hey|Hi|Hello)\b.*,\s*$|^\{\{firstName\}\},$",
    re.IGNORECASE,
)


def _looks_like_greeting_attempt(variations: list[str]) -> bool:
    """True if variation 1 looks like a greeting (salutation + comma).

    Used by lint() to detect blocks where the model tried to spin a
    greeting but produced variations outside the strict allowlist. In that
    case the block fails count/length checks for the wrong reason; emit
    a targeted error pointing the model at the allowlist instead.
    """
    if not variations:
        return False
    return bool(_GREETING_LOOKAHEAD_RE.match(variations[0].strip()))


# Invisible / zero-width characters. If any variation contains one, that's a
# hard error - the model is trying to game the length check without changing
# visible text. These render as nothing (or break rendering) in email clients.
INVISIBLE_CHARS = {
    "​": "ZERO WIDTH SPACE",
    "‌": "ZERO WIDTH NON-JOINER",
    "‍": "ZERO WIDTH JOINER",
    "⁠": "WORD JOINER",
    "﻿": "ZERO WIDTH NO-BREAK SPACE (BOM)",
    "­": "SOFT HYPHEN",
    "᠎": "MONGOLIAN VOWEL SEPARATOR",
    " ": "LINE SEPARATOR",
    " ": "PARAGRAPH SEPARATOR",
}

BANNED_AI_WORDS = [
    "utilize",
    "leverage",
    "facilitate",
    "optimize",
    "streamline",
    "robust",
    "seamless",
    "comprehensive",
    "innovative",
    "cutting-edge",
    "holistic",
    "synergy",
    "ecosystem",
    "empower",
    "transform",
    "navigate",
    "unlock",
    "deep dive",
    "circle back",
    "touch base",
    "bandwidth",
    "scalable",
    "actionable",
    "alignment",
    "exciting",
    "excited",
    "thrilled",
    "delighted",
    "fantastic",
    "wonderful",
    "amazing",
    "incredible",
    "exceptional",
    "outstanding",
    "remarkable",
    "game-changing",
    "groundbreaking",
    "revolutionary",
    "elevate",
    "subsequently",
    "nevertheless",
    "consequently",
    "furthermore",
    "moreover",
]

SPAM_TRIGGERS = [
    "100% free",
    "100% guaranteed",
    "100% off",
    "act now",
    "act fast",
    "act immediately",
    "apply now",
    "best deal",
    "big win",
    "buy now",
    "call now",
    "cash bonus",
    "cash out",
    "claim now",
    "click below",
    "click here",
    "click now",
    "deal ending soon",
    "don't delete",
    "don't hesitate",
    "double your money",
    "double your income",
    "double your wealth",
    "easy income",
    "earn extra cash",
    "exclusive deal",
    "expires today",
    "fantastic offer",
    "fast cash",
    "final call",
    "financial freedom",
    "free access",
    "free consultation",
    "free gift",
    "free money",
    "free trial",
    "get it now",
    "get started now",
    "guaranteed results",
    "hurry up",
    "incredible deal",
    "instant earnings",
    "instant savings",
    "limited time",
    "lowest price",
    "make money",
    "miracle cure",
    "money-back guarantee",
    "no catch",
    "no cost",
    "no obligation",
    "no strings attached",
    "once in a lifetime",
    "only available here",
    "order now",
    "order today",
    "pure profit",
    "risk-free",
    "satisfaction guaranteed",
    "save big money",
    "special offer",
    "special promotion",
    "take action",
    "this won't last",
    "urgent",
    "while supplies last",
    "will not believe",
]


def _has_top_level_pipe(s):
    """True if `s` contains a `|` at brace depth 0."""
    depth = 0
    for c in s:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "|" and depth == 0:
            return True
    return False


def _extract_instantly_blocks(text):
    """Find all `{{RANDOM | ... }}` blocks. Brace-depth aware - handles
    nested `{{variable}}` placeholders inside the block."""
    blocks = []
    i = 0
    while i < len(text):
        m = INSTANTLY_BLOCK_OPEN_RE.search(text, i)
        if not m:
            break
        start = m.start()
        content_start = m.end()
        depth = 1
        j = content_start
        closed = False
        while j <= len(text) - 2:
            if text[j : j + 2] == "{{":
                depth += 1
                j += 2
            elif text[j : j + 2] == "}}":
                depth -= 1
                if depth == 0:
                    blocks.append((start, text[content_start:j]))
                    i = j + 2
                    closed = True
                    break
                j += 2
            else:
                j += 1
        if not closed:
            break
    return blocks


def _extract_emailbison_blocks(text):
    """Find all single-brace spintax blocks `{v1|v2|...}`. Brace-depth aware -
    correctly handles nested `{VAR}` variables inside variations.
    A brace pair counts as spintax only if it contains a top-level pipe."""
    blocks = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        depth = 1
        j = i + 1
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    inner = text[i + 1 : j]
                    if _has_top_level_pipe(inner):
                        blocks.append((i, inner))
                    i = j + 1
                    break
            j += 1
        else:
            i += 1
    return blocks


def extract_blocks(text: str, platform: str) -> list[tuple[int, str]]:
    """Return list of (char_offset, inner_text) for each spintax block."""
    if platform == "instantly":
        return _extract_instantly_blocks(text)
    return _extract_emailbison_blocks(text)


def _split_variations(block_inner: str, platform: str) -> list[str]:
    """Split a spintax block's inner text on top-level pipes only -
    never on pipes inside nested `{{var}}` or `{var}` placeholders."""
    parts = []
    current = []
    depth = 0
    for c in block_inner:
        if c == "{":
            depth += 1
            current.append(c)
        elif c == "}":
            depth -= 1
            current.append(c)
        elif c == "|" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(c)
    parts.append("".join(current))
    return [p.strip() for p in parts]


def _reassemble_instantly(body: str, replacements: dict) -> str:
    """Splice new inner text into specific Instantly blocks.

    `replacements` maps 0-indexed block position to a new inner-text string
    in the same format extract_blocks returns (the content between the first
    `|` after RANDOM and the closing `}}`). Untouched blocks are preserved
    byte-for-byte.
    """
    out: list[str] = []
    cursor = 0
    block_idx = 0
    while cursor < len(body):
        m = INSTANTLY_BLOCK_OPEN_RE.search(body, cursor)
        if not m:
            out.append(body[cursor:])
            return "".join(out)
        out.append(body[cursor:m.start()])
        content_start = m.end()
        depth = 1
        j = content_start
        closed = False
        while j <= len(body) - 2:
            if body[j:j + 2] == "{{":
                depth += 1
                j += 2
            elif body[j:j + 2] == "}}":
                depth -= 1
                if depth == 0:
                    closed = True
                    break
                j += 2
            else:
                j += 1
        if not closed:
            out.append(body[m.start():])
            return "".join(out)
        if block_idx in replacements:
            prefix = body[m.start():content_start]
            out.append(prefix + replacements[block_idx] + "}}")
        else:
            out.append(body[m.start():j + 2])
        cursor = j + 2
        block_idx += 1
    return "".join(out)


def _reassemble_emailbison(body: str, replacements: dict) -> str:
    """Splice new inner text into specific EmailBison blocks (single brace)."""
    out: list[str] = []
    cursor = 0
    block_idx = 0
    while cursor < len(body):
        if body[cursor] != "{":
            out.append(body[cursor])
            cursor += 1
            continue
        depth = 1
        j = cursor + 1
        found_pipe = False
        end = -1
        while j < len(body):
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end == -1:
            out.append(body[cursor:])
            return "".join(out)
        inner = body[cursor + 1:end]
        # Top-level pipe check (mirrors _has_top_level_pipe semantics)
        d = 0
        for ch in inner:
            if ch == "{":
                d += 1
            elif ch == "}":
                d -= 1
            elif ch == "|" and d == 0:
                found_pipe = True
                break
        if not found_pipe:
            # Not a spintax block (e.g. plain `{VAR}`). Keep as-is.
            out.append(body[cursor:end + 1])
            cursor = end + 1
            continue
        if block_idx in replacements:
            out.append("{" + replacements[block_idx] + "}")
        else:
            out.append(body[cursor:end + 1])
        cursor = end + 1
        block_idx += 1
    return "".join(out)


def reassemble(body: str, replacements: dict, platform: str) -> str:
    """Replace specified spintax blocks in `body` with new inner text.

    Args:
        body: full spintax body
        replacements: {block_idx (0-based): new_inner_text}, where new_inner_text
            is in the same format extract_blocks returns (variations joined with
            `|`, no surrounding `{{RANDOM | ... }}` wrapper for instantly,
            no surrounding `{...}` wrapper for emailbison)
        platform: "instantly" or "emailbison"

    Returns the new body. Blocks not in `replacements` are byte-preserved.
    """
    if not replacements:
        return body
    if platform == "instantly":
        return _reassemble_instantly(body, replacements)
    return _reassemble_emailbison(body, replacements)


def check_length(variations, tolerance, floor_chars=DEFAULT_TOLERANCE_FLOOR):
    """Return list of length-related error strings.

    Tolerance policy: +/-(tolerance % of base) OR +/-floor_chars, whichever is
    larger. The floor protects very short sentences where a pure percentage
    is too tight to fit 5 meaningfully different variations.
    """
    issues = []
    if len(variations) != 5:
        issues.append(f"variation count: expected 5, got {len(variations)}")
        return issues

    base = variations[0]
    base_len = len(base)
    if base_len == 0:
        issues.append("Variation 1 is empty")
        return issues

    allowed_diff = max(base_len * tolerance, floor_chars)
    for i, v in enumerate(variations[1:], start=2):
        diff = abs(len(v) - base_len)
        if diff > allowed_diff:
            pct = (diff / base_len) * 100
            limit_desc = (
                f"limit {tolerance * 100:.0f}% or {floor_chars} chars floor - "
                f"effective {allowed_diff:.0f} chars"
            )
            issues.append(
                f"variation {i} length {len(v)} vs base {base_len} "
                f"(diff {diff} chars = {pct:.1f}%, {limit_desc})"
            )
    return issues


def check_em_dashes(variations):
    issues = []
    for i, v in enumerate(variations, start=1):
        if EM_DASH in v:
            issues.append(f"variation {i} contains em-dash")
    return issues


def check_invisible_chars(variations):
    """Zero-width / invisible Unicode chars are a hard error.
    The model uses them to pad length without visible text."""
    issues = []
    for i, v in enumerate(variations, start=1):
        hits = sorted({ch for ch in v if ch in INVISIBLE_CHARS})
        if hits:
            names = ", ".join(f"{INVISIBLE_CHARS[c]} (U+{ord(c):04X})" for c in hits)
            issues.append(f"variation {i} contains invisible character(s): {names}")
    return issues


def check_banned_words(variations):
    issues = []
    for i, v in enumerate(variations, start=1):
        lower = v.lower()
        for word in BANNED_AI_WORDS:
            pattern = r"\b" + re.escape(word.lower()) + r"\b"
            if re.search(pattern, lower):
                issues.append(f"variation {i} contains banned word: '{word}'")
    return issues


def check_spam_triggers(variations):
    """Return list of warning strings (not errors)."""
    warnings = []
    for i, v in enumerate(variations, start=1):
        lower = v.lower()
        for trigger in SPAM_TRIGGERS:
            pattern = r"\b" + re.escape(trigger.lower()) + r"\b"
            if re.search(pattern, lower):
                warnings.append(f"variation {i} contains spam trigger: '{trigger}'")
    return warnings


def check_variable_casing_emailbison(text):
    """Variables in EmailBison must be ALL CAPS. Returns errors."""
    issues = []
    seen = set()
    for m in EMAILBISON_VAR_RE.finditer(text):
        var = m.group(1)
        if var != var.upper() and var not in seen:
            seen.add(var)
            issues.append(f"variable '{{{var}}}' should be ALL CAPS: '{{{var.upper()}}}'")
    return issues


def lint(
    text: str,
    platform: str,
    tolerance: float,
    tolerance_floor: int = DEFAULT_TOLERANCE_FLOOR,
) -> tuple[list[str], list[str]]:
    """Run all checks. Return (errors, warnings) as lists of strings."""
    errors = []
    warnings = []

    blocks = extract_blocks(text, platform)
    if not blocks:
        errors.append("no spintax blocks found in input")
        return errors, warnings

    for idx, (offset, block_text) in enumerate(blocks, start=1):
        line_no = text[:offset].count("\n") + 1
        prefix = f"block {idx} (line {line_no})"

        variations = _split_variations(block_text, platform)

        # Greeting blocks are exempt from the length tolerance check.
        if not is_greeting_block(variations):
            if _looks_like_greeting_attempt(variations):
                # Block looks like a greeting but has invalid variation(s).
                # Without this branch the model would see a length-tolerance
                # error and try to fix it by adding/removing words — wrong
                # diagnosis for "Howdy {{firstName}}!" or similar. Point
                # the model at the strict greeting allowlist instead.
                invalid = [
                    v.strip()
                    for v in variations
                    if not any(p.match(v.strip()) for p in GREETING_PATTERNS)
                ]
                allowed = (
                    "Hey {{firstName}},  Hi {{firstName}},  Hello {{firstName}},  "
                    "Hey there,  {{firstName}},"
                )
                errors.append(
                    f"{prefix}: invalid greeting variation(s) {invalid!r}. "
                    f"Use EXACTLY one of: {allowed}"
                )
            else:
                for issue in check_length(variations, tolerance, tolerance_floor):
                    errors.append(f"{prefix}: {issue}")
        else:
            # Still enforce count == 5 for greeting blocks.
            if len(variations) != 5:
                errors.append(f"{prefix}: variation count: expected 5, got {len(variations)}")

        # If variation count is wrong, still run the other checks on what we have
        if not variations:
            continue

        for issue in check_em_dashes(variations):
            errors.append(f"{prefix}: {issue}")

        for issue in check_invisible_chars(variations):
            errors.append(f"{prefix}: {issue}")

        for issue in check_banned_words(variations):
            errors.append(f"{prefix}: {issue}")

        for warn in check_spam_triggers(variations):
            warnings.append(f"{prefix}: {warn}")

    if platform == "emailbison":
        for issue in check_variable_casing_emailbison(text):
            errors.append(issue)

    return errors, warnings


def main():  # pragma: no cover
    parser = argparse.ArgumentParser(
        description="Lint spintax email copy for length, em-dashes, banned words, and format.",
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=["instantly", "emailbison"],
        help="Target platform (determines spintax syntax).",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Path to copy file. If omitted, reads from stdin.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help=f"Length tolerance as fraction (default {DEFAULT_TOLERANCE} = 5%%).",
    )
    parser.add_argument(
        "--tolerance-floor",
        type=int,
        default=DEFAULT_TOLERANCE_FLOOR,
        help=f"Minimum absolute char tolerance (default {DEFAULT_TOLERANCE_FLOOR}). "
        f"Protects short blocks. Effective tolerance = max(base*percent, floor).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress warnings. Only print errors and PASS/FAIL summary.",
    )
    args = parser.parse_args()

    if args.file:
        if not args.file.exists():
            print(f"error: file not found: {args.file}", file=sys.stderr)
            sys.exit(2)
        text = args.file.read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()

    errors, warnings = lint(text, args.platform, args.tolerance, args.tolerance_floor)

    if warnings and not args.quiet:
        print("WARNINGS:", file=sys.stderr)
        for w in warnings:
            print(f"  {w}", file=sys.stderr)
        print(file=sys.stderr)

    if errors:
        print("ERRORS:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        print(
            f"\nFAIL: {len(errors)} error(s), {len(warnings)} warning(s)",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"PASS: 0 errors, {len(warnings)} warning(s)")
    sys.exit(0)


if __name__ == "__main__":
    main()
