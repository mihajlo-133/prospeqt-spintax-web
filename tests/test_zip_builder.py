"""Tests for app.zip_builder — in-memory zip assembly, no disk writes."""

import io
import zipfile
from datetime import datetime, timezone


from app.batch import (
    BATCH_STATUS_CANCELLED,
    BATCH_STATUS_DONE,
    BODY_STATUS_DONE,
    BODY_STATUS_FAILED,
    BODY_STATUS_QUEUED,
    BODY_STATUS_RUNNING,
    BatchEmailJob,
    BatchSegment,
    BatchState,
)
from app.zip_builder import (
    _aggregate_flag,
    _email_body_block,
    _failed_md,
    _fmt_duration,
    _md_escape,
    _segment_filename,
    _segment_md,
    _slug,
    _summary_md,
    build_zip,
    zip_filename,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_email(
    *,
    section: str = "",
    segment_idx: int = 0,
    email_idx: int = 0,
    email_label: str = "Email 1",
    subject_raw: str = "Subject line",
    body_raw: str = "Original body text",
    status: str = BODY_STATUS_DONE,
    spintax_body: str | None = "{Hello|Hi} there",
    retry_count: int = 0,
    last_error: str | None = None,
    lint_passed: bool = True,
    qa_passed: bool = True,
    qa_warnings: list[str] | None = None,
    cost_usd: float = 0.01,
) -> BatchEmailJob:
    return BatchEmailJob(
        section=section,
        segment_idx=segment_idx,
        email_idx=email_idx,
        email_label=email_label,
        subject_raw=subject_raw,
        body_raw=body_raw,
        parser_warnings=[],
        status=status,
        spintax_body=spintax_body,
        retry_count=retry_count,
        last_error=last_error,
        lint_passed=lint_passed,
        qa_passed=qa_passed,
        qa_errors=[],
        qa_warnings=qa_warnings or [],
        cost_usd=cost_usd,
    )


def _make_segment(
    *,
    section: str = "",
    segment_name: str = "Segment A",
    emails: list[BatchEmailJob] | None = None,
    parser_warnings: list[str] | None = None,
) -> BatchSegment:
    return BatchSegment(
        section=section,
        segment_name=segment_name,
        parser_warnings=parser_warnings or [],
        emails=emails or [_make_email()],
    )


def _make_state(
    *,
    segments: list[BatchSegment] | None = None,
    status: str = BATCH_STATUS_DONE,
    platform: str = "instantly",
    model: str = "gpt-4o",
    concurrency: int = 5,
    parse_warnings: list[str] | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    failure_reason: str | None = None,
) -> BatchState:
    return BatchState(
        batch_id="test-batch-001",
        status=status,
        platform=platform,
        model=model,
        concurrency=concurrency,
        segments=segments or [_make_segment()],
        parse_warnings=parse_warnings or [],
        created_at=_NOW,
        started_at=started_at or _NOW,
        completed_at=completed_at or _NOW,
        failure_reason=failure_reason,
    )


# ---------------------------------------------------------------------------
# _slug
# ---------------------------------------------------------------------------


class TestSlug:
    def test_basic_lowercases(self):
        assert _slug("Hello World") == "hello_world"

    def test_removes_special_chars(self):
        assert _slug("Hello & World!") == "hello_world"

    def test_strips_markdown_bold(self):
        assert _slug("**Bold Text**") == "bold_text"

    def test_strips_markdown_underline(self):
        assert _slug("__Underline__") == "underline"

    def test_strips_google_docs_backslash(self):
        assert _slug("foo\\.bar") == "foo_bar"

    def test_truncates_at_max_len(self):
        long = "a" * 60
        result = _slug(long, max_len=50)
        assert len(result) <= 50

    def test_empty_string_returns_untitled(self):
        assert _slug("") == "untitled"

    def test_only_special_chars_returns_untitled(self):
        assert _slug("!@#$%") == "untitled"

    def test_trailing_underscore_stripped(self):
        result = _slug("hello world ")
        assert not result.endswith("_")

    def test_multiple_spaces_collapsed(self):
        assert _slug("hello   world") == "hello_world"


# ---------------------------------------------------------------------------
# _segment_filename
# ---------------------------------------------------------------------------


class TestSegmentFilename:
    def _seg(self, section="", name="Segment A"):
        return _make_segment(section=section, segment_name=name)

    def test_single_section_no_prefix(self):
        seg = self._seg(section="Copy Agencies")
        result = _segment_filename(1, seg, multi_section=False)
        assert result == "01_segment_a.md"
        assert "agencies" not in result

    def test_multi_section_includes_section(self):
        seg = self._seg(section="Copy Agencies", name="Segment 1")
        result = _segment_filename(1, seg, multi_section=True)
        assert result == "01_copy_agencies_segment_1.md"

    def test_ordinal_zero_padded(self):
        seg = self._seg()
        result = _segment_filename(9, seg, multi_section=False)
        assert result.startswith("09_")

    def test_multi_section_no_section_field_falls_back(self):
        seg = self._seg(section="", name="Segment 1")
        result = _segment_filename(3, seg, multi_section=True)
        # section is empty - should still produce a valid filename
        assert result.endswith(".md")

    def test_markdown_noise_stripped_from_name(self):
        seg = self._seg(name="**Bold Segment**")
        result = _segment_filename(1, seg, multi_section=False)
        assert "bold_segment" in result
        assert "**" not in result


# ---------------------------------------------------------------------------
# _email_body_block
# ---------------------------------------------------------------------------


class TestEmailBodyBlock:
    def test_done_returns_spintax_body(self):
        em = _make_email(status=BODY_STATUS_DONE, spintax_body="{Hi|Hello} world")
        result = _email_body_block(em)
        assert result == "{Hi|Hello} world"

    def test_done_trims_trailing_whitespace(self):
        em = _make_email(status=BODY_STATUS_DONE, spintax_body="text   \n  ")
        result = _email_body_block(em)
        assert result == "text"

    def test_failed_includes_error_and_original(self):
        em = _make_email(
            status=BODY_STATUS_FAILED,
            spintax_body=None,
            last_error="timeout",
            retry_count=3,
            body_raw="original body",
        )
        result = _email_body_block(em)
        assert "FAILED" in result
        assert "3 retries" in result
        assert "timeout" in result
        assert "original body" in result

    def test_queued_returns_not_generated(self):
        em = _make_email(status=BODY_STATUS_QUEUED, spintax_body=None)
        result = _email_body_block(em)
        assert "NOT GENERATED" in result
        assert BODY_STATUS_QUEUED in result

    def test_running_returns_not_generated(self):
        em = _make_email(status=BODY_STATUS_RUNNING, spintax_body=None)
        result = _email_body_block(em)
        assert "NOT GENERATED" in result


# ---------------------------------------------------------------------------
# _aggregate_flag
# ---------------------------------------------------------------------------


class TestAggregateFlag:
    def test_empty_returns_dash(self):
        assert _aggregate_flag([]) == "-"

    def test_all_true_returns_pass(self):
        assert _aggregate_flag([True, True]) == "PASS"

    def test_all_false_returns_fail(self):
        assert _aggregate_flag([False, False]) == "FAIL"

    def test_mixed_returns_fraction(self):
        result = _aggregate_flag([True, False, True])
        assert result == "2/3"


# ---------------------------------------------------------------------------
# _fmt_duration
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_under_60s(self):
        assert _fmt_duration(45.0) == "45s"

    def test_exactly_60s(self):
        assert _fmt_duration(60.0) == "1m 00s"

    def test_minutes_and_seconds(self):
        assert _fmt_duration(125.0) == "2m 05s"

    def test_zero(self):
        assert _fmt_duration(0.0) == "0s"


# ---------------------------------------------------------------------------
# _md_escape
# ---------------------------------------------------------------------------


class TestMdEscape:
    def test_escapes_pipe(self):
        assert _md_escape("foo|bar") == r"foo\|bar"

    def test_no_pipe_unchanged(self):
        assert _md_escape("hello world") == "hello world"

    def test_multiple_pipes(self):
        result = _md_escape("a|b|c")
        assert result.count(r"\|") == 2


# ---------------------------------------------------------------------------
# _segment_md
# ---------------------------------------------------------------------------


class TestSegmentMd:
    def test_contains_segment_name(self):
        seg = _make_segment(segment_name="Big Segment")
        state = _make_state(segments=[seg])
        result = _segment_md(state, seg)
        assert "Big Segment" in result

    def test_contains_model_and_platform(self):
        state = _make_state(model="gpt-4o", platform="instantly")
        seg = state.segments[0]
        result = _segment_md(state, seg)
        assert "gpt-4o" in result
        assert "instantly" in result

    def test_contains_subject(self):
        email = _make_email(subject_raw="My Subject Line")
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _segment_md(state, seg)
        assert "My Subject Line" in result

    def test_contains_spintax_body(self):
        email = _make_email(spintax_body="{Hi|Hello} partner")
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _segment_md(state, seg)
        assert "{Hi|Hello} partner" in result

    def test_section_shown_when_present(self):
        seg = _make_segment(section="Copy Agencies")
        state = _make_state(segments=[seg])
        result = _segment_md(state, seg)
        assert "Copy Agencies" in result

    def test_parser_warnings_shown(self):
        seg = _make_segment(parser_warnings=["missing_subject"])
        state = _make_state(segments=[seg])
        result = _segment_md(state, seg)
        assert "missing_subject" in result

    def test_multiple_emails_separated_by_horizontal_rule(self):
        emails = [
            _make_email(email_idx=0, email_label="Email 1"),
            _make_email(email_idx=1, email_label="Email 2"),
        ]
        seg = _make_segment(emails=emails)
        state = _make_state(segments=[seg])
        result = _segment_md(state, seg)
        # at least one horizontal rule between emails
        assert "---" in result
        assert "Email 1" in result
        assert "Email 2" in result


# ---------------------------------------------------------------------------
# _summary_md
# ---------------------------------------------------------------------------


class TestSummaryMd:
    def test_contains_batch_id(self):
        state = _make_state()
        result = _summary_md(state)
        assert "test-batch-001" in result

    def test_contains_segment_table_header(self):
        state = _make_state()
        result = _summary_md(state)
        assert "| # | Segment |" in result

    def test_shows_cost(self):
        email = _make_email(cost_usd=0.05)
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _summary_md(state)
        assert "$0.05" in result

    def test_shows_status(self):
        state = _make_state(status=BATCH_STATUS_DONE)
        result = _summary_md(state)
        assert "done" in result

    def test_shows_parse_warnings(self):
        state = _make_state(parse_warnings=["parse_warn_1"])
        result = _summary_md(state)
        assert "parse_warn_1" in result
        assert "Parser warnings" in result

    def test_no_parse_warnings_section_when_empty(self):
        state = _make_state(parse_warnings=[])
        result = _summary_md(state)
        assert "Parser warnings" not in result

    def test_failure_reason_shown(self):
        state = _make_state(failure_reason="daily cap exceeded")
        result = _summary_md(state)
        assert "daily cap exceeded" in result

    def test_segment_label_truncated_when_long(self):
        long_name = "A" * 70
        seg = _make_segment(segment_name=long_name)
        state = _make_state(segments=[seg])
        result = _summary_md(state)
        assert "..." in result

    def test_lint_qa_pass(self):
        email = _make_email(lint_passed=True, qa_passed=True)
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _summary_md(state)
        assert "PASS" in result

    def test_lint_qa_fail(self):
        email = _make_email(lint_passed=False, qa_passed=False)
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _summary_md(state)
        assert "FAIL" in result

    def test_segment_notes_with_failed_bodies(self):
        email = _make_email(status=BODY_STATUS_FAILED, spintax_body=None, last_error="err")
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _summary_md(state)
        assert "failed" in result

    def test_segment_notes_with_qa_warnings(self):
        email = _make_email(qa_warnings=["repetition_detected"])
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _summary_md(state)
        assert "QA warnings" in result

    def test_segment_notes_dash_when_clean(self):
        email = _make_email(qa_warnings=[])
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _summary_md(state)
        # The dash appears in the notes column for clean segments
        assert "| - |" in result

    def test_started_not_started_placeholder(self):
        # Pass started_at=None explicitly, bypassing _make_state default
        state = BatchState(
            batch_id="test-batch-001",
            status=BATCH_STATUS_DONE,
            platform="instantly",
            model="gpt-4o",
            concurrency=5,
            segments=[_make_segment()],
            parse_warnings=[],
            created_at=_NOW,
            started_at=None,
            completed_at=_NOW,
        )
        result = _summary_md(state)
        assert "(not started)" in result

    def test_completed_at_in_progress_placeholder(self):
        # Pass completed_at=None explicitly, bypassing _make_state default
        state = BatchState(
            batch_id="test-batch-001",
            status=BATCH_STATUS_DONE,
            platform="instantly",
            model="gpt-4o",
            concurrency=5,
            segments=[_make_segment()],
            parse_warnings=[],
            created_at=_NOW,
            started_at=_NOW,
            completed_at=None,
        )
        result = _summary_md(state)
        assert "(in progress)" in result


# ---------------------------------------------------------------------------
# _failed_md
# ---------------------------------------------------------------------------


class TestFailedMd:
    def test_returns_none_when_no_failures(self):
        state = _make_state()
        assert _failed_md(state) is None

    def test_returns_content_when_failures_present(self):
        email = _make_email(
            status=BODY_STATUS_FAILED,
            spintax_body=None,
            last_error="timeout",
            retry_count=3,
        )
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _failed_md(state)
        assert result is not None
        assert "Failed Bodies" in result
        assert "timeout" in result

    def test_failed_md_includes_original_body(self):
        email = _make_email(
            status=BODY_STATUS_FAILED,
            spintax_body=None,
            body_raw="the original content",
            last_error="err",
        )
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _failed_md(state)
        assert "the original content" in result

    def test_failed_md_includes_subject(self):
        email = _make_email(
            status=BODY_STATUS_FAILED,
            spintax_body=None,
            subject_raw="My Subject",
            last_error="err",
        )
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        result = _failed_md(state)
        assert "My Subject" in result

    def test_failed_md_includes_section_when_present(self):
        email = _make_email(
            section="Copy Agencies",
            status=BODY_STATUS_FAILED,
            spintax_body=None,
            last_error="err",
        )
        seg = _make_segment(section="Copy Agencies", emails=[email])
        state = _make_state(segments=[seg])
        result = _failed_md(state)
        assert "Copy Agencies" in result


# ---------------------------------------------------------------------------
# zip_filename
# ---------------------------------------------------------------------------


class TestZipFilename:
    def test_contains_batch_id(self):
        state = _make_state()
        result = zip_filename(state)
        assert "test-batch-001" in result

    def test_ends_with_zip(self):
        state = _make_state()
        assert zip_filename(state).endswith(".zip")

    def test_contains_date(self):
        state = _make_state()
        result = zip_filename(state)
        assert "2025-06-01" in result


# ---------------------------------------------------------------------------
# build_zip — full integration (in-memory only)
# ---------------------------------------------------------------------------


class TestBuildZip:
    def _open_zip(self, state: BatchState):
        raw = build_zip(state)
        buf = io.BytesIO(raw)
        return zipfile.ZipFile(buf, "r")

    def test_returns_bytes(self):
        state = _make_state()
        result = build_zip(state)
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_is_valid_zip(self):
        state = _make_state()
        raw = build_zip(state)
        buf = io.BytesIO(raw)
        assert zipfile.is_zipfile(buf)

    def test_contains_summary_md(self):
        state = _make_state()
        zf = self._open_zip(state)
        assert "_summary.md" in zf.namelist()

    def test_contains_segment_file(self):
        state = _make_state()
        zf = self._open_zip(state)
        names = zf.namelist()
        segment_files = [n for n in names if n.startswith("01_")]
        assert len(segment_files) == 1

    def test_no_failed_md_when_no_failures(self):
        state = _make_state()
        zf = self._open_zip(state)
        assert "_failed.md" not in zf.namelist()

    def test_failed_md_present_when_failures_exist(self):
        email = _make_email(status=BODY_STATUS_FAILED, spintax_body=None, last_error="err")
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        zf = self._open_zip(state)
        assert "_failed.md" in zf.namelist()

    def test_segment_file_content_has_subject(self):
        email = _make_email(subject_raw="Special Subject")
        seg = _make_segment(emails=[email])
        state = _make_state(segments=[seg])
        zf = self._open_zip(state)
        names = [n for n in zf.namelist() if n.startswith("01_")]
        content = zf.read(names[0]).decode()
        assert "Special Subject" in content

    def test_multi_section_filenames_include_section(self):
        emails_a = [_make_email(section="Copy Agencies")]
        emails_b = [_make_email(section="Copy Sales Teams")]
        seg_a = _make_segment(section="Copy Agencies", segment_name="Segment 1", emails=emails_a)
        seg_b = _make_segment(section="Copy Sales Teams", segment_name="Segment 1", emails=emails_b)
        state = _make_state(segments=[seg_a, seg_b])
        zf = self._open_zip(state)
        names = zf.namelist()
        assert any("copy_agencies" in n for n in names)
        assert any("copy_sales_teams" in n for n in names)

    def test_single_section_filenames_exclude_section(self):
        emails = [_make_email(section="Copy Agencies")]
        seg = _make_segment(section="Copy Agencies", segment_name="Segment 1", emails=emails)
        state = _make_state(segments=[seg])
        zf = self._open_zip(state)
        names = [n for n in zf.namelist() if n.startswith("01_")]
        assert all("agencies" not in n for n in names)

    def test_multiple_segments_produce_multiple_files(self):
        seg_a = _make_segment(segment_name="Segment A")
        seg_b = _make_segment(segment_name="Segment B")
        state = _make_state(segments=[seg_a, seg_b])
        zf = self._open_zip(state)
        segment_files = [n for n in zf.namelist() if n[0].isdigit()]
        assert len(segment_files) == 2

    def test_cancelled_batch_still_produces_zip(self):
        state = _make_state(status=BATCH_STATUS_CANCELLED)
        raw = build_zip(state)
        assert zipfile.is_zipfile(io.BytesIO(raw))

    def test_summary_contains_batch_id(self):
        state = _make_state()
        zf = self._open_zip(state)
        summary = zf.read("_summary.md").decode()
        assert "test-batch-001" in summary

    def test_segment_md_no_section_field_when_empty(self):
        seg = _make_segment(section="", segment_name="Clean Segment")
        state = _make_state(segments=[seg])
        zf = self._open_zip(state)
        names = [n for n in zf.namelist() if n.startswith("01_")]
        content = zf.read(names[0]).decode()
        assert "Section:" not in content
