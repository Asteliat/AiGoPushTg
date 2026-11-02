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



def test_direct_invite_permission_requires_invite_users_right():
    from types import SimpleNamespace
    from app.services.automation import _has_invite_permission

    assert _has_invite_permission(SimpleNamespace(is_creator=True, is_admin=True))
    assert _has_invite_permission(SimpleNamespace(is_admin=True, invite_users=True))
    assert not _has_invite_permission(SimpleNamespace(is_admin=True, invite_users=False))
    assert not _has_invite_permission(SimpleNamespace(is_admin=False, invite_users=True))
    assert not _has_invite_permission(SimpleNamespace(is_admin=True))


def test_direct_invite_permission_checks_admin_rights_object():
    from types import SimpleNamespace
    from app.services.automation import _has_invite_permission

    assert _has_invite_permission(SimpleNamespace(is_admin=True, admin_rights=SimpleNamespace(invite_users=True)))
    assert not _has_invite_permission(SimpleNamespace(is_admin=True, admin_rights=SimpleNamespace(invite_users=False)))



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



def test_recurring_schedule_interval_boundaries():
    from app.services.automation import _validate_schedule_interval

    assert _validate_schedule_interval(3600) == 3600
    assert _validate_schedule_interval(31536000) == 31536000
    with pytest.raises(ValueError, match="1 часа"):
        _validate_schedule_interval(3599)
    with pytest.raises(ValueError, match="365 дней"):
        _validate_schedule_interval(31536001)



@pytest.mark.asyncio
async def test_campaign_peer_resolution_rejects_reassigned_username():
    from telethon.tl.types import PeerUser
    from app.services.automation import _resolve_campaign_peer

    class FakeClient:
        async def get_entity(self, reference):
            assert reference == "@old_username"
            return PeerUser(user_id=999)

    with pytest.raises(ValueError, match="Username получателя изменился"):
        await _resolve_campaign_peer(FakeClient(), 123, "@old_username")


@pytest.mark.asyncio
async def test_campaign_peer_resolution_accepts_matching_username_id():
    from telethon.tl.types import PeerUser
    from app.services.automation import _resolve_campaign_peer

    expected = PeerUser(user_id=123)

    class FakeClient:
        async def get_entity(self, reference):
            assert reference == "@current_username"
            return expected

    result = await _resolve_campaign_peer(FakeClient(), 123, "@current_username")
    assert result == expected



def test_module_kind_mapping_is_explicit():
    from app.services.automation import _module_name_for_kind

    assert _module_name_for_kind("invite") == "invite"
    assert _module_name_for_kind("direct_invite") == "invite"
    assert _module_name_for_kind("broadcast") == "broadcast"
    assert _module_name_for_kind("subscription_join") == "subscription"
    assert _module_name_for_kind("subscription_leave") == "subscription"
    assert _module_name_for_kind("reaction") == "reaction"


def test_global_module_policy_defaults_and_batch_size():
    from app.services.automation import MODULE_POLICY_DEFAULTS

    assert MODULE_POLICY_DEFAULTS["invite"]["per_account_limit"] == 99
    assert MODULE_POLICY_DEFAULTS["invite"]["daily_account_limit"] == 25
    assert MODULE_POLICY_DEFAULTS["broadcast"]["batch_size"] == 10
    assert MODULE_POLICY_DEFAULTS["subscription"]["floodwait_threshold"] == 10


@pytest.mark.asyncio
async def test_individual_operation_limits_are_rejected_without_database_access():
    from app.services.automation import set_operation_limits

    with pytest.raises(ValueError, match="Индивидуальные лимиты отключены"):
        await set_operation_limits(
            "invite", interval_seconds=10, daily_limit=10, account_id=1
        )


@pytest.mark.asyncio
async def test_unknown_module_policy_is_rejected():
    from app.services.automation import module_policy

    with pytest.raises(ValueError, match="Неизвестный модуль"):
        await module_policy("unknown")
