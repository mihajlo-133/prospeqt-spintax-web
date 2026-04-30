"""Daily USD spend cap tracker.

What this does:
    Tracks total USD spent on OpenAI calls today, in process memory,
    and enforces the daily cap configured by DAILY_SPEND_CAP_USD env var.

    State auto-resets at midnight UTC: when check_cap() or add_cost() is
    called and the stored date is not today, the counter is zeroed first.

What it depends on:
    - Python stdlib (datetime, threading)
    - app.config.settings for daily_spend_cap_usd

What depends on it:
    - app/routes/spintax.py calls check_cap() before creating a job
    - app/spintax_runner.py calls add_cost() after each API call totals

Design choices:
    - In-process dict, NOT Redis. Single-worker Render free tier.
    - Date string ('2026-04-26'), NOT timestamp. Human-readable in logs.
    - threading.Lock around all state mutations. Mandatory.
    - Test helper _reset_for_test(spent_usd, date_override) is module-private
      but exposed for unit tests.

Concurrency:
    EVERY read or write to _state goes through _lock. Same discipline as
    app.jobs.
"""

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from app.config import settings

# Module-private state. Use _reset_for_test() for tests.
_state: dict[str, Any] = {"date": "", "spent_usd": 0.0}
_lock = threading.Lock()


def _today_utc_str() -> str:
    """UTC date as ISO string ('2026-04-26'). Single source of truth."""
    return datetime.now(tz=timezone.utc).date().isoformat()


def _next_midnight_utc() -> datetime:
    """The next midnight UTC datetime (when the daily counter resets)."""
    now = datetime.now(tz=timezone.utc)
    tomorrow = now.date() + timedelta(days=1)
    return datetime(
        year=tomorrow.year,
        month=tomorrow.month,
        day=tomorrow.day,
        tzinfo=timezone.utc,
    )


def _maybe_reset_locked() -> None:
    """If the stored date is not today (UTC), zero the counter.

    Caller must hold _lock.
    """
    today = _today_utc_str()
    if _state["date"] != today:
        _state["date"] = today
        _state["spent_usd"] = 0.0


def get_spent_today() -> float:
    """Return USD spent today (after applying the midnight reset).

    Thread-safe.
    """
    with _lock:
        _maybe_reset_locked()
        return float(_state["spent_usd"])


def add_cost(amount_usd: float) -> float:
    """Add `amount_usd` to today's spend counter and return the new total.

    Thread-safe. Auto-applies the midnight reset before adding.
    """
    if amount_usd < 0:
        raise ValueError(f"amount_usd must be >= 0, got {amount_usd}")
    with _lock:
        _maybe_reset_locked()
        _state["spent_usd"] = float(_state["spent_usd"]) + float(amount_usd)
        return float(_state["spent_usd"])


def check_cap() -> None:
    """Raise HTTPException(429) if the daily cap is at or over the limit.

    Thread-safe. Uses settings.daily_spend_cap_usd as the threshold.

    Raises:
        HTTPException: status_code=429 with detail envelope:
            {
              "error": "daily_cap_hit",
              "cap_usd": <float>,
              "spent_usd": <float>,
              "resets_at": "<iso8601 utc>",
            }
    """
    cap = float(settings.daily_spend_cap_usd)
    spent = get_spent_today()
    if spent >= cap:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "daily_cap_hit",
                "cap_usd": cap,
                "spent_usd": spent,
                "resets_at": _next_midnight_utc().isoformat().replace("+00:00", "Z"),
            },
        )


# ---------------------------------------------------------------------------
# Test helpers (module-private, but exposed for unit tests).
# ---------------------------------------------------------------------------


def _reset_for_test(
    spent_usd: float = 0.0,
    date_override: str | None = None,
) -> None:
    """Reset internal state. For tests only.

    Args:
        spent_usd: amount to seed into the counter.
        date_override: 'today', 'yesterday', or None.
            None => uses today's UTC date.
            'today' => same as None.
            'yesterday' => date one day before today (forces a reset
                on the next check_cap()/add_cost() call).
    """
    today = _today_utc_str()
    if date_override == "yesterday":
        d = (datetime.now(tz=timezone.utc).date() - timedelta(days=1)).isoformat()
    else:
        d = today
    with _lock:
        _state["date"] = d
        _state["spent_usd"] = float(spent_usd)
