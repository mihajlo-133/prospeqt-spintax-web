"""Unit tests for the Responses API tool-call adapter (gpt-5.x path).

Mirrors the four happy-path scenarios from test_spintax_runner.py but
uses Responses-shape MagicMocks instead of Chat Completions shape.
Patches `app.spintax_runner._make_openai_client` to return a mock client
whose `responses.create` is an AsyncMock returning the constructed
responses (no real network).

Pattern adapted from `tests/test_failure_modes.py:362-394` —
hand-built MagicMocks, no respx.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import jobs as jobs_mod
from app import spend as spend_mod
from app import spintax_runner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_state() -> None:
    """Reload jobs + spend modules to get clean state."""
    importlib.reload(jobs_mod)
    importlib.reload(spend_mod)


def _make_function_call_item(
    *, call_id: str, name: str, arguments: str, item_id: str = "fc_x"
) -> MagicMock:
    """Build a MagicMock that quacks like a Responses function_call output item."""
    item = MagicMock()
    item.type = "function_call"
    item.id = item_id
    item.call_id = call_id
    item.name = name
    item.arguments = arguments
    item.status = "completed"
    # model_dump() must return a serializable dict; the adapter strips status.
    item.model_dump = MagicMock(
        return_value={
            "type": "function_call",
            "id": item_id,
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
            "status": "completed",
        }
    )
    return item


def _make_message_item(text: str, *, item_id: str = "msg_x") -> MagicMock:
    """Build a MagicMock that quacks like a Responses message output item."""
    item = MagicMock()
    item.type = "message"
    item.id = item_id
    item.status = "completed"
    item.model_dump = MagicMock(
        return_value={
            "type": "message",
            "id": item_id,
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
            "status": "completed",
        }
    )
    return item


def _make_reasoning_item(*, item_id: str = "rs_x") -> MagicMock:
    """Build a MagicMock that quacks like a Responses reasoning output item."""
    item = MagicMock()
    item.type = "reasoning"
    item.id = item_id
    item.status = None
    item.model_dump = MagicMock(
        return_value={
            "type": "reasoning",
            "id": item_id,
            "summary": [],
        }
    )
    return item


def _make_responses_response(
    *, output_items: list[MagicMock], output_text: str = "", usage_dict: dict | None = None
) -> MagicMock:
    """Build a MagicMock that quacks like a Responses API response."""
    usage_dict = usage_dict or {}
    resp = MagicMock()
    resp.output = output_items
    resp.output_text = output_text

    usage = MagicMock()
    usage.input_tokens = usage_dict.get("input_tokens", 100)
    usage.output_tokens = usage_dict.get("output_tokens", 50)
    # Explicitly mark Chat-shape fields as None so _compute_cost picks
    # the Responses-shape branch via _is_int.
    usage.prompt_tokens = None
    usage.completion_tokens = None
    usage.completion_tokens_details = None

    details = MagicMock()
    details.reasoning_tokens = usage_dict.get("reasoning_tokens", 10)
    usage.output_tokens_details = details
    resp.usage = usage
    return resp


# Sample valid spintax body that passes lint - mirrors the chat fixture.
_VALID_SPINTAX_BODY = (
    "{Hey {firstName},|Hi {firstName},|Hello {firstName},|Hey there,|{firstName},}\n"
    "\n"
    "{Test body sentence one.|Test body variation two.|"
    "Test body choice three.|Test body option four.|Test body pick five.}\n"
    "\n"
    "{Cheers,|Best,|Thanks,|Regards,|Talk soon,}"
)


# ---------------------------------------------------------------------------
# Scenario 1: Pass on first try (tool call -> lint passes -> message)
# ---------------------------------------------------------------------------


class TestResponsesPassFirstTry:
    async def test_job_reaches_done_for_gpt5(self):
        _reset_state()
        from app.jobs import create, get

        job = create("Test body.", "instantly", "gpt-5.5")

        # Round 1: function_call asking to lint a passing body.
        round_1 = _make_responses_response(
            output_items=[
                _make_reasoning_item(item_id="rs_1"),
                _make_function_call_item(
                    call_id="call_001",
                    name="lint_spintax",
                    arguments=json.dumps({"spintax_body": _VALID_SPINTAX_BODY}),
                ),
            ],
            output_text="",
            usage_dict={"input_tokens": 200, "output_tokens": 80, "reasoning_tokens": 30},
        )
        # Round 2: model emits final body via message (no tool calls).
        round_2 = _make_responses_response(
            output_items=[_make_message_item(_VALID_SPINTAX_BODY)],
            output_text=_VALID_SPINTAX_BODY,
            usage_dict={"input_tokens": 350, "output_tokens": 60, "reasoning_tokens": 20},
        )

        responses_seq = [round_1, round_2]
        call_count = [0]

        async def _mock_create(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return responses_seq[idx] if idx < len(responses_seq) else responses_seq[-1]

        mock_client = MagicMock()
        mock_client.responses.create = _mock_create

        # Lint always passes for this body (mock the wrapper so we don't
        # depend on the real spintax linter accepting our test body).
        lint_pass = {
            "passed": True,
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
        }

        with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch(
                    "app.spintax_runner._lint_tool_wrapper", return_value=lint_pass
                ):
                    with patch(
                        "app.spintax_runner.qa",
                        return_value={"passed": True, "errors": [], "warnings": []},
                    ):
                        await spintax_runner.run(
                            job_id=job.job_id,
                            plain_body="Test body.",
                            platform="instantly",
                            model="gpt-5.5",
                            max_tool_calls=10,
                        )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "done", (
            f"gpt-5.5 pass-first-try should land in 'done', got {final.status}. "
            f"error={final.error}"
        )
        assert final.tool_calls == 1, f"expected 1 tool call, got {final.tool_calls}"
        assert final.api_calls == 2, f"expected 2 api calls, got {final.api_calls}"
        assert final.cost_usd > 0, "cost_usd must be positive"
        # Two responses calls were made.
        assert call_count[0] == 2

    async def test_status_field_stripped_from_echoed_function_call(self):
        """Verify the adapter strips `status` from echoed output items.

        Spike showed: re-feeding output items with status='completed' to
        the next responses.create() call yields HTTP 400 'Unknown parameter:
        input[1].status'. The adapter must drop it.
        """
        _reset_state()
        from app.jobs import create, get

        job = create("Test body.", "instantly", "gpt-5.5")

        round_1 = _make_responses_response(
            output_items=[
                _make_function_call_item(
                    call_id="call_001",
                    name="lint_spintax",
                    arguments=json.dumps({"spintax_body": _VALID_SPINTAX_BODY}),
                ),
            ],
        )
        round_2 = _make_responses_response(
            output_items=[_make_message_item(_VALID_SPINTAX_BODY)],
            output_text=_VALID_SPINTAX_BODY,
        )

        captured_inputs: list[list[dict]] = []
        responses_seq = [round_1, round_2]
        call_count = [0]

        async def _mock_create(**kwargs):
            captured_inputs.append(list(kwargs["input"]))
            idx = call_count[0]
            call_count[0] += 1
            return responses_seq[idx] if idx < len(responses_seq) else responses_seq[-1]

        mock_client = MagicMock()
        mock_client.responses.create = _mock_create

        lint_pass = {
            "passed": True,
            "error_count": 0,
            "warning_count": 0,
            "errors": [],
            "warnings": [],
        }

        with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch(
                    "app.spintax_runner._lint_tool_wrapper", return_value=lint_pass
                ):
                    with patch(
                        "app.spintax_runner.qa",
                        return_value={"passed": True, "errors": [], "warnings": []},
                    ):
                        await spintax_runner.run(
                            job_id=job.job_id,
                            plain_body="Test body.",
                            platform="instantly",
                            model="gpt-5.5",
                            max_tool_calls=10,
                        )

        # Round 2's input must echo the function_call item but WITHOUT `status`.
        assert len(captured_inputs) >= 2
        round_2_input = captured_inputs[1]
        echoed_fc_items = [
            it
            for it in round_2_input
            if isinstance(it, dict) and it.get("type") == "function_call"
        ]
        assert echoed_fc_items, "function_call must be echoed back into round 2's input"
        for it in echoed_fc_items:
            assert "status" not in it, (
                f"status field MUST be stripped from echoed function_call; "
                f"present in {it}"
            )

        # And the function_call_output (tool result) must be present too.
        outputs = [
            it
            for it in round_2_input
            if isinstance(it, dict) and it.get("type") == "function_call_output"
        ]
        assert outputs, "function_call_output must be appended after the echoed call"
        assert outputs[0]["call_id"] == "call_001"


# ---------------------------------------------------------------------------
# Scenario 2: Iterate once (lint fails -> retry -> passes)
# ---------------------------------------------------------------------------


class TestResponsesIterateOnce:
    async def test_iterate_once_then_pass(self):
        _reset_state()
        from app.jobs import create, get

        job = create("Test body.", "instantly", "gpt-5.5")

        round_1 = _make_responses_response(
            output_items=[
                _make_function_call_item(
                    call_id="call_001",
                    name="lint_spintax",
                    arguments=json.dumps(
                        {"spintax_body": "BAD BODY (will fail lint)"}
                    ),
                ),
            ],
        )
        round_2 = _make_responses_response(
            output_items=[
                _make_function_call_item(
                    call_id="call_002",
                    name="lint_spintax",
                    arguments=json.dumps({"spintax_body": _VALID_SPINTAX_BODY}),
                ),
            ],
        )
        round_3 = _make_responses_response(
            output_items=[_make_message_item(_VALID_SPINTAX_BODY)],
            output_text=_VALID_SPINTAX_BODY,
        )

        responses_seq = [round_1, round_2, round_3]
        call_count = [0]

        async def _mock_create(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return responses_seq[idx] if idx < len(responses_seq) else responses_seq[-1]

        mock_client = MagicMock()
        mock_client.responses.create = _mock_create

        # First lint fails; second passes.
        lint_results = [
            {
                "passed": False,
                "error_count": 1,
                "warning_count": 0,
                "errors": ["mock error"],
                "warnings": [],
            },
            {
                "passed": True,
                "error_count": 0,
                "warning_count": 0,
                "errors": [],
                "warnings": [],
            },
        ]
        lint_idx = [0]

        def _lint_side_effect(*args, **kwargs):
            r = lint_results[lint_idx[0]]
            lint_idx[0] += 1
            return r

        with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch(
                    "app.spintax_runner._lint_tool_wrapper",
                    side_effect=_lint_side_effect,
                ):
                    with patch(
                        "app.spintax_runner.qa",
                        return_value={"passed": True, "errors": [], "warnings": []},
                    ):
                        await spintax_runner.run(
                            job_id=job.job_id,
                            plain_body="Test body.",
                            platform="instantly",
                            model="gpt-5.5",
                            max_tool_calls=10,
                        )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "done", f"expected done, got {final.status}"
        assert final.tool_calls == 2, f"expected 2 tool calls, got {final.tool_calls}"
        assert final.api_calls == 3, f"expected 3 api calls, got {final.api_calls}"


# ---------------------------------------------------------------------------
# Scenario 3: Iterate to max (budget exhausted -> failed)
# ---------------------------------------------------------------------------


class TestResponsesIterateToMax:
    async def test_max_tool_calls_reached_marks_failed(self):
        _reset_state()
        from app.jobs import ERR_MAX_TOOL_CALLS, create, get

        job = create("Test body.", "instantly", "gpt-5.5")

        # Every round returns a function_call; lint always fails.
        def _make_fc_round(call_id: str) -> MagicMock:
            return _make_responses_response(
                output_items=[
                    _make_function_call_item(
                        call_id=call_id,
                        name="lint_spintax",
                        arguments=json.dumps({"spintax_body": "STILL BAD"}),
                    ),
                ],
            )

        max_calls = 2
        # Adapter loops max_calls + 2 rounds; supply enough rounds.
        responses_seq = [_make_fc_round(f"call_{i:03d}") for i in range(max_calls + 4)]
        call_count = [0]

        async def _mock_create(**kwargs):
            idx = call_count[0]
            call_count[0] += 1
            return responses_seq[idx] if idx < len(responses_seq) else responses_seq[-1]

        mock_client = MagicMock()
        mock_client.responses.create = _mock_create

        lint_fail = {
            "passed": False,
            "error_count": 1,
            "warning_count": 0,
            "errors": ["fail forever"],
            "warnings": [],
        }

        with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch(
                    "app.spintax_runner._lint_tool_wrapper", return_value=lint_fail
                ):
                    await spintax_runner.run(
                        job_id=job.job_id,
                        plain_body="Test body.",
                        platform="instantly",
                        model="gpt-5.5",
                        max_tool_calls=max_calls,
                    )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "failed", f"expected failed, got {final.status}"
        assert final.error == ERR_MAX_TOOL_CALLS
        assert final.tool_calls == max_calls


# ---------------------------------------------------------------------------
# Scenario 4: Empty final body -> malformed
# ---------------------------------------------------------------------------


class TestResponsesMalformedEmpty:
    async def test_empty_message_body_marks_malformed(self):
        _reset_state()
        from app.jobs import ERR_MALFORMED, create, get

        job = create("Test body.", "instantly", "gpt-5.5")

        # Only a message item with empty text and empty output_text.
        empty_resp = _make_responses_response(
            output_items=[_make_message_item("")],
            output_text="",
        )

        async def _mock_create(**kwargs):
            return empty_resp

        mock_client = MagicMock()
        mock_client.responses.create = _mock_create

        with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                await spintax_runner.run(
                    job_id=job.job_id,
                    plain_body="Test body.",
                    platform="instantly",
                    model="gpt-5.5",
                    max_tool_calls=10,
                )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "failed", f"expected failed, got {final.status}"
        assert final.error == ERR_MALFORMED


# ---------------------------------------------------------------------------
# Dispatcher: feature flag and model gating
# ---------------------------------------------------------------------------


class TestDispatcher:
    async def test_dispatcher_picks_responses_path_for_gpt5(self):
        """The dispatcher should call `responses.create` (not `chat.completions`) for gpt-5.x."""
        _reset_state()
        from app.jobs import create, get

        job = create("Test body.", "instantly", "gpt-5-mini")

        round_1 = _make_responses_response(
            output_items=[_make_message_item(_VALID_SPINTAX_BODY)],
            output_text=_VALID_SPINTAX_BODY,
        )

        responses_create = AsyncMock(return_value=round_1)
        chat_create = AsyncMock(return_value=MagicMock())
        mock_client = MagicMock()
        mock_client.responses.create = responses_create
        mock_client.chat.completions.create = chat_create

        with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch(
                    "app.spintax_runner.qa",
                    return_value={"passed": True, "errors": [], "warnings": []},
                ):
                    await spintax_runner.run(
                        job_id=job.job_id,
                        plain_body="Test body.",
                        platform="instantly",
                        model="gpt-5-mini",
                        max_tool_calls=10,
                    )

        assert responses_create.await_count >= 1, (
            "dispatcher must call responses.create for gpt-5-mini"
        )
        assert chat_create.await_count == 0, (
            "dispatcher must NOT touch chat.completions.create for gpt-5-mini"
        )
        final = get(job.job_id)
        assert final.status == "done"

    async def test_dispatcher_picks_chat_path_for_o3(self):
        """o3 stays on chat.completions even with feature flag enabled."""
        _reset_state()
        from app.jobs import create, get

        job = create("Test body.", "instantly", "o3")

        # Build a chat-shape response that emits final body directly (no tool call).
        chat_msg = MagicMock()
        chat_msg.content = _VALID_SPINTAX_BODY
        chat_msg.tool_calls = None
        chat_choice = MagicMock()
        chat_choice.message = chat_msg
        chat_resp = MagicMock()
        chat_resp.choices = [chat_choice]
        chat_usage = MagicMock()
        chat_usage.prompt_tokens = 100
        chat_usage.completion_tokens = 50
        chat_usage.input_tokens = None
        chat_usage.output_tokens = None
        chat_usage.output_tokens_details = None
        chat_details = MagicMock()
        chat_details.reasoning_tokens = 10
        chat_usage.completion_tokens_details = chat_details
        chat_resp.usage = chat_usage

        responses_create = AsyncMock(return_value=MagicMock())
        chat_create = AsyncMock(return_value=chat_resp)
        mock_client = MagicMock()
        mock_client.responses.create = responses_create
        mock_client.chat.completions.create = chat_create

        with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch(
                    "app.spintax_runner.qa",
                    return_value={"passed": True, "errors": [], "warnings": []},
                ):
                    await spintax_runner.run(
                        job_id=job.job_id,
                        plain_body="Test body.",
                        platform="instantly",
                        model="o3",
                        max_tool_calls=10,
                    )

        assert chat_create.await_count >= 1, "o3 must use chat.completions"
        assert responses_create.await_count == 0, "o3 must NOT use responses.create"
        final = get(job.job_id)
        assert final.status == "done"

    async def test_dispatcher_killswitch_routes_gpt5_to_chat(self, monkeypatch):
        """When RESPONSES_API_ENABLED=False, gpt-5.x falls back to chat.completions."""
        _reset_state()
        from app.jobs import create, get

        # Flip the feature flag off for this test.
        monkeypatch.setattr(
            "app.spintax_runner.settings.responses_api_enabled", False
        )

        job = create("Test body.", "instantly", "gpt-5.5")

        chat_msg = MagicMock()
        chat_msg.content = _VALID_SPINTAX_BODY
        chat_msg.tool_calls = None
        chat_choice = MagicMock()
        chat_choice.message = chat_msg
        chat_resp = MagicMock()
        chat_resp.choices = [chat_choice]
        chat_usage = MagicMock()
        chat_usage.prompt_tokens = 100
        chat_usage.completion_tokens = 50
        chat_usage.input_tokens = None
        chat_usage.output_tokens = None
        chat_usage.output_tokens_details = None
        chat_details = MagicMock()
        chat_details.reasoning_tokens = 10
        chat_usage.completion_tokens_details = chat_details
        chat_resp.usage = chat_usage

        responses_create = AsyncMock(return_value=MagicMock())
        chat_create = AsyncMock(return_value=chat_resp)
        mock_client = MagicMock()
        mock_client.responses.create = responses_create
        mock_client.chat.completions.create = chat_create

        with patch("app.spintax_runner._make_openai_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch(
                    "app.spintax_runner.qa",
                    return_value={"passed": True, "errors": [], "warnings": []},
                ):
                    await spintax_runner.run(
                        job_id=job.job_id,
                        plain_body="Test body.",
                        platform="instantly",
                        model="gpt-5.5",
                        max_tool_calls=10,
                    )

        assert chat_create.await_count >= 1, "killswitch: gpt-5.5 must fall back to chat"
        assert responses_create.await_count == 0
