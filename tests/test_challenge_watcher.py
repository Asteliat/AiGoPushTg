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
    def __init__(self, text: str, url: str | None = None) -> None:
        self.text = text
        self.url = url


class FakeMessage:
    def __init__(
        self,
        text: str,
        buttons: list[list[str | tuple[str, str]]] | None = None,
        *,
        message_id: int = 11,
        reply_to: object | None = None,
        mentioned: bool = False,
    ) -> None:
        self.message = text
        self.buttons = [
            [FakeButton(*label) if isinstance(label, tuple) else FakeButton(label) for label in row]
            for row in (buttons or [])
        ]
        self.id = message_id
        self.out = False
        self.reply_to = reply_to
        self.mentioned = mentioned
        self.clicks: list[tuple[int, int]] = []

    async def click(self, row: int, column: int):
        self.clicks.append((row, column))
        return None


class FakeVerificationBotClient:
    """Stands in for the Telethon client while a verification bot is opened."""

    def __init__(
        self,
        *,
        bot: bool = True,
        reply: FakeMessage | None = None,
        entity_error: Exception | None = None,
    ) -> None:
        self.bot = bot
        self.reply = reply
        self.entity_error = entity_error
        self.sent: list[tuple[object, str]] = []
        self.entities_requested: list[str] = []

    async def get_entity(self, username: str):
        self.entities_requested.append(username)
        if self.entity_error is not None:
            raise self.entity_error
        return SimpleNamespace(bot=self.bot, username=username, id=555)

    async def send_message(self, entity, text: str):
        self.sent.append((entity, text))
        return SimpleNamespace(id=100)

    async def get_messages(self, entity, *, min_id: int = 0, limit: int = 1):
        return [self.reply] if self.reply is not None else []


class FakeEvent:
    def __init__(
        self,
        message: FakeMessage,
        *,
        sender: object | None = None,
        replied: object | None = None,
        client: FakeVerificationBotClient | None = None,
    ) -> None:
        self.is_group = True
        self.chat_id = -100123
        self.message = message
        self.client = client or FakeVerificationBotClient()
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


# ------------------------------------------------------------ verification bot flow


BOT_PROMPT = "Перейдите в бота @JoinCaptchaBot и нажмите Start."
BOT_URL = "https://t.me/JoinCaptchaBot?start=abc123"


@pytest.mark.asyncio
async def test_link_button_opens_the_bot_and_presses_its_button(watcher, logs) -> None:
    message = FakeMessage(BOT_PROMPT, [[("Проверить", BOT_URL)]])
    reply = FakeMessage("Нажмите кнопку, чтобы подтвердить, что вы не робот", [["✅ Я не робот"]])
    client = FakeVerificationBotClient(reply=reply)
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id), client=client)

    await watcher.handle(event, IDENTITY)

    assert message.clicks == []                      # a link is never clicked in the group
    assert client.sent[0][1] == "/start abc123"      # deep-link payload is passed through
    assert reply.clicks == [(0, 0)]                  # the bot's own button was pressed
    assert len(logs) == 1
    category, text = logs[0]
    assert category == "main"
    assert "личка @JoinCaptchaBot" in text


@pytest.mark.asyncio
async def test_bot_reply_without_button_confirms_the_account(watcher, logs) -> None:
    message = FakeMessage(BOT_PROMPT, [[("Проверить", BOT_URL)]])
    client = FakeVerificationBotClient(reply=FakeMessage("Вы подтверждены ✅"))
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id), client=client)

    await watcher.handle(event, IDENTITY)

    assert client.sent
    assert logs and logs[0][0] == "main" and "пройдена" in logs[0][1]


@pytest.mark.asyncio
async def test_bot_that_is_not_a_bot_is_reported(watcher, logs) -> None:
    message = FakeMessage(BOT_PROMPT, [[("Проверить", BOT_URL)]])
    client = FakeVerificationBotClient(bot=False)
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id), client=client)

    await watcher.handle(event, IDENTITY)

    assert client.sent == []
    assert logs and logs[0][0] == "errors"
    assert "оказался не ботом" in logs[0][1]


@pytest.mark.asyncio
async def test_unreachable_bot_is_reported(watcher, logs) -> None:
    message = FakeMessage(BOT_PROMPT, [[("Проверить", BOT_URL)]])
    client = FakeVerificationBotClient(entity_error=ValueError("no user has that username"))
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id), client=client)

    await watcher.handle(event, IDENTITY)

    assert client.sent == []
    assert logs and logs[0][0] == "errors"
    assert "недоступен" in logs[0][1]


@pytest.mark.asyncio
async def test_silent_bot_is_reported_after_timeout(watcher, logs, monkeypatch) -> None:
    monkeypatch.setattr(watcher_module, "_BOT_REPLY_TIMEOUT_SECONDS", 0.01)
    message = FakeMessage(BOT_PROMPT, [[("Проверить", BOT_URL)]])
    client = FakeVerificationBotClient(reply=None)
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id), client=client)

    await watcher.handle(event, IDENTITY)

    assert logs and logs[0][0] == "errors"
    assert "не ответил" in logs[0][1]


@pytest.mark.asyncio
async def test_same_verification_bot_is_started_once(watcher, logs) -> None:
    message = FakeMessage(BOT_PROMPT, [[("Проверить", BOT_URL)]])
    client = FakeVerificationBotClient(reply=FakeMessage("Вы подтверждены"))
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=IDENTITY.user_id), client=client)

    await watcher.handle(event, IDENTITY)
    await watcher.handle(event, IDENTITY)

    assert len(client.sent) == 1
    assert len(logs) == 1


@pytest.mark.asyncio
async def test_foreign_bot_prompt_never_opens_the_bot(watcher, logs) -> None:
    message = FakeMessage(BOT_PROMPT, [[("Проверить", BOT_URL)]])
    client = FakeVerificationBotClient(reply=FakeMessage("Вы подтверждены"))
    event = FakeEvent(message, replied=SimpleNamespace(sender_id=999), client=client)

    await watcher.handle(event, IDENTITY)

    assert client.sent == []
    assert message.clicks == []
    assert logs == []
