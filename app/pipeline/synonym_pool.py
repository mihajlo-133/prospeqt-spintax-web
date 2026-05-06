"""Stage 3 — Synonym Pool Generator.

Receives a ``BlockList`` and ``Profile`` (from stages 1 and 2) and sends
all lockable blocks to the LLM in a single batched call.  Returns a
``SynonymPool`` mapping block ids to synonym/syntax-option entries, plus
``SynonymPoolDiagnostics`` for observability.

Public API::

    from app.pipeline.synonym_pool import generate_synonym_pool

    pool, diag = await generate_synonym_pool(block_list, profile)
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from app.pipeline.contracts import (
    ERR_SYNONYM_POOL,
    BlockList,
    BlockPoolEntry,
    PipelineStageError,
    Profile,
    SynonymPool,
    SynonymPoolDiagnostics,
)
from app.pipeline.llm_client import call_llm_json

logger = logging.getLogger(__name__)

# Function words that should never appear as synonym keys.  The spintaxer
# gains nothing from swapping determiners, conjunctions, or prepositions —
# they do not drive meaning variation in marketing copy.
FUNCTION_WORDS: set[str] = {
    "the", "a", "an", "is", "are", "was", "were",
    "of", "and", "or", "but", "to", "in", "on", "at",
    "for", "with", "by", "from", "as", "it", "this", "that",
}

# Maximum allowed character-length difference between a synonym and its key.
_MAX_LENGTH_DELTA = 6

_SYNONYM_POOL_PROMPT_TEMPLATE = """\
You are a synonym pool generator for an email spintax system.

For each block in the email, return a synonym pool the spintaxer
can pick from. A block is one paragraph of the original email; it
may contain a single sentence OR multiple sentences that together
form one paragraph. Treat the block as one unit when generating
synonyms and syntax options. The spintaxer is STRICT: it can only
use words from the pool you provide.

Profile:
  Tone: {tone}
  Audience: {audience_hint}
  Locked nouns: {locked_common_nouns}
  Proper nouns: {proper_nouns}

For each block:

1. synonyms: dict of {{original_word: [synonym_options]}}
   - Only for content words (skip 'the', 'a', 'is', 'of', etc.)
   - Skip any word in locked_common_nouns or proper_nouns
   - Each synonym must MATCH THE TONE (no "cheerful" in a professional email)
   - Each synonym must be within +/- 3 chars of the original word's length
     (max +/- 6 in edge cases)
   - 3 to 5 synonyms per content word, drawn from across the whole block
     (multi-sentence blocks pool synonyms from every sentence)

2. syntax_options: 2-4 alternative phrasings of the entire block
   - Preserve meaning exactly across all sentences in the block
   - For multi-sentence blocks, the alternative phrasing should
     rephrase the whole block (you may merge two short sentences,
     split a long one, or keep the same sentence count - whichever
     reads more natural)
   - Preserve all placeholders ({{{{...}}}}) and locked nouns
   - Vary syntactic structure (clause order, voice, framing)
   - Do NOT introduce new content words; only reorder existing meaning

Blocks:
{blocks_json}

