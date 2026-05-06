"""Beta block-first runner: production wrapper around ``pipeline_runner.run_pipeline``.

This module exposes an ``async def run(...)`` with the **same signature** as
``app.spintax_runner.run`` so ``pipeline_dispatch.resolve_pipeline`` can
return either runner interchangeably.

Where alpha drives a single-call tool-loop until QA passes, the beta runner
drives the staged pipeline (splitter -> profiler -> synonym pool -> per-block
spintaxer -> assembler) and then runs the existing alpha QA + lint validators
to populate a ``SpintaxJobResult`` the API route can serve unchanged.

What this does:
    - Updates ``app.jobs`` on every state transition (queued -> drafting -> qa -> done).
    - Streams cost via ``_on_api_call``: every LLM response is metered and
      accumulated into ``job.cost_usd``; ``api_calls_delta`` increments per call.
    - Catches OpenAI / Anthropic exception types the same way alpha does so
      the UI shows the same ``error`` keys regardless of which pipeline ran.
    - Maps ``PipelineStageError`` to the canonical pipeline error keys
      (``splitter_error`` etc.).
    - Re-runs ``app.qa.qa`` and ``app.lint.lint`` on the assembled spintax to
      populate the lint / qa fields of ``SpintaxJobResult`` and attaches the
      ``PipelineDiagnostics`` for ``/api/status`` exposure.

What it depends on:
    - app.pipeline.pipeline_runner.run_pipeline (lazy-imported at call time)
    - app.jobs (state transitions, SpintaxJobResult dataclass, error keys)
    - app.lint.lint as lint_body (final lint pass)
    - app.qa.qa (final QA pass)
    - app.spend.add_cost (daily cap accumulator)
    - app.config.settings (default model)

What depends on it:
    - app.pipeline_dispatch.resolve_pipeline (selects this runner when
      ``SPINTAX_PIPELINE=beta_v1`` or ``pipeline='beta_v1'`` override).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import anthropic
import httpx
import openai

from app import jobs, spend
from app.config import settings
from app.jobs import (
    ERR_AUTH,
    ERR_BAD_REQUEST,
    ERR_LOW_BALANCE,
    ERR_MALFORMED,
    ERR_MODEL_NOT_FOUND,
    ERR_QUOTA,
    ERR_TIMEOUT,
    ERR_UNKNOWN,
    SpintaxJobResult,
)
from app.pipeline.contracts import (
    ERR_BLOCK_SPINTAX,
    ERR_PROFILER,
    ERR_SPLITTER,
    ERR_SYNONYM_POOL,
    PipelineStageError,
)


def _safe_fail(job_id: str, error: str, detail: str | None = None) -> None:
    """Mark a job failed; swallow KeyError if the job has TTL-evicted."""
    try:
        jobs.update(job_id, status="failed", error=error, error_detail=detail)
    except KeyError:
        logging.warning(
            "spintax_runner_v2: job %s evicted before fail update", job_id
        )


def _safe_update(job_id: str, **fields: Any) -> None:
    """Mirror of alpha's helper. Wrapper around jobs.update() that swallows
    KeyError so the asyncio task does not crash on a TTL-evicted job."""
    try:
        jobs.update(job_id, **fields)
    except KeyError:
        logging.warning("spintax_runner_v2: job %s missing during update", job_id)


def _set_progress(job_id: str, phase: str, label: str, **extra: Any) -> None:
    """Publish a live-progress payload on the job for the /api/status route.

    Phase slugs surfaced by this runner:
        - "drafting"        : pipeline kicked off (initial bookkeeping).
        - "pipeline_stages" : stages 1-5 in flight.
        - "qa"              : final QA + lint pass on assembled spintax.
    """
    payload: dict[str, Any] = {"phase": phase, "label": label}
    payload.update(extra)
    _safe_update(job_id, progress=payload)


def _safe_api_calls(job_id: str) -> int:
    """Return the current api_calls counter on a job, or 0 if missing."""
    job = jobs.get(job_id)
    return job.api_calls if job is not None else 0


_STAGE_ERROR_TO_JOB_ERROR: dict[str, str] = {
    ERR_SPLITTER: ERR_SPLITTER,
    ERR_PROFILER: ERR_PROFILER,
    ERR_SYNONYM_POOL: ERR_SYNONYM_POOL,
    ERR_BLOCK_SPINTAX: ERR_BLOCK_SPINTAX,
}


async def run(
    job_id: str,
    plain_body: str,
    platform: str,
    model: str | None = None,
    reasoning_effort: str = "medium",
    tolerance: float = 0.05,
    tolerance_floor: int = 3,
    max_tool_calls: int = 0,
) -> None:
    """Drive the beta block-first pipeline for one spintax generation job.

    Same signature shape as ``app.spintax_runner.run`` so the dispatcher can
    select either runner without route-side changes. ``max_tool_calls`` is
    accepted for signature compatibility but unused (the beta pipeline has
    no agent-tool loop; per-block retries are the equivalent guardrail and
    are configured inside ``run_pipeline`` itself).

    State machine:
        queued -> drafting -> qa -> done
        Any uncaught exception path -> failed.

    Args:
        job_id: the UUID returned by ``jobs.create()`` at request time.
        plain_body: original email body to spintax.
        platform: ``"instantly"`` or ``"emailbison"``.
        model: OpenAI model name for the **spintaxer** stage (heaviest call).
            ``None`` resolves to ``settings.default_model``. The lighter
            stages (splitter / profiler / pool) keep their defaults inside
            ``run_pipeline``.
        reasoning_effort: ``"low" | "medium" | "high"``. Forwarded to the
            spintaxer reasoning param. Other stages run at their defaults.
        tolerance / tolerance_floor: lint tolerance bounds, applied to the
            final assembled spintax.
        max_tool_calls: ignored. Present for signature parity with alpha.
    """
    if model is None:
        model = settings.default_model

    # cost_box is a one-element list so the on_api_call closure can mutate
    # accumulated cost without a `nonlocal` in every nested helper - same
    # pattern alpha uses.
    cost_box: list[float] = [0.0]

    try:
        if not plain_body or not plain_body.strip():
            _safe_fail(job_id, ERR_MALFORMED)
            return

        _safe_update(job_id, status="drafting")
        _set_progress(job_id, "drafting", "starting beta block-first pipeline")

        # Lazy imports keep this module's import-time footprint tiny. Both
        # alpha qa (~850 lines + lint transitive) and the runner itself are
        # only loaded at first call.
        from app.lint import lint as lint_body
        from app.pipeline.pipeline_runner import run_pipeline
        from app.qa import qa as run_qa
        # _compute_cost lives in alpha; reusing avoids drift across pricing tables.
        from app.spintax_runner import _compute_cost

        def _on_api_call(usage: Any) -> None:
            """Per-LLM-call meter. Forwarded to every stage via run_pipeline."""
            c = _compute_cost(usage, model)
            cost_box[0] += c["total_cost_usd"]
            _safe_update(
                job_id,
                api_calls_delta=1,
                cost_usd_delta=c["total_cost_usd"],
            )

        _set_progress(job_id, "pipeline_stages", "splitter + profiler in flight")

        assembled, diagnostics = await run_pipeline(
            plain_body,
            platform=platform,
            spintaxer_model=model,
            spintaxer_reasoning=reasoning_effort,
            tolerance=tolerance,
            tolerance_floor=tolerance_floor,
            on_api_call=_on_api_call,
        )

        _safe_update(job_id, status="qa")
        _set_progress(job_id, "qa", "final lint + QA on assembled spintax")

        final_lint_errors, final_lint_warnings = lint_body(
            assembled.spintax, platform, tolerance, tolerance_floor
        )
        qa_result = run_qa(assembled.spintax, plain_body, platform)

        result = SpintaxJobResult(
            spintax_body=assembled.spintax,
            lint_errors=list(final_lint_errors),
            lint_warnings=list(final_lint_warnings),
            lint_passed=not final_lint_errors,
            qa_errors=qa_result.get("errors", []),
            qa_warnings=qa_result.get("warnings", []),
            qa_passed=bool(qa_result.get("passed", False)),
            tool_calls=0,
            lint_calls=0,
            agent_tool_calls=0,
            agent_tool_breakdown={},
            api_calls=_safe_api_calls(job_id),
            cost_usd=cost_box[0],
            drift_revisions=0,
            drift_unresolved=[],
            qa_diversity_block_scores=qa_result.get("diversity_block_scores", []),
            qa_diversity_corpus_avg=qa_result.get("diversity_corpus_avg"),
            qa_diversity_floor_block_avg=qa_result.get("diversity_floor_block_avg"),
            qa_diversity_floor_pair=qa_result.get("diversity_floor_pair"),
            qa_diversity_gate_level=qa_result.get("diversity_gate_level"),
            diversity_retries=0,
        )
        # Attach the beta diagnostics on a side attribute so the API
        # converter can pick it up without touching the alpha SpintaxJobResult
        # dataclass shape. setattr() rather than dataclass field keeps the
        # alpha reload-safety guard in app.jobs unaffected.
        setattr(result, "pipeline_diagnostics", diagnostics)

        _safe_update(job_id, status="done", result=result)
        spend.add_cost(cost_box[0])
        return

    except PipelineStageError as exc:
        error_key = _STAGE_ERROR_TO_JOB_ERROR.get(exc.error_key, ERR_UNKNOWN)
        logging.warning(
            "spintax_runner_v2: pipeline stage failure for job %s (%s): %s",
            job_id,
            error_key,
            exc.detail or exc,
        )
        _safe_fail(job_id, error_key, detail=(exc.detail or str(exc))[:500])
        spend.add_cost(cost_box[0])
    except (openai.RateLimitError, anthropic.RateLimitError) as exc:
        logging.warning("spintax_runner_v2: rate limit for job %s: %s", job_id, exc)
        _safe_fail(job_id, ERR_QUOTA, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except (anthropic.AuthenticationError, openai.AuthenticationError) as exc:
        logging.error("spintax_runner_v2: auth failed for job %s: %s", job_id, exc)
        _safe_fail(job_id, ERR_AUTH, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except (anthropic.PermissionDeniedError, openai.PermissionDeniedError) as exc:
        logging.error(
            "spintax_runner_v2: permission denied for job %s: %s", job_id, exc
        )
        _safe_fail(job_id, ERR_AUTH, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except (anthropic.NotFoundError, openai.NotFoundError) as exc:
        logging.error(
            "spintax_runner_v2: model/resource not found for job %s: %s",
            job_id,
            exc,
        )
        _safe_fail(job_id, ERR_MODEL_NOT_FOUND, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except anthropic.BadRequestError as exc:
        msg = str(exc)
        msg_lower = msg.lower()
        is_low_balance = any(
            s in msg_lower
            for s in ("credit balance", "low balance", "billing", "out of credit")
        )
        code = ERR_LOW_BALANCE if is_low_balance else ERR_BAD_REQUEST
        logging.error(
            "spintax_runner_v2: anthropic bad request for job %s (%s): %s",
            job_id,
            code,
            exc,
        )
        _safe_fail(job_id, code, detail=msg[:500])
        spend.add_cost(cost_box[0])
    except openai.BadRequestError as exc:
        logging.error(
            "spintax_runner_v2: openai bad request for job %s: %s", job_id, exc
        )
        _safe_fail(job_id, ERR_BAD_REQUEST, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except (
        httpx.TimeoutException,
        openai.APITimeoutError,
        anthropic.APITimeoutError,
        asyncio.TimeoutError,
    ) as exc:
        logging.warning("spintax_runner_v2: timeout for job %s: %s", job_id, exc)
        _safe_fail(job_id, ERR_TIMEOUT, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except (openai.APIConnectionError, anthropic.APIConnectionError) as exc:
        logging.warning(
            "spintax_runner_v2: connection error for job %s: %s", job_id, exc
        )
        _safe_fail(job_id, ERR_TIMEOUT, detail=str(exc)[:500])
        spend.add_cost(cost_box[0])
    except KeyError:
        logging.warning(
            "spintax_runner_v2: job %s evicted during run (TTL)", job_id
        )
    except asyncio.CancelledError:
        _safe_fail(job_id, ERR_UNKNOWN, detail="task cancelled (server shutdown)")
        raise
    except Exception as exc:
        logging.exception("spintax_runner_v2: unexpected error for job %s", job_id)
        _safe_fail(
            job_id,
            ERR_UNKNOWN,
            detail=f"{type(exc).__name__}: {str(exc)[:400]}",
        )
        spend.add_cost(cost_box[0])
