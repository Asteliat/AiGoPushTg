"""Settings hub, verification mode, FloodWait policy and logs."""

from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import Flow
from app.bot.core import cfg
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import delete_msg
from app.bot.core import edit
from app.bot.core import finish_prompt
from app.bot.core import get_join_floodwait_mode
from app.bot.core import kb
from app.bot.core import private
from app.bot.core import prompt_message
from app.bot.core import router
from app.bot.navigation import cancel_return_for
from app.services.challenge_monitor import ChallengeMode
from app.services.challenge_settings import read_challenge_mode
from app.services.challenge_settings import write_challenge_mode
from app.storage.db import SessionFactory
from app.storage.models import AppSetting
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message


@router.callback_query(F.data == "nav:settings")
async def settings_cb(c: CallbackQuery):
    if not await check_cb(c):
        return
    await edit(
        c,
        "⚙️ Настройки\n\n"
        "🎚 Вероятности и паузы — как часто аккаунты отвечают.\n"
        "⏳ FloodWait — что делать, если Telegram задерживает вступление.\n"
        "🛡 Проверки — как проходить подтверждения в группах.\n"
        "🧾 Логи — куда присылать события.\n\n"
        "Секреты (токен бота, API ID/hash, ключ шифрования) задаются в .env на сервере.",
        kb(
            [
                [("🎚 Вероятности и паузы", "runtime:menu"), ("⏳ FloodWait", "joinpolicy:menu")],
                [("🛡 Проверки Telegram", "challenge:settings"), ("🧾 Логи", "logs:menu")],
                [("⬅️ Главное меню", "nav:home")],
            ]
        ),
    )


