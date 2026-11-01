from app.telegram_gateway.discovery import _engine_search, _is_group, _normalize_telegram_link, _quality


class Chat:
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
    assert _is_group(Chat())
    assert _is_group(Supergroup())
    assert not _is_group(BroadcastChannel())


def test_group_quality_uses_members_and_rejects_hard_spam():
    quality = _quality(Supergroup())
    assert quality is not None
    assert quality[1] == 1500

    spam = Supergroup()
    spam.title = "Casino Python"
    assert _quality(spam) is None



def test_search_engine_captcha_is_reported_and_not_parsed(monkeypatch):
    from io import BytesIO
    import app.telegram_gateway.discovery as discovery

    def fake_urlopen(request, timeout):
        return BytesIO(b"<html><title>CAPTCHA</title>verify you are human</html>")

    monkeypatch.setattr(discovery, "urlopen", fake_urlopen)
    results, status = _engine_search(
        "Google",
        "https://example.com/search?q={q}&start={offset}",
        "python groups",
        0,
    )

    assert results == []
    assert status == "captcha"



def test_provider_cooldown_pauses_only_blocked_engine():
    import app.telegram_gateway.discovery as discovery

    discovery._PROVIDER_COOLDOWNS.clear()
    discovery._PROVIDER_FAILURES.clear()

    discovery._record_provider_status("Google", "captcha")

    assert not discovery._provider_available("Google")
    assert discovery._provider_available("Bing")
    assert discovery._PROVIDER_FAILURES["Google"] == 1


def test_provider_success_clears_its_cooldown():
    import app.telegram_gateway.discovery as discovery

    discovery._PROVIDER_COOLDOWNS["Google"] = 9999999999.0
    discovery._PROVIDER_FAILURES["Google"] = 2

    discovery._record_provider_status("Google", "ok")

    assert discovery._provider_available("Google")
    assert "Google" not in discovery._PROVIDER_FAILURES


async def test_web_search_uses_short_cache(monkeypatch):
    import app.telegram_gateway.discovery as discovery

    discovery._SEARCH_CACHE.clear()
    calls = 0

    def fake_engine_search(engine, template, query, page=0):
        nonlocal calls
        calls += 1
        return ([{
            "id": None,
            "title": "Python group",
            "username": "python_group",
            "link": "https://t.me/python_group",
            "members": 0,
            "verified": False,
            "score": 24,
            "source": engine,
            "kind": "unknown",
            "search_page": page + 1,
        }], "ok")

    monkeypatch.setattr(discovery, "_engine_search", fake_engine_search)
    first = await discovery.search_web_groups("python cache test", pages_per_query=1)
    first_call_count = calls
    second = await discovery.search_web_groups("python cache test", pages_per_query=1)

    assert first
    assert second == first
    assert calls == first_call_count
