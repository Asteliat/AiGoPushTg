"""Pass Telegram group verifications addressed to our own accounts.

The watcher is deliberately silent about other members' verifications: a group bot
posts one prompt per joining member, and reacting to all of them is what produced the
old 🛡 notification spam. A prompt is processed only when it replies to our message,
mentions our account or addresses it by name, and then the matching inline button is
pressed once (with Redis de-duplication and a per-chat cooldown).

Everything that is not "press this inline button" (typing an answer, external captcha
services, web forms) is intentionally out of scope: those prompts are reported to the
operator instead of being guessed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telethon import events
from telethon.errors import FloodWaitError

from app.services.challenge_monitor import (
    ChallengeMode,
    extract_button_labels,
    format_failed_log,
    format_solved_log,
    is_challenge_prompt,
    is_directed_to_account,
    solve_verification,
)
from app.services.challenge_settings import read_challenge_mode
from app.storage.models import Account
from app.workers import redis_keys

_MESSAGE_DEDUPE_TTL = 24 * 3600
_CHAT_COOLDOWN_SECONDS = 60
_CLICK_TIMEOUT_SECONDS = 25

SendLog = Callable[..., Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    """Minimal account data needed to recognise our own verification prompts."""

    account_id: int
    name: str
    user_id: int
    username: str | None
    names: tuple[str, ...]

    @classmethod
    def from_account(cls, account: Account) -> AccountIdentity:
        display_name = (account.display_name or "").strip()
        parts = tuple(part for part in display_name.split() if len(part) >= 3)
        names = tuple(dict.fromkeys((display_name, *parts))) if display_name else ()
        return cls(
            account_id=int(account.id),
            name=display_name or f"Аккаунт #{account.id}",
            user_id=int(account.telegram_user_id),
            username=(account.username or "").strip() or None,
            names=names,
        )


class ChallengeWatcher:
    """Presses verification buttons for one connected account."""

    def __init__(
        self,
        *,
        redis,
        send_log: SendLog,
        logger: logging.Logger | None = None,
    ) -> None:
        self._redis = redis
        self._send_log = send_log
        self._logger = logger or logging.getLogger(__name__)

    def install(self, client, account: Account) -> None:
        """Register the handler on an account's Telethon client."""
        identity = AccountIdentity.from_account(account)
        client.add_event_handler(
            lambda event, identity=identity: self.handle(event, identity),
            events.NewMessage(incoming=True),
        )

    async def handle(self, event, identity: AccountIdentity) -> None:
        try:
            await self._handle(event, identity)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("Challenge handling failed for account %s", identity.account_id)

    async def _handle(self, event, identity: AccountIdentity) -> None:
        if not event.is_group:
            return
        message = event.message
        if message is None or message.out:
            return
        sender = await event.get_sender()
        if sender is None or not bool(getattr(sender, "bot", False)):
            return

        body = message.message or ""
        labels = extract_button_labels(message)
        if not is_challenge_prompt(body, labels):
            return

        mode = await read_challenge_mode()
        if mode is ChallengeMode.OFF:
            return

        if not await self._is_our_challenge(event, message, identity, body):
            # Somebody else's verification. Never notify, never click.
            self._logger.debug(
                "Ignored foreign verification prompt: account=%s chat=%s message=%s",
                identity.account_id,
                event.chat_id,
                message.id,
            )
            return

        chat_id = int(event.chat_id or 0)
        message_key = redis_keys.challenge_seen(identity.account_id, chat_id, int(message.id))
        if not await self._redis.set(message_key, "1", nx=True, ex=_MESSAGE_DEDUPE_TTL):
            return
        cooldown_key = redis_keys.challenge_cooldown(identity.account_id, chat_id)
        if not await self._redis.set(cooldown_key, "1", nx=True, ex=_CHAT_COOLDOWN_SECONDS):
            return

        chat = await event.get_chat()
        chat_title = getattr(chat, "title", "") or ""
        sender_name = self._sender_name(sender)

        if mode is ChallengeMode.NOTIFY:
            await self._send_log(
                format_failed_log(
                    account_id=identity.account_id,
                    account_name=identity.name,
                    chat_title=chat_title,
                    sender_name=sender_name,
                    reason="режим «только уведомлять»: кнопка не нажата",
                    button_labels=labels,
                ),
                category="main",
            )
            return

        rows = [
            [str(getattr(button, "text", "")) for button in row] for row in (message.buttons or ())
        ]
        target = solve_verification(rows, body)
        if target is None:
            await self._send_log(
                format_failed_log(
                    account_id=identity.account_id,
                    account_name=identity.name,
                    chat_title=chat_title,
                    sender_name=sender_name,
                    reason="не удалось однозначно выбрать кнопку проверки",
                    button_labels=labels,
                ),
                category="errors",
            )
            return

        row_index, column_index = target
        button_text = rows[row_index][column_index]
        failure = await self._press(message, row_index, column_index)
        if failure is None:
            self._logger.info(
                "Passed Telegram verification: account=%s chat=%s button=%s",
                identity.account_id,
                chat_id,
                button_text,
            )
            await self._send_log(
                format_solved_log(
                    account_id=identity.account_id,
                    account_name=identity.name,
                    chat_title=chat_title,
                    sender_name=sender_name,
                    button_text=button_text,
                ),
                category="main",
            )
            return

        await self._send_log(
            format_failed_log(
                account_id=identity.account_id,
                account_name=identity.name,
                chat_title=chat_title,
                sender_name=sender_name,
                reason=failure,
                button_labels=labels,
            ),
            category="errors",
        )

    async def _is_our_challenge(self, event, message, identity: AccountIdentity, body: str) -> bool:
        """Cheap checks first; the reply lookup costs one API round-trip."""
        common = {
            "prompt_text": body,
            "account_user_id": identity.user_id,
            "account_username": identity.username,
            "account_names": identity.names,
            "mentioned": bool(getattr(message, "mentioned", False)),
        }
        if is_directed_to_account(reply_to_sender_id=None, **common):
            return True
        if getattr(message, "reply_to", None) is None:
            return False
        try:
            replied = await event.get_reply_message()
        except Exception:
            self._logger.debug(
                "Could not resolve replied message for challenge: account=%s chat=%s",
                identity.account_id,
                event.chat_id,
                exc_info=True,
            )
            return False
        reply_sender_id = None if replied is None else getattr(replied, "sender_id", None)
        return is_directed_to_account(reply_to_sender_id=reply_sender_id, **common)

    async def _press(self, message, row: int, column: int) -> str | None:
        """Click the inline button; return a short failure reason or ``None``."""
        try:
            await asyncio.wait_for(message.click(row, column), timeout=_CLICK_TIMEOUT_SECONDS)
        except FloodWaitError as exc:
            return f"Telegram ограничил нажатие: подождать {int(getattr(exc, 'seconds', 0))} с"
        except asyncio.TimeoutError:
            return f"нажатие не подтвердилось за {_CLICK_TIMEOUT_SECONDS} с"
        except Exception as exc:
            return f"{type(exc).__name__}: {str(exc)[:220]}"
        return None

    @staticmethod
    def _sender_name(sender) -> str:
        return str(
            getattr(sender, "username", None)
            or getattr(sender, "first_name", None)
            or getattr(sender, "title", None)
            or getattr(sender, "id", "bot")
        )
