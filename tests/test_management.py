import pytest

from app.telegram_gateway.management import simulate_post_activity


@pytest.mark.asyncio
async def test_activity_simulation_is_local_only():
    result = await simulate_post_activity(
        post_key="test-post", requested_views=1200, requested_reactions=35
    )

    assert result["mode"] == "simulation"
    assert result["simulated_views"] == 1200
    assert result["simulated_reactions"] == 35
    assert result["telegram_modified"] is False


@pytest.mark.asyncio
async def test_activity_simulation_clamps_values():
    result = await simulate_post_activity(
        post_key="test-post", requested_views=2_000_000, requested_reactions=-10
    )

    assert result["simulated_views"] == 1_000_000
    assert result["simulated_reactions"] == 0


@pytest.mark.asyncio
async def test_activity_simulation_requires_post_key():
    with pytest.raises(ValueError, match="идентификатор"):
        await simulate_post_activity(
            post_key=" ", requested_views=10, requested_reactions=1
        )


@pytest.mark.asyncio
async def test_parse_chat_participants_returns_only_public_fields(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.telegram_gateway import management

    class FakeClient:
        async def get_entity(self, _target):
            return SimpleNamespace(id=42, title="Test group", username="testgroup", megagroup=True)

        async def get_permissions(self, _entity, _user):
            return SimpleNamespace(is_admin=True, is_creator=False)

        async def iter_participants(self, _entity, limit):
            assert limit == 2
            for item in [
                SimpleNamespace(id=1, username="one", first_name="One", last_name="User", bot=False, deleted=False),
                SimpleNamespace(id=2, username=None, first_name="Two", last_name="", bot=True, deleted=False),
            ]:
                yield item

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    result = await management.parse_chat_participants(1, "@testgroup", limit=2)

    assert result["title"] == "Test group"
    assert result["visible_count"] == 2
    assert result["participants"][0]["username"] == "one"
    assert "phone" not in result["participants"][0]


@pytest.mark.asyncio
async def test_parse_chat_participants_rejects_non_group(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.telegram_gateway import management

    class FakeClient:
        async def get_entity(self, _target):
            return SimpleNamespace(id=42, title="User", username="user", megagroup=False, broadcast=False)

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    with pytest.raises(ValueError, match="группу или канал"):
        await management.parse_chat_participants(1, "@user")



@pytest.mark.asyncio
async def test_list_account_dialogs_returns_groups_and_channels(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.telegram_gateway import management

    class FakeClient:
        async def iter_dialogs(self, limit):
            assert limit == 10
            yield SimpleNamespace(
                entity=SimpleNamespace(id=11, title="Group", username="group", megagroup=True),
                id=-10011, unread_count=2, dialog=SimpleNamespace(notify_settings=None),
            )
            yield SimpleNamespace(
                entity=SimpleNamespace(id=12, title="Channel", username="channel", broadcast=True),
                id=-10012, unread_count=0, dialog=SimpleNamespace(notify_settings=None),
            )

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    result = await management.list_account_dialogs(1, limit=10)
    assert [item["kind"] for item in result] == ["group", "channel"]


@pytest.mark.asyncio
async def test_batch_subscription_stops_on_floodwait(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from telethon.errors import FloodWaitError
    from app.telegram_gateway import management

    class FakeClient:
        async def get_entity(self, target):
            if target == "second":
                raise FloodWaitError(request=None, capture=17)
            return SimpleNamespace(__class__=type("Channel", (), {}))

        async def __call__(self, request):
            return None

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    result = await management.manage_subscriptions_batch(1, ["first", "second", "third"], action="join")
    assert result["flood_wait_seconds"] == 17
    assert result["processed"] == 2
