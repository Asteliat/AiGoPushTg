"""Verifications that continue inside another bot's private chat.

Some group bots do not accept a button press in the group: they ask the member to
open a verification bot and press Start there. This module recognises that variant
and extracts the bot username plus the deep-link payload, without any network access:

* inline button URLs like ``https://t.me/JoinCaptchaBot?start=abc123``;
* ``tg://resolve?domain=…&start=…`` links in the message text;
* a plain ``@SomeCheckBot`` mention.

Telegram bot usernames always end with ``bot``, which is what keeps this code from
ever addressing a real person.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_BOT_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,30}bot$", re.IGNORECASE)
_TME_LINK = re.compile(
    r"(?:https?://)?(?:t|telegram)\.me/(?:s/)?"
    r"(?P<user>[A-Za-z][A-Za-z0-9_]{2,31})"
    r"(?:\?(?:[^#\s]*?&)?start=(?P<payload>[A-Za-z0-9_\-]{1,64}))?",
    re.IGNORECASE,
)
_TG_RESOLVE = re.compile(
    r"tg://resolve\?domain=(?P<user>[A-Za-z][A-Za-z0-9_]{2,31})"
    r"(?:&(?:[^#\s]*?&)?start=(?P<payload>[A-Za-z0-9_\-]{1,64}))?",
    re.IGNORECASE,
)
_MENTION = re.compile(r"(?<![\w/])@(?P<user>[A-Za-z][A-Za-z0-9_]{2,31})")

_VERIFIED_PATTERNS: tuple[str, ...] = (
    r"подтвержд",
    r"проверк\w*\s+пройден",
    r"вы\s+(?:человек|не\s+бот|не\s+робот)",
    r"успешн",
    r"доступ\s+разреш",
    r"добро\s+пожаловать",
    r"\bverified\b",
    r"\bsuccess(?:fully)?\b",
    r"\baccess\s+granted\b",
    r"\bwelcome\b",
    r"✅",
)
_COMPILED_VERIFIED = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _VERIFIED_PATTERNS)


@dataclass(frozen=True, slots=True)
class BotVerificationTarget:
    """A bot to open privately, with the optional deep-link payload."""

    username: str
    payload: str | None = None

    @property
    def mention(self) -> str:
        return f"@{self.username}"

    @property
    def start_command(self) -> str:
        return f"/start {self.payload}" if self.payload else "/start"


def is_bot_username(username: str | None) -> bool:
    return bool(username and _BOT_USERNAME.match(username.strip().lstrip("@")))


def _target(username: str | None, payload: str | None) -> BotVerificationTarget | None:
    if not is_bot_username(username):
        return None
    return BotVerificationTarget(username=username.strip().lstrip("@"), payload=payload)


def _from_url(url: str) -> BotVerificationTarget | None:
    resolve = _TG_RESOLVE.search(url)
    if resolve:
        return _target(resolve.group("user"), resolve.group("payload"))
    link = _TME_LINK.search(url)
    if link:
        return _target(link.group("user"), link.group("payload"))
    return None


def extract_bot_target(
    *,
    text: str | None = None,
    button_urls: Iterable[str] = (),
    button_labels: Sequence[str] = (),
) -> BotVerificationTarget | None:
    """Find the verification bot a prompt asks the member to open.

    Button URLs win: they usually carry the per-member ``start`` payload the bot
    expects. Then links in the text, then a bare ``@…bot`` mention.
    """
    for url in button_urls:
        target = _from_url(str(url))
        if target is not None:
            return target
    haystack = text or ""
    for match in _TG_RESOLVE.finditer(haystack):
        target = _target(match.group("user"), match.group("payload"))
        if target is not None:
            return target
    for match in _TME_LINK.finditer(haystack):
        target = _target(match.group("user"), match.group("payload"))
        if target is not None:
            return target
    for match in _MENTION.finditer(haystack):
        target = _target(match.group("user"), None)
        if target is not None:
            return target
    for label in button_labels:
        for match in _MENTION.finditer(label or ""):
            target = _target(match.group("user"), None)
            if target is not None:
                return target
    return None


def looks_like_verified_reply(text: str | None) -> bool:
    """True for a bot reply that means the account passed instead of offering buttons."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _COMPILED_VERIFIED)
