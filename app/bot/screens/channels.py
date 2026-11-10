"""Channel monitoring, comments and publication settings."""

from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import Flow
from app.bot.core import cfg
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import delete_msg
from app.bot.core import edit
from app.bot.core import finish_prompt
from app.bot.core import isolated_telegram_client
from app.bot.core import kb
from app.bot.core import private
from app.bot.core import prompt_message
from app.bot.core import router
from app.bot.navigation import cancel_return_for
from app.bot.screens.runtime import format_duration
from app.services.channels import decode_channel_comment_policy_action
from app.services.channels import effective_channel_chat_replies_enabled
from app.services.channels import effective_channel_reply_mode
from app.services.channels import encode_channel_comment_policy_action
from app.services.channels import resolve_channel_comment_policy
from app.storage.db import SessionFactory
from app.storage.models import Account
from app.storage.models import ChannelAnalysis
from app.storage.models import ChannelBinding
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from datetime import datetime
from datetime import timezone
from sqlalchemy import select

CHANNEL_FIELD_HINTS = {
    "post_instructions": "Что учитывать в самих публикациях.",
    "comments_instructions": "На что обращать внимание в обсуждении.",
    "restrictions_instructions": "Каких тем и действий избегать.",
    "reply_probability": "Шанс, что ИИ подготовит комментарий к публикации.",
    "reply_cooldown_seconds": "Минимальная пауза между ответами в этом канале.",
}

