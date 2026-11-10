from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.types import ChatInvite, ChatInviteAlready

from app.telegram_gateway import discovery


class FakeClient:
    """Resolves public usernames and invite hashes the way Telegram would."""

    def __init__(self, entities=None, invites=None, failures=None) -> None:
        self.entities = entities or {}
        self.invites = invites or {}
        self.failures = failures or {}
        self.calls: list[str] = []

    async def get_entity(self, target):
        self.calls.append(f"entity:{target}")
        failure = self.failures.get(target)
        if failure is not None:
            self.failures.pop(target)
            raise failure
        if target not in self.entities:
            raise ValueError("no user has that username")
        return self.entities[target]

    async def __call__(self, request):
        invite_hash = getattr(request, "hash", None)
        self.calls.append(f"invite:{invite_hash}")
        if invite_hash not in self.invites:
            raise ValueError("invite not found")
        return self.invites[invite_hash]


def group_entity(*, username="python_group", members=5000, title="Python Group", megagroup=True):
    return SimpleNamespace(
        id=101,
        username=username,
        title=title,
        participants_count=members,
        megagroup=megagroup,
        gigagroup=False,
        broadcast=False,
        deleted=False,
        scam=False,
        fake=False,
        restricted=False,
        forum=False,
        verified=False,
    )


def channel_entity(*, username="python_news", subscribers=9000, title="Python News"):
    return SimpleNamespace(
        id=202,
        username=username,
        title=title,
        participants_count=subscribers,
        megagroup=False,
        gigagroup=False,
        broadcast=True,
        deleted=False,
        scam=False,
        fake=False,
        restricted=False,
        forum=False,
        verified=False,
    )


@pytest.mark.asyncio
async def test_public_group_is_verified_with_canonical_link() -> None:
    client = FakeClient(entities={"python_group": group_entity()})

    result = await discovery.verify_group_candidates(
        client, [{"link": "t.me/python_group", "score": 10, "source": "Web"}]
    )

    assert len(result) == 1
    item = result[0]
    assert item["link"] == "https://t.me/python_group"
    assert item["kind"] == "supergroup"
    assert item["verified"] is True
    assert item["id"] == 101
    assert item["members"] == 5000


@pytest.mark.asyncio
async def test_broadcast_channel_is_rejected_by_group_verification() -> None:
    client = FakeClient(entities={"python_news": channel_entity()})

    assert await discovery.verify_group_candidates(
        client, [{"link": "https://t.me/python_news"}]
    ) == []


@pytest.mark.asyncio
async def test_group_without_username_is_rejected() -> None:
    client = FakeClient(entities={"hidden_group": group_entity(username=None)})

    assert await discovery.verify_group_candidates(
        client, [{"link": "https://t.me/hidden_group"}]
    ) == []


@pytest.mark.asyncio
async def test_already_joined_invite_is_verified_from_its_chat() -> None:
    client = FakeClient(
        invites={"abcdefghij": ChatInviteAlready(chat=group_entity(username="python_group"))}
    )

    result = await discovery.verify_group_candidates(
        client, [{"link": "https://t.me/+abcdefghij"}]
    )

    assert result and result[0]["link"] == "https://t.me/python_group"
    assert result[0]["kind"] == "supergroup"


@pytest.mark.asyncio
async def test_private_group_invite_is_accepted_without_a_username() -> None:
    client = FakeClient(
        invites={
            "abcdefghij": ChatInvite(
                title="Закрытая группа",
                photo=None,
                participants_count=42,
                color=0,
                channel=False,
                broadcast=False,
                public=False,
                megagroup=False,
                request_needed=False,
            )
        }
    )

    result = await discovery.verify_group_candidates(
        client, [{"link": "https://t.me/+abcdefghij"}]
    )

    assert len(result) == 1
    assert result[0]["kind"] == "invite"
    assert result[0]["members"] == 42
    assert result[0]["link"] == "https://t.me/+abcdefghij"


@pytest.mark.asyncio
async def test_private_channel_invite_is_rejected() -> None:
    client = FakeClient(
        invites={
            "abcdefghij": ChatInvite(
                title="Закрытый канал",
                photo=None,
                participants_count=42,
                color=0,
                channel=True,
                broadcast=True,
                public=False,
                megagroup=False,
                request_needed=False,
            )
        }
    )

    assert await discovery.verify_group_candidates(
        client, [{"link": "https://t.me/+abcdefghij"}]
    ) == []


@pytest.mark.asyncio
async def test_unresolvable_candidate_is_dropped() -> None:
    client = FakeClient()

    assert await discovery.verify_group_candidates(
        client, [{"link": "https://t.me/missing_group"}, {"link": "not a link"}]
    ) == []


@pytest.mark.asyncio
async def test_duplicate_candidates_are_merged_by_link() -> None:
    client = FakeClient(entities={"python_group": group_entity()})

    result = await discovery.verify_group_candidates(
        client,
        [
            {"link": "t.me/python_group", "score": 10, "source": "Bing"},
            {"link": "https://t.me/python_group", "score": 40, "source": "Yandex"},
        ],
    )

    assert len(result) == 1
    # The Telegram quality score wins over the search-engine score, and both
    # sources are kept so the operator can see where the group was found.
    assert result[0]["score"] >= 40
    assert set(result[0]["source"].split(", ")) == {"Bing", "Yandex"}


@pytest.mark.asyncio
async def test_flood_wait_is_retried_once(monkeypatch) -> None:
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(discovery.asyncio, "sleep", no_sleep)
    client = FakeClient(
        entities={"python_group": group_entity()},
        failures={"python_group": FloodWaitError(request=None, capture=1)},
    )

    result = await discovery.verify_group_candidates(
        client, [{"link": "https://t.me/python_group"}]
    )

    assert result and result[0]["verified"] is True
    assert client.calls.count("entity:python_group") == 2
