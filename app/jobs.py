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
from datetime import datetime, timezone
from typing import Literal

JobStatus = Literal["queued", "drafting", "linting", "iterating", "qa", "done", "failed"]

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
    # Diagnostics dataclasses (added 2026-05-05). Reuse if the previous
    # module had them; otherwise fall through to fresh-define below so a
    # reload from a pre-V2 module doesn't leave them undefined.
    _DiversitySubCallRecord_prev = getattr(_prev_module, "DiversitySubCallRecord", None)
    _DiversityRevertRecord_prev = getattr(_prev_module, "DiversityRevertRecord", None)
    _DiversityRetryDiagnostics_prev = getattr(_prev_module, "DiversityRetryDiagnostics", None)
    if _DiversitySubCallRecord_prev is not None:
        DiversitySubCallRecord = _DiversitySubCallRecord_prev  # type: ignore[misc]
    if _DiversityRevertRecord_prev is not None:
        DiversityRevertRecord = _DiversityRevertRecord_prev  # type: ignore[misc]
    if _DiversityRetryDiagnostics_prev is not None:
        DiversityRetryDiagnostics = _DiversityRetryDiagnostics_prev  # type: ignore[misc]
    # V3 Workstream 1 diagnostics (added 2026-05-06). Same pattern.
    _JaccardSubCallRecord_prev = getattr(_prev_module, "JaccardSubCallRecord", None)
    _JaccardCleanupDiagnostics_prev = getattr(_prev_module, "JaccardCleanupDiagnostics", None)
    if _JaccardSubCallRecord_prev is not None:
        JaccardSubCallRecord = _JaccardSubCallRecord_prev  # type: ignore[misc]
    if _JaccardCleanupDiagnostics_prev is not None:
        JaccardCleanupDiagnostics = _JaccardCleanupDiagnostics_prev  # type: ignore[misc]
    _lock: threading.Lock = _prev_module._lock  # type: ignore[attr-defined]
    _jobs: dict[str, "Job"] = {}
