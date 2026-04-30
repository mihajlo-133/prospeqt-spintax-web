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
    SpintaxJobResult,
)
from app.lint import lint as lint_body
from app.qa import qa
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

        response = await client.chat.completions.create(**kwargs)
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

        response = await client.responses.create(**kwargs)
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
        r = await client.messages.create(messages=messages, **static_kwargs)
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
        f"2. Restructure or swap synonyms only. Do not add new framings, "
        f"stakeholders, time horizons, or actors.\n"
        f"3. Variation 1 must remain word-for-word identical to the original "
        f"input paragraph.\n"
        f"4. All {{{{variables}}}} preserved exactly with correct brackets.\n"
        f"5. Re-call lint_spintax to verify length tolerance and banned-word "
        f"rules before emitting the revised body.\n\n"
        f"Produce the corrected spintax now."
    )


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
        current_user_content = user_content
        outcome = None  # type: ignore[assignment]
        qa_result: dict[str, Any] = {}
        drift_revisions = 0
        unresolved_drift: list[str] = []

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
            current_user_content = _build_drift_revision_prompt(
                plain_body=plain_body,
                previous_body=outcome.final_body,
                drift_warnings=drift_warnings,
                platform=platform,
                attempt=drift_revisions,
            )

        totals_cost = cost_box[0]

        # T4: linting -> qa
        _safe_update(job_id, status="qa")

        result = SpintaxJobResult(
            spintax_body=outcome.final_body,
            lint_errors=[],
            lint_warnings=[],
            lint_passed=True,
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
    except (httpx.TimeoutException, openai.APITimeoutError, anthropic.APITimeoutError) as exc:
        logging.warning("spintax_runner: timeout for job %s: %s", job_id, exc)
        _safe_fail(job_id, ERR_TIMEOUT, detail=str(exc)[:500])
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
