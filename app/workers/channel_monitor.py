"""Reliable per-post channel monitor with recovery after worker restarts.

Only text-bearing channel posts are considered. Each post is tracked in the database,
so Redis loss/restarts cannot cause completed analyses to be repeated. Replies use the
account reply mode: automatic mode publishes them, manual mode sends drafts to the operator.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
import random

from sqlalchemy import select
from telethon import events, utils
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from app.ai.provider import AIUnavailableError
from app.services.channels import analyze_channel_post, channel_comment_threshold_action
from app.services.ai_responses import generate_response, global_ai_enabled
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, ChannelAnalysis, ChannelBinding, ChannelPostState


SCAN_INTERVAL_SECONDS = 60
RECOVERY_SCAN_LIMIT = 500
PROCESSING_STALE_MINUTES = 20


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def install_channel_monitor(client, account_id: int, redis, send_log):
    active_posts: set[tuple[int, int]] = set()
    background_tasks: set[asyncio.Task] = set()
    discussion_channels: dict[int, int] = {}

    def spawn(coro):
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task

    async def update_state(channel_id: int, post_id: int, **values) -> None:
        async with SessionFactory() as db:
            state = await db.scalar(select(ChannelPostState).where(
                ChannelPostState.account_id == account_id,
                ChannelPostState.channel_id == channel_id,
                ChannelPostState.post_id == post_id,
            ))
            if state:
                for key, value in values.items():
                    setattr(state, key, value)
                state.updated_at = _utcnow()
                await db.commit()

    async def register_discussion_group(channel_id: int, group) -> None:
        group_peer_id = int(utils.get_peer_id(group))
        for old_group_id, mapped_channel_id in list(discussion_channels.items()):
            if mapped_channel_id == channel_id and old_group_id != group_peer_id:
                discussion_channels.pop(old_group_id, None)
        discussion_channels[group_peer_id] = int(channel_id)
        async with SessionFactory() as db:
            binding = await db.scalar(select(ChannelBinding).where(
                ChannelBinding.account_id == account_id,
                ChannelBinding.channel_id == channel_id,
            ))
            if binding and binding.discussion_group_id != group_peer_id:
                binding.discussion_group_id = group_peer_id
                await db.commit()

    async def fetch_comments(group, root_id: int) -> list[str]:
        found = []
        async for item in client.iter_messages(group, reply_to=root_id, limit=30):
            text = (item.message or "").strip()
            if text and not item.out:
                found.append(text[:1000])
                if len(found) >= 10:
                    break
        found.reverse()
        return found

    async def process_post(channel_entity, channel_id: int, post_id: int, post_text: str, channel_title: str):
        key = (channel_id, post_id)
        if key in active_posts or not post_text.strip():
            return
        active_posts.add(key)
        lock_key = f"atcivperson:channel-post:{account_id}:{channel_id}:{post_id}"
        lock_acquired = False
        try:
            # Check account/channel eligibility before acquiring a lock. A paused account
            # must not leave a seven-day Redis lock that blocks processing after resume.
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
                wait_value = binding.comment_wait_seconds_override if binding.comment_wait_seconds_override is not None else account.channel_wait_seconds
                wait_seconds = max(0, int(wait_value if wait_value is not None else binding.comment_wait_seconds))
                min_comments = max(1, min(10, int(binding.min_comments_override if binding.min_comments_override is not None else account.channel_min_comments or 2)))
                insufficient_action = binding.insufficient_action_override or account.channel_insufficient_action or "wait"

                # One distributed lock covers both the stored-analysis recovery path and
                # the fresh-analysis path, preventing duplicate operator notifications.
                lock_acquired = bool(await redis.set(lock_key, "processing", nx=True, ex=30 * 60))
                if not lock_acquired:
                    return

                existing_analysis = await db.scalar(select(ChannelAnalysis).where(
                    ChannelAnalysis.account_id == account_id,
                    ChannelAnalysis.channel_id == channel_id,
                    ChannelAnalysis.post_id == post_id,
                ))
                if existing_analysis:
                    state = await db.scalar(select(ChannelPostState).where(
                        ChannelPostState.account_id == account_id,
                        ChannelPostState.channel_id == channel_id,
                        ChannelPostState.post_id == post_id,
                    ))
                    if state and state.status != "done":
                        label = "по теме" if existing_analysis.on_topic else "не по теме"
                        notification = (
                            f"📡 Анализ канала: {existing_analysis.channel_title or channel_title}\n"
                            f"Аккаунт: #{account_id} · Публикация #{post_id}\n"
                            f"Комментариев проанализировано: {existing_analysis.comments_count}\n"
                            f"Обсуждение: {label}\n\n"
                            f"Сводка ИИ:\n{existing_analysis.comments_summary[:1200]}\n\n"
                            f"Вариант текстового ответа (не опубликован):\n"
                            f"{existing_analysis.suggested_response[:1800]}"
                        )
                        # Analysis may have been committed just before a crash or a
                        # notification failure. Re-send the stored result, never re-run AI.
                        await send_log(notification, category="channels")
                        state.status = "done"
                        state.next_retry_at = None
                        state.last_error = ""
                        state.updated_at = _utcnow()
                        await db.commit()
                    elif state is None:
                        db.add(ChannelPostState(
                            account_id=account_id, channel_id=channel_id, post_id=post_id,
                            channel_title=channel_title[:255], post_text=post_text[:12000],
                            status="done", attempts=0, updated_at=_utcnow(),
                        ))
                        await db.commit()
                    await redis.set(lock_key, "done", ex=365 * 24 * 3600)
                    return

                state = await db.scalar(select(ChannelPostState).where(
                    ChannelPostState.account_id == account_id,
                    ChannelPostState.channel_id == channel_id,
                    ChannelPostState.post_id == post_id,
                ).with_for_update())
                now = _utcnow()
                if state:
                    if state.status == "done":
                        await redis.set(lock_key, "done", ex=365 * 24 * 3600)
                        return
                    if state.next_retry_at and state.next_retry_at > now:
                        return
                    if state.status == "processing" and state.updated_at > now - timedelta(minutes=PROCESSING_STALE_MINUTES):
                        return
                    state.status = "processing"
                    state.attempts += 1
                    state.post_text = post_text[:12000]
                    state.channel_title = channel_title[:255]
                    state.next_retry_at = None
                    state.last_error = ""
                    state.updated_at = now
                else:
                    state = ChannelPostState(
                        account_id=account_id,
                        channel_id=channel_id,
                        post_id=post_id,
                        channel_title=channel_title[:255],
                        post_text=post_text[:12000],
                        status="processing",
                        attempts=1,
                        updated_at=now,
                    )
                    db.add(state)
                await db.commit()

            try:
                discussion = await client(GetDiscussionMessageRequest(peer=channel_entity, msg_id=post_id))
            except Exception as exc:
                await update_state(
                    channel_id, post_id, status="retry",
                    next_retry_at=_utcnow() + timedelta(minutes=10),
                    last_error=f"discussion lookup: {type(exc).__name__}",
                )
                logging.info("Discussion lookup failed for account %s channel %s post %s: %s",
                             account_id, channel_id, post_id, exc)
                return

            group_entity = next(
                (chat for chat in getattr(discussion, "chats", []) if getattr(chat, "megagroup", False)),
                None,
            )
            discussion_messages = getattr(discussion, "messages", [])
            root_message = next(
                (
                    item for item in discussion_messages
                    if getattr(getattr(item, "peer_id", None), "channel_id", None)
                    == getattr(group_entity, "id", None)
                ),
                None,
            )
            if not group_entity or not root_message:
                await update_state(
                    channel_id, post_id, status="retry",
                    next_retry_at=_utcnow() + timedelta(hours=1),
                    last_error="linked discussion group unavailable",
                )
                return

            await register_discussion_group(channel_id, group_entity)
            comments = await fetch_comments(group_entity, int(root_message.id))
            if len(comments) < min_comments:
                if insufficient_action == "skip":
                    await update_state(
                        channel_id, post_id, status="done", next_retry_at=None,
                        last_error=f"skipped: {len(comments)} comments, minimum {min_comments}",
                    )
                    await redis.set(lock_key, "done", ex=365 * 24 * 3600)
                else:
                    await update_state(
                        channel_id, post_id, status="waiting",
                        next_retry_at=_utcnow() + timedelta(seconds=wait_seconds),
                        last_error=f"waiting: {len(comments)} comments, minimum {min_comments}",
                    )
                return

            async with SessionFactory() as db:
                account = await db.get(Account, account_id)
                binding = await db.scalar(select(ChannelBinding).where(
                    ChannelBinding.account_id == account_id,
                    ChannelBinding.channel_id == channel_id,
                    ChannelBinding.enabled.is_(True),
                ))
                if not account or not binding or account.manual_pause or not account.enabled:
                    await update_state(
                        channel_id, post_id, status="retry",
                        next_retry_at=_utcnow() + timedelta(minutes=2),
                        last_error="account or channel paused during processing",
                    )
                    return
                result = await analyze_channel_post(
                    db, account=account, channel=binding, post_id=post_id,
                    post_text=post_text, comments=comments[:10],
                )
                # The unique constraint in channel_analyses is the final duplicate guard.
                label = "по теме" if result.on_topic else "не по теме"
                notification = (
                    f"📡 Анализ канала: {channel_title}\n"
                    f"Аккаунт: #{account_id} · Публикация #{post_id}\n"
                    f"Комментариев проанализировано: {result.comments_count}\n"
                    f"Обсуждение: {label}\n\n"
                    f"Сводка ИИ:\n{result.comments_summary[:1200]}\n\n"
                    f"Вариант текстового ответа (не опубликован):\n{result.suggested_response[:1800]}"
                )
            # Mark the durable state complete only after the operator notification succeeds.
            await send_log(notification, category="channels")
            await update_state(channel_id, post_id, status="done", next_retry_at=None, last_error="")
            await redis.set(lock_key, "done", ex=365 * 24 * 3600)
        except AIUnavailableError as exc:
            logging.warning("Channel AI analysis unavailable for account %s: %s", account_id, exc)
            await update_state(
                channel_id, post_id, status="retry",
                next_retry_at=_utcnow() + timedelta(minutes=5),
                last_error=str(exc)[:500],
            )
            await send_log(
                f"⚠️ Не удалось проанализировать публикацию #{post_id} канала "
                f"{channel_title}: {str(exc)[:500]}. Повторная попытка будет позже.", category="errors"
            )
        except Exception as exc:
            logging.exception("Channel post processing failed for account %s", account_id)
            await update_state(
                channel_id, post_id, status="retry",
                next_retry_at=_utcnow() + timedelta(minutes=5),
                last_error=f"{type(exc).__name__}: {str(exc)[:400]}",
            )
            await send_log(
                f"⚠️ Ошибка обработки публикации #{post_id} канала {channel_title}: "
                f"{type(exc).__name__}: {str(exc)[:400]}",
                category="errors",
            )
        finally:
            try:
                if lock_acquired:
                    # Successful posts retain a long-lived done marker; retryable posts
                    # release the lock so the recovery scanner can retry them later.
                    async with SessionFactory() as db:
                        state_status = await db.scalar(select(ChannelPostState.status).where(
                            ChannelPostState.account_id == account_id,
                            ChannelPostState.channel_id == channel_id,
                            ChannelPostState.post_id == post_id,
                        ))
                    if state_status != "done":
                        await redis.delete(lock_key)
            except Exception:
                logging.exception("Could not release channel post lock for account %s", account_id)
            finally:
                active_posts.discard(key)


    async def handle_discussion_comment(event):
        message = event.message
        if not event.is_group or not message or message.out or not (message.message or "").strip():
            return
        if not getattr(message, "reply_to", None):
            return
        try:
            sender = await event.get_sender()
            if getattr(sender, "bot", False):
                return
        except Exception:
            pass
        channel_id = discussion_channels.get(int(event.chat_id))
        if not channel_id:
            return
        post_id = int(getattr(message.reply_to, "reply_to_top_id", None) or message.reply_to.reply_to_msg_id)
        comment_id = int(message.id)
        lock_key = f"atcivperson:channel-comment:{account_id}:{channel_id}:{comment_id}"
        locked = await redis.set(lock_key, "processing", nx=True, ex=2147483647)
        if not locked:
            return
        try:
            async with SessionFactory() as db:
                account = await db.get(Account, account_id)
                binding = await db.scalar(select(ChannelBinding).where(
                    ChannelBinding.account_id == account_id,
                    ChannelBinding.channel_id == channel_id,
                    ChannelBinding.enabled.is_(True),
                    ChannelBinding.reply_enabled.is_(True),
                ))
                if (
                    not account or not binding or not account.enabled or account.manual_pause
                    or not account.ai_enabled or not await global_ai_enabled(db)
                    or not account.ai_agent_id
                    or int(message.sender_id or 0) == int(account.telegram_user_id)
                ):
                    await redis.delete(lock_key)
                    return
                agent = await db.get(AIAgent, account.ai_agent_id)
                if not agent or not agent.enabled:
                    await redis.delete(lock_key)
                    return
                probability = (
                    binding.reply_probability_override
                    if binding.reply_probability_override is not None
                    else agent.channel_reply_probability_override
                    if agent.channel_reply_probability_override is not None
                    else account.channel_reply_probability
                )
                probability = max(0, min(100, int(probability)))
                if probability == 0 or random.randint(1, 100) > probability:
                    await redis.set(lock_key, "skipped", ex=30 * 24 * 3600)
                    return
                min_comments = max(1, min(10, int(
                    binding.min_comments_override
                    if binding.min_comments_override is not None
                    else account.channel_min_comments or 2
                )))
                insufficient_action = binding.insufficient_action_override or account.channel_insufficient_action or "wait"
                wait_value = binding.comment_wait_seconds_override if binding.comment_wait_seconds_override is not None else account.channel_wait_seconds
                wait_seconds = max(0, int(wait_value if wait_value is not None else binding.comment_wait_seconds))
                post_instructions = binding.post_instructions_override if binding.post_instructions_override is not None else account.channel_post_instructions
                comments_instructions = binding.comments_instructions_override if binding.comments_instructions_override is not None else account.channel_comments_instructions
                restrictions = binding.restrictions_instructions_override if binding.restrictions_instructions_override is not None else account.channel_restrictions_instructions
                learned = binding.learning_prompt
                title = binding.title or f"канал {channel_id}"
                mode = account.reply_mode
                account_name = account.display_name

            comments = await fetch_comments(event.chat, post_id)
            threshold_action = channel_comment_threshold_action(insufficient_action, len(comments), min_comments)
            if threshold_action == "skip":
                await redis.set(lock_key, "skipped", ex=30 * 24 * 3600)
                return
            if threshold_action == "wait":
                if wait_seconds:
                    await asyncio.sleep(wait_seconds)
                comments = await fetch_comments(event.chat, post_id)
                threshold_action = channel_comment_threshold_action(insufficient_action, len(comments), min_comments)
                if threshold_action == "skip":
                    await redis.set(lock_key, "skipped", ex=30 * 24 * 3600)
                    return
                if threshold_action == "wait":
                    # A later incoming comment will trigger a fresh attempt.
                    await redis.delete(lock_key)
                    return

            cooldown = (
                binding.reply_cooldown_seconds_override
                if binding.reply_cooldown_seconds_override is not None
                else agent.channel_reply_cooldown_seconds_override
                if agent.channel_reply_cooldown_seconds_override is not None
                else account.channel_reply_cooldown_seconds
            )
            cooldown = max(0, int(cooldown or 0))
            if cooldown:
                allowed = await redis.set(
                    f"atcivperson:channel-reply-cooldown:{account_id}:{channel_id}",
                    "1", nx=True, ex=cooldown,
                )
                if not allowed:
                    await redis.set(lock_key, "skipped", ex=30 * 24 * 3600)
                    return

            try:
                discussion_context = await client.get_messages(event.chat_id, ids=post_id)
                post_text = (getattr(discussion_context, "message", None) or "").strip()
            except Exception:
                post_text = ""
            additional = "\n".join(x for x in [
                "Инструкции по публикациям: " + post_instructions if post_instructions.strip() else "",
                "Что учитывать в комментариях: " + comments_instructions if comments_instructions.strip() else "",
                "Ограничения: " + restrictions if restrictions.strip() else "",
                "Выводы автообучения: " + learned if learned.strip() else "",
            ] if x)
            context = (
                f"Канал: {title}\nПубликация: {post_text[:2500]}\n"
                "Недавние комментарии в этом обсуждении:\n"
                + "\n".join(f"- {item[:500]}" for item in comments[-10:])
            )
            async with SessionFactory() as db:
                draft = await generate_response(
                    db, incoming_text=message.message.strip(), account_id=account_id,
                    chat_label=f"{account_name} · комментарии {title}",
                    conversation_context=context, additional_instructions=additional,
                )
                latest_account = await db.get(Account, account_id)
                latest_binding = await db.scalar(select(ChannelBinding).where(
                    ChannelBinding.account_id == account_id,
                    ChannelBinding.channel_id == channel_id,
                    ChannelBinding.enabled.is_(True),
                    ChannelBinding.reply_enabled.is_(True),
                ))
                if (
                    not latest_account or not latest_binding or latest_account.manual_pause
                    or not latest_account.enabled or not latest_account.ai_enabled
                ):
                    await redis.delete(lock_key)
                    return
                mode = latest_account.reply_mode
                account_name = latest_account.display_name
            if mode == "automatic":
                await event.reply(draft.proposed_text)
                await send_log(
                    f"✅ Ответ в комментариях отправлен\nАккаунт: {account_name} (#{account_id})\n"
                    f"Канал: {title}\nКомментарий: {message.message[:700]}\nОтвет: {draft.proposed_text[:1200]}", category="replies"
                )
            else:
                await send_log(
                    f"💬 Подготовлен ответ в комментариях\nАккаунт: {account_name} (#{account_id})\n"
                    f"Канал: {title}\nКомментарий: {message.message[:700]}\n"
                    f"Ответ ИИ: {draft.proposed_text[:1400]}\n"
                    "Ручной режим: ответ не опубликован.", category="replies"
                )
            await redis.set(lock_key, "done", ex=30 * 24 * 3600)
        except asyncio.CancelledError:
            await redis.delete(lock_key)
            raise
        except Exception as exc:
            await redis.delete(lock_key)
            logging.exception("Channel comment reply failed for account %s channel %s", account_id, channel_id)
            await send_log(
                f"⚠️ Не удалось ответить в комментариях\nАккаунт: #{account_id} · канал {channel_id}\n"
                f"Ошибка: {type(exc).__name__}: {str(exc)[:500]}", category="errors"
            )

    async def handle(event):
        message = event.message
        if not message or not (message.message or "").strip():
            return
        if not event.is_channel or event.is_group:
            return
        channel_id = int(event.chat_id)
        spawn(process_post(event.chat, channel_id, int(message.id), message.message, getattr(event.chat, "title", "") or "Канал"))

    async def recovery_loop():
        """Revisit recent channel history to recover missed events and due retries."""
        while True:
            if not client.is_connected():
                return
            try:
                async with SessionFactory() as db:
                    saved_mappings = list((await db.execute(
                        select(ChannelBinding.channel_id, ChannelBinding.discussion_group_id).where(
                            ChannelBinding.account_id == account_id,
                            ChannelBinding.enabled.is_(True),
                            ChannelBinding.discussion_group_id.is_not(None),
                        )
                    )).all())
                for saved_channel_id, saved_group_id in saved_mappings:
                    discussion_channels[int(saved_group_id)] = int(saved_channel_id)
                async with SessionFactory() as db:
                    bindings = list((await db.scalars(select(ChannelBinding).where(
                        ChannelBinding.account_id == account_id,
                        ChannelBinding.enabled.is_(True),
                    ))).all())
                for binding in bindings:
                    try:
                        async with SessionFactory() as db:
                            known_states = list((await db.scalars(select(ChannelPostState).where(
                                ChannelPostState.account_id == account_id,
                                ChannelPostState.channel_id == binding.channel_id,
                            ))).all())
                        state_by_post = {item.post_id: item for item in known_states}
                        now = _utcnow()
                        entity = await client.get_entity(binding.channel_id)
                        # Discover the linked discussion group even when every recent post
                        # already has a completed analysis (and would otherwise be skipped).
                        async for recent_post in client.iter_messages(entity, limit=RECOVERY_SCAN_LIMIT):
                            if (recent_post.message or "").strip():
                                try:
                                    discussion = await client(GetDiscussionMessageRequest(peer=entity, msg_id=recent_post.id))
                                    group = next((chat for chat in getattr(discussion, "chats", []) if getattr(chat, "megagroup", False)), None)
                                    if group:
                                        await register_discussion_group(int(binding.channel_id), group)
                                except Exception:
                                    pass
                                break
                        async for message in client.iter_messages(entity, limit=RECOVERY_SCAN_LIMIT):
                            body = (message.message or "").strip()
                            if not body:
                                continue
                            previous = state_by_post.get(int(message.id))
                            if previous:
                                if previous.status == "done":
                                    continue
                                if previous.next_retry_at and previous.next_retry_at > now:
                                    continue
                                if (
                                    previous.status == "processing"
                                    and previous.updated_at > now - timedelta(minutes=PROCESSING_STALE_MINUTES)
                                ):
                                    continue
                            else:
                                # Never backfill posts from before this channel was added.
                                created_at = binding.created_at
                                msg_date = message.date
                                if created_at and msg_date:
                                    if created_at.tzinfo is None:
                                        created_at = created_at.replace(tzinfo=timezone.utc)
                                    if msg_date < created_at:
                                        continue
                            spawn(process_post(
                                entity, int(binding.channel_id), int(message.id), body,
                                binding.title or getattr(entity, "title", "") or "Канал",
                            ))
                    except Exception:
                        logging.exception(
                            "Channel recovery scan failed for account %s channel %s",
                            account_id, binding.channel_id,
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Channel recovery loop failed for account %s", account_id)
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    client.add_event_handler(handle, events.NewMessage(incoming=True))
    client.add_event_handler(handle_discussion_comment, events.NewMessage(incoming=True))
    recovery_task = spawn(recovery_loop())
    return recovery_task
