"""Dynamic account worker with per-account pause and automatic eligible-chat discovery."""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from aiogram import Bot
from sqlalchemy import select
from app.workers.channel_monitor import install_channel_monitor
from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import Account, ChatBinding
from app.services.notifications import dispatch_pending_notifications
from app.workers.challenge_watcher import ChallengeWatcher
from app.workers.account_status import set_connection_status
from app.workers.connection import AccountConnector
from app.workers.reply_pipeline import on_message
from app.workers.state import WorkerState
from app.workers.learning import finalize_learning_buffer, run_learning
from app.workers.dialogs_sync import sync_account_dialogs
from app.workers.lease import (
    release_session_lease,
    schedule_reconnect,
)
from app.workers import redis_keys

async def heartbeat(redis):
    while True:
        await redis.set(redis_keys.WORKER_HEARTBEAT, "ok", ex=60)
        await asyncio.sleep(20)

async def main():
    from redis.asyncio import Redis

    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level.upper())
    worker = WorkerState(
        cfg=cfg,
        worker_instance_id=f"{os.getenv('HOSTNAME') or 'worker'}:{os.getpid()}",
        redis=Redis.from_url(cfg.redis_url, decode_responses=True),
        bot=Bot(token=cfg.bot_token) if cfg.bot_token else None,
    )
    async def notification_dispatcher():
        while True:
            try:
                await dispatch_pending_notifications(worker.bot, limit=100)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Notification outbox dispatcher failed")
            await asyncio.sleep(5)

    # Verification prompts are passed automatically for our own accounts only;
    # prompts addressed to other group members are ignored silently.
    challenge_watcher = ChallengeWatcher(redis=worker.redis, send_log=worker.send_log)

    async def manager():
        while True:
            try:
                async with SessionFactory() as db:
                    all_accounts=list((await db.scalars(select(Account))).all())
                    bindings=list((await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all())
                all_by_id={account.id:account for account in all_accounts}
                accounts=[account for account in all_accounts if account.enabled and not account.manual_pause]
                active={a.id:a for a in accounts}
                now = datetime.now(timezone.utc)
                for account in all_accounts:
                    expires = account.learning_ends_at
                    if expires and expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if account.learning_status in {"training", "stopping"} and account.id not in worker.clients:
                        should_finalize = (
                            account.learning_status == "stopping"
                            or account.learning_started_at is None
                            or account.learning_ends_at is None
                            or (expires and now >= expires)
                        )
                        if should_finalize:
                            started = account.learning_started_at
                            if started:
                                key = redis_keys.learning_buffer(account.id, int(started.timestamp()))
                                await finalize_learning_buffer(
                                redis=worker.redis,
                                send_log=worker.send_log,
                                aid=account.id,
                                key=key,
                                sample_limit=min(1000, max(500, account.learning_limit)),
                            )
                            else:
                                async with SessionFactory() as cleanup_db:
                                    current = await cleanup_db.get(Account, account.id)
                                    if current:
                                        current.learning_status = "idle"
                                        current.learning_started_at = None
                                        current.learning_ends_at = None
                                        await cleanup_db.commit()
                            account.learning_status = "idle"
                            account.learning_started_at = None
                            account.learning_ends_at = None
                for aid in list(worker.clients):
                    existing_task=worker.tasks.get(aid)
                    previous_snapshot = worker.account_snapshot.get(aid)
                    latest_account = all_by_id.get(aid)
                    runtime_config_changed = bool(
                        previous_snapshot and latest_account and (
                            previous_snapshot.proxy_config_encrypted != latest_account.proxy_config_encrypted
                            or previous_snapshot.session_path != latest_account.session_path
                        )
                    )
                    if existing_task is not None and existing_task.done():
                        schedule_reconnect(worker, aid)
                    if aid not in active or runtime_config_changed or (existing_task is not None and existing_task.done()):
                        learning = worker.learning_tasks.pop(aid, None)
                        if learning and not learning.done():
                            learning.cancel()
                        task=worker.tasks.pop(aid,None)
                        if task and not task.done():
                            task.cancel()
                        client=worker.clients.pop(aid,None)
                        if client and client.is_connected():
                            await client.disconnect()
                        await release_session_lease(worker, aid)
                        worker.watched.pop(aid,None); worker.account_snapshot.pop(aid,None)
                        previous = all_by_id.get(aid)
                        status = "paused" if previous and previous.manual_pause else ("disconnected" if not previous or not previous.enabled else "error")
                        await set_connection_status(aid, status)
                by_account=defaultdict(list)
                for b in bindings:
                    by_account[b.account_id].append(b)
                for aid,account in active.items():
                    if aid not in worker.clients:
                        if now >= worker.reconnect_after.get(aid, datetime.min.replace(tzinfo=timezone.utc)):
                            await connector.connect(account)
                    else:
                        labels=worker.watched.get(aid,{})
                        if now - worker.last_discovery.get(aid, datetime.min.replace(tzinfo=timezone.utc)) >= timedelta(minutes=5):
                            labels.clear()
                            labels.update(
                                await sync_account_dialogs(
                                    worker.clients[aid], aid, last_discovery=worker.last_discovery
                                )
                            )
                        else:
                            labels.clear()
                            labels.update({b.chat_id:b.title for b in by_account.get(aid,[])})
                        # UI actions such as resuming an account set the DB status to
                        # "connecting" immediately. If the Telethon client never disconnected,
                        # connect_account() won't run again, so reconcile the status here.
                        if worker.clients[aid].is_connected() and account.connection_status != "connected":
                            await set_connection_status(aid, "connected")
                    if account.learning_status in {"training", "stopping"} and aid in worker.clients:
                        learning = worker.learning_tasks.get(aid)
                        if learning is None or learning.done():
                            worker.learning_tasks[aid] = asyncio.create_task(
                                run_learning(redis=worker.redis, send_log=worker.send_log, aid=aid, client=worker.clients[aid])
                            )
                if not worker.clients:
                    logging.info("No connected accounts yet; waiting for accounts to be added in the bot")
            except Exception:
                logging.exception("Account manager iteration failed")
            await asyncio.sleep(10)

    connector = AccountConnector(
        worker,
        on_message=on_message,
        challenge_watcher=challenge_watcher,
        install_monitor=install_channel_monitor,
    )
    heartbeat_task=asyncio.create_task(heartbeat(worker.redis))
    manager_task=asyncio.create_task(manager())
    notification_task=asyncio.create_task(notification_dispatcher())
    try:
        await asyncio.gather(heartbeat_task,manager_task,notification_task)
    finally:
        # Cancel and await everything before touching connections: a handler that
        # outlives shutdown would write into a closed Redis or a dead session.
        for task in (heartbeat_task, manager_task, notification_task):
            task.cancel()
        await asyncio.gather(
            heartbeat_task, manager_task, notification_task, return_exceptions=True
        )
        await worker.cancel_background_tasks()
        for aid, client in list(worker.clients.items()):
            if client.is_connected():
                await client.disconnect()
            await release_session_lease(worker, aid)
        if worker.bot:
            await worker.bot.session.close()
        await worker.redis.aclose()

if __name__=="__main__":
    asyncio.run(main())
