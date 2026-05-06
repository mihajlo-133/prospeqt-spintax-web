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

from app import jobs, pipeline_dispatch, spend, spintax_runner
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
    pipeline_name, runner = pipeline_dispatch.resolve_pipeline(body.pipeline)
    job = jobs.create(
        input_text=body.text,
        platform=body.platform,
        model=resolved_model,
    )

    # Fire-and-forget. The task survives this request handler returning.
    asyncio.create_task(
        runner(
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
        progress=getattr(job, "progress", None),
        result=result_model,
        error=job.error,
        error_detail=getattr(job, "error_detail", None),
        cost_usd=job.cost_usd,
        elapsed_sec=round(elapsed, 1),
    )


def _convert_diagnostics(raw_diags: Any) -> Any:
    """Convert app.jobs.DiversityRetryDiagnostics dataclass -> pydantic embed.

    Returns the pydantic DiversityRetryDiagnosticsEmbed, or None if raw_diags
    is None. Tolerates dict input (some legacy paths) or dataclass attrs.
    """
    if raw_diags is None:
        return None
    from app.api_models import (
        DiversityRetryDiagnosticsEmbed,
        DiversityRevertEmbed,
        DiversitySubCallEmbed,
    )

    def _attr(obj: Any, name: str, default: Any) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    sub_calls = []
    for sc in _attr(raw_diags, "sub_calls", []) or []:
        sub_calls.append(
            DiversitySubCallEmbed(
                block_idx=int(_attr(sc, "block_idx", 0)),
                outcome=str(_attr(sc, "outcome", "")),
                cost_usd=float(_attr(sc, "cost_usd", 0.0) or 0.0),
                strategies=list(_attr(sc, "strategies", []) or []),
                error_msg=_attr(sc, "error_msg", None),
            )
        )
    reverted = []
    for rb in _attr(raw_diags, "reverted_blocks", []) or []:
        reverted.append(
            DiversityRevertEmbed(
                block_idx=int(_attr(rb, "block_idx", 0)),
                pre_score=float(_attr(rb, "pre_score", 0.0)),
                post_score=float(_attr(rb, "post_score", 0.0)),
                reason=str(_attr(rb, "reason", "")),
            )
        )
    return DiversityRetryDiagnosticsEmbed(
        fired=bool(_attr(raw_diags, "fired", False)),
        skipped_reason=_attr(raw_diags, "skipped_reason", None),
        failing_blocks=list(_attr(raw_diags, "failing_blocks", []) or []),
        pre_retry_block_scores=list(
            _attr(raw_diags, "pre_retry_block_scores", []) or []
        ),
        post_retry_block_scores=list(
            _attr(raw_diags, "post_retry_block_scores", []) or []
        ),
        sub_calls=sub_calls,
        reverted_blocks=reverted,
        splice_corrupted=bool(_attr(raw_diags, "splice_corrupted", False)),
        retry_cost_usd=float(_attr(raw_diags, "retry_cost_usd", 0.0) or 0.0),
    )


def _convert_jaccard_diagnostics(raw_diags: Any) -> Any:
    """Convert app.jobs.JaccardCleanupDiagnostics dataclass -> pydantic embed.

    Returns the pydantic JaccardCleanupDiagnosticsEmbed, or None if
    raw_diags is None. Tolerates dict input or dataclass attrs.
    """
    if raw_diags is None:
        return None
    from app.api_models import (
        JaccardCleanupDiagnosticsEmbed,
        JaccardSubCallEmbed,
    )

    def _attr(obj: Any, name: str, default: Any) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    sub_calls = []
    for sc in _attr(raw_diags, "sub_calls", []) or []:
        post = _attr(sc, "post_score", None)
        sub_calls.append(
            JaccardSubCallEmbed(
                block_idx=int(_attr(sc, "block_idx", 0)),
                attempt_num=int(_attr(sc, "attempt_num", 1)),
                outcome=str(_attr(sc, "outcome", "")),
                cost_usd=float(_attr(sc, "cost_usd", 0.0) or 0.0),
                pre_score=float(_attr(sc, "pre_score", 0.0) or 0.0),
                post_score=float(post) if post is not None else None,
                error_msg=_attr(sc, "error_msg", None),
            )
        )
    return JaccardCleanupDiagnosticsEmbed(
        fired=bool(_attr(raw_diags, "fired", False)),
        skipped_reason=_attr(raw_diags, "skipped_reason", None),
        blocks_attempted=list(_attr(raw_diags, "blocks_attempted", []) or []),
        sub_calls=sub_calls,
        blocks_at_cap=list(_attr(raw_diags, "blocks_at_cap", []) or []),
        cleanup_cost_usd=float(
            _attr(raw_diags, "cleanup_cost_usd", 0.0) or 0.0
        ),
        pre_cleanup_block_scores=list(
            _attr(raw_diags, "pre_cleanup_block_scores", []) or []
        ),
        post_cleanup_block_scores=list(
            _attr(raw_diags, "post_cleanup_block_scores", []) or []
        ),
    )


def _convert_pipeline_diagnostics(raw_diags: Any) -> Any:
    """Convert app.pipeline.contracts.PipelineDiagnostics -> pydantic embed.

    The beta runner attaches a PipelineDiagnostics pydantic model on the
    SpintaxJobResult dataclass via setattr() (the alpha dataclass has no
    such field). Returns None when raw_diags is None / missing, which is
    the alpha-pipeline case. Tolerates both pydantic-model attrs and the
    dict shape some tests use.
    """
    if raw_diags is None:
        return None
    from app.api_models import (
        BlockSpintaxerDiagnosticsEmbed,
        PipelineDiagnosticsEmbed,
        ProfilerDiagnosticsEmbed,
        SplitterDiagnosticsEmbed,
        SynonymPoolDiagnosticsEmbed,
    )

    def _attr(obj: Any, name: str, default: Any) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    splitter_raw = _attr(raw_diags, "splitter", None)
    profiler_raw = _attr(raw_diags, "profiler", None)
    pool_raw = _attr(raw_diags, "synonym_pool", None)
    spintaxer_raw = _attr(raw_diags, "block_spintaxer", None)

    return PipelineDiagnosticsEmbed(
        pipeline=str(_attr(raw_diags, "pipeline", "beta_v1")),
        splitter=SplitterDiagnosticsEmbed(
            duration_ms=int(_attr(splitter_raw, "duration_ms", 0) or 0),
            block_count=int(_attr(splitter_raw, "block_count", 0) or 0),
            lockable_count=int(_attr(splitter_raw, "lockable_count", 0) or 0),
        ),
        profiler=ProfilerDiagnosticsEmbed(
            duration_ms=int(_attr(profiler_raw, "duration_ms", 0) or 0),
            tone=str(_attr(profiler_raw, "tone", "") or ""),
            locked_nouns=list(_attr(profiler_raw, "locked_nouns", []) or []),
            proper_nouns=list(_attr(profiler_raw, "proper_nouns", []) or []),
        ),
        synonym_pool=SynonymPoolDiagnosticsEmbed(
            duration_ms=int(_attr(pool_raw, "duration_ms", 0) or 0),
            total_synonyms=int(_attr(pool_raw, "total_synonyms", 0) or 0),
            blocks_covered=int(_attr(pool_raw, "blocks_covered", 0) or 0),
        ),
        block_spintaxer=BlockSpintaxerDiagnosticsEmbed(
            blocks_completed=int(
                _attr(spintaxer_raw, "blocks_completed", 0) or 0
            ),
            blocks_retried=int(_attr(spintaxer_raw, "blocks_retried", 0) or 0),
            max_retries_per_block=int(
                _attr(spintaxer_raw, "max_retries_per_block", 0) or 0
            ),
            p95_block_duration_ms=int(
                _attr(spintaxer_raw, "p95_block_duration_ms", 0) or 0
            ),
        ),
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
                diversity_block_scores=list(
                    getattr(raw, "qa_diversity_block_scores", []) or []
                ),
                diversity_corpus_avg=getattr(raw, "qa_diversity_corpus_avg", None),
                diversity_floor_block_avg=getattr(
                    raw, "qa_diversity_floor_block_avg", None
                ),
                diversity_floor_pair=getattr(raw, "qa_diversity_floor_pair", None),
                diversity_gate_level=getattr(raw, "qa_diversity_gate_level", None),
            ),
            tool_calls=getattr(raw, "tool_calls", 0),
            lint_calls=int(getattr(raw, "lint_calls", 0) or 0),
            agent_tool_calls=int(getattr(raw, "agent_tool_calls", 0) or 0),
            agent_tool_breakdown=dict(getattr(raw, "agent_tool_breakdown", {}) or {}),
            api_calls=getattr(raw, "api_calls", 0),
            cost_usd=float(getattr(raw, "cost_usd", 0.0)),
            drift_revisions=int(getattr(raw, "drift_revisions", 0) or 0),
            drift_unresolved=list(getattr(raw, "drift_unresolved", []) or []),
            diversity_retries=int(getattr(raw, "diversity_retries", 0) or 0),
            diversity_retry_diagnostics=_convert_diagnostics(
                getattr(raw, "diversity_retry_diagnostics", None)
            ),
            jaccard_cleanup_diagnostics=_convert_jaccard_diagnostics(
                getattr(raw, "jaccard_cleanup_diagnostics", None)
            ),
            pipeline_diagnostics=_convert_pipeline_diagnostics(
                getattr(raw, "pipeline_diagnostics", None)
            ),
        )
    # Plain string fallback (defensive - used by some unit tests)
    if isinstance(raw, str):
        return SpintaxJobResult(
            spintax_body=raw,
            lint=LintResultEmbed(passed=True, errors=[], warnings=[]),
            qa=QAResultEmbed(
                passed=True,
                errors=[],
                warnings=[],
                diversity_block_scores=[],
                diversity_corpus_avg=None,
                diversity_floor_block_avg=None,
                diversity_floor_pair=None,
                diversity_gate_level=None,
            ),
            tool_calls=0,
            api_calls=0,
            cost_usd=0.0,
            drift_revisions=0,
            drift_unresolved=[],
            diversity_retries=0,
        )
    return None
