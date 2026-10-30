"""Dynamic account worker with per-account pause and automatic eligible-chat discovery."""
import asyncio
import heapq
import json
import logging
import os
import re
import random
import secrets
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from aiogram import Bot
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from sqlalchemy import select
from app.ai.provider import AIUnavailableError, NoReplyNeeded, summarize_chat_context
from app.services.ai_responses import generate_response, global_ai_enabled
from app.workers.channel_monitor import install_channel_monitor
from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, AppSetting, ChatBinding
from app.services.notifications import enqueue_notification, dispatch_pending_notifications
from app.services.chat_bindings import discovered_binding_values
from app.telegram_gateway.proxy import telethon_proxy

async def heartbeat(redis):
    while True:
        await redis.set("atcivperson:worker:last_seen", "ok", ex=60)
        await asyncio.sleep(20)

async def main():
    from redis.asyncio import Redis
    cfg=get_settings()
    logging.basicConfig(level=cfg.log_level.upper())
    redis=Redis.from_url(cfg.redis_url,decode_responses=True)
    bot=Bot(token=cfg.bot_token) if cfg.bot_token else None
    clients: dict[int,TelegramClient]={}
    tasks: dict[int,asyncio.Task]={}
    watched: dict[int,dict[int,str]]={}
    account_snapshot: dict[int,Account]={}
    learning_tasks: dict[int,asyncio.Task]={}
    last_discovery: dict[int,datetime] = {}
    reconnect_failures: dict[int, int] = {}
    reconnect_after: dict[int, datetime] = {}
    session_lease_tasks: dict[int, asyncio.Task] = {}
    session_lease_tokens: dict[int, str] = {}

    async def send_log(text: str, category: str = "main") -> bool:
        # The outbox commit is the delivery guarantee; Telegram delivery is retried
        # asynchronously and survives worker restarts.
        return await enqueue_notification(bot, text, category, cfg.log_chat_id)

    async def notification_dispatcher():
        while True:
            try:
                await dispatch_pending_notifications(bot, limit=100)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Notification outbox dispatcher failed")
            await asyncio.sleep(5)

    async def remember_message(aid: int, chat_id: int, sender_id: int, item: dict) -> None:
        chat_key = f"atcivperson:memory:{aid}:{chat_id}:chat"
        person_key = f"atcivperson:memory:{aid}:{chat_id}:person:{sender_id}"
        raw = json.dumps(item, ensure_ascii=False)
        for key, limit in ((chat_key, 30), (person_key, 16)):
            await redis.lpush(key, raw)
            await redis.ltrim(key, 0, limit - 1)
            await redis.expire(key, 30 * 24 * 3600)

    async def read_conversation_context(aid: int, chat_id: int, sender_id: int) -> str:
        keys = [
            f"atcivperson:memory:{aid}:{chat_id}:chat",
            f"atcivperson:memory:{aid}:{chat_id}:person:{sender_id}",
        ]
        merged = {}
        for key in keys:
            for raw in reversed(await redis.lrange(key, 0, 11)):
                try:
                    item = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                merged[(item.get("message_id"), item.get("sender_id"), item.get("text"))] = item
        items = sorted(merged.values(), key=lambda x: x.get("date", ""))[-18:]
        return "\n".join(
            f"[{str(item.get('speaker', 'Участник'))[:60]}] {str(item.get('text', ''))[:500]}"
            for item in items if str(item.get("text", "")).strip()
        )[-7000:]

    async def automatic_send_allowed(aid: int, chat_id: int, expected_agent_id: int, *, allow_unbound: bool = False) -> bool:
        async with SessionFactory() as db:
            account = await db.get(Account, aid)
            if (
                not account or not account.enabled or account.manual_pause
                or not account.ai_enabled
                or account.reply_mode != "automatic"
                or account.learning_status in {"training", "stopping"}
                or account.ai_agent_id != expected_agent_id
                or not await global_ai_enabled(db)
            ):
                return False
            binding = None
            if not allow_unbound:
                binding = await db.scalar(select(ChatBinding).where(
                    ChatBinding.account_id == aid,
                    ChatBinding.chat_id == chat_id,
                    ChatBinding.enabled.is_(True),
                ))
            agent = await db.get(AIAgent, account.ai_agent_id) if account.ai_agent_id else None
            return bool(agent and agent.enabled and (allow_unbound or binding))

    async def on_message(event, *, aid, labels, allow_unbound: bool = False, forced_label: str | None = None):
        label = forced_label or labels.get(event.chat_id)
        if (label is None and not allow_unbound) or not event.message or not event.message.message:
            return
        incoming = event.message.message.strip()
        if not incoming or event.message.out:
            return
        sender_id = int(event.sender_id or 0)
        try:
            sender = await event.get_sender()
            speaker = " ".join(filter(None, [
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            ])).strip() or getattr(sender, "title", None) or getattr(sender, "username", None) or f"Участник {sender_id}"
        except Exception:
            speaker = f"Участник {sender_id}"

        async with SessionFactory() as db:
            current = await db.get(Account, aid)
            if (
                not current or not current.enabled or current.manual_pause
                or not current.ai_enabled or not await global_ai_enabled(db)
            ):
                return
            if current.learning_status in {"training", "stopping"}:
                # Discussion-group replies routed from a published channel comment
                # are part of the account's Chats learning context, without requiring
                # that linked discussion group to be added to chat_bindings.
                if allow_unbound and current.learning_status == "training" and current.learning_started_at:
                    started = current.learning_started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    learning_key = f"atcivperson:learning:{aid}:{int(started.timestamp())}"
                    learning_item = {
                        "chat_id": int(event.chat_id),
                        "message_id": int(event.message.id),
                        "chat": label or "обсуждение канала",
                        "text": incoming[:450],
                        "outgoing": False,
                        "date": event.message.date.isoformat() if event.message.date else datetime.now(timezone.utc).isoformat(),
                    }
                    await redis.lpush(learning_key, json.dumps(learning_item, ensure_ascii=False))
                    await redis.ltrim(learning_key, 0, min(1000, max(500, int(current.learning_limit or 500))) - 1)
                    await redis.expire(learning_key, 7 * 24 * 3600)
                return
            if not current.ai_agent_id:
                logging.info("Account %s has AI enabled but no agent assigned", aid)
                return

            # Store a rolling chat history and a separate history for each participant.
            # Redis expires conversation records after 30 days.
            context = await read_conversation_context(aid, int(event.chat_id), sender_id)
            item = {
                "message_id": int(event.message.id),
                "sender_id": sender_id,
                "speaker": speaker[:80],
                "text": incoming[:700],
                "date": event.message.date.isoformat() if event.message.date else "",
            }
            await remember_message(aid, int(event.chat_id), sender_id, item)

            agent = await db.get(AIAgent, current.ai_agent_id)
            if not agent or not agent.enabled:
                return

            proactive = False
            if event.is_group:
                username = (current.username or "").strip().lstrip("@")
                mentioned = bool(username and re.search(r"(?<!\w)@" + re.escape(username) + r"\b", incoming, re.IGNORECASE))
                reply_to = await event.get_reply_message() if event.message.reply_to else None
                addressed = mentioned or (reply_to and reply_to.sender_id == current.telegram_user_id)
                if addressed:
                    probability_key = "mention_reply_probability"
                elif current.ai_identity_disclosed:
                    probability_key = "proactive_reply_probability"
                    proactive = True
                else:
                    return
            elif event.is_private:
                probability_key = "dm_reply_probability"
            else:
                return

            setting = await db.get(AppSetting, probability_key)
            global_probability = int(setting.value) if setting and setting.value.isdigit() else (0 if probability_key == "proactive_reply_probability" else 100)
            override = getattr(agent, probability_key + "_override")
            probability = override if override is not None else global_probability
            if probability <= 0 or random.randint(1, 100) > min(probability, 100):
                logging.info("Reply skipped by %s probability (%s%%) for account %s in chat %s", probability_key, probability, aid, event.chat_id)
                return

            cooldown_setting = await db.get(AppSetting, "reply_cooldown_seconds")
            global_cooldown = int(cooldown_setting.value) if cooldown_setting and cooldown_setting.value.isdigit() else 0
            cooldown_override = agent.reply_cooldown_seconds_override
            cooldown_seconds = cooldown_override if cooldown_override is not None else global_cooldown
            if cooldown_seconds > 0:
                allowed = await redis.set(
                    f"atcivperson:reply-cooldown:{aid}:{event.chat_id}", "1",
                    ex=cooldown_seconds, nx=True,
                )
                if not allowed:
                    logging.info("Reply skipped by cooldown for account %s in chat %s", aid, event.chat_id)
                    return
            try:
                draft = await generate_response(
                    db, incoming_text=incoming, account_id=aid,
                    chat_label=f"{current.display_name} · {label}",
                    conversation_context=context, proactive=proactive,
                )
            except NoReplyNeeded:
                logging.info("AI judged proactive message unnecessary for account %s in chat %s", aid, event.chat_id)
                return
            except AIUnavailableError as exc:
                logging.warning("AI generation failed for account %s: %s", aid, exc)
                await send_log(f"⚠️ Ошибка генерации ИИ\nАккаунт: {current.display_name} (#{aid})\nЧат: {label}\nОшибка: {str(exc)[:900]}", category="errors")
                return
            except Exception as exc:
                logging.exception("Could not generate response for account %s", aid)
                await send_log(f"⚠️ Не удалось создать ответ\nАккаунт: {current.display_name} (#{aid})\nЧат: {label}\nОшибка: {type(exc).__name__}: {str(exc)[:700]}", category="errors")
                return
            mode = current.reply_mode
            account_name = current.display_name

        async with SessionFactory() as db:
            latest = await db.get(Account, aid)
            if not latest or not latest.enabled or latest.manual_pause:
                return
            mode = latest.reply_mode
            account_name = latest.display_name
            expected_agent_id = latest.ai_agent_id

        sent = False
        if mode == "automatic":
            if not await automatic_send_allowed(aid, int(event.chat_id), int(expected_agent_id or 0), allow_unbound=allow_unbound):
                logging.info("Automatic reply cancelled because account/chat/AI settings changed during generation (account %s, chat %s)", aid, event.chat_id)
                return
            try:
                sent_message = await (event.client.send_message(event.chat_id, draft.proposed_text) if proactive else event.reply(draft.proposed_text))
                sent = True
            except FloodWaitError as exc:
                wait_seconds = max(1, int(getattr(exc, "seconds", 1))) + 1
                logging.warning("Telegram FloodWait for account %s: retry in %ss", aid, wait_seconds)
                await asyncio.sleep(wait_seconds)
                if not await automatic_send_allowed(aid, int(event.chat_id), int(expected_agent_id or 0), allow_unbound=allow_unbound):
                    return
                try:
                    sent_message = await (event.client.send_message(event.chat_id, draft.proposed_text) if proactive else event.reply(draft.proposed_text))
                    sent = True
                except Exception as retry_exc:
                    logging.exception("Automatic reply retry failed for account %s in chat %s", aid, event.chat_id)
                    await send_log(f"⚠️ Повторная отправка ответа не удалась\\nАккаунт: {account_name} (#{aid})\\nЧат: {label}\\nОшибка: {type(retry_exc).__name__}: {str(retry_exc)[:700]}", category="errors")
            except Exception as exc:
                logging.exception("Automatic reply failed for account %s in chat %s", aid, event.chat_id)
                await send_log(f"⚠️ Не удалось отправить ответ\nАккаунт: {account_name} (#{aid})\nЧат: {label}\nОшибка: {type(exc).__name__}: {str(exc)[:700]}", category="errors")
            if sent:
                sent_item = {
                    "message_id": int(getattr(sent_message, "id", 0)),
                    "sender_id": int(current.telegram_user_id or 0),
                    "speaker": account_name[:80],
                    "text": draft.proposed_text[:700],
                    "date": datetime.now(timezone.utc).isoformat(),
                }
                await remember_message(aid, int(event.chat_id), int(current.telegram_user_id or 0), sent_item)

        if sent:
            text = f"✅ Ответ отправлен\nАккаунт: {account_name} (#{aid})\nЧат: {label}\nОтвет: {draft.proposed_text[:1800]}"
        elif mode == "automatic":
            text = f"⚠️ Ответ не отправлен\nАккаунт: {account_name} (#{aid})\nЧат: {label}\nТекст не сохранён; повторите обработку после проверки настроек."
        else:
            text = f"💬 Ответ подготовлен\nАккаунт: {account_name} (#{aid})\nЧат: {label}\nВходящее: {incoming[:900]}\nОтвет ИИ: {draft.proposed_text[:1400]}\nРучной режим: в чат ничего не отправлено; текст не сохраняется."
        await send_log(text, category="replies")

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

    async def set_connection_status(aid: int, status: str) -> None:
        async with SessionFactory() as db:
            account = await db.get(Account, aid)
            if account:
                account.connection_status = status
                await db.commit()

    async def sync_account_dialogs(client: TelegramClient, aid: int, attempt: int = 0) -> dict[int, str]:
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
                            account_id=aid, chat_id=chat_id,
                            **discovered_binding_values(title),
                        ))
                await db.commit()
            logging.info("Discovered %d eligible dialogs for account %s", len(discovered), aid)
        except FloodWaitError as exc:
            wait_seconds = max(1, int(getattr(exc, "seconds", 1))) + 1
            if attempt < 2:
                logging.warning("Telegram FloodWait while discovering dialogs for account %s; retry %s after %ss", aid, attempt + 1, wait_seconds)
                await asyncio.sleep(min(wait_seconds, 300))
                return await sync_account_dialogs(client, aid, attempt + 1)
            logging.error("Dialog discovery paused after repeated Telegram FloodWait for account %s", aid)
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

    def schedule_reconnect(aid: int) -> None:
        failures = reconnect_failures.get(aid, 0) + 1
        reconnect_failures[aid] = failures
        delay = min(300, 5 * (2 ** min(failures - 1, 6)))
        reconnect_after[aid] = datetime.now(timezone.utc) + timedelta(seconds=delay)
        logging.warning("Telegram account %s reconnect scheduled in %ss (attempt %s)", aid, delay, failures)

    async def maintain_session_lease(aid: int, token: str, client: TelegramClient) -> None:
        key = f"atcivperson:telegram-session:{aid}"
        script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], 90) else return 0 end"
        while True:
            await asyncio.sleep(20)
            try:
                renewed = await redis.eval(script, 1, key, token)
            except Exception:
                logging.exception("Could not renew Telegram session lease for account %s", aid)
                continue
            if not renewed:
                logging.error("Telegram session lease lost for account %s; disconnecting", aid)
                if client.is_connected():
                    await client.disconnect()
                return

    async def release_session_lease(aid: int) -> None:
        task = session_lease_tasks.pop(aid, None)
        if task and not task.done():
            task.cancel()
        token = session_lease_tokens.pop(aid, None)
        if token:
            key = f"atcivperson:telegram-session:{aid}"
            script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
            try:
                await redis.eval(script, 1, key, token)
            except Exception:
                logging.exception("Could not release Telegram session lease for account %s", aid)

    async def connect_account(account, binding_rows):
        if not cfg.telegram_api_id or not cfg.telegram_api_hash:
            await set_connection_status(account.id, "error")
            schedule_reconnect(account.id)
            return
        session_file = str(account.session_path)
        if not session_file.endswith(".session"):
            session_file += ".session"
        if os.path.isfile(session_file):
            try:
                os.chmod(session_file, 0o600)
            except OSError:
                logging.exception("Could not harden session file permissions for account %s", account.id)
        lease_key = f"atcivperson:telegram-session:{account.id}"
        lease_token = secrets.token_urlsafe(24)
        if not await redis.set(lease_key, lease_token, nx=True, ex=90):
            logging.warning("Telegram session for account %s is already owned by another Worker", account.id)
            schedule_reconnect(account.id)
            return
        await set_connection_status(account.id, "connecting")
        client=TelegramClient(account.session_path,cfg.telegram_api_id,cfg.telegram_api_hash,proxy=telethon_proxy(account.proxy_config_encrypted),connection_retries=5,retry_delay=2,auto_reconnect=True)
        lease_task = asyncio.create_task(maintain_session_lease(account.id, lease_token, client))
        try:
            await client.connect()
            if not await client.is_user_authorized():
                logging.error("Account %s session is unauthorized",account.id)
                await set_connection_status(account.id, "unauthorized")
                await client.disconnect()
                lease_task.cancel()
                await redis.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end", 1, lease_key, lease_token)
                schedule_reconnect(account.id)
                return
            labels = await sync_account_dialogs(client, account.id)
            await set_connection_status(account.id, "connected")
            watched[account.id]=labels
            client.add_event_handler(lambda event, aid=account.id, labels=labels: on_message(event,aid=aid,labels=labels),events.NewMessage(incoming=True))

            async def route_discussion_reply(event, channel_title):
                await on_message(
                    event,
                    aid=account.id,
                    labels=watched.get(account.id, {}),
                    allow_unbound=True,
                    forced_label=f"Обсуждение канала · {channel_title}",
                )

            install_channel_monitor(client, account.id, redis, send_log, route_chat_reply=route_discussion_reply)
            await send_log(f"🟢 Аккаунт подключён: {account.display_name} (#{account.id}). Отслеживается чатов: {len(labels)}.", category="main")
            clients[account.id]=client
            tasks[account.id]=asyncio.create_task(client.run_until_disconnected())
            session_lease_tasks[account.id] = lease_task
            session_lease_tokens[account.id] = lease_token
            account_snapshot[account.id]=account
            reconnect_failures.pop(account.id, None)
            reconnect_after.pop(account.id, None)
            logging.info("Connected account %s; watching %d eligible chats",account.id,len(labels))
        except Exception:
            logging.exception("Could not connect account %s",account.id)
            await set_connection_status(account.id, "error")
            schedule_reconnect(account.id)
            lease_task.cancel()
            try:
                await redis.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end", 1, lease_key, lease_token)
            except Exception:
                logging.exception("Could not release failed Telegram session lease for account %s", account.id)
            if client.is_connected():
                await client.disconnect()

    async def manager():
        while True:
            try:
                async with SessionFactory() as db:
                    all_accounts=list((await db.scalars(select(Account))).all())
                    bindings=list((await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all())
                all_by_id={account.id:account for account in all_accounts}
                accounts=[account for account in all_accounts if account.enabled and not account.manual_pause]
                active={a.id:a for a in accounts}
                now = datetime.now(timezone.utc)
                for account in all_accounts:
                    expires = account.learning_ends_at
                    if expires and expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if account.learning_status in {"training", "stopping"} and account.id not in clients:
                        should_finalize = (
                            account.learning_status == "stopping"
                            or account.learning_started_at is None
                            or account.learning_ends_at is None
                            or (expires and now >= expires)
                        )
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
                    previous_snapshot = account_snapshot.get(aid)
                    latest_account = all_by_id.get(aid)
                    runtime_config_changed = bool(
                        previous_snapshot and latest_account and (
                            previous_snapshot.proxy_config_encrypted != latest_account.proxy_config_encrypted
                            or previous_snapshot.session_path != latest_account.session_path
                        )
                    )
                    if existing_task is not None and existing_task.done():
                        schedule_reconnect(aid)
                    if aid not in active or runtime_config_changed or (existing_task is not None and existing_task.done()):
                        learning = learning_tasks.pop(aid, None)
                        if learning and not learning.done():
                            learning.cancel()
                        task=tasks.pop(aid,None)
                        if task and not task.done():
                            task.cancel()
                        client=clients.pop(aid,None)
                        if client and client.is_connected():
                            await client.disconnect()
                        await release_session_lease(aid)
                        watched.pop(aid,None); account_snapshot.pop(aid,None)
                        previous = all_by_id.get(aid)
                        status = "paused" if previous and previous.manual_pause else ("disconnected" if not previous or not previous.enabled else "error")
                        await set_connection_status(aid, status)
                by_account=defaultdict(list)
                for b in bindings:
                    by_account[b.account_id].append(b)
                for aid,account in active.items():
                    if aid not in clients:
                        if now >= reconnect_after.get(aid, datetime.min.replace(tzinfo=timezone.utc)):
                            await connect_account(account,by_account.get(aid,[]))
                    else:
                        labels=watched.get(aid,{})
                        if now - last_discovery.get(aid, datetime.min.replace(tzinfo=timezone.utc)) >= timedelta(minutes=5):
                            labels.clear()
                            labels.update(await sync_account_dialogs(clients[aid], aid))
                        else:
                            labels.clear()
                            labels.update({b.chat_id:b.title for b in by_account.get(aid,[])})
                        # UI actions such as resuming an account set the DB status to
                        # "connecting" immediately. If the Telethon client never disconnected,
                        # connect_account() won't run again, so reconcile the status here.
                        if clients[aid].is_connected() and account.connection_status != "connected":
                            await set_connection_status(aid, "connected")
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
    notification_task=asyncio.create_task(notification_dispatcher())
    try:
        await asyncio.gather(heartbeat_task,manager_task,notification_task)
    finally:
        heartbeat_task.cancel(); manager_task.cancel(); notification_task.cancel()
        for task in tasks.values():
            task.cancel()
        for task in learning_tasks.values():
            task.cancel()
        for aid, client in list(clients.items()):
            if client.is_connected():
                await client.disconnect()
            await release_session_lease(aid)
        if bot:
            await bot.session.close()
        await redis.aclose()

if __name__=="__main__":
    asyncio.run(main())
