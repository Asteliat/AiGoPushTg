"""Account connection: QR code, phone number and proxy input."""

from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import Flow
from app.bot.core import LOGIN_TASKS
from app.bot.core import MAIN
from app.bot.core import PHONE_LOGIN_DATA
from app.bot.core import QR_LOGIN_DATA
from app.bot.core import cfg
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import delete_msg
from app.bot.core import edit
from app.bot.core import edit_anchor
from app.bot.core import finish_prompt
from app.bot.core import kb
from app.bot.core import private
from app.bot.core import prompt_message
from app.bot.core import router
from app.bot.navigation import cancel_return_for
from app.storage.db import SessionFactory
from app.storage.models import Account
from app.telegram_gateway.proxy import ProxyConfigurationError
from app.telegram_gateway.proxy import encrypt_proxy
from app.telegram_gateway.proxy import telethon_proxy
from app.telegram_gateway.session import configure_telethon_sqlite_session
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile
from aiogram.types import CallbackQuery
from aiogram.types import Message
from sqlalchemy import select
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
import asyncio
import io
import os
import re
import secrets


@router.callback_query(F.data == "acc:add")
async def acc_add(c, state: FSMContext):
    if not await check_cb(c):
        return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True)
        return
    await state.clear()
    await edit(
        c,
        "Выберите способ подключения:",
        kb(
            [
                [("📷 QR · без прокси", "acc:add:direct")],
                [("🌐 QR · через прокси", "acc:add:proxy")],
                [("📱 Номер телефона · без прокси", "acc:add:phone:direct")],
                [("📱 Номер телефона · через прокси", "acc:add:phone:proxy")],
                [("⬅️ Назад", "acc:list")],
            ]
        ),
    )


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
    if not await edit_anchor(
        bot, ui_data, "Готовлю QR-вход…", kb([[("❌ Отменить подключение", "nav:cancel")]])
    ):
        await bot.send_message(
            uid, "Готовлю QR-вход…", reply_markup=kb([[("❌ Отменить подключение", "nav:cancel")]])
        )
    task = asyncio.create_task(qr_login(bot, uid, proxy_config, encrypted, ui_data, state))
    LOGIN_TASKS[uid] = task


@router.callback_query(F.data == "acc:add:direct")
async def acc_add_direct(c, state: FSMContext):
    if not await check_cb(c):
        return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
    )
    await c.answer()
    await start_qr_login(c.message.bot, c.from_user.id, state)


@router.callback_query(F.data == "acc:add:proxy")
async def acc_add_proxy(c, state: FSMContext):
    if not await check_cb(c):
        return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True)
        return
    if not cfg.proxy_encryption_key.strip():
        await c.answer(
            "Сначала задайте PROXY_ENCRYPTION_KEY в .env и перезапустите приложение.",
            show_alert=True,
        )
        return
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
        login_method="qr",
    )
    await edit(
        c,
        "Выберите тип прокси:",
        kb(
            [
                [("SOCKS5", "proxy:type:socks5"), ("HTTP", "proxy:type:http")],
                [("⬅️ Назад", "acc:add")],
            ]
        ),
    )


@router.callback_query(F.data == "acc:add:phone:direct")
async def acc_add_phone_direct(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
        login_method="phone",
    )
    await c.answer()
    await start_phone_login_prompt(c.message.bot, c.from_user.id, state)


