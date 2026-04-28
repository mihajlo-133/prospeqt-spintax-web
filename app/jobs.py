"""In-memory job store with a swappable interface.

What this does:
    Provides the four-function interface (create / update / get / list)
    that the rest of the app uses to track spintax generation jobs. The
    storage backend is intentionally hidden behind these functions so that
    it can be swapped (e.g., to Redis) in a future phase by changing only
    this module.

What it depends on:
    Python stdlib only (dataclasses, datetime, threading, typing, uuid).

What depends on it:
    - app/spintax_runner.py calls update() on each state transition
    - app/routes/spintax.py calls create() / get()

Phase 2:
    Real in-memory implementation. dict + threading.Lock + TTL sweep on
    access. The Job dataclass holds all per-job fields; SpintaxJobResult
    holds the structured result attached when status == "done".

Concurrency:
    EVERY read or write to _jobs goes through _lock. This is mandatory.
    The lock is a threading.Lock (not RLock) - no recursive acquisition.

TTL strategy:
    Sweep on access. get() and list() evict expired jobs inline.
    No background task. TTL = 1 hour from created_at.

Swap contract:
    The signatures of create(), update(), get(), and list() must stay stable.
    Callers must never reach into the storage backend directly - always go
    through this module's public API.
"""

import sys as _sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

JobStatus = Literal[
    "queued", "drafting", "linting", "iterating", "qa", "done", "failed"
]

# Error key constants (machine-readable, asserted in tests).
ERR_TIMEOUT = "openai_timeout"
ERR_QUOTA = "openai_quota"
ERR_MAX_TOOL_CALLS = "max_tool_calls"
ERR_MALFORMED = "malformed_response"
ERR_UNKNOWN = "internal_error"
# Added 2026-04-28 - surface previously-opaque failures (Anthropic credit
# depletion, bad API key, model name typos, malformed request shape).
ERR_AUTH = "auth_failed"
ERR_LOW_BALANCE = "low_balance"
ERR_BAD_REQUEST = "bad_request"
ERR_MODEL_NOT_FOUND = "model_not_found"

# TTL for in-memory job retention.
TTL_SECONDS = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Reload-safety: detect importlib.reload(app.jobs) and reuse the existing
# Job / SpintaxJobResult class objects so that isinstance() checks against
# pre-reload imports continue to hold. Tests in test_failure_modes.py and
# test_state_machine.py call importlib.reload(jobs) to clear state - without
# this guard, the Job dataclass identity changes on every reload and
# test_jobs.py:49/230 fail with `isinstance(job, Job) == False`.
# ---------------------------------------------------------------------------

_prev_module = _sys.modules.get(__name__)
_is_reload = (
    _prev_module is not None
    and hasattr(_prev_module, "Job")
    and hasattr(_prev_module, "SpintaxJobResult")
    and hasattr(_prev_module, "_lock")
)

if _is_reload:
    # Reuse class identities and the lock; reset _jobs dict (the test's
    # signal that it wants clean state).
    Job = _prev_module.Job  # type: ignore[misc]
    SpintaxJobResult = _prev_module.SpintaxJobResult  # type: ignore[misc]
    _lock: threading.Lock = _prev_module._lock  # type: ignore[attr-defined]
    _jobs: dict[str, "Job"] = {}
else:
    _lock = threading.Lock()
    _jobs: dict[str, "Job"] = {}

    @dataclass
    class SpintaxJobResult:  # noqa: F811 - defined only on first load
        """Structured result attached to a Job when status == 'done'.

        This is a plain dataclass, not a pydantic model, so the runner can
        construct it without a pydantic dependency at the storage layer.
        The HTTP route that returns job status converts this to the pydantic
        SpintaxJobResult model defined in app.api_models.
        """

        spintax_body: str
        lint_errors: list[str] = field(default_factory=list)
        lint_warnings: list[str] = field(default_factory=list)
        lint_passed: bool = True
        qa_errors: list[str] = field(default_factory=list)
        qa_warnings: list[str] = field(default_factory=list)
        qa_passed: bool = True
        tool_calls: int = 0
        api_calls: int = 0
        cost_usd: float = 0.0
        # Number of drift-revision passes the runner triggered (0..MAX).
        # 0 = clean on first try; > 0 = model had to revise. Surfaces in
        # the UI so we can see when a model is hallucinating context.
        drift_revisions: int = 0
        # Drift warnings that REMAINED after all revision attempts.
        # Empty when drift was resolved or never detected.
        drift_unresolved: list[str] = field(default_factory=list)

    @dataclass
    class Job:  # noqa: F811 - defined only on first load
        """A single spintax generation job, tracked from creation through
        a terminal state ('done' or 'failed')."""

        job_id: str
        status: JobStatus
        created_at: datetime
        updated_at: datetime
        input_text: str
        platform: str  # "instantly" | "emailbison"
        model: str  # OpenAI model name
        result: object | None  # str or SpintaxJobResult, set when status == "done"
        error: str | None  # machine-readable error key when status == "failed"
        cost_usd: float  # accumulated cost across all API calls
        tool_calls: int  # accumulated lint tool calls
        api_calls: int = 0  # accumulated OpenAI API round-trips
        started_at: float = 0.0  # time.monotonic() at creation
        error_detail: str | None = None  # human-readable provider message (e.g. "credit balance is too low")


