from types import SimpleNamespace

import pytest

from app.ai.provider import chat_completion_with_retry
from app.services.chat_bindings import discovered_binding_values
from app.services.notifications import _destinations


def test_newly_discovered_chat_is_not_enabled():
    assert discovered_binding_values("  Example chat  ") == {
        "title": "Example chat",
        "enabled": False,
        "auto_discovered": True,
    }


def test_notification_destinations_parse_threads_and_ignore_invalid_values():
    assert _destinations("-100123:42, -100123:42, bad, 0, -100456") == [
        (-100123, 42),
        (-100456, None),
    ]


@pytest.mark.asyncio
async def test_ai_provider_retries_temporary_server_errors(monkeypatch):
    class TemporaryError(Exception):
        status_code = 503

    class FakeClient:
        def __init__(self):
            self.attempts = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            self.attempts += 1
            if self.attempts < 3:
                raise TemporaryError("temporary")
            return "ok"

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("app.ai.provider.asyncio.sleep", no_wait)
    client = FakeClient()
    result = await chat_completion_with_retry(client, model="test")
    assert result == "ok"
    assert client.attempts == 3


@pytest.mark.asyncio
async def test_ai_provider_does_not_retry_permanent_errors(monkeypatch):
    class PermanentError(Exception):
        status_code = 400

    class FakeClient:
        def __init__(self):
            self.attempts = 0
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        async def create(self, **kwargs):
            self.attempts += 1
            raise PermanentError("bad request")

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("app.ai.provider.asyncio.sleep", no_wait)
    client = FakeClient()
    with pytest.raises(PermanentError):
        await chat_completion_with_retry(client, model="test")
    assert client.attempts == 1
