"""Markdown parser for batch spintax input.

What this does:
    Takes a raw markdown file (the output of a GTM strategist's email-copy
    document) and extracts the structured segment + email tree using
    OpenAI's structured-outputs feature on a small, fast model (o4-mini).

    The strategist's docs follow a loose convention but are NEVER clean:
    escaped backslashes, mixed heading levels, typos, blank subject lines,
    sub-variations (Variation A / Variation B), occasional missing braces.
    A regex/markdown-AST parser would be brittle and we'd spend our days
    chasing edge cases. The model handles the messiness.

    Strategy for large docs:
    Big docs (HeyReach: 33 segments / 62k chars) are split on top-level
    `# ` headings into chunks, then chunks containing email markers are
    parsed in parallel. Each chunk's parse is bounded — no risk of the
    model "deciding" it's done after the first section. Single-section
    docs (Enavra) skip splitting and parse in one call.

What it depends on:
    - openai (AsyncOpenAI client)
    - app.config.settings (OPENAI_API_KEY)

What depends on it:
    - app/batch.py calls parse_markdown() before firing spintax jobs
    - app/routes/batch.py exposes parse output via the dry_run flag

Hard rules (per BATCH_API_SPEC.md section 4):
    - Subjects pass through verbatim. The parser NEVER rewrites them, even
      if the source already has hand-written spintax in the subject line.
    - Bodies pass through verbatim. The spintax engine takes the raw body
      and produces the spintaxed version separately.
    - Sub-variations (Variation A / Variation B inside a segment) become
      SEPARATE emails with a `sub_variations_split` warning on the segment.
    - Empty subjects (Email 2 convention) are preserved as empty strings.
    - The parser flags ambiguity with warnings; it never refuses or fails
      hard on weird input.

Cost expectation:
    Average .md = ~5k input tokens, ~1k output tokens. At o4-mini's
    $1.10/$4.40 per million tokens, ~$0.011 per parse. Negligible.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import openai

from app.config import RESPONSES_MODELS, settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ParsedEmail:
    """One email inside a segment."""

    email_label: str  # e.g. "Email 1", "Email 2", "Email 1 (Var A)"
    subject_raw: str  # verbatim from source, may be ""
    body_raw: str  # verbatim from source


@dataclass
class ParsedSegment:
    """One segment (= one .md output file in the final .zip)."""

    section: str  # top-level grouping, e.g. "Copy Agencies". May be "".
    segment_name: str  # e.g. "Segment 1 — Follows Instantly + cold email"
    emails: list[ParsedEmail] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParseResult:
    """Top-level parse output."""

    segments: list[ParsedSegment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_bodies(self) -> int:
        return sum(len(s.emails) for s in self.segments)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form for the API response."""
        return {
            "segments": [
                {
                    "section": s.section,
                    "segment_name": s.segment_name,
                    "email_count": len(s.emails),
                    "warnings": s.warnings,
                }
                for s in self.segments
            ],
            "total_bodies": self.total_bodies,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# JSON Schema for structured outputs
# ---------------------------------------------------------------------------

# Strict mode requires every property to be in `required` and
# `additionalProperties: false` on every object.
PARSER_SCHEMA: dict[str, Any] = {
    "name": "parsed_batch",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": (
                                "Top-level section heading the segment lives "
                                "under (e.g. 'Copy Agencies'). Empty string "
                                "if the segment is not under a section."
                            ),
                        },
                        "segment_name": {
                            "type": "string",
                            "description": (
                                "Human-readable segment label. Should match "
                                "what the strategist wrote (e.g. 'Segment 1 "
                                "— Follows Instantly + cold email + LinkedIn')."
                            ),
                        },
                        "emails": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "email_label": {
                                        "type": "string",
                                        "description": (
                                            "Email label. Use 'Email 1', "
                                            "'Email 2', etc. For sub-"
                                            "variations use 'Email 1 (Var A)' "
                                            "or 'Email 2 (Var B)'."
                                        ),
                                    },
                                    "subject_raw": {
                                        "type": "string",
                                        "description": (
                                            "Subject line verbatim from "
                                            "source. Empty string if the "
                                            "subject is missing/blank. "
                                            "DO NOT rewrite. DO NOT "
                                            "interpret hand-written spintax. "
                                            "Pass through character-for-"
                                            "character."
                                        ),
                                    },
                                    "body_raw": {
                                        "type": "string",
                                        "description": (
                                            "Email body verbatim from source. "
                                            "Multi-paragraph allowed. DO NOT "
                                            "rewrite. Preserve paragraph "
                                            "breaks. Strip leading/trailing "
                                            "whitespace ONLY."
                                        ),
                                    },
                                },
                                "required": [
                                    "email_label",
                                    "subject_raw",
                                    "body_raw",
                                ],
                                "additionalProperties": False,
                            },
                        },
                        "warnings": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Per-segment warnings such as "
                                "'sub_variations_split', "
                                "'missing_email_2', 'empty_body'."
                            ),
                        },
                    },
                    "required": ["section", "segment_name", "emails", "warnings"],
                    "additionalProperties": False,
                },
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Top-level warnings about overall structure. "
                    "Use sparingly — most concerns belong on the segment."
                ),
            },
        },
        "required": ["segments", "warnings"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a precise structural extractor for cold-email marketing documents.

A GTM strategist writes email-copy documents in markdown. Your job is to
extract the segment + email structure into JSON so that an automated
spintax engine can spin each body separately.

The documents follow a loose convention but are NEVER clean. You must
handle:

- Mixed heading levels (`###`, `####`, `##`, sometimes inconsistent)
- Escaped backslashes from Google Docs export (`pain\\_one`, `100\\+`)
- Typos in variable braces (`{{firstName}` missing the closing brace)
- Sub-variations inside a segment ("Variation A" / "Variation B")
- Blank subject lines (Email 2 typically has an empty subject)
- Sections that group multiple segments (e.g. "Copy Agencies" then
  "Copy Sales Teams")
- Subjects that already contain hand-written spintax like
  `Subj:{{eat what you sell | per/seat margin | flat-fee/stack}}`

EXTRACTION RULES:

1. **Find segments.** A segment usually starts with a heading like
   "Segment 1", "Segment N", or "DiscoLike Agencies Segment 5". Some
   documents prefix segments with a section heading like "Copy Agencies"
   or "Copy Sales Teams". Use that as the `section` field.

2. **Find emails inside each segment.** Look for "Email 1", "Email 2"
   markers. A segment usually has 1-2 emails, sometimes more.

3. **Sub-variations.** If a segment contains "Variation A" and
   "Variation B" sub-headings (typically when the strategist wants two
   parallel variants targeting different personas), split each into
   a separate email. Use labels like "Email 1 (Var A)" and
   "Email 1 (Var B)". Add the warning "sub_variations_split" to the
   segment's warnings array.

4. **Subject lines.** Look for "Subj:" or "Subject:" markers. Take the
   text AFTER the marker as `subject_raw`. If it's empty (Email 2
   convention) or missing, set `subject_raw` to an empty string.

   CRITICAL: DO NOT rewrite, clean, or interpret subject lines. If the
   strategist wrote `Subj:{{eat what you sell | per/seat margin}}`, you
   pass through `{{eat what you sell | per/seat margin}}` verbatim. The
   spintax engine NEVER touches subjects — they are always hand-written.

