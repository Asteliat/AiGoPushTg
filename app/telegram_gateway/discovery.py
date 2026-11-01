"""Telegram group discovery with strict group-only verification.

Search engines are only candidate sources. A result is returned only after Telegram
confirms that it resolves to a basic group or a supergroup (never a broadcast channel).
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from telethon.errors import FloodWaitError
from telethon.tl.functions.contacts import SearchRequest as ContactsSearchRequest
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready


_HARD_SPAM = (
    "казино", "casino", "порно", "porn", "xxx", "накрутка",
    "ставки на спорт", "букмекер", "onlyfans",
)
_SOFT_SPAM = (
    "розыгрыш", "заработок", "сигналы", "инвестиции", "airdrop",
    "крипта", "crypto", "обменник", "продвижение", "подписчики",
)
_logger = logging.getLogger(__name__)

_PROVIDER_COOLDOWNS: dict[str, float] = {}
_PROVIDER_FAILURES: dict[str, int] = {}
_SEARCH_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL_SECONDS = 15 * 60
_CACHE_MAX_ITEMS = 256


def _provider_cooldown_seconds(status: str, failures: int) -> int:
    base = {"captcha": 30 * 60, "blocked": 15 * 60, "rate_limited": 10 * 60}.get(status, 5 * 60)
    return min(base * (2 ** min(max(failures - 1, 0), 4)), 6 * 60 * 60)


def _provider_available(name: str, now: float | None = None) -> bool:
    return _PROVIDER_COOLDOWNS.get(name, 0.0) <= (time.monotonic() if now is None else now)


def _record_provider_status(name: str, status: str) -> None:
    if status == "ok":
        _PROVIDER_FAILURES.pop(name, None)
        _PROVIDER_COOLDOWNS.pop(name, None)
        return
    if status not in {"captcha", "blocked", "rate_limited"}:
        return
    failures = _PROVIDER_FAILURES.get(name, 0) + 1
    _PROVIDER_FAILURES[name] = failures
    delay = _provider_cooldown_seconds(status, failures)
    _PROVIDER_COOLDOWNS[name] = time.monotonic() + delay
    _logger.warning("Group discovery provider %s paused for %ss (%s)", name, delay, status)


_SPECIAL_NAMES = {
    "share", "addstickers", "addemoji", "proxy", "socks", "login", "iv",
    "confirmphone", "setlanguage", "boost", "giftcode", "premium", "c",
}
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_INVITE_RE = re.compile(r"^[A-Za-z0-9_-]{10,}$")


def _is_group(entity) -> bool:
    """True only for Telegram basic groups and supergroups, never channels."""
    if entity is None or getattr(entity, "broadcast", False):
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
    score = 45
    if username:
        score += 8
    if members >= 100:
        score += 8
    if members >= 1000:
        score += 8
    if members >= 10000:
        score += 4
    if getattr(entity, "verified", False):
        score += 12
    if getattr(entity, "forum", False):
        score += 3
    if any(term in normalized for term in _SOFT_SPAM):
        score -= 18
    if len(title) < 4:
        score -= 8
    return score, members


def _normalize_telegram_link(raw: str) -> tuple[str, str, str] | None:
    """Return (canonical link, kind, target) for supported t.me group links."""
    value = html.unescape((raw or "").strip().rstrip(".,);]}>"))
    if not value:
        return None
    if value.startswith("//"):
        value = "https:" + value
    if not re.match(r"(?i)^https?://", value):
        value = "https://" + value.lstrip("@")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host not in {"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog"}:
        return None
    path = unquote(parsed.path).strip("/")
    if path.startswith("+"):
        invite_hash = path[1:]
        if not _INVITE_RE.fullmatch(invite_hash):
            return None
        return f"https://t.me/+{invite_hash}", "invite", invite_hash
    if path.startswith("joinchat/"):
        invite_hash = path.split("/", 1)[1]
        if not _INVITE_RE.fullmatch(invite_hash):
            return None
        return f"https://t.me/+{invite_hash}", "invite", invite_hash
    if not _USERNAME_RE.fullmatch(path) or path.casefold() in _SPECIAL_NAMES:
        return None
    return f"https://t.me/{path}", "public", path


async def search_public_groups(client, query: str, limit: int = 80) -> list[dict]:
    """Search Telegram's own public index. Results are already Telegram entities."""
    response = await client(ContactsSearchRequest(q=query[:100], limit=min(max(limit, 1), 100)))
    found: dict[int, dict] = {}
    for entity in response.chats:
        quality = _quality(entity)
        username = getattr(entity, "username", None)
        if quality is None or not username:
            continue
        score, members = quality
        item = {
            "id": int(entity.id),
            "title": (getattr(entity, "title", None) or username or str(entity.id))[:120],
            "username": username,
            "link": f"https://t.me/{username}",
            "members": members,
            "verified": True,
            "score": score,
            "source": "Telegram",
            "kind": "supergroup" if getattr(entity, "megagroup", False) else "group",
        }
        found[item["id"]] = item
    return sorted(found.values(), key=lambda x: (x["score"], x["members"]), reverse=True)


