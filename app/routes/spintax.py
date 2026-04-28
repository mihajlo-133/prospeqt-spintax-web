"""POST /api/spintax and GET /api/status/{job_id} routes.

What this does:
    POST /api/spintax: validates request, checks auth via Depends(require_auth),
    checks the daily spend cap (raises 429 if hit), creates a job in the store,
    fires asyncio.create_task(run(...)) for the async generation, returns
    job_id immediately.

    GET /api/status/{job_id}: returns current job state. 404 if job not found.
    Auth-gated.

What it depends on:
    app.jobs (create, get, SpintaxJobResult dataclass)
    app.spintax_runner (run coroutine)
    app.api_models (SpintaxRequest, SpintaxResponse, JobStatusResponse,
        SpintaxJobResult model, LintResultEmbed, QAResultEmbed, ErrorEnvelope)
    app.dependencies.require_auth (cookie gate)
    app.spend (daily-cap enforcement)
    app.config.settings (default model)

What depends on it:
    app.main mounts this router.
"""

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app import jobs, spend, spintax_runner
from app.api_models import (
    JobStatusResponse,
    LintResultEmbed,
    QAResultEmbed,
    SpintaxJobResult,
    SpintaxRequest,
    SpintaxResponse,
)
from app.config import settings
from app.dependencies import require_auth

router = APIRouter(tags=["spintax"])


@router.post(
    "/api/spintax",
    dependencies=[Depends(require_auth)],
)
async def create_spintax_job(body: SpintaxRequest):
    """Kick off a spintax generation job. Returns the job_id immediately.

    The actual generation runs in the background as an asyncio task.
    Poll GET /api/status/{job_id} to track progress.

    Errors:
        401 (require_auth): no session cookie
        422 (pydantic): invalid input
        429 (spend.check_cap): daily USD cap reached.
            Body shape: {error, cap_usd, spent_usd, resets_at} at the
            top level (NOT wrapped in 'detail').
    """
    # Spend cap is checked BEFORE creating the job so we don't leak orphans.
    try:
        spend.check_cap()
    except HTTPException as exc:
        if exc.status_code == 429 and isinstance(exc.detail, dict):
            # Flatten to top-level shape that the contract requires.
            return JSONResponse(status_code=429, content=exc.detail)
        raise

    resolved_model = body.model or settings.default_model
    job = jobs.create(
        input_text=body.text,
        platform=body.platform,
        model=resolved_model,
    )

    # Fire-and-forget. The task survives this request handler returning.
    asyncio.create_task(
        spintax_runner.run(
            job_id=job.job_id,
            plain_body=body.text,
            platform=body.platform,
            model=resolved_model,
            reasoning_effort=body.reasoning_effort,
        )
    )

    return SpintaxResponse(job_id=job.job_id)


@router.get(
    "/api/status/{job_id}",
    response_model=JobStatusResponse,
    dependencies=[Depends(require_auth)],
)
async def get_job_status(job_id: str) -> JobStatusResponse:
    """Return current state of a job by ID, or 404 if not found / expired."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    result_model: SpintaxJobResult | None = None
    if job.status == "done" and job.result is not None:
        result_model = _convert_result(job.result)

    elapsed = max(0.0, time.monotonic() - (job.started_at or time.monotonic()))
    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        result=result_model,
        error=job.error,
        error_detail=getattr(job, "error_detail", None),
        cost_usd=job.cost_usd,
        elapsed_sec=round(elapsed, 1),
    )


def _convert_result(raw: Any) -> SpintaxJobResult | None:
    """Convert a job's raw result to the pydantic SpintaxJobResult model.

    The runner stores a dataclass (app.jobs.SpintaxJobResult). Direct
    string results (used in some unit tests) are wrapped into a result
    that exposes spintax_body only.
    """
    # Dataclass branch (the runner's normal path)
    if hasattr(raw, "spintax_body"):
        return SpintaxJobResult(
            spintax_body=raw.spintax_body,
            lint=LintResultEmbed(
                passed=getattr(raw, "lint_passed", True),
                errors=list(getattr(raw, "lint_errors", []) or []),
                warnings=list(getattr(raw, "lint_warnings", []) or []),
            ),
            qa=QAResultEmbed(
                passed=getattr(raw, "qa_passed", True),
                errors=list(getattr(raw, "qa_errors", []) or []),
                warnings=list(getattr(raw, "qa_warnings", []) or []),
            ),
            tool_calls=getattr(raw, "tool_calls", 0),
            api_calls=getattr(raw, "api_calls", 0),
            cost_usd=float(getattr(raw, "cost_usd", 0.0)),
            drift_revisions=int(getattr(raw, "drift_revisions", 0) or 0),
            drift_unresolved=list(getattr(raw, "drift_unresolved", []) or []),
        )
    # Plain string fallback (defensive - used by some unit tests)
    if isinstance(raw, str):
        return SpintaxJobResult(
            spintax_body=raw,
            lint=LintResultEmbed(passed=True, errors=[], warnings=[]),
            qa=QAResultEmbed(passed=True, errors=[], warnings=[]),
            tool_calls=0,
            api_calls=0,
            cost_usd=0.0,
            drift_revisions=0,
            drift_unresolved=[],
        )
    return None
