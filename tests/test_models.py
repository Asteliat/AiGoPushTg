from app.storage.models import Account, DraftStatus

def test_draft_status_values() -> None:
    assert {x.value for x in DraftStatus} == {"pending", "approved", "rejected"}


def test_account_has_learning_controls() -> None:
    assert {"learning_status", "learning_started_at", "learning_ends_at", "learning_limit", "learning_prompt"} <= set(Account.__table__.columns.keys())
