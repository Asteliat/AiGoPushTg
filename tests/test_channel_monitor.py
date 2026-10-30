from types import SimpleNamespace

import pytest

from app.workers.channel_monitor import (
    fetch_discussion_comments,
    has_channel_post_content,
    is_legacy_comment_threshold_skip,
)


class FakeTelegramClient:
    def __init__(self, messages=None, errors=None):
        self.messages = messages or {}
        self.errors = errors or {}
        self.calls = []

    async def iter_messages(self, peer, *, reply_to, limit):
        self.calls.append((peer, reply_to, limit))
        error = self.errors.get((peer, reply_to))
        if error:
            raise error
        for message in self.messages.get((peer, reply_to), []):
            yield message


def message(text="", *, outgoing=False, photo=None):
    return SimpleNamespace(message=text, out=outgoing, photo=photo)


def test_empty_channel_posts_are_rejected_but_text_and_photo_posts_are_accepted():
    assert has_channel_post_content("  text  ", None)
    assert has_channel_post_content("", message(photo=object()))
    assert not has_channel_post_content("", message())
    assert not has_channel_post_content(None, None)


@pytest.mark.asyncio
async def test_discussion_comments_use_group_root_id_and_ignore_empty_or_own_messages():
    client = FakeTelegramClient({
        ("discussion", 61): [
            message("second comment"),
            message(""),
            message("our own comment", outgoing=True),
            message("first comment"),
        ],
    })

    comments, diagnostics = await fetch_discussion_comments(
        client,
        channel_entity="channel",
        channel_post_id=17,
        discussion_group="discussion",
        discussion_root_id=61,
    )

    assert comments == ["first comment", "second comment"]
    assert client.calls == [("discussion", 61, 100)]
    assert "discussion_group" in diagnostics
    assert "text=2" in diagnostics


@pytest.mark.asyncio
async def test_discussion_comments_fall_back_to_broadcast_channel_peer():
    client = FakeTelegramClient({
        ("discussion", 61): [],
        ("channel", 17): [message("comment from channel reply lookup")],
    })

    comments, diagnostics = await fetch_discussion_comments(
        client,
        channel_entity="channel",
        channel_post_id=17,
        discussion_group="discussion",
        discussion_root_id=61,
    )

    assert comments == ["comment from channel reply lookup"]
    assert client.calls == [("discussion", 61, 100), ("channel", 17, 100)]
    assert "channel: messages=1, text=1" in diagnostics


@pytest.mark.asyncio
async def test_no_audience_comments_is_a_valid_empty_context_not_an_error():
    client = FakeTelegramClient()

    comments, diagnostics = await fetch_discussion_comments(
        client,
        channel_entity="channel",
        channel_post_id=17,
        discussion_group="discussion",
        discussion_root_id=61,
    )

    assert comments == []
    assert len(client.calls) == 2
    assert "text=0" in diagnostics


@pytest.mark.asyncio
async def test_comment_fetch_raises_only_when_both_telegram_paths_fail():
    client = FakeTelegramClient(errors={
        ("discussion", 61): RuntimeError("group read failed"),
        ("channel", 17): RuntimeError("channel read failed"),
    })

    with pytest.raises(RuntimeError, match="channel read failed"):
        await fetch_discussion_comments(
            client,
            channel_entity="channel",
            channel_post_id=17,
            discussion_group="discussion",
            discussion_root_id=61,
        )


def test_legacy_posts_skipped_only_by_comment_threshold_are_reopened():
    assert is_legacy_comment_threshold_skip("skipped by comment threshold: 0/1")
    assert is_legacy_comment_threshold_skip("skipped: 0 comments, minimum 2")
    assert not is_legacy_comment_threshold_skip("waiting for comments: 0/2")
    assert not is_legacy_comment_threshold_skip("skipped by AI: no reply")


from app.services.channels import (
    build_channel_analysis_user_content,
    build_channel_analysis_user_text,
    is_multimodal_content_type_error,
    request_channel_analysis_completion,
)


def test_text_only_channel_analysis_uses_string_content():
    content = build_channel_analysis_user_content("Проверка поста", ["Комментарий"])
    assert isinstance(content, str)
    assert "Проверка поста" in content
    assert "ПУБЛИКАЦИЯ:\nПроверка поста" in content
    assert "\n\nКОММЕНТАРИИ:\n- Комментарий" in content


def test_image_channel_analysis_uses_multimodal_parts():
    content = build_channel_analysis_user_content(
        "Пост с фото", [], "data:image/jpeg;base64,abc"
    )
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"


def test_image_fallback_explicitly_avoids_claiming_image_was_analyzed():
    content = build_channel_analysis_user_text(
        "Пост с фото", [], image_not_analyzed=True
    )
    assert isinstance(content, str)
    assert "не приняла изображение" in content
    assert "не делай выводов о содержимом изображения" in content


class FakeBadRequestError(Exception):
    status_code = 400


def test_multimodal_content_rejection_is_detected_only_for_content_schema_error():
    assert is_multimodal_content_type_error(
        FakeBadRequestError("messages[1].content must be a string")
    )
    assert not is_multimodal_content_type_error(
        FakeBadRequestError("model does not exist")
    )


@pytest.mark.asyncio
async def test_channel_ai_retries_image_request_as_text_on_content_type_error(monkeypatch):
    import app.services.channels as channels

    calls = []

    async def fake_chat_completion(client, **kwargs):
        calls.append(kwargs["messages"][1]["content"])
        if len(calls) == 1:
            raise FakeBadRequestError("messages[1].content must be a string")
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"on_topic":true,"should_reply":false,"summary":"ok","response":""}')
        )])

    monkeypatch.setattr(channels, "chat_completion_with_retry", fake_chat_completion)

    multimodal = build_channel_analysis_user_content(
        "Пост", [], "data:image/jpeg;base64,abc"
    )
    fallback = build_channel_analysis_user_text(
        "Пост", [], image_not_analyzed=True
    )
    result = await request_channel_analysis_completion(
        object(),
        model="llama-text-model",
        prompt="system",
        user_content=multimodal,
        image_fallback_text=fallback,
    )

    assert len(calls) == 2
    assert isinstance(calls[0], list)
    assert isinstance(calls[1], str)
    assert "не делай выводов" in calls[1]
    assert result.choices[0].message.content.startswith("{")
