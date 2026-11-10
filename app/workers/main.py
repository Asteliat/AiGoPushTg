"""Worker entry point: build the state, run three loops, shut everything down cleanly."""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot
from redis.asyncio import Redis

from app.services.notifications import dispatch_pending_notifications
from app.settings import get_settings
from app.workers import redis_keys
from app.workers.challenge_watcher import ChallengeWatcher
from app.workers.channel_monitor import install_channel_monitor
from app.workers.connection import AccountConnector
from app.workers.lease import release_session_lease
from app.workers.manager import manager_loop
from app.workers.reply_pipeline import on_message
from app.workers.state import WorkerState


async def heartbeat(redis: Redis) -> None:
    """Tell the bot that this worker is alive."""
    while True:
        await redis.set(redis_keys.WORKER_HEARTBEAT, "ok", ex=60)
        await asyncio.sleep(20)


async def notification_dispatcher(worker: WorkerState) -> None:
    """Deliver queued notifications even when nothing else is happening."""
    while True:
        try:
            await dispatch_pending_notifications(worker.bot, limit=100)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Notification outbox dispatcher failed")
        await asyncio.sleep(5)


async def shutdown(worker: WorkerState, tasks: list[asyncio.Task]) -> None:
    """Cancel and await every task before touching connections or storage."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await worker.cancel_background_tasks()
    for account_id, client in list(worker.clients.items()):
        if client.is_connected():
            await client.disconnect()
        await release_session_lease(worker, account_id)
    if worker.bot:
        await worker.bot.session.close()
    await worker.redis.aclose()


async def main() -> None:
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level.upper())
    worker = WorkerState(
        cfg=cfg,
        worker_instance_id=f"{os.getenv('HOSTNAME') or 'worker'}:{os.getpid()}",
        redis=Redis.from_url(cfg.redis_url, decode_responses=True),
        bot=Bot(token=cfg.bot_token) if cfg.bot_token else None,
    )
    # Verification prompts are passed automatically for our own accounts only;
    # prompts addressed to other group members are ignored silently.
    connector = AccountConnector(
        worker,
        on_message=on_message,
        challenge_watcher=ChallengeWatcher(redis=worker.redis, send_log=worker.send_log),
        install_monitor=install_channel_monitor,
    )
    tasks = [
        asyncio.create_task(heartbeat(worker.redis)),
        asyncio.create_task(manager_loop(worker, connector)),
        asyncio.create_task(notification_dispatcher(worker)),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        await shutdown(worker, tasks)


if __name__ == "__main__":
    asyncio.run(main())
