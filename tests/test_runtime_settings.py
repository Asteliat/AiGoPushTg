from app.services.ai_responses import resolve_runtime_override


def test_agent_runtime_override_is_used_when_configured():
    assert resolve_runtime_override(70, 100) == 70


def test_global_runtime_value_is_used_when_agent_inherits():
    assert resolve_runtime_override(None, 100) == 100


def test_zero_is_a_valid_explicit_agent_override():
    assert resolve_runtime_override(0, 70) == 0
