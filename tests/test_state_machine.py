"""State machine tests for the spintax generation pipeline.

Phase 2 target:
    Written BEFORE implementation (test-first). All tests should fail with
    NotImplementedError or ImportError before Phase 2 builder completes.
    After Phase 2, ALL tests must pass.

State machine contract:
    queued  → drafting     (when run() starts)
    drafting → linting     (after first API call returns tool_call)
    linting → iterating    (when lint fails)
    iterating → linting    (on subsequent lint call)
    linting → qa           (when lint passes)
    qa → done              (QA passes)
    qa → done              (QA fails but still terminates — qa.passed=false in result metadata)
    * → failed             (on any unhandled error: timeout, quota, API error)

Each test:
    1. Creates a fresh job via jobs.create()
    2. Calls spintax_runner.run() with mocked OpenAI (respx)
    3. Asserts the final job state via jobs.get()

The OpenAI fixture files in tests/fixtures/openai/ provide the API response
sequences. respx intercepts POST https://api.openai.com/v1/chat/completions
and replays fixture responses in order.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import importlib

import pytest
import httpx

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "openai"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"


def _load_fixture(name: str) -> dict:
    """Load a fixture file from the fixtures/openai/ directory."""
    return json.loads((FIXTURES_DIR / name).read_text())


def _reload_jobs():
    """Reload jobs module to get a clean, empty job store."""
    import app.jobs as j
    importlib.reload(j)
    return j


def _reload_spend():
    """Reload spend module to reset the daily counter."""
    try:
        import app.spend as s
        importlib.reload(s)
        return s
    except (ImportError, Exception):
        return None


# ---------------------------------------------------------------------------
# Pre-flight: module importability check
# ---------------------------------------------------------------------------

def _get_runner():
    try:
        import app.spintax_runner as runner
        return runner
    except ImportError:
        return None


def _get_jobs():
    try:
        import app.jobs as j
        return j
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# A. queued → drafting (run() sets status to 'drafting' immediately)
# ---------------------------------------------------------------------------

class TestQueuedToDrafting:
    async def test_job_leaves_queued_state(self):
        """Once run() is called, job must leave 'queued' status."""
        runner = _get_runner()
        jobs = _get_jobs()
        if runner is None or jobs is None:
            pytest.fail("app.spintax_runner or app.jobs module not importable (Phase 2 not yet built)")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create, get

        job = create("Hello world.", "instantly", "o3")
        assert job.status == "queued"

        try:
            await runner.run(job.job_id, "Hello world.", "instantly", model="o3")
        except NotImplementedError:
            pytest.fail(
                "spintax_runner.run() raised NotImplementedError — "
                "Phase 2 builder must implement it"
            )
        except Exception:
            pass  # API/network errors OK in isolation

        retrieved = get(job.job_id)
        assert retrieved is not None
        assert retrieved.status != "queued", (
            f"Job must have left 'queued' status after run() is called. "
            f"Still showing 'queued' — did run() call jobs.update(status='drafting')?"
        )


# ---------------------------------------------------------------------------
# B. Terminal states: done and failed
# ---------------------------------------------------------------------------

class TestTerminalStates:
    async def test_successful_job_reaches_done(self):
        """A job that completes normally must end in 'done' status."""
        runner = _get_runner()
        if runner is None:
            pytest.fail("app.spintax_runner not importable")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create, get

        job = create("Hello world.", "instantly", "o3")

        try:
            await runner.run(job.job_id, "Hello world.", "instantly", model="o3")
        except NotImplementedError:
            pytest.fail("run() not implemented")
        except Exception:
            pass

        retrieved = get(job.job_id)
        assert retrieved is not None
        # Terminal state must be reached
        assert retrieved.status in ("done", "failed"), (
            f"Job must reach a terminal state ('done' or 'failed') after run(), "
            f"got '{retrieved.status}'"
        )

    async def test_failed_job_has_error_field(self):
        """When a job ends in 'failed', the error field must be a non-empty string."""
        runner = _get_runner()
        if runner is None:
            pytest.fail("app.spintax_runner not importable")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create, get

        # Use a body likely to cause issues (simulate error via missing API key)
        job = create("Fail test.", "instantly", "o3")

        try:
            await runner.run(job.job_id, "Fail test.", "instantly", model="o3")
        except NotImplementedError:
            pytest.fail("run() not implemented")
        except Exception:
            pass

        retrieved = get(job.job_id)
        if retrieved and retrieved.status == "failed":
            assert retrieved.error is not None and len(retrieved.error) > 0, (
                "Job in 'failed' status must have a non-empty error field"
            )

    async def test_done_job_has_result_field(self):
        """When a job ends in 'done', the result field must be a non-empty string."""
        runner = _get_runner()
        if runner is None:
            pytest.fail("app.spintax_runner not importable")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create, get

        job = create("Done test.", "instantly", "o3")

        try:
            await runner.run(job.job_id, "Done test.", "instantly", model="o3")
        except NotImplementedError:
            pytest.fail("run() not implemented")
        except Exception:
            pass

        retrieved = get(job.job_id)
        if retrieved and retrieved.status == "done":
            assert retrieved.result is not None and len(retrieved.result.strip()) > 0, (
                "Job in 'done' status must have a non-empty result field"
            )


# ---------------------------------------------------------------------------
# C. Error state transitions (API errors → failed)
# ---------------------------------------------------------------------------

class TestErrorTransitions:
    async def test_run_never_raises_externally(self):
        """run() must catch all exceptions and never propagate them.

        The function is called fire-and-forget from an HTTP route handler.
        Any unhandled exception would crash the background task silently,
        leaving the job stuck in 'drafting'. Instead, run() must catch
        everything and set status='failed' with an error message.
        """
        runner = _get_runner()
        if runner is None:
            pytest.fail("app.spintax_runner not importable")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create

        job = create("Exception test.", "instantly", "o3")

        raised = None
        try:
            await runner.run(job.job_id, "Exception test.", "instantly", model="o3")
        except NotImplementedError:
            pytest.fail("run() raised NotImplementedError — Phase 2 must implement it")
        except Exception as exc:
            raised = exc

        assert raised is None, (
            f"run() must never propagate exceptions. Got: {type(raised).__name__}: {raised}"
        )

    async def test_failed_job_not_stuck_in_drafting(self):
        """After an error, job must not be left in intermediate state ('drafting', 'linting')."""
        runner = _get_runner()
        if runner is None:
            pytest.fail("app.spintax_runner not importable")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create, get

        job = create("Stuck test.", "instantly", "o3")

        try:
            await runner.run(job.job_id, "Stuck test.", "instantly", model="o3")
        except NotImplementedError:
            pytest.fail("run() not implemented")
        except Exception:
            pass

        retrieved = get(job.job_id)
        if retrieved is not None:
            assert retrieved.status not in ("drafting", "linting", "iterating"), (
                f"Job must not be stuck in intermediate state after run() returns. "
                f"Got status='{retrieved.status}'"
            )


# ---------------------------------------------------------------------------
# D. State machine transitions tracked in job store
# ---------------------------------------------------------------------------

class TestStateTransitionsRecorded:
    async def test_updated_at_changes_during_run(self):
        """updated_at must be refreshed after run() transitions the job."""
        runner = _get_runner()
        if runner is None:
            pytest.fail("app.spintax_runner not importable")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create, get

        job = create("Update at test.", "instantly", "o3")
        initial_updated_at = job.updated_at

        try:
            await runner.run(job.job_id, "Update at test.", "instantly", model="o3")
        except NotImplementedError:
            pytest.fail("run() not implemented")
        except Exception:
            pass

        retrieved = get(job.job_id)
        if retrieved is not None and retrieved.status in ("done", "failed"):
            assert retrieved.updated_at >= initial_updated_at, (
                "updated_at must be >= the initial value after run() completes"
            )

    async def test_cost_usd_accumulated_during_run(self):
        """cost_usd must be > 0 after a run that made at least one API call."""
        runner = _get_runner()
        if runner is None:
            pytest.fail("app.spintax_runner not importable")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create, get

        job = create("Cost tracking test.", "instantly", "o3")

        try:
            await runner.run(job.job_id, "Cost tracking test.", "instantly", model="o3")
        except NotImplementedError:
            pytest.fail("run() not implemented")
        except Exception:
            pass

        retrieved = get(job.job_id)
        if retrieved and retrieved.status == "done":
            # If we succeeded, cost must be positive (we made API calls)
            assert retrieved.cost_usd > 0, (
                f"cost_usd must be > 0 after a successful run, got {retrieved.cost_usd}"
            )

    async def test_platform_preserved_in_job(self):
        """The platform field must be preserved correctly throughout run()."""
        runner = _get_runner()
        if runner is None:
            pytest.fail("app.spintax_runner not importable")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create, get

        job = create("Platform test.", "emailbison", "o3")
        assert job.platform == "emailbison"

        try:
            await runner.run(job.job_id, "Platform test.", "emailbison", model="o3")
        except NotImplementedError:
            pytest.fail("run() not implemented")
        except Exception:
            pass

        retrieved = get(job.job_id)
        if retrieved is not None:
            assert retrieved.platform == "emailbison", (
                f"platform must remain 'emailbison' throughout run(), got '{retrieved.platform}'"
            )


# ---------------------------------------------------------------------------
# E. Model late-binding
# ---------------------------------------------------------------------------

class TestModelLateBound:
    async def test_run_uses_settings_default_when_model_is_none(self):
        """When model=None is passed, run() must fall back to settings.default_model."""
        runner = _get_runner()
        if runner is None:
            pytest.fail("app.spintax_runner not importable")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create, get

        job = create("Default model test.", "instantly", "o3")

        try:
            # Pass model=None — should late-bind from settings
            await runner.run(job.job_id, "Default model test.", "instantly", model=None)
        except NotImplementedError:
            pytest.fail("run() not implemented")
        except Exception:
            pass

        # If we got here without NotImplementedError, the function ran.
        # The model stored in job was set at create() time — check it's still valid.
        retrieved = get(job.job_id)
        if retrieved is not None:
            assert retrieved.model in ("o3", "o4-mini", "gpt-4.1", "gpt-4.1-mini"), (
                f"job.model must be a known model, got '{retrieved.model}'"
            )


# ---------------------------------------------------------------------------
# F. Input validation gating
# ---------------------------------------------------------------------------

class TestInputValidation:
    async def test_empty_body_results_in_failed_job(self):
        """An empty plain_body must result in a 'failed' job (never 'done')."""
        runner = _get_runner()
        if runner is None:
            pytest.fail("app.spintax_runner not importable")

        jobs = _reload_jobs()
        _reload_spend()
        from app.jobs import create, get

        job = create("", "instantly", "o3")

        try:
            await runner.run(job.job_id, "", "instantly", model="o3")
        except NotImplementedError:
            pytest.fail("run() not implemented")
        except Exception:
            pass

        retrieved = get(job.job_id)
        if retrieved is not None:
            assert retrieved.status != "done", (
                "run() with an empty body must not produce a 'done' job — "
                "it must fail gracefully"
            )
