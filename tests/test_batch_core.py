"""Tests for app.batch — public API functions and data model helpers."""

import pytest

from app.batch import (
    BATCH_STATUS_CANCELLED,
    BATCH_STATUS_DONE,
    BATCH_STATUS_FAILED,
    BATCH_STATUS_PARSED,
    BATCH_STATUS_RUNNING,
    BODY_STATUS_DONE,
    BODY_STATUS_FAILED,
    BODY_STATUS_QUEUED,
    DEFAULT_CONCURRENCY,
    MAX_CONCURRENCY,
    BatchEmailJob,
    BatchSegment,
    BatchState,
    _reset_for_test,
    _should_skip_spintax,
    cancel_batch,
    create_batch,
    get_batch,
    list_batches,
)
from app.parser import ParsedEmail, ParsedSegment, ParseResult


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_parse_result(
    n_segments: int = 1,
    emails_per_seg: int = 2,
    warnings: list[str] | None = None,
) -> ParseResult:
    """Build a minimal ParseResult for create_batch calls."""
    segments = []
    for i in range(n_segments):
        emails = [
            ParsedEmail(
                email_label=f"Email {j + 1}",
                subject_raw=f"Subject {j + 1}",
                body_raw=f"Body text for email {j + 1} segment {i + 1}",
            )
            for j in range(emails_per_seg)
        ]
        segments.append(
            ParsedSegment(
                section=f"Section {i + 1}",
                segment_name=f"Segment {i + 1}",
                emails=emails,
                warnings=[],
            )
        )
    return ParseResult(segments=segments, warnings=warnings or [])


@pytest.fixture(autouse=True)
def _clean_store():
    """Reset the in-memory batch store before and after every test."""
    _reset_for_test()
    yield
    _reset_for_test()


# ---------------------------------------------------------------------------
# _should_skip_spintax
# ---------------------------------------------------------------------------


class TestShouldSkipSpintax:
    def test_empty_label_returns_false(self):
        assert _should_skip_spintax("") is False

    def test_none_like_empty(self):
        # Technically the type hint is str, but guard anyway
        assert _should_skip_spintax("") is False

    def test_email_1_not_skipped(self):
        assert _should_skip_spintax("Email 1") is False

    def test_email_2_skipped(self):
        assert _should_skip_spintax("Email 2") is True

    def test_email_3_skipped(self):
        assert _should_skip_spintax("Email 3") is True

    def test_email_1_var_a_not_skipped(self):
        assert _should_skip_spintax("Email 1 (Var A)") is False

    def test_email_1_var_b_not_skipped(self):
        assert _should_skip_spintax("Email 1 (Var B)") is False

    def test_unrecognized_label_returns_false(self):
        assert _should_skip_spintax("Follow-up") is False

    def test_case_insensitive(self):
        assert _should_skip_spintax("EMAIL 2") is True

    def test_label_without_number_returns_false(self):
        assert _should_skip_spintax("Outreach A") is False


# ---------------------------------------------------------------------------
# create_batch
# ---------------------------------------------------------------------------


