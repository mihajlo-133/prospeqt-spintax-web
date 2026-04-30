"""Failure mode tests - 7 scenarios that exercise error handling across the full stack.

Phase 2 target:
    Written BEFORE implementation (test-first). Failures before Phase 2 builder
    completes are expected. After Phase 2, ALL tests must pass.

Test scenarios:
    1. Empty text → 422 (pydantic validates, rejects before route handler)
    2. OpenAI timeout → job state=failed, error="openai_timeout"
    3. OpenAI quota (429) → job state=failed, error="openai_quota"
    4. Daily cap hit → 429 with exact envelope {error, cap_usd, spent_usd, resets_at}
    5. Auth missing on /api/spintax → 401
    6. Lint pass but QA fail → job done, qa.passed=False, output rendered
    7. Job not found on GET /api/status/{bad_id} → 404

All tests use TestClient (sync HTTP interface over ASGI).
No real OpenAI calls. No real network I/O.
"""

import importlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import httpx

os.environ.setdefault("ADMIN_PASSWORD", "test-password-sentinel")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-000-sentinel")
os.environ.setdefault("OPENAI_MODEL", "o3")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Function-scoped unauthenticated TestClient."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def authed_client():
    """Function-scoped TestClient pre-logged-in."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/admin/login", json={"password": "test-password-sentinel"})
        # Login may 404 if /admin/login not yet implemented - that's OK pre-Phase2
        yield c


# ---------------------------------------------------------------------------
# 1. Empty text → 422
# ---------------------------------------------------------------------------


class TestEmptyTextRejected:
    def test_empty_text_returns_422(self, authed_client):
        """POST /api/spintax with empty text must return 422.
        Pydantic validates before route handler fires.
        """
        r = authed_client.post(
            "/api/spintax",
            json={"text": "", "platform": "instantly"},
        )
        assert r.status_code in (401, 422), (
            f"Empty text must return 422 (or 401 if /api/spintax not yet implemented). "
            f"Got {r.status_code}. Body: {r.text}"
        )
        # If route exists and auth works, 422 is required
        if r.status_code == 422:
            body = r.json()
            assert "detail" in body, f"422 must include 'detail' in body. Got: {body}"

    def test_whitespace_only_text_returns_422(self, authed_client):
        """POST /api/spintax with whitespace-only text must return 422."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": "   \n  ", "platform": "instantly"},
        )
        assert r.status_code in (401, 422), (
            f"Whitespace-only text must return 422. Got {r.status_code}"
        )

    def test_invalid_platform_returns_422(self, authed_client):
        """POST /api/spintax with invalid platform must return 422."""
        r = authed_client.post(
            "/api/spintax",
            json={"text": "Hello world.", "platform": "invalid_platform"},
        )
        assert r.status_code in (401, 422), f"Invalid platform must return 422. Got {r.status_code}"


# ---------------------------------------------------------------------------
# 2. OpenAI timeout → job state=failed, error="openai_timeout"
# ---------------------------------------------------------------------------


class TestOpenAITimeout:
    def test_timeout_sets_job_to_failed(self, authed_client):
        """When OpenAI times out, the job must eventually reach failed state
        with error='openai_timeout'.

        We patch the runner's _make_openai_client() so that any chat
        completion call raises httpx.TimeoutException - the architect's
        spec maps that exception to ERR_TIMEOUT ('openai_timeout').
        """
        import time
        from unittest.mock import patch, MagicMock

        async def _raise_timeout(**kwargs):
            raise httpx.TimeoutException("simulated timeout")

        mock_client = MagicMock()
        mock_client.chat.completions.create = _raise_timeout

        with patch(
            "app.spintax_runner._make_openai_client",
            return_value=mock_client,
        ):
            r = authed_client.post(
                "/api/spintax",
                json={"text": "Hello world.", "platform": "instantly"},
            )

            if r.status_code in (401, 404):
                pytest.fail(
                    f"POST /api/spintax must be implemented by Phase 2 builder. "
                    f"Got {r.status_code}. This test requires the route to exist."
                )

            assert r.status_code in (200, 202), (
                f"POST /api/spintax must return 200/202, got {r.status_code}. Body: {r.text}"
            )

            body = r.json()
            assert "job_id" in body, f"Response must include job_id. Got: {body}"
            job_id = body["job_id"]

            # Poll for terminal state. The asyncio task runs in the same
            # event loop the TestClient drives, so by the time poll #1
            # completes the task has typically already finished.
            final_status = None
            for _ in range(20):  # up to 2 seconds of polling
                status_r = authed_client.get(f"/api/status/{job_id}")
                if status_r.status_code == 200:
                    status_body = status_r.json()
                    s = status_body.get("status")
                    if s in ("done", "failed"):
                        final_status = status_body
                        break
                time.sleep(0.1)

        assert final_status is not None, "Job must reach terminal state within 2s"
        assert final_status["status"] == "failed", (
            f"Timeout scenario must result in failed job. Got: {final_status['status']}"
        )
        assert final_status.get("error") == "openai_timeout", (
            f"Expected error='openai_timeout', got {final_status.get('error')!r}"
        )


