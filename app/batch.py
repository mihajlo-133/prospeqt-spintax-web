"""Batch spintax orchestrator.

What this does:
    Drives N parallel spintax jobs from a parser output, with bounded
    concurrency, retry-on-failure, and cancel support. Each "body" in
    the batch is one email body that gets spintaxed via the existing
    spintax_runner pipeline.

    The batch state is held in-memory (single-worker Render free tier)
    behind a threading lock, mirroring the design of app.jobs.

What it depends on:
    - app.parser (ParseResult / ParsedSegment / ParsedEmail) — input shape
    - app.jobs — per-body job tracking via the existing in-memory store
    - app.spintax_runner — the actual OpenAI tool-calling loop
    - app.spend — daily cap pre-check before firing each retry
    - asyncio (Semaphore for concurrency, gather for orchestration)

What depends on it:
    - app.routes.batch — exposes create / status / cancel endpoints
    - app.zip_builder — reads BatchState to produce the final .zip

Hard rules from BATCH_API_SPEC.md:
    - Default concurrency = 5, max = 20
    - Retry up to 3x with backoff [5s, 15s, 45s]
    - Hard failures (quota, org not verified) skip retries and stop batch
    - Cancel signal stops queueing new bodies; in-flight bodies finish
    - Subjects are NEVER touched — passed through verbatim from parser
    - TTL: 24h after completion
"""

import asyncio
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app import jobs, spend
from app.config import settings
from app.parser import ParseResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONCURRENCY = 5
MAX_CONCURRENCY = 20
MAX_RETRIES = 3  # 1 initial attempt + up to 3 retries

# Backoff between retries. Indexed by retry attempt (0 = first retry).
RETRY_BACKOFF_SEC: list[float] = [5.0, 15.0, 45.0]

# Retain completed batches for 24 hours so the team has time to download.
BATCH_TTL_SECONDS = 86_400

# Status values for a body.
BODY_STATUS_QUEUED = "queued"
BODY_STATUS_RUNNING = "running"
BODY_STATUS_DONE = "done"
BODY_STATUS_RETRYING = "failed_retrying"
BODY_STATUS_FAILED = "failed_permanent"

# Status values for the batch.
BATCH_STATUS_PARSED = "parsed"
BATCH_STATUS_RUNNING = "running"
BATCH_STATUS_DONE = "done"
BATCH_STATUS_FAILED = "failed"
BATCH_STATUS_CANCELLED = "cancelled"

# Errors that should not be retried — they will fail every attempt.
HARD_FAIL_ERRORS = frozenset(
    {
        "openai_quota",
        "openai_org_not_verified",
        "daily_spend_cap_exceeded",
    }
)

# Strategist convention: only Email 1 of each segment gets spintaxed.
# Email 2 (and any higher-numbered email) is a hand-written follow-up
# that the strategist owns directly. The .md output still includes it,
# but the body passes through verbatim — no OpenAI call, no cost.
# Variations like 'Email 1 (Var A)' / 'Email 1 (Var B)' are still
# Email 1 and DO get spintaxed.
_EMAIL_NUM_RE = re.compile(r"email\s+(\d+)", re.IGNORECASE)


def _should_skip_spintax(email_label: str) -> bool:
    """True if the email is Email 2 or higher — pass through verbatim.

    Returns False for unrecognized labels (defaults to spinning).
    """
    if not email_label:
        return False
    m = _EMAIL_NUM_RE.search(email_label)
    if not m:
        return False
    try:
        return int(m.group(1)) > 1
    except (ValueError, IndexError):
        return False


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class BatchEmailJob:
    """One email body to spintax. Tracks per-body state + result."""

    section: str
    segment_idx: int  # index of parent segment in BatchState.segments
    email_idx: int  # index within the segment
    email_label: str
    subject_raw: str  # passed through verbatim, never spintaxed
    body_raw: str  # input to spintax engine
    parser_warnings: list[str] = field(default_factory=list)
    job_id: str | None = None  # current attempt
    retry_count: int = 0
    status: str = BODY_STATUS_QUEUED
    last_error: str | None = None
    spintax_body: str | None = None  # final output (None until done)
    lint_passed: bool = False
    qa_passed: bool = False
    qa_errors: list[str] = field(default_factory=list)
    qa_warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    elapsed_sec: float = 0.0


