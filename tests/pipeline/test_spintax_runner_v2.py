"""Unit tests for app/spintax_runner_v2.py — the beta block-first runner.

These tests mock at the ``run_pipeline`` boundary rather than at each
LLM stage. End-to-end stage wiring is already covered by
``tests/pipeline/test_pipeline_integration.py``. The job here is to
prove the v2 runner's job-state plumbing, error mapping, and result
shape match what alpha guarantees.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import openai
import pytest

from app.pipeline.contracts import (
    AssembledSpintax,
    BlockSpintaxerDiagnostics,
    ERR_BLOCK_SPINTAX,
    ERR_PROFILER,
    ERR_SPLITTER,
    ERR_SYNONYM_POOL,
    PipelineDiagnostics,
    PipelineStageError,
    ProfilerDiagnostics,
    SplitterDiagnostics,
    SynonymPoolDiagnostics,
)


def _reset_jobs():
    import app.jobs as jobs_mod

    importlib.reload(jobs_mod)
    return jobs_mod


def _reset_spend():
    import app.spend as spend_mod

    importlib.reload(spend_mod)
    return spend_mod


def _make_diagnostics() -> PipelineDiagnostics:
    return PipelineDiagnostics(
        pipeline="beta_v1",
        splitter=SplitterDiagnostics(
            duration_ms=120, block_count=3, lockable_count=2
        ),
        profiler=ProfilerDiagnostics(
            duration_ms=110,
            tone="warm consultative",
            locked_nouns=["practice"],
            proper_nouns=["Acme"],
        ),
        synonym_pool=SynonymPoolDiagnostics(
            duration_ms=140, total_synonyms=18, blocks_covered=2
        ),
        block_spintaxer=BlockSpintaxerDiagnostics(
            blocks_completed=2,
            blocks_retried=0,
            max_retries_per_block=0,
            p95_block_duration_ms=2300,
        ),
    )


_QA_PASS = {
    "passed": True,
    "errors": [],
    "warnings": [],
    "diversity_block_scores": [None, 0.42, 0.55],
    "diversity_corpus_avg": 0.485,
    "diversity_floor_block_avg": 0.30,
    "diversity_floor_pair": 0.20,
    "diversity_gate_level": "warning",
}

_LINT_PASS: tuple[list[str], list[str]] = ([], [])


@pytest.fixture
def fresh_state():
    """Reset jobs + spend before each test so state from prior tests can't leak."""
    jobs_mod = _reset_jobs()
    spend_mod = _reset_spend()
    return jobs_mod, spend_mod


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    async def test_job_reaches_done_with_diagnostics(self, fresh_state):
        jobs_mod, _ = fresh_state
        from app import spintax_runner_v2

        assembled = AssembledSpintax(spintax="{Hi|Hello} there.")
        diagnostics = _make_diagnostics()

        job = jobs_mod.create("Hi there.", "instantly", "gpt-5")

        with (
            patch(
                "app.pipeline.pipeline_runner.run_pipeline",
                new=AsyncMock(return_value=(assembled, diagnostics)),
            ),
            patch("app.qa.qa", return_value=_QA_PASS),
            patch("app.lint.lint", return_value=_LINT_PASS),
        ):
            await spintax_runner_v2.run(
                job_id=job.job_id,
                plain_body="Hi there.",
                platform="instantly",
                model="gpt-5",
            )

        final = jobs_mod.get(job.job_id)
        assert final is not None
        assert final.status == "done"
        assert final.error is None
        assert final.result is not None
        assert final.result.spintax_body == "{Hi|Hello} there."
        assert final.result.lint_passed is True
        assert final.result.qa_passed is True
        assert final.result.qa_diversity_corpus_avg == pytest.approx(0.485)
        assert getattr(final.result, "pipeline_diagnostics", None) is diagnostics

    async def test_progress_phases_advance(self, fresh_state):
        jobs_mod, _ = fresh_state
        from app import spintax_runner_v2

        assembled = AssembledSpintax(spintax="{Hi|Hello}.")
        diagnostics = _make_diagnostics()
        job = jobs_mod.create("Hi.", "instantly", "gpt-5")

        with (
            patch(
                "app.pipeline.pipeline_runner.run_pipeline",
                new=AsyncMock(return_value=(assembled, diagnostics)),
            ),
            patch("app.qa.qa", return_value=_QA_PASS),
            patch("app.lint.lint", return_value=_LINT_PASS),
        ):
            await spintax_runner_v2.run(
                job_id=job.job_id,
                plain_body="Hi.",
                platform="instantly",
                model="gpt-5",
            )

        final = jobs_mod.get(job.job_id)
        # Final progress is published in the qa phase, immediately before done.
        assert final.progress is not None
        assert final.progress["phase"] == "qa"

    async def test_on_api_call_accumulates_cost(self, fresh_state):
        """The on_api_call hook the runner passes into run_pipeline should
        increment job.cost_usd and api_calls. We simulate by invoking the
        captured callback inside the mocked run_pipeline."""
        jobs_mod, _ = fresh_state
        from app import spintax_runner_v2

        assembled = AssembledSpintax(spintax="{Hi|Hello}.")
        diagnostics = _make_diagnostics()
        job = jobs_mod.create("Hi.", "instantly", "gpt-5")

        captured_cb: dict[str, Any] = {}

        async def _fake_run_pipeline(plain_body, **kwargs):
            captured_cb["cb"] = kwargs.get("on_api_call")
            # Fire two synthetic usages with input/output token shape.
            cb = kwargs["on_api_call"]
            for _ in range(2):
                usage = type(
                    "U",
                    (),
                    {
                        "input_tokens": 1_000_000,  # 1M tokens for big-number testing
                        "output_tokens": 0,
                        "output_tokens_details": None,
                        "completion_tokens_details": None,
                    },
                )()
                cb(usage)
            return assembled, diagnostics

        with (
            patch(
                "app.pipeline.pipeline_runner.run_pipeline",
                new=_fake_run_pipeline,
            ),
            patch("app.qa.qa", return_value=_QA_PASS),
            patch("app.lint.lint", return_value=_LINT_PASS),
        ):
            await spintax_runner_v2.run(
                job_id=job.job_id,
                plain_body="Hi.",
                platform="instantly",
                model="gpt-5",
            )

        final = jobs_mod.get(job.job_id)
        assert final.api_calls == 2
        # Two calls of 1M input tokens each. gpt-5 input price is non-zero
        # in alpha's pricing table - we assert >0 rather than an exact USD
        # so this test stays robust to pricing-table tweaks.
        assert final.cost_usd > 0.0
        assert final.result.api_calls == 2
        assert final.result.cost_usd == pytest.approx(final.cost_usd)


