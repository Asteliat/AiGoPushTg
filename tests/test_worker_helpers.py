from datetime import datetime, timedelta, timezone

import pytest

from app.workers import redis_keys
from app.workers.reconnect import MAX_DELAY_SECONDS, next_delay_seconds, reconnect_at


@pytest.mark.parametrize(
    ("failures", "expected"),
    [
        (1, 5),
        (2, 10),
        (3, 20),
        (4, 40),
        (5, 80),
        (6, 160),
        (7, 300),
        (8, 300),
        (25, 300),
    ],
)
def test_next_delay_seconds_backoff(failures: int, expected: int) -> None:
    assert next_delay_seconds(failures) == expected


def test_next_delay_seconds_clamps_non_positive_failures() -> None:
    assert next_delay_seconds(0) == 5
    assert next_delay_seconds(-3) == 5


def test_reconnect_at_uses_the_backoff() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert reconnect_at(3, now=now) == now + timedelta(seconds=20)
    assert reconnect_at(99, now=now) == now + timedelta(seconds=MAX_DELAY_SECONDS)


def test_session_lease_key_is_stable() -> None:
    assert redis_keys.session_lease(7) == "aigopushtg:telegram-session:7"


def test_memory_keys_differ_per_person() -> None:
    chat = redis_keys.chat_memory(1, -100)
    first = redis_keys.person_memory(1, -100, 10)
    second = redis_keys.person_memory(1, -100, 11)
    assert chat != first and first != second


def test_challenge_keys_are_scoped_per_account_and_chat() -> None:
    assert redis_keys.challenge_seen(1, -100, 42) == "aigopushtg:challenge:seen:1:-100:42"
    assert redis_keys.challenge_cooldown(1, -100) == "aigopushtg:challenge:cooldown:1:-100"
    assert redis_keys.challenge_seen(1, -100, 42) != redis_keys.challenge_seen(2, -100, 42)


def test_learning_buffer_key_uses_start_timestamp() -> None:
    assert redis_keys.learning_buffer(5, 1700000000) == "aigopushtg:learning:5:1700000000"


def test_cas_scripts_only_touch_matching_tokens() -> None:
    assert "redis.call('get', KEYS[1]) == ARGV[1]" in redis_keys.CAS_DELETE
    assert "redis.call('del'" in redis_keys.CAS_DELETE
    assert "redis.call('expire', KEYS[1], ARGV[2])" in redis_keys.CAS_EXPIRE
    assert redis_keys.SESSION_LEASE_TTL_SECONDS == 90
    assert redis_keys.SESSION_LEASE_RENEW_SECONDS < redis_keys.SESSION_LEASE_TTL_SECONDS