5. **Body content.** Take the email body content AS WRITTEN. Preserve
   paragraph breaks. Strip leading/trailing whitespace from the whole
   body. Keep escaped backslashes, typos, and weird formatting AS-IS —
   the strategist may want them. The spintax engine handles the body
   later.

6. **Skip non-email content.** SPECIFICALLY skip these structures:
   - Filter logic tables (rows with "Filter Logic", "Why Priority",
     "Pain Point", "Personalization" cells)
   - "List of Titles" / "List:" sections (lists of job titles)
   - Pain-point bullet lists outside an Email body
   - Intro paragraphs, ICP descriptions, segmentation overview text

   DO NOT skip a segment just because the section heading contains the
   word "Strategy". Some documents have a "Strategy Sales Teams"
   section followed by a "Copy Sales Teams" subsection — the COPY
   subsection contains real emails and MUST be extracted.

   The reliable signal that a chunk is an email is an "Email 1" /
   "Email 2" marker followed by Subject and body content.

7. **MULTIPLE SECTIONS WITH RESET NUMBERING.** A single document can
   contain TWO OR MORE top-level sections, each with its OWN segment
   numbering starting from 1. Common pattern in HeyReach docs:

       # Copy Agencies
         Segment 1, Segment 2, ... Segment 8
       # Copy Sales Teams (or "Strategy Sales Teams" + "Copy Sales Teams")
         Segment 1, Segment 2, ... Segment 25

   Both Section A's "Segment 1" AND Section B's "Segment 1" are valid
   distinct segments. Extract BOTH. Use the `section` field to
   disambiguate. NEVER stop after the first section's segments.

   If you find one section with N segments, scan the rest of the
   document for ANOTHER set of segments under a different section
   heading. Do not assume "I'm done" until you've read to the end.

