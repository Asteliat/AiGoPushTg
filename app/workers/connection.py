"""Connect one account: lease the Telegram session, open the client, wire handlers."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets

from telethon import TelegramClient, events

from app.telegram_gateway.proxy import telethon_proxy
from app.telegram_gateway.session import configure_telethon_sqlite_session
from app.workers import redis_keys
from app.workers.account_status import set_connection_status
from app.workers.dialogs_sync import sync_account_dialogs
from app.workers.lease import lease_owner, maintain_session_lease, schedule_reconnect


class AccountConnector:
    """Owns the connection lifecycle of the accounts assigned to this worker.

    The reply pipeline, the challenge watcher and the channel monitor are injected:
    the connector only decides when they are wired, which keeps the ordering (lease
    first, handlers last) testable without Telegram.
    """

    def __init__(
        self,
        worker,
        *,
        on_message,
        challenge_watcher,
        install_monitor,
        client_factory=TelegramClient,
    ) -> None:
        self.worker = worker
        self._on_message = on_message
        self._challenge_watcher = challenge_watcher
        self._install_monitor = install_monitor
        self._client_factory = client_factory

    async def connect(self, account) -> None:
        worker = self.worker

        if not worker.cfg.telegram_api_id or not worker.cfg.telegram_api_hash:
            await set_connection_status(account.id, "error")
            schedule_reconnect(worker, account.id)
            return
        session_file = str(account.session_path)
        if not session_file.endswith(".session"):
            session_file += ".session"
        if os.path.isfile(session_file):
            try:
                os.chmod(session_file, 0o600)
            except OSError:
                logging.exception("Could not harden session file permissions for account %s", account.id)

        lease_key = redis_keys.session_lease(account.id)
        lease_token = f"{worker.worker_instance_id}|{secrets.token_urlsafe(24)}"
        lease_acquired = bool(await worker.redis.set(lease_key, lease_token, nx=True, ex=90))
        if not lease_acquired:
            current_lease = await worker.redis.get(lease_key)
            owner_text = str(current_lease or "")
            owner_id = lease_owner(owner_text)
            lease_ttl = await worker.redis.ttl(lease_key)

            # A failed connection in this same process must never strand its own lease.
            # Clear it only with compare-and-delete, and only when no local client owns it.
            if owner_id == worker.worker_instance_id and account.id not in worker.clients and current_lease:
                removed = await worker.redis.eval(
                    redis_keys.CAS_DELETE, 1, lease_key, current_lease,
                )
                if removed:
                    logging.warning(
                        "Cleared stale self-owned Telegram session lease: account=%s worker=%s",
                        account.id, worker.worker_instance_id,
                    )
                    lease_acquired = bool(await worker.redis.set(
                        lease_key, lease_token, nx=True, ex=90
                    ))

            if not lease_acquired:
                logging.warning(
                    "Telegram session for account %s is owned by another Worker: "
                    "owner=%s lease_ttl=%ss this_worker=%s",
                    account.id, owner_id, lease_ttl, worker.worker_instance_id,
                )
                schedule_reconnect(worker, account.id)
                return

        client = None
        lease_task = None

        async def release_connecting_lease():
            if lease_task:
                lease_task.cancel()
                await asyncio.gather(lease_task, return_exceptions=True)
            try:
                await worker.redis.eval(
                    redis_keys.CAS_DELETE, 1, lease_key, lease_token,
                )
            except Exception:
                logging.exception(
                    "Could not release Telegram session lease for account %s on worker %s",
                    account.id, worker.worker_instance_id,
                )

        try:
            await set_connection_status(account.id, "connecting")
            client = self._client_factory(
                account.session_path,
                worker.cfg.telegram_api_id,
                worker.cfg.telegram_api_hash,
                proxy=telethon_proxy(account.proxy_config_encrypted),
                connection_retries=5,
                retry_delay=2,
                auto_reconnect=True,
            )
            configure_telethon_sqlite_session(client)
            lease_task = asyncio.create_task(
                maintain_session_lease(worker, account.id, lease_token, client)
            )
            await client.connect()
            if not await client.is_user_authorized():
                logging.error("Account %s session is unauthorized", account.id)
                await set_connection_status(account.id, "unauthorized")
                if client.is_connected():
                    await client.disconnect()
                await release_connecting_lease()
                schedule_reconnect(worker, account.id)
                return

            labels = await sync_account_dialogs(
                client, account.id, last_discovery=worker.last_discovery
            )
            await set_connection_status(account.id, "connected")
            worker.watched[account.id] = labels
            client.add_event_handler(
                lambda event, aid=account.id, labels=labels: self._on_message(
                    event, aid=aid, labels=labels, worker=worker
                ),
                events.NewMessage(incoming=True),
            )
            self._challenge_watcher.install(client, account)

            async def route_discussion_reply(event, channel_title, extra_context):
                await self._on_message(
                    event,
                    aid=account.id,
                    labels=worker.watched.get(account.id, {}),
                    worker=worker,
                    allow_unbound=True,
                    forced_label=f"Обсуждение канала · {channel_title}",
                    extra_context=extra_context,
                    force_addressed=True,
                )

            worker.monitors[account.id] = self._install_monitor(
                client, account.id, worker.redis, worker.send_log,
                route_chat_reply=route_discussion_reply,
            )
            await worker.send_log(
                f"🟢 Аккаунт подключён: {account.display_name} (#{account.id}). "
                f"Отслеживается чатов: {len(labels)}.",
                category="main",
            )
            worker.clients[account.id] = client
            worker.tasks[account.id] = asyncio.create_task(client.run_until_disconnected())
            worker.session_lease_tasks[account.id] = lease_task
            worker.session_lease_tokens[account.id] = lease_token
            worker.account_snapshot[account.id] = account
            worker.reconnect_failures.pop(account.id, None)
            worker.reconnect_after.pop(account.id, None)
            logging.info(
                "Connected account %s; watching %d eligible chats; worker=%s",
                account.id, len(labels), worker.worker_instance_id,
            )
        except asyncio.CancelledError:
            if client and client.is_connected():
                try:
                    await client.disconnect()
                except Exception:
                    logging.exception("Could not disconnect cancelled account %s", account.id)
            await release_connecting_lease()
            raise
        except Exception:
            logging.exception(
                "Could not connect account %s on worker %s", account.id, worker.worker_instance_id
            )
            schedule_reconnect(worker, account.id)
            if client and client.is_connected():
                try:
                    await client.disconnect()
                except Exception:
                    logging.exception("Could not disconnect failed account %s", account.id)
            await release_connecting_lease()
            try:
                await set_connection_status(account.id, "error")
            except Exception:
                logging.exception("Could not set error connection status for account %s", account.id)
