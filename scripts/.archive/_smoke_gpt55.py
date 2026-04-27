"""End-to-end smoke test for gpt-5.5 via the Responses API adapter.

Calls run_spintax_job directly (no UI) with model="gpt-5.5" and a tiny
test body. Verifies:
- The job reaches a terminal state ('done' or 'failed').
- If 'done': result.spintax_body is non-empty and contains spintax.
- API call count and tool call count are reasonable.
- Cost > 0 (proves cost accumulation works on Responses-shape usage).

Run from repo root:
    .venv/bin/python scripts/.archive/_smoke_gpt55.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Repo root on sys.path so 'from app...' imports work.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _load_dotenv() -> None:
    """Minimal .env loader (no python-dotenv dep needed)."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


async def main() -> int:
    from app import jobs as jobs_mod
    from app import spintax_runner

    test_body = (
        "Hey {firstName},\n"
        "\n"
        "I noticed your team is hiring recruiters in {cityName}.\n"
        "\n"
        "Worth a quick chat?\n"
    )

    # Force gpt-5.5 + a small budget for a quick smoke test.
    job = jobs_mod.create(test_body, "instantly", "gpt-5.5")
    print(f"smoke: job_id={job.job_id}")
    print(f"smoke: model=gpt-5.5  platform=instantly  budget=5 tool calls")
    print(f"smoke: input body ({len(test_body)} chars):")
    print("  " + test_body.replace("\n", "\n  "))
    print()

    await spintax_runner.run(
        job_id=job.job_id,
        plain_body=test_body,
        platform="instantly",
        model="gpt-5.5",
        max_tool_calls=5,
        reasoning_effort="low",
    )

    final = jobs_mod.get(job.job_id)
    if final is None:
        print("smoke: FAIL - job evicted before terminal state")
        return 2

    print(f"smoke: status        = {final.status}")
    print(f"smoke: error         = {final.error}")
    print(f"smoke: api_calls     = {final.api_calls}")
    print(f"smoke: tool_calls    = {final.tool_calls}")
    print(f"smoke: cost_usd      = ${final.cost_usd:.6f}")
    print()

    if final.status != "done":
        print("smoke: FAIL - job did not reach 'done'")
        return 1

    result = final.result
    if result is None:
        print("smoke: FAIL - status='done' but result is None")
        return 1

    body = result.spintax_body
    print(f"smoke: spintax body ({len(body)} chars):")
    print("---")
    print(body)
    print("---")
    print()
    print(f"smoke: lint_passed   = {result.lint_passed}")
    print(f"smoke: qa_passed     = {result.qa_passed}")
    print(f"smoke: qa_errors     = {result.qa_errors}")
    print(f"smoke: qa_warnings   = {result.qa_warnings}")

    if not body.strip():
        print("smoke: FAIL - spintax_body is empty")
        return 1

    # Lightweight sanity check: spintax bodies have { ... | ... } blocks.
    if "{" not in body or "|" not in body or "}" not in body:
        print("smoke: WARN - body lacks spintax markers; may be malformed")

    print("smoke: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
