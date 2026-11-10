"""Chat discovery, joining and per-account chat toggles."""

from __future__ import annotations

from app.bot.core import CANCEL
from app.bot.core import CHANNEL_PENDING_JOIN
from app.bot.core import CHAT_CANDIDATES
from app.bot.core import CHAT_PENDING_JOIN
from app.bot.core import Flow
from app.bot.core import cfg
from app.bot.candidates import candidate_at
from app.bot.core import check_cb
from app.bot.core import check_msg
from app.bot.core import delete_msg
from app.bot.core import edit
from app.bot.core import edit_anchor
from app.bot.core import isolated_telegram_client
from app.bot.core import kb
from app.bot.core import perform_join_request
from app.bot.core import private
from app.bot.core import prompt_message
from app.bot.core import router
from app.bot.navigation import cancel_return_for
from app.bot.targets import channel_entity
from app.bot.targets import group_entity
from app.bot.targets import isolated_account_client
from app.bot.targets import parse_chat_link
from app.services.chat_bindings import discovered_binding_values
from app.storage.db import SessionFactory
from app.storage.models import Account
from app.storage.models import ChannelBinding
from app.storage.models import ChatBinding
from app.telegram_gateway import management as telegram_management
from app.telegram_gateway.discovery import find_invite_links_in_dialogs
from app.telegram_gateway.discovery import search_public_groups
from app.telegram_gateway.discovery import search_web_groups
from app.telegram_gateway.discovery import verify_group_candidates
from aiogram import F
from aiogram.fsm.context import FSMContext
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


def _candidate_keyboard(aid: int, candidates: list[dict], offset: int = 0):
    rows = []
    page = candidates[offset : offset + 12]
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
    if any(item.get("verified") and item.get("link") for item in candidates):
        rows.append([("🔗 Показать ссылками", f"chat:links:{aid}")])
    rows += [[("⬅️ Инструменты", "tool:menu"), ("🏠 Главное меню", "nav:home")]]
    return kb(rows)


async def _verify_group_candidates_isolated(aid: int, candidates: list[dict], concurrency: int = 4):
    """Verify search results on a snapshot, never opening the worker's live session."""
    async with isolated_account_client(aid) as (_account, client):
        return await verify_group_candidates(client, candidates, concurrency=concurrency)


@router.callback_query(F.data.startswith("chat:search:"))
async def chat_search_start(c, state: FSMContext):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback="tool:menu",
        chat_search_account_id=aid,
    )
    await state.set_state(Flow.chat_search_query)
    await edit(
        c,
        f"🔎 Поиск Telegram-групп · аккаунт #{aid}\n\n"
        "Введите тему или несколько ключевых слов, например: «Python разработка», "
        "«настольные игры», «дизайн».\n\n"
        "Поиск собирает кандидатов из Telegram и нескольких поисковых систем, затем "
        "проверяет каждую ссылку через Telegram. В выдаче остаются только группы и супергруппы; "
        "каналы, профили, боты и нерабочие ссылки исключаются.",
        CANCEL,
    )


