#!/usr/bin/env python3
"""Benchmark the beta block-first pipeline against the corpus fixtures.

Runs the live beta_v1 pipeline (real OpenAI calls) on every YAML fixture
in ``tests/pipeline/fixtures/`` and reports per-email pass/fail,
latency, retries, total LLM calls, and approximate cost.

Alpha pipeline comparison is intentionally NOT included in v1: the alpha
``app.spintax_runner.run()`` writes results into the job-state system
rather than returning them, so direct programmatic invocation requires
mocking that infrastructure. Once the beta-v1 entrypoint
(``app.spintax_runner_v2.run``) lands in task #18 and exposes a clean
callable interface, this script will grow an ``--alpha`` mode that
mirrors it for both pipelines. Until then the actual A/B comparison
happens via teammate testing in the web UI (task #24).

USAGE
=====

    # Run all fixtures, print Markdown report to stdout
    python scripts/benchmark.py

    # Single fixture
    python scripts/benchmark.py --fixture continuum_finops

    # Save JSON report
    python scripts/benchmark.py --output benchmark_report.json --format json

    # Use cheaper models for cost-sensitive runs
    python scripts/benchmark.py --spintaxer-model gpt-5-mini

ENVIRONMENT
===========

Requires ``OPENAI_API_KEY``. The script imports ``app.config.settings``
which reads the key from the env or ``.env`` via pydantic-settings.

EXIT CODES
==========

* 0 - all fixtures passed validators
* 1 - one or more fixtures failed
* 2 - script error (missing fixture, bad config, etc.)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allow running this script directly without PYTHONPATH=. set.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import yaml
except ImportError:
    print(
        "error: PyYAML is required. Install with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)

from app.pipeline.contracts import PipelineStageError  # noqa: E402
from app.pipeline.pipeline_runner import run_pipeline  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "tests" / "pipeline" / "fixtures"

# Conservative price table for the OpenAI gpt-5.x family. Prices are
# per 1k tokens. Update as the public price list changes.
# Source: https://openai.com/api/pricing/ (as of 2026-04, indicative).
_PRICES: dict[str, tuple[float, float]] = {
    # model: (input $/1k, output $/1k)
    "gpt-5": (0.005, 0.015),
    "gpt-5-mini": (0.001, 0.004),
    "gpt-5-nano": (0.0002, 0.0008),
}


@dataclass
class StageUsage:
    """Aggregated token + cost stats for one pipeline run."""

    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class FixtureResult:
    fixture_id: str
    client: str
    passed: bool
    error_key: str | None = None
    error_detail: str | None = None
    latency_seconds: float = 0.0
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    blocks_completed: int = 0
    blocks_retried: int = 0
    max_retries: int = 0
    spintax_length: int = 0
    notes: list[str] = field(default_factory=list)


def _load_fixtures(fixture_filter: str | None) -> list[dict[str, Any]]:
    """Load all YAML fixtures matching the optional filter."""
    if not FIXTURES_DIR.is_dir():
        raise FileNotFoundError(f"fixtures dir not found: {FIXTURES_DIR}")

    yaml_files = sorted(FIXTURES_DIR.glob("*.yaml"))
    if not yaml_files:
        raise FileNotFoundError(f"no .yaml fixtures found in {FIXTURES_DIR}")

    fixtures: list[dict[str, Any]] = []
    for path in yaml_files:
        with path.open() as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict) or "email_id" not in data:
            print(f"warning: skipping malformed fixture: {path.name}", file=sys.stderr)
            continue
        if fixture_filter and data["email_id"] != fixture_filter:
            continue
        fixtures.append(data)

    if fixture_filter and not fixtures:
        raise FileNotFoundError(
            f"no fixture matched email_id={fixture_filter!r} in {FIXTURES_DIR}"
        )
    return fixtures


def _make_usage_callback(
    model_by_call: list[str],
    usage: StageUsage,
):
    """Return an on_api_call callback that accumulates token usage and cost.

    The orchestrator passes a single callback through every stage, so we
    cannot distinguish models per call from inside the callback alone.
    The caller pre-builds ``model_by_call`` is left unused in this v1
    implementation; all calls are charged at the spintaxer's price as a
    pessimistic upper bound. Refine in v2 once the orchestrator threads
    per-stage model names into its on_api_call signature.
    """

    def _on_api_call(usage_obj: Any) -> None:
        if usage_obj is None:
            return
        # Responses-API usage objects expose .input_tokens / .output_tokens.
        # Be defensive: fall back to dict access if the SDK shape changes.
        in_tokens = (
            getattr(usage_obj, "input_tokens", None)
            or getattr(usage_obj, "prompt_tokens", None)
            or 0
        )
        out_tokens = (
            getattr(usage_obj, "output_tokens", None)
            or getattr(usage_obj, "completion_tokens", None)
            or 0
        )
        usage.api_calls += 1
        usage.input_tokens += in_tokens
        usage.output_tokens += out_tokens

    return _on_api_call


def _compute_cost(usage: StageUsage, default_model: str) -> float:
    """Compute approximate cost in USD using the price for ``default_model``.

    This is a coarse upper-bound: the splitter / profiler / pool stages
    use a cheaper model than the spintaxer in the default config, but
    the callback can't tell us which model fired a given call. Pricing
    everything at the spintaxer's rate over-estimates total cost slightly
    (good for budget planning, conservative for cost-comparison tables).
    """
    rates = _PRICES.get(default_model)
    if rates is None:
        return 0.0
    in_rate, out_rate = rates
    return (
        (usage.input_tokens / 1000.0) * in_rate
        + (usage.output_tokens / 1000.0) * out_rate
    )


async def _run_one_fixture(
    fixture: dict[str, Any],
    *,
    splitter_model: str,
    profiler_model: str,
    pool_model: str,
    spintaxer_model: str,
    spintaxer_reasoning: str,
    max_retries_per_block: int,
) -> FixtureResult:
    """Run the beta pipeline against one fixture and return a result row."""
    fixture_id = fixture["email_id"]
    client = fixture.get("client", "unknown")
    plain_body = fixture["plain_body"]

    usage = StageUsage()
    callback = _make_usage_callback([], usage)

    t_start = time.perf_counter()
    try:
        assembled, diag = await run_pipeline(
            plain_body,
            splitter_model=splitter_model,
            profiler_model=profiler_model,
            pool_model=pool_model,
            spintaxer_model=spintaxer_model,
            spintaxer_reasoning=spintaxer_reasoning,
            max_retries_per_block=max_retries_per_block,
            on_api_call=callback,
        )
    except PipelineStageError as exc:
        elapsed = time.perf_counter() - t_start
        return FixtureResult(
            fixture_id=fixture_id,
            client=client,
            passed=False,
            error_key=exc.error_key,
            error_detail=str(exc.detail),
            latency_seconds=elapsed,
            api_calls=usage.api_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=_compute_cost(usage, spintaxer_model),
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = time.perf_counter() - t_start
        return FixtureResult(
            fixture_id=fixture_id,
            client=client,
            passed=False,
            error_key="unexpected_exception",
            error_detail=f"{type(exc).__name__}: {exc}",
            latency_seconds=elapsed,
            api_calls=usage.api_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=_compute_cost(usage, spintaxer_model),
        )

    elapsed = time.perf_counter() - t_start

    notes: list[str] = []
    expected_block_count = fixture.get("expected_block_count")
    if expected_block_count is not None and diag.splitter.block_count != expected_block_count:
        notes.append(
            f"block_count mismatch: actual={diag.splitter.block_count}, "
            f"expected={expected_block_count}"
        )

    return FixtureResult(
        fixture_id=fixture_id,
        client=client,
        passed=True,
        latency_seconds=elapsed,
        api_calls=usage.api_calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=_compute_cost(usage, spintaxer_model),
        blocks_completed=diag.block_spintaxer.blocks_completed,
        blocks_retried=diag.block_spintaxer.blocks_retried,
        max_retries=diag.block_spintaxer.max_retries_per_block,
        spintax_length=len(assembled.spintax),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _render_markdown(results: list[FixtureResult], spintaxer_model: str) -> str:
    """Render results as a Markdown table + totals."""
    total_cost = sum(r.cost_usd for r in results)
    total_calls = sum(r.api_calls for r in results)
    total_pass = sum(1 for r in results if r.passed)
    total_fail = len(results) - total_pass

    lines: list[str] = []
    lines.append("# Beta Pipeline Benchmark Report")
    lines.append("")
    lines.append(f"- Fixtures: **{len(results)}** ({total_pass} passed, {total_fail} failed)")
    lines.append(f"- Total LLM calls: **{total_calls}**")
    lines.append(f"- Approx total cost (priced at {spintaxer_model}): **${total_cost:.4f}**")
    lines.append("")
    lines.append(
        "| fixture | client | status | latency | calls | retries | tokens (in/out) | cost | notes |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|"
    )
    for r in results:
        status = "pass" if r.passed else f"FAIL ({r.error_key})"
        notes_cell = "; ".join(r.notes) if r.notes else (r.error_detail or "")
        lines.append(
            f"| {r.fixture_id} | {r.client} | {status} | "
            f"{r.latency_seconds:.2f}s | {r.api_calls} | "
            f"{r.blocks_retried}/{r.max_retries}max | "
            f"{r.input_tokens}/{r.output_tokens} | "
            f"${r.cost_usd:.4f} | {notes_cell} |"
        )
    return "\n".join(lines) + "\n"


def _render_json(results: list[FixtureResult], spintaxer_model: str) -> str:
    payload = {
        "spintaxer_model": spintaxer_model,
        "totals": {
            "fixture_count": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
            "total_api_calls": sum(r.api_calls for r in results),
            "total_cost_usd": sum(r.cost_usd for r in results),
        },
        "fixtures": [
            {
                "fixture_id": r.fixture_id,
                "client": r.client,
                "passed": r.passed,
                "error_key": r.error_key,
                "error_detail": r.error_detail,
                "latency_seconds": round(r.latency_seconds, 3),
                "api_calls": r.api_calls,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": round(r.cost_usd, 6),
                "blocks_completed": r.blocks_completed,
                "blocks_retried": r.blocks_retried,
                "max_retries": r.max_retries,
                "spintax_length": r.spintax_length,
                "notes": r.notes,
            }
            for r in results
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Benchmark the beta block-first pipeline."
    )
    p.add_argument("--fixture", help="Run only the fixture with this email_id")
    p.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format (default: markdown)",
    )
    p.add_argument(
        "--output",
        help="Write report to this path (default: stdout)",
    )
    p.add_argument("--splitter-model", default="gpt-5-mini")
    p.add_argument("--profiler-model", default="gpt-5-mini")
    p.add_argument("--pool-model", default="gpt-5-mini")
    p.add_argument("--spintaxer-model", default="gpt-5")
    p.add_argument(
        "--spintaxer-reasoning",
        choices=("low", "medium", "high"),
        default="high",
    )
    p.add_argument("--max-retries-per-block", type=int, default=2)
    p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of fixtures to run in parallel (default: 1, sequential)",
    )
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    fixtures = _load_fixtures(args.fixture)
    print(
        f"Running {len(fixtures)} fixture(s) with spintaxer={args.spintaxer_model} "
        f"(reasoning={args.spintaxer_reasoning})...",
        file=sys.stderr,
    )

    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def _bounded(fx: dict[str, Any]) -> FixtureResult:
        async with sem:
            print(f"  > {fx['email_id']}", file=sys.stderr)
            return await _run_one_fixture(
                fx,
                splitter_model=args.splitter_model,
                profiler_model=args.profiler_model,
                pool_model=args.pool_model,
                spintaxer_model=args.spintaxer_model,
                spintaxer_reasoning=args.spintaxer_reasoning,
                max_retries_per_block=args.max_retries_per_block,
            )

    results = await asyncio.gather(*[_bounded(fx) for fx in fixtures])

    if args.format == "json":
        text = _render_json(results, args.spintaxer_model)
    else:
        text = _render_markdown(results, args.spintaxer_model)

    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)

    failed = sum(1 for r in results if not r.passed)
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    try:
        return asyncio.run(_amain(args))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
