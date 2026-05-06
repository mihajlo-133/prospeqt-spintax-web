"""Stage 2: Email Tone Profiler.

Detects tone, audience hint, domain-locked common nouns, and proper nouns
(brand/company/product names) from a plain email body.

Algorithm:
1. Regex pre-pass: detect proper-noun candidates (capitalized multi-word
   phrases, joined by & / and / of; all-caps acronyms 2-5 chars).
   Skip sentence-initial words and {{...}} placeholders.
2. Build profiler prompt including regex candidates.
3. Call LLM (reasoning_effort="low") for JSON with tone, audience_hint,
   locked_common_nouns, proper_nouns_added.
4. Validate response shape.
5. Build Profile: merge regex + LLM proper nouns; lowercase/dedup locked nouns.
6. Return (Profile, ProfilerDiagnostics).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from app.pipeline.contracts import (
    ERR_PROFILER,
    PipelineStageError,
    Profile,
    ProfilerDiagnostics,
)
from app.pipeline.llm_client import call_llm_json

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Placeholder pattern: {{anything}}
_PLACEHOLDER_RE = re.compile(r"\{\{[^}]+\}\}")

# Sentence boundary: split on ". ", "! ", "? " or start of string
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")

# Capitalized word (starts with uppercase, followed by one or more word chars)
_CAP_WORD = r"[A-Z][A-Za-z0-9]*"

# Multi-word proper noun: 1+ cap words optionally joined by " & ", " and ", " of "
_PROPER_NOUN_RE = re.compile(
    r"\b(?:"
    + _CAP_WORD
    + r"(?:\s+(?:&|and|of)\s+"
    + _CAP_WORD
    + r"|\s+"
    + _CAP_WORD
    + r")*)"
    + r"\b"
)

# All-caps acronyms: 2-5 uppercase letters (not followed by more caps to avoid
# matching words that happen to be all caps at sentence start)
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,5}\b")


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_PROFILER_PROMPT_TMPL = """\
You are an email tone profiler. You read a marketing email body and
extract four things:

1. tone: a short phrase describing register and voice
   (e.g. "professional B2B, consultative" or "casual, friendly").
   Be vague on tone, do NOT prescribe per-sentence intent.

2. audience_hint: who this email is for, if inferrable
   (e.g. "law firms", "BPO operators"). Use null if unclear.

3. locked_common_nouns: common nouns AND compound modifier-noun
   phrases that carry domain meaning and must NOT be swapped for
   synonyms. Single-word examples:
   - "clients" in legal/professional services
   - "patients" in healthcare
   - "tenants" in real estate
   - "candidates" in recruiting
   Compound / modifier-noun examples (lock the WHOLE phrase, not just
   the noun, when the phrase is a specific term-of-art):
   - "revenue-based funding" (a specific funding product, not just any
     funding - "income-based" or "sales-based" would be a different
     product)
   - "hard pull" (a specific credit-check operation, not just any pull)
   - "wire transfer" (a specific transfer method)
   - "credit check" (a specific verification step)
   Rule of thumb: if the noun is locked AND a hyphenated or compound
   adjective preceding it changes which specific thing is being
   referred to, lock the full phrase. Lowercase, plural form when
   appropriate. Skip generic nouns ("email", "thing", "way") - only
   domain-loaded ones.

4. proper_nouns_added: brand, company, or product names that must
   be preserved exactly. The regex pre-pass already detected:
   {regex_proper_nouns}
   Only return proper nouns the regex MISSED. If none, return [].

Output JSON shape (the ONLY allowed shape):
{{
  "tone": "...",
  "audience_hint": "..." | null,
  "locked_common_nouns": ["...", "..."],
  "proper_nouns_added": []
}}