CHANNEL_SETTING_FIELDS = {
    "post_instructions": (
        "⚙️ Постов",
        "channel_post_instructions",
        "post_instructions_override",
        "text",
    ),
    "wait_seconds": (
        "⏱ Время ожидания",
        "channel_wait_seconds",
        "comment_wait_seconds_override",
        "seconds",
    ),
    "min_comments": (
        "💬 Минимум комментариев",
        "channel_min_comments",
        "min_comments_override",
        "min_comments",
    ),
    "comments_instructions": (
        "⚙️ Комментариев",
        "channel_comments_instructions",
        "comments_instructions_override",
        "text",
    ),
    "restrictions_instructions": (
        "⚙️ Ограничений",
        "channel_restrictions_instructions",
        "restrictions_instructions_override",
        "text",
    ),
    "reply_probability": (
        "🎯 Вероятность ответа",
        "channel_reply_probability",
        "reply_probability_override",
        "percent",
    ),
    "reply_cooldown_seconds": (
        "⏳ Пауза между ответами",
        "channel_reply_cooldown_seconds",
        "reply_cooldown_seconds_override",
        "seconds",
    ),
    "reply_mode": ("🎚 Режим публикации", "channel_reply_mode", "reply_mode_override", "mode"),
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
    return label


@router.callback_query(F.data == "channel:menu")
async def channels_legacy_redirect(c):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        accounts = list((await db.scalars(select(Account).order_by(Account.id))).all())
    rows = [[(f"👤 {a.display_name[:28]}", f"channel:account:{a.id}")] for a in accounts]
    rows.append([("⬅️ К аккаунтам", "acc:list")])
    await edit(
        c, "Управление каналами теперь находится в карточке соответствующего аккаунта.", kb(rows)
    )


async def channel_account_screen(c, aid):
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        bindings = list(
            (
                await db.scalars(
                    select(ChannelBinding)
                    .where(ChannelBinding.account_id == aid)
                    .order_by(ChannelBinding.title, ChannelBinding.id)
                )
            ).all()
        )
    if not account:
        await c.answer("Аккаунт не найден.", show_alert=True)
        return
    rows = [
        [
            (
                f"{'🟢' if b.reply_enabled else '🔴'} {b.title[:42]}",
                f"channel:open:{aid}:{b.channel_id}",
            )
        ]
        for b in bindings[:70]
    ]
    rows += [
        [("↻ Загрузить / обновить каналы", f"channel:sync:{aid}")],
        [("⚙️ Глобальные настройки каналов", f"channel:gsettings:{aid}")],
        [("🧾 История анализа", f"channel:history:{aid}")],
        [("⬅️ К аккаунту", f"acc:view:{aid}")],
    ]
    summary = (
        "Каналов пока нет. Нажмите «Загрузить / обновить каналы»."
        if not bindings
        else "🟢 — ответы в комментариях включены · 🔴 — выключены.\nНажмите на канал, чтобы открыть его карточку. Нажатие не меняет его состояние."
    )
    await edit(c, f"📡 Каналы · аккаунт #{aid} · {account.display_name}\n\n{summary}", kb(rows))


@router.callback_query(F.data.startswith("channel:account:"))
async def channel_account(c):
    if not await check_cb(c):
        return
    await channel_account_screen(c, int(c.data.split(":")[2]))


@router.callback_query(F.data.startswith("channel:sync:"))
async def channel_sync(c):
    if not await check_cb(c):
        return
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
        await c.answer(
            f"Ошибка загрузки каналов: {type(exc).__name__}: {str(exc)[:180]}", show_alert=True
        )
        return
    async with SessionFactory() as db:
        existing = list(
            (await db.scalars(select(ChannelBinding).where(ChannelBinding.account_id == aid))).all()
        )
        by_id = {row.channel_id: row for row in existing}
        added = 0
        for channel_id, title in dialogs:
            row = by_id.get(channel_id)
            if row:
                row.title = title
            else:
                db.add(
                    ChannelBinding(
                        account_id=aid,
                        channel_id=channel_id,
                        title=title,
                        enabled=False,
                        reply_enabled=False,
                    )
                )
                added += 1
        await db.commit()
    await channel_account_screen(c, aid)
    await c.message.answer(f"✅ Загружено каналов: {len(dialogs)} · новых добавлено: {added}")


@router.callback_query(F.data.startswith("channel:open:"))
async def channel_open(c, *, answer_callback=True):
    if not await check_cb(c):
        return
    _, _, aid, channel_id = c.data.split(":")
    aid, channel_id = int(aid), int(channel_id)
    async with SessionFactory() as db:
        binding = await db.scalar(
            select(ChannelBinding).where(
                ChannelBinding.account_id == aid, ChannelBinding.channel_id == channel_id
            )
        )
    if not binding:
        await c.answer("Канал не найден. Обновите список каналов.", show_alert=True)
        return
    state = (
        "🟢 Отвечать в комментариях включено"
        if binding.reply_enabled
        else "🔴 Отвечать в комментариях выключено"
    )
    await edit(
        c,
        f"📡 {binding.title}\nАккаунт: #{aid}\n\n{state}\n\nИИ анализирует публикацию и комментарии как контекст аудитории. Вероятность определяет, будет ли подготовлен комментарий к исходному посту; при 100% ИИ готовит его для каждой публикации, если ответ не запрещён ограничениями. На отдельные комментарии пользователей канал-автоматизация не отвечает.",
        kb(
            [
                [
                    (
                        "Выключить ответы" if binding.reply_enabled else "Включить ответы",
                        f"channel:reply:{binding.id}",
                    )
                ],
                [("⚙️ Настроить", f"channel:csettings:{binding.id}")],
                [("🧾 История анализа", f"channel:history:{aid}")],
                [
                    ("⬅️ К каналам аккаунта", f"channel:account:{aid}"),
                    ("🏠 Главное меню", "nav:home"),
                ],
            ]
        ),
        answer_callback=answer_callback,
    )


@router.callback_query(F.data.startswith("channel:reply:"))
async def channel_reply_toggle(c):
    if not await check_cb(c):
        return

    # Stop Telegram's callback spinner immediately; the button state itself is
    # changed only after PostgreSQL confirms the transaction.
    try:
        await c.answer()
    except Exception:
        # An expired callback must not prevent a valid database update.
        pass

    binding_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        binding = await db.get(ChannelBinding, binding_id)
        if not binding:
            if c.message:
                await c.message.answer("⚠️ Канал не найден. Обновите список каналов.")
            return

        new_state = not binding.reply_enabled
        binding.reply_enabled = new_state
        binding.enabled = new_state
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            if c.message:
                await c.message.answer(
                    "❌ Не удалось сохранить настройку. Состояние переключателя не изменено."
                )
            return

    # Render from the committed database state without answering the callback twice.
    await channel_open(c, answer_callback=False)


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
    lines = [
        f"⚙️ {title}",
        (
            "Канал переопределяет общие настройки аккаунта."
            if scope == "channel"
            else "Эти значения наследуют все каналы аккаунта."
        ),
        "",
    ]
    rows = []
    prefix = "g" if scope == "global" else "c"
    mode = (
        effective_channel_reply_mode(account, binding)
        if binding is not None
        else ("automatic" if account.channel_reply_mode == "automatic" else "manual")
    )
    mode_label = "Автоматическая публикация" if mode == "automatic" else "Ручной режим · черновик"
    mode_source = (
        "собственная настройка"
        if binding is not None and binding.reply_mode_override is not None
        else "глобальная настройка"
    )
    lines.append(f"Режим публикации: {mode_label} ({mode_source})")
    rows.append(
        [(f"🔄 Переключить режим публикации · {mode_label}", f"channel:mode:{prefix}:{object_id}")]
    )

    chat_replies_enabled = (
        bool(account.channel_chat_replies_enabled)
        if binding is None
        else effective_channel_chat_replies_enabled(account, binding)
    )
    chat_replies_source = (
        "глобальная настройка аккаунта"
        if binding is None or binding.chat_replies_enabled_override is None
        else "индивидуальная настройка канала"
    )
    chat_replies_icon = "🟢" if chat_replies_enabled else "🔴"
    chat_replies_label = "включены" if chat_replies_enabled else "выключены"
    lines.append(
        f"Ответы людям в комментариях: {chat_replies_icon} {chat_replies_label} ({chat_replies_source})"
    )
    rows.append(
        [
            (
                f"Ответы людям в комментариях {chat_replies_icon}",
                f"channel:chatreplies:info:{prefix}:{object_id}",
            )
        ]
    )

    policy_enabled, policy_action, policy_minimum, policy_wait = resolve_channel_comment_policy(
        account, binding
    )
    policy_icon = "🟢" if policy_enabled else "🔴"
    lines.append(
        f"Условие по комментариям: {policy_icon} {'включено' if policy_enabled else 'выключено'} "
        f"({policy_minimum} комментария · ожидание {format_duration(policy_wait)})"
    )
    rows.append(
        [
            (
                f"💬 Условие по комментариям {policy_icon}",
                f"channel:commentpolicy:open:{prefix}:{object_id}",
            )
        ]
    )
    if binding is not None and binding.chat_replies_enabled_override is not None:
        rows.append(
            [
                (
                    "↩️ Наследовать глобальную настройку ответов коммент",
                    f"channel:chatreplies:inherit:{object_id}",
                )
            ]
        )

    for field in (
        "post_instructions",
        "comments_instructions",
        "restrictions_instructions",
        "reply_probability",
        "reply_cooldown_seconds",
    ):
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
        prefix = "g" if scope == "global" else "c"
        rows.append([(f"✏️ {label}", f"channel:edit:{prefix}:{object_id}:{field}")])
    if scope == "channel":
        learning_state = (
            "идёт"
            if binding.learning_status == "training"
            else "ошибка — запустите повторно"
            if binding.learning_status == "error"
            else "завершено"
            if binding.learning_completed_at
            else "не запускалось"
        )
        lines.append(f"Автообучение: {learning_state}")
        lines.append("Автообучение: до 200 постов и до 100 комментариев к каждому.")
        rows.append([("🧠 Автообучение по последним 200 постам", f"channel:learn:{object_id}")])
        rows.append([("↩️ Сбросить настройки канала к глобальным", f"channel:inherit:{object_id}")])
        rows.append([("⬅️ К карточке канала", f"channel:open:{account.id}:{binding.channel_id}")])
    else:
        rows.append([("⬅️ К каналам аккаунта", f"channel:account:{account.id}")])
    rows.append([("🏠 Главное меню", "nav:home")])
    await edit(c, "\n".join(lines), kb(rows))


@router.callback_query(F.data.startswith("channel:gsettings:"))
async def channel_global_settings(c):
    if not await check_cb(c):
        return
    await channel_settings_screen(c, "global", int(c.data.split(":")[2]))


@router.callback_query(F.data.startswith("channel:csettings:"))
async def channel_custom_settings(c):
    if not await check_cb(c):
        return
    await channel_settings_screen(c, "channel", int(c.data.split(":")[2]))


async def channel_comment_policy_screen(c, scope, object_id):
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            binding = None
        else:
            binding = await db.get(ChannelBinding, object_id)
            account = await db.get(Account, binding.account_id) if binding else None
    if not account or (scope == "c" and not binding):
        await c.answer("Настройка не найдена.", show_alert=True)
        return

    enabled, action, minimum, wait_seconds = resolve_channel_comment_policy(account, binding)
    icon = "🟢" if enabled else "🔴"
    source = (
        "глобальная настройка аккаунта"
        if binding is None or binding.insufficient_action_override is None
        else "индивидуальная настройка канала"
    )
    action_labels = {
        "wait": "Продолжать ждать, пока не наберётся минимум",
        "skip": "После одного интервала пропустить публикацию, если минимума нет",
        "skip_empty": "Пропустить только если комментариев совсем нет; если они есть — продолжать ждать",
    }
    if enabled:
        condition_text = (
            f"Бот ждёт минимум {minimum} комментария в обсуждении новой публикации. "
            "Как только порог достигнут, ИИ анализирует публикацию и комментарии, "
            "определяет, относятся ли они к теме поста, и использует их как контекст "
            "для собственного комментария."
        )
    else:
        condition_text = (
            "Условие выключено: бот не ждёт определённого количества комментариев "
            "и передаёт публикацию на анализ сразу."
        )
    description = (
        f"💬 Условие по комментариям {icon}\n"
        f"Состояние: {'включено' if enabled else 'выключено'} · {source}.\n"
        f"Минимум: {minimum}\n"
        f"Время ожидания: {format_duration(wait_seconds)} ({wait_seconds} сек.)\n"
        f"Действие после ожидания: {action_labels[action]}.\n\n"
        f"{condition_text}\n\n"
        "Вероятность публикации комментария остаётся отдельной настройкой и работает "
        "по прежним правилам. Этот параметр её не изменяет."
    )
    prefix = "g" if scope == "g" else "c"
    rows = [
        [
            (
                "Выключить условие" if enabled else "Включить условие",
                f"channel:commentpolicy:toggle:{prefix}:{object_id}",
            )
        ]
    ]
    rows.append(
        [
            (
                "⚙️ Настроить минимум, ожидание и действие",
                f"channel:commentpolicy:config:{prefix}:{object_id}",
            )
        ]
    )
    back = (
        f"channel:gsettings:{account.id}" if binding is None else f"channel:csettings:{binding.id}"
    )
    rows.append([("⬅️ К настройкам канала", back)])
    await edit(c, description, kb(rows))


async def channel_comment_policy_config_screen(c, scope, object_id):
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            binding = None
        else:
            binding = await db.get(ChannelBinding, object_id)
            account = await db.get(Account, binding.account_id) if binding else None
    if not account or (scope == "c" and not binding):
        await c.answer("Настройка не найдена.", show_alert=True)
        return

    enabled, action, minimum, wait_seconds = resolve_channel_comment_policy(account, binding)
    action_labels = {
        "wait": "Ждать дальше до достижения минимума",
        "skip": "Пропустить после одного интервала, если минимума нет",
        "skip_empty": "Пропустить, только если комментариев нет совсем",
    }
    prefix = "g" if scope == "g" else "c"
    description = (
        "⚙️ Параметры условия по комментариям\n\n"
        f"Условие: {'включено' if enabled else 'выключено'}\n"
        f"Минимум комментариев: {minimum}\n"
        f"Интервал ожидания: {format_duration(wait_seconds)} ({wait_seconds} сек.)\n"
        f"Действие по окончании интервала: {action_labels[action]}\n\n"
        "Если минимум ещё не набран, бот ждёт указанный интервал и проверяет обсуждение повторно. "
        "При достижении минимума запускается анализ. Если минимум не набран, применяется "
        "выбранное действие."
    )
    rows = [
        [
            (
                f"💬 Минимум комментариев · {minimum}",
                f"channel:edit:{prefix}:{object_id}:min_comments",
            )
        ],
        [
            (
                f"⏱ Время ожидания · {format_duration(wait_seconds)}",
                f"channel:edit:{prefix}:{object_id}:wait_seconds",
            )
        ],
        [
            (
                f"⏭ Действие после ожидания · {action_labels[action]}",
                f"channel:commentpolicy:action:{prefix}:{object_id}",
            )
        ],
        [("⬅️ Назад", f"channel:commentpolicy:open:{prefix}:{object_id}")],
    ]
    await edit(c, description, kb(rows))


@router.callback_query(F.data.startswith("channel:commentpolicy:open:"))
async def channel_comment_policy_open(c):
    if not await check_cb(c):
        return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    await channel_comment_policy_screen(c, scope, int(object_raw))


@router.callback_query(F.data.startswith("channel:commentpolicy:config:"))
async def channel_comment_policy_config(c):
    if not await check_cb(c):
        return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    await channel_comment_policy_config_screen(c, scope, int(object_raw))


@router.callback_query(F.data.startswith("channel:commentpolicy:toggle:"))
async def channel_comment_policy_toggle(c):
    if not await check_cb(c):
        return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    object_id = int(object_raw)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            enabled, action = decode_channel_comment_policy_action(
                account.channel_insufficient_action
            )
            account.channel_insufficient_action = encode_channel_comment_policy_action(
                not enabled, action
            )
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await c.answer("Канал не найден.", show_alert=True)
                return
            raw_action = (
                binding.insufficient_action_override
                if binding.insufficient_action_override is not None
                else (await db.get(Account, binding.account_id)).channel_insufficient_action
            )
            enabled, action = decode_channel_comment_policy_action(raw_action)
            binding.insufficient_action_override = encode_channel_comment_policy_action(
                not enabled, action
            )
        await db.commit()
    await channel_comment_policy_screen(c, scope, object_id)


@router.callback_query(F.data.startswith("channel:commentpolicy:action:"))
async def channel_comment_policy_action(c):
    if not await check_cb(c):
        return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    object_id = int(object_raw)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            enabled, action = decode_channel_comment_policy_action(
                account.channel_insufficient_action
            )
            next_action = {"wait": "skip", "skip": "skip_empty", "skip_empty": "wait"}[action]
            account.channel_insufficient_action = encode_channel_comment_policy_action(
                enabled, next_action
            )
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await c.answer("Канал не найден.", show_alert=True)
                return
            account = await db.get(Account, binding.account_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            raw_action = (
                binding.insufficient_action_override
                if binding.insufficient_action_override is not None
                else account.channel_insufficient_action
            )
            enabled, action = decode_channel_comment_policy_action(raw_action)
            next_action = {"wait": "skip", "skip": "skip_empty", "skip_empty": "wait"}[action]
            binding.insufficient_action_override = encode_channel_comment_policy_action(
                enabled, next_action
            )
        await db.commit()
    await channel_comment_policy_config_screen(c, scope, object_id)


@router.callback_query(F.data.startswith("channel:edit:"))
async def channel_setting_edit(c, state: FSMContext):
    if not await check_cb(c):
        return
    _, _, scope, object_raw, field = c.data.split(":")
    if scope not in {"g", "c"} or field not in CHANNEL_SETTING_FIELDS:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    label, _, _, kind = CHANNEL_SETTING_FIELDS[field]
    minimum, maximum = (
        (1, 10)
        if kind == "min_comments"
        else (0, 100)
        if kind == "percent"
        else (0, 2147483647)
        if kind == "seconds"
        else (0, 4000)
    )
    await state.clear()
    await state.update_data(
        channel_scope=scope,
        channel_object_id=int(object_raw),
        channel_field=field,
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
    )
    await state.set_state(Flow.channel_setting_value)
    if kind == "text":
        hint = "Отправьте текст инструкции. Можно несколько абзацев. Чтобы очистить, отправьте точку: ."
    elif kind == "seconds":
        hint = "Введите целое число секунд. Например: 3600 секунд = 01:00:00 (60 минут)."
    elif kind == "percent":
        hint = "Введите процент от 0 до 100."
    elif kind == "min_comments":
        hint = "Введите минимальное количество комментариев от 1 до 10."
    else:
        hint = "Введите число от 1 до 10."
    range_hint = (
        f"Максимум: {maximum} сек. ({format_duration(maximum)})."
        if kind == "seconds"
        else f"Допустимый диапазон: {minimum}–{maximum}."
    )
    meaning = CHANNEL_FIELD_HINTS.get(field)
    prefix = f"{meaning}\n\n" if meaning else ""
    await edit(c, f"{label}\n\n{prefix}{hint}\n{range_hint}", CANCEL)


@router.message(Flow.channel_setting_value)
async def channel_setting_save(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    field, scope, object_id = (
        data.get("channel_field"),
        data.get("channel_scope"),
        int(data.get("channel_object_id", 0)),
    )
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
        minimum, maximum = (
            (1, 10)
            if kind == "min_comments"
            else (0, 100)
            if kind == "percent"
            else (0, 2147483647)
        )
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
    destination = (
        f"channel:commentpolicy:config:{scope}:{object_id}"
        if field in {"wait_seconds", "min_comments"}
        else f"channel:gsettings:{object_id}"
        if scope == "g"
        else f"channel:csettings:{object_id}"
    )
    shown = (
        format_duration(value)
        if kind == "seconds"
        else f"{value}%"
        if kind == "percent"
        else str(value)
    )
    await finish_prompt(
        m,
        state,
        f"✅ {label}: {shown}",
        kb([[("⬅️ К настройкам", destination)], [("🏠 Главное меню", "nav:home")]]),
    )


@router.callback_query(F.data.startswith("channel:mode:"))
async def channel_mode_toggle(c):
    if not await check_cb(c):
        return
    _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестный режим.", show_alert=True)
        return
    object_id = int(object_raw)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            current = "automatic" if account.channel_reply_mode == "automatic" else "manual"
            account.channel_reply_mode = "manual" if current == "automatic" else "automatic"
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await c.answer("Канал не найден.", show_alert=True)
                return
            account = await db.get(Account, binding.account_id)
            current = effective_channel_reply_mode(account, binding)
            binding.reply_mode_override = "manual" if current == "automatic" else "automatic"
        await db.commit()
    await channel_settings_screen(c, "global" if scope == "g" else "channel", object_id)


@router.callback_query(F.data.startswith("channel:chatreplies:inherit:"))
async def channel_chat_replies_inherit(c):
    if not await check_cb(c):
        return
    binding_id = int(c.data.split(":")[3])
    async with SessionFactory() as db:
        binding = await db.get(ChannelBinding, binding_id)
        if not binding:
            await c.answer("Канал не найден.", show_alert=True)
            return
        binding.chat_replies_enabled_override = None
        await db.commit()
    await channel_chat_replies_screen(c, "channel", binding_id)


async def channel_chat_replies_screen(c, scope, object_id):
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            binding = None
        else:
            binding = await db.get(ChannelBinding, object_id)
            account = await db.get(Account, binding.account_id) if binding else None
    if not account or (scope == "c" and not binding):
        await c.answer("Настройка не найдена.", show_alert=True)
        return

    enabled = (
        bool(account.channel_chat_replies_enabled)
        if binding is None
        else effective_channel_chat_replies_enabled(account, binding)
    )
    icon = "🟢" if enabled else "🔴"
    status = "включены" if enabled else "выключены"
    source = (
        "глобальная настройка аккаунта"
        if binding is None or binding.chat_replies_enabled_override is None
        else "индивидуальная настройка канала"
    )
    subject = (
        "в обсуждениях всех каналов аккаунта"
        if binding is None
        else f"в обсуждении канала «{binding.title}»"
    )
    description = (
        f"Ответы людям в комментариях {icon}\n"
        f"Сейчас {status} · {source}.\n\n"
        f"Эта настройка управляет дальнейшим общением {subject}. "
        "Если пользователь отвечает на сообщение, отправленное аккаунтом, "
        "бот может продолжить с ним диалог через систему «Чаты». Это относится "
        "и к комментарию аккаунта под публикацией, и к ответу аккаунта другому участнику. "
        "При отключении входящие ответы на сообщения аккаунта не передаются в «Чаты», "
        "и бот не продолжает такой диалог.\n\n"
        "За ответы и дальнейшее общение в комментариях канала отвечают настройки «Чаты»: "
        "режим ответов, ИИ-агент, персона, правила, примеры и автообучение.\n\n"
        "Эта настройка не отключает анализ публикаций и не влияет на отдельную функцию "
        "публикации одного комментария к исходному посту."
    )
    rows = [
        [
            (
                "Выключить ответы в комментариях" if enabled else "Включить ответы в комментариях",
                f"channel:chatreplies:toggle:{'g' if scope == 'g' else 'c'}:{object_id}",
            )
        ]
    ]
    if binding is not None and binding.chat_replies_enabled_override is not None:
        rows.append(
            [
                (
                    "↩️ Наследовать глобальную настройку",
                    f"channel:chatreplies:inherit:{object_id}",
                )
            ]
        )
    back = (
        f"channel:gsettings:{account.id}" if binding is None else f"channel:csettings:{binding.id}"
    )
    rows.append([("⬅️ К настройкам", back)])
    await edit(c, description, kb(rows))


@router.callback_query(F.data.startswith("channel:chatreplies:info:"))
async def channel_chat_replies_info(c):
    if not await check_cb(c):
        return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    await channel_chat_replies_screen(c, scope, int(object_raw))


@router.callback_query(F.data.startswith("channel:chatreplies:toggle:"))
async def channel_chat_replies_toggle(c):
    if not await check_cb(c):
        return
    _, _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    object_id = int(object_raw)
    async with SessionFactory() as db:
        if scope == "g":
            account = await db.get(Account, object_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            account.channel_chat_replies_enabled = not bool(account.channel_chat_replies_enabled)
        else:
            binding = await db.get(ChannelBinding, object_id)
            if not binding:
                await c.answer("Канал не найден.", show_alert=True)
                return
            account = await db.get(Account, binding.account_id)
            if not account:
                await c.answer("Аккаунт не найден.", show_alert=True)
                return
            binding.chat_replies_enabled_override = not effective_channel_chat_replies_enabled(
                account, binding
            )
        await db.commit()
    await channel_chat_replies_screen(c, scope, object_id)


@router.callback_query(F.data.startswith("channel:action:"))
async def channel_legacy_action(c):
    if not await check_cb(c):
        return
    _, _, scope, object_raw = c.data.split(":")
    if scope not in {"g", "c"}:
        await c.answer("Неизвестная настройка.", show_alert=True)
        return
    object_id = int(object_raw)
    await channel_comment_policy_screen(c, scope, object_id)


@router.callback_query(F.data.startswith("channel:inherit:"))
async def channel_inherit(c):
    if not await check_cb(c):
        return
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
        binding.reply_mode_override = None
        binding.chat_replies_enabled_override = None
        await db.commit()
    await channel_settings_screen(c, "channel", binding_id)


@router.callback_query(F.data.startswith("channel:learn:"))
async def channel_learn(c):
    if not await check_cb(c):
        return
    try:
        await c.answer()
    except Exception:
        pass
    binding_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        binding = await db.get(ChannelBinding, binding_id)
        account = await db.get(Account, binding.account_id) if binding else None
        if not binding or not account:
            if c.message:
                await c.message.answer("⚠️ Канал или аккаунт не найден.")
            return
        if binding.learning_status == "training":
            if c.message:
                await c.message.answer("Автообучение этого канала уже выполняется.")
            return
        binding.learning_status = "training"
        binding.learning_started_at = datetime.now(timezone.utc)
        await db.commit()
        channel_title = binding.title
    if c.message:
        await c.message.answer(
            f"🧠 Автообучение канала «{channel_title}» поставлено в работу. "
            "Задача сохранена и автоматически продолжится после перезапуска Worker."
        )


@router.callback_query(F.data.startswith("channel:history:"))
async def channel_history(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        rows = list(
            (
                await db.scalars(
                    select(ChannelAnalysis)
                    .where(ChannelAnalysis.account_id == aid)
                    .order_by(ChannelAnalysis.created_at.desc())
                    .limit(10)
                )
            ).all()
        )
    if not rows:
        await edit(
            c,
            f"🧾 История анализа каналов аккаунта #{aid} пока пуста.",
            kb(
                [
                    [("⬅️ К каналам аккаунта", f"channel:account:{aid}")],
                    [("🏠 Главное меню", "nav:home")],
                ]
            ),
        )
        return
    buttons = [
        [
            (
                f"{'🟢' if row.on_topic else '🟠'} {row.channel_title[:25]} · пост #{row.post_id}",
                f"channel:analysis:{row.id}",
            )
        ]
        for row in rows
    ]
    buttons += [
        [("⬅️ К каналам аккаунта", f"channel:account:{aid}"), ("🏠 Главное меню", "nav:home")]
    ]
    await edit(c, f"🧾 Последние анализы аккаунта #{aid}.", kb(buttons))


@router.callback_query(F.data.startswith("channel:analysis:"))
async def channel_analysis_view(c):
    if not await check_cb(c):
        return
    analysis_id = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        row = await db.get(ChannelAnalysis, analysis_id)
    if not row:
        await c.answer("Результат анализа не найден.", show_alert=True)
        return
    topic = "по теме" if row.on_topic else "не по теме"
    publication_labels = {
        "published": "Комментарий опубликован под исходным постом",
        "manual": "Подготовлен черновик · ручной режим",
        "skipped_ai": "ИИ решил, что комментарий не нужен",
        "skipped_probability": "Не выбран комментарий по вероятности",
        "skipped_cooldown": "Пропущено из-за паузы между ответами",
        "legacy": "Старый анализ · публикация не выполнялась",
        "pending": "Ожидает обработки",
        "approved": "Ожидает публикации",
        "publishing": "Публикуется",
    }
    if row.publication_status == "legacy":
        decision = "не определялось в старом анализе"
    elif row.publication_status == "skipped_probability":
        decision = "ответ не выбран по вероятности"
    else:
        decision = "ответ нужен" if row.should_reply else "ответ не требуется"
    text = (
        f"📡 {row.channel_title} · пост #{row.post_id}\n"
        f"Комментариев: {row.comments_count} · обсуждение: {topic}\n"
        f"Решение ИИ: {decision}\n"
        f"Статус: {publication_labels.get(row.publication_status, row.publication_status)}\n\n"
        f"Сводка ИИ:\n{row.comments_summary[:1800]}\n\n"
        f"Вариант комментария к публикации:\n{row.suggested_response[:1800] or 'ИИ не предложил публиковать комментарий.'}"
        + (f"\n\nОшибка публикации: {row.publication_error[:700]}" if row.publication_error else "")
    )
    await edit(
        c,
        text,
        kb(
            [
                [("⬅️ История анализа", f"channel:history:{row.account_id}")],
                [("🏠 Главное меню", "nav:home")],
            ]
        ),
    )
