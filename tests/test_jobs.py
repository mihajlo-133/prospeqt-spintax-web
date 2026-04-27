"""Unit tests for app/jobs.py — the in-memory job store.

Phase 2 target:
    This test file is written BEFORE the implementation (test-first).
    When Phase 2 starts, all tests here should fail for the right reason:
    - NotImplementedError on create/update/get/list (current skeleton bodies)

    After Phase 2 implementation, ALL tests must pass.

Test surfaces:
    - create() returns a Job with uuid4 job_id and correct initial state
    - get() retrieves created jobs; returns None for unknown IDs
    - update() mutates state fields and deltas
    - list() returns all active jobs
    - Concurrent updates: 100 threads each calling update() on the same job
    - TTL expiry: jobs older than 1h are removed; jobs younger than 1h remain
    - update() raises KeyError for unknown job_id (not None, not silent)

No real OpenAI calls. No real I/O. Pure in-memory.
"""

import threading
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

import app.jobs as jobs_module
from app.jobs import Job, create, update, get, list as list_jobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(text: str = "Hello world.", platform: str = "instantly", model: str = "o3") -> Job:
    return create(text, platform, model)


# ---------------------------------------------------------------------------
# A. create()
# ---------------------------------------------------------------------------

class TestCreate:
    def test_create_returns_job_instance(self):
        """create() must return a Job object."""
        job = _make_job()
        assert isinstance(job, Job)

    def test_create_returns_uuid4_job_id(self):
        """job_id must be a valid uuid4 string."""
        job = _make_job()
        # Must parse as UUID without error
        parsed = uuid.UUID(job.job_id, version=4)
        assert str(parsed) == job.job_id

    def test_create_initial_status_is_queued(self):
        """Newly created job must have status='queued'."""
        job = _make_job()
        assert job.status == "queued"

    def test_create_stores_input_text(self):
        """create() must store the input_text verbatim."""
        text = "Unique test body for create."
        job = create(text, "instantly", "o3")
        assert job.input_text == text

    def test_create_stores_platform(self):
        """create() must store the platform."""
        job = create("body.", "emailbison", "o3")
        assert job.platform == "emailbison"

    def test_create_stores_model(self):
        """create() must store the model."""
        job = create("body.", "instantly", "o4-mini")
        assert job.model == "o4-mini"

    def test_create_initial_cost_is_zero(self):
        """cost_usd must start at 0.0."""
        job = _make_job()
        assert job.cost_usd == 0.0

    def test_create_initial_tool_calls_is_zero(self):
        """tool_calls must start at 0."""
        job = _make_job()
        assert job.tool_calls == 0

    def test_create_initial_result_is_none(self):
        """result must start as None."""
        job = _make_job()
        assert job.result is None

    def test_create_initial_error_is_none(self):
        """error must start as None."""
        job = _make_job()
        assert job.error is None

    def test_create_sets_created_at(self):
        """created_at must be a timezone-aware UTC datetime."""
        job = _make_job()
        assert isinstance(job.created_at, datetime)
        assert job.created_at.tzinfo is not None

    def test_create_two_jobs_have_different_ids(self):
        """Two separate create() calls must produce different job_ids."""
        job_a = _make_job()
        job_b = _make_job()
        assert job_a.job_id != job_b.job_id


# ---------------------------------------------------------------------------
# B. get()
# ---------------------------------------------------------------------------

class TestGet:
    def test_get_returns_created_job(self):
        """get(job_id) must return the same job that create() returned."""
        job = _make_job()
        retrieved = get(job.job_id)
        assert retrieved is not None
        assert retrieved.job_id == job.job_id

    def test_get_unknown_returns_none(self):
        """get() with an unknown UUID must return None, not raise."""
        result = get("00000000-0000-0000-0000-000000000000")
        assert result is None

    def test_get_returns_job_with_correct_status(self):
        """get() after create() returns a job with status='queued'."""
        job = _make_job()
        retrieved = get(job.job_id)
        assert retrieved.status == "queued"


