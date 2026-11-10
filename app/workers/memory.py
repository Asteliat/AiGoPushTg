"""Rolling conversation memory kept in Redis.

Every account keeps two bounded lists per chat: the last messages of the chat and the
last messages of that participant, merged when the AI needs context.
"""

from __future__ import annotations

import json

from app.workers import redis_keys

CHAT_HISTORY_LIMIT = 30
PERSON_HISTORY_LIMIT = 16
MEMORY_TTL_SECONDS = 30 * 24 * 3600
CONTEXT_MAX_ITEMS = 18
CONTEXT_MAX_CHARS = 7000


async def remember_message(redis, aid: int, chat_id: int, sender_id: int, item: dict) -> None:
    chat_key = redis_keys.chat_memory(aid, chat_id)
    person_key = redis_keys.person_memory(aid, chat_id, sender_id)
    raw = json.dumps(item, ensure_ascii=False)
    for key, limit in ((chat_key, 30), (person_key, 16)):
        await redis.lpush(key, raw)
        await redis.ltrim(key, 0, limit - 1)
        await redis.expire(key, 30 * 24 * 3600)


async def read_conversation_context(redis, aid: int, chat_id: int, sender_id: int) -> str:
    keys = [
        redis_keys.chat_memory(aid, chat_id),
        redis_keys.person_memory(aid, chat_id, sender_id),
    ]
    merged = {}
    for key in keys:
        for raw in reversed(await redis.lrange(key, 0, 11)):
            try:
                item = json.loads(raw)
            except (TypeError, ValueError):
                continue
            merged[(item.get("message_id"), item.get("sender_id"), item.get("text"))] = item
    items = sorted(merged.values(), key=lambda x: x.get("date", ""))[-18:]
    return "\n".join(
        f"[{str(item.get('speaker', 'Участник'))[:60]}] {str(item.get('text', ''))[:500]}"
        for item in items
        if str(item.get("text", "")).strip()
    )[-7000:]
