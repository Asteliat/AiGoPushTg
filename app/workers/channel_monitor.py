"""Asynchronous monitor for explicitly enabled broadcast channels.

It analyzes public post text and linked discussion comments, stores analysis history, and
notifies the operator. It deliberately never publishes comments automatically.
"""
import asyncio
import logging

from sqlalchemy import select
from telethon import events
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from app.ai.provider import AIUnavailableError
from app.services.channels import analyze_channel_post
from app.services.ai_responses import global_ai_enabled
from app.storage.db import SessionFactory
from app.storage.models import Account, ChannelBinding


def install_channel_monitor(client, account_id: int, redis, send_log):
    active_posts: set[tuple[int, int]] = set()

    async def fetch_comments(group, root_id: int) -> list[str]:
        found = []
        async for item in client.iter_messages(group, reply_to=root_id, limit=20):
            text = (item.message or "").strip()
            if text and not item.out:
                found.append(text[:1000])
                if len(found) >= 5:
                    break
        found.reverse()
        return found

    async def handle(event):
        message = event.message
        if not message or not (message.message or "").strip():
            # Media-only posts have no text and are intentionally ignored.
            return
        if not event.is_channel or event.is_group:
            return
        channel_id = int(event.chat_id)
        post_id = int(message.id)
        key = (channel_id, post_id)
        if key in active_posts:
            return
        active_posts.add(key)
        lock_key = f"atcivperson:channel-post:{account_id}:{channel_id}:{post_id}"
        try:
            if not await redis.set(lock_key, "processing", nx=True, ex=7 * 24 * 3600):
                return
            async with SessionFactory() as db:
                account = await db.get(Account, account_id)
                binding = await db.scalar(select(ChannelBinding).where(
                    ChannelBinding.account_id == account_id,
                    ChannelBinding.channel_id == channel_id,
                    ChannelBinding.enabled.is_(True),
                ))
                if (
                    not account or not binding or not account.enabled or account.manual_pause
                    or not account.ai_enabled or not await global_ai_enabled(db)
                    or not account.ai_agent_id
                ):
                    return
                post_text = (message.message or "").strip()
                channel_title = binding.title
                wait_seconds = max(60, min(int(binding.comment_wait_seconds or 300), 3600))

            try:
                discussion = await client(GetDiscussionMessageRequest(peer=event.chat, msg_id=post_id))
            except Exception as exc:
                await send_log(
                    f"📡 Канал: {channel_title}\nАккаунт: #{account_id}\n"
                    f"Публикация #{post_id}: не удалось получить привязанную группу обсуждений "
                    f"({type(exc).__name__})."
                )
                await redis.set(lock_key, "no-discussion", ex=3600)
                return

            group_entity = next(
                (chat for chat in getattr(discussion, "chats", []) if getattr(chat, "megagroup", False)),
                None,
            )
            discussion_messages = getattr(discussion, "messages", [])
            root_message = next(
                (
                    item for item in discussion_messages
                    if getattr(getattr(item, "peer_id", None), "channel_id", None) == getattr(group_entity, "id", None)
                ),
                None,
            )
            if not group_entity or not root_message:
                await send_log(
                    f"📡 Канал: {channel_title}\nАккаунт: #{account_id}\n"
                    f"Публикация #{post_id}: у канала нет доступной привязанной группы обсуждений."
                )
                await redis.set(lock_key, "no-discussion", ex=3600)
                return

            comments = await fetch_comments(group_entity, int(root_message.id))
            if len(comments) < 2:
                # Do not block the account's chat listener; only this post task sleeps.
                await asyncio.sleep(wait_seconds)
                comments = await fetch_comments(group_entity, int(root_message.id))
            if len(comments) < 2:
                await send_log(
                    f"⏳ Канал: {channel_title}\nПубликация #{post_id}: за {wait_seconds} секунд "
                    f"не появилось двух комментариев. Анализ пропущен."
                )
                await redis.set(lock_key, "not-enough-comments", ex=3600)
                return

            async with SessionFactory() as db:
                account = await db.get(Account, account_id)
                binding = await db.scalar(select(ChannelBinding).where(
                    ChannelBinding.account_id == account_id,
                    ChannelBinding.channel_id == channel_id,
                    ChannelBinding.enabled.is_(True),
                ))
                if not account or not binding or account.manual_pause or not account.enabled:
                    return
                result = await analyze_channel_post(
                    db, account=account, channel=binding, post_id=post_id,
                    post_text=post_text, comments=comments[:5],
                )
                label = "по теме" if result.on_topic else "не по теме"
                await send_log(
                    f"📡 Анализ канала: {channel_title}\n"
                    f"Аккаунт: #{account_id} · Публикация #{post_id}\n"
                    f"Комментариев проанализировано: {result.comments_count}\n"
                    f"Обсуждение: {label}\n\n"
                    f"Сводка ИИ:\n{result.comments_summary[:1200]}\n\n"
                    f"Вариант текстового ответа (не опубликован):\n{result.suggested_response[:1800]}"
                )
            await redis.set(lock_key, "done", ex=30 * 24 * 3600)
        except AIUnavailableError as exc:
            logging.warning("Channel AI analysis unavailable for account %s: %s", account_id, exc)
            await send_log(f"⚠️ Не удалось проанализировать публикацию канала #{account_id}: {str(exc)[:700]}")
            await redis.delete(lock_key)
        except Exception:
            logging.exception("Channel post processing failed for account %s", account_id)
            await redis.delete(lock_key)
        finally:
            active_posts.discard(key)

    client.add_event_handler(handle, events.NewMessage(incoming=True))
