"""Async wrapper around the OpenAI tool-calling loop for spintax generation.

What this does:
    Drives the full reasoning-model + tool-calling loop:
        1. Build the system prompt from the skill markdown files.
        2. Call the OpenAI Chat Completions API with the lint_spintax tool.
        3. On each tool call, run the deterministic linter (app/lint.py).
        4. Iterate until the linter passes or the tool-call budget is hit.
        5. Update the job record (app/jobs.py) on every state transition.
        6. Track USD cost; bump the daily spend tracker (app/spend.py)
           after each run regardless of outcome.

What it depends on:
    - openai (AsyncOpenAI client)
    - httpx (for TimeoutException catching)
    - app.lint.lint (pure function used inside the lint tool wrapper)
    - app.qa.qa (pure function used after the model emits the final body)
    - app.jobs (state transitions + result attachment)
    - app.spend (cost accumulator)
    - app.config.settings (default model, OPENAI_API_KEY)
    - skill markdown files at app/skills/spintax/

What depends on it:
    - app/routes/spintax.py fires run() as an asyncio.create_task() and
      returns the job_id immediately to the caller.

Rule 3 compliance:
    `model` is late-bound from app.config.settings.default_model so the
    OPENAI_MODEL env var is the single source of truth. No model literal
    strings appear in this module.

State machine:
    queued -> drafting -> linting -> (iterating -> linting)* -> qa -> done
    Every transition fires a jobs.update() call. Errors map to 'failed'
    with a machine-readable error key.

The runner NEVER raises externally. Every exception path catches and
sets the job to 'failed' before returning.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import httpx
import openai

from app import jobs, spend
from app.config import (
    ANTHROPIC_MODELS,
    MODEL_PRICES,
    REASONING_MODELS,
    RESPONSES_MODELS,
    settings,
)
from app.jobs import (
    ERR_AUTH,
    ERR_BAD_REQUEST,
    ERR_LOW_BALANCE,
    ERR_MALFORMED,
    ERR_MAX_TOOL_CALLS,
    ERR_MODEL_NOT_FOUND,
    ERR_QUOTA,
    ERR_TIMEOUT,
    ERR_UNKNOWN,
    DiversityRetryDiagnostics,
    DiversityRevertRecord,
    DiversitySubCallRecord,
    JaccardCleanupDiagnostics,
    JaccardSubCallRecord,
    SpintaxJobResult,
)
from app.lint import (
    _split_variations,
    extract_blocks,
    lint as lint_body,
    reassemble,
)
from app.qa import (
    qa,
    BLOCK_AVG_FLOOR,
    BLOCK_PAIR_FLOOR,
    _diversity_tokens,
    _jaccard_distance,
)
from app.tools.schemas import ALL_SPINTAX_TOOLS
from app.tools.tool_impls import (
    SPINTAX_TOOL_NAMES,
    dispatch_anthropic,
    dispatch_chat,
    dispatch_responses,
)

# Re-export for any tests/imports that reach into this module.
__all__ = [
    "MODEL_PRICES",
    "REASONING_MODELS",
    "TOOL_LINT_SPINTAX",
    "DEFAULT_MAX_TOOL_CALLS",
    "build_system_prompt",
    "run",
]


TOOL_LINT_SPINTAX = {
    "type": "function",
    "function": {
        "name": "lint_spintax",
        "description": (
            "Run the deterministic Python linter on your current draft. "
            "Returns structured per-block errors and warnings. ALWAYS call "
            "this to verify character counts. Never attempt to count "
            "characters yourself - language models cannot count reliably."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "spintax_body": {
                    "type": "string",
                    "description": "The full spintax email body to check.",
                }
            },
            "required": ["spintax_body"],
            "additionalProperties": False,
        },
    },
}


def _to_responses_tool(chat_tool: dict[str, Any]) -> dict[str, Any]:
    """Convert a Chat-Completions tool spec to the Responses API flat shape.

    Chat:      {"type": "function", "function": {"name": ..., "parameters": ...}}
    Responses: {"type": "function", "name": ..., "parameters": ..., "strict": True}
    """
    fn = chat_tool["function"]
    return {
        "type": "function",
        "name": fn["name"],
        "description": fn.get("description", ""),
        "parameters": fn["parameters"],
        "strict": True,
    }


# Pre-built Responses-API shape of the lint tool for gpt-5.x calls.
TOOL_LINT_SPINTAX_RESPONSES = _to_responses_tool(TOOL_LINT_SPINTAX)


def _to_anthropic_tool(chat_tool: dict[str, Any]) -> dict[str, Any]:
    """Convert a Chat-Completions tool spec to the Anthropic Messages shape.

    Chat:      {"type": "function", "function": {"name", "description", "parameters"}}
    Anthropic: {"name", "description", "input_schema"}  -- no "function" wrapper,
               "parameters" renamed to "input_schema".
    """
    fn = chat_tool["function"]
    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "input_schema": fn["parameters"],
    }


# Pre-built Anthropic-Messages shape of the lint tool for claude-* calls.
TOOL_LINT_SPINTAX_ANTHROPIC = _to_anthropic_tool(TOOL_LINT_SPINTAX)


# Pre-built per-API shapes of the 8 spintax agent tools. Built once at
# import so the per-loop runners don't pay conversion cost on every call.
SPINTAX_TOOLS_CHAT: list[dict[str, Any]] = list(ALL_SPINTAX_TOOLS)
SPINTAX_TOOLS_RESPONSES: list[dict[str, Any]] = [_to_responses_tool(t) for t in ALL_SPINTAX_TOOLS]
SPINTAX_TOOLS_ANTHROPIC: list[dict[str, Any]] = [_to_anthropic_tool(t) for t in ALL_SPINTAX_TOOLS]


# Two separate budgets — Phase 4 introduced the 8 spintax agent tools
# alongside the existing lint_spintax tool. Conflating both under a single
# counter (the original DEFAULT_MAX_TOOL_CALLS) made the cap ambiguous:
# either lint retries gobbled the budget for agent-tool exploration, or
# vice versa. Splitting gives each surface its own knob and makes cost
# observable per benchmark run.
DEFAULT_MAX_LINT_CALLS = 10
DEFAULT_MAX_AGENT_TOOL_CALLS = 30

# Backwards-compatible alias for callers that still reference the old name
# (route handlers, jobs.update tool_calls_delta plumbing). Equal to the
# lint-call budget so existing default behavior is unchanged.
DEFAULT_MAX_TOOL_CALLS = DEFAULT_MAX_LINT_CALLS

# How many drift-revision passes to run after the initial generation.
# Set to 3 per product spec - gpt-5.5 was hallucinating context (e.g. "this
# quarter", "first demo") in initial output; we feed those warnings back to
# the model and ask for a corrected version. Cost is a non-issue here.
MAX_DRIFT_REVISIONS = 3

# Phase A diversity gate: auto-retry on diversity failure when
# DIVERSITY_GATE_LEVEL=='error'. Day 1 (warning level) skips retry entirely.
# See DIVERSITY_GATE_SPEC.md.
MAX_DIVERSITY_RETRIES = 1

# V2 per-block retry budget. Hard ceiling on incremental cost from
# diversity sub-calls within a single job. 13-40x typical job cost; tuned
# conservatively pending V2.1 proportional formula. Skipped if remaining
# budget below MIN_REMAINING_BUDGET_FOR_RETRY.
MAX_RETRY_COST_USD = 4.00
MIN_REMAINING_BUDGET_FOR_RETRY = 0.50

# Estimated USD cost per per-block sub-call. Used for the pre-loop budget
# gate. Real cost on gpt-5.5-pro is ~$0.02-0.05 per sub-call; conservative
# estimate so we don't enter a partial run we can't finish.
ESTIMATED_BLOCK_RETRY_COST_USD = 0.05

# V3 Workstream 1: per-block Jaccard cleanup phase. Sits between drift_retry
# exit and V2 retry start. Targets blocks where drift_retry shipped
# pure word-reorder pseudo-variants (Jaccard distance 0.0 vs V1) that
# QA's pair-floor flags but drift's content-word check accepts. Capped per
# block to bound cost; leftover blocks fall through to V2.
# See V3_DRIFT_JACCARD_AND_V2_RETRY_SPEC.md.
MAX_JACCARD_REPROMPTS_PER_BLOCK = 2

# Per-API-call wall-clock cap. The OpenAI Python SDK's default timeout
# does not reliably fire on reasoning-model "thinking" stalls (o3 / gpt-5.5
# can hold an ESTABLISHED TCP connection open for 30+ minutes with zero
# tokens streamed). We wrap every model call in asyncio.wait_for so a hung
# call surfaces as ERR_TIMEOUT instead of being SIGKILL'd by gunicorn's
# 600s worker timeout - which would also kill any other in-flight job
# co-located on that worker. See task #19.
#
# Two tiers:
#   - TOOL_LOOP_API_TIMEOUT_SEC: main generation call (drafting/iterating).
#     Set under gunicorn's 600s so we fail-fast inside the worker rather
#     than being killed mid-call.
#   - SUBCALL_API_TIMEOUT_SEC: per-block V2/V3 sub-calls. Tighter because
#     they regenerate ~1 paragraph, not a whole email; a stall here is
#     almost certainly a model hang, not legit deep reasoning.
TOOL_LOOP_API_TIMEOUT_SEC = 540
SUBCALL_API_TIMEOUT_SEC = 240


@dataclass
class LoopOutcome:
    """Result of running the tool-call loop against either API surface.

    Attributes:
        final_body: stripped final spintax body, empty string if loop failed.
        last_passed: True if the last lint call returned passed=true.
        tool_calls_made: total invocations across all tools (lint + agent).
            Kept for backwards compatibility with route/job plumbing that
            already references this name.
        lint_calls_made: count of lint_spintax invocations only.
        agent_tool_calls_made: count of the 8 spintax agent-tool invocations.
        agent_tool_breakdown: per-tool-name invocation count for the
            agent-tool surface. Empty dict when no agent tool was called.
        max_calls_reached: True if loop exited because the LINT budget was hit
            (kept for backwards compat — historically the only cap).
        agent_budget_exhausted: True if loop exited because the AGENT-TOOL
            budget was hit. Distinct from max_calls_reached so the caller
            can tell which knob to turn.
        rounds_exhausted: True if the for-loop hit its round cap without a
            final body emerging (rare; indicates the model kept tool-calling).
    """

    final_body: str
    last_passed: bool
    tool_calls_made: int
    lint_calls_made: int = 0
    agent_tool_calls_made: int = 0
    agent_tool_breakdown: dict[str, int] = field(default_factory=dict)
    max_calls_reached: bool = False
    agent_budget_exhausted: bool = False
    rounds_exhausted: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _skills_dir() -> Path:
    return Path(__file__).resolve().parent / "skills" / "spintax"


def _strip_wrapping(text: str) -> str:
    """Remove triple-backtick fences and trailing whitespace from model output."""
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip() + "\n"


def _lint_tool_wrapper(
    spintax_body: str,
    platform: str,
    tolerance: float,
    tolerance_floor: int,
) -> dict[str, Any]:
    """Invoke the deterministic linter and return a JSON-serializable dict."""
    errors, warnings = lint_body(spintax_body, platform, tolerance, tolerance_floor)
    return {
        "passed": len(errors) == 0,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }


def _is_int(val: Any) -> bool:
    """Return True if val is an actual integer (not a MagicMock auto-attribute or None)."""
    return isinstance(val, int) and not isinstance(val, bool)


def _compute_cost_anthropic(usage: Any, prices: dict[str, float]) -> dict[str, Any]:
    """USD cost from an Anthropic Messages API response.usage object.

    Anthropic uses the SAME `input_tokens`/`output_tokens` field names as the
    OpenAI Responses API. To avoid silently dropping cache fields (which
    materially affect cost when prompt caching is in play), the dispatcher
    in `_compute_cost` routes Anthropic models here explicitly.

    Cache-token billing multipliers:
      - cache_creation_input_tokens (write): real Anthropic billing is 1.25x
        base input price. v1 applies 1.0x (base) until verified against the
        billing dashboard. TODO: spike on first heavy-cache run.
      - cache_read_input_tokens (read): real Anthropic billing is 0.10x base
        input price. v1 applies 1.0x. TODO: same spike.

    Returns a dict with FOUR token counters (separate cache buckets) plus
    the USD total. Callers using `.get(...)` for OpenAI-only keys still work.
    """
    input_tok = getattr(usage, "input_tokens", None)
    if not _is_int(input_tok):
        input_tok = 0
    output_tok = getattr(usage, "output_tokens", None)
    if not _is_int(output_tok):
        output_tok = 0
    cache_create = getattr(usage, "cache_creation_input_tokens", None)
    if not _is_int(cache_create):
        cache_create = 0
    cache_read = getattr(usage, "cache_read_input_tokens", None)
    if not _is_int(cache_read):
        cache_read = 0

    # v1: apply base input price (1.0x) to all input buckets. Cache
    # multipliers (1.25x write, 0.10x read) are TODO post-spike.
    cost = (
        (input_tok / 1_000_000) * prices["input"]
        + (cache_create / 1_000_000) * prices["input"]
        + (cache_read / 1_000_000) * prices["input"]
        + (output_tok / 1_000_000) * prices["output"]
    )
    return {
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "reasoning_tokens": 0,  # Anthropic: thinking tokens are folded into output_tokens.
        "cache_creation_tokens": cache_create,
        "cache_read_tokens": cache_read,
        "total_cost_usd": cost,
    }


def _compute_cost(usage: Any, model: str) -> dict[str, Any]:
    """USD cost from a single OpenAI response.usage object.

    Handles both Chat Completions and Responses API usage shapes:
    - Chat:      prompt_tokens / completion_tokens / completion_tokens_details.reasoning_tokens
    - Responses: input_tokens  / output_tokens     / output_tokens_details.reasoning_tokens

    Anthropic models dispatch to `_compute_cost_anthropic` (separate cache
    token tracking).

    Falls back to zero-cost when the model is not in MODEL_PRICES (defensive).
    Uses _is_int to ignore MagicMock auto-attributes so tests with hand-built
    mocks of either shape produce identical USD for identical token counts.
    """
    prices = MODEL_PRICES.get(model, {"input": 0.0, "output": 0.0})

    # Anthropic dispatch: separate cache token bookkeeping.
    if model in ANTHROPIC_MODELS:
        return _compute_cost_anthropic(usage, prices)

    # Prefer Responses-API field names (gpt-5.x); fall back to Chat names (o3, o4-mini).
    in_resp = getattr(usage, "input_tokens", None)
    in_chat = getattr(usage, "prompt_tokens", None)
    if _is_int(in_resp):
        input_tok = in_resp
    elif _is_int(in_chat):
        input_tok = in_chat
    else:
        input_tok = 0

    out_resp = getattr(usage, "output_tokens", None)
    out_chat = getattr(usage, "completion_tokens", None)
    if _is_int(out_resp):
        output_tok = out_resp
    elif _is_int(out_chat):
        output_tok = out_chat
    else:
        output_tok = 0

    reasoning_tok = 0
    for details_attr in ("output_tokens_details", "completion_tokens_details"):
        details = getattr(usage, details_attr, None)
        if details is None:
            continue
        rt = getattr(details, "reasoning_tokens", None)
        if _is_int(rt):
            reasoning_tok = rt
            break

    input_cost = (input_tok / 1_000_000) * prices["input"]
    output_cost = (output_tok / 1_000_000) * prices["output"]
    return {
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "reasoning_tokens": reasoning_tok,
        "total_cost_usd": input_cost + output_cost,
    }


def _safe_fail(job_id: str, error: str, detail: str | None = None) -> None:
    """Update job to failed state. Silently ignores KeyError (job evicted).

    `detail` is the human-readable provider message (e.g. "credit balance is
    too low"). Surfaced to the UI via JobStatusResponse.error_detail. This
    is what tells you WHY it failed - the `error` code alone is not enough.
    """
    try:
        jobs.update(job_id, status="failed", error=error, error_detail=detail)
    except KeyError:
        logging.warning("spintax_runner: job %s evicted before fail update", job_id)


def _safe_update(job_id: str, **fields: Any) -> None:
    """Wrapper around jobs.update() that swallows KeyError (job TTL-evicted).

    Used during the loop where we want the runner to keep going if a job
    was already cleaned up (instead of crashing the asyncio task).
    """
    try:
        jobs.update(job_id, **fields)
    except KeyError:
        # job was evicted while we were running - log and continue
        logging.warning("spintax_runner: job %s missing during update", job_id)


def _set_progress(job_id: str, phase: str, label: str, **extra: Any) -> None:
    """Publish a live-progress payload on the job for the /api/status route.

    Shape: {"phase": <slug>, "label": <human-readable>, ...extra}. Replaces
    the existing progress dict; the runner is the single writer so a
    last-write-wins model is fine.

    Phases used:
        - "drafting": initial draft API call in flight
        - "drift_retry": running a drift-revision pass
        - "diversity_retry_subcall": running a per-block V2 sub-call
        - "diversity_retry_splice": reassembling spliced body
        - "diversity_retry_qa": re-running QA on spliced body
        - "diversity_retry_revert": reverting one block to pre-retry state
        - "qa": final QA pass (unchanged from existing behavior)

    `label` should be human-readable enough to display directly in a poller
    log without further interpretation.
    """
    payload: dict[str, Any] = {"phase": phase, "label": label}
    payload.update(extra)
    _safe_update(job_id, progress=payload)


def _make_openai_client() -> openai.AsyncOpenAI:
    """Create the async OpenAI client. Pulls API key from settings.

    Tests patch this function (or AsyncOpenAI) to avoid real network.
    """
    return openai.AsyncOpenAI(api_key=settings.openai_api_key)


def _make_anthropic_client() -> anthropic.AsyncAnthropic:
    """Create the async Anthropic client. Pulls API key from settings.

    Tests patch this function to avoid real network calls.
    """
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


# ---------------------------------------------------------------------------
# Tool-call loop adapters
#
# Two implementations against the same callback contract:
#   - _run_tool_loop_chat:      /v1/chat/completions (o3, o4-mini, gpt-4.1, ...)
#   - _run_tool_loop_responses: /v1/responses        (gpt-5.x)
#
# Both drive the lint_spintax tool until the model emits a final body or
# the tool-call budget is exhausted. State transitions (drafting/linting/
# iterating) and cost accumulation happen via callbacks supplied by run().
#
# OpenAI exceptions (RateLimitError, APITimeoutError, etc.) intentionally
# propagate out of these functions - run() catches them and translates to
# the job-failure error keys.
# ---------------------------------------------------------------------------


async def _run_tool_loop_chat(
    client: openai.AsyncOpenAI,
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    platform: str,
    tolerance: float,
    tolerance_floor: int,
    is_reasoning: bool,
    reasoning_effort: str,
    max_tool_calls: int,
    on_api_call: Callable[[Any], None],
    on_status: Callable[[str], None],
    on_tool_call_complete: Callable[[], None],
    max_lint_calls: int | None = None,
    max_agent_tool_calls: int = DEFAULT_MAX_AGENT_TOOL_CALLS,
) -> LoopOutcome:
    """Run the tool-call loop against /v1/chat/completions.

    Two independent budgets:
      - max_lint_calls (default = max_tool_calls for backwards compat):
        cap on lint_spintax retries. Hitting it returns max_calls_reached.
      - max_agent_tool_calls (default DEFAULT_MAX_AGENT_TOOL_CALLS):
        cap on the 8 spintax agent-tool invocations. Hitting it returns
        agent_budget_exhausted.

    The for-loop iteration cap is the SUM plus a small safety buffer so
    the inner caps are what actually terminate the loop.
    """
    if max_lint_calls is None:
        max_lint_calls = max_tool_calls
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    tools = [TOOL_LINT_SPINTAX, *SPINTAX_TOOLS_CHAT]
    lint_calls_made = 0
    agent_tool_calls_made = 0
    agent_tool_breakdown: dict[str, int] = {}
    last_passed = False

    for _round in range(max_lint_calls + max_agent_tool_calls + 5):
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        }
        if is_reasoning:
            kwargs["reasoning_effort"] = reasoning_effort
        else:
            kwargs["temperature"] = 0.6

        response = await asyncio.wait_for(
            client.chat.completions.create(**kwargs),
            timeout=TOOL_LOOP_API_TIMEOUT_SEC,
        )
        on_api_call(response.usage)

        msg = response.choices[0].message

        if msg.tool_calls:
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ],
            }
            messages.append(assistant_msg)

            for tc in msg.tool_calls:
                tool_name = tc.function.name
                is_lint = tool_name == "lint_spintax"
                is_agent = tool_name in SPINTAX_TOOL_NAMES

                # Per-surface cap check BEFORE running the tool.
                cap_hit_msg = None
                if is_lint and lint_calls_made >= max_lint_calls:
                    cap_hit_msg = (
                        f"Max lint_spintax calls ({max_lint_calls}) reached. Emit final body now."
                    )
                elif is_agent and agent_tool_calls_made >= max_agent_tool_calls:
                    cap_hit_msg = (
                        f"Max agent-tool calls ({max_agent_tool_calls}) reached. "
                        f"No more spintax tooling — call lint_spintax or emit final body."
                    )
                if cap_hit_msg is not None:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps({"error": cap_hit_msg}),
                        }
                    )
                    continue

                on_status("linting")

                if is_lint:
                    try:
                        args = json.loads(tc.function.arguments)
                        body = args.get("spintax_body", "")
                        tool_result = _lint_tool_wrapper(body, platform, tolerance, tolerance_floor)
                    except Exception as exc:  # noqa: BLE001
                        tool_result = {
                            "passed": False,
                            "error_count": 1,
                            "warning_count": 0,
                            "errors": [f"Tool failed: {exc}"],
                            "warnings": [],
                        }
                elif is_agent:
                    try:
                        tool_result = await dispatch_chat(tool_name, tc.function.arguments)
                    except Exception as exc:  # noqa: BLE001
                        tool_result = {"error": f"Spintax tool {tool_name!r} failed: {exc}"}
                else:
                    tool_result = {
                        "passed": False,
                        "error_count": 1,
                        "warning_count": 0,
                        "errors": [f"Unknown tool: {tool_name}"],
                        "warnings": [],
                    }

                if is_lint:
                    lint_calls_made += 1
                elif is_agent:
                    agent_tool_calls_made += 1
                    agent_tool_breakdown[tool_name] = agent_tool_breakdown.get(tool_name, 0) + 1
                on_tool_call_complete()

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(tool_result),
                    }
                )

                if is_lint:
                    last_passed = bool(tool_result.get("passed"))
                    if not last_passed:
                        on_status("iterating")
                        if lint_calls_made >= max_lint_calls:
                            return LoopOutcome(
                                final_body="",
                                last_passed=False,
                                tool_calls_made=lint_calls_made + agent_tool_calls_made,
                                lint_calls_made=lint_calls_made,
                                agent_tool_calls_made=agent_tool_calls_made,
                                agent_tool_breakdown=dict(agent_tool_breakdown),
                                max_calls_reached=True,
                            )
                elif is_agent and agent_tool_calls_made >= max_agent_tool_calls:
                    # Don't return immediately on agent-budget exhaustion;
                    # let the model still call lint to finish. Just don't
                    # process any further agent tools this round.
                    pass
        else:
            final_body = _strip_wrapping(msg.content or "")
            return LoopOutcome(
                final_body=final_body,
                last_passed=last_passed,
                tool_calls_made=lint_calls_made + agent_tool_calls_made,
                lint_calls_made=lint_calls_made,
                agent_tool_calls_made=agent_tool_calls_made,
                agent_tool_breakdown=dict(agent_tool_breakdown),
            )

    # Round budget exhausted without a final body.
    return LoopOutcome(
        final_body="",
        last_passed=last_passed,
        tool_calls_made=lint_calls_made + agent_tool_calls_made,
        lint_calls_made=lint_calls_made,
        agent_tool_calls_made=agent_tool_calls_made,
        agent_tool_breakdown=dict(agent_tool_breakdown),
        rounds_exhausted=True,
    )


async def _run_tool_loop_responses(
    client: openai.AsyncOpenAI,
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    platform: str,
    tolerance: float,
    tolerance_floor: int,
    is_reasoning: bool,
    reasoning_effort: str,
    max_tool_calls: int,
    on_api_call: Callable[[Any], None],
    on_status: Callable[[str], None],
    on_tool_call_complete: Callable[[], None],
    max_lint_calls: int | None = None,
    max_agent_tool_calls: int = DEFAULT_MAX_AGENT_TOOL_CALLS,
) -> LoopOutcome:
    """Run the tool-call loop against /v1/responses (gpt-5.x).

    See _run_tool_loop_chat docstring for the dual-budget semantics.

    Spike-validated shape:
      - Pass `instructions` (system prompt) and `input` (list of role+content
        items). On subsequent rounds, echo response.output items back into
        `input`, stripping `status` (rejected on input) but leaving reasoning
        items intact.
      - Tools use the flat Responses shape (TOOL_LINT_SPINTAX_RESPONSES).
      - `reasoning={"effort": ...}` instead of `reasoning_effort=...`.
      - Loop terminator: stop when no function_call items in response.output.
        Final body lives at response.output_text (preferred) or in the
        message item's content blocks.
    """
    if max_lint_calls is None:
        max_lint_calls = max_tool_calls
    input_list: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    tools = [TOOL_LINT_SPINTAX_RESPONSES, *SPINTAX_TOOLS_RESPONSES]
    lint_calls_made = 0
    agent_tool_calls_made = 0
    agent_tool_breakdown: dict[str, int] = {}
    last_passed = False

    for _round in range(max_lint_calls + max_agent_tool_calls + 5):
        kwargs: dict[str, Any] = {
            "model": model,
            "input": input_list,
            "instructions": system_prompt,
            "tools": tools,
            "tool_choice": "auto",
        }
        if is_reasoning:
            kwargs["reasoning"] = {"effort": reasoning_effort}
        else:
            kwargs["temperature"] = 0.6

        response = await asyncio.wait_for(
            client.responses.create(**kwargs),
            timeout=TOOL_LOOP_API_TIMEOUT_SEC,
        )
        on_api_call(response.usage)

        # Identify function-call items in this response.
        output_items = list(response.output or [])
        tool_calls = [it for it in output_items if getattr(it, "type", None) == "function_call"]

        if not tool_calls:
            # Model emitted final body (message item) with no further tool calls.
            final_body = _strip_wrapping(getattr(response, "output_text", "") or "")
            return LoopOutcome(
                final_body=final_body,
                last_passed=last_passed,
                tool_calls_made=lint_calls_made + agent_tool_calls_made,
                lint_calls_made=lint_calls_made,
                agent_tool_calls_made=agent_tool_calls_made,
                agent_tool_breakdown=dict(agent_tool_breakdown),
            )

        # Echo every output item back into input for the next round.
        # Strip `status` because the API rejects it on input ('Unknown parameter').
        # `model_dump(exclude_none=True)` filters out the null `status` on
        # reasoning items naturally; only function_call items carry a non-null
        # status that we must remove.
        for item in output_items:
            if hasattr(item, "model_dump"):
                d = item.model_dump(exclude_none=True)
            else:
                # Fall back for hand-built mocks that don't quack like Pydantic.
                d = dict(item) if isinstance(item, dict) else {}
            d.pop("status", None)
            input_list.append(d)

        for tc in tool_calls:
            call_id = getattr(tc, "call_id", None)
            tc_name = getattr(tc, "name", "")
            tc_args = getattr(tc, "arguments", "")

            is_lint = tc_name == "lint_spintax"
            is_agent = tc_name in SPINTAX_TOOL_NAMES

            cap_hit_msg = None
            if is_lint and lint_calls_made >= max_lint_calls:
                cap_hit_msg = (
                    f"Max lint_spintax calls ({max_lint_calls}) reached. Emit final body now."
                )
            elif is_agent and agent_tool_calls_made >= max_agent_tool_calls:
                cap_hit_msg = (
                    f"Max agent-tool calls ({max_agent_tool_calls}) reached. "
                    f"No more spintax tooling — call lint_spintax or emit final body."
                )
            if cap_hit_msg is not None:
                input_list.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"error": cap_hit_msg}),
                    }
                )
                continue

            on_status("linting")

            if is_lint:
                try:
                    args = json.loads(tc_args)
                    body = args.get("spintax_body", "")
                    tool_result = _lint_tool_wrapper(body, platform, tolerance, tolerance_floor)
                except Exception as exc:  # noqa: BLE001
                    tool_result = {
                        "passed": False,
                        "error_count": 1,
                        "warning_count": 0,
                        "errors": [f"Tool failed: {exc}"],
                        "warnings": [],
                    }
            elif is_agent:
                try:
                    tool_result = await dispatch_responses(tc_name, tc_args)
                except Exception as exc:  # noqa: BLE001
                    tool_result = {"error": f"Spintax tool {tc_name!r} failed: {exc}"}
            else:
                tool_result = {
                    "passed": False,
                    "error_count": 1,
                    "warning_count": 0,
                    "errors": [f"Unknown tool: {tc_name}"],
                    "warnings": [],
                }

            if is_lint:
                lint_calls_made += 1
            elif is_agent:
                agent_tool_calls_made += 1
                agent_tool_breakdown[tc_name] = agent_tool_breakdown.get(tc_name, 0) + 1
            on_tool_call_complete()

            input_list.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(tool_result),
                }
            )

            if is_lint:
                last_passed = bool(tool_result.get("passed"))
                if not last_passed:
                    on_status("iterating")
                    if lint_calls_made >= max_lint_calls:
                        return LoopOutcome(
                            final_body="",
                            last_passed=False,
                            tool_calls_made=lint_calls_made + agent_tool_calls_made,
                            lint_calls_made=lint_calls_made,
                            agent_tool_calls_made=agent_tool_calls_made,
                            agent_tool_breakdown=dict(agent_tool_breakdown),
                            max_calls_reached=True,
                        )

    # Round budget exhausted without a final body.
    return LoopOutcome(
        final_body="",
        last_passed=last_passed,
        tool_calls_made=lint_calls_made + agent_tool_calls_made,
        lint_calls_made=lint_calls_made,
        agent_tool_calls_made=agent_tool_calls_made,
        agent_tool_breakdown=dict(agent_tool_breakdown),
        rounds_exhausted=True,
    )


async def _run_tool_loop_anthropic(
    client: anthropic.AsyncAnthropic,
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    platform: str,
    tolerance: float,
    tolerance_floor: int,
    is_reasoning: bool,  # accepted but unused; Anthropic uses thinking config
    reasoning_effort: str,
    max_tool_calls: int,
    on_api_call: Callable[[Any], None],
    on_status: Callable[[str], None],
    on_tool_call_complete: Callable[[], None],
    max_lint_calls: int | None = None,
    max_agent_tool_calls: int = DEFAULT_MAX_AGENT_TOOL_CALLS,
) -> LoopOutcome:
    """Run the tool-call loop against Anthropic Messages API (claude-* models).

    See _run_tool_loop_chat docstring for the dual-budget semantics.

    Key differences from the OpenAI adapters:
    - `system` is a top-level kwarg, NOT a message in the list.
    - `max_tokens` is required (hardcoded to 8192).
    - Tool shape uses `input_schema` instead of `parameters` (no `function` wrapper).
    - `tool_choice` must be a dict: {"type": "auto"} - NOT the string "auto".
    - Adaptive thinking: `thinking={"type": "adaptive"}` (Opus 4.7 only form).
    - `output_config.effort` maps to the reasoning_effort value.
    - The full assistant `r.content` block-list is echoed back UNMODIFIED.
      Stripping thinking blocks invalidates the encrypted `signature` → 400.
    - `block.input` is already a parsed dict; passed straight to dispatch_anthropic.
    - tool_result field name is `tool_use_id` (NOT `tool_id`).
    - `temperature` must not be set alongside `thinking` (Anthropic 400s).
    """
    if max_lint_calls is None:
        max_lint_calls = max_tool_calls
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    tools = [TOOL_LINT_SPINTAX_ANTHROPIC, *SPINTAX_TOOLS_ANTHROPIC]
    lint_calls_made = 0
    agent_tool_calls_made = 0
    agent_tool_breakdown: dict[str, int] = {}
    last_passed = False

    static_kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 8192,
        "system": system_prompt,
        "tools": tools,
        "tool_choice": {"type": "auto"},
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": reasoning_effort or "medium"},
    }

    for _round in range(max_lint_calls + max_agent_tool_calls + 5):
        r = await asyncio.wait_for(
            client.messages.create(messages=messages, **static_kwargs),
            timeout=TOOL_LOOP_API_TIMEOUT_SEC,
        )
        on_api_call(r.usage)

        tool_use_blocks = [b for b in r.content if getattr(b, "type", None) == "tool_use"]

        if r.stop_reason == "end_turn" and not tool_use_blocks:
            text = "".join(
                getattr(b, "text", "") for b in r.content if getattr(b, "type", None) == "text"
            )
            return LoopOutcome(
                final_body=_strip_wrapping(text),
                last_passed=last_passed,
                tool_calls_made=lint_calls_made + agent_tool_calls_made,
                lint_calls_made=lint_calls_made,
                agent_tool_calls_made=agent_tool_calls_made,
                agent_tool_breakdown=dict(agent_tool_breakdown),
            )

        if not tool_use_blocks:
            # max_tokens or unexpected stop without final text - give up cleanly.
            return LoopOutcome(
                final_body="",
                last_passed=last_passed,
                tool_calls_made=lint_calls_made + agent_tool_calls_made,
                lint_calls_made=lint_calls_made,
                agent_tool_calls_made=agent_tool_calls_made,
                agent_tool_breakdown=dict(agent_tool_breakdown),
            )

        # Echo the FULL assistant content unmodified (thinking blocks must not
        # be stripped - their encrypted `signature` is validated on next call).
        messages.append({"role": "assistant", "content": r.content})

        tool_results = []
        for b in tool_use_blocks:
            tool_name = getattr(b, "name", "")
            is_lint = tool_name == "lint_spintax"
            is_agent = tool_name in SPINTAX_TOOL_NAMES

            cap_hit_msg = None
            if is_lint and lint_calls_made >= max_lint_calls:
                cap_hit_msg = (
                    f"Max lint_spintax calls ({max_lint_calls}) reached. Emit final body now."
                )
            elif is_agent and agent_tool_calls_made >= max_agent_tool_calls:
                cap_hit_msg = (
                    f"Max agent-tool calls ({max_agent_tool_calls}) reached. "
                    f"No more spintax tooling — call lint_spintax or emit final body."
                )
            if cap_hit_msg is not None:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": json.dumps({"error": cap_hit_msg}),
                    }
                )
                continue

            on_status("linting")

            if is_lint:
                # b.input is a parsed dict from the SDK; lint wrapper takes the body string.
                body = (b.input or {}).get("spintax_body", "") if isinstance(b.input, dict) else ""
                result = _lint_tool_wrapper(
                    body,
                    platform,
                    tolerance,
                    tolerance_floor,
                )
            elif is_agent:
                try:
                    result = await dispatch_anthropic(tool_name, b.input or {})
                except Exception as exc:  # noqa: BLE001
                    result = {"error": f"Spintax tool {tool_name!r} failed: {exc}"}
            else:
                result = {"error": f"Unknown tool: {tool_name!r}"}

            if is_lint:
                lint_calls_made += 1
            elif is_agent:
                agent_tool_calls_made += 1
                agent_tool_breakdown[tool_name] = agent_tool_breakdown.get(tool_name, 0) + 1
            on_tool_call_complete()

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": json.dumps(result),
                    "is_error": False,
                }
            )

            if is_lint:
                last_passed = bool(result.get("passed"))
                if not last_passed:
                    on_status("iterating")
                    if lint_calls_made >= max_lint_calls:
                        return LoopOutcome(
                            final_body="",
                            last_passed=False,
                            tool_calls_made=lint_calls_made + agent_tool_calls_made,
                            lint_calls_made=lint_calls_made,
                            agent_tool_calls_made=agent_tool_calls_made,
                            agent_tool_breakdown=dict(agent_tool_breakdown),
                            max_calls_reached=True,
                        )

        messages.append({"role": "user", "content": tool_results})

    return LoopOutcome(
        final_body="",
        last_passed=last_passed,
        tool_calls_made=lint_calls_made + agent_tool_calls_made,
        lint_calls_made=lint_calls_made,
        agent_tool_calls_made=agent_tool_calls_made,
        agent_tool_breakdown=dict(agent_tool_breakdown),
        rounds_exhausted=True,
    )


async def _run_tool_loop(
    client: openai.AsyncOpenAI,
    *,
    model: str,
    system_prompt: str,
    user_content: str,
    platform: str,
    tolerance: float,
    tolerance_floor: int,
    is_reasoning: bool,
    reasoning_effort: str,
    max_tool_calls: int,
    on_api_call: Callable[[Any], None],
    on_status: Callable[[str], None],
    on_tool_call_complete: Callable[[], None],
) -> LoopOutcome:
    """Dispatch to the chat-completions or responses adapter based on model.

    The feature flag `settings.responses_api_enabled` is the kill switch:
    flip it False in env to force gpt-5.x back onto chat-completions (which
    will fail at OpenAI's edge, but allows debugging if the Responses path
    has a regression).
    """
    use_anthropic = settings.anthropic_enabled and model in ANTHROPIC_MODELS
    use_responses = settings.responses_api_enabled and model in RESPONSES_MODELS
    if use_anthropic:
        return await _run_tool_loop_anthropic(
            client,  # type: ignore[arg-type]  # AsyncAnthropic passed in for claude-* models
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            platform=platform,
            tolerance=tolerance,
            tolerance_floor=tolerance_floor,
            is_reasoning=is_reasoning,
            reasoning_effort=reasoning_effort,
            max_tool_calls=max_tool_calls,
            on_api_call=on_api_call,
            on_status=on_status,
            on_tool_call_complete=on_tool_call_complete,
        )
    if use_responses:
        return await _run_tool_loop_responses(
            client,
            model=model,
            system_prompt=system_prompt,
            user_content=user_content,
            platform=platform,
            tolerance=tolerance,
            tolerance_floor=tolerance_floor,
            is_reasoning=is_reasoning,
            reasoning_effort=reasoning_effort,
            max_tool_calls=max_tool_calls,
            on_api_call=on_api_call,
            on_status=on_status,
            on_tool_call_complete=on_tool_call_complete,
        )
    return await _run_tool_loop_chat(
        client,
        model=model,
        system_prompt=system_prompt,
        user_content=user_content,
        platform=platform,
        tolerance=tolerance,
        tolerance_floor=tolerance_floor,
        is_reasoning=is_reasoning,
        reasoning_effort=reasoning_effort,
        max_tool_calls=max_tool_calls,
        on_api_call=on_api_call,
        on_status=on_status,
        on_tool_call_complete=on_tool_call_complete,
    )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def _build_hard_rules(platform: str, max_tool_calls: int) -> str:
    """Hard-rules block ported verbatim from spintax_openai_v3.build_system_prompt().

    The double-brace `{{{{firstName}}}}` escape lets us interpolate `{platform}`
    and `{max_tool_calls}` while preserving the literal `{{firstName}}` in the
    final prompt that the model receives.
    """
    return f"""\
ROLE
You generate cold-email spintax for the Prospeqt V2 pipeline. Each sentence
becomes 5 variations. All variations must fall within +/-5% of the base
variation's character count, or +/-3 chars - whichever is larger. A
deterministic linter will reject anything outside that tolerance.

THE HARD RULE: YOU CANNOT COUNT CHARACTERS
Language models cannot reliably count characters. Never estimate, never
count in your head, never trust your instinct on length. ALWAYS call the
`lint_spintax` tool to verify your draft. This is non-negotiable.

WORKFLOW
1. Draft the full spintax body following the style rules below.
1.5. REQUIRED — make at least ONE synonym/syntax tool call BEFORE the
   first `lint_spintax` call. Word-swap-only reskins are not acceptable
   variations; this seeding step is how you get real structural
   diversity. Pick one or more from this set, in cost order
   (cheapest first):

   - `get_pre_approved_synonyms(source_word, role, sense_label)` — FREE,
     instant, no network. Returns curated synonyms for common words
     (saw, send, show, help). Try this FIRST if the synonym-critical
     word in your draft is in the approved lexicon.
   - `classify_word_sense_for_sentence(word, sentence, role)` — FREE.
     Tags a word with its sense in context (e.g. data_observation vs
     visual_observation). Recommends WordHippo context_ids to look up.
   - `score_synonym_candidates(source_word, sentence, candidates,
     role, sense_label)` — FREE. Validate a candidate list (your own
     or from a previous tool) against the sentence. Returns
     approved | candidate_review | rejected per item.
   - `identify_syntax_family(sentence, role)` — FREE. Classify the
     structural posture of a block (cta_curiosity, proof_helper_led,
     evidence_first_observation, etc.) BEFORE you draft variations.
     Knowing the family is what tells you which alternate families
     are reachable.
   - `reshape_blocks(sentence, role, source_family, target_family,
     max_variants)` — FREE. Generate structurally distinct variants
     for a block — different clause order, active vs passive, subject
     reordering, evidence-first vs greeting-first, etc. NOT word-swap
     reskins. Use this to seed your variations so they differ in 2+
     axes (word choice + clause order, evidence type + CTA framing,
     etc.).
   - `wordhippo_lookup(word, context_id=null|"C0-N")` — NETWORK call
     to Spider (costs money). Use only when `get_pre_approved_synonyms`
     came up empty for the word. Pass context_id=null first to
     discover buckets, then call again with the right C0-N.

   The goal: variations differ in 2+ axes (word choice + clause order,
   evidence type + CTA framing, active vs passive, subject reordering).
   Word-swap-only reskins do not satisfy this rule.

2. ONLY AFTER step 1.5: call `lint_spintax` with your seeded draft. Do
   NOT promise to call it later. Do NOT describe what you plan to do.
   Emit the call now.
3. Read the tool result:
   - passed=true: respond with the final body as your text message and stop.
     Do NOT call the tool again on a passing draft.
   - passed=false: note which blocks/variations are flagged.
4. Rewrite ONLY the flagged variations. Keep every other block and every
   other variation EXACTLY as it was. Do NOT change Variation 1 of any
   block (Variation 1 is the original, word for word).
5. Call `lint_spintax` again with the updated full body.
5b. STUCK? — if the SAME variation fails for the SAME reason on two
   consecutive lint calls, you are in a length-tweaking loop. Do NOT
   keep shaving and adding words. Reach for the same tools listed in
   step 1.5 to escape. Common moves:
   - banned-word error -> `get_pre_approved_synonyms` for a curated
     swap; if the bank is empty, `wordhippo_lookup` then
     `score_synonym_candidates` to validate.
   - length error that single-word swaps can't fix -> `reshape_blocks`
     in a different `target_family` to restructure the sentence.
   After using one, go back to step 4 with the new material.
6. Repeat steps 3-5b until `passed=true` or you have made
   {max_tool_calls} lint_spintax calls.

POST-GENERATION QA (optional, free):
   - `lint_structure_repetition(lines, role)` — check that your 5
     variations vary structurally, not just lexically. If risk_level
     comes back high, loop back to step 4 with `reshape_blocks` for
     the most concentrated family.

SURGICAL FIX RULE
When a length error says "block 3 var 4: 50 chars vs base 67 (17 short)",
adjust ONLY block 3 variation 4. Add or remove words until the length is
within tolerance of the base. Do NOT rewrite the whole block. Do NOT
touch other variations in that block. Do NOT touch other blocks.

When fixing a banned-word error, swap the banned word for a natural
synonym. Do NOT rewrite the whole variation unless necessary.

FINAL OUTPUT FORMAT
Your final text message (after a passing lint) must contain ONLY the
spintax body. No markdown fences. No commentary. No explanations.
- Start with the salutation line (e.g. `Hey {{{{firstName}}}},`).
- End with the last block of the original email.
- Do NOT append an opt-out block.

HARD CONSTRAINTS (enforced by the linter)
- Variation 1 = EXACT original input, word for word, character for character.
- Exactly 5 variations per spintax block.
- No em-dashes anywhere. Use a regular hyphen-minus if needed.
- No banned AI words (see _rules-ai-patterns.md below).
- Variables like {{{{firstName}}}} count as their literal text length.
- All 5 variations must preserve the original meaning.
- NO INVISIBLE CHARACTERS. Do not pad length with zero-width spaces
  (U+200B), word joiners (U+2060), soft hyphens (U+00AD), or any other
  invisible Unicode. Every character must be visible and render normally
  in an email client. The linter will reject these as hard errors. If a
  variation is too short, add actual words. If it's too long, cut words.

BLOCK STRUCTURE RULE (critical - do not deviate)
Preserve the input's paragraph structure 1-to-1:
- Each paragraph in the input becomes EXACTLY ONE spintax block in the output.
- A multi-sentence paragraph ("Sentence A. Sentence B. Fair?") is ONE block.
  Variation 1 = the whole paragraph verbatim. The other 4 variations rewrite
  the same multi-sentence paragraph.
- Do NOT split a paragraph into multiple blocks.
- Do NOT merge separate paragraphs into one block.
- Every prose paragraph gets spun, INCLUDING the P.S. line. The P.S. is
  prose, not a signature - give it 5 variations like any other paragraph.
- ONLY these things stay unspun: bullet list lines, single-line variable
  tokens on their own line such as `{{{{accountSignature}}}}`, closing email
  signatures (e.g. `Best,\nDanica` — closing word + comma on line 1, short
  sender name on line 2, no variable tokens), and blank lines.
- If the input has N spintaxable paragraphs, your output has exactly N
  spintax blocks - no more, no fewer.

GREETING RULE (professional tone only - STRICT whitelist)
If the input's first line is a greeting like `Hey {{{{firstName}}}},`, spin it
into a 5-variation block. The block must contain EXACTLY these 5 strings,
verbatim - no additions, no substitutions, no exclamation points, no
"folks" or "team" tacked on. Variation 1 must match the input; the other
four are drawn from the list below, one each:

  1. `Hey {{{{firstName}}}},`
  2. `Hi {{{{firstName}}}},`
  3. `Hello {{{{firstName}}}},`
  4. `Hey there,`
  5. `{{{{firstName}}}},`  (bare, just the name + comma)

These 5 strings are the ONLY allowed greetings. Do NOT invent new ones.
NEVER use: Howdy, Heya, Yo, Sup, Dude, What's up, Greetings, Hey folks, Hi team.
Do NOT swap the comma for an exclamation point or question mark.
Length check does not apply to the greeting block - these 5 strings are
pre-approved regardless of length differences.

APPROXIMATION TILDE
If a variation needs to be shorter, you MAY drop the `~` before a number
(e.g. `~{{{{tam_size}}}}` becomes `{{{{tam_size}}}}`). The tilde marks approximation
and dropping it is acceptable; readers still understand the value is an estimate.

PLATFORM: {platform.upper()}
Use the spintax syntax defined in the PLATFORM FORMAT section below.
"""


def build_system_prompt(
    platform: str,
    skills_dir: Path,
) -> str:
    """Assemble the system prompt: hard rules + skill markdown files.

    Reads the following files from skills_dir:
        SKILL.md
        _rules-length.md
        _rules-ai-patterns.md
        _rules-spam-words.md
        _format-{platform}.md

    Args:
        platform: "instantly" or "emailbison" - selects the format file.
        skills_dir: path to the directory holding skill markdown files.

    Returns:
        The fully-assembled system prompt string ready for the OpenAI call.
    """
    hard_rules = _build_hard_rules(platform, DEFAULT_MAX_TOOL_CALLS)
    orchestrator = (skills_dir / "SKILL.md").read_text(encoding="utf-8")
    length = (skills_dir / "_rules-length.md").read_text(encoding="utf-8")
    ai_patterns = (skills_dir / "_rules-ai-patterns.md").read_text(encoding="utf-8")
    spam_words = (skills_dir / "_rules-spam-words.md").read_text(encoding="utf-8")
    fmt = (skills_dir / f"_format-{platform}.md").read_text(encoding="utf-8")

    parts = [
        hard_rules,
        "\n" + "=" * 60,
        "ORCHESTRATOR (pipeline overview)",
        "=" * 60,
        orchestrator,
        "\n" + "=" * 60,
        "LENGTH RULE",
        "=" * 60,
        length,
        "\n" + "=" * 60,
        "AI-PATTERN RULES",
        "=" * 60,
        ai_patterns,
        "\n" + "=" * 60,
        "SPAM TRIGGER WORDS (warning-level, avoid unless load-bearing)",
        "=" * 60,
        spam_words,
        "\n" + "=" * 60,
        f"PLATFORM FORMAT ({platform.upper()})",
        "=" * 60,
        fmt,
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Drift revision helpers
#
# After the initial spintax generation, we run the QA suite. If it reports
# concept-drift warnings (variations 2-5 introducing nouns not in V1),
# we send the offending body BACK to the model with the warnings attached
# and ask for a revision. Up to MAX_DRIFT_REVISIONS attempts.
# ---------------------------------------------------------------------------


def _extract_drift_warnings(qa_result: dict[str, Any]) -> list[str]:
    """Pull only the concept-drift warnings out of a QA result.

    QA returns a flat list of warnings covering smart quotes, doubled
    punctuation, AND drift. We only revise on drift - the other warnings
    are advisory.
    """
    return [
        w
        for w in qa_result.get("warnings", [])
        if "drift phrase" in w or "new content words not in V1" in w
    ]


def _build_drift_revision_prompt(
    plain_body: str,
    previous_body: str,
    drift_warnings: list[str],
    platform: str,
    attempt: int,
) -> str:
    """Build the user message for a drift-revision pass.

    The model has just produced `previous_body` which the QA flagged for
    inventing context not in `plain_body`. This prompt tells it exactly
    what drifted and demands a revision that ONLY swaps synonyms or
    restructures - no new concepts.
    """
    bullets = "\n".join(f"  - {w}" for w in drift_warnings)
    return (
        f"REVISION PASS #{attempt} - concept drift detected.\n\n"
        f"Your previous spintax draft introduced ideas that were NOT in the "
        f"original input. This is a quality bug - variations 2-5 must restate "
        f"V1 in different words, not invent new framings, time horizons, or "
        f"stakeholders.\n\n"
        f"Drift issues found by the QA pass:\n{bullets}\n\n"
        f"Your previous draft:\n```\n{previous_body}\n```\n\n"
        f"Original plain input (target platform: {platform}):\n```\n{plain_body}\n```\n\n"
        f"REVISION RULES - non-negotiable:\n"
        f"1. Variations 2-5 must contain ONLY concepts present in Variation 1. "
        f"No invented context (no 'this quarter', 'first demo', 'your team', "
        f"'next month', etc. unless those exact phrases are in V1).\n"
        f"2. Use synonym swaps OR sentence-shape changes (voice, clause "
        f"order, question form). Do NOT add new framings, stakeholders, "
        f"time horizons, or actors.\n"
        f"3. Variation 1 must remain word-for-word identical to the original "
        f"input paragraph.\n"
        f"4. All {{{{variables}}}} preserved exactly with correct brackets.\n"
        f"5. Re-call lint_spintax to verify length tolerance and banned-word "
        f"rules before emitting the revised body.\n\n"
        f"Produce the corrected spintax now."
    )


def _extract_diversity_diagnostics(
    qa_result: dict[str, Any], level: str
) -> list[str]:
    """Pull diversity diagnostics from a QA result for the retry trigger.

    Day 1 (level=='warning'): diagnostics live in `warnings` (demoted), but
    we never retry on warning-level by design. Function returns [] so the
    trigger short-circuits without needing the level dispatch downstream.

    Post-promotion (level=='error'): diagnostics live in `errors`. We match
    the block-level prefixes only - corpus diagnostics are advisory and a
    single retry can't lift whole-email blandness, so we exclude them.
    """
    if level != "error":
        return []
    block_prefixes = ("block ",)  # broad; the substring check below narrows
    return [
        e
        for e in qa_result.get("errors", [])
        if e.startswith(block_prefixes)
        and ("pairwise diversity below floor" in e or "diversity below floor" in e)
    ]


# ---------------------------------------------------------------------------
# V2 per-block diversity retry helpers
#
# The V1 whole-email retry produced WORSE output than the failing input
# (block 1 score 0.479 -> 0.083 in benchmark job 2). Root cause was the
# drift loop poisoning the model's working context with "synonym swaps
# only, V1 word-for-word identical" instructions, then the diversity
# retry firing into that poisoned context with the same model conversation.
#
# V2 architecture: per-block sub-calls in CLEAN context (no drift history,
# no other-block context). Each failing block gets its own LLM round,
# splices back via lint.reassemble. On any post-splice diversity regression
# OR splice corruption, revert that single block to its pre-retry state.
# ---------------------------------------------------------------------------


_BLOCK_INDEX_RE = re.compile(r"^block (\d+)\b")


class SpliceCorruptionError(RuntimeError):
    """Raised when revert_single_block detects unintended mutations.

    Caller should fall back to shipping the pre-retry body wholesale (P6).
    """


def compute_failing_blocks_from_errors(qa_result: dict[str, Any]) -> list[int]:
    """Return 0-indexed block indices that have diversity-related errors.

    Reads from qa_result['errors'] (NOT block scores) so the CTA pair-floor
    carve-out in qa.py:600 is auto-inherited (CTA blocks never appear here
    when they fail only the pair floor).
    """
    failing: set[int] = set()
    for err in qa_result.get("errors", []):
        if (
            "diversity below floor" not in err
            and "pairwise diversity below floor" not in err
        ):
            continue
        m = _BLOCK_INDEX_RE.match(err)
        if m:
            failing.add(int(m.group(1)) - 1)  # qa.py uses 1-indexed; we use 0-indexed
    return sorted(failing)


def compute_jaccard_failing_blocks(qa_result: dict[str, Any]) -> list[int]:
    """Return 0-indexed block indices with Jaccard-style diversity failures.

    Reads from `qa_result['diversity_block_scores']` and
    `qa_result['diversity_pair_distances']` directly (not error strings) so
    it works at BOTH 'warning' and 'error' gate levels. The drift-loop
    cleanup phase (Workstream 1) needs to fire even when diversity
    findings are demoted to warnings on Day 1.

    A block fails when either:
      - block-avg V1<->Vn distance < BLOCK_AVG_FLOOR (0.30), OR
      - any single V1<->Vn pair distance < BLOCK_PAIR_FLOOR (0.20).

    Greeting / short / unscorable blocks (score=None) are skipped.

    Note: this does NOT inherit the qa.py CTA pair-floor carve-out
    (qa.py:600 skips pair-floor for CTA blocks). The cleanup phase is
    intentionally stricter than the gate: a CTA block with a 0.0-distance
    pair still ships an obviously-broken pseudo-variant and we want the
    chance to clean it up. The downstream gate stays carve-out-aware.
    """
    block_scores = qa_result.get("diversity_block_scores", []) or []
    pair_distances_per_block = qa_result.get("diversity_pair_distances", []) or []
    failing: list[int] = []
    for idx, score in enumerate(block_scores):
        if score is None:
            continue
        if score < BLOCK_AVG_FLOOR:
            failing.append(idx)
            continue
        pairs = (
            pair_distances_per_block[idx]
            if idx < len(pair_distances_per_block)
            else []
        )
        if any(d is not None and d < BLOCK_PAIR_FLOOR for d in pairs):
            failing.append(idx)
    return failing


def revert_single_block(
    post_body: str,
    pre_body: str,
    block_idx: int,
    platform: str,
) -> str:
    """Replace block_idx in post_body with the corresponding block from pre_body.

    Verifies invariants in both directions:
        1. The reverted block matches pre_body's block exactly
        2. All OTHER blocks in post_body remain untouched

    Raises SpliceCorruptionError if either invariant fails. Caller should
    fall back to shipping pre_body wholesale.
    """
    pre_blocks = extract_blocks(pre_body, platform)
    post_blocks = extract_blocks(post_body, platform)
    if block_idx >= len(pre_blocks) or block_idx >= len(post_blocks):
        raise SpliceCorruptionError(
            f"block_idx {block_idx} out of range "
            f"(pre={len(pre_blocks)}, post={len(post_blocks)})"
        )
    target_inner = pre_blocks[block_idx][1]

    new_body = reassemble(post_body, {block_idx: target_inner}, platform)
    new_blocks = extract_blocks(new_body, platform)
    if len(new_blocks) != len(post_blocks):
        raise SpliceCorruptionError(
            f"revert of block {block_idx} changed block count "
            f"({len(post_blocks)} -> {len(new_blocks)})"
        )
    if new_blocks[block_idx][1] != target_inner:
        raise SpliceCorruptionError(
            f"revert of block {block_idx} did not restore pre-retry content"
        )
    for i, (_, post_inner) in enumerate(post_blocks):
        if i == block_idx:
            continue
        if new_blocks[i][1] != post_inner:
            raise SpliceCorruptionError(
                f"revert of block {block_idx} corrupted block {i}"
            )
    return new_body


def joint_score(
    diversity_avg: float,
    drift_count: int,
    content_word_count: int,
) -> float:
    """Combined drift+diversity score for revert decisioning.

    The drift inverse is scaled by block length so long body blocks
    (14-18 content words) with drift_count=6+ aren't penalized into
    always-revert vs short CTA/p.s. blocks. Floor of 5 prevents
    short-block divide-by-zero domination.
    """
    drift_denom = max(5, content_word_count // 2)
    drift_inverse = max(0.0, 1.0 - drift_count / drift_denom)
    return 0.7 * diversity_avg + 0.3 * drift_inverse


def _build_diversity_revision_prompt(
    block_v1: str,
    block_variants: list[str],
    block_score: float,
    block_pairwise_diagnostics: list[str],
    block_position: int,
    platform: str,
    tolerance: float = 0.05,
    tolerance_floor: int = 3,
) -> str:
    """Build a per-block diversity-revision prompt.

    Sent to the model in a CLEAN sub-call (no drift conversation history,
    no other-block context). Returns instructions to revise V2-V5 of a
    single block to clear the diversity floor. The clean context is the
    load-bearing fix: without the drift loop's 'synonym swaps only'
    instructions in working memory, the model is free to pick structural
    revisions.

    Args:
        block_v1: the V1 variant (must be preserved word-for-word)
        block_variants: V2-V5 from the previous (failing) generation
        block_score: average Jaccard distance for this block (0.0-1.0)
        block_pairwise_diagnostics: per-variant diagnostics from qa.py
        block_position: 1-indexed block position in the email
        platform: "instantly" or "emailbison"
        tolerance: fractional length tolerance (default 0.05 = 5%)
        tolerance_floor: minimum absolute char tolerance (default 3)
    """
    diagnostics_block = "\n".join(
        f"  - {d}" for d in block_pairwise_diagnostics
    ) or "  - (none; flagged on average diversity only)"
    variants_block = "\n".join(
        f"  V{i + 2}: {v}" for i, v in enumerate(block_variants)
    )
    v1_len = len(block_v1)
    allowed_diff = max(int(v1_len * tolerance), tolerance_floor)
    band_lo = max(0, v1_len - allowed_diff)
    band_hi = v1_len + allowed_diff
    return (
        f"You are revising one paragraph of a cold email to fix a diversity "
        f"failure. The block scored {block_score:.2f} Jaccard distance "
        f"average, below the 0.30 floor. Your variants 2-5 read as near-"
        f"duplicates of V1.\n\n"
        f"BLOCK POSITION: {block_position} (1=greeting, 2=opener, "
        f"middle=body, last-1=CTA, last=signature/p.s.)\n"
        f"PLATFORM: {platform}\n\n"
        f"V1 (must be preserved word-for-word):\n  {block_v1}\n\n"
        f"Your previous V2-V5 (failing):\n{variants_block}\n\n"
        f"Pairwise issues:\n{diagnostics_block}\n\n"
        f"---\n\n"
        f"REVISION STRATEGY - pick ONE per variant, in priority order:\n\n"
        f"1. **structural** (PREFERRED): Change sentence shape. Voice shift "
        f"(active<->passive), clause reorder, statement<->question flip, "
        f"lead with object instead of subject, split into two clauses, "
        f"merge two clauses. The CONTENT stays the same; the SHAPE "
        f"changes.\n\n"
        f"2. **lexical**: Swap individual content words for synonyms while "
        f"preserving sentence shape. Use only if structural change is not "
        f"viable for this block (e.g., one-clause greeting).\n\n"
        f"3. **combined**: Both shape change AND synonym swaps. Most "
        f"variation per variant. Use sparingly - high risk of drift.\n\n"
        f"---\n\n"
        f"REVISION RULES - non-negotiable:\n\n"
        f"1. V1 must remain word-for-word identical to what's shown above.\n"
        f"2. Aim for **40-50% relative word change** between V1 and each of "
        f"V2-V5 (after stopwording short function words). NOT 80-90% same; "
        f"NOT 100% different. Mid-range diversity reads as natural rewriting.\n"
        f"3. Across V2-V5, use AT LEAST 2 different strategies. If you "
        f"only do synonym swaps, the gate will fail again.\n"
        f"4. Do NOT invent new concepts (no drift). The model output is "
        f"checked separately for content drift; new ideas/stakeholders/"
        f"time horizons not in V1 are forbidden.\n"
        f"5. **Length tolerance**: each of V2-V5 must be between "
        f"{band_lo} and {band_hi} characters long (V1 is {v1_len} chars; "
        f"allowed band is +/-{allowed_diff} chars = max("
        f"{tolerance * 100:.0f}% of V1, {tolerance_floor} char floor)). "
        f"Variants outside this band will be rejected and reverted, "
        f"costing the gate. Count carefully before emitting.\n"
        f"6. All `{{{{variables}}}}` preserved exactly with double-brace "
        f"syntax.\n\n"
        f"---\n\n"
        f"WORKED EXAMPLES (abstract placeholders to prevent imitation bleed):\n\n"
        f"INPUT V1: \"At {{{{company_name}}}}, {{{{trigger_event}}}} happened "
        f"in {{{{time_period}}}} and we saw {{{{outcome_metric}}}}.\"\n\n"
        f"GOOD V2 (structural - clause-first reorder):\n"
        f"  \"{{{{outcome_metric}}}} came after {{{{trigger_event}}}} at "
        f"{{{{company_name}}}} in {{{{time_period}}}}.\"\n\n"
        f"GOOD V3 (combined - voice flip + synonym):\n"
        f"  \"In {{{{time_period}}}}, {{{{trigger_event}}}} drove "
        f"{{{{outcome_metric}}}} for {{{{company_name}}}}.\"\n\n"
        f"GOOD V4 (lexical - pure synonyms):\n"
        f"  \"{{{{company_name}}}} hit {{{{outcome_metric}}}} once "
        f"{{{{trigger_event}}}} occurred during {{{{time_period}}}}.\"\n\n"
        f"BAD V5 (single verb swap - what the gate catches):\n"
        f"  \"At {{{{company_name}}}}, {{{{trigger_event}}}} took place "
        f"in {{{{time_period}}}} and we saw {{{{outcome_metric}}}}.\"\n"
        f"  <- only 'happened'->'took place'; ~92% word overlap; FAILS gate.\n\n"
        f"---\n\n"
        f"OUTPUT FORMAT (JSON):\n\n"
        f"{{\n"
        f'  "v2": "<revised variant>",\n'
        f'  "v3": "<revised variant>",\n'
        f'  "v4": "<revised variant>",\n'
        f'  "v5": "<revised variant>",\n'
        f'  "strategies": ["structural", "combined", "structural", "lexical"]\n'
        f"}}\n\n"
        f"`strategies` must be a 4-element array; one of structural / "
        f"lexical / combined per variant; AT LEAST 2 distinct values.\n\n"
        f"Produce the JSON now. No prose before or after."
    )


# ---------------------------------------------------------------------------
# V3 Workstream 1: per-block Jaccard cleanup helpers
#
# Different signal than V2's _build_diversity_revision_prompt. V2 fires on
# 'block-avg too low' (mostly synonym-only variants); V3 cleanup fires
# specifically on 'V1 and Vn share too many content words' (drift_retry's
# pure-word-reorder failure mode). The prompt names the overlapping words
# and proper-noun preserves explicitly so the model knows what to swap and
# what to leave alone.
# ---------------------------------------------------------------------------


def _extract_preserve_tokens(v1: str) -> list[str]:
    """Heuristically extract tokens that must be preserved verbatim across V2-V5.

    Returns a deduplicated list (insertion-order) of:
      - `{{instantly_var}}` placeholders (full match)
      - `{EMAILBISON_VAR}` placeholders (full match)
      - Multi-word capitalized phrases (proper-noun runs longer than 1
        token, joined with spaces; e.g. "Fox & Farmer", "United States")
      - Single capitalized tokens that are NOT the first word of a sentence
        (skips sentence-initial capitalization that's just orthography)

    Heuristic, not perfect. The model still has V1 and can use judgment.
    Used in _build_jaccard_cleanup_prompt to tell the model what stays
    exact (proper nouns, brand names, product names, placeholders).
    """
    preserves: list[str] = []
    seen: set[str] = set()

    def _push(item: str) -> None:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            preserves.append(key)

    for m in re.finditer(r"\{\{[^}]+\}\}", v1):
        _push(m.group(0))
    for m in re.finditer(r"\{[A-Z_][A-Z0-9_]*\}", v1):
        _push(m.group(0))

    # Tokenize on whitespace (preserves '&', '-' inside tokens like Fox-Farmer).
    # Track sentence-initial position so we can ignore lone caps after '.', '!', '?'.
    sentence_initial = True
    raw_tokens = re.findall(r"\S+", v1)

    def _is_capitalized(tok: str) -> bool:
        # Strip leading/trailing punctuation that isn't part of an identifier.
        core = tok.strip(".,;:!?\"'()[]")
        if not core:
            return False
        # Must start with an uppercase letter; allow internal apostrophes/hyphens.
        return core[0:1].isupper() and any(c.isalpha() for c in core)

    # Build runs of capitalized tokens (allowing connector tokens like "&", "of", "the"
    # within a known proper-noun phrase). For simplicity we only join adjacent caps
    # plus single-char connectors ('&', '-'), which catches "Fox & Farmer" but not
    # general "United States of America" - that's acceptable for the heuristic.
    i = 0
    while i < len(raw_tokens):
        tok = raw_tokens[i]
        if _is_capitalized(tok):
            run = [tok]
            j = i + 1
            while j < len(raw_tokens):
                nxt = raw_tokens[j]
                if _is_capitalized(nxt):
                    run.append(nxt)
                    j += 1
                elif nxt in {"&", "-", "/"} and j + 1 < len(raw_tokens) \
                        and _is_capitalized(raw_tokens[j + 1]):
                    run.append(nxt)
                    j += 1
                else:
                    break
            if len(run) >= 2:
                # Multi-word proper-noun phrase: always preserve.
                _push(" ".join(run).strip(".,;:!?"))
            elif not sentence_initial:
                # Single capitalized token mid-sentence: likely a proper noun.
                _push(tok.strip(".,;:!?\"'()[]"))
            sentence_initial = False
            i = j
        else:
            sentence_initial = tok.endswith((".", "!", "?"))
            i += 1

    return preserves


def _build_jaccard_cleanup_prompt(
    block_v1: str,
    block_variants: list[str],
    pair_distances: list[float | None],
    block_position: int,
    platform: str,
    tolerance: float = 0.05,
    tolerance_floor: int = 3,
) -> str:
    """Build a per-block sub-call prompt focused on word-set duplication.

    The drift loop's safety nets (length, lint, drift) all pass on
    pure-word-reorder pseudo-variants because none of them check word-set
    overlap. This prompt names:
      - the specific overlapping content words to swap out
      - the proper-noun / placeholder phrases that must stay exact
      - the length band (Bug A's 5%/3-char rule)
    so the model has zero ambiguity about what's broken and what to do.

    Args:
        block_v1: the V1 variant (must be preserved word-for-word).
        block_variants: V2-V5 from the failing draft (4 strings).
        pair_distances: V1<->V2..V5 Jaccard distances; aligns with
            block_variants. None entries (empty token sets) treated as 1.0.
        block_position: 1-indexed block position in the email.
        platform: 'instantly' or 'emailbison'.
        tolerance: fractional length tolerance (default 5%).
        tolerance_floor: minimum absolute char tolerance (default 3).
    """
    v1_tokens = _diversity_tokens(block_v1)
    overlap_words: set[str] = set()
    failing_indices: list[int] = []
    for k, v in enumerate(block_variants):
        d = pair_distances[k] if k < len(pair_distances) else None
        v_tokens = _diversity_tokens(v)
        if d is not None and d < BLOCK_PAIR_FLOOR:
            failing_indices.append(k + 2)  # 1-indexed Vn
            overlap_words |= (v1_tokens & v_tokens)

    if not overlap_words:
        # Block-avg failed but no individual pair below pair-floor; use the
        # union of words shared with any variant as the change-suggestion list.
        for v in block_variants:
            overlap_words |= (v1_tokens & _diversity_tokens(v))

    overlap_list = sorted(overlap_words)
    overlap_block = (
        ", ".join(f'"{w}"' for w in overlap_list)
        if overlap_list
        else "(none above the stopword threshold)"
    )

    preserves = _extract_preserve_tokens(block_v1)
    preserves_block = (
        "\n".join(f'  - "{p}"' for p in preserves)
        if preserves
        else "  - (none detected; still keep any obvious proper nouns from V1)"
    )

    pair_diag_lines: list[str] = []
    for k, v in enumerate(block_variants):
        d = pair_distances[k] if k < len(pair_distances) else None
        if d is None:
            pair_diag_lines.append(
                f"  V{k + 2}: distance unscored (no content tokens after stopwording)"
            )
            continue
        overlap_pct = (1.0 - d) * 100
        flag = " <-- REWRITE" if d < BLOCK_PAIR_FLOOR else ""
        pair_diag_lines.append(
            f"  V{k + 2}: distance {d:.2f} (~{overlap_pct:.0f}% word overlap){flag}"
        )
    pair_diag_block = "\n".join(pair_diag_lines)

    variants_block = "\n".join(
        f"  V{k + 2}: {v}" for k, v in enumerate(block_variants)
    )

    v1_len = len(block_v1)
    allowed_diff = max(int(v1_len * tolerance), tolerance_floor)
    band_lo = max(0, v1_len - allowed_diff)
    band_hi = v1_len + allowed_diff

    failing_summary = (
        f"V{', V'.join(str(i) for i in failing_indices)}"
        if failing_indices
        else "the block as a whole"
    )

    return (
        f"You are revising one paragraph of a cold email to fix a "
        f"WORD-SET DUPLICATION failure. The diversity gate counts shared "
        f"content words IGNORING ORDER, so reordering V1's words is NOT "
        f"a valid variation. {failing_summary} below share too many "
        f"content words with V1.\n\n"
        f"BLOCK POSITION: {block_position} (1=greeting, 2=opener, "
        f"middle=body, last-1=CTA, last=signature/p.s.)\n"
        f"PLATFORM: {platform}\n\n"
        f"V1 (must be preserved word-for-word):\n  {block_v1}\n\n"
        f"Your previous V2-V5 (failing):\n{variants_block}\n\n"
        f"Per-pair overlap with V1 (lower distance = more shared words):\n"
        f"{pair_diag_block}\n\n"
        f"---\n\n"
        f"WORDS TO SWAP OUT (these appear in BOTH V1 and the failing "
        f"variations - replace at least HALF of them per variant with "
        f"synonyms or paraphrases):\n  {overlap_block}\n\n"
        f"PRESERVE-LIST (these must stay EXACT across all variations - "
        f"do not paraphrase, replace, or pluralize):\n{preserves_block}\n\n"
        f"---\n\n"
        f"REVISION RULES - non-negotiable:\n\n"
        f"1. V1 stays word-for-word identical to what's shown above.\n"
        f"2. For EACH failing variation, REPLACE at least half of the "
        f"overlap words above. Reordering V1's words is the failure - "
        f"do not do that. Use synonyms, paraphrase the action, or "
        f"restructure with different content words.\n"
        f"3. **Match V1's register.** Read V1's tone (formal, "
        f"professional, conversational, casual) and pick synonyms that "
        f"fit. If V1 reads professional or business-like, do NOT drift "
        f"to whimsical or overly casual alternatives. For example, in "
        f"a professional email, AVOID 'cheerful', 'upbeat', 'jazzed', "
        f"'thrilled', 'stoked' as substitutes for 'happy' or 'pleased'. "
        f"Prefer register-matched synonyms like 'satisfied', 'pleased', "
        f"'content', 'glad'.\n"
        f"4. **Lock domain nouns.** If V1 uses a specific noun like "
        f"'clients', 'patients', 'customers', 'students', 'tenants', "
        f"'guests', 'members', etc., keep that exact noun across all "
        f"of V2-V5. Do not swap 'clients' for 'customers' or vice "
        f"versa. Domain nouns carry meaning that generic swaps lose.\n"
        f"5. **Vary structure, not just vocabulary.** It is fine - and "
        f"often better - for some variants to keep V1's main verbs and "
        f"reorder the clause, swap one or two adjectives, or shift a "
        f"time phrase. The Jaccard floor is 0.30, not 1.0; you do NOT "
        f"need to change every word. Mix small structural edits with "
        f"bigger lexical edits across the four variants.\n"
        f"6. Across V2-V5, target Jaccard distance >= 0.30 vs V1 "
        f"(roughly: each variant should differ from V1 by 30%+ of its "
        f"content words after stopwording).\n"
        f"7. Do NOT introduce new concepts, stakeholders, time horizons, "
        f"or claims not present in V1. Drift is checked separately.\n"
        f"8. **Length tolerance**: each of V2-V5 must be between "
        f"{band_lo} and {band_hi} characters long (V1 is {v1_len} chars; "
        f"allowed band is +/-{allowed_diff} chars = max("
        f"{tolerance * 100:.0f}% of V1, {tolerance_floor} char floor)). "
        f"Variants outside this band will be rejected. Count carefully.\n"
        f"9. ALL `{{{{variables}}}}` and `{{VARIABLES}}` preserved with "
        f"exact double-brace / single-brace syntax.\n\n"
        f"---\n\n"
        f"OUTPUT FORMAT (JSON):\n\n"
        f"{{\n"
        f'  "v2": "<revised variant>",\n'
        f'  "v3": "<revised variant>",\n'
        f'  "v4": "<revised variant>",\n'
        f'  "v5": "<revised variant>",\n'
        f'  "strategies": ["lexical", "structural", "combined", "lexical"]\n'
        f"}}\n\n"
        f"`strategies` must be a 4-element array; one of structural / "
        f"lexical / combined per variant.\n\n"
        f"Produce the JSON now. No prose before or after."
    )


def _parse_revision_json(text: str) -> dict[str, Any]:
    """Parse the per-block revision JSON returned by the model.

    Tolerates markdown code fences and stray prose before/after the JSON
    object. Normalizes key casing (V2 -> v2). Raises ValueError on any
    structural malformation; the caller treats this as a failed sub-call
    (no splice, count the attempt).
    """
    text = _strip_wrapping(text or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        first = text.find("{")
        last = text.rfind("}")
        if first == -1 or last == -1 or last <= first:
            raise ValueError("no JSON object in response") from None
        try:
            data = json.loads(text[first:last + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON parse failed: {e}") from None

    if not isinstance(data, dict):
        raise ValueError(f"expected dict, got {type(data).__name__}")

    normalized: dict[str, Any] = {}
    for k, v in data.items():
        key = str(k).strip().lower()
        normalized[key] = v

    required = ("v2", "v3", "v4", "v5")
    missing = [k for k in required if k not in normalized]
    if missing:
        raise ValueError(f"missing keys: {missing}")
    for k in required:
        v = normalized[k]
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"key {k!r} not a non-empty string")

    strategies = normalized.get("strategies", [])
    if not isinstance(strategies, list) or len(strategies) != 4:
        strategies = []  # tolerate; not load-bearing for splice

    return {
        "v2": normalized["v2"].strip(),
        "v3": normalized["v3"].strip(),
        "v4": normalized["v4"].strip(),
        "v5": normalized["v5"].strip(),
        "strategies": strategies,
    }


async def _run_per_block_revision_subcall(
    client: Any,
    *,
    model: str,
    prompt: str,
    on_api_call: Callable[[Any], None],
) -> dict[str, Any]:
    """Single LLM call in CLEAN context for a per-block diversity revision.

    No tools, no system prompt history, no other-block context. The clean
    context is the load-bearing fix: without the drift loop's "synonym
    swaps only" instructions in working memory, the model is free to pick
    structural revisions.

    Returns parsed JSON dict {v2, v3, v4, v5, strategies}. Raises
    ValueError on malformed JSON or missing keys (caller counts as failed
    attempt). Cost is tracked through `on_api_call` exactly like the main
    tool loop, so per-block sub-calls show up in job-total cost.
    """
    use_anthropic = settings.anthropic_enabled and model in ANTHROPIC_MODELS
    use_responses = settings.responses_api_enabled and model in RESPONSES_MODELS
    is_reasoning = model in REASONING_MODELS

    if use_anthropic:
        resp = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=SUBCALL_API_TIMEOUT_SEC,
        )
        on_api_call(resp.usage)
        text_parts: list[str] = []
        for blk in resp.content or []:
            if getattr(blk, "type", None) == "text":
                text_parts.append(getattr(blk, "text", ""))
        text = "".join(text_parts)
    elif use_responses:
        kwargs: dict[str, Any] = {
            "model": model,
            "input": [{"role": "user", "content": prompt}],
        }
        if is_reasoning:
            # Diversity revisions are short; "medium" is the lowest tier
            # gpt-5.5-pro accepts (low rejected with HTTP 400). Higher
            # tiers add cost without measurable quality gain on this task.
            kwargs["reasoning"] = {"effort": "medium"}
        resp = await asyncio.wait_for(
            client.responses.create(**kwargs),
            timeout=SUBCALL_API_TIMEOUT_SEC,
        )
        on_api_call(resp.usage)
        text = getattr(resp, "output_text", "") or ""
    else:
        kwargs = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if is_reasoning:
            kwargs["reasoning_effort"] = "medium"
        else:
            kwargs["temperature"] = 0.6
        resp = await asyncio.wait_for(
            client.chat.completions.create(**kwargs),
            timeout=SUBCALL_API_TIMEOUT_SEC,
        )
        on_api_call(resp.usage)
        text = (resp.choices[0].message.content or "")

    return _parse_revision_json(text)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run(
    job_id: str,
    plain_body: str,
    platform: str,
    model: str | None = None,
    reasoning_effort: str = "medium",
    tolerance: float = 0.05,
    tolerance_floor: int = 3,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
) -> None:
    """Drive the OpenAI tool-calling loop for one spintax generation job.

    Updates job status via jobs.update() on each state transition.
    Catches all exceptions and sets the job to "failed" with a
    machine-readable error key. Never raises externally - callers
    fire-and-forget via asyncio.create_task().

    State machine:
        queued -> drafting -> linting -> (iterating -> linting)* -> qa -> done
        Any uncaught exception path: -> failed.

    Args:
        job_id: the UUID returned by jobs.create() at request time.
        plain_body: the original email body text to spintax.
        platform: "instantly" or "emailbison".
        model: OpenAI model name. If None, resolved from
            app.config.settings.default_model.
        reasoning_effort: "low" | "medium" | "high" - only honored by o-series.
        tolerance: per-variation length tolerance as a fraction (default 5%).
        tolerance_floor: minimum absolute char tolerance (default 3 chars).
        max_tool_calls: hard cap on linter retries inside one job.
    """
    if model is None:
        model = settings.default_model

    # Mutable container so adapter callbacks AND exception handlers below
    # can both see accumulated cost (closures over an int rebind locally,
    # so we use list-as-box for shared mutability).
    cost_box: list[float] = [0.0]

    try:
        # Reject empty input early - never enter the OpenAI loop on garbage.
        if not plain_body or not plain_body.strip():
            _safe_fail(job_id, ERR_MALFORMED)
            return

        # T1: queued -> drafting
        _safe_update(job_id, status="drafting")
        _set_progress(job_id, "drafting", "initial generation")

        if settings.anthropic_enabled and model in ANTHROPIC_MODELS:
            client: Any = _make_anthropic_client()
        else:
            client = _make_openai_client()
        system_prompt = build_system_prompt(platform, _skills_dir())

        user_content = (
            f"Here is the plain Email 1 body to spintax. "
            f"Target platform: {platform}.\n\n"
            f"Plain body:\n```\n{plain_body}\n```\n\n"
            f"Produce the V2 spintax following ALL the rules above. "
            f"Remember: you MUST call `lint_spintax` to verify. "
            f"Never count characters yourself."
        )

        is_reasoning = model in REASONING_MODELS

        def _on_api_call(usage: Any) -> None:
            c = _compute_cost(usage, model)
            cost_box[0] += c["total_cost_usd"]
            _safe_update(
                job_id,
                api_calls_delta=1,
                cost_usd_delta=c["total_cost_usd"],
            )

        def _on_status(status: str) -> None:
            _safe_update(job_id, status=status)

        def _on_tool_call_complete() -> None:
            _safe_update(job_id, tool_calls_delta=1)

        # ---- Generation + drift-revision loop -----------------------
        # Pass 0 = initial generation with the original user prompt.
        # Passes 1..MAX_DRIFT_REVISIONS = revision retries that include the
        # previous (drifted) output and the QA warnings, asking the model
        # to fix concept drift while keeping V1 fidelity intact.
        # We break out of the loop the moment QA reports zero drift
        # warnings, OR when we've used up all revision attempts.
        # -------------------------------------------------------------
        diversity_retries = 0
        current_user_content = user_content
        outcome = None  # type: ignore[assignment]
        qa_result: dict[str, Any] = {}
        drift_revisions = 0
        unresolved_drift: list[str] = []
        # V2 diagnostics: built up across the diversity retry section.
        # Always attached to the final result (with fired=False on the
        # no-retry path) so the operator can answer "did V2 fire? did
        # sub-calls succeed? did blocks improve?" without parsing logs.
        diversity_diags = DiversityRetryDiagnostics()
        # V3 Workstream 1 diagnostics: per-block Jaccard cleanup phase
        # that runs between drift_retry exit and V2 retry start. Always
        # attached (fired=False on the clean-drift path).
        jaccard_diags = JaccardCleanupDiagnostics()
        # Per-block re-prompt counter for the cleanup phase. 0-indexed
        # block -> attempt count. Capped by MAX_JACCARD_REPROMPTS_PER_BLOCK.
        jaccard_reprompts_per_block: dict[int, int] = {}

        # Outer diversity-retry loop wraps the existing drift loop. Retries
        # only fire when DIVERSITY_GATE_LEVEL=='error' (Day 1 = 'warning' so
        # this short-circuits to a single iteration). See DIVERSITY_GATE_SPEC.md.
        while True:
            for attempt in range(MAX_DRIFT_REVISIONS + 1):
                outcome = await _run_tool_loop(
                    client,
                    model=model,
                    system_prompt=system_prompt,
                    user_content=current_user_content,
                    platform=platform,
                    tolerance=tolerance,
                    tolerance_floor=tolerance_floor,
                    is_reasoning=is_reasoning,
                    reasoning_effort=reasoning_effort,
                    max_tool_calls=max_tool_calls,
                    on_api_call=_on_api_call,
                    on_status=_on_status,
                    on_tool_call_complete=_on_tool_call_complete,
                )

                if outcome.max_calls_reached or outcome.rounds_exhausted:
                    _safe_fail(job_id, ERR_MAX_TOOL_CALLS)
                    spend.add_cost(cost_box[0])
                    return

                if not outcome.final_body.strip():
                    _safe_fail(job_id, ERR_MALFORMED)
                    spend.add_cost(cost_box[0])
                    return

                # Run QA. The drift portion drives the revision loop;
                # the rest is recorded but doesn't trigger retries (those
                # are judgment calls or structural issues we surface as-is).
                qa_result = qa(outcome.final_body, plain_body, platform)
                drift_warnings = _extract_drift_warnings(qa_result)

                if not drift_warnings:
                    unresolved_drift = []
                    break

                # Drift detected. If we still have revision budget, build a
                # revision prompt and loop. Otherwise return the last attempt
                # with the unresolved warnings recorded.
                if attempt >= MAX_DRIFT_REVISIONS:
                    unresolved_drift = drift_warnings
                    logging.warning(
                        "spintax_runner: job %s exhausted %d drift revisions, "
                        "returning best-effort body with %d unresolved warnings",
                        job_id,
                        MAX_DRIFT_REVISIONS,
                        len(drift_warnings),
                    )
                    break

                drift_revisions += 1
                logging.info(
                    "spintax_runner: job %s drift detected on pass %d (%d warnings) - "
                    "triggering revision",
                    job_id,
                    attempt,
                    len(drift_warnings),
                )
                # Surface the revision pass to the UI via the existing
                # 'iterating' status, so callers see the spinner.
                _safe_update(job_id, status="iterating")
                _set_progress(
                    job_id,
                    "drift_retry",
                    f"drift retry {drift_revisions} of {MAX_DRIFT_REVISIONS} "
                    f"({len(drift_warnings)} warnings)",
                    attempt=drift_revisions,
                    max=MAX_DRIFT_REVISIONS,
                    warning_count=len(drift_warnings),
                )
                current_user_content = _build_drift_revision_prompt(
                    plain_body=plain_body,
                    previous_body=outcome.final_body,
                    drift_warnings=drift_warnings,
                    platform=platform,
                    attempt=drift_revisions,
                )

            # ============================================================
            # V3 Workstream 1: per-block Jaccard cleanup phase
            #
            # Runs at ALL gate levels (warning AND error), unlike V2 which
            # only fires at error level. Targets blocks where drift_retry
            # shipped pure word-reorder pseudo-variants (Jaccard 0.0 vs V1)
            # that drift's concept-word check accepts but the diversity
            # gate flags. Per-block sub-calls capped by
            # MAX_JACCARD_REPROMPTS_PER_BLOCK; leftovers fall through to V2.
            # See V3_DRIFT_JACCARD_AND_V2_RETRY_SPEC.md.
            # ============================================================
            jaccard_initial_failing = compute_jaccard_failing_blocks(qa_result)
            if jaccard_initial_failing:
                jaccard_diags.fired = True
                jaccard_diags.pre_cleanup_block_scores = list(
                    qa_result.get("diversity_block_scores", []) or []
                )
                jaccard_cost_baseline = cost_box[0]
                logging.info(
                    "spintax_runner: job %s Jaccard cleanup phase entered, "
                    "%d failing block(s): %s",
                    job_id,
                    len(jaccard_initial_failing),
                    jaccard_initial_failing,
                )
                _safe_update(job_id, status="iterating")
                _set_progress(
                    job_id,
                    "jaccard_cleanup_start",
                    f"Jaccard cleanup: {len(jaccard_initial_failing)} block(s) "
                    f"with word-set duplicates",
                    failing_blocks=list(jaccard_initial_failing),
                )

            while jaccard_initial_failing:
                failing_blocks_now = compute_jaccard_failing_blocks(qa_result)
                eligible = [
                    b for b in failing_blocks_now
                    if jaccard_reprompts_per_block.get(b, 0)
                    < MAX_JACCARD_REPROMPTS_PER_BLOCK
                ]
                if not eligible:
                    # Either all clean OR all over-cap. Record the over-cap
                    # blocks so V2 / diagnostics know what to pick up.
                    jaccard_diags.blocks_at_cap = sorted(
                        b for b in failing_blocks_now
                        if jaccard_reprompts_per_block.get(b, 0)
                        >= MAX_JACCARD_REPROMPTS_PER_BLOCK
                    )
                    break

                pre_body = outcome.final_body
                pre_blocks = extract_blocks(pre_body, platform)
                pre_block_scores: list[Any] = list(
                    qa_result.get("diversity_block_scores", []) or []
                )
                pre_pair_distances: list[Any] = list(
                    qa_result.get("diversity_pair_distances", []) or []
                )

                replacements: dict[int, str] = {}
                for sc_idx, idx in enumerate(eligible, start=1):
                    if idx not in jaccard_diags.blocks_attempted:
                        jaccard_diags.blocks_attempted.append(idx)
                    attempt_num = (
                        jaccard_reprompts_per_block.get(idx, 0) + 1
                    )

                    if idx >= len(pre_blocks):
                        jaccard_diags.sub_calls.append(
                            JaccardSubCallRecord(
                                block_idx=idx,
                                attempt_num=attempt_num,
                                outcome="skipped_short_block",
                                cost_usd=0.0,
                                pre_score=0.0,
                                post_score=None,
                                error_msg="block index out of range",
                            )
                        )
                        jaccard_reprompts_per_block[idx] = attempt_num
                        continue

                    inner = pre_blocks[idx][1]
                    variants = _split_variations(inner, platform)
                    if len(variants) < 5:
                        jaccard_diags.sub_calls.append(
                            JaccardSubCallRecord(
                                block_idx=idx,
                                attempt_num=attempt_num,
                                outcome="skipped_short_block",
                                cost_usd=0.0,
                                pre_score=0.0,
                                post_score=None,
                                error_msg=f"block has {len(variants)} variants (<5)",
                            )
                        )
                        jaccard_reprompts_per_block[idx] = attempt_num
                        continue

                    v1 = variants[0]
                    v2_to_v5 = variants[1:5]
                    pre_score = (
                        pre_block_scores[idx]
                        if idx < len(pre_block_scores)
                        and pre_block_scores[idx] is not None
                        else 0.0
                    )
                    block_pairs = (
                        pre_pair_distances[idx]
                        if idx < len(pre_pair_distances)
                        else []
                    )

                    _set_progress(
                        job_id,
                        "jaccard_cleanup_subcall",
                        f"Jaccard cleanup: block {idx + 1} attempt "
                        f"{attempt_num}/{MAX_JACCARD_REPROMPTS_PER_BLOCK}",
                        block_idx=idx,
                        attempt=attempt_num,
                        max=MAX_JACCARD_REPROMPTS_PER_BLOCK,
                        pre_score=float(pre_score),
                    )

                    prompt = _build_jaccard_cleanup_prompt(
                        block_v1=v1,
                        block_variants=v2_to_v5,
                        pair_distances=list(block_pairs),
                        block_position=idx + 1,
                        platform=platform,
                        tolerance=tolerance,
                        tolerance_floor=tolerance_floor,
                    )
                    cost_before_subcall = cost_box[0]
                    try:
                        parsed = await _run_per_block_revision_subcall(
                            client,
                            model=model,
                            prompt=prompt,
                            on_api_call=_on_api_call,
                        )
                    except ValueError as exc:
                        jaccard_diags.sub_calls.append(
                            JaccardSubCallRecord(
                                block_idx=idx,
                                attempt_num=attempt_num,
                                outcome="json_parse_error",
                                cost_usd=max(
                                    0.0, cost_box[0] - cost_before_subcall
                                ),
                                pre_score=float(pre_score),
                                post_score=None,
                                error_msg=str(exc)[:200],
                            )
                        )
                        jaccard_reprompts_per_block[idx] = attempt_num
                        logging.warning(
                            "spintax_runner: job %s jaccard cleanup "
                            "block %d attempt %d JSON parse error: %s",
                            job_id, idx + 1, attempt_num, exc,
                        )
                        continue
                    except asyncio.TimeoutError:
                        # Sub-call hung past SUBCALL_API_TIMEOUT_SEC.
                        # Record it, count the attempt, move on. Don't
                        # cascade a single hung sub-call into a full job
                        # failure - the cleanup phase tolerates partial
                        # success and V2 / final QA are still downstream.
                        jaccard_diags.sub_calls.append(
                            JaccardSubCallRecord(
                                block_idx=idx,
                                attempt_num=attempt_num,
                                outcome="timeout",
                                cost_usd=max(
                                    0.0, cost_box[0] - cost_before_subcall
                                ),
                                pre_score=float(pre_score),
                                post_score=None,
                                error_msg=(
                                    f"sub-call exceeded "
                                    f"{SUBCALL_API_TIMEOUT_SEC}s"
                                ),
                            )
                        )
                        jaccard_reprompts_per_block[idx] = attempt_num
                        logging.warning(
                            "spintax_runner: job %s jaccard cleanup "
                            "block %d attempt %d timed out (>%ds)",
                            job_id, idx + 1, attempt_num,
                            SUBCALL_API_TIMEOUT_SEC,
                        )
                        continue
                    except (openai.OpenAIError, anthropic.AnthropicError) as exc:
                        jaccard_diags.sub_calls.append(
                            JaccardSubCallRecord(
                                block_idx=idx,
                                attempt_num=attempt_num,
                                outcome="api_error",
                                cost_usd=max(
                                    0.0, cost_box[0] - cost_before_subcall
                                ),
                                pre_score=float(pre_score),
                                post_score=None,
                                error_msg=str(exc)[:200],
                            )
                        )
                        jaccard_reprompts_per_block[idx] = attempt_num
                        logging.warning(
                            "spintax_runner: job %s jaccard cleanup "
                            "block %d attempt %d API error: %s",
                            job_id, idx + 1, attempt_num, exc,
                        )
                        continue

                    # Evaluate parsed output: length + Jaccard.
                    new_variants = [
                        parsed["v2"], parsed["v3"], parsed["v4"], parsed["v5"]
                    ]
                    v1_len = len(v1)
                    # Use integer floor (truncation), not round, so the band
                    # advertised to the model never exceeds what app/lint.py's
                    # check_length permits. Lint compares with strict `diff >
                    # base * tolerance` (float), so an integer floor of
                    # base*tolerance is the largest safe diff. round() would
                    # admit a 1-char overshoot at the top of the band.
                    allowed_diff = max(
                        int(v1_len * tolerance), tolerance_floor
                    )
                    band_lo = max(0, v1_len - allowed_diff)
                    band_hi = v1_len + allowed_diff
                    length_ok = all(
                        band_lo <= len(v) <= band_hi for v in new_variants
                    )

                    v1_tokens = _diversity_tokens(v1)
                    new_pair_distances = []
                    for v in new_variants:
                        d = _jaccard_distance(
                            v1_tokens, _diversity_tokens(v)
                        )
                        new_pair_distances.append(
                            d if d is not None else 1.0
                        )
                    new_block_avg = (
                        sum(new_pair_distances) / len(new_pair_distances)
                    )
                    pair_ok = all(
                        d >= BLOCK_PAIR_FLOOR for d in new_pair_distances
                    )
                    avg_ok = new_block_avg >= BLOCK_AVG_FLOOR

                    sub_cost = max(0.0, cost_box[0] - cost_before_subcall)

                    if not length_ok:
                        jaccard_diags.sub_calls.append(
                            JaccardSubCallRecord(
                                block_idx=idx,
                                attempt_num=attempt_num,
                                outcome="length_band_violation",
                                cost_usd=sub_cost,
                                pre_score=float(pre_score),
                                post_score=float(new_block_avg),
                                error_msg=(
                                    f"variants outside band {band_lo}-{band_hi}"
                                ),
                            )
                        )
                        jaccard_reprompts_per_block[idx] = attempt_num
                        continue

                    if not (pair_ok and avg_ok):
                        jaccard_diags.sub_calls.append(
                            JaccardSubCallRecord(
                                block_idx=idx,
                                attempt_num=attempt_num,
                                outcome="no_improvement",
                                cost_usd=sub_cost,
                                pre_score=float(pre_score),
                                post_score=float(new_block_avg),
                                error_msg=(
                                    None if pair_ok
                                    else "still below pair-floor"
                                ),
                            )
                        )
                        jaccard_reprompts_per_block[idx] = attempt_num
                        continue

                    # Improvement: clear path to splice.
                    new_inner = (
                        f" {v1} | {parsed['v2']} | {parsed['v3']} | "
                        f"{parsed['v4']} | {parsed['v5']}"
                    )
                    replacements[idx] = new_inner
                    jaccard_diags.sub_calls.append(
                        JaccardSubCallRecord(
                            block_idx=idx,
                            attempt_num=attempt_num,
                            outcome="improved",
                            cost_usd=sub_cost,
                            pre_score=float(pre_score),
                            post_score=float(new_block_avg),
                        )
                    )
                    jaccard_reprompts_per_block[idx] = attempt_num

                if not replacements:
                    # No successful sub-calls this iteration. Loop will
                    # exit on the next eligibility check (those blocks now
                    # have incremented counters but didn't ship; if any
                    # are still under cap they'll retry, otherwise break).
                    if all(
                        jaccard_reprompts_per_block.get(b, 0)
                        >= MAX_JACCARD_REPROMPTS_PER_BLOCK
                        for b in eligible
                    ):
                        # Every block we tried this iteration is now over cap
                        # AND no replacements. Mark the skip reason.
                        if jaccard_diags.skipped_reason is None:
                            jaccard_diags.skipped_reason = "no_successful_subcalls"
                        break
                    # Else: some block still has retries left, but didn't
                    # succeed this round either. Continue the while loop
                    # to give it another shot.
                    continue

                try:
                    new_body = reassemble(pre_body, replacements, platform)
                except Exception as exc:  # noqa: BLE001
                    logging.error(
                        "spintax_runner: job %s jaccard cleanup reassemble "
                        "failed: %s; skipping splice",
                        job_id, exc,
                    )
                    if jaccard_diags.skipped_reason is None:
                        jaccard_diags.skipped_reason = "reassemble_failed"
                    break

                outcome.final_body = new_body
                qa_result = qa(new_body, plain_body, platform)

            if jaccard_diags.fired:
                jaccard_diags.cleanup_cost_usd = max(
                    0.0, cost_box[0] - jaccard_cost_baseline
                )
                jaccard_diags.post_cleanup_block_scores = list(
                    qa_result.get("diversity_block_scores", []) or []
                )

            # ============================================================
            # V2 per-block diversity retry
            #
            # Replaces the V1 whole-email retry that produced WORSE output
            # (block scores 0.479 -> 0.083 in benchmark job 2). Each
            # failing block gets its own clean-context LLM sub-call,
            # decoupled from the drift conversation. After splicing,
            # re-run QA; per-block revert if a block regressed; final
            # fallback to pre-retry body on splice corruption.
            #
            # Always single-pass: V2 exits the outer while after one
            # retry attempt regardless of outcome (MAX_DIVERSITY_RETRIES=1).
            # Tightening to 0 retries (kill V2) is a one-line change.
            # ============================================================
            level = qa_result.get("diversity_gate_level", "warning")
            if level != "error" or diversity_retries >= MAX_DIVERSITY_RETRIES:
                if level != "error" and diversity_diags.skipped_reason is None:
                    diversity_diags.skipped_reason = "warning_level"
                break

            failing_blocks = compute_failing_blocks_from_errors(qa_result)
            if not failing_blocks:
                # Errors exist but none are diversity-related (e.g. only
                # corpus warning, which is advisory). Nothing to retry.
                diversity_diags.skipped_reason = "no_failing_blocks"
                break

            # Pre-loop budget check. Skip retry if we can't afford the
            # full set of sub-calls; partial runs confuse revert logic.
            remaining_budget = MAX_RETRY_COST_USD - cost_box[0]
            need = len(failing_blocks) * ESTIMATED_BLOCK_RETRY_COST_USD
            if (
                remaining_budget < MIN_REMAINING_BUDGET_FOR_RETRY
                or remaining_budget < need
            ):
                logging.warning(
                    "spintax_runner: job %s skipping diversity retry, "
                    "insufficient budget (need ~%.2f, have %.2f)",
                    job_id,
                    need,
                    remaining_budget,
                )
                diversity_diags.skipped_reason = "budget"
                diversity_diags.failing_blocks = list(failing_blocks)
                break

            diversity_retries += 1
            # Snapshot the cost box at retry entry so we can compute the
            # incremental V2 sub-call cost at the end (independent of the
            # main loop's accumulated cost).
            retry_cost_baseline = cost_box[0]
            diversity_diags.fired = True
            diversity_diags.failing_blocks = list(failing_blocks)
            diversity_diags.pre_retry_block_scores = list(
                qa_result.get("diversity_block_scores", []) or []
            )
            logging.info(
                "spintax_runner: job %s diversity gate failed (%d failing "
                "blocks) - per-block retry pass %d/%d",
                job_id,
                len(failing_blocks),
                diversity_retries,
                MAX_DIVERSITY_RETRIES,
            )
            _safe_update(job_id, status="iterating")
            _set_progress(
                job_id,
                "diversity_retry_start",
                f"diversity retry: {len(failing_blocks)} failing block(s)",
                failing_blocks=list(failing_blocks),
            )

            pre_body = outcome.final_body
            pre_blocks = extract_blocks(pre_body, platform)
            pre_block_scores: list[Any] = list(
                qa_result.get("diversity_block_scores", []) or []
            )
            qa_errors_pre: list[str] = list(qa_result.get("errors", []))

            replacements: dict[int, str] = {}
            total_subcalls = len(failing_blocks)
            for sc_idx, idx in enumerate(failing_blocks, start=1):
                if idx >= len(pre_blocks):
                    diversity_diags.sub_calls.append(
                        DiversitySubCallRecord(
                            block_idx=idx,
                            outcome="skipped_short_block",
                            cost_usd=0.0,
                            error_msg="block index out of range",
                        )
                    )
                    continue
                inner = pre_blocks[idx][1]
                variants = _split_variations(inner, platform)
                if len(variants) < 5:
                    diversity_diags.sub_calls.append(
                        DiversitySubCallRecord(
                            block_idx=idx,
                            outcome="skipped_short_block",
                            cost_usd=0.0,
                            error_msg=f"block has {len(variants)} variants (<5)",
                        )
                    )
                    continue
                v1 = variants[0]
                v2_to_v5 = variants[1:5]

                # Per-block diagnostics: only this block's diversity errors.
                block_prefix = f"block {idx + 1} "
                block_diags = [
                    e
                    for e in qa_errors_pre
                    if e.startswith(block_prefix)
                    and (
                        "pairwise diversity below floor" in e
                        or "diversity below floor" in e
                    )
                ]
                pre_score = (
                    pre_block_scores[idx]
                    if idx < len(pre_block_scores)
                    and pre_block_scores[idx] is not None
                    else 0.0
                )

                _set_progress(
                    job_id,
                    "diversity_retry_subcall",
                    f"diversity retry: sub-call {sc_idx}/{total_subcalls} "
                    f"(block {idx + 1}, score {float(pre_score):.2f})",
                    step=sc_idx,
                    step_total=total_subcalls,
                    block_idx=idx,
                    pre_score=float(pre_score),
                )

                prompt = _build_diversity_revision_prompt(
                    block_v1=v1,
                    block_variants=v2_to_v5,
                    block_score=float(pre_score),
                    block_pairwise_diagnostics=block_diags,
                    block_position=idx + 1,
                    platform=platform,
                    tolerance=tolerance,
                    tolerance_floor=tolerance_floor,
                )
                cost_before_subcall = cost_box[0]
                try:
                    parsed = await _run_per_block_revision_subcall(
                        client,
                        model=model,
                        prompt=prompt,
                        on_api_call=_on_api_call,
                    )
                except ValueError as exc:
                    diversity_diags.sub_calls.append(
                        DiversitySubCallRecord(
                            block_idx=idx,
                            outcome="json_parse_error",
                            cost_usd=max(0.0, cost_box[0] - cost_before_subcall),
                            error_msg=str(exc)[:200],
                        )
                    )
                    logging.warning(
                        "spintax_runner: job %s block %d sub-call failed (JSON): %s",
                        job_id,
                        idx + 1,
                        exc,
                    )
                    continue
                except asyncio.TimeoutError:
                    # V2 sub-call hung past SUBCALL_API_TIMEOUT_SEC. Record,
                    # skip, do not cascade. Final QA + post-V2 lint pass
                    # cover the rest of the safety net.
                    diversity_diags.sub_calls.append(
                        DiversitySubCallRecord(
                            block_idx=idx,
                            outcome="timeout",
                            cost_usd=max(0.0, cost_box[0] - cost_before_subcall),
                            error_msg=(
                                f"sub-call exceeded {SUBCALL_API_TIMEOUT_SEC}s"
                            ),
                        )
                    )
                    logging.warning(
                        "spintax_runner: job %s block %d V2 sub-call timed out (>%ds)",
                        job_id, idx + 1, SUBCALL_API_TIMEOUT_SEC,
                    )
                    continue
                except (openai.OpenAIError, anthropic.AnthropicError) as exc:
                    diversity_diags.sub_calls.append(
                        DiversitySubCallRecord(
                            block_idx=idx,
                            outcome="api_error",
                            cost_usd=max(0.0, cost_box[0] - cost_before_subcall),
                            error_msg=str(exc)[:200],
                        )
                    )
                    logging.warning(
                        "spintax_runner: job %s block %d sub-call failed (API): %s",
                        job_id,
                        idx + 1,
                        exc,
                    )
                    continue

                diversity_diags.sub_calls.append(
                    DiversitySubCallRecord(
                        block_idx=idx,
                        outcome="success",
                        cost_usd=max(0.0, cost_box[0] - cost_before_subcall),
                        strategies=list(parsed.get("strategies") or []),
                    )
                )

                # Build new inner: V1 preserved verbatim; V2-V5 from model.
                new_inner = (
                    f" {v1} | {parsed['v2']} | {parsed['v3']} | "
                    f"{parsed['v4']} | {parsed['v5']}"
                )
                replacements[idx] = new_inner

            if not replacements:
                logging.warning(
                    "spintax_runner: job %s no successful sub-calls; "
                    "shipping pre-retry body",
                    job_id,
                )
                diversity_diags.skipped_reason = "no_successful_subcalls"
                diversity_diags.retry_cost_usd = max(
                    0.0, cost_box[0] - retry_cost_baseline
                )
                break

            # Splice all successful replacements back into the body.
            _set_progress(
                job_id,
                "diversity_retry_splice",
                f"diversity retry: reassembling body with "
                f"{len(replacements)} replaced block(s)",
                replaced_count=len(replacements),
            )
            try:
                new_body = reassemble(pre_body, replacements, platform)
            except Exception as exc:  # noqa: BLE001
                logging.error(
                    "spintax_runner: job %s reassemble failed: %s; "
                    "shipping pre-retry body",
                    job_id,
                    exc,
                )
                diversity_diags.skipped_reason = "reassemble_failed"
                diversity_diags.retry_cost_usd = max(
                    0.0, cost_box[0] - retry_cost_baseline
                )
                break

            # Re-run QA on the spliced body. This becomes the final qa_result.
            _set_progress(
                job_id,
                "diversity_retry_qa",
                "diversity retry: re-running QA on spliced body",
            )
            new_qa = qa(new_body, plain_body, platform)
            new_block_scores: list[Any] = list(
                new_qa.get("diversity_block_scores", []) or []
            )
            diversity_diags.post_retry_block_scores = list(new_block_scores)

            # P6 per-block revert: if any retried block regressed, revert
            # that block to its pre-retry state. SpliceCorruptionError on
            # revert -> ship the pre-retry body wholesale.
            corrupted = False
            for idx in list(replacements.keys()):
                pre_s = (
                    pre_block_scores[idx]
                    if idx < len(pre_block_scores)
                    and pre_block_scores[idx] is not None
                    else 0.0
                )
                post_s = (
                    new_block_scores[idx]
                    if idx < len(new_block_scores)
                    and new_block_scores[idx] is not None
                    else 0.0
                )
                if post_s < pre_s - 0.05:  # regression threshold
                    logging.warning(
                        "spintax_runner: job %s block %d regressed "
                        "(%.2f -> %.2f); reverting that block",
                        job_id,
                        idx + 1,
                        pre_s,
                        post_s,
                    )
                    _set_progress(
                        job_id,
                        "diversity_retry_revert",
                        f"diversity retry: reverting block {idx + 1} "
                        f"({float(pre_s):.2f} -> {float(post_s):.2f})",
                        block_idx=idx,
                        pre_score=float(pre_s),
                        post_score=float(post_s),
                    )
                    try:
                        new_body = revert_single_block(
                            new_body, pre_body, idx, platform
                        )
                        diversity_diags.reverted_blocks.append(
                            DiversityRevertRecord(
                                block_idx=idx,
                                pre_score=float(pre_s),
                                post_score=float(post_s),
                                reason="regression",
                            )
                        )
                    except SpliceCorruptionError as exc:
                        logging.error(
                            "spintax_runner: job %s splice corruption on "
                            "block %d revert: %s; shipping pre-retry body",
                            job_id,
                            idx + 1,
                            exc,
                        )
                        diversity_diags.splice_corrupted = True
                        diversity_diags.reverted_blocks.append(
                            DiversityRevertRecord(
                                block_idx=idx,
                                pre_score=float(pre_s),
                                post_score=float(post_s),
                                reason="splice_corruption",
                            )
                        )
                        new_body = pre_body
                        corrupted = True
                        break

            # Bug B fix: re-lint the spliced body and revert any retried
            # blocks that introduced lint errors (length tolerance,
            # em-dashes, banned words, invisible chars, etc.). The V2
            # sub-call prompt is supposed to honor these constraints
            # (see Bug A fix in _build_diversity_revision_prompt) but the
            # lint pass here is defense-in-depth: if the model violates,
            # revert the offending block instead of shipping bad copy.
            if not corrupted:
                lint_errs, _ = lint_body(
                    new_body, platform, tolerance, tolerance_floor
                )
                lint_failing: set[int] = set()
                for err in lint_errs:
                    m = _BLOCK_INDEX_RE.match(err)
                    if m:
                        lint_failing.add(int(m.group(1)) - 1)
                already_reverted = {
                    rb.block_idx for rb in diversity_diags.reverted_blocks
                }
                lint_revert_targets = sorted(
                    (lint_failing & set(replacements.keys()))
                    - already_reverted
                )
                for idx in lint_revert_targets:
                    pre_s = (
                        pre_block_scores[idx]
                        if idx < len(pre_block_scores)
                        and pre_block_scores[idx] is not None
                        else 0.0
                    )
                    post_s = (
                        new_block_scores[idx]
                        if idx < len(new_block_scores)
                        and new_block_scores[idx] is not None
                        else 0.0
                    )
                    _set_progress(
                        job_id,
                        "diversity_retry_revert",
                        f"diversity retry: reverting block {idx + 1} "
                        f"(post-lint fail)",
                        block_idx=idx,
                        pre_score=float(pre_s),
                        post_score=float(post_s),
                        reason="post_lint_fail",
                    )
                    try:
                        new_body = revert_single_block(
                            new_body, pre_body, idx, platform
                        )
                        diversity_diags.reverted_blocks.append(
                            DiversityRevertRecord(
                                block_idx=idx,
                                pre_score=float(pre_s),
                                post_score=float(post_s),
                                reason="post_lint_fail",
                            )
                        )
                    except SpliceCorruptionError as exc:
                        logging.error(
                            "spintax_runner: job %s splice corruption on "
                            "post-lint revert of block %d: %s; "
                            "shipping pre-retry body",
                            job_id,
                            idx + 1,
                            exc,
                        )
                        diversity_diags.splice_corrupted = True
                        diversity_diags.reverted_blocks.append(
                            DiversityRevertRecord(
                                block_idx=idx,
                                pre_score=float(pre_s),
                                post_score=float(post_s),
                                reason="splice_corruption",
                            )
                        )
                        new_body = pre_body
                        corrupted = True
                        break

            outcome.final_body = new_body
            qa_result = (
                qa(pre_body, plain_body, platform)
                if corrupted
                else qa(new_body, plain_body, platform)
            )
            # If we shipped pre-retry wholesale due to corruption, the
            # post-retry scores no longer reflect what was actually
            # shipped. Update them to the pre-retry scores so the
            # diagnostic record is consistent with the body.
            if corrupted:
                diversity_diags.post_retry_block_scores = list(
                    qa_result.get("diversity_block_scores", []) or []
                )
                diversity_diags.skipped_reason = "splice_corrupted"
            diversity_diags.retry_cost_usd = max(
                0.0, cost_box[0] - retry_cost_baseline
            )

            # V2 is single-pass: exit the outer while regardless of outcome.
            break

        totals_cost = cost_box[0]

        # T4: linting -> qa
        _safe_update(job_id, status="qa")

        # Bug B fix: defense-in-depth final lint on whatever body we ship.
        # The main lint loop already validated non-V2 bodies; the V2 path
        # has its own revert pass above. This call gives us truthful
        # lint_errors/warnings/passed values for the result, replacing
        # the previously hardcoded lint_passed=True.
        final_lint_errors, final_lint_warnings = lint_body(
            outcome.final_body, platform, tolerance, tolerance_floor
        )

        result = SpintaxJobResult(
            spintax_body=outcome.final_body,
            lint_errors=list(final_lint_errors),
            lint_warnings=list(final_lint_warnings),
            lint_passed=not final_lint_errors,
            qa_errors=qa_result.get("errors", []),
            qa_warnings=qa_result.get("warnings", []),
            qa_passed=bool(qa_result.get("passed", False)),
            tool_calls=outcome.tool_calls_made,
            lint_calls=outcome.lint_calls_made,
            agent_tool_calls=outcome.agent_tool_calls_made,
            agent_tool_breakdown=dict(outcome.agent_tool_breakdown),
            api_calls=_safe_api_calls(job_id),
            cost_usd=totals_cost,
            drift_revisions=drift_revisions,
            drift_unresolved=unresolved_drift,
            qa_diversity_block_scores=qa_result.get(
                "diversity_block_scores", []
            ),
            qa_diversity_corpus_avg=qa_result.get("diversity_corpus_avg"),
            qa_diversity_floor_block_avg=qa_result.get(
                "diversity_floor_block_avg"
            ),
            qa_diversity_floor_pair=qa_result.get("diversity_floor_pair"),
            qa_diversity_gate_level=qa_result.get("diversity_gate_level"),
            diversity_retries=diversity_retries,
            diversity_retry_diagnostics=diversity_diags,
            jaccard_cleanup_diagnostics=jaccard_diags,
        )

        # T7 / T8: qa -> done (regardless of qa.passed)
        _safe_update(job_id, status="done", result=result)
        spend.add_cost(totals_cost)
        return

    except (openai.RateLimitError, anthropic.RateLimitError) as exc:
        # Anthropic returns RateLimitError (429) for some quota states; the
        # "credit balance too low" case is BadRequestError (400), handled below.
        logging.warning("spintax_runner: rate limit for job %s: %s", job_id, exc)
        _safe_fail(job_id, ERR_QUOTA, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except (anthropic.AuthenticationError, openai.AuthenticationError) as exc:
        # Bad API key, expired key, or revoked key. Distinct from rate limit -
        # this won't fix itself with retry; needs operator intervention.
        logging.error("spintax_runner: auth failed for job %s: %s", job_id, exc)
        _safe_fail(job_id, ERR_AUTH, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except (anthropic.PermissionDeniedError, openai.PermissionDeniedError) as exc:
        # Org doesn't have access to this model, or key is scoped wrong.
        logging.error("spintax_runner: permission denied for job %s: %s", job_id, exc)
        _safe_fail(job_id, ERR_AUTH, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except (anthropic.NotFoundError, openai.NotFoundError) as exc:
        # Model name typo or model deprecated. Surface as ERR_MODEL_NOT_FOUND
        # so the UI can show "model 'gpt-5.5-pro' is not available".
        logging.error("spintax_runner: model/resource not found for job %s: %s", job_id, exc)
        _safe_fail(job_id, ERR_MODEL_NOT_FOUND, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except anthropic.BadRequestError as exc:
        # Anthropic 400 covers two important real-world cases:
        #   1. "credit balance is too low" - account ran out of money
        #   2. malformed request shape (bad parameter combo, oversized input)
        # We distinguish by inspecting the message string. If we see "credit
        # balance" or "billing", route to ERR_LOW_BALANCE so the UI says so.
        msg = str(exc)
        msg_lower = msg.lower()
        is_low_balance = any(
            s in msg_lower for s in ("credit balance", "low balance", "billing", "out of credit")
        )
        code = ERR_LOW_BALANCE if is_low_balance else ERR_BAD_REQUEST
        logging.error(
            "spintax_runner: anthropic bad request for job %s (%s): %s", job_id, code, exc
        )
        _safe_fail(job_id, code, detail=msg[:500])
        spend.add_cost(cost_box[0])
    except openai.BadRequestError as exc:
        # OpenAI 400 - bad parameter, model rejected request, etc.
        # OpenAI signals quota differently (RateLimitError + insufficient_quota
        # subtype on the body), so a 400 here is almost always a request shape
        # problem rather than billing. Still log the full message.
        logging.error("spintax_runner: openai bad request for job %s: %s", job_id, exc)
        _safe_fail(job_id, ERR_BAD_REQUEST, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except (
        httpx.TimeoutException,
        openai.APITimeoutError,
        anthropic.APITimeoutError,
        asyncio.TimeoutError,
    ) as exc:
        # asyncio.TimeoutError lands here for our explicit asyncio.wait_for
        # caps on tool-loop API calls. The SDK timeouts (openai/anthropic
        # APITimeoutError) cover the SDK-internal connect/read deadlines;
        # ours covers the reasoning-stall case where the SDK's deadline is
        # extended or removed. Both surface as ERR_TIMEOUT.
        logging.warning("spintax_runner: timeout for job %s: %s", job_id, exc)
        _safe_fail(
            job_id,
            ERR_TIMEOUT,
            detail=(
                f"api call exceeded {TOOL_LOOP_API_TIMEOUT_SEC}s "
                "(model likely stalled in reasoning)"
                if isinstance(exc, asyncio.TimeoutError)
                else str(exc)[:500]
            ),
        )
        spend.add_cost(cost_box[0])
    except (openai.APIConnectionError, anthropic.APIConnectionError) as exc:
        logging.warning("spintax_runner: connection error for job %s: %s", job_id, exc)
        _safe_fail(job_id, ERR_TIMEOUT, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except KeyError:
        # Job was TTL-evicted during run - log and exit silently.
        logging.warning("spintax_runner: job %s evicted during run (TTL)", job_id)
    except asyncio.CancelledError:
        # Task was cancelled (server shutdown) - mark as failed and re-raise.
        _safe_fail(job_id, ERR_UNKNOWN, detail="task cancelled (server shutdown)")
        raise
    except Exception as exc:
        # Last-resort catch. Anything that lands here is a code bug or
        # an exception type we haven't classified yet. The detail message
        # carries the type name + message so the next failure tells us what
        # to add an explicit handler for.
        logging.exception("spintax_runner: unexpected error for job %s", job_id)
        _safe_fail(job_id, ERR_UNKNOWN, detail=f"{type(exc).__name__}: {str(exc)[:400]}")
        spend.add_cost(cost_box[0])


def _safe_api_calls(job_id: str) -> int:
    """Return the current api_calls counter on a job, or 0 if missing."""
    job = jobs.get(job_id)
    return job.api_calls if job is not None else 0