@dataclass
class BatchSegment:
    """One segment containing its emails."""

    section: str
    segment_name: str
    parser_warnings: list[str]
    emails: list[BatchEmailJob]


@dataclass
class BatchState:
    """Top-level state for one batch."""

    batch_id: str
    status: str
    platform: str
    model: str
    concurrency: int
    segments: list[BatchSegment]
    parse_warnings: list[str]
    created_at: datetime
    reasoning_effort: str = "high"
    pipeline: str | None = None  # None = use SPINTAX_PIPELINE env default
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_reason: str | None = None
    _started_monotonic: float = 0.0  # for elapsed calc

    @property
    def all_bodies(self) -> list[BatchEmailJob]:
        return [e for s in self.segments for e in s.emails]

    @property
    def total_bodies(self) -> int:
        return sum(len(s.emails) for s in self.segments)

    def counts(self) -> dict[str, int]:
        bodies = self.all_bodies
        return {
            "completed": sum(1 for b in bodies if b.status == BODY_STATUS_DONE),
            "failed": sum(1 for b in bodies if b.status == BODY_STATUS_FAILED),
            "in_progress": sum(1 for b in bodies if b.status == BODY_STATUS_RUNNING),
            "retrying": sum(1 for b in bodies if b.status == BODY_STATUS_RETRYING),
            "queued": sum(1 for b in bodies if b.status == BODY_STATUS_QUEUED),
        }

    def total_cost_usd(self) -> float:
        return sum(b.cost_usd for b in self.all_bodies)

    def total_retries(self) -> int:
        return sum(b.retry_count for b in self.all_bodies)

    def elapsed_sec(self) -> float:
        # Prefer wall-clock when both ends are known (works for completed
        # batches AND for tests that don't go through run_batch()).
        if self.started_at and self.completed_at:
            return max(0.0, (self.completed_at - self.started_at).total_seconds())
        # Live batch — fall back to the monotonic clock if running.
        if self._started_monotonic > 0:
            return max(0.0, time.monotonic() - self._started_monotonic)
        return 0.0


# ---------------------------------------------------------------------------
# In-memory batch store
# ---------------------------------------------------------------------------

_batches: dict[str, BatchState] = {}
_lock = threading.Lock()


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _is_expired(state: BatchState) -> bool:
    age = (_now_utc() - state.created_at).total_seconds()
    return age > BATCH_TTL_SECONDS


