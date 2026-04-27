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
from pathlib import Path
from typing import Any

import httpx
import openai

from app import jobs, spend
from app.config import MODEL_PRICES, REASONING_MODELS, settings
from app.jobs import (
    ERR_MALFORMED,
    ERR_MAX_TOOL_CALLS,
    ERR_QUOTA,
    ERR_TIMEOUT,
    ERR_UNKNOWN,
    SpintaxJobResult,
)
from app.lint import lint as lint_body
from app.qa import qa

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

DEFAULT_MAX_TOOL_CALLS = 10


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


def _compute_cost(usage: Any, model: str) -> dict[str, Any]:
    """USD cost from a single OpenAI Chat Completions response.usage object.

    Falls back to zero-cost when the model is not in MODEL_PRICES (defensive).
    """
    prices = MODEL_PRICES.get(model, {"input": 0.0, "output": 0.0})
    input_tok = getattr(usage, "prompt_tokens", 0) or 0
    output_tok = getattr(usage, "completion_tokens", 0) or 0
    reasoning_tok = 0
    details = getattr(usage, "completion_tokens_details", None)
    if details is not None:
        reasoning_tok = getattr(details, "reasoning_tokens", 0) or 0
    input_cost = (input_tok / 1_000_000) * prices["input"]
    output_cost = (output_tok / 1_000_000) * prices["output"]
    return {
        "input_tokens": input_tok,
        "output_tokens": output_tok,
        "reasoning_tokens": reasoning_tok,
        "total_cost_usd": input_cost + output_cost,
    }


def _safe_fail(job_id: str, error: str) -> None:
    """Update job to failed state. Silently ignores KeyError (job evicted)."""
    try:
        jobs.update(job_id, status="failed", error=error)
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
2. IMMEDIATELY call `lint_spintax` with your draft. Do NOT promise to
   call it later. Do NOT describe what you plan to do. Emit the call now.
3. Read the tool result:
   - passed=true: respond with the final body as your text message and stop.
     Do NOT call the tool again on a passing draft.
   - passed=false: note which blocks/variations are flagged.
4. Rewrite ONLY the flagged variations. Keep every other block and every
   other variation EXACTLY as it was. Do NOT change Variation 1 of any
   block (Variation 1 is the original, word for word).