# ---------------------------------------------------------------------------
# 3. OpenAI quota/429 → job state=failed, error="openai_quota"
# ---------------------------------------------------------------------------


class TestOpenAIQuota:
    def test_quota_error_sets_job_to_failed(self):
        """When OpenAI returns 429 (quota), job must reach failed with error='openai_quota'.
        Tests directly at the runner level (not via HTTP route) for isolation.
        """
        try:
            from app import jobs
            from app import spintax_runner
            import openai
        except ImportError:
            pytest.fail("app.jobs or app.spintax_runner not importable (Phase 2 not built)")

        importlib.reload(jobs)
        job = jobs.create("Quota test.", "instantly", "o3")

        async def _mock_create(**kwargs):
            raise openai.RateLimitError(
                "You exceeded your current quota",
                response=MagicMock(status_code=429),
                body={"error": {"type": "insufficient_quota"}},
            )

        mock_client = MagicMock()
        mock_client.chat.completions.create = _mock_create

        import asyncio

        async def _run():
            with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
                with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                    await spintax_runner.run(
                        job_id=job.job_id,
                        plain_body="Quota test.",
                        platform="instantly",
                        model="o3",
                    )

        # asyncio.get_event_loop() is deprecated in 3.12+; use asyncio.run()
        # which creates a fresh event loop and tears it down cleanly.
        asyncio.run(_run())

        final = jobs.get(job.job_id)
        assert final is not None
        assert final.status == "failed", f"Quota error must set failed. Got: {final.status}"
        assert final.error == "openai_quota", f"Expected error='openai_quota', got {final.error!r}"


# ---------------------------------------------------------------------------
# 4. Daily cap hit → 429 with exact envelope
# ---------------------------------------------------------------------------


class TestDailyCapHit:
    def test_cap_hit_returns_429_with_envelope(self):
        """When daily cap is hit, POST /api/spintax must return 429 with exact shape."""
        try:
            import app.spend as spend
        except ImportError:
            pytest.fail("app.spend module must be created in Phase 2")

        importlib.reload(spend)
        spend._reset_for_test(50.0)  # set to cap

        from fastapi.testclient import TestClient
        from app.main import app

        with TestClient(app, raise_server_exceptions=False) as c:
            # Login
            c.post("/admin/login", json={"password": "test-password-sentinel"})

            r = c.post(
                "/api/spintax",
                json={"text": "Hello world.", "platform": "instantly"},
            )

        if r.status_code in (404,):
            pytest.fail("POST /api/spintax not yet implemented - Phase 2 builder must add it")

        assert r.status_code == 429, (
            f"Daily cap hit must return 429. Got {r.status_code}. Body: {r.text}"
        )

        body = r.json()
        required_keys = {"error", "cap_usd", "spent_usd", "resets_at"}
        missing = required_keys - set(body.keys())
        assert not missing, f"429 body missing keys: {missing}. Got: {body}"
        assert body["error"] == "daily_cap_hit", (
            f"Expected error='daily_cap_hit', got {body['error']!r}"
        )
        assert body["cap_usd"] == 50.0, f"Expected cap_usd=50.0, got {body['cap_usd']}"

    def test_cap_hit_429_shape_has_resets_at(self):
        """The 429 envelope's resets_at must be a future ISO 8601 string."""
        from datetime import datetime, timezone

        try:
            import app.spend as spend
        except ImportError:
            pytest.fail("app.spend module not found")

        importlib.reload(spend)
        spend._reset_for_test(55.0)

        from fastapi.testclient import TestClient
        from app.main import app

        with TestClient(app, raise_server_exceptions=False) as c:
            c.post("/admin/login", json={"password": "test-password-sentinel"})
            r = c.post(
                "/api/spintax",
                json={"text": "Hello world.", "platform": "instantly"},
            )

        if r.status_code == 404:
            pytest.fail("POST /api/spintax not yet implemented")

        if r.status_code == 429:
            body = r.json()
            resets_at_str = body.get("resets_at", "")
            parsed = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
            assert parsed > datetime.now(tz=timezone.utc), "resets_at must be in the future"