class TestCreateBatch:
    def test_creates_state_in_parsed_status(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        assert state.status == BATCH_STATUS_PARSED

    def test_batch_id_starts_with_bat_(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        assert state.batch_id.startswith("bat_")

    def test_segments_match_parse_result(self):
        pr = _make_parse_result(n_segments=3)
        state = create_batch(pr, platform="instantly")
        assert len(state.segments) == 3

    def test_emails_per_segment_preserved(self):
        pr = _make_parse_result(n_segments=1, emails_per_seg=3)
        state = create_batch(pr, platform="instantly")
        assert len(state.segments[0].emails) == 3

    def test_platform_stored(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="emailbison")
        assert state.platform == "emailbison"

    def test_model_stored(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly", model="gpt-4o")
        assert state.model == "gpt-4o"

    def test_default_model_used_when_none(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly", model=None)
        # Should fall back to settings.default_model (non-empty)
        assert state.model is not None
        assert len(state.model) > 0

    def test_concurrency_stored(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly", concurrency=3)
        assert state.concurrency == 3

    def test_default_concurrency(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        assert state.concurrency == DEFAULT_CONCURRENCY

    def test_invalid_concurrency_low_raises(self):
        pr = _make_parse_result()
        with pytest.raises(ValueError, match="concurrency"):
            create_batch(pr, platform="instantly", concurrency=0)

    def test_invalid_concurrency_high_raises(self):
        pr = _make_parse_result()
        with pytest.raises(ValueError, match="concurrency"):
            create_batch(pr, platform="instantly", concurrency=MAX_CONCURRENCY + 1)

    def test_empty_segments_raises(self):
        pr = ParseResult(segments=[], warnings=[])
        with pytest.raises(ValueError, match="no segments"):
            create_batch(pr, platform="instantly")

    def test_parse_warnings_stored(self):
        pr = _make_parse_result(warnings=["warn1", "warn2"])
        state = create_batch(pr, platform="instantly")
        assert state.parse_warnings == ["warn1", "warn2"]

    def test_batch_stored_in_memory(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        retrieved = get_batch(state.batch_id)
        assert retrieved is not None
        assert retrieved.batch_id == state.batch_id

    def test_email_labels_preserved(self):
        pr = _make_parse_result(emails_per_seg=2)
        state = create_batch(pr, platform="instantly")
        labels = [e.email_label for e in state.segments[0].emails]
        assert labels == ["Email 1", "Email 2"]

    def test_body_raw_preserved(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        assert state.segments[0].emails[0].body_raw == "Body text for email 1 segment 1"


# ---------------------------------------------------------------------------
# get_batch
# ---------------------------------------------------------------------------


class TestGetBatch:
    def test_returns_none_for_unknown_id(self):
        assert get_batch("unknown-id") is None

    def test_returns_state_for_known_id(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        result = get_batch(state.batch_id)
        assert result is not None
        assert result.batch_id == state.batch_id

    def test_returns_none_for_expired_batch(self, monkeypatch):
        from datetime import datetime, timedelta, timezone

        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")

        # Patch created_at to be way in the past
        past = datetime.now(tz=timezone.utc) - timedelta(hours=48)
        state.created_at = past

        result = get_batch(state.batch_id)
        assert result is None


# ---------------------------------------------------------------------------
# list_batches
# ---------------------------------------------------------------------------


class TestListBatches:
    def test_returns_empty_list_when_no_batches(self):
        assert list_batches() == []

    def test_returns_created_batch(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        batches = list_batches()
        assert len(batches) == 1
        assert batches[0].batch_id == state.batch_id

    def test_returns_most_recent_first(self):
        pr = _make_parse_result()
        s1 = create_batch(pr, platform="instantly")
        s2 = create_batch(pr, platform="instantly")
        batches = list_batches()
        # Most recent created last should come first
        assert batches[0].batch_id == s2.batch_id
        assert batches[1].batch_id == s1.batch_id

    def test_limit_respected(self):
        pr = _make_parse_result()
        for _ in range(5):
            create_batch(pr, platform="instantly")
        batches = list_batches(limit=3)
        assert len(batches) == 3

    def test_evicts_expired_batches(self, monkeypatch):
        from datetime import datetime, timedelta, timezone

        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        # Mark as expired
        state.created_at = datetime.now(tz=timezone.utc) - timedelta(hours=48)

        batches = list_batches()
        assert len(batches) == 0


# ---------------------------------------------------------------------------
# cancel_batch
# ---------------------------------------------------------------------------


class TestCancelBatch:
    def test_cancel_unknown_returns_false(self):
        assert cancel_batch("nope") is False

    def test_cancel_parsed_batch_succeeds(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        result = cancel_batch(state.batch_id)
        assert result is True

    def test_cancel_sets_status_cancelled(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        cancel_batch(state.batch_id)
        updated = get_batch(state.batch_id)
        assert updated.status == BATCH_STATUS_CANCELLED

    def test_cancel_already_done_returns_false(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_DONE
        assert cancel_batch(state.batch_id) is False

    def test_cancel_already_failed_returns_false(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_FAILED
        assert cancel_batch(state.batch_id) is False

    def test_cancel_already_cancelled_returns_false(self):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_CANCELLED
        assert cancel_batch(state.batch_id) is False


# ---------------------------------------------------------------------------
# BatchState helpers
# ---------------------------------------------------------------------------


class TestBatchStateHelpers:
    def _make_state_with_emails(self, statuses: list[str]) -> BatchState:
        from datetime import datetime, timezone

        emails = [
            BatchEmailJob(
                section="",
                segment_idx=0,
                email_idx=i,
                email_label=f"Email {i + 1}",
                subject_raw="Sub",
                body_raw="Body",
                parser_warnings=[],
                status=s,
                qa_errors=[],
                qa_warnings=[],
            )
            for i, s in enumerate(statuses)
        ]
        seg = BatchSegment(section="", segment_name="Seg", parser_warnings=[], emails=emails)
        return BatchState(
            batch_id="t1",
            status=BATCH_STATUS_RUNNING,
            platform="instantly",
            model="gpt-4o",
            concurrency=5,
            segments=[seg],
            parse_warnings=[],
            created_at=datetime.now(tz=timezone.utc),
        )

    def test_all_bodies_returns_flat_list(self):
        state = self._make_state_with_emails([BODY_STATUS_DONE, BODY_STATUS_QUEUED])
        assert len(state.all_bodies) == 2

    def test_total_bodies_count(self):
        state = self._make_state_with_emails([BODY_STATUS_DONE] * 4)
        assert state.total_bodies == 4

    def test_counts_completed(self):
        state = self._make_state_with_emails([BODY_STATUS_DONE, BODY_STATUS_QUEUED])
        assert state.counts()["completed"] == 1

    def test_counts_failed(self):
        state = self._make_state_with_emails([BODY_STATUS_FAILED, BODY_STATUS_DONE])
        assert state.counts()["failed"] == 1

    def test_total_cost_sums_all(self):
        from datetime import datetime, timezone

        emails = [
            BatchEmailJob(
                section="",
                segment_idx=0,
                email_idx=i,
                email_label="Email 1",
                subject_raw="S",
                body_raw="B",
                parser_warnings=[],
                cost_usd=0.05,
                qa_errors=[],
                qa_warnings=[],
            )
            for i in range(3)
        ]
        seg = BatchSegment(section="", segment_name="S", parser_warnings=[], emails=emails)
        state = BatchState(
            batch_id="t2",
            status=BATCH_STATUS_DONE,
            platform="instantly",
            model="gpt-4o",
            concurrency=5,
            segments=[seg],
            parse_warnings=[],
            created_at=datetime.now(tz=timezone.utc),
        )
        assert abs(state.total_cost_usd() - 0.15) < 0.001

    def test_elapsed_sec_with_start_and_end(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(tz=timezone.utc)
        state = self._make_state_with_emails([BODY_STATUS_DONE])
        state.started_at = now
        state.completed_at = now + timedelta(seconds=30)
        assert abs(state.elapsed_sec() - 30.0) < 0.1

    def test_elapsed_sec_no_start_returns_zero(self):
        state = self._make_state_with_emails([BODY_STATUS_DONE])
        state.started_at = None
        state.completed_at = None
        assert state.elapsed_sec() == 0.0

    def test_total_retries_sums_all(self):
        from datetime import datetime, timezone

        emails = [
            BatchEmailJob(
                section="",
                segment_idx=0,
                email_idx=i,
                email_label="Email 1",
                subject_raw="S",
                body_raw="B",
                parser_warnings=[],
                retry_count=i + 1,
                qa_errors=[],
                qa_warnings=[],
            )
            for i in range(3)
        ]
        seg = BatchSegment(section="", segment_name="S", parser_warnings=[], emails=emails)
        state = BatchState(
            batch_id="t3",
            status=BATCH_STATUS_DONE,
            platform="instantly",
            model="gpt-4o",
            concurrency=5,
            segments=[seg],
            parse_warnings=[],
            created_at=datetime.now(tz=timezone.utc),
        )
        # retry_counts are 1, 2, 3 = 6 total
        assert state.total_retries() == 6
