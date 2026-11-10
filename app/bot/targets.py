"""Telegram link parsing, entity coercion and isolated clients."""
from __future__ import annotations

from app.bot.core import cfg
from app.bot.core import isolated_telegram_client
from app.storage.db import SessionFactory
from app.storage.models import Account
from contextlib import asynccontextmanager
import re

def parse_chat_link(raw: str) -> tuple[str, str]:
    value = (raw or "").strip().strip("<>[](){}.,;")
    if value.startswith("@") and re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value):
        return "public", value[1:]
    if re.search(r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/c/\d+", value):
        raise ValueError(
            "Это внутренняя ссылка t.me/c/... на сообщение, а не ссылка для вступления. "
            "Попросите администратора прислать пригласительную ссылку t.me/+… "
            "или t.me/joinchat/…"
        )
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
def group_entity(entity) -> bool:
    if getattr(entity, "broadcast", False):
        return False
    return bool(getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False) or entity.__class__.__name__ == "Chat")
@asynccontextmanager
async def isolated_account_client(aid: int):
    """Open an authorized account through a temporary session snapshot."""
    if not cfg.telegram_api_id or not cfg.telegram_api_hash:
        raise RuntimeError("Не заданы TELEGRAM_API_ID и TELEGRAM_API_HASH.")
    async with SessionFactory() as db:
        account = await db.get(Account, aid)
    if not account or not account.enabled:
        raise RuntimeError("Аккаунт не найден или отключён.")
    async with isolated_telegram_client(account) as client:
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия аккаунта не авторизована.")
        yield account, client
def channel_entity(entity) -> bool:
    return bool(
        getattr(entity, "broadcast", False)
        and not getattr(entity, "megagroup", False)
        and not getattr(entity, "gigagroup", False)
    )
