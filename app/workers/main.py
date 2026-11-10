"""Dynamic account worker with per-account pause and automatic eligible-chat discovery."""
import asyncio
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
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from app.ai.provider import AIUnavailableError, NoReplyNeeded
from app.services.ai_responses import generate_response, resolve_runtime_override, resolve_agent_runtime_override, passes_reply_probability, global_ai_enabled
from app.workers.channel_monitor import install_channel_monitor
from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, AppSetting, ChatBinding, ChannelBinding, TriggerRule, TriggerEvent
from app.services.triggers import select_matching_trigger
from app.services.notifications import dispatch_pending_notifications
from app.telegram_gateway.proxy import telethon_proxy
from app.telegram_gateway.session import configure_telethon_sqlite_session
from app.services.challenge_monitor import (
    extract_button_labels,
    is_challenge_prompt,
)
from app.workers.challenge_watcher import ChallengeWatcher
from app.workers.state import WorkerState
from app.workers.learning import finalize_learning_buffer, run_learning
from app.workers.dialogs_sync import sync_account_dialogs
from app.workers.reconnect import reconnect_at
from app.workers.send_policy import automatic_send_allowed
from app.workers import redis_keys

async def heartbeat(redis):
    while True:
        await redis.set(redis_keys.WORKER_HEARTBEAT, "ok", ex=60)
        await asyncio.sleep(20)

