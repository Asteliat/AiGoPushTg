"""Reply probabilities and per-agent/per-account overrides."""
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
from app.bot.navigation import cancel_return_for
from app.storage.db import SessionFactory
from app.storage.models import AIAgent
from app.storage.models import Account
from app.storage.models import AppSetting
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message
from sqlalchemy import select

RUNTIME_KEYS = {
    "mention_reply_probability": ("📣 Ответ на упоминания", "%", 0, 100),
    "dm_reply_probability": ("💬 Ответ в личке", "%", 0, 100),
    "proactive_reply_probability": ("🗣 Самостоятельное участие", "%", 0, 100),
    "reply_cooldown_seconds": ("⏱ Пауза между ответами", "сек.", 0, 2147483647),
}
ACCOUNT_RUNTIME_KEYS = {
    "reply_cooldown_seconds": RUNTIME_KEYS["reply_cooldown_seconds"],
}
AGENT_CHANNEL_RUNTIME_KEYS = {
    "channel_reply_probability": ("📡 Ответы в комментариях", "%", 0, 100, "channel_reply_probability_override"),
    "channel_reply_cooldown_seconds": ("⏳ Пауза в комментариях", "сек.", 0, 2147483647, "channel_reply_cooldown_seconds_override"),
}
def format_duration(seconds: int) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
async def read_global_runtime(db):
    values = {}
    for key, (_, _, _, _) in RUNTIME_KEYS.items():
        row = await db.get(AppSetting, key)
        fallback = 0 if key in {"proactive_reply_probability", "reply_cooldown_seconds"} else 100
        try:
            value = int(row.value) if row and row.value.isdigit() else fallback
        except (TypeError, ValueError):
            value = fallback
        values[key] = value
    return values
async def runtime_settings_text():
    async with SessionFactory() as db:
        values = await read_global_runtime(db)
    lines = ["⚙️ Глобальные настройки ответов", ""]
    for key, (label, unit, _, _) in RUNTIME_KEYS.items():
        shown = format_duration(values[key]) if key.endswith("_seconds") else f"{values[key]}{unit}"
        lines.append(f"{label}: {shown}")
    lines += [
        "",
        "Приоритет значений: настройка назначенного ИИ-агента → глобальное значение. Индивидуальные настройки агента можно оставить пустыми, чтобы использовать общий уровень.",
        "При изменении глобального значения бот спросит, перезаписать ли настройки всех агентов или изменить только значение по умолчанию для тех, кто наследует глобальные настройки.",
        "",
        "Вероятность самостоятельных ответов в группах применяется ко всем новым сообщениям в выбранных чатах: 100% — ответ на каждое сообщение, 1–2% — редкие ответы. Значение агента имеет приоритет над глобальным.",
    ]
    return "\n".join(lines)
@router.callback_query(F.data == "runtime:menu")
async def runtime_settings_menu(c: CallbackQuery):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        values = await read_global_runtime(db)
    buttons = []
    for key, (label, unit, _, _) in RUNTIME_KEYS.items():
        shown = format_duration(values[key]) if key.endswith("_seconds") else f"{values[key]}{unit}"
        buttons.append([(f"✏️ {label}: {shown}", f"runtime:input:{key}")])
    buttons.append([("⬅️ Настройки", "nav:settings"), ("🏠 Главное меню", "nav:home")])
    await edit(c, await runtime_settings_text(), kb(buttons))
@router.callback_query(F.data == "runtime:noop")
async def runtime_settings_noop(c: CallbackQuery):
    if not await check_cb(c): return
    await c.answer("Выберите значение в кнопках ниже.")