8. **Warnings.** When something looks ambiguous, add a warning string
   on the segment (or the top-level result for global concerns). Common
   warnings:
   - "sub_variations_split" — segment had Var A/B and was split
   - "missing_email_2" — segment had Email 1 but no Email 2
   - "empty_body" — body looked truncated or blank
   - "ambiguous_segment_boundary" — heading levels were inconsistent
   - "subject_already_spintaxed" — informational, subject contained
     `{{a | b | c}}` style content

NEVER REFUSE. If the document is unparseable, return `segments: []` and
add a top-level warning explaining what went wrong. The downstream UI
will surface this to the user.

Output strictly conforms to the provided JSON schema. No commentary.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _make_client() -> openai.AsyncOpenAI:
    """Create the async OpenAI client. Tests patch this."""
    return openai.AsyncOpenAI(api_key=settings.openai_api_key)


# PARSER_MODEL is sourced from settings.parser_model so the env var
# OPENAI_PARSER_MODEL can swap o4-mini for gpt-5-mini etc. Read at
# module import time so existing tests that compare against PARSER_MODEL
# keep working.
PARSER_MODEL = settings.parser_model


async def _call_parser(
    client: openai.AsyncOpenAI,
    *,
    model: str,
    system_prompt: str,
    user_content: str,
) -> str:
    """Run the structured-output parse against either API surface.

    Returns the raw JSON string the model emitted. The caller is
    responsible for json.loads + dataclass conversion + warnings.

    Dispatch:
      - Models in RESPONSES_MODELS use /v1/responses with text.format.json_schema
      - Everything else uses /v1/chat/completions with response_format.json_schema

    The chat path sets `reasoning_effort="high"`; the responses path uses
    `reasoning={"effort": "high"}` (the SDK uses different parameter shapes
    for the two endpoints).
    """
    use_responses = settings.responses_api_enabled and model in RESPONSES_MODELS

    if use_responses:
        # Responses API expects a flat text.format object. PARSER_SCHEMA already
        # has name/strict/schema in the right shape — just add the discriminator.
        text_format = {
            "type": "json_schema",
            "name": PARSER_SCHEMA["name"],
            "schema": PARSER_SCHEMA["schema"],
            "strict": PARSER_SCHEMA["strict"],
        }
        response = await client.responses.create(
            model=model,
            instructions=system_prompt,
            input=[{"role": "user", "content": user_content}],
            text={"format": text_format},
            reasoning={"effort": "high"},
        )
        return getattr(response, "output_text", "") or ""

    # Chat completions path (o4-mini, o3, gpt-4.1, ...)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": PARSER_SCHEMA,
        },
        # o4-mini is a reasoning model — temperature is not honored.
        reasoning_effort="high",
    )
    return response.choices[0].message.content or ""


