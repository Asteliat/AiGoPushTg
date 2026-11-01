import pytest

from app.services.automation import _validate_payload


def test_campaign_payload_accepts_text():
    assert _validate_payload("broadcast", {"text": "Hello"}) == {
        "text": "Hello", "file_path": "", "caption": ""
    }


def test_invitation_requires_message_with_link():
    with pytest.raises(ValueError, match="ссылку-приглашение"):
        _validate_payload("invite", {"text": "Привет"})


def test_campaign_rejects_empty_payload():
    with pytest.raises(ValueError, match="Добавьте текст или файл"):
        _validate_payload("broadcast", {})


def test_campaign_rejects_unknown_kind():
    with pytest.raises(ValueError, match="Поддерживаются"):
        _validate_payload("members", {"text": "hello"})


def test_invitation_accepts_telegram_invite_link():
    assert _validate_payload("invite", {"text": "Join us: https://t.me/example"})["text"].endswith("https://t.me/example")
