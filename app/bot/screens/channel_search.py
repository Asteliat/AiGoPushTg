"""Channel discovery and joining."""

from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import CHANNEL_CANDIDATES
from app.bot.core import CHANNEL_PENDING_JOIN
from app.bot.core import Flow
from app.bot.candidates import candidate_at
from app.bot.discovery_flow import collect_candidates
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import delete_msg
from app.bot.core import edit
from app.bot.core import edit_anchor
from app.bot.core import finish_prompt
from app.bot.core import kb
from app.bot.core import perform_join_request
from app.bot.core import private
from app.bot.core import prompt_message
from app.bot.core import router
from app.bot.targets import channel_entity
from app.bot.targets import isolated_account_client
from app.bot.targets import parse_chat_link
from app.storage.db import SessionFactory
from app.storage.models import Account
from app.storage.models import ChannelBinding
from app.telegram_gateway import management as telegram_management
from app.telegram_gateway.discovery import search_public_channels
from app.telegram_gateway.discovery import search_web_channels
from app.telegram_gateway.discovery import verify_channel_candidates
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message
from sqlalchemy import select
from telethon import utils
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import ChatInviteAlready
import asyncio
import logging


async def _channel_candidates_isolated(aid: int, candidates: list[dict], concurrency: int = 4):
    async with isolated_account_client(aid) as (_account, client):
        return await verify_channel_candidates(client, candidates, concurrency=concurrency)


def _channel_candidate_keyboard(aid: int, candidates: list[dict], offset: int = 0):
    rows = []
    for index, item in enumerate(candidates[offset : offset + 12], start=offset):
        title = str(item.get("title") or "Канал")[:30]
        subscribers = int(item.get("members") or 0)
        suffix = f" · {subscribers:,}".replace(",", " ") if subscribers else ""
        rows.append([(f"📣 {title}{suffix}"[:60], f"channel:candidate:{aid}:{index}")])
    navigation = []
    if offset > 0:
        navigation.append(("⬅️ Назад", f"channel:page:{aid}:{max(0, offset - 12)}"))
    if offset + 12 < len(candidates):
        navigation.append(("➡️ Ещё результаты", f"channel:page:{aid}:{offset + 12}"))
    if navigation:
        rows.append(navigation)
    if any(item.get("verified") and item.get("link") for item in candidates):
        rows.append([("🔗 Показать ссылками", f"channel:links:{aid}")])
    rows.append([("⬅️ Инструменты", "tool:menu"), ("🏠 Главное меню", "nav:home")])
    return kb(rows)


async def _channel_search_prompt(c: CallbackQuery, state: FSMContext, account_ids: list[int]):
    if not account_ids:
        await c.answer("Нет включённых аккаунтов.", show_alert=True)
        return
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback="tool:menu",
        channel_search_account_id=account_ids[0],
        channel_search_account_ids=account_ids,
    )
    await state.set_state(Flow.channel_search_query)
    label = "всеми аккаунтами" if len(account_ids) > 1 else f"аккаунтом #{account_ids[0]}"
    await edit(
        c,
        f"📣 Поиск Telegram-каналов · {label}\n\n"
        "Введите тему или несколько ключевых слов. Например: «технологии», "
        "«новости IT», «автомобили».\n\n"
        "Поиск соберёт кандидатов из Telegram и нескольких поисковых систем, "
        "затем проверит каждую ссылку через Telegram API. В результатах останутся "
        "только каналы вещания; группы, чаты, профили, боты и нерабочие ссылки исключаются.",
        CANCEL,
    )


