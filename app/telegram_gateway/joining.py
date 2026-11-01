"""Controlled handling of Telegram FloodWait errors during chat joins."""

import asyncio
from collections.abc import Awaitable, Callable

from telethon.errors import FloodWaitError


async def run_join_with_floodwait(
    operation: Callable[[], Awaitable[object]],
    *,
    mode: str = "stop",
    on_wait: Callable[[int], Awaitable[None]] | None = None,
):
    """Run one join request, optionally waiting and retrying until Telegram accepts it."""
    while True:
        try:
            return await operation()
        except FloodWaitError as exc:
            if mode != "wait":
                raise
            seconds = max(1, int(exc.seconds))
            if on_wait is not None:
                await on_wait(seconds)
            await asyncio.sleep(seconds)
