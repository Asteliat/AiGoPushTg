"""AI agents, providers and assignment to accounts."""

from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import Flow
from app.bot.core import back_home
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import delete_msg
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
from app.storage.models import AppSetting
from aiogram import F
from sqlalchemy import select


@router.message(F.text == "🤖 ИИ-агенты")
async def agents(m):
    if not await check_msg(m):
        return
    await agents_screen(m)


async def _agents_overview():
    """Agents, the global AI switch and how many accounts run their own AI."""
    async with SessionFactory() as db:
        agents = list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all())
        global_on = await global_ai_enabled(db)
        enabled_count = len(
            (await db.scalars(select(Account).where(Account.ai_enabled.is_(True)))).all()
        )
        total_accounts = len((await db.scalars(select(Account))).all())
    return agents, global_on, enabled_count, total_accounts


def _agents_keyboard(agents, global_on: bool, enabled_count: int, total_accounts: int):
    all_accounts_ai = total_accounts > 0 and enabled_count == total_accounts
    buttons = [
        [
            (
                f"{'⭐ ' if agent.is_default else ''}{'🟢' if agent.enabled else '⚪'} {agent.name}",
                f"agent:view:{agent.id}",
            )
        ]
        for agent in agents
    ]
    buttons += [
        [
            (
                "⛔ Выключить общий ИИ" if global_on else "✅ Включить общий ИИ",
                "agent:global",
            )
        ],
        [
            (
                "⛔ Выключить ИИ у аккаунтов"
                if all_accounts_ai
                else "✅ Включить ИИ у аккаунтов",
                "agent:accounts_toggle",
            )
        ],
        [("🔀 Назначить агента всем", "agent:assignall")],
        [("➕ Добавить агента", "agent:add")],
        [("⬅️ Главное меню", "nav:home")],
    ]
    return kb(buttons)


def _agents_text(global_on: bool, enabled_count: int, total_accounts: int) -> str:
    return (
        "🤖 ИИ-агенты\n\n"
        f"Общий ИИ: {'включён' if global_on else 'выключен'}\n"
        f"Свой ИИ у аккаунтов: {enabled_count} из {total_accounts}\n"
        "Нажмите агента, чтобы открыть настройки."
    )


async def agents_screen(m):
    agents, global_on, enabled_count, total_accounts = await _agents_overview()
    await m.answer(
        _agents_text(global_on, enabled_count, total_accounts),
        reply_markup=_agents_keyboard(agents, global_on, enabled_count, total_accounts),
    )


@router.callback_query(F.data == "agent:menu")
async def agents_cb(c):
    if not await check_cb(c):
        return
    agents, global_on, enabled_count, total_accounts = await _agents_overview()
    await edit(
        c,
        _agents_text(global_on, enabled_count, total_accounts),
        _agents_keyboard(agents, global_on, enabled_count, total_accounts),
    )


@router.callback_query(F.data == "agent:accounts_toggle")
async def accounts_ai_toggle(c):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        accounts = list((await db.scalars(select(Account))).all())
        enable = not bool(accounts) or not all(a.ai_enabled for a in accounts)
        for account in accounts:
            account.ai_enabled = enable
        await db.commit()
    await agents_cb(c)


@router.callback_query(F.data == "agent:global")
async def global_toggle(c):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        s = await db.get(AppSetting, "ai_global_enabled")
        if not s:
            s = AppSetting(key="ai_global_enabled", value="false")
            db.add(s)
        s.value = "false" if s.value.lower() == "true" else "true"
        await db.commit()
    await agents_cb(c)


@router.callback_query(F.data == "agent:add")
async def agent_add(c, state):
    if not await check_cb(c):
        return
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
    )
    await state.set_state(Flow.agent_name)
    await edit(c, "Введите название агента, например Groq или OpenAI.", CANCEL)


@router.message(Flow.agent_name)
async def agent_name(m, state):
    if not await check_msg(m) or not await private(m):
        return
    name = (m.text or "").strip()
    if not name or len(name) > 80:
        await prompt_message(m, state, "Название должно содержать от 1 до 80 символов.")
        return
    async with SessionFactory() as db:
        if await db.scalar(select(AIAgent.id).where(AIAgent.name == name)):
            await prompt_message(m, state, "Такое название уже есть.")
            return
    await delete_msg(m)
    await state.update_data(name=name)
    await state.set_state(Flow.agent_url)
    await prompt_message(m, state, "Введите Base URL API, например https://api.openai.com/v1")