# Split threshold: docs larger than this are pre-split on top-level
# headings. Below this, we parse in one call. The boundary is set so
# that Enavra (~6k chars) stays single-call and HeyReach (~62k chars)
# always splits.
SPLIT_THRESHOLD_CHARS = 20_000

# Max chunks parsed in parallel. Conservative to avoid OpenAI rate limits.
MAX_CONCURRENT_CHUNK_PARSES = 5

# Heuristic for "this chunk contains emails": the regex matches an
# `Email 1` / `Email 2` / etc. label as a standalone marker. Used to
# skip "List of Titles" / strategy notes / segmentation tables.
_EMAIL_MARKER_RE = re.compile(r"(?im)^\s*#{0,6}\s*Email\s+\d", re.MULTILINE)

# Matches segment headings under any naming convention:
#   `## Segment 1`, `### Segment N`, `## DiscoLike Agencies Segment 5`
#   `## Segment A`, `## Segment B:` (Enavra letter convention)
# `\w+` covers digits, letters, and combinations. Used by:
#   - _split_chunk_by_segments to break large sections into sub-chunks
#   - _parse_single_chunk to count expected segments and feed the model
#     a count_hint (improves extraction reliability for big docs)
_SEGMENT_HEADING_RE = re.compile(
    r"(?im)^#+\s+(?:DiscoLike\s+Agencies\s+)?Segment\s+\w+",
)

# Max segments per sub-chunk when splitting a large section. Empirically,
# o4-mini drops segments past ~4 per call even with high reasoning. Smaller
# chunks = more API calls but reliable extraction. We trade cost for quality.
SUB_CHUNK_MAX_SEGMENTS = 4


def _split_on_h1(md_content: str) -> list[tuple[str, str]]:
    """Split markdown on top-level (`# `) headings.

    Returns list of (section_label, chunk_content) tuples. The section
    label is the heading text without the leading `# ` and any markdown
    bold/escape characters. Chunk content includes everything from the
    heading through the line before the next `# ` heading.

    If the document has 0 or 1 `# ` headings, returns a single
    (`""`, md_content) entry — caller treats as a single chunk.
    """
    lines = md_content.splitlines(keepends=True)

    # Find indices of `# ` lines (level-1 only; `## ` and below stay in chunk).
    h1_indices: list[int] = []
    for i, line in enumerate(lines):
        # Match `# ...` but not `## ...` / `### ...`
        if line.startswith("# ") and not line.startswith("## "):
            h1_indices.append(i)

    if len(h1_indices) <= 1:
        return [("", md_content)]

    chunks: list[tuple[str, str]] = []
    for idx, start in enumerate(h1_indices):
        end = h1_indices[idx + 1] if idx + 1 < len(h1_indices) else len(lines)
        section_label = _clean_heading(lines[start])
        chunk = "".join(lines[start:end])
        chunks.append((section_label, chunk))
    return chunks


def _clean_heading(line: str) -> str:
    """Strip `# `, markdown bold (`**`), and Google-Docs escape backslashes."""
    s = line.lstrip("#").strip()
    # Strip leading/trailing **
    if s.startswith("**") and s.endswith("**"):
        s = s[2:-2].strip()
    # Strip Google Docs escape backslashes (`\\.`, `\\+`, etc.)
    s = re.sub(r"\\(.)", r"\1", s)
    return s.strip()


def _has_email_marker(chunk: str) -> bool:
    """Return True if chunk contains at least one `Email N` marker."""
    return bool(_EMAIL_MARKER_RE.search(chunk))


