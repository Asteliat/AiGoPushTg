"""Persist the account connection status shown in the bot."""
from __future__ import annotations

from app.storage.db import SessionFactory
from app.storage.models import Account


async def set_connection_status(aid: int, status: str) -> None:
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if account:
            account.connection_status = status
            await db.commit()
