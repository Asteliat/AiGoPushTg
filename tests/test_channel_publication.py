import pytest

from app.workers import redis_keys
from app.workers.channel_publication import (
    PUBLICATION_STATUS_TEXT,
    first_defined,
    publication_status_text,
)


@pytest.mark.parametrize("status", sorted(PUBLICATION_STATUS_TEXT))
def test_every_publication_status_has_an_explanation(status: str) -> None:
    text = publication_status_text(status)

    assert text and text != publication_status_text("unknown-status")
    assert text[0].isupper()


def test_unknown_status_falls_back_to_a_generic_sentence() -> None:
    assert publication_status_text("legacy") == publication_status_text("whatever")


def test_first_defined_prefers_the_channel_override() -> None:
    assert first_defined(5, 10, 20) == 5
    assert first_defined(None, 10, 20) == 10
    assert first_defined(None, None, 20) == 20
    assert first_defined(None, None, None) is None


def test_zero_override_is_not_treated_as_missing() -> None:
    assert first_defined(0, 10, 20) == 0


def test_channel_redis_keys_are_scoped_per_account_and_channel() -> None:
    assert redis_keys.channel_post(7, 500, 42) == "atcivperson:channel-post:7:500:42"
    assert redis_keys.channel_cooldown_lock(7, 500) == "atcivperson:channel-cooldown-lock:7:500"
    assert (
        redis_keys.channel_reply_cooldown(7, 500)
        == "atcivperson:channel-reply-cooldown:7:500"
    )
    assert redis_keys.channel_post(8, 500, 42) != redis_keys.channel_post(7, 500, 42)