@router.message(Flow.agent_url)
async def agent_url(m, state):
    if not await check_msg(m) or not await private(m):
        return
    v = (m.text or "").strip().rstrip("/")
    if not v.startswith("https://") and not (v.startswith("http://") and "localhost" in v):
        await prompt_message(m, state, "Нужен HTTPS URL (HTTP только localhost).")
        return
    await delete_msg(m)
    await state.update_data(url=v)
    await state.set_state(Flow.agent_key)
    await prompt_message(m, state, "Введите API-ключ. Сообщение будет удалено по возможности.")


@router.message(Flow.agent_key)
async def agent_key(m, state):
    if not await check_msg(m) or not await private(m):
        return
    v = (m.text or "").strip()
    await delete_msg(m)
    if len(v) < 5 or len(v) > 1000:
        await prompt_message(m, state, "Некорректная длина ключа.")
        return
    await state.update_data(key=v)
    await state.set_state(Flow.agent_model)
    await prompt_message(m, state, "Введите точное имя модели.")


@router.message(Flow.agent_model)
async def agent_model(m, state):
    if not await check_msg(m) or not await private(m):
        return
    v = (m.text or "").strip()
    if not v or len(v) > 160:
        await prompt_message(m, state, "Некорректное имя модели.")
        return
    await delete_msg(m)
    await state.update_data(model=v)
    await state.set_state(Flow.agent_timeout)
    await prompt_message(m, state, "Введите тайм-аут в секундах от 10 до 180.")


@router.message(Flow.agent_timeout)
async def agent_timeout(m, state):
    if not await check_msg(m) or not await private(m):
        return
    v = (m.text or "").strip()
    if not v.isdigit() or not 10 <= int(v) <= 180:
        await prompt_message(m, state, "Введите число от 10 до 180.")
        return
    d = await state.get_data()
    async with SessionFactory() as db:
        has = bool(await db.scalar(select(AIAgent.id)))
        a = AIAgent(
            name=d["name"],
            base_url=d["url"],
            api_key=d["key"],
            model=d["model"],
            timeout_seconds=int(v),
            is_default=not has,
        )
        db.add(a)
        await db.commit()
        await db.refresh(a)
    await finish_prompt(
        m,
        state,
        f"Агент «{a.name}» сохранён.",
        kb([[("🤖 К ИИ-агентам", "agent:menu")], [("🏠 Главное меню", "nav:home")]]),
    )


@router.callback_query(F.data == "agent:assignall")
async def assign_all_menu(c):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        aa = list((await db.scalars(select(AIAgent).where(AIAgent.enabled.is_(True)))).all())
    await edit(
        c,
        "Выберите агент для всех аккаунтов:",
        kb([[(a.name, f"agent:assignall:{a.id}")] for a in aa] + [[("⬅️ Назад", "agent:menu")]]),
    )


