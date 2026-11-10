"""Trigger rules for one account or for every account."""
from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import Flow
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import delete_msg
from app.bot.core import edit
from app.bot.core import finish_prompt
from app.bot.core import kb
from app.bot.core import private
from app.bot.core import prompt_message
from app.bot.core import router
from app.services.triggers import format_trigger_duration
from app.services.triggers import parse_trigger_terms
from app.storage.db import SessionFactory
from app.storage.models import Account
from app.storage.models import TriggerRule
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message
from sqlalchemy import select

def trigger_back_callback(scope: str, account_id: int | None = None) -> str:
    return f"acc:view:{account_id}" if scope == "account" else "tool:menu"
def trigger_rule_title(rule: TriggerRule) -> str:
    terms = ", ".join(parse_trigger_terms(rule.terms))
    return (terms[:42] + "…") if len(terms) > 43 else terms
@router.callback_query(F.data == "trigger:global")
async def trigger_global_menu(c: CallbackQuery):
    await trigger_screen(c, "global")
@router.callback_query(F.data.startswith("trigger:account:"))
async def trigger_account_menu(c: CallbackQuery):
    if not await check_cb(c): return
    await trigger_screen(c, "account", int(c.data.split(":")[2]))
async def trigger_screen(c: CallbackQuery, scope: str, account_id: int | None = None):
    if not await check_cb(c): return
    if scope not in {"account", "global"}:
        await c.answer("Неизвестный раздел.", show_alert=True)
        return
    async with SessionFactory() as db:
        if scope == "account":
            account = await db.get(Account, account_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            rules = list((await db.scalars(
                select(TriggerRule).where(TriggerRule.account_id == account_id).order_by(TriggerRule.id)
            )).all())
            title = f"🎯 Триггеры аккаунта #{account_id}"
            detail = "Локальные триггеры аккаунта имеют приоритет над глобальными."
        else:
            rules = list((await db.scalars(
                select(TriggerRule).where(TriggerRule.account_id.is_(None)).order_by(TriggerRule.id)
            )).all())
            title = "🎯 Глобальные триггеры"
            detail = (
                "Глобальные триггеры применяются по выбранному режиму. "
                "Если совпал локальный триггер аккаунта, он всегда важнее глобального."
            )
    lines = [title, "", detail, ""]
    rows = []
    if not rules:
        lines.append("Триггеры пока не добавлены.")
    for rule in rules:
        state = "🟢" if rule.enabled else "⚪"
        apply_text = ""
        if scope == "global":
            apply_text = " · все аккаунты" if rule.apply_mode == "all" else " · без локальных"
        lines.append(
            f"{state} #{rule.id} · {trigger_rule_title(rule)}{apply_text}\n"
            f"Пауза триггера: {format_trigger_duration(rule.cooldown_seconds)}\n"
            f"Пауза между сообщениями: {format_trigger_duration(rule.message_cooldown_seconds)}"
        )
        rows.append([(f"{state} {trigger_rule_title(rule)}", f"trigger:view:{rule.id}")])
    if scope == "account":
        rows.append([("➕ Добавить триггер", f"trigger:add:account:{account_id}")])
    else:
        rows.append([("➕ Добавить глобальный триггер", "trigger:global:add")])
    rows.append([("⬅️ Назад", trigger_back_callback(scope, account_id))])
    await edit(c, "\n".join(lines), kb(rows))
@router.callback_query(F.data == "trigger:global:add")
async def trigger_global_add(c: CallbackQuery):
    if not await check_cb(c): return
    await edit(c, "Как распространить новый глобальный триггер?", kb([
        [("Добавить аккаунтам без своих триггеров", "trigger:global:empty")],
        [("Добавить всем аккаунтам без исключения", "trigger:global:all")],
        [("⬅️ К глобальным триггерам", "trigger:global")],
    ]))
@router.callback_query(F.data == "trigger:global:all")
async def trigger_global_all_confirm(c: CallbackQuery):
    if not await check_cb(c): return
    await edit(c,
        "⚠️ Подтвердите добавление глобального триггера для всех аккаунтов.\n\n"
        "Глобальное правило станет доступно каждому аккаунту. При совпадении локального триггера "
        "аккаунта локальное правило всё равно будет иметь приоритет. Продолжить?",
        kb([
            [("✅ Да, для всех аккаунтов", "trigger:global:allconfirm")],
            [("❌ Отмена", "trigger:global")],
        ]),
    )
async def trigger_add_start(c: CallbackQuery, state: FSMContext, scope: str, account_id: int | None, apply_mode: str):
    await state.clear()
    await state.update_data(
        trigger_scope=scope, trigger_account_id=account_id, trigger_apply_mode=apply_mode,
        ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id,
        return_callback=(f"trigger:account:{account_id}" if scope == "account" else "trigger:global"),
    )
    await state.set_state(Flow.trigger_terms)
    await edit(c,
        "Отправьте слова или фразы для одного триггера.\n\n"
        "Одиночные слова можно перечислить через пробел, запятую или с новой строки. "
        "Фразы из нескольких слов разделяйте запятыми или переносами строк.\n"
        "Например: впн VPN Випиэн\n"
        "Другой триггер: коровы, продажа коров, амбары.",
        CANCEL,
    )
@router.callback_query(F.data.startswith("trigger:add:account:"))
async def trigger_account_add(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[3])
    await trigger_add_start(c, state, "account", aid, "account")
@router.callback_query(F.data == "trigger:global:empty")
async def trigger_global_empty_add(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    await trigger_add_start(c, state, "global", None, "empty_only")
@router.callback_query(F.data == "trigger:global:allconfirm")
async def trigger_global_all_add(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    await trigger_add_start(c, state, "global", None, "all")
@router.message(Flow.trigger_terms)
async def trigger_terms_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    terms = parse_trigger_terms(m.text or "")
    if not terms or len(terms) > 30:
        await prompt_message(m, state, "Добавьте от 1 до 30 слов или фраз. Слова можно разделять пробелами, фразы — запятыми или переносами строк.")
        return
    await delete_msg(m)
    await state.update_data(trigger_terms="\n".join(terms))
    await state.set_state(Flow.trigger_response)
    await prompt_message(m, state,
        "Теперь отправьте инструкцию для ИИ по этому триггеру.\n\n"
        "Она будет добавляться к персоне, примерам, правилам аккаунта и контексту автообучения. "
        "Можно отправить /skip, чтобы использовать стандартную инструкцию."
    )
@router.message(Flow.trigger_response)
async def trigger_response_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    value = (m.text or "").strip()
    if value == "/skip": value = ""
    if len(value) > 3500:
        await prompt_message(m, state, "Максимум 3500 символов. Отправьте более короткую инструкцию.")
        return
    await delete_msg(m)
    await state.update_data(trigger_response=value)
    await state.set_state(Flow.trigger_cooldown)
    await prompt_message(m, state,
        "Введите паузу до следующего ответа по этому триггеру в секундах.\n"
        "Например, 900 = 00:00:15:00 (15 минут). Можно указать 0, чтобы отключить."
    )
async def trigger_seconds_received(m: Message, state: FSMContext, *, field: str, next_state, next_prompt: str):
    raw = (m.text or "").strip()
    if not raw.isdigit() or int(raw) > 31536000:
        await prompt_message(m, state, "Введите целое число секунд от 0 до 31536000 (не более 365 дней).")
        return
    await delete_msg(m)
    await state.update_data(**{field: int(raw)})
    await state.set_state(next_state)
    await prompt_message(m, state, next_prompt)
@router.message(Flow.trigger_cooldown)
async def trigger_cooldown_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    await trigger_seconds_received(
        m, state, field="trigger_cooldown_seconds", next_state=Flow.trigger_message_cooldown,
        next_prompt="Введите паузу между ответами на любые триггеры этого аккаунта в секундах.\n"
        "Например, 60 = 00:00:01:00. Укажите 0, если общая пауза не нужна.",
    )
@router.message(Flow.trigger_message_cooldown)
async def trigger_message_cooldown_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    raw = (m.text or "").strip()
    if not raw.isdigit() or int(raw) > 31536000:
        await prompt_message(m, state, "Введите целое число секунд от 0 до 31536000.")
        return
    await delete_msg(m)
    data = await state.get_data()
    async with SessionFactory() as db:
        rule = TriggerRule(
            account_id=data.get("trigger_account_id"),
            apply_mode=data.get("trigger_apply_mode") or "empty_only",
            terms=data["trigger_terms"],
            response_instructions=data.get("trigger_response", ""),
            cooldown_seconds=int(data.get("trigger_cooldown_seconds", 900)),
            message_cooldown_seconds=int(raw),
            enabled=True,
        )
        db.add(rule)
        await db.commit()
    scope = data["trigger_scope"]
    account_id = data.get("trigger_account_id")
    await finish_prompt(
        m, state, "✅ Триггер сохранён. Подключённые аккаунты начнут учитывать его сразу.",
        kb([[("🎯 К списку триггеров", f"trigger:account:{account_id}" if scope == "account" else "trigger:global")]]),
    )
async def trigger_rule_screen(c: CallbackQuery, rule_id: int):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, rule_id)
        if not rule:
            await c.answer("Триггер не найден.", show_alert=True)
            return
        scope = "global" if rule.account_id is None else "account"
        account_id = rule.account_id
        terms = parse_trigger_terms(rule.terms)
        title = "Глобальный триггер" if scope == "global" else f"Триггер аккаунта #{account_id}"
        apply_mode = "для всех аккаунтов" if rule.apply_mode == "all" else "только для аккаунтов без локальных триггеров"
        text = (
            f"🎯 {title} · #{rule.id}\n\n"
            f"Искомые слова/фразы:\n{', '.join(terms)}\n\n"
            f"Инструкция для ИИ:\n{rule.response_instructions or 'Стандартная инструкция'}\n\n"
            f"Пауза этого триггера: {format_trigger_duration(rule.cooldown_seconds)}\n"
            f"Пауза между сообщениями: {format_trigger_duration(rule.message_cooldown_seconds)}\n"
            f"Статус: {'включён' if rule.enabled else 'выключен'}"
        )
        if scope == "global":
            text += f"\nПрименение: {apply_mode}"
    back = f"trigger:account:{account_id}" if scope == "account" else "trigger:global"
    rows = [
        [("✏️ Изменить слова", f"trigger:edit:terms:{rule_id}")],
        [("✏️ Изменить инструкцию ИИ", f"trigger:edit:response:{rule_id}")],
        [("⏱ Пауза триггера", f"trigger:edit:cooldown_seconds:{rule_id}")],
        [("⏱ Пауза между сообщениями", f"trigger:edit:message_cooldown_seconds:{rule_id}")],
        [("🔴 Выключить" if rule.enabled else "🟢 Включить", f"trigger:toggle:{rule_id}")],
        [("🗑 Удалить", f"trigger:delete:{rule_id}")],
        [("⬅️ Назад", back)],
    ]
    await edit(c, text, kb(rows))
@router.callback_query(F.data.startswith("trigger:view:"))
async def trigger_view(c: CallbackQuery):
    if not await check_cb(c): return
    await trigger_rule_screen(c, int(c.data.split(":")[2]))
@router.callback_query(F.data.startswith("trigger:edit:"))
async def trigger_edit_start(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    _, _, field, rule_raw = c.data.split(":")
    if field not in {"terms", "response", "cooldown_seconds", "message_cooldown_seconds"}:
        await c.answer("Неизвестное поле.", show_alert=True)
        return
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, int(rule_raw))
        if not rule:
            await c.answer("Триггер не найден.", show_alert=True)
            return
        scope = "global" if rule.account_id is None else "account"
        back = f"trigger:account:{rule.account_id}" if scope == "account" else "trigger:global"
    await state.clear()
    await state.update_data(trigger_edit_id=int(rule_raw), trigger_edit_field=field,
        return_callback=back, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await state.set_state(Flow.trigger_edit_value)
    hints = {
        "terms": "Отправьте слова через пробел, а фразы из нескольких слов разделяйте запятыми или переносами строк.",
        "response": "Отправьте новую инструкцию ИИ. Для очистки отправьте /clear.",
        "cooldown_seconds": "Введите новую паузу этого триггера в секундах (0–31536000).",
        "message_cooldown_seconds": "Введите общую паузу между триггерами аккаунта в секундах (0–31536000).",
    }
    await edit(c, hints[field], CANCEL)
@router.message(Flow.trigger_edit_value)
async def trigger_edit_value_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    field = data["trigger_edit_field"]
    raw = (m.text or "").strip()
    if field == "terms":
        terms = parse_trigger_terms(raw)
        if not terms or len(terms) > 30:
            await prompt_message(m, state, "Укажите от 1 до 30 слов/фраз: слова через пробел, фразы — через запятую или с новой строки.")
            return
        value = "\n".join(terms)
    elif field == "response":
        value = "" if raw == "/clear" else raw
        if len(value) > 3500:
            await prompt_message(m, state, "Максимум 3500 символов.")
            return
    else:
        if not raw.isdigit() or int(raw) > 31536000:
            await prompt_message(m, state, "Введите целое число секунд от 0 до 31536000.")
            return
        value = int(raw)
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, data["trigger_edit_id"])
        if not rule:
            await finish_prompt(m, state, "Триггер не найден.")
            return
        attr = {
            "terms": "terms", "response": "response_instructions",
            "cooldown_seconds": "cooldown_seconds",
            "message_cooldown_seconds": "message_cooldown_seconds",
        }[field]
        setattr(rule, attr, value)
        await db.commit()
    rule_id = data["trigger_edit_id"]
    await finish_prompt(m, state, "✅ Настройка триггера обновлена.", kb([
        [("🎯 Открыть триггер", f"trigger:view:{rule_id}")],
    ]))
@router.callback_query(F.data.startswith("trigger:toggle:"))
async def trigger_toggle(c: CallbackQuery):
    if not await check_cb(c): return
    rule_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, rule_id)
        if not rule:
            await c.answer("Триггер не найден.", show_alert=True)
            return
        rule.enabled = not rule.enabled
        await db.commit()
    await trigger_rule_screen(c, rule_id)
@router.callback_query(F.data.startswith("trigger:delete:"))
async def trigger_delete_confirm(c: CallbackQuery):
    if not await check_cb(c): return
    rule_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, rule_id)
        if not rule:
            await c.answer("Триггер не найден.", show_alert=True)
            return
        title = trigger_rule_title(rule)
    await edit(c, f"Удалить триггер «{title}»?", kb([
        [("🗑 Да, удалить", f"trigger:deleteconfirm:{rule_id}")],
        [("⬅️ Отмена", f"trigger:view:{rule_id}")],
    ]))
@router.callback_query(F.data.startswith("trigger:deleteconfirm:"))
async def trigger_delete(c: CallbackQuery):
    if not await check_cb(c): return
    rule_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        rule = await db.get(TriggerRule, rule_id)
        if not rule:
            await c.answer("Триггер уже удалён.", show_alert=True)
            return
        back = f"trigger:account:{rule.account_id}" if rule.account_id is not None else "trigger:global"
        await db.delete(rule)
        await db.commit()
    if back.startswith("trigger:account:"):
        await trigger_screen(c, "account", int(back.split(":")[2]))
    else:
        await trigger_screen(c, "global")
