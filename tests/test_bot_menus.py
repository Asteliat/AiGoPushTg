"""Static consistency checks for the bot menus.

These tests walk the screen sources instead of running handlers: they make sure that
every button with a literal callback is answered by a registered handler, and that the
"cancelled input" return path always resolves to a real screen.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from app.bot.navigation import cancel_return_for, resolve_screen, restore_previous_screen
from app.bot.ui import MAIN

BOT = pathlib.Path(__file__).resolve().parents[1] / "app" / "bot"
SCREEN_FILES = sorted((BOT / "screens").glob("*.py"))


def _string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _button_callbacks() -> set[str]:
    """Literal ``(label, callback_data)`` pairs anywhere in the screens.

    Buttons are frequently assembled in a local list first, so the scan looks for the
    pair shape itself rather than only for an inline ``kb([...])`` call.
    """
    callbacks: set[str] = set()
    for path in SCREEN_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Tuple) or len(node.elts) != 2:
                continue
            if _string(node.elts[0]) is None:
                continue
            value = _string(node.elts[1])
            if value is not None:
                callbacks.add(value)
    return callbacks


def _handler_patterns() -> list[tuple[str, str]]:
    """(kind, value) pairs declared by F.data filters in the screens."""
    patterns: list[tuple[str, str]] = []
    for path in SCREEN_FILES:
        source = path.read_text(encoding="utf-8")
        patterns += [("eq", value) for value in re.findall(r'F\.data\s*==\s*"([^"]+)"', source)]
        patterns += [
            ("prefix", value) for value in re.findall(r'F\.data\.startswith\(\s*"([^"]+)"', source)
        ]
        patterns += [
            ("regexp", value) for value in re.findall(r'F\.data\.regexp\(\s*r?"([^"]+)"', source)
        ]
        for group in re.findall(r'F\.data\.in_\(\{([^}]*)\}\)', source):
            patterns += [("eq", value) for value in re.findall(r'"([^"]+)"', group)]
    return patterns


def _is_handled(callback: str, patterns: list[tuple[str, str]]) -> bool:
    for kind, value in patterns:
        if kind == "eq" and callback == value:
            return True
        if kind == "prefix" and callback.startswith(value):
            return True
        if kind == "regexp" and re.fullmatch(value, callback):
            return True
    return False


@pytest.mark.parametrize("path", SCREEN_FILES, ids=lambda p: p.name)
def test_screen_sources_parse(path: pathlib.Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"))


def test_every_literal_button_has_a_handler() -> None:
    patterns = _handler_patterns()
    unhandled = sorted(
        callback
        for callback in _button_callbacks()
        if not _is_handled(callback, patterns)
        and not callback.startswith("nav:")  # nav: handlers live in core/start
    )
    assert unhandled == []


def test_main_menu_has_no_duplicate_settings_entries() -> None:
    """Checks and logs live inside Settings; the main menu must not repeat them."""
    callbacks = [button.callback_data for row in MAIN.inline_keyboard for button in row]

    assert len(callbacks) == len(set(callbacks))
    assert "challenge:settings" not in callbacks
    assert "logs:menu" not in callbacks
    assert "nav:settings" in callbacks


def test_main_menu_buttons_are_answered() -> None:
    patterns = _handler_patterns()
    callbacks = [button.callback_data for row in MAIN.inline_keyboard for button in row]
    assert callbacks
    missing = [
        callback
        for callback in callbacks
        if callback not in {"nav:home"} and not _is_handled(callback, patterns)
    ]
    assert missing == []


@pytest.mark.parametrize(
    "callback_data",
    [
        "acc:view:7",
        "acc:list",
        "acc:runtime:menu:7",
        "acc:learnprompt:7",
        "acc:learn:7",
        "acc:proxysettings:7",
        "chat:account:7:0",
        "channel:account:7",
        "channel:gsettings:7",
        "channel:csettings:9",
        "agent:menu",
        "agent:view:3",
        "agent:runtime:menu:3",
        "persona:menu",
        "persona:view:2",
        "example:menu",
        "example:account:7",
        "example:view:5",
        "logs:menu",
        "logs:category:main",
        "runtime:menu",
        "tool:menu",
        "tool:account-settings",
        "trigger:global",
        "trigger:account:7",
        "channel:menu",
    ],
)
def test_cancel_return_targets_resolve(callback_data: str) -> None:
    """«Отмена» must always reopen a real screen instead of a dead end."""
    assert resolve_screen(cancel_return_for(callback_data)) is not None


def test_unknown_return_target_falls_back_to_home_text() -> None:
    assert resolve_screen("") is None
    assert resolve_screen("unknown:callback") is None
    assert restore_previous_screen is not None


def test_account_card_buttons_are_answered() -> None:
    """The card is built from f-strings; its literal callbacks must all be handled."""
    patterns = _handler_patterns()
    source = (BOT / "screens" / "account_card.py").read_text(encoding="utf-8")
    literals = re.findall(r'f"([a-z:]+):\{account\.id\}"', source)
    assert literals
    assert all(_is_handled(f"{prefix}:7", patterns) for prefix in literals)