# ---------------------------------------------------------------------------
# Validation: empty body fails fast
# ---------------------------------------------------------------------------


class TestEmptyBody:
    async def test_empty_body_fails_with_malformed(self, fresh_state):
        jobs_mod, _ = fresh_state
        from app import spintax_runner_v2
        from app.jobs import ERR_MALFORMED

        job = jobs_mod.create("", "instantly", "gpt-5")

        with patch(
            "app.pipeline.pipeline_runner.run_pipeline",
            new=AsyncMock(),
        ) as mock_pipe:
            await spintax_runner_v2.run(
                job_id=job.job_id,
                plain_body="",
                platform="instantly",
                model="gpt-5",
            )
            mock_pipe.assert_not_called()

        final = jobs_mod.get(job.job_id)
        assert final.status == "failed"
        assert final.error == ERR_MALFORMED


# ---------------------------------------------------------------------------
# Pipeline stage errors map to canonical job error keys
# ---------------------------------------------------------------------------


class TestPipelineStageErrorMapping:
    @pytest.mark.parametrize(
        "stage_err",
        [ERR_SPLITTER, ERR_PROFILER, ERR_SYNONYM_POOL, ERR_BLOCK_SPINTAX],
    )
    async def test_pipeline_stage_error_propagates_key(
        self, fresh_state, stage_err
    ):
        jobs_mod, _ = fresh_state
        from app import spintax_runner_v2

        job = jobs_mod.create("Hi there.", "instantly", "gpt-5")

        async def _raise(*_args, **_kwargs):
            raise PipelineStageError(stage_err, detail="forced for test")

        with patch(
            "app.pipeline.pipeline_runner.run_pipeline", new=_raise
        ):
            await spintax_runner_v2.run(
                job_id=job.job_id,
                plain_body="Hi there.",
                platform="instantly",
                model="gpt-5",
            )

        final = jobs_mod.get(job.job_id)
        assert final.status == "failed"
        assert final.error == stage_err
        assert "forced for test" in (final.error_detail or "")


# ---------------------------------------------------------------------------
# Upstream API errors map the same way alpha maps them
# ---------------------------------------------------------------------------


