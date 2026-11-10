from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import app.bot.screens.chats as chats


@pytest.mark.asyncio
async def test_group_candidate_verification_uses_isolated_session(monkeypatch):
    client = SimpleNamespace(is_user_authorized=lambda: _authorized())
    account_id = 1
    candidates = [{"link": "https://t.me/python_group"}]
    expected = [{"link": "https://t.me/python_group", "verified": True}]
    calls = {}

    async def _authorized():
        return True

    @asynccontextmanager
    async def fake_isolated_client(received_aid):
        calls["account_id"] = received_aid
        yield SimpleNamespace(id=received_aid), client

    async def fake_verify(received_client, received_candidates, concurrency):
        calls["client"] = received_client
        calls["candidates"] = received_candidates
        calls["concurrency"] = concurrency
        return expected

    monkeypatch.setattr(chats, "isolated_account_client", fake_isolated_client)
    monkeypatch.setattr(chats, "verify_group_candidates", fake_verify)

    result = await chats._verify_group_candidates_isolated(account_id, candidates, concurrency=3)

    assert result == expected
    assert calls == {
        "account_id": account_id,
        "client": client,
        "candidates": candidates,
        "concurrency": 3,
    }


@pytest.mark.asyncio
async def test_group_candidate_verification_rejects_unauthorized_snapshot(monkeypatch):
    async def _unauthorized():
        return False

    client = SimpleNamespace(is_user_authorized=_unauthorized)

    @asynccontextmanager
    async def fake_isolated_client(aid):
        if not await client.is_user_authorized():
            raise RuntimeError("Сессия аккаунта не авторизована.")
        yield SimpleNamespace(id=aid), client

    async def unexpected_verify(*_args, **_kwargs):
        pytest.fail("Verification must not run for an unauthorized session")

    monkeypatch.setattr(chats, "isolated_account_client", fake_isolated_client)
    monkeypatch.setattr(chats, "verify_group_candidates", unexpected_verify)

    with pytest.raises(RuntimeError, match="Сессия аккаунта не авторизована"):
        await chats._verify_group_candidates_isolated(1, [])
