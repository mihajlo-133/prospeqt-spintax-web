"""End-to-end integration test for the beta block-first pipeline.

Loads pre-recorded LLM responses from
``tests/pipeline/fixtures/recorded/`` and replays them through the real
splitter / profiler / synonym pool / block spintaxer / assembler /
pipeline_runner code path. No live API calls.

Each stage's ``call_llm_json`` is patched at its module-of-origin import
path. The stage functions themselves run for real, exercising prompt
construction, response validation, and the assembler's join logic.

The QA validator (``app.qa.qa``) is patched to always pass - we are
testing pipeline plumbing here, not validator quality. Validator quality
is exercised by the benchmark script (``scripts/benchmark.py``) and by
each validator's own unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.pipeline.contracts import AssembledSpintax, PipelineDiagnostics
from app.pipeline.pipeline_runner import run_pipeline


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "recorded"


def _load_fixture(name: str) -> dict:
    path = FIXTURES_DIR / name
    with path.open() as fh:
        return json.load(fh)


def _passing_qa_result(block_count: int) -> dict:
    """A QA result that reports passed=True for an arbitrary block count."""
    return {
        "passed": True,
        "error_count": 0,
        "warning_count": 0,
        "errors": [],
        "warnings": [],
        "block_count": block_count,
        "input_paragraph_count": block_count,
        "diversity_block_scores": [0.5] * block_count,
        "diversity_pair_distances": [[0.5, 0.5, 0.5, 0.5] for _ in range(block_count)],
        "diversity_corpus_avg": 0.5,
        "diversity_floor_block_avg": 0.30,
        "diversity_floor_pair": 0.20,
        "diversity_gate_level": "warning",
    }


def _make_spintaxer_router(block_responses: dict[str, dict]):
    """Build a side_effect callable that routes by block_id in the prompt.

    The block spintaxer fires N parallel calls into ``call_llm_json``.
    asyncio.gather does not guarantee ordering, so we cannot use a list
    side_effect. Instead, we inspect the ``prompt`` kwarg, find the
    block_id substring, and return the matching pre-recorded response.
    """

    async def _route(*args, **kwargs):
        prompt = kwargs.get("prompt") or (args[0] if args else "")
        # The spintaxer prompt contains a JSON block with the expected
        # block_id; look for the literal block_id string.
        for block_id, response in block_responses.items():
            # Each block_id is unique like "block_1", "block_2" — string
            # match is sufficient because the prompt mentions it in the
            # output-shape example.
            if f'"{block_id}"' in prompt:
                return response
        # Fallback: surface a clear error so the test fails informatively
        # rather than via a confusing AsyncMock side_effect mismatch.
        raise AssertionError(
            f"No fixture matched. Prompt did not contain any of "
            f"{list(block_responses.keys())}. First 200 chars of prompt: "
            f"{prompt[:200]!r}"
        )

    return _route


@pytest.mark.asyncio
async def test_three_block_simple_email_end_to_end():
    """Drives the full pipeline against a 3-block synthetic fixture."""
    fx = _load_fixture("three_block_simple_responses.json")
    plain_body = fx["_meta"]["plain_body"]

    splitter_mock = AsyncMock(return_value=fx["splitter"])
    profiler_mock = AsyncMock(return_value=fx["profiler"])
    pool_mock = AsyncMock(return_value=fx["synonym_pool"])
    spintaxer_mock = AsyncMock(side_effect=_make_spintaxer_router(fx["block_spintaxer"]))

    # Real qa() runs on real spintax strings; for a hand-crafted fixture
    # we cannot guarantee diversity floors, so we mock qa() to always
    # report passed=True. Validator quality is tested separately.
    qa_mock = MagicMock(return_value=_passing_qa_result(block_count=3))

    with patch(
        "app.pipeline.splitter.call_llm_json", splitter_mock
    ), patch(
        "app.pipeline.profiler.call_llm_json", profiler_mock
    ), patch(
        "app.pipeline.synonym_pool.call_llm_json", pool_mock
    ), patch(
        "app.pipeline.block_spintaxer.call_llm_json", spintaxer_mock
    ), patch("app.qa.qa", qa_mock):
        # lint_max_retries_per_block=0: this fixture's hand-crafted variants
        # are not length-matched to V1, so the per-block lint retry would
        # fire repeatedly. We're testing pipeline plumbing here, not lint
        # retry; the dedicated TestLintFeedbackRetry class exercises that.
        assembled, diag = await run_pipeline(
            plain_body, lint_max_retries_per_block=0
        )

    # ---- Output shape ----
    assert isinstance(assembled, AssembledSpintax)
    assert isinstance(diag, PipelineDiagnostics)

    # ---- Spintax content ----
    s = assembled.spintax
    # block_1 ("Hi {{firstName}}.") is unlockable: stripping the
    # placeholder leaves "Hi ." which is below MIN_SPINTAXABLE_CHARS=8.
    # The assembler emits it as V1 only, no spintax wrapping.
    assert "{{firstName}}" in s
    assert "Hi {{firstName}}." in s

    # block_2 lockable: variants must be wrapped in the instantly-platform
    # default format `{{RANDOM | V1 | V2 | ... }}` (default platform=instantly).
    assert "{{RANDOM | We help small firms grow their practice. |" in s
    assert "We support lean firms scale their practice." in s

    # block_3 lockable: variants must also be wrapped.
    assert "{{RANDOM | Talk soon. |" in s
    assert "Chat soon." in s
    assert "Speak soon." in s

    # ---- Diagnostics ----
    assert diag.pipeline == "beta_v1"
    assert diag.splitter.block_count == 3
    # block_1 unlockable, block_2 + block_3 lockable
    assert diag.splitter.lockable_count == 2
    assert diag.profiler.tone == "warm professional B2B"
    assert "firms" in diag.profiler.locked_nouns
    assert diag.synonym_pool.blocks_covered == 2
    assert diag.block_spintaxer.blocks_completed == 3
    assert diag.block_spintaxer.blocks_retried == 0  # qa passed first try

    # ---- Stage call counts ----
    splitter_mock.assert_awaited_once()
    profiler_mock.assert_awaited_once()
    pool_mock.assert_awaited_once()
    # Spintaxer call_llm_json fires only for lockable blocks; block_1 is
    # an unlockable pure-passthrough that returns [text]*5 without an
    # LLM call. So 2 calls, not 3.
    assert spintaxer_mock.await_count == 2
    # qa() called once because it passed first try
    assert qa_mock.call_count == 1
