import pytest


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
    assert result["participants"][0]["is_scam"] is False


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
            if target == "first":
                raise FloodWaitError(request=None, capture=17)
            return SimpleNamespace(id=100, access_hash=200)

        async def __call__(self, request):
            return None

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    from app.services import automation

    async def no_rate_limit(_account_id, _operation):
        return None

    async def no_floodwait_record(_account_id, _module, _seconds):
        return {"count": 1, "threshold": 10, "quarantined": False}

    monkeypatch.setattr(automation, "reserve_operation_slot", no_rate_limit)
    monkeypatch.setattr(automation, "record_module_floodwait", no_floodwait_record)
    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    result = await management.manage_subscriptions_batch(1, ["first", "second", "third"], action="join")
    assert result["flood_wait_seconds"] == 17
    assert result["processed"] == 1



@pytest.mark.asyncio
async def test_parse_chat_participants_requires_group_admin(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.telegram_gateway import management

    class FakeClient:
        async def get_entity(self, _target):
            return SimpleNamespace(id=42, title="Private group", megagroup=True, broadcast=False)

        async def get_permissions(self, _entity, _user):
            return SimpleNamespace(is_admin=False, is_creator=False)

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    with pytest.raises(PermissionError, match="администраторам"):
        await management.parse_chat_participants(1, "@privategroup")



def test_sqlite_session_snapshot_is_valid_and_independent(tmp_path):
    import sqlite3

    from app.telegram_gateway.management import _copy_sqlite_session

    source = tmp_path / "source.session"
    snapshot = tmp_path / "snapshot.session"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE session_data (value TEXT NOT NULL)")
        db.execute("INSERT INTO session_data(value) VALUES ('original')")
    _copy_sqlite_session(str(source), str(snapshot))
    with sqlite3.connect(source) as db:
        db.execute("UPDATE session_data SET value = 'changed'")
    with sqlite3.connect(snapshot) as db:
        assert db.execute("SELECT value FROM session_data").fetchone()[0] == "original"
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"



@pytest.mark.asyncio
async def test_update_profile_rejects_invalid_username_before_session_access():
    from app.telegram_gateway.management import update_profile

    with pytest.raises(ValueError, match="Username"):
        await update_profile(1, username="bad username")
