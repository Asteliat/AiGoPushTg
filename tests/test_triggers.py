from app.services.triggers import find_matching_term, format_trigger_duration, parse_trigger_terms, select_matching_trigger
from app.storage.models import TriggerEvent, TriggerRule


def test_trigger_terms_accept_comma_semicolon_and_newline_separators():
    assert parse_trigger_terms("впн, VPN; Випиэн\nVPN") == ["впн", "VPN", "Випиэн"]


def test_trigger_terms_accept_space_separated_words_and_quoted_phrases():
    assert parse_trigger_terms("впн VPN Випиэн") == ["впн", "VPN", "Випиэн"]
    assert parse_trigger_terms('"продажа коров" амбары') == ["продажа коров", "амбары"]


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


class Rule:
    def __init__(self, terms, apply_mode="all"):
        self.terms = terms
        self.apply_mode = apply_mode


def test_account_trigger_has_priority_over_global_trigger():
    local = Rule("vpn")
    global_rule = Rule("vpn")
    selected, term = select_matching_trigger([local], [global_rule], "VPN работает", has_local_rules=True)
    assert selected is local
    assert term == "vpn"


def test_empty_only_global_trigger_skips_accounts_with_local_rules():
    global_rule = Rule("vpn", apply_mode="empty_only")
    selected, term = select_matching_trigger([], [global_rule], "VPN работает", has_local_rules=True)
    assert selected is None
    assert term is None


def test_empty_only_global_trigger_applies_to_accounts_without_local_rules():
    global_rule = Rule("vpn", apply_mode="empty_only")
    selected, term = select_matching_trigger([], [global_rule], "VPN работает", has_local_rules=False)
    assert selected is global_rule
    assert term == "vpn"


def test_all_global_trigger_is_fallback_after_local_triggers():
    local = Rule("коровы")
    global_rule = Rule("vpn", apply_mode="all")
    selected, term = select_matching_trigger([local], [global_rule], "Нужен VPN", has_local_rules=True)
    assert selected is global_rule
    assert term == "vpn"
