from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.storage.models import AIAgent, AppSetting
from app.workers import reply_pipeline as pipeline_module
from app.workers.reply_pipeline import on_message
from app.workers.state import WorkerState
from tests.fakes import (
    FakeEvent,
    FakeMessage,
    FakeRedis,
    FakeSessionFactory,
    FakeTelethonClient,
    make_account,
)


def make_agent() -> AIAgent:
    return AIAgent(
        id=2,
        name="GPT",
        enabled=True,
        mention_reply_probability_override=None,
        proactive_reply_probability_override=None,
        dm_reply_probability_override=None,
        reply_cooldown_seconds_override=None,
    )


class Env:
    def __init__(
        self,
        *,
        text="привет @account1",
        ai_enabled=True,
        reply_mode="automatic",
        learning_status="idle",
        manual_pause=False,
        labels=None,
        is_group=True,
        is_private=False,
        mention_probability="100",
        proactive_probability="0",
    ):
        self.account = make_account(
            1,
            ai_enabled=ai_enabled,
            reply_mode=reply_mode,
            ai_agent_id=2,
            learning_status=learning_status,
            manual_pause=manual_pause,
        )
        self.factory = FakeSessionFactory(
            {
                ("Account", 1): self.account,
                ("AIAgent", 2): make_agent(),
                ("AppSetting", "ai_global_enabled"): AppSetting(
                    key="ai_global_enabled", value="true"
                ),
                ("AppSetting", "mention_reply_probability"): AppSetting(
                    key="mention_reply_probability", value=mention_probability
                ),
                ("AppSetting", "proactive_reply_probability"): AppSetting(
                    key="proactive_reply_probability", value=proactive_probability
                ),
                ("AppSetting", "dm_reply_probability"): AppSetting(
                    key="dm_reply_probability", value=mention_probability
                ),
                ("AppSetting", "reply_cooldown_seconds"): AppSetting(
                    key="reply_cooldown_seconds", value="0"
                ),
            }
        )
        self.redis = FakeRedis()
        self.client = FakeTelethonClient()
        self.worker = WorkerState(
            cfg=SimpleNamespace(log_chat_id=-100999),
            worker_instance_id="worker-a",
            redis=self.redis,
            bot=None,
        )
        self.event = FakeEvent(
            FakeMessage(text, message_id=10, sender_id=5),
            chat_id=-100,
            is_group=is_group,
            is_private=is_private,
            client=self.client,
        )
        self.labels = labels if labels is not None else {self.event.chat_id: "Чат 1"}
        self.generated: list[dict] = []

    async def generate(self, db, **kwargs):
        self.generated.append(kwargs)
        return SimpleNamespace(proposed_text="Ответ ИИ")

    async def run(self, **overrides):
        options = {
            "aid": 1,
            "labels": self.labels,
            "worker": self.worker,
            "session_factory": self.factory,
            "generate": self.generate,
        }
        options.update(overrides)
        return await on_message(self.event, **options)


@pytest.fixture(autouse=True)
def sending_allowed(monkeypatch):
    """The final send policy is tested in test_worker_send_policy; here it allows."""

    async def fake_allowed(*_args, **_kwargs):
        return True

    monkeypatch.setattr(pipeline_module, "automatic_send_allowed", fake_allowed)


@pytest.fixture
def logs(monkeypatch):
    recorded: list[tuple[str, str]] = []

    async def fake_enqueue(bot, text, category="main", fallback_chat_id=0):
        recorded.append((category, text))
        return True

    from app.workers import state as state_module

    monkeypatch.setattr(state_module, "enqueue_notification", fake_enqueue)
    return recorded


@pytest.fixture
def autoresponder_calls(monkeypatch):
    recorded: list[dict] = []

    async def fake_autoresponder(event, **kwargs):
        recorded.append(kwargs)
        return True

    monkeypatch.setattr(pipeline_module, "process_static_autoresponder", fake_autoresponder)
    return recorded


@pytest.mark.asyncio
async def test_verification_prompt_from_a_bot_is_never_answered(autoresponder_calls) -> None:
    env = Env(text="Нажмите на кнопку, чтобы подтвердить, что вы не робот")
    env.event._sender = SimpleNamespace(id=900, bot=True)
    env.event.message.buttons = [["Я не бот"]]

    await env.run()

    assert env.generated == []
    assert env.event.replies == []


@pytest.mark.asyncio
async def test_group_chat_not_in_the_account_list_is_ignored() -> None:
    env = Env(labels={})

    await env.run()

    assert env.generated == []


@pytest.mark.asyncio
async def test_paused_account_does_nothing(autoresponder_calls) -> None:
    env = Env(manual_pause=True)

    await env.run()

    assert env.generated == []
    assert autoresponder_calls == []


@pytest.mark.asyncio
async def test_ai_disabled_hands_the_private_chat_to_the_autoresponder(
    autoresponder_calls, logs
) -> None:
    env = Env(ai_enabled=False, is_group=False, is_private=True)

    await env.run()

    assert env.generated == []
    assert autoresponder_calls and autoresponder_calls[0]["account"] is env.account
    assert autoresponder_calls[0]["redis"] is env.redis


@pytest.mark.asyncio
async def test_training_account_only_buffers_the_message(logs) -> None:
    env = Env(learning_status="training")
    env.account.learning_started_at = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

    await env.run(allow_unbound=True, forced_label="Обсуждение канала")

    assert env.generated == []
    # The training buffer is keyed by the learning start timestamp.
    assert any(key.startswith("atcivperson:learning:1:") for key in env.redis.lists)


@pytest.mark.asyncio
async def test_mention_in_automatic_mode_sends_and_remembers_the_reply(logs) -> None:
    env = Env(reply_mode="automatic", text="привет @account1")

    await env.run()

    assert env.event.replies == ["Ответ ИИ"]
    assert logs and "отправлен" in logs[-1][1]
    stored = [
        item
        for values in env.redis.lists.values()
        for item in values
    ]
    assert any("Ответ ИИ" in item for item in stored)


@pytest.mark.asyncio
async def test_manual_mode_prepares_without_sending(logs) -> None:
    env = Env(reply_mode="manual", text="привет @account1")

    await env.run()

    assert env.event.replies == []
    assert logs and "подготовлен" in logs[-1][1]


@pytest.mark.asyncio
async def test_proactive_reply_with_zero_probability_is_skipped(logs) -> None:
    env = Env(text="просто сообщение в группе", proactive_probability="0")

    await env.run()

    assert env.generated == []
    assert env.event.replies == []
