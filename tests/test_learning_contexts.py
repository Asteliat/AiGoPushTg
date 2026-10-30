from app.services.ai_responses import build_reply_style_instructions
from app.services.channels import build_channel_analysis_style, build_channel_analysis_user_content


def test_channel_analysis_uses_channel_learning_not_chat_learning() -> None:
    style = build_channel_analysis_style(
        post_instructions="Правила публикаций",
        comments_instructions="Правила комментариев",
        restrictions_instructions="Ограничения канала",
        channel_learning_prompt="Контекст автообучения канала",
    )

    assert "Правила публикаций" in style
    assert "Правила комментариев" in style
    assert "Ограничения канала" in style
    assert "Контекст автообучения канала" in style
    assert "Стиль личности" not in style
    assert "Общие правила аккаунта" not in style
    assert "контекст выбранных чатов" not in style


def test_channel_comment_replies_reuse_chat_learning_and_account_style() -> None:
    style = build_reply_style_instructions(
        persona_style="Стиль личности",
        account_style_override="Общие правила аккаунта",
        chat_learning_prompt="Контекст автообучения чатов",
    )

    assert "Стиль личности" in style
    assert "Общие правила аккаунта" in style
    assert "Контекст автообучения чатов" in style
    assert "Ограничения комментариев канала" not in style
    assert "автообучения этого канала" not in style


def test_channel_analysis_payload_includes_photo_as_image() -> None:
    image = "data:image/jpeg;base64,ZmFrZQ=="
    content = build_channel_analysis_user_content(
        "Текст поста", ["Первый комментарий"], image
    )
    assert len(content) == 2
    assert content[0]["type"] == "text"
    assert "Текст поста" in content[0]["text"]
    assert "Первый комментарий" in content[0]["text"]
    assert content[1] == {"type": "image_url", "image_url": {"url": image}}


def test_channel_analysis_payload_without_photo_stays_text_only() -> None:
    content = build_channel_analysis_user_content("Текст поста", [])
    assert len(content) == 1
    assert content[0]["type"] == "text"
