from app.services.ai_responses import resolve_runtime_override


def test_account_runtime_override_has_highest_priority():
    assert resolve_runtime_override(35, 70, 100) == 35


def test_agent_runtime_override_is_used_when_account_inherits():
    assert resolve_runtime_override(None, 70, 100) == 70


def test_global_runtime_value_is_used_when_account_and_agent_inherit():
    assert resolve_runtime_override(None, None, 100) == 100


def test_zero_is_a_valid_explicit_override():
    assert resolve_runtime_override(0, 70, 100) == 0
