import asyncio

import pytest

from app.telegram_gateway import joining


class FakeFloodWait(Exception):
    def __init__(self, seconds):
        self.seconds = seconds


def test_stop_mode_propagates_floodwait(monkeypatch):
    monkeypatch.setattr(joining, "FloodWaitError", FakeFloodWait)

    async def operation():
        raise FakeFloodWait(42)

    with pytest.raises(FakeFloodWait):
        asyncio.run(joining.run_join_with_floodwait(operation, mode="stop"))


@pytest.mark.asyncio
async def test_wait_mode_sleeps_and_retries_until_success(monkeypatch):
    monkeypatch.setattr(joining, "FloodWaitError", FakeFloodWait)
    waits = []
    progress = []
    attempts = 0

    async def fake_sleep(seconds):
        waits.append(seconds)

    async def on_wait(seconds):
        progress.append(seconds)

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise FakeFloodWait(7 + attempts)
        return "joined"

    monkeypatch.setattr(joining.asyncio, "sleep", fake_sleep)
    result = await joining.run_join_with_floodwait(operation, mode="wait", on_wait=on_wait)

    assert result == "joined"
    assert attempts == 3
    assert waits == [8, 9]
    assert progress == [8, 9]


@pytest.mark.asyncio
async def test_wait_mode_works_without_progress_callback(monkeypatch):
    monkeypatch.setattr(joining, "FloodWaitError", FakeFloodWait)
    sleeps = []
    attempts = 0

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FakeFloodWait(0)
        return True

    monkeypatch.setattr(joining.asyncio, "sleep", fake_sleep)
    assert await joining.run_join_with_floodwait(operation, mode="wait") is True
    assert sleeps == [1]
