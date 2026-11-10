"""Dialog discovery: keep ChatBinding in sync with the account's real dialogs."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from telethon import utils
from telethon.errors import FloodWaitError, MsgIdInvalidError
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from app.services.chat_bindings import discovered_binding_values
from app.storage.db import SessionFactory
from app.storage.models import ChannelBinding, ChatBinding
from app.workers.channel_monitor import has_channel_post_content


async def sync_account_dialogs(
    client, aid: int, *, last_discovery: dict, attempt: int = 0
) -> dict[int, str]:
        discovered = []
        try:
            # Resolve linked discussion groups before dialog discovery so they never
            # enter the ordinary Chats list or its auto-learning sample by accident.
            async with SessionFactory() as db:
                unresolved_channels = list((await db.scalars(
                    select(ChannelBinding).where(
                        ChannelBinding.account_id == aid,
                        ChannelBinding.discussion_group_id.is_(None),
                    )
                )).all())
            for channel_binding in unresolved_channels:
                try:
                    channel_entity = await client.get_entity(channel_binding.channel_id)
                    async for channel_post in client.iter_messages(channel_entity, limit=5):
                        if not has_channel_post_content(channel_post.message, channel_post):
                            continue
                        try:
                            discussion = await client(GetDiscussionMessageRequest(
                                peer=channel_entity, msg_id=int(channel_post.id)
                            ))
                        except MsgIdInvalidError:
                            logging.debug(
                                "No discussion thread on recent channel post; checking an older one: "
                                "account=%s channel_id=%s post_id=%s",
                                aid, int(channel_binding.channel_id), int(channel_post.id),
                            )
                            continue
                        group = next((
                            chat for chat in getattr(discussion, "chats", [])
                            if getattr(chat, "megagroup", False)
                        ), None)
                        if not group:
                            continue
                        group_peer_id = int(utils.get_peer_id(group))
                        async with SessionFactory() as db:
                            current_binding = await db.get(ChannelBinding, channel_binding.id)
                            if current_binding:
                                current_binding.discussion_group_id = group_peer_id
                                await db.commit()
                        logging.info(
                            "Linked channel discussion discovered before chat sync: "
                            "account=%s channel_id=%s post_id=%s group_id=%s",
                            aid, int(channel_binding.channel_id), int(channel_post.id), group_peer_id,
                        )
                        break
                except FloodWaitError as exc:
                    logging.warning(
                        "FloodWait while resolving linked discussion before chat sync: "
                        "account=%s channel_id=%s wait=%ss",
                        aid, int(channel_binding.channel_id), int(getattr(exc, "seconds", 0)),
                    )
                except Exception:
                    logging.exception(
                        "Could not resolve linked discussion before chat sync: account=%s channel_id=%s",
                        aid, int(channel_binding.channel_id),
                    )
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if (
                    getattr(entity, "deleted", False)
                    or getattr(entity, "bot", False)
                    or (getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False))
                ):
                    continue
                title = (dialog.name or "").strip()
                if not title:
                    continue
                discovered.append((int(dialog.id), title[:255]))
            async with SessionFactory() as db:
                discussion_ids = set((await db.scalars(
                    select(ChannelBinding.discussion_group_id).where(
                        ChannelBinding.account_id == aid,
                        ChannelBinding.discussion_group_id.is_not(None),
                    )
                )).all())
                channel_ids = set((await db.scalars(
                    select(ChannelBinding.channel_id).where(ChannelBinding.account_id == aid)
                )).all())
            discovered = [
                item for item in discovered
                if item[0] not in discussion_ids and item[0] not in channel_ids
            ]
            async with SessionFactory() as db:
                current_bindings = list((await db.scalars(
                    select(ChatBinding).where(ChatBinding.account_id == aid)
                )).all())
                by_chat = {binding.chat_id: binding for binding in current_bindings}
                for chat_id, title in discovered:
                    binding = by_chat.get(chat_id)
                    if binding:
                        binding.title = title
                    else:
                        db.add(ChatBinding(
                            account_id=aid, chat_id=chat_id,
                            **discovered_binding_values(title),
                        ))
                await db.commit()
            logging.info("Discovered %d eligible dialogs for account %s", len(discovered), aid)
        except FloodWaitError as exc:
            wait_seconds = max(1, int(getattr(exc, "seconds", 1))) + 1
            if attempt < 2:
                logging.warning("Telegram FloodWait while discovering dialogs for account %s; retry %s after %ss", aid, attempt + 1, wait_seconds)
                await asyncio.sleep(min(wait_seconds, 300))
                return await sync_account_dialogs(client, aid, last_discovery=last_discovery, attempt=attempt + 1)
            logging.error("Dialog discovery paused after repeated Telegram FloodWait for account %s", aid)
        except Exception:
            # A transient history/dialogs failure must not take the whole account offline.
            logging.exception("Could not refresh dialogs for account %s", aid)
        finally:
            last_discovery[aid] = datetime.now(timezone.utc)
        async with SessionFactory() as db:
            current_bindings = list((await db.scalars(
                select(ChatBinding).where(
                    ChatBinding.account_id == aid,
                    ChatBinding.enabled.is_(True),
                )
            )).all())
            discussion_ids = set((await db.scalars(
                select(ChannelBinding.discussion_group_id).where(
                    ChannelBinding.account_id == aid,
                    ChannelBinding.discussion_group_id.is_not(None),
                )
            )).all())
            channel_ids = set((await db.scalars(
                select(ChannelBinding.channel_id).where(ChannelBinding.account_id == aid)
            )).all())
        return {
            binding.chat_id: binding.title for binding in current_bindings
            if int(binding.chat_id) not in discussion_ids and int(binding.chat_id) not in channel_ids
        }