def _cleanup_expired() -> int:
    evicted = 0
    with _lock:
        for bid in [bid for bid, s in _batches.items() if _is_expired(s)]:
            del _batches[bid]
            evicted += 1
    return evicted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_batch(
    parsed: ParseResult,
    platform: str,
    model: str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    reasoning_effort: str = "high",
    pipeline: str | None = None,
) -> BatchState:
    """Create a new batch from a parsed markdown result.

    The batch is in PARSED state — call run_batch() to fire jobs.

    Args:
        parsed: ParseResult from app.parser.parse_markdown()
        platform: 'instantly' or 'emailbison'
        model: OpenAI model name. Defaults to settings.default_model.
        concurrency: bounded async semaphore size (1..MAX_CONCURRENCY)
        reasoning_effort: 'low' | 'medium' | 'high'. Forwarded to the
            runner for o-series and gpt-5.x models. Defaults to 'high'
            because the cleanup phase has stacked constraints (register,
            domain noun lock, structural variation, length band, Jaccard
            floor) that medium effort often fails to satisfy in one shot.

    Returns:
        The newly created BatchState (also stored in the in-memory map).

    Raises:
        ValueError: invalid concurrency, empty segments, etc.
    """
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be in [1, {MAX_CONCURRENCY}], got {concurrency}")
    if reasoning_effort not in ("low", "medium", "high"):
        raise ValueError(
            f"reasoning_effort must be one of low/medium/high, got {reasoning_effort!r}"
        )
    if pipeline is not None and pipeline not in ("alpha", "beta_v1"):
        raise ValueError(
            f"pipeline must be 'alpha', 'beta_v1', or None, got {pipeline!r}"
        )
    if not parsed.segments:
        raise ValueError("parsed result has no segments")
    if model is None:
        model = settings.default_model

    batch_id = f"bat_{_now_utc().strftime('%Y-%m-%d')}_{uuid.uuid4().hex[:8]}"

    segments: list[BatchSegment] = []
    for s_idx, seg in enumerate(parsed.segments):
        emails: list[BatchEmailJob] = []
        for e_idx, em in enumerate(seg.emails):
            emails.append(
                BatchEmailJob(
                    section=seg.section,
                    segment_idx=s_idx,
                    email_idx=e_idx,
                    email_label=em.email_label,
                    subject_raw=em.subject_raw,
                    body_raw=em.body_raw,
                    parser_warnings=list(seg.warnings),
                )
            )
        segments.append(
            BatchSegment(
                section=seg.section,
                segment_name=seg.segment_name,
                parser_warnings=list(seg.warnings),
                emails=emails,
            )
        )

    state = BatchState(
        batch_id=batch_id,
        status=BATCH_STATUS_PARSED,
        platform=platform,
        model=model,
        concurrency=concurrency,
        segments=segments,
        parse_warnings=list(parsed.warnings),
        created_at=_now_utc(),
        reasoning_effort=reasoning_effort,
        pipeline=pipeline,
    )
    with _lock:
        _batches[batch_id] = state
    return state


def get_batch(batch_id: str) -> BatchState | None:
    """Return BatchState by ID, or None if not found or expired."""
    with _lock:
        state = _batches.get(batch_id)
        if state is None:
            return None
        if _is_expired(state):
            del _batches[batch_id]
            return None
        return state


def list_batches(limit: int = 50) -> list[BatchState]:
    """Return most-recent batches first, evicting expired entries."""
    with _lock:
        for bid in [bid for bid, s in _batches.items() if _is_expired(s)]:
            del _batches[bid]
        return sorted(_batches.values(), key=lambda s: s.created_at, reverse=True)[:limit]