@router.callback_query(F.data.startswith("runtime:input:"))
async def runtime_settings_input(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    key = c.data.split(":")[2]
    if key not in RUNTIME_KEYS:
        await c.answer("Неизвестная настройка.", show_alert=True); return
    label, unit, minimum, maximum = RUNTIME_KEYS[key]
    await state.clear()
    await state.update_data(runtime_key=key, runtime_scope="global", ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.runtime_value)
    hint = f"Введите число секунд. Например: 3600 = 01:00:00 (60 минут). Максимум: {maximum} сек. ({format_duration(maximum)})." if unit == "сек." else f"Введите процент от {minimum}% до {maximum}%."
    await edit(c, f"{label}\n{hint}", CANCEL)
@router.message(Flow.runtime_value)
async def runtime_value_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    key = data.get("runtime_key")
    runtime_scope = data.get("runtime_scope")
    if runtime_scope == "account":
        meta = ACCOUNT_RUNTIME_KEYS.get(key)
        if not meta:
            await finish_prompt(m, state, "Для аккаунта доступна только настройка паузы. Вероятности задаются в ИИ-агенте или глобально.")
            return
        label, unit, minimum, maximum = meta
        attr = key + "_override"
    elif runtime_scope == "agent_channel":
        meta = AGENT_CHANNEL_RUNTIME_KEYS.get(key)
        if not meta:
            await finish_prompt(m, state, "Настройка не найдена.")
            return
        label, unit, minimum, maximum, attr = meta
    else:
        if key not in RUNTIME_KEYS:
            await finish_prompt(m, state, "Настройка не найдена.")
            return
        label, unit, minimum, maximum = RUNTIME_KEYS[key]
        attr = key + "_override"
    raw = (m.text or "").strip()
    if not raw.isdigit():
        await prompt_message(m, state, "Нужно ввести целое число.")
        return
    value = int(raw)
    if not minimum <= value <= maximum:
        await prompt_message(m, state, f"Допустимый диапазон для «{label}»: {minimum}–{maximum}.")
        return
    await delete_msg(m)
    shown = format_duration(value) if unit == "сек." else f"{value}{unit}"
    if runtime_scope == "account":
        aid = int(data["runtime_aid"])
        async with SessionFactory() as db:
            account = await db.get(Account, aid)
            if not account:
                await finish_prompt(m, state, "Аккаунт не найден.")
                return
            setattr(account, attr, value)
            await db.commit()
        await finish_prompt(
            m, state, f"✅ {label}: {shown} · своя настройка аккаунта",
            kb([[
                ("⬅️ К вероятностям аккаунта", f"acc:runtime:menu:{aid}"),
                ("👤 К аккаунту", f"acc:view:{aid}"),
            ]]),
        )
        return
    if runtime_scope in {"agent", "agent_channel"}:
        aid = int(data["runtime_aid"])
        async with SessionFactory() as db:
            agent = await db.get(AIAgent, aid)
            if not agent:
                await finish_prompt(m, state, "ИИ-агент не найден.")
                return
            setattr(agent, attr, value)
            await db.commit()
        await finish_prompt(m, state, f"✅ {label}: {shown}", kb([[("⬅️ К настройкам агента", f"agent:runtime:menu:{aid}")], [("🏠 Главное меню", "nav:home")]]))
        return
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, key)
        if setting:
            setting.value = str(value)
        else:
            db.add(AppSetting(key=key, value=str(value)))
        await db.commit()
    await finish_prompt(m, state, f"Глобальное значение «{label}» установлено: {shown}.\nКак применить его к ИИ-агентам?", kb([
        [("🌐 Применить ко всем агентам без исключения", f"runtime:apply:all:{key}:{value}")],
        [("🧩 Только агентам без собственного значения", f"runtime:apply:inherit:{key}:{value}")],
        [("⬅️ К настройкам", "runtime:menu")],
    ]))
@router.callback_query(F.data.startswith("runtime:apply:"))
async def runtime_settings_apply(c: CallbackQuery):
    if not await check_cb(c): return
    parts = c.data.split(":")
    if len(parts) != 5 or parts[2] not in {"all", "inherit"} or parts[3] not in RUNTIME_KEYS:
        await c.answer("Неизвестное действие.", show_alert=True); return
    scope, key = parts[2], parts[3]
    try:
        value = int(parts[4])
    except ValueError:
        await c.answer("Некорректное значение.", show_alert=True); return
    if not RUNTIME_KEYS[key][2] <= value <= RUNTIME_KEYS[key][3]:
        await c.answer("Недопустимое значение.", show_alert=True); return
    attr = key + "_override"
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, key)
        if setting:
            setting.value = str(value)
        else:
            db.add(AppSetting(key=key, value=str(value)))
        if scope == "all":
            for agent in (await db.scalars(select(AIAgent))).all():
                setattr(agent, attr, value)
        await db.commit()
    label, unit, _, _ = RUNTIME_KEYS[key]
    suffix = "Значение записано во все агенты." if scope == "all" else "Индивидуальные значения агентов сохранены; обновлено глобальное значение для наследующих агентов."
    shown = format_duration(value) if unit == "сек." else f"{value}{unit}"
    await edit(c, f"✅ {label}: {shown}\n{suffix}", kb([
        [("⚙️ К настройкам ответов", "runtime:menu")],
        [("🤖 К ИИ-агентам", "agent:menu"), ("🏠 Главное меню", "nav:home")],
    ]))
