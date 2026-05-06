"""Tests for Stage 2: Email Tone Profiler (app.pipeline.profiler).

All tests mock app.pipeline.profiler.call_llm_json so no real API calls are made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.pipeline.contracts import ERR_PROFILER, Profile, ProfilerDiagnostics
from app.pipeline.profiler import _detect_proper_nouns, profile_email
from app.pipeline.contracts import PipelineStageError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_LLM_RESPONSE = {
    "tone": "professional B2B, consultative",
    "audience_hint": "law firms",
    "locked_common_nouns": ["clients", "matters"],
    "proper_nouns_added": [],
}


def _make_mock(response: dict) -> AsyncMock:
    """Return an AsyncMock that resolves to *response* when awaited."""
    return AsyncMock(return_value=response)


# ---------------------------------------------------------------------------
# Test 1: happy path — returns Profile and ProfilerDiagnostics
# ---------------------------------------------------------------------------


async def test_happy_path_returns_profile_and_diagnostics():
    """profile_email returns a Profile + ProfilerDiagnostics on valid input."""
    body = "We help law firms manage their clients and matters efficiently."

    with patch(
        "app.pipeline.profiler.call_llm_json",
        _make_mock(_VALID_LLM_RESPONSE),
    ):
        profile, diag = await profile_email(body)

    assert isinstance(profile, Profile)
    assert isinstance(diag, ProfilerDiagnostics)
    assert profile.tone == "professional B2B, consultative"
    assert profile.audience_hint == "law firms"
    assert "clients" in profile.locked_common_nouns
    assert "matters" in profile.locked_common_nouns
    assert diag.duration_ms >= 0


# ---------------------------------------------------------------------------
# Test 2: locked_common_nouns are lowercased and deduplicated
# ---------------------------------------------------------------------------


async def test_locked_nouns_lowercased_and_deduped():
    """LLM returning mixed-case or duplicate locked nouns is normalised."""
    body = "We serve Clients and Clients and CLIENTS."
    llm_response = {
        "tone": "casual",
        "audience_hint": None,
        "locked_common_nouns": ["Clients", "clients", "CLIENTS"],
        "proper_nouns_added": [],
    }

    with patch(
        "app.pipeline.profiler.call_llm_json",
        _make_mock(llm_response),
    ):
        profile, _ = await profile_email(body)

    assert profile.locked_common_nouns == ["clients"]


# ---------------------------------------------------------------------------
# Test 3: proper_nouns union — regex first, then LLM additions
# ---------------------------------------------------------------------------


async def test_proper_nouns_union_regex_then_llm():
    """Regex-detected proper nouns come first; LLM additions are appended."""
    body = "Acme Corp is our partner. Please contact ZenHire for details."
    llm_response = {
        "tone": "professional",
        "audience_hint": None,
        "locked_common_nouns": [],
        "proper_nouns_added": ["SomeExtra"],
    }

    with patch(
        "app.pipeline.profiler.call_llm_json",
        _make_mock(llm_response),
    ):
        profile, _ = await profile_email(body)

    nouns = profile.proper_nouns
    # Regex should have found Acme Corp and/or ZenHire
    assert any("Acme" in n for n in nouns)
    # LLM addition should be present
    assert "SomeExtra" in nouns
    # LLM addition must come after regex nouns
    some_extra_idx = nouns.index("SomeExtra")
    for n in nouns:
        if n != "SomeExtra" and n in ("Acme Corp", "ZenHire"):
            assert nouns.index(n) < some_extra_idx


# ---------------------------------------------------------------------------
# Test 4: empty body raises PipelineStageError with ERR_PROFILER
# ---------------------------------------------------------------------------


async def test_empty_body_raises_pipeline_stage_error():
    """An empty or whitespace-only body must raise PipelineStageError."""
    with pytest.raises(PipelineStageError) as exc_info:
        await profile_email("   ")

    assert exc_info.value.error_key == ERR_PROFILER


# ---------------------------------------------------------------------------
# Test 5: LLM call failure propagates as PipelineStageError
# ---------------------------------------------------------------------------


async def test_llm_failure_propagates_as_pipeline_stage_error():
    """If call_llm_json raises PipelineStageError, it is re-raised unchanged."""
    body = "Hello, this is an email body."
    error = PipelineStageError(ERR_PROFILER, detail="LLM timed out")

    async def _raise(*_, **__):
        raise error

    with patch("app.pipeline.profiler.call_llm_json", _raise):
        with pytest.raises(PipelineStageError) as exc_info:
            await profile_email(body)

    assert exc_info.value is error


# ---------------------------------------------------------------------------
# Test 6: LLM response missing 'tone' raises PipelineStageError
# ---------------------------------------------------------------------------


async def test_missing_tone_raises_pipeline_stage_error():
    """A LLM response without a 'tone' field raises PipelineStageError."""
    body = "We serve healthcare patients daily."
    bad_response = {
        # 'tone' intentionally absent
        "audience_hint": None,
        "locked_common_nouns": ["patients"],
        "proper_nouns_added": [],
    }

    with patch(
        "app.pipeline.profiler.call_llm_json",
        _make_mock(bad_response),
    ):
        with pytest.raises(PipelineStageError) as exc_info:
            await profile_email(body)

    assert exc_info.value.error_key == ERR_PROFILER


# ---------------------------------------------------------------------------
# Test 7: regex pre-pass skips sentence-initial words
# ---------------------------------------------------------------------------


def test_regex_skips_sentence_initial_words():
    """Words at the start of a sentence are not returned as proper nouns."""
    # "We" starts the only sentence — should not appear in results
    body = "We help companies grow faster."
    result = _detect_proper_nouns(body)
    assert "We" not in result


# ---------------------------------------------------------------------------
# Test 8: regex pre-pass skips placeholders
# ---------------------------------------------------------------------------


def test_regex_skips_placeholders():
    """{{Placeholders}} are stripped and must not appear in proper noun output."""
    body = "Hello {{FirstName}}, welcome to {{CompanyName}}."
    result = _detect_proper_nouns(body)
    # Neither placeholder nor its interior text should be a match
    for item in result:
        assert "{{" not in item
        assert "}}" not in item
        assert "FirstName" not in item
        assert "CompanyName" not in item