class _SearchAnchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.parts: list[str] = []

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


def _engine_search(engine: str, template: str, query: str, page: int = 0) -> tuple[list[dict], str]:
    """Fetch one public search-result page and report CAPTCHA/rate-limit status."""
    offset = page * 50
    user_agents = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 Version/17.0 Safari/605.1.15",
    )
    request = Request(
        template.format(q=quote_plus(query), page=page, offset=offset, first=offset + 1),
        headers={
            "User-Agent": user_agents[(page + len(engine)) % len(user_agents)],
            "Accept-Language": "ru,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=12) as response:
            status_code = getattr(response, "status", 200)
            body = response.read(2_000_000).decode("utf-8", "replace")
    except Exception as exc:
        status_code = getattr(exc, "code", None)
        if status_code in {403, 429, 503}:
            return [], "rate_limited" if status_code == 429 else "blocked"
        return [], "error"

    lowered = body.casefold()
    captcha_markers = (
        "captcha", "unusual traffic", "verify you are human",
        "подтвердите, что вы не робот", "challenge-form",
        "g-recaptcha", "h-captcha", "cf-challenge",
    )
    if any(marker in lowered for marker in captcha_markers):
        return [], "captcha"
    if status_code in {403, 429, 503}:
        return [], "rate_limited" if status_code == 429 else "blocked"

    parser = _SearchAnchors()
    try:
        parser.feed(body)
    except Exception:
        return [], "error"

    results = []
    for href, title in parser.links:
        href = html.unescape(href)
        parsed = urlparse(href if "://" in href else "https://" + href.lstrip("/"))
        params = parse_qs(parsed.query)
        for key in ("uddg", "url", "u", "q", "RU"):
            target = params.get(key, [None])[0]
            if target and target.startswith("http"):
                href = unquote(target)
                break
        if href.startswith("//"):
            href = "https:" + href
        candidate = _normalize_telegram_link(href)
        if not candidate:
            continue
        link, kind, target = candidate
        results.append({
            "id": None,
            "title": (title or target)[:120],
            "username": target if kind == "public" else None,
            "link": link,
            "members": 0,
            "verified": False,
            "score": 20 if kind == "invite" else 24,
            "source": engine,
            "kind": "invite" if kind == "invite" else "unknown",
            "search_page": page + 1,
        })
    return results, "ok"


async def search_web_groups(query: str, limit: int = 600, pages_per_query: int = 5) -> list[dict]:
    """Search public engines with independent cooldowns and a short result cache.

    A CAPTCHA or provider block pauses only that provider. Subsequent searches
    automatically skip it until its cooldown expires; healthy providers continue.
    Telegram-native discovery is independent and remains available to the caller.
    """
    clean_query = " ".join((query or "").split())[:100]
    if len(clean_query) < 2:
        return []

    cache_key = clean_query.casefold()
    now = time.monotonic()
    cached = _SEARCH_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return [dict(item) for item in cached[1]][:max(1, min(int(limit), 1000))]
    if cached:
        _SEARCH_CACHE.pop(cache_key, None)

    engines = (
        ("Google", "https://www.google.com/search?q={q}&num=50&start={offset}"),
        ("Yandex", "https://yandex.com/search/?text={q}&p={page}"),
        ("Bing", "https://www.bing.com/search?q={q}&count=50&first={first}"),
        ("DuckDuckGo", "https://html.duckduckgo.com/html/?q={q}&s={offset}"),
        ("Yahoo", "https://search.yahoo.com/search?p={q}&n=50&b={first}"),
    )
    terms = (
        f'site:t.me "{clean_query}" (группа OR чат OR group)',
        f'site:t.me "{clean_query}"',
        f'site:telegram.me "{clean_query}"',
        f'site:t.me/+ "{clean_query}" OR site:t.me/joinchat "{clean_query}"',
    )
    page_count = max(1, min(int(pages_per_query), 8))
    semaphore = asyncio.Semaphore(5)

    async def run_engine(name: str, template: str) -> list[dict]:
        collected: list[dict] = []
        if not _provider_available(name):
            _logger.info("Skipping group discovery provider %s during cooldown", name)
            return collected
        async with semaphore:
            for term in terms:
                for page in range(page_count):
                    batch, status = await asyncio.to_thread(
                        _engine_search, name, template, term, page
                    )
                    if status in {"captcha", "blocked", "rate_limited"}:
                        _record_provider_status(name, status)
                        return collected
                    if status == "ok":
                        _record_provider_status(name, "ok")
                    collected.extend(batch)
                    if status == "error":
                        continue
                    if page + 1 < page_count:
                        await asyncio.sleep(0.25)
        return collected

    batches = await asyncio.gather(
        *(run_engine(name, template) for name, template in engines),
        return_exceptions=True,
    )

    found: dict[str, dict] = {}
    for batch in batches:
        if isinstance(batch, Exception):
            continue
        for item in batch:
            key = item["link"].casefold()
            if key in found:
                sources = found[key]["source"].split(", ")
                if item["source"] not in sources:
                    found[key]["source"] += ", " + item["source"]
                found[key]["score"] = min(50, found[key]["score"] + 2)
                found[key]["search_pages"] = sorted(set(
                    found[key].get("search_pages", [found[key].get("search_page", 1)])
                    + [item.get("search_page", 1)]
                ))
                if item.get("title") and len(item["title"]) > len(found[key]["title"]):
                    found[key]["title"] = item["title"]
            else:
                item["search_pages"] = [item.pop("search_page", 1)]
                found[key] = item
    ordered = sorted(found.values(), key=lambda x: x["score"], reverse=True)
    result = ordered[:max(1, min(int(limit), 1000))]
    _SEARCH_CACHE[cache_key] = (
        time.monotonic() + _CACHE_TTL_SECONDS,
        [dict(item) for item in result],
    )
    if len(_SEARCH_CACHE) > _CACHE_MAX_ITEMS:
        oldest = sorted(_SEARCH_CACHE.items(), key=lambda entry: entry[1][0])
        for old_key, _ in oldest[:len(_SEARCH_CACHE) - _CACHE_MAX_ITEMS]:
            _SEARCH_CACHE.pop(old_key, None)
    return result


async def verify_group_candidates(client, candidates: list[dict], concurrency: int = 4) -> list[dict]:
    """Resolve every candidate with Telegram and discard anything that is not a group."""
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 8)))

    async def verify(item: dict) -> dict | None:
        parsed = _normalize_telegram_link(str(item.get("link") or ""))
        if not parsed:
            return None
        link, kind, target = parsed
        async with semaphore:
            try:
                if kind == "public":
                    entity = await client.get_entity(target)
                    quality = _quality(entity)
                    if quality is None:
                        return None
                    score, members = quality
                    username = getattr(entity, "username", None)
                    if not username:
                        return None
                    canonical = f"https://t.me/{username}"
                    title = (getattr(entity, "title", None) or item.get("title") or username)[:120]
                    return {
                        **item,
                        "id": int(entity.id),
                        "title": title,
                        "username": username,
                        "link": canonical,
                        "members": members,
                        "verified": True,
                        "score": max(score, int(item.get("score") or 0)),
                        "kind": "supergroup" if getattr(entity, "megagroup", False) else "group",
                    }

                invite = await client(CheckChatInviteRequest(hash=target))
                if isinstance(invite, ChatInviteAlready):
                    entity = invite.chat
                    quality = _quality(entity)
                    if quality is None:
                        return None
                    score, members = quality
                    username = getattr(entity, "username", None)
                    canonical = f"https://t.me/{username}" if username else link
                    return {
                        **item,
                        "id": int(entity.id),
                        "title": (getattr(entity, "title", None) or item.get("title") or "Группа")[:120],
                        "username": username,
                        "link": canonical,
                        "members": members,
                        "verified": True,
                        "score": max(score, int(item.get("score") or 0)),
                        "kind": "supergroup" if getattr(entity, "megagroup", False) else "group",
                    }

                # A not-yet-joined invite is a ChatInvite constructor. Telegram exposes
                # channel/broadcast/megagroup flags; only non-channel invites are groups.
                if getattr(invite, "channel", False) or getattr(invite, "broadcast", False):
                    return None
                if not getattr(invite, "megagroup", False) and getattr(invite, "channel", False):
                    return None
                title = (getattr(invite, "title", None) or item.get("title") or "Приватная группа")[:120]
                members = int(getattr(invite, "participants_count", 0) or 0)
                return {
                    **item,
                    "id": None,
                    "title": title,
                    "username": None,
                    "link": link,
                    "members": members,
                    "verified": True,
                    "score": max(35, int(item.get("score") or 0)),
                    "kind": "invite",
                }
            except FloodWaitError as exc:
                await asyncio.sleep(min(max(int(exc.seconds), 1), 30))
                try:
                    if kind == "public":
                        entity = await client.get_entity(target)
                        quality = _quality(entity)
                        if quality is None:
                            return None
                        score, members = quality
                        username = getattr(entity, "username", None)
                        if not username:
                            return None
                        return {
                            **item, "id": int(entity.id),
                            "title": (getattr(entity, "title", None) or username)[:120],
                            "username": username, "link": f"https://t.me/{username}",
                            "members": members, "verified": True, "score": score,
                            "kind": "supergroup" if getattr(entity, "megagroup", False) else "group",
                        }
                except Exception:
                    return None
                return None
            except Exception:
                return None

    checked = await asyncio.gather(*(verify(item) for item in candidates), return_exceptions=True)
    unique: dict[str, dict] = {}
    for item in checked:
        if not isinstance(item, dict) or not item.get("verified"):
            continue
        key = str(item.get("link") or "").casefold()
        if not key:
            continue
        if key not in unique:
            unique[key] = item
        else:
            current = unique[key]
            current["score"] = max(int(current.get("score") or 0), int(item.get("score") or 0))
            current["members"] = max(int(current.get("members") or 0), int(item.get("members") or 0))
            current["source"] = ", ".join(dict.fromkeys(
                (current.get("source", "") + ", " + item.get("source", "")).strip(", ").split(", ")
            ))
    return sorted(unique.values(), key=lambda x: (int(x.get("score") or 0), int(x.get("members") or 0)), reverse=True)


