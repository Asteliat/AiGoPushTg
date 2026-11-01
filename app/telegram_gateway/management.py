"""Safe Telegram account management operations on isolated session snapshots.

All mutating operations are executed through a temporary SQLite backup, never by
opening a second Telethon client against the worker-owned live session. Telegram
FloodWait errors are intentionally propagated to the caller; this module does not
sleep through, suppress, or bypass server-side limits.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest, SendReactionRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.types import ChatInviteAlready, InputPhoneContact, ReactionEmoji

from app.settings import get_settings
from sqlalchemy import text

from app.storage.db import SessionFactory, engine
from app.storage.models import Account
from app.telegram_gateway.proxy import telethon_proxy
from app.telegram_gateway.session import configure_telethon_sqlite_session


_ACCOUNT_LOCKS: dict[int, asyncio.Lock] = {}
cfg = get_settings()


@dataclass(frozen=True)
class ChatOverview:
    title: str
    username: str | None
    chat_id: int
    participants_count: int | None
    is_channel: bool
    is_group: bool


@asynccontextmanager
async def isolated_account_client(account_id: int) -> AsyncIterator[tuple[Account, TelegramClient]]:
    """Open a temporary session snapshot while holding a cross-process account lock.

    PostgreSQL advisory locks serialize bot, worker, and other tool processes that share
    the same authorized SQLite session. The live session is only read through SQLite
    backup; all Telethon operations happen on the isolated copy.
    """
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        raise RuntimeError("Не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH.")

    lock_namespace = 741928462
    async with engine.connect() as lock_connection:
        await lock_connection.execute(
            text("SELECT pg_advisory_lock(:namespace, :account_id)"),
            {"namespace": lock_namespace, "account_id": int(account_id)},
        )
        await lock_connection.commit()
        try:
            async with SessionFactory() as db:
                account = await db.get(Account, account_id)
                if not account or not account.enabled:
                    raise RuntimeError("Аккаунт не найден или отключён.")
                db.expunge(account)

            source_path = str(account.session_path)
            if not source_path.endswith(".session"):
                source_path += ".session"
            if not os.path.isfile(source_path):
                raise RuntimeError("Файл сессии аккаунта не найден.")

            with tempfile.TemporaryDirectory(prefix=f"atciv-tools-{account_id}-") as temp_dir:
                snapshot_path = os.path.join(temp_dir, "account.session")
                source_db = sqlite3.connect(source_path, timeout=30)
                snapshot_db = sqlite3.connect(snapshot_path, timeout=30)
                try:
                    source_db.execute("PRAGMA busy_timeout = 30000")
                    snapshot_db.execute("PRAGMA busy_timeout = 30000")
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
                configure_telethon_sqlite_session(client)
                try:
                    await client.connect()
                    if not await client.is_user_authorized():
                        raise RuntimeError("Сессия аккаунта не авторизована.")
                    client.flood_sleep_threshold = 0
                    yield account, client
                finally:
                    if client.is_connected():
                        await client.disconnect()
        finally:
            try:
                await lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:namespace, :account_id)"),
                    {"namespace": lock_namespace, "account_id": int(account_id)},
                )
                await lock_connection.commit()
            except Exception:
                # A broken connection will release its session-level lock when closed.
                await lock_connection.invalidate()




def _account_lock(account_id: int) -> asyncio.Lock:
    return _ACCOUNT_LOCKS.setdefault(account_id, asyncio.Lock())


async def get_profile(account_id: int) -> dict:
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            me = await client.get_me()
            full = await client(GetFullUserRequest(me))
            return {
                "id": int(me.id),
                "username": me.username,
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
                "phone": me.phone or "",
                "about": full.full_user.about or "",
            }


async def update_profile(
    account_id: int,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    about: str | None = None,
    username: str | None = None,
) -> dict:
    """Update only explicitly supplied profile fields."""
    if first_name is not None and len(first_name) > 64:
        raise ValueError("Имя должно быть не длиннее 64 символов.")
    if last_name is not None and len(last_name) > 64:
        raise ValueError("Фамилия должна быть не длиннее 64 символов.")
    if about is not None and len(about) > 70:
        raise ValueError("BIO должен быть не длиннее 70 символов.")
    if username is not None:
        username = username.strip().lstrip("@")
        if username and not (5 <= len(username) <= 32):
            raise ValueError("Username должен содержать от 5 до 32 символов.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            if any(value is not None for value in (first_name, last_name, about)):
                current = await client.get_me()
                full = await client(GetFullUserRequest(current))
                await client(UpdateProfileRequest(
                    first_name=first_name if first_name is not None else (current.first_name or ""),
                    last_name=last_name if last_name is not None else (current.last_name or ""),
                    about=about if about is not None else (full.full_user.about or ""),
                ))
            if username is not None:
                await client(UpdateUsernameRequest(username=username))
            me = await client.get_me()
            return {
                "username": me.username,
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
            }


async def set_profile_photo(account_id: int, photo_path: str) -> None:
    """Upload one operator-selected profile photo through an isolated session."""
    if not os.path.isfile(photo_path):
        raise ValueError("Файл фотографии не найден.")
    if os.path.getsize(photo_path) > 10 * 1024 * 1024:
        raise ValueError("Размер фотографии не должен превышать 10 МБ.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            await client.upload_profile_photo(photo_path)


async def list_contacts(account_id: int, *, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            contacts = await client.get_contacts(limit=limit)
            return [
                {
                    "id": int(item.id),
                    "username": item.username,
                    "first_name": item.first_name or "",
                    "last_name": item.last_name or "",
                    "phone": item.phone or "",
                }
                for item in contacts
            ]


async def add_contact(
    account_id: int, *, phone: str, first_name: str, last_name: str = ""
) -> int:
    phone = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if len(phone) < 7 or len(phone) > 16:
        raise ValueError("Проверьте номер телефона.")
    if not first_name.strip() or len(first_name) > 64 or len(last_name) > 64:
        raise ValueError("Проверьте имя контакта.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            result = await client(ImportContactsRequest([
                InputPhoneContact(
                    client_id=0,
                    phone=phone,
                    first_name=first_name.strip(),
                    last_name=last_name.strip(),
                )
            ]))
            return len(result.imported)


async def import_contacts(account_id: int, contacts: list[dict]) -> dict:
    """Import up to 100 operator-supplied contacts in one Telegram request."""
    if not contacts or len(contacts) > 100:
        raise ValueError("За один импорт можно передать от 1 до 100 контактов.")
    normalized = []
    for index, item in enumerate(contacts):
        phone = "".join(ch for ch in str(item.get("phone", "")) if ch.isdigit() or ch == "+")
        first_name = str(item.get("first_name", "")).strip()
        last_name = str(item.get("last_name", "")).strip()
        if len(phone) < 7 or len(phone) > 16 or not first_name or len(first_name) > 64 or len(last_name) > 64:
            raise ValueError(f"Проверьте телефон и имя в строке {index + 1}.")
        normalized.append(InputPhoneContact(
            client_id=index, phone=phone, first_name=first_name, last_name=last_name,
        ))
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            result = await client(ImportContactsRequest(normalized))
            return {
                "imported": len(result.imported),
                "retry_contacts": len(result.retry_contacts),
                "users": len(result.users),
            }


async def delete_contact(account_id: int, user_id: int) -> None:
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            entity = await client.get_entity(int(user_id))
            await client(DeleteContactsRequest(id=[entity]))


async def join_chat(account_id: int, link: str) -> None:
    """Join a public chat or a private invite; FloodWait is returned to the caller."""
    value = link.strip()
    if not value:
        raise ValueError("Укажите ссылку или @username.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            lowered = value.lower().rstrip("/")
            if "t.me/+" in lowered or "t.me/joinchat/" in lowered:
                invite_hash = value.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lstrip("+")
                if not invite_hash:
                    raise ValueError("Некорректная приватная ссылка-приглашение.")
                invite = await client(CheckChatInviteRequest(hash=invite_hash))
                if isinstance(invite, ChatInviteAlready):
                    return
                await client(ImportChatInviteRequest(hash=invite_hash))
                return
            target = value
            if "t.me/" in lowered:
                target = value.rstrip("/").rsplit("/", 1)[-1]
            entity = await client.get_entity(target)
            await client(JoinChannelRequest(entity))


async def leave_chat(account_id: int, link: str) -> None:
    value = link.strip()
    if not value:
        raise ValueError("Укажите ссылку или @username.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            target = value.split("t.me/", 1)[1].split("/", 1)[0].split("?", 1)[0] if "t.me/" in value.lower() else value
            entity = await client.get_entity(target)
            if entity.__class__.__name__ == "Chat":
                await client.delete_dialog(entity)
            else:
                await client(LeaveChannelRequest(entity))


async def get_chat_overview(account_id: int, link: str) -> ChatOverview:
    """Read chat metadata only; does not harvest participants or export member lists."""
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            entity = await client.get_entity(link.strip())
            count = getattr(entity, "participants_count", None)
            return ChatOverview(
                title=str(getattr(entity, "title", "") or getattr(entity, "username", "") or "Чат"),
                username=getattr(entity, "username", None),
                chat_id=int(entity.id),
                participants_count=int(count) if count is not None else None,
                is_channel=bool(getattr(entity, "broadcast", False)),
                is_group=bool(
                    getattr(entity, "megagroup", False)
                    or getattr(entity, "gigagroup", False)
                    or entity.__class__.__name__ == "Chat"
                ),
            )


async def parse_chat_history(
    account_id: int, channel: str, *, limit: int = 20
) -> dict:
    """Parse recent messages from an accessible channel/group, never participant lists."""
    limit = max(1, min(int(limit), 100))
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            entity = await client.get_entity(channel.strip())
            if not (
                getattr(entity, "broadcast", False)
                or getattr(entity, "megagroup", False)
                or entity.__class__.__name__ == "Chat"
            ):
                raise ValueError("Укажите канал или группу, историю которой аккаунт имеет право просматривать.")
            posts = []
            async for message in client.iter_messages(entity, limit=limit):
                if not message or getattr(message, "action", None):
                    continue
                reaction_items = []
                reactions = getattr(message, "reactions", None)
                for item in getattr(reactions, "results", []) or []:
                    reaction = getattr(item, "reaction", None)
                    reaction_items.append({
                        "emoji": getattr(reaction, "emoticon", None) or getattr(reaction, "document_id", None),
                        "count": int(getattr(item, "count", 0) or 0),
                    })
                posts.append({
                    "id": int(message.id),
                    "date": message.date.isoformat() if message.date else "",
                    "text": (message.message or "")[:3000],
                    "views": int(message.views or 0),
                    "forwards": int(message.forwards or 0),
                    "replies": int(getattr(getattr(message, "replies", None), "replies", 0) or 0),
                    "reactions": reaction_items,
                    "has_media": bool(message.media),
                })
            return {
                "channel_id": int(entity.id),
                "title": str(getattr(entity, "title", "") or getattr(entity, "username", "") or channel),
                "username": getattr(entity, "username", None),
                "posts": posts,
            }


async def parse_chat_participants(
    account_id: int, chat: str, *, limit: int = 200
) -> dict:
    """Return Telegram-visible public participant fields for an accessible group.

    Telegram decides which members are visible. The result is deliberately capped and
    excludes phone numbers, private profile fields, and any attempt to bypass API limits.
    """
    limit = max(1, min(int(limit), 500))
    target = chat.strip()
    if not target:
        raise ValueError("Укажите @username или ссылку на группу.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            entity = await client.get_entity(target)
            is_group = bool(
                getattr(entity, "megagroup", False)
                or getattr(entity, "gigagroup", False)
                or entity.__class__.__name__ == "Chat"
            )
            is_channel = bool(getattr(entity, "broadcast", False))
            if not (is_group or is_channel):
                raise ValueError("Укажите группу или канал.")
            participants = []
            async for user in client.iter_participants(entity, limit=limit):
                if not getattr(user, "id", None):
                    continue
                participants.append({
                    "id": int(user.id),
                    "username": getattr(user, "username", None) or "",
                    "first_name": getattr(user, "first_name", None) or "",
                    "last_name": getattr(user, "last_name", None) or "",
                    "is_bot": bool(getattr(user, "bot", False)),
                    "is_deleted": bool(getattr(user, "deleted", False)),
                })
            return {
                "chat_id": int(entity.id),
                "title": str(getattr(entity, "title", "") or getattr(entity, "username", "") or target),
                "username": getattr(entity, "username", None),
                "visible_count": len(participants),
                "requested_limit": limit,
                "participants": participants,
            }


async def react_to_message(
    account_id: int, *, chat: str, message_id: int, emoji: str
) -> None:
    """Add one operator-selected reaction to one explicitly selected message."""
    allowed = {"👍", "👎", "❤️", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🎉", "💩", "🙏"}
    if emoji not in allowed:
        raise ValueError("Эта реакция не входит в список поддерживаемых.")
    if int(message_id) <= 0:
        raise ValueError("Некорректный ID публикации.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            entity = await client.get_entity(chat.strip())
            await client(SendReactionRequest(
                peer=entity,
                msg_id=int(message_id),
                reaction=[ReactionEmoji(emoticon=emoji)],
                big=False,
                add_to_recent=False,
            ))


async def resolve_peer_for_consent(
    account_id: int, peer_ref: str, *, allow_group: bool = False
) -> dict:
    """Resolve a public user or (when requested) a public group for explicit opt-in."""
    value = peer_ref.strip()
    if not value:
        raise ValueError("Укажите @username или публичную ссылку t.me.")
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/").split("/", 1)[0]
    value = value.lstrip("@")
    if not value or value.startswith("+") or value.startswith("joinchat"):
        raise ValueError("Нужен публичный username; приватные invite-ссылки здесь не принимаются.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            entity = await client.get_entity(value)
            if not getattr(entity, "id", None):
                raise ValueError("Telegram не вернул корректный объект получателя.")
            is_channel_entity = entity.__class__.__name__ == "Channel"
            is_group = entity.__class__.__name__ == "Chat" or (
                is_channel_entity and bool(
                    getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
                )
            )
            if is_channel_entity and not is_group:
                raise ValueError("Каналы вещания нельзя использовать как групповые получатели.")
            if is_group:
                if not allow_group:
                    raise ValueError("Для личной рассылки укажите пользователя, а не группу.")
                username = getattr(entity, "username", None)
                if not username:
                    raise ValueError("Для групповой рассылки нужна публичная группа с username.")
                from telethon import utils
                return {
                    "peer_id": int(utils.get_peer_id(entity)),
                    "peer_ref": f"@{username}",
                    "peer_label": str(getattr(entity, "title", None) or f"@{username}"),
                    "peer_type": "group",
                }
            username = getattr(entity, "username", None)
            if not username:
                raise ValueError("У пользователя нет публичного username; добавьте его через контакты.")
            return {
                "peer_id": int(entity.id),
                "peer_ref": f"@{username}",
                "peer_label": " ".join(filter(None, [
                    getattr(entity, "first_name", None),
                    getattr(entity, "last_name", None),
                ])) or f"@{username}",
                "peer_type": "user",
            }


async def resolve_peers_for_consent(
    account_id: int, peer_refs: list[str], *, allow_groups: bool = False
) -> list[dict]:
    """Resolve explicitly selected public users or groups in one isolated session."""
    values = []
    for raw in peer_refs:
        value = raw.strip()
        if value.startswith("https://t.me/"):
            value = value.removeprefix("https://t.me/").split("/", 1)[0]
        value = value.lstrip("@")
        if not value or value.startswith("+") or value.startswith("joinchat"):
            raise ValueError(f"Некорректный username получателя: {raw}")
        values.append(value)
    if not values or len(values) > 100:
        raise ValueError("Укажите от 1 до 100 получателей за одну задачу.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            result = []
            for value in values:
                entity = await client.get_entity(value)
                if not getattr(entity, "id", None):
                    raise ValueError(f"@{value} не найден в Telegram.")
                is_channel_entity = entity.__class__.__name__ == "Channel"
                is_group = entity.__class__.__name__ == "Chat" or (
                    is_channel_entity and bool(
                        getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False)
                    )
                )
                if is_channel_entity and not is_group:
                    raise ValueError(f"@{value} — канал вещания, а не группа.")
                if is_group:
                    if not allow_groups:
                        raise ValueError(f"@{value} — группа. Для неё выберите тип group_broadcast.")
                    username = getattr(entity, "username", None)
                    if not username:
                        raise ValueError(f"Группа @{value} должна иметь публичный username.")
                    from telethon import utils
                    peer_id = int(utils.get_peer_id(entity))
                    label = str(getattr(entity, "title", None) or f"@{username}")
                else:
                    username = getattr(entity, "username", None)
                    if not username:
                        raise ValueError(f"У @{value} нет публичного username.")
                    peer_id = int(entity.id)
                    label = " ".join(filter(None, [
                        getattr(entity, "first_name", None),
                        getattr(entity, "last_name", None),
                    ])) or f"@{username}"
                result.append({
                    "peer_id": peer_id,
                    "peer_ref": f"@{username}",
                    "peer_label": label,
                    "peer_type": "group" if is_group else "user",
                })
            return result


async def simulate_post_activity(
    *, post_key: str, requested_views: int, requested_reactions: int
) -> dict:
    """Return local-only synthetic test metrics; never calls Telegram or changes a post."""
    if not post_key.strip():
        raise ValueError("Для теста укажите идентификатор тестовой публикации.")
    views = max(0, min(int(requested_views), 1_000_000))
    reactions = max(0, min(int(requested_reactions), 100_000))
    return {
        "mode": "simulation",
        "post_key": post_key.strip()[:160],
        "simulated_views": views,
        "simulated_reactions": reactions,
        "telegram_modified": False,
        "notice": "Только локальная симуляция. Счётчики Telegram не изменялись.",
    }


__all__ = [
    "ChatOverview", "FloodWaitError", "add_contact", "delete_contact",
    "get_chat_overview", "get_profile", "join_chat", "leave_chat",
    "import_contacts", "list_contacts", "parse_chat_history", "react_to_message", "resolve_peer_for_consent", "resolve_peers_for_consent", "set_profile_photo", "simulate_post_activity",
    "update_profile",
]
