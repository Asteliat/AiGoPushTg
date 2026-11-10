"""Account context learning and persona overrides."""
from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import Flow
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import edit
from app.bot.core import finish_prompt
from app.bot.core import kb
from app.bot.core import private
from app.bot.core import prompt_message
from app.bot.core import router
from app.bot.navigation import cancel_return_for
from app.services.ai_responses import global_ai_enabled
from app.storage.db import SessionFactory
from app.storage.models import AIAgent
from app.storage.models import Account
from app.storage.models import ChannelBinding
from app.storage.models import ChatBinding
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from datetime import datetime
from datetime import timezone
from sqlalchemy import select

@router.callback_query(F.data.startswith("acc:learnprompt:"))
async def acc_learning_prompt(c, account_id: int | None = None):
    if not await check_cb(c): return
    aid = account_id if account_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True); return
    prompt = account.learning_prompt or ""
    shown = prompt[:3000] if prompt.strip() else "Контекст пока не сохранён. Можно запустить обучение или добавить текст вручную."
    training_active = account.learning_status == "training"
    training_stopping = account.learning_status == "stopping"
    buttons = [[("✏️ Редактировать контекст", f"acc:context:edit:{aid}")]]
    if training_active:
        buttons.append([("⏹ Остановить обучение", f"learn:stop:{aid}")])
    elif training_stopping:
        buttons.append([("⏳ Обучение завершается", f"acc:view:{aid}")])
    else:
        buttons.append([("🧠 Запустить / повторить обучение", f"acc:learn:{aid}")])
    if prompt.strip() and not training_active and not training_stopping:
        buttons.append([("🗑 Очистить контекст", f"acc:context:clear:{aid}")])
    buttons.append([("⬅️ К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")])
    await edit(c, f"🧠 Контекст обучения · аккаунт #{aid}\n\n{shown}", kb(buttons))
@router.callback_query(F.data.startswith("acc:context:edit:"))
async def acc_context_edit_start(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[3])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True); return
    if account.learning_status in {"training", "stopping"}:
        await c.answer("Сначала завершите или остановите обучение, чтобы не потерять изменения.", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data), context_account_id=aid)
    await state.set_state(Flow.learning_prompt_edit)
    await edit(c, f"Редактирование контекста аккаунта #{aid}\nОтправьте новый текст одним сообщением (до 3500 символов). Старый контекст останется, пока вы не отправите новый. Для отмены нажмите кнопку ниже.", CANCEL)
