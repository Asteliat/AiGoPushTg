from app.services.challenge_monitor import (
    extract_button_labels,
    format_challenge_alert,
    is_challenge_prompt,
)


def test_detects_russian_button_verification() -> None:
    assert is_challenge_prompt("Rita, а ты точно не бот? Нажми на кнопку!")


def test_detects_emoji_verification() -> None:
    assert is_challenge_prompt("Выберите смайлик, чтобы подтвердить, что вы человек")


def test_detects_english_verification() -> None:
    assert is_challenge_prompt("Please click the button to verify you are human")


def test_ignores_unrelated_bot_message() -> None:
    assert not is_challenge_prompt("Добро пожаловать в группу! Правила закреплены.")


def test_reads_button_labels_without_clicking() -> None:
    class Button:
        def __init__(self, text):
            self.text = text

    class Message:
        buttons = [[Button("Я не бот"), Button("Правила")]]

    assert extract_button_labels(Message()) == ["Я не бот", "Правила"]


def test_alert_contains_account_and_manual_review_notice() -> None:
    alert = format_challenge_alert(
        account_id=7,
        account_name="Test",
        chat_id=-100123,
        chat_title="My group",
        message_id=55,
        sender_name="LolsBot",
        text="Нажми на кнопку!",
        button_labels=["Подтвердить"],
    )
    assert "LolsBot" in alert
    assert "ID группы: -100123" in alert
    assert "не нажимал кнопки" in alert