@router.callback_query(F.data == "challenge:settings")
async def challenge_settings(c: CallbackQuery):
    if not await check_cb(c):
        return
    mode = await read_challenge_mode()
    current = {
        ChallengeMode.AUTO: "Авто — проходить сам",
        ChallengeMode.NOTIFY: "Только уведомлять",
        ChallengeMode.OFF: "Не реагировать",
    }[mode]

    def mark(value: ChallengeMode) -> str:
        return " ✅" if mode is value else ""

    await edit(
        c,
        "🛡 Проверки Telegram\n\n"
        "В группах боты просят подтвердить, что вы человек, и присылают кнопку.\n"
        "Авто — аккаунт сам нажимает нужную кнопку, если проверка адресована ему.\n"
        "Чужие проверки пропускаются молча.\n\n"
        f"Сейчас: {current}",
        kb(
            [
                [("Авто" + mark(ChallengeMode.AUTO), "challenge:set:auto")],
                [("Только уведомлять" + mark(ChallengeMode.NOTIFY), "challenge:set:notify")],
                [("Не реагировать" + mark(ChallengeMode.OFF), "challenge:set:off")],
                [("⬅️ Настройки", "nav:settings")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("challenge:set:"))
async def challenge_settings_save(c: CallbackQuery):
    if not await check_cb(c):
        return
    value = c.data.rsplit(":", 1)[-1]
    modes = {mode.value: mode for mode in ChallengeMode}
    if value not in modes:
        await c.answer("Неизвестное значение.", show_alert=True)
        return
    await write_challenge_mode(modes[value])
    await c.answer("Настройка сохранена.")
    await challenge_settings(c)


@router.callback_query(F.data == "joinpolicy:menu")
async def join_floodwait_settings(c: CallbackQuery):
    if not await check_cb(c):
        return
    mode = await get_join_floodwait_mode()
    current = "ждать и продолжать" if mode == "wait" else "остановить вступление"
    await edit(
        c,
        "⏳ FloodWait при вступлении\n\n"
        f"Сейчас: {current}.\n\n"
        "Ждать — дождаться срока Telegram и повторить попытку.\n"
        "Остановить — прервать и показать срок ограничения.\n\n"
        "Каждое вступление всё равно требует подтверждения.",
        kb(
            [
                [("⏳ Ждать и продолжать" + (" ✓" if mode == "wait" else ""), "joinpolicy:set:wait")],
                [("⛔ Остановить вступление" + (" ✓" if mode == "stop" else ""), "joinpolicy:set:stop")],
                [("⬅️ Настройки", "nav:settings")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("joinpolicy:set:"))
async def join_floodwait_settings_save(c: CallbackQuery):
    if not await check_cb(c):
        return
    mode = c.data.rsplit(":", 1)[-1]
    if mode not in {"wait", "stop"}:
        await c.answer("Неизвестный режим.", show_alert=True)
        return
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, "join_floodwait_mode")
        if setting:
            setting.value = mode
        else:
            db.add(AppSetting(key="join_floodwait_mode", value=mode))
        await db.commit()
    await c.answer("Настройка сохранена.")
    await join_floodwait_settings(c)


LOG_CATEGORIES = {
    "main": "📌 Основные логи",
    "replies": "💬 Ответы ИИ",
    "errors": "🚨 Ошибки",
    "learning": "🧠 Обучение",
    "channels": "📡 Каналы и обсуждения",
}


def log_destinations_from_value(value: str | None) -> list[tuple[int, int | None]]:
    """Parse legacy chat IDs and chat_id:message_thread_id destinations."""
    result: list[tuple[int, int | None]] = []
    for raw in (value or "").replace(";", ",").split(","):
        parts = raw.strip().split(":")
        try:
            chat_id = int(parts[0])
            thread_id = int(parts[1]) if len(parts) == 2 else None
        except (TypeError, ValueError):
            continue
        if len(parts) > 2 or chat_id == 0 or (thread_id is not None and thread_id <= 0):
            continue
        destination = (chat_id, thread_id)
        if destination not in result:
            result.append(destination)
    return result


def log_ids_from_value(value: str | None) -> list[int]:
    """Backward-compatible helper for code/tests that only need chat IDs."""
    return list(dict.fromkeys(chat_id for chat_id, _ in log_destinations_from_value(value)))


async def logs_menu_text():
    async with SessionFactory() as db:
        rows = {}
        for category in LOG_CATEGORIES:
            setting = await db.get(AppSetting, f"log_chat_ids_{category}")
            destinations = log_destinations_from_value(setting.value if setting else "")
            if category == "main" and setting is None and cfg.log_chat_id:
                destinations = [(cfg.log_chat_id, None)]
            rows[category] = destinations
    lines = [
        "🧾 Логи",
        "",
        "Получатели: ID чата или ID чата:ID темы. Несколько — через запятую.",
        "",
    ]
    for category, label in LOG_CATEGORIES.items():
        destinations = rows[category]
        shown = (
            ", ".join(
                f"{chat_id}" + (f" · тема {thread_id}" if thread_id is not None else "")
                for chat_id, thread_id in destinations
            )
            if destinations
            else "не настроено"
        )
        lines.append(f"{label}: {shown}")
    lines += [
        "",
        "📌 Основные — берут LOG_CHAT_ID из .env, пока не добавлен свой получатель.",
    ]
    return "\n".join(lines)


@router.callback_query(F.data == "logs:menu")
async def logs_menu(c: CallbackQuery):
    if not await check_cb(c):
        return
    rows = [[(label, f"logs:category:{category}")] for category, label in LOG_CATEGORIES.items()]
    rows += [[("⬅️ Настройки", "nav:settings"), ("🏠 Главное меню", "nav:home")]]
    await edit(c, await logs_menu_text(), kb(rows))


@router.callback_query(F.data.startswith("logs:category:"))
async def logs_category(c: CallbackQuery, category_override: str | None = None):
    if not await check_cb(c):
        return
    category = category_override or c.data.split(":")[2]
    if category not in LOG_CATEGORIES:
        await c.answer("Неизвестная категория.", show_alert=True)
        return
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, f"log_chat_ids_{category}")
        destinations = log_destinations_from_value(setting.value if setting else "")
    if category == "main" and setting is None and cfg.log_chat_id:
        destinations = [(cfg.log_chat_id, None)]
    lines = [LOG_CATEGORIES[category], "", "Получатели этой категории:"]
    lines += (
        [
            f"• {chat_id}" + (f" · тема {thread_id}" if thread_id is not None else " · общий чат")
            for chat_id, thread_id in destinations
        ]
        if destinations
        else ["• пока не добавлены"]
    )
    rows = [[("➕ Добавить чат или тему", f"logs:add:{category}")]]
    rows += [
        [
            (
                f"➖ Удалить {chat_id}" + (f" · тема {thread_id}" if thread_id is not None else ""),
                f"logs:remove:{category}:{chat_id}:{thread_id or 0}",
            )
        ]
        for chat_id, thread_id in destinations
    ]
    rows += [[("⬅️ К списку логов", "logs:menu")]]
    await edit(c, "\n".join(lines), kb(rows))


@router.callback_query(F.data.startswith("logs:add:"))
async def logs_add(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    category = c.data.split(":")[2]
    if category not in LOG_CATEGORIES:
        await c.answer("Неизвестная категория.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        log_category=category,
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
    )
    await state.set_state(Flow.log_chat_id)
    await edit(
        c,
        f"{LOG_CATEGORIES[category]}\n\nОтправьте ID чата или супергруппы. Чтобы направлять логи в тему форума, добавьте через двоеточие ID темы.\n\nПримеры:\n-1004351902275 — общий чат\n-1004351902275:108 — тема с ID 108.",
        CANCEL,
    )


@router.message(Flow.log_chat_id)
async def logs_add_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    category = data.get("log_category")
    parsed = log_destinations_from_value(m.text or "")
    if len(parsed) != 1:
        await prompt_message(
            m,
            state,
            "Неверный формат. Отправьте ID чата или пару ID_чата:ID_темы, например -1004351902275:108.",
        )
        return
    chat_id, thread_id = parsed[0]
    await delete_msg(m)
    try:
        kwargs = {"message_thread_id": thread_id} if thread_id is not None else {}
        await m.bot.send_message(
            chat_id, "✅ Проверка связи: этот получатель добавлен для логов AtcivPerson.", **kwargs
        )
    except Exception as exc:
        target = f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id)
        await finish_prompt(
            m,
            state,
            f"Не удалось отправить тестовое сообщение получателю {target}. Проверьте ID, ID темы и права бота.\nОшибка: {type(exc).__name__}: {str(exc)[:300]}",
            kb([[("↩️ К категории", f"logs:category:{category}")], [("🧾 Все логи", "logs:menu")]]),
        )
        return
    async with SessionFactory() as db:
        key = f"log_chat_ids_{category}"
        setting = await db.get(AppSetting, key)
        destinations = log_destinations_from_value(setting.value if setting else "")
        if category == "main" and setting is None and cfg.log_chat_id:
            destinations = [(cfg.log_chat_id, None)]
        if (chat_id, thread_id) not in destinations:
            destinations.append((chat_id, thread_id))
        value = ",".join(
            f"{target_chat}:{target_thread}" if target_thread is not None else str(target_chat)
            for target_chat, target_thread in destinations
        )
        if setting:
            setting.value = value
        else:
            db.add(AppSetting(key=key, value=value))
        await db.commit()
    target = f"{chat_id} · тема {thread_id}" if thread_id is not None else str(chat_id)
    await finish_prompt(
        m,
        state,
        f"✅ Получатель {target} добавлен в категорию «{LOG_CATEGORIES[category]}».",
        kb([[("↩️ К категории", f"logs:category:{category}")], [("🧾 Все логи", "logs:menu")]]),
    )


@router.callback_query(F.data.startswith("logs:remove:"))
async def logs_remove(c: CallbackQuery):
    if not await check_cb(c):
        return
    parts = c.data.split(":")
    if len(parts) != 5:
        await c.answer("Некорректная команда.", show_alert=True)
        return
    _, _, category, chat_raw, thread_raw = parts
    if category not in LOG_CATEGORIES:
        await c.answer("Неизвестная категория.", show_alert=True)
        return
    try:
        destination = (int(chat_raw), int(thread_raw) or None)
    except ValueError:
        await c.answer("Некорректный получатель.", show_alert=True)
        return
    async with SessionFactory() as db:
        key = f"log_chat_ids_{category}"
        setting = await db.get(AppSetting, key)
        destinations = log_destinations_from_value(setting.value if setting else "")
        destinations = [item for item in destinations if item != destination]
        value = ",".join(
            f"{chat_id}:{thread_id}" if thread_id is not None else str(chat_id)
            for chat_id, thread_id in destinations
        )
        if setting:
            setting.value = value
        else:
            db.add(AppSetting(key=key, value=value))
        await db.commit()
    await logs_category(c)
