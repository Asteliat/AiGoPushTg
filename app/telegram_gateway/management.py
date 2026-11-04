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
from typing import AsyncIterator

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.types import InputPeerChannel, InputPeerChat

from app.settings import get_settings
from sqlalchemy import select, text

from app.storage.db import SessionFactory, engine
from app.storage.models import Account, ChatBinding
from app.telegram_gateway.proxy import telethon_proxy
from app.telegram_gateway.session import configure_telethon_sqlite_session


_ACCOUNT_LOCKS: dict[int, asyncio.Lock] = {}
cfg = get_settings()


@dataclass(frozen=True)
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
                # Telegram's UpdateProfile response is not enough to tell the user
                # that the requested Bio is actually visible. Read it back from
                # account.getFullUser and fail instead of reporting a false success.
                if about is not None:
                    # Telegram may apply profile changes asynchronously. Poll the
                    # authoritative full-user profile for up to 20 seconds.
                    deadline = time.monotonic() + 20
                    actual_about = full.full_user.about or ""
                    while True:
                        await asyncio.sleep(1 if actual_about != about else 0)
                        updated_full = await client(GetFullUserRequest(await client.get_me()))
                        actual_about = updated_full.full_user.about or ""
                        if actual_about == about:
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeError(
                                "Telegram не подтвердил изменение Bio за 20 секунд. "
                                f"Ожидалось {len(about)} символов, сохранено {len(actual_about)}."
                            )
                        await asyncio.sleep(min(2, remaining))
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
                uploaded_file = await client.upload_file(jpeg_path)
                from telethon.tl.functions.photos import UploadProfilePhotoRequest

                await client(UploadProfilePhotoRequest(file=uploaded_file))
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


async def verify_chat_membership(client, entity) -> bool:
    """Verify that the exact authenticated Telegram user is an active chat participant."""
    me = await client.get_me()
    if me is None or not getattr(me, "id", None):
        raise RuntimeError("Telegram не вернул текущий аккаунт для проверки членства.")
    me_id = int(me.id)

    # channels.getParticipant only accepts channels/supergroups. Basic groups use
    # messages.getFullChat under Telethon's participant iterator.
    if entity.__class__.__name__ == "Chat":
        participants = await client.get_participants(entity, limit=200)
        return any(int(getattr(user, "id", 0) or 0) == me_id for user in participants)

    from telethon.tl.types import InputPeerSelf
    try:
        result = await client(GetParticipantRequest(
            channel=entity,
            participant=InputPeerSelf(),
        ))
    except Exception as exc:
        if type(exc).__name__ in {
            "UserNotParticipantError",
            "ParticipantIdInvalidError",
        }:
            return False
        raise

    participant = getattr(result, "participant", None)
    if participant is None:
        return False
    participant_type = type(participant).__name__
    if participant_type in {"ChannelParticipantLeft", "ChannelParticipantBanned"}:
        return False
    if bool(getattr(participant, "left", False)) or bool(getattr(participant, "kicked", False)):
        return False
    return True


async def verify_channel_membership(client, entity) -> bool:
    """Verify broadcast-channel membership using the authenticated user entity.

    Do not use InputPeerSelf here: Telegram's channel participant lookup can reject
    that peer form for broadcast channels even when the account has joined.
    """
    me = await client.get_me()
    if me is None or not getattr(me, "id", None):
        raise RuntimeError("Telegram не вернул текущий аккаунт для проверки подписки.")
    try:
        await client.get_permissions(entity, me)
        return True
    except Exception as exc:
        if type(exc).__name__ in {"UserNotParticipantError", "ParticipantIdInvalidError"}:
            return False
        raise


def _normalize_manual_message_target(target: str):
    value = str(target or "").strip()
    if not value or len(value) > 256:
        raise ValueError("Укажите @username, ссылку t.me или ID чата.")
    if value.startswith(("https://t.me/", "http://t.me/", "t.me/")):
        value = value.removeprefix("https://").removeprefix("http://")
        path = value.removeprefix("t.me/").rstrip("/")
        if path.startswith(("+", "joinchat/")):
            raise ValueError("Ссылки-приглашения не подходят. Сначала вступите в чат.")
        if path.startswith("c/"):
            raise ValueError("Для приватной ссылки t.me/c укажите ID чата из списка диалогов.")
        value = "@" + path.split("/", 1)[0].lstrip("@")
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


def _bound_chat_input_peer(binding):
    """Build a Telegram input peer from the stable ID and access hash saved at join time."""
    bare_id, peer = utils.resolve_id(int(binding.chat_id))
    if binding.peer_type == "chat":
        return InputPeerChat(chat_id=bare_id)
    if binding.peer_type == "channel" and binding.access_hash is not None:
        return InputPeerChannel(channel_id=bare_id, access_hash=int(binding.access_hash))
    return None


