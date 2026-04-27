#!/usr/bin/env python3
"""
spintax_cli.py — call the Prospeqt Spintax Batch API from Claude Code or the terminal.

USAGE
=====

    # Single email — paste body via stdin or pass --md-file
    python3 scripts/spintax_cli.py /path/to/email_or_doc.md

    # Pick a target platform / model / concurrency
    python3 scripts/spintax_cli.py /path/to/doc.md \\
        --platform instantly \\
        --model o3 \\
        --concurrency 5

    # Dry-run only (parse and print structure, don't fire OpenAI jobs)
    python3 scripts/spintax_cli.py /path/to/doc.md --dry-run

    # Save the .zip somewhere specific (default: same dir as input file)
    python3 scripts/spintax_cli.py /path/to/doc.md --output /tmp/result.zip

ENVIRONMENT
===========

The script reads two env vars (or you can pass them as flags):

    SPINTAX_API_URL   — base URL of the API.  Default: http://localhost:8086
    SPINTAX_API_KEY   — the bearer token (BATCH_API_KEY value on the server).

Example (zsh / bash):

    export SPINTAX_API_URL="https://prospeqt-spintax.onrender.com"
    export SPINTAX_API_KEY="sk_batch_..."
    python3 scripts/spintax_cli.py ~/Downloads/Enavra\\ \\(2\\).md

CLAUDE CODE WORKFLOW
====================

Drop this script into your local clone of the repo or anywhere on disk.
In Claude Code, ask:

    "spintax this file using the Prospeqt API: ~/Downloads/Enavra (2).md"

Claude will run:

    python3 ~/path/to/spintax_cli.py "~/Downloads/Enavra (2).md"

…and return the .zip path when done. The script prints progress every poll
so Claude can show you live updates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = os.environ.get("SPINTAX_API_URL", "http://localhost:8086")
POLL_INTERVAL_SEC = 5.0
PARSE_TIMEOUT_SEC = 600  # 10min — large HeyReach docs can take 3-5 min
RUN_TIMEOUT_SEC = 1800   # 30min — full HeyReach with 5 concurrent o3 jobs


# ---------------------------------------------------------------------------
# Tiny HTTP helpers (urllib only — keep stdlib-only)
# ---------------------------------------------------------------------------


def _http(
    method: str,
    url: str,
    api_key: str,
    body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> tuple[int, bytes, dict[str, str]]:
    """Run an HTTP request. Returns (status, body_bytes, headers)."""
    data: bytes | None = None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def _http_json(method: str, url: str, api_key: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    status, content, _ = _http(method, url, api_key, body, timeout=PARSE_TIMEOUT_SEC)
    try:
        decoded = json.loads(content.decode("utf-8"))
    except json.JSONDecodeError:
        sys.stderr.write(f"ERROR: non-JSON response from {url} (HTTP {status})\n")
        sys.stderr.write(content.decode("utf-8", errors="replace")[:500] + "\n")
        sys.exit(1)
    if status >= 400:
        sys.stderr.write(f"ERROR: HTTP {status} from {url}\n")
        sys.stderr.write(json.dumps(decoded, indent=2) + "\n")
        sys.exit(1)
    return decoded


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


def submit_batch(
    base_url: str,
    api_key: str,
    md: str,
    platform: str,
    model: str,
    concurrency: int,
    dry_run: bool,
) -> dict[str, Any]:
    """POST /api/spintax/batch and return the response dict."""
    return _http_json(
        "POST",
        f"{base_url}/api/spintax/batch",
        api_key,
        body={
            "md": md,
            "platform": platform,
            "model": model,
            "concurrency": concurrency,
            "dry_run": dry_run,
        },
    )


def poll_batch(base_url: str, api_key: str, batch_id: str) -> dict[str, Any]:
    """GET /api/spintax/batch/{id}."""
    return _http_json("GET", f"{base_url}/api/spintax/batch/{batch_id}", api_key)


def download_zip(
    base_url: str,
    api_key: str,
    batch_id: str,
    out_path: Path,
) -> int:
    """GET .zip → write to disk. Returns bytes written."""
    status, content, headers = _http(
        "GET",
        f"{base_url}/api/spintax/batch/{batch_id}/download",
        api_key,
        timeout=120.0,
    )
    if status != 200:
        sys.stderr.write(f"ERROR: download HTTP {status}\n")
        sys.stderr.write(content.decode("utf-8", errors="replace")[:500] + "\n")
        sys.exit(1)
    out_path.write_bytes(content)
    return len(content)


def fmt_duration(sec: float) -> str:
    if sec < 60:
        return f"{sec:.0f}s"
    m = int(sec // 60)
    s = int(sec - m * 60)
    return f"{m}m {s:02d}s"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Call the Prospeqt Spintax Batch API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("md_file", type=Path, help="Markdown file to spintax.")
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"API base URL (default: {DEFAULT_URL}, or $SPINTAX_API_URL).",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("SPINTAX_API_KEY", ""),
        help="Bearer token (default: $SPINTAX_API_KEY).",
    )
    parser.add_argument(
        "--platform",
        choices=["instantly", "emailbison"],
        default="instantly",
    )
    parser.add_argument("--model", default="o3", help="OpenAI model (default: o3).")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=5,
        help="Parallel spintax jobs (1-20, default 5).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse only, print structure, don't fire OpenAI jobs.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Where to save the .zip. Default: alongside the input .md.",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip the 'parsed N segments. proceed?' prompt — useful for scripts.",
    )
    args = parser.parse_args()

    if not args.api_key:
        sys.stderr.write(
            "ERROR: bearer token required. Set SPINTAX_API_KEY env var or "
            "pass --api-key.\n"
        )
        return 2

    if not args.md_file.exists():
        sys.stderr.write(f"ERROR: file not found: {args.md_file}\n")
        return 2

    md = args.md_file.read_text(encoding="utf-8")
    if not md.strip():
        sys.stderr.write(f"ERROR: file is empty: {args.md_file}\n")
        return 2

    print(f"[1/4] Loading {args.md_file.name} ({len(md):,} chars)")

    # ── Step 1: dry-run parse ──────────────────────────────────────────────
    print(f"[2/4] Parsing with o4-mini... (this takes 30s-3min depending on doc size)")
    t0 = time.monotonic()
    parsed_resp = submit_batch(
        args.url,
        args.api_key,
        md,
        args.platform,
        args.model,
        args.concurrency,
        dry_run=True,
    )
    parsed = parsed_resp["parsed"]
    n_seg = len(parsed["segments"])
    n_bodies = parsed["total_bodies"]
    n_to_spin = parsed.get("total_bodies_to_spin", n_bodies)
    print(
        f"        parsed in {fmt_duration(time.monotonic() - t0)}: "
        f"{n_seg} segments, {n_bodies} bodies "
        f"({n_to_spin} to spin, {n_bodies - n_to_spin} Email 2 pass through)"
    )
    if parsed.get("warnings"):
        print("        WARNINGS:")
        for w in parsed["warnings"][:5]:
            print(f"          - {w}")

    if args.dry_run:
        print("\n[done] dry-run requested. Exiting without firing jobs.")
        print(json.dumps(parsed, indent=2))
        return 0

    # Optional confirm
    if not args.no_confirm and sys.stdin.isatty():
        ans = input(
            f"\nProceed to spin {n_to_spin} bodies on {args.model} "
            f"({args.concurrency} concurrent)? [y/N] "
        ).strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 0

    # ── Step 2: real run ───────────────────────────────────────────────────
    print(f"\n[3/4] Firing batch (concurrency={args.concurrency})...")
    submit_resp = submit_batch(
        args.url,
        args.api_key,
        md,
        args.platform,
        args.model,
        args.concurrency,
        dry_run=False,
    )
    batch_id = submit_resp["batch_id"]
    print(f"        batch_id: {batch_id}")

    # ── Step 3: poll ───────────────────────────────────────────────────────
    poll_start = time.monotonic()
    last_completed = -1
    while True:
        if time.monotonic() - poll_start > RUN_TIMEOUT_SEC:
            sys.stderr.write(
                f"\nERROR: batch did not finish within {RUN_TIMEOUT_SEC}s.\n"
            )
            return 1
        time.sleep(POLL_INTERVAL_SEC)
        status = poll_batch(args.url, args.api_key, batch_id)
        if status["completed"] != last_completed or status["status"] != "running":
            done_n = status["completed"]
            fail_n = status["failed"]
            running = status["in_progress"]
            elapsed = status["elapsed_sec"]
            cost = status["cost_usd_so_far"]
            print(
                f"        {fmt_duration(elapsed):>7} | {done_n}/{status['total']} done "
                f"({fail_n} failed, {running} running) | ${cost:.2f} spent"
            )
            last_completed = done_n
        if status["status"] in ("done", "failed", "cancelled"):
            break

    if status["status"] != "done":
        sys.stderr.write(
            f"\nERROR: batch ended in status={status['status']!r} "
            f"(reason={status.get('failure_reason')!r})\n"
        )
        return 1

    # ── Step 4: download ───────────────────────────────────────────────────
    if args.output:
        out_path = args.output
    else:
        ts = time.strftime("%Y-%m-%d_%H%M%S")
        out_path = args.md_file.parent / f"spintax_{args.md_file.stem}_{ts}.zip"

    print(f"\n[4/4] Downloading .zip → {out_path}")
    n_bytes = download_zip(args.url, args.api_key, batch_id, out_path)
    print(
        f"        {n_bytes:,} bytes  |  total cost ${status['cost_usd_so_far']:.2f}  "
        f"|  total time {fmt_duration(status['elapsed_sec'])}"
    )

    print(f"\n[done] zip saved: {out_path}")
    print(f"        unzip -l {str(out_path).replace(' ', chr(92) + ' ')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
