from __future__ import annotations
import asyncio, io, os, re, secrets, sqlite3, tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, BufferedInputFile
from sqlalchemy import select
from telethon import TelegramClient, utils
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready
from app.telegram_gateway.discovery import search_public_groups, search_web_groups, find_invite_links_in_dialogs
from app.services.ai_responses import global_ai_enabled
from app.services.channels import learn_channel_style
from app.settings import get_settings
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, AppSetting, ChatBinding, ChannelBinding, ChannelAnalysis, Example, Persona
from app.telegram_gateway.proxy import ProxyConfigurationError, encrypt_proxy, telethon_proxy

router = Router()
cfg = get_settings()
LOGIN_TASKS: dict[int, asyncio.Task] = {}
PHONE_LOGIN_DATA: dict[int, dict] = {}
QR_LOGIN_DATA: dict[int, dict] = {}
CHAT_CANDIDATES: dict[tuple[int, int], list[dict]] = {}
CHAT_PENDING_JOIN: dict[tuple[int, int], dict] = {}


@asynccontextmanager
async def isolated_telegram_client(account):
    """Use a consistent temporary copy of the Telethon session SQLite database."""
    with tempfile.TemporaryDirectory(prefix=f"atciv-session-{account.id}-") as temp_dir:
        snapshot_path = os.path.join(temp_dir, "account.session")
        # Telethon appends ".session" when Account.session_path is stored
        # without the suffix. Open that exact live database, never a new empty file.
        source_path = str(account.session_path)
        if not source_path.endswith(".session"):
            source_path += ".session"
        source_db = sqlite3.connect(source_path, timeout=20)
        snapshot_db = sqlite3.connect(snapshot_path, timeout=20)
        try:
            # SQLite backup includes committed WAL content without opening the
            # worker's live session through another Telethon client.
            source_db.backup(snapshot_db, pages=128, sleep=0.05)
        finally:
            snapshot_db.close()
            source_db.close()

        client = TelegramClient(
            snapshot_path,
            cfg.telegram_api_id,
            cfg.telegram_api_hash,
            proxy=telethon_proxy(account.proxy_config_encrypted),
        )
        try:
            await client.connect()
            yield client
        finally:
            if client.is_connected():
                await client.disconnect()


# All navigation uses inline keyboards.

class Flow(StatesGroup):
    agent_name=State(); agent_url=State(); agent_key=State(); agent_model=State(); agent_timeout=State()
    agent_edit=State(); persona_name=State(); persona_style=State()
    example_incoming=State(); example_reply=State()
    proxy_host=State(); proxy_port=State(); proxy_username=State(); proxy_password=State()
    learning_prompt_edit=State(); persona_override=State()
    runtime_value=State(); channel_setting_value=State(); log_chat_id=State()
    phone_number=State(); phone_code=State(); phone_password=State(); qr_password=State()
    chat_search_query=State(); chat_join_link=State()

def admin(uid): return uid is not None and uid in cfg.admin_id_set
def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t,d in row] for row in rows])

MAIN = kb([
    [("👤 Аккаунты", "acc:list"), ("🤖 ИИ-агенты", "agent:menu")],
    [("🎭 Персоны", "persona:menu"), ("💡 Примеры", "example:menu")],
    [("💬 Чаты", "chat:menu"), ("📡 Каналы", "channel:menu")],
    [("⚙️ Настройки", "nav:settings")],
])
CANCEL = kb([[("❌ Отмена", "nav:cancel")]])

async def home_text():
    async with SessionFactory() as db:
        accounts = list((await db.scalars(select(Account).order_by(Account.id))).all())
        agents = list((await db.scalars(select(AIAgent))).all())
        ai_on = await global_ai_enabled(db)
    training = sum(not a.manual_pause and a.learning_status in {"training", "stopping"} for a in accounts)
    paused = sum(a.manual_pause for a in accounts)
    return (
        "🧭 AtcivPerson\nПанель управления Telegram-аккаунтами\n\n"
        f"👤 Аккаунтов: {len(accounts)} · ⏸ На паузе: {paused}\n"
        f"🧠 На обучении: {training}\n"
        f"🤖 ИИ-агентов: {len(agents)} · Глобальный ИИ: {'включён' if ai_on else 'выключен'}\n"
        "Выберите раздел ниже."
    )

async def edit_anchor(bot, data, text, keyboard):
    chat_id, message_id = data.get("ui_chat_id"), data.get("ui_message_id")
    if not chat_id or not message_id:
        return False
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=keyboard)
        return True
    except Exception as exc:
        if "message is not modified" in str(exc).lower():
            return True
        return False

async def prompt_message(m, state, text):
    data = await state.get_data()
    await delete_msg(m)
    if not await edit_anchor(m.bot, data, text, CANCEL):
        await m.answer(text, reply_markup=CANCEL)

async def finish_prompt(m, state, text, keyboard=MAIN):
    data = await state.get_data()
    await delete_msg(m)
    await state.clear()
    if not await edit_anchor(m.bot, data, text, keyboard):
        await m.answer(text, reply_markup=keyboard)
async def check_msg(m):
    if not admin(m.from_user.id if m.from_user else None):
        await m.answer("Доступ закрыт. Проверьте ADMIN_IDS."); return False
    return True
async def check_cb(c):
    if not admin(c.from_user.id):
        await c.answer("Доступ закрыт", show_alert=True); return False
    return True
async def private(m):
    if m.chat.type != "private":
        await m.answer("Это действие доступно только в личном чате с ботом."); return False
    return True
async def delete_msg(m):
    try: await m.delete()
    except Exception: pass
async def show_home(m):
    await m.answer(await home_text(), reply_markup=MAIN)
async def edit(c, text, keyboard):
    # Every ordinary menu offers a direct route home. Input screens retain their cancel button.
    has_home = any(button.callback_data == "nav:home" for row in keyboard.inline_keyboard for button in row)
    has_cancel = any(button.callback_data == "nav:cancel" for row in keyboard.inline_keyboard for button in row)
    if not has_home and not has_cancel and keyboard is not MAIN:
        rows = [list(row) for row in keyboard.inline_keyboard]
        rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="nav:home")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    if c.message:
        try:
            await c.message.edit_text(text, reply_markup=keyboard)
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                try: await c.message.answer(text, reply_markup=keyboard)
                except Exception: pass
    await c.answer()
def back_home(): return kb([[('🏠 Главное меню','nav:home')]])

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    if await check_msg(m):
        await state.clear()
        await cleanup_phone_login(m.from_user.id)
        await cleanup_qr_login(m.from_user.id)
        await show_home(m)
@router.message(F.text.in_({"❌ Отмена","Отмена"}))
async def cancel(m: Message, state: FSMContext):
    if await check_msg(m):
        await state.clear()
        task=LOGIN_TASKS.pop(m.from_user.id, None)
        if task and not task.done(): task.cancel()
        await cleanup_phone_login(m.from_user.id)
        await cleanup_qr_login(m.from_user.id)
        await m.answer("Действие отменено.", reply_markup=MAIN)

