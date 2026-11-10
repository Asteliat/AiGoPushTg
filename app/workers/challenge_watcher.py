"""Pass Telegram group verifications addressed to our own accounts.

The watcher is deliberately silent about other members' verifications: a group bot
posts one prompt per joining member, and reacting to all of them is what produced the
old 🛡 notification spam. A prompt is processed only when it replies to our message,
mentions our account or addresses it by name, and then the matching inline button is
pressed once (with Redis de-duplication and a per-chat cooldown).

Two variants are handled: a button inside the group, and the "open this bot and press
Start" flow — for the second one the watcher sends ``/start`` (with the deep-link
payload the group bot provided) to the verification bot and presses the button it
answers with.

Everything that is not "press this button" (typing a captcha answer, external captcha
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
from app.services.challenge_targets import (
    BotVerificationTarget,
    extract_bot_target,
    looks_like_verified_reply,
)
from app.storage.models import Account
from app.workers import redis_keys

_MESSAGE_DEDUPE_TTL = 24 * 3600
_CHAT_COOLDOWN_SECONDS = 60
_CLICK_TIMEOUT_SECONDS = 25
_BOT_DM_DEDUPE_TTL = 6 * 3600
_BOT_REPLY_TIMEOUT_SECONDS = 30
_BOT_REQUEST_TIMEOUT_SECONDS = 20

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

        rows, button_urls = self._button_state(message)
        target = solve_verification(rows, body)
        if target is None:
            bot_target = extract_bot_target(
                text=body, button_urls=button_urls, button_labels=labels
            )
            if bot_target is not None:
                await self._pass_in_bot_chat(
                    event, identity, bot_target, chat_title, sender_name, labels
                )
                return
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

    async def _pass_in_bot_chat(
        self,
        event,
        identity: AccountIdentity,
        target: BotVerificationTarget,
        chat_title: str,
        sender_name: str,
        labels: list[str],
    ) -> None:
        """Complete a verification that continues in the bot's private chat."""
        dm_key = redis_keys.challenge_bot_dm(identity.account_id, target.username)
        if not await self._redis.set(dm_key, "1", nx=True, ex=_BOT_DM_DEDUPE_TTL):
            self._logger.info(
                "Verification bot %s was already started for account %s",
                target.mention,
                identity.account_id,
            )
            return

        failure = await self._start_verification_bot(event.client, target)
        solution = f"личка {target.mention}"
        if failure is None:
            self._logger.info(
                "Passed Telegram verification in private chat: account=%s bot=%s",
                identity.account_id,
                target.mention,
            )
            await self._send_log(
                format_solved_log(
                    account_id=identity.account_id,
                    account_name=identity.name,
                    chat_title=chat_title,
                    sender_name=sender_name,
                    button_text=solution,
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

    async def _start_verification_bot(
        self, client, target: BotVerificationTarget
    ) -> str | None:
        """Send ``/start`` to the bot and press its verification button.

        Returns ``None`` when the bot confirmed the account, or a short failure
        reason for the operator. Only ``/start`` (with the deep-link payload the
        group bot provided) is ever sent: no answers are guessed.
        """
        try:
            entity = await asyncio.wait_for(
                client.get_entity(target.username), timeout=_BOT_REQUEST_TIMEOUT_SECONDS
            )
        except FloodWaitError as exc:
            return f"Telegram ограничил открытие бота: подождать {int(getattr(exc, 'seconds', 0))} с"
        except asyncio.TimeoutError:
            return f"бот {target.mention} не открылся за {_BOT_REQUEST_TIMEOUT_SECONDS} с"
        except Exception as exc:
            return f"бот {target.mention} недоступен: {type(exc).__name__}: {str(exc)[:160]}"

        if not bool(getattr(entity, "bot", False)):
            return f"{target.mention} оказался не ботом — проверить вручную"

        try:
            sent = await asyncio.wait_for(
                client.send_message(entity, target.start_command),
                timeout=_BOT_REQUEST_TIMEOUT_SECONDS,
            )
        except FloodWaitError as exc:
            return f"Telegram ограничил отправку боту: подождать {int(getattr(exc, 'seconds', 0))} с"
        except asyncio.TimeoutError:
            return f"бот {target.mention} не принял /start за {_BOT_REQUEST_TIMEOUT_SECONDS} с"
        except Exception as exc:
            return f"не удалось отправить /start боту {target.mention}: {type(exc).__name__}: {str(exc)[:160]}"

        reply = await self._wait_for_bot_reply(client, entity, int(getattr(sent, "id", 0)))
        if reply is None:
            return f"бот {target.mention} не ответил за {_BOT_REPLY_TIMEOUT_SECONDS} с"
        reply_text = str(getattr(reply, "message", "") or "")
        rows, _ = self._button_state(reply)
        if rows:
            choice = solve_verification(rows, reply_text)
            if choice is None:
                return f"бот {target.mention} просит выбрать вариант, который не распознан"
            return await self._press(reply, choice[0], choice[1])
        if looks_like_verified_reply(reply_text):
            return None
        return f"бот {target.mention} ответил без кнопки проверки — нужен оператор"

    async def _wait_for_bot_reply(self, client, entity, after_message_id: int):
        """Poll for the first bot message newer than our ``/start``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BOT_REPLY_TIMEOUT_SECONDS
        while True:
            try:
                messages = await client.get_messages(entity, min_id=after_message_id, limit=1)
            except FloodWaitError:
                return None
            except Exception:
                self._logger.debug(
                    "Could not read verification bot reply for min_id=%s",
                    after_message_id,
                    exc_info=True,
                )
                return None
            if messages:
                return messages[0]
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(2)

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

    @staticmethod
    def _button_state(message) -> tuple[list[list[str]], list[str]]:
        """Captions per row plus the URLs of link buttons.

        Link buttons are filled with an empty caption so the solver never picks
        them, while :func:`extract_bot_target` still sees their URL.
        """
        rows: list[list[str]] = []
        urls: list[str] = []
        for row in getattr(message, "buttons", None) or ():
            cells: list[str] = []
            for button in row:
                url = getattr(button, "url", None)
                if url:
                    urls.append(str(url))
                    cells.append("")
                else:
                    cells.append(str(getattr(button, "text", "") or ""))
            rows.append(cells)
        return rows, urls

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
