"""Zip builder for completed batch results.

What this does:
    Takes a completed BatchState and produces an in-memory .zip with:
      - One paste-ready .md per segment
      - _summary.md with batch stats and per-segment results
      - _failed.md (only if any body failed all retries)

What it depends on:
    - app.batch.BatchState / BatchSegment / BatchEmailJob — input shapes
    - Python stdlib (io, zipfile, datetime, re)

What depends on it:
    - app.routes.batch — streams the .zip back via FileResponse / Response

Output format (per BATCH_API_SPEC.md section 6):
    - Filename: spintax_batch_{batch_id}_{YYYY-MM-DD}.zip
    - Per-segment file: {ordinal:02d}_{section_slug}_{segment_slug}.md
    - Per-email block: `Subject: ...\n\nEmail Body:\n\n{spintax_body}`
    - Subjects always pass through verbatim — never spintaxed
    - Email 2 typically has empty subject; that's preserved cleanly

Naming rule:
    section_slug + segment_slug = lowercase, alphanumerics + underscores.
    Both Google-Docs escape backslashes (\\.) and markdown markers (**)
    are stripped before slugifying so filenames are clean.
"""

import io
import re
import zipfile
from datetime import datetime, timezone
from typing import Iterable

from app.batch import (
    BODY_STATUS_DONE,
    BODY_STATUS_FAILED,
    BatchEmailJob,
    BatchSegment,
    BatchState,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_zip(state: BatchState) -> bytes:
    """Produce the .zip bytes for a (completed) batch.

    Args:
        state: BatchState — must have at least some completed bodies.
            We don't gate on state.status because the caller may want
            to download partial results from a cancelled batch.

    Returns:
        Raw bytes of the assembled .zip ready to write to an HTTP response.
    """
    # Decide whether to prefix filenames with the section slug. Multi-section
    # docs (e.g., HeyReach: "Copy Agencies" vs "Copy Sales teams") need the
    # section in the name to disambiguate `Segment 1` across sections.
    # Single-section docs (e.g., Enavra) drop the section to keep the
    # filename about the segment, not the parent heading the parser
    # happened to pick (which is non-deterministic across runs).
    unique_sections = {seg.section for seg in state.segments if seg.section}
    multi_section = len(unique_sections) > 1

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for ordinal, seg in enumerate(state.segments, start=1):
            filename = _segment_filename(ordinal, seg, multi_section)
            content = _segment_md(state, seg)
            zf.writestr(filename, content)

        zf.writestr("_summary.md", _summary_md(state))

        failed_md = _failed_md(state)
        if failed_md is not None:
            zf.writestr("_failed.md", failed_md)

    return buf.getvalue()


def zip_filename(state: BatchState) -> str:
    """Suggested filename for the download. UTF-8-safe, ASCII-friendly."""
    today = state.created_at.strftime("%Y-%m-%d")
    return f"spintax_batch_{state.batch_id}_{today}.zip"


# ---------------------------------------------------------------------------
# Filename + slug helpers
# ---------------------------------------------------------------------------


def _slug(s: str, max_len: int = 50) -> str:
    """Lowercase, alphanumeric + underscores. Strip markdown noise first."""
    # Drop Google Docs escape backslashes (\\., \\+, \\-)
    s = re.sub(r"\\(.)", r"\1", s)
    # Drop markdown bold/italic markers
    s = s.replace("**", "").replace("__", "")
    # Lowercase + non-alnum -> underscore
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    if not s:
        s = "untitled"
    return s[:max_len].rstrip("_")


def _segment_filename(
    ordinal: int,
    seg: BatchSegment,
    multi_section: bool = False,
) -> str:
    """Build the per-segment filename.

    Single-section batch (Enavra):
        01_segment_a_recent_large_fixed_price_awards.md

    Multi-section batch (HeyReach):
        01_copy_agencies_segment_1.md
        09_copy_sales_teams_segment_1.md
    """
    segment_part = _slug(seg.segment_name)
    if multi_section and seg.section:
        section_part = _slug(seg.section)
        return f"{ordinal:02d}_{section_part}_{segment_part}.md"
    return f"{ordinal:02d}_{segment_part}.md"


# ---------------------------------------------------------------------------
# Per-segment .md
# ---------------------------------------------------------------------------


def _segment_md(state: BatchState, seg: BatchSegment) -> str:
    """Render one segment as a paste-ready markdown file.

    Hard rule: subjects are NEVER touched — passed through verbatim
    from the parser. The spintax engine only generates the body.
    """
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    lines.append(f"# {seg.segment_name}")
    lines.append("")
    if seg.section:
        lines.append(f"_Section: {seg.section}_  ")
    lines.append(f"_Generated: {ts}_  ")
    lines.append(f"_Model: {state.model}_  ")
    lines.append(f"_Platform: {state.platform}_")
    if seg.parser_warnings:
        lines.append("")
        lines.append(f"_Parser warnings: {', '.join(seg.parser_warnings)}_")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, em in enumerate(seg.emails):
        if i > 0:
            lines.append("---")
            lines.append("")
        lines.append(f"## {em.email_label}")
        lines.append("")
        lines.append(f"Subject: {em.subject_raw}")
        lines.append("")
        lines.append("Email Body:")
        lines.append("")
        lines.append(_email_body_block(em))
        lines.append("")

    return "\n".join(lines)


def _email_body_block(em: BatchEmailJob) -> str:
    """Return the body block for one email — spintax if done,
    otherwise an explanatory placeholder for failed/queued bodies."""
    if em.status == BODY_STATUS_DONE and em.spintax_body is not None:
        # Trim trailing whitespace; preserve internal line breaks.
        return em.spintax_body.rstrip()
    if em.status == BODY_STATUS_FAILED:
        return (
            f"[FAILED after {em.retry_count} retries: {em.last_error}]\n\n"
            "Original body:\n\n"
            f"{em.body_raw.rstrip()}"
        )
    # Queued, retrying, running — should not happen if batch is done,
    # but be defensive so partial downloads (cancelled batches) work.
    return f"[NOT GENERATED — status: {em.status}]\n\nOriginal body:\n\n{em.body_raw.rstrip()}"


# ---------------------------------------------------------------------------
# _summary.md
# ---------------------------------------------------------------------------


def _summary_md(state: BatchState) -> str:
    """Build the _summary.md content — batch stats + per-segment table."""
    counts = state.counts()
    completed = counts["completed"]
    failed = counts["failed"]
    total = state.total_bodies
    total_cost = state.total_cost_usd()
    elapsed = state.elapsed_sec()
    retries = state.total_retries()

    started = (
        state.started_at.strftime("%Y-%m-%dT%H:%M:%SZ") if state.started_at else "(not started)"
    )
    completed_at = (
        state.completed_at.strftime("%Y-%m-%dT%H:%M:%SZ") if state.completed_at else "(in progress)"
    )

    lines: list[str] = []
    lines.append("# Spintax Batch Summary")
    lines.append("")
    lines.append(f"- **Batch ID:** `{state.batch_id}`")
    lines.append(f"- **Status:** {state.status}")
    if state.failure_reason:
        lines.append(f"- **Failure reason:** {state.failure_reason}")
    lines.append(f"- **Created:** {state.created_at.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append(f"- **Started:** {started}")
    lines.append(f"- **Finished:** {completed_at}")
    lines.append(f"- **Platform:** {state.platform}")
    lines.append(f"- **Model:** {state.model}")
    lines.append(f"- **Concurrency:** {state.concurrency}")
    lines.append(f"- **Total segments:** {len(state.segments)}")
    lines.append(f"- **Total bodies:** {total}")
    lines.append(f"- **Bodies completed:** {completed}")
    lines.append(f"- **Bodies failed (after {3} retries):** {failed}")
    lines.append(f"- **Total retries used:** {retries}")
    lines.append(f"- **Total cost:** ${total_cost:.2f}")
    lines.append(f"- **Total elapsed:** {_fmt_duration(elapsed)}")
    if state.parse_warnings:
        lines.append("")
        lines.append("## Parser warnings")
        for w in state.parse_warnings:
            lines.append(f"- {w}")
    lines.append("")
    lines.append("## Per-segment results")
    lines.append("")
    lines.append("| # | Segment | Bodies | Cost | Lint | QA | Notes |")
    lines.append("|---|---------|--------|------|------|-----|-------|")
    for ordinal, seg in enumerate(state.segments, start=1):
        seg_cost = sum(e.cost_usd for e in seg.emails)
        n = len(seg.emails)
        lint_cell = _aggregate_flag(e.lint_passed for e in seg.emails)
        qa_cell = _aggregate_flag(e.qa_passed for e in seg.emails)
        notes = _segment_notes(seg)
        seg_label = seg.segment_name
        if len(seg_label) > 60:
            seg_label = seg_label[:57] + "..."
        lines.append(
            f"| {ordinal:02d} | {_md_escape(seg_label)} | {n} | "
            f"${seg_cost:.2f} | {lint_cell} | {qa_cell} | {notes} |"
        )
    return "\n".join(lines)


def _aggregate_flag(values: Iterable[bool]) -> str:
    vs = list(values)
    if not vs:
        return "-"
    if all(vs):
        return "PASS"
    if not any(vs):
        return "FAIL"
    return f"{sum(vs)}/{len(vs)}"


def _segment_notes(seg: BatchSegment) -> str:
    notes: list[str] = []
    if seg.parser_warnings:
        notes.extend(seg.parser_warnings)
    failed = sum(1 for e in seg.emails if e.status == BODY_STATUS_FAILED)
    if failed:
        notes.append(f"{failed} failed")
    qa_warns = sum(len(e.qa_warnings) for e in seg.emails)
    if qa_warns:
        notes.append(f"{qa_warns} QA warnings")
    return ", ".join(notes) if notes else "-"


def _md_escape(s: str) -> str:
    """Escape `|` for markdown table cells."""
    return s.replace("|", "\\|")


def _fmt_duration(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f}s"
    m = int(sec // 60)
    s = int(sec - m * 60)
    return f"{m}m {s:02d}s"


# ---------------------------------------------------------------------------
# _failed.md
# ---------------------------------------------------------------------------


def _failed_md(state: BatchState) -> str | None:
    """Return _failed.md content, or None if no bodies failed."""
    failed_bodies = [b for b in state.all_bodies if b.status == BODY_STATUS_FAILED]
    if not failed_bodies:
        return None

    lines: list[str] = []
    lines.append("# Failed Bodies")
    lines.append("")
    lines.append(
        "The following bodies failed all retries. Fix the source markdown "
        "for these segments and re-submit just those, or run a smaller "
        "batch with only the failures."
    )
    lines.append("")

    for b in failed_bodies:
        seg = state.segments[b.segment_idx]
        lines.append(f"## {seg.segment_name} — {b.email_label}")
        lines.append("")
        if b.section:
            lines.append(f"_Section: {b.section}_  ")
        lines.append(f"_Reason: {b.last_error}_  ")
        lines.append(f"_Retries used: {b.retry_count}_")
        lines.append("")
        lines.append(f"_Subject (preserved verbatim): `{b.subject_raw}`_")
        lines.append("")
        lines.append("**Original body:**")
        lines.append("")
        lines.append("```")
        lines.append(b.body_raw.rstrip())
        lines.append("```")
        lines.append("")

    return "\n".join(lines)
