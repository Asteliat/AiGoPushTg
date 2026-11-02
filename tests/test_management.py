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
    assert result["participants"][0]["is_blocked"] is False


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

@pytest.mark.asyncio
async def test_parse_chat_history_applies_message_delay(monkeypatch):
    from datetime import datetime, timezone
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import app.telegram_gateway.management as management

    class FakeClient:
        async def get_entity(self, _target):
            return SimpleNamespace(id=77, title="Test channel", username="test", broadcast=True)

        async def iter_messages(self, _entity, limit):
            assert limit == 2
            for message_id in (1, 2):
                yield SimpleNamespace(
                    id=message_id, action=None, date=datetime.now(timezone.utc),
                    message=("See https://t.me/example and https://example.org/path " if message_id == 1 else f"message {message_id}"), reactions=None, replies=None,
                    views=0, forwards=0, media=None, sender_id=10,
                )

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    monkeypatch.setattr(management.asyncio, "sleep", fake_sleep)

    result = await management.parse_chat_history(
        1, "@test", limit=2, message_delay_seconds=3
    )

    assert len(result["posts"]) == 2
    assert result["posts"][0]["links"] == ["https://t.me/example", "https://example.org/path"]
    assert result["posts"][1]["links"] == []
    assert result["filters"]["message_delay_seconds"] == 3
    assert sleeps == [3, 3]

@pytest.mark.asyncio
async def test_parse_account_history_uses_each_dialog_and_isolates_chat_errors(monkeypatch):
    import app.telegram_gateway.management as management

    dialogs = [
        {"id": 1, "peer_id": -1001, "title": "One"},
        {"id": 2, "peer_id": -1002, "title": "Two"},
        {"id": 3, "peer_id": -1003, "title": "Three"},
    ]
    parsed = []

    async def fake_dialogs(_account_id, *, limit, kind):
        assert limit == 500
        assert kind == "all"
        return dialogs

    async def fake_history(_account_id, target, **kwargs):
        parsed.append((target, kwargs))
        if target == "-1002":
            raise ValueError("private history")
        return {"title": target, "channel_id": int(target), "posts": [{"id": 1}], "scanned": 1}

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(management, "list_account_dialogs", fake_dialogs)
    monkeypatch.setattr(management, "parse_chat_history", fake_history)
    monkeypatch.setattr(management.asyncio, "sleep", fake_sleep)

    result = await management.parse_account_history(
        9, limit_per_chat=10, days=3, keywords=["vpn"],
        message_delay_seconds=2, chat_delay_seconds=4,
    )

    assert result["chats_scanned"] == 3
    assert result["chats_parsed"] == 2
    assert result["messages_count"] == 2
    assert len(result["errors"]) == 1
    assert [item[0] for item in parsed] == ["-1001", "-1002", "-1003"]
    assert sleeps == [4, 4]
    assert all(item[1]["message_delay_seconds"] == 2 for item in parsed)

@pytest.mark.asyncio
async def test_parse_chat_participants_can_exclude_account_blocklist(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.telegram_gateway import management

    class FakeClient:
        async def get_entity(self, _target):
            return SimpleNamespace(id=42, title="Test group", megagroup=True)

        async def get_permissions(self, _entity, _user):
            return SimpleNamespace(is_admin=True, is_creator=False)

        async def __call__(self, request):
            assert request.limit == 100
            return SimpleNamespace(
                blocked=[SimpleNamespace(peer=SimpleNamespace(user_id=1))]
            )

        async def iter_participants(self, _entity, limit):
            for user_id in (1, 2):
                yield SimpleNamespace(
                    id=user_id, username=f"user{user_id}", first_name="User",
                    last_name="", bot=False, deleted=False, scam=False,
                )

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    result = await management.parse_chat_participants(
        1, "@testgroup", limit=10, exclude_blocked=True
    )

    assert [item["id"] for item in result["participants"]] == [2]
    assert result["filters"]["exclude_blocked"] is True


@pytest.mark.asyncio
async def test_parse_chat_comments_exports_only_matching_public_comments(monkeypatch):
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.telegram_gateway import management

    now = datetime.now(timezone.utc)

    class FakeMessage:
        def __init__(self, message_id, body, sender_id=10, replies=None):
            self.id = message_id
            self.message = body
            self.sender_id = sender_id
            self.date = now
            self.action = None
            self.replies = replies

    post = FakeMessage(100, "post", replies=SimpleNamespace(replies=2))
    comments = [
        FakeMessage(101, "VPN works"),
        FakeMessage(102, "ordinary discussion"),
    ]

    class FakeClient:
        async def get_entity(self, _target):
            return SimpleNamespace(id=77, title="Test channel", username="test", broadcast=True)

        async def iter_messages(self, _entity, limit, reply_to=None):
            if reply_to is None:
                assert limit == 5
                yield post
            else:
                assert reply_to == 100
                assert limit == 5
                for comment in comments:
                    yield comment

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    result = await management.parse_chat_comments(
        1, "@test", post_limit=5, comment_limit=5, keywords=["vpn"]
    )

    assert result["title"] == "Test channel"
    assert result["scanned_posts"] == 1
    assert result["scanned_comments"] == 2
    assert result["visible_count"] == 1
    assert result["comments"][0]["comment_id"] == 101
    assert result["comments"][0]["text"] == "VPN works"


def test_parser_profile_filters_use_explicit_telegram_fields_only():
    from types import SimpleNamespace

    from app.telegram_gateway.management import _profile_matches_parser_filters

    user = SimpleNamespace(
        id=1, bot=True, deleted=False, scam=False, username="person",
        photo=object(), premium=True, status=None,
    )
    assert not _profile_matches_parser_filters(user, exclude_bots=True)
    assert _profile_matches_parser_filters(user, username_only=True, photo_only=True, premium_only=True)
    assert not _profile_matches_parser_filters(user, active_days=7)
    assert not _profile_matches_parser_filters(user, exclude_blocked=True, blocked_ids={1})
