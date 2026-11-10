from types import SimpleNamespace

import pytest

from app.ai.provider import AIUnavailableError
from app.storage.models import ChannelBinding
from app.workers import channel_monitor as monitor_module
from app.workers.channel_monitor import ChannelMonitor, install_channel_monitor
from tests.fakes import FakeRedis, FakeSessionFactory, FakeTelethonClient


def make_monitor(client=None) -> ChannelMonitor:
    return ChannelMonitor(
        client or FakeTelethonClient(),
        account_id=7,
        redis=FakeRedis(),
        send_log=_noop_log,
        route_chat_reply=_noop_route,
    )


async def _noop_log(*_args, **_kwargs) -> bool:
    return True


async def _noop_route(*_args, **_kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_install_channel_monitor_registers_handlers_and_keeps_state() -> None:
    client = FakeTelethonClient()

    monitor = install_channel_monitor(
        client, 7, FakeRedis(), _noop_log, route_chat_reply=_noop_route
    )

    assert isinstance(monitor, ChannelMonitor)
    assert monitor.client is client
    assert monitor.account_id == 7
    assert len(client.handlers) == 2
    assert monitor.recovery_task is not None
    await monitor.cancel_background_tasks()


@pytest.mark.asyncio
async def test_spawn_tracks_tasks_and_forgets_finished_ones() -> None:
    monitor = make_monitor()
    finished = []

    async def finish() -> None:
        finished.append(True)

    task = monitor.spawn(finish())
    assert task in monitor.background_tasks

    await task
    assert finished == [True]

    await monitor.cancel_background_tasks()
    assert monitor.background_tasks == set()


@pytest.mark.asyncio
async def test_register_discussion_group_replaces_a_stale_mapping(monkeypatch) -> None:
    monkeypatch.setattr(monitor_module.utils, "get_peer_id", lambda group: group.id)
    binding = ChannelBinding(account_id=7, channel_id=500, discussion_group_id=111)
    factory = FakeSessionFactory()
    factory.scalar_result = binding
    monkeypatch.setattr(monitor_module, "SessionFactory", factory)
    monitor = make_monitor()
    monitor.discussion_channels[111] = 500
    monitor.discussion_entities[111] = SimpleNamespace(id=111)

    await monitor.register_discussion_group(500, SimpleNamespace(id=222))

    assert 111 not in monitor.discussion_channels
    assert 111 not in monitor.discussion_entities
    assert monitor.discussion_channels[222] == 500
    assert binding.discussion_group_id == 222
    assert factory.commits == 1


@pytest.mark.asyncio
async def test_register_discussion_group_skips_the_write_when_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(monitor_module.utils, "get_peer_id", lambda group: group.id)
    binding = ChannelBinding(account_id=7, channel_id=500, discussion_group_id=222)
    factory = FakeSessionFactory()
    factory.scalar_result = binding
    monkeypatch.setattr(monitor_module, "SessionFactory", factory)
    monitor = make_monitor()

    await monitor.register_discussion_group(500, SimpleNamespace(id=222))

    assert factory.commits == 0


@pytest.mark.asyncio
async def test_account_message_ancestor_finds_our_own_message(monkeypatch) -> None:
    chain = {
        2: SimpleNamespace(
            id=2, out=False, sender_id=99, reply_to=SimpleNamespace(reply_to_msg_id=3)
        ),
        3: SimpleNamespace(
            id=3, out=False, sender_id=1000, reply_to=SimpleNamespace(reply_to_msg_id=4)
        ),
        4: SimpleNamespace(id=4, out=True, sender_id=0, reply_to=None),
    }
    client = FakeTelethonClient()

    async def get_messages(_chat_id, *, ids):
        return chain[ids]

    client.get_messages = get_messages  # type: ignore[method-assign]
    monitor = make_monitor(client)
    message = SimpleNamespace(reply_to=SimpleNamespace(reply_to_msg_id=2))

    ancestor = await monitor.find_account_message_ancestor(
        chat_id=500, message=message, immediate_parent=None, own_user_id=1000
    )

    assert ancestor is chain[3]


@pytest.mark.asyncio
async def test_account_message_ancestor_gives_up_after_the_chain_limit() -> None:
    client = FakeTelethonClient()

    async def get_messages(_chat_id, *, ids):
        return SimpleNamespace(id=ids, out=False, sender_id=99, reply_to=None)

    client.get_messages = get_messages  # type: ignore[method-assign]
    monitor = make_monitor(client)

    ancestor = await monitor.find_account_message_ancestor(
        chat_id=500,
        message=SimpleNamespace(reply_to=SimpleNamespace(reply_to_msg_id=2)),
        immediate_parent=None,
        own_user_id=1000,
    )

    assert ancestor is None


@pytest.mark.asyncio
async def test_photo_without_attachment_is_skipped() -> None:
    monitor = make_monitor()

    assert await monitor.download_post_image_data_uri(SimpleNamespace(photo=None)) is None


@pytest.mark.asyncio
async def test_failed_photo_download_raises_a_clear_error() -> None:
    client = FakeTelethonClient()

    async def failing_download(*_args, **_kwargs):
        raise OSError("network")

    client.download_media = failing_download  # type: ignore[method-assign]
    monitor = make_monitor(client)

    with pytest.raises(AIUnavailableError, match="фотографию публикации"):
        await monitor.download_post_image_data_uri(SimpleNamespace(photo=object()))


@pytest.mark.asyncio
async def test_recovery_loop_stops_when_the_client_is_disconnected() -> None:
    monitor = make_monitor()  # FakeTelethonClient is not connected

    await monitor.recovery_loop()

    assert monitor.background_tasks == set()
