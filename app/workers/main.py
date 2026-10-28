"""Dynamic account worker. Only explicitly selected chats are observed."""
import asyncio
import logging
import re
from datetime import datetime, timezone
from collections import defaultdict
from aiogram import Bot
from telethon import TelegramClient, events
from sqlalchemy import select
from app.ai.provider import AIUnavailableError
from app.services.drafts import create_draft, global_ai_enabled
from app.settings import get_settings
from app.storage.db import SessionFactory, init_db
from app.storage.models import Account, ChatBinding, Draft, DraftStatus

async def heartbeat(redis):
    while True:
        await redis.set("atcivperson:worker:last_seen", "ok", ex=60)
        await asyncio.sleep(20)

async def main():
    from redis.asyncio import Redis
    cfg=get_settings()
    logging.basicConfig(level=cfg.log_level.upper())
    await init_db()
    redis=Redis.from_url(cfg.redis_url,decode_responses=True)
    bot=Bot(token=cfg.bot_token) if cfg.bot_token else None
    clients: dict[int,TelegramClient]={}
    tasks: dict[int,asyncio.Task]={}
    watched: dict[int,dict[int,str]]={}
    account_snapshot: dict[int,Account]={}

    async def on_message(event, *, aid, labels):
        label=labels.get(event.chat_id)
        if label is None or not event.message or not event.message.message:
            return
        incoming=event.message.message.strip()
        if not incoming:
            return
        async with SessionFactory() as db:
            current=await db.get(Account,aid)
            if not current or not current.enabled or not current.ai_enabled or not await global_ai_enabled(db):
                return
            if not current.ai_agent_id:
                logging.info("Account %s has AI enabled but no agent assigned",aid)
                return
            if event.is_group:
                username=(current.username or "").strip()
                mentioned=bool(username and re.search(r"(?<!\w)@"+re.escape(username)+r"\b",incoming,re.IGNORECASE))
                reply_to=await event.get_reply_message() if event.message.reply_to else None
                if not (mentioned or (reply_to and reply_to.sender_id==current.telegram_user_id)):
                    return
            try:
                draft=await create_draft(db,incoming_text=incoming,account_id=aid,chat_label=f"{current.display_name} · {label}")
            except AIUnavailableError as exc:
                logging.warning("AI generation failed for account %s: %s",aid,exc)
                return
            except Exception:
                logging.exception("Could not create draft for account %s",aid)
                return
            mode=current.reply_mode
            account_name=current.display_name
        sent=False
        if mode=="automatic":
            try:
                await event.reply(draft.proposed_text)
                sent=True
                async with SessionFactory() as db:
                    saved=await db.get(Draft,draft.id)
                    if saved and saved.status==DraftStatus.PENDING:
                        saved.status=DraftStatus.APPROVED
                        saved.reviewed_at=datetime.now(timezone.utc)
                        await db.commit()
            except Exception:
                logging.exception("Automatic reply failed for account %s in chat %s",aid,event.chat_id)
        if bot:
            for admin_id in cfg.admin_id_set:
                try:
                    if sent:
                        text=f"Автоматический ответ отправлен\\nАккаунт: {account_name}\\nЧат: {label}\\nЧерновик #{draft.id}\\n\\nВходящее:\\n{incoming[:1200]}\\n\\nОтвет:\\n{draft.proposed_text[:1600]}"
                    elif mode=="automatic":
                        text=f"Не удалось отправить ответ автоматически\\nАккаунт: {account_name}\\nЧат: {label}\\nЧерновик #{draft.id} сохранён для проверки.\\n\\nВходящее:\\n{incoming[:1200]}\\n\\nОтвет ИИ:\\n{draft.proposed_text[:1600]}"
                    else:
                        text=f"Новое сообщение\\nАккаунт: {account_name}\\nЧат: {label}\\n\\nВходящее:\\n{incoming[:1200]}\\n\\nЧерновик #{draft.id}:\\n{draft.proposed_text[:1600]}\\n\\nРучной режим: в чат ничего не отправлено."
                    await bot.send_message(admin_id,text)
                except Exception:
                    logging.exception("Could not notify administrator %s",admin_id)

    async def connect_account(account, binding_rows):
        if not cfg.telegram_api_id or not cfg.telegram_api_hash:
            return
        client=TelegramClient(account.session_path,cfg.telegram_api_id,cfg.telegram_api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logging.error("Account %s session is unauthorized",account.id)
                await client.disconnect()
                return
            labels={b.chat_id:b.title for b in binding_rows if b.account_id==account.id and b.enabled}
            watched[account.id]=labels
            client.add_event_handler(lambda event, aid=account.id, labels=labels: on_message(event,aid=aid,labels=labels),events.NewMessage(incoming=True))
            clients[account.id]=client
            tasks[account.id]=asyncio.create_task(client.run_until_disconnected())
            account_snapshot[account.id]=account
            logging.info("Connected account %s; watching %d selected chats",account.id,len(labels))
        except Exception:
            logging.exception("Could not connect account %s",account.id)
            if client.is_connected():
                await client.disconnect()

    async def manager():
        while True:
            try:
                async with SessionFactory() as db:
                    accounts=list((await db.scalars(select(Account).where(Account.enabled.is_(True)))).all())
                    bindings=list((await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all())
                active={a.id:a for a in accounts}
                for aid in list(clients):
                    existing_task=tasks.get(aid)
                    if aid not in active or (existing_task is not None and existing_task.done()):
                        task=tasks.pop(aid,None)
                        if task and not task.done():
                            task.cancel()
                        client=clients.pop(aid,None)
                        if client and client.is_connected():
                            await client.disconnect()
                        watched.pop(aid,None); account_snapshot.pop(aid,None)
                by_account=defaultdict(list)
                for b in bindings:
                    by_account[b.account_id].append(b)
                for aid,account in active.items():
                    if aid not in clients:
                        await connect_account(account,by_account.get(aid,[]))
                    else:
                        labels=watched.get(aid,{})
                        labels.clear()
                        labels.update({b.chat_id:b.title for b in by_account.get(aid,[])})
                if not clients:
                    logging.info("No connected accounts yet; waiting for accounts to be added in the bot")
            except Exception:
                logging.exception("Account manager iteration failed")
            await asyncio.sleep(10)

    heartbeat_task=asyncio.create_task(heartbeat(redis))
    manager_task=asyncio.create_task(manager())
    try:
        await asyncio.gather(heartbeat_task,manager_task)
    finally:
        heartbeat_task.cancel(); manager_task.cancel()
        for task in tasks.values():
            task.cancel()
        for client in clients.values():
            if client.is_connected():
                await client.disconnect()
        if bot:
            await bot.session.close()
        await redis.aclose()

if __name__=="__main__":
    asyncio.run(main())
