from app.services.ai_responses import build_reply_style_instructions
from app.services.channels import (
    build_channel_analysis_prompt,
    build_channel_analysis_style,
    build_channel_analysis_user_content,
)


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


def test_channel_analysis_payload_without_photo_stays_plain_text() -> None:
    content = build_channel_analysis_user_content("Текст поста", [])
    assert isinstance(content, str)
    assert "ПУБЛИКАЦИЯ:\\nТекст поста" not in content
    assert "ПУБЛИКАЦИЯ:\nТекст поста" in content



def test_channel_probability_selected_reply_requires_comment_unless_restricted() -> None:
    prompt = build_channel_analysis_prompt("Ограничения канала", reply_requested=True)

    assert "не решай самостоятельно, нужен ли комментарий" in prompt
    assert "подготовь один естественный, содержательный комментарий" in prompt
    assert "запрещён заданными ограничениями" in prompt
    assert "Ограничения канала" in prompt


def test_channel_probability_not_selected_requests_analysis_without_comment() -> None:
    prompt = build_channel_analysis_prompt("Стиль канала", reply_requested=False)

    assert "Случайная вероятность не выбрала" in prompt
    assert 'should_reply=false и response=""' in prompt
    assert "Стиль канала" in prompt
