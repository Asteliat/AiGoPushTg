from app.storage.models import Account, ChatBinding, DraftStatus, Example

def test_draft_status_values() -> None:
    assert {x.value for x in DraftStatus} == {"pending", "approved", "rejected"}


def test_account_has_learning_controls() -> None:
    assert {"learning_status", "learning_started_at", "learning_ends_at", "learning_limit", "learning_prompt"} <= set(Account.__table__.columns.keys())

def test_account_has_pause_and_personal_context_controls() -> None:
    columns = set(Account.__table__.columns.keys())
    assert {"manual_pause", "persona_style_override", "learning_prompt"} <= columns


def test_examples_support_account_specific_scope() -> None:
    assert "account_id" in Example.__table__.columns


def test_chat_bindings_support_auto_discovery() -> None:
    assert "auto_discovered" in ChatBinding.__table__.columns
