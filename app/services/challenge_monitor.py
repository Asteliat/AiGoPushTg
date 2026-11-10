"""Telegram group verification prompts: detection, targeting and safe auto-solving.

Many Telegram groups gate new members behind a bot that posts a short question and a
row of inline buttons ("I'm not a bot", an emoji, a number). Such a bot posts one
prompt per joining member, so every account in the group sees prompts that belong to
*other* people. The pre-refactor code alerted the operator about all of them, which
produced a constant stream of notifications about strangers' verifications.

This module keeps the decision pure and testable:

* :func:`is_challenge_prompt` — does a bot message look like a verification prompt;
* :func:`is_directed_to_account` — is the prompt addressed to *our* account;
* :func:`select_verification_index` — which inline button passes the check;
* :func:`format_solved_log` / :func:`format_failed_log` — one bounded log line.

Network and Telegram interactions live in :mod:`app.workers.challenge_watcher`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from enum import StrEnum

# --------------------------------------------------------------------------- detection

_CHALLENGE_PATTERNS: tuple[str, ...] = (
    r"\bне\s+бот\b",
    r"\bне\s+робот\b",
    r"нажми(?:те)?\s+на\s+кнопк",
    r"\bкнопк(?:а|у|и)\s+ниже",
    r"пройди(?:те)?\s+проверк",
    r"проверк(?:а|у|и)\s+на\s+(?:бот|робот)",
    r"выбери(?:те)?\s+(?:смайлик|эмодзи|цвет|цифру|число)",
    r"подтверди(?:те)?,?\s+что\s+вы\s+(?:человек|не\s+бот|не\s+робот)",
    r"докажи(?:те)?,?\s+что\s+вы\s+(?:человек|не\s+бот|не\s+робот)",
    r"капч",
    r"верификац",
    r"\bverify\s+(?:that\s+)?you(?:'re|\s+are)\s+human\b",
    r"\bnot\s+a\s+robot\b",
    r"\bclick\s+(?:the\s+)?button\b",
    r"\bpress\s+(?:the\s+)?button\b",
    r"\bchoose\s+(?:an?\s+)?(?:emoji|colour|color|number)\b",
    r"\bselect\s+(?:an?\s+)?(?:emoji|colour|color|number)\b",
    r"\bcaptcha\b",
    r"\bhuman\s+verification\b",
)
_COMPILED_CHALLENGE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _CHALLENGE_PATTERNS)

# Buttons that must never be pressed automatically: they cancel, report or leave.
_DESTRUCTIVE_BUTTON = re.compile(
    r"отмен|cancel|жалоб|report|покинут|выйти|leave|удал|delete|спам|spam",
    re.IGNORECASE,
)

# Buttons that confirm a human rather than choosing an answer.
_CONFIRM_BUTTON = re.compile(
    r"не\s+(?:бот|робот)|человек|подтверд|проверк|готово|продолж|"
    r"(?<!\w)да(?!\w)|(?<!\w)ok(?!\w)|(?<!\w)yes(?!\w)|verify|human|confirm|"
    r"✅|✔|☑",
    re.IGNORECASE,
)

_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U00002190-\U000021ff"
    "\U00002b00-\U00002bff\U0000fe0f\U0000200d\U0001f1e6-\U0001f1ff]"
)
_EMOJI_ONLY = re.compile("^[^\\w\\s]+$")
_NUMBER = re.compile(r"(?<!\d)(\d{1,3})(?!\d)")
_MATH = re.compile(r"(?<!\d)(\d{1,3})\s*([+\-−×xX*÷/:])\s*(\d{1,3})(?!\d)")


def is_challenge_prompt(text: str | None, button_labels: Iterable[str] = ()) -> bool:
    """Return True only for likely human-verification prompts, not generic bot posts."""
    combined = normalize_text(text, button_labels)
    if not combined:
        return False
    return any(pattern.search(combined) for pattern in _COMPILED_CHALLENGE)


def normalize_text(text: str | None, button_labels: Iterable[str] = ()) -> str:
    """Join visible text and button captions into one bounded haystack."""
    parts = [part.strip() for part in (text or "", *tuple(button_labels)) if isinstance(part, str)]
    return " ".join(part for part in parts if part)


def extract_button_labels(message: object) -> list[str]:
    """Read visible button captions without invoking callbacks or interacting with them."""
    labels: list[str] = []
    for row in getattr(message, "buttons", None) or ():
        for button in row or ():
            label = getattr(button, "text", None)
            if isinstance(label, str) and label.strip():
                labels.append(label.strip()[:80])
    return labels


# --------------------------------------------------------------------------- targeting


def is_directed_to_account(
    *,
    prompt_text: str | None,
    reply_to_sender_id: int | None,
    account_user_id: int,
    account_username: str | None = None,
    account_names: Sequence[str] = (),
    mentioned: bool = False,
) -> bool:
    """Tell own verification prompts from prompts meant for other group members.

    A prompt belongs to our account when the bot replied to our message, marked us in
    a Telegram entity, used our @username, or addressed us by name (``"Иван, нажми…"``).
    Anything else is somebody else's verification and is ignored without a single
    notification.
    """
    if mentioned:
        return True
    if reply_to_sender_id is not None and int(reply_to_sender_id) == int(account_user_id):
        return True
    haystack = (prompt_text or "").casefold()
    if not haystack:
        return False

    username = (account_username or "").strip().lstrip("@").casefold()
    if username and re.search(rf"(?<!\w)@{re.escape(username)}(?!\w)", haystack):
        return True
    if (
        username
        and len(username) >= 4
        and re.search(rf"(?<!\w){re.escape(username)}(?!\w)", haystack)
    ):
        return True

    for raw_name in account_names:
        name = _clean_name(raw_name).casefold()
        # Bots address a member by first name or by the full name; one- and
        # two-letter parts match too many unrelated words, three is enough.
        for part in (name, *name.split()):
            if len(part) < 3:
                continue
            if re.search(rf"(?<!\w){re.escape(part)}(?!\w)", haystack):
                return True
    return False


def _clean_name(name: str | None) -> str:
    """Drop emoji and decorative characters so ``"Иван 🌚"`` matches ``Иван``."""
    if not name:
        return ""
    cleaned = "".join(
        char
        for char in name
        if not _EMOJI.match(char) and unicodedata.category(char)[0] not in {"S", "C"}
    )
    return " ".join(cleaned.split())


# --------------------------------------------------------------------------- solving


def find_verification_emoji(prompt_text: str | None) -> str | None:
    """Return the emoji the prompt asks to choose ("выберите 🐱"), if any."""
    if not prompt_text:
        return None
    for match in _EMOJI.finditer(prompt_text):
        value = match.group(0)
        # Variation selectors and joiners are not useful for matching captions.
        if value not in {"\ufe0f", "\u200d"}:
            return value
    return None


def _is_destructive(label: str) -> bool:
    return bool(_DESTRUCTIVE_BUTTON.search(label))


def _number_token(label: str) -> str | None:
    match = _NUMBER.search(label)
    return match.group(1) if match else None


def _answer_token(prompt_text: str) -> str | None:
    """The answer a prompt asks for: a simple math result or the only number in it."""
    match = _MATH.search(prompt_text)
    if match:
        left, operator, right = int(match.group(1)), match.group(2), int(match.group(3))
        if operator == "+":
            result = left + right
        elif operator in {"-", "−"}:
            result = left - right
        elif operator in {"*", "×", "x", "X"}:
            result = left * right
        elif right == 0 or left % right:
            # Integer captcha answers only; a fraction is never a button caption.
            return None
        else:
            result = left // right
        return str(result) if 0 <= result <= 999 else None
    numbers = _NUMBER.findall(prompt_text)
    if len(numbers) == 1:
        return numbers[0]
    return None


def select_verification_index(
    labels: Sequence[str],
    prompt_text: str | None = None,
) -> int | None:
    """Pick the inline button that passes the check.

    The prompt is authoritative: a mentioned emoji or number wins, then a
    confirmation caption, then the only non-destructive button. Ambiguous prompts
    return ``None`` so that the operator is told instead of pressing a wrong button.
    """
    if not labels:
        return None
    candidates = [index for index, label in enumerate(labels) if not _is_destructive(label)]
    if not candidates:
        return None

    emoji = find_verification_emoji(prompt_text)
    if emoji:
        by_emoji = [index for index in candidates if emoji in labels[index]]
        if len(by_emoji) == 1:
            return by_emoji[0]

    if prompt_text:
        answer = _answer_token(prompt_text)
        if answer is not None:
            by_answer = [index for index in candidates if _number_token(labels[index]) == answer]
            if len(by_answer) == 1:
                return by_answer[0]

    by_confirm = [index for index in candidates if _CONFIRM_BUTTON.search(labels[index])]
    if len(by_confirm) == 1:
        return by_confirm[0]
    if len(by_confirm) > 1:
        # Several "yes-like" buttons: only trust an explicit lead button.
        return by_confirm[0] if _looks_like_lead_button(labels[by_confirm[0]]) else None

    if len(candidates) == 1:
        return candidates[0]

    # A pure emoji keyboard without a hint in the text: any press verifies the
    # account in most group bots, and the emoji set is the only control offered.
    if all(
        _EMOJI_ONLY.match(label) or _EMOJI.search(label)
        for label in (labels[index] for index in candidates)
    ):
        return candidates[0]
    return None


def _looks_like_lead_button(label: str) -> bool:
    return bool(re.search(r"✅|✔|☑|подтверд|verify|continue|продолж", label, re.IGNORECASE))


def solve_verification(
    button_rows: Sequence[Sequence[str]], prompt_text: str | None
) -> tuple[int, int] | None:
    """Return ``(row, column)`` for :meth:`telethon.tl.custom.message.Message.click`."""
    labels = [label for row in button_rows for label in row]
    index = select_verification_index(labels, prompt_text)
    if index is None:
        return None
    for row_index, row in enumerate(button_rows):
        if index < len(row):
            return row_index, index
        index -= len(row)
    return None


# --------------------------------------------------------------------------- logging


def format_solved_log(
    *,
    account_id: int,
    account_name: str,
    chat_title: str,
    sender_name: str,
    button_text: str,
) -> str:
    """One bounded line: the check was passed, no operator action is required."""
    return (
        "🛡 Проверка Telegram пройдена\n"
        f"Аккаунт: {account_name} · #{account_id}\n"
        f"Группа: {chat_title or 'без названия'}\n"
        f"Бот: {sender_name or 'неизвестен'}\n"
        f"Кнопка: {button_text}"
    )


def format_failed_log(
    *,
    account_id: int,
    account_name: str,
    chat_title: str,
    sender_name: str,
    reason: str,
    button_labels: Iterable[str] = (),
) -> str:
    """One bounded line: our account was challenged but the check was not passed."""
    buttons = ", ".join(list(button_labels)[:8]) or "нет"
    return (
        "⚠️ Проверка Telegram не пройдена\n"
        f"Аккаунт: {account_name} · #{account_id}\n"
        f"Группа: {chat_title or 'без названия'}\n"
        f"Бот: {sender_name or 'неизвестен'}\n"
        f"Кнопки: {buttons}\n"
        f"Причина: {reason}"
    )


# --------------------------------------------------------------------------- settings


class ChallengeMode(StrEnum):
    """How the worker reacts to verification prompts addressed to our accounts."""

    AUTO = "auto"
    NOTIFY = "notify"
    OFF = "off"


_CHALLENGE_MODE_VALUES: dict[str, ChallengeMode] = {
    **{mode.value: mode for mode in ChallengeMode},
    "on": ChallengeMode.AUTO,
    "true": ChallengeMode.AUTO,
    "1": ChallengeMode.AUTO,
    "yes": ChallengeMode.AUTO,
    "enabled": ChallengeMode.AUTO,
    "false": ChallengeMode.OFF,
    "0": ChallengeMode.OFF,
    "no": ChallengeMode.OFF,
    "disabled": ChallengeMode.OFF,
}


def parse_challenge_mode(
    value: str | None, *, default: ChallengeMode = ChallengeMode.AUTO
) -> ChallengeMode:
    """Parse a stored setting, tolerating the legacy boolean ``challenge_monitor_enabled``."""
    if value is None:
        return default
    return _CHALLENGE_MODE_VALUES.get(value.strip().casefold(), default)