def _split_chunk_by_segments(
    chunk: str,
    max_per_chunk: int = SUB_CHUNK_MAX_SEGMENTS,
) -> list[str]:
    """Split a large section chunk into smaller groups of segments.

    Returns a list of sub-chunks, each containing at most `max_per_chunk`
    segments. The chunk header (everything before the first segment
    heading) is prepended to each sub-chunk so the model retains section
    context (`# Copy Sales teams`, etc.).

    If the chunk has fewer segments than `max_per_chunk`, returns
    `[chunk]` unchanged.

    Empirically, o4-mini produces complete output for ~8 segments per
    call. Beyond that, it omits segments even at high reasoning effort.
    """
    matches = list(_SEGMENT_HEADING_RE.finditer(chunk))
    if len(matches) <= max_per_chunk:
        return [chunk]

    # Header = everything before the first `Segment` heading. Re-applied
    # to every sub-chunk so the model knows what section it's reading.
    header = chunk[: matches[0].start()]

    sub_chunks: list[str] = []
    for i in range(0, len(matches), max_per_chunk):
        start = matches[i].start()
        if i + max_per_chunk < len(matches):
            end = matches[i + max_per_chunk].start()
        else:
            end = len(chunk)
        sub_chunks.append(header + chunk[start:end])
    return sub_chunks


async def parse_markdown(md_content: str) -> ParseResult:
    """Parse raw markdown into structured segments via o4-mini.

    Splits large docs on top-level headings and parses chunks in parallel.
    Single-section docs are parsed in one call.

    Args:
        md_content: full markdown document as a string.

    Returns:
        ParseResult with segments and warnings populated. Never raises
        on parser ambiguity — returns an empty result with a top-level
        warning if the model can't extract anything.

    Raises:
        openai.RateLimitError: if the OpenAI quota is hit.
        openai.APITimeoutError / httpx.TimeoutException: on timeout.
        openai.NotFoundError: if the model isn't available to this org.
        ValueError: if md_content is empty.
    """
    if not md_content or not md_content.strip():
        raise ValueError("md_content must not be empty")

    # Decide whether to split.
    if len(md_content) <= SPLIT_THRESHOLD_CHARS:
        return await _parse_single_chunk(md_content, fallback_section="")

    chunks = _split_on_h1(md_content)
    if len(chunks) == 1:
        # Document is large but has no usable section boundaries.
        # Parse as one call.
        return await _parse_single_chunk(md_content, fallback_section="")

    # Filter to chunks that actually contain email markers.
    email_chunks: list[tuple[str, str]] = [
        (label, chunk) for label, chunk in chunks if _has_email_marker(chunk)
    ]

    if not email_chunks:
        logger.warning("parser: no chunk contains email markers")
        return ParseResult(
            segments=[],
            warnings=["no_email_markers_found_in_any_section"],
        )

    # Second-level split: if a section has many segments, break it into
    # sub-chunks of at most SUB_CHUNK_MAX_SEGMENTS each. Each sub-chunk
    # carries the section label so we can backfill correctly.
    sub_chunks_with_labels: list[tuple[str, str]] = []
    for label, chunk in email_chunks:
        for sub_chunk in _split_chunk_by_segments(chunk):
            sub_chunks_with_labels.append((label, sub_chunk))

    logger.info(
        "parser: %d email-bearing sections -> %d sub-chunks (skipped %d non-email)",
        len(email_chunks),
        len(sub_chunks_with_labels),
        len(chunks) - len(email_chunks),
    )

    sem = asyncio.Semaphore(MAX_CONCURRENT_CHUNK_PARSES)

    async def _bounded_parse(label: str, chunk: str) -> ParseResult:
        async with sem:
            return await _parse_single_chunk(chunk, fallback_section=label)

    chunk_results = await asyncio.gather(
        *[_bounded_parse(label, chunk) for label, chunk in sub_chunks_with_labels],
        return_exceptions=True,
    )

    return _merge_chunk_results(chunk_results, sub_chunks_with_labels)


