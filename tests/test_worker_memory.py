import json

import pytest

from app.workers import memory
from tests.fakes import FakeRedis


def item(message_id: int, text: str, *, sender_id: int = 5, speaker: str = "Иван", date: str = ""):
    return {
        "message_id": message_id,
        "sender_id": sender_id,
        "speaker": speaker,
        "text": text,
        "date": date or f"2026-11-10T12:{message_id:02d}:00+00:00",
    }


@pytest.mark.asyncio
async def test_remember_message_writes_both_lists_with_limits() -> None:
    redis = FakeRedis()

    await memory.remember_message(redis, 1, -100, 5, item(1, "привет"))

    chat_key = "aigopushtg:memory:1:-100:chat"
    person_key = "aigopushtg:memory:1:-100:person:5"
    assert json.loads(redis.lists[chat_key][0])["text"] == "привет"
    assert json.loads(redis.lists[person_key][0])["text"] == "привет"
    assert redis.expires[chat_key] == memory.MEMORY_TTL_SECONDS


@pytest.mark.asyncio
async def test_remember_message_keeps_only_the_newest_entries() -> None:
    redis = FakeRedis()

    await memory.remember_message(redis, 1, -100, 5, item(1, "первое"))
    await memory.remember_message(redis, 1, -100, 5, item(2, "второе"))

    chat_key = "aigopushtg:memory:1:-100:chat"
    texts = [json.loads(raw)["text"] for raw in redis.lists[chat_key]]
    # Newest first: lpush puts the second message at the head.
    assert texts == ["второе", "первое"]


@pytest.mark.asyncio
async def test_context_merges_chat_and_person_history_in_time_order() -> None:
    redis = FakeRedis()
    await memory.remember_message(redis, 1, -100, 5, item(1, "вопрос", date="2026-11-10T12:00:00+00:00"))
    await memory.remember_message(redis, 1, -100, 6, item(2, "реплика другого", sender_id=6, date="2026-11-10T12:01:00+00:00"))
    await memory.remember_message(redis, 1, -100, 5, item(3, "ответ", date="2026-11-10T12:02:00+00:00"))

    context = await memory.read_conversation_context(redis, 1, -100, 5)

    # Chat history keeps every participant, so the person list only adds depth
    # for our sender; the merged view is ordered by message date.
    assert context.splitlines() == [
        "[Иван] вопрос",
        "[Иван] реплика другого",
        "[Иван] ответ",
    ]


@pytest.mark.asyncio
async def test_context_deduplicates_the_same_message() -> None:
    redis = FakeRedis()
    payload = item(7, "дубль")
    redis.lists["aigopushtg:memory:1:-100:chat"] = [json.dumps(payload)]
    redis.lists["aigopushtg:memory:1:-100:person:5"] = [json.dumps(payload)]

    context = await memory.read_conversation_context(redis, 1, -100, 5)

    assert context == "[Иван] дубль"


@pytest.mark.asyncio
async def test_context_ignores_broken_json_and_empty_text() -> None:
    redis = FakeRedis()
    redis.lists["aigopushtg:memory:1:-100:chat"] = [
        "{not json",
        json.dumps(item(1, "   ")),
        json.dumps(item(2, "живое сообщение")),
    ]

    context = await memory.read_conversation_context(redis, 1, -100, 5)

    assert context == "[Иван] живое сообщение"


@pytest.mark.asyncio
async def test_context_is_bounded() -> None:
    redis = FakeRedis()
    for number in range(1, 31):
        await memory.remember_message(redis, 1, -100, 5, item(number, f"сообщение {number}", date=f"2026-11-10T12:{number:02d}:00+00:00"))

    context = await memory.read_conversation_context(redis, 1, -100, 5)

    assert len(context.splitlines()) <= memory.CONTEXT_MAX_ITEMS
    assert len(context) <= memory.CONTEXT_MAX_CHARS
    assert "сообщение 30" in context
