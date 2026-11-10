import pytest

from app.telegram_gateway.discovery import PROVIDER_POOL, ProviderPool


@pytest.fixture(autouse=True)
def clean_pool():
    PROVIDER_POOL.reset()
    yield
    PROVIDER_POOL.reset()


def test_pools_do_not_share_penalties() -> None:
    first, second = ProviderPool(), ProviderPool()

    first.record("Google", "captcha")

    assert first.available("Google") is False
    assert second.available("Google") is True


def test_ok_status_clears_penalties() -> None:
    pool = ProviderPool()
    pool.record("Google", "blocked")
    assert pool.available("Google") is False

    pool.record("Google", "ok")

    assert pool.available("Google") is True


@pytest.mark.parametrize("status", ["captcha", "blocked", "rate_limited"])
def test_repeated_failures_grow_the_cooldown(status: str) -> None:
    pool = ProviderPool()
    pool.record("Yandex", status)
    first = pool._cooldowns["Yandex"]
    pool.record("Yandex", status)
    second = pool._cooldowns["Yandex"]

    assert second > first


def test_unknown_status_does_not_penalise_the_engine() -> None:
    pool = ProviderPool()

    pool.record("DuckDuckGo", "error")

    assert pool.available("DuckDuckGo") is True


def test_reset_clears_everything() -> None:
    pool = ProviderPool()
    pool.record("Bing", "captcha")

    pool.reset()

    assert pool.available("Bing") is True
    assert pool._failures == {}


def test_cooldown_is_still_exposed_for_compatibility() -> None:
    from app.telegram_gateway.discovery import _provider_available, _record_provider_status

    _record_provider_status("Google", "captcha")

    assert _provider_available("Google") is False
    assert _provider_available("Bing") is True