# ---------------------------------------------------------------------------
# 5. Auth missing on /api/spintax → 401
# ---------------------------------------------------------------------------


class TestAuthMissingReturns401:
    def test_no_cookie_on_spintax_route_returns_401(self, client):
        """POST /api/spintax without session cookie must return 401."""
        r = client.post(
            "/api/spintax",
            json={"text": "Hello world.", "platform": "instantly"},
        )
        assert r.status_code == 401, (
            f"Unauthenticated POST /api/spintax must return 401. "
            f"Got {r.status_code}. Body: {r.text}"
        )

    def test_no_cookie_on_status_route_returns_401(self, client):
        """GET /api/status/{job_id} without session cookie must return 401."""
        r = client.get("/api/status/00000000-0000-0000-0000-000000000000")
        assert r.status_code == 401, (
            f"Unauthenticated GET /api/status must return 401. Got {r.status_code}. Body: {r.text}"
        )

    def test_health_is_still_public(self, client):
        """GET /health must be public even in Phase 2 (regression guard)."""
        r = client.get("/health")
        assert r.status_code == 200, (
            f"GET /health must be public (no auth), got {r.status_code}. Body: {r.text}"
        )


# ---------------------------------------------------------------------------
# 6. Lint pass but QA fail → job done, qa.passed=False
# ---------------------------------------------------------------------------


