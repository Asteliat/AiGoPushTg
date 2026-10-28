"""Inspect dialogs of one already-authorized account without sending messages."""
import asyncio
import sys
from telethon import TelegramClient
from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import Account
from app.telegram_gateway.proxy import telethon_proxy

async def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("Использование: python -m app.telegram_gateway.dialogs ID_АККАУНТА")
    cfg = get_settings()
    async with SessionFactory() as db:
        account = await db.get(Account, int(sys.argv[1]))
    if not account or not account.enabled:
        raise SystemExit("Аккаунт не найден или отключён.")
    client = TelegramClient(account.session_path, cfg.telegram_api_id, cfg.telegram_api_hash, proxy=telethon_proxy(account.proxy_config_encrypted))
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Сессия не авторизована; повторите подключение через enroll.")
        print(f"Диалоги аккаунта {account.display_name}:")
        async for dialog in client.iter_dialogs(limit=100):
            entity = dialog.entity
            kind = "группа/супергруппа" if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False) else ("канал" if getattr(entity, "broadcast", False) else "диалог")
            print(f"id={dialog.id}\ttype={kind}\tunread={dialog.unread_count}\ttitle={dialog.name}")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
