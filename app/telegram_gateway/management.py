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
import re
import secrets
import sqlite3
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.tl.functions.contacts import DeleteContactsRequest, GetBlockedRequest, ImportContactsRequest
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


def _copy_sqlite_session(source_path: str, snapshot_path: str) -> None:
    """Create and integrity-check a private SQLite session snapshot with bounded busy retries."""
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
            return
        except sqlite3.OperationalError as exc:
            last_error = exc
            message = str(exc).lower()
            if attempt == 4 or not any(word in message for word in ("locked", "busy")):
                raise RuntimeError(f"Не удалось безопасно скопировать SQLite-сессию: {exc}") from exc
            time.sleep(min(0.25 * (2 ** attempt), 2.0))
        finally:
            if snapshot_db is not None:
                snapshot_db.close()
            if source_db is not None:
                source_db.close()
    if last_error is not None:
        raise RuntimeError(f"Не удалось создать снимок сессии: {last_error}") from last_error


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
            await asyncio.to_thread(_copy_sqlite_session, source_path, snapshot_path)

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


async def _sync_cached_account_profile(account_id: int, profile: dict) -> None:
    """Keep the account picker in sync with Telegram's authoritative profile."""
    username = (profile.get("username") or "").strip().lstrip("@") or None
    display_name = " ".join(
        value.strip() for value in (
            str(profile.get("first_name") or ""),
            str(profile.get("last_name") or ""),
        ) if value and value.strip()
    ).strip()
    if not display_name:
        display_name = f"@{username}" if username else f"Аккаунт #{int(account_id)}"
    async with SessionFactory() as db:
        account = await db.get(Account, int(account_id))
        if account:
            account.username = username
            account.display_name = display_name[:160]
            await db.commit()


async def get_profile(account_id: int) -> dict:
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            me = await client.get_me()
            full = await client(GetFullUserRequest(me))
            profile = {
                "id": int(me.id),
                "username": me.username,
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
                "phone": me.phone or "",
                "about": full.full_user.about or "",
            }
        await _sync_cached_account_profile(account_id, profile)
        return profile


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
        if username and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
            raise ValueError(
                "Username должен начинаться с латинской буквы и содержать 5–32 символа: "
                "латинские буквы, цифры или подчёркивание."
            )
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
            profile = {
                "username": me.username,
                "first_name": me.first_name or "",
                "last_name": me.last_name or "",
            }
        await _sync_cached_account_profile(account_id, profile)
        return profile


async def set_profile_photo(account_id: int, photo_path: str) -> None:
    """Normalize the supplied image to JPEG and upload it as the Telegram profile photo."""
    if not os.path.isfile(photo_path):
        raise ValueError("Файл фотографии не найден.")
    if os.path.getsize(photo_path) > 10 * 1024 * 1024:
        raise ValueError("Размер фотографии не должен превышать 10 МБ.")

    from PIL import Image, ImageOps

    with tempfile.TemporaryDirectory(prefix=f"atciv-profile-photo-{int(account_id)}-") as temp_dir:
        jpeg_path = os.path.join(temp_dir, "profile.jpg")
        try:
            with Image.open(photo_path) as source:
                image = ImageOps.exif_transpose(source)
                if getattr(image, "is_animated", False):
                    image.seek(0)
                image.convert("RGB").save(
                    jpeg_path, format="JPEG", quality=92, optimize=True,
                )
        except Exception as exc:
            raise ValueError(f"Не удалось прочитать изображение: {type(exc).__name__}: {exc}") from exc

        if os.path.getsize(jpeg_path) > 10 * 1024 * 1024:
            raise ValueError("После обработки изображение превышает 10 МБ.")

        async with _account_lock(account_id):
            async with isolated_account_client(account_id) as (_account, client):
                await client.upload_profile_photo(jpeg_path)
                uploaded = await client.get_profile_photos("me", limit=1)
                if not uploaded:
                    raise RuntimeError("Telegram не подтвердил наличие фотографии профиля.")