# ---------------------------------------------------------------------------
# C. update()
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_update_status_reflects_in_get(self):
        """update(job_id, status='drafting') must change status visible via get()."""
        job = _make_job()
        update(job.job_id, status="drafting")
        retrieved = get(job.job_id)
        assert retrieved.status == "drafting"

    def test_update_sets_result(self):
        """update(job_id, result='body') must store the result."""
        job = _make_job()
        update(job.job_id, status="done", result="Final spintax body here.")
        retrieved = get(job.job_id)
        assert retrieved.result == "Final spintax body here."
        assert retrieved.status == "done"

    def test_update_sets_error(self):
        """update(job_id, error='timeout') must store the error."""
        job = _make_job()
        update(job.job_id, status="failed", error="timeout")
        retrieved = get(job.job_id)
        assert retrieved.error == "timeout"
        assert retrieved.status == "failed"

    def test_update_cost_usd_delta_accumulates(self):
        """cost_usd_delta is additive: two calls of 0.05 each => 0.10 total."""
        job = _make_job()
        update(job.job_id, cost_usd_delta=0.05)
        update(job.job_id, cost_usd_delta=0.05)
        retrieved = get(job.job_id)
        assert abs(retrieved.cost_usd - 0.10) < 1e-9

    def test_update_tool_calls_delta_accumulates(self):
        """tool_calls_delta is additive: two calls of 1 each => 2 total."""
        job = _make_job()
        update(job.job_id, tool_calls_delta=1)
        update(job.job_id, tool_calls_delta=1)
        retrieved = get(job.job_id)
        assert retrieved.tool_calls == 2

    def test_update_sets_updated_at(self):
        """update() must refresh updated_at."""
        job = _make_job()
        original_updated_at = job.updated_at
        update(job.job_id, status="drafting")
        retrieved = get(job.job_id)
        assert retrieved.updated_at >= original_updated_at

    def test_update_unknown_raises_key_error(self):
        """update() with an unknown job_id must raise KeyError."""
        with pytest.raises(KeyError):
            update("00000000-0000-0000-0000-000000000000", status="drafting")

    def test_update_partial_fields_preserved(self):
        """update(status=...) must not clear unrelated fields."""
        job = create("body.", "instantly", "o3")
        update(job.job_id, cost_usd_delta=0.03)
        update(job.job_id, status="linting")  # no cost_usd_delta here
        retrieved = get(job.job_id)
        assert retrieved.status == "linting"
        assert abs(retrieved.cost_usd - 0.03) < 1e-9  # not reset to 0


# ---------------------------------------------------------------------------
# D. list()
# ---------------------------------------------------------------------------

class TestList:
    def test_list_returns_list_type(self):
        """list_jobs() must return a list."""
        result = list_jobs()
        assert isinstance(result, list)

    def test_list_includes_created_jobs(self):
        """Newly created jobs appear in list_jobs()."""
        job = _make_job()
        result = list_jobs()
        ids = [j.job_id for j in result]
        assert job.job_id in ids

    def test_list_default_limit_returns_at_most_50(self):
        """Default limit is 50; list must not exceed it."""
        result = list_jobs()
        assert len(result) <= 50

    def test_list_returns_job_objects(self):
        """All items returned by list_jobs() must be Job instances."""
        _make_job()
        result = list_jobs()
        for j in result:
            assert isinstance(j, Job)


# ---------------------------------------------------------------------------
# E. Concurrent updates — thread safety
# ---------------------------------------------------------------------------

class TestConcurrentUpdates:
    def test_concurrent_updates_no_race_no_exception(self):
        """100 threads each calling update() on the same job must not raise
        any exception and must complete without data corruption."""
        job = create("Concurrent test body.", "instantly", "o3")
        errors = []

        def do_update():
            try:
                update(job.job_id, cost_usd_delta=0.001, tool_calls_delta=1)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_update) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent update raised exceptions: {errors}"

        # Verify all 100 updates landed — no lost writes
        retrieved = get(job.job_id)
        assert retrieved.tool_calls == 100
        assert abs(retrieved.cost_usd - 0.1) < 1e-6  # 100 * 0.001

    def test_concurrent_creates_all_distinct_ids(self):
        """100 concurrent create() calls must produce 100 distinct job_ids."""
        results = []
        errors = []

        def do_create():
            try:
                j = create("Concurrent create body.", "instantly", "o3")
                results.append(j.job_id)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_create) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(set(results)) == 100, "Duplicate job_ids produced under concurrency"


# ---------------------------------------------------------------------------
# F. TTL expiry
# ---------------------------------------------------------------------------

class TestTTL:
    def test_ttl_keeps_recent_job(self):
        """A job created 30 minutes ago must still be visible in list/get."""
        job = create("Recent job.", "instantly", "o3")
        # Simulate job created 30 minutes ago
        thirty_min_ago = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
        # Directly mutate the stored record's created_at (white-box, required for TTL testing)
        # The implementation must expose the internal dict OR accept a backdated timestamp.
        # Here we patch via the module-level _jobs dict (standard pattern for TTL unit tests).
        if hasattr(jobs_module, "_jobs"):
            jobs_module._jobs[job.job_id].created_at = thirty_min_ago
        # Trigger cleanup (if the module exposes it)
        if hasattr(jobs_module, "_cleanup_expired"):
            jobs_module._cleanup_expired()
        result = get(job.job_id)
        assert result is not None, "Recent job (30 min old) must not be expired"

    def test_ttl_removes_old_job(self):
        """A job created 2 hours ago must be gone after cleanup."""
        job = create("Old job.", "instantly", "o3")
        two_hours_ago = datetime.now(tz=timezone.utc) - timedelta(hours=2)
        if hasattr(jobs_module, "_jobs"):
            jobs_module._jobs[job.job_id].created_at = two_hours_ago
        if hasattr(jobs_module, "_cleanup_expired"):
            jobs_module._cleanup_expired()
            result = get(job.job_id)
            assert result is None, "2-hour-old job must be expired and removed"
        else:
            pytest.skip("_cleanup_expired not exposed; TTL test requires it")
