from types import SimpleNamespace

import pytest

from app.ai import provider


class FakeCompletions:
    def __init__(self, *, failure=None):
        self.failure = failure

    async def create(self, **_kwargs):
        if self.failure:
            raise self.failure
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Готовый ответ"))]
        )


class FakeClient:
    def __init__(self, *, failure=None):
        self.chat = SimpleNamespace(completions=FakeCompletions(failure=failure))
        self.closed = False

    async def close(self):
        self.closed = True


def fake_agent():
    return SimpleNamespace(
        enabled=True,
        api_key="test-key",
        base_url="https://api.example/v1",
        model="test-model",
        timeout_seconds=10,
    )


@pytest.mark.asyncio
async def test_generate_reply_closes_provider_client_on_success(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(provider, "AsyncOpenAI", lambda **_kwargs: client)

    result = await provider.generate_reply(
        agent=fake_agent(),
        incoming_text="Привет",
        style_instructions="Кратко",
        examples=[],
    )

    assert result == "Готовый ответ"
    assert client.closed is True


@pytest.mark.asyncio
async def test_generate_reply_closes_provider_client_on_failure(monkeypatch):
    client = FakeClient(failure=ValueError("provider error"))
    monkeypatch.setattr(provider, "AsyncOpenAI", lambda **_kwargs: client)

    with pytest.raises(provider.AIUnavailableError):
        await provider.generate_reply(
            agent=fake_agent(),
            incoming_text="Привет",
            style_instructions="Кратко",
            examples=[],
        )

    assert client.closed is True