@router.message(Flow.learning_prompt_edit)
async def acc_context_edit_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    value = (m.text or "").strip()
    data = await state.get_data()
    if not value or len(value) > 3500:
        await prompt_message(m, state, "Контекст должен содержать от 1 до 3500 символов. Отправьте исправленный текст или нажмите «Отмена».")
        return
    async with SessionFactory() as db:
        account = await db.get(Account, data["context_account_id"])
        if not account:
            await finish_prompt(m, state, "Аккаунт не найден.", kb([[("🏠 Главное меню", "nav:home")]])); return
        account.learning_prompt = value
        await db.commit()
    aid = data["context_account_id"]
    await finish_prompt(m, state, "Контекст сохранён. Он применяется только к этому аккаунту.", kb([
        [("📋 Открыть контекст", f"acc:learnprompt:{aid}")],
        [("👤 К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))
@router.callback_query(F.data.startswith("acc:context:clear:"))
async def acc_context_clear_confirm(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[3])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True); return
    if account.learning_status in {"training", "stopping"}:
        await c.answer("Сначала завершите или остановите обучение.", show_alert=True); return
    await edit(c, f"Очистить сохранённый контекст обучения аккаунта #{aid}? Переписка в Telegram не удаляется.", kb([
        [("🗑 Да, очистить", f"acc:context:clearconfirm:{aid}")],
        [("⬅️ Отмена", f"acc:learnprompt:{aid}")],
    ]))
@router.callback_query(F.data.startswith("acc:context:clearconfirm:"))
async def acc_context_clear(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[3])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account: await c.answer("Аккаунт не найден", show_alert=True); return
        if account.learning_status in {"training", "stopping"}:
            await c.answer("Обучение уже идёт; контекст не очищен.", show_alert=True); return
        account.learning_prompt = ""
        await db.commit()
    await edit(c, f"Контекст аккаунта #{aid} очищен.", kb([
        [("🧠 Открыть контекст", f"acc:learnprompt:{aid}")],
        [("👤 К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))
@router.callback_query(F.data.startswith("acc:personaoverride:"))
async def acc_persona_override_start(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account: await c.answer("Аккаунт не найден", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data), persona_override_account_id=aid)
    await state.set_state(Flow.persona_override)
    current = account.persona_style_override or "не заданы"
    await edit(c, f"Индивидуальные правила аккаунта #{aid}\n\nТекущие: {current[:1200]}\n\nОтправьте новый текст до 3500 символов. Для очистки отправьте /clear.", CANCEL)
@router.message(Flow.persona_override)
async def acc_persona_override_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    value = (m.text or "").strip()
    data = await state.get_data()
    if value == "/clear": value = ""
    elif not value or len(value) > 3500:
        await prompt_message(m, state, "Введите от 1 до 3500 символов или /clear для очистки.")
        return
    async with SessionFactory() as db:
        account = await db.get(Account, data["persona_override_account_id"])
        if not account:
            await finish_prompt(m, state, "Аккаунт не найден.", kb([[("🏠 Главное меню", "nav:home")]])); return
        account.persona_style_override = value
        await db.commit()
    aid = data["persona_override_account_id"]
    await finish_prompt(m, state, "Индивидуальные правила сохранены.", kb([
        [("👤 К аккаунту", f"acc:view:{aid}")],
        [("🏠 Главное меню", "nav:home")],
    ]))
@router.callback_query(F.data.startswith("acc:learn:"))
async def acc_learning_menu(c, account_id: int | None = None):
    if not await check_cb(c): return
    aid = account_id if account_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        active_chat_ids = list((await db.scalars(select(ChatBinding.chat_id).where(
            ChatBinding.account_id == aid, ChatBinding.enabled.is_(True)
        ))).all())
        discussion_ids = set((await db.scalars(select(ChannelBinding.discussion_group_id).where(
            ChannelBinding.account_id == aid, ChannelBinding.discussion_group_id.is_not(None)
        ))).all())
        channel_ids = set((await db.scalars(select(ChannelBinding.channel_id).where(
            ChannelBinding.account_id == aid
        ))).all())
        excluded_chat_ids = discussion_ids | channel_ids
        chat_count = sum(1 for chat_id in active_chat_ids if chat_id not in excluded_chat_ids)
        enabled_agent = await db.scalar(select(AIAgent.id).where(AIAgent.enabled.is_(True)))
        ai_on = await global_ai_enabled(db)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True); return
    learning = account.learning_status in {"training", "stopping"}
    requirements = []
    if not account.enabled: requirements.append("включите аккаунт")
    if account.manual_pause: requirements.append("возобновите работу аккаунта")
    if account.reply_mode != "manual": requirements.append("переключите режим на «Ручной»")
    if not chat_count: requirements.append("загрузите чаты и включите хотя бы один")
    if not ai_on or not enabled_agent: requirements.append("включите глобальный ИИ и настройте ИИ-агента")
    lines = [f"🧠 Автообучение · аккаунт #{aid}", "", "Контекст: сохранён" if account.learning_prompt.strip() else "Контекст: пока не задан"]
    if learning:
        lines.append("Статус: обучение выполняется." if account.learning_status == "training" else "Статус: обучение останавливается.")
    elif requirements:
        lines += ["", "Для запуска необходимо:"] + [f"• {item}" for item in requirements]
    else:
        lines += ["", "Готово к запуску. Перед стартом подтвердите право передавать сообщения выбранному ИИ-провайдеру."]
    rows = [[("🧠 Контекст обучения", f"acc:learnprompt:{aid}")]]
    if learning: rows.append([("⏹ Остановить обучение", f"learn:stop:{aid}")])
    elif not requirements: rows.append([("▶️ Запустить автообучение", f"learn:confirm:{aid}")])
    rows.append([("⬅️ К аккаунту", f"acc:view:{aid}")])
    await edit(c, "\n".join(lines), kb(rows))
@router.callback_query(F.data.startswith("learn:confirm:"))
async def acc_learning_confirm(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    buttons = [
        [("Изучить последние 500 сообщений", f"learn:start:{aid}:500")],
        [("Изучить последние 1000 сообщений", f"learn:start:{aid}:1000")],
        [("⬅️ Назад", f"acc:view:{aid}")],
    ]
    await edit(c, f"Выберите объём выборки для аккаунта #{aid}. Будут собраны последние 500 или 1000 подходящих текстовых сообщений по всем доступным чатам. Временные данные удаляются после завершения.", kb(buttons))
@router.callback_query(F.data.startswith("learn:start:"))
async def acc_learning_start(c):
    if not await check_cb(c): return
    _, _, aid_s, limit_s = c.data.split(":")
    aid, limit = int(aid_s), int(limit_s)
    if limit not in {500, 1000}:
        await c.answer("Недопустимый лимит", show_alert=True); return
    now = datetime.now(timezone.utc)
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        chats = list((await db.scalars(select(ChatBinding).where(
            ChatBinding.account_id == aid, ChatBinding.enabled.is_(True)
        ))).all())
        discussion_ids = set((await db.scalars(select(ChannelBinding.discussion_group_id).where(
            ChannelBinding.account_id == aid, ChannelBinding.discussion_group_id.is_not(None)
        ))).all())
        channel_ids = set((await db.scalars(select(ChannelBinding.channel_id).where(
            ChannelBinding.account_id == aid
        ))).all())
        excluded_chat_ids = discussion_ids | channel_ids
        chats = [binding for binding in chats if int(binding.chat_id) not in excluded_chat_ids]
        if not account or not account.enabled:
            await c.answer("Аккаунт не найден или выключен", show_alert=True); return
        if account.manual_pause:
            await c.answer("Сначала возобновите работу аккаунта.", show_alert=True); return
        if not chats:
            await c.answer("Список диалогов ещё не загружен. Откройте «Чаты аккаунта» и повторите попытку.", show_alert=True); return
        if account.learning_status in {"training", "stopping"}:
            await c.answer("Обучение уже запущено", show_alert=True); return
        if account.reply_mode != "manual":
            await c.answer("Перед обучением переключите режим ответов на «Ручной».", show_alert=True); return
        if not await global_ai_enabled(db):
            await c.answer("Сначала включите глобальный ИИ", show_alert=True); return
        agent = await db.get(AIAgent, account.ai_agent_id) if account.ai_agent_id else None
        if not agent or not agent.enabled or not agent.api_key.strip():
            agent = await db.scalar(select(AIAgent).where(
                AIAgent.enabled.is_(True), AIAgent.is_default.is_(True)
            ).order_by(AIAgent.id))
        if not agent or not agent.enabled or not agent.api_key.strip():
            agent = await db.scalar(select(AIAgent).where(
                AIAgent.enabled.is_(True)
            ).order_by(AIAgent.id))
        if not agent or not agent.api_key.strip():
            await c.answer("Настройте включённого ИИ-агента с API-ключом", show_alert=True); return
        account.learning_status = "training"
        account.learning_started_at = now
        from datetime import timedelta
        account.learning_ends_at = now + timedelta(days=3)
        account.learning_limit = limit
        await db.commit()
    await edit(c, f"🧠 Обучение аккаунта #{aid} запущено. Лимит: {limit} сообщений. Автоостановка — через 3 дня.", kb([
        [("⏹ Закончить обучение", f"learn:stop:{aid}")],
        [("⬅️ К аккаунту", f"acc:view:{aid}")],
    ]))
@router.callback_query(F.data.startswith("learn:stop:"))
async def acc_learning_stop(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account:
            await c.answer("Аккаунт не найден", show_alert=True); return
        if account.learning_status == "training":
            account.learning_status = "stopping"
            await db.commit()
    await edit(c, f"⏳ Обучение аккаунта #{aid} останавливается. После сводки временные сообщения будут удалены.", kb([
        [("⬅️ К аккаунту", f"acc:view:{aid}")],
        [("📚 Список аккаунтов", "acc:list")],
    ]))