5. Call `lint_spintax` again with the updated full body.
6. Repeat steps 3-5 until `passed=true` or you have made {max_tool_calls} tool calls.

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
  tokens on their own line such as `{{{{accountSignature}}}}`, and blank lines.
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

    totals_cost: float = 0.0

    try:
        # Reject empty input early - never enter the OpenAI loop on garbage.
        if not plain_body or not plain_body.strip():
            _safe_fail(job_id, ERR_MALFORMED)
            return

        # T1: queued -> drafting
        _safe_update(job_id, status="drafting")

        client = _make_openai_client()
        system_prompt = build_system_prompt(platform, _skills_dir())

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Here is the plain Email 1 body to spintax. "
                    f"Target platform: {platform}.\n\n"
                    f"Plain body:\n```\n{plain_body}\n```\n\n"
                    f"Produce the V2 spintax following ALL the rules above. "
                    f"Remember: you MUST call `lint_spintax` to verify. "
                    f"Never count characters yourself."
                ),
            },
        ]

        tools = [TOOL_LINT_SPINTAX]
        tool_calls_made = 0
        is_reasoning = model in REASONING_MODELS
        last_lint_passed = False

        for _round in range(max_tool_calls + 2):
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

            cost = _compute_cost(response.usage, model)
            totals_cost += cost["total_cost_usd"]
            _safe_update(
                job_id,
                api_calls_delta=1,
                cost_usd_delta=cost["total_cost_usd"],
            )

            msg = response.choices[0].message

            if msg.tool_calls:
                # Append the assistant message with the tool_calls intact.
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
                    if tool_calls_made >= max_tool_calls:
                        # Tell the model to emit the final body now.
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": json.dumps(
                                    {
                                        "error": (
                                            f"Max tool calls "
                                            f"({max_tool_calls}) reached. "
                                            f"Emit final body now."
                                        )
                                    }
                                ),
                            }
                        )
                        continue

                    # T2 (drafting -> linting) on first tool call,
                    # T5 (iterating -> linting) on subsequent rounds.
                    _safe_update(job_id, status="linting")

                    if tc.function.name == "lint_spintax":
                        try:
                            args = json.loads(tc.function.arguments)
                            body = args.get("spintax_body", "")
                            tool_result = _lint_tool_wrapper(
                                body, platform, tolerance, tolerance_floor
                            )
                        except Exception as exc:  # noqa: BLE001
                            tool_result = {
                                "passed": False,
                                "error_count": 1,
                                "warning_count": 0,
                                "errors": [f"Tool failed: {exc}"],
                                "warnings": [],
                            }
                    else:
                        tool_result = {
                            "passed": False,
                            "error_count": 1,
                            "warning_count": 0,
                            "errors": [f"Unknown tool: {tc.function.name}"],
                            "warnings": [],
                        }

                    tool_calls_made += 1
                    _safe_update(job_id, tool_calls_delta=1)

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(tool_result),
                        }
                    )

                    last_lint_passed = bool(tool_result.get("passed"))
                    if not last_lint_passed:
                        # T3: linting -> iterating
                        _safe_update(job_id, status="iterating")
                        if tool_calls_made >= max_tool_calls:
                            # T6: iterating -> failed (max budget reached)
                            _safe_fail(job_id, ERR_MAX_TOOL_CALLS)
                            spend.add_cost(totals_cost)
                            return

            else:
                # Model emitted final body (or empty) with no tool_calls.
                final_body = _strip_wrapping(msg.content or "")
                if not final_body.strip():
                    _safe_fail(job_id, ERR_MALFORMED)
                    spend.add_cost(totals_cost)
                    return

                # T4: linting -> qa
                _safe_update(job_id, status="qa")

                qa_result = qa(final_body, plain_body, platform)

                result = SpintaxJobResult(
                    spintax_body=final_body,
                    lint_errors=[],
                    lint_warnings=[],
                    lint_passed=True,
                    qa_errors=qa_result.get("errors", []),
                    qa_warnings=qa_result.get("warnings", []),
                    qa_passed=bool(qa_result.get("passed", False)),
                    tool_calls=tool_calls_made,
                    api_calls=_safe_api_calls(job_id),
                    cost_usd=totals_cost,
                )

                # T7 / T8: qa -> done (regardless of qa.passed)
                _safe_update(job_id, status="done", result=result)
                spend.add_cost(totals_cost)
                return

        # Round budget exhausted without the model emitting a final body.
        _safe_fail(job_id, ERR_MAX_TOOL_CALLS)
        spend.add_cost(totals_cost)

    except openai.RateLimitError:
        _safe_fail(job_id, ERR_QUOTA)
        spend.add_cost(totals_cost)
    except (httpx.TimeoutException, openai.APITimeoutError):
        _safe_fail(job_id, ERR_TIMEOUT)
        spend.add_cost(totals_cost)
    except openai.APIConnectionError:
        _safe_fail(job_id, ERR_TIMEOUT)
        spend.add_cost(totals_cost)
    except KeyError:
        # Job was TTL-evicted during run - log and exit silently.
        logging.warning("spintax_runner: job %s evicted during run (TTL)", job_id)
    except asyncio.CancelledError:
        # Task was cancelled (server shutdown) - mark as failed and re-raise.
        _safe_fail(job_id, ERR_UNKNOWN)
        raise
    except Exception:
        logging.exception("spintax_runner: unexpected error for job %s", job_id)
        _safe_fail(job_id, ERR_UNKNOWN)
        spend.add_cost(totals_cost)


def _safe_api_calls(job_id: str) -> int:
    """Return the current api_calls counter on a job, or 0 if missing."""
    job = jobs.get(job_id)
    return job.api_calls if job is not None else 0