else:
    _lock = threading.Lock()
    _jobs: dict[str, "Job"] = {}

    @dataclass
    class DiversitySubCallRecord:  # noqa: F811
        """Per-sub-call record inside DiversityRetryDiagnostics.

        outcome is one of:
            "success"             - JSON parsed, replacement spliced
            "json_parse_error"    - model returned malformed/missing JSON
            "api_error"           - upstream OpenAI/Anthropic error
            "skipped_short_block" - block had < 5 variants; not retried
        """

        block_idx: int  # 0-indexed
        outcome: str
        cost_usd: float
        strategies: list[str] = field(default_factory=list)
        error_msg: str | None = None

    @dataclass
    class DiversityRevertRecord:  # noqa: F811
        """One per-block revert event inside DiversityRetryDiagnostics."""

        block_idx: int  # 0-indexed
        pre_score: float
        post_score: float
        reason: str  # "regression" | "splice_corruption"

    @dataclass
    class DiversityRetryDiagnostics:  # noqa: F811
        """Structured record of what V2 per-block diversity retry did.

        Captured during the V2 orchestration in spintax_runner.py and
        attached to SpintaxJobResult so the operator can inspect retry
        behavior without combing through logs. Always present on a `done`
        result; `fired=False` + `skipped_reason` covers the no-retry path.

        skipped_reason values:
            "warning_level"      - DIVERSITY_GATE_LEVEL != "error"; no retry by design
            "no_failing_blocks"  - errors exist but none diversity-related
            "budget"             - cost cap would be exceeded
            "no_successful_subcalls" - all sub-calls failed; pre-retry body shipped
            "reassemble_failed"  - splice raised; pre-retry body shipped
            "splice_corrupted"   - revert detected unintended mutations; pre-retry body shipped
        """

        fired: bool = False
        skipped_reason: str | None = None
        failing_blocks: list[int] = field(default_factory=list)
        pre_retry_block_scores: list[float | None] = field(default_factory=list)
        post_retry_block_scores: list[float | None] = field(default_factory=list)
        sub_calls: list[DiversitySubCallRecord] = field(default_factory=list)
        reverted_blocks: list[DiversityRevertRecord] = field(default_factory=list)
        splice_corrupted: bool = False
        retry_cost_usd: float = 0.0

    @dataclass
    class JaccardSubCallRecord:  # noqa: F811
        """Per-sub-call record inside JaccardCleanupDiagnostics.

        outcome is one of:
            "improved"            - splice raised the block above pair-floor
            "no_improvement"      - parsed OK but didn't clear the floor
            "json_parse_error"    - model returned malformed JSON
            "api_error"           - upstream OpenAI/Anthropic error
            "skipped_short_block" - block had <5 variants; not retried
            "length_band_violation" - all variants outside length band; rejected
        """

        block_idx: int  # 0-indexed
        attempt_num: int  # 1-indexed within MAX_JACCARD_REPROMPTS_PER_BLOCK
        outcome: str
        cost_usd: float
        pre_score: float
        post_score: float | None  # None when sub-call failed before scoring
        error_msg: str | None = None

    @dataclass
    class JaccardCleanupDiagnostics:  # noqa: F811
        """Structured record of what V3 per-block Jaccard cleanup did.

        Sits between drift_retry exit and V2 retry start. fired=True means
        at least one block had a Jaccard violation we attempted to clean
        up. blocks_at_cap surfaces blocks that exhausted their per-block
        budget without resolving - V2 picks them up downstream.

        skipped_reason values:
            "no_failing_blocks"   - drift_retry shipped clean (default path)
            "all_at_cap"          - every failing block already at MAX retries
            "no_successful_subcalls" - tried, all sub-calls failed
        """

        fired: bool = False
        skipped_reason: str | None = None
        blocks_attempted: list[int] = field(default_factory=list)
        sub_calls: list[JaccardSubCallRecord] = field(default_factory=list)
        blocks_at_cap: list[int] = field(default_factory=list)
        cleanup_cost_usd: float = 0.0
        pre_cleanup_block_scores: list[float | None] = field(default_factory=list)
        post_cleanup_block_scores: list[float | None] = field(default_factory=list)

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
        # Phase 4 split: separate lint retries from agent-tool exploration
        # for cost observability. tool_calls = lint_calls + agent_tool_calls
        # (kept for backwards compat with callers that haven't migrated).
        lint_calls: int = 0
        agent_tool_calls: int = 0
        # Per-agent-tool invocation count (e.g. {"wordhippo_lookup": 2,
        # "get_pre_approved_synonyms": 5}). Empty dict when no agent tool
        # was called. Surfaces in benchmark JSON for cost-per-tool tracking.
        agent_tool_breakdown: dict[str, int] = field(default_factory=dict)
        api_calls: int = 0
        cost_usd: float = 0.0
        # Number of drift-revision passes the runner triggered (0..MAX).
        # 0 = clean on first try; > 0 = model had to revise. Surfaces in
        # the UI so we can see when a model is hallucinating context.
        drift_revisions: int = 0
        # Drift warnings that REMAINED after all revision attempts.
        # Empty when drift was resolved or never detected.
        drift_unresolved: list[str] = field(default_factory=list)
        # Phase A diversity gate (added 2026-05-04). See DIVERSITY_GATE_SPEC.md.
        qa_diversity_block_scores: list[float | None] = field(default_factory=list)
        qa_diversity_corpus_avg: float | None = None
        qa_diversity_floor_block_avg: float | None = None
        qa_diversity_floor_pair: float | None = None
        qa_diversity_gate_level: str | None = None
        diversity_retries: int = 0
        # V2 retry diagnostic record (added 2026-05-05). Captures the
        # per-block retry trajectory: pre/post scores, sub-call outcomes,
        # strategies chosen, reverted blocks. Always present (with
        # fired=False) when DIVERSITY_GATE_LEVEL=="warning". See
        # DIVERSITY_GATE_SPEC.md Section 4.7 for the V2 design.
        diversity_retry_diagnostics: "DiversityRetryDiagnostics | None" = None
        # V3 Workstream 1 cleanup record (added 2026-05-06). Captures the
        # per-block Jaccard cleanup that runs between drift_retry exit and
        # V2 retry start. Always present (with fired=False) on jobs where
        # drift_retry shipped clean. See V3_DRIFT_JACCARD_AND_V2_RETRY_SPEC.md.
        jaccard_cleanup_diagnostics: "JaccardCleanupDiagnostics | None" = None

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
        error_detail: str | None = (
            None  # human-readable provider message (e.g. "credit balance is too low")
        )
        # Live progress payload. Updated as the runner moves through phases.
        # Shape: {"phase": str, "label": str, ...phase-specific fields}.
        # Surfaced via /api/status so the UI / poller can show meaningful
        # state instead of just the coarse JobStatus literal. None until
        # the runner publishes its first progress update.
        progress: dict | None = None


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
    progress: dict | None = None,
) -> Job:
    """Update an existing job. Raises KeyError if job_id not found.

    Thread-safe. Sets updated_at to utcnow().
    Deltas are added to existing accumulated values.
    Does NOT evict expired jobs - caller sees stale jobs if they exist.

    `progress` REPLACES the existing progress dict (no merge). Pass an
    empty dict to clear; pass None (the default) to leave unchanged.
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
        if progress is not None:
            job.progress = progress
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
