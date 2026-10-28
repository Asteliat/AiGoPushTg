from app.settings import Settings

def test_admin_id_set_parses_csv() -> None:
    assert Settings(admin_ids="123, 456,no,,789").admin_id_set == {123, 456, 789}

def test_ai_disabled_by_default() -> None:
    assert Settings().ai_enabled is False
