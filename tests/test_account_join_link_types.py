from types import SimpleNamespace

import pytest

from app.bot.ui import _channel_entity, _group_entity, _parse_chat_link


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://t.me/public_news_channel", ("public", "public_news_channel")),
        ("https://t.me/+PrivateChannelInviteHash", ("invite", "PrivateChannelInviteHash")),
        ("https://t.me/joinchat/PrivateInviteHash123", ("invite", "PrivateInviteHash123")),
    ],
)
def test_account_join_link_parser_accepts_public_and_private_community_links(link, expected):
    assert _parse_chat_link(link) == expected


def test_join_target_detection_distinguishes_broadcast_channels_from_groups():
    broadcast = SimpleNamespace(
        broadcast=True, megagroup=False, gigagroup=False
    )
    supergroup = SimpleNamespace(
        broadcast=False, megagroup=True, gigagroup=False
    )
    basic_group = type("Chat", (), {})()

    assert _channel_entity(broadcast)
    assert not _group_entity(broadcast)
    assert not _channel_entity(supergroup)
    assert _group_entity(supergroup)
    assert not _channel_entity(basic_group)
    assert _group_entity(basic_group)


def test_join_target_detection_rejects_user_or_bot_entities():
    user = SimpleNamespace(broadcast=False, megagroup=False, gigagroup=False)

    assert not _channel_entity(user)
    assert not _group_entity(user)
