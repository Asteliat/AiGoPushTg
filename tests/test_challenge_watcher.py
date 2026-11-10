from types import SimpleNamespace

import pytest

from app.services.challenge_monitor import ChallengeMode
from app.workers import challenge_watcher as watcher_module
from app.workers.challenge_watcher import AccountIdentity, ChallengeWatcher

IDENTITY = AccountIdentity(
    account_id=7,
    name="Rita",
    user_id=4242,
    username="rita",
    names=("Rita Petrova", "Rita", "Petrova"),
)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


class FakeButton:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessage:
    def __init__(
        self,
        text: str,
        buttons: list[list[str]] | None = None,
        *,
        message_id: int = 11,
        reply_to: object | None = None,
        mentioned: bool = False,
    ) -> None:
        self.message = text
        self.buttons = [[FakeButton(label) for label in row] for row in (buttons or [])]
        self.id = message_id
        self.out = False
        self.reply_to = reply_to
        self.mentioned = mentioned
        self.clicks: list[tuple[int, int]] = []

    async def click(self, row: int, column: int):
        self.clicks.append((row, column))
        return None


class FakeEvent:
    def __init__(
        self,
        message: FakeMessage,
        *,
        sender: object | None = None,
        replied: object | None = None,
    ) -> None:
        self.is_group = True
        self.chat_id = -100123
        self.message = message
        self._sender = sender if sender is not None else SimpleNamespace(bot=True, username="LolsBot", id=1)
        self._replied = replied
        if replied is not None:
            # A resolved reply implies the reply header exists on the message.
            message.reply_to = message.reply_to or object()

    async def get_sender(self):
        return self._sender

    async def get_chat(self):
        return SimpleNamespace(title="Тестовая группа")

    async def get_reply_message(self):
        return self._replied


@pytest.fixture
def logs() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def watcher(logs):
    async def send_log(text: str, category: str = "main") -> bool:
        logs.append((category, text))
        return True

    return ChallengeWatcher(redis=FakeRedis(), send_log=send_log)


@pytest.fixture(autouse=True)
def auto_mode(monkeypatch):
    async def mode() -> ChallengeMode:
        return ChallengeMode.AUTO

    monkeypatch.setattr(watcher_module, "read_challenge_mode", mode)
    return mode


@pytest.mark.asyncio
async def test_prompt_for_another_member_is_ignored_silently(watcher, logs) -> None:
    message = FakeMessage("Иван, нажмите на кнопку, чтобы подтвердить, что вы не робот", [["Я не бот"]])
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=999))

    await watcher.handle(event, IDENTITY)

    assert message.clicks == []
    assert logs == []


@pytest.mark.asyncio
async def test_own_reply_prompt_is_passed_and_logged_once(watcher, logs) -> None:
    message = FakeMessage("Нажмите на кнопку, чтобы подтвердить, что вы не робот", [["Я не бот"]])
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id))

    await watcher.handle(event, IDENTITY)

    assert message.clicks == [(0, 0)]
    assert len(logs) == 1
    category, text = logs[0]
    assert category == "main"
    assert "Проверка Telegram пройдена" in text


@pytest.mark.asyncio
async def test_same_prompt_is_not_pressed_twice(watcher, logs) -> None:
    message = FakeMessage("Нажмите на кнопку, чтобы подтвердить, что вы не робот", [["Я не бот"]])
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id))

    await watcher.handle(event, IDENTITY)
    await watcher.handle(event, IDENTITY)

    assert message.clicks == [(0, 0)]
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_ambiguous_prompt_reports_failure_without_clicking(watcher, logs) -> None:
    message = FakeMessage(
        "Выберите правильный ответ и подтвердите, что вы человек",
        [["Москва", "Казань"]],
    )
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id))

    await watcher.handle(event, IDENTITY)

    assert message.clicks == []
    assert len(logs) == 1
    category, text = logs[0]
    assert category == "errors"
    assert "не пройдена" in text


@pytest.mark.asyncio
async def test_notify_mode_only_reports_own_prompt(watcher, logs, monkeypatch) -> None:
    async def notify_mode() -> ChallengeMode:
        return ChallengeMode.NOTIFY

    monkeypatch.setattr(watcher_module, "read_challenge_mode", notify_mode)
    message = FakeMessage("Нажми на кнопку, чтобы подтвердить, что ты не бот", [["Я не бот"]])
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id))

    await watcher.handle(event, IDENTITY)

    assert message.clicks == []
    assert len(logs) == 1
    assert "кнопка не нажата" in logs[0][1]


@pytest.mark.asyncio
async def test_disabled_mode_performs_nothing(watcher, logs, monkeypatch) -> None:
    async def off_mode() -> ChallengeMode:
        return ChallengeMode.OFF

    monkeypatch.setattr(watcher_module, "read_challenge_mode", off_mode)
    message = FakeMessage("Нажми на кнопку, чтобы подтвердить, что ты не бот", [["Я не бот"]])
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id))

    await watcher.handle(event, IDENTITY)

    assert message.clicks == []
    assert logs == []


@pytest.mark.asyncio
async def test_non_bot_sender_is_ignored(watcher, logs) -> None:
    message = FakeMessage("Нажмите на кнопку, чтобы подтвердить, что вы не робот", [["Я не бот"]])
    event = FakeEvent(message, sender=SimpleNamespace(bot=False, id=5))

    await watcher.handle(event, IDENTITY)

    assert message.clicks == []
    assert logs == []


@pytest.mark.asyncio
async def test_flood_wait_is_reported_as_failure(watcher, logs, monkeypatch) -> None:
    from telethon.errors import FloodWaitError

    message = FakeMessage("Нажмите на кнопку, чтобы подтвердить, что вы не робот", [["Я не бот"]])

    async def flood(row, column):
        raise FloodWaitError(request=None, capture=30)

    message.click = flood  # type: ignore[method-assign]
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id))

    await watcher.handle(event, IDENTITY)

    assert len(logs) == 1
    assert logs[0][0] == "errors"
    assert "ограничил нажатие" in logs[0][1]