class TestUpstreamErrorMapping:
    async def test_rate_limit_maps_to_quota(self, fresh_state):
        jobs_mod, _ = fresh_state
        from app import spintax_runner_v2
        from app.jobs import ERR_QUOTA

        job = jobs_mod.create("Hi.", "instantly", "gpt-5")

        async def _raise(*_a, **_k):
            raise openai.RateLimitError(
                "rate limited",
                response=MagicMock(status_code=429),
                body={"error": {"type": "insufficient_quota"}},
            )

        with patch(
            "app.pipeline.pipeline_runner.run_pipeline", new=_raise
        ):
            await spintax_runner_v2.run(
                job_id=job.job_id,
                plain_body="Hi.",
                platform="instantly",
                model="gpt-5",
            )

        final = jobs_mod.get(job.job_id)
        assert final.status == "failed"
        assert final.error == ERR_QUOTA

    async def test_unexpected_error_maps_to_unknown(self, fresh_state):
        jobs_mod, _ = fresh_state
        from app import spintax_runner_v2
        from app.jobs import ERR_UNKNOWN

        job = jobs_mod.create("Hi.", "instantly", "gpt-5")

        async def _raise(*_a, **_k):
            raise RuntimeError("unexpected explosion")

        with patch(
            "app.pipeline.pipeline_runner.run_pipeline", new=_raise
        ):
            await spintax_runner_v2.run(
                job_id=job.job_id,
                plain_body="Hi.",
                platform="instantly",
                model="gpt-5",
            )

        final = jobs_mod.get(job.job_id)
        assert final.status == "failed"
        assert final.error == ERR_UNKNOWN
        assert "RuntimeError" in (final.error_detail or "")

    async def test_anthropic_low_balance_maps_to_low_balance(self, fresh_state):
        jobs_mod, _ = fresh_state
        from app import spintax_runner_v2
        from app.jobs import ERR_LOW_BALANCE

        job = jobs_mod.create("Hi.", "instantly", "claude-opus-4")

        async def _raise(*_a, **_k):
            raise anthropic.BadRequestError(
                "Your credit balance is too low to access the API",
                response=MagicMock(status_code=400),
                body={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Your credit balance is too low to access the API",
                    }
                },
            )

        with patch(
            "app.pipeline.pipeline_runner.run_pipeline", new=_raise
        ):
            await spintax_runner_v2.run(
                job_id=job.job_id,
                plain_body="Hi.",
                platform="instantly",
                model="claude-opus-4",
            )

        final = jobs_mod.get(job.job_id)
        assert final.status == "failed"
        assert final.error == ERR_LOW_BALANCE


# ---------------------------------------------------------------------------
# /api/status converter surfaces pipeline_diagnostics
# ---------------------------------------------------------------------------


class TestStatusConverter:
    def test_pipeline_diagnostics_round_trips_to_pydantic(self):
        """The route's _convert_pipeline_diagnostics must accept the
        contracts.PipelineDiagnostics shape and emit the api_models embed."""
        from app.routes.spintax import _convert_pipeline_diagnostics

        raw = _make_diagnostics()
        converted = _convert_pipeline_diagnostics(raw)
        assert converted is not None
        assert converted.pipeline == "beta_v1"
        assert converted.splitter.block_count == 3
        assert converted.splitter.lockable_count == 2
        assert converted.profiler.tone == "warm consultative"
        assert converted.synonym_pool.total_synonyms == 18
        assert converted.block_spintaxer.blocks_completed == 2

    def test_none_diagnostics_returns_none(self):
        from app.routes.spintax import _convert_pipeline_diagnostics

        assert _convert_pipeline_diagnostics(None) is None

    def test_convert_result_includes_pipeline_diagnostics_when_attached(
        self, fresh_state
    ):
        """When the v2 runner attaches pipeline_diagnostics on the dataclass
        via setattr(), the routes converter must pick it up and emit it on
        the pydantic SpintaxJobResult."""
        from app.jobs import SpintaxJobResult as RawResult
        from app.routes.spintax import _convert_result

        raw = RawResult(
            spintax_body="{Hi|Hello}.",
            lint_errors=[],
            lint_warnings=[],
            lint_passed=True,
            qa_errors=[],
            qa_warnings=[],
            qa_passed=True,
            tool_calls=0,
            api_calls=2,
            cost_usd=0.04,
        )
        setattr(raw, "pipeline_diagnostics", _make_diagnostics())

        converted = _convert_result(raw)
        assert converted is not None
        assert converted.pipeline_diagnostics is not None
        assert converted.pipeline_diagnostics.pipeline == "beta_v1"
        assert converted.pipeline_diagnostics.splitter.block_count == 3

    def test_convert_result_pipeline_diagnostics_is_none_when_missing(
        self, fresh_state
    ):
        """Alpha jobs never set pipeline_diagnostics. The converter must
        leave the field as None rather than blowing up on the missing attr."""
        from app.jobs import SpintaxJobResult as RawResult
        from app.routes.spintax import _convert_result

        raw = RawResult(
            spintax_body="{Hi|Hello}.",
            lint_errors=[],
            lint_warnings=[],
            lint_passed=True,
            qa_errors=[],
            qa_warnings=[],
            qa_passed=True,
            tool_calls=0,
            api_calls=2,
            cost_usd=0.04,
        )

        converted = _convert_result(raw)
        assert converted is not None
        assert converted.pipeline_diagnostics is None
