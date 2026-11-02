from app.services.ai_responses import resolve_agent_runtime_override, resolve_runtime_override


def test_probability_uses_agent_override():
    assert resolve_agent_runtime_override(70, 100) == 70


def test_probability_uses_global_value_when_agent_inherits():
    assert resolve_agent_runtime_override(None, 100) == 100


def test_zero_is_a_valid_agent_probability_override():
    assert resolve_agent_runtime_override(0, 70) == 0


def test_account_cooldown_override_still_has_highest_priority():
    assert resolve_runtime_override(35, 70, 100) == 35


def test_cooldown_falls_back_to_agent_then_global():
    assert resolve_runtime_override(None, 70, 100) == 70
    assert resolve_runtime_override(None, None, 100) == 100
