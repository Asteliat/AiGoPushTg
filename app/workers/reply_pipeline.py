"""Reply pipeline: decide whether an incoming message gets an AI answer and send it.

Trigger rules, mention/DM/proactive probabilities, cooldowns, the final automatic-send
policy and the reply notifications all live here. The session factory and the AI call
are injectable, so the pipeline runs in tests without PostgreSQL or an AI provider.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timezone

from sqlalchemy import select
from telethon.errors import FloodWaitError

from app.ai.provider import AIUnavailableError, NoReplyNeeded
from app.services.ai_responses import (
    generate_response,
    global_ai_enabled,
    passes_reply_probability,
    resolve_agent_runtime_override,
    resolve_runtime_override,
)
from app.services.challenge_monitor import extract_button_labels, is_challenge_prompt
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, AppSetting, ChannelBinding
from app.workers import redis_keys
from app.workers.autoresponder import process_static_autoresponder
from app.workers.memory import read_conversation_context, remember_message
from app.workers.send_policy import automatic_send_allowed
from app.workers.trigger_replies import process_trigger_message


async def on_message(
    event,
    *,
    aid,
    labels,
    worker,
    allow_unbound: bool = False,
    forced_label: str | None = None,
    extra_context: str = "",
    force_addressed: bool = False,
    session_factory=SessionFactory,
    generate=generate_response,
) -> None:
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
        if bool(getattr(sender, "bot", False)) and is_challenge_prompt(
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

    async with session_factory() as db:
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
                    event,
                    db=db,
                    account=current,
                    incoming=incoming,
                    sender_id=sender_id,
                    redis=worker.redis,
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
        history_context = await read_conversation_context(
            worker.redis, aid, int(event.chat_id), sender_id
        )
        context = "\n\n".join(part for part in (extra_context.strip(), history_context) if part)
        item = {
            "message_id": int(event.message.id),
            "sender_id": sender_id,
            "speaker": speaker[:80],
            "text": incoming[:700],
            "date": event.message.date.isoformat() if event.message.date else "",
        }
        await remember_message(worker.redis, aid, int(event.chat_id), sender_id, item)

        agent = await db.get(AIAgent, current.ai_agent_id)
        if not agent or not agent.enabled:
            return

        # Trigger rules take precedence over ordinary mention/DM/proactive replies.
        if not allow_unbound and await process_trigger_message(
            event,
            db=db,
            account=current,
            label=label or "чат",
            incoming=incoming,
            sender_id=sender_id,
            worker=worker,
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
            draft = await generate(
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

    async with session_factory() as db:
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
            await remember_message(
                worker.redis, aid, int(event.chat_id), int(current.telegram_user_id or 0), sent_item
            )

    if sent:
        text = f"✅ Ответ отправлен\nАккаунт: {account_name} (#{aid})\nЧат: {label}\nОтвет: {draft.proposed_text[:1800]}"
    elif mode == "automatic":
        text = f"⚠️ Ответ не отправлен\nАккаунт: {account_name} (#{aid})\nЧат: {label}\nТекст не сохранён; повторите обработку после проверки настроек."
    else:
        text = f"💬 Ответ подготовлен\nАккаунт: {account_name} (#{aid})\nЧат: {label}\nВходящее: {incoming[:900]}\nОтвет ИИ: {draft.proposed_text[:1400]}\nРучной режим: в чат ничего не отправлено; текст не сохраняется."
    await worker.send_log(text, category="replies")
