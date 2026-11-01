"""Safe Telegram account management operations on isolated session snapshots.

All mutating operations are executed through a temporary SQLite backup, never by
opening a second Telethon client against the worker-owned live session. Telegram
FloodWait errors are intentionally propagated to the caller; this module does not
sleep through, suppress, or bypass server-side limits.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from telethon import TelegramClient, utils
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
    """Run an operation on a private SQLite snapshot under a cross-process account lock.

    The source session is never opened by this client. A PostgreSQL advisory lock serializes
    snapshots and account-management operations across bot/worker processes. SQLite backup
    retries transient busy/locked states instead of exposing a half-copied session.
    """
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        raise RuntimeError("Не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH.")

    lock_namespace = 741928462
    lock_connection = await engine.connect()
    lock_acquired = False
    try:
        for _attempt in range(120):
            lock_acquired = bool(await lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(:namespace, :account_id)"),
                {"namespace": lock_namespace, "account_id": int(account_id)},
            ))
            if lock_acquired:
                break
            await asyncio.sleep(0.5)
        if not lock_acquired:
            raise TimeoutError("Не удалось получить блокировку Telegram-аккаунта за 60 секунд.")

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
            last_error: Exception | None = None
            for attempt in range(5):
                source_db = None
                snapshot_db = None
                try:
                    source_db = sqlite3.connect(source_path, timeout=30)
                    snapshot_db = sqlite3.connect(snapshot_path, timeout=30)
                    source_db.execute("PRAGMA busy_timeout = 30000")
                    snapshot_db.execute("PRAGMA busy_timeout = 30000")
                    source_db.backup(snapshot_db, pages=128, sleep=0.05)
                    check = snapshot_db.execute("PRAGMA quick_check").fetchone()
                    if not check or check[0] != "ok":
                        raise sqlite3.DatabaseError("SQLite snapshot integrity check failed.")
                    last_error = None
                    break
                except sqlite3.OperationalError as exc:
                    last_error = exc
                    message = str(exc).lower()
                    if attempt == 4 or not any(word in message for word in ("locked", "busy")):
                        raise RuntimeError(
                            f"Не удалось безопасно скопировать SQLite-сессию: {exc}"
                        ) from exc
                    await asyncio.sleep(min(0.25 * (2 ** attempt), 2.0))
                finally:
                    if snapshot_db is not None:
                        snapshot_db.close()
                    if source_db is not None:
                        source_db.close()
            if last_error is not None:
                raise RuntimeError(f"Не удалось создать снимок сессии: {last_error}") from last_error

            client = TelegramClient(
                snapshot_path,
                cfg.telegram_api_id,
                cfg.telegram_api_hash,
                proxy=telethon_proxy(account.proxy_config_encrypted),
                connection_retries=3,
                retry_delay=1,
                auto_reconnect=False,
            )
            configure_telethon_sqlite_session(client)
            client.flood_sleep_threshold = 0
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Сессия аккаунта не авторизована.")
                yield account, client
            finally:
                if client.is_connected():
                    try:
                        await client.disconnect()
                    except Exception:
                        logging.exception("Could not disconnect isolated Telegram client for account %s", account_id)
    finally:
        if lock_acquired:
            try:
                await lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:namespace, :account_id)"),
                    {"namespace": lock_namespace, "account_id": int(account_id)},
                )
                await lock_connection.commit()
            except Exception:
                logging.exception("Could not release Telegram account lock for account %s", account_id)
                await lock_connection.invalidate()
        await lock_connection.close()




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
    from app.services.automation import reserve_operation_slot
    await reserve_operation_slot(account_id, "subscription")
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
    from app.services.automation import reserve_operation_slot
    await reserve_operation_slot(account_id, "subscription")
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
            if not is_group:
                raise ValueError("Список участников можно выгружать только из группы, не из канала.")
            permissions = await client.get_permissions(entity, "me")
            if not (getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False)):
                raise PermissionError("Экспорт участников доступен только администраторам собственной группы.")
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


async def list_account_dialogs(
    account_id: int, *, limit: int = 200, kind: str = "all"
) -> list[dict]:
    """List Telegram dialogs visible to the account, with stable public metadata."""
    if kind not in {"all", "groups", "channels"}:
        raise ValueError("Фильтр должен быть all, groups или channels.")
    limit = max(1, min(int(limit), 500))
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            result = []
            async for dialog in client.iter_dialogs(limit=limit):
                entity = dialog.entity
                is_channel = bool(getattr(entity, "broadcast", False))
                is_group = bool(
                    getattr(entity, "megagroup", False)
                    or getattr(entity, "gigagroup", False)
                    or entity.__class__.__name__ == "Chat"
                )
                if not (is_channel or is_group):
                    continue
                if kind == "groups" and not is_group:
                    continue
                if kind == "channels" and not is_channel:
                    continue
                result.append({
                    "id": int(getattr(entity, "id", 0)),
                    "peer_id": int(dialog.id),
                    "title": str(getattr(entity, "title", "") or getattr(entity, "username", "") or "Без названия"),
                    "username": getattr(entity, "username", None) or "",
                    "kind": "channel" if is_channel else "group",
                    "participants_count": getattr(entity, "participants_count", None),
                    "unread_count": int(getattr(dialog, "unread_count", 0) or 0),
                    "is_muted": bool(getattr(getattr(dialog, "dialog", None), "notify_settings", None)
                                     and getattr(dialog.dialog.notify_settings, "mute_until", None)),
                })
            return result


async def manage_subscriptions_batch(
    account_id: int, links: list[str], *, action: str
) -> dict:
    """Join/leave an explicit list sequentially in one isolated client; never bypass FloodWait."""
    if action not in {"join", "leave"}:
        raise ValueError("Действие должно быть join или leave.")
    normalized = list(dict.fromkeys(str(value).strip() for value in links if str(value).strip()))
    if not normalized or len(normalized) > 20:
        raise ValueError("Укажите от 1 до 20 ссылок за одну операцию.")
    results = []
    flood_wait = 0
    local_limit_wait = 0
    from app.services.automation import OperationRateLimitError, reserve_operation_slot
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            for value in normalized:
                try:
                    try:
                        await reserve_operation_slot(account_id, "subscription")
                    except OperationRateLimitError as limit_error:
                        if "интервал" in str(limit_error).lower() and limit_error.retry_after <= 30:
                            await asyncio.sleep(limit_error.retry_after)
                            await reserve_operation_slot(account_id, "subscription")
                        else:
                            raise
                    if action == "join":
                        lowered = value.lower().rstrip("/")
                        if "t.me/+" in lowered or "t.me/joinchat/" in lowered:
                            invite_hash = value.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1].lstrip("+")
                            if not invite_hash:
                                raise ValueError("Некорректная приватная ссылка.")
                            invite = await client(CheckChatInviteRequest(hash=invite_hash))
                            if isinstance(invite, ChatInviteAlready):
                                results.append({"link": value, "status": "already_member", "error": ""})
                                continue
                            await client(ImportChatInviteRequest(hash=invite_hash))
                        else:
                            target = value.split("t.me/", 1)[1].split("/", 1)[0].split("?", 1)[0] if "t.me/" in lowered else value
                            entity = await client.get_entity(target)
                            await client(JoinChannelRequest(entity))
                    else:
                        target = value.split("t.me/", 1)[1].split("/", 1)[0].split("?", 1)[0] if "t.me/" in value.lower() else value
                        entity = await client.get_entity(target)
                        if entity.__class__.__name__ == "Chat":
                            await client.delete_dialog(entity)
                        else:
                            await client(LeaveChannelRequest(entity))
                    results.append({"link": value, "status": "done", "error": ""})
                except OperationRateLimitError as exc:
                    local_limit_wait = exc.retry_after
                    results.append({"link": value, "status": "local_limit", "error": str(exc)})
                    break
                except FloodWaitError as exc:
                    flood_wait = max(1, int(exc.seconds))
                    results.append({"link": value, "status": "flood_wait", "error": f"{flood_wait}s"})
                    break
                except Exception as exc:
                    results.append({"link": value, "status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:250]}"})
    return {
        "action": action, "processed": len(results), "total": len(normalized),
        "flood_wait_seconds": flood_wait, "local_limit_seconds": local_limit_wait,
        "results": results,
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
    from app.services.automation import reserve_operation_slot
    await reserve_operation_slot(account_id, "reaction")
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


async def resolve_peer_for_consent(account_id: int, peer_ref: str) -> dict:
    """Resolve an explicitly opted-in Telegram peer and retain a reusable reference."""
    value = peer_ref.strip()
    if not value:
        raise ValueError("Укажите @username или ссылку на пользователя.")
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/").split("/", 1)[0]
    value = value.lstrip("@")
    if not value or value.startswith("+") or value.startswith("joinchat"):
        raise ValueError("Для личного согласия нужен публичный username пользователя.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            entity = await client.get_entity(value)
            if not getattr(entity, "id", None) or entity.__class__.__name__ in {"Channel", "Chat"}:
                raise ValueError("Для рассылки можно добавить только личный Telegram-аккаунт.")
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
            }


async def resolve_group_for_consent(account_id: int, group_ref: str) -> dict:
    """Resolve a group where the account is an admin before allowing scheduled publishing."""
    value = group_ref.strip()
    if not value:
        raise ValueError("Укажите @username или ссылку на собственную группу.")
    if "t.me/" in value.lower():
        value = value.split("t.me/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    value = value.lstrip("@")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            target = int(value) if value.lstrip("-").isdigit() else value
            entity = await client.get_entity(target)
            is_group = bool(
                getattr(entity, "megagroup", False)
                or getattr(entity, "gigagroup", False)
                or entity.__class__.__name__ == "Chat"
            )
            if not is_group:
                raise ValueError("Публикация разрешена только в группу, не в личный чат или канал.")
            permissions = await client.get_permissions(entity, "me")
            if not (getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False)):
                raise PermissionError("Аккаунт должен быть администратором группы.")
            username = getattr(entity, "username", None)
            return {
                "peer_id": int(utils.get_peer_id(entity)),
                "peer_ref": f"@{username}" if username else "",
                "peer_label": str(getattr(entity, "title", None) or username or entity.id),
            }


async def resolve_groups_for_consent(account_id: int, group_refs: list[str]) -> list[dict]:
    """Resolve up to 100 operator-selected groups and verify admin rights in one session."""
    values = []
    for raw in group_refs:
        value = raw.strip()
        if "t.me/" in value.lower():
            value = value.split("t.me/", 1)[1].split("/", 1)[0].split("?", 1)[0]
        value = value.lstrip("@")
        if not value:
            raise ValueError(f"Некорректная ссылка на группу: {raw}")
        values.append(value)
    if not values or len(values) > 100:
        raise ValueError("Укажите от 1 до 100 групп.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            result = []
            for value in values:
                target = int(value) if value.lstrip("-").isdigit() else value
                entity = await client.get_entity(target)
                is_group = bool(
                    getattr(entity, "megagroup", False)
                    or getattr(entity, "gigagroup", False)
                    or entity.__class__.__name__ == "Chat"
                )
                if not is_group:
                    raise ValueError(f"@{value} не является группой.")
                permissions = await client.get_permissions(entity, "me")
                if not (getattr(permissions, "is_admin", False) or getattr(permissions, "is_creator", False)):
                    raise PermissionError(f"Аккаунт не является администратором группы @{value}.")
                peer_id = int(utils.get_peer_id(entity))
                username = getattr(entity, "username", None)
                result.append({
                    "peer_id": peer_id,
                    "peer_ref": f"@{username}" if username else "",
                    "peer_label": str(getattr(entity, "title", None) or username or entity.id),
                })
            return result


async def resolve_group_reference(account_id: int, group_ref: str) -> dict:
    """Resolve a group reference for revoking a previously granted publish permission."""
    value = group_ref.strip()
    if "t.me/" in value.lower():
        value = value.split("t.me/", 1)[1].split("/", 1)[0].split("?", 1)[0]
    value = value.lstrip("@")
    if not value:
        raise ValueError("Укажите группу.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            target = int(value) if value.lstrip("-").isdigit() else value
            entity = await client.get_entity(target)
            is_group = bool(
                getattr(entity, "megagroup", False)
                or getattr(entity, "gigagroup", False)
                or entity.__class__.__name__ == "Chat"
            )
            if not is_group:
                raise ValueError("Это не группа.")
            peer_id = int(utils.get_peer_id(entity))
            username = getattr(entity, "username", None)
            return {"peer_id": peer_id, "peer_ref": f"@{username}" if username else "",
                    "peer_label": str(getattr(entity, "title", None) or username or entity.id)}


async def resolve_peers_for_consent(account_id: int, peer_refs: list[str]) -> list[dict]:
    """Resolve several explicitly selected personal accounts in one isolated session."""
    values = []
    for raw in peer_refs:
        value = raw.strip()
        if value.startswith("https://t.me/"):
            value = value.removeprefix("https://t.me/").split("/", 1)[0]
        value = value.lstrip("@")
        if not value or value.startswith("+") or value.startswith("joinchat"):
            raise ValueError(f"Некорректный username получателя: {raw}")
        values.append(value)
    if not values or len(values) > 500:
        raise ValueError("Укажите от 1 до 500 получателей за одну задачу.")
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            result = []
            for value in values:
                entity = await client.get_entity(value)
                if not getattr(entity, "id", None) or entity.__class__.__name__ in {"Channel", "Chat"}:
                    raise ValueError(f"@{value} не является личным Telegram-аккаунтом.")
                username = getattr(entity, "username", None)
                if not username:
                    raise ValueError(f"У @{value} нет публичного username.")
                result.append({
                    "peer_id": int(entity.id),
                    "peer_ref": f"@{username}",
                    "peer_label": " ".join(filter(None, [
                        getattr(entity, "first_name", None),
                        getattr(entity, "last_name", None),
                    ])) or f"@{username}",
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
    "import_contacts", "list_contacts", "list_account_dialogs", "manage_subscriptions_batch",
    "parse_chat_history", "parse_chat_participants", "react_to_message",
    "resolve_peer_for_consent", "resolve_peers_for_consent", "resolve_group_for_consent",
    "resolve_groups_for_consent", "resolve_group_reference", "set_profile_photo",
    "simulate_post_activity",
    "update_profile",
]
