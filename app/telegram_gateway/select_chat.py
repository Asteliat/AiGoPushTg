"""Select an existing dialog for operator-reviewed message drafting; does not join or send."""
import argparse
import asyncio
from telethon import TelegramClient
from sqlalchemy import select
from app.settings import get_settings
from app.storage.db import SessionFactory, init_db
from app.storage.models import Account, ChatBinding

async def main() -> None:
    parser = argparse.ArgumentParser(description="Enable or disable monitoring for an existing Telegram dialog.")
    parser.add_argument("account_id", type=int)
    parser.add_argument("chat_id", type=int, help="ID printed by app.telegram_gateway.dialogs")
    parser.add_argument("--disable", action="store_true", help="Stop monitoring this selected dialog")
    args = parser.parse_args()
    cfg = get_settings()
    await init_db()
    async with SessionFactory() as db:
        account = await db.get(Account, args.account_id)
        if not account or not account.enabled:
            raise SystemExit("Аккаунт не найден или выключен.")
    client = TelegramClient(account.session_path, cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise SystemExit("Сессия не авторизована; повторите подключение аккаунта.")
        selected = None
        async for dialog in client.iter_dialogs(limit=None):
            if dialog.id == args.chat_id:
                selected = dialog
                break
        if selected is None:
            raise SystemExit("Диалог не найден среди существующих диалогов аккаунта. Сначала проверьте ID через dialogs.py.")
        async with SessionFactory() as db:
            binding = await db.scalar(
                select(ChatBinding).where(
                    ChatBinding.account_id == account.id,
                    ChatBinding.chat_id == args.chat_id,
                )
            )
            if binding:
                binding.enabled = not args.disable
                binding.title = selected.name
            else:
                binding = ChatBinding(
                    account_id=account.id,
                    chat_id=args.chat_id,
                    title=selected.name,
                    enabled=not args.disable,
                )
                db.add(binding)
            await db.commit()
        print(f"{'Отключён' if args.disable else 'Выбран'} чат «{selected.name}» (id={args.chat_id}) для аккаунта #{account.id}.")
        print("Сообщения не отправляются автоматически; при включённом ИИ оператор получает только черновики.")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
