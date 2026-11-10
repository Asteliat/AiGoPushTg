"""Pure channel-monitor decisions: what to process, when to reply, how to read context."""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

DISCUSSION_COMMENT_SCAN_LIMIT = 100
DISCUSSION_COMMENT_CONTEXT_LIMIT = 10


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def has_channel_post_content(post_text: str | None, post_message=None) -> bool:
    """Only process channel posts that contain text or a supported photo."""
    return bool((post_text or "").strip() or getattr(post_message, "photo", None))


def channel_reply_probability_passes(probability: int) -> bool:
    """Select whether this post gets a generated reply; 0/100 are deterministic."""
    value = max(0, min(100, int(probability or 0)))
    if value == 0:
        return False
    if value == 100:
        return True
    return random.randint(1, 100) <= value


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


