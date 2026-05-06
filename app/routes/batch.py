"""Batch spintax HTTP routes.

What this does:
    Exposes four endpoints for the batch flow described in
    BATCH_API_SPEC.md:

      POST /api/spintax/batch          — submit + (optional) dry-run + fire jobs
      GET  /api/spintax/batch/{id}     — poll status
      GET  /api/spintax/batch/{id}/download — download .zip when done
      POST /api/spintax/batch/{id}/cancel   — cancel a running batch

What it depends on:
    - app.parser.parse_markdown — async parse via o4-mini
    - app.batch — in-memory batch state + run_batch coroutine
    - app.zip_builder — produces the final .zip bytes
    - app.dependencies.require_auth — session cookie gate
    - app.spend — daily cap pre-flight estimate (informational only)

What depends on it:
    - app.main mounts this router (Phase 4)
    - tests/test_routes_batch.py
"""

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from app import batch, parser
from app.api_models import VALID_PLATFORMS
from app.batch import _should_skip_spintax
from app.config import MODEL_PRICES
from app.dependencies import require_auth
from app.zip_builder import build_zip, zip_filename

logger = logging.getLogger(__name__)

router = APIRouter(tags=["batch"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class BatchRequest(BaseModel):
    """Body for POST /api/spintax/batch."""

    md: str = Field(description="Full markdown document content.")
    platform: str = Field(description="'instantly' or 'emailbison'.")
    model: str | None = Field(
        default=None,
        description="OpenAI model. Defaults to settings.default_model.",
    )
    concurrency: int = Field(
        default=batch.DEFAULT_CONCURRENCY,
        ge=1,
        le=batch.MAX_CONCURRENCY,
        description=f"Concurrent jobs (1..{batch.MAX_CONCURRENCY}).",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "If true, parse only and return structure WITHOUT firing any "
            "spintax jobs. Use this to confirm parser output before paying."
        ),
    )
    reasoning_effort: Literal["low", "medium", "high"] = Field(
        default="high",
        description=(
            "Reasoning effort for o-series and gpt-5.x models. Defaults "
            "to 'high' so the cleanup phase has enough budget to satisfy "
            "register, domain noun lock, and Jaccard constraints in one shot."
        ),
    )
    pipeline: Literal["alpha", "beta_v1"] | None = Field(
        default=None,
        description=(
            "Spintax pipeline override. None = use SPINTAX_PIPELINE env var. "
            "'alpha' = whole-email runner. 'beta_v1' = block-first runner."
        ),
    )

    @field_validator("platform")
    @classmethod
    def _platform_valid(cls, v: str) -> str:
        if v not in VALID_PLATFORMS:
            raise ValueError(f"platform must be one of {sorted(VALID_PLATFORMS)!r}, got {v!r}")
        return v

    @field_validator("md")
    @classmethod
    def _md_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("md must not be empty")
        return v


class BatchSegmentSummary(BaseModel):
    """One segment in the parsed-summary block of the response."""

    name: str
    section: str
    email_count: int
    emails_to_spin: int  # bodies that will actually call OpenAI (Email 1 only)
    warnings: list[str]


class BatchParsedSummary(BaseModel):
    """Top-level parsed-structure block."""

    segments: list[BatchSegmentSummary]
    total_bodies: int
    total_bodies_to_spin: int  # sum of segments[].emails_to_spin
    warnings: list[str]


class BatchSubmitResponse(BaseModel):
    """Response for POST /api/spintax/batch."""

    batch_id: str
    parsed: BatchParsedSummary
    status: str
    fired: bool
    total_jobs: int


class BatchStatusResponse(BaseModel):
    """Response for GET /api/spintax/batch/{id}."""

    batch_id: str
    status: str
    platform: str
    model: str
    completed: int
    failed: int
    in_progress: int
    retrying: int
    queued: int
    total: int
    retries_used: int
    elapsed_sec: float
    cost_usd_so_far: float
    cost_usd_estimated_total: float
    failure_reason: str | None
    download_url: str | None
    parsed: BatchParsedSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parsed_summary(state: batch.BatchState) -> BatchParsedSummary:
    """Build the BatchParsedSummary block from a BatchState.

    Computes `emails_to_spin` per segment using the same Email-2-skip rule
    that the runner enforces, so the UI count matches the actual OpenAI
    call count rather than the raw body count.
    """
    seg_summaries: list[BatchSegmentSummary] = []
    total_to_spin = 0
    for seg in state.segments:
        to_spin = sum(1 for em in seg.emails if not _should_skip_spintax(em.email_label))
        total_to_spin += to_spin
        seg_summaries.append(
            BatchSegmentSummary(
                name=seg.segment_name,
                section=seg.section,
                email_count=len(seg.emails),
                emails_to_spin=to_spin,
                warnings=list(seg.parser_warnings),
            )
        )
    return BatchParsedSummary(
        segments=seg_summaries,
        total_bodies=state.total_bodies,
        total_bodies_to_spin=total_to_spin,
        warnings=list(state.parse_warnings),
    )


def _estimate_cost_usd(state: batch.BatchState) -> float:
    """Crude per-batch estimate based on the chosen model's pricing.

    Heuristic: ~3000 input tokens + ~2000 output tokens per body for o3
    (verified against historical runs in the existing single-body flow).
    Used only for the UI cost-so-far/estimated display.
    """
    prices = MODEL_PRICES.get(state.model, {"input": 2.0, "output": 8.0})
    per_body = (3000 / 1_000_000) * prices["input"] + (2000 / 1_000_000) * prices["output"]
    return round(per_body * state.total_bodies, 2)


def _build_status(state: batch.BatchState) -> BatchStatusResponse:
    """Translate a BatchState into the wire response shape."""
    counts = state.counts()
    download_url: str | None = None
    if state.status in (batch.BATCH_STATUS_DONE, batch.BATCH_STATUS_CANCELLED):
        # Allow download once the batch is done or cancelled (partial bytes).
        download_url = f"/api/spintax/batch/{state.batch_id}/download"

    return BatchStatusResponse(
        batch_id=state.batch_id,
        status=state.status,
        platform=state.platform,
        model=state.model,
        completed=counts["completed"],
        failed=counts["failed"],
        in_progress=counts["in_progress"],
        retrying=counts["retrying"],
        queued=counts["queued"],
        total=state.total_bodies,
        retries_used=state.total_retries(),
        elapsed_sec=round(state.elapsed_sec(), 1),
        cost_usd_so_far=round(state.total_cost_usd(), 4),
        cost_usd_estimated_total=_estimate_cost_usd(state),
        failure_reason=state.failure_reason,
        download_url=download_url,
        parsed=_parsed_summary(state),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/api/spintax/batch",
    response_model=BatchSubmitResponse,
    dependencies=[Depends(require_auth)],
)
async def submit_batch(body: BatchRequest) -> BatchSubmitResponse:
    """Parse the .md and (unless dry_run) fire spintax jobs.

    Errors:
        401 (require_auth)
        422 (pydantic) — bad platform, empty md, etc.
        500 — unexpected parser error
    """
    try:
        parsed = await parser.parse_markdown(body.md)
    except ValueError as exc:
        # Empty md or similar — surface as 422.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("submit_batch: parser failed")
        raise HTTPException(
            status_code=500,
            detail={"error": "parser_failed", "message": str(exc)},
        ) from exc

    if not parsed.segments:
        # Parser ran but found nothing. Don't 500 — surface the warnings.
        raise HTTPException(
            status_code=422,
            detail={
                "error": "no_segments_found",
                "message": "Parser found no email segments in the document.",
                "warnings": parsed.warnings,
            },
        )

    state = batch.create_batch(
        parsed=parsed,
        platform=body.platform,
        model=body.model,
        concurrency=body.concurrency,
        reasoning_effort=body.reasoning_effort,
        pipeline=body.pipeline,
    )

    fired = False
    if not body.dry_run:
        # Fire the orchestrator as a background task. Survives the
        # request returning. State updates are visible via GET status.
        asyncio.create_task(batch.run_batch(state.batch_id))
        fired = True
        # Reflect the running status immediately so the first poll
        # doesn't show 'parsed' (which would confuse the UI).
        state.status = batch.BATCH_STATUS_RUNNING

    return BatchSubmitResponse(
        batch_id=state.batch_id,
        parsed=_parsed_summary(state),
        status=state.status,
        fired=fired,
        total_jobs=state.total_bodies,
    )


@router.get(
    "/api/spintax/batch/{batch_id}",
    response_model=BatchStatusResponse,
    dependencies=[Depends(require_auth)],
)
async def get_batch_status(batch_id: str) -> BatchStatusResponse:
    """Poll status of a batch."""
    state = batch.get_batch(batch_id)
    if state is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return _build_status(state)


@router.get(
    "/api/spintax/batch/{batch_id}/download",
    dependencies=[Depends(require_auth)],
)
async def download_batch(batch_id: str) -> Response:
    """Stream the .zip back to the client.

    409 if the batch is still running. Allowed when status is `done`,
    `cancelled`, or `failed` (partial output may still be useful).
    """
    state = batch.get_batch(batch_id)
    if state is None:
        raise HTTPException(status_code=404, detail="batch not found")

    if state.status in (batch.BATCH_STATUS_PARSED, batch.BATCH_STATUS_RUNNING):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "batch_not_complete",
                "message": (
                    f"Batch is {state.status}. Wait for completion or cancel before downloading."
                ),
                "status": state.status,
            },
        )

    zip_bytes = build_zip(state)
    filename = zip_filename(state)
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(zip_bytes)),
        },
    )


@router.post(
    "/api/spintax/batch/{batch_id}/cancel",
    dependencies=[Depends(require_auth)],
)
async def cancel_batch_route(batch_id: str) -> dict[str, Any]:
    """Mark a batch as cancelled. In-flight bodies finish naturally."""
    state = batch.get_batch(batch_id)
    if state is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if state.status in (
        batch.BATCH_STATUS_DONE,
        batch.BATCH_STATUS_FAILED,
        batch.BATCH_STATUS_CANCELLED,
    ):
        # Already terminal — no-op but tell the caller.
        return {
            "batch_id": batch_id,
            "status": state.status,
            "cancelled": False,
            "message": f"batch already {state.status}",
        }
    batch.cancel_batch(batch_id)
    return {
        "batch_id": batch_id,
        "status": batch.BATCH_STATUS_CANCELLED,
        "cancelled": True,
    }
