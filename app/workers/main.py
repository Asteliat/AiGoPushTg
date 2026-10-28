"""Watch selected dialogs; send AI replies only for accounts configured for automatic mode."""
import asyncio
import logging
from datetime import datetime, timezone
import re
from collections import defaultdict
from aiogram import Bot
from telethon import TelegramClient, events
from sqlalchemy import select
from app.ai.provider import AIUnavailableError
from app.services.drafts import create_draft
from app.settings import get_settings
from app.storage.db import SessionFactory, init_db
from app.storage.models import Account, ChatBinding, Draft, DraftStatus


async def heartbeat(redis) -> None:
    while True:
        await redis.set("atcivperson:worker:last_seen", "ok", ex=60)
        await asyncio.sleep(20)


async def refresh_bindings(watched: dict[int, dict[int, str]]) -> None:
    """Update selected chat IDs without restarting Telethon clients."""
    while True:
        async with SessionFactory() as db:
            rows = list((await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all())
        fresh: dict[int, dict[int, str]] = defaultdict(dict)
        for row in rows:
            if row.account_id in watched:
                fresh[row.account_id][row.chat_id] = row.title
        for account_id, labels in watched.items():
            labels.clear()
            labels.update(fresh.get(account_id, {}))
        await asyncio.sleep(15)


async def main() -> None:
    from redis.asyncio import Redis

    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level.upper())
    if not cfg.bot_token:
        raise RuntimeError("BOT_TOKEN не задан: worker уведомляет администраторов через бота.")
    await init_db()
    redis = Redis.from_url(cfg.redis_url, decode_responses=True)
    bot = Bot(token=cfg.bot_token)
    clients: list[TelegramClient] = []
    watched: dict[int, dict[int, str]] = {}
    try:
        async with SessionFactory() as db:
            accounts = list((await db.scalars(select(Account).where(Account.enabled.is_(True)))).all())
            bindings = list((await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all())
        initial: dict[int, dict[int, str]] = defaultdict(dict)
        for binding in bindings:
            initial[binding.account_id][binding.chat_id] = binding.title

        for account in accounts:
            client = TelegramClient(account.session_path, cfg.telegram_api_id, cfg.telegram_api_hash)
            await client.connect()
            if not await client.is_user_authorized():
                logging.error("Account %s session is unauthorized; skipping", account.id)
                await client.disconnect()
                continue
            chat_labels = dict(initial.get(account.id, {}))
            watched[account.id] = chat_labels

            async def on_message(event, *, account=account, chat_labels=chat_labels):
                # Filter before processing content: only explicitly selected dialogs are handled.
                label = chat_labels.get(event.chat_id)
                if label is None:
                    return
                if not event.message or not event.message.message:
                    return
                incoming = event.message.message.strip()
                if not incoming:
                    return
                # In groups, draft only when the account is explicitly addressed.
                if event.is_group:
                    username = (account.username or "").strip()
                    mentioned = bool(username and re.search(r"(?<!\w)@" + re.escape(username) + r"\b", incoming, re.IGNORECASE))
                    reply_to = await event.get_reply_message() if event.message.reply_to else None
                    replied_to_account = bool(reply_to and reply_to.sender_id == account.telegram_user_id)
                    if not (mentioned or replied_to_account):
                        return
                async with SessionFactory() as db:
                    try:
                        current_account = await db.get(Account, account.id)
                        reply_mode = current_account.reply_mode if current_account else "manual"
                        draft = await create_draft(
                            db,
                            incoming_text=incoming,
                            account_id=account.id,
                            chat_label=f"{account.display_name} · {label}",
                        )
                    except AIUnavailableError:
                        logging.info("AI disabled; skipped draft for account %s", account.id)
                        return
                    except Exception:
                        logging.exception("Could not create draft for account %s", account.id)
                        return

                sent_automatically = False
                if reply_mode == "automatic":
                    try:
                        # Only reply in this explicitly selected dialog. Group messages
                        # have already passed the mention/reply-to-account check above.
                        await event.reply(draft.proposed_text)
                        sent_automatically = True
                        async with SessionFactory() as db:
                            saved_draft = await db.get(Draft, draft.id)
                            if saved_draft and saved_draft.status == DraftStatus.PENDING.value:
                                saved_draft.status = DraftStatus.APPROVED.value
                                saved_draft.reviewed_at = datetime.now(timezone.utc)
                                await db.commit()
                    except Exception:
                        logging.exception("Automatic reply failed for account %s in chat %s", account.id, event.chat_id)

                for admin_id in cfg.admin_id_set:
                    try:
                        if sent_automatically:
                            text = (
                                f"Автоматический ответ отправлен\nАккаунт: {account.display_name}\nЧат: {label}\n"
                                f"Черновик #{draft.id}\n\nВходящее:\n{incoming[:1200]}\n\n"
                                f"Отправленный ответ:\n{draft.proposed_text[:1600]}"
                            )
                        elif reply_mode == "automatic":
                            text = (
                                f"Не удалось отправить автоматический ответ\nАккаунт: {account.display_name}\nЧат: {label}\n"
                                f"Черновик #{draft.id} сохранён для ручной проверки.\n\n"
                                f"Входящее:\n{incoming[:1200]}\n\nОтвет ИИ:\n{draft.proposed_text[:1600]}"
                            )
                        else:
                            text = (
                                f"Новое сообщение в выбранном чате\nАккаунт: {account.display_name}\nЧат: {label}\n\n"
                                f"Входящее:\n{incoming[:1200]}\n\nЧерновик ИИ #{draft.id}:\n{draft.proposed_text[:1600]}\n\n"
                                "Режим ручной: в чат ничего не отправлено."
                            )
                        await bot.send_message(admin_id, text)
                    except Exception:
                        logging.exception("Could not notify administrator %s", admin_id)

            client.add_event_handler(on_message, events.NewMessage(incoming=True))
            clients.append(client)
            logging.info("Connected account %s; currently watching %d selected chats", account.id, len(chat_labels))

        if not clients:
            logging.warning("No authorized accounts. Connect an account using enroll.py.")
        tasks = [
            asyncio.create_task(heartbeat(redis)),
            asyncio.create_task(refresh_bindings(watched)),
        ]
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
