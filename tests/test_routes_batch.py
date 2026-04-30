"""Tests for app.routes.batch — HTTP endpoints."""

import io
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

from app.batch import (
    BATCH_STATUS_CANCELLED,
    BATCH_STATUS_DONE,
    BATCH_STATUS_FAILED,
    BATCH_STATUS_PARSED,
    BATCH_STATUS_RUNNING,
    BODY_STATUS_DONE,
    _reset_for_test,
    create_batch,
)
from app.parser import ParsedEmail, ParsedSegment, ParseResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_parse_result(n_segments: int = 1, emails_per_seg: int = 2) -> ParseResult:
    segments = []
    for i in range(n_segments):
        emails = [
            ParsedEmail(
                email_label=f"Email {j + 1}",
                subject_raw=f"Subject {j + 1}",
                body_raw=f"Body {j + 1}",
            )
            for j in range(emails_per_seg)
        ]
        segments.append(
            ParsedSegment(
                section="",
                segment_name=f"Segment {i + 1}",
                emails=emails,
                warnings=[],
            )
        )
    return ParseResult(segments=segments, warnings=[])


@pytest.fixture(autouse=True)
def _clean_batch_store():
    _reset_for_test()
    yield
    _reset_for_test()


# ---------------------------------------------------------------------------
# POST /api/spintax/batch
# ---------------------------------------------------------------------------