@router.callback_query(F.data == "acc:add:phone:proxy")
async def acc_add_phone_proxy(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    if not c.message or c.message.chat.type != "private":
        await c.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в .env.", show_alert=True)
        return
    if not cfg.proxy_encryption_key.strip():
        await c.answer(
            "Сначала задайте PROXY_ENCRYPTION_KEY в .env и перезапустите приложение.",
            show_alert=True,
        )
        return
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
        login_method="phone",
    )
    await edit(
        c,
        "Выберите тип прокси:",
        kb(
            [
                [("SOCKS5", "proxy:type:socks5"), ("HTTP", "proxy:type:http")],
                [("⬅️ Назад", "acc:add")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("proxy:type:"))
async def proxy_type(c, state: FSMContext):
    if not await check_cb(c):
        return
    kind = c.data.rsplit(":", 1)[-1]
    if kind not in {"socks5", "http"}:
        await c.answer("Неизвестный тип прокси.", show_alert=True)
        return
    existing = await state.get_data()
    await state.update_data(
        proxy_type=kind,
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=existing.get("return_callback", "acc:list"),
        login_method=existing.get("login_method", "qr"),
    )
    await state.set_state(Flow.proxy_host)
    await edit(c, "Введите IP-адрес или домен прокси:", CANCEL)


@router.message(Flow.proxy_host)
async def proxy_host(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    host = (m.text or "").strip()
    if (
        not host
        or len(host) > 253
        or any(ch.isspace() for ch in host)
        or "://" in host
        or "/" in host
    ):
        await prompt_message(m, state, "Введите только IP или домен, без протокола и пути.")
        return
    await delete_msg(m)
    await state.update_data(proxy_host=host)
    await state.set_state(Flow.proxy_port)
    await prompt_message(m, state, "Введите порт прокси (1–65535):")


@router.message(Flow.proxy_port)
async def proxy_port(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    value = (m.text or "").strip()
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        await prompt_message(m, state, "Порт должен быть числом от 1 до 65535.")
        return
    await delete_msg(m)
    await state.update_data(proxy_port=int(value))
    await state.set_state(Flow.proxy_username)
    await prompt_message(m, state, "Логин прокси или «-», если авторизация не нужна:")


@router.message(Flow.proxy_username)
async def proxy_username(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    value = (m.text or "").strip()
    await delete_msg(m)
    if value == "-":
        data = await state.get_data()
        config = {
            "type": data["proxy_type"],
            "host": data["proxy_host"],
            "port": data["proxy_port"],
            "username": "",
            "password": "",
        }
        if data.get("proxy_account_id"):
            await save_account_proxy(m, state, config)
        else:
            await start_qr_login(m.bot, m.from_user.id, state, config)
        return
    if not value or len(value) > 255:
        await prompt_message(m, state, "Логин должен быть от 1 до 255 символов или «-».")
        return
    await state.update_data(proxy_username=value)
    await state.set_state(Flow.proxy_password)
    await prompt_message(m, state, "Введите пароль прокси. Сообщение будет удалено по возможности:")


@router.message(Flow.proxy_password)
async def proxy_password(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    password = (m.text or "").strip()
    await delete_msg(m)
    if not password or len(password) > 255:
        await prompt_message(m, state, "Пароль должен быть от 1 до 255 символов.")
        return
    data = await state.get_data()
    config = {
        "type": data["proxy_type"],
        "host": data["proxy_host"],
        "port": data["proxy_port"],
        "username": data["proxy_username"],
        "password": password,
    }
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
        account.connection_status = (
            "paused"
            if account.manual_pause
            else ("connecting" if account.enabled else "disconnected")
        )
        await db.commit()
    await finish_prompt(
        m,
        state,
        "Прокси сохранён зашифрованно. Worker переподключит аккаунт автоматически.",
        kb(
            [
                [("🌐 Настройки прокси", f"acc:proxysettings:{aid}")],
                [("👤 К аккаунту", f"acc:view:{aid}"), ("🏠 Главное меню", "nav:home")],
            ]
        ),
    )


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
        await bot.send_message(
            uid, "Вход уже запущен. Нажмите «Отмена», прежде чем начинать заново."
        )
        return
    ui_data = await state.get_data()
    try:
        encrypted = encrypt_proxy(proxy_config) if proxy_config else None
    except ProxyConfigurationError as exc:
        if not await edit_anchor(bot, ui_data, str(exc), MAIN):
            await bot.send_message(uid, str(exc), reply_markup=MAIN)
        return
    PHONE_LOGIN_DATA[uid] = {
        "proxy_config": proxy_config,
        "encrypted_proxy": encrypted,
        "client": None,
        "session_path": None,
        "ui_data": ui_data,
        "success": False,
    }
    await state.clear()
    await state.update_data(
        ui_chat_id=ui_data.get("ui_chat_id"),
        ui_message_id=ui_data.get("ui_message_id"),
        return_callback=ui_data.get("return_callback"),
    )
    await state.set_state(Flow.phone_number)
    text = "📱 Вход по номеру телефона\n\nВведите номер в международном формате, например +79991234567. Сообщение будет удалено по возможности."
    if not await edit_anchor(bot, ui_data, text, CANCEL):
        await bot.send_message(uid, text, reply_markup=CANCEL)


@router.message(Flow.phone_number)
async def phone_number_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    uid = m.from_user.id
    phone = (m.text or "").strip()
    await delete_msg(m)
    if not re.fullmatch(r"\+?[0-9]{7,16}", phone):
        await prompt_message(
            m, state, "Введите номер в международном формате, например +79991234567."
        )
        return
    data = PHONE_LOGIN_DATA.get(uid)
    if not data:
        await finish_prompt(m, state, "Сеанс входа истёк. Начните подключение заново.", MAIN)
        return
    folder = cfg.data_dir / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    session_path = folder / ("account_" + secrets.token_hex(8))
    client = TelegramClient(
        str(session_path),
        cfg.telegram_api_id,
        cfg.telegram_api_hash,
        proxy=telethon_proxy(config=data["proxy_config"]),
    )
    configure_telethon_sqlite_session(client)
    data["client"], data["session_path"], data["phone"] = client, session_path, phone
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        data["phone_code_hash"] = sent.phone_code_hash
        await state.set_state(Flow.phone_code)
        await prompt_message(
            m,
            state,
            "Код отправлен Telegram. Проверьте сообщения от Telegram или SMS и введите код. Сообщение удаляется по возможности.",
        )
    except Exception as exc:
        await cleanup_phone_login(uid)
        await finish_prompt(
            m,
            state,
            f"Не удалось запросить код: {type(exc).__name__}. Проверьте номер и попробуйте снова.",
            MAIN,
        )


async def save_phone_account(uid: int, state: FSMContext, bot):
    data = PHONE_LOGIN_DATA.get(uid)
    if not data or not data.get("client"):
        await state.clear()
        await bot.send_message(
            uid, "Сеанс входа истёк. Начните подключение заново.", reply_markup=MAIN
        )
        return
    client = data["client"]
    me = await client.get_me()
    if not me:
        raise RuntimeError("Telegram не вернул профиль.")
    name = " ".join(x for x in [me.first_name, me.last_name] if x) or str(me.id)
    async with SessionFactory() as db:
        row = await db.scalar(select(Account).where(Account.telegram_user_id == me.id))
        if row:
            row.username, row.display_name, row.session_path, row.enabled = (
                me.username,
                name,
                str(data["session_path"]),
                True,
            )
            row.manual_pause = False
            row.connection_status = "connecting"
            row.proxy_config_encrypted = data["encrypted_proxy"]
        else:
            db.add(
                Account(
                    telegram_user_id=me.id,
                    username=me.username,
                    display_name=name,
                    session_path=str(data["session_path"]),
                    enabled=True,
                    connection_status="connecting",
                    reply_mode="manual",
                    ai_enabled=False,
                    proxy_config_encrypted=data["encrypted_proxy"],
                )
            )
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
    if not await edit_anchor(
        bot,
        ui_data,
        text,
        kb([[("👤 Открыть аккаунты", "acc:list")], [("🏠 Главное меню", "nav:home")]]),
    ):
        await bot.send_message(uid, text, reply_markup=MAIN)


@router.message(Flow.phone_code)
async def phone_code_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
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
        await data["client"].sign_in(
            phone=data["phone"], code=code, phone_code_hash=data["phone_code_hash"]
        )
        await save_phone_account(uid, state, m.bot)
    except SessionPasswordNeededError:
        await state.set_state(Flow.phone_password)
        await prompt_message(
            m,
            state,
            "🔐 Telegram запросил пароль двухэтапной аутентификации (2FA).\n\n"
            "Важно: пароль будет доступен владельцу этого бота и сервера. Бот не записывает его в БД "
            "и удаляет сообщение по возможности, но удаление не гарантируется. Продолжайте только "
            "если доверяете этому серверу. Отправьте пароль или нажмите «Отмена».",
        )
    except Exception as exc:
        await prompt_message(
            m, state, f"Код не принят ({type(exc).__name__}). Проверьте код и попробуйте ещё раз."
        )


@router.message(Flow.phone_password)
async def phone_password_received(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
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
        await prompt_message(
            m,
            state,
            f"Пароль 2FA не принят ({type(exc).__name__}). Попробуйте ещё раз или отмените вход.",
        )


async def qr_login(bot, uid, proxy_config=None, encrypted_proxy=None, ui_data=None, state=None):
    folder = cfg.data_dir / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    session_path = folder / ("account_" + secrets.token_hex(8))
    client = TelegramClient(
        str(session_path),
        cfg.telegram_api_id,
        cfg.telegram_api_hash,
        proxy=telethon_proxy(config=proxy_config),
    )
    configure_telethon_sqlite_session(client)
    success = False
    keep_client = False
    qr_message_id = None
    try:
        await client.connect()
        qr = await client.qr_login()
        import qrcode

        img = qrcode.make(qr.url)
        stream = io.BytesIO()
        img.save(stream, format="PNG")
        qr_message = await bot.send_photo(
            uid,
            BufferedInputFile(stream.getvalue(), filename="telegram-login.png"),
            caption="Отсканируйте QR в Telegram: Настройки → Устройства → Подключить устройство. Не пересылайте QR другим людям. Ожидаю подтверждение…",
        )
        qr_message_id = qr_message.message_id
        try:
            await qr.wait(timeout=120)
        except SessionPasswordNeededError:
            if state is None:
                raise RuntimeError("Telegram запросил пароль 2FA, но состояние входа недоступно.")
            QR_LOGIN_DATA[uid] = {
                "client": client,
                "session_path": session_path,
                "proxy_config": proxy_config,
                "encrypted_proxy": encrypted_proxy,
                "ui_data": ui_data or {},
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
        me = await client.get_me()
        if not me:
            raise RuntimeError("Telegram не вернул профиль.")
        name = " ".join(x for x in [me.first_name, me.last_name] if x) or str(me.id)
        async with SessionFactory() as db:
            row = await db.scalar(select(Account).where(Account.telegram_user_id == me.id))
            if row:
                row.username, row.display_name, row.session_path, row.enabled = (
                    me.username,
                    name,
                    str(session_path),
                    True,
                )
                row.manual_pause = False
                row.connection_status = "connecting"
                row.proxy_config_encrypted = encrypted_proxy
            else:
                db.add(
                    Account(
                        telegram_user_id=me.id,
                        username=me.username,
                        display_name=name,
                        session_path=str(session_path),
                        enabled=True,
                        connection_status="connecting",
                        reply_mode="manual",
                        ai_enabled=False,
                        proxy_config_encrypted=encrypted_proxy,
                    )
                )
            await db.commit()
        success = True
        try:
            os.chmod(str(session_path) + ".session", 0o600)
        except OSError:
            pass
        result_text = (
            f"Аккаунт «{name}» подключён. ИИ выключен до отдельного включения в настройках."
        )
        if not await edit_anchor(
            bot,
            ui_data or {},
            result_text,
            kb([[("👤 Открыть аккаунты", "acc:list")], [("🏠 Главное меню", "nav:home")]]),
        ):
            await bot.send_message(uid, result_text, reply_markup=MAIN)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error_text = f"Не удалось подключить аккаунт: {type(exc).__name__}: {str(exc)[:220]}"
        if not await edit_anchor(bot, ui_data or {}, error_text, MAIN):
            await bot.send_message(uid, error_text, reply_markup=MAIN)
    finally:
        LOGIN_TASKS.pop(uid, None)
        if qr_message_id is not None:
            try:
                await bot.delete_message(uid, qr_message_id)
            except Exception:
                pass
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
    if not await check_msg(m) or not await private(m):
        return
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
                row.username, row.display_name, row.session_path, row.enabled = (
                    me.username,
                    name,
                    str(data["session_path"]),
                    True,
                )
                row.manual_pause = False
                row.connection_status = "connecting"
                row.proxy_config_encrypted = data["encrypted_proxy"]
            else:
                db.add(
                    Account(
                        telegram_user_id=me.id,
                        username=me.username,
                        display_name=name,
                        session_path=str(data["session_path"]),
                        enabled=True,
                        connection_status="connecting",
                        reply_mode="manual",
                        ai_enabled=False,
                        proxy_config_encrypted=data["encrypted_proxy"],
                    )
                )
            await db.commit()
        try:
            os.chmod(str(data["session_path"]) + ".session", 0o600)
        except OSError:
            pass
        ui_data = data.get("ui_data") or await state.get_data()
        await state.clear()
        QR_LOGIN_DATA.pop(uid, None)
        try:
            await data["client"].disconnect()
        except Exception:
            pass
        result_text = (
            f"Аккаунт «{name}» подключён. ИИ выключен до отдельного включения в настройках."
        )
        if not await edit_anchor(
            m.bot,
            ui_data,
            result_text,
            kb([[("👤 Открыть аккаунты", "acc:list")], [("🏠 Главное меню", "nav:home")]]),
        ):
            await m.answer(result_text, reply_markup=MAIN)
    except Exception as exc:
        password = ""
        await prompt_message(
            m,
            state,
            f"Пароль 2FA не принят ({type(exc).__name__}). Попробуйте ещё раз или нажмите «Отмена».",
        )
