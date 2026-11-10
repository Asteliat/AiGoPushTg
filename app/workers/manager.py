"""Manager loop: keep the worker's clients in sync with the accounts in the database."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.storage.db import SessionFactory
from app.storage.models import Account, ChatBinding
from app.workers import redis_keys
from app.workers.account_status import set_connection_status
from app.workers.dialogs_sync import sync_account_dialogs
from app.workers.learning import finalize_learning_buffer, run_learning
from app.workers.lease import release_session_lease, schedule_reconnect

_LEARNING_STATUSES = frozenset({"training", "stopping"})
_DISCOVERY_INTERVAL = timedelta(minutes=5)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


async def manager_loop(worker, connector, *, session_factory=SessionFactory) -> None:
    """Run one manager iteration every 10 seconds until the task is cancelled."""
    while True:
        try:
            await manager_iteration(worker, connector, session_factory=session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Account manager iteration failed")
        await asyncio.sleep(10)


async def manager_iteration(worker, connector, *, session_factory=SessionFactory, now=None) -> None:
    """Reconcile the accounts in the database with the clients of this worker."""
    now = now or datetime.now(timezone.utc)
    async with session_factory() as db:
        all_accounts = list((await db.scalars(select(Account))).all())
        bindings = list(
            (await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all()
        )
    all_by_id = {account.id: account for account in all_accounts}
    accounts = [account for account in all_accounts if account.enabled and not account.manual_pause]
    active = {a.id: a for a in accounts}
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
                    async with session_factory() as cleanup_db:
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
        existing_task = worker.tasks.get(aid)
        previous_snapshot = worker.account_snapshot.get(aid)
        latest_account = all_by_id.get(aid)
        runtime_config_changed = bool(
            previous_snapshot
            and latest_account
            and (
                previous_snapshot.proxy_config_encrypted != latest_account.proxy_config_encrypted
                or previous_snapshot.session_path != latest_account.session_path
            )
        )
        if existing_task is not None and existing_task.done():
            schedule_reconnect(worker, aid)
        if (
            aid not in active
            or runtime_config_changed
            or (existing_task is not None and existing_task.done())
        ):
            learning = worker.learning_tasks.pop(aid, None)
            if learning and not learning.done():
                learning.cancel()
            task = worker.tasks.pop(aid, None)
            if task and not task.done():
                task.cancel()
            client = worker.clients.pop(aid, None)
            if client and client.is_connected():
                await client.disconnect()
            await release_session_lease(worker, aid)
            monitor = worker.monitors.pop(aid, None)
            if monitor is not None:
                await monitor.cancel_background_tasks()
            worker.watched.pop(aid, None)
            worker.account_snapshot.pop(aid, None)
            previous = all_by_id.get(aid)
            status = (
                "paused"
                if previous and previous.manual_pause
                else ("disconnected" if not previous or not previous.enabled else "error")
            )
            await set_connection_status(aid, status)
    by_account = defaultdict(list)
    for b in bindings:
        by_account[b.account_id].append(b)
    for aid, account in active.items():
        if aid not in worker.clients:
            if now >= worker.reconnect_after.get(aid, datetime.min.replace(tzinfo=timezone.utc)):
                await connector.connect(account)
        else:
            labels = worker.watched.get(aid, {})
            if now - worker.last_discovery.get(
                aid, datetime.min.replace(tzinfo=timezone.utc)
            ) >= timedelta(minutes=5):
                labels.clear()
                labels.update(
                    await sync_account_dialogs(
                        worker.clients[aid], aid, last_discovery=worker.last_discovery
                    )
                )
            else:
                labels.clear()
                labels.update({b.chat_id: b.title for b in by_account.get(aid, [])})
            # UI actions such as resuming an account set the DB status to
            # "connecting" immediately. If the Telethon client never disconnected,
            # connect_account() won't run again, so reconcile the status here.
            if worker.clients[aid].is_connected() and account.connection_status != "connected":
                await set_connection_status(aid, "connected")
        if account.learning_status in {"training", "stopping"} and aid in worker.clients:
            learning = worker.learning_tasks.get(aid)
            if learning is None or learning.done():
                worker.learning_tasks[aid] = asyncio.create_task(
                    run_learning(
                        redis=worker.redis,
                        send_log=worker.send_log,
                        aid=aid,
                        client=worker.clients[aid],
                    )
                )
    if not worker.clients:
        logging.info("No connected accounts yet; waiting for accounts to be added in the bot")
