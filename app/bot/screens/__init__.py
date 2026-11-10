"""Bot screens, imported in the order their handlers are registered.

Every module registers its handlers on the shared router from
:mod:`app.bot.core`. Import order is the handler priority order, so this file is the
single place that decides which screen answers a callback first.
"""
from __future__ import annotations

from app.bot import navigation
from app.bot.screens import (  # noqa: F401  (importing registers handlers)
    account_messages,
    account_network,
    accounts,
    agents,
    channel_search,
    channels,
    chats,
    examples,
    learning,
    login,
    personas,
    profile,
    runtime,
    settings,
    start,
    tools,
    triggers,
)


def _part(data: str | None, index: int) -> int:
    return int((data or "").split(":")[index])


def _register_returns() -> None:
    """Screens reachable again after a cancelled input step."""
    navigation.register_screen("tool:menu", tools.tools_menu)
    navigation.register_screen("trigger:global", lambda c: triggers.trigger_screen(c, "global"))
    navigation.register_screen(
        "trigger:account:", lambda c: triggers.trigger_screen(c, "account", _part(c.data, 2))
    )
    navigation.register_screen("acc:list", accounts.acc_list)
    navigation.register_screen("acc:view:", accounts.acc_view)
    navigation.register_screen(
        "acc:runtime:menu:", lambda c: runtime.account_runtime_menu(c, _part(c.data, 3))
    )
    navigation.register_screen(
        "acc:learnprompt:", lambda c: learning.acc_learning_prompt(c, _part(c.data, 2))
    )
    navigation.register_screen(
        "acc:learn:", lambda c: learning.acc_learning_menu(c, _part(c.data, 2))
    )
    navigation.register_screen(
        "acc:proxysettings:", lambda c: account_network.acc_proxy_settings(c, _part(c.data, 2))
    )
    navigation.register_screen(
        "channel:account:", lambda c: channels.channel_account_screen(c, _part(c.data, 2))
    )
    navigation.register_screen(
        "channel:gsettings:",
        lambda c: channels.channel_settings_screen(c, "global", _part(c.data, 2)),
    )
    navigation.register_screen(
        "channel:csettings:",
        lambda c: channels.channel_settings_screen(c, "channel", _part(c.data, 2)),
    )
    navigation.register_screen(
        "chat:account:",
        lambda c: chats.chat_account_screen(
            c, _part(c.data, 2), _part(c.data, 3) if len((c.data or "").split(":")) > 3 else 0
        ),
    )
    navigation.register_screen("agent:menu", agents.agents_cb)
    navigation.register_screen("agent:view:", lambda c: agents.agent_view(c, _part(c.data, 2)))
    navigation.register_screen(
        "agent:runtime:menu:", lambda c: runtime.agent_runtime_menu(c, _part(c.data, 3))
    )
    navigation.register_screen("persona:menu", personas.persona_menu)
    navigation.register_screen(
        "persona:view:", lambda c: personas.persona_view(c, _part(c.data, 2))
    )
    navigation.register_screen("example:menu", examples.example_menu)
    navigation.register_screen(
        "example:account:", lambda c: examples.account_examples(c, _part(c.data, 2))
    )
    navigation.register_screen(
        "example:view:", lambda c: examples.example_view(c, _part(c.data, 2))
    )
    navigation.register_screen("logs:menu", settings.logs_menu)
    navigation.register_screen(
        "logs:category:", lambda c: settings.logs_category(c, (c.data or "").split(":")[2])
    )
    navigation.register_screen("runtime:menu", runtime.runtime_settings_menu)
    navigation.register_screen("channel:menu", channels.channels_legacy_redirect)


_register_returns()
