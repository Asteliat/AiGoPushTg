"""High-signal discovery of public Telegram groups and invite links.

Telegram does not expose a complete public directory. This module combines its native
public-name search with invite links shared in dialogs the account can already read.
"""
from __future__ import annotations

import re
from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest

# Deliberately conservative: obvious spam terms are excluded; other weak candidates
# receive a lower score instead of being silently discarded.
_HARD_SPAM = (
    "казино", "casino", "порно", "porn", "xxx", "накрутка", "подписчик за",
    "ставки на спорт", "букмекер", "onlyfans", "18+ знакомства",
)
_SOFT_SPAM = (
    "розыгрыш", "заработок", "сигналы", "инвестиции", "airdrop", "крипта",
    "crypto", "обменник", "продвижение", "подписчики", "бесплатно",
)
_LINK_RE = re.compile(
    r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/"
    r"(?:\\+([A-Za-z0-9_-]{10,})|joinchat/([A-Za-z0-9_-]{10,})|([A-Za-z][A-Za-z0-9_]{4,31}))"
)
_SPECIAL_NAMES = {
    "share", "addstickers", "addemoji", "proxy", "socks", "login", "iv",
    "confirmphone", "setlanguage", "boost", "giftcode", "premium",
}


def _is_group(entity) -> bool:
    # Do not return broadcast channels or people/bots as "groups".
    if getattr(entity, "broadcast", False):
        return False
    if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
        return True
    return entity.__class__.__name__ == "Chat"


def _quality(entity) -> tuple[int, int] | None:
    if not _is_group(entity):
        return None
    if any(getattr(entity, flag, False) for flag in ("deleted", "scam", "fake", "restricted")):
        return None
    title = (getattr(entity, "title", "") or "").strip()
    username = (getattr(entity, "username", "") or "").strip()
    normalized = f"{title} {username}".casefold()
    if not title or any(term in normalized for term in _HARD_SPAM):
        return None

    members = int(getattr(entity, "participants_count", 0) or 0)
    score = 35
    if username:
        score += 8
    if members >= 100:
        score += 8
    if members >= 1000:
        score += 8
    if members >= 10000:
        score += 4
    if getattr(entity, "verified", False):
        score += 18
    if getattr(entity, "forum", False):
        score += 3
    if any(term in normalized for term in _SOFT_SPAM):
        score -= 18
    if len(title) < 4:
        score -= 8
    return score, members


async def search_public_groups(client, query: str, limit: int = 50) -> list[dict]:
    """Search Telegram's public name/username index and rank plausible groups."""
    response = await client(ContactsSearchRequest(q=query[:100], limit=min(max(limit, 1), 100)))
    found: dict[int, dict] = {}
    for entity in response.chats:
        quality = _quality(entity)
        if quality is None:
            continue
        score, members = quality
        username = getattr(entity, "username", None)
        item = {
            "id": int(entity.id),
            "title": (getattr(entity, "title", None) or username or str(entity.id))[:120],
            "username": username,
            "link": f"https://t.me/{username}" if username else None,
            "members": members,
            "verified": bool(getattr(entity, "verified", False)),
            "score": score,
            "source": "Telegram",
        }
        found[item["id"]] = item
    return sorted(found.values(), key=lambda x: (x["score"], x["members"]), reverse=True)


async def find_invite_links_in_dialogs(client, dialog_limit: int = 20, messages_per_dialog: int = 30) -> list[dict]:
    """Collect distinct group-looking links shared in recent messages of joined groups."""
    found: dict[str, dict] = {}
    async for dialog in client.iter_dialogs(limit=dialog_limit):
        entity = dialog.entity
        if not _is_group(entity):
            continue
        source_title = (dialog.name or "чат")[:70]
        try:
            async for message in client.iter_messages(entity, limit=messages_per_dialog):
                body = (getattr(message, "message", None) or "").strip()
                if not body:
                    continue
                for match in _LINK_RE.finditer(body):
                    invite_hash, join_hash, username = match.groups()
                    if invite_hash:
                        link = "https://t.me/+" + invite_hash
                    elif join_hash:
                        link = "https://t.me/joinchat/" + join_hash
                    else:
                        username = (username or "").strip()
                        if username.casefold() in _SPECIAL_NAMES or username.casefold() == "c":
                            continue
                        link = "https://t.me/" + username
                    found.setdefault(link, {
                        "link": link,
                        "title": link.rsplit("/", 1)[-1][:70],
                        "source": source_title,
                        "members": 0,
                        "score": 0,
                    })
                    if len(found) >= 40:
                        return list(found.values())
        except Exception:
            # One inaccessible/slow dialog should not abort the whole scan.
            continue
    return list(found.values())
