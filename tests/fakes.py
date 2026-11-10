"""In-memory stand-ins for Redis, SQLAlchemy sessions, Telethon and aiogram.

The worker code is written against four external systems. These fakes implement only
the calls that code actually makes, so unit tests can exercise the real orchestration
(order of lease, connect, handlers, statuses) without PostgreSQL, Redis or Telegram.

Keep the fakes explicit: if production code starts using a new Redis command or a new
session method, the test must fail loudly instead of silently passing.
"""
from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace

from app.workers import redis_keys


class FakeRedis:
    """Lists, strings and the two compare-and-swap scripts used by the worker."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, int | None] = {}
        self.evals: list[tuple[str, tuple]] = []
        self.deleted: list[str] = []
        self.closed = False

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return None
        self.values[key] = str(value)
        self.expires[key] = ex
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            self.deleted.append(key)
            if self.values.pop(key, None) is not None:
                removed += 1
        return removed

    async def exists(self, *keys):
        return sum(1 for key in keys if key in self.values or key in self.lists)

    async def ttl(self, key):
        return self.expires.get(key) or -1

    async def expire(self, key, seconds):
        self.expires[key] = seconds
        return True

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def ltrim(self, key, start, stop):
        values = self.lists.get(key, [])
        self.lists[key] = values[start: stop + 1] if stop >= 0 else values[start:]
        return True

    async def lrange(self, key, start, stop):
        values = self.lists.get(key, [])
        if stop == -1:
            return values[start:]
        return values[start: stop + 1]

    async def eval(self, script, numkeys, *args):
        self.evals.append((script, args))
        key, *rest = args
        if script == redis_keys.CAS_DELETE:
            if self.values.get(key) == rest[0]:
                self.values.pop(key, None)
                return 1
            return 0
        if script == redis_keys.CAS_EXPIRE:
            return 1 if self.values.get(key) == rest[0] else 0
        raise AssertionError(f"unexpected script: {script!r}")

    async def aclose(self):
        self.closed = True


class FakeScalars:
    def __init__(self, rows: Iterable) -> None:
        self._rows = list(rows)

    def all(self) -> list:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeResult:
    def __init__(self, rows: Iterable = ()) -> None:
        self._rows = list(rows)

    def all(self) -> list:
        return list(self._rows)


class FakeSession:
    """Serves objects by ``(model name, primary key)``; queries return nothing."""

    def __init__(self, factory: FakeSessionFactory) -> None:
        self.factory = factory

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, model, key):
        return self.factory.objects.get((model.__name__, int(key)))

    async def scalar(self, _statement):
        return self.factory.scalar_result

    async def scalars(self, _statement):
        return FakeScalars(self.factory.scalar_rows)

    async def execute(self, _statement):
        return FakeResult(self.factory.execute_rows)

    def add(self, obj) -> None:
        self.factory.added.append(obj)

    async def delete(self, obj) -> None:
        self.factory.deleted.append(obj)

    async def commit(self) -> None:
        self.factory.commits += 1

    async def rollback(self) -> None:
        self.factory.rollbacks += 1


class FakeSessionFactory:
    """Callable session factory: ``async with factory() as db``."""

    def __init__(self, objects: dict | None = None) -> None:
        self.objects = objects or {}
        self.scalar_result = None
        self.scalar_rows: list = []
        self.execute_rows: list = []
        self.added: list = []
        self.deleted: list = []
        self.commits = 0
        self.rollbacks = 0

    def __call__(self) -> FakeSession:
        return FakeSession(self)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.session = SimpleNamespace(close=self._close)
        self.closed = False

    async def _close(self) -> None:
        self.closed = True

    async def send_message(self, chat_id, text, **_kwargs):
        self.messages.append((int(chat_id), text))
        return SimpleNamespace(message_id=len(self.messages))


class FakeTelethonClient:
    """Telethon surface used by the worker: handlers, connection, entities, dialogs."""

    def __init__(
        self,
        *,
        authorized: bool = True,
        connected: bool = False,
        dialogs: Iterable | None = None,
        entities: dict | None = None,
        connect_error: Exception | None = None,
    ) -> None:
        self.authorized = authorized
        self.connected = connected
        self._dialogs = list(dialogs or ())
        self.entities = entities or {}
        self.connect_error = connect_error
        self.handlers: list[tuple[object, object]] = []
        self.requests: list[object] = []
        self.sent: list[tuple[object, str]] = []
        self.disconnects = 0
        self.flood_sleep_threshold = 0

    def add_event_handler(self, callback, event_filter=None) -> None:
        self.handlers.append((callback, event_filter))

    def remove_event_handler(self, callback, event_filter=None) -> None:
        self.handlers = [item for item in self.handlers if item[0] is not callback]

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def run_until_disconnected(self) -> None:
        while self.connected:
            await _idle()

    async def __call__(self, request):
        self.requests.append(request)
        handler = getattr(self, "_handle_request", None)
        if handler is None:
            raise AssertionError(f"unexpected request: {request!r}")
        return await handler(request)

    async def iter_dialogs(self, limit=None):
        for dialog in self._dialogs:
            yield dialog

    async def iter_messages(self, _peer, **_kwargs):
        if False:  # pragma: no cover - empty async generator
            yield None

    async def get_entity(self, target):
        if target not in self.entities:
            raise ValueError(f"no entity {target!r}")
        return self.entities[target]

    async def send_message(self, entity, text, **_kwargs):
        self.sent.append((entity, text))
        return SimpleNamespace(id=len(self.sent))

    async def get_messages(self, _entity, **_kwargs):
        return []


async def _idle() -> None:
    import asyncio

    await asyncio.sleep(0)


class FakeMessage:
    def __init__(
        self,
        text: str = "",
        *,
        message_id: int = 1,
        out: bool = False,
        sender_id: int | None = None,
        date=None,
        buttons: Iterable = (),
        reply_to=None,
        mentioned: bool = False,
    ) -> None:
        self.message = text
        self.id = message_id
        self.out = out
        self.sender_id = sender_id
        self.date = date
        self.buttons = [list(row) for row in buttons]
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
        chat_id: int = -1001,
        is_group: bool = True,
        is_private: bool = False,
        is_channel: bool = False,
        sender=None,
        chat=None,
        replied=None,
        client=None,
    ) -> None:
        self.message = message
        self.chat_id = chat_id
        self.is_group = is_group
        self.is_private = is_private
        self.is_channel = is_channel
        self.sender_id = message.sender_id
        self._sender = sender if sender is not None else SimpleNamespace(id=message.sender_id)
        self._chat = chat if chat is not None else SimpleNamespace(title="Тестовая группа", id=chat_id)
        self._replied = replied
        self.client = client
        self.replies: list[str] = []

    async def get_sender(self):
        return self._sender

    async def get_chat(self):
        return self._chat

    async def get_reply_message(self):
        return self._replied

    async def reply(self, text, **_kwargs):
        self.replies.append(text)
        return SimpleNamespace(id=len(self.replies) + 100)


def make_account(account_id: int = 1, **overrides):
    """Account model instance without a database; only attributes are set."""
    from app.storage.models import Account

    values = {
        "id": account_id,
        "telegram_user_id": 1000 + account_id,
        "username": f"account{account_id}",
        "display_name": f"Аккаунт {account_id}",
        "session_path": f"/data/sessions/{account_id}.session",
        "proxy_config_encrypted": None,
        "enabled": True,
        "manual_pause": False,
        "connection_status": "disconnected",
        "reply_mode": "manual",
        "ai_enabled": False,
        "ai_agent_id": None,
        "learning_status": "idle",
        "learning_prompt": "",
        "persona_style_override": "",
        "reply_cooldown_seconds_override": None,
    }
    values.update(overrides)
    return Account(**values)
