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


@pytest.mark.asyncio
async def test_sync_cached_account_profile_updates_picker_name_and_username(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.telegram_gateway import management

    account = SimpleNamespace(username="oldname", display_name="Old Name")

    class FakeDB:
        async def get(self, _model, account_id):
            assert account_id == 7
            return account

        async def commit(self):
            return None

    @asynccontextmanager
    async def fake_session():
        yield FakeDB()

    monkeypatch.setattr(management, "SessionFactory", fake_session)
    await management._sync_cached_account_profile(7, {
        "username": "newname",
        "first_name": "New",
        "last_name": "Person",
    })

    assert account.username == "newname"
    assert account.display_name == "New Person"


@pytest.mark.asyncio
async def test_set_profile_photo_normalizes_image_to_jpeg_before_upload(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from PIL import Image
    from app.telegram_gateway import management

    source_path = tmp_path / "avatar.png"
    Image.new("RGBA", (320, 320), (30, 120, 210, 255)).save(source_path)
    uploaded = {}

    class FakeClient:
        async def upload_file(self, path):
            uploaded["path"] = path
            with Image.open(path) as image:
                uploaded["format"] = image.format
                uploaded["mode"] = image.mode
            return "uploaded-file"

        async def __call__(self, request):
            from telethon.tl.functions.photos import UploadProfilePhotoRequest

            assert isinstance(request, UploadProfilePhotoRequest)
            assert request.file == "uploaded-file"
            uploaded["request_sent"] = True

        async def get_profile_photos(self, _entity, limit):
            assert limit == 1
            return [object()]

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    await management.set_profile_photo(7, str(source_path))

    assert uploaded["format"] == "JPEG"
    assert uploaded["mode"] == "RGB"
    assert uploaded["request_sent"] is True


@pytest.mark.asyncio
async def test_publish_story_photo_checks_eligibility_and_limits_visibility_to_contacts(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from PIL import Image
    from telethon.tl.functions.stories import CanSendStoryRequest, SendStoryRequest
    from telethon.tl.types import InputPrivacyValueAllowContacts

    from app.telegram_gateway import management

    photo_path = tmp_path / "story.jpg"
    Image.new("RGB", (720, 1280), (40, 90, 150)).save(photo_path)
    sent = {}
    uploaded = object()

    class FakeClient:
        async def get_input_entity(self, peer):
            assert peer == "me"
            return "self-peer"

        async def __call__(self, request):
            if isinstance(request, CanSendStoryRequest):
                sent["eligibility_checked"] = True
                return SimpleNamespace(count_remains=1)
            assert isinstance(request, SendStoryRequest)
            sent["request"] = request
            return SimpleNamespace()

        async def upload_file(self, path):
            assert path == str(photo_path)
            return uploaded

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "isolated_account_client", fake_client)
    await management.publish_story_photo(7, str(photo_path), caption="Hello")

    request = sent["request"]
    assert sent["eligibility_checked"] is True
    assert request.peer == "self-peer"
    assert request.media.file is uploaded
    assert request.caption == "Hello"
    assert request.period == 86400
    assert len(request.privacy_rules) == 1
    assert isinstance(request.privacy_rules[0], InputPrivacyValueAllowContacts)


@pytest.mark.asyncio
async def test_publish_story_photo_rejects_landscape_before_opening_account_session(tmp_path):
    from PIL import Image
    from app.telegram_gateway.management import publish_story_photo

    photo_path = tmp_path / "landscape.jpg"
    Image.new("RGB", (1280, 720), (40, 90, 150)).save(photo_path)

    with pytest.raises(ValueError, match="вертикальное"):
        await publish_story_photo(7, str(photo_path))

@pytest.mark.asyncio
async def test_update_profile_bio_fails_when_telegram_does_not_save_it(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.telegram_gateway import management

    class FakeClient:
        def __init__(self):
            self.full_user_reads = 0

        async def get_me(self):
            return SimpleNamespace(first_name="Test", last_name="", username=None)

        async def __call__(self, request):
            self.full_user_reads += 1
            return SimpleNamespace(full_user=SimpleNamespace(about="old bio"))

    fake_client = FakeClient()
    fake_account = SimpleNamespace(proxy_config_encrypted=None)
    clock = {"now": 0.0}

    def fake_monotonic():
        return clock["now"]

    async def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(management, "time", SimpleNamespace(monotonic=fake_monotonic))
    monkeypatch.setattr(management.asyncio, "sleep", fake_sleep)

    @asynccontextmanager
    async def fake_lock(_account_id):
        yield

    @asynccontextmanager
    async def fake_isolated(_account_id):
        yield fake_account, fake_client

    async def no_sync(*_args, **_kwargs):
        return None

    monkeypatch.setattr(management, "_account_lock", lambda aid: fake_lock(aid))
    monkeypatch.setattr(management, "isolated_account_client", fake_isolated)
    monkeypatch.setattr(management, "_sync_cached_account_profile", no_sync)

    with pytest.raises(RuntimeError, match="Telegram не подтвердил изменение Bio"):
        await management.update_profile(1, about="new bio")

    assert fake_client.full_user_reads > 2



@pytest.mark.asyncio
async def test_preview_manual_message_target_resolves_one_group(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.telegram_gateway import management

    entity = SimpleNamespace(
        id=77, title="Test group", username="testgroup",
        megagroup=True, gigagroup=False, broadcast=False,
    )

    class FakeClient:
        async def get_entity(self, target):
            assert target == "@testgroup"
            return entity

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "_account_lock", lambda _aid: _AsyncNullLock())
    monkeypatch.setattr(management, "isolated_account_client", fake_client)

    result = await management.preview_message_target(1, "https://t.me/testgroup")
    assert result["title"] == "Test group"
    assert result["peer_id"] == 77
    assert result["kind"] == "группа"


@pytest.mark.asyncio
async def test_send_manual_message_uses_plain_text_and_one_explicit_peer(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.telegram_gateway import management

    entity = SimpleNamespace(
        id=88, title="Private group", username="privategroup",
        megagroup=True, gigagroup=False, broadcast=False,
    )
    sent = {}

    class FakeClient:
        async def get_entity(self, target):
            assert target == "@privategroup"
            return entity

        async def send_message(self, peer, message, **kwargs):
            sent.update(peer=peer, message=message, kwargs=kwargs)
            return SimpleNamespace(id=1234)

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "_account_lock", lambda _aid: _AsyncNullLock())
    monkeypatch.setattr(management, "isolated_account_client", fake_client)

    result = await management.send_message_to_chat(
        1, "https://t.me/privategroup", "Текст без форматирования <b>тег</b>"
    )
    assert result["message_id"] == 1234
    assert result["peer_id"] == 88
    assert sent["peer"] is entity
    assert sent["message"] == "Текст без форматирования <b>тег</b>"
    assert sent["kwargs"] == {"parse_mode": None, "link_preview": False}


@pytest.mark.asyncio
async def test_send_manual_message_rejects_broadcast_channel(monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from app.telegram_gateway import management

    entity = SimpleNamespace(
        id=99, title="Broadcast channel", username="broadcast",
        megagroup=False, broadcast=True,
    )

    class FakeClient:
        async def get_entity(self, _target):
            return entity

        async def send_message(self, *_args, **_kwargs):
            raise AssertionError("A broadcast channel must never receive a manual chat message.")

    @asynccontextmanager
    async def fake_client(_account_id):
        yield SimpleNamespace(id=1), FakeClient()

    monkeypatch.setattr(management, "_account_lock", lambda _aid: _AsyncNullLock())
    monkeypatch.setattr(management, "isolated_account_client", fake_client)

    with pytest.raises(ValueError, match="вещательные каналы"):
        await management.send_message_to_chat(1, "@broadcast", "hello")


class _AsyncNullLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


@pytest.mark.asyncio
async def test_verify_chat_membership_requires_telegram_confirmation():
    from app.telegram_gateway import management

    class FakeClient:
        async def get_permissions(self, entity, user):
            assert entity.id == 123
            assert user == "me"
            return object()

    assert await management.verify_chat_membership(
        FakeClient(), type("Group", (), {"id": 123})()
    ) is True


@pytest.mark.asyncio
async def test_verify_chat_membership_returns_false_for_non_member():
    from app.telegram_gateway import management

    class UserNotParticipantError(Exception):
        pass

    class FakeClient:
        async def get_permissions(self, _entity, _user):
            raise UserNotParticipantError()

    assert await management.verify_chat_membership(
        FakeClient(), object()
    ) is False


@pytest.mark.asyncio
async def test_bound_private_group_builds_input_peer_with_saved_access_hash():
    from types import SimpleNamespace
    from telethon.tl.types import InputPeerChannel
    from telethon import utils
    from app.telegram_gateway.management import _bound_chat_input_peer

    marked_id = utils.get_peer_id(type("ChannelPeer", (), {"id": 987654, "__class__": type("Channel", (), {})})())
    # Use Telethon's canonical marked channel ID rather than a guessed negative ID.
    marked_id = -1000000987654
    binding = SimpleNamespace(chat_id=marked_id, peer_type="channel", access_hash=123456789, title="Private group")
    peer = _bound_chat_input_peer(binding)
    assert isinstance(peer, InputPeerChannel)
    assert peer.channel_id == 987654
    assert peer.access_hash == 123456789


def test_bound_basic_group_builds_input_peer():
    from types import SimpleNamespace
    from telethon.tl.types import InputPeerChat
    from app.telegram_gateway.management import _bound_chat_input_peer

    binding = SimpleNamespace(chat_id=-42, peer_type="chat", access_hash=None, title="Basic group")
    peer = _bound_chat_input_peer(binding)
    assert isinstance(peer, InputPeerChat)
    assert peer.chat_id == 42
