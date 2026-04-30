"""Async fetch adapters for WordHippo.

`spider` mode is the production default (WORDHIPPO_MODE=spider).
`direct` mode is a sync fallback for local dev only.

The async SpiderFetcher uses httpx.AsyncClient so it does not block the
FastAPI event loop. Tests mock it with respx.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Protocol

import httpx

from app.config import settings


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


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
    """Async Spider Cloud adapter.

    Uses httpx.AsyncClient — safe to await inside FastAPI request handlers.

    Expected settings:
    - settings.spider_api_key  (SPIDER_API_KEY env var)
    - settings.spider_fetch_url  (SPIDER_FETCH_URL env var, defaults to /scrape)

    Spider /scrape response shape: [{content: "<html>...", url: "..."}]
    The list-wrapping is deliberate — /crawl returned a dict, /scrape returns a list.
    """

    async def fetch(self, word: str) -> str:
        api_key = settings.spider_api_key
        if not api_key:
            raise RuntimeError("SPIDER_API_KEY is not set for spider fetch mode.")
        fetch_url = settings.spider_fetch_url
        encoded = urllib.parse.quote(word.strip().lower())
        target_url = f"https://www.wordhippo.com/what-is/another-word-for/{encoded}.html"
        payload = {"url": target_url}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        async with httpx.AsyncClient(timeout=60) as client:
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
    resolved = mode or settings.wordhippo_mode
    if resolved == "spider":
        return SpiderFetcher()
    raise ValueError(f"Unsupported async fetch mode: {resolved!r}. Use 'spider'.")
