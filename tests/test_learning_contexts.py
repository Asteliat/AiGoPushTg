from app.services.ai_responses import build_reply_style_instructions
from app.services.channels import build_channel_analysis_style


def test_channel_analysis_uses_channel_learning_not_chat_learning() -> None:
    style = build_channel_analysis_style(
        persona_style="Стиль личности",
        account_style_override="Общие правила аккаунта",
        post_instructions="Правила публикаций",
        comments_instructions="Правила комментариев",
        restrictions_instructions="Ограничения канала",
        channel_learning_prompt="Контекст автообучения канала",
    )

    assert "Стиль личности" in style
    assert "Общие правила аккаунта" in style
    assert "Контекст автообучения канала" in style
    assert "контекст выбранных чатов" not in style


def test_channel_comment_replies_reuse_chat_learning_and_account_style() -> None:
    style = build_reply_style_instructions(
        persona_style="Стиль личности",
        account_style_override="Общие правила аккаунта",
        chat_learning_prompt="Контекст автообучения чатов",
        additional_instructions="Ограничения комментариев канала",
    )

    assert "Стиль личности" in style
    assert "Общие правила аккаунта" in style
    assert "Контекст автообучения чатов" in style
    assert "Ограничения комментариев канала" in style
    assert "автообучения этого канала" not in style
