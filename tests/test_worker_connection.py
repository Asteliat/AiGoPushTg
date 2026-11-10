import asyncio
from types import SimpleNamespace

import pytest

from app.workers import account_status as account_status_module
from app.workers import connection as connection_module
from app.workers import state as state_module
from app.workers.connection import AccountConnector
from app.workers.state import WorkerState
from tests.fakes import FakeRedis, FakeSessionFactory, FakeTelethonClient, make_account


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


class InstallRecorder:
    """Stands in for objects with an ``install(client, account)`` method."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def install(self, client, account=None) -> None:
        self.calls.append((client, account))


@pytest.fixture
def statuses(monkeypatch):
    recorded: list[tuple[int, str]] = []

    async def fake_status(account_id: int, status: str) -> None:
        recorded.append((account_id, status))

    monkeypatch.setattr(connection_module, "set_connection_status", fake_status)
    monkeypatch.setattr(connection_module, "telethon_proxy", lambda _value: None)
    monkeypatch.setattr(connection_module, "configure_telethon_sqlite_session", lambda _client: None)
    return recorded


@pytest.fixture
def logs(monkeypatch):
    recorded: list[tuple[str, str]] = []

    async def fake_enqueue(bot, text, category="main", fallback_chat_id=0):
        recorded.append((category, text))
        return True

    monkeypatch.setattr(state_module, "enqueue_notification", fake_enqueue)
    return recorded


@pytest.fixture
def labels(monkeypatch):
    recorded: dict[int, dict[int, str]] = {7: {10: "Чат 1"}}

    async def fake_sync(client, aid, *, last_discovery, attempt=0):
        return recorded[aid]

    monkeypatch.setattr(connection_module, "sync_account_dialogs", fake_sync)
    return recorded


def make_worker(redis=None, **cfg_overrides) -> WorkerState:
    cfg = SimpleNamespace(
        telegram_api_id=123,
        telegram_api_hash="hash",
        log_chat_id=-100999,
    )
    for key, value in cfg_overrides.items():
        setattr(cfg, key, value)
    return WorkerState(
        cfg=cfg,
        worker_instance_id="worker-a",
        redis=redis or FakeRedis(),
        bot=None,
    )


class ReturningRecorder(Recorder):
    """Records the call and returns a marker, like install_channel_monitor does."""

    def __init__(self, result=None) -> None:
        super().__init__()
        self.result = result

    def __call__(self, *args, **kwargs):
        super().__call__(*args, **kwargs)
        return self.result


def make_connector(worker, client, *, on_message=None, challenge=None, monitor=None):
    on_message = on_message or Recorder()
    challenge = challenge or InstallRecorder()
    monitor = monitor if monitor is not None else Recorder()
    connector = AccountConnector(
        worker,
        on_message=on_message,
        challenge_watcher=challenge,
        install_monitor=monitor,
        client_factory=lambda *args, **kwargs: client,
    )
    return connector, on_message, challenge, monitor


@pytest.mark.asyncio
async def test_connect_leases_session_wires_handlers_and_stores_state(
    statuses, logs, labels
) -> None:
    worker = make_worker()
    client = FakeTelethonClient(authorized=True)
    monitor_marker = object()
    connector, on_message, challenge, monitor = make_connector(
        worker, client, monitor=ReturningRecorder(monitor_marker)
    )
    account = make_account(7)

    await connector.connect(account)
    try:
        assert statuses == [(7, "connecting"), (7, "connected")]
        assert worker.clients[7] is client
        assert worker.watched[7] == {10: "Чат 1"}
        assert worker.account_snapshot[7] is account
        assert 7 in worker.tasks
        assert worker.session_lease_tokens[7].startswith("worker-a|")
        assert 7 in worker.session_lease_tasks
        assert len(client.handlers) == 1
        assert challenge.calls and challenge.calls[0][0] is client
        assert monitor.calls and monitor.calls[0][0][0] is client
        assert worker.monitors[7] is monitor.result
        assert logs and "Аккаунт подключён" in logs[0][1]
    finally:
        await worker.cancel_background_tasks()


@pytest.mark.asyncio
async def test_missing_api_credentials_report_error_without_connecting(statuses) -> None:
    worker = make_worker(telegram_api_id=None, telegram_api_hash=None)
    client = FakeTelethonClient()
    connector, _, _, _ = make_connector(worker, client)

    await connector.connect(make_account(7))

    assert statuses == [(7, "error")]
    assert 7 in worker.reconnect_after
    assert 7 not in worker.clients


@pytest.mark.asyncio
async def test_lease_owned_by_another_worker_skips_the_connection(statuses) -> None:
    redis = FakeRedis()
    redis.values["aigopushtg:telegram-session:7"] = "worker-b|token"
    worker = make_worker(redis)
    client = FakeTelethonClient()
    connector, on_message, _, _ = make_connector(worker, client)

    await connector.connect(make_account(7))

    assert statuses == []
    assert 7 not in worker.clients
    assert 7 in worker.reconnect_after
    assert client.handlers == []


@pytest.mark.asyncio
async def test_stale_self_owned_lease_is_recovered(statuses, logs, labels) -> None:
    redis = FakeRedis()
    redis.values["aigopushtg:telegram-session:7"] = "worker-a|stale"
    worker = make_worker(redis)
    client = FakeTelethonClient()
    connector, _, _, _ = make_connector(worker, client)

    await connector.connect(make_account(7))
    try:
        assert worker.clients[7] is client
        assert redis.values["aigopushtg:telegram-session:7"].startswith("worker-a|")
    finally:
        await worker.cancel_background_tasks()


@pytest.mark.asyncio
async def test_unauthorized_session_releases_the_lease(statuses) -> None:
    redis = FakeRedis()
    worker = make_worker(redis)
    client = FakeTelethonClient(authorized=False)
    connector, on_message, _, _ = make_connector(worker, client)

    await connector.connect(make_account(7))

    assert statuses == [(7, "connecting"), (7, "unauthorized")]
    assert "aigopushtg:telegram-session:7" not in redis.values
    assert client.disconnects == 1
    assert 7 not in worker.clients
    assert 7 in worker.reconnect_after


@pytest.mark.asyncio
async def test_connect_error_sets_error_status_and_releases_the_lease(statuses) -> None:
    redis = FakeRedis()
    worker = make_worker(redis)
    client = FakeTelethonClient(connect_error=ConnectionError("network"))
    connector, _, _, _ = make_connector(worker, client)

    await connector.connect(make_account(7))

    assert statuses == [(7, "connecting"), (7, "error")]
    assert "aigopushtg:telegram-session:7" not in redis.values
    assert 7 in worker.reconnect_after
    assert 7 not in worker.clients


@pytest.mark.asyncio
async def test_cancellation_during_connect_releases_the_lease(statuses) -> None:
    redis = FakeRedis()
    worker = make_worker(redis)
    client = FakeTelethonClient(connect_error=asyncio.CancelledError())
    connector, _, _, _ = make_connector(worker, client)

    with pytest.raises(asyncio.CancelledError):
        await connector.connect(make_account(7))

    assert "aigopushtg:telegram-session:7" not in redis.values


@pytest.mark.asyncio
async def test_set_connection_status_updates_the_account_row(monkeypatch) -> None:
    account = make_account(3)
    factory = FakeSessionFactory({("Account", 3): account})
    monkeypatch.setattr(account_status_module, "SessionFactory", factory)

    await account_status_module.set_connection_status(3, "connected")

    assert account.connection_status == "connected"
    assert factory.commits == 1


@pytest.mark.asyncio
async def test_set_connection_status_ignores_a_missing_account(monkeypatch) -> None:
    factory = FakeSessionFactory()
    monkeypatch.setattr(account_status_module, "SessionFactory", factory)

    await account_status_module.set_connection_status(404, "connected")

    assert factory.commits == 0
