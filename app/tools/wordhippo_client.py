"""Async fetch adapters for WordHippo.

`spider` mode is the production default (WORDHIPPO_MODE=spider).
`direct` mode is a sync fallback for local dev only.

The async SpiderFetcher uses a module-level lazily-initialized
`httpx.AsyncClient` singleton so connections pool across concurrent
tool calls inside one agent loop. The FastAPI lifespan hook in
`app/main.py` calls `close_fetchers()` on shutdown to release the
underlying TCP connections cleanly.

Tests mock the network with respx and can call `_reset_for_tests()`
to drop the singleton between test cases.
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from typing import Protocol

import httpx

from app import config


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Shared httpx.AsyncClient singleton
#
# Connection-pool sized for fan-out: a single agent loop can fire 5+
# wordhippo_lookup calls in parallel. 20 max conns / 10 keep-alive gives
# headroom without exhausting file descriptors if Spider gets slow.
# ---------------------------------------------------------------------------

_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0)
_HTTP_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

_client: httpx.AsyncClient | None = None
_client_lock = asyncio.Lock()


async def _get_async_client() -> httpx.AsyncClient:
    """Return the shared httpx.AsyncClient, creating it on first use.

    Double-checked locking guards against races where two coroutines
    initialize the singleton simultaneously during cold-start fan-out.
    """
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            _client = httpx.AsyncClient(
                timeout=_HTTP_TIMEOUT,
                limits=_HTTP_LIMITS,
            )
    return _client


async def close_fetchers() -> None:
    """Close the shared httpx client. Wired into FastAPI's lifespan hook."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _reset_for_tests() -> None:
    """Drop the singleton without awaiting close. Tests use respx mocks."""
    global _client
    _client = None


class AsyncFetcher(Protocol):
    async def fetch(self, word: str) -> str: ...


class DirectFetcher:
    """Synchronous fallback using urllib. Not suitable for async call paths."""

    def fetch(self, word: str) -> str:
        encoded = urllib.parse.quote(word.strip().lower())
        url = f"https://www.wordhippo.com/what-is/another-word-for/{encoded}.html"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read().decode("utf-8", "ignore")


class SpiderFetcher:
    """Async Spider Cloud adapter using the shared httpx client.

    Expected settings:
    - settings.spider_api_key  (SPIDER_API_KEY env var)
    - settings.spider_fetch_url  (SPIDER_FETCH_URL env var, defaults to /scrape)

    Spider /scrape response shape: [{content: "<html>...", url: "..."}]
    The list-wrapping is deliberate — /crawl returned a dict, /scrape returns a list.
    """

    async def fetch(self, word: str) -> str:
        api_key = config.settings.spider_api_key
        if not api_key:
            raise RuntimeError("SPIDER_API_KEY is not set for spider fetch mode.")
        fetch_url = config.settings.spider_fetch_url
        encoded = urllib.parse.quote(word.strip().lower())
        target_url = f"https://www.wordhippo.com/what-is/another-word-for/{encoded}.html"
        payload = {"url": target_url}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        client = await _get_async_client()
        response = await client.post(fetch_url, json=payload, headers=headers)
        response.raise_for_status()
        raw = response.text
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Spider response was not valid JSON.") from exc
        # Spider /scrape returns a list; unwrap the first element.
        # /crawl returns a dict — defensive isinstance check supports both.
        item = parsed[0] if isinstance(parsed, list) and parsed else parsed
        if not isinstance(item, dict):
            return ""
        # Empty-string fallback (not RuntimeError): Spider sometimes returns
        # a result envelope with empty/missing content (page hit but no body
        # extracted). Returning "" lets `parse_wordhippo_sections` produce
        # zero definitions + a warning — the agent tool surfaces that as a
        # structured warning rather than crashing the runner mid-tool-call.
        return item.get("content") or item.get("html") or ""


async def get_fetcher_async(mode: str | None = None) -> AsyncFetcher:
    """Return the appropriate async fetcher based on WORDHIPPO_MODE setting.

    mode=None reads settings.wordhippo_mode (default: 'spider').
    """
    resolved = mode or config.settings.wordhippo_mode
    if resolved == "spider":
        return SpiderFetcher()
    raise ValueError(f"Unsupported async fetch mode: {resolved!r}. Use 'spider'.")