@router.callback_query(F.data == "agent:runtime:noop")
async def agent_runtime_noop(c: CallbackQuery):
    if not await check_cb(c): return
    await c.answer("Выберите значение ниже.")
async def agent_runtime_menu(c: CallbackQuery, aid: int):
    async with SessionFactory() as db:
        agent = await db.get(AIAgent, aid)
        global_values = await read_global_runtime(db)
    if not agent:
        await c.answer("ИИ-агент не найден.", show_alert=True); return
    buttons = []
    lines = [f"🎚 Индивидуальные настройки · {agent.name}", "", "«Наследует» означает, что используется глобальное значение."]
    for key, (label, unit, _minimum, _maximum) in RUNTIME_KEYS.items():
        override = getattr(agent, key + "_override")
        shown = (format_duration(override) if key.endswith("_seconds") else f"{override}{unit}") if override is not None else (f"Наследует ({format_duration(global_values[key])})" if key.endswith("_seconds") else f"Наследует ({global_values[key]}{unit})")
        lines.append(f"{label}: {shown}")
        buttons.append([(f"✏️ {label}: {shown}", f"agent:rt:input:{aid}:{key}")])
        if override is not None:
            buttons.append([("↩️ Наследовать глобальное значение", f"agent:rt:inherit:{aid}:{key}")])
    for key, (label, unit, _minimum, _maximum, attr) in AGENT_CHANNEL_RUNTIME_KEYS.items():
        override = getattr(agent, attr)
        shown = (format_duration(override) if unit == "сек." else f"{override}{unit}") if override is not None else "Наследует настройки аккаунта"
        lines.append(f"{label}: {shown}")
        buttons.append([(f"✏️ {label}: {shown}", f"agent:channelrt:input:{aid}:{key}")])
        if override is not None:
            buttons.append([("↩️ Наследовать настройки аккаунта", f"agent:channelrt:inherit:{aid}:{key}")])
    buttons += [[("⬅️ К агенту", f"agent:view:{aid}"), ("🏠 Главное меню", "nav:home")]]
    await edit(c, "\n".join(lines), kb(buttons))
async def account_runtime_menu(c: CallbackQuery, aid: int):
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        global_values = await read_global_runtime(db)
        agent = await db.get(AIAgent, account.ai_agent_id) if account and account.ai_agent_id else None
    if not account:
        await c.answer("Аккаунт не найден.", show_alert=True)
        return
    buttons = []
    lines = [
        f"⏱ Пауза ответов · {account.display_name}",
        "",
        "Вероятности ответов больше не настраиваются отдельно для аккаунта. Они задаются в назначенном ИИ-агенте или в глобальных настройках.",
    ]
    for key, (label, unit, _minimum, _maximum) in ACCOUNT_RUNTIME_KEYS.items():
        account_override = getattr(account, key + "_override")
        agent_override = getattr(agent, key + "_override") if agent else None
        effective = account_override if account_override is not None else agent_override if agent_override is not None else global_values[key]
        shown_value = format_duration(effective) if key.endswith("_seconds") else f"{effective}{unit}"
        source = (
            f"Своя пауза · {shown_value}" if account_override is not None
            else f"Из ИИ-агента · {shown_value}" if agent_override is not None
            else f"Глобальная · {shown_value}"
        )
        lines.append(f"{label}: {source}")
        buttons.append([(f"✏️ {label}: {source}", f"acc:rt:input:{aid}:{key}")])
        if account_override is not None:
            buttons.append([("↩️ Наследовать значение агента / глобальное", f"acc:rt:inherit:{aid}:{key}")])
    buttons += [[("⬅️ К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")]]
    await edit(c, "\n".join(lines), kb(buttons))
@router.callback_query(F.data.startswith("acc:runtime:menu:"))
async def account_runtime_menu_cb(c: CallbackQuery):
    if not await check_cb(c):
        return
    await account_runtime_menu(c, int(c.data.split(":")[3]))