class TestLintPassQAFail:
    def test_qa_fail_job_is_done_not_failed(self):
        """When lint passes but QA fails, job must end with status='done'
        (NOT 'failed') and result.qa_passed must be False.

        This verifies the architect's T8 decision:
        'qa fail -> done with qa.passed=False, NOT failed'
        """
        try:
            from app import jobs
            from app import spintax_runner
        except ImportError:
            pytest.fail("app modules not importable")

        importlib.reload(jobs)
        job = jobs.create("Test body.", "instantly", "o3")

        # Build fixture response: pass_first_try (lint passes on first call)
        fixture_path = Path(__file__).parent / "fixtures" / "openai" / "o3_pass_first_try.json"
        fixture = json.loads(fixture_path.read_text())
        turns = fixture["_sequence"]

        call_count = [0]

        def _make_mock_response(turn_data):
            choice_data = turn_data["choices"][0]
            msg_data = choice_data["message"]
            usage_data = turn_data.get("usage", {})

            msg = MagicMock()
            msg.content = msg_data.get("content")
            msg.tool_calls = None

            if msg_data.get("tool_calls"):
                tc_list = []
                for tc_data in msg_data["tool_calls"]:
                    tc = MagicMock()
                    tc.id = tc_data["id"]
                    tc.function = MagicMock()
                    tc.function.name = tc_data["function"]["name"]
                    tc.function.arguments = tc_data["function"]["arguments"]
                    tc_list.append(tc)
                msg.tool_calls = tc_list

            usage = MagicMock()
            usage.prompt_tokens = usage_data.get("prompt_tokens", 0)
            usage.completion_tokens = usage_data.get("completion_tokens", 0)
            details = MagicMock()
            details.reasoning_tokens = usage_data.get("completion_tokens_details", {}).get(
                "reasoning_tokens", 0
            )
            usage.completion_tokens_details = details

            choice = MagicMock()
            choice.message = msg
            response = MagicMock()
            response.choices = [choice]
            response.usage = usage
            return response

        async def _mock_create(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return _make_mock_response(turns[idx] if idx < len(turns) else turns[-1])

        mock_client = MagicMock()
        mock_client.chat.completions.create = _mock_create

        # QA will fail for this body
        qa_fail = {
            "passed": False,
            "errors": ["block_count mismatch: got 1, expected 3"],
            "warnings": [],
            "error_count": 1,
            "warning_count": 0,
            "block_count": 1,
            "input_paragraph_count": 3,
        }

        import asyncio

        async def _run():
            with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
                with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                    with patch("app.spintax_runner.qa", return_value=qa_fail):
                        await spintax_runner.run(
                            job_id=job.job_id,
                            plain_body="Test body.",
                            platform="instantly",
                            model="o3",
                        )

        # asyncio.get_event_loop() is deprecated in 3.12+; use asyncio.run().
        asyncio.run(_run())

        final = jobs.get(job.job_id)
        assert final is not None
        assert final.status == "done", (
            f"QA fail must produce 'done' (not 'failed'). Got: {final.status}. Error: {final.error}"
        )
        assert final.result is not None, "result must be set even when QA fails"

        # Check qa_passed is False in the result
        result = final.result
        if hasattr(result, "qa_passed"):
            qa_passed = result.qa_passed
        elif isinstance(result, dict):
            qa_passed = result.get("qa_passed")
        else:
            pytest.skip(f"Unexpected result type: {type(result)}")

        assert qa_passed is False, f"result.qa_passed must be False when QA fails. Got: {qa_passed}"


# ---------------------------------------------------------------------------
# 7. Job not found → 404
# ---------------------------------------------------------------------------


class TestJobNotFound:
    def test_unknown_job_id_returns_404(self, authed_client):
        """GET /api/status with unknown job_id must return 404."""
        r = authed_client.get("/api/status/00000000-0000-0000-0000-000000000000")
        # Pre-Phase2: returns 401 (route doesn't exist) or 404 (route exists but job not found)
        assert r.status_code in (401, 404), (
            f"Unknown job_id must return 404 (or 401 if route not yet implemented). "
            f"Got {r.status_code}. Body: {r.text}"
        )
        # Post-Phase2: must be exactly 404
        if r.status_code == 404:
            body = r.json()
            assert "detail" in body or "error" in body, (
                f"404 response must include 'detail' or 'error'. Got: {body}"
            )

    def test_404_for_nonexistent_job_is_explicit(self, authed_client):
        """404 must be returned for a well-formed UUID that doesn't map to a job."""
        # A well-formed UUID that definitely doesn't exist
        fake_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        r = authed_client.get(f"/api/status/{fake_id}")
        assert r.status_code in (401, 404), (
            f"Nonexistent well-formed UUID must return 404. Got {r.status_code}"
        )


# ---------------------------------------------------------------------------
# 8. Anthropic credit balance too low → ERR_LOW_BALANCE + detail
#
# This is the failure mode we hit on 2026-04-28: 6/9 spintax-ab segments
# returned `internal_error` because the Anthropic account ran out of credits
# mid-batch. The new handler must (a) classify it as ERR_LOW_BALANCE,
# (b) preserve the provider's message in error_detail.
# ---------------------------------------------------------------------------


class TestAnthropicLowBalance:
    def test_credit_balance_low_classifies_as_low_balance(self):
        try:
            from app import jobs
            from app import spintax_runner
            import anthropic
        except ImportError:
            pytest.fail("app.jobs or app.spintax_runner not importable")

        importlib.reload(jobs)
        job = jobs.create("Body.", "instantly", "claude-opus-4-7")

        async def _mock_create(**kwargs):
            raise anthropic.BadRequestError(
                "Your credit balance is too low to access the Anthropic API.",
                response=MagicMock(status_code=400),
                body={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Your credit balance is too low to access the Anthropic API.",
                    }
                },
            )

        mock_client = MagicMock()
        mock_client.messages.create = _mock_create

        import asyncio

        async def _run():
            with patch("app.spintax_runner._make_anthropic_client", return_value=mock_client):
                with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                    await spintax_runner.run(
                        job_id=job.job_id,
                        plain_body="Body.",
                        platform="instantly",
                        model="claude-opus-4-7",
                    )

        asyncio.run(_run())
        final = jobs.get(job.job_id)
        assert final is not None
        assert final.status == "failed"
        assert final.error == "low_balance", (
            f"Anthropic credit balance error must map to ERR_LOW_BALANCE. Got {final.error!r}"
        )
        assert final.error_detail and "credit balance" in final.error_detail.lower(), (
            f"error_detail must carry the provider's actual message. Got: {final.error_detail!r}"
        )


