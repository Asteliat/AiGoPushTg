import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.storage.models import ChatBinding
from app.workers import manager as manager_module
from app.workers.manager import manager_iteration
from app.workers.state import WorkerState
from tests.fakes import FakeRedis, FakeSessionFactory, FakeTelethonClient, make_account

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


class ConnectorRecorder:
    def __init__(self) -> None:
        self.connected: list = []

    async def connect(self, account) -> None:
        self.connected.append(account)


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    recorded = SimpleNamespace(
        statuses=[],
        released=[],
        reconnects=[],
        finalized=[],
        learning_tasks=[],
    )

    async def fake_status(account_id, status):
        recorded.statuses.append((account_id, status))

    async def fake_release(worker, account_id):
        recorded.released.append(account_id)

    def fake_schedule(worker, account_id):
        recorded.reconnects.append(account_id)

    async def fake_finalize(**kwargs):
        recorded.finalized.append(kwargs)

    async def fake_run_learning(**kwargs):
        recorded.learning_tasks.append(kwargs)
        await asyncio.sleep(0)

    monkeypatch.setattr(manager_module, "set_connection_status", fake_status)
    monkeypatch.setattr(manager_module, "release_session_lease", fake_release)
    monkeypatch.setattr(manager_module, "schedule_reconnect", fake_schedule)
    monkeypatch.setattr(manager_module, "finalize_learning_buffer", fake_finalize)
    monkeypatch.setattr(manager_module, "run_learning", fake_run_learning)
    return recorded


def make_worker(redis=None) -> WorkerState:
    return WorkerState(
        cfg=SimpleNamespace(log_chat_id=-100999),
        worker_instance_id="worker-a",
        redis=redis or FakeRedis(),
        bot=None,
    )


def make_factory(accounts, bindings=()) -> FakeSessionFactory:
    factory = FakeSessionFactory()
    factory.scalar_queue = [list(accounts), list(bindings)]
    return factory


@pytest.mark.asyncio
async def test_new_active_account_is_connected(no_side_effects) -> None:
    worker = make_worker()
    connector = ConnectorRecorder()
    account = make_account(1)

    await manager_iteration(
        worker, connector, session_factory=make_factory([account]), now=NOW
    )

    assert connector.connected == [account]
    assert 1 not in worker.clients


@pytest.mark.asyncio
async def test_disabled_account_is_disconnected_and_marked(no_side_effects) -> None:
    worker = make_worker()
    client = FakeTelethonClient(connected=True)
    worker.clients[1] = client
    worker.watched[1] = {}
    account = make_account(1, enabled=False)

    await manager_iteration(
        worker, ConnectorRecorder(), session_factory=make_factory([account]), now=NOW
    )

    assert client.disconnects == 1
    assert no_side_effects.released == [1]
    assert (1, "disconnected") in no_side_effects.statuses
    assert 1 not in worker.clients
    assert 1 not in worker.watched


@pytest.mark.asyncio
async def test_paused_account_reports_paused_status(no_side_effects) -> None:
    worker = make_worker()
    worker.clients[1] = FakeTelethonClient(connected=True)
    worker.watched[1] = {}
    account = make_account(1, manual_pause=True)

    await manager_iteration(
        worker, ConnectorRecorder(), session_factory=make_factory([account]), now=NOW
    )

    assert (1, "paused") in no_side_effects.statuses


@pytest.mark.asyncio
async def test_changed_proxy_reconnects_the_account(no_side_effects) -> None:
    worker = make_worker()
    client = FakeTelethonClient(connected=True)
    worker.clients[1] = client
    worker.watched[1] = {}
    worker.account_snapshot[1] = make_account(1, proxy_config_encrypted="old")
    account = make_account(1, proxy_config_encrypted="new")

    await manager_iteration(
        worker, ConnectorRecorder(), session_factory=make_factory([account]), now=NOW
    )

    assert client.disconnects == 1
    assert no_side_effects.released == [1]
    assert 1 not in worker.clients


@pytest.mark.asyncio
async def test_old_discovery_refreshes_labels_in_the_same_dict(no_side_effects, monkeypatch) -> None:
    worker = make_worker()
    labels = {10: "Старый чат"}
    worker.clients[1] = FakeTelethonClient(connected=True)
    worker.watched[1] = labels
    worker.last_discovery[1] = NOW - timedelta(minutes=10)
    syncs: list[int] = []

    async def fake_sync(client, aid, *, last_discovery, attempt=0):
        syncs.append(aid)
        return {20: "Новый чат"}

    monkeypatch.setattr(manager_module, "sync_account_dialogs", fake_sync)

    await manager_iteration(
        worker, ConnectorRecorder(), session_factory=make_factory([make_account(1)]), now=NOW
    )

    assert syncs == [1]
    # Handlers keep a reference to this dict, so it must be updated in place.
    assert worker.watched[1] is labels
    assert labels == {20: "Новый чат"}


@pytest.mark.asyncio
async def test_recent_discovery_uses_chat_bindings(no_side_effects, monkeypatch) -> None:
    worker = make_worker()
    worker.clients[1] = FakeTelethonClient(connected=True)
    worker.watched[1] = {}
    worker.last_discovery[1] = NOW - timedelta(minutes=1)

    async def unexpected_sync(*_args, **_kwargs):
        raise AssertionError("Telegram must not be asked again within the interval")

    monkeypatch.setattr(manager_module, "sync_account_dialogs", unexpected_sync)
    binding = ChatBinding(account_id=1, chat_id=30, title="Чат из базы", enabled=True)

    await manager_iteration(
        worker,
        ConnectorRecorder(),
        session_factory=make_factory([make_account(1)], [binding]),
        now=NOW,
    )

    assert worker.watched[1] == {30: "Чат из базы"}


@pytest.mark.asyncio
async def test_connected_status_is_reconciled_for_running_client(no_side_effects) -> None:
    worker = make_worker()
    worker.clients[1] = FakeTelethonClient(connected=True)
    worker.watched[1] = {}
    worker.last_discovery[1] = NOW
    account = make_account(1, connection_status="connecting")

    await manager_iteration(
        worker, ConnectorRecorder(), session_factory=make_factory([account]), now=NOW
    )

    assert (1, "connected") in no_side_effects.statuses


@pytest.mark.asyncio
async def test_stopping_account_without_client_is_finalized(no_side_effects) -> None:
    worker = make_worker()
    account = make_account(1, learning_status="stopping")
    account.learning_started_at = NOW - timedelta(hours=1)

    await manager_iteration(
        worker, ConnectorRecorder(), session_factory=make_factory([account]), now=NOW
    )

    assert no_side_effects.finalized, "learning buffer must be finalized"
    assert no_side_effects.finalized[0]["aid"] == 1
    assert account.learning_status == "idle"


@pytest.mark.asyncio
async def test_training_account_starts_the_collector(no_side_effects) -> None:
    worker = make_worker()
    client = FakeTelethonClient(connected=True)
    worker.clients[1] = client
    worker.watched[1] = {}
    worker.last_discovery[1] = NOW
    account = make_account(1, learning_status="training")

    await manager_iteration(
        worker, ConnectorRecorder(), session_factory=make_factory([account]), now=NOW
    )

    assert 1 in worker.learning_tasks
    await worker.cancel_background_tasks()