@router.callback_query(F.data == "tool:channels")
async def tool_channels_accounts(c: CallbackQuery):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        accounts = list(
            (
                await db.scalars(
                    select(Account).where(Account.enabled.is_(True)).order_by(Account.id)
                )
            ).all()
        )
    if not accounts:
        await edit(
            c,
            "📣 Поиск каналов\n\nНет включённых аккаунтов. Сначала подключите и включите Telegram-аккаунт.",
            kb(
                [
                    [("👤 Аккаунты", "acc:list")],
                    [("⬅️ Инструменты", "tool:menu")],
                ]
            ),
        )
        return
    rows = [[(f"🌐 Все аккаунты · {len(accounts)}", "tool:channels:all")]]
    rows += [
        [(f"👤 {account.display_name[:32]} · #{account.id}", f"tool:channels:account:{account.id}")]
        for account in accounts
    ]
    rows.append([("⬅️ Инструменты", "tool:menu")])
    await edit(
        c,
        "📣 Поиск Telegram-каналов\n\n"
        "Можно искать всеми включёнными аккаунтами сразу или выбрать один. "
        "В результатах будут только публичные и доступные по приглашению каналы вещания — "
        "группы и супергруппы будут исключены.",
        kb(rows),
    )


@router.callback_query(F.data == "tool:channels:all")
async def tool_channels_all(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    async with SessionFactory() as db:
        ids = list(
            (
                await db.scalars(
                    select(Account.id).where(Account.enabled.is_(True)).order_by(Account.id)
                )
            ).all()
        )
    await _channel_search_prompt(c, state, [int(value) for value in ids])


@router.callback_query(F.data.startswith("tool:channels:account:"))
async def tool_channels_account(c: CallbackQuery, state: FSMContext):
    if not await check_cb(c):
        return
    aid = int(c.data.rsplit(":", 1)[1])
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account or not account.enabled:
        await c.answer("Аккаунт не найден или выключен.", show_alert=True)
        return
    await _channel_search_prompt(c, state, [aid])


@router.message(Flow.channel_search_query)
async def channel_search_query(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    query = " ".join((m.text or "").split())
    await delete_msg(m)
    if len(query) < 2:
        await prompt_message(m, state, "Введите хотя бы два символа для поиска.")
        return
    query = query[:100]
    account_ids = list(
        dict.fromkeys(
            int(value)
            for value in (data.get("channel_search_account_ids") or [])
            if str(value).isdigit()
        )
    )
    aid = int(data.get("channel_search_account_id", account_ids[0] if account_ids else 0))
    if aid not in account_ids:
        account_ids.insert(0, aid)
    if not account_ids:
        await finish_prompt(
            m,
            state,
            "Не выбраны включённые аккаунты.",
            kb(
                [
                    [("📣 Поиск каналов", "tool:channels")],
                    [("⬅️ Инструменты", "tool:menu")],
                ]
            ),
        )
        return
    anchor = {"ui_chat_id": data.get("ui_chat_id"), "ui_message_id": data.get("ui_message_id")}
    await state.clear()
    await edit_anchor(
        m.bot,
        anchor,
        f"📣 Ищу каналы по запросу «{query}»\n\n"
        f"Telegram-поиск: {len(account_ids)} аккаунт(ов)\n"
        "1/3 · Ищу в Telegram и нескольких поисковых системах…\n"
        "2/3 · Объединяю результаты и проверяю тип ссылок через Telegram API.",
        kb([[("⬅️ Инструменты", "tool:menu")]]),
    )

    try:

        raw_candidates = await collect_candidates(
            account_ids,
            query,
            native_search=search_public_channels,
            web_search=search_web_channels,
            log_label="Channel",
        )
        await edit_anchor(
            m.bot,
            anchor,
            f"📣 Проверяю найденные ссылки · «{query}»\n\n"
            f"Аккаунтов в поиске: {len(account_ids)}\n"
            f"Кандидатов собрано: {len(raw_candidates)}\n"
            "Проверяю тип каждого канала непосредственно в Telegram. Группы и нерабочие ссылки отбрасываются.",
            kb([[("⬅️ Инструменты", "tool:menu")]]),
        )
        candidates = await _channel_candidates_isolated(aid, raw_candidates, concurrency=4)
    except Exception as exc:
        retry = "tool:channels:all" if len(account_ids) > 1 else f"tool:channels:account:{aid}"
        await edit_anchor(
            m.bot,
            anchor,
            f"❌ Не удалось выполнить поиск или проверить результаты.\n\n"
            f"{type(exc).__name__}: {str(exc)[:220]}\n\n"
            "Проверьте подключение аккаунта и повторите поиск.",
            kb([[("📣 Повторить поиск", retry)], [("⬅️ Инструменты", "tool:menu")]]),
        )
        return

    CHANNEL_CANDIDATES[(m.from_user.id, aid)] = candidates
    retry = "tool:channels:all" if len(account_ids) > 1 else f"tool:channels:account:{aid}"
    if not candidates:
        await edit_anchor(
            m.bot,
            anchor,
            f"По запросу «{query}» не удалось найти каналы, которые Telegram подтвердил как каналы вещания.\n\n"
            "Это не означает, что таких каналов нет: поисковики могут ограничивать выдачу, "
            "а Telegram не предоставляет полный каталог всех каналов. Попробуйте синонимы "
            "или более короткий запрос.",
            kb([[("📣 Новый поиск", retry)], [("⬅️ Инструменты", "tool:menu")]]),
        )
        return

    lines = [
        f"📣 Найдено каналов: {len(candidates)} · запрос «{query}»",
        f"Поиск выполнен через {len(account_ids)} аккаунт(ов) Telegram",
        "✓ Каждая ссылка проверена через Telegram API",
        "Группы, супергруппы, боты и профили исключены.",
        "",
        f"Показываю 1–{min(12, len(candidates))}. Нажмите на канал, чтобы открыть проверку.",
    ]
    for index, item in enumerate(candidates[:12], 1):
        subscribers = (
            f"{int(item.get('members') or 0):,}".replace(",", " ") + " подписчиков"
            if item.get("members")
            else "подписчики: неизвестно"
        )
        visibility = (
            "приватный канал"
            if item.get("kind") == "channel" and not item.get("username")
            else "канал"
        )
        lines.append(
            f"{index}. {str(item.get('title') or 'Канал')[:75]} · {subscribers} · {visibility}"
        )
    await edit_anchor(
        m.bot, anchor, "\n".join(lines), _channel_candidate_keyboard(aid, candidates, 0)
    )


@router.callback_query(F.data.startswith("channel:links:"))
async def channel_candidate_links(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    candidates = CHANNEL_CANDIDATES.get((c.from_user.id, aid), [])
    links = list(
        dict.fromkeys(
            str(item.get("link") or "").strip()
            for item in candidates
            if item.get("verified") and item.get("link")
        )
    )
    if not links:
        await c.answer("Нет проверенных ссылок. Запустите поиск заново.", show_alert=True)
        return
    await c.answer("Отправляю список ссылок.")
    header = f"🔗 Проверенные ссылки на каналы · всего {len(links)}\n\n"
    chunks, current, current_len = [], [], len(header)
    for link in links:
        if current and current_len + len(link) + 1 > 3700:
            chunks.append(header + "\n".join(current))
            current, current_len = [], 0
        current.append(link)
        current_len += len(link) + 1
    if current:
        chunks.append((header if not chunks else "") + "\n".join(current))
    for index, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            chunk = f"📄 Часть {index}/{len(chunks)}\n" + chunk
        await c.message.answer(chunk, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("channel:page:"))
async def channel_candidate_page(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, aid_raw, offset_raw = c.data.split(":")
    aid, offset = int(aid_raw), max(0, int(offset_raw))
    candidates = CHANNEL_CANDIDATES.get((c.from_user.id, aid), [])
    if not candidates:
        await edit(
            c,
            "Результаты больше не сохранены. Запустите поиск ещё раз.",
            kb(
                [
                    [("📣 Поиск каналов", f"tool:channels:account:{aid}")],
                    [("⬅️ Инструменты", "tool:menu")],
                ]
            ),
        )
        return
    page = candidates[offset : offset + 12]
    lines = [
        f"Найденные каналы · аккаунт #{aid}",
        f"Показаны {offset + 1}–{min(offset + 12, len(candidates))} из {len(candidates)}",
        "",
        "Выберите канал для проверки.",
    ]
    for index, item in enumerate(page, start=offset + 1):
        lines.append(f"{index}. {str(item.get('title') or item.get('link') or 'Канал')[:70]}")
    await edit(c, "\n".join(lines), _channel_candidate_keyboard(aid, candidates, offset))


@router.callback_query(F.data.startswith("channel:results:"))
async def channel_results_back(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    candidates = CHANNEL_CANDIDATES.get((c.from_user.id, aid), [])
    if not candidates:
        await edit(
            c,
            "Результаты больше не сохранены. Запустите поиск ещё раз.",
            kb(
                [
                    [("📣 Поиск каналов", f"tool:channels:account:{aid}")],
                    [("⬅️ Инструменты", "tool:menu")],
                ]
            ),
        )
        return
    lines = [f"Найденные каналы · аккаунт #{aid}", "", "Выберите канал для проверки."]
    for index, item in enumerate(candidates[:12], 1):
        lines.append(f"{index}. {str(item.get('title') or item.get('link') or 'Канал')[:70]}")
    await edit(c, "\n".join(lines), _channel_candidate_keyboard(aid, candidates, 0))


@router.callback_query(F.data.startswith("channel:candidate:"))
async def channel_candidate_preview(c: CallbackQuery):
    if not await check_cb(c):
        return
    _, _, aid_raw, index_raw = c.data.split(":")
    aid, index = int(aid_raw), int(index_raw)
    candidates = CHANNEL_CANDIDATES.get((c.from_user.id, aid), [])
    item = candidate_at(candidates, index)
    if item is None:
        await c.answer("Результаты устарели")
        await edit(
            c,
            "Результаты поиска больше не сохранены (бот перезапускался).\n"
            "Запустите поиск ещё раз — запрос вводить заново не нужно, кнопка ниже.",
            kb(
                [
                    [("🔎 Новый поиск", f"tool:channels:account:{aid}")],
                    [("⬅️ Инструменты", "tool:menu")],
                ]
            ),
        )
        return
    link = item.get("link")
    if not link:
        await c.answer("У этого канала нет ссылки.", show_alert=True)
        return
    try:
        kind, target = parse_chat_link(link)
        async with isolated_account_client(aid) as (_account, client):
            already = False
            if kind == "public":
                entity = await client.get_entity(target)
                if not channel_entity(entity):
                    raise ValueError("Это группа или профиль, а не канал. Проверка отменена.")
                title = getattr(entity, "title", None) or target
                subscribers = int(getattr(entity, "participants_count", 0) or 0)
                entity_id = utils.get_peer_id(entity)
            else:
                invite = await client(CheckChatInviteRequest(hash=target))
                if isinstance(invite, ChatInviteAlready):
                    entity = invite.chat
                    if not channel_entity(entity):
                        raise ValueError("Ссылка ведёт не на канал. Проверка отменена.")
                    title = getattr(entity, "title", None) or "Канал"
                    subscribers = int(getattr(entity, "participants_count", 0) or 0)
                    entity_id = utils.get_peer_id(entity)
                    already = True
                else:
                    if not getattr(invite, "channel", False) or getattr(invite, "megagroup", False):
                        raise ValueError("Ссылка ведёт на группу, а не на канал.")
                    title = getattr(invite, "title", None) or "Приватный канал"
                    subscribers = int(getattr(invite, "participants_count", 0) or 0)
                    entity_id = None
    except Exception as exc:
        await edit(
            c,
            f"Не удалось проверить ссылку:\n{str(exc)[:220]}",
            kb(
                [
                    [("⬅️ К результатам", f"channel:results:{aid}")],
                    [("⬅️ Инструменты", "tool:menu")],
                ]
            ),
        )
        return
    CHANNEL_PENDING_JOIN[(c.from_user.id, aid)] = {
        "link": link,
        "kind": kind,
        "target": target,
        "title": title,
        "members": subscribers,
        "entity_id": entity_id,
        "already": already,
    }
    status = (
        "Аккаунт уже состоит в канале."
        if already
        else "Подписка произойдёт только после отдельного подтверждения."
    )
    await edit(
        c,
        f"📣 {title}\nПодписчиков: {subscribers or 'неизвестно'}\nСсылка: {link}\n\n"
        f"{status}\n\nПроверьте название и ссылку перед подпиской.",
        kb(
            [
                [("✅ Подтвердить подписку", f"channel:joinconfirm:{aid}")],
                [("⬅️ К результатам", f"channel:results:{aid}")],
                [("⬅️ Инструменты", "tool:menu")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("channel:joinconfirm:"))
async def channel_join_confirm(c: CallbackQuery):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    pending = CHANNEL_PENDING_JOIN.get((c.from_user.id, aid))
    if not pending:
        await c.answer("Предпросмотр устарел. Проверьте канал ещё раз.", show_alert=True)
        return
    try:
        async with isolated_account_client(aid) as (_account, client):
            me = await client.get_me()
            expected_user_id = getattr(_account, "telegram_user_id", None)
            if me is None or not getattr(me, "id", None):
                raise RuntimeError("Telegram не вернул авторизованный аккаунт.")
            if expected_user_id and int(me.id) != int(expected_user_id):
                raise RuntimeError(
                    f"Сессия аккаунта #{aid} не совпадает с записью: "
                    f"ожидался Telegram ID {expected_user_id}, получен {me.id}."
                )
            logging.info(
                "Channel join started: account_id=%s telegram_user_id=%s username=%s kind=%s already=%s",
                aid,
                int(me.id),
                getattr(me, "username", None),
                pending.get("kind"),
                pending.get("already", False),
            )
            entity = None
            if pending.get("already") and pending.get("entity_id"):
                entity = await client.get_entity(pending["entity_id"])
            elif pending["kind"] == "public":
                entity = await client.get_entity(pending["target"])
                if not channel_entity(entity):
                    raise ValueError("Ссылка больше не ведёт на канал.")

                async def report_floodwait(seconds):
                    await edit(
                        c,
                        f"⏳ Telegram временно ограничил вступления. Жду {seconds} сек., затем повторю подписку на канал «{pending['title']}»…",
                        kb([[("⬅️ К результатам", f"channel:results:{aid}")]]),
                    )

                await perform_join_request(
                    aid, client, JoinChannelRequest(entity), report_floodwait
                )
            else:

                async def report_floodwait(seconds):
                    await edit(
                        c,
                        f"⏳ Telegram временно ограничил вступления. Жду {seconds} сек., затем повторю подписку на канал «{pending['title']}»…",
                        kb([[("⬅️ К результатам", f"channel:results:{aid}")]]),
                    )

                joined = await perform_join_request(
                    aid, client, ImportChatInviteRequest(hash=pending["target"]), report_floodwait
                )
                entity = next((chat for chat in joined.chats if channel_entity(chat)), None)
                if entity is None:
                    raise RuntimeError("Telegram не вернул вступивший канал.")
            if entity is None:
                entity = await client.get_entity(pending["target"])
            if not channel_entity(entity):
                raise ValueError("Ссылка не ведёт на канал.")
            is_member = False
            for attempt in range(3):
                is_member = await telegram_management.verify_channel_membership(client, entity)
                if is_member:
                    break
                if attempt < 2:
                    await asyncio.sleep(1)
            logging.info(
                "Channel membership verification: account_id=%s telegram_user_id=%s channel_id=%s entity_type=%s is_member=%s",
                aid,
                int(me.id),
                int(utils.get_peer_id(entity)),
                type(entity).__name__,
                is_member,
            )
            if not is_member:
                raise RuntimeError(
                    "Telegram выполнил запрос, но не подтвердил подписку на канал. "
                    "Возможно, канал требует одобрения администратора."
                )
            channel_id = int(utils.get_peer_id(entity))
            title = (getattr(entity, "title", None) or pending["title"] or "Канал")[:255]
    except Exception as exc:
        logging.exception(
            "Channel join failed: account_id=%s kind=%s error_type=%s",
            aid,
            pending.get("kind"),
            type(exc).__name__,
        )
        if isinstance(exc, FloodWaitError):
            await edit(
                c,
                f"⛔ Telegram ограничил вступления. Повторная попытка остановлена. Подождите {int(exc.seconds)} сек. и попробуйте снова.",
                kb(
                    [
                        [("📣 Другой канал", f"tool:channels:account:{aid}")],
                        [("⬅️ К результатам", f"channel:results:{aid}")],
                    ]
                ),
            )
            return
        if "InviteRequestSentError" in type(exc).__name__:
            await edit(
                c,
                "Запрос на подписку отправлен администраторам канала. Канал появится в списке после одобрения.",
                kb(
                    [
                        [("📣 Проверить другой канал", f"tool:channels:account:{aid}")],
                        [("⬅️ Инструменты", "tool:menu")],
                    ]
                ),
            )
            return
        if "UserAlreadyParticipantError" in type(exc).__name__:
            try:
                async with isolated_account_client(aid) as (_account, client):
                    if pending["kind"] == "public":
                        entity = await client.get_entity(pending["target"])
                    else:
                        invite = await client(CheckChatInviteRequest(hash=pending["target"]))
                        entity = invite.chat if isinstance(invite, ChatInviteAlready) else None
                    if entity is None or not channel_entity(entity):
                        raise RuntimeError(
                            "Не удалось определить канал, на который уже подписан аккаунт."
                        )
                    channel_id = int(utils.get_peer_id(entity))
                    title = (getattr(entity, "title", None) or pending["title"] or "Канал")[:255]
            except Exception as nested:
                await edit(
                    c,
                    f"Аккаунт уже подписан, но не удалось обновить список каналов: {type(nested).__name__}.",
                    kb(
                        [
                            [("⬅️ К результатам", f"channel:results:{aid}")],
                            [("⬅️ Инструменты", "tool:menu")],
                        ]
                    ),
                )
                return
        else:
            await edit(
                c,
                f"Не удалось подписаться: {type(exc).__name__}: {str(exc)[:180]}. Проверьте ссылку и доступ аккаунта к каналу.",
                kb(
                    [
                        [("📣 Другой канал", f"tool:channels:account:{aid}")],
                        [("⬅️ К результатам", f"channel:results:{aid}")],
                    ]
                ),
            )
            return

    async with SessionFactory() as db:
        binding = await db.scalar(
            select(ChannelBinding).where(
                ChannelBinding.account_id == aid, ChannelBinding.channel_id == channel_id
            )
        )
        if binding:
            binding.title = title
        else:
            db.add(
                ChannelBinding(account_id=aid, channel_id=channel_id, title=title, enabled=False)
            )
        await db.commit()
    CHANNEL_PENDING_JOIN.pop((c.from_user.id, aid), None)
    await edit(
        c,
        f"✅ Канал «{title}» добавлен в список каналов аккаунта #{aid}.\n\n"
        "Подписка оформлена. Обработка канала пока выключена — при необходимости включите её в разделе «Каналы» аккаунта.",
        kb(
            [
                [("📡 Открыть каналы аккаунта", f"channel:account:{aid}")],
                [("👤 К аккаунту", f"acc:view:{aid}")],
                [("🏠 Главное меню", "nav:home")],
            ]
        ),
    )
