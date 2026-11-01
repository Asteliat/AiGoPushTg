from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import app.bot.ui as ui


@pytest.mark.asyncio
async def test_group_candidate_verification_uses_isolated_session(monkeypatch):
    client = SimpleNamespace(is_user_authorized=lambda: _authorized())
    account = SimpleNamespace(id=1)
    candidates = [{"link": "https://t.me/python_group"}]
    expected = [{"link": "https://t.me/python_group", "verified": True}]
    calls = {}

    async def _authorized():
        return True

    @asynccontextmanager
    async def fake_isolated_client(received_aid):
        calls["account_id"] = received_aid
        yield client

    async def fake_verify(received_client, received_candidates, concurrency):
        calls["client"] = received_client
        calls["candidates"] = received_candidates
        calls["concurrency"] = concurrency
        return expected

    monkeypatch.setattr(ui, "isolated_account_client", fake_isolated_client)
    monkeypatch.setattr(ui, "verify_group_candidates", fake_verify)

    result = await ui._verify_group_candidates_isolated(account, candidates, concurrency=3)

    assert result == expected
    assert calls == {
        "account_id": account.id,
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
    async def fake_isolated_client(_aid):
        yield client

    async def unexpected_verify(*_args, **_kwargs):
        pytest.fail("Verification must not run for an unauthorized session")

    monkeypatch.setattr(ui, "isolated_account_client", fake_isolated_client)
    monkeypatch.setattr(ui, "verify_group_candidates", unexpected_verify)

    with pytest.raises(RuntimeError, match="Сессия аккаунта не авторизована"):
        await ui._verify_group_candidates_isolated(SimpleNamespace(id=1), [])