# ---------------------------------------------------------------------------
# 9. Anthropic auth error → ERR_AUTH + detail
# ---------------------------------------------------------------------------


class TestAnthropicAuthError:
    def test_authentication_error_classifies_as_auth(self):
        try:
            from app import jobs
            from app import spintax_runner
            import anthropic
        except ImportError:
            pytest.fail("app.jobs or app.spintax_runner not importable")

        importlib.reload(jobs)
        job = jobs.create("Body.", "instantly", "claude-opus-4-7")

        async def _mock_create(**kwargs):
            raise anthropic.AuthenticationError(
                "Invalid API key.",
                response=MagicMock(status_code=401),
                body={"error": {"type": "authentication_error", "message": "Invalid API key."}},
            )

        mock_client = MagicMock()
        mock_client.messages.create = _mock_create

        import asyncio

        async def _run():
            with patch("app.spintax_runner._make_anthropic_client", return_value=mock_client):
                with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                    await spintax_runner.run(
                        job_id=job.job_id,
                        plain_body="Body.",
                        platform="instantly",
                        model="claude-opus-4-7",
                    )

        asyncio.run(_run())
        final = jobs.get(job.job_id)
        assert final is not None
        assert final.status == "failed"
        assert final.error == "auth_failed", (
            f"AuthenticationError must map to ERR_AUTH. Got {final.error!r}"
        )
        assert final.error_detail, "error_detail must be set on auth failures"


# ---------------------------------------------------------------------------
# 10. Model not found → ERR_MODEL_NOT_FOUND
#
# This is what gpt-5.5-pro will hit if the model name is wrong or the
# account doesn't have access. Surfaces clearly to the UI.
# ---------------------------------------------------------------------------


class TestModelNotFound:
    def test_openai_not_found_classifies_as_model_not_found(self):
        try:
            from app import jobs
            from app import spintax_runner
            import openai
        except ImportError:
            pytest.fail("app.jobs or app.spintax_runner not importable")

        importlib.reload(jobs)
        job = jobs.create("Body.", "instantly", "gpt-5.5-pro")

        async def _mock_create(**kwargs):
            raise openai.NotFoundError(
                "The model 'gpt-5.5-pro' does not exist or you do not have access to it.",
                response=MagicMock(status_code=404),
                body={"error": {"type": "invalid_request_error", "code": "model_not_found"}},
            )

        mock_client = MagicMock()
        # gpt-5.5-pro routes through Responses API, not chat completions
        mock_client.responses.create = _mock_create

        import asyncio

        async def _run():
            with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
                with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                    await spintax_runner.run(
                        job_id=job.job_id,
                        plain_body="Body.",
                        platform="instantly",
                        model="gpt-5.5-pro",
                    )

        asyncio.run(_run())
        final = jobs.get(job.job_id)
        assert final is not None
        assert final.status == "failed"
        assert final.error == "model_not_found", (
            f"NotFoundError must map to ERR_MODEL_NOT_FOUND. Got {final.error!r}"
        )
        assert final.error_detail and "gpt-5.5-pro" in final.error_detail, (
            f"error_detail must include the model name. Got: {final.error_detail!r}"
        )


