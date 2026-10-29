"""High-signal discovery of public Telegram groups and invite links.

Telegram does not expose a complete public directory. This module combines its native
public-name search with invite links shared in dialogs the account can already read.
"""
from __future__ import annotations

import re
import asyncio
import html
from html.parser import HTMLParser
from urllib.parse import quote_plus, urlparse, parse_qs, unquote
from urllib.request import Request, urlopen
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
        if not username:
            continue
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


async def find_invite_links_in_dialogs(client, dialog_limit: int | None = None, messages_per_dialog: int = 5000, progress_callback=None) -> list[dict]:
    """Collect distinct group-looking links shared in recent messages of joined groups."""
    found: dict[str, dict] = {}
    scanned_groups = 0
    scanned_messages = 0
    async for dialog in client.iter_dialogs(limit=dialog_limit):
        entity = dialog.entity
        if not _is_group(entity):
            continue
        scanned_groups += 1
        source_title = (dialog.name or "чат")[:70]
        try:
            async for message in client.iter_messages(entity, limit=messages_per_dialog):
                scanned_messages += 1
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
                if progress_callback and scanned_messages % 500 == 0:
                    await progress_callback(scanned_groups, scanned_messages, len(found))
        except Exception:
            # One inaccessible/slow dialog should not abort the whole scan.
            continue
    if progress_callback:
        await progress_callback(scanned_groups, scanned_messages, len(found))
    return list(found.values())


class _SearchAnchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.href = None
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.href = dict(attrs).get("href")
            self.parts = []
    def handle_data(self, data):
        if self.href is not None:
            self.parts.append(data)
    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.href is not None:
            self.links.append((self.href, " ".join(" ".join(self.parts).split())))
            self.href, self.parts = None, []

def _engine_search(engine, template, query):
    request = Request(template.format(q=quote_plus(query)), headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ru,en;q=0.8"})
    try:
        with urlopen(request, timeout=10) as response:
            body = response.read(1500000).decode("utf-8", "replace")
    except Exception:
        return []
    parser = _SearchAnchors()
    try:
        parser.feed(body)
    except Exception:
        return []
    results = []
    for href, title in parser.links:
        href = html.unescape(href)
        parsed = urlparse(href)
        params = parse_qs(parsed.query)
        for key in ("uddg", "url", "u", "q", "RU"):
            target = params.get(key, [None])[0]
            if target and target.startswith("http"):
                href = unquote(target)
                break
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href if "://" in href else "https://" + href)
        if (parsed.hostname or "").lower() not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog"}:
            continue
        path = unquote(parsed.path).strip("/")
        private = path.startswith("+") or path.startswith("joinchat/")
        if private:
            slug = path[1:] if path.startswith("+") else path.split("/", 1)[-1]
            if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", slug):
                continue
        elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", path):
            continue
        if path.startswith(("s/", "share/", "proxy/", "login/", "addstickers/", "addemoji/")):
            continue
        link = "https://t.me/" + path
        results.append({"id": None, "title": (title or path)[:120], "username": None if private else path,
                        "link": link, "members": 0, "verified": False, "score": 25 if private else 30,
                        "source": engine, "private_invite": private})
    return results

async def search_web_groups(query: str, limit: int = 150) -> list[dict]:
    """Find public Telegram links indexed by common search engines."""
    engines = (
        ("Google", "https://www.google.com/search?q={q}&num=30"),
        ("Yandex", "https://yandex.com/search/?text={q}"),
        ("Bing", "https://www.bing.com/search?q={q}&count=30"),
        ("Yahoo", "https://search.yahoo.com/search?p={q}&n=30"),
        ("DuckDuckGo", "https://html.duckduckgo.com/html/?q={q}"),
    )
    terms = f'site:t.me ("members" OR "участников" OR "группа" OR "group" OR "чат") {query[:100]}'
    batches = await asyncio.gather(*(asyncio.to_thread(_engine_search, name, url, terms) for name, url in engines))
    found = {}
    for batch in batches:
        for item in batch:
            key = item["link"].casefold()
            if key in found:
                if item["source"] not in found[key]["source"]:
                    found[key]["source"] += ", " + item["source"]
            else:
                found[key] = item
    return sorted(found.values(), key=lambda item: (item["score"], item["title"].casefold()), reverse=True)[:max(1, min(limit, 300))]