@router.message(Flow.chat_search_query)
async def chat_search_query(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    query = " ".join((m.text or "").split())
    await delete_msg(m)
    if len(query) < 2:
        await prompt_message(m, state, "Введите хотя бы два символа для поиска.")
        return
    query = query[:100]
    aid = int(data.get("chat_search_account_id", 0))
    account_ids = list(
        dict.fromkeys(
            int(value)
            for value in (data.get("chat_search_account_ids") or [aid])
            if str(value).isdigit()
        )
    )
    if aid not in account_ids:
        account_ids.insert(0, aid)
    anchor = {"ui_chat_id": data.get("ui_chat_id"), "ui_message_id": data.get("ui_message_id")}
    await state.clear()
    await edit_anchor(
        m.bot,
        anchor,
        f"🔎 Ищу группы по запросу «{query}»\n\n"
        f"Telegram-поиск: {len(account_ids)} аккаунт(ов)\n"
        "1/3 · Ищу через подключённые аккаунты и несколько поисковиков…\n"
        "2/3 · Объединяю результаты и проверяю ссылки через Telegram API.\n"
        "Если поисковик ограничивает запросы, остальные продолжают работу.",
        kb([[("⬅️ Инструменты", "tool:menu")]]),
    )

    try:

        async def search_one_account(search_aid: int):
            try:
                async with SessionFactory() as db:
                    search_account = await db.get(Account, search_aid)
                if not search_account or not search_account.enabled:
                    return []
                async with isolated_telegram_client(search_account) as search_client:
                    if not await search_client.is_user_authorized():
                        return []
                    return await search_public_groups(search_client, query, limit=100)
            except Exception as exc:
                logging.warning("Group discovery failed for account %s: %s", search_aid, exc)
                return []

        async def search_accounts():
            semaphore = asyncio.Semaphore(3)

            async def limited(search_aid):
                async with semaphore:
                    return await search_one_account(search_aid)

            results = await asyncio.gather(
                *(limited(search_aid) for search_aid in account_ids),
                return_exceptions=True,
            )
            return [item for batch in results if isinstance(batch, list) for item in batch]

        native_task = asyncio.create_task(search_accounts())
        web_task = asyncio.create_task(search_web_groups(query, limit=600, pages_per_query=5))
        native_result, web_result = await asyncio.gather(
            native_task, web_task, return_exceptions=True
        )
        native_results = native_result if isinstance(native_result, list) else []
        web_results = web_result if isinstance(web_result, list) else []
        combined = {}
        for item in native_results + web_results:
            link = str(item.get("link") or "").strip()
            if not link:
                continue
            key = link.casefold()
            if key in combined:
                current = combined[key]
                current["score"] = max(int(current.get("score") or 0), int(item.get("score") or 0))
                current["source"] = ", ".join(
                    dict.fromkeys(
                        (current.get("source", "") + ", " + item.get("source", ""))
                        .strip(", ")
                        .split(", ")
                    )
                )
            else:
                combined[key] = item

        raw_candidates = list(combined.values())[:600]
        await edit_anchor(
            m.bot,
            anchor,
            f"🔎 Проверяю найденные ссылки · «{query}»\n\n"
            f"Аккаунтов в поиске: {len(account_ids)}\n"
            f"Кандидатов собрано: {len(raw_candidates)}\n"
            "Проверяю тип каждого чата непосредственно в Telegram. "
            "Каналы и нерабочие ссылки отбрасываются.",
            kb([[("⬅️ Инструменты", "tool:menu")]]),
        )
        candidates = await _verify_group_candidates_isolated(aid, raw_candidates, concurrency=4)
    except Exception as exc:
        await edit_anchor(
            m.bot,
            anchor,
            f"❌ Не удалось выполнить поиск или проверить результаты.\n\n"
            f"{type(exc).__name__}: {str(exc)[:220]}\n\n"
            "Проверьте подключение аккаунта и повторите поиск.",
            kb(
                [
                    [
                        (
                            "🔎 Повторить всеми аккаунтами"
                            if len(account_ids) > 1
                            else "🔎 Повторить поиск",
                            "tool:groups:all" if len(account_ids) > 1 else f"chat:search:{aid}",
                        )
                    ],
                    [("⬅️ Инструменты", "tool:menu")],
                ]
            ),
        )
        return
    CHAT_CANDIDATES[(m.from_user.id, aid)] = candidates
    if not candidates:
        await edit_anchor(
            m.bot,
            anchor,
            f"По запросу «{query}» не удалось найти группы, которые Telegram подтвердил как группы.\n\n"
            "Это не означает, что таких групп нет: поисковики могут ограничивать выдачу, "
            "а Telegram не предоставляет полный каталог всех публичных групп. "
            "Попробуйте синонимы, более короткий запрос или другую формулировку.",
            kb(
                [
                    [
                        (
                            "🔎 Новый поиск всеми аккаунтами"
                            if len(account_ids) > 1
                            else "🔎 Новый запрос",
                            "tool:groups:all" if len(account_ids) > 1 else f"chat:search:{aid}",
                        )
                    ],
                    [("⬅️ Инструменты", "tool:menu")],
                ]
            ),
        )
        return

    lines = [
        f"🔎 Найдено групп: {len(candidates)} · запрос «{query}»",
        f"Поиск выполнен через {len(account_ids)} аккаунт(ов) Telegram",
        "✓ Каждая ссылка проверена через Telegram API",
        "Каналы, боты и профили исключены.",
        "",
        f"Показываю 1–{min(12, len(candidates))}. Нажмите на группу, чтобы открыть проверку.",
    ]
    for i, item in enumerate(candidates[:12], 1):
        members = (
            f"{int(item.get('members') or 0):,}".replace(",", " ") + " участников"
            if item.get("members")
            else "участники: неизвестно"
        )
        kind_label = (
            "супергруппа"
            if item.get("kind") == "supergroup"
            else ("приватная группа" if item.get("kind") == "invite" else "группа")
        )
        lines.append(f"{i}. {str(item.get('title') or 'Группа')[:75]} · {members} · {kind_label}")
    await edit_anchor(m.bot, anchor, "\n".join(lines), _candidate_keyboard(aid, candidates, 0))


@router.callback_query(F.data.startswith("chat:links:"))
async def chat_candidate_links(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
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
    # Telegram message limit is 4096 chars; keep a safety margin and never split a URL.
    header = f"🔗 Проверенные ссылки на группы · всего {len(links)}\n\n"
    chunks = []
    current = []
    current_len = len(header)
    for link in links:
        if current and current_len + len(link) + 1 > 3700:
            chunks.append(header + "\n".join(current))
            current = []
            current_len = 0
        current.append(link)
        current_len += len(link) + 1
    if current:
        chunks.append((header if not chunks else "") + "\n".join(current))
    for index, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            chunk = f"📄 Часть {index}/{len(chunks)}\n" + chunk
        await c.message.answer(chunk, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("chat:discover:"))
async def chat_discover_links(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    await c.answer("Запущено глубокое сканирование. Это может занять время.")
    anchor = {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id}
    await edit_anchor(
        c.bot,
        anchor,
        f"🧭 Глубокий поиск ссылок · аккаунт #{aid}\n\nПросматриваю до 5 000 последних сообщений в каждой группе, где состоит аккаунт. Сканирование может занять значительное время.",
        kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]),
    )
    scan_stats = {"groups": 0, "messages": 0, "links": 0}

    async def report_progress(groups, messages, links):
        scan_stats.update(groups=groups, messages=messages, links=links)
        if messages % 5000 != 0 and messages != 0:
            return
        progress_text = f"🧭 Глубокий поиск ссылок · аккаунт #{aid}\n\nГрупп обработано: {groups}\nСообщений просмотрено: {messages:,}\nУникальных ссылок найдено: {links}\n\nСканирование продолжается…".replace(
            ",", " "
        )
        await edit_anchor(c.bot, anchor, progress_text, kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]))

    try:
        async with isolated_account_client(aid) as (_account, client):
            candidates = await find_invite_links_in_dialogs(
                client,
                dialog_limit=None,
                messages_per_dialog=5000,
                progress_callback=report_progress,
            )
    except Exception as exc:
        await edit_anchor(
            c.bot,
            {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id},
            f"Не удалось найти ссылки: {str(exc)[:180]}",
            kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]),
        )
        return
    CHAT_CANDIDATES[(c.from_user.id, aid)] = candidates
    if not candidates:
        await edit_anchor(
            c.bot,
            {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id},
            "🧭 Подходящих ссылок в последних сообщениях доступных групп не найдено.\n\nПоиск проверяет недавние сообщения в группах, к которым аккаунт уже имеет доступ; он не читает закрытые чаты, где аккаунт не состоит.",
            kb(
                [
                    [("🔎 Поиск публичных групп", f"chat:search:{aid}")],
                    [("⬅️ К аккаунту", f"acc:view:{aid}")],
                ]
            ),
        )
        return
    lines = [
        f"🧭 Найдено ссылок: {len(candidates)} · аккаунт #{aid}",
        f"Групп обработано: {scan_stats['groups']} · сообщений просмотрено: {scan_stats['messages']:,}".replace(
            ",", " "
        ),
        "Просмотрено до 5 000 последних сообщений в каждой доступной группе.",
        "",
        "Источник показывает чат, в котором была найдена ссылка. Каждая ссылка будет проверена перед вступлением.",
    ]
    for i, item in enumerate(candidates[:12], 1):
        lines.append(f"{i}. {item['link'][:75]}\n   найдено в: {item.get('source', 'сообщение')}")
    await edit_anchor(
        c.bot,
        {"ui_chat_id": c.message.chat.id, "ui_message_id": c.message.message_id},
        "\n".join(lines),
        _candidate_keyboard(aid, candidates, 0),
    )


