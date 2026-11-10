"""Static private-chat autoresponder used while the account's AI is disabled."""

from __future__ import annotations

import logging
import re

from sqlalchemy import select

from app.storage.db import SessionFactory
from app.storage.models import Account, AppSetting
from app.workers import redis_keys

_MESSAGE_DEDUPE_TTL = 30 * 24 * 3600
_TRUTHY = frozenset({"1", "true", "yes", "on"})


async def process_static_autoresponder(
    event,
    *,
    db,
    account,
    incoming: str,
    sender_id: int,
    redis,
    session_factory=SessionFactory,
) -> bool:
    """Private-chat autoresponder, strictly disabled whenever account AI is enabled."""
    if not event.is_private or event.message.out or not incoming.strip():
        return False
    aid = int(account.id)
    values = {}
    keys = [
        f"autoresponder.{aid}.enabled",
        f"autoresponder.{aid}.keywords",
        f"autoresponder.{aid}.response",
        f"autoresponder.{aid}.cooldown_seconds",
    ]
    rows = (await db.scalars(select(AppSetting).where(AppSetting.key.in_(keys)))).all()
    values = {row.key: row.value for row in rows}
    if values.get(keys[0], "false").lower() not in _TRUTHY:
        return False
    response = (values.get(keys[2], "") or "").strip()
    if not response:
        return False
    terms = [
        item.strip().casefold()
        for item in re.split(r"[,;\n]", values.get(keys[1], ""))
        if item.strip()
    ]
    folded = incoming.casefold()
    if terms and not any(term in folded for term in terms):
        return False

    # Reserve the message before sending so a worker reconnect cannot double-reply.
    dedupe_key = redis_keys.autoresponder_message(aid, int(event.chat_id), int(event.message.id))
    reserved = await redis.set(dedupe_key, "1", nx=True, ex=_MESSAGE_DEDUPE_TTL)
    if not reserved:
        return True

    # Re-read the account immediately before the side effect. AI-enabled accounts
    # must never receive this static responder, even if the setting changed mid-event.
    async with session_factory() as check_db:
        fresh = await check_db.get(Account, aid)
        if not fresh or not fresh.enabled or fresh.manual_pause or fresh.ai_enabled:
            await redis.delete(dedupe_key)
            return False

    try:
        cooldown = max(0, min(86400, int(values.get(keys[3], "0") or 0)))
    except ValueError:
        cooldown = 0
    if cooldown:
        cooldown_key = redis_keys.autoresponder_cooldown(aid, sender_id)
        allowed = await redis.set(cooldown_key, "1", nx=True, ex=cooldown)
        if not allowed:
            return True
    try:
        await event.client.send_message(
            int(event.chat_id), response[:4096], reply_to=int(event.message.id)
        )
    except Exception:
        # Do not auto-retry a possibly accepted message; operator can inspect logs.
        logging.exception("Static autoresponder failed: account=%s chat=%s", aid, event.chat_id)
    return True
