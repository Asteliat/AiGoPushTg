from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError
from telethon.errors import FloodWaitError

from app.storage.models import TriggerRule
from app.workers import redis_keys
from app.workers import state as state_module
from app.workers import trigger_replies as trigger_module
from app.workers.state import WorkerState
from app.workers.trigger_replies import process_trigger_message
from tests.fakes import (
    FakeEvent,
    FakeMessage,
    FakeRedis,
    FakeSessionFactory,
    FakeTelethonClient,
    make_account,
)


def make_rule(**overrides) -> TriggerRule:
    values = {
        "id": 11,
        "account_id": 1,
        "terms": "цена",
        "response_instructions": "Отвечай вежливо.",
        "apply_mode": "all",
        "cooldown_seconds": 0,
        "message_cooldown_seconds": 0,
        "enabled": True,
    }
    values.update(overrides)
    return TriggerRule(**values)


class Env:
    def __init__(
        self,
        *,
        rules=None,
        reply_mode="manual",
        allowed=True,
        draft="Здравствуйте!",
    ) -> None:
        self.account = make_account(1, reply_mode=reply_mode, ai_agent_id=2)
        self.factory = FakeSessionFactory({("Account", 1): self.account})
        self.factory.scalar_rows = list(rules if rules is not None else [make_rule()])
        self.db = self.factory()
        self.redis = FakeRedis()
        self.client = FakeTelethonClient()
        self.event = FakeEvent(
            FakeMessage("сколько стоит цена?", message_id=10, sender_id=5),
            chat_id=-100,
            client=self.client,
        )
        self.worker = WorkerState(
            cfg=SimpleNamespace(log_chat_id=-100999),
            worker_instance_id="worker-a",
            redis=self.redis,
            bot=None,
        )
        self.logs: list[tuple[str, str]] = []
        self.generated: list[dict] = []
        self.allowed = allowed
        self.draft_text = draft

    async def generate(self, db, **kwargs):
        self.generated.append(kwargs)
        return SimpleNamespace(proposed_text=self.draft_text)

    async def send_allowed(self, *_args, **_kwargs):
        return self.allowed

    async def run(self):
        return await process_trigger_message(
            self.event,
            db=self.db,
            account=self.account,
            label="Тестовый чат",
            incoming="сколько стоит цена?",
            sender_id=5,
            worker=self.worker,
            generate=self.generate,
            send_allowed=self.send_allowed,
        )


@pytest.fixture
def logs(monkeypatch):
    recorded: list[tuple[str, str]] = []

    async def fake_enqueue(bot, text, category="main", fallback_chat_id=0):
        recorded.append((category, text))
        return True

    monkeypatch.setattr(state_module, "enqueue_notification", fake_enqueue)
    return recorded


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(trigger_module.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
async def test_no_matching_trigger_returns_false(logs) -> None:
    env = Env(rules=[make_rule(terms="доставка")])

    assert await env.run() is False
    assert env.generated == []
    assert env.factory.added == []


@pytest.mark.asyncio
async def test_manual_mode_prepares_and_logs_the_reply(logs) -> None:
    env = Env(reply_mode="manual")

    assert await env.run() is True
    record = env.factory.added[0]
    assert record.status == "prepared"
    assert env.event.replies == []
    assert logs and logs[0][0] == "replies"
    assert "подготовлен" in logs[0][1]


@pytest.mark.asyncio
async def test_automatic_mode_sends_the_reply(logs) -> None:
    env = Env(reply_mode="automatic")

    assert await env.run() is True
    record = env.factory.added[0]
    assert record.status == "sent"
    assert env.event.replies == ["Здравствуйте!"]
    assert "отправлен" in logs[0][1]


@pytest.mark.asyncio
async def test_automatic_mode_cancels_when_policy_denies(logs) -> None:
    env = Env(reply_mode="automatic", allowed=False)

    assert await env.run() is True
    assert env.factory.added[0].status == "cancelled"
    assert env.event.replies == []


@pytest.mark.asyncio
async def test_active_cooldown_skips_generation(logs) -> None:
    env = Env()
    env.redis.values[redis_keys.trigger_message_cooldown(1)] = "1"

    assert await env.run() is True
    assert env.factory.added[0].status == "skipped_cooldown"
    assert env.generated == []


@pytest.mark.asyncio
async def test_duplicate_message_is_ignored(logs) -> None:
    env = Env()
    env.factory.commit_errors = [IntegrityError("insert", {}, Exception("duplicate"))]

    assert await env.run() is True
    assert env.generated == []
    assert env.event.replies == []
    assert env.factory.rollbacks == 1


@pytest.mark.asyncio
async def test_flood_wait_is_retried_once(logs) -> None:
    env = Env(reply_mode="automatic")
    attempts = {"count": 0}
    original_reply = env.event.reply

    async def flaky_reply(text, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise FloodWaitError(request=None, capture=1)
        return await original_reply(text, **kwargs)

    env.event.reply = flaky_reply

    assert await env.run() is True
    assert env.factory.added[0].status == "sent"
    assert attempts["count"] == 2
    assert env.event.replies == ["Здравствуйте!"]


@pytest.mark.asyncio
async def test_trigger_lock_is_released_even_after_an_error(logs) -> None:
    env = Env()

    async def broken_generate(db, **kwargs):
        raise RuntimeError("AI is down")

    env.generate = broken_generate

    assert await env.run() is True
    assert env.factory.added[0].status == "failed"
    assert redis_keys.trigger_lock(1) not in env.redis.values
    assert logs[-1][0] == "errors"


@pytest.mark.asyncio
async def test_cooldown_timers_are_stored_after_the_reply(logs) -> None:
    env = Env()
    env.factory.scalar_rows = [
        make_rule(cooldown_seconds=300, message_cooldown_seconds=60)
    ]

    await env.run()

    assert env.redis.expires[redis_keys.trigger_cooldown(1, 11)] == 300
    assert env.redis.expires[redis_keys.trigger_message_cooldown(1)] == 60