def _now_utc() -> datetime:
    """Single time source. Shimmed for tests in some scenarios."""
    return datetime.now(tz=timezone.utc)


def _is_expired(job: Job) -> bool:
    """True if the job's created_at is older than TTL_SECONDS ago."""
    return (_now_utc() - job.created_at).total_seconds() > TTL_SECONDS


def _cleanup_expired() -> int:
    """Evict all expired jobs. Returns the number evicted.

    Public for tests. Holds _lock during the sweep.
    """
    evicted = 0
    with _lock:
        expired_ids = [jid for jid, j in _jobs.items() if _is_expired(j)]
        for jid in expired_ids:
            del _jobs[jid]
            evicted += 1
    return evicted


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create(
    input_text: str,
    platform: str,
    model: str,
) -> Job:
    """Create a new job in 'queued' state. Returns the Job.

    Thread-safe. Generates a UUID4 job_id internally. The caller does
    NOT pass a job_id - the contract is that this function owns it.
    """
    now = _now_utc()
    job_id = str(uuid.uuid4())
    job = Job(
        job_id=job_id,
        status="queued",
        created_at=now,
        updated_at=now,
        input_text=input_text,
        platform=platform,
        model=model,
        result=None,
        error=None,
        cost_usd=0.0,
        tool_calls=0,
        api_calls=0,
        started_at=time.monotonic(),
        error_detail=None,
    )
    with _lock:
        _jobs[job_id] = job
    return job


def update(
    job_id: str,
    status: JobStatus | None = None,
    result: object | None = None,
    error: str | None = None,
    cost_usd_delta: float = 0.0,
    tool_calls_delta: int = 0,
    api_calls_delta: int = 0,
    error_detail: str | None = None,
) -> Job:
    """Update an existing job. Raises KeyError if job_id not found.

    Thread-safe. Sets updated_at to utcnow().
    Deltas are added to existing accumulated values.
    Does NOT evict expired jobs - caller sees stale jobs if they exist.
    """
    with _lock:
        if job_id not in _jobs:
            raise KeyError(job_id)
        job = _jobs[job_id]
        if status is not None:
            job.status = status
        if result is not None:
            job.result = result
        if error is not None:
            job.error = error
        if error_detail is not None:
            job.error_detail = error_detail
        if cost_usd_delta:
            job.cost_usd += cost_usd_delta
        if tool_calls_delta:
            job.tool_calls += tool_calls_delta
        if api_calls_delta:
            job.api_calls += api_calls_delta
        job.updated_at = _now_utc()
        return job


def get(job_id: str) -> Job | None:
    """Return Job by ID, or None if not found or expired.

    Thread-safe. Evicts the job inline if expired before returning None.
    """
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        if _is_expired(job):
            del _jobs[job_id]
            return None
        return job


def list(limit: int = 50) -> list[Job]:  # noqa: A001 - public API name fixed by tests
    """Return most-recent jobs first, up to limit. Sweeps expired entries.

    Thread-safe. Full sweep on every call - acceptable at MVP scale.
    """
    with _lock:
        # Sweep expired first
        expired_ids = [jid for jid, j in _jobs.items() if _is_expired(j)]
        for jid in expired_ids:
            del _jobs[jid]
        # Sort by created_at descending (most recent first)
        all_jobs = sorted(_jobs.values(), key=lambda j: j.created_at, reverse=True)
        return all_jobs[:limit]
