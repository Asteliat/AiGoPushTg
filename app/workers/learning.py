"""Account context learning: collect a buffer, then summarise it with the AI agent."""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from telethon import events

from app.ai.provider import summarize_chat_context
from app.storage.db import SessionFactory
from app.storage.models import AIAgent, Account, ChannelBinding, ChatBinding
from app.workers import redis_keys


async def finalize_learning_buffer(
    *, redis, send_log, aid: int, key: str, sample_limit: int
) -> None:
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
                    account.learning_completed_at = datetime.now(timezone.utc)
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
    await send_log(
        f"🧠 Обучение аккаунта #{aid} завершено. "
        f"{'Краткий контекст сохранён для черновиков.' if summary_saved else 'Сводка не сохранена: нет подходящих сообщений или доступного ИИ-агента.'}",
        category="learning",
    )


async def run_learning(*, redis, send_log, aid: int, client) -> None:
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
                discussion_ids = set((await db.scalars(
                    select(ChannelBinding.discussion_group_id).where(
                        ChannelBinding.account_id == aid,
                        ChannelBinding.discussion_group_id.is_not(None),
                    )
                )).all())
                channel_ids = set((await db.scalars(
                    select(ChannelBinding.channel_id).where(ChannelBinding.account_id == aid)
                )).all())
                excluded_chat_ids = discussion_ids | channel_ids
                bindings = [binding for binding in bindings if int(binding.chat_id) not in excluded_chat_ids]
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
            key = redis_keys.learning_buffer(aid, int(started.timestamp()))
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
                # Keep only the newest global sample while scanning many chats; avoid
                # retaining sample_limit messages for every dialog in memory.
                history_heap = []
                for binding in bindings:
                    try:
                        async for message in client.iter_messages(binding.chat_id, limit=sample_limit):
                            body = (message.message or "").strip()
                            if not body:
                                continue
                            date = message.date.isoformat() if message.date else ""
                            timestamp = message.date.timestamp() if message.date else 0.0
                            item = {
                                "chat_id": int(binding.chat_id),
                                "message_id": int(message.id),
                                "chat": labels.get(binding.chat_id, "чат"),
                                "text": body[:450],
                                "outgoing": bool(message.out),
                                "date": date,
                            }
                            entry = (timestamp, int(binding.chat_id), int(message.id), item)
                            if len(history_heap) < sample_limit:
                                heapq.heappush(history_heap, entry)
                            elif entry[:3] > history_heap[0][:3]:
                                heapq.heapreplace(history_heap, entry)
                    except Exception:
                        logging.exception("Could not read training history for account %s, chat %s", aid, binding.chat_id)
                history = [entry[3] for entry in history_heap]
                history.sort(key=lambda item: item["date"], reverse=True)
                for item in history[:sample_limit]:
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
                    discussion_ids = set((await db.scalars(select(ChannelBinding.discussion_group_id).where(
                        ChannelBinding.account_id == aid,
                        ChannelBinding.discussion_group_id.is_not(None),
                    ))).all())
                    channel_ids = set((await db.scalars(select(ChannelBinding.channel_id).where(
                        ChannelBinding.account_id == aid,
                    ))).all())
                    current_chat_ids -= discussion_ids | channel_ids
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
                        account.learning_completed_at = datetime.now(timezone.utc)
                        summary_saved = True
                    else:
                        logging.warning("No enabled AI agent for learning on account %s", aid)
                account.learning_status = "idle"
                account.learning_started_at = None
                account.learning_ends_at = None
                await db.commit()
            await redis.delete(key)
            await send_log(
                f"🧠 Обучение аккаунта #{aid} завершено. "
                f"{'Краткий контекст сохранён для черновиков.' if summary_saved else 'Сводка не сохранена: нет подходящих сообщений или доступного ИИ-агента.'}",
                category="learning",
            )
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
