"""Reliable per-post channel monitor with recovery after worker restarts.

Text and photo channel posts are analyzed independently from ordinary Chats.
Channel publication mode and its settings never inherit the Chats reply mode.
"""
import asyncio
import base64
import io
import logging
from datetime import datetime, timedelta, timezone
import random
from types import SimpleNamespace

from sqlalchemy import select
from telethon import events, utils
from telethon.errors import FloodWaitError, MsgIdInvalidError
from telethon.tl.functions.messages import GetDiscussionMessageRequest

from app.ai.provider import AIUnavailableError
from app.services.channels import (
    analyze_channel_post,
    effective_channel_chat_replies_enabled,
    effective_channel_reply_mode,
    learn_channel_style,
)
from app.services.ai_responses import global_ai_enabled
from app.storage.db import SessionFactory
from app.storage.models import Account, AIAgent, ChannelAnalysis, ChannelBinding, ChannelPostState


SCAN_INTERVAL_SECONDS = 60
RECOVERY_SCAN_LIMIT = 500
PROCESSING_STALE_MINUTES = 20
PUBLISHED_REPLY_POLL_LIMIT = 100
PUBLISHED_REPLY_COMMENT_LIMIT = 100
DISCUSSION_COMMENT_SCAN_LIMIT = 100
DISCUSSION_COMMENT_CONTEXT_LIMIT = 10

# Keep channel diagnostics visible even when the application root log level is WARNING.
logging.getLogger(__name__).setLevel(logging.INFO)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def has_channel_post_content(post_text: str | None, post_message=None) -> bool:
    """Only process channel posts that contain text or a supported photo."""
    return bool((post_text or "").strip() or getattr(post_message, "photo", None))


def is_legacy_comment_threshold_skip(last_error: str | None) -> bool:
    """Identify old terminal states that were incorrectly skipped for having few comments."""
    reason = (last_error or "").strip().lower()
    return reason.startswith("skipped by comment threshold:") or (
        reason.startswith("skipped:")
        and "comment" in reason
        and "minimum" in reason
    )


async def fetch_discussion_comments(
    client,
    *,
    channel_entity,
    channel_post_id: int,
    discussion_group,
    discussion_root_id: int,
) -> tuple[list[str], str]:
    """Read textual audience context from the linked thread using both Telegram peer forms.

    A channel post and its auto-forwarded root message in the linked megagroup have
    different message IDs. Try the discussion-group/root pair first, then Telethon's
    documented broadcast-channel/post pair as a fallback. No comment count gates analysis.
    """
    attempts = (
        ("discussion_group", discussion_group, int(discussion_root_id)),
        ("channel", channel_entity, int(channel_post_id)),
    )
    diagnostics: list[str] = []
    errors: list[Exception] = []
    successful_reads = 0

    for source, peer, reply_to_id in attempts:
        comments: list[str] = []
        seen = 0
        outgoing = 0
        without_text = 0
        try:
            async for item in client.iter_messages(
                peer,
                reply_to=reply_to_id,
                limit=DISCUSSION_COMMENT_SCAN_LIMIT,
            ):
                seen += 1
                if getattr(item, "out", False):
                    outgoing += 1
                    continue
                text = (getattr(item, "message", None) or "").strip()
                if not text:
                    without_text += 1
                    continue
                comments.append(text[:1000])
                if len(comments) >= DISCUSSION_COMMENT_CONTEXT_LIMIT:
                    break
            successful_reads += 1
            diagnostics.append(
                f"{source}: messages={seen}, text={len(comments)}, "
                f"outgoing={outgoing}, without_text={without_text}"
            )
            if comments:
                comments.reverse()
                return comments, "; ".join(diagnostics)
        except Exception as exc:
            errors.append(exc)
            diagnostics.append(f"{source}: error={type(exc).__name__}")
            logging.warning(
                "Could not fetch channel discussion comments via %s: %s", source, exc
            )

    if successful_reads == 0 and errors:
        raise errors[-1]
    return [], "; ".join(diagnostics) or "no discussion comments returned"


