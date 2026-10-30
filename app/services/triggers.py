"""Matching and formatting helpers for account/global chat triggers."""
from __future__ import annotations

import re


def parse_trigger_terms(raw: str) -> list[str]:
    """Parse one or more trigger words/phrases separated by commas, semicolons or lines."""
    values = []
    seen = set()
    for item in re.split(r"[,;\n]+", raw or ""):
        term = " ".join(item.split()).strip()
        key = term.casefold()
        if term and key not in seen:
            seen.add(key)
            values.append(term[:255])
    return values


def find_matching_term(message: str, terms: str | list[str]) -> str | None:
    """Return the first whole-word/phrase match, case-insensitively."""
    candidates = parse_trigger_terms(terms) if isinstance(terms, str) else terms
    text = message or ""
    for term in candidates:
        pattern = r"(?<!\w)" + re.escape(term.strip()) + r"(?!\w)"
        if term.strip() and re.search(pattern, text, flags=re.IGNORECASE | re.UNICODE):
            return term
    return None


def format_trigger_duration(seconds: int) -> str:
    value = max(0, int(seconds))
    days, value = divmod(value, 86400)
    hours, value = divmod(value, 3600)
    minutes, secs = divmod(value, 60)
    return f"{days:02d}:{hours:02d}:{minutes:02d}:{secs:02d}"
