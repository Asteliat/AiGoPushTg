"""Persisted operator setting that controls Telegram verification handling.

The single source of truth is the ``challenge_mode`` application setting. The legacy
boolean ``challenge_monitor_enabled`` switch is still understood (older deployments
keep it), and it is removed as soon as the mode is saved from the bot.
"""

from __future__ import annotations

from app.services.challenge_monitor import ChallengeMode, parse_challenge_mode
from app.storage.db import SessionFactory
from app.storage.models import AppSetting

MODE_SETTING_KEY = "challenge_mode"
LEGACY_SETTING_KEY = "challenge_monitor_enabled"

_LEGACY_OFF_VALUES = frozenset({"false", "0", "no", "off", "disabled"})


async def read_challenge_mode() -> ChallengeMode:
    """Resolve the configured mode, defaulting to automatic solving."""
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, MODE_SETTING_KEY)
        if setting is not None and setting.value.strip():
            return parse_challenge_mode(setting.value)
        legacy = await db.get(AppSetting, LEGACY_SETTING_KEY)
    if legacy is None:
        return ChallengeMode.AUTO
    if legacy.value.strip().casefold() in _LEGACY_OFF_VALUES:
        return ChallengeMode.OFF
    # The legacy switch only meant "watch for prompts"; watching now means solving.
    return ChallengeMode.AUTO


async def write_challenge_mode(mode: ChallengeMode) -> None:
    """Store the mode and drop the legacy switch so there is one authority."""
    async with SessionFactory() as db:
        setting = await db.get(AppSetting, MODE_SETTING_KEY)
        if setting is None:
            db.add(AppSetting(key=MODE_SETTING_KEY, value=mode.value))
        else:
            setting.value = mode.value
        legacy = await db.get(AppSetting, LEGACY_SETTING_KEY)
        if legacy is not None:
            await db.delete(legacy)
        await db.commit()
