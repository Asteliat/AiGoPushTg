from types import SimpleNamespace

import pytest

from app.storage.models import AppSetting
from app.workers.autoresponder import process_static_autoresponder
from tests.fakes import (
    FakeEvent,
    FakeMessage,
    FakeRedis,
    FakeSessionFactory,
    FakeTelethonClient,
    make_account,
)


def settings_rows(**values):
    """AppSetting rows as the SQLAlchemy ``scalars`` call would return them."""
    return [AppSetting(key=key, value=value) for key, value in values.items()]


async def run(env, *, incoming="сколько стоит?", sender_id=5):
    return await process_static_autoresponder(
        env.event,
        db=env.db,
        account=env.account,
        incoming=incoming,
        sender_id=sender_id,
        redis=env.redis,
        session_factory=env.factory,
    )


def setup(
    *,
    ai_enabled: bool = False,
    enabled: str = "true",
    keywords: str = "сколько, цена",
    response: str = "Здравствуйте! Отвечу в течение дня.",
    cooldown: str = "0",
):
    account = make_account(1, ai_enabled=ai_enabled)
    factory = FakeSessionFactory({("Account", 1): account})
    factory.scalar_rows = settings_rows(
        **{
            "autoresponder.1.enabled": enabled,
            "autoresponder.1.keywords": keywords,
            "autoresponder.1.response": response,
            "autoresponder.1.cooldown_seconds": cooldown,
        }
    )
    redis = FakeRedis()
    client = FakeTelethonClient()
    event = FakeEvent(
        FakeMessage("сколько стоит?", sender_id=5),
        chat_id=5,
        is_group=False,
        is_private=True,
        client=client,
    )
    return SimpleNamespace(
        account=account,
        factory=factory,
        redis=redis,
        client=client,
        event=event,
        db=SimpleNamespace(scalars=factory().scalars),
    )


@pytest.mark.asyncio
async def test_matching_private_message_gets_one_static_reply() -> None:
    env = setup()

    handled = await run(env, incoming="сколько стоит?")

    assert handled is True
    assert len(env.client.sent) == 1
    entity, text = env.client.sent[0]
    assert entity == 5
    assert text.startswith("Здравствуйте!")


@pytest.mark.asyncio
async def test_the_same_message_is_answered_once() -> None:
    env = setup()

    for _ in range(2):
        await run(env, incoming="сколько стоит?")

    assert len(env.client.sent) == 1


@pytest.mark.asyncio
async def test_disabled_autoresponder_does_nothing() -> None:
    env = setup(enabled="false")

    handled = await run(env, incoming="сколько стоит?")

    assert handled is False
    assert env.client.sent == []


@pytest.mark.asyncio
async def test_keyword_mismatch_does_nothing() -> None:
    env = setup()

    handled = await run(env, incoming="привет")

    assert handled is False
    assert env.client.sent == []


@pytest.mark.asyncio
async def test_account_with_enabled_ai_never_gets_the_static_reply() -> None:
    env = setup(ai_enabled=True)

    handled = await run(env, incoming="сколько стоит?")

    assert handled is False
    assert env.client.sent == []
    # The reservation is returned, so the message may be answered by the AI path.
    assert not env.redis.values


@pytest.mark.asyncio
async def test_group_messages_are_ignored() -> None:
    env = setup()
    env.event.is_private = False

    handled = await run(env, incoming="сколько стоит?")

    assert handled is False
    assert env.client.sent == []


@pytest.mark.asyncio
async def test_cooldown_blocks_a_second_reply_to_the_same_person() -> None:
    env = setup(cooldown="60", response="Ответ")

    first = await run(env, incoming="цена")
    env.event.message.id = 2
    second = await run(env, incoming="цена")

    assert first is True
    assert second is True  # handled (skipped by cooldown), not an error
    assert len(env.client.sent) == 1


@pytest.mark.asyncio
async def test_empty_response_setting_disables_the_reply() -> None:
    env = setup(response="   ")

    handled = await run(env, incoming="цена")

    assert handled is False
    assert env.client.sent == []
