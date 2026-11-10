"""Chat trigger rules: match a configured phrase and answer it once per cooldown."""
from __future__ import annotations

import asyncio
import logging
import secrets

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from telethon.errors import FloodWaitError

from app.services.ai_responses import generate_response
from app.services.triggers import select_matching_trigger
from app.storage.models import TriggerEvent, TriggerRule
from app.workers import redis_keys
from app.workers.send_policy import automatic_send_allowed


async def process_trigger_message(
    event,
    *,
    db,
    account,
    label: str,
    incoming: str,
    sender_id: int,
    worker,
    generate=generate_response,
    send_allowed=automatic_send_allowed,
) -> bool:
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
        draft = await generate(
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
            if not await send_allowed(
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
                if await send_allowed(aid, int(event.chat_id), expected_agent_id):
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
