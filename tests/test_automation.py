from datetime import datetime, timezone

import pytest

from app.services.automation import _quiet_until, _validate_payload


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
    with pytest.raises(ValueError, match="Неизвестный тип кампании"):
        _validate_payload("members", {"text": "hello"})


def test_invitation_accepts_telegram_invite_link():
    assert _validate_payload("invite", {"text": "Join us: https://t.me/example"})["text"].endswith("https://t.me/example")


def test_quiet_hours_cross_midnight():
    now = datetime(2026, 11, 1, 23, 15, tzinfo=timezone.utc)
    assert _quiet_until(now, "22:00", "07:00") == datetime(
        2026, 11, 2, 7, 0, tzinfo=timezone.utc
    )


def test_quiet_hours_before_end():
    now = datetime(2026, 11, 1, 2, 15, tzinfo=timezone.utc)
    assert _quiet_until(now, "22:00", "07:00") == datetime(
        2026, 11, 1, 7, 0, tzinfo=timezone.utc
    )


def test_quiet_hours_disabled():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    assert _quiet_until(now, None, None) is None



def test_recurring_schedule_interval_must_be_at_least_one_day():
    from app.services.automation import _validate_payload

    assert _validate_payload("broadcast", {"text": "Hello"})["text"] == "Hello"


def test_quiet_hours_reject_partial_window():
    from app.services.automation import _normalize_quiet_hours

    with pytest.raises(ValueError):
        _normalize_quiet_hours("22:00", None)



def test_group_broadcast_payload_accepts_text():
    from app.services.automation import _validate_payload

    assert _validate_payload("group_broadcast", {"text": "Announcement"})["text"] == "Announcement"


def test_operation_limit_quiet_hours_are_validated():
    from app.services.automation import _normalize_quiet_hours

    assert _normalize_quiet_hours("23:00", "07:00") == ("23:00", "07:00")



def test_direct_invite_requires_a_target_group():
    from app.services.automation import _validate_payload

    payload = _validate_payload("direct_invite", {
        "target_group_id": -100123456,
        "target_group_ref": "@mygroup",
        "target_group_label": "My Group",
    })
    assert payload["target_group_id"] == -100123456
    with pytest.raises(ValueError, match="целевую супергруппу"):
        _validate_payload("direct_invite", {})



@pytest.mark.asyncio
async def test_direct_invite_consent_requires_supergroup():
    from app.services.automation import grant_direct_invite_consent

    with pytest.raises(ValueError, match="супергруппу"):
        await grant_direct_invite_consent(
            1,
            peer={"peer_id": 42, "peer_label": "User"},
            group={"peer_id": -10042, "is_megagroup": False},
            consent_source="user asked",
        )



@pytest.mark.asyncio
async def test_direct_invite_schedule_must_be_one_shot():
    from datetime import datetime, timezone
    from app.services.automation import create_schedule

    with pytest.raises(ValueError, match="однократно"):
        await create_schedule(
            1, kind="direct_invite", recipients=[42],
            payload={"target_group_id": -100123},
            starts_at=datetime.now(timezone.utc), interval_seconds=86400,
            max_runs=3,
        )
