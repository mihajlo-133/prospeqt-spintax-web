"""Unit tests for the Anthropic Messages API tool-call adapter (claude-* path).

Mirrors test_spintax_runner_responses.py but uses Anthropic-shape MagicMocks.
Patches `app.spintax_runner._make_anthropic_client` to return a mock client
whose `messages.create` is an AsyncMock — no real network.

Key differences from the Responses-API adapter tests:
- No status-field stripping (Anthropic history is message-reconstruction, not
  output-item echo).
- `r.content` is passed UNMODIFIED to the next assistant message — thinking
  blocks carry an encrypted `signature` that Anthropic validates.
- `block.input` is a parsed dict, not a JSON string.
- tool_result uses `tool_use_id`, not `call_id`.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import MagicMock, patch

import anthropic

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


def _make_usage(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> MagicMock:
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = cache_creation_input_tokens
    usage.cache_read_input_tokens = cache_read_input_tokens
    return usage


def _make_text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_thinking_block(
    thinking: str = "Let me think...", signature: str = "sig_abc"
) -> MagicMock:
    """Build a thinking block. Must be echoed unmodified (signature validated)."""
    block = MagicMock()
    block.type = "thinking"
    block.thinking = thinking
    block.signature = signature
    return block


def _make_tool_use_block(
    tool_use_id: str,
    name: str = "lint_spintax",
    input_dict: dict | None = None,
) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_use_id
    block.name = name
    block.input = input_dict or {"spintax_body": _VALID_SPINTAX_BODY}
    return block


def _make_anthropic_response(
    stop_reason: str,
    content_blocks: list[MagicMock],
    *,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content_blocks
    resp.usage = _make_usage(input_tokens=input_tokens, output_tokens=output_tokens)
    return resp


def _make_mock_client(response_seq: list[MagicMock]) -> MagicMock:
    """Build a mock AsyncAnthropic client that returns responses in order."""
    call_count = [0]

    async def _mock_create(**kwargs: Any) -> MagicMock:
        idx = call_count[0]
        call_count[0] += 1
        return response_seq[idx] if idx < len(response_seq) else response_seq[-1]

    mock_client = MagicMock()
    mock_client.messages.create = _mock_create
    mock_client._call_count = call_count
    return mock_client


# Sample valid spintax body that passes lint.
_VALID_SPINTAX_BODY = (
    "{Hey {firstName},|Hi {firstName},|Hello {firstName},|Hey there,|{firstName},}\n"
    "\n"
    "{Test body sentence one.|Test body variation two.|"
    "Test body choice three.|Test body option four.|Test body pick five.}\n"
    "\n"
    "{Cheers,|Best,|Thanks,|Regards,|Talk soon,}"
)

_LINT_PASS = {
    "passed": True,
    "error_count": 0,
    "warning_count": 0,
    "errors": [],
    "warnings": [],
}

_LINT_FAIL = {
    "passed": False,
    "error_count": 1,
    "warning_count": 0,
    "errors": ["length mismatch"],
    "warnings": [],
}


# ---------------------------------------------------------------------------
# Scenario 1: Pass on first try
# ---------------------------------------------------------------------------


class TestAnthropicPassFirstTry:
    async def test_job_reaches_done_for_claude_opus(self):
        _reset_state()
        from app.jobs import create, get

        job = create("Test body.", "instantly", "claude-opus-4-7")

        # Round 1: model calls lint_spintax.
        round_1 = _make_anthropic_response(
            stop_reason="tool_use",
            content_blocks=[
                _make_thinking_block(),
                _make_tool_use_block("tu_001"),
            ],
            input_tokens=200,
            output_tokens=80,
        )
        # Round 2: lint passes, model emits final body.
        round_2 = _make_anthropic_response(
            stop_reason="end_turn",
            content_blocks=[_make_text_block(_VALID_SPINTAX_BODY)],
            input_tokens=350,
            output_tokens=60,
        )

        mock_client = _make_mock_client([round_1, round_2])

        with patch("app.spintax_runner._make_anthropic_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch("app.spintax_runner._lint_tool_wrapper", return_value=_LINT_PASS):
                    with patch(
                        "app.spintax_runner.qa",
                        return_value={"passed": True, "errors": [], "warnings": []},
                    ):
                        await spintax_runner.run(
                            job_id=job.job_id,
                            plain_body="Test body.",
                            platform="instantly",
                            model="claude-opus-4-7",
                            max_tool_calls=10,
                        )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "done", f"expected done, got {final.status} (err={final.error})"
        assert final.tool_calls == 1, f"expected 1 tool call, got {final.tool_calls}"
        assert final.api_calls == 2, f"expected 2 api calls, got {final.api_calls}"
        assert final.cost_usd > 0, "cost_usd must be positive"
        assert mock_client._call_count[0] == 2

    async def test_sonnet_model_also_routes_to_anthropic(self):
        _reset_state()
        from app.jobs import create, get

        job = create("Test body.", "instantly", "claude-sonnet-4-6")

        round_1 = _make_anthropic_response(
            stop_reason="tool_use",
            content_blocks=[_make_tool_use_block("tu_001")],
        )
        round_2 = _make_anthropic_response(
            stop_reason="end_turn",
            content_blocks=[_make_text_block(_VALID_SPINTAX_BODY)],
        )
        mock_client = _make_mock_client([round_1, round_2])

        with patch("app.spintax_runner._make_anthropic_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch("app.spintax_runner._lint_tool_wrapper", return_value=_LINT_PASS):
                    with patch(
                        "app.spintax_runner.qa",
                        return_value={"passed": True, "errors": [], "warnings": []},
                    ):
                        await spintax_runner.run(
                            job_id=job.job_id,
                            plain_body="Test body.",
                            platform="instantly",
                            model="claude-sonnet-4-6",
                            max_tool_calls=10,
                        )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "done"


# ---------------------------------------------------------------------------
# Scenario 2: Iterate once (lint fails first, passes second)
# ---------------------------------------------------------------------------


class TestAnthropicIterateOnce:
    async def test_iterate_once_then_pass(self):
        _reset_state()
        from app.jobs import create, get

        job = create("Test body.", "instantly", "claude-opus-4-7")

        # Round 1: model calls lint with a bad draft.
        round_1 = _make_anthropic_response(
            stop_reason="tool_use",
            content_blocks=[
                _make_tool_use_block("tu_001", input_dict={"spintax_body": "bad draft"}),
            ],
        )
        # Round 2: model refines and calls lint again with good draft.
        round_2 = _make_anthropic_response(
            stop_reason="tool_use",
            content_blocks=[
                _make_tool_use_block("tu_002"),
            ],
        )
        # Round 3: lint passes, model emits final body.
        round_3 = _make_anthropic_response(
            stop_reason="end_turn",
            content_blocks=[_make_text_block(_VALID_SPINTAX_BODY)],
        )
        mock_client = _make_mock_client([round_1, round_2, round_3])

        lint_results = [_LINT_FAIL, _LINT_PASS]
        lint_idx = [0]

        def _lint_side_effect(*args: Any, **kwargs: Any) -> dict:
            result = lint_results[lint_idx[0]]
            lint_idx[0] = min(lint_idx[0] + 1, len(lint_results) - 1)
            return result

        with patch("app.spintax_runner._make_anthropic_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch("app.spintax_runner._lint_tool_wrapper", side_effect=_lint_side_effect):
                    with patch(
                        "app.spintax_runner.qa",
                        return_value={"passed": True, "errors": [], "warnings": []},
                    ):
                        await spintax_runner.run(
                            job_id=job.job_id,
                            plain_body="Test body.",
                            platform="instantly",
                            model="claude-opus-4-7",
                            max_tool_calls=10,
                        )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "done"
        assert final.tool_calls == 2


# ---------------------------------------------------------------------------
# Scenario 3: Max tool calls reached
# ---------------------------------------------------------------------------


class TestAnthropicMaxToolCalls:
    async def test_max_tool_calls_returns_failed(self):
        _reset_state()
        from app.jobs import create, get

        job = create("Test body.", "instantly", "claude-opus-4-7")

        # Model always calls lint; lint always fails.
        def _make_lint_round(tu_id: str) -> MagicMock:
            return _make_anthropic_response(
                stop_reason="tool_use",
                content_blocks=[_make_tool_use_block(tu_id)],
            )

        # 5 rounds of tool calls (max_tool_calls=3, so last 2 get error injection).
        rounds = [_make_lint_round(f"tu_{i:03d}") for i in range(5)]
        mock_client = _make_mock_client(rounds)

        with patch("app.spintax_runner._make_anthropic_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch("app.spintax_runner._lint_tool_wrapper", return_value=_LINT_FAIL):
                    await spintax_runner.run(
                        job_id=job.job_id,
                        plain_body="Test body.",
                        platform="instantly",
                        model="claude-opus-4-7",
                        max_tool_calls=3,
                    )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "failed"
        from app.jobs import ERR_MAX_TOOL_CALLS

        assert final.error == ERR_MAX_TOOL_CALLS


# ---------------------------------------------------------------------------
# Scenario 4: Thinking block is present (must be echoed unmodified)
# ---------------------------------------------------------------------------


class TestAnthropicThinkingBlock:
    async def test_thinking_block_present_job_still_succeeds(self):
        """The adapter must echo r.content unmodified including thinking blocks.

        If thinking blocks are stripped, the encrypted `signature` is
        invalidated and Anthropic returns a 400 on the next call.
        This test exercises the code path with a thinking block in round 1.
        """
        _reset_state()
        from app.jobs import create, get

        job = create("Test body.", "instantly", "claude-opus-4-7")

        round_1 = _make_anthropic_response(
            stop_reason="tool_use",
            content_blocks=[
                _make_thinking_block("Thinking through the spintax...", "sig_enc_abc123"),
                _make_tool_use_block("tu_001"),
            ],
        )
        round_2 = _make_anthropic_response(
            stop_reason="end_turn",
            content_blocks=[_make_text_block(_VALID_SPINTAX_BODY)],
        )
        mock_client = _make_mock_client([round_1, round_2])

        # Capture the messages argument on round 2 to verify thinking block included.
        captured_messages: list[Any] = []

        original_create = mock_client.messages.create

        async def _capturing_create(**kwargs: Any) -> MagicMock:
            captured_messages.append(kwargs.get("messages", []))
            return await original_create(**kwargs)

        mock_client.messages.create = _capturing_create

        with patch("app.spintax_runner._make_anthropic_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                with patch("app.spintax_runner._lint_tool_wrapper", return_value=_LINT_PASS):
                    with patch(
                        "app.spintax_runner.qa",
                        return_value={"passed": True, "errors": [], "warnings": []},
                    ):
                        await spintax_runner.run(
                            job_id=job.job_id,
                            plain_body="Test body.",
                            platform="instantly",
                            model="claude-opus-4-7",
                            max_tool_calls=10,
                        )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "done"

        # Round 2 messages should include the assistant message with full content
        # (which contains the thinking block from round 1).
        assert len(captured_messages) == 2
        round_2_messages = captured_messages[1]
        # The assistant message echoed back should have at least the thinking + tool_use blocks.
        assistant_messages = [m for m in round_2_messages if m.get("role") == "assistant"]
        assert len(assistant_messages) == 1
        content = assistant_messages[0]["content"]
        # Content is the raw r.content from round 1 — should be a list with the thinking block.
        has_thinking = any(getattr(b, "type", None) == "thinking" for b in content)
        assert has_thinking, "Thinking block must be present in echoed assistant message"


# ---------------------------------------------------------------------------
# Scenario 5: RateLimitError -> ERR_QUOTA
# ---------------------------------------------------------------------------


class TestAnthropicRateLimitError:
    async def test_rate_limit_error_maps_to_err_quota(self):
        _reset_state()
        from app.jobs import create, get
        from app.jobs import ERR_QUOTA

        job = create("Test body.", "instantly", "claude-opus-4-7")

        async def _raise_rate_limit(**kwargs: Any) -> None:
            raise anthropic.RateLimitError(
                "rate_limit",
                response=MagicMock(status_code=429),
                body={"error": {"type": "rate_limit_error"}},
            )

        mock_client = MagicMock()
        mock_client.messages.create = _raise_rate_limit

        with patch("app.spintax_runner._make_anthropic_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                await spintax_runner.run(
                    job_id=job.job_id,
                    plain_body="Test body.",
                    platform="instantly",
                    model="claude-opus-4-7",
                    max_tool_calls=10,
                )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "failed"
        assert final.error == ERR_QUOTA


# ---------------------------------------------------------------------------
# Scenario 6: APITimeoutError -> ERR_TIMEOUT
# ---------------------------------------------------------------------------


class TestAnthropicTimeoutError:
    async def test_timeout_error_maps_to_err_timeout(self):
        _reset_state()
        from app.jobs import create, get
        from app.jobs import ERR_TIMEOUT

        job = create("Test body.", "instantly", "claude-opus-4-7")

        async def _raise_timeout(**kwargs: Any) -> None:
            raise anthropic.APITimeoutError(request=MagicMock())

        mock_client = MagicMock()
        mock_client.messages.create = _raise_timeout

        with patch("app.spintax_runner._make_anthropic_client", return_value=mock_client):
            with patch("app.spintax_runner.build_system_prompt", return_value="[mock]"):
                await spintax_runner.run(
                    job_id=job.job_id,
                    plain_body="Test body.",
                    platform="instantly",
                    model="claude-opus-4-7",
                    max_tool_calls=10,
                )

        final = get(job.job_id)
        assert final is not None
        assert final.status == "failed"
        assert final.error == ERR_TIMEOUT