@router.callback_query(F.data.startswith("agent:assignall:"))
async def assign_all(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a = await db.get(AIAgent, aid)
        if not a or not a.enabled:
            await c.answer("Агент не найден или выключен", show_alert=True)
            return
        for account in (await db.scalars(select(Account))).all():
            account.ai_agent_id = aid
        for other in (await db.scalars(select(AIAgent))).all():
            other.is_default = other.id == aid
        await db.commit()
    await edit(
        c,
        f"«{a.name}» назначен всем аккаунтам. Локальные переключатели ИИ не менялись.",
        back_home(),
    )


@router.callback_query(F.data.startswith("agent:view:"))
async def agent_view(c, agent_id: int | None = None):
    if not await check_cb(c):
        return
    aid = agent_id if agent_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a = await db.get(AIAgent, aid)
        if not a:
            await c.answer("Агент не найден", show_alert=True)
            return
        assigned = list((await db.scalars(select(Account).where(Account.ai_agent_id == aid))).all())
    masked = ("•" * min(12, len(a.api_key))) if a.api_key else "не задан"
    txt = f"🤖 {a.name}\nСтатус: {'включён' if a.enabled else 'выключен'}\nПо умолчанию: {'да' if a.is_default else 'нет'}\nBase URL: {a.base_url}\nМодель: {a.model}\nAPI-ключ: {masked}\nТайм-аут: {a.timeout_seconds} сек.\nАккаунты: {', '.join('#' + str(x.id) for x in assigned) or 'нет'}"
    buttons = [
        [(("⏸ Отключить агента" if a.enabled else "▶️ Включить агента"), f"agent:toggle:{aid}")],
        [("⭐ По умолчанию", f"agent:default:{aid}")],
    ]
    if a.is_default:
        buttons.append([("✖️ Убрать статус по умолчанию", f"agent:unsetdefault:{aid}")])
    buttons.extend(
        [
            [("🎚 Вероятности и паузы", f"agent:runtime:menu:{aid}")],
            [("✏️ Base URL", f"agent:edit:url:{aid}"), ("✏️ Модель", f"agent:edit:model:{aid}")],
            [("🔑 API-ключ", f"agent:edit:key:{aid}"), ("⏱ Тайм-аут", f"agent:edit:timeout:{aid}")],
            [("🗑 Удалить агента", f"agent:delete:{aid}")],
            [("⬅️ К ИИ-агентам", "agent:menu")],
        ]
    )
    await edit(c, txt, kb(buttons))


@router.callback_query(F.data.startswith("agent:toggle:"))
async def agent_toggle(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a = await db.get(AIAgent, aid)
        if not a:
            await c.answer("Агент не найден", show_alert=True)
            return
        a.enabled = not a.enabled
        await db.commit()
    await agent_view(c)


@router.callback_query(F.data.startswith("agent:default:"))
async def agent_default(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a = await db.get(AIAgent, aid)
        if not a or not a.enabled:
            await c.answer("Сначала включите агента", show_alert=True)
            return
        for x in (await db.scalars(select(AIAgent))).all():
            x.is_default = x.id == aid
        await db.commit()
    await agent_view(c)


@router.callback_query(F.data.startswith("agent:unsetdefault:"))
async def agent_unset_default(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a = await db.get(AIAgent, aid)
        if not a:
            await c.answer("Агент не найден", show_alert=True)
            return
        if not a.is_default:
            await c.answer("Этот агент уже не является агентом по умолчанию", show_alert=True)
            await agent_view(c)
            return
        a.is_default = False
        await db.commit()
    await agent_view(c)


@router.callback_query(F.data.startswith("agent:edit:"))
async def agent_edit_start(c, state):
    if not await check_cb(c):
        return
    _, _, field, aid = c.data.split(":")
    await state.clear()
    await state.update_data(
        edit_field=field,
        edit_aid=int(aid),
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
    )
    await state.set_state(Flow.agent_edit)
    await edit(
        c,
        "Введите новое значение для "
        + field
        + ". Для ключа сообщение будет удалено по возможности.",
        CANCEL,
    )


@router.message(Flow.agent_edit)
async def agent_edit_save(m, state):
    if not await check_msg(m) or not await private(m):
        return
    d = await state.get_data()
    field = d["edit_field"]
    v = (m.text or "").strip()
    if field == "key":
        await delete_msg(m)
        if len(v) < 5 or len(v) > 1000:
            await prompt_message(m, state, "Некорректный ключ.")
            return
    elif field == "url":
        v = v.rstrip("/")
        if not v.startswith("https://") and not (v.startswith("http://") and "localhost" in v):
            await prompt_message(m, state, "Нужен HTTPS URL.")
            return
    elif field == "model":
        if not v or len(v) > 160:
            await prompt_message(m, state, "Некорректная модель.")
            return
    elif field == "timeout":
        if not v.isdigit() or not 10 <= int(v) <= 180:
            await prompt_message(m, state, "Введите число от 10 до 180.")
            return
        v = int(v)
    attr = {"url": "base_url", "model": "model", "key": "api_key", "timeout": "timeout_seconds"}[
        field
    ]
    async with SessionFactory() as db:
        a = await db.get(AIAgent, d["edit_aid"])
        if not a:
            await finish_prompt(m, state, "Агент не найден.")
            return
        setattr(a, attr, v)
        await db.commit()
    await finish_prompt(
        m,
        state,
        "Настройки сохранены.",
        kb([[("🤖 К ИИ-агентам", "agent:menu")], [("🏠 Главное меню", "nav:home")]]),
    )


@router.callback_query(F.data.startswith("agent:delete:"))
async def agent_delete(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a = await db.get(AIAgent, aid)
        if not a:
            await c.answer("Агент не найден", show_alert=True)
            return
        was = a.is_default
        for acc in (await db.scalars(select(Account).where(Account.ai_agent_id == aid))).all():
            acc.ai_agent_id = None
            acc.ai_enabled = False
        await db.delete(a)
        await db.flush()
        if was:
            replacement = await db.scalar(
                select(AIAgent).where(AIAgent.enabled.is_(True)).order_by(AIAgent.id)
            )
            if replacement:
                replacement.is_default = True
        await db.commit()
    await agents_cb(c)