@router.callback_query(F.data.startswith("chat:page:"))
async def chat_candidate_page(c):
    if not await check_cb(c):
        return
    _, _, aid_raw, offset_raw = c.data.split(":")
    aid, offset = int(aid_raw), max(0, int(offset_raw))
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
    if not candidates:
        await edit(
            c,
            "Результаты больше не сохранены. Запустите поиск ещё раз.",
            kb([[("🔎 Поиск групп", f"chat:search:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]),
        )
        return
    page = candidates[offset : offset + 12]
    lines = [
        f"Найденные группы · аккаунт #{aid}",
        f"Показаны {offset + 1}–{min(offset + 12, len(candidates))} из {len(candidates)}",
        "",
        "Выберите результат для проверки.",
    ]
    for i, item in enumerate(page, start=offset + 1):
        lines.append(f"{i}. {str(item.get('title') or item.get('link') or 'Группа')[:70]}")
    await edit(c, "\n".join(lines), _candidate_keyboard(aid, candidates, offset))


@router.callback_query(F.data.startswith("chat:candidate:"))
async def chat_candidate_preview(c):
    if not await check_cb(c):
        return
    _, _, aid_raw, index_raw = c.data.split(":")
    aid, index = int(aid_raw), int(index_raw)
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
    item = candidate_at(candidates, index)
    if item is None:
        await c.answer("Результаты устарели")
        await edit(
            c,
            "Результаты поиска больше не сохранены (бот перезапускался).\n"
            "Запустите поиск ещё раз — запрос вводить заново не нужно, кнопка ниже.",
            kb(
                [
                    [("🔎 Новый поиск", f"chat:search:{aid}")],
                    [("⬅️ Инструменты", "tool:menu")],
                ]
            ),
        )
        return
    link = item.get("link")
    if not link:
        await c.answer("У этой группы нет публичной ссылки.", show_alert=True)
        return
    try:
        kind, target = parse_chat_link(link)
        async with isolated_account_client(aid) as (_account, client):
            already = False
            if kind == "public":
                entity = await client.get_entity(target)
                if not group_entity(entity):
                    raise ValueError("Это канал или профиль, а не группа. Вступление отменено.")
                title = getattr(entity, "title", None) or target
                members = int(getattr(entity, "participants_count", 0) or 0)
                entity_id = utils.get_peer_id(entity)
            else:
                invite = await client(CheckChatInviteRequest(hash=target))
                if isinstance(invite, ChatInviteAlready):
                    entity = invite.chat
                    if not group_entity(entity):
                        raise ValueError("Ссылка ведёт не на группу. Вступление отменено.")
                    title = getattr(entity, "title", None) or "Группа"
                    members = int(getattr(entity, "participants_count", 0) or 0)
                    entity_id = utils.get_peer_id(entity)
                    already = True
                else:
                    if getattr(invite, "channel", False) and not getattr(
                        invite, "megagroup", False
                    ):
                        raise ValueError("Ссылка ведёт на канал, а не группу.")
                    title = getattr(invite, "title", None) or "Закрытая группа"
                    members = len(getattr(invite, "participants", []) or []) or int(
                        getattr(invite, "participants_count", 0) or 0
                    )
                    entity_id = None
    except Exception as exc:
        await edit(
            c,
            f"Не удалось проверить ссылку:\n{str(exc)[:220]}",
            kb(
                [
                    [("⬅️ К результатам", f"chat:results:{aid}")],
                    [("⬅️ К аккаунту", f"acc:view:{aid}")],
                ]
            ),
        )
        return
    CHAT_PENDING_JOIN[(c.from_user.id, aid)] = {
        "link": link,
        "kind": kind,
        "target": target,
        "title": title,
        "members": members,
        "entity_id": entity_id,
        "already": already,
    }
    status = (
        "Аккаунт уже состоит в группе."
        if already
        else "Вступление произойдёт только после вашего подтверждения."
    )
    await edit(
        c,
        f"👥 {title}\nУчастников: {members or 'неизвестно'}\nСсылка: {link}\n\n{status}\n\nПроверьте название и ссылку перед вступлением.",
        kb(
            [
                [("✅ Подтвердить вступление", f"chat:joinconfirm:{aid}")],
                [("⬅️ К результатам", f"chat:results:{aid}")],
                [("⬅️ К аккаунту", f"acc:view:{aid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("chat:results:"))
async def chat_results_back(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    candidates = CHAT_CANDIDATES.get((c.from_user.id, aid), [])
    if not candidates:
        await edit(
            c,
            "Результаты больше не сохранены. Запустите поиск ещё раз.",
            kb([[("🔎 Поиск групп", f"chat:search:{aid}")], [("⬅️ К аккаунту", f"acc:view:{aid}")]]),
        )
        return
    lines = [f"Найденные кандидаты · аккаунт #{aid}", "", "Выберите группу для проверки."]
    for i, item in enumerate(candidates[:12], 1):
        lines.append(f"{i}. {str(item.get('title') or item.get('link') or 'Группа')[:70]}")
    await edit(c, "\n".join(lines), _candidate_keyboard(aid, candidates, 0))


@router.callback_query(F.data.startswith("chat:joinlink:"))
async def chat_join_link_start(c, state: FSMContext):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    await state.clear()
    await state.update_data(
        ui_chat_id=c.message.chat.id,
        ui_message_id=c.message.message_id,
        return_callback=cancel_return_for(c.data),
        chat_join_account_id=aid,
    )
    await state.set_state(Flow.chat_join_link)
    await edit(
        c,
        f"🔗 Вступление в группу или канал · аккаунт #{aid}\n\nОтправьте публичную ссылку t.me/username или пригласительную ссылку t.me/+… / t.me/joinchat/…. Поддерживаются группы, супергруппы и каналы — как публичные, так и приватные.\n\nЯ автоматически определю тип сообщества, проверю ссылку и покажу предпросмотр. Вступление или подписка произойдёт только после вашего отдельного подтверждения.",
        CANCEL,
    )


@router.message(Flow.chat_join_link)
async def chat_join_link_input(m: Message, state: FSMContext):
    if not await check_msg(m) or not await private(m):
        return
    data = await state.get_data()
    raw = (m.text or "").strip()
    await delete_msg(m)
    try:
        kind, target = parse_chat_link(raw)
    except ValueError as exc:
        await prompt_message(m, state, str(exc))
        return
    aid = int(data.get("chat_join_account_id", 0))
    anchor = {"ui_chat_id": data.get("ui_chat_id"), "ui_message_id": data.get("ui_message_id")}
    await state.clear()
    CHAT_CANDIDATES[(m.from_user.id, aid)] = [
        {"title": raw[:80], "link": raw, "source": "Введено вручную"}
    ]
    # Reuse the same guarded preview flow as search results.
    CHAT_PENDING_JOIN.pop((m.from_user.id, aid), None)
    CHANNEL_PENDING_JOIN.pop((m.from_user.id, aid), None)
    try:
        async with isolated_account_client(aid) as (_account, client):
            already = False
            is_channel = False
            if kind == "public":
                entity = await client.get_entity(target)
                is_channel = channel_entity(entity)
                if not is_channel and not group_entity(entity):
                    raise ValueError("Ссылка ведёт не на группу и не на канал Telegram.")
                title = getattr(entity, "title", None) or target
                members = int(getattr(entity, "participants_count", 0) or 0)
                entity_id = utils.get_peer_id(entity)
            else:
                invite = await client(CheckChatInviteRequest(hash=target))
                if isinstance(invite, ChatInviteAlready):
                    entity = invite.chat
                    is_channel = channel_entity(entity)
                    if not is_channel and not group_entity(entity):
                        raise ValueError("Пригласительная ссылка ведёт не на группу и не на канал.")
                    title = getattr(entity, "title", None) or ("Канал" if is_channel else "Группа")
                    members = int(getattr(entity, "participants_count", 0) or 0)
                    entity_id = utils.get_peer_id(entity)
                    already = True
                else:
                    is_channel = bool(
                        getattr(invite, "channel", False)
                        and not getattr(invite, "megagroup", False)
                    )
                    title = getattr(invite, "title", None) or (
                        "Приватный канал" if is_channel else "Закрытая группа"
                    )
                    members = len(getattr(invite, "participants", []) or []) or int(
                        getattr(invite, "participants_count", 0) or 0
                    )
                    entity_id = None
    except Exception as exc:
        await edit_anchor(
            m.bot,
            anchor,
            f"Не удалось проверить ссылку:\n{str(exc)[:220]}",
            kb(
                [
                    [("🔗 Ввести другую ссылку", f"chat:joinlink:{aid}")],
                    [("⬅️ К аккаунту", f"acc:view:{aid}")],
                ]
            ),
        )
        return

    if is_channel:
        CHANNEL_PENDING_JOIN[(m.from_user.id, aid)] = {
            "link": raw,
            "kind": kind,
            "target": target,
            "title": title,
            "members": members,
            "entity_id": entity_id,
            "already": already,
        }
        status = (
            "Аккаунт уже подписан на канал."
            if already
            else "Подписка произойдёт только после отдельного подтверждения."
        )
        await edit_anchor(
            m.bot,
            anchor,
            f"📣 {title}\nПодписчиков: {members or 'неизвестно'}\nСсылка: {raw}\n\n{status}",
            kb(
                [
                    [("✅ Подтвердить подписку", f"channel:joinconfirm:{aid}")],
                    [("🔗 Другая ссылка", f"chat:joinlink:{aid}")],
                    [("⬅️ К аккаунту", f"acc:view:{aid}")],
                ]
            ),
        )
        return

    CHAT_PENDING_JOIN[(m.from_user.id, aid)] = {
        "link": raw,
        "kind": kind,
        "target": target,
        "title": title,
        "members": members,
        "entity_id": entity_id,
        "already": already,
    }
    status = (
        "Аккаунт уже состоит в группе."
        if already
        else "Вступление произойдёт только после подтверждения."
    )
    await edit_anchor(
        m.bot,
        anchor,
        f"👥 {title}\nУчастников: {members or 'неизвестно'}\nСсылка: {raw}\n\n{status}",
        kb(
            [
                [("✅ Подтвердить вступление", f"chat:joinconfirm:{aid}")],
                [("🔗 Другая ссылка", f"chat:joinlink:{aid}")],
                [("⬅️ К аккаунту", f"acc:view:{aid}")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("chat:joinconfirm:"))
async def chat_join_confirm(c):
    if not await check_cb(c):
        return
    aid = int(c.data.split(":")[2])
    pending = CHAT_PENDING_JOIN.get((c.from_user.id, aid))
    if not pending:
        await c.answer("Предпросмотр устарел. Проверьте ссылку ещё раз.", show_alert=True)
        return
    try:
        async with isolated_account_client(aid) as (_account, client):
            me = await client.get_me()
            if me is None or int(me.id) != int(_account.telegram_user_id):
                actual_id = int(me.id) if me is not None and getattr(me, "id", None) else None
                raise RuntimeError(
                    f"Сессия аккаунта #{aid} не совпадает с записью аккаунта: "
                    f"ожидался Telegram ID {_account.telegram_user_id}, получен {actual_id}."
                )
            logging.info(
                "Chat join started: account_id=%s telegram_user_id=%s username=%s kind=%s already=%s",
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
                if not group_entity(entity):
                    raise ValueError("Ссылка ведёт не на группу.")

                async def report_floodwait(seconds):
                    await edit(
                        c,
                        f"⏳ Telegram временно ограничил вступления. Жду {seconds} сек., затем повторю вступление в группу «{pending['title']}»…",
                        kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]),
                    )

                join_result = await perform_join_request(
                    aid, client, JoinChannelRequest(entity), report_floodwait
                )
                logging.info(
                    "Chat join RPC accepted: account_id=%s telegram_user_id=%s request=JoinChannelRequest result=%s",
                    aid,
                    int(me.id),
                    type(join_result).__name__,
                )
            else:

                async def report_floodwait(seconds):
                    await edit(
                        c,
                        f"⏳ Telegram временно ограничил вступления. Жду {seconds} сек., затем повторю вступление в группу «{pending['title']}»…",
                        kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]),
                    )

                joined = await perform_join_request(
                    aid, client, ImportChatInviteRequest(hash=pending["target"]), report_floodwait
                )
                logging.info(
                    "Chat join RPC accepted: account_id=%s telegram_user_id=%s request=ImportChatInviteRequest chats=%s",
                    aid,
                    int(me.id),
                    [
                        (type(x).__name__, getattr(x, "id", None))
                        for x in (getattr(joined, "chats", []) or [])
                    ],
                )
                entity = next((x for x in joined.chats if group_entity(x)), None)
                if entity is None:
                    raise RuntimeError("Telegram не вернул вступившую группу.")
            if entity is None:
                entity = await client.get_entity(pending["target"])
            if not group_entity(entity):
                raise ValueError("Ссылка не ведёт на группу.")
            chat_id = int(utils.get_peer_id(entity))
            title = (getattr(entity, "title", None) or pending["title"] or "Группа")[:255]
            # Verify membership on the SAME Telegram client/session that executed the join.
            # A second isolated snapshot can carry stale peer state and must not decide success.
            is_member = False
            for attempt in range(3):
                is_member = await telegram_management.verify_chat_membership(client, entity)
                if is_member:
                    break
                if attempt < 2:
                    await asyncio.sleep(1)
            logging.info(
                "Chat membership verification: account_id=%s telegram_user_id=%s chat_id=%s entity_type=%s is_member=%s",
                aid,
                int(me.id),
                chat_id,
                type(entity).__name__,
                is_member,
            )
            if not is_member:
                await edit(
                    c,
                    "Telegram не подтвердил членство аккаунта в группе. Возможно, вступление ожидает одобрения или Telegram отклонил его. Группа не добавлена в список чатов.",
                    kb(
                        [
                            [("🔁 Проверить после одобрения", f"chat:joinlink:{aid}")],
                            [("⬅️ К аккаунту", f"acc:view:{aid}")],
                        ]
                    ),
                )
                return
    except Exception as exc:
        logging.exception(
            "Chat join failed: account_id=%s kind=%s error_type=%s",
            aid,
            pending.get("kind"),
            type(exc).__name__,
        )
        # Approval-only invite links are not equivalent to membership; explain that no binding was created.
        if isinstance(exc, FloodWaitError):
            await edit(
                c,
                f"⛔ Telegram ограничил вступления. Повторная попытка остановлена. Подождите {int(exc.seconds)} сек. и попробуйте снова.",
                kb(
                    [
                        [("🔗 Другая ссылка", f"chat:joinlink:{aid}")],
                        [("⬅️ К аккаунту", f"acc:view:{aid}")],
                    ]
                ),
            )
            return
        if type(exc).__name__ == "ChannelPrivateError":
            await edit(
                c,
                "⛔ Telegram сообщил, что группа приватная или аккаунту закрыт доступ. "
                "Ссылка t.me/username не даёт права вступить в такую группу. "
                "Нужна действующая пригласительная ссылка t.me/+… или t.me/joinchat/… "
                "именно для этого аккаунта. Если аккаунт заблокирован администраторами, "
                "пригласительная ссылка тоже не поможет. В список чатов группа не добавлена.",
                kb(
                    [
                        [("🔗 Ввести пригласительную ссылку", f"chat:joinlink:{aid}")],
                        [("⬅️ К аккаунту", f"acc:view:{aid}")],
                    ]
                ),
            )
            return
        if "InviteRequestSentError" in type(exc).__name__:
            await edit(
                c,
                "Запрос на вступление отправлен администраторам группы. Аккаунт пока не добавлен в активные чаты — повторите после одобрения.",
                kb(
                    [
                        [("🔗 Проверить другую ссылку", f"chat:joinlink:{aid}")],
                        [("⬅️ К аккаунту", f"acc:view:{aid}")],
                    ]
                ),
            )
            return
        if "UserAlreadyParticipantError" in type(exc).__name__:
            try:
                async with isolated_account_client(aid) as (_account, client):
                    kind, target = pending["kind"], pending["target"]
                    if kind == "public":
                        entity = await client.get_entity(target)
                    else:
                        invite = await client(CheckChatInviteRequest(hash=target))
                        entity = invite.chat if isinstance(invite, ChatInviteAlready) else None
                    if entity is None or not group_entity(entity):
                        raise RuntimeError("Не удалось определить уже доступную группу.")
                    chat_id = int(utils.get_peer_id(entity))
                    title = (getattr(entity, "title", None) or pending["title"] or "Группа")[:255]
                    if not await telegram_management.verify_chat_membership(client, entity):
                        raise RuntimeError("Telegram не подтвердил членство аккаунта в группе.")
            except Exception as nested:
                await edit(
                    c,
                    f"Аккаунт уже состоит в группе, но не удалось обновить её в списке: {type(nested).__name__}.",
                    kb([[("⬅️ К аккаунту", f"acc:view:{aid}")]]),
                )
                return
        else:
            await edit(
                c,
                f"Не удалось вступить: {type(exc).__name__}: {str(exc)[:180]}. Проверьте ссылку, ограничения Telegram и доступность группы.",
                kb(
                    [
                        [("🔗 Другая ссылка", f"chat:joinlink:{aid}")],
                        [("⬅️ К аккаунту", f"acc:view:{aid}")],
                    ]
                ),
            )
            return
    if entity is None:
        await edit(
            c,
            "Telegram не подтвердил вступление: чат не определён. Запись не создана.",
            kb(
                [
                    [("🔗 Проверить ссылку ещё раз", f"chat:joinlink:{aid}")],
                    [("⬅️ К аккаунту", f"acc:view:{aid}")],
                ]
            ),
        )
        return
    async with SessionFactory() as db:
        binding = await db.scalar(
            select(ChatBinding).where(ChatBinding.account_id == aid, ChatBinding.chat_id == chat_id)
        )
        access_hash = getattr(entity, "access_hash", None)
        peer_type = (
            "channel"
            if access_hash is not None or entity.__class__.__name__.startswith("Channel")
            else "chat"
        )
        if binding:
            binding.title = title
            binding.enabled = True
            binding.access_hash = int(access_hash) if access_hash is not None else None
            binding.peer_type = peer_type
        else:
            db.add(
                ChatBinding(
                    account_id=aid,
                    chat_id=chat_id,
                    title=title,
                    enabled=True,
                    auto_discovered=False,
                    access_hash=int(access_hash) if access_hash is not None else None,
                    peer_type=peer_type,
                )
            )
        await db.commit()
    CHAT_PENDING_JOIN.pop((c.from_user.id, aid), None)
    await edit(
        c,
        f"✅ Группа «{title}» добавлена в чаты аккаунта #{aid}.\n\nЧат включён для работы и обучения. Если нужно, его можно выключить в разделе «Чаты аккаунта».",
        kb(
            [
                [("💬 Чаты аккаунта", f"chat:account:{aid}:0")],
                [("👤 К аккаунту", f"acc:view:{aid}")],
                [("🏠 Главное меню", "nav:home")],
            ]
        ),
    )


@router.callback_query(F.data == "chat:menu")
async def chats_legacy_redirect(c):
    if not await check_cb(c):
        return
    await edit(
        c,
        "Управление чатами теперь находится внутри карточки аккаунта.",
        kb([[("👤 К аккаунтам", "acc:list")], [("🏠 Главное меню", "nav:home")]]),
    )


async def chat_account_screen(c, aid, page=0):
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
        bindings = list(
            (
                await db.scalars(
                    select(ChatBinding)
                    .where(ChatBinding.account_id == aid)
                    .order_by(ChatBinding.title, ChatBinding.id)
                )
            ).all()
        )
        discussion_ids = set(
            (
                await db.scalars(
                    select(ChannelBinding.discussion_group_id).where(
                        ChannelBinding.account_id == aid,
                        ChannelBinding.discussion_group_id.is_not(None),
                    )
                )
            ).all()
        )
        channel_ids = set(
            (
                await db.scalars(
                    select(ChannelBinding.channel_id).where(ChannelBinding.account_id == aid)
                )
            ).all()
        )
        excluded_chat_ids = discussion_ids | channel_ids
        bindings = [row for row in bindings if int(row.chat_id) not in excluded_chat_ids]
    if not account:
        await c.answer("Аккаунт не найден.", show_alert=True)
        return
    page_size = 20
    max_page = max(0, (len(bindings) - 1) // page_size)
    page = max(0, min(page, max_page))
    visible = bindings[page * page_size : (page + 1) * page_size]
    rows = [
        [
            (
                f"{'🟢' if row.enabled else '⚪'} {row.title[:38]}",
                f"chat:select:{aid}:{row.id}:{page}",
            )
        ]
        for row in visible
    ]
    nav = []
    if page > 0:
        nav.append(("⬅️ Назад", f"chat:account:{aid}:{page - 1}"))
    if page < max_page:
        nav.append(("➡️ Далее", f"chat:account:{aid}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows += [
        [
            (
                "↻ Загрузить чаты" if not bindings else "↻ Обновить список чатов",
                f"chat:load:{aid}:{page}",
            )
        ],
        [("🧭 Поиск ссылок", f"chat:discover:{aid}")],
        [("⬅️ К аккаунту", f"acc:view:{aid}")],
    ]
    hint = (
        "Сначала нажмите «Загрузить чаты»."
        if not bindings
        else f"Всего чатов: {len(bindings)}. Нажатие на строку сразу включает или выключает чат."
    )
    await edit(
        c,
        f"💬 Чаты · {account.display_name} · страница {page + 1}/{max_page + 1}\n\n{hint}\n🟢 — включён · ⚪ — выключен",
        kb(rows),
    )


@router.callback_query(F.data.startswith("chat:account:"))
async def chat_account(c):
    if not await check_cb(c):
        return
    parts = c.data.split(":")
    aid = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    await chat_account_screen(c, aid, page)


@router.callback_query(F.data.startswith("chat:load:"))
async def chat_load(c):
    if not await check_cb(c):
        return
    parts = c.data.split(":")
    aid = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        await c.answer("Не заданы Telegram API credentials.", show_alert=True)
        return
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account:
        await c.answer("Аккаунт не найден", show_alert=True)
        return
    try:
        async with isolated_telegram_client(account) as client:
            if not await client.is_user_authorized():
                await c.answer("Сессия не авторизована", show_alert=True)
                return
            dialogs = []
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if (
                    getattr(entity, "deleted", False)
                    or getattr(entity, "bot", False)
                    or (
                        getattr(entity, "broadcast", False)
                        and not getattr(entity, "megagroup", False)
                    )
                ):
                    continue
                title = (dialog.name or str(dialog.id)).strip()
                if title:
                    dialogs.append((int(dialog.id), title[:255]))
    except Exception as exc:
        await c.answer(f"Ошибка загрузки: {type(exc).__name__}: {str(exc)[:180]}", show_alert=True)
        return
    async with SessionFactory() as db:
        discussion_ids = set(
            (
                await db.scalars(
                    select(ChannelBinding.discussion_group_id).where(
                        ChannelBinding.account_id == aid,
                        ChannelBinding.discussion_group_id.is_not(None),
                    )
                )
            ).all()
        )
        channel_ids = set(
            (
                await db.scalars(
                    select(ChannelBinding.channel_id).where(ChannelBinding.account_id == aid)
                )
            ).all()
        )
        excluded_chat_ids = discussion_ids | channel_ids
        dialogs = [item for item in dialogs if item[0] not in excluded_chat_ids]
        existing = list(
            (await db.scalars(select(ChatBinding).where(ChatBinding.account_id == aid))).all()
        )
        by_chat = {row.chat_id: row for row in existing}
        added = 0
        for chat_id, title in dialogs:
            binding = by_chat.get(chat_id)
            if binding:
                binding.title = title
            else:
                db.add(
                    ChatBinding(account_id=aid, chat_id=chat_id, **discovered_binding_values(title))
                )
                added += 1
        await db.commit()
    await chat_account_screen(c, aid, page)
    await c.message.answer(f"✅ Загружено чатов: {len(dialogs)} · новых добавлено: {added}")


@router.callback_query(F.data.startswith("chat:select:"))
async def chat_select(c):
    if not await check_cb(c):
        return
    _, _, aid_raw, binding_raw, page_raw = c.data.split(":")
    aid, binding_id, page = int(aid_raw), int(binding_raw), int(page_raw)
    async with SessionFactory() as db:
        binding = await db.get(ChatBinding, binding_id)
        if not binding or binding.account_id != aid:
            await c.answer("Чат не найден.", show_alert=True)
            return
        binding.enabled = not binding.enabled
        await db.commit()
    await chat_account_screen(c, aid, page)


async def chat_account_refresh(c, aid, page=0):
    await chat_account_screen(c, aid, page)


@router.callback_query(F.data.startswith("chat:toggle:"))
async def chat_toggle(c):
    if not await check_cb(c):
        return
    bid = int(c.data.split(":")[2])
    async with SessionFactory() as db:
        b = await db.get(ChatBinding, bid)
        if not b:
            await c.answer("Чат не найден", show_alert=True)
            return
        b.enabled = not b.enabled
        aid = b.account_id
        await db.commit()
    await chat_account_refresh(c, aid)
