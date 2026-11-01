from app.telegram_gateway.discovery import _is_group, _normalize_telegram_link, _quality


class BasicChat:
    title = "Python Community"
    username = None
    participants_count = 120
    verified = False
    deleted = False
    scam = False
    fake = False
    restricted = False


class Supergroup:
    title = "Python Developers"
    username = "python_developers"
    participants_count = 1500
    broadcast = False
    megagroup = True
    gigagroup = False
    verified = True
    deleted = False
    scam = False
    fake = False
    restricted = False


class BroadcastChannel:
    title = "Python News"
    username = "python_news"
    broadcast = True
    megagroup = False


def test_telegram_link_normalization_supports_public_and_private_group_links():
    assert _normalize_telegram_link("https://t.me/python_group") == (
        "https://t.me/python_group",
        "public",
        "python_group",
    )
    assert _normalize_telegram_link("https://t.me/+AbCdEfGhIjKlMn") == (
        "https://t.me/+AbCdEfGhIjKlMn",
        "invite",
        "AbCdEfGhIjKlMn",
    )
    assert _normalize_telegram_link("https://t.me/joinchat/AbCdEfGhIjKlMn") == (
        "https://t.me/+AbCdEfGhIjKlMn",
        "invite",
        "AbCdEfGhIjKlMn",
    )


def test_telegram_link_normalization_rejects_non_chat_routes_and_bad_usernames():
    assert _normalize_telegram_link("https://t.me/c/123456/7") is None
    assert _normalize_telegram_link("https://t.me/share/url") is None
    assert _normalize_telegram_link("https://example.com/python_group") is None
    assert _normalize_telegram_link("https://t.me/ab") is None


def test_only_basic_groups_and_supergroups_pass_group_filter():
    assert _is_group(BasicChat())
    assert _is_group(Supergroup())
    assert not _is_group(BroadcastChannel())


def test_group_quality_uses_members_and_rejects_hard_spam():
    quality = _quality(Supergroup())
    assert quality is not None
    assert quality[1] == 1500

    spam = Supergroup()
    spam.title = "Casino Python"
    assert _quality(spam) is None
