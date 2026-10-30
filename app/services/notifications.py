"""Durable Telegram notification outbox with bounded exponential retries."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, select
from aiogram import Bot

from app.storage.db import SessionFactory
from app.storage.models import AppSetting, OutboxNotification


def _destinations(value: str) -> list[tuple[int, int | None]]:
    result: list[tuple[int, int | None]] = []
    for raw in value.replace(";", ",").split(","):
        parts = raw.strip().split(":")
        try:
            chat_id = int(parts[0])
            thread_id = int(parts[1]) if len(parts) == 2 else None
        except (ValueError, IndexError):
            continue
        if len(parts) > 2 or chat_id == 0 or (thread_id is not None and thread_id <= 0):
            continue
        item = (chat_id, thread_id)
        if item not in result:
            result.append(item)
    return result


async def enqueue_notification(bot: Bot | None, text: str, category: str = "main", fallback_chat_id: int = 0) -> bool:
    """Persist notification before attempting delivery. True means durably queued."""
    if bot is None:
        return True
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, f"log_chat_ids_{category}")
        destinations = _destinations(setting.value) if setting and setting.value else []
        if setting is None and category == "main" and fallback_chat_id:
            destinations = [(fallback_chat_id, None)]
        if not destinations:
            return True
        for chat_id, thread_id in destinations:
            db.add(OutboxNotification(
                category=category,
                chat_id=chat_id,
                thread_id=thread_id,
                text=text[:3900],
                status="pending",
                attempts=0,
            ))
        await db.commit()
    await dispatch_pending_notifications(bot, limit=max(10, len(destinations)))
    return True


async def dispatch_pending_notifications(bot: Bot | None, limit: int = 50) -> int:
    if bot is None:
        return 0
    now = datetime.now(timezone.utc)
    async with SessionFactory() as db:
        await db.execute(delete(OutboxNotification).where(
            OutboxNotification.status == "sent",
            OutboxNotification.sent_at < now - timedelta(days=30),
        ))
        rows = list((await db.scalars(
            select(OutboxNotification)
            .where(
                or_(
                    (OutboxNotification.status == "pending") &
                    (or_(OutboxNotification.next_retry_at.is_(None), OutboxNotification.next_retry_at <= now)),
                    (OutboxNotification.status == "sending") &
                    (OutboxNotification.next_retry_at <= now),
                )
            )
            .order_by(OutboxNotification.created_at, OutboxNotification.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )).all())
        payloads = []
        for row in rows:
            row.status = "sending"
            row.attempts += 1
            row.next_retry_at = now + timedelta(minutes=2)
            payloads.append((row.id, row.chat_id, row.thread_id, row.text, row.attempts))
        await db.commit()

    delivered = 0
    for row_id, chat_id, thread_id, message_text, attempts in payloads:
        try:
            kwargs = {"message_thread_id": thread_id} if thread_id is not None else {}
            await bot.send_message(chat_id, message_text, **kwargs)
            async with SessionFactory() as db:
                row = await db.get(OutboxNotification, row_id)
                if row:
                    row.status = "sent"
                    row.sent_at = datetime.now(timezone.utc)
                    row.next_retry_at = None
                    row.last_error = ""
                    await db.commit()
            delivered += 1
        except Exception as exc:
            retry_after = getattr(exc, "retry_after", None)
            delay = int(retry_after) + 1 if retry_after else min(3600, 5 * (2 ** min(attempts - 1, 9)))
            async with SessionFactory() as db:
                row = await db.get(OutboxNotification, row_id)
                if row:
                    row.status = "pending"
                    row.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                    row.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                    await db.commit()
            logging.warning("Notification %s delivery failed; retry in %ss: %s", row_id, delay, exc)
    return delivered
