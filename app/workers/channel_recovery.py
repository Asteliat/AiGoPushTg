"""Recovery decisions for the channel monitor.

The monitor must revisit recent channel history after a restart without
re-processing posts it already handled. These rules are pure functions so the
retry/staleness matrix can be tested without Telegram or PostgreSQL.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

PROCESSING_STALE_MINUTES = 20


def as_utc(moment: datetime | None) -> datetime | None:
    """Treat naive timestamps (possible with SQLite/legacy rows) as UTC."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def is_stale_processing(
    status: str | None,
    updated_at: datetime | None,
    now: datetime,
    *,
    stale_minutes: int = PROCESSING_STALE_MINUTES,
) -> bool:
    """A ``processing`` row is only re-enterable once its owner is considered dead."""
    if status != "processing":
        return False
    updated = as_utc(updated_at)
    if updated is None:
        return True
    return updated <= now - timedelta(minutes=stale_minutes)


def should_scan_recovered_post(
    *,
    status: str | None,
    next_retry_at: datetime | None,
    updated_at: datetime | None,
    message_date: datetime | None,
    binding_created_at: datetime | None,
    now: datetime,
    stale_minutes: int = PROCESSING_STALE_MINUTES,
) -> bool:
    """Decide whether a post found during recovery still needs processing.

    ``status`` is ``None`` for a post that was never seen by this account.
    """
    if status is None:
        created = as_utc(binding_created_at)
        posted = as_utc(message_date)
        # Never backfill posts published before the channel was added.
        return not (created and posted and posted < created)
    if status == "done":
        return False
    retry_at = as_utc(next_retry_at)
    if retry_at is not None and retry_at > now:
        return False
    if is_stale_processing(status, updated_at, now, stale_minutes=stale_minutes):
        return True
    return status != "processing"