@router.callback_query(F.data.startswith("acc:rt:input:"))
async def account_runtime_input(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    parts = c.data.split(":")
    aid, key = int(parts[3]), parts[4]
    if key not in ACCOUNT_RUNTIME_KEYS:
        await c.answer("Вероятности задаются в ИИ-агенте или глобальных настройках. Для аккаунта доступна только пауза.", show_alert=True)
        return
    label, unit, minimum, maximum = ACCOUNT_RUNTIME_KEYS[key]
    await state.clear()
    await state.update_data(
        runtime_key=key, runtime_scope="account", runtime_aid=aid,
        ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id,
        return_callback=f"acc:runtime:menu:{aid}",
    )
    await state.set_state(Flow.runtime_value)
    hint = (
        f"Введите число секунд. Максимум: {maximum} сек. ({format_duration(maximum)})."
        if unit == "сек."
        else f"Введите вероятность от {minimum}% до {maximum}%."
    )
    await edit(c, f"{label} · аккаунт #{aid}\n{hint}", CANCEL)
@router.callback_query(F.data.startswith("acc:rt:inherit:"))
async def account_runtime_inherit(c: CallbackQuery):
    if not await check_cb(c):
        return
    parts = c.data.split(":")
    if len(parts) != 5 or parts[4] not in ACCOUNT_RUNTIME_KEYS:
        await c.answer("Для аккаунта доступна только настройка паузы.", show_alert=True)
        return
    aid, key = int(parts[3]), parts[4]
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account:
            await c.answer("Аккаунт не найден.", show_alert=True)
            return
        setattr(account, key + "_override", None)
        await db.commit()
    await account_runtime_menu(c, aid)
@router.callback_query(F.data.startswith("agent:runtime:menu:"))
async def agent_runtime_menu_cb(c: CallbackQuery):
    if not await check_cb(c): return
    await agent_runtime_menu(c, int(c.data.split(":")[3]))
@router.callback_query(F.data.startswith("agent:rt:input:"))
async def agent_runtime_input(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    parts = c.data.split(":")
    aid, key = int(parts[3]), parts[4]
    if key not in RUNTIME_KEYS:
        await c.answer("Неизвестная настройка.", show_alert=True); return
    label, unit, minimum, maximum = RUNTIME_KEYS[key]
    await state.clear()
    await state.update_data(runtime_key=key, runtime_scope="agent", runtime_aid=aid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.runtime_value)
    hint = f"Введите число секунд. Например: 3600 = 01:00:00. Максимум: {maximum} сек. ({format_duration(maximum)})." if unit == "сек." else f"Введите процент от {minimum}% до {maximum}%."
    await edit(c, f"{label}\n{hint}", CANCEL)
@router.callback_query(F.data.startswith("agent:channelrt:input:"))
async def agent_channel_runtime_input(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    parts = c.data.split(":")
    aid, key = int(parts[3]), parts[4]
    if key not in AGENT_CHANNEL_RUNTIME_KEYS:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    label, unit, minimum, maximum, _ = AGENT_CHANNEL_RUNTIME_KEYS[key]
    await state.clear()
    await state.update_data(runtime_key=key, runtime_scope="agent_channel", runtime_aid=aid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, return_callback=cancel_return_for(c.data))
    await state.set_state(Flow.runtime_value)
    hint = f"Введите целое число секунд. Например: 3600 = 01:00:00. Максимум: {maximum} сек. ({format_duration(maximum)})." if unit == "сек." else f"Введите процент от {minimum}% до {maximum}%."
    await edit(c, f"{label}\n{hint}", CANCEL)
@router.callback_query(F.data.startswith("agent:channelrt:inherit:"))
async def agent_channel_runtime_inherit(c: CallbackQuery):
    if not await check_cb(c): return
    parts = c.data.split(":")
    aid, key = int(parts[3]), parts[4]
    if key not in AGENT_CHANNEL_RUNTIME_KEYS:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    attr = AGENT_CHANNEL_RUNTIME_KEYS[key][4]
    async with SessionFactory() as db:
        agent = await db.get(AIAgent, aid)
        if not agent:
            await c.answer("ИИ-агент не найден.", show_alert=True)
            return
        setattr(agent, attr, None)
        await db.commit()
    await agent_runtime_menu(c, aid)
@router.callback_query(F.data.startswith("agent:rt:inherit:"))
async def agent_runtime_inherit(c: CallbackQuery):
    if not await check_cb(c): return
    parts = c.data.split(":")
    if len(parts) != 5 or parts[4] not in RUNTIME_KEYS:
        await c.answer("Неизвестная настройка.", show_alert=True); return
    aid, key = int(parts[3]), parts[4]
    async with SessionFactory() as db:
        agent = await db.get(AIAgent, aid)
        if not agent:
            await c.answer("ИИ-агент не найден.", show_alert=True); return
        setattr(agent, key + "_override", None)
        await db.commit()
    await agent_runtime_menu(c, aid)
