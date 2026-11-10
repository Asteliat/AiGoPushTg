"""Return targets for the cancel button and screen resolution."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from app.bot.core import edit, kb


def cancel_return_for(callback_data: str) -> str:
    parts = callback_data.split(":")
    if callback_data.startswith("channel:edit:") and len(parts) >= 5:
        return f"channel:{'gsettings' if parts[2] == 'g' else 'csettings'}:{parts[3]}"
    if callback_data.startswith("acc:sendmessage:"): return f"acc:view:{parts[-1]}"
    if callback_data.startswith("acc:rt:input:"): return f"acc:runtime:menu:{parts[3]}"
    if callback_data.startswith("acc:context:edit:"): return f"acc:learnprompt:{parts[3]}"
    if callback_data.startswith("acc:personaoverride:"): return f"acc:view:{parts[2]}"
    if callback_data.startswith("trigger:account:"): return callback_data
    if callback_data.startswith("trigger:global"): return "trigger:global"
    if callback_data.startswith("acc:proxyset:"): return f"acc:proxysettings:{parts[2]}"
    if callback_data.startswith("chat:search:"): return "tool:menu"
    if callback_data.startswith("chat:joinlink:"): return f"acc:view:{parts[2]}"
    if callback_data.startswith("logs:add:"): return f"logs:category:{parts[2]}"
    if callback_data.startswith(("runtime:input:", "runtime:apply:")): return "runtime:menu"
    if callback_data.startswith(("agent:rt:input:", "agent:channelrt:input:")): return f"agent:runtime:menu:{parts[3]}"
    if callback_data.startswith("agent:edit:"): return f"agent:view:{parts[3]}"
    if callback_data.startswith("agent:"): return "agent:menu"
    if callback_data.startswith("persona:edit:"): return f"persona:view:{parts[2]}"
    if callback_data.startswith("persona:"): return "persona:menu"
    if callback_data.startswith("example:addacc:"): return f"example:account:{parts[2]}"
    if callback_data.startswith("example:add:"): return f"persona:view:{parts[2]}" if parts[2] != "0" else "example:menu"
    if callback_data.startswith("example:account:"): return callback_data
    if callback_data.startswith("example:"): return "example:menu"
    if callback_data.startswith("channel:"): return "channel:menu"
    if callback_data.startswith("acc:"): return f"acc:view:{parts[-1]}" if parts[-1].isdigit() else "acc:list"
    return callback_data

Screen = Callable[[CallbackQuery], Awaitable[None]]

_SCREENS: dict[str, Screen] = {}


def register_screen(prefix: str, handler: Screen) -> None:
    """Map a callback prefix to the screen that renders it."""
    _SCREENS[prefix] = handler


def resolve_screen(callback_data: str | None) -> Screen | None:
    """Longest matching prefix wins, so nested screens beat their parents."""
    if not callback_data:
        return None
    for prefix in sorted(_SCREENS, key=len, reverse=True):
        if callback_data == prefix or callback_data.startswith(prefix):
            return _SCREENS[prefix]
    return None


async def restore_previous_screen(
    c: CallbackQuery, callback_data: str | None, state: FSMContext | None = None
) -> None:
    """Reopen the screen the user came from after an input step was cancelled."""
    handler = resolve_screen(callback_data)
    if handler is not None:
        await handler(c)
        return
    fallback = callback_data or "nav:home"
    await edit(c, "Действие отменено. Вернитесь в предыдущий раздел:", kb([[("↩️ Назад", fallback)]]))