async def _resolve_manual_message_peer(client, account_id: int, peer):
    """Resolve a peer normally, then fall back to the durable binding for private groups."""
    resolution_error = None
    try:
        entity = await client.get_entity(peer)
        if entity is not None:
            return entity, None
    except (ValueError, TypeError) as exc:
        resolution_error = exc

    if isinstance(peer, int):
        async with SessionFactory() as db:
            binding = await db.scalar(select(ChatBinding).where(
                ChatBinding.account_id == int(account_id),
                ChatBinding.chat_id == int(peer),
            ))
            if binding:
                input_peer = _bound_chat_input_peer(binding)
                if input_peer is not None:
                    # Resolve the persisted InputPeer back to a full Telegram entity.
                    # Returning InputPeerChannel directly is unsafe for callers that
                    # need entity metadata (id/title/permissions) and can fail inside
                    # Telethon's send_message path when the entity cache is stale.
                    try:
                        entity = await client.get_entity(input_peer)
                        if entity is not None:
                            return entity, binding.title
                    except (ValueError, TypeError) as exc:
                        resolution_error = exc

                # Also refresh from the account's live dialogs. This repairs legacy
                # bindings without an access hash and stale hashes after migrations.
                async for dialog in client.iter_dialogs():
                    candidate = dialog.entity
                    if int(utils.get_peer_id(candidate)) != int(peer):
                        continue
                    access_hash = getattr(candidate, "access_hash", None)
                    if access_hash is not None:
                        binding.access_hash = int(access_hash)
                        binding.peer_type = "channel"
                        await db.commit()
                        return candidate, binding.title
                    if candidate.__class__.__name__ == "Chat":
                        binding.peer_type = "chat"
                        await db.commit()
                        return candidate, binding.title
                raise ValueError(
                    "Не удалось восстановить доступ к этой группе. "
                    "Повторно вступите в неё по ссылке-приглашению через бота."
                ) from resolution_error

    if resolution_error:
        raise ValueError(
            "Не удалось разрешить чат. Если это приватная группа, повторно добавьте её "
            "через ссылку-приглашение в карточке аккаунта."
        ) from resolution_error
    raise ValueError("Telegram не вернул объект чата. Проверьте ID и членство аккаунта.")


async def preview_message_target(account_id: int, target: str) -> dict:
    """Resolve one explicitly selected Telegram peer before a manual message is sent."""
    value = _normalize_manual_message_target(target)
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            entity, saved_title = await _resolve_manual_message_peer(client, account_id, value)
            title = (
                getattr(entity, "title", None)
                or saved_title
                or " ".join(filter(None, [
                    getattr(entity, "first_name", None),
                    getattr(entity, "last_name", None),
                ]))
                or (f"@{entity.username}" if getattr(entity, "username", None) else str(getattr(entity, "id", value)))
            )
            kind = "группа" if (
                getattr(entity, "megagroup", False)
                or getattr(entity, "gigagroup", False)
                or entity.__class__.__name__ in {"Chat", "InputPeerChat", "InputPeerChannel"}
            ) else "личный чат"
            peer_id = int(getattr(entity, "id", value))
            return {
                "title": str(title)[:200],
                "peer_id": peer_id,
                "username": str(getattr(entity, "username", None) or ""),
                "kind": kind,
            }


async def send_message_to_chat(account_id: int, target: str, message: str) -> dict:
    """Send one operator-authored message to one explicitly resolved Telegram chat."""
    value = str(message or "").strip()
    if not value:
        raise ValueError("Текст сообщения не может быть пустым.")
    if len(value) > 4096:
        raise ValueError("Сообщение не должно превышать 4096 символов.")
    peer = _normalize_manual_message_target(target)
    async with _account_lock(account_id):
        async with isolated_account_client(account_id) as (_account, client):
            entity, saved_title = await _resolve_manual_message_peer(client, account_id, peer)
            if bool(getattr(entity, "broadcast", False)) and not bool(getattr(entity, "megagroup", False)):
                raise ValueError("Отправка в вещательные каналы отключена. Выберите группу или личный диалог.")
            sent = await client.send_message(entity, value, parse_mode=None, link_preview=False)
            if sent is None or getattr(sent, "id", None) is None:
                raise RuntimeError("Telegram не вернул подтверждение отправленного сообщения.")
            return {
                "message_id": int(sent.id),
                "peer_id": int(getattr(entity, "id", peer)),
                "title": str(
                    getattr(entity, "title", None)
                    or saved_title
                    or " ".join(filter(None, [
                        getattr(entity, "first_name", None),
                        getattr(entity, "last_name", None),
                    ]))
                    or (f"@{entity.username}" if getattr(entity, "username", None) else str(getattr(entity, "id", peer)))
                )[:200],
            }


__all__ = [
    "FloodWaitError",
    "isolated_account_client",
    "preview_message_target",
    "publish_story_photo",
    "send_message_to_chat",
    "set_profile_photo",
    "update_profile",
    "verify_channel_membership",
    "verify_chat_membership",
]