async def publish_story_photo(account_id: int, photo_path: str, *, caption: str = "") -> None:
    """Publish one operator-supplied photo as a 24-hour Telegram story for contacts only.

    Eligibility is checked with stories.canSendStory before upload. Telegram remains
    authoritative for Premium requirements, active-story limits and weekly/monthly caps.
    """
    if not os.path.isfile(photo_path):
        raise ValueError("Файл истории не найден.")
    if os.path.getsize(photo_path) > 20 * 1024 * 1024:
        raise ValueError("Фото для истории не должно превышать 20 МБ.")
    if len(caption) > 2048:
        raise ValueError("Подпись к истории не должна превышать 2048 символов.")

    from PIL import Image
    try:
        with Image.open(photo_path) as image:
            if image.width > image.height:
                raise ValueError("Для истории отправьте вертикальное изображение.")
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Поддерживаются JPG, PNG и WEBP.")
            image.verify()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Не удалось прочитать изображение истории: {type(exc).__name__}: {exc}") from exc

    # Imported lazily so accounts remain manageable on Telethon builds without Stories TL.
    try:
        from telethon.tl.functions.stories import CanSendStoryRequest, SendStoryRequest
        from telethon.tl.types import InputMediaUploadedPhoto, InputPrivacyValueAllowContacts
    except ImportError as exc:
        raise RuntimeError("Установленная версия Telethon не поддерживает публикацию историй.") from exc

    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            peer = await client.get_input_entity("me")
            availability = await client(CanSendStoryRequest(peer=peer))
            if int(getattr(availability, "count_remains", getattr(availability, "count", 0)) or 0) < 1:
                raise RuntimeError("Telegram не разрешает добавить историю: нет доступных слотов.")
            uploaded = await client.upload_file(photo_path)
            await client(SendStoryRequest(
                peer=peer,
                media=InputMediaUploadedPhoto(file=uploaded),
                caption=caption or None,
                privacy_rules=[InputPrivacyValueAllowContacts()],
                random_id=secrets.randbits(63),
                period=86400,
            ))


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
    """Join one explicit public chat or invite link, respecting global subscription policy."""
    value = link.strip()
    if not value:
        raise ValueError("Укажите ссылку или @username.")
    from app.services.automation import record_module_floodwait, reserve_operation_slot
    await reserve_operation_slot(account_id, "subscription")
    try:
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
    except FloodWaitError as exc:
        await record_module_floodwait(account_id, "subscription", max(1, int(exc.seconds)))
        raise


async def leave_chat(account_id: int, link: str) -> None:
    """Leave one explicitly selected chat, respecting global subscription policy."""
    value = link.strip()
    if not value:
        raise ValueError("Укажите ссылку или @username.")
    from app.services.automation import record_module_floodwait, reserve_operation_slot
    await reserve_operation_slot(account_id, "subscription")
    try:
        async with _account_lock(account_id):
            async with isolated_account_client(account_id) as (_account, client):
                target = value.split("t.me/", 1)[1].split("/", 1)[0].split("?", 1)[0] if "t.me/" in value.lower() else value
                entity = await client.get_entity(target)
                if entity.__class__.__name__ == "Chat":
                    await client.delete_dialog(entity)
                else:
                    await client(LeaveChannelRequest(entity))
    except FloodWaitError as exc:
        await record_module_floodwait(account_id, "subscription", max(1, int(exc.seconds)))
        raise


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


async def _blocked_user_ids(client) -> set[int]:
    """Return only the account's own Telegram block list."""
    blocked_ids: set[int] = set()
    offset = 0
    while True:
        page = await client(GetBlockedRequest(offset=offset, limit=100))
        items = list(getattr(page, "blocked", []) or [])
        for item in items:
            user_id = getattr(getattr(item, "peer", None), "user_id", None)
            if user_id:
                blocked_ids.add(int(user_id))
        if len(items) < 100:
            break
        offset += len(items)
    return blocked_ids


