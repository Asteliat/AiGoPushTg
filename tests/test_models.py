from app.storage.models import DraftStatus

def test_draft_status_values() -> None:
    assert {x.value for x in DraftStatus} == {"pending", "approved", "rejected"}
