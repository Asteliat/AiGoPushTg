"""Watch only operator-selected chats and forward AI drafts to the operator; never send to chats."""
import asyncio
import logging
from collections import defaultdict
from aiogram import Bot
from telethon import TelegramClient, events
from sqlalchemy import select
from app.ai.provider import AIUnavailableError
from app.services.drafts import create_draft
from app.settings import get_settings
from app.storage.db import SessionFactory, init_db
from app.storage.models import Account, ChatBinding

async def heartbeat(redis):
    while True:
        await redis.set("atcivperson:worker:last_seen", "ok", ex=60)
        await asyncio.sleep(20)

async def main() -> None:
    from redis.asyncio import Redis
    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level.upper())
    if not cfg.bot_token:
        raise RuntimeError("BOT_TOKEN не задан: worker отправляет уведомления только администраторам.")
    await init_db()
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    bot = Bot(token=cfg.bot_token)
    clients: list[TelegramClient] = []
    try:
        async with SessionFactory() as db:
            accounts = list((await db.scalars(select(Account).where(Account.enabled.is_(True)))).all())
            bindings = list((await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all())
        account_map = {a.id: a for a in accounts}
        by_account: dict[int, list[ChatBinding]] = defaultdict(list)
        for binding in bindings:
            if binding.account_id in account_map:
                by_account[binding.account_id].append(binding)

        for account_id, chats in by_account.items():
            account = account_map[account_id]
            client = TelegramClient(account.session_path, cfg.telegram_api_id, cfg.telegram_api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                logging.error("Account %s session is unauthorized; skipping", account_id)
                await client.disconnect()
                continue
            chat_labels = {item.chat_id: item.title for item in chats}
            chat_ids = list(chat_labels.keys())

            async def on_message(event, *, account_id=account_id, chat_labels=chat_labels, account=account):
                if not event.message or not event.message.message:
                    return
                incoming = event.message.message.strip()
                if not incoming:
                    return
                label = chat_labels.get(event.chat_id, "Выбранный чат")
                async with SessionFactory() as db:
                    try:
                        draft = await create_draft(
                            db,
                            incoming_text=incoming,
                            account_id=account_id,
                            chat_label=f"{account.display_name} · {label}",
                        )
                    except AIUnavailableError:
                        logging.info("AI disabled; skipped draft for account %s", account_id)
                        return
                    except Exception:
                        logging.exception("Could not create draft for account %s", account_id)
                        return
                for admin_id in cfg.admin_id_set:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"Новое сообщение в выбранном чате\nАккаунт: {account.display_name}\nЧат: {label}\n\n"
                            f"Входящее:\n{incoming[:1400]}\n\nЧерновик ИИ #{draft.id}:\n{draft.proposed_text[:1800]}\n\n"
                            "Это только подсказка. В чат ничего не отправлено.",
                        )
                    except Exception:
                        logging.exception("Could not notify administrator %s", admin_id)

            client.add_event_handler(on_message, events.NewMessage(incoming=True, chats=chat_ids))
            clients.append(client)
            logging.info("Watching %d selected chats for account %s", len(chat_ids), account_id)

        if not clients:
            logging.warning("No authorized accounts with selected chats. Use dialogs.py and select_chat.py.")
        tasks = [asyncio.create_task(heartbeat(redis))]
        tasks.extend(asyncio.create_task(client.run_until_disconnected()) for client in clients)
        await asyncio.gather(*tasks)
    finally:
        for client in clients:
            if client.is_connected():
                await client.disconnect()
        await bot.session.close()
        await redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())