@router.callback_query(F.data=="nav:home")
async def home_cb(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    await state.clear()
    await edit(c, await home_text(), MAIN)

@router.callback_query(F.data=="nav:cancel")
async def cancel_cb(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    await state.clear()
    task = LOGIN_TASKS.pop(c.from_user.id, None)
    if task and not task.done(): task.cancel()
    await cleanup_phone_login(c.from_user.id)
    await cleanup_qr_login(c.from_user.id)
    await edit(c, "Действие отменено.\n\n" + await home_text(), MAIN)

@router.callback_query(F.data=="nav:settings")
async def settings_cb(c: CallbackQuery):
    if not await check_cb(c): return
    text = (
        "⚙️ Настройки и справочник\n\n"
        "👤 Аккаунты — подключение по QR или номеру, статус, пауза, прокси, чаты и обучение.\n"
        "🤖 ИИ-агенты — провайдеры, API-ключи, модели, глобальный ИИ и назначение агента аккаунтам.\n"
        "🎭 Персоны — общие стили ответов; изменения применяются ко всем назначенным аккаунтам.\n"
        "💡 Примеры — образцы входящих сообщений и желаемых ответов для стиля.\n"
        "💬 Чаты — выбрать, какие существующие диалоги аккаунтов доступны для обработки.\n"
        "📡 Каналы — отдельный список каналов и анализ обсуждений.\n"
        "Ответы в ручном режиме показываются в уведомлении и не сохраняются.\n\n"
        "В карточке аккаунта: «Пауза» временно останавливает работу; «ИИ» включает генерацию; «Режим» выбирает ручную или автоматическую отправку; «Агент» и «Персона» задают модель и стиль; «Прокси» меняет сетевое подключение; «Автообучение» обновляет контекст.\n\n"
        "API ID/hash, токен бота и ключ шифрования задаются в .env на сервере. Сообщения с кодом и паролем входа удаляются ботом по возможности; не используйте бота, которому не доверяете."
    )
    await edit(c, text, kb([
        [("🎚 Вероятность ответов", "runtime:menu")],
        [("🧾 Логи", "logs:menu")],
        [("⬅️ Главное меню", "nav:home")],
    ]))

LOG_CATEGORIES = {
    "main": "📌 Основные логи",
    "replies": "💬 Ответы ИИ",
    "errors": "🚨 Ошибки",
    "learning": "🧠 Обучение",
    "channels": "📡 Каналы и обсуждения",
}

def log_ids_from_value(value: str | None) -> list[int]:
    result = []
    for raw in (value or "").replace(";", ",").split(","):
        try:
            chat_id = int(raw.strip())
        except (TypeError, ValueError):
            continue
        if chat_id and chat_id not in result:
            result.append(chat_id)
    return result

async def logs_menu_text():
    async with SessionFactory() as db:
        rows = {}
        for category in LOG_CATEGORIES:
            setting = await db.get(AppSetting, f"log_chat_ids_{category}")
            ids = log_ids_from_value(setting.value if setting else "")
            if category == "main" and not ids and cfg.log_chat_id:
                ids = [cfg.log_chat_id]
            rows[category] = ids
    lines = ["🧾 Настройка логов", "", "Для каждого типа событий можно задать один или несколько чатов/групп. Добавляйте ID чата или супергруппы, куда бот сможет отправлять сообщения.", ""]
    for category, label in LOG_CATEGORIES.items():
        ids = rows[category]
        shown = ", ".join(str(value) for value in ids) if ids else "не настроено"
        lines.append(f"{label}: {shown}")
    lines += ["", "Основные логи по умолчанию используют LOG_CHAT_ID из .env, если отдельный ID не добавлен."]
    return "\n".join(lines)

@router.callback_query(F.data == "logs:menu")
async def logs_menu(c: CallbackQuery):
    if not await check_cb(c): return
    rows = [[(label, f"logs:category:{category}")] for category, label in LOG_CATEGORIES.items()]
    rows += [[("⬅️ Настройки", "nav:settings"), ("🏠 Главное меню", "nav:home")]]
    await edit(c, await logs_menu_text(), kb(rows))

@router.callback_query(F.data.startswith("logs:category:"))
async def logs_category(c: CallbackQuery):
    if not await check_cb(c): return
    category = c.data.split(":")[2]
    if category not in LOG_CATEGORIES:
        await c.answer("Неизвестная категория.", show_alert=True); return
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, f"log_chat_ids_{category}")
        ids = log_ids_from_value(setting.value if setting else "")
    if category == "main" and not ids and cfg.log_chat_id:
        ids = [cfg.log_chat_id]
    lines = [LOG_CATEGORIES[category], "", "Чаты для этой категории:"]
    lines += [f"• {chat_id}" for chat_id in ids] if ids else ["• пока не добавлены"]
    rows = [[("➕ Добавить ID чата", f"logs:add:{category}")]]
    rows += [[(f"➖ Удалить {chat_id}", f"logs:remove:{category}:{chat_id}")] for chat_id in ids]
    rows += [[("⬅️ К списку логов", "logs:menu")]]
    await edit(c, "\n".join(lines), kb(rows))

@router.callback_query(F.data.startswith("logs:add:"))
async def logs_add(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    category = c.data.split(":")[2]
    if category not in LOG_CATEGORIES:
        await c.answer("Неизвестная категория.", show_alert=True); return
    await state.clear()
    await state.update_data(log_category=category, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await state.set_state(Flow.log_chat_id)
    await edit(c, f"{LOG_CATEGORIES[category]}\n\nОтправьте числовой ID пользователя, чата или группы. Для супергрупп обычно используется ID вида -1001234567890.", CANCEL)

@router.message(Flow.log_chat_id)
async def logs_add_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    category = data.get("log_category")
    try:
        chat_id = int((m.text or "").strip())
        if chat_id == 0: raise ValueError
    except ValueError:
        await prompt_message(m, state, "Неверный ID. Отправьте целое ненулевое число, например -1001234567890.")
        return
    await delete_msg(m)
    try:
        await m.bot.send_message(chat_id, "✅ Проверка связи: этот чат добавлен для логов AtcivPerson.")
    except Exception as exc:
        await finish_prompt(m, state, f"Не удалось отправить тестовое сообщение в {chat_id}. Проверьте ID и права бота.\nОшибка: {type(exc).__name__}: {str(exc)[:300]}", kb([[("↩️ К категории", f"logs:category:{category}")], [("🧾 Все логи", "logs:menu")]]))
        return
    async with SessionFactory() as db:
        key = f"log_chat_ids_{category}"
        setting = await db.get(AppSetting, key)
        ids = log_ids_from_value(setting.value if setting else "")
        if chat_id not in ids: ids.append(chat_id)
        value = ",".join(str(item) for item in ids)
        if setting: setting.value = value
        else: db.add(AppSetting(key=key, value=value))
        await db.commit()
    await finish_prompt(m, state, f"✅ Чат {chat_id} добавлен в категорию «{LOG_CATEGORIES[category]}».", kb([[("↩️ К категории", f"logs:category:{category}")], [("🧾 Все логи", "logs:menu")]]))

@router.callback_query(F.data.startswith("logs:remove:"))
async def logs_remove(c: CallbackQuery):
    if not await check_cb(c): return
    _, _, category, chat_raw = c.data.split(":")
    if category not in LOG_CATEGORIES:
        await c.answer("Неизвестная категория.", show_alert=True); return
    chat_id = int(chat_raw)
    async with SessionFactory() as db:
        key = f"log_chat_ids_{category}"
        setting = await db.get(AppSetting, key)
        ids = log_ids_from_value(setting.value if setting else "")
        ids = [item for item in ids if item != chat_id]
        if setting: setting.value = ",".join(str(item) for item in ids)
        await db.commit()
    await logs_category(c)

RUNTIME_KEYS = {
    "mention_reply_probability": ("📣 Ответ на упоминания", "%", 0, 100),
    "dm_reply_probability": ("💬 Ответ в личке", "%", 0, 100),
    "proactive_reply_probability": ("🗣 Самостоятельное участие", "%", 0, 100),
    "reply_cooldown_seconds": ("⏱ Пауза между ответами", "сек.", 0, 2147483647),
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
        "Эти значения используются всеми агентами, у которых для конкретного параметра не задано индивидуальное значение.",
        "При изменении глобального значения бот спросит, перезаписать ли настройки всех агентов или изменить только значение по умолчанию для тех, кто наследует глобальные настройки.",
        "",
        "Самостоятельное участие включайте только для аккаунтов, которые явно обозначены как управляемые ИИ. Для таких аккаунтов также включите отметку в карточке аккаунта.",
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
    await state.update_data(runtime_key=key, runtime_scope="global", ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await state.set_state(Flow.runtime_value)
    hint = f"Введите число секунд. Например: 3600 = 01:00:00 (60 минут). Максимум: {maximum} сек. ({format_duration(maximum)})." if unit == "сек." else f"Введите процент от {minimum}% до {maximum}%."
    await edit(c, f"{label}\n{hint}", CANCEL)

@router.message(Flow.runtime_value)
async def runtime_value_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    key = data.get("runtime_key")
    runtime_scope = data.get("runtime_scope")
    if runtime_scope == "agent_channel":
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
    await state.update_data(runtime_key=key, runtime_scope="agent", runtime_aid=aid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
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
    await state.update_data(runtime_key=key, runtime_scope="agent_channel", runtime_aid=aid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
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

# Account menu and QR login
@router.message(F.text=="👤 Аккаунты")
async def accounts(m: Message):
    if not await check_msg(m): return
    await accounts_screen(m)
async def accounts_screen(m):
    async with SessionFactory() as db: rows=list((await db.scalars(select(Account).order_by(Account.id))).all())
    buttons=[[(f"{'⏸' if a.manual_pause else ('🟢' if a.connection_status == 'connected' else '⚪')} #{a.id} · {a.display_name[:28]}",f"acc:view:{a.id}")] for a in rows]
    buttons += [[("🟢 В работе","acc:status:work"),("🧠 На обучении","acc:status:training")], [("💤 В простое","acc:status:idle")]]
    buttons += [[("➕ Добавить аккаунт","acc:add")],[("⬅️ Главное меню","nav:home")]]
    await m.answer("👤 Аккаунты\nВыберите аккаунт или подключите новый." if rows else "Аккаунтов пока нет. Подключите первый аккаунт кнопкой ниже.",reply_markup=kb(buttons))
@router.callback_query(F.data=="acc:list")
async def acc_list(c):
    if not await check_cb(c): return
    async with SessionFactory() as db: rows=list((await db.scalars(select(Account).order_by(Account.id))).all())
    buttons=[[(f"{'⏸' if a.manual_pause else ('🟢' if a.connection_status == 'connected' else '⚪')} #{a.id} · {a.display_name[:28]}",f"acc:view:{a.id}")] for a in rows]
    buttons += [[("🟢 В работе","acc:status:work"),("🧠 На обучении","acc:status:training")], [("💤 В простое","acc:status:idle")]]
    buttons += [[("➕ Добавить аккаунт","acc:add")],[("⬅️ Главное меню","nav:home")]]
    await edit(c,"👤 Аккаунты",kb(buttons))

@router.callback_query(F.data.startswith("acc:status:"))
async def acc_status(c):
    if not await check_cb(c): return
    status = c.data.split(":")[2]
    if status not in {"work", "training", "idle"}:
        await c.answer("Неизвестный статус", show_alert=True); return
    async with SessionFactory() as db:
        rows = list((await db.scalars(select(Account).order_by(Account.id))).all())
        bindings = list((await db.scalars(select(ChatBinding).where(ChatBinding.enabled.is_(True)))).all())
    active_chat_accounts = {b.account_id for b in bindings}
    selected = []
    for account in rows:
        if account.manual_pause:
            current = "idle"
        elif account.learning_status in {"training", "stopping"}:
            current = "training"
        elif account.enabled and account.connection_status == "connected" and account.id in active_chat_accounts:
            current = "work"
        else:
            current = "idle"
        if current == status:
            selected.append(account)
    labels = {"work": "🟢 В работе", "training": "🧠 На обучении", "idle": "💤 В простое"}
    buttons = [[(f"#{a.id} · {a.display_name[:32]}", f"acc:view:{a.id}")] for a in selected]
    buttons += [[("⬅️ Все аккаунты", "acc:list")]]
    await edit(c, f"{labels[status]} · {len(selected)}", kb(buttons))

@router.callback_query(F.data=="acc:add")
async def acc_add(c, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type!="private":
        await c.answer("Откройте личный чат с ботом.",show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.",show_alert=True); return
    await state.clear()
    await edit(c, "Выберите способ подключения:", kb([
        [("📷 QR · без прокси", "acc:add:direct")],
        [("🌐 QR · через прокси", "acc:add:proxy")],
        [("📱 Номер телефона · без прокси", "acc:add:phone:direct")],
        [("📱 Номер телефона · через прокси", "acc:add:phone:proxy")],
        [("⬅️ Назад", "acc:list")],
    ]))

async def start_qr_login(bot, uid, state: FSMContext, proxy_config=None):
    ui_data = await state.get_data()
    if ui_data.get("login_method") == "phone":
        await start_phone_login_prompt(bot, uid, state, proxy_config)
        return
    if uid in LOGIN_TASKS and not LOGIN_TASKS[uid].done():
        await bot.send_message(uid, "Вход уже запущен. Отмените текущий процесс перед новым.")
        return
    ui_data = await state.get_data()
    try:
        encrypted = encrypt_proxy(proxy_config) if proxy_config else None
    except ProxyConfigurationError as exc:
        if not await edit_anchor(bot, ui_data, str(exc), MAIN):
            await bot.send_message(uid, str(exc), reply_markup=MAIN)
        return
    await state.clear()
    if not await edit_anchor(bot, ui_data, "Готовлю QR-вход…", kb([[("❌ Отменить подключение", "nav:cancel")]])):
        await bot.send_message(uid, "Готовлю QR-вход…", reply_markup=kb([[("❌ Отменить подключение", "nav:cancel")]]))
    task = asyncio.create_task(qr_login(bot, uid, proxy_config, encrypted, ui_data, state))
    LOGIN_TASKS[uid] = task

@router.callback_query(F.data=="acc:add:direct")
async def acc_add_direct(c, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await c.answer()
    await start_qr_login(c.message.bot, c.from_user.id, state)

@router.callback_query(F.data=="acc:add:proxy")
async def acc_add_proxy(c, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True); return
    if not cfg.proxy_encryption_key.strip():
        await c.answer("Сначала задайте PROXY_ENCRYPTION_KEY в .env и перезапустите приложение.", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, login_method="qr")
    await edit(c, "Выберите тип прокси:", kb([[("SOCKS5", "proxy:type:socks5"), ("HTTP", "proxy:type:http")], [("⬅️ Назад", "acc:add")]]))

@router.callback_query(F.data=="acc:add:phone:direct")
async def acc_add_phone_direct(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, login_method="phone")
    await c.answer()
    await start_phone_login_prompt(c.message.bot, c.from_user.id, state)

@router.callback_query(F.data=="acc:add:phone:proxy")
async def acc_add_phone_proxy(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c): return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True); return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True); return
    if not cfg.proxy_encryption_key.strip():
        await c.answer("Сначала задайте PROXY_ENCRYPTION_KEY в .env и перезапустите приложение.", show_alert=True); return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, login_method="phone")
    await edit(c, "Выберите тип прокси:", kb([[("SOCKS5", "proxy:type:socks5"), ("HTTP", "proxy:type:http")], [("⬅️ Назад", "acc:add")]]))

@router.callback_query(F.data.startswith("proxy:type:"))
async def proxy_type(c, state: FSMContext):
    if not await check_cb(c): return
    kind = c.data.rsplit(":", 1)[-1]
    if kind not in {"socks5", "http"}:
        await c.answer("Неизвестный тип прокси.", show_alert=True); return
    existing = await state.get_data()
    await state.update_data(proxy_type=kind, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, login_method=existing.get("login_method", "qr"))
    await state.set_state(Flow.proxy_host)
    await edit(c, "Введите IP-адрес или домен прокси:", CANCEL)

@router.message(Flow.proxy_host)
async def proxy_host(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    host = (m.text or "").strip()
    if not host or len(host) > 253 or any(ch.isspace() for ch in host) or "://" in host or "/" in host:
        await prompt_message(m, state, "Введите только IP или домен, без протокола и пути."); return
    await delete_msg(m)
    await state.update_data(proxy_host=host)
    await state.set_state(Flow.proxy_port)
    await prompt_message(m, state, "Введите порт прокси (1–65535):")

@router.message(Flow.proxy_port)
async def proxy_port(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    value = (m.text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        await prompt_message(m, state, "Порт должен быть числом от 1 до 65535."); return
    await delete_msg(m)
    await state.update_data(proxy_port=int(value))
    await state.set_state(Flow.proxy_username)
    await prompt_message(m, state, "Логин прокси или «-», если авторизация не нужна:")

@router.message(Flow.proxy_username)
async def proxy_username(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    value = (m.text or "").strip()
    await delete_msg(m)
    if value == "-":
        data = await state.get_data()
        config = {"type": data["proxy_type"], "host": data["proxy_host"], "port": data["proxy_port"], "username": "", "password": ""}
        if data.get("proxy_account_id"):
            await save_account_proxy(m, state, config)
        else:
            await start_qr_login(m.bot, m.from_user.id, state, config)
        return
    if not value or len(value) > 255:
        await prompt_message(m, state, "Логин должен быть от 1 до 255 символов или «-»."); return
    await state.update_data(proxy_username=value)
    await state.set_state(Flow.proxy_password)
    await prompt_message(m, state, "Введите пароль прокси. Сообщение будет удалено по возможности:")

@router.message(Flow.proxy_password)
async def proxy_password(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    password = (m.text or "").strip()
    await delete_msg(m)
    if not password or len(password) > 255:
        await prompt_message(m, state, "Пароль должен быть от 1 до 255 символов."); return
    data = await state.get_data()
    config = {"type": data["proxy_type"], "host": data["proxy_host"], "port": data["proxy_port"], "username": data["proxy_username"], "password": password}
    if data.get("proxy_account_id"):
        await save_account_proxy(m, state, config)
    else:
        await start_qr_login(m.bot, m.from_user.id, state, config)

async def save_account_proxy(m: Message, state: FSMContext, config: dict):
    data = await state.get_data()
    aid = data.get("proxy_account_id")
    try:
        encrypted = encrypt_proxy(config)
    except ProxyConfigurationError as exc:
        await prompt_message(m, state, str(exc))
        return
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account:
            await finish_prompt(m, state, "Аккаунт не найден.")
            return
        account.proxy_config_encrypted = encrypted
        account.connection_status = "paused" if account.manual_pause else ("connecting" if account.enabled else "disconnected")
        await db.commit()
    await finish_prompt(m, state, "Прокси сохранён зашифрованно. Worker переподключит аккаунт автоматически.", kb([
        [("🌐 Настройки прокси", f"acc:proxysettings:{aid}")],
        [("👤 К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))

async def cleanup_phone_login(uid: int) -> None:
    data = PHONE_LOGIN_DATA.pop(uid, None)
    if not data:
        return
    client = data.get("client")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    if not data.get("success"):
        path = data.get("session_path")
        if path:
            for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
                try:
                    os.unlink(str(path) + suffix)
                except FileNotFoundError:
                    pass

async def start_phone_login_prompt(bot, uid: int, state: FSMContext, proxy_config=None):
    if uid in LOGIN_TASKS and not LOGIN_TASKS[uid].done():
        await bot.send_message(uid, "Вход уже запущен. Отмените текущий процесс перед новым.")
        return
    if uid in PHONE_LOGIN_DATA:
        await bot.send_message(uid, "Вход уже запущен. Нажмите «Отмена», прежде чем начинать заново.")
        return
    ui_data = await state.get_data()
    try:
        encrypted = encrypt_proxy(proxy_config) if proxy_config else None
    except ProxyConfigurationError as exc:
        if not await edit_anchor(bot, ui_data, str(exc), MAIN):
            await bot.send_message(uid, str(exc), reply_markup=MAIN)
        return
    PHONE_LOGIN_DATA[uid] = {
        "proxy_config": proxy_config, "encrypted_proxy": encrypted, "client": None,
        "session_path": None, "ui_data": ui_data, "success": False,
    }
    await state.clear()
    await state.update_data(ui_chat_id=ui_data.get("ui_chat_id"), ui_message_id=ui_data.get("ui_message_id"))
    await state.set_state(Flow.phone_number)
    text = "📱 Вход по номеру телефона\n\nВведите номер в международном формате, например +79991234567. Сообщение будет удалено по возможности."
    if not await edit_anchor(bot, ui_data, text, CANCEL):
        await bot.send_message(uid, text, reply_markup=CANCEL)

@router.message(Flow.phone_number)
async def phone_number_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    uid = m.from_user.id
    phone = (m.text or "").strip()
    await delete_msg(m)
    if not re.fullmatch(r"\+?[0-9]{7,16}", phone):
        await prompt_message(m, state, "Введите номер в международном формате, например +79991234567.")
        return
    data = PHONE_LOGIN_DATA.get(uid)
    if not data:
        await finish_prompt(m, state, "Сеанс входа истёк. Начните подключение заново.", MAIN)
        return
    folder = cfg.data_dir / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    session_path = folder / ("account_" + secrets.token_hex(8))
    client = TelegramClient(str(session_path), cfg.telegram_api_id, cfg.telegram_api_hash, proxy=telethon_proxy(config=data["proxy_config"]))
    data["client"], data["session_path"], data["phone"] = client, session_path, phone
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        data["phone_code_hash"] = sent.phone_code_hash
        await state.set_state(Flow.phone_code)
        await prompt_message(m, state, "Код отправлен Telegram. Проверьте сообщения от Telegram или SMS и введите код. Сообщение удаляется по возможности.")
    except Exception as exc:
        await cleanup_phone_login(uid)
        await finish_prompt(m, state, f"Не удалось запросить код: {type(exc).__name__}. Проверьте номер и попробуйте снова.", MAIN)

async def save_phone_account(uid: int, state: FSMContext, bot):
    data = PHONE_LOGIN_DATA.get(uid)
    if not data or not data.get("client"):
        await state.clear()
        await bot.send_message(uid, "Сеанс входа истёк. Начните подключение заново.", reply_markup=MAIN)
        return
    client = data["client"]
    me = await client.get_me()
    if not me:
        raise RuntimeError("Telegram не вернул профиль.")
    name = " ".join(x for x in [me.first_name, me.last_name] if x) or str(me.id)
    async with SessionFactory() as db:
        row = await db.scalar(select(Account).where(Account.telegram_user_id == me.id))
        if row:
            row.username, row.display_name, row.session_path, row.enabled = me.username, name, str(data["session_path"]), True
            row.manual_pause = False
            row.connection_status = "connecting"
            row.proxy_config_encrypted = data["encrypted_proxy"]
        else:
            db.add(Account(
                telegram_user_id=me.id, username=me.username, display_name=name,
                session_path=str(data["session_path"]), enabled=True, connection_status="connecting",
                reply_mode="manual", ai_enabled=False, proxy_config_encrypted=data["encrypted_proxy"],
            ))
        await db.commit()
    try:
        os.chmod(str(data["session_path"]) + ".session", 0o600)
    except OSError:
        pass
    data["success"] = True
    ui_data = data.get("ui_data") or await state.get_data()
    await state.clear()
    await cleanup_phone_login(uid)
    text = f"Аккаунт «{name}» подключён по номеру телефона. ИИ выключен до отдельного включения в настройках."
    if not await edit_anchor(bot, ui_data, text, kb([[("👤 Открыть аккаунты", "acc:list")], [("🏠 Главное меню", "nav:home")]])):
        await bot.send_message(uid, text, reply_markup=MAIN)

@router.message(Flow.phone_code)
async def phone_code_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    uid = m.from_user.id
    code = (m.text or "").strip().replace(" ", "")
    await delete_msg(m)
    data = PHONE_LOGIN_DATA.get(uid)
    if not data or not data.get("client"):
        await finish_prompt(m, state, "Сеанс входа истёк. Начните подключение заново.", MAIN)
        return
    if not re.fullmatch(r"[0-9]{3,10}", code):
        await prompt_message(m, state, "Введите код Telegram цифрами.")
        return
    try:
        await data["client"].sign_in(phone=data["phone"], code=code, phone_code_hash=data["phone_code_hash"])
        await save_phone_account(uid, state, m.bot)
    except SessionPasswordNeededError:
        await state.set_state(Flow.phone_password)
        await prompt_message(
            m, state,
            "🔐 Telegram запросил пароль двухэтапной аутентификации (2FA).\n\n"
            "Важно: пароль будет доступен владельцу этого бота и сервера. Бот не записывает его в БД "
            "и удаляет сообщение по возможности, но удаление не гарантируется. Продолжайте только "
            "если доверяете этому серверу. Отправьте пароль или нажмите «Отмена»."
        )
    except Exception as exc:
        await prompt_message(m, state, f"Код не принят ({type(exc).__name__}). Проверьте код и попробуйте ещё раз.")

@router.message(Flow.phone_password)
async def phone_password_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    uid = m.from_user.id
    password = m.text or ""
    await delete_msg(m)
    data = PHONE_LOGIN_DATA.get(uid)
    if not data or not data.get("client"):
        await finish_prompt(m, state, "Сеанс входа истёк. Начните подключение заново.", MAIN)
        return
    try:
        # Keep the 2FA password only in this stack frame; never log or persist it.
        await data["client"].sign_in(password=password)
        password = ""
        await save_phone_account(uid, state, m.bot)
    except Exception as exc:
        password = ""
        await prompt_message(m, state, f"Пароль 2FA не принят ({type(exc).__name__}). Попробуйте ещё раз или отмените вход.")

async def qr_login(bot, uid, proxy_config=None, encrypted_proxy=None, ui_data=None, state=None):
    folder=cfg.data_dir/"sessions"; folder.mkdir(parents=True,exist_ok=True); os.chmod(folder,0o700)
    session_path=folder/("account_"+secrets.token_hex(8))
    client=TelegramClient(str(session_path),cfg.telegram_api_id,cfg.telegram_api_hash, proxy=telethon_proxy(config=proxy_config))
    success = False
    keep_client = False
    qr_message_id = None
    try:
        await client.connect()
        qr=await client.qr_login()
        import qrcode
        img=qrcode.make(qr.url); stream=io.BytesIO(); img.save(stream,format="PNG")
        qr_message = await bot.send_photo(uid,BufferedInputFile(stream.getvalue(),filename="telegram-login.png"),
            caption="Отсканируйте QR в Telegram: Настройки → Устройства → Подключить устройство. Не пересылайте QR другим людям. Ожидаю подтверждение…")
        qr_message_id = qr_message.message_id
        try:
            await qr.wait(timeout=120)
        except SessionPasswordNeededError:
            if state is None:
                raise RuntimeError("Telegram запросил пароль 2FA, но состояние входа недоступно.")
            QR_LOGIN_DATA[uid] = {
                "client": client, "session_path": session_path, "proxy_config": proxy_config,
                "encrypted_proxy": encrypted_proxy, "ui_data": ui_data or {},
            }
            keep_client = True
            await state.set_state(Flow.qr_password)
            twofa_text = (
                "🔐 Telegram запросил пароль двухэтапной аутентификации (2FA).\n\n"
                "Отправьте пароль в этот личный чат. Бот не сохраняет его в БД и не выводит в логи, "
                "а сообщение удаляется по возможности. Удаление не гарантируется — используйте только "
                "собственный доверенный сервер. Нажмите «Отмена», если передумали."
            )
            if not await edit_anchor(bot, ui_data or {}, twofa_text, CANCEL):
                await bot.send_message(uid, twofa_text, reply_markup=CANCEL)
            return
        me=await client.get_me()
        if not me: raise RuntimeError("Telegram не вернул профиль.")
        name=" ".join(x for x in [me.first_name,me.last_name] if x) or str(me.id)
        async with SessionFactory() as db:
            row=await db.scalar(select(Account).where(Account.telegram_user_id==me.id))
            if row:
                row.username,row.display_name,row.session_path,row.enabled=me.username,name,str(session_path),True
                row.manual_pause=False
                row.connection_status="connecting"
                row.proxy_config_encrypted=encrypted_proxy
            else:
                db.add(Account(telegram_user_id=me.id,username=me.username,display_name=name,session_path=str(session_path),enabled=True,connection_status="connecting",reply_mode="manual",ai_enabled=False,proxy_config_encrypted=encrypted_proxy))
            await db.commit()
        success = True
        try: os.chmod(str(session_path)+".session",0o600)
        except OSError: pass
        result_text = f"Аккаунт «{name}» подключён. ИИ выключен до отдельного включения в настройках."
        if not await edit_anchor(bot, ui_data or {}, result_text, kb([[("👤 Открыть аккаунты", "acc:list")], [("🏠 Главное меню", "nav:home")]])):
            await bot.send_message(uid, result_text, reply_markup=MAIN)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error_text = f"Не удалось подключить аккаунт: {type(exc).__name__}: {str(exc)[:220]}"
        if not await edit_anchor(bot, ui_data or {}, error_text, MAIN):
            await bot.send_message(uid, error_text, reply_markup=MAIN)
    finally:
        LOGIN_TASKS.pop(uid,None)
        if qr_message_id is not None:
            try: await bot.delete_message(uid, qr_message_id)
            except Exception: pass
        try:
            if not keep_client:
                await client.disconnect()
        finally:
            if not success and not keep_client:
                for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
                    try:
                        os.unlink(str(session_path) + suffix)
                    except FileNotFoundError:
                        pass

async def cleanup_qr_login(uid: int) -> None:
    data = QR_LOGIN_DATA.pop(uid, None)
    if not data:
        return
    client = data.get("client")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    path = data.get("session_path")
    if path:
        for suffix in (".session", ".session-journal", ".session-wal", ".session-shm"):
            try:
                os.unlink(str(path) + suffix)
            except FileNotFoundError:
                pass

@router.message(Flow.qr_password)
async def qr_password_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    uid = m.from_user.id
    password = m.text or ""
    await delete_msg(m)
    data = QR_LOGIN_DATA.get(uid)
    if not data or not data.get("client"):
        await state.clear()
        await finish_prompt(m, state, "Сеанс QR-входа истёк. Начните подключение заново.", MAIN)
        return
    try:
        await data["client"].sign_in(password=password)
        password = ""
        me = await data["client"].get_me()
        if not me:
            raise RuntimeError("Telegram не вернул профиль.")
        name = " ".join(x for x in [me.first_name, me.last_name] if x) or str(me.id)
        async with SessionFactory() as db:
            row = await db.scalar(select(Account).where(Account.telegram_user_id == me.id))
            if row:
                row.username, row.display_name, row.session_path, row.enabled = me.username, name, str(data["session_path"]), True
                row.manual_pause = False
                row.connection_status = "connecting"
                row.proxy_config_encrypted = data["encrypted_proxy"]
            else:
                db.add(Account(
                    telegram_user_id=me.id, username=me.username, display_name=name,
                    session_path=str(data["session_path"]), enabled=True, connection_status="connecting",
                    reply_mode="manual", ai_enabled=False, proxy_config_encrypted=data["encrypted_proxy"],
                ))
            await db.commit()
        try: os.chmod(str(data["session_path"]) + ".session", 0o600)
        except OSError: pass
        ui_data = data.get("ui_data") or await state.get_data()
        await state.clear()
        QR_LOGIN_DATA.pop(uid, None)
        try: await data["client"].disconnect()
        except Exception: pass
        result_text = f"Аккаунт «{name}» подключён. ИИ выключен до отдельного включения в настройках."
        if not await edit_anchor(m.bot, ui_data, result_text, kb([[("👤 Открыть аккаунты", "acc:list")], [("🏠 Главное меню", "nav:home")]])):
            await m.answer(result_text, reply_markup=MAIN)
    except Exception as exc:
        password = ""
        await prompt_message(m, state, f"Пароль 2FA не принят ({type(exc).__name__}). Попробуйте ещё раз или нажмите «Отмена».")

@router.callback_query(F.data.startswith("acc:view:"))
async def acc_view(c, account_id: int | None = None):
    if not await check_cb(c): return
    aid = account_id if account_id is not None else int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        agent=await db.get(AIAgent,a.ai_agent_id) if a.ai_agent_id else None
        persona=await db.get(Persona,a.persona_id) if a.persona_id else None
        chats=len((await db.scalars(select(ChatBinding).where(ChatBinding.account_id==aid,ChatBinding.enabled.is_(True)))).all())
    learning = a.learning_status in {"training", "stopping"}
    if learning and a.learning_ends_at:
        deadline = a.learning_ends_at.strftime("%d.%m.%Y %H:%M UTC")
        learning_text = f"обучение на паузе · {a.learning_limit} сообщений · до {deadline}" if a.manual_pause else (f"обучение · {a.learning_limit} сообщений · до {deadline}" if a.learning_status == "training" else "завершается")
    else:
        if a.learning_prompt.strip() and a.learning_completed_at:
            completed = a.learning_completed_at.strftime("%d.%m.%Y %H:%M UTC")
            learning_text = f"контекст сохранён · последнее обучение {completed}"
        else:
            learning_text = "контекст сохранён вручную" if a.learning_prompt.strip() else "не запускалось"
    conn_label = {"connected": "подключён", "connecting": "подключается", "paused": "пауза", "disconnected": "отключён", "error": "ошибка подключения", "unauthorized": "сессия не авторизована"}.get(a.connection_status, a.connection_status)
    txt=f"👤 {a.display_name} (@{a.username or 'нет username'})\nID: #{aid}\nАккаунт: {'включён' if a.enabled else 'выключен'}\nСоединение: {conn_label}\nРабота: {'⏸ на паузе' if a.manual_pause else ('активна' if a.enabled else 'отключена')}\nИИ для аккаунта: {'включён' if a.ai_enabled else 'выключен'}\nРежим ответов: {'автоматический' if a.reply_mode=='automatic' else 'ручной'}\nАгент: {agent.name if agent else 'не назначен'}\nПерсона: {persona.name if persona else 'не назначена'}\nАктивных чатов: {chats}\nОбучение: {learning_text}\nПрокси: {'настроен' if a.proxy_config_encrypted else 'нет'}\nИИ обозначен в профиле: {'да' if a.ai_identity_disclosed else 'нет'}\nИндивидуальные правила: {'заданы' if a.persona_style_override.strip() else 'нет'}"
    buttons=[
        [(("▶️ Возобновить работу" if a.manual_pause else "⏸ Пауза работы"),f"acc:pause:{aid}")],
        [(("⏸ Отключить аккаунт" if a.enabled else "▶️ Подключить аккаунт"),f"acc:toggle:{aid}")],
        [(("🤖 Выключить ИИ" if a.ai_enabled else "🤖 Включить ИИ"),f"acc:ai:{aid}")],
        [(f"Режим: {'Автомат' if a.reply_mode=='automatic' else 'Ручной'} · изменить",f"acc:mode:{aid}")],
        [("🧠 Назначить ИИ-агента",f"acc:agents:{aid}")],
        [("🎭 Назначить персону",f"acc:personas:{aid}")],
        [("🌐 Настройки прокси",f"acc:proxysettings:{aid}")],
        [(("🤖 Снять отметку об ИИ в профиле" if a.ai_identity_disclosed else "🤖 Подтвердить: ИИ указан в профиле"),f"acc:aidisclosure:{aid}")],
        [("✏️ Индивидуальные правила",f"acc:personaoverride:{aid}")],
        [("💡 Примеры аккаунта",f"example:account:{aid}")],
        [("💬 Чаты аккаунта",f"chat:account:{aid}")],
        [("🔗 Вступить в чат по ссылке",f"chat:joinlink:{aid}")],
        [((("⏳ Завершается обучение" if a.learning_status == "stopping" else "⏹ Закончить обучение") if learning else "🧠 Автообучение"), (f"learn:stop:{aid}" if learning else f"acc:learn:{aid}"))],
        [("🧠 Контекст обучения",f"acc:learnprompt:{aid}")],
        [("⬅️ К аккаунтам","acc:list"),("🏠 Главное меню","nav:home")]]
    await edit(c,txt,kb(buttons))
@router.callback_query(F.data.startswith("acc:aidisclosure:"))
async def acc_ai_disclosure_toggle(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account:
            await c.answer("Аккаунт не найден.", show_alert=True); return
        account.ai_identity_disclosed = not account.ai_identity_disclosed
        await db.commit()
    await acc_view(c, aid)

@router.callback_query(F.data.startswith("acc:proxysettings:"))
async def acc_proxy_settings(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account: await c.answer("Аккаунт не найден", show_alert=True); return
    current = "прокси настроен" if account.proxy_config_encrypted else "прямое соединение"
    await edit(c, f"🌐 Настройки прокси аккаунта #{aid}\nТекущее подключение: {current}. Настройки сохраняются зашифрованными.", kb([
        [("🔄 Изменить прокси", f"acc:proxyset:{aid}")],
        [("🚫 Убрать прокси", f"acc:proxyclear:{aid}")],
        [("⬅️ К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))

@router.callback_query(F.data.startswith("acc:proxyset:"))
async def acc_proxy_set(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    if not cfg.proxy_encryption_key.strip():
        await c.answer("Сначала задайте PROXY_ENCRYPTION_KEY в .env.", show_alert=True); return
    await state.clear()
    await state.update_data(proxy_account_id=aid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await edit(c, "Выберите тип прокси для существующего аккаунта:", kb([
        [("SOCKS5", "proxy:type:socks5"), ("HTTP", "proxy:type:http")],
        [("⬅️ Отмена", f"acc:proxysettings:{aid}")],
    ]))

@router.callback_query(F.data.startswith("acc:proxyclear:"))
async def acc_proxy_clear_confirm(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    await edit(c, f"Убрать прокси у аккаунта #{aid}? После этого worker переподключит его напрямую, если аккаунт включён и не на паузе.", kb([
        [("🚫 Да, убрать прокси", f"acc:proxyclearconfirm:{aid}")],
        [("⬅️ Отмена", f"acc:proxysettings:{aid}")],
    ]))

@router.callback_query(F.data.startswith("acc:proxyclearconfirm:"))
async def acc_proxy_clear(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        if not account: await c.answer("Аккаунт не найден", show_alert=True); return
        account.proxy_config_encrypted = None
        account.connection_status = "paused" if account.manual_pause else ("connecting" if account.enabled else "disconnected")
        await db.commit()
    await edit(c, f"Прокси аккаунта #{aid} удалён. Worker переподключится согласно состоянию аккаунта.", kb([
        [("🌐 Настройки прокси", f"acc:proxysettings:{aid}")],
        [("👤 К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))

@router.callback_query(F.data.startswith("acc:pause:"))
async def acc_pause(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        if not a.enabled:
            await c.answer("Сначала подключите аккаунт.", show_alert=True); return
        a.manual_pause=not a.manual_pause
        a.connection_status = "paused" if a.manual_pause else "connecting"
        await db.commit()
    await acc_view(c)

@router.callback_query(F.data.startswith("acc:toggle:"))
async def acc_toggle(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        if a.enabled and a.learning_status in {"training", "stopping"}:
            await c.answer("Сначала завершите автообучение, затем отключайте аккаунт.", show_alert=True); return
        a.enabled=not a.enabled
        a.manual_pause = False
        a.connection_status = "connecting" if a.enabled else "disconnected"
        await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:ai:"))
async def acc_ai(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.ai_enabled=not a.ai_enabled; await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:mode:"))
async def acc_mode(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(Account,aid)
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        if a.learning_status in {"training", "stopping"} and a.reply_mode == "manual":
            await c.answer("Нельзя включить автоматические ответы во время обучения.", show_alert=True); return
        a.reply_mode="manual" if a.reply_mode=="automatic" else "automatic"; await db.commit()
    await acc_view(c)
@router.callback_query(F.data.startswith("acc:agents:"))
async def acc_agents(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db: agents=list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all())
    buttons=[[(f"{'⭐ ' if a.is_default else ''}{a.name} · {'вкл' if a.enabled else 'выкл'}",f"acc:setagent:x:{aid}:{a.id}")] for a in agents]
    buttons += [[("Без агента",f"acc:setagent:x:{aid}:0")],[("⬅️ Назад",f"acc:view:{aid}")]]
    await edit(c,"Выберите ИИ-агента:",kb(buttons))
@router.callback_query(F.data.startswith("acc:setagent:"))
async def acc_set_agent(c):
    if not await check_cb(c): return
    _,_,_,aid,agent= c.data.split(":")
    async with SessionFactory() as db:
        a=await db.get(Account,int(aid)); ag=await db.get(AIAgent,int(agent)) if int(agent) else None
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.ai_agent_id=ag.id if ag else None; await db.commit()
    await acc_view(c, int(aid))
@router.callback_query(F.data.startswith("acc:personas:"))
async def acc_personas(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db: ps=list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    buttons=[[(p.name,f"acc:setpersona:x:{aid}:{p.id}")] for p in ps]
    buttons += [[("Без персоны",f"acc:setpersona:x:{aid}:0")],[("⬅️ Назад",f"acc:view:{aid}")]]
    await edit(c,"Выберите персону:",kb(buttons))
@router.callback_query(F.data.startswith("acc:setpersona:"))
async def acc_set_persona(c):
    if not await check_cb(c): return
    _,_,_,aid,pid=c.data.split(":")
    async with SessionFactory() as db:
        a=await db.get(Account,int(aid))
        if not a: await c.answer("Аккаунт не найден",show_alert=True); return
        a.persona_id=int(pid) or None; await db.commit()
    await acc_view(c, int(aid))


@router.callback_query(F.data.startswith("acc:learnprompt:"))
async def acc_learning_prompt(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
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
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, context_account_id=aid)
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
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, persona_override_account_id=aid)
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
async def acc_learning_menu(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        chat_count = len((await db.scalars(select(ChatBinding).where(
            ChatBinding.account_id == aid, ChatBinding.enabled.is_(True)
        ))).all())
        enabled_agent = await db.scalar(select(AIAgent.id).where(AIAgent.enabled.is_(True)))
        ai_on = await global_ai_enabled(db)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True); return
    if not account.enabled:
        await c.answer("Сначала включите аккаунт.", show_alert=True); return
    if account.manual_pause:
        await c.answer("Сначала возобновите работу аккаунта.", show_alert=True); return
    if account.reply_mode != "manual":
        await c.answer("Перед обучением переключите режим ответов на «Ручной».", show_alert=True); return
    if not chat_count:
        await c.answer("Список диалогов ещё не загружен. Откройте «Чаты аккаунта» для первичной загрузки и повторите попытку.", show_alert=True); return
    if not ai_on or not enabled_agent:
        await c.answer("Включите глобальный ИИ и настройте хотя бы одного ИИ-агента.", show_alert=True); return
    buttons = [
        [("✅ Подтверждаю право на обработку", f"learn:confirm:{aid}")],
        [("⬅️ Назад", f"acc:view:{aid}")],
    ]
    await edit(c, f"🧠 Автообучение · аккаунт #{aid}\n\nБудут обработаны все доступные подходящие чаты аккаунта. Система сохранит краткую сводку, временные тексты удалит после завершения. Новые сообщения учитываются до остановки или максимум 3 дня. Подтвердите право передавать эти сообщения выбранному ИИ-провайдеру. Обучение не отправляет сообщения в чаты.", kb(buttons))


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

# AI agent management
@router.message(F.text=="🤖 ИИ-агенты")
async def agents(m):
    if not await check_msg(m): return
    await agents_screen(m)
async def agents_screen(m):
    async with SessionFactory() as db:
        aa=list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all())
        on=await global_ai_enabled(db)
        enabled_count=len((await db.scalars(select(Account).where(Account.ai_enabled.is_(True)))).all())
        total_accounts=len((await db.scalars(select(Account))).all())
    all_accounts_ai=total_accounts > 0 and enabled_count == total_accounts
    buttons=[[(f"{'⭐ ' if a.is_default else ''}{'🟢' if a.enabled else '⚪'} {a.name}",f"agent:view:{a.id}")] for a in aa]
    buttons += [[(("🟢 Выключить ИИ для всех" if on else "🤖 Включить ИИ для всех"),"agent:global")],
                [("⏸ Выключить ИИ на аккаунтах" if all_accounts_ai else "🤖 Включить ИИ на аккаунтах","agent:accounts_toggle")],
                [("🔀 Назначить агент всем аккаунтам","agent:assignall")],
                [("➕ Добавить ИИ-агента","agent:add")], [("⬅️ Главное меню","nav:home")]]
    await m.answer(f"🤖 ИИ-агенты\nГлобально: {'включён' if on else 'выключен'}\nАккаунтов с локальным ИИ: {enabled_count}",reply_markup=kb(buttons))
@router.callback_query(F.data=="agent:menu")
async def agents_cb(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        aa=list((await db.scalars(select(AIAgent).order_by(AIAgent.id))).all())
        on=await global_ai_enabled(db)
        enabled_count=len((await db.scalars(select(Account).where(Account.ai_enabled.is_(True)))).all())
        total_accounts=len((await db.scalars(select(Account))).all())
    all_accounts_ai=total_accounts > 0 and enabled_count == total_accounts
    buttons=[[(f"{'⭐ ' if a.is_default else ''}{'🟢' if a.enabled else '⚪'} {a.name}",f"agent:view:{a.id}")] for a in aa]
    buttons += [[(("🟢 Выключить ИИ для всех" if on else "🤖 Включить ИИ для всех"),"agent:global")],
                [("⏸ Выключить ИИ на аккаунтах" if all_accounts_ai else "🤖 Включить ИИ на аккаунтах","agent:accounts_toggle")],
                [("🔀 Назначить агент всем аккаунтам","agent:assignall")],
                [("➕ Добавить ИИ-агента","agent:add")], [("⬅️ Главное меню","nav:home")]]
    await edit(c,f"🤖 ИИ-агенты · глобально {'включён' if on else 'выключен'}",kb(buttons))
@router.callback_query(F.data=="agent:accounts_toggle")
async def accounts_ai_toggle(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        accounts=list((await db.scalars(select(Account))).all())
        enable=not bool(accounts) or not all(a.ai_enabled for a in accounts)
        for account in accounts:
            account.ai_enabled=enable
        await db.commit()
    await agents_cb(c)

@router.callback_query(F.data=="agent:global")
async def global_toggle(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        s=await db.get(AppSetting,"ai_global_enabled")
        if not s: s=AppSetting(key="ai_global_enabled",value="false"); db.add(s)
        s.value="false" if s.value.lower()=="true" else "true"; await db.commit()
    await agents_cb(c)
@router.callback_query(F.data=="agent:add")
async def agent_add(c,state):
    if not await check_cb(c): return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await state.set_state(Flow.agent_name)
    await edit(c, "Введите название профиля, например Groq или OpenAI.", CANCEL)
@router.message(Flow.agent_name)
async def agent_name(m,state):
    if not await check_msg(m) or not await private(m): return
    name=(m.text or "").strip()
    if not name or len(name)>80: await prompt_message(m,state,"Название должно содержать от 1 до 80 символов."); return
    async with SessionFactory() as db:
        if await db.scalar(select(AIAgent.id).where(AIAgent.name==name)): await prompt_message(m,state,"Такое название уже есть."); return
    await delete_msg(m)
    await state.update_data(name=name); await state.set_state(Flow.agent_url)
    await prompt_message(m,state,"Введите Base URL API, например https://api.openai.com/v1")
@router.message(Flow.agent_url)
async def agent_url(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip().rstrip("/")
    if not v.startswith("https://") and not (v.startswith("http://") and "localhost" in v): await prompt_message(m,state,"Нужен HTTPS URL (HTTP только localhost)."); return
    await delete_msg(m)
    await state.update_data(url=v); await state.set_state(Flow.agent_key); await prompt_message(m,state,"Введите API-ключ. Сообщение будет удалено по возможности.")
@router.message(Flow.agent_key)
async def agent_key(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip(); await delete_msg(m)
    if len(v)<5 or len(v)>1000: await prompt_message(m,state,"Некорректная длина ключа."); return
    await state.update_data(key=v); await state.set_state(Flow.agent_model); await prompt_message(m,state,"Введите точное имя модели.")
@router.message(Flow.agent_model)
async def agent_model(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v or len(v)>160: await prompt_message(m,state,"Некорректное имя модели."); return
    await delete_msg(m)
    await state.update_data(model=v); await state.set_state(Flow.agent_timeout); await prompt_message(m,state,"Введите тайм-аут в секундах от 10 до 180.")
@router.message(Flow.agent_timeout)
async def agent_timeout(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v.isdigit() or not 10<=int(v)<=180: await prompt_message(m,state,"Введите число от 10 до 180."); return
    d=await state.get_data()
    async with SessionFactory() as db:
        has=bool(await db.scalar(select(AIAgent.id)))
        a=AIAgent(name=d["name"],base_url=d["url"],api_key=d["key"],model=d["model"],timeout_seconds=int(v),is_default=not has)
        db.add(a); await db.commit(); await db.refresh(a)
    await finish_prompt(m,state,f"Профиль «{a.name}» сохранён.",kb([[("🤖 К ИИ-агентам","agent:menu")],[("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data=="agent:assignall")
async def assign_all_menu(c):
    if not await check_cb(c): return
    async with SessionFactory() as db: aa=list((await db.scalars(select(AIAgent).where(AIAgent.enabled.is_(True)))).all())
    await edit(c,"Выберите агент для всех аккаунтов:",kb([[ (a.name,f"agent:assignall:{a.id}") ] for a in aa]+[[("⬅️ Назад","agent:menu")]]))
@router.callback_query(F.data.startswith("agent:assignall:"))
async def assign_all(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a or not a.enabled: await c.answer("Агент не найден или выключен",show_alert=True); return
        for account in (await db.scalars(select(Account))).all(): account.ai_agent_id=aid
        for other in (await db.scalars(select(AIAgent))).all(): other.is_default=other.id==aid
        await db.commit()
    await edit(c,f"«{a.name}» назначен всем аккаунтам. Локальные переключатели ИИ не менялись.",back_home())
@router.callback_query(F.data.startswith("agent:view:"))
async def agent_view(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        assigned=list((await db.scalars(select(Account).where(Account.ai_agent_id==aid))).all())
    masked=("•"*min(12,len(a.api_key))) if a.api_key else "не задан"
    txt=f"🤖 {a.name}\nСтатус: {'включён' if a.enabled else 'выключен'}\nПо умолчанию: {'да' if a.is_default else 'нет'}\nBase URL: {a.base_url}\nМодель: {a.model}\nAPI-ключ: {masked}\nТайм-аут: {a.timeout_seconds} сек.\nАккаунты: {', '.join('#'+str(x.id) for x in assigned) or 'нет'}"
    buttons=[[(("⏸ Выключить профиль" if a.enabled else "▶️ Включить профиль"),f"agent:toggle:{aid}")],
        [("⭐ По умолчанию",f"agent:default:{aid}")]]
    if a.is_default:
        buttons.append([("✖️ Убрать статус по умолчанию",f"agent:unsetdefault:{aid}")])
    buttons.extend([
        [("🎚 Вероятности и паузы",f"agent:runtime:menu:{aid}")],
        [("✏️ Base URL",f"agent:edit:url:{aid}"),("✏️ Модель",f"agent:edit:model:{aid}")],
        [("🔑 API-ключ",f"agent:edit:key:{aid}"),("⏱ Тайм-аут",f"agent:edit:timeout:{aid}")],
        [("🗑 Удалить профиль",f"agent:delete:{aid}")],[("⬅️ К ИИ-агентам","agent:menu")]])
    await edit(c,txt,kb(buttons))
@router.callback_query(F.data.startswith("agent:toggle:"))
async def agent_toggle(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        a.enabled=not a.enabled; await db.commit()
    await agent_view(c)
@router.callback_query(F.data.startswith("agent:default:"))
async def agent_default(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a or not a.enabled: await c.answer("Сначала включите агента",show_alert=True); return
        for x in (await db.scalars(select(AIAgent))).all(): x.is_default=x.id==aid
        await db.commit()
    await agent_view(c)
@router.callback_query(F.data.startswith("agent:unsetdefault:"))
async def agent_unset_default(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        if not a.is_default:
            await c.answer("Этот агент уже не является агентом по умолчанию",show_alert=True)
            await agent_view(c)
            return
        a.is_default=False
        await db.commit()
    await agent_view(c)

@router.callback_query(F.data.startswith("agent:edit:"))
async def agent_edit_start(c,state):
    if not await check_cb(c): return
    _,_,field,aid=c.data.split(":")
    await state.clear(); await state.update_data(edit_field=field,edit_aid=int(aid),ui_chat_id=c.message.chat.id,ui_message_id=c.message.message_id); await state.set_state(Flow.agent_edit)
    await edit(c, "Введите новое значение для "+field+". Для ключа сообщение будет удалено по возможности.", CANCEL)
@router.message(Flow.agent_edit)
async def agent_edit_save(m,state):
    if not await check_msg(m) or not await private(m): return
    d=await state.get_data(); field=d["edit_field"]; v=(m.text or "").strip()
    if field=="key":
        await delete_msg(m)
        if len(v)<5 or len(v)>1000: await prompt_message(m,state,"Некорректный ключ."); return
    elif field=="url":
        v=v.rstrip("/")
        if not v.startswith("https://") and not (v.startswith("http://") and "localhost" in v): await prompt_message(m,state,"Нужен HTTPS URL."); return
    elif field=="model":
        if not v or len(v)>160: await prompt_message(m,state,"Некорректная модель."); return
    elif field=="timeout":
        if not v.isdigit() or not 10<=int(v)<=180: await prompt_message(m,state,"Введите число от 10 до 180."); return
        v=int(v)
    attr={"url":"base_url","model":"model","key":"api_key","timeout":"timeout_seconds"}[field]
    async with SessionFactory() as db:
        a=await db.get(AIAgent,d["edit_aid"])
        if not a: await finish_prompt(m,state,"Агент не найден."); return
        setattr(a,attr,v); await db.commit()
    await finish_prompt(m,state,"Настройки сохранены.",kb([[("🤖 К ИИ-агентам","agent:menu")],[("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("agent:delete:"))
async def agent_delete(c):
    if not await check_cb(c): return
    aid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        a=await db.get(AIAgent,aid)
        if not a: await c.answer("Агент не найден",show_alert=True); return
        was=a.is_default
        for acc in (await db.scalars(select(Account).where(Account.ai_agent_id==aid))).all(): acc.ai_agent_id=None; acc.ai_enabled=False
        await db.delete(a); await db.flush()
        if was:
            replacement=await db.scalar(select(AIAgent).where(AIAgent.enabled.is_(True)).order_by(AIAgent.id))
            if replacement: replacement.is_default=True
        await db.commit()
    await agents_cb(c)

# Personas
@router.message(F.text=="🎭 Персоны")
async def personas(m):
    if not await check_msg(m): return
    await persona_screen(m)
async def persona_screen(m):
    async with SessionFactory() as db: ps=list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    await m.answer("🎭 Персоны\nВыберите персону." if ps else "Персон пока нет. Добавьте первую.",reply_markup=kb([[(p.name,f"persona:view:{p.id}")] for p in ps]+[[("➕ Добавить персону","persona:add")],[("⬅️ Главное меню","nav:home")]]))
@router.callback_query(F.data=="persona:menu")
async def persona_menu(c):
    if not await check_cb(c): return
    async with SessionFactory() as db: ps=list((await db.scalars(select(Persona).order_by(Persona.id))).all())
    await edit(c,"🎭 Персоны",kb([[(p.name,f"persona:view:{p.id}")] for p in ps]+[[("➕ Добавить персону","persona:add")],[("⬅️ Главное меню","nav:home")]]))
@router.callback_query(F.data=="persona:add")
async def persona_add(c,state):
    if not await check_cb(c): return
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await state.set_state(Flow.persona_name)
    await edit(c, "Введите название персоны.", CANCEL)
@router.message(Flow.persona_name)
async def persona_name(m,state):
    if not await check_msg(m) or not await private(m): return
    name=(m.text or "").strip()
    if not name or len(name)>80: await prompt_message(m,state,"Название должно содержать от 1 до 80 символов."); return
    async with SessionFactory() as db:
        if await db.scalar(select(Persona.id).where(Persona.name==name)): await prompt_message(m,state,"Такое имя уже есть."); return
    await delete_msg(m)
    await state.update_data(persona_name=name); await state.set_state(Flow.persona_style); await prompt_message(m,state,"Введите инструкции стиля общения.")
@router.message(Flow.persona_style)
async def persona_style(m,state):
    if not await check_msg(m) or not await private(m): return
    style=(m.text or "").strip()
    if not style or len(style)>5000: await prompt_message(m,state,"Текст должен содержать от 1 до 5000 символов."); return
    d=await state.get_data()
    async with SessionFactory() as db:
        if d.get("persona_edit_id"):
            p = await db.get(Persona, d["persona_edit_id"])
            if not p:
                await finish_prompt(m, state, "Персона не найдена.")
                return
            p.style_instructions = style
            label = "Общая персона обновлена. Изменения применены ко всем использующим её аккаунтам."
        else:
            p=Persona(name=d["persona_name"],description="Создана через бот",style_instructions=style)
            db.add(p)
            label = "Персона сохранена."
        await db.commit()
        await db.refresh(p)
    await finish_prompt(m, state, label, kb([[("🎭 К персонам","persona:menu")],[("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("persona:view:"))
async def persona_view(c):
    if not await check_cb(c): return
    pid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        p=await db.get(Persona,pid); n=len((await db.scalars(select(Example).where(Example.persona_id==pid))).all())
    if not p: await c.answer("Персона не найдена",show_alert=True); return
    await edit(c,f"🎭 {p.name}\n\n{p.style_instructions[:2200]}\n\nПримеров: {n}",kb([[("✏️ Изменить стиль",f"persona:edit:{pid}")],[("🧩 Добавить пример",f"example:add:{pid}")],[("🗑 Удалить персону",f"persona:delete:{pid}")],[("⬅️ К персонам","persona:menu"),("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("persona:edit:"))
async def persona_edit_start(c, state: FSMContext):
    if not await check_cb(c): return
    pid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        persona = await db.get(Persona, pid)
    if not persona: await c.answer("Персона не найдена", show_alert=True); return
    await state.clear()
    await state.update_data(persona_edit_id=pid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await state.set_state(Flow.persona_style)
    await edit(c, f"Изменение общей персоны «{persona.name}». Изменения сразу повлияют на все аккаунты, которые её используют. Отправьте новый стиль (до 5000 символов).", CANCEL)

@router.callback_query(F.data.startswith("persona:delete:"))
async def persona_delete(c):
    if not await check_cb(c): return
    pid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        p=await db.get(Persona,pid)
        if p:
            for a in (await db.scalars(select(Account).where(Account.persona_id==pid))).all(): a.persona_id=None
            await db.delete(p); await db.commit()
    await persona_menu(c)

# Examples
@router.message(F.text=="🧩 Примеры")
async def examples(m):
    if not await check_msg(m): return
    await example_screen(m)
async def example_screen(m):
    async with SessionFactory() as db:
        es=list((await db.scalars(select(Example).where(Example.account_id.is_(None)).order_by(Example.id.desc()).limit(15))).all())
        ps={p.id:p.name for p in (await db.scalars(select(Persona))).all()}
    buttons=[[(f"#{e.id} · {ps.get(e.persona_id,'Общие')} · {e.incoming_text[:24]}",f"example:view:{e.id}")] for e in es]
    buttons += [[("➕ Добавить общий пример","example:add:0")],[("⬅️ Главное меню","nav:home")]]
    await m.answer("🧩 Примеры · выберите запись." if es else "Примеров пока нет.",reply_markup=kb(buttons))
@router.callback_query(F.data=="example:menu")
async def example_menu(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        es=list((await db.scalars(select(Example).where(Example.account_id.is_(None)).order_by(Example.id.desc()).limit(15))).all()); ps={p.id:p.name for p in (await db.scalars(select(Persona))).all()}
    buttons=[[(f"#{e.id} · {ps.get(e.persona_id,'Общие')} · {e.incoming_text[:24]}",f"example:view:{e.id}")] for e in es]
    buttons += [[("➕ Добавить общий пример","example:add:0")],[("⬅️ Главное меню","nav:home")]]
    await edit(c,"🧩 Примеры",kb(buttons))
@router.callback_query(F.data.startswith("example:account:"))
async def account_examples(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        examples = list((await db.scalars(select(Example).where(Example.account_id == aid).order_by(Example.id.desc()).limit(15))).all())
        common_count = len((await db.scalars(select(Example).where(Example.account_id.is_(None), Example.consent_confirmed.is_(True)))).all())
    if not account: await c.answer("Аккаунт не найден", show_alert=True); return
    buttons = [[(f"#{e.id} · {e.incoming_text[:30]}", f"example:view:{e.id}")] for e in examples]
    buttons += [[("➕ Добавить пример аккаунта", f"example:addacc:{aid}")],
                [("💡 Общая библиотека примеров", "example:menu")],
                [("⬅️ К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")]]
    await edit(c, f"Примеры аккаунта #{aid}\nИндивидуальных: {len(examples)} · Общих доступно: {common_count}", kb(buttons))

@router.callback_query(F.data.startswith("example:addacc:"))
async def account_example_add(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        if not await db.get(Account, aid): await c.answer("Аккаунт не найден", show_alert=True); return
    await state.clear()
    await state.update_data(example_pid=0, example_account_id=aid, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await state.set_state(Flow.example_incoming)
    await edit(c, "Добавление примера для конкретного аккаунта. Отправьте входящее сообщение без личных данных.", CANCEL)

@router.callback_query(F.data.startswith("example:add:"))
async def example_add(c,state):
    if not await check_cb(c): return
    pid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        if pid and not await db.get(Persona,pid): await c.answer("Персона не найдена",show_alert=True); return
    await state.clear(); await state.update_data(example_pid=pid, example_account_id=None, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id); await state.set_state(Flow.example_incoming)
    await edit(c, "Введите входящее сообщение для примера. Удалите личные данные и сохраняйте только разрешённый текст.", CANCEL)
@router.message(Flow.example_incoming)
async def example_incoming(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v or len(v)>3500: await prompt_message(m,state,"Введите текст до 3500 символов."); return
    await delete_msg(m)
    await state.update_data(example_incoming=v); await state.set_state(Flow.example_reply); await prompt_message(m,state,"Введите предпочтительный ответ.")
@router.message(Flow.example_reply)
async def example_reply(m,state):
    if not await check_msg(m) or not await private(m): return
    v=(m.text or "").strip()
    if not v or len(v)>3500: await prompt_message(m,state,"Введите ответ до 3500 символов."); return
    d=await state.get_data()
    async with SessionFactory() as db:
        e=Example(account_id=d.get("example_account_id"),persona_id=d["example_pid"] or None,title="Добавлено через бот",incoming_text=d["example_incoming"],preferred_reply=v,consent_confirmed=True)
        db.add(e)
        await db.commit()
    if d.get("example_account_id"):
        aid = d["example_account_id"]
        await finish_prompt(m,state,"Пример аккаунта сохранён.",kb([[("💡 Примеры аккаунта",f"example:account:{aid}")],[("🏠 Главное меню","nav:home")]]))
    else:
        await finish_prompt(m,state,"Общий пример сохранён.",kb([[("💡 К примерам","example:menu")],[("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("example:view:"))
async def example_view(c):
    if not await check_cb(c): return
    eid=int(c.data.split(":")[2])
    async with SessionFactory() as db: e=await db.get(Example,eid); p=await db.get(Persona,e.persona_id) if e and e.persona_id else None
    if not e: await c.answer("Пример не найден",show_alert=True); return
    back = f"example:account:{e.account_id}" if e.account_id else "example:menu"
    scope = f"Аккаунт #{e.account_id}" if e.account_id else (p.name if p else "Общий")
    await edit(c,f"Пример #{eid} · {scope}\n\nВходящее:\n{e.incoming_text[:1500]}\n\nОтвет:\n{e.preferred_reply[:1500]}",kb([[("🗑 Удалить",f"example:delete:{eid}")],[("⬅️ Назад",back),("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("example:delete:"))
async def example_delete(c):
    if not await check_cb(c): return
    eid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        e=await db.get(Example,eid)
        account_id = e.account_id if e else None
        if e: await db.delete(e); await db.commit()
    if account_id:
        await edit(c, "Пример удалён.", kb([[("💡 К примерам аккаунта", f"example:account:{account_id}")], [("🏠 Главное меню", "nav:home")]]))
    else:
        await example_menu(c)

# Chat discovery and joining
def _parse_chat_link(raw: str) -> tuple[str, str]:
    value = (raw or "").strip().strip("<>[](){}.,;")
    if value.startswith("@") and re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value):
        return "public", value[1:]
    match = re.search(r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(joinchat/[A-Za-z0-9_-]+|\+?[A-Za-z0-9_+-]+)", value)
    if not match:
        raise ValueError("Пришлите публичную ссылку t.me/username или пригласительную ссылку t.me/+…")
    path = match.group(1).split("?")[0].rstrip("/")
    if path.startswith("+"):
        return "invite", path[1:]
    if path.startswith("joinchat/"):
        return "invite", path.split("/", 1)[1]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", path):
        raise ValueError("Не удалось распознать ссылку. Нужна ссылка на группу Telegram.")
    return "public", path


def _group_entity(entity) -> bool:
    if getattr(entity, "broadcast", False):
        return False
    return bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False) or entity.__class__.__name__ == "Chat")


def _candidate_keyboard(aid: int, candidates: list[dict], offset: int = 0):
    rows = []
    page = candidates[offset:offset + 12]
    for i, item in enumerate(page, start=offset):
        title = str(item.get("title") or "Группа")[:30]
        members = int(item.get("members") or 0)
        suffix = f" · {members:,}".replace(",", " ") if members else ""
        rows.append([(f"🔎 {title}{suffix}"[:60], f"chat:candidate:{aid}:{i}")])
    navigation = []
    if offset > 0:
        navigation.append(("⬅️ Назад", f"chat:page:{aid}:{max(0, offset - 12)}"))
    if offset + 12 < len(candidates):
        navigation.append(("➡️ Ещё результаты", f"chat:page:{aid}:{offset + 12}"))
    if navigation:
        rows.append(navigation)
    rows += [[("⬅️ К чатам", "chat:menu"), ("🏠 Главное меню", "nav:home")]]
    return kb(rows)


async def _load_account_client(aid: int):
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        raise RuntimeError("Не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH.")
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account or not account.enabled:
        raise RuntimeError("Аккаунт не найден или отключён.")
    client = TelegramClient(account.session_path, cfg.telegram_api_id, cfg.telegram_api_hash, proxy=telethon_proxy(account.proxy_config_encrypted))
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Сессия аккаунта не авторизована.")
    return account, client


@router.callback_query(F.data.startswith("chat:search:"))
async def chat_search_start(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, chat_search_account_id=aid)
    await state.set_state(Flow.chat_search_query)
    await edit(c, f"🔎 Расширенный поиск Telegram-групп · аккаунт #{aid}\n\nВведите тему или несколько ключевых слов, например: «Python разработка», «настольные игры», «дизайн».\n\nПоиск объединяет результаты Telegram, Google, Яндекса, Bing, Yahoo и DuckDuckGo. Закрытые группы находятся, только если их пригласительные ссылки опубликованы в интернете; неопубликованные приватные группы поисковики не видят.", CANCEL)


@router.message(Flow.chat_search_query)
async def chat_search_query(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    query = (m.text or "").strip()
    await delete_msg(m)
    if len(query) < 2:
        await prompt_message(m, state, "Введите хотя бы два символа для поиска.")
        return
    if len(query) > 100:
        query = query[:100]
    aid = int(data.get("chat_search_account_id", 0))
    anchor = {"ui_chat_id": data.get("ui_chat_id"), "ui_message_id": data.get("ui_message_id")}
    await state.clear()
    native_results = []
    try:
        account, client = await _load_account_client(aid)
        try:
            native_results = await search_public_groups(client, query, limit=80)
        finally:
            await client.disconnect()
    except Exception:
        native_results = []
    try:
        web_results = await search_web_groups(query, limit=180)
    except Exception:
        web_results = []
    combined = {}
    for item in native_results + web_results:
        link = str(item.get("link") or "").casefold()
        if not link:
            continue
        if link in combined:
            current = combined[link]
            current["score"] = max(int(current.get("score") or 0), int(item.get("score") or 0))
            current["source"] = ", ".join(dict.fromkeys((current.get("source", "") + ", " + item.get("source", "")).strip(", ").split(", ")))
            if item.get("members"):
                current["members"] = item["members"]
        else:
            combined[link] = item
    candidates = sorted(combined.values(), key=lambda item: (int(item.get("score") or 0), int(item.get("members") or 0)), reverse=True)
    CHAT_CANDIDATES[(m.from_user.id, aid)] = candidates
    if not candidates:
        await edit_anchor(m.bot, anchor, f"По запросу «{query}» подходящих публичных групп не найдено.\n\nПопробуйте более короткие или альтернативные ключевые слова.", kb([[("🔎 Новый запрос", f"chat:search:{aid}")], [("🧭 Найти ссылки в чатах", f"chat:discover:{aid}")], [("⬅️ К чатам", "chat:menu")]]))
        return
    visible = candidates[:12]
    text_lines = [f"🔎 Результаты по запросу «{query}» · найдено {len(candidates)}", "Страница 1 · показываю 1–{min(12, len(candidates))}", "", "Результаты собраны из Telegram и внешних поисковиков. Ссылки проверяются перед вступлением."]
    for i, item in enumerate(visible, 1):
        flags = " · ✓ проверена" if item.get("verified") else ""
        members = f"{int(item.get('members') or 0):,}".replace(",", " ") + " участников" if item.get("members") else "размер неизвестен"
        text_lines.append(f"{i}. {item['title']} · {members}{flags} · {item.get('source', 'Telegram')}")
    text_lines.append("\nНажмите на результат, чтобы проверить группу и подтвердить вступление.")
    await edit_anchor(m.bot, anchor, "\n".join(text_lines), _candidate_keyboard(aid, candidates, 0))


@router.callback_query(F.data.startswith("chat:discover:"))
async def chat_discover_links(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    await c.answer("Запущено глубокое сканирование. Это может занять время.")
    anchor = {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id}
    await edit_anchor(c.bot, anchor, f"🧭 Глубокий поиск ссылок · аккаунт #{aid}\n\nПросматриваю до 5 000 последних сообщений в каждой группе, где состоит аккаунт. Сканирование может занять значительное время.", kb([[("⬅️ К чатам", "chat:menu")]]))
    scan_stats = {"groups": 0, "messages": 0, "links": 0}
    async def report_progress(groups, messages, links):
        scan_stats.update(groups=groups, messages=messages, links=links)
        if messages % 5000 != 0 and messages != 0:
            return
        progress_text = f"🧭 Глубокий поиск ссылок · аккаунт #{aid}\n\nГрупп обработано: {groups}\nСообщений просмотрено: {messages:,}\nУникальных ссылок найдено: {links}\n\nСканирование продолжается…".replace(",", " ")
        await edit_anchor(c.bot, anchor, progress_text, kb([[("⬅️ К чатам", "chat:menu")]]))
    try:
        account, client = await _load_account_client(aid)
        try:
            candidates = await find_invite_links_in_dialogs(client, dialog_limit=None, messages_per_dialog=5000, progress_callback=report_progress)
        finally:
            await client.disconnect()
    except Exception as exc:
        await edit_anchor(c.bot, {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id}, f"Не удалось найти ссылки: {str(exc)[:180]}", kb([[("⬅️ К чатам", "chat:menu")]]))
        return
    CHAT_CANDIDATES[(c.from_user.id, aid)] = candidates
    if not candidates:
        await edit_anchor(c.bot, {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id}, "🧭 Подходящих ссылок в последних сообщениях доступных групп не найдено.\n\nПоиск проверяет недавние сообщения в группах, к которым аккаунт уже имеет доступ; он не читает закрытые чаты, где аккаунт не состоит.", kb([[("🔎 Поиск публичных групп", f"chat:search:{aid}")], [("⬅️ К чатам", "chat:menu")]]))
        return
    lines = [
        f"🧭 Найдено ссылок: {len(candidates)} · аккаунт #{aid}",
        f"Групп обработано: {scan_stats['groups']} · сообщений просмотрено: {scan_stats['messages']:,}".replace(",", " "),
        "Просмотрено до 5 000 последних сообщений в каждой доступной группе.",
        "",
        "Источник показывает чат, в котором была найдена ссылка. Каждая ссылка будет проверена перед вступлением.",
    ]
    for i, item in enumerate(candidates[:12], 1):
        lines.append(f"{i}. {item['link'][:75]}\n   найдено в: {item.get('source', 'сообщение')}")
    await edit_anchor(c.bot, {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id}, "\n".join(lines), _candidate_keyboard(aid, candidates, 0))


@router.callback_query(F.data.startswith("chat:page:"))
async def chat_candidate_page(c):
    if not await check_cb(c): return
    _, _, aid_raw, offset_raw = c.data.split(":")
    aid, offset = int(aid_raw), max(0, int(offset_raw))
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
    if not candidates:
        await edit(c, "Результаты больше не сохранены. Запустите поиск ещё раз.", kb([[("🔎 Поиск групп", f"chat:search:{aid}")], [("⬅️ К чатам", "chat:menu")]]))
        return
    page = candidates[offset:offset + 12]
    lines = [f"Найденные группы · аккаунт #{aid}", f"Показаны {offset + 1}–{min(offset + 12, len(candidates))} из {len(candidates)}", "", "Выберите результат для проверки."]
    for i, item in enumerate(page, start=offset + 1):
        lines.append(f"{i}. {str(item.get('title') or item.get('link') or 'Группа')[:70]}")
    await edit(c, "\n".join(lines), _candidate_keyboard(aid, candidates, offset))


@router.callback_query(F.data.startswith("chat:candidate:"))
async def chat_candidate_preview(c):
    if not await check_cb(c): return
    _, _, aid_raw, index_raw = c.data.split(":")
    aid, index = int(aid_raw), int(index_raw)
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
    if index < 0 or index >= len(candidates):
        await c.answer("Результаты устарели. Запустите поиск ещё раз.", show_alert=True); return
    item = candidates[index]
    link = item.get("link")
    if not link:
        await c.answer("У этой группы нет публичной ссылки.", show_alert=True); return
    try:
        kind, target = _parse_chat_link(link)
        account, client = await _load_account_client(aid)
        try:
            already = False
            if kind == "public":
                entity = await client.get_entity(target)
                if not _group_entity(entity):
                    raise ValueError("Это канал или профиль, а не группа. Вступление отменено.")
                title = getattr(entity, "title", None) or target
                members = int(getattr(entity, "participants_count", 0) or 0)
                entity_id = utils.get_peer_id(entity)
            else:
                invite = await client(CheckChatInviteRequest(hash=target))
                if isinstance(invite, ChatInviteAlready):
                    entity = invite.chat
                    if not _group_entity(entity):
                        raise ValueError("Ссылка ведёт не на группу. Вступление отменено.")
                    title = getattr(entity, "title", None) or "Группа"
                    members = int(getattr(entity, "participants_count", 0) or 0)
                    entity_id = utils.get_peer_id(entity)
                    already = True
                else:
                    if getattr(invite, "channel", False) and not getattr(invite, "megagroup", False):
                        raise ValueError("Ссылка ведёт на канал, а не группу.")
                    title = getattr(invite, "title", None) or "Закрытая группа"
                    members = len(getattr(invite, "participants", []) or []) or int(getattr(invite, "participants_count", 0) or 0)
                    entity_id = None
        finally:
            await client.disconnect()
    except Exception as exc:
        await edit(c, f"Не удалось проверить ссылку:\n{str(exc)[:220]}", kb([[("⬅️ К результатам", f"chat:results:{aid}")], [("⬅️ К чатам", "chat:menu")]]))
        return
    CHAT_PENDING_JOIN[(c.from_user.id, aid)] = {"link": link, "kind": kind, "target": target, "title": title, "members": members, "entity_id": entity_id, "already": already}
    status = "Аккаунт уже состоит в группе." if already else "Вступление произойдёт только после вашего подтверждения."
    await edit(c, f"👥 {title}\nУчастников: {members or 'неизвестно'}\nСсылка: {link}\n\n{status}\n\nПроверьте название и ссылку перед вступлением.", kb([[("✅ Подтвердить вступление", f"chat:joinconfirm:{aid}")], [("⬅️ К результатам", f"chat:results:{aid}")], [("⬅️ К чатам", "chat:menu")]]))


@router.callback_query(F.data.startswith("chat:results:"))
async def chat_results_back(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
    if not candidates:
        await edit(c, "Результаты больше не сохранены. Запустите поиск ещё раз.", kb([[("🔎 Поиск групп", f"chat:search:{aid}")], [("⬅️ К чатам", "chat:menu")]]))
        return
    lines = [f"Найденные кандидаты · аккаунт #{aid}", "", "Выберите группу для проверки."]
    for i, item in enumerate(candidates[:12], 1):
        lines.append(f"{i}. {str(item.get('title') or item.get('link') or 'Группа')[:70]}")
    await edit(c, "\n".join(lines), _candidate_keyboard(aid, candidates, 0))


@router.callback_query(F.data.startswith("chat:joinlink:"))
async def chat_join_link_start(c, state: FSMContext):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    await state.clear()
    await state.update_data(ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id, chat_join_account_id=aid)
    await state.set_state(Flow.chat_join_link)
    await edit(c, f"🔗 Вступление в группу · аккаунт #{aid}\n\nОтправьте ссылку-приглашение t.me/+… / t.me/joinchat/… или публичную ссылку t.me/username.\n\nСначала бот проверит ссылку и покажет название группы. Само вступление будет только после отдельного подтверждения.", CANCEL)


@router.message(Flow.chat_join_link)
async def chat_join_link_input(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    raw = (m.text or "").strip()
    await delete_msg(m)
    try:
        kind, target = _parse_chat_link(raw)
    except ValueError as exc:
        await prompt_message(m, state, str(exc))
        return
    aid = int(data.get("chat_join_account_id", 0))
    anchor = {"ui_chat_id": data.get("ui_chat_id"), "ui_message_id": data.get("ui_message_id")}
    await state.clear()
    CHAT_CANDIDATES[(m.from_user.id, aid)] = [{"title": raw[:80], "link": raw, "source": "Введено вручную"}]
    # Reuse the same guarded preview flow as search results.
    CHAT_PENDING_JOIN.pop((m.from_user.id, aid), None)
    try:
        account, client = await _load_account_client(aid)
        try:
            already = False
            if kind == "public":
                entity = await client.get_entity(target)
                if not _group_entity(entity):
                    raise ValueError("Ссылка ведёт на канал или профиль, а не группу.")
                title = getattr(entity, "title", None) or target
                members = int(getattr(entity, "participants_count", 0) or 0)
                entity_id = utils.get_peer_id(entity)
            else:
                invite = await client(CheckChatInviteRequest(hash=target))
                if isinstance(invite, ChatInviteAlready):
                    entity = invite.chat
                    if not _group_entity(entity):
                        raise ValueError("Ссылка ведёт не на группу.")
                    title = getattr(entity, "title", None) or "Группа"
                    members = int(getattr(entity, "participants_count", 0) or 0)
                    entity_id = utils.get_peer_id(entity)
                    already = True
                else:
                    if getattr(invite, "channel", False) and not getattr(invite, "megagroup", False):
                        raise ValueError("Ссылка ведёт на канал, а не группу.")
                    title = getattr(invite, "title", None) or "Закрытая группа"
                    members = len(getattr(invite, "participants", []) or []) or int(getattr(invite, "participants_count", 0) or 0)
                    entity_id = None
        finally:
            await client.disconnect()
    except Exception as exc:
        await edit_anchor(m.bot, anchor, f"Не удалось проверить ссылку:\n{str(exc)[:220]}", kb([[("🔗 Ввести другую ссылку", f"chat:joinlink:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))
        return
    CHAT_PENDING_JOIN[(m.from_user.id, aid)] = {"link": raw, "kind": kind, "target": target, "title": title, "members": members, "entity_id": entity_id, "already": already}
    status = "Аккаунт уже состоит в группе." if already else "Вступление произойдёт только после подтверждения."
    await edit_anchor(m.bot, anchor, f"👥 {title}\nУчастников: {members or 'неизвестно'}\nСсылка: {raw}\n\n{status}", kb([[("✅ Подтвердить вступление", f"chat:joinconfirm:{aid}")], [("🔗 Другая ссылка", f"chat:joinlink:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))


@router.callback_query(F.data.startswith("chat:joinconfirm:"))
async def chat_join_confirm(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    pending = CHAT_PENDING_JOIN.get((c.from_user.id, aid))
    if not pending:
        await c.answer("Предпросмотр устарел. Проверьте ссылку ещё раз.", show_alert=True); return
    try:
        account, client = await _load_account_client(aid)
        try:
            entity = None
            if pending.get("already") and pending.get("entity_id"):
                entity = await client.get_entity(pending["entity_id"])
            elif pending["kind"] == "public":
                entity = await client.get_entity(pending["target"])
                await client(JoinChannelRequest(entity))
            else:
                joined = await client(ImportChatInviteRequest(hash=pending["target"]))
                entity = next((x for x in joined.chats if _group_entity(x)), None)
                if entity is None:
                    raise RuntimeError("Telegram не вернул вступившую группу.")
            if entity is None:
                entity = await client.get_entity(pending["target"])
            if not _group_entity(entity):
                raise ValueError("Ссылка не ведёт на группу.")
            chat_id = int(utils.get_peer_id(entity))
            title = (getattr(entity, "title", None) or pending["title"] or "Группа")[:255]
        finally:
            await client.disconnect()
    except Exception as exc:
        # Approval-only invite links are not equivalent to membership; explain that no binding was created.
        if "InviteRequestSentError" in type(exc).__name__:
            await edit(c, "Запрос на вступление отправлен администраторам группы. Аккаунт пока не добавлен в активные чаты — повторите после одобрения.", kb([[("🔗 Проверить другую ссылку", f"chat:joinlink:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))
            return
        if "UserAlreadyParticipantError" in type(exc).__name__:
            try:
                account, client = await _load_account_client(aid)
                try:
                    kind, target = pending["kind"], pending["target"]
                    if kind == "public":
                        entity = await client.get_entity(target)
                    else:
                        invite = await client(CheckChatInviteRequest(hash=target))
                        entity = invite.chat if isinstance(invite, ChatInviteAlready) else None
                    if entity is None or not _group_entity(entity):
                        raise RuntimeError("Не удалось определить уже доступную группу.")
                    chat_id = int(utils.get_peer_id(entity))
                    title = (getattr(entity, "title", None) or pending["title"] or "Группа")[:255]
                finally:
                    await client.disconnect()
            except Exception as nested:
                await edit(c, f"Аккаунт уже состоит в группе, но не удалось обновить её в списке: {type(nested).__name__}.", kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]))
                return
        else:
            await edit(c, f"Не удалось вступить: {type(exc).__name__}. Проверьте срок действия ссылки, лимиты Telegram и доступность группы.", kb([[("🔗 Другая ссылка", f"chat:joinlink:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]))
            return
    async with SessionFactory() as db:
        binding = await db.scalar(select(ChatBinding).where(ChatBinding.account_id == aid, ChatBinding.chat_id == chat_id))
        if binding:
            binding.title = title
            binding.enabled = True
        else:
            db.add(ChatBinding(account_id=aid, chat_id=chat_id, title=title, enabled=True, auto_discovered=False))
        await db.commit()
    CHAT_PENDING_JOIN.pop((c.from_user.id, aid), None)
    await edit(c, f"✅ Группа «{title}» добавлена в чаты аккаунта #{aid}.\n\nЧат включён для работы и обучения. Если нужно, его можно выключить в разделе «Чаты аккаунта».", kb([[("💬 Чаты аккаунта", f"chat:account:{aid}:0")], [("👤 К аккаунту", f"acc:view:{aid}")], [("🏠 Главное меню", "nav:home")]]))


# Chats
@router.message(F.text=="💬 Чаты")
async def chats(m):
    if not await check_msg(m): return
    await chats_screen(m)
async def chats_screen(m):
    async with SessionFactory() as db:
        aa=list((await db.scalars(select(Account).order_by(Account.id))).all())
        bs=list((await db.scalars(select(ChatBinding).order_by(ChatBinding.account_id,ChatBinding.id))).all())
    buttons=[[(f"Загрузить чаты · #{a.id} {a.display_name[:22]}",f"chat:account:{a.id}")] for a in aa]
    buttons += [[(f"🔎 Поиск групп · #{a.id}",f"chat:search:{a.id}"),(f"🧭 Найти ссылки · #{a.id}",f"chat:discover:{a.id}")] for a in aa]
    buttons += [[(f"{'🟢' if b.enabled else '⚪'} #{b.account_id} · {b.title[:30]}",f"chat:toggle:{b.id}")] for b in bs[:20]]
    buttons += [[("⬅️ Главное меню","nav:home")]]
    await m.answer("💬 Чаты\nВыберите аккаунт, чтобы загрузить его чаты, найти публичные группы или просканировать ссылки в сообщениях. Вступление всегда подтверждается вручную." if aa else "Сначала подключите аккаунт в разделе «👤 Аккаунты».",reply_markup=kb(buttons))
@router.callback_query(F.data=="chat:menu")
async def chats_cb(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        aa=list((await db.scalars(select(Account).order_by(Account.id))).all()); bs=list((await db.scalars(select(ChatBinding).order_by(ChatBinding.account_id,ChatBinding.id))).all())
    buttons=[[(f"Загрузить чаты · #{a.id} {a.display_name[:22]}",f"chat:account:{a.id}")] for a in aa]
    buttons += [[(f"🔎 Поиск групп · #{a.id}",f"chat:search:{a.id}"),(f"🧭 Найти ссылки · #{a.id}",f"chat:discover:{a.id}")] for a in aa]
    buttons += [[(f"{'🟢' if b.enabled else '⚪'} #{b.account_id} · {b.title[:30]}",f"chat:toggle:{b.id}")] for b in bs[:20]]
    buttons += [[("⬅️ Главное меню","nav:home")]]
    await edit(c,"💬 Чаты · выберите аккаунт или переключите выбранный чат.",kb(buttons))
@router.callback_query(F.data.startswith("chat:account:"))
async def chat_account(c):
    if not await check_cb(c): return
    parts = c.data.split(":")
    aid = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    if not cfg.telegram_api_id or not cfg.telegram_api_hash: await c.answer("Не заданы Telegram API credentials.",show_alert=True); return
    async with SessionFactory() as db: a=await db.get(Account,aid)
    if not a: await c.answer("Аккаунт не найден",show_alert=True); return
    try:
        async with isolated_telegram_client(a) as client:
            if not await client.is_user_authorized():
                await c.answer("Сессия не авторизована",show_alert=True)
                return
            dialogs=[]
            async for d in client.iter_dialogs():
                entity = d.entity
                if getattr(entity, "deleted", False) or getattr(entity, "bot", False):
                    continue
                title = (d.name or str(d.id)).strip()
                if title:
                    dialogs.append((int(d.id), title[:255]))
    except Exception as exc:
        await c.answer(f"Ошибка загрузки: {type(exc).__name__}: {str(exc)[:180]}",show_alert=True)
        return
    async with SessionFactory() as db:
        existing = list((await db.scalars(select(ChatBinding).where(ChatBinding.account_id == aid))).all())
        by_chat = {binding.chat_id: binding for binding in existing}
        for cid, title in dialogs:
            binding = by_chat.get(cid)
            if binding:
                binding.title = title
            else:
                db.add(ChatBinding(account_id=aid, chat_id=cid, title=title, enabled=True, auto_discovered=True))
        await db.commit()
        selected={b.chat_id for b in (await db.scalars(select(ChatBinding).where(ChatBinding.account_id==aid,ChatBinding.enabled.is_(True)))).all()}
    page_size = 20
    max_page = max(0, (len(dialogs) - 1) // page_size)
    page = max(0, min(page, max_page))
    visible = dialogs[page * page_size:(page + 1) * page_size]
    buttons=[[(f"{'🟢' if cid in selected else '▫️'} {title[:36]}",f"chat:select:x:{aid}:{cid}:{page}")] for cid,title in visible]
    nav = []
    if page > 0: nav.append(("⬅️ Назад", f"chat:account:{aid}:{page-1}"))
    if page < max_page: nav.append(("➡️ Далее", f"chat:account:{aid}:{page+1}"))
    if nav: buttons.append(nav)
    buttons += [[("📋 Выбранные чаты",f"chat:refresh:{aid}:{page}")], [("⬅️ К чатам","chat:menu"),("🏠 Главное меню","nav:home")]]
    await edit(c,f"Чаты аккаунта #{aid}\nСтраница {page+1} из {max_page+1} · Всего доступно: {len(dialogs)}\n🟢 включён для работы и обучения · ▫️ исключён.",kb(buttons))

@router.callback_query(F.data.startswith("chat:select:"))
async def chat_select(c):
    if not await check_cb(c): return
    parts=c.data.split(":"); aid,cid=int(parts[3]),int(parts[4]); page=int(parts[5]) if len(parts)>5 and parts[5].isdigit() else 0
    async with SessionFactory() as db: a=await db.get(Account,aid)
    if not a:
        await c.answer("Аккаунт не найден",show_alert=True)
        return
    try:
        async with isolated_telegram_client(a) as client:
            entity=await client.get_entity(cid)
            title=getattr(entity,"title",None) or getattr(entity,"username",None) or " ".join(x for x in [getattr(entity,"first_name",None),getattr(entity,"last_name",None)] if x) or str(cid)
    except Exception as exc:
        await c.answer(f"Не удалось открыть чат: {type(exc).__name__}: {str(exc)[:180]}",show_alert=True)
        return
    async with SessionFactory() as db:
        b=await db.scalar(select(ChatBinding).where(ChatBinding.account_id==aid,ChatBinding.chat_id==cid))
        if b:
            b.enabled = not b.enabled
            b.title = title
        else:
            db.add(ChatBinding(account_id=aid, chat_id=cid, title=title, enabled=True, auto_discovered=True))
        await db.commit()
    await chat_account_refresh(c,aid,page)
@router.callback_query(F.data.startswith("chat:refresh:"))
async def chat_refresh(c):
    if not await check_cb(c): return
    parts = c.data.split(":")
    aid = int(parts[2]); page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    await chat_account_refresh(c, aid, page)
async def chat_account_refresh(c,aid,page=0):
    async with SessionFactory() as db: bs=list((await db.scalars(select(ChatBinding).where(ChatBinding.account_id==aid).order_by(ChatBinding.id))).all())
    await edit(c,f"Выбранные чаты аккаунта #{aid}. 🟢 включён · ⚪ исключён.",kb([[(f"{'🟢' if b.enabled else '⚪'} {b.title[:36]}",f"chat:toggle:{b.id}")] for b in bs[:40]]+[[("↻ Загрузить список чатов",f"chat:account:{aid}:{page}")],[("⬅️ К чатам","chat:menu"),("🏠 Главное меню","nav:home")]]))
@router.callback_query(F.data.startswith("chat:toggle:"))
async def chat_toggle(c):
    if not await check_cb(c): return
    bid=int(c.data.split(":")[2])
    async with SessionFactory() as db:
        b=await db.get(ChatBinding,bid)
        if not b: await c.answer("Чат не найден",show_alert=True); return
        b.enabled = not b.enabled
        aid = b.account_id
        await db.commit()
    await chat_account_refresh(c,aid)


# Channels: account -> channel -> channel card -> inherited/overridden settings.
CHANNEL_SETTING_FIELDS = {
    "post_instructions": ("⚙️ Постов", "channel_post_instructions", "post_instructions_override", "text"),
    "comments_instructions": ("⚙️ Комментариев", "channel_comments_instructions", "comments_instructions_override", "text"),
    "restrictions_instructions": ("⚙️ Ограничений", "channel_restrictions_instructions", "restrictions_instructions_override", "text"),
    "wait_seconds": ("⏱ Ожидание", "channel_wait_seconds", "comment_wait_seconds_override", "seconds"),
    "min_comments": ("💬 Минимум комментариев", "channel_min_comments", "min_comments_override", "min_comments"),
    "reply_probability": ("🎯 Вероятность ответа", "channel_reply_probability", "reply_probability_override", "percent"),
    "reply_cooldown_seconds": ("⏳ Пауза между ответами", "channel_reply_cooldown_seconds", "reply_cooldown_seconds_override", "seconds"),
}

def channel_setting_value(account, binding, field):
    _, account_attr, channel_attr, _ = CHANNEL_SETTING_FIELDS[field]
    if binding is not None:
        override = getattr(binding, channel_attr)
        if override is not None:
            return override
    return getattr(account, account_attr)

def channel_setting_text(field, value):
    label, _, _, kind = CHANNEL_SETTING_FIELDS[field]
    if kind == "seconds":
        return f"{label}: {format_duration(value)} ({value} сек.)"
    if kind == "percent":
        return f"{label}: {value}%"
    if kind == "min_comments":
        return f"{label}: {value}"
    return label

async def channels_screen_message(m):
    async with SessionFactory() as db:
        accounts = list((await db.scalars(select(Account).order_by(Account.id))).all())
        counts = {}
        for account in accounts:
            counts[account.id] = len((await db.scalars(
                select(ChannelBinding.id).where(ChannelBinding.account_id == account.id)
            )).all())
    rows = [[(f"👤 #{a.id} · {a.display_name[:24]} · каналов: {counts[a.id]}", f"channel:account:{a.id}")] for a in accounts]
    rows += [[("⬅️ Главное меню", "nav:home")]]
    await m.answer("📡 Каналы\nСначала выберите Telegram-аккаунт. Внутри него находятся его каналы и общие настройки для них.", reply_markup=kb(rows))

@router.message(F.text == "📡 Каналы")
async def channels_message(m):
    if not await check_msg(m): return
    await channels_screen_message(m)

@router.callback_query(F.data == "channel:menu")
async def channels_menu(c):
    if not await check_cb(c): return
    async with SessionFactory() as db:
        accounts = list((await db.scalars(select(Account).order_by(Account.id))).all())
        counts = {}
        for account in accounts:
            counts[account.id] = len((await db.scalars(select(ChannelBinding.id).where(ChannelBinding.account_id == account.id))).all())
    rows = [[(f"👤 #{a.id} · {a.display_name[:24]} · каналов: {counts[a.id]}", f"channel:account:{a.id}")] for a in accounts]
    rows += [[("⬅️ Главное меню", "nav:home")]]
    await edit(c, "📡 Каналы\nВыберите аккаунт, чтобы открыть список его каналов и настройки.", kb(rows))

async def channel_account_screen(c, aid):
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        bindings = list((await db.scalars(select(ChannelBinding).where(
            ChannelBinding.account_id == aid
        ).order_by(ChannelBinding.title, ChannelBinding.id))).all())
    if not account:
        await c.answer("Аккаунт не найден.", show_alert=True)
        return
    rows = [[(f"{'🟢' if b.reply_enabled else '🔴'} {b.title[:42]}", f"channel:open:{aid}:{b.channel_id}")] for b in bindings[:70]]
    rows += [
        [("↻ Загрузить / обновить каналы", f"channel:sync:{aid}")],
        [("⚙️ Глобальные настройки каналов", f"channel:gsettings:{aid}")],
        [("🧾 История анализа", f"channel:history:{aid}")],
        [("⬅️ К аккаунтам", "channel:menu"), ("🏠 Главное меню", "nav:home")],
    ]
    summary = "Каналов пока нет. Нажмите «Загрузить / обновить каналы»." if not bindings else "🟢 — ответы в комментариях включены · 🔴 — выключены.\nНажмите на канал, чтобы открыть его карточку. Нажатие не меняет его состояние."
    await edit(c, f"📡 Каналы · аккаунт #{aid} · {account.display_name}\n\n{summary}", kb(rows))

@router.callback_query(F.data.startswith("channel:account:"))
async def channel_account(c):
    if not await check_cb(c): return
    await channel_account_screen(c, int(c.data.split(":")[2]))

@router.callback_query(F.data.startswith("channel:sync:"))
async def channel_sync(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Не заданы Telegram API credentials.", show_alert=True)
        return
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден.", show_alert=True)
        return
    dialogs = []
    try:
        async with isolated_telegram_client(account) as client:
            if not await client.is_user_authorized():
                await c.answer("Сессия аккаунта не авторизована.", show_alert=True)
                return
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if not getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
                    continue
                title = (dialog.name or str(dialog.id)).strip()
                if title:
                    dialogs.append((int(dialog.id), title[:255]))
    except Exception as exc:
        await c.answer(f"Ошибка загрузки каналов: {type(exc).__name__}: {str(exc)[:180]}", show_alert=True)
        return
    async with SessionFactory() as db:
        existing = list((await db.scalars(select(ChannelBinding).where(ChannelBinding.account_id == aid))).all())
        by_id = {row.channel_id: row for row in existing}
        added = 0
        for channel_id, title in dialogs:
            row = by_id.get(channel_id)
            if row:
                row.title = title
            else:
                db.add(ChannelBinding(account_id=aid, channel_id=channel_id, title=title, enabled=False, reply_enabled=False))
                added += 1
        await db.commit()
    await channel_account_screen(c, aid)
    await c.message.answer(f"✅ Загружено каналов: {len(dialogs)} · новых добавлено: {added}")

@router.callback_query(F.data.startswith("channel:open:"))
async def channel_open(c):
    if not await check_cb(c): return
    _, _, aid, channel_id = c.data.split(":")
    aid, channel_id = int(aid), int(channel_id)
    async with SessionFactory() as db:
        binding = await db.scalar(select(ChannelBinding).where(
            ChannelBinding.account_id == aid, ChannelBinding.channel_id == channel_id
        ))
    if not binding:
        await c.answer("Канал не найден. Обновите список каналов.", show_alert=True)
        return
    state = "🟢 Отвечать в комментариях включено" if binding.reply_enabled else "🔴 Отвечать в комментариях выключено"
    await edit(c, f"📡 {binding.title}\nАккаунт: #{aid}\n\n{state}\n\nЗдесь можно включить ответы и отдельно настроить поведение ИИ для этого канала.", kb([
        [("🔴 Выключить ответы" if binding.reply_enabled else "🟢 Включить ответы", f"channel:reply:{binding.id}")],
        [("⚙️ Настроить", f"channel:csettings:{binding.id}")],
        [("🧾 История анализа", f"channel:history:{aid}")],
        [("⬅️ К каналам аккаунта", f"channel:account:{aid}"), ("🏠 Главное меню", "nav:home")],
    ]))

@router.callback_query(F.data.startswith("channel:reply:"))
async def channel_reply_toggle(c):
    if not await check_cb(c): return
    binding_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        binding = await db.get(ChannelBinding, binding_id)
        if not binding:
            await c.answer("Канал не найден.", show_alert=True)
            return
        binding.reply_enabled = not binding.reply_enabled
        binding.enabled = binding.reply_enabled
        await db.commit()
    await channel_open(c)

async def channel_settings_screen(c, scope, object_id):
    async with SessionFactory() as db:
        if scope == "global":
            account = await db.get(Account, object_id)
            binding = None
        else:
            binding = await db.get(ChannelBinding, object_id)
            account = await db.get(Account, binding.account_id) if binding else None
    if not account or (scope == "channel" and not binding):
        await c.answer("Настройки не найдены.", show_alert=True)
        return
    title = "Глобальные настройки" if scope == "global" else f"Настройки канала · {binding.title}"
    lines = [f"⚙️ {title}", "Общие значения этого аккаунта применяются ко всем его каналам. Индивидуальные настройки канала могут их переопределить.", ""]
    rows = []
    explanations = {
        "post_instructions": "Как ИИ должен учитывать содержание публикаций.",
        "comments_instructions": "На что обращать внимание в обсуждении.",
        "restrictions_instructions": "Каких тем и действий избегать.",
        "wait_seconds": "Сколько ждать, пока появится нужное число комментариев.",
        "min_comments": "Минимум комментариев перед анализом и ответом.",
        "reply_probability": "Шанс ответа на подходящий комментарий.",
        "reply_cooldown_seconds": "Минимальная пауза между ответами в этом канале.",
    }
    for field in ("post_instructions", "comments_instructions", "restrictions_instructions", "wait_seconds", "min_comments", "reply_probability", "reply_cooldown_seconds"):
        label, _, _, kind = CHANNEL_SETTING_FIELDS[field]
        value = channel_setting_value(account, binding, field)
        if kind == "text":
            shown = "заданы" if str(value).strip() else "не заданы"
        elif kind == "seconds":
            shown = f"{format_duration(value)} · {value} сек."
        elif kind == "percent":
            shown = f"{value}%"
        else:
            shown = str(value)
        lines.append(f"{label}: {shown}")
        lines.append(f"  {explanations[field]}")
        prefix = "g" if scope == "global" else "c"
        rows.append([(f"✏️ {label}", f"channel:edit:{prefix}:{object_id}:{field}")])
    action = account.channel_insufficient_action if binding is None else (binding.insufficient_action_override or account.channel_insufficient_action)
    action_labels = {
        "wait": "Ждать до появления нужного числа комментариев",
        "skip": "Пропускать, если комментариев меньше минимума",
        "skip_empty": "Не отвечать, если комментариев нет",
    }
    lines.append(f"Если комментариев меньше минимума: {action_labels.get(action, action_labels['wait'])}")
    prefix = "g" if scope == "global" else "c"
    next_action = {"wait": "skip", "skip": "skip_empty", "skip_empty": "wait"}.get(action, "wait")
    next_labels = {
        "wait": "⏳ Ждать до появления нужного числа комментариев",
        "skip": "⏭ Пропускать, если комментариев мало",
        "skip_empty": "🚫 Не отвечать, если комментариев нет",
    }
    rows.append([(next_labels[next_action], f"channel:action:{prefix}:{object_id}")])
    if scope == "channel":
        learning_state = "идёт" if binding.learning_status == "training" else "ошибка — запустите повторно" if binding.learning_status == "error" else "завершено" if binding.learning_completed_at else "не запускалось"
        lines.append(f"Автообучение: {learning_state}")
        lines.append("ИИ изучает до 200 последних текстовых постов и до 5 комментариев к каждому, чтобы понять тематику и стиль обсуждений.")
        rows.append([("🧠 Автообучение по последним 200 постам", f"channel:learn:{object_id}")])
        rows.append([("↩️ Сбросить настройки канала к глобальным", f"channel:inherit:{object_id}")])
        rows.append([("⬅️ К карточке канала", f"channel:open:{account.id}:{binding.channel_id}")])
    else:
        rows.append([("⬅️ К каналам аккаунта", f"channel:account:{account.id}")])
    rows.append([("🏠 Главное меню", "nav:home")])
    await edit(c, "\n".join(lines), kb(rows))

@router.callback_query(F.data.startswith("channel:gsettings:"))
async def channel_global_settings(c):
    if not await check_cb(c): return
    await channel_settings_screen(c, "global", int(c.data.split(":")[2]))

@router.callback_query(F.data.startswith("channel:csettings:"))
async def channel_custom_settings(c):
    if not await check_cb(c): return
    await channel_settings_screen(c, "channel", int(c.data.split(":")[2]))

@router.callback_query(F.data.startswith("channel:edit:"))
async def channel_setting_edit(c, state: FSMContext):
    if not await check_cb(c): return
    _, _, scope, object_raw, field = c.data.split(":")
    if scope not in {"g", "c"} or field not in CHANNEL_SETTING_FIELDS:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    label, _, _, kind = CHANNEL_SETTING_FIELDS[field]
    minimum, maximum = (1, 10) if kind == "min_comments" else (0, 100) if kind == "percent" else (0, 2147483647) if kind == "seconds" else (0, 4000)
    await state.clear()
    await state.update_data(channel_scope=scope, channel_object_id=int(object_raw), channel_field=field, ui_chat_id=c.message.chat.id, ui_message_id=c.message.message_id)
    await state.set_state(Flow.channel_setting_value)
    if kind == "text":
        hint = "Отправьте текст инструкции. Можно несколько абзацев. Чтобы очистить, отправьте точку: ."
    elif kind == "seconds":
        hint = "Введите целое число секунд. Например: 3600 секунд = 01:00:00 (60 минут)."
    elif kind == "percent":
        hint = "Введите процент от 0 до 100."
    else:
        hint = "Введите число от 1 до 10."
    range_hint = f"Максимум: {maximum} сек. ({format_duration(maximum)})." if kind == "seconds" else f"Допустимый диапазон: {minimum}–{maximum}."
    await edit(c, f"{label}\n{hint}\n{range_hint}", CANCEL)

@router.message(Flow.channel_setting_value)
async def channel_setting_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m): return
    data = await state.get_data()
    field, scope, object_id = data.get("channel_field"), data.get("channel_scope"), int(data.get("channel_object_id", 0))
    if field not in CHANNEL_SETTING_FIELDS or scope not in {"g", "c"}:
        await finish_prompt(m, state, "Настройка не найдена.")
        return
    label, account_attr, channel_attr, kind = CHANNEL_SETTING_FIELDS[field]
    raw = (m.text or "").strip()
    if kind == "text":
        value = "" if raw == "." else raw
        if len(value) > 4000:
            await prompt_message(m, state, "Максимальная длина инструкции — 4000 символов.")
            return
    else:
        if not raw.isdigit():
            await prompt_message(m, state, "Введите целое число.")
            return
        value = int(raw)
        minimum, maximum = (1, 10) if kind == "min_comments" else (0, 100) if kind == "percent" else (0, 2147483647)
        if not minimum <= value <= maximum:
            await prompt_message(m, state, f"Допустимый диапазон: {minimum}–{maximum}.")
            return
    await delete_msg(m)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await finish_prompt(m, state, "Аккаунт не найден.")
                return
            setattr(account, account_attr, value)
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await finish_prompt(m, state, "Канал не найден.")
                return
            setattr(binding, channel_attr, value)
            if field == "wait_seconds":
                binding.comment_wait_seconds = value
        await db.commit()
    destination = f"channel:gsettings:{object_id}" if scope == "g" else f"channel:csettings:{object_id}"
    shown = format_duration(value) if kind == "seconds" else f"{value}%" if kind == "percent" else str(value)
    await finish_prompt(m, state, f"✅ {label}: {shown}", kb([[("⬅️ К настройкам", destination)], [("🏠 Главное меню", "nav:home")]]))

@router.callback_query(F.data.startswith("channel:action:"))
async def channel_action_toggle(c):
    if not await check_cb(c): return
    _, _, scope, object_raw = c.data.split(":")
    object_id = int(object_raw)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            current = account.channel_insufficient_action
            account.channel_insufficient_action = {"wait": "skip", "skip": "skip_empty", "skip_empty": "wait"}.get(current, "wait")
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await c.answer("Канал не найден.", show_alert=True)
                return
            account = await db.get(Account, binding.account_id)
            current = binding.insufficient_action_override or account.channel_insufficient_action
            binding.insufficient_action_override = {"wait": "skip", "skip": "skip_empty", "skip_empty": "wait"}.get(current, "wait")
        await db.commit()
    await channel_settings_screen(c, "global" if scope == "g" else "channel", object_id)

@router.callback_query(F.data.startswith("channel:inherit:"))
async def channel_inherit(c):
    if not await check_cb(c): return
    binding_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        binding = await db.get(ChannelBinding, binding_id)
        if not binding:
            await c.answer("Канал не найден.", show_alert=True)
            return
        binding.post_instructions_override = None
        binding.comments_instructions_override = None
        binding.restrictions_instructions_override = None
        binding.comment_wait_seconds_override = None
        binding.min_comments_override = None
        binding.insufficient_action_override = None
        binding.reply_probability_override = None
        binding.reply_cooldown_seconds_override = None
        await db.commit()
    await channel_settings_screen(c, "channel", binding_id)

@router.callback_query(F.data.startswith("channel:learn:"))
async def channel_learn(c):
    if not await check_cb(c): return
    binding_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        binding = await db.get(ChannelBinding, binding_id)
        account = await db.get(Account, binding.account_id) if binding else None
        if not binding or not account:
            await c.answer("Канал или аккаунт не найден.", show_alert=True)
            return
        if binding.learning_status == "training":
            await c.answer("Автообучение этого канала уже выполняется.", show_alert=True)
            return
        binding.learning_status = "training"
        await db.commit()
        account_snapshot = account
    await c.answer("Автообучение запущено.")
    status = await c.message.answer("🧠 Изучаю до 200 последних текстовых публикаций и доступные комментарии. Это может занять несколько минут…")
    try:
        async with isolated_telegram_client(account_snapshot) as client:
            if not await client.is_user_authorized():
                raise RuntimeError("Сессия аккаунта не авторизована.")
            prompt = await learn_channel_style(client, account=account_snapshot, channel=binding)
        async with SessionFactory() as db:
            current = await db.get(ChannelBinding, binding_id)
            if current:
                current.learning_prompt = prompt
                current.learning_status = "idle"
                current.learning_completed_at = datetime.now(timezone.utc)
                await db.commit()
        await status.edit_text("✅ Автообучение завершено. Выводы сохранены и будут учитываться при ответах в этом канале.")
    except Exception as exc:
        async with SessionFactory() as db:
            current = await db.get(ChannelBinding, binding_id)
            if current:
                current.learning_status = "error"
                await db.commit()
        await status.edit_text(f"⚠️ Не удалось выполнить автообучение: {type(exc).__name__}: {str(exc)[:500]}")

@router.callback_query(F.data.startswith("channel:history:"))
async def channel_history(c):
    if not await check_cb(c): return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        rows = list((await db.scalars(
            select(ChannelAnalysis).where(ChannelAnalysis.account_id == aid)
            .order_by(ChannelAnalysis.created_at.desc()).limit(10)
        )).all())
    if not rows:
        await edit(c, f"🧾 История анализа каналов аккаунта #{aid} пока пуста.", kb([
            [("⬅️ К каналам аккаунта", f"channel:account:{aid}")], [("🏠 Главное меню", "nav:home")],
        ]))
        return
    buttons = [[(f"{'🟢' if row.on_topic else '🟠'} {row.channel_title[:25]} · пост #{row.post_id}", f"channel:analysis:{row.id}")] for row in rows]
    buttons += [[("⬅️ К каналам аккаунта", f"channel:account:{aid}"), ("🏠 Главное меню", "nav:home")]]
    await edit(c, f"🧾 Последние анализы аккаунта #{aid}.", kb(buttons))

@router.callback_query(F.data.startswith("channel:analysis:"))
async def channel_analysis_view(c):
    if not await check_cb(c): return
    analysis_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        row = await db.get(ChannelAnalysis, analysis_id)
    if not row:
        await c.answer("Результат анализа не найден.", show_alert=True)
        return
    topic = "по теме" if row.on_topic else "не по теме"
    text = (
        f"📡 {row.channel_title} · пост #{row.post_id}\n"
        f"Комментариев: {row.comments_count} · обсуждение: {topic}\n\n"
        f"Сводка ИИ:\n{row.comments_summary[:1800]}\n\n"
        f"Вариант ответа:\n{row.suggested_response[:1800]}"
    )
    await edit(c, text, kb([
        [("⬅️ История анализа", f"channel:history:{row.account_id}")],
        [("🏠 Главное меню", "nav:home")],
    ]))
