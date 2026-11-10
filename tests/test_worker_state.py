import asyncio
import pathlib
from types import SimpleNamespace

import pytest

from app.workers import state as state_module
from app.workers.state import WorkerState

BOT = SimpleNamespace(session=SimpleNamespace(close=lambda: None))


def make_state(**overrides) -> WorkerState:
    values = {
        "cfg": SimpleNamespace(log_chat_id=-100123),
        "worker_instance_id": "worker-1",
        "redis": SimpleNamespace(),
        "bot": BOT,
    }
    values.update(overrides)
    return WorkerState(**values)


@pytest.mark.asyncio
async def test_send_log_uses_the_outbox_and_the_configured_chat(monkeypatch) -> None:
    calls = {}

    async def fake_enqueue(bot, text, category="main", fallback_chat_id=0):
        calls.update(bot=bot, text=text, category=category, fallback=fallback_chat_id)
        return True

    monkeypatch.setattr(state_module, "enqueue_notification", fake_enqueue)
    worker = make_state()

    assert await worker.send_log("hello", category="replies") is True
    assert calls == {
        "bot": BOT,
        "text": "hello",
        "category": "replies",
        "fallback": -100123,
    }


@pytest.mark.asyncio
async def test_cancel_background_tasks_cancels_and_awaits_them() -> None:
    started = asyncio.Event()
    finished = asyncio.Event()

    async def long_running():
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            finished.set()
            raise

    worker = make_state()
    task = asyncio.create_task(long_running())
    worker.tasks[1] = task
    worker.learning_tasks[2] = asyncio.create_task(long_running())
    await started.wait()

    await worker.cancel_background_tasks()

    assert task.cancelled()
    assert finished.is_set()
    assert worker.background_tasks() == []


def test_background_tasks_skips_completed_tasks() -> None:
    class FinishedTask:
        def done(self) -> bool:
            return True

        def cancel(self) -> None:
            raise AssertionError("a finished task must not be cancelled")

    worker = make_state()
    worker.tasks[1] = FinishedTask()
    assert worker.background_tasks() == []


def test_state_collections_are_per_instance() -> None:
    first, second = make_state(), make_state()
    first.clients[1] = "client"
    assert second.clients == {}
    assert first.watched is not second.watched


def test_every_worker_module_is_importable() -> None:
    for module in pathlib.Path("app/workers").glob("*.py"):
        if module.name == "__init__.py":
            continue
        __import__(f"app.workers.{module.stem}")
