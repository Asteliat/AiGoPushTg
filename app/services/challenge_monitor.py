"""Detect Telegram group anti-bot challenges and expose safe, human-reviewed signals.

This module deliberately does not solve challenges, click verification buttons, or submit
emoji answers. It only identifies likely verification prompts for operator visibility.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

_RU_PATTERNS = (
    r"\\bне\\s+бот\\b",
    r"нажми(?:те)?\\s+на\\s+кнопк",
    r"пройдите?\\s+проверку",
    r"проверка\\s+на\\s+бот",
    r"выберите?\\s+(?:смайлик|эмодзи)",
    r"выбери(?:те)?\\s+(?:смайлик|эмодзи)",
    r"подтвердите?,?\\s+что\\s+вы\\s+человек",
    r"капч",
)
_EN_PATTERNS = (
    r"\\bcaptcha\\b",
    r"\\bverify\\s+(?:that\\s+)?you(?:'re|\\s+are)\\s+human\\b",
    r"\\bclick\\s+(?:the\\s+)?button\\b",
    r"\\bchoose\\s+(?:an?\\s+)?emoji\\b",
    r"\\bselect\\s+(?:an?\\s+)?emoji\\b",
)
_COMPILED = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (*_RU_PATTERNS, *_EN_PATTERNS))


def is_challenge_prompt(text: str | None, button_labels: Iterable[str] = ()) -> bool:
    """Return True only for likely human-verification prompts, not generic bot messages."""
    combined = " ".join(
        part.strip()
        for part in (text or "", *tuple(button_labels))
        if isinstance(part, str) and part.strip()
    )
    if not combined:
        return False
    return any(pattern.search(combined) for pattern in _COMPILED)


def extract_button_labels(message) -> list[str]:
    """Read visible button captions without invoking callbacks or interacting with them."""
    labels: list[str] = []
    for row in getattr(message, "buttons", None) or ():
        for button in row or ():
            label = getattr(button, "text", None)
            if isinstance(label, str) and label.strip():
                labels.append(label.strip()[:80])
    return labels


def format_challenge_alert(
    *,
    account_id: int,
    account_name: str,
    chat_id: int,
    chat_title: str,
    message_id: int,
    sender_name: str,
    text: str,
    button_labels: Iterable[str] = (),
) -> str:
    """Build a bounded plain-text operator alert; never executes challenge actions."""
    body = " ".join((text or "").split())[:1200] or "[сообщение без текста]"
    buttons = list(button_labels)
    buttons_text = ", ".join(buttons[:12]) if buttons else "нет"
    return (
        "🛡 Обнаружена возможная проверка Telegram\n\n"
        f"Аккаунт: {account_name} · #{account_id}\n"
        f"Группа: {chat_title or 'без названия'}\n"
        f"ID группы: {chat_id}\n"
        f"Проверяющий бот: {sender_name or 'неизвестен'}\n"
        f"ID сообщения: {message_id}\n"
        f"Кнопки: {buttons_text}\n\n"
        f"Текст проверки: {body}\n\n"
        "Нужна проверка оператором. Аккаунт не нажимал кнопки и не отправлял ответы."
    )