# ---------------------------------------------------------------------------
# 11. Generic Anthropic BadRequest (not credit related) → ERR_BAD_REQUEST
# ---------------------------------------------------------------------------


class TestAnthropicGenericBadRequest:
    def test_non_credit_bad_request_classifies_as_bad_request(self):
        try:
            from app import jobs
            from app import spintax_runner
            import anthropic
        except ImportError:
            pytest.fail("app.jobs or app.spintax_runner not importable")

        importlib.reload(jobs)
        job = jobs.create("Body.", "instantly", "claude-opus-4-7")

        async def _mock_create(**kwargs):
            raise anthropic.BadRequestError(
                "Invalid parameter: max_tokens must be a positive integer.",
                response=MagicMock(status_code=400),
                body={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Invalid parameter: max_tokens must be a positive integer.",
                    }
                },
            )

        mock_client = MagicMock()
        mock_client.messages.create = _mock_create

        import asyncio

        async def _run():
            with patch("app.spintax_runner._make_anthropic_client", return_value=mock_client):
                with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                    await spintax_runner.run(
                        job_id=job.job_id,
                        plain_body="Body.",
                        platform="instantly",
                        model="claude-opus-4-7",
                    )

        asyncio.run(_run())
        final = jobs.get(job.job_id)
        assert final is not None
        assert final.status == "failed"
        assert final.error == "bad_request", (
            f"Non-credit BadRequestError must map to ERR_BAD_REQUEST. Got {final.error!r}"
        )
        assert final.error_detail and "max_tokens" in final.error_detail, (
            f"error_detail must include the provider message. Got: {final.error_detail!r}"
        )


# ---------------------------------------------------------------------------
# 12. Drift revision loop - clean on first try
# ---------------------------------------------------------------------------


class TestDriftRevisionCleanFirstTry:
    def test_no_drift_means_zero_revisions(self):
        """When the first generation has no drift, revision loop must NOT fire.
        drift_revisions stays 0 and drift_unresolved is empty.
        """
        try:
            from app import jobs
            from app import spintax_runner
        except ImportError:
            pytest.fail("modules not importable")

        importlib.reload(jobs)
        job = jobs.create("Hey {{firstName}}, quick question?", "instantly", "o3")

        # Mock the tool loop to return a clean body (no drift expected)
        from app.spintax_runner import LoopOutcome

        clean_body = (
            "{{RANDOM | Hey {{firstName}}, quick question? | "
            "Hi {{firstName}}, quick question? | "
            "Hello {{firstName}}, quick question? | "
            "Hey there, quick question? | "
            "{{firstName}}, quick question? }}"
        )
        clean_outcome = LoopOutcome(
            final_body=clean_body,
            last_passed=True,
            tool_calls_made=2,
        )

        async def _mock_loop(client, *, user_content, **kwargs):
            return clean_outcome

        # Mock QA: clean output → no warnings
        clean_qa = {
            "passed": True,
            "errors": [],
            "warnings": [],
            "error_count": 0,
            "warning_count": 0,
            "block_count": 1,
            "input_paragraph_count": 1,
        }

        import asyncio

        async def _run():
            with patch("app.spintax_runner._run_tool_loop", side_effect=_mock_loop) as mock_loop:
                with patch("app.spintax_runner.qa", return_value=clean_qa):
                    with patch("app.spintax_runner._make_openai_client", return_value=MagicMock()):
                        with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                            await spintax_runner.run(
                                job_id=job.job_id,
                                plain_body="Hey {{firstName}}, quick question?",
                                platform="instantly",
                                model="o3",
                            )
                # Tool loop called exactly ONCE (no revisions needed)
                assert mock_loop.call_count == 1, (
                    f"Clean first attempt must NOT trigger revisions. "
                    f"Tool loop called {mock_loop.call_count} times."
                )

        asyncio.run(_run())
        final = jobs.get(job.job_id)
        assert final is not None
        assert final.status == "done"
        assert final.result.drift_revisions == 0
        assert final.result.drift_unresolved == []


# ---------------------------------------------------------------------------
# 13. Drift revision loop - drift fixed after 1 revision
# ---------------------------------------------------------------------------


