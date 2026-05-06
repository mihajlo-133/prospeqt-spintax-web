"""Shared LLM client for pipeline stages.

All four LLM-calling stages (splitter, profiler, synonym pool, block
spintaxer) route through this module so:

- Tests patch a single helper instead of the OpenAI SDK directly
- Cost / timeout / retry behavior stays consistent across stages
- The Responses-API call shape is defined once

V1 only supports gpt-5.x via the OpenAI Responses API. Anthropic and
the older Chat Completions path are intentionally out of scope for the
beta pipeline; alpha keeps its own runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import openai

from app.config import REASONING_MODELS, RESPONSES_MODELS, settings
from app.pipeline.contracts import PipelineStageError

DEFAULT_TIMEOUT_SEC = 300.0  # Bumped on 2026-05-06 after a real 7-block
# email synonym_pool call measured 130s in isolation - 180s was too close
# to that ceiling under any jitter. Alpha's tool-loop uses 540s; 300s
# covers any single stage call comfortably while still failing fast.

logger = logging.getLogger(__name__)


def _make_client() -> openai.AsyncOpenAI:
    """Build the async OpenAI client. Tests patch this to avoid network."""
    return openai.AsyncOpenAI(api_key=settings.openai_api_key)


async def call_llm_json(
    *,
    prompt: str,
    model: str,
    error_key: str,
    reasoning_effort: str = "medium",
    timeout: float = DEFAULT_TIMEOUT_SEC,
    on_api_call: Callable[[Any], None] | None = None,
    instructions: str | None = None,
) -> dict[str, Any]:
    """Call a Responses-API model and return the parsed JSON output.

    Stage modules wrap their stage-specific prompt and JSON contract
    around this helper. The helper itself is contract-free: it just
    enforces "model is gpt-5.x", "response is non-empty", and "response
    parses as JSON."

    Args:
      prompt: full user prompt; must instruct the model to return JSON
      model: OpenAI model name; must be in RESPONSES_MODELS for v1
      error_key: PipelineStageError.error_key to raise on any failure
      reasoning_effort: "low" | "medium" | "high"
      timeout: seconds before asyncio.TimeoutError -> PipelineStageError
      on_api_call: optional callback receiving response.usage (cost track)
      instructions: optional system-style instructions string

    Returns: dict parsed from the model's output_text.

    Raises: PipelineStageError(error_key=...) on every failure path.
    """
    if model not in RESPONSES_MODELS:
        raise PipelineStageError(
            error_key,
            detail=(
                f"Pipeline v1 requires a Responses-API model, got {model!r}. "
                f"Allowed: {sorted(RESPONSES_MODELS)}"
            ),
        )

    client = _make_client()
    is_reasoning = model in REASONING_MODELS

    kwargs: dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
    }
    if instructions:
        kwargs["instructions"] = instructions
    if is_reasoning:
        kwargs["reasoning"] = {"effort": reasoning_effort}

    try:
        resp = await asyncio.wait_for(
            client.responses.create(**kwargs),
            timeout=timeout,
        )
    except TimeoutError as e:
        raise PipelineStageError(
            error_key, detail=f"LLM call timed out after {timeout}s"
        ) from e
    except openai.OpenAIError as e:
        raise PipelineStageError(
            error_key, detail=f"OpenAI API error: {e}"
        ) from e

    if on_api_call is not None:
        try:
            on_api_call(resp.usage)
        except Exception:
            logger.exception("on_api_call callback failed (non-fatal)")

    text = getattr(resp, "output_text", "") or ""
    text = text.strip()
    if not text:
        raise PipelineStageError(error_key, detail="LLM returned empty output")

    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise PipelineStageError(
            error_key, detail=f"LLM returned invalid JSON: {e}"
        ) from e

    if not isinstance(parsed, dict):
        raise PipelineStageError(
            error_key,
            detail=f"LLM returned non-object JSON (got {type(parsed).__name__})",
        )

    return parsed
