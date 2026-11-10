"""Reconnect backoff for accounts whose Telegram session is not usable yet."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

BASE_DELAY_SECONDS = 5
MAX_DELAY_SECONDS = 300
_MAX_EXPONENT = 6


def next_delay_seconds(failures: int) -> int:
    """5s, 10s, 20s … capped at 300s; ``failures`` counts consecutive failures."""
    safe_failures = max(1, int(failures))
    return min(
        MAX_DELAY_SECONDS,
        BASE_DELAY_SECONDS * 2 ** min(safe_failures - 1, _MAX_EXPONENT),
    )


def reconnect_at(failures: int, *, now: datetime | None = None) -> datetime:
    """Moment after which the next connection attempt is allowed."""
    moment = now or datetime.now(timezone.utc)
    return moment + timedelta(seconds=next_delay_seconds(failures))
