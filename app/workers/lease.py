"""Session leases: only one worker may drive one Telegram session at a time.

The lease is a Redis key whose value is ``"<worker instance>|<random token>"``. It is
renewed by a background task, released only by its owner (compare-and-delete), and a
lost lease disconnects the client so the session is not used by two processes.
"""
from __future__ import annotations

import asyncio
import logging

from app.workers import redis_keys
from app.workers.reconnect import reconnect_at
from app.workers.state import WorkerState

_logger = logging.getLogger(__name__)


def lease_owner(value: object) -> str:
    """Worker instance that owns a lease value, for diagnostics."""
    text = str(value or "")
    return text.split("|", 1)[0] if "|" in text else "unknown/legacy-worker"


def schedule_reconnect(worker: WorkerState, account_id: int) -> None:
    """Count a failed connection and set the next attempt time with backoff."""
    failures = worker.reconnect_failures.get(account_id, 0) + 1
    worker.reconnect_failures[account_id] = failures
    worker.reconnect_after[account_id] = reconnect_at(failures)
    _logger.warning(
        "Telegram account %s reconnect scheduled (attempt %s)", account_id, failures
    )


async def maintain_session_lease(
    worker: WorkerState, account_id: int, token: str, client
) -> None:
    """Renew the lease; disconnect the client as soon as the lease is lost."""
    key = redis_keys.session_lease(account_id)
    while True:
        await asyncio.sleep(redis_keys.SESSION_LEASE_RENEW_SECONDS)
        try:
            renewed = await worker.redis.eval(
                redis_keys.CAS_EXPIRE,
                1,
                key,
                token,
                redis_keys.SESSION_LEASE_TTL_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception(
                "Could not renew Telegram session lease for account %s", account_id
            )
            continue
        if not renewed:
            _logger.error(
                "Telegram session lease lost for account %s on worker %s; disconnecting",
                account_id,
                worker.worker_instance_id,
            )
            if client.is_connected():
                await client.disconnect()
            return


async def release_session_lease(worker: WorkerState, account_id: int) -> None:
    """Stop renewing and drop the lease, but only if this worker still owns it."""
    task = worker.session_lease_tasks.pop(account_id, None)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    token = worker.session_lease_tokens.pop(account_id, None)
    if not token:
        return
    try:
        await worker.redis.eval(
            redis_keys.CAS_DELETE, 1, redis_keys.session_lease(account_id), token
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _logger.exception(
            "Could not release Telegram session lease for account %s", account_id
        )
