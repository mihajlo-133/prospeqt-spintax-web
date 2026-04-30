"""Tests for the parser adapter (Chat vs Responses dispatch).

Covers:
1. _call_parser dispatches to chat.completions for o4-mini (default).
2. _call_parser dispatches to responses.create for gpt-5-mini.
3. The Responses path uses text.format.json_schema (not response_format).
4. The Chat path uses response_format.json_schema (unchanged).
5. _parse_single_chunk passes the model from settings into _call_parser.
6. Multiple chunks parsed in parallel (semaphore) all go through the
   same dispatch and produce a merged ParseResult.

Pattern: hand-built MagicMocks, no respx. No real network.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch


from app import parser as parser_mod


# Sample valid JSON the model would return.
SAMPLE_PARSED = {
    "segments": [
        {
            "section": "Copy Agencies",
            "segment_name": "Segment 1",
            "emails": [
                {
                    "email_label": "Email 1",
                    "subject_raw": "Subject line",
                    "body_raw": "Body line.",
                }
            ],
            "warnings": [],
        }
    ],
    "warnings": [],
}


# ---------------------------------------------------------------------------
# Dispatch tests on _call_parser
# ---------------------------------------------------------------------------


class TestCallParserDispatch:
    async def test_chat_path_for_o4_mini(self):
        """o4-mini must use chat.completions with response_format.json_schema."""
        chat_msg = MagicMock()
        chat_msg.content = json.dumps(SAMPLE_PARSED)
        chat_choice = MagicMock()
        chat_choice.message = chat_msg
        chat_resp = MagicMock()
        chat_resp.choices = [chat_choice]

        responses_create = AsyncMock()
        chat_create = AsyncMock(return_value=chat_resp)
        client = MagicMock()
        client.responses.create = responses_create
        client.chat.completions.create = chat_create

        raw = await parser_mod._call_parser(
            client,
            model="o4-mini",
            system_prompt="SYS",
            user_content="USER",
        )

        assert raw == json.dumps(SAMPLE_PARSED)
        assert chat_create.await_count == 1, "o4-mini must call chat.completions"
        assert responses_create.await_count == 0, "o4-mini must NOT call responses"

        # Verify the chat call used response_format json_schema.
        call_kwargs = chat_create.await_args.kwargs
        assert call_kwargs["response_format"]["type"] == "json_schema"
        assert (
            call_kwargs["response_format"]["json_schema"]["name"]
            == parser_mod.PARSER_SCHEMA["name"]
        )
        # And reasoning_effort was set (chat path uses the kwarg, not nested).
        assert call_kwargs.get("reasoning_effort") == "high"

    async def test_responses_path_for_gpt5_mini(self):
        """gpt-5-mini must use responses.create with text.format.json_schema."""
        # Responses-shape mock: output_text holds the JSON string.
        resp = MagicMock()
        resp.output_text = json.dumps(SAMPLE_PARSED)

        responses_create = AsyncMock(return_value=resp)
        chat_create = AsyncMock()
        client = MagicMock()
        client.responses.create = responses_create
        client.chat.completions.create = chat_create

        raw = await parser_mod._call_parser(
            client,
            model="gpt-5-mini",
            system_prompt="SYS",
            user_content="USER",
        )

        assert raw == json.dumps(SAMPLE_PARSED)
        assert responses_create.await_count == 1, "gpt-5-mini must call responses"
        assert chat_create.await_count == 0, "gpt-5-mini must NOT call chat.completions"

        call_kwargs = responses_create.await_args.kwargs
        # Responses API uses text.format, not response_format.
        assert "response_format" not in call_kwargs
        text_cfg = call_kwargs["text"]["format"]
        assert text_cfg["type"] == "json_schema"
        assert text_cfg["name"] == parser_mod.PARSER_SCHEMA["name"]
        assert text_cfg["strict"] is True
        # Schema body is preserved.
        assert text_cfg["schema"] == parser_mod.PARSER_SCHEMA["schema"]
        # Responses path uses reasoning={"effort": ...} not reasoning_effort=.
        assert call_kwargs.get("reasoning") == {"effort": "high"}
        # System prompt becomes `instructions`.
        assert call_kwargs["instructions"] == "SYS"

    async def test_responses_path_killswitch_falls_back_to_chat(self, monkeypatch):
        """When responses_api_enabled=False, gpt-5-mini drops back to chat.completions."""
        monkeypatch.setattr(parser_mod.settings, "responses_api_enabled", False)

        chat_msg = MagicMock()
        chat_msg.content = json.dumps(SAMPLE_PARSED)
        chat_choice = MagicMock()
        chat_choice.message = chat_msg
        chat_resp = MagicMock()
        chat_resp.choices = [chat_choice]

        responses_create = AsyncMock()
        chat_create = AsyncMock(return_value=chat_resp)
        client = MagicMock()
        client.responses.create = responses_create
        client.chat.completions.create = chat_create

        await parser_mod._call_parser(
            client,
            model="gpt-5-mini",
            system_prompt="SYS",
            user_content="USER",
        )

        assert chat_create.await_count == 1, "killswitch: gpt-5-mini must use chat"
        assert responses_create.await_count == 0


# ---------------------------------------------------------------------------
# _parse_single_chunk passes through to _call_parser correctly
# ---------------------------------------------------------------------------


class TestParseSingleChunk:
    async def test_single_chunk_uses_settings_parser_model(self, monkeypatch):
        """_parse_single_chunk should call _call_parser with settings.parser_model."""
        monkeypatch.setattr(parser_mod.settings, "parser_model", "gpt-5-mini")

        captured = {}

        async def _mock_call_parser(client, *, model, system_prompt, user_content):
            captured["model"] = model
            captured["user_content"] = user_content
            return json.dumps(SAMPLE_PARSED)

        with patch("app.parser._make_client", return_value=MagicMock()):
            with patch("app.parser._call_parser", side_effect=_mock_call_parser):
                result = await parser_mod._parse_single_chunk(
                    "## Segment 1\n\nEmail 1\n\nSubj: Hello\n\nBody.",
                    fallback_section="Test Section",
                )

        assert captured["model"] == "gpt-5-mini"
        assert "DOCUMENT:" in captured["user_content"]
        assert isinstance(result, parser_mod.ParseResult)
        assert len(result.segments) == 1
        # The fallback_section gets backfilled when section came back as
        # the original "Copy Agencies" — so it's not overwritten.
        assert result.segments[0].section in ("Copy Agencies", "Test Section")

    async def test_single_chunk_handles_empty_response(self):
        """Empty model response -> ParseResult with parser_returned_empty_response warning."""

        async def _mock_call_parser(client, *, model, system_prompt, user_content):
            return ""

        with patch("app.parser._make_client", return_value=MagicMock()):
            with patch("app.parser._call_parser", side_effect=_mock_call_parser):
                result = await parser_mod._parse_single_chunk("## Segment 1\n\nEmail 1\n\nBody.")

        assert isinstance(result, parser_mod.ParseResult)
        assert result.segments == []
        assert "parser_returned_empty_response" in result.warnings


# ---------------------------------------------------------------------------
# parse_markdown end-to-end (single chunk + multi-chunk via semaphore)
# ---------------------------------------------------------------------------


class TestParseMarkdownE2E:
    async def test_single_short_doc_parses_once(self):
        """Short doc (under SPLIT_THRESHOLD_CHARS) parses in one call."""
        call_count = [0]

        async def _mock_call_parser(client, *, model, system_prompt, user_content):
            call_count[0] += 1
            return json.dumps(SAMPLE_PARSED)

        short_doc = "# Section A\n\n## Segment 1\n\nEmail 1\n\nSubj: Hi\n\nHello world."
        with patch("app.parser._make_client", return_value=MagicMock()):
            with patch("app.parser._call_parser", side_effect=_mock_call_parser):
                result = await parser_mod.parse_markdown(short_doc)

        # Short single-section doc should result in exactly one parser call.
        assert call_count[0] == 1
        assert isinstance(result, parser_mod.ParseResult)

    async def test_multi_chunk_doc_splits_and_merges(self):
        """Doc with multiple H1 sections parses each in parallel and merges."""
        call_count = [0]

        async def _mock_call_parser(client, *, model, system_prompt, user_content):
            call_count[0] += 1
            return json.dumps(SAMPLE_PARSED)

        # Force the multi-chunk path by exceeding SPLIT_THRESHOLD_CHARS
        # AND having multiple H1 sections.
        section_a = (
            "# Copy Agencies\n\n" + ("## Segment 1\n\nEmail 1\n\nSubj: Hi\n\nBody A.\n\n") * 5
        )
        section_b = (
            "# Copy Sales Teams\n\n" + ("## Segment 1\n\nEmail 1\n\nSubj: Hi\n\nBody B.\n\n") * 5
        )
        # Pad to exceed SPLIT_THRESHOLD_CHARS.
        big_doc = section_a + section_b + ("\n\nfiller " * 5000)

        with patch("app.parser._make_client", return_value=MagicMock()):
            with patch("app.parser._call_parser", side_effect=_mock_call_parser):
                result = await parser_mod.parse_markdown(big_doc)

        # Should split and call parser more than once.
        assert call_count[0] >= 2, (
            f"expected multi-chunk parse to call _call_parser >= 2 times, got {call_count[0]}"
        )
        assert isinstance(result, parser_mod.ParseResult)
        # Merged result should have segments from each chunk.
        assert len(result.segments) >= 2