async def find_invite_links_in_dialogs(
    client,
    dialog_limit: int | None = None,
    messages_per_dialog: int = 5000,
    progress_callback=None,
) -> list[dict]:
    """Collect Telegram links from recent messages in groups the account can read."""
    import re as _re

    found: dict[str, dict] = {}
    scanned_groups = scanned_messages = 0
    async for dialog in client.iter_dialogs(limit=dialog_limit):
        entity = dialog.entity
        if not _is_group(entity):
            continue
        scanned_groups += 1
        source_title = (dialog.name or "чат")[:70]
        processed = 0
        last_message_id = 0
        while processed < messages_per_dialog:
            batch_count = 0
            try:
                async for message in client.iter_messages(
                    entity,
                    limit=messages_per_dialog - processed,
                    offset_id=last_message_id,
                ):
                    batch_count += 1
                    processed += 1
                    scanned_messages += 1
                    last_message_id = int(getattr(message, "id", 0) or 0)
                    bodies = [getattr(message, "message", None) or ""]
                    for text_entity in getattr(message, "entities", None) or []:
                        hidden_url = getattr(text_entity, "url", None)
                        if hidden_url:
                            bodies.append(hidden_url)
                    markup = getattr(message, "reply_markup", None)
                    for row in getattr(markup, "rows", None) or []:
                        for button in getattr(row, "buttons", None) or []:
                            button_url = getattr(button, "url", None)
                            if button_url:
                                bodies.append(button_url)
                    for body in bodies:
                        for raw in _re.findall(
                            r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"']+",
                            body,
                        ):
                            parsed = _normalize_telegram_link(raw)
                            if not parsed:
                                continue
                            link, kind, target = parsed
                            found.setdefault(link.casefold(), {
                                "id": None,
                                "title": link.rsplit("/", 1)[-1][:70],
                                "username": target if kind == "public" else None,
                                "link": link,
                                "source": source_title,
                                "members": 0,
                                "score": 0,
                                "verified": False,
                                "private_invite": kind == "invite",
                            })
                    if progress_callback and scanned_messages % 500 == 0:
                        await progress_callback(scanned_groups, scanned_messages, len(found))
                if batch_count == 0 or processed >= messages_per_dialog:
                    break
                break
            except FloodWaitError as exc:
                await asyncio.sleep(max(int(exc.seconds), 1) + 1)
                continue
            except Exception:
                break
    if progress_callback:
        await progress_callback(scanned_groups, scanned_messages, len(found))
    return sorted(found.values(), key=lambda item: (bool(item.get("private_invite")), item["link"]), reverse=True)
