import pytest


@pytest.mark.asyncio
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
async def test_verify_chat_membership_uses_get_participant_for_supergroup():
    from types import SimpleNamespace
    from telethon.tl.functions.channels import GetParticipantRequest
    from telethon.tl.types import InputPeerSelf
    from app.telegram_gateway import management

    class FakeClient:
        async def get_me(self):
            return SimpleNamespace(id=456)

        async def __call__(self, request):
            assert isinstance(request, GetParticipantRequest)
            assert request.channel.id == 123
            assert isinstance(request.participant, InputPeerSelf)
            return SimpleNamespace(participant=SimpleNamespace(left=False, kicked=False))

    assert await management.verify_chat_membership(
        FakeClient(), type("Channel", (), {"id": 123})()
    ) is True


@pytest.mark.asyncio
async def test_verify_chat_membership_returns_false_for_non_member():
    from types import SimpleNamespace
    from app.telegram_gateway import management

    class UserNotParticipantError(Exception):
        pass

    class FakeClient:
        async def get_me(self):
            return SimpleNamespace(id=456)

        async def __call__(self, _request):
            raise UserNotParticipantError()

    assert await management.verify_chat_membership(
        FakeClient(), type("Channel", (), {"id": 123})()
    ) is False


@pytest.mark.asyncio
async def test_verify_chat_membership_supports_basic_groups():
    from types import SimpleNamespace
    from app.telegram_gateway import management

    class FakeClient:
        async def get_me(self):
            return SimpleNamespace(id=456)

        async def get_participants(self, entity, limit):
            assert entity.id == 123
            assert limit == 200
            return [SimpleNamespace(id=111), SimpleNamespace(id=456)]

    assert await management.verify_chat_membership(
        FakeClient(), type("Chat", (), {"id": 123})()
    ) is True


@pytest.mark.asyncio
async def test_verify_chat_membership_rejects_left_participant():
    from types import SimpleNamespace
    from app.telegram_gateway import management

    class FakeClient:
        async def get_me(self):
            return SimpleNamespace(id=456)

        async def __call__(self, _request):
            return SimpleNamespace(
                participant=type("ChannelParticipantLeft", (), {})()
            )

    assert await management.verify_chat_membership(
        FakeClient(), type("Channel", (), {"id": 123})()
    ) is False


@pytest.mark.asyncio
async def test_bound_private_group_builds_input_peer_with_saved_access_hash():
    from types import SimpleNamespace
    from telethon.tl.types import InputPeerChannel
    from app.telegram_gateway.management import _bound_chat_input_peer

    # Telethon's canonical marked channel ID for raw channel ID 987654.
    marked_id = -1000000987654
    binding = SimpleNamespace(chat_id=marked_id, peer_type="channel", access_hash=123456789, title="Private group")
    peer = _bound_chat_input_peer(binding)
    assert isinstance(peer, InputPeerChannel)
    assert peer.channel_id == 987654
    assert peer.access_hash == 123456789


@pytest.mark.asyncio
async def test_manual_message_resolves_saved_input_peer_to_full_entity(monkeypatch):
    from types import SimpleNamespace
    from telethon.tl.types import InputPeerChannel
    from app.telegram_gateway import management

    binding = SimpleNamespace(
        chat_id=-1000000987654,
        peer_type="channel",
        access_hash=123456789,
        title="Private group",
    )
    entity = SimpleNamespace(
        id=987654,
        title="Private group",
        access_hash=123456789,
        megagroup=True,
        broadcast=False,
    )

    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def scalar(self, _query):
            return binding

    class FakeClient:
        async def get_entity(self, target):
            if isinstance(target, int):
                raise ValueError("Peer is not in the entity cache")
            assert isinstance(target, InputPeerChannel)
            return entity

    monkeypatch.setattr(management, "SessionFactory", lambda: FakeDB())

    resolved, title = await management._resolve_manual_message_peer(
        FakeClient(), 1, -1000000987654
    )

    assert resolved is entity
    assert resolved.id == 987654
    assert title == "Private group"


def test_bound_basic_group_builds_input_peer():
    from types import SimpleNamespace
    from telethon.tl.types import InputPeerChat
    from app.telegram_gateway.management import _bound_chat_input_peer

    binding = SimpleNamespace(chat_id=-42, peer_type="chat", access_hash=None, title="Basic group")
    peer = _bound_chat_input_peer(binding)
    assert isinstance(peer, InputPeerChat)
    assert peer.chat_id == 42


@pytest.mark.asyncio
async def test_verify_channel_membership_uses_authenticated_user_permissions():
    from types import SimpleNamespace
    from app.telegram_gateway import management

    class FakeClient:
        async def get_me(self):
            return SimpleNamespace(id=456)

        async def get_permissions(self, entity, user):
            assert entity.id == 123
            assert user.id == 456
            return object()

    assert await management.verify_channel_membership(
        FakeClient(), SimpleNamespace(id=123, broadcast=True)
    ) is True


@pytest.mark.asyncio
async def test_verify_channel_membership_returns_false_for_non_member():
    from types import SimpleNamespace
    from app.telegram_gateway import management

    class UserNotParticipantError(Exception):
        pass

    class FakeClient:
        async def get_me(self):
            return SimpleNamespace(id=456)

        async def get_permissions(self, entity, user):
            assert entity.id == 123
            assert user.id == 456
            raise UserNotParticipantError()

    assert await management.verify_channel_membership(
        FakeClient(), SimpleNamespace(id=123, broadcast=True)
    ) is False
