"""Telegram control bot: router assembly and the historical import surface.

Handlers live in :mod:`app.bot.screens`; this module keeps ``app.bot.ui.router``
working for :mod:`app.bot.main` and re-exports helpers that older code and tests
import from here.
"""

from __future__ import annotations

from app.bot import screens  # noqa: F401  (importing registers every handler)
from app.bot.core import MAIN, Flow, router
from app.bot.screens.channel_search import channel_entity
from app.bot.screens.chats import group_entity, parse_chat_link
from app.bot.screens.settings import log_destinations_from_value, log_ids_from_value
from app.bot.targets import isolated_account_client

# Backwards-compatible private aliases used by tests and older call sites.
_channel_entity = channel_entity
_group_entity = group_entity
_parse_chat_link = parse_chat_link

__all__ = [
    "Flow",
    "MAIN",
    "router",
    "_channel_entity",
    "_group_entity",
    "_parse_chat_link",
    "isolated_account_client",
    "log_destinations_from_value",
    "log_ids_from_value",
]