def install_channel_monitor(client, account_id: int, redis, send_log, route_chat_reply=None):
    active_posts: set[tuple[int, int]] = set()
    background_tasks: set[asyncio.Task] = set()
    active_channel_learning: set[int] = set()
    discussion_channels: dict[int, int] = {}
    discussion_entities: dict[int, object] = {}
    active_reply_polls: set[int] = set()

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
                discussion_entities.pop(old_group_id, None)
        discussion_channels[group_peer_id] = int(channel_id)
        discussion_entities[group_peer_id] = group
        async with SessionFactory() as db:
            binding = await db.scalar(select(ChannelBinding).where(
                ChannelBinding.account_id == account_id,
                ChannelBinding.channel_id == channel_id,
            ))
            if binding and binding.discussion_group_id != group_peer_id:
                binding.discussion_group_id = group_peer_id
                await db.commit()

    async def download_post_image_data_uri(message) -> str | None:
        """Download and normalize a Telegram photo for a vision-capable AI model."""
        if not getattr(message, "photo", None):
            return None
        try:
            raw = await client.download_media(message, file=bytes)
            if not raw:
                return None
            if not isinstance(raw, (bytes, bytearray)):
                raise TypeError("Telegram did not return photo bytes")
            if len(raw) > 20 * 1024 * 1024:
                raise AIUnavailableError("Фотография слишком большая для анализа (лимит 20 МБ).")
            from PIL import Image

            with Image.open(io.BytesIO(raw)) as source:
                source.load()
                source.thumbnail((1600, 1600))
                if source.mode not in {"RGB", "L"}:
                    source = source.convert("RGB")
                output = io.BytesIO()
                source.save(output, format="JPEG", quality=82, optimize=True)
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
            return "data:image/jpeg;base64," + encoded
        except AIUnavailableError:
            raise
        except Exception as exc:
            raise AIUnavailableError(
                f"Не удалось подготовить фотографию публикации: {type(exc).__name__}: {str(exc)[:180]}"
            ) from exc


    async def find_published_comment_ancestor(chat_id: int, message, immediate_parent=None, published_by_id=None):
        """Return the saved analysis when a reply chain descends from a published channel comment."""
        if published_by_id is None:
            async with SessionFactory() as db:
                rows = list((await db.scalars(select(ChannelAnalysis).where(
                    ChannelAnalysis.account_id == account_id,
                    ChannelAnalysis.discussion_group_id == int(chat_id),
                    ChannelAnalysis.publication_status == "published",
                    ChannelAnalysis.published_comment_id.is_not(None),
                ))).all())
            published_by_id = {int(row.published_comment_id): row for row in rows}
        if not published_by_id:
            return None
        header = getattr(message, "reply_to", None)
        parent_id = int(getattr(header, "reply_to_msg_id", 0) or 0)
        parent = immediate_parent
        for _ in range(8):
            if not parent_id:
                return None
            matched = published_by_id.get(parent_id)
            if matched:
                return matched
            if parent is None:
                try:
                    parent = await client.get_messages(chat_id, ids=parent_id)
                except Exception:
                    return None
            if not parent:
                return None
            parent_header = getattr(parent, "reply_to", None)
            parent_id = int(getattr(parent_header, "reply_to_msg_id", 0) or 0)
            parent = None
        return None


    async def find_account_message_ancestor(chat_id: int, message, immediate_parent, own_user_id: int):
        """Find an account-authored message in the reply chain, including replies not stored as analyses."""
        header = getattr(message, "reply_to", None)
        parent_id = int(getattr(header, "reply_to_msg_id", 0) or 0)
        parent = immediate_parent
        for _ in range(8):
            if not parent_id:
                return None
            if parent is None:
                try:
                    parent = await client.get_messages(chat_id, ids=parent_id)
                except Exception:
                    return None
            if not parent:
                return None
            sender_id = int(getattr(parent, "sender_id", 0) or 0)
            if bool(getattr(parent, "out", False)) or sender_id == int(own_user_id):
                return parent
            parent_header = getattr(parent, "reply_to", None)
            parent_id = int(getattr(parent_header, "reply_to_msg_id", 0) or 0)
            parent = None
        return None


    async def poll_published_comment_replies(binding, channel_entity) -> None:
        """Recover only replies addressed to a comment previously published by this account."""
        binding_id = int(binding.id)
        if binding_id in active_reply_polls:
            return
        active_reply_polls.add(binding_id)
        try:
            async with SessionFactory() as db:
                published_rows = list((await db.scalars(
                    select(ChannelAnalysis).where(
                        ChannelAnalysis.account_id == account_id,
                        ChannelAnalysis.channel_id == int(binding.channel_id),
                        ChannelAnalysis.publication_status == "published",
                        ChannelAnalysis.published_comment_id.is_not(None),
                        ChannelAnalysis.discussion_group_id.is_not(None),
                    ).order_by(ChannelAnalysis.created_at.desc()).limit(PUBLISHED_REPLY_POLL_LIMIT)
                )).all())

            for analysis in published_rows:
                group_peer_id = int(analysis.discussion_group_id)
                published_by_id = {
                    int(row.published_comment_id): row
                    for row in published_rows
                    if int(row.discussion_group_id) == group_peer_id
                }
                group_entity = discussion_entities.get(group_peer_id)
                if group_entity is None:
                    try:
                        group_entity = await client.get_entity(group_peer_id)
                        discussion_entities[group_peer_id] = group_entity
                    except Exception:
                        logging.exception(
                            "Could not resolve discussion group while polling own-comment replies: "
                            "account=%s group_id=%s", account_id, group_peer_id,
                        )
                        continue

                async for comment in client.iter_messages(
                    channel_entity,
                    reply_to=int(analysis.post_id),
                    limit=PUBLISHED_REPLY_COMMENT_LIMIT,
                ):
                    if (
                        comment.out or not (comment.message or "").strip()
                        or not getattr(comment, "reply_to", None)
                    ):
                        continue
                    async def get_reply_message(parent_id=int(getattr(comment.reply_to, "reply_to_msg_id", 0) or 0)):
                        return await client.get_messages(group_entity, ids=parent_id)

                    async def get_sender(comment=comment):
                        if not comment.sender_id:
                            return None
                        return await client.get_entity(comment.sender_id)

                    async def reply(text, comment=comment):
                        return await client.send_message(
                            group_entity, text, reply_to=int(comment.id)
                        )

                    event = SimpleNamespace(
                        message=comment,
                        chat_id=group_peer_id,
                        chat=group_entity,
                        client=client,
                        sender_id=int(comment.sender_id or 0),
                        is_group=True,
                        is_private=False,
                        is_channel=False,
                        get_reply_message=get_reply_message,
                        get_sender=get_sender,
                        published_by_id=published_by_id,
                        reply=reply,
                    )
                    await handle_reply_to_our_channel_comment(event)
        except asyncio.CancelledError:
            raise
        except FloodWaitError as exc:
            logging.warning(
                "Published-comment reply polling FloodWait: account=%s channel_id=%s wait=%ss",
                account_id, int(binding.channel_id), int(getattr(exc, "seconds", 0)),
            )
        except Exception:
            logging.exception(
                "Published-comment reply polling failed: account=%s channel_id=%s",
                account_id, int(binding.channel_id),
            )
        finally:
            active_reply_polls.discard(binding_id)


    async def process_post(channel_entity, channel_id: int, post_id: int, post_text: str, channel_title: str, post_message=None):
        key = (channel_id, post_id)
        post_text = post_text or ""
        if key in active_posts or not has_channel_post_content(post_text, post_message):
            return
        stored_post_text = post_text.strip() or "[Фотография без текстового описания]"
        active_posts.add(key)
        lock_key = f"atcivperson:channel-post:{account_id}:{channel_id}:{post_id}"
        cooldown_lock_key = None
        cooldown_lock_acquired = False
        lock_acquired = False
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
                ):
                    return

                lock_acquired = bool(await redis.set(lock_key, "processing", nx=True, ex=30 * 60))
                if not lock_acquired:
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
                    state.post_text = stored_post_text
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
                        post_text=stored_post_text,
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
                    next_retry_at=_utcnow() + timedelta(minutes=5),
                    last_error=f"discussion lookup: {type(exc).__name__}",
                )
                logging.exception(
                    "Channel discussion lookup failed: account=%s channel_id=%s post_id=%s",
                    account_id, channel_id, post_id,
                )
                return

            group_entity = next(
                (chat for chat in getattr(discussion, "chats", []) if getattr(chat, "megagroup", False)),
                None,
            )
            discussion_messages = getattr(discussion, "messages", [])
            root_message = next(
                (
                    item for item in discussion_messages
                    if group_entity
                    and getattr(getattr(item, "peer_id", None), "channel_id", None) == getattr(group_entity, "id", None)
                ),
                None,
            )
            if not group_entity or not root_message:
                await update_state(
                    channel_id, post_id, status="retry",
                    next_retry_at=_utcnow() + timedelta(minutes=30),
                    last_error="linked discussion group or root message unavailable",
                )
                logging.warning(
                    "Channel discussion target unavailable: account=%s channel_id=%s post_id=%s",
                    account_id, channel_id, post_id,
                )
                return

            await register_discussion_group(channel_id, group_entity)
            group_peer_id = int(utils.get_peer_id(group_entity))
            root_message_id = int(root_message.id)
            logging.info(
                "Channel post processing started: account=%s channel_id=%s post_id=%s "
                "discussion_group=%s root_message_id=%s",
                account_id, channel_id, post_id, group_peer_id, root_message_id,
            )

            comments, comment_fetch_diagnostics = await fetch_discussion_comments(
                client,
                channel_entity=channel_entity,
                channel_post_id=post_id,
                discussion_group=group_entity,
                discussion_root_id=root_message_id,
            )
            reply_metadata = (
                getattr(post_message, "replies", None)
                or getattr(root_message, "replies", None)
            )
            telegram_reply_count = getattr(reply_metadata, "replies", None)
            logging.info(
                "Channel discussion context loaded: account=%s channel_id=%s post_id=%s "
                "telegram_replies=%s text_comments=%s sources=[%s]; comment count does not gate analysis",
                account_id, channel_id, post_id, telegram_reply_count,
                len(comments), comment_fetch_diagnostics,
            )

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
                ):
                    await update_state(
                        channel_id, post_id, status="retry",
                        next_retry_at=_utcnow() + timedelta(minutes=2),
                        last_error="account or channel paused during processing",
                    )
                    return

                image_data_uri = await download_post_image_data_uri(post_message)
                result = await analyze_channel_post(
                    db, account=account, channel=binding, post_id=post_id,
                    post_text=stored_post_text, comments=comments[:10],
                    image_data_uri=image_data_uri,
                )
                result.discussion_group_id = group_peer_id
                if not result.should_reply and result.publication_status == "pending":
                    result.publication_status = "skipped_ai"
                    await db.commit()

                # Existing analyses from before publication support was introduced are
                # deliberately not published retroactively.
                if result.publication_status == "legacy":
                    await db.commit()
                    notification_status = "Ранее сохранённый анализ; автоматическая публикация не выполнялась."
                    label = "по теме" if result.on_topic else "не по теме"
                    notification = (
                        f"📡 Анализ канала: {result.channel_title or channel_title}\n"
                        f"Аккаунт: #{account_id} · Публикация #{post_id}\n"
                        f"Комментариев проанализировано: {result.comments_count}\n"
                        f"Обсуждение: {label}\n\n"
                        f"Сводка ИИ:\n{result.comments_summary[:1200]}\n\n"
                        f"Вариант ответа:\n{result.suggested_response[:1800]}\n\n"
                        f"Статус: {notification_status}"
                    )
                    await send_log(notification, category="channels")
                    await update_state(channel_id, post_id, status="done", next_retry_at=None, last_error="")
                    await redis.set(lock_key, "done", ex=365 * 24 * 3600)
                    return

                agent = await db.get(AIAgent, account.ai_agent_id)
                if not agent or not agent.enabled:
                    await update_state(
                        channel_id, post_id, status="retry",
                        next_retry_at=_utcnow() + timedelta(minutes=5),
                        last_error="assigned AI agent unavailable",
                    )
                    return

                if result.publication_status == "pending":
                    probability = (
                        binding.reply_probability_override
                        if binding.reply_probability_override is not None
                        else agent.channel_reply_probability_override
                        if agent.channel_reply_probability_override is not None
                        else account.channel_reply_probability
                    )
                    probability = max(0, min(100, int(probability or 0)))
                    if probability == 0 or random.randint(1, 100) > probability:
                        result.publication_status = "skipped_probability"
                        await db.commit()
                    else:
                        result.publication_status = "approved"
                        await db.commit()

                mode = effective_channel_reply_mode(account, binding)
                account_name = account.display_name
                effective_cooldown = (
                    binding.reply_cooldown_seconds_override
                    if binding.reply_cooldown_seconds_override is not None
                    else agent.channel_reply_cooldown_seconds_override
                    if agent.channel_reply_cooldown_seconds_override is not None
                    else account.channel_reply_cooldown_seconds
                )
                cooldown_seconds = max(0, int(effective_cooldown or 0))

                if result.publication_status == "approved" and mode != "automatic":
                    result.publication_status = "manual"
                    await db.commit()

                publication_status = result.publication_status

            if publication_status == "approved" or publication_status == "publishing":
                if cooldown_seconds:
                    cooldown_lock_key = f"atcivperson:channel-cooldown-lock:{account_id}:{channel_id}"
                    cooldown_lock_acquired = bool(await redis.set(
                        cooldown_lock_key, str(post_id), nx=True, ex=90
                    ))
                    if not cooldown_lock_acquired:
                        await update_state(
                            channel_id, post_id, status="retry",
                            next_retry_at=_utcnow() + timedelta(seconds=60),
                            last_error="another channel publication is in progress",
                        )
                        return
                    cooldown_key = f"atcivperson:channel-reply-cooldown:{account_id}:{channel_id}"
                    if await redis.exists(cooldown_key):
                        async with SessionFactory() as db:
                            current = await db.scalar(select(ChannelAnalysis).where(
                                ChannelAnalysis.account_id == account_id,
                                ChannelAnalysis.channel_id == channel_id,
                                ChannelAnalysis.post_id == post_id,
                            ))
                            if current:
                                current.publication_status = "skipped_cooldown"
                                await db.commit()
                        publication_status = "skipped_cooldown"

                if publication_status in {"approved", "publishing"}:
                    async with SessionFactory() as db:
                        current = await db.scalar(select(ChannelAnalysis).where(
                            ChannelAnalysis.account_id == account_id,
                            ChannelAnalysis.channel_id == channel_id,
                            ChannelAnalysis.post_id == post_id,
                        ))
                        if not current:
                            raise RuntimeError("Channel analysis disappeared before publication")
                        if current.publication_status == "approved":
                            current.publication_status = "publishing"
                            current.discussion_group_id = group_peer_id
                            await db.commit()
                        was_publishing = current.publication_status == "publishing"
                        known_comment_id = current.published_comment_id

                    sent_message = None
                    if was_publishing and known_comment_id:
                        sent_message = await client.get_messages(group_entity, ids=int(known_comment_id))
                    if sent_message is None and was_publishing:
                        # Recover the narrow crash window where Telegram accepted the
                        # comment but the worker stopped before saving its message ID.
                        async for candidate in client.iter_messages(
                            group_entity, reply_to=root_message_id, limit=100
                        ):
                            if (
                                candidate.out
                                and int(candidate.sender_id or 0) == int(account.telegram_user_id or 0)
                                and (candidate.message or "").strip() == result.suggested_response.strip()
                            ):
                                sent_message = candidate
                                break
                    if sent_message is None:
                        sent_message = await client.send_message(
                            group_entity, result.suggested_response, reply_to=root_message_id
                        )

                    async with SessionFactory() as db:
                        current = await db.scalar(select(ChannelAnalysis).where(
                            ChannelAnalysis.account_id == account_id,
                            ChannelAnalysis.channel_id == channel_id,
                            ChannelAnalysis.post_id == post_id,
                        ))
                        if current:
                            current.published_comment_id = int(sent_message.id)
                            current.discussion_group_id = group_peer_id
                            current.publication_status = "published"
                            current.publication_error = ""
                            await db.commit()
                    if cooldown_seconds:
                        await redis.set(
                            f"atcivperson:channel-reply-cooldown:{account_id}:{channel_id}",
                            "1", ex=cooldown_seconds,
                        )
                    publication_status = "published"
                    logging.info(
                        "Channel-level comment published: account=%s channel_id=%s post_id=%s "
                        "discussion_group=%s comment_id=%s",
                        account_id, channel_id, post_id, group_peer_id, int(sent_message.id),
                    )

            label = "по теме" if result.on_topic else "не по теме"
            status_text = {
                "published": "Комментарий опубликован ответом на исходную публикацию.",
                "manual": "Подготовлен черновик. Ручной режим: комментарий не опубликован.",
                "skipped_probability": "Публикация пропущена по вероятности ответа.",
                "skipped_ai": "ИИ решил, что отдельный комментарий к публикации не нужен.",
                "skipped_cooldown": "Публикация пропущена из-за паузы между ответами.",
                "approved": "Ответ подготовлен, ожидает публикации.",
                "publishing": "Публикация выполняется.",
            }.get(publication_status, "Ответ не опубликован.")
            notification = (
                f"📡 Анализ канала: {result.channel_title or channel_title}\n"
                f"Аккаунт: {account_name} (#{account_id}) · Публикация #{post_id}\n"
                f"Комментариев проанализировано: {result.comments_count}\n"
                f"Обсуждение: {label}\n\n"
                f"Сводка ИИ:\n{result.comments_summary[:1200]}\n\n"
                f"Вариант ответа:\n{result.suggested_response[:1800]}\n\n"
                f"Статус: {status_text}"
            )
            await send_log(notification, category="channels")
            await update_state(channel_id, post_id, status="done", next_retry_at=None, last_error="")
            await redis.set(lock_key, "done", ex=365 * 24 * 3600)

        except AIUnavailableError as exc:
            logging.warning(
                "Channel AI analysis unavailable: account=%s channel_id=%s post_id=%s error=%s",
                account_id, channel_id, post_id, exc,
            )
            await update_state(
                channel_id, post_id, status="retry",
                next_retry_at=_utcnow() + timedelta(minutes=5),
                last_error=str(exc)[:500],
            )
            await send_log(
                f"⚠️ Не удалось проанализировать публикацию #{post_id} канала "
                f"{channel_title}: {str(exc)[:500]}. Повторная попытка будет позже.",
                category="errors",
            )
        except Exception as exc:
            logging.exception(
                "Channel post processing failed: account=%s channel_id=%s post_id=%s",
                account_id, channel_id, post_id,
            )
            try:
                async with SessionFactory() as db:
                    failed_analysis = await db.scalar(select(ChannelAnalysis).where(
                        ChannelAnalysis.account_id == account_id,
                        ChannelAnalysis.channel_id == channel_id,
                        ChannelAnalysis.post_id == post_id,
                    ))
                    if failed_analysis and failed_analysis.publication_status == "publishing":
                        failed_analysis.publication_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                        await db.commit()
            except Exception:
                logging.exception("Could not save channel publication error: account=%s channel_id=%s post_id=%s", account_id, channel_id, post_id)
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
                if cooldown_lock_acquired and cooldown_lock_key:
                    await redis.delete(cooldown_lock_key)
                if lock_acquired:
                    async with SessionFactory() as db:
                        state_status = await db.scalar(select(ChannelPostState.status).where(
                            ChannelPostState.account_id == account_id,
                            ChannelPostState.channel_id == channel_id,
                            ChannelPostState.post_id == post_id,
                        ))
                    if state_status != "done":
                        await redis.delete(lock_key)
            except Exception:
                logging.exception("Could not release channel post locks: account=%s channel_id=%s post_id=%s", account_id, channel_id, post_id)
            finally:
                active_posts.discard(key)


    async def process_channel_learning(binding_id: int) -> None:
        try:
            async with SessionFactory() as db:
                binding = await db.get(ChannelBinding, binding_id)
                account = await db.get(Account, binding.account_id) if binding else None
                if not binding or not account or binding.learning_status != "training":
                    return
                if not account.enabled or account.manual_pause:
                    return
            prompt = await learn_channel_style(client, account=account, channel=binding)
            async with SessionFactory() as db:
                current = await db.get(ChannelBinding, binding_id)
                if current and current.learning_status == "training":
                    current.learning_prompt = prompt
                    current.learning_status = "idle"
                    current.learning_started_at = None
                    current.learning_completed_at = _utcnow()
                    await db.commit()
            await send_log("Автообучение канала завершено: " + binding.title, category="learning")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("Channel learning failed for binding %s", binding_id)
            async with SessionFactory() as db:
                current = await db.get(ChannelBinding, binding_id)
                if current and current.learning_status == "training":
                    current.learning_status = "error"
                    current.learning_started_at = None
                    await db.commit()
            await send_log("Ошибка автообучения канала: " + str(exc)[:500], category="errors")
        finally:
            active_channel_learning.discard(binding_id)

    async def handle_reply_to_our_channel_comment(event):
        """Route replies to account-authored messages in linked channel discussions into Chats."""
        message = event.message
        if (
            not event.is_group or not message or message.out
            or not (message.message or "").strip()
            or not getattr(message, "reply_to", None)
            or not int(message.sender_id or 0)
        ):
            return

        chat_id = int(event.chat_id)
        try:
            immediate_parent = await event.get_reply_message()
        except Exception:
            return
        if not immediate_parent:
            return

        try:
            sender = await event.get_sender()
            if getattr(sender, "bot", False):
                return
        except Exception:
            pass

        analysis = await find_published_comment_ancestor(
            chat_id, message, immediate_parent, getattr(event, "published_by_id", None)
        )

        async with SessionFactory() as db:
            account = await db.get(Account, account_id)
            if (
                not account or not account.enabled or account.manual_pause
                or int(message.sender_id or 0) == int(account.telegram_user_id or 0)
            ):
                return

            own_message = None
            if not analysis:
                own_message = await find_account_message_ancestor(
                    chat_id, message, immediate_parent, int(account.telegram_user_id or 0)
                )
                if not own_message:
                    return
                binding = await db.scalar(select(ChannelBinding).where(
                    ChannelBinding.account_id == account_id,
                    ChannelBinding.discussion_group_id == chat_id,
                ).limit(1))
            else:
                binding = await db.scalar(select(ChannelBinding).where(
                    ChannelBinding.account_id == account_id,
                    ChannelBinding.channel_id == int(analysis.channel_id),
                ))
            if not binding:
                return
            if binding.discussion_group_id is not None and int(binding.discussion_group_id) != chat_id:
                return
            if analysis and analysis.discussion_group_id is not None and int(analysis.discussion_group_id) != chat_id:
                return
            if not effective_channel_chat_replies_enabled(account, binding):
                logging.info(
                    "Channel discussion reply ignored by Chats-reply setting: "
                    "account=%s channel_id=%s group_id=%s message_id=%s",
                    account_id, int(binding.channel_id), chat_id, int(message.id),
                )
                return

            channel_title = (
                analysis.channel_title if analysis else binding.title
            ) or f"канал {binding.channel_id}"
            original_post = analysis.post_text[:2500] if analysis else ""
            own_message_text = (
                analysis.suggested_response[:1500]
                if analysis else (getattr(own_message, "message", "") or "")[:1500]
            )
            chat_context = (
                f"Контекст обсуждения канала «{channel_title}». "
                "Используй его только чтобы понять, на какое сообщение аккаунта отвечает собеседник. "
                "Дальше веди обычный диалог по настройкам раздела «Чаты».\n"
                + (f"Исходная публикация:\n{original_post}\n\n" if original_post else "")
                + f"Сообщение аккаунта, с которого начался диалог:\n{own_message_text}"
            )

        root_message_id = (
            int(analysis.published_comment_id) if analysis else int(own_message.id)
        )
        if route_chat_reply is None:
            logging.warning(
                "A reply to our channel message was detected but no chat router is configured: "
                "account=%s group_id=%s root_message_id=%s",
                account_id, chat_id, root_message_id,
            )
            return

        lock_key = f"atcivperson:channel-to-chat:{account_id}:{chat_id}:{int(message.id)}"
        if not await redis.set(lock_key, "processing", nx=True, ex=30 * 60):
            return
        try:
            logging.info(
                "Routing reply-chain to our channel comment into Chats pipeline: "
                "account=%s channel=%s group_id=%s root_message_id=%s message_id=%s",
                account_id, channel_title, chat_id, root_message_id, int(message.id),
            )
            await route_chat_reply(event, channel_title, chat_context)
            await redis.set(lock_key, "done", ex=365 * 24 * 3600)
        except asyncio.CancelledError:
            await redis.delete(lock_key)
            raise
        except Exception:
            await redis.delete(lock_key)
            logging.exception(
                "Could not route channel-discussion reply into Chats pipeline: account=%s group_id=%s message_id=%s",
                account_id, chat_id, int(message.id),
            )


    async def handle(event):
        message = event.message
        if not message or not has_channel_post_content(message.message, message):
            return
        if not event.is_channel or event.is_group:
            return
        channel_id = int(event.chat_id)
        logging.info(
            "Channel post event: account=%s channel_id=%s post_id=%s text_length=%s",
            account_id, channel_id, int(message.id), len(message.message or ""),
        )
        spawn(process_post(
            event.chat, channel_id, int(message.id), message.message or "",
            getattr(event.chat, "title", "") or "Канал", post_message=message,
        ))

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
                async with SessionFactory() as db:
                    training_bindings = list((await db.scalars(select(ChannelBinding).where(
                        ChannelBinding.account_id == account_id,
                        ChannelBinding.learning_status == "training",
                    ))).all())
                for learning_binding in training_bindings:
                    if learning_binding.id not in active_channel_learning:
                        active_channel_learning.add(learning_binding.id)
                        spawn(process_channel_learning(learning_binding.id))
                # Existing replies to already-published channel comments remain ordinary
                # Chats conversations even if channel automation is later disabled.
                async with SessionFactory() as db:
                    all_channel_bindings = list((await db.scalars(
                        select(ChannelBinding).where(ChannelBinding.account_id == account_id)
                    )).all())
                enabled_channel_ids = {int(item.channel_id) for item in bindings}
                for old_binding in all_channel_bindings:
                    if int(old_binding.channel_id) in enabled_channel_ids:
                        continue
                    try:
                        old_entity = await client.get_entity(old_binding.channel_id)
                        spawn(poll_published_comment_replies(old_binding, old_entity))
                    except Exception:
                        logging.exception(
                            "Could not poll replies for disabled channel binding: account=%s channel_id=%s",
                            account_id, int(old_binding.channel_id),
                        )

                for binding in bindings:
                    try:
                        now = _utcnow()
                        reopened_posts: list[int] = []
                        async with SessionFactory() as db:
                            known_states = list((await db.scalars(select(ChannelPostState).where(
                                ChannelPostState.account_id == account_id,
                                ChannelPostState.channel_id == binding.channel_id,
                            ))).all())
                            for previous_state in known_states:
                                if (
                                    previous_state.status == "done"
                                    and is_legacy_comment_threshold_skip(previous_state.last_error)
                                ):
                                    # These posts were incorrectly marked terminal by the old
                                    # audience-comment threshold. Remove the permanent Redis lock
                                    # before reopening the DB state so they are processed once.
                                    await redis.delete(
                                        f"atcivperson:channel-post:{account_id}:"
                                        f"{int(binding.channel_id)}:{int(previous_state.post_id)}"
                                    )
                                    previous_state.status = "queued"
                                    previous_state.next_retry_at = None
                                    previous_state.last_error = ""
                                    previous_state.updated_at = now
                                    reopened_posts.append(int(previous_state.post_id))
                            if reopened_posts:
                                await db.commit()
                            state_by_post = {
                                int(item.post_id): SimpleNamespace(
                                    status=item.status,
                                    next_retry_at=item.next_retry_at,
                                    updated_at=item.updated_at,
                                )
                                for item in known_states
                            }
                        for reopened_post_id in reopened_posts:
                            logging.warning(
                                "Re-queued post previously skipped by the removed comment threshold: "
                                "account=%s channel_id=%s post_id=%s",
                                account_id, int(binding.channel_id), reopened_post_id,
                            )
                        entity = await client.get_entity(binding.channel_id)
                        # A recent post can have comments disabled. Try several posts and
                        # keep searching when Telegram reports MSG_ID_INVALID for one thread.
                        async for recent_post in client.iter_messages(entity, limit=10):
                            if not has_channel_post_content(recent_post.message, recent_post):
                                continue
                            try:
                                logging.info(
                                    "Discussion discovery started: account=%s channel_id=%s post_id=%s",
                                    account_id, int(binding.channel_id), int(recent_post.id),
                                )
                                discussion = await client(GetDiscussionMessageRequest(
                                    peer=entity, msg_id=int(recent_post.id)
                                ))
                            except MsgIdInvalidError:
                                logging.debug(
                                    "No discussion thread for channel post; checking an older post: "
                                    "account=%s channel_id=%s post_id=%s",
                                    account_id, int(binding.channel_id), int(recent_post.id),
                                )
                                continue
                            except FloodWaitError as exc:
                                logging.warning(
                                    "Discussion discovery FloodWait: account=%s channel_id=%s wait=%ss",
                                    account_id, int(binding.channel_id), int(getattr(exc, "seconds", 0)),
                                )
                                break
                            except Exception as exc:
                                logging.exception(
                                    "Discussion discovery failed: account=%s channel_id=%s post_id=%s "
                                    "error_type=%s error=%s",
                                    account_id, int(binding.channel_id), int(recent_post.id),
                                    type(exc).__name__, exc,
                                )
                                continue

                            group = next((
                                chat for chat in getattr(discussion, "chats", [])
                                if getattr(chat, "megagroup", False)
                            ), None)
                            if not group:
                                continue
                            await register_discussion_group(int(binding.channel_id), group)
                            logging.info(
                                "Discussion discovery succeeded: account=%s channel_id=%s post_id=%s "
                                "group_id=%s group_peer_id=%s",
                                account_id, int(binding.channel_id), int(recent_post.id),
                                int(group.id), int(utils.get_peer_id(group)),
                            )
                            break
                        spawn(poll_published_comment_replies(binding, entity))
                        async for message in client.iter_messages(entity, limit=RECOVERY_SCAN_LIMIT):
                            body = (message.message or "").strip()
                            if not has_channel_post_content(body, message):
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
                            logging.info(
                                "Channel recovery discovered post: account=%s channel_id=%s post_id=%s "
                                "previous_state=%s",
                                account_id, int(binding.channel_id), int(message.id),
                                previous.status if previous else "missing",
                            )
                            spawn(process_post(
                                entity, int(binding.channel_id), int(message.id), body,
                                binding.title or getattr(entity, "title", "") or "Канал",
                                post_message=message,
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
    client.add_event_handler(handle_reply_to_our_channel_comment, events.NewMessage(incoming=True))
    recovery_task = spawn(recovery_loop())
    return recovery_task