class TestSubmitBatch:
    def test_dry_run_returns_batch_id(self, authed_client):
        pr = _make_parse_result()
        with patch("app.routes.batch.parser.parse_markdown", new=AsyncMock(return_value=pr)):
            r = authed_client.post(
                "/api/spintax/batch",
                json={"md": "# Doc\n\nEmail 1\n\nBody", "platform": "instantly", "dry_run": True},
            )
        assert r.status_code == 200
        data = r.json()
        assert "batch_id" in data
        assert data["fired"] is False

    def test_dry_run_status_is_parsed(self, authed_client):
        pr = _make_parse_result()
        with patch("app.routes.batch.parser.parse_markdown", new=AsyncMock(return_value=pr)):
            r = authed_client.post(
                "/api/spintax/batch",
                json={"md": "# Doc\n\nEmail 1\n\nBody", "platform": "instantly", "dry_run": True},
            )
        assert r.json()["status"] == BATCH_STATUS_PARSED

    def test_unauthenticated_returns_401(self, client):
        r = client.post(
            "/api/spintax/batch",
            json={"md": "# Doc", "platform": "instantly", "dry_run": True},
        )
        assert r.status_code == 401

    def test_invalid_platform_returns_422(self, authed_client):
        r = authed_client.post(
            "/api/spintax/batch",
            json={"md": "# Doc", "platform": "bad_platform", "dry_run": True},
        )
        assert r.status_code == 422

    def test_empty_md_returns_422(self, authed_client):
        r = authed_client.post(
            "/api/spintax/batch",
            json={"md": "   ", "platform": "instantly", "dry_run": True},
        )
        assert r.status_code == 422

    def test_no_segments_found_returns_422(self, authed_client):
        empty_result = ParseResult(segments=[], warnings=["no segments"])
        with patch(
            "app.routes.batch.parser.parse_markdown",
            new=AsyncMock(return_value=empty_result),
        ):
            r = authed_client.post(
                "/api/spintax/batch",
                json={"md": "# Doc\n\nBody only", "platform": "instantly", "dry_run": True},
            )
        assert r.status_code == 422
        data = r.json()
        assert data["detail"]["error"] == "no_segments_found"

    def test_parser_exception_returns_500(self, authed_client):
        with patch(
            "app.routes.batch.parser.parse_markdown",
            new=AsyncMock(side_effect=RuntimeError("openai down")),
        ):
            r = authed_client.post(
                "/api/spintax/batch",
                json={"md": "# Doc\n\nEmail 1\n\nBody", "platform": "instantly", "dry_run": True},
            )
        assert r.status_code == 500

    def test_parsed_summary_in_response(self, authed_client):
        pr = _make_parse_result(n_segments=2)
        with patch("app.routes.batch.parser.parse_markdown", new=AsyncMock(return_value=pr)):
            r = authed_client.post(
                "/api/spintax/batch",
                json={"md": "# Doc", "platform": "instantly", "dry_run": True},
            )
        data = r.json()
        assert "parsed" in data
        assert len(data["parsed"]["segments"]) == 2

    def test_total_jobs_in_response(self, authed_client):
        pr = _make_parse_result(n_segments=2, emails_per_seg=3)
        with patch("app.routes.batch.parser.parse_markdown", new=AsyncMock(return_value=pr)):
            r = authed_client.post(
                "/api/spintax/batch",
                json={"md": "# Doc", "platform": "instantly", "dry_run": True},
            )
        data = r.json()
        assert data["total_jobs"] == 6

    def test_non_dry_run_fires_batch(self, authed_client):
        pr = _make_parse_result()
        with (
            patch("app.routes.batch.parser.parse_markdown", new=AsyncMock(return_value=pr)),
            patch("app.routes.batch.asyncio.create_task") as mock_task,
        ):
            r = authed_client.post(
                "/api/spintax/batch",
                json={"md": "# Doc", "platform": "instantly", "dry_run": False},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["fired"] is True
        assert data["status"] == BATCH_STATUS_RUNNING
        mock_task.assert_called_once()

    def test_emailbison_platform_accepted(self, authed_client):
        pr = _make_parse_result()
        with patch("app.routes.batch.parser.parse_markdown", new=AsyncMock(return_value=pr)):
            r = authed_client.post(
                "/api/spintax/batch",
                json={"md": "# Doc\n\nEmail 1\n\nBody", "platform": "emailbison", "dry_run": True},
            )
        assert r.status_code == 200

    def test_emails_to_spin_excludes_email_2(self, authed_client):
        # Email 2 should NOT be counted as to_spin
        pr = _make_parse_result(emails_per_seg=2)  # Email 1 + Email 2
        with patch("app.routes.batch.parser.parse_markdown", new=AsyncMock(return_value=pr)):
            r = authed_client.post(
                "/api/spintax/batch",
                json={"md": "# Doc", "platform": "instantly", "dry_run": True},
            )
        data = r.json()
        # Only Email 1 should be in emails_to_spin
        seg = data["parsed"]["segments"][0]
        assert seg["emails_to_spin"] == 1
        assert seg["email_count"] == 2


# ---------------------------------------------------------------------------
# GET /api/spintax/batch/{id}
# ---------------------------------------------------------------------------


class TestGetBatchStatus:
    def _create_batch(self) -> str:
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        return state.batch_id

    def test_known_batch_returns_200(self, authed_client):
        batch_id = self._create_batch()
        r = authed_client.get(f"/api/spintax/batch/{batch_id}")
        assert r.status_code == 200

    def test_unknown_batch_returns_404(self, authed_client):
        r = authed_client.get("/api/spintax/batch/does-not-exist")
        assert r.status_code == 404

    def test_unauthenticated_returns_401(self, client):
        r = client.get("/api/spintax/batch/any-id")
        assert r.status_code == 401

    def test_response_has_status_field(self, authed_client):
        batch_id = self._create_batch()
        r = authed_client.get(f"/api/spintax/batch/{batch_id}")
        data = r.json()
        assert "status" in data
        assert data["status"] == BATCH_STATUS_PARSED

    def test_response_has_counts(self, authed_client):
        batch_id = self._create_batch()
        r = authed_client.get(f"/api/spintax/batch/{batch_id}")
        data = r.json()
        for field in ("completed", "failed", "queued", "total"):
            assert field in data

    def test_download_url_present_when_done(self, authed_client):
        from datetime import datetime, timezone

        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_DONE
        state.started_at = datetime.now(tz=timezone.utc)
        state.completed_at = datetime.now(tz=timezone.utc)

        r = authed_client.get(f"/api/spintax/batch/{state.batch_id}")
        data = r.json()
        assert data["download_url"] is not None
        assert state.batch_id in data["download_url"]

    def test_download_url_none_when_running(self, authed_client):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_RUNNING

        r = authed_client.get(f"/api/spintax/batch/{state.batch_id}")
        data = r.json()
        assert data["download_url"] is None

    def test_download_url_present_when_cancelled(self, authed_client):
        from datetime import datetime, timezone

        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_CANCELLED
        state.started_at = datetime.now(tz=timezone.utc)
        state.completed_at = datetime.now(tz=timezone.utc)

        r = authed_client.get(f"/api/spintax/batch/{state.batch_id}")
        data = r.json()
        assert data["download_url"] is not None


# ---------------------------------------------------------------------------
# GET /api/spintax/batch/{id}/download
# ---------------------------------------------------------------------------


class TestDownloadBatch:
    def _done_batch_id(self) -> str:
        from datetime import datetime, timezone

        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_DONE
        state.started_at = datetime.now(tz=timezone.utc)
        state.completed_at = datetime.now(tz=timezone.utc)
        # Mark email as done so zip has content
        state.segments[0].emails[0].status = BODY_STATUS_DONE
        state.segments[0].emails[0].spintax_body = "{Hi|Hello} there"
        return state.batch_id

    def test_download_done_returns_200(self, authed_client):
        batch_id = self._done_batch_id()
        r = authed_client.get(f"/api/spintax/batch/{batch_id}/download")
        assert r.status_code == 200

    def test_download_done_returns_zip(self, authed_client):
        batch_id = self._done_batch_id()
        r = authed_client.get(f"/api/spintax/batch/{batch_id}/download")
        assert r.headers["content-type"] == "application/zip"
        assert zipfile.is_zipfile(io.BytesIO(r.content))

    def test_download_unknown_returns_404(self, authed_client):
        r = authed_client.get("/api/spintax/batch/nope/download")
        assert r.status_code == 404

    def test_download_unauthenticated_returns_401(self, client):
        r = client.get("/api/spintax/batch/any/download")
        assert r.status_code == 401

    def test_download_running_returns_409(self, authed_client):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_RUNNING
        r = authed_client.get(f"/api/spintax/batch/{state.batch_id}/download")
        assert r.status_code == 409

    def test_download_parsed_returns_409(self, authed_client):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        # status defaults to PARSED
        r = authed_client.get(f"/api/spintax/batch/{state.batch_id}/download")
        assert r.status_code == 409

    def test_download_cancelled_returns_200(self, authed_client):
        from datetime import datetime, timezone

        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_CANCELLED
        state.started_at = datetime.now(tz=timezone.utc)
        state.completed_at = datetime.now(tz=timezone.utc)
        r = authed_client.get(f"/api/spintax/batch/{state.batch_id}/download")
        assert r.status_code == 200

    def test_download_has_content_disposition(self, authed_client):
        batch_id = self._done_batch_id()
        r = authed_client.get(f"/api/spintax/batch/{batch_id}/download")
        assert "content-disposition" in r.headers
        assert "attachment" in r.headers["content-disposition"]


# ---------------------------------------------------------------------------
# POST /api/spintax/batch/{id}/cancel
# ---------------------------------------------------------------------------


class TestCancelBatchRoute:
    def test_cancel_known_batch_returns_200(self, authed_client):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_RUNNING
        r = authed_client.post(f"/api/spintax/batch/{state.batch_id}/cancel")
        assert r.status_code == 200

    def test_cancel_sets_cancelled_true(self, authed_client):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_RUNNING
        r = authed_client.post(f"/api/spintax/batch/{state.batch_id}/cancel")
        data = r.json()
        assert data["cancelled"] is True

    def test_cancel_unknown_returns_404(self, authed_client):
        r = authed_client.post("/api/spintax/batch/nope/cancel")
        assert r.status_code == 404

    def test_cancel_unauthenticated_returns_401(self, client):
        r = client.post("/api/spintax/batch/any/cancel")
        assert r.status_code == 401

    def test_cancel_already_done_returns_cancelled_false(self, authed_client):
        from datetime import datetime, timezone

        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_DONE
        state.started_at = datetime.now(tz=timezone.utc)
        state.completed_at = datetime.now(tz=timezone.utc)
        r = authed_client.post(f"/api/spintax/batch/{state.batch_id}/cancel")
        assert r.status_code == 200
        data = r.json()
        assert data["cancelled"] is False

    def test_cancel_already_failed_returns_cancelled_false(self, authed_client):

        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_FAILED
        r = authed_client.post(f"/api/spintax/batch/{state.batch_id}/cancel")
        assert r.status_code == 200
        data = r.json()
        assert data["cancelled"] is False

    def test_cancel_already_cancelled_returns_cancelled_false(self, authed_client):
        pr = _make_parse_result()
        state = create_batch(pr, platform="instantly")
        state.status = BATCH_STATUS_CANCELLED
        r = authed_client.post(f"/api/spintax/batch/{state.batch_id}/cancel")
        assert r.status_code == 200
        data = r.json()
        assert data["cancelled"] is False
