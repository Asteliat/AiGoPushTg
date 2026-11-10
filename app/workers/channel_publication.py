"""Publication outcomes and override resolution for channel posts."""

from __future__ import annotations


PUBLICATION_STATUS_TEXT: dict[str, str] = {
    "published": "Комментарий опубликован ответом на исходную публикацию.",
    "manual": "Подготовлен черновик. Ручной режим: комментарий не опубликован.",
    "skipped_probability": "ИИ проанализировал пост, но вероятность не выбрала подготовку комментария.",
    "skipped_ai": "ИИ решил, что отдельный комментарий к публикации не нужен.",
    "skipped_cooldown": "Публикация пропущена из-за паузы между ответами.",
    "approved": "Ответ подготовлен, ожидает публикации.",
    "publishing": "Публикация выполняется.",
}

_FALLBACK_STATUS_TEXT = "Ответ не опубликован."


def publication_status_text(status: str) -> str:
    """Operator-facing sentence for a channel publication status."""
    return PUBLICATION_STATUS_TEXT.get(status, _FALLBACK_STATUS_TEXT)


def first_defined(*values: object) -> object:
    """First value that is not ``None``; used for channel -> agent -> account overrides."""
    for value in values:
        if value is not None:
            return value
    return None
