"""Dynamic account worker with per-account pause and automatic eligible-chat discovery."""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from aiogram import Bot
from telethon import TelegramClient, events
from sqlalchemy import select
from app.ai.provider import AIUnavailableError, summarize_chat_context
from app.services.drafts import create_draft, global_ai_enabled
from app.settings import get_settings
from app.storage.db import SessionFactory, init_db
from app.storage.models import Account, AIAgent, ChatBinding, Draft, DraftStatus
from app.telegram_gateway.proxy import telethon_proxy

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
    learning_tasks: dict[int,asyncio.Task]={}
    last_discovery: dict[int,datetime] = {}

    async def on_message(event, *, aid, labels):
        label=labels.get(event.chat_id)
        if label is None or not event.message or not event.message.message:
            return
        incoming=event.message.message.strip()
        if not incoming:
            return
        async with SessionFactory() as db:
            current=await db.get(Account,aid)
            if not current or not current.enabled or current.manual_pause or not current.ai_enabled or not await global_ai_enabled(db):
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
        async with SessionFactory() as db:
            latest = await db.get(Account, aid)
            if not latest or not latest.enabled or latest.manual_pause:
                return
            mode = latest.reply_mode
            account_name = latest.display_name
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



    async def finalize_learning_buffer(aid: int, key: str, sample_limit: int) -> None:
        raw_items = await redis.lrange(key, 0, -1)
        parsed = []
        for raw in raw_items:
            try:
                parsed.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        unique = {}
        for item in parsed:
            unique[(item.get("chat_id"), item.get("message_id"))] = item
        samples = sorted(unique.values(), key=lambda item: item.get("date", ""))[-sample_limit:]
        summary_saved = False
        try:
            async with SessionFactory() as db:
                account = await db.get(Account, aid)
                if not account:
                    await redis.delete(key)
                    return
                if samples:
                    agent = await db.get(AIAgent, account.ai_agent_id) if account.ai_agent_id else None
                    if not agent or not agent.enabled or not agent.api_key.strip():
                        agent = await db.scalar(select(AIAgent).where(
                            AIAgent.enabled.is_(True), AIAgent.is_default.is_(True)
                        ).order_by(AIAgent.id))
                    if not agent:
                        agent = await db.scalar(select(AIAgent).where(
                            AIAgent.enabled.is_(True)
                        ).order_by(AIAgent.id))
                    if agent and agent.api_key.strip():
                        account.learning_prompt = await summarize_chat_context(agent=agent, messages=samples)
                        summary_saved = True
                account.learning_status = "idle"
                account.learning_started_at = None
                account.learning_ends_at = None
                await db.commit()
        except Exception:
            logging.exception("Could not finalize learning buffer for account %s", aid)
            async with SessionFactory() as db:
                account = await db.get(Account, aid)
                if account:
                    account.learning_status = "idle"
                    account.learning_started_at = None
                    account.learning_ends_at = None
                    await db.commit()
        finally:
            await redis.delete(key)
        if bot:
            for admin_id in cfg.admin_id_set:
                try:
                    await bot.send_message(
                        admin_id,
                        f"Обучение аккаунта #{aid} завершено. "
                        f"{'Краткий контекст сохранён для черновиков.' if summary_saved else 'Сводка не сохранена: нет подходящих сообщений или доступного ИИ-агента.'}",
                    )
                except Exception:
                    logging.exception("Could not notify administrator about learning completion")

    async def run_learning(aid: int, client: TelegramClient) -> None:
        handler = None
        key = None
        try:
            async with SessionFactory() as db:
                account = await db.get(Account, aid)
                if not account or account.learning_status not in {"training", "stopping"}:
                    return
                started = account.learning_started_at or datetime.now(timezone.utc)
                sample_limit = min(1000, max(500, account.learning_limit))
                bindings = list((await db.scalars(
                    select(ChatBinding).where(
                        ChatBinding.account_id == aid,
                        ChatBinding.enabled.is_(True),
                    )
                )).all())
            if not bindings:
                async with SessionFactory() as db:
                    account = await db.get(Account, aid)
                    if account:
                        account.learning_status = "idle"
                        account.learning_started_at = None
                        account.learning_ends_at = None
                        await db.commit()
                logging.warning("Learning for account %s stopped: no selected chats", aid)
                return

            chat_ids = [binding.chat_id for binding in bindings]
            labels = {binding.chat_id: f"Чат {index + 1}" for index, binding in enumerate(bindings)}
            key = f"atcivperson:learning:{aid}:{int(started.timestamp())}"
            had_buffer = bool(await redis.exists(key))

            async def capture_training_message(event):
                message = event.message
                body = (message.message or "").strip()
                if not body:
                    return
                item = {
                    "chat_id": int(event.chat_id),
                    "message_id": int(message.id),
                    "chat": labels.get(event.chat_id, "выбранный чат"),
                    "text": body[:450],
                    "outgoing": bool(message.out),
                    "date": message.date.isoformat() if message.date else "",
                }
                await redis.lpush(key, json.dumps(item, ensure_ascii=False))
                await redis.ltrim(key, 0, sample_limit - 1)
                await redis.expire(key, 7 * 24 * 3600)

            client.add_event_handler(capture_training_message, events.NewMessage(chats=chat_ids))
            handler = capture_training_message

            if not had_buffer:
                history = []
                for binding in bindings:
                    try:
                        async for message in client.iter_messages(binding.chat_id, limit=sample_limit):
                            body = (message.message or "").strip()
                            if not body:
                                continue
                            history.append({
                                "chat_id": int(binding.chat_id),
                                "message_id": int(message.id),
                                "chat": labels.get(binding.chat_id, "выбранный чат"),
                                "text": body[:450],
                                "outgoing": bool(message.out),
                                "date": message.date.isoformat() if message.date else "",
                            })
                    except Exception:
                        logging.exception("Could not read training history for account %s, chat %s", aid, binding.chat_id)
                history.sort(key=lambda item: item["date"], reverse=True)
                unique = {}
                for item in history:
                    unique[(item["chat_id"], item["message_id"])] = item
                for item in list(unique.values())[:sample_limit]:
                    await redis.rpush(key, json.dumps(item, ensure_ascii=False))
                await redis.ltrim(key, 0, sample_limit - 1)
                await redis.expire(key, 7 * 24 * 3600)

            while True:
                await asyncio.sleep(10)
                async with SessionFactory() as db:
                    account = await db.get(Account, aid)
                    if not account or account.learning_status != "training":
                        break
                    current_chat_ids = set((await db.scalars(select(ChatBinding.chat_id).where(
                        ChatBinding.account_id == aid, ChatBinding.enabled.is_(True)
                    ))).all())
                    if current_chat_ids != set(chat_ids):
                        break
                    expires = account.learning_ends_at
                    if expires and expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if expires and datetime.now(timezone.utc) >= expires:
                        break

            client.remove_event_handler(handler)
            handler = None
            raw_items = await redis.lrange(key, 0, -1)
            parsed = []
            for raw in raw_items:
                try:
                    parsed.append(json.loads(raw))
                except (TypeError, ValueError):
                    continue
            unique = {}
            for item in parsed:
                unique[(item.get("chat_id"), item.get("message_id"))] = item
            samples = sorted(unique.values(), key=lambda item: item.get("date", ""))[-sample_limit:]

            summary_saved = False
            async with SessionFactory() as db:
                account = await db.get(Account, aid)
                if not account:
                    await redis.delete(key)
                    return
                if samples:
                    agent = await db.get(AIAgent, account.ai_agent_id) if account.ai_agent_id else None
                    if not agent or not agent.enabled or not agent.api_key.strip():
                        agent = await db.scalar(
                            select(AIAgent).where(
                                AIAgent.enabled.is_(True),
                                AIAgent.is_default.is_(True),
                            ).order_by(AIAgent.id)
                        )
                    if not agent:
                        agent = await db.scalar(
                            select(AIAgent).where(AIAgent.enabled.is_(True)).order_by(AIAgent.id)
                        )
                    if agent:
                        account.learning_prompt = await summarize_chat_context(agent=agent, messages=samples)
                        summary_saved = True
                    else:
                        logging.warning("No enabled AI agent for learning on account %s", aid)
                account.learning_status = "idle"
                account.learning_started_at = None
                account.learning_ends_at = None
                await db.commit()
            await redis.delete(key)
            if bot:
                for admin_id in cfg.admin_id_set:
                    try:
                        await bot.send_message(
                            admin_id,
                            f"Обучение аккаунта #{aid} завершено. "
                            f"{'Краткий контекст сохранён для черновиков.' if summary_saved else 'Сводка не сохранена: нет подходящих сообщений или доступного ИИ-агента.'}",
                        )
                    except Exception:
                        logging.exception("Could not notify administrator about learning completion")
        except asyncio.CancelledError:
            if handler:
                client.remove_event_handler(handler)
            raise
        except Exception:
            if handler:
                client.remove_event_handler(handler)
            logging.exception("Learning failed for account %s", aid)
            async with SessionFactory() as db:
                account = await db.get(Account, aid)
                if account:
                    account.learning_status = "idle"
                    account.learning_started_at = None
                    account.learning_ends_at = None
                    await db.commit()
            if key:
                await redis.delete(key)

    async def sync_account_dialogs(client: TelegramClient, aid: int) -> dict[int, str]:
        discovered = []
        try:
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if getattr(entity, "deleted", False) or getattr(entity, "bot", False):
                    continue
                title = (dialog.name or "").strip()
                if not title:
                    continue
                discovered.append((int(dialog.id), title[:255]))
            async with SessionFactory() as db:
                current_bindings = list((await db.scalars(
                    select(ChatBinding).where(ChatBinding.account_id == aid)
                )).all())
                by_chat = {binding.chat_id: binding for binding in current_bindings}
                for chat_id, title in discovered:
                    binding = by_chat.get(chat_id)
                    if binding:
                        binding.title = title
                    else:
                        db.add(ChatBinding(
                            account_id=aid, chat_id=chat_id, title=title,
                            enabled=True, auto_discovered=True,
                        ))
                await db.commit()
            logging.info("Discovered %d eligible dialogs for account %s", len(discovered), aid)
        except Exception:
            # A transient history/dialogs failure must not take the whole account offline.
            logging.exception("Could not refresh dialogs for account %s", aid)
        finally:
            last_discovery[aid] = datetime.now(timezone.utc)
        async with SessionFactory() as db:
            current_bindings = list((await db.scalars(
                select(ChatBinding).where(
                    ChatBinding.account_id == aid,
                    ChatBinding.enabled.is_(True),
                )
            )).all())
        return {binding.chat_id: binding.title for binding in current_bindings}

    async def connect_account(account, binding_rows):
        if not cfg.telegram_api_id or not cfg.telegram_api_hash:
            return
        client=TelegramClient(account.session_path,cfg.telegram_api_id,cfg.telegram_api_hash,proxy=telethon_proxy(account.proxy_config_encrypted))
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logging.error("Account %s session is unauthorized",account.id)
                await client.disconnect()
                return
            labels = await sync_account_dialogs(client, account.id)
            watched[account.id]=labels
            client.add_event_handler(lambda event, aid=account.id, labels=labels: on_message(event,aid=aid,labels=labels),events.NewMessage(incoming=True))
            clients[account.id]=client
            tasks[account.id]=asyncio.create_task(client.run_until_disconnected())
            account_snapshot[account.id]=account
            logging.info("Connected account %s; watching %d eligible chats",account.id,len(labels))
        except Exception:
            logging.exception("Could not connect account %s",account.id)
            if client.is_connected():
                await client.disconnect()

    async def manager():
        while True:
            try:
                async with SessionFactory() as db:
                    all_accounts=list((await db.scalars(select(Account).where(Account.enabled.is_(True)))).all())
                    bindings=list((await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all())
                accounts=[account for account in all_accounts if not account.manual_pause]
                active={a.id:a for a in accounts}
                now = datetime.now(timezone.utc)
                for account in all_accounts:
                    expires = account.learning_ends_at
                    if expires and expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if account.learning_status in {"training", "stopping"} and account.id not in clients:
                        should_finalize = account.learning_status == "stopping" or (expires and now >= expires)
                        if should_finalize:
                            started = account.learning_started_at
                            if started:
                                key = f"atcivperson:learning:{account.id}:{int(started.timestamp())}"
                                await finalize_learning_buffer(account.id, key, min(1000, max(500, account.learning_limit)))
                            else:
                                async with SessionFactory() as cleanup_db:
                                    current = await cleanup_db.get(Account, account.id)
                                    if current:
                                        current.learning_status = "idle"
                                        current.learning_started_at = None
                                        current.learning_ends_at = None
                                        await cleanup_db.commit()
                            account.learning_status = "idle"
                            account.learning_started_at = None
                            account.learning_ends_at = None
                for aid in list(clients):
                    existing_task=tasks.get(aid)
                    if aid not in active or (existing_task is not None and existing_task.done()):
                        learning = learning_tasks.pop(aid, None)
                        if learning and not learning.done():
                            learning.cancel()
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
                        if now - last_discovery.get(aid, datetime.min.replace(tzinfo=timezone.utc)) >= timedelta(minutes=5):
                            labels.clear()
                            labels.update(await sync_account_dialogs(clients[aid], aid))
                        else:
                            labels.clear()
                            labels.update({b.chat_id:b.title for b in by_account.get(aid,[])})
                    if account.learning_status in {"training", "stopping"} and aid in clients:
                        learning = learning_tasks.get(aid)
                        if learning is None or learning.done():
                            learning_tasks[aid] = asyncio.create_task(run_learning(aid, clients[aid]))
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
        for task in learning_tasks.values():
            task.cancel()
        for client in clients.values():
            if client.is_connected():
                await client.disconnect()
        if bot:
            await bot.session.close()
        await redis.aclose()

if __name__=="__main__":
    asyncio.run(main())