class TestDriftRevisionFixedOnSecondTry:
    def test_drift_resolved_on_revision_records_count(self):
        """First attempt has drift, second is clean. Revision loop must fire
        exactly once. drift_revisions=1, drift_unresolved=[].
        """
        try:
            from app import jobs
            from app import spintax_runner
        except ImportError:
            pytest.fail("modules not importable")

        importlib.reload(jobs)
        job = jobs.create("Show them they can win this deal.", "instantly", "o3")

        from app.spintax_runner import LoopOutcome

        drifted_outcome = LoopOutcome(
            final_body="DRIFTED_BODY",
            last_passed=True,
            tool_calls_made=2,
        )
        clean_outcome = LoopOutcome(
            final_body="CLEAN_BODY",
            last_passed=True,
            tool_calls_made=2,
        )

        # Tool loop returns drifted on call 1, clean on call 2
        call_count = {"n": 0}

        async def _mock_loop(client, *, user_content, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return drifted_outcome
            return clean_outcome

        # QA returns drift warnings on call 1, clean on call 2
        drift_qa = {
            "passed": True,
            "errors": [],
            "warnings": ["block 1 variation 2: drift phrase 'this quarter' not present in V1"],
            "error_count": 0,
            "warning_count": 1,
            "block_count": 1,
            "input_paragraph_count": 1,
        }
        clean_qa = {
            "passed": True,
            "errors": [],
            "warnings": [],
            "error_count": 0,
            "warning_count": 0,
            "block_count": 1,
            "input_paragraph_count": 1,
        }
        qa_calls = {"n": 0}

        def _mock_qa(*args, **kwargs):
            qa_calls["n"] += 1
            return drift_qa if qa_calls["n"] == 1 else clean_qa

        import asyncio

        async def _run():
            with patch("app.spintax_runner._run_tool_loop", side_effect=_mock_loop) as mock_loop:
                with patch("app.spintax_runner.qa", side_effect=_mock_qa):
                    with patch("app.spintax_runner._make_openai_client", return_value=MagicMock()):
                        with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                            await spintax_runner.run(
                                job_id=job.job_id,
                                plain_body="Show them they can win this deal.",
                                platform="instantly",
                                model="o3",
                            )
                # Exactly 2 tool-loop calls: initial + 1 revision
                assert mock_loop.call_count == 2, (
                    f"Single drift incident must trigger exactly 1 revision. "
                    f"Got {mock_loop.call_count} tool-loop calls."
                )
                # Verify revision call had revision prompt (not original)
                second_call_user = mock_loop.call_args_list[1].kwargs["user_content"]
                assert "REVISION PASS" in second_call_user
                assert "this quarter" in second_call_user, (
                    "Revision prompt must include the drift warning"
                )
                assert "DRIFTED_BODY" in second_call_user, (
                    "Revision prompt must include the previous drifted body"
                )

        asyncio.run(_run())
        final = jobs.get(job.job_id)
        assert final is not None
        assert final.status == "done"
        assert final.result.spintax_body == "CLEAN_BODY", (
            f"Final body must be the CLEAN second attempt. Got: {final.result.spintax_body}"
        )
        assert final.result.drift_revisions == 1
        assert final.result.drift_unresolved == []


# ---------------------------------------------------------------------------
# 14. Drift revision loop - drift never resolves, max 3 revisions enforced
# ---------------------------------------------------------------------------


class TestDriftRevisionExhausted:
    def test_max_revisions_capped_at_3(self):
        """If drift persists every revision, runner must cap at MAX_DRIFT_REVISIONS.
        drift_revisions=3, drift_unresolved is non-empty, job still ends in 'done'
        (not 'failed') so the operator can still see the best-effort body.
        """
        try:
            from app import jobs
            from app import spintax_runner
        except ImportError:
            pytest.fail("modules not importable")

        importlib.reload(jobs)
        job = jobs.create("Stubborn input.", "instantly", "o3")

        from app.spintax_runner import LoopOutcome

        async def _mock_loop(client, *, user_content, **kwargs):
            return LoopOutcome(
                final_body="STILL_DRIFTING",
                last_passed=True,
                tool_calls_made=2,
            )

        # QA always returns drift
        drift_qa = {
            "passed": True,
            "errors": [],
            "warnings": ["block 1 variation 3: drift phrase 'first demo' not present in V1"],
            "error_count": 0,
            "warning_count": 1,
            "block_count": 1,
            "input_paragraph_count": 1,
        }

        import asyncio

        async def _run():
            with patch("app.spintax_runner._run_tool_loop", side_effect=_mock_loop) as mock_loop:
                with patch("app.spintax_runner.qa", return_value=drift_qa):
                    with patch("app.spintax_runner._make_openai_client", return_value=MagicMock()):
                        with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                            await spintax_runner.run(
                                job_id=job.job_id,
                                plain_body="Stubborn input.",
                                platform="instantly",
                                model="o3",
                            )
                # Initial + 3 revisions = 4 total tool-loop calls
                assert mock_loop.call_count == 4, (
                    f"Stubborn drift must use exactly 1 + MAX_DRIFT_REVISIONS=3 "
                    f"calls = 4. Got {mock_loop.call_count}."
                )

        asyncio.run(_run())
        final = jobs.get(job.job_id)
        assert final is not None
        assert final.status == "done", (
            f"Unresolved drift must NOT fail the job - operator still gets the body. "
            f"Got status={final.status!r}"
        )
        assert final.result.drift_revisions == 3, (
            f"drift_revisions must equal MAX_DRIFT_REVISIONS=3. Got {final.result.drift_revisions}"
        )
        assert len(final.result.drift_unresolved) > 0, (
            "drift_unresolved must carry the remaining warnings so the UI "
            "can flag the body for manual review."
        )
        assert "first demo" in final.result.drift_unresolved[0]


# ---------------------------------------------------------------------------
# 15. Drift revision loop - non-drift warnings (smart quotes etc.) do NOT trigger revisions
# ---------------------------------------------------------------------------


class TestDriftRevisionIgnoresNonDriftWarnings:
    def test_smart_quote_warning_alone_does_not_trigger_revision(self):
        """QA can warn about smart quotes, doubled punctuation, etc. These are
        NOT drift and must NOT cause a revision. drift_revisions stays 0.
        """
        try:
            from app import jobs
            from app import spintax_runner
        except ImportError:
            pytest.fail("modules not importable")

        importlib.reload(jobs)
        job = jobs.create("Test body.", "instantly", "o3")

        from app.spintax_runner import LoopOutcome

        async def _mock_loop(client, *, user_content, **kwargs):
            return LoopOutcome(
                final_body="BODY_WITH_SMART_QUOTE",
                last_passed=True,
                tool_calls_made=2,
            )

        # QA flags a smart quote warning - NOT drift
        smart_quote_qa = {
            "passed": True,
            "errors": [],
            "warnings": ["block 1 variation 2: smart quote(s) present ('’')"],
            "error_count": 0,
            "warning_count": 1,
            "block_count": 1,
            "input_paragraph_count": 1,
        }

        import asyncio

        async def _run():
            with patch("app.spintax_runner._run_tool_loop", side_effect=_mock_loop) as mock_loop:
                with patch("app.spintax_runner.qa", return_value=smart_quote_qa):
                    with patch("app.spintax_runner._make_openai_client", return_value=MagicMock()):
                        with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                            await spintax_runner.run(
                                job_id=job.job_id,
                                plain_body="Test body.",
                                platform="instantly",
                                model="o3",
                            )
                # Only the initial tool-loop call - smart quotes don't trigger revisions
                assert mock_loop.call_count == 1, (
                    f"Non-drift warnings must NOT trigger revisions. "
                    f"Got {mock_loop.call_count} calls."
                )

        asyncio.run(_run())
        final = jobs.get(job.job_id)
        assert final is not None
        assert final.status == "done"
        assert final.result.drift_revisions == 0
        assert final.result.drift_unresolved == []
        # The smart quote warning should still surface in qa_warnings though
        assert any("smart quote" in w for w in final.result.qa_warnings)
