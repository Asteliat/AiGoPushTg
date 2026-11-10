"""Candidate lists kept in memory during chat and channel discovery.

The lists live in the bot process (``CHAT_CANDIDATES`` / ``CHANNEL_CANDIDATES``), so a
restart or an expired keyboard leaves a button pointing at a list that is gone. Both
screens answer that case the same way: explain it and offer a fresh search instead of
an alert that leaves the keyboard dead.
"""
from __future__ import annotations

from collections.abc import Sequence


def candidate_at(candidates: Sequence[dict], index: int) -> dict | None:
    """Candidate by button index, or ``None`` when the list is gone or shorter."""
    if index < 0 or index >= len(candidates):
        return None
    return candidates[index]