def _profile_matches_parser_filters(
    user, *, exclude_bots: bool = False, exclude_deleted: bool = False,
    exclude_scam: bool = False, exclude_blocked: bool = False,
    username_only: bool = False, photo_only: bool = False,
    premium_only: bool = False, active_days: int = 0,
    blocked_ids: set[int] | None = None,
) -> bool:
    """Apply only explicit Telegram profile fields; do not infer demographics or location."""
    if user is None or not getattr(user, "id", None):
        return False
    if exclude_bots and bool(getattr(user, "bot", False)):
        return False
    if exclude_deleted and bool(getattr(user, "deleted", False)):
        return False
    if exclude_scam and bool(getattr(user, "scam", False)):
        return False
    if exclude_blocked and int(user.id) in (blocked_ids or set()):
        return False
    if username_only and not getattr(user, "username", None):
        return False
    if photo_only and not getattr(user, "photo", None):
        return False
    if premium_only and not bool(getattr(user, "premium", False)):
        return False
    if active_days:
        from datetime import datetime, timedelta, timezone
        status = getattr(user, "status", None)
        last_seen = getattr(status, "was_online", None) or getattr(status, "expires", None)
        if last_seen is None:
            return False
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen < datetime.now(timezone.utc) - timedelta(days=int(active_days)):
            return False
    return True


async def _sender_passes_parser_filters(
    message, client, *, exclude_bots: bool = False, exclude_deleted: bool = False,
    exclude_scam: bool = False, exclude_blocked: bool = False,
    username_only: bool = False, photo_only: bool = False,
    premium_only: bool = False, active_days: int = 0,
    blocked_ids: set[int] | None = None,
) -> bool:
    enabled = any((
        exclude_bots, exclude_deleted, exclude_scam, exclude_blocked,
        username_only, photo_only, premium_only, active_days,
    ))
    if not enabled:
        return True
    try:
        sender = await message.get_sender()
    except Exception:
        sender = None
    return _profile_matches_parser_filters(
        sender, exclude_bots=exclude_bots, exclude_deleted=exclude_deleted,
        exclude_scam=exclude_scam, exclude_blocked=exclude_blocked,
        username_only=username_only, photo_only=photo_only,
        premium_only=premium_only, active_days=active_days,
        blocked_ids=blocked_ids,
    )


