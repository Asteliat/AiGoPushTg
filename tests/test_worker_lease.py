import asyncio
from types import SimpleNamespace

import pytest

from app.workers import redis_keys
from app.workers.lease import (
    lease_owner,
    maintain_session_lease,
    release_session_lease,
    schedule_reconnect,
)
from app.workers.state import WorkerState


class FakeRedis:
    """Enough of Redis for the lease scripts: get/set/delete plus CAS eval."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.evals: list[tuple[str, tuple]] = []

    async def eval(self, script, numkeys, *args):
        self.evals.append((script, args))
        key, *rest = args
        if script == redis_keys.CAS_DELETE:
            if self.store.get(key) == rest[0]:
                self.store.pop(key, None)
                return 1
            return 0
        if script == redis_keys.CAS_EXPIRE:
            return 1 if self.store.get(key) == rest[0] else 0
        raise AssertionError("unexpected script")

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


class FakeClient:
    def __init__(self) -> None:
        self.disconnected = False

    def is_connected(self) -> bool:
        return not self.disconnected

    async def disconnect(self) -> None:
        self.disconnected = True


def make_worker(redis=None) -> WorkerState:
    return WorkerState(
        cfg=SimpleNamespace(log_chat_id=0),
        worker_instance_id="worker-a",
        redis=redis or FakeRedis(),
        bot=None,
    )


def test_lease_owner_is_the_part_before_the_token() -> None:
    assert lease_owner("worker-a|abc") == "worker-a"
    assert lease_owner("legacy") == "unknown/legacy-worker"
    assert lease_owner(None) == "unknown/legacy-worker"


def test_reconnect_backoff_counts_failures() -> None:
    worker = make_worker()

    schedule_reconnect(worker, 7)
    first = worker.reconnect_after[7]
    assert worker.reconnect_failures[7] == 1

    schedule_reconnect(worker, 7)
    assert worker.reconnect_failures[7] == 2
    assert worker.reconnect_after[7] > first


@pytest.mark.asyncio
async def test_lost_lease_disconnects_the_client(monkeypatch) -> None:
    monkeypatch.setattr(redis_keys, "SESSION_LEASE_RENEW_SECONDS", 0.01)
    redis = FakeRedis()
    worker = make_worker(redis)
    key = redis_keys.session_lease(1)
    redis.store[key] = "worker-a|token"
    client = FakeClient()

    task = asyncio.create_task(maintain_session_lease(worker, 1, "worker-a|token", client))
    await asyncio.sleep(0.05)
    assert client.disconnected is False

    redis.store.pop(key)  # another worker took the session over
    await asyncio.wait_for(task, timeout=2)
    assert client.disconnected is True


@pytest.mark.asyncio
async def test_release_cancels_renewal_and_deletes_own_token() -> None:
    redis = FakeRedis()
    worker = make_worker(redis)
    key = redis_keys.session_lease(1)
    redis.store[key] = "worker-a|token"
    worker.session_lease_tokens[1] = "worker-a|token"

    renewal = asyncio.create_task(asyncio.sleep(3600))
    worker.session_lease_tasks[1] = renewal

    await release_session_lease(worker, 1)

    assert renewal.cancelled()
    assert key not in redis.store
    assert 1 not in worker.session_lease_tokens
    assert 1 not in worker.session_lease_tasks


@pytest.mark.asyncio
async def test_release_keeps_a_foreign_token() -> None:
    redis = FakeRedis()
    worker = make_worker(redis)
    key = redis_keys.session_lease(1)
    redis.store[key] = "worker-b|other"
    worker.session_lease_tokens[1] = "worker-a|token"

    await release_session_lease(worker, 1)

    assert redis.store[key] == "worker-b|other"


@pytest.mark.asyncio
async def test_release_without_token_does_nothing() -> None:
    redis = FakeRedis()
    worker = make_worker(redis)

    await release_session_lease(worker, 5)

    assert redis.evals == []
