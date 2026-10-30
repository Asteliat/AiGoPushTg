from app.services.triggers import find_matching_term, format_trigger_duration, parse_trigger_terms
from app.storage.models import TriggerEvent, TriggerRule


def test_trigger_terms_accept_comma_semicolon_and_newline_separators():
    assert parse_trigger_terms("впн, VPN; Випиэн\nVPN") == ["впн", "VPN", "Випиэн"]


def test_trigger_matching_is_case_insensitive_and_matches_phrases_as_whole_terms():
    assert find_matching_term("Мне нужен VPN прямо сейчас", ["впн", "VPN", "Випиэн"]) == "VPN"
    assert find_matching_term("обсуждаем продажа коров сегодня", ["коровы", "продажа коров", "амбары"]) == "продажа коров"
    assert find_matching_term("впнщик написал", ["впн"]) is None


def test_trigger_duration_includes_days_hours_minutes_seconds():
    assert format_trigger_duration(90061) == "01:01:01:01"
    assert format_trigger_duration(0) == "00:00:00:00"


def test_trigger_models_have_global_and_account_scopes_and_deduplication():
    assert {"account_id", "apply_mode", "terms", "response_instructions", "cooldown_seconds", "message_cooldown_seconds", "enabled"} <= set(TriggerRule.__table__.columns.keys())
    assert {"account_id", "trigger_id", "chat_id", "message_id", "sender_id", "matched_term", "status"} <= set(TriggerEvent.__table__.columns.keys())
    constraints = {constraint.name for constraint in TriggerEvent.__table__.constraints}
    assert "uq_trigger_event_message" in constraints
