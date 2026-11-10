from datetime import datetime, timedelta, timezone

import pytest

from app.workers.channel_recovery import (
    PROCESSING_STALE_MINUTES,
    as_utc,
    is_stale_processing,
    should_scan_recovered_post,
)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def test_as_utc_treats_naive_timestamps_as_utc() -> None:
    naive = datetime(2026, 11, 10, 12, 0)
    assert as_utc(naive) == NOW
    assert as_utc(NOW) == NOW
    assert as_utc(None) is None


@pytest.mark.parametrize("status", ["done", "queued", "waiting", "failed", None])
def test_only_processing_can_be_stale(status: str | None) -> None:
    assert is_stale_processing(status, NOW - timedelta(days=1), NOW) is False


def test_processing_row_without_timestamp_is_stale() -> None:
    assert is_stale_processing("processing", None, NOW) is True


def test_processing_row_is_stale_only_after_the_timeout() -> None:
    fresh = NOW - timedelta(minutes=PROCESSING_STALE_MINUTES - 1)
    old = NOW - timedelta(minutes=PROCESSING_STALE_MINUTES + 1)
    assert is_stale_processing("processing", fresh, NOW) is False
    assert is_stale_processing("processing", old, NOW) is True


def test_unseen_post_is_scanned_when_published_after_the_channel_was_added() -> None:
    assert should_scan_recovered_post(
        status=None,
        next_retry_at=None,
        updated_at=None,
        message_date=NOW - timedelta(hours=1),
        binding_created_at=NOW - timedelta(days=1),
        now=NOW,
    )


def test_unseen_post_from_before_the_channel_was_added_is_skipped() -> None:
    assert not should_scan_recovered_post(
        status=None,
        next_retry_at=None,
        updated_at=None,
        message_date=NOW - timedelta(days=2),
        binding_created_at=NOW - timedelta(days=1),
        now=NOW,
    )


def test_unseen_post_without_known_dates_is_scanned() -> None:
    assert should_scan_recovered_post(
        status=None,
        next_retry_at=None,
        updated_at=None,
        message_date=None,
        binding_created_at=None,
        now=NOW,
    )


def test_naive_binding_date_is_compared_as_utc() -> None:
    assert not should_scan_recovered_post(
        status=None,
        next_retry_at=None,
        updated_at=None,
        message_date=NOW - timedelta(days=2),
        binding_created_at=datetime(2026, 11, 9, 12, 0),
        now=NOW,
    )


def test_done_post_is_never_rescanned() -> None:
    assert not should_scan_recovered_post(
        status="done",
        next_retry_at=None,
        updated_at=NOW,
        message_date=NOW,
        binding_created_at=None,
        now=NOW,
    )


def test_future_retry_time_blocks_the_scan() -> None:
    assert not should_scan_recovered_post(
        status="queued",
        next_retry_at=NOW + timedelta(minutes=5),
        updated_at=NOW,
        message_date=NOW,
        binding_created_at=None,
        now=NOW,
    )
    assert should_scan_recovered_post(
        status="queued",
        next_retry_at=NOW - timedelta(minutes=5),
        updated_at=NOW,
        message_date=NOW,
        binding_created_at=None,
        now=NOW,
    )


def test_live_processing_blocks_but_stale_processing_resumes() -> None:
    assert not should_scan_recovered_post(
        status="processing",
        next_retry_at=None,
        updated_at=NOW - timedelta(minutes=1),
        message_date=NOW,
        binding_created_at=None,
        now=NOW,
    )
    assert should_scan_recovered_post(
        status="processing",
        next_retry_at=None,
        updated_at=NOW - timedelta(minutes=PROCESSING_STALE_MINUTES + 1),
        message_date=NOW,
        binding_created_at=None,
        now=NOW,
    )


def test_waiting_post_is_picked_up_again() -> None:
    assert should_scan_recovered_post(
        status="waiting",
        next_retry_at=None,
        updated_at=NOW - timedelta(hours=1),
        message_date=NOW,
        binding_created_at=None,
        now=NOW,
    )
