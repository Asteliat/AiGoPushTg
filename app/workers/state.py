"""Mutable state of one worker process.

The worker used to keep fourteen dictionaries in the closure of ``main()``, which made
every helper implicitly depend on all of them. They live in one object now: what a
function needs is visible in its signature, and shutdown can cancel and await the same
collections it starts tasks in.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from aiogram import Bot
from redis.asyncio import Redis
from telethon import TelegramClient

from app.services.notifications import enqueue_notification
from app.settings import Settings
from app.storage.models import Account


@dataclass(slots=True)
class WorkerState:
    """Connections, per-account tasks and retry bookkeeping of this worker."""

    cfg: Settings
    worker_instance_id: str
    redis: Redis
    bot: Bot | None
    clients: dict[int, TelegramClient] = field(default_factory=dict)
    tasks: dict[int, asyncio.Task] = field(default_factory=dict)
    watched: dict[int, dict[int, str]] = field(default_factory=dict)
    account_snapshot: dict[int, Account] = field(default_factory=dict)
    learning_tasks: dict[int, asyncio.Task] = field(default_factory=dict)
    last_discovery: dict[int, datetime] = field(default_factory=dict)
    reconnect_failures: dict[int, int] = field(default_factory=dict)
    reconnect_after: dict[int, datetime] = field(default_factory=dict)
    session_lease_tasks: dict[int, asyncio.Task] = field(default_factory=dict)
    session_lease_tokens: dict[int, str] = field(default_factory=dict)

    async def send_log(self, text: str, category: str = "main") -> bool:
        """Queue a notification durably.

        The outbox commit is the delivery guarantee: Telegram delivery is retried
        asynchronously and survives worker restarts.
        """
        return await enqueue_notification(self.bot, text, category, self.cfg.log_chat_id)

    def background_tasks(self) -> list[asyncio.Task]:
        """Every task this worker owns, for cancellation on shutdown."""
        tasks = [
            *self.tasks.values(),
            *self.learning_tasks.values(),
            *self.session_lease_tasks.values(),
        ]
        return [task for task in tasks if task is not None and not task.done()]

    async def cancel_background_tasks(self) -> None:
        """Cancel and await owned tasks so handlers never run after shutdown."""
        pending = self.background_tasks()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