async def _parse_single_chunk(
    md_content: str,
    fallback_section: str = "",
) -> ParseResult:
    """Parse one markdown chunk via a single o4-mini call.

    The fallback_section is used when the model returns an empty
    `section` field for a segment — useful when we pre-split and want
    to propagate the section label even if the model doesn't infer it.
    """
    client = _make_client()

    # Count segment headings in the chunk so we can tell the model the
    # expected count. The model otherwise tends to "give up early" and
    # return fewer segments than the document actually contains.
    expected_count = len(_SEGMENT_HEADING_RE.findall(md_content))
    count_hint = (
        f"\n\nIMPORTANT: This chunk contains {expected_count} `Segment` "
        f"heading(s). Your output MUST include all {expected_count} of them. "
        f"Do not stop early. Do not deduplicate. Even if two segments look "
        f"similar, extract both."
        if expected_count > 0
        else ""
    )

    user_content = (
        "Extract the segment + email structure from this "
        "markdown document. Return JSON matching the schema."
        f"{count_hint}\n\n"
        "DOCUMENT:\n"
        "```markdown\n"
        f"{md_content}\n"
        "```"
    )

    raw = await _call_parser(
        client,
        model=settings.parser_model,
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
    )
    if not raw.strip():
        logger.warning("parser: model returned empty content")
        return ParseResult(
            segments=[],
            warnings=["parser_returned_empty_response"],
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("parser: model returned invalid JSON: %s", exc)
        return ParseResult(
            segments=[],
            warnings=[f"parser_invalid_json: {exc}"],
        )

    result = _result_from_dict(data)

    # Backfill section label for segments where the model returned ""
    # but we know the chunk's section from pre-splitting.
    if fallback_section:
        for seg in result.segments:
            if not seg.section:
                seg.section = fallback_section

    return result


def _merge_chunk_results(
    chunk_results: list[Any],
    email_chunks: list[tuple[str, str]],
) -> ParseResult:
    """Combine ParseResult objects from multiple chunks into one.

    Exceptions from gather() are turned into top-level warnings so a
    single chunk failure doesn't doom the whole batch.
    """
    merged_segments: list[ParsedSegment] = []
    merged_warnings: list[str] = []

    for (label, _chunk), res in zip(email_chunks, chunk_results, strict=True):
        if isinstance(res, BaseException):
            logger.error(
                "parser: chunk %r failed: %s: %s",
                label,
                type(res).__name__,
                res,
            )
            merged_warnings.append(f"chunk_parse_failed: {label!r}: {type(res).__name__}: {res}")
            continue
        merged_segments.extend(res.segments)
        # Prefix per-chunk top-level warnings with the section label so
        # the user knows which chunk each one came from.
        for w in res.warnings:
            merged_warnings.append(f"[{label}] {w}" if label else w)

    return ParseResult(
        segments=merged_segments,
        warnings=merged_warnings,
    )


def _result_from_dict(data: dict[str, Any]) -> ParseResult:
    """Convert the model's JSON output into typed dataclasses.

    Defensive: if the model omits expected fields (despite the schema),
    we substitute empty defaults rather than crash.
    """
    segments: list[ParsedSegment] = []
    for seg in data.get("segments", []):
        emails = []
        for em in seg.get("emails", []):
            emails.append(
                ParsedEmail(
                    email_label=str(em.get("email_label", "")),
                    subject_raw=str(em.get("subject_raw", "")),
                    body_raw=str(em.get("body_raw", "")),
                )
            )
        segments.append(
            ParsedSegment(
                section=str(seg.get("section", "")),
                segment_name=str(seg.get("segment_name", "")),
                emails=emails,
                warnings=[str(w) for w in seg.get("warnings", [])],
            )
        )
    return ParseResult(
        segments=segments,
        warnings=[str(w) for w in data.get("warnings", [])],
    )
