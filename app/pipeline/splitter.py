"""Stage 1 — Sentence Splitter.

Receives a plain email body and returns an ordered ``BlockList`` where each
entry is one sentence (or bullet point).  The ``lockable`` flag is set in
*code*, not by the model: a block is lockable when its text, after stripping
all ``{{placeholder}}`` tokens, has at least ``MIN_SPINTAXABLE_CHARS``
non-whitespace characters.

Public API::

    from app.pipeline.splitter import split_email

    block_list, diag = await split_email(plain_body)
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from app.pipeline.contracts import (
    MIN_SPINTAXABLE_CHARS,
    ERR_SPLITTER,
    Block,
    BlockList,
    PipelineStageError,
    SplitterDiagnostics,
)
from app.pipeline.llm_client import call_llm_json

# Regex that strips every {{...}} placeholder so we can measure the
# remaining "real" content length.
_PLACEHOLDER_RE = re.compile(r"\{\{[^}]+\}\}")

_SPLITTER_PROMPT_TEMPLATE = """\
You are a paragraph splitter for marketing email bodies.

Input: a plain email body. Output: a JSON object with a single key
"blocks" mapping to an ordered array of paragraph-level blocks. A
paragraph is a contiguous run of text bounded by newlines (single
newline OR blank line). Multi-sentence paragraphs MUST be kept
together as a single block - do NOT split on sentence boundaries
within a paragraph.

Rules:
- Split on paragraph boundaries (newlines). Each non-empty line in
  the input becomes one block. Skip lines that are entirely empty
  or whitespace-only.
- A paragraph that spans multiple sentences (e.g. "We offer X.
  Funds land in Y.") is ONE block, not two. The block's text
  contains both sentences with their original punctuation and the
  space/newline that joined them.
- Treat each bullet point as its own block.
- A line that is purely a placeholder (e.g. "{{{{accountSignature}}}}")
  is its own block.
- Preserve placeholders like {{{{firstName}}}} EXACTLY as written.
- Preserve all whitespace and punctuation WITHIN a paragraph
  (including trailing punctuation and inter-sentence whitespace).
  Drop only the inter-paragraph newlines themselves; the assembler
  reinstates them.
- Return blocks in the order they appear in the email.
- Block ids must be "block_1", "block_2", ... in order.

Output JSON shape (this is the ONLY allowed shape):
{{"blocks": [{{"id": "block_1", "text": "..."}}, {{"id": "block_2", "text": "..."}}]}}

Email body to split:
---
{plain_body}
---\
"""


def _is_lockable(text: str) -> bool:
    """Return True if *text* has enough non-placeholder content to spintax."""
    stripped = _PLACEHOLDER_RE.sub("", text)
    return len(stripped.strip()) >= MIN_SPINTAXABLE_CHARS


def _validate_blocks_response(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the raw dict returned by the LLM and return the blocks list.

    Raises ``PipelineStageError(ERR_SPLITTER, ...)`` on any structural
    problem so the caller never has to do ad-hoc dict access.
    """
    if "blocks" not in data:
        raise PipelineStageError(
            ERR_SPLITTER,
            detail="LLM response missing required 'blocks' key",
        )

    raw_blocks = data["blocks"]
    if not isinstance(raw_blocks, list):
        raise PipelineStageError(
            ERR_SPLITTER,
            detail=f"'blocks' must be a list, got {type(raw_blocks).__name__}",
        )

    for i, entry in enumerate(raw_blocks):
        if not isinstance(entry, dict):
            raise PipelineStageError(
                ERR_SPLITTER,
                detail=f"Block at index {i} is not an object",
            )
        for field in ("id", "text"):
            if field not in entry:
                raise PipelineStageError(
                    ERR_SPLITTER,
                    detail=f"Block at index {i} is missing required field '{field}'",
                )
            if not isinstance(entry[field], str):
                raise PipelineStageError(
                    ERR_SPLITTER,
                    detail=(
                        f"Block at index {i} field '{field}' must be a string, "
                        f"got {type(entry[field]).__name__}"
                    ),
                )

    return raw_blocks


async def split_email(
    plain_body: str,
    *,
    model: str = "gpt-5-mini",
    on_api_call: Callable[[Any], None] | None = None,
) -> tuple[BlockList, SplitterDiagnostics]:
    """Split a plain email body into sentence-level blocks.

    Args:
        plain_body: The raw email body text (no HTML).
        model: OpenAI Responses-API model name.
        on_api_call: Optional callback receiving ``response.usage`` for cost
            tracking (forwarded verbatim to ``call_llm_json``).

    Returns:
        A ``(BlockList, SplitterDiagnostics)`` tuple.

    Raises:
        ``PipelineStageError(error_key=ERR_SPLITTER)`` on every failure path.
    """
    # Step 1 — guard against empty input before touching the network.
    if not plain_body or not plain_body.strip():
        raise PipelineStageError(ERR_SPLITTER, detail="empty body")

    prompt = _SPLITTER_PROMPT_TEMPLATE.format(plain_body=plain_body)

    t_start = time.perf_counter()

    # Step 2 — call the model; llm_client raises PipelineStageError on
    # every failure, so we do not catch here.
    data = await call_llm_json(
        prompt=prompt,
        model=model,
        error_key=ERR_SPLITTER,
        reasoning_effort="low",
        on_api_call=on_api_call,
    )

    duration_ms = int((time.perf_counter() - t_start) * 1000)

    # Step 3 — validate structure.
    raw_blocks = _validate_blocks_response(data)

    # Step 4 — build Block objects with lockable set in code.
    blocks: list[Block] = []
    for entry in raw_blocks:
        text: str = entry["text"]
        blocks.append(
            Block(
                id=entry["id"],
                text=text,
                lockable=_is_lockable(text),
            )
        )

    block_list = BlockList(blocks=blocks)
    lockable_count = sum(1 for b in blocks if b.lockable)

    diagnostics = SplitterDiagnostics(
        block_count=len(blocks),
        lockable_count=lockable_count,
        duration_ms=duration_ms,
    )

    return block_list, diagnostics
