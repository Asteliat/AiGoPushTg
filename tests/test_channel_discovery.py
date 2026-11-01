from types import SimpleNamespace

import pytest

from app.telegram_gateway import discovery


def _channel(**overrides):
    values = {
        "id": 101,
        "title": "Technology News",
        "username": "technology_news",
        "broadcast": True,
        "megagroup": False,
        "gigagroup": False,
        "participants_count": 12500,
        "verified": True,
        "deleted": False,
        "scam": False,
        "fake": False,
        "restricted": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _group(**overrides):
    values = {
        "id": 202,
        "title": "Technology Chat",
        "username": "technology_chat",
        "broadcast": False,
        "megagroup": True,
        "gigagroup": False,
        "participants_count": 800,
        "verified": False,
        "deleted": False,
        "scam": False,
        "fake": False,
        "restricted": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_search_public_channels_keeps_broadcast_channels_only():
    class Client:
        async def __call__(self, request):
            assert request.q == "technology"
            return SimpleNamespace(chats=[_group(), _channel()])

    results = await discovery.search_public_channels(Client(), "technology")

    assert len(results) == 1
    assert results[0]["id"] == 101
    assert results[0]["title"] == "Technology News"
    assert results[0]["link"] == "https://t.me/technology_news"
    assert results[0]["kind"] == "channel"
    assert results[0]["verified"] is True


@pytest.mark.asyncio
async def test_verify_channel_candidates_accepts_channel_and_rejects_group():
    class Client:
        async def get_entity(self, username):
            if username == "technology_news":
                return _channel()
            return _group()

    candidates = [
        {"title": "Technology News", "link": "https://t.me/technology_news", "source": "Search"},
        {"title": "Technology Chat", "link": "https://t.me/technology_chat", "source": "Search"},
    ]

    results = await discovery.verify_channel_candidates(Client(), candidates)

    assert len(results) == 1
    assert results[0]["id"] == 101
    assert results[0]["kind"] == "channel"
    assert results[0]["verified"] is True


@pytest.mark.asyncio
async def test_verify_channel_candidates_accepts_private_channel_invite():
    class Client:
        async def __call__(self, request):
            assert request.__class__.__name__ == "CheckChatInviteRequest"
            return SimpleNamespace(
                channel=True,
                megagroup=False,
                title="Private Technology",
                participants_count=420,
            )

    results = await discovery.verify_channel_candidates(
        Client(),
        [{"title": "Private Technology", "link": "https://t.me/+abcdefghijk", "source": "Search"}],
    )

    assert len(results) == 1
    assert results[0]["title"] == "Private Technology"
    assert results[0]["kind"] == "channel"
    assert results[0]["verified"] is True


@pytest.mark.asyncio
async def test_verify_channel_candidates_rejects_private_group_invite():
    class Client:
        async def __call__(self, request):
            return SimpleNamespace(
                channel=True,
                megagroup=True,
                title="Private Group",
                participants_count=42,
            )

    results = await discovery.verify_channel_candidates(
        Client(),
        [{"title": "Private Group", "link": "https://t.me/+abcdefghijk", "source": "Search"}],
    )

    assert results == []


def test_channel_quality_rejects_non_broadcast_and_restricted_entities():
    assert discovery._channel_quality(_group()) is None
    assert discovery._channel_quality(_channel(restricted=True)) is None
    assert discovery._channel_quality(_channel(deleted=True)) is None