Output JSON shape: a single object whose keys are block ids and whose
values are objects with "synonyms" (dict) and "syntax_options" (list).
Example:
{{
  "block_1": {{
    "synonyms": {{"happy": ["pleased", "glad", "satisfied"]}},
    "syntax_options": ["I noticed your firm.", "Your firm caught my eye."]
  }}
}}\
"""


def _apply_synonym_filters(
    raw_synonyms: dict[str, list[str]],
    locked_nouns: set[str],
) -> dict[str, list[str]]:
    """Apply three synonym filters in order and return a cleaned dict.

    Filters applied:
    1. Drop keys that are locked nouns or proper nouns.
    2. Drop keys that are function words.
    3. Drop individual synonym strings whose length differs from the key
       by more than ``_MAX_LENGTH_DELTA`` characters.
    """
    filtered: dict[str, list[str]] = {}
    for key, synonyms in raw_synonyms.items():
        # Filter 1 — locked / proper nouns
        if key in locked_nouns:
            continue
        # Filter 2 — function words
        if key.lower() in FUNCTION_WORDS:
            continue
        # Filter 3 — length band per individual synonym
        kept = [
            syn for syn in synonyms
            if abs(len(syn) - len(key)) <= _MAX_LENGTH_DELTA
        ]
        filtered[key] = kept
    return filtered


async def generate_synonym_pool(
    block_list: BlockList,
    profile: Profile,
    *,
    model: str = "gpt-5-mini",
    on_api_call: Callable[[Any], None] | None = None,
) -> tuple[SynonymPool, SynonymPoolDiagnostics]:
    """Generate a synonym pool for all lockable blocks in one LLM call.

    Args:
        block_list: Output of the splitter stage.
        profile: Output of the profiler stage.
        model: OpenAI Responses-API model name.
        on_api_call: Optional callback receiving ``response.usage`` for cost
            tracking (forwarded verbatim to ``call_llm_json``).

    Returns:
        A ``(SynonymPool, SynonymPoolDiagnostics)`` tuple.

    Raises:
        ``PipelineStageError(error_key=ERR_SYNONYM_POOL)`` on every
        failure path.
    """
    # Step 1 — identify lockable blocks.
    lockable_blocks = [b for b in block_list.blocks if b.lockable]
    lockable_ids: set[str] = {b.id for b in lockable_blocks}

    # Step 2 — no-LLM short-circuit when there is nothing to spintax.
    if not lockable_blocks:
        return SynonymPool(), SynonymPoolDiagnostics()

    # Step 3 — build the prompt.
    blocks_json = json.dumps(
        [{"id": b.id, "text": b.text} for b in lockable_blocks],
        indent=2,
    )
    locked_nouns_list = profile.locked_common_nouns + profile.proper_nouns
    prompt = _SYNONYM_POOL_PROMPT_TEMPLATE.format(
        tone=profile.tone,
        audience_hint=profile.audience_hint or "general professional",
        locked_common_nouns=", ".join(profile.locked_common_nouns) or "none",
        proper_nouns=", ".join(profile.proper_nouns) or "none",
        blocks_json=blocks_json,
    )

    # Step 4 — call the model.
    t_start = time.perf_counter()
    data = await call_llm_json(
        prompt=prompt,
        model=model,
        error_key=ERR_SYNONYM_POOL,
        reasoning_effort="low",  # 2026-05-06: synonym lookup is mechanical;
        # medium reasoning measured at 130s on a 7-block email vs 60-90s at low.
        on_api_call=on_api_call,
    )
    duration_ms = int((time.perf_counter() - t_start) * 1000)

    # Step 5 — validate: all lockable ids must be present; extra ids are
    # silently dropped with a warning.
    returned_ids: set[str] = set(data.keys())
    extra_ids = returned_ids - lockable_ids
    if extra_ids:
        logger.warning(
            "synonym_pool: LLM returned unexpected block ids %s — dropping",
            sorted(extra_ids),
        )
    missing_ids = lockable_ids - returned_ids
    if missing_ids:
        raise PipelineStageError(
            ERR_SYNONYM_POOL,
            detail=f"LLM response missing required block ids: {sorted(missing_ids)}",
        )

    # Build the set of locked words for filter step.
    locked_words: set[str] = set(locked_nouns_list)

    # Steps 6-7 — filter synonyms and prune empty entries.
    pool_entries: dict[str, BlockPoolEntry] = {}
    for block_id in lockable_ids:
        raw_entry = data[block_id]

        # Tolerate a missing "synonyms" or "syntax_options" key gracefully.
        raw_synonyms: dict[str, list[str]] = raw_entry.get("synonyms", {}) if isinstance(raw_entry, dict) else {}
        raw_syntax: list[str] = raw_entry.get("syntax_options", []) if isinstance(raw_entry, dict) else []

        cleaned_synonyms = _apply_synonym_filters(raw_synonyms, locked_words)

        # Step 7 — prune empty entries.
        if not cleaned_synonyms and not raw_syntax:
            continue

        pool_entries[block_id] = BlockPoolEntry(
            synonyms=cleaned_synonyms,
            syntax_options=raw_syntax,
        )

    # Step 8 — compute diagnostics.
    total_synonyms = sum(
        len(syns)
        for entry in pool_entries.values()
        for syns in entry.synonyms.values()
    )
    blocks_covered = sum(
        1 for entry in pool_entries.values()
        if entry.synonyms or entry.syntax_options
    )

    pool = SynonymPool(blocks=pool_entries)
    diagnostics = SynonymPoolDiagnostics(
        total_synonyms=total_synonyms,
        blocks_covered=blocks_covered,
        duration_ms=duration_ms,
    )

    return pool, diagnostics
