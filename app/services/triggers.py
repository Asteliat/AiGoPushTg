"""Matching and formatting helpers for account/global chat triggers."""
from __future__ import annotations

import re
import shlex


def parse_trigger_terms(raw: str) -> list[str]:
    """Parse comma-separated phrases or space-separated words; quote multiword terms."""
    source = raw or ""
    if re.search(r"[,;\\n]", source):
        candidates = [" ".join(item.split()).strip() for item in re.split(r"[,;\\n]+", source)]
    else:
        try:
            candidates = shlex.split(source)
        except ValueError:
            candidates = [source.strip()]
    values = []
    seen = set()
    for item in candidates:
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


def select_matching_trigger(local_rules, global_rules, message: str, *, has_local_rules: bool):
    """Prefer a matching account rule; then apply eligible global rules."""
    for rule in local_rules:
        term = find_matching_term(message, rule.terms)
        if term:
            return rule, term
    for rule in global_rules:
        if rule.apply_mode == "empty_only" and has_local_rules:
            continue
        term = find_matching_term(message, rule.terms)
        if term:
            return rule, term
    return None, None