async def main():
    from redis.asyncio import Redis

    cfg = get_settings()
    logging.basicConfig(level=cfg.log_level.upper())
    worker = WorkerState(
        cfg=cfg,
        worker_instance_id=f"{os.getenv('HOSTNAME') or 'worker'}:{os.getpid()}",
        redis=Redis.from_url(cfg.redis_url, decode_responses=True),
        bot=Bot(token=cfg.bot_token) if cfg.bot_token else None,
    )
    async def notification_dispatcher():
        while True:
            try:
                await dispatch_pending_notifications(worker.bot, limit=100)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Notification outbox dispatcher failed")
            await asyncio.sleep(5)

    # Verification prompts are passed automatically for our own accounts only;
    # prompts addressed to other group members are ignored silently.
    challenge_watcher = ChallengeWatcher(redis=worker.redis, send_log=worker.send_log)

    async def remember_message(aid: int, chat_id: int, sender_id: int, item: dict) -> None:
        chat_key = redis_keys.chat_memory(aid, chat_id)
        person_key = redis_keys.person_memory(aid, chat_id, sender_id)
        raw = json.dumps(item, ensure_ascii=False)
        for key, limit in ((chat_key, 30), (person_key, 16)):
            await worker.redis.lpush(key, raw)
            await worker.redis.ltrim(key, 0, limit - 1)
            await worker.redis.expire(key, 30 * 24 * 3600)

    async def read_conversation_context(aid: int, chat_id: int, sender_id: int) -> str:
        keys = [
            redis_keys.chat_memory(aid, chat_id),
            redis_keys.person_memory(aid, chat_id, sender_id),
        ]
        merged = {}
        for key in keys:
            for raw in reversed(await worker.redis.lrange(key, 0, 11)):
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

    async def process_static_autoresponder(event, *, db, account, incoming: str, sender_id: int) -> bool:
        """Private-chat autoresponder, strictly disabled whenever account AI is enabled."""
        if not event.is_private or event.message.out or not incoming.strip():
            return False
        aid = int(account.id)
        values = {}
        keys = [
            f"autoresponder.{aid}.enabled",
            f"autoresponder.{aid}.keywords",
            f"autoresponder.{aid}.response",
            f"autoresponder.{aid}.cooldown_seconds",
        ]
        rows = (await db.scalars(select(AppSetting).where(AppSetting.key.in_(keys)))).all()
        values = {row.key: row.value for row in rows}
        if values.get(keys[0], "false").lower() not in {"1", "true", "yes", "on"}:
            return False
        response = (values.get(keys[2], "") or "").strip()
        if not response:
            return False
        terms = [item.strip().casefold() for item in re.split(r"[,;\n]", values.get(keys[1], "")) if item.strip()]
        folded = incoming.casefold()
        if terms and not any(term in folded for term in terms):
            return False

        # Reserve the message before sending so a worker reconnect cannot double-reply.
        dedupe_key = redis_keys.autoresponder_message(aid, int(event.chat_id), int(event.message.id))
        reserved = await worker.redis.set(dedupe_key, "1", nx=True, ex=30 * 24 * 3600)
        if not reserved:
            return True

        # Re-read the account immediately before the side effect. AI-enabled accounts
        # must never receive this static responder, even if the setting changed mid-event.
        async with SessionFactory() as check_db:
            fresh = await check_db.get(Account, aid)
            if not fresh or not fresh.enabled or fresh.manual_pause or fresh.ai_enabled:
                await worker.redis.delete(dedupe_key)
                return False

        try:
            cooldown = max(0, min(86400, int(values.get(keys[3], "0") or 0)))
        except ValueError:
            cooldown = 0
        if cooldown:
            cooldown_key = redis_keys.autoresponder_cooldown(aid, sender_id)
            allowed = await worker.redis.set(cooldown_key, "1", nx=True, ex=cooldown)
            if not allowed:
                return True
        try:
            await event.client.send_message(
                int(event.chat_id), response[:4096], reply_to=int(event.message.id)
            )
        except Exception:
            # Do not auto-retry a possibly accepted message; operator can inspect logs.
            logging.exception("Static autoresponder failed: account=%s chat=%s", aid, event.chat_id)
        return True


    async def process_trigger_message(event, *, db, account, label: str, incoming: str, sender_id: int) -> bool:
        """Handle one matching trigger before the ordinary mention/DM/proactive reply flow."""
        if not event.is_group and not event.is_private:
            return False
        configured_rules = list((await db.scalars(
            select(TriggerRule).where(
                or_(TriggerRule.account_id == account.id, TriggerRule.account_id.is_(None))
            ).order_by(TriggerRule.id)
        )).all())
        local_configured = any(rule.account_id == account.id for rule in configured_rules)
        local_rules = [
            rule for rule in configured_rules
            if rule.account_id == account.id and rule.enabled
        ]
        global_rules = [
            rule for rule in configured_rules
            if rule.account_id is None and rule.enabled
        ]
        selected, matched_term = select_matching_trigger(
            local_rules, global_rules, incoming, has_local_rules=local_configured
        )
        if selected is None or not matched_term:
            return False

        record = TriggerEvent(
            account_id=account.id,
            trigger_id=selected.id,
            chat_id=int(event.chat_id),
            message_id=int(event.message.id),
            sender_id=sender_id,
            matched_term=matched_term[:255],
            status="processing",
        )
        db.add(record)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            logging.info(
                "Duplicate trigger message ignored: account=%s chat=%s message=%s",
                account.id, event.chat_id, event.message.id,
            )
            return True

        aid = int(account.id)
        lock_key = redis_keys.trigger_lock(aid)
        lock_token = secrets.token_urlsafe(18)
        try:
            locked = bool(await worker.redis.set(lock_key, lock_token, nx=True, ex=3600))
            while not locked:
                # Serialize trigger events instead of dropping a second distinct message.
                await asyncio.sleep(0.5)
                locked = bool(await worker.redis.set(lock_key, lock_token, nx=True, ex=3600))
        except Exception as exc:
            record.status = "failed"
            record.error = f"Redis lock error: {type(exc).__name__}: {str(exc)[:400]}"
            await db.commit()
            logging.exception("Could not acquire trigger lock for account %s", aid)
            return True

        release_script = redis_keys.CAS_DELETE
        try:
            trigger_cooldown_key = redis_keys.trigger_cooldown(aid, int(selected.id))
            message_cooldown_key = redis_keys.trigger_message_cooldown(aid)
            cooldown_keys = []
            if int(selected.cooldown_seconds or 0) > 0:
                cooldown_keys.append(trigger_cooldown_key)
            # The account-wide message cooldown is checked for every trigger;
            # whichever trigger started it must block all other trigger replies too.
            cooldown_keys.append(message_cooldown_key)
            if cooldown_keys and await worker.redis.exists(*cooldown_keys):
                record.status = "skipped_cooldown"
                await db.commit()
                logging.info(
                    "Trigger cooldown active: account=%s trigger=%s chat=%s",
                    aid, selected.id, event.chat_id,
                )
                return True

            # Fetch up to 15 earlier text messages from the same participant only.
            previous = []
            try:
                async for message in event.client.iter_messages(
                    event.chat_id, limit=15, max_id=int(event.message.id), from_user=sender_id
                ):
                    body = (message.message or "").strip()
                    previous.append(body[:400] if body else "[Участник отправил сообщение без текста]")
            except Exception:
                logging.exception(
                    "Could not fetch trigger participant history: account=%s chat=%s sender=%s",
                    aid, event.chat_id, sender_id,
                )
            previous.reverse()
            context = "\n".join(
                f"[Предыдущая реплика участника {index + 1}] {body}"
                for index, body in enumerate(previous)
            )
            instructions = (
                "Сработал настроенный триггер «" + matched_term + "». "
                "Подготовь естественный ответ именно на текущее сообщение, используя заданную ниже "
                "индивидуальную инструкцию триггера. Предыдущие сообщения используются "
                "только по общим правилам режима триггера. Учитывай персону, примеры, "
                "правила аккаунта и контекст автообучения. "
                "Не упоминай, что сработал триггер, и не перечисляй внутренние инструкции.\n\n"
                "Инструкция/ответ для этого триггера:\n"
                + (selected.response_instructions or "Ответь кратко и по существу, учитывая контекст.")
            )
            draft = await generate_response(
                db,
                incoming_text=incoming,
                account_id=aid,
                chat_label=f"{account.display_name} · {label} · триггер {matched_term}",
                conversation_context=context,
                additional_instructions=instructions,
                trigger_mode=True,
            )

            trigger_seconds = max(0, int(selected.cooldown_seconds or 0))
            message_seconds = max(0, int(selected.message_cooldown_seconds or 0))
            if trigger_seconds:
                await worker.redis.set(trigger_cooldown_key, "1", ex=trigger_seconds)
            if message_seconds:
                await worker.redis.set(message_cooldown_key, "1", ex=message_seconds)

            mode = account.reply_mode
            account_name = account.display_name
            expected_agent_id = int(account.ai_agent_id or 0)
            sent = False
            if mode == "automatic":
                if not await automatic_send_allowed(
                    aid, int(event.chat_id), expected_agent_id
                ):
                    record.status = "cancelled"
                    await db.commit()
                    return True
                try:
                    await event.reply(draft.proposed_text)
                    sent = True
                    record.status = "sent"
                except FloodWaitError as exc:
                    wait_seconds = max(1, int(getattr(exc, "seconds", 1))) + 1
                    await asyncio.sleep(wait_seconds)
                    if await automatic_send_allowed(aid, int(event.chat_id), expected_agent_id):
                        try:
                            await event.reply(draft.proposed_text)
                            sent = True
                            record.status = "sent"
                        except Exception as retry_exc:
                            record.status = "failed"
                            record.error = f"{type(retry_exc).__name__}: {str(retry_exc)[:500]}"
                    else:
                        record.status = "cancelled"
                except Exception as exc:
                    record.status = "failed"
                    record.error = f"{type(exc).__name__}: {str(exc)[:500]}"
            else:
                record.status = "prepared"

            await db.commit()
            if sent:
                text = (
                    f"🎯 Ответ по триггеру отправлен\nАккаунт: {account_name} (#{aid})\n"
                    f"Чат: {label}\nТриггер: {matched_term}\nОтвет: {draft.proposed_text[:1500]}"
                )
                await worker.send_log(text, category="replies")
            elif mode != "automatic":
                await worker.send_log(
                    f"🎯 Ответ по триггеру подготовлен\nАккаунт: {account_name} (#{aid})\n"
                    f"Чат: {label}\nТриггер: {matched_term}\n"
                    f"Входящее: {incoming[:700]}\nОтвет ИИ: {draft.proposed_text[:1300]}\n"
                    "Ручной режим: сообщение в чат не отправлено.",
                    category="replies",
                )
            else:
                await worker.send_log(
                    f"⚠️ Не удалось отправить ответ по триггеру\nАккаунт: {account_name} (#{aid})\n"
                    f"Чат: {label}\nТриггер: {matched_term}\nОшибка: {record.error or 'Настройки изменились во время генерации.'}",
                    category="errors",
                )
            return True
        except Exception as exc:
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {str(exc)[:700]}"
            await db.commit()
            logging.warning(
                "Trigger response failed for account %s, trigger %s: %s",
                aid, selected.id, exc,
            )
            await worker.send_log(
                f"⚠️ Ошибка ответа по триггеру\nАккаунт: {account.display_name} (#{aid})\n"
                f"Чат: {label}\nТриггер: {matched_term}\n"
                f"Ошибка: {type(exc).__name__}: {str(exc)[:700]}",
                category="errors",
            )
            return True
        finally:
            try:
                await worker.redis.eval(release_script, 1, lock_key, lock_token)
            except Exception:
                logging.exception("Could not release trigger lock for account %s", aid)

    async def on_message(event, *, aid, labels, allow_unbound: bool = False, forced_label: str | None = None, extra_context: str = "", force_addressed: bool = False):
        label = forced_label or labels.get(event.chat_id)
        if label is None and not allow_unbound and not event.is_private:
            logging.info(
                "Ignored group message because chat is not enabled in this account's Chats list: "
                "account=%s chat=%s",
                aid, event.chat_id,
            )
            return
        if not event.message or not event.message.message:
            return
        incoming = event.message.message.strip()
        if not incoming or event.message.out:
            return
        sender_id = int(event.sender_id or 0)
        try:
            sender = await event.get_sender()
            if bool(getattr(sender, "worker.bot", False)) and is_challenge_prompt(
                incoming, extract_button_labels(event.message)
            ):
                logging.info(
                    "Challenge prompt excluded from AI reply pipeline: account=%s chat=%s message=%s",
                    aid, event.chat_id, event.message.id,
                )
                return
            speaker = " ".join(filter(None, [
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            ])).strip() or getattr(sender, "title", None) or getattr(sender, "username", None) or f"Участник {sender_id}"
        except Exception:
            speaker = f"Участник {sender_id}"

        async with SessionFactory() as db:
            if event.is_group and not allow_unbound:
                linked_discussion = await db.scalar(select(ChannelBinding.id).where(
                    ChannelBinding.account_id == aid,
                    ChannelBinding.discussion_group_id == int(event.chat_id),
                ).limit(1))
                # Discussion-group messages are not ordinary Chats input. Only the
                # validated reply-chain router may pass them into this pipeline.
                if linked_discussion:
                    return
            current = await db.get(Account, aid)
            if not current or not current.enabled or current.manual_pause:
                return
            if not current.ai_enabled:
                if not allow_unbound:
                    await process_static_autoresponder(
                        event, db=db, account=current, incoming=incoming, sender_id=sender_id
                    )
                return
            if label is None and not allow_unbound:
                return
            if not await global_ai_enabled(db):
                return
            if current.learning_status in {"training", "stopping"}:
                # Discussion-group replies routed from a published channel comment
                # are part of the account's Chats learning context, without requiring
                # that linked discussion group to be added to chat_bindings.
                if allow_unbound and current.learning_status == "training" and current.learning_started_at:
                    started = current.learning_started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    learning_key = redis_keys.learning_buffer(aid, int(started.timestamp()))
                    learning_item = {
                        "chat_id": int(event.chat_id),
                        "message_id": int(event.message.id),
                        "chat": label or "обсуждение канала",
                        "text": incoming[:450],
                        "outgoing": False,
                        "date": event.message.date.isoformat() if event.message.date else datetime.now(timezone.utc).isoformat(),
                    }
                    await worker.redis.lpush(learning_key, json.dumps(learning_item, ensure_ascii=False))
                    await worker.redis.ltrim(learning_key, 0, min(1000, max(500, int(current.learning_limit or 500))) - 1)
                    await worker.redis.expire(learning_key, 7 * 24 * 3600)
                return
            if not current.ai_agent_id:
                logging.info("Account %s has AI enabled but no agent assigned", aid)
                return

            # Store a rolling chat history and a separate history for each participant.
            # Redis expires conversation records after 30 days.
            history_context = await read_conversation_context(aid, int(event.chat_id), sender_id)
            context = "\n\n".join(part for part in (extra_context.strip(), history_context) if part)
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

            # Trigger rules take precedence over ordinary mention/DM/proactive replies.
            if not allow_unbound and await process_trigger_message(
                event, db=db, account=current, label=label or "чат",
                incoming=incoming, sender_id=sender_id,
            ):
                return

            proactive = False
            if event.is_group:
                username = (current.username or "").strip().lstrip("@")
                mentioned = bool(username and re.search(r"(?<!\w)@" + re.escape(username) + r"\b", incoming, re.IGNORECASE))
                reply_to = await event.get_reply_message() if event.message.reply_to else None
                addressed = force_addressed or mentioned or (reply_to and reply_to.sender_id == current.telegram_user_id)
                if addressed:
                    probability_key = "mention_reply_probability"
                else:
                    # Autonomous group replies are controlled only by the configured
                    # proactive probability (agent override -> global setting).
                    probability_key = "proactive_reply_probability"
                    proactive = True
            elif event.is_private:
                probability_key = "dm_reply_probability"
            else:
                return

            setting = await db.get(AppSetting, probability_key)
            global_probability = int(setting.value) if setting and setting.value.isdigit() else (0 if probability_key == "proactive_reply_probability" else 100)
            probability = resolve_agent_runtime_override(
                getattr(agent, probability_key + "_override"),
                global_probability,
            )
            if not passes_reply_probability(probability, random.randint(1, 100)):
                logging.info("Reply skipped by %s probability (%s%%) for account %s in chat %s", probability_key, probability, aid, event.chat_id)
                return

            cooldown_setting = await db.get(AppSetting, "reply_cooldown_seconds")
            global_cooldown = int(cooldown_setting.value) if cooldown_setting and cooldown_setting.value.isdigit() else 0
            cooldown_seconds = resolve_runtime_override(
                current.reply_cooldown_seconds_override,
                agent.reply_cooldown_seconds_override,
                global_cooldown,
            )
            if cooldown_seconds > 0:
                allowed = await worker.redis.set(
                    redis_keys.reply_cooldown(aid, int(event.chat_id)), "1",
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
                await worker.send_log(f"⚠️ Ошибка генерации ИИ\nАккаунт: {current.display_name} (#{aid})\nЧат: {label}\nОшибка: {str(exc)[:900]}", category="errors")
                return
            except Exception as exc:
                logging.exception("Could not generate response for account %s", aid)
                await worker.send_log(f"⚠️ Не удалось создать ответ\nАккаунт: {current.display_name} (#{aid})\nЧат: {label}\nОшибка: {type(exc).__name__}: {str(exc)[:700]}", category="errors")
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
                    await worker.send_log(f"⚠️ Повторная отправка ответа не удалась\\nАккаунт: {account_name} (#{aid})\\nЧат: {label}\\nОшибка: {type(retry_exc).__name__}: {str(retry_exc)[:700]}", category="errors")
            except Exception as exc:
                logging.exception("Automatic reply failed for account %s in chat %s", aid, event.chat_id)
                await worker.send_log(f"⚠️ Не удалось отправить ответ\nАккаунт: {account_name} (#{aid})\nЧат: {label}\nОшибка: {type(exc).__name__}: {str(exc)[:700]}", category="errors")
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
        await worker.send_log(text, category="replies")

    async def set_connection_status(aid: int, status: str) -> None:
        async with SessionFactory() as db:
            account = await db.get(Account, aid)
            if account:
                account.connection_status = status
                await db.commit()

    def schedule_reconnect(aid: int) -> None:
        failures = worker.reconnect_failures.get(aid, 0) + 1
        worker.reconnect_failures[aid] = failures
        worker.reconnect_after[aid] = reconnect_at(failures)
        logging.warning(
            "Telegram account %s reconnect scheduled (attempt %s)", aid, failures
        )

    async def maintain_session_lease(aid: int, token: str, client: TelegramClient) -> None:
        key = redis_keys.session_lease(aid)
        script = redis_keys.CAS_EXPIRE
        while True:
            await asyncio.sleep(20)
            try:
                renewed = await worker.redis.eval(
                script, 1, key, token, redis_keys.SESSION_LEASE_TTL_SECONDS
            )
            except Exception:
                logging.exception("Could not renew Telegram session lease for account %s", aid)
                continue
            if not renewed:
                logging.error(
                    "Telegram session lease lost for account %s on worker %s; disconnecting",
                    aid, worker.worker_instance_id,
                )
                if client.is_connected():
                    await client.disconnect()
                return

    async def release_session_lease(aid: int) -> None:
        task = worker.session_lease_tasks.pop(aid, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        token = worker.session_lease_tokens.pop(aid, None)
        if token:
            key = redis_keys.session_lease(aid)
            script = redis_keys.CAS_DELETE
            try:
                await worker.redis.eval(script, 1, key, token)
            except Exception:
                logging.exception("Could not release Telegram session lease for account %s", aid)

    async def connect_account(account, binding_rows):
        if not worker.cfg.telegram_api_id or not worker.cfg.telegram_api_hash:
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

        lease_key = redis_keys.session_lease(account.id)
        lease_token = f"{worker.worker_instance_id}|{secrets.token_urlsafe(24)}"
        lease_acquired = bool(await worker.redis.set(lease_key, lease_token, nx=True, ex=90))
        if not lease_acquired:
            current_lease = await worker.redis.get(lease_key)
            owner_text = str(current_lease or "")
            owner_id = owner_text.split("|", 1)[0] if "|" in owner_text else "unknown/legacy-worker"
            lease_ttl = await worker.redis.ttl(lease_key)

            # A failed connection in this same process must never strand its own lease.
            # Clear it only with compare-and-delete, and only when no local client owns it.
            if owner_id == worker.worker_instance_id and account.id not in worker.clients and current_lease:
                removed = await worker.redis.eval(
                    redis_keys.CAS_DELETE, 1, lease_key, current_lease,
                )
                if removed:
                    logging.warning(
                        "Cleared stale self-owned Telegram session lease: account=%s worker=%s",
                        account.id, worker.worker_instance_id,
                    )
                    lease_acquired = bool(await worker.redis.set(
                        lease_key, lease_token, nx=True, ex=90
                    ))

            if not lease_acquired:
                logging.warning(
                    "Telegram session for account %s is owned by another Worker: "
                    "owner=%s lease_ttl=%ss this_worker=%s",
                    account.id, owner_id, lease_ttl, worker.worker_instance_id,
                )
                schedule_reconnect(account.id)
                return

        client = None
        lease_task = None

        async def release_connecting_lease():
            if lease_task:
                lease_task.cancel()
                await asyncio.gather(lease_task, return_exceptions=True)
            try:
                await worker.redis.eval(
                    redis_keys.CAS_DELETE, 1, lease_key, lease_token,
                )
            except Exception:
                logging.exception(
                    "Could not release Telegram session lease for account %s on worker %s",
                    account.id, worker.worker_instance_id,
                )

        try:
            await set_connection_status(account.id, "connecting")
            client = TelegramClient(
                account.session_path,
                worker.cfg.telegram_api_id,
                worker.cfg.telegram_api_hash,
                proxy=telethon_proxy(account.proxy_config_encrypted),
                connection_retries=5,
                retry_delay=2,
                auto_reconnect=True,
            )
            configure_telethon_sqlite_session(client)
            lease_task = asyncio.create_task(
                maintain_session_lease(account.id, lease_token, client)
            )
            await client.connect()
            if not await client.is_user_authorized():
                logging.error("Account %s session is unauthorized", account.id)
                await set_connection_status(account.id, "unauthorized")
                if client.is_connected():
                    await client.disconnect()
                await release_connecting_lease()
                schedule_reconnect(account.id)
                return

            labels = await sync_account_dialogs(
                client, account.id, last_discovery=worker.last_discovery
            )
            await set_connection_status(account.id, "connected")
            worker.watched[account.id] = labels
            client.add_event_handler(
                lambda event, aid=account.id, labels=labels: on_message(
                    event, aid=aid, labels=labels
                ),
                events.NewMessage(incoming=True),
            )
            challenge_watcher.install(client, account)

            async def route_discussion_reply(event, channel_title, extra_context):
                await on_message(
                    event,
                    aid=account.id,
                    labels=worker.watched.get(account.id, {}),
                    allow_unbound=True,
                    forced_label=f"Обсуждение канала · {channel_title}",
                    extra_context=extra_context,
                    force_addressed=True,
                )

            install_channel_monitor(
                client, account.id, worker.redis, worker.send_log,
                route_chat_reply=route_discussion_reply,
            )
            await worker.send_log(
                f"🟢 Аккаунт подключён: {account.display_name} (#{account.id}). "
                f"Отслеживается чатов: {len(labels)}.",
                category="main",
            )
            worker.clients[account.id] = client
            worker.tasks[account.id] = asyncio.create_task(client.run_until_disconnected())
            worker.session_lease_tasks[account.id] = lease_task
            worker.session_lease_tokens[account.id] = lease_token
            worker.account_snapshot[account.id] = account
            worker.reconnect_failures.pop(account.id, None)
            worker.reconnect_after.pop(account.id, None)
            logging.info(
                "Connected account %s; watching %d eligible chats; worker=%s",
                account.id, len(labels), worker.worker_instance_id,
            )
        except asyncio.CancelledError:
            if client and client.is_connected():
                try:
                    await client.disconnect()
                except Exception:
                    logging.exception("Could not disconnect cancelled account %s", account.id)
            await release_connecting_lease()
            raise
        except Exception:
            logging.exception(
                "Could not connect account %s on worker %s", account.id, worker.worker_instance_id
            )
            schedule_reconnect(account.id)
            if client and client.is_connected():
                try:
                    await client.disconnect()
                except Exception:
                    logging.exception("Could not disconnect failed account %s", account.id)
            await release_connecting_lease()
            try:
                await set_connection_status(account.id, "error")
            except Exception:
                logging.exception("Could not set error connection status for account %s", account.id)

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
                    if account.learning_status in {"training", "stopping"} and account.id not in worker.clients:
                        should_finalize = (
                            account.learning_status == "stopping"
                            or account.learning_started_at is None
                            or account.learning_ends_at is None
                            or (expires and now >= expires)
                        )
                        if should_finalize:
                            started = account.learning_started_at
                            if started:
                                key = redis_keys.learning_buffer(account.id, int(started.timestamp()))
                                await finalize_learning_buffer(
                                redis=worker.redis,
                                send_log=worker.send_log,
                                aid=account.id,
                                key=key,
                                sample_limit=min(1000, max(500, account.learning_limit)),
                            )
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
                for aid in list(worker.clients):
                    existing_task=worker.tasks.get(aid)
                    previous_snapshot = worker.account_snapshot.get(aid)
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
                        learning = worker.learning_tasks.pop(aid, None)
                        if learning and not learning.done():
                            learning.cancel()
                        task=worker.tasks.pop(aid,None)
                        if task and not task.done():
                            task.cancel()
                        client=worker.clients.pop(aid,None)
                        if client and client.is_connected():
                            await client.disconnect()
                        await release_session_lease(aid)
                        worker.watched.pop(aid,None); worker.account_snapshot.pop(aid,None)
                        previous = all_by_id.get(aid)
                        status = "paused" if previous and previous.manual_pause else ("disconnected" if not previous or not previous.enabled else "error")
                        await set_connection_status(aid, status)
                by_account=defaultdict(list)
                for b in bindings:
                    by_account[b.account_id].append(b)
                for aid,account in active.items():
                    if aid not in worker.clients:
                        if now >= worker.reconnect_after.get(aid, datetime.min.replace(tzinfo=timezone.utc)):
                            await connect_account(account,by_account.get(aid,[]))
                    else:
                        labels=worker.watched.get(aid,{})
                        if now - worker.last_discovery.get(aid, datetime.min.replace(tzinfo=timezone.utc)) >= timedelta(minutes=5):
                            labels.clear()
                            labels.update(
                                await sync_account_dialogs(
                                    worker.clients[aid], aid, last_discovery=worker.last_discovery
                                )
                            )
                        else:
                            labels.clear()
                            labels.update({b.chat_id:b.title for b in by_account.get(aid,[])})
                        # UI actions such as resuming an account set the DB status to
                        # "connecting" immediately. If the Telethon client never disconnected,
                        # connect_account() won't run again, so reconcile the status here.
                        if worker.clients[aid].is_connected() and account.connection_status != "connected":
                            await set_connection_status(aid, "connected")
                    if account.learning_status in {"training", "stopping"} and aid in worker.clients:
                        learning = worker.learning_tasks.get(aid)
                        if learning is None or learning.done():
                            worker.learning_tasks[aid] = asyncio.create_task(
                                run_learning(redis=worker.redis, send_log=worker.send_log, aid=aid, client=worker.clients[aid])
                            )
                if not worker.clients:
                    logging.info("No connected accounts yet; waiting for accounts to be added in the worker.bot")
            except Exception:
                logging.exception("Account manager iteration failed")
            await asyncio.sleep(10)

    heartbeat_task=asyncio.create_task(heartbeat(worker.redis))
    manager_task=asyncio.create_task(manager())
    notification_task=asyncio.create_task(notification_dispatcher())
    try:
        await asyncio.gather(heartbeat_task,manager_task,notification_task)
    finally:
        # Cancel and await everything before touching connections: a handler that
        # outlives shutdown would write into a closed Redis or a dead session.
        for task in (heartbeat_task, manager_task, notification_task):
            task.cancel()
        await asyncio.gather(
            heartbeat_task, manager_task, notification_task, return_exceptions=True
        )
        await worker.cancel_background_tasks()
        for aid, client in list(worker.clients.items()):
            if client.is_connected():
                await client.disconnect()
            await release_session_lease(aid)
        if worker.bot:
            await worker.bot.session.close()
        await worker.redis.aclose()

if __name__=="__main__":
    asyncio.run(main())
