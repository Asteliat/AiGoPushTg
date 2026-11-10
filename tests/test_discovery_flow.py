from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.bot import discovery_flow
from app.bot.discovery_flow import (
    CANDIDATE_LIMIT,
    collect_candidates,
    merge_candidates,
    search_with_accounts,
)
from tests.fakes import FakeSessionFactory, make_account


def candidate(link: str, *, score: int = 10, source: str = "Web", members: int = 0) -> dict:
    return {"link": link, "score": score, "source": source, "members": members}


def test_merge_keeps_the_best_score_and_every_source() -> None:
    merged = merge_candidates(
        [candidate("https://t.me/a", score=10, source="Bing")],
        [candidate("https://t.me/A", score=40, source="Yandex")],
    )

    assert len(merged) == 1
    assert merged[0]["score"] == 40
    assert set(merged[0]["source"].split(", ")) == {"Bing", "Yandex"}


def test_merge_drops_items_without_a_link() -> None:
    merged = merge_candidates([{"score": 5, "source": "Web"}, candidate("t.me/a")])

    assert [item["link"] for item in merged] == ["t.me/a"]


def test_merge_is_bounded() -> None:
    merged = merge_candidates([candidate(f"t.me/x{index}") for index in range(CANDIDATE_LIMIT + 50)])

    assert len(merged) == CANDIDATE_LIMIT


@pytest.mark.asyncio
async def test_collect_candidates_combines_both_sources(monkeypatch) -> None:
    async def fake_search(account_ids, query, *, native_search, log_label):
        return [candidate("t.me/native", source="Telegram")]

    async def native_search(client, query, *, limit):
        return []

    async def web_search(query, *, limit, pages_per_query):
        assert (limit, pages_per_query) == (600, 5)
        return [candidate("t.me/web", source="Google")]

    monkeypatch.setattr(discovery_flow, "search_with_accounts", fake_search)

    result = await collect_candidates(
        [1], "python", native_search=native_search, web_search=web_search, log_label="Group"
    )

    assert sorted(item["link"] for item in result) == ["t.me/native", "t.me/web"]


@pytest.mark.asyncio
async def test_collect_candidates_survives_a_failing_source(monkeypatch) -> None:
    async def failing_search(account_ids, query, *, native_search, log_label):
        raise RuntimeError("telegram is down")

    async def web_search(query, *, limit, pages_per_query):
        return [candidate("t.me/web")]

    monkeypatch.setattr(discovery_flow, "search_with_accounts", failing_search)

    result = await collect_candidates(
        [1],
        "python",
        native_search=lambda *a, **k: None,
        web_search=web_search,
        log_label="Group",
    )

    assert [item["link"] for item in result] == ["t.me/web"]


@pytest.mark.asyncio
async def test_search_with_accounts_skips_disabled_and_unauthorized(monkeypatch) -> None:
    enabled = make_account(1)
    disabled = make_account(2, enabled=False)
    factory = FakeSessionFactory({("Account", 1): enabled, ("Account", 2): disabled})
    monkeypatch.setattr(discovery_flow, "SessionFactory", factory)
    used: list[int] = []

    @asynccontextmanager
    async def fake_client(account):
        used.append(account.id)
        yield SimpleNamespace(is_user_authorized=lambda: _bool(account.id == 3))

    monkeypatch.setattr(discovery_flow, "isolated_telegram_client", fake_client)
    factory.objects[("Account", 3)] = make_account(3)

    async def native_search(client, query, *, limit):
        return [candidate("t.me/native")]

    result = await search_with_accounts(
        [1, 2, 3], "python", native_search=native_search, log_label="Group"
    )

    # Account 2 is disabled and never opened; 1 is opened but its session is not
    # authorized, so only account 3 contributes results.
    assert result == [candidate("t.me/native")]
    assert used == [1, 3]


async def _bool(value):
    return value