def cancel_batch(batch_id: str) -> bool:
    """Mark batch as cancelled. Returns True if found, False otherwise.

    In-flight bodies finish naturally (we don't kill running OpenAI calls).
    Queued bodies skip immediately.
    """
    with _lock:
        state = _batches.get(batch_id)
        if state is None:
            return False
        if state.status in (BATCH_STATUS_DONE, BATCH_STATUS_FAILED, BATCH_STATUS_CANCELLED):
            return False
        state.status = BATCH_STATUS_CANCELLED
        return True


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def run_batch(batch_id: str) -> None:
    """Fire all queued bodies in the batch with bounded concurrency.

    Mutates the BatchState in-place. Never raises externally — all
    failures are recorded on individual bodies or as
    `state.failure_reason`. Designed to be fired via
    `asyncio.create_task()` from an HTTP route.

    State transitions:
        parsed -> running -> done | cancelled
        on hard failure (quota / cap) -> failed
    """
    state = get_batch(batch_id)
    if state is None:
        logger.warning("run_batch: batch %s not found", batch_id)
        return

    state.status = BATCH_STATUS_RUNNING
    state.started_at = _now_utc()
    state._started_monotonic = time.monotonic()

    # Lazy import to avoid circular dependency at module load.
    from app import pipeline_dispatch

    # Resolve which runner this batch should use. Done once per batch so
    # all bodies in the batch use the same pipeline (no mid-batch swap).
    _, batch_runner = pipeline_dispatch.resolve_pipeline(state.pipeline)

    sem = asyncio.Semaphore(state.concurrency)

    async def _run_one(body: BatchEmailJob) -> None:
        # Skip empty bodies — not a failure, just nothing to do.
        if not body.body_raw or not body.body_raw.strip():
            body.status = BODY_STATUS_DONE
            body.spintax_body = ""
            body.last_error = "empty_body_skipped"
            return

        # Strategist convention: only Email 1 gets spintaxed.
        # Email 2+ are hand-written follow-ups; pass through verbatim
        # so they appear in the output .md without any OpenAI call.
        if _should_skip_spintax(body.email_label):
            body.status = BODY_STATUS_DONE
            body.spintax_body = body.body_raw
            body.lint_passed = True
            body.qa_passed = True
            body.last_error = None
            body.elapsed_sec = 0.0
            body.cost_usd = 0.0
            return

        async with sem:
            for attempt in range(MAX_RETRIES + 1):
                if state.status == BATCH_STATUS_CANCELLED:
                    body.status = (
                        BODY_STATUS_QUEUED if body.status == BODY_STATUS_QUEUED else body.status
                    )
                    return

                # Daily cap pre-check (cheap, just reads spend counter).
                try:
                    spend.check_cap()
                except Exception:  # noqa: BLE001 - HTTPException from spend.check_cap
                    body.status = BODY_STATUS_FAILED
                    body.last_error = "daily_spend_cap_exceeded"
                    state.failure_reason = "daily_spend_cap_exceeded"
                    state.status = BATCH_STATUS_FAILED
                    return

                # Create a fresh job for each attempt — the existing job
                # store doesn't support resetting a failed job's status.
                job = jobs.create(body.body_raw, state.platform, state.model)
                body.job_id = job.job_id
                body.retry_count = attempt
                body.status = BODY_STATUS_RUNNING

                t0 = time.monotonic()
                try:
                    await batch_runner(
                        job_id=job.job_id,
                        plain_body=body.body_raw,
                        platform=state.platform,
                        model=state.model,
                        reasoning_effort=state.reasoning_effort,
                    )
                except Exception:  # noqa: BLE001
                    # spintax_runner.run() never re-raises by contract,
                    # but if it ever did, treat as a transient error.
                    logger.exception(
                        "batch %s body %s/%s: runner raised unexpectedly",
                        batch_id,
                        body.segment_idx,
                        body.email_idx,
                    )

                # Read final job state.
                final = jobs.get(job.job_id)
                if final is None:
                    body.last_error = "job_lost"
                elif final.status == "done":
                    res = final.result  # SpintaxJobResult dataclass
                    body.spintax_body = getattr(res, "spintax_body", "")
                    body.lint_passed = bool(getattr(res, "lint_passed", False))
                    body.qa_passed = bool(getattr(res, "qa_passed", False))
                    body.qa_errors = list(getattr(res, "qa_errors", []))
                    body.qa_warnings = list(getattr(res, "qa_warnings", []))
                    body.cost_usd = float(final.cost_usd)
                    body.elapsed_sec = time.monotonic() - t0
                    body.status = BODY_STATUS_DONE
                    return
                else:
                    body.last_error = final.error or "unknown"
                    body.cost_usd += float(final.cost_usd)

                # Hard-fail errors stop the whole batch.
                if body.last_error in HARD_FAIL_ERRORS:
                    body.status = BODY_STATUS_FAILED
                    state.failure_reason = body.last_error
                    state.status = BATCH_STATUS_FAILED
                    return

                # Retry if budget remains.
                if attempt < MAX_RETRIES:
                    body.status = BODY_STATUS_RETRYING
                    backoff = RETRY_BACKOFF_SEC[attempt]
                    logger.info(
                        "batch %s body %s/%s retry %s after %.0fs (err=%s)",
                        batch_id,
                        body.segment_idx,
                        body.email_idx,
                        attempt + 1,
                        backoff,
                        body.last_error,
                    )
                    await asyncio.sleep(backoff)
                else:
                    body.status = BODY_STATUS_FAILED
                    return

    tasks = [asyncio.create_task(_run_one(b)) for b in state.all_bodies]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        state.completed_at = _now_utc()
        # Don't override CANCELLED or FAILED states.
        if state.status == BATCH_STATUS_RUNNING:
            state.status = BATCH_STATUS_DONE


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _reset_for_test() -> None:
    """Clear in-memory state. Tests only."""
    with _lock:
        _batches.clear()