async def parse_chat_history(
    account_id: int, channel: str, *, limit: int = 20, days: int = 0,
    keywords: list[str] | None = None, include_comments: bool = False,
    comment_limit: int = 20, message_delay_seconds: int = 0,
    exclude_bots: bool = False, exclude_deleted: bool = False,
    exclude_scam: bool = False, exclude_blocked: bool = False,
    username_only: bool = False, photo_only: bool = False,
    premium_only: bool = False, active_days: int = 0,
    blocked_ids: set[int] | None = None,
) -> dict:
    """Parse accessible message history with optional date/keyword filters and comments."""
    from datetime import datetime, timedelta, timezone

    limit = max(1, min(int(limit), 500))
    days = max(0, min(int(days), 365))
    comment_limit = max(1, min(int(comment_limit), 100))
    message_delay_seconds = max(0, min(int(message_delay_seconds), 9999))
    terms = [str(term).strip().casefold() for term in (keywords or []) if str(term).strip()]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None
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
            scanned = 0
            blocked_ids = (
                set(blocked_ids) if blocked_ids is not None
                else (await _blocked_user_ids(client) if exclude_blocked else set())
            )
            async for message in client.iter_messages(entity, limit=limit):
                if message_delay_seconds:
                    await asyncio.sleep(message_delay_seconds)
                if not message or getattr(message, "action", None):
                    continue
                scanned += 1
                if not await _sender_passes_parser_filters(
                    message, client, exclude_bots=exclude_bots,
                    exclude_deleted=exclude_deleted, exclude_scam=exclude_scam,
                    exclude_blocked=exclude_blocked, username_only=username_only,
                    photo_only=photo_only, premium_only=premium_only,
                    active_days=active_days, blocked_ids=blocked_ids,
                ):
                    continue
                message_date = message.date
                if message_date and message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)
                if cutoff and (not message_date or message_date < cutoff):
                    continue
                body = (message.message or "").strip()
                links = list(dict.fromkeys(re.findall(r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/[^\s<>()]+|https?://[^\s<>()]+", body)))
                if terms and not any(term in body.casefold() for term in terms):
                    continue
                reaction_items = []
                reactions = getattr(message, "reactions", None)
                for item in getattr(reactions, "results", []) or []:
                    reaction = getattr(item, "reaction", None)
                    reaction_items.append({
                        "emoji": getattr(reaction, "emoticon", None) or getattr(reaction, "document_id", None),
                        "count": int(getattr(item, "count", 0) or 0),
                    })
                comments = []
                if include_comments and int(getattr(getattr(message, "replies", None), "replies", 0) or 0):
                    try:
                        async for comment in client.iter_messages(
                            entity, reply_to=int(message.id), limit=comment_limit
                        ):
                            if not comment or getattr(comment, "action", None):
                                continue
                            comments.append({
                                "id": int(comment.id),
                                "date": comment.date.isoformat() if comment.date else "",
                                "sender_id": int(comment.sender_id or 0),
                                "text": (comment.message or "")[:2000],
                            })
                    except Exception as exc:
                        logging.info(
                            "Could not read comments for account=%s chat=%s post=%s: %s",
                            account_id, getattr(entity, "id", None), message.id, type(exc).__name__,
                        )
                posts.append({
                    "id": int(message.id),
                    "date": message.date.isoformat() if message.date else "",
                    "text": body[:3000],
                    "links": links,
                    "views": int(message.views or 0),
                    "forwards": int(message.forwards or 0),
                    "replies": int(getattr(getattr(message, "replies", None), "replies", 0) or 0),
                    "reactions": reaction_items,
                    "has_media": bool(message.media),
                    "comments": comments,
                })
            return {
                "channel_id": int(entity.id),
                "title": str(getattr(entity, "title", "") or getattr(entity, "username", "") or channel),
                "username": getattr(entity, "username", None),
                "posts": posts,
                "scanned": scanned,
                "filters": {
                    "days": days, "keywords": terms, "comments": include_comments,
                    "message_delay_seconds": message_delay_seconds,
                    "exclude_bots": exclude_bots, "exclude_deleted": exclude_deleted,
                    "exclude_scam": exclude_scam, "exclude_blocked": exclude_blocked,
                    "username_only": username_only, "photo_only": photo_only,
                    "premium_only": premium_only, "active_days": active_days,
                },
            }



async def parse_account_history(
    account_id: int, *, limit_per_chat: int = 100, days: int = 0,
    keywords: list[str] | None = None, include_comments: bool = False,
    comment_limit: int = 20, message_delay_seconds: int = 0,
    chat_delay_seconds: int = 0, max_chats: int = 500,
    exclude_bots: bool = False, exclude_deleted: bool = False,
    exclude_scam: bool = False, exclude_blocked: bool = False,
    username_only: bool = False, photo_only: bool = False,
    premium_only: bool = False, active_days: int = 0,
) -> dict:
    """Parse visible history across this account's own dialogs, isolating each chat."""
    max_chats = max(1, min(int(max_chats), 500))
    chat_delay_seconds = max(0, min(int(chat_delay_seconds), 9999))
    dialogs = await list_account_dialogs(account_id, limit=500, kind="all")
    selected = dialogs[:max_chats]
    shared_blocked_ids = None
    if exclude_blocked:
        async with _account_lock(account_id):
            async with isolated_account_client(account_id) as (_account, client):
                shared_blocked_ids = await _blocked_user_ids(client)
    results = []
    errors = []
    for index, dialog in enumerate(selected):
        if index and chat_delay_seconds:
            await asyncio.sleep(chat_delay_seconds)
        target = dialog.get("peer_id") or dialog.get("id")
        try:
            parsed = await parse_chat_history(
                account_id, str(target), limit=limit_per_chat, days=days,
                keywords=keywords, include_comments=include_comments,
                comment_limit=comment_limit,
                message_delay_seconds=message_delay_seconds,
                exclude_bots=exclude_bots, exclude_deleted=exclude_deleted,
                exclude_scam=exclude_scam, exclude_blocked=exclude_blocked,
                username_only=username_only, photo_only=photo_only,
                premium_only=premium_only, active_days=active_days,
                blocked_ids=shared_blocked_ids,
            )
            results.append(parsed)
        except FloodWaitError:
            raise
        except Exception as exc:
            logging.warning(
                "History parser skipped account=%s chat=%s: %s",
                account_id, target, type(exc).__name__,
            )
            errors.append({
                "chat_id": int(dialog.get("id") or 0),
                "title": str(dialog.get("title") or target),
                "error": f"{type(exc).__name__}: {str(exc)[:180]}",
            })
    return {
        "account_id": int(account_id),
        "chats_scanned": len(selected),
        "chats_parsed": len(results),
        "messages_count": sum(len(item["posts"]) for item in results),
        "results": results,
        "errors": errors,
        "filters": {
            "days": max(0, min(int(days), 365)),
            "keywords": [str(term).strip().casefold() for term in (keywords or []) if str(term).strip()],
            "comments": bool(include_comments),
            "message_delay_seconds": max(0, min(int(message_delay_seconds), 9999)),
            "chat_delay_seconds": chat_delay_seconds,
        },
    }


async def parse_chat_comments(
    account_id: int, channel: str, *, post_limit: int = 100,
    comment_limit: int = 100, days: int = 0,
    keywords: list[str] | None = None, comment_delay_seconds: int = 0,
    exclude_bots: bool = False, exclude_deleted: bool = False,
    exclude_scam: bool = False, exclude_blocked: bool = False,
    username_only: bool = False, photo_only: bool = False,
    premium_only: bool = False, active_days: int = 0,
) -> dict:
    """Export accessible public discussion comments with bounded filters and delays."""
    from datetime import datetime, timedelta, timezone

    post_limit = max(1, min(int(post_limit), 500))
    comment_limit = max(1, min(int(comment_limit), 500))
    days = max(0, min(int(days), 365))
    comment_delay_seconds = max(0, min(int(comment_delay_seconds), 9999))
    terms = [str(term).strip().casefold() for term in (keywords or []) if str(term).strip()]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None

    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            entity = await client.get_entity(channel.strip())
            if not (
                getattr(entity, "broadcast", False)
                or getattr(entity, "megagroup", False)
                or entity.__class__.__name__ == "Chat"
            ):
                raise ValueError("Укажите канал или группу, комментарии которой доступны аккаунту.")

            blocked_ids = await _blocked_user_ids(client) if exclude_blocked else set()
            comments = []
            scanned_posts = 0
            scanned_comments = 0
            async for post in client.iter_messages(entity, limit=post_limit):
                if not post or getattr(post, "action", None):
                    continue
                post_date = post.date
                if post_date and post_date.tzinfo is None:
                    post_date = post_date.replace(tzinfo=timezone.utc)
                if cutoff and (not post_date or post_date < cutoff):
                    break
                scanned_posts += 1
                reply_count = int(getattr(getattr(post, "replies", None), "replies", 0) or 0)
                if not reply_count:
                    continue
                try:
                    async for comment in client.iter_messages(
                        entity, reply_to=int(post.id), limit=comment_limit,
                    ):
                        if not comment or getattr(comment, "action", None):
                            continue
                        scanned_comments += 1
                        if comment_delay_seconds:
                            await asyncio.sleep(comment_delay_seconds)
                        comment_date = comment.date
                        if comment_date and comment_date.tzinfo is None:
                            comment_date = comment_date.replace(tzinfo=timezone.utc)
                        if cutoff and (not comment_date or comment_date < cutoff):
                            continue
                        body = (comment.message or "").strip()
                        if terms and not any(term in body.casefold() for term in terms):
                            continue
                        if not await _sender_passes_parser_filters(
                            comment, client, exclude_bots=exclude_bots,
                            exclude_deleted=exclude_deleted, exclude_scam=exclude_scam,
                            exclude_blocked=exclude_blocked, username_only=username_only,
                            photo_only=photo_only, premium_only=premium_only,
                            active_days=active_days, blocked_ids=blocked_ids,
                        ):
                            continue
                        comments.append({
                            "post_id": int(post.id),
                            "post_date": post.date.isoformat() if post.date else "",
                            "comment_id": int(comment.id),
                            "date": comment.date.isoformat() if comment.date else "",
                            "sender_id": int(comment.sender_id or 0),
                            "text": body[:3000],
                        })
                except FloodWaitError:
                    raise
                except Exception as exc:
                    logging.info(
                        "Could not read discussion comments for account=%s chat=%s post=%s: %s",
                        account_id, getattr(entity, "id", None), post.id, type(exc).__name__,
                    )
                    continue
            return {
                "channel_id": int(entity.id),
                "title": str(getattr(entity, "title", "") or getattr(entity, "username", "") or channel),
                "username": getattr(entity, "username", None),
                "scanned_posts": scanned_posts,
                "scanned_comments": scanned_comments,
                "visible_count": len(comments),
                "comments": comments,
                "filters": {
                    "post_limit": post_limit, "comment_limit": comment_limit,
                    "days": days, "keywords": terms,
                    "comment_delay_seconds": comment_delay_seconds,
                    "exclude_bots": exclude_bots, "exclude_deleted": exclude_deleted,
                    "exclude_scam": exclude_scam, "exclude_blocked": exclude_blocked,
                    "username_only": username_only, "photo_only": photo_only,
                    "premium_only": premium_only, "active_days": active_days,
                },
            }


async def parse_chat_participants(
    account_id: int, chat: str, *, limit: int = 200,
    exclude_bots: bool = False, exclude_deleted: bool = False,
    username_only: bool = False, photo_only: bool = False,
    premium_only: bool = False, active_days: int = 0, exclude_scam: bool = False,
    exclude_blocked: bool = False,
) -> dict:
    """Export visible participant metadata with opt-in filters; never infer personal attributes."""
    from datetime import datetime, timedelta, timezone

    limit = max(1, min(int(limit), 500))
    active_days = max(0, min(int(active_days), 365))
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
            scanned = 0
            activity_unknown = 0
            blocked_ids: set[int] = set()
            if exclude_blocked:
                offset = 0
                while True:
                    blocked_page = await client(GetBlockedRequest(offset=offset, limit=100))
                    blocked_items = list(getattr(blocked_page, "blocked", []) or [])
                    for blocked_item in blocked_items:
                        peer = getattr(blocked_item, "peer", None)
                        blocked_id = getattr(peer, "user_id", None)
                        if blocked_id:
                            blocked_ids.add(int(blocked_id))
                    if len(blocked_items) < 100:
                        break
                    offset += len(blocked_items)
            cutoff = datetime.now(timezone.utc) - timedelta(days=active_days) if active_days else None
            async for user in client.iter_participants(entity, limit=limit):
                if not getattr(user, "id", None):
                    continue
                scanned += 1
                is_bot = bool(getattr(user, "bot", False))
                is_deleted = bool(getattr(user, "deleted", False))
                is_scam = bool(getattr(user, "scam", False))
                is_blocked = int(user.id) in blocked_ids
                username = getattr(user, "username", None) or ""
                if exclude_bots and is_bot:
                    continue
                if exclude_deleted and is_deleted:
                    continue
                if exclude_scam and is_scam:
                    continue
                if exclude_blocked and is_blocked:
                    continue
                if username_only and not username:
                    continue
                if photo_only and not getattr(user, "photo", None):
                    continue
                if premium_only and not bool(getattr(user, "premium", False)):
                    continue
                last_seen = None
                status = getattr(user, "status", None)
                if status is not None:
                    last_seen = getattr(status, "was_online", None) or getattr(status, "expires", None)
                if cutoff:
                    if last_seen is None:
                        activity_unknown += 1
                        continue
                    if last_seen.tzinfo is None:
                        last_seen = last_seen.replace(tzinfo=timezone.utc)
                    if last_seen < cutoff:
                        continue
                participants.append({
                    "id": int(user.id),
                    "username": username,
                    "first_name": getattr(user, "first_name", None) or "",
                    "last_name": getattr(user, "last_name", None) or "",
                    "is_bot": is_bot,
                    "is_deleted": is_deleted,
                    "is_scam": is_scam,
                    "is_blocked": is_blocked,
                    "has_photo": bool(getattr(user, "photo", None)),
                    "is_premium": bool(getattr(user, "premium", False)),
                    "last_seen": last_seen.isoformat() if last_seen else "",
                })
            return {
                "chat_id": int(entity.id),
                "title": str(getattr(entity, "title", "") or getattr(entity, "username", "") or target),
                "username": getattr(entity, "username", None),
                "visible_count": len(participants),
                "scanned_count": scanned,
                "activity_unknown_count": activity_unknown,
                "requested_limit": limit,
                "filters": {
                    "exclude_bots": exclude_bots, "exclude_deleted": exclude_deleted,
                    "exclude_scam": exclude_scam, "exclude_blocked": exclude_blocked,
                    "username_only": username_only, "photo_only": photo_only,
                    "premium_only": premium_only, "active_days": active_days,
                },
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
                    from app.services.automation import record_module_floodwait
                    await record_module_floodwait(account_id, "subscription", flood_wait)
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
    from app.services.automation import record_module_floodwait, reserve_operation_slot
    await reserve_operation_slot(account_id, "reaction")
    try:
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
    except FloodWaitError as exc:
        await record_module_floodwait(account_id, "reaction", max(1, int(exc.seconds)))
        raise


async def resolve_peer(account_id: int, peer_ref: str) -> dict:
    """Resolve an explicitly selected Telegram peer and retain a reusable reference."""
    value = peer_ref.strip()
    if not value:
        raise ValueError("Укажите @username или ссылку на пользователя.")
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/").split("/", 1)[0]
    value = value.lstrip("@")
    if not value or value.startswith("+") or value.startswith("joinchat"):
        raise ValueError("Для личной рассылки укажите публичный username пользователя.")
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


async def resolve_group(account_id: int, group_ref: str) -> dict:
    """Resolve a selected group and verify the account is an administrator."""
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
                "is_megagroup": bool(getattr(entity, "megagroup", False)),
            }


async def resolve_group_for_direct_invite(account_id: int, group_ref: str) -> dict:
    """Resolve a supergroup and verify Telegram's invite_users permission."""
    group = await resolve_group(account_id, group_ref)
    if not group.get("is_megagroup"):
        raise ValueError("Прямое добавление доступно только в супергруппы.")
    from app.services.automation import _has_invite_permission
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            target = await client.get_entity(group["peer_id"])
            permissions = await client.get_permissions(target, "me")
            if not _has_invite_permission(permissions):
                raise PermissionError(
                    "У аккаунта нет права приглашать участников (invite_users) в этой группе."
                )
    return group


async def resolve_groups(account_id: int, group_refs: list[str]) -> list[dict]:
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
    if not values or len(values) > 500:
        raise ValueError("Укажите от 1 до 500 групп.")
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


async def resolve_peers(account_id: int, peer_refs: list[str]) -> list[dict]:
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


__all__ = [
    "ChatOverview", "FloodWaitError", "add_contact", "delete_contact",
    "get_chat_overview", "get_profile", "join_chat", "leave_chat",
    "import_contacts", "list_contacts", "list_account_dialogs", "manage_subscriptions_batch",
    "parse_account_history", "parse_chat_history", "parse_chat_participants", "publish_story_photo", "react_to_message",
    "resolve_peer", "resolve_peers", "resolve_group",
    "resolve_groups", "resolve_group_reference", "set_profile_photo",
    "update_profile",
]