Email body:
---
{plain_body}
---"""


# ---------------------------------------------------------------------------
# Regex pre-pass
# ---------------------------------------------------------------------------


def _detect_proper_nouns(plain_body: str) -> list[str]:
    """Detect proper-noun candidates from plain_body using regex.

    Rules:
    - Match runs of capitalized tokens optionally joined by &, and, of.
    - Also match all-caps acronyms (2-5 chars).
    - Skip the first word of every sentence (sentence-initial capitalization).
    - Skip anything inside {{...}} placeholders.
    - De-duplicate while preserving first-seen order.
    """
    # Collect sentence-initial words to exclude
    sentence_starts: set[str] = set()
    sentences = _SENTENCE_BOUNDARY_RE.split(plain_body)
    for sentence in sentences:
        # Strip placeholders from the front of each sentence before finding
        # the first real word
        stripped = _PLACEHOLDER_RE.sub("", sentence).strip()
        # First non-empty token
        first_tokens = stripped.split()
        if first_tokens:
            sentence_starts.add(first_tokens[0])

    # Build a scrubbed body with placeholders blanked out (preserve positions)
    scrubbed = _PLACEHOLDER_RE.sub(" ", plain_body)

    seen: dict[str, None] = {}  # ordered set via dict

    # Pass 1: multi-word / single cap-word runs (excluding sentence-initial)
    for m in _PROPER_NOUN_RE.finditer(scrubbed):
        candidate = m.group(0)
        # If it's a single token, check it's not sentence-initial
        tokens = candidate.split()
        if len(tokens) == 1 and candidate in sentence_starts:
            continue
        # Skip if the entire match is lower/mixed-single-char — shouldn't
        # happen with our pattern but be safe
        if candidate not in seen:
            seen[candidate] = None

    # Pass 2: all-caps acronyms not already captured
    for m in _ACRONYM_RE.finditer(scrubbed):
        candidate = m.group(0)
        # Single token: skip if sentence-initial
        if candidate in sentence_starts:
            continue
        if candidate not in seen:
            seen[candidate] = None

    return list(seen.keys())


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def profile_email(
    plain_body: str,
    *,
    model: str = "gpt-5-mini",
    on_api_call: Callable[[Any], None] | None = None,
) -> tuple[Profile, ProfilerDiagnostics]:
    """Run Stage 2: profile tone + extract locked nouns and proper nouns.

    Args:
        plain_body: The raw email body text (plain, not HTML).
        model: OpenAI Responses-API model to use.
        on_api_call: Optional callback passed to call_llm_json for cost tracking.

    Returns:
        (Profile, ProfilerDiagnostics) tuple.

    Raises:
        PipelineStageError(ERR_PROFILER, ...) on any failure.
    """
    if not plain_body or not plain_body.strip():
        raise PipelineStageError(ERR_PROFILER, detail="plain_body is empty")

    t0 = time.monotonic()

    # Step 1: regex pre-pass
    regex_proper_nouns = _detect_proper_nouns(plain_body)

    # Step 2: build prompt
    regex_list_str = (
        ", ".join(repr(n) for n in regex_proper_nouns)
        if regex_proper_nouns
        else "(none detected)"
    )
    prompt = _PROFILER_PROMPT_TMPL.format(
        regex_proper_nouns=regex_list_str,
        plain_body=plain_body,
    )

    # Step 3: call LLM
    raw = await call_llm_json(
        prompt=prompt,
        model=model,
        error_key=ERR_PROFILER,
        reasoning_effort="low",
        on_api_call=on_api_call,
    )

    # Step 4: validate response shape
    _validate_llm_response(raw)

    tone: str = raw["tone"]
    audience_hint: str | None = raw.get("audience_hint")
    llm_locked: list[str] = raw["locked_common_nouns"]
    llm_added: list[str] = raw["proper_nouns_added"]

    # Step 5a: locked_common_nouns — lowercase + de-duplicate
    locked_seen: dict[str, None] = {}
    for noun in llm_locked:
        key = noun.lower()
        if key not in locked_seen:
            locked_seen[key] = None
    locked_common_nouns = list(locked_seen.keys())

    # Step 5b: proper_nouns — union(regex, llm_added), regex first
    proper_seen: dict[str, None] = {}
    for noun in regex_proper_nouns:
        if noun not in proper_seen:
            proper_seen[noun] = None
    for noun in llm_added:
        if noun not in proper_seen:
            proper_seen[noun] = None
    proper_nouns = list(proper_seen.keys())

    duration_ms = int((time.monotonic() - t0) * 1000)

    # Step 6: build return objects
    profile = Profile(
        tone=tone,
        audience_hint=audience_hint,
        locked_common_nouns=locked_common_nouns,
        proper_nouns=proper_nouns,
    )
    diagnostics = ProfilerDiagnostics(
        duration_ms=duration_ms,
        tone=tone,
        locked_nouns=locked_common_nouns,
        proper_nouns=proper_nouns,
    )
    return profile, diagnostics


# ---------------------------------------------------------------------------
# Validation helper
# ---------------------------------------------------------------------------


def _validate_llm_response(raw: dict) -> None:
    """Validate LLM JSON response shape. Raises PipelineStageError on failure."""
    # tone: non-empty string
    tone = raw.get("tone")
    if not isinstance(tone, str) or not tone.strip():
        raise PipelineStageError(
            ERR_PROFILER,
            detail=f"LLM response missing or empty 'tone' field (got {tone!r})",
        )

    # locked_common_nouns: list of strings
    locked = raw.get("locked_common_nouns")
    if not isinstance(locked, list):
        raise PipelineStageError(
            ERR_PROFILER,
            detail=(
                f"LLM 'locked_common_nouns' must be a list, "
                f"got {type(locked).__name__!r}"
            ),
        )
    for i, item in enumerate(locked):
        if not isinstance(item, str):
            raise PipelineStageError(
                ERR_PROFILER,
                detail=f"'locked_common_nouns[{i}]' is not a string (got {item!r})",
            )

    # proper_nouns_added: list of strings
    added = raw.get("proper_nouns_added")
    if not isinstance(added, list):
        raise PipelineStageError(
            ERR_PROFILER,
            detail=(
                f"LLM 'proper_nouns_added' must be a list, "
                f"got {type(added).__name__!r}"
            ),
        )
    for i, item in enumerate(added):
        if not isinstance(item, str):
            raise PipelineStageError(
                ERR_PROFILER,
                detail=f"'proper_nouns_added[{i}]' is not a string (got {item!r})",
            )

    # audience_hint: str or None
    hint = raw.get("audience_hint")
    if hint is not None and not isinstance(hint, str):
        raise PipelineStageError(
            ERR_PROFILER,
            detail=f"LLM 'audience_hint' must be str or null (got {type(hint).__name__!r})",
        )
