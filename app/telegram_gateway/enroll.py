"""Authorize a Telegram account from a trusted terminal, never through the control bot."""
import asyncio
import os
import re
from telethon import TelegramClient
from sqlalchemy import select
from app.settings import get_settings
from app.storage.db import SessionFactory, init_db
from app.storage.models import Account

async def main() -> None:
    cfg = get_settings()
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        raise SystemExit("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    folder = cfg.data_dir / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    phone = input("Номер телефона вашего Telegram-аккаунта: ").strip()
    if not re.fullmatch(r"\+?[0-9]{7,16}", phone):
        raise SystemExit("Некорректный номер.")
    session_path = folder / ("account_" + re.sub(r"\D", "", phone)[-12:])
    client = TelegramClient(str(session_path), cfg.telegram_api_id, cfg.telegram_api_hash)
    await client.start(phone=phone)
    try:
        me = await client.get_me()
        if me is None:
            raise RuntimeError("Telegram не вернул профиль.")
        name = " ".join(x for x in [me.first_name, me.last_name] if x) or str(me.id)
        async with SessionFactory() as db:
            row = await db.scalar(select(Account).where(Account.telegram_user_id == me.id))
            if row:
                row.username, row.display_name, row.session_path, row.enabled = me.username, name, str(session_path), True
            else:
                db.add(Account(telegram_user_id=me.id, username=me.username, display_name=name, session_path=str(session_path), enabled=True))
            await db.commit()
        os.chmod(str(session_path) + ".session", 0o600)
        print(f"Подключено: {name} (id={me.id}). Сессия: {session_path}.session")
        print("Не копируйте session-файл в Git или чаты.")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    async def run() -> None:
        await init_db()
        await main()
    asyncio.run(run())
