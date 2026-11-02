import pytest
from pydantic import ValidationError

from app.api import miniapp
from app.api.miniapp import ChannelAssistantPatch, ContentGenerateInput, ContentDraftPatch
from app.storage.models import ContentDraft


def test_content_generation_requires_a_useful_brief():
    with pytest.raises(ValidationError):
        ContentGenerateInput(brief="no")
    payload = ContentGenerateInput(brief="Write a concise update about the new release.")
    assert payload.account_id is None
    assert payload.brief.startswith("Write")


def test_content_draft_patch_only_accepts_draft_or_archived():
    assert ContentDraftPatch(status="archived").status == "archived"
    with pytest.raises(ValidationError):
        ContentDraftPatch(status="published")


def test_channel_assistant_settings_are_bounded():
    assert ChannelAssistantPatch(reply_probability_override=0).reply_probability_override == 0
    with pytest.raises(ValidationError):
        ChannelAssistantPatch(reply_probability_override=101)
    with pytest.raises(ValidationError):
        ChannelAssistantPatch(reply_cooldown_seconds_override=-1)


def test_content_draft_is_persisted_as_its_own_table():
    assert ContentDraft.__tablename__ == "content_drafts"
    assert {"prompt", "body", "status", "account_id", "agent_id", "persona_id"} <= set(ContentDraft.__table__.columns.keys())


def test_retired_bulk_automation_is_disabled_by_default():
    from app.settings import Settings

    assert Settings(_env_file=None).legacy_automation_enabled is False


def test_new_workspace_routes_exist_and_legacy_module_routes_are_retired():
    paths = {route.path for route in miniapp.router.routes}
    assert "/v1/miniapp/channel-assistant" in paths
    assert "/v1/miniapp/channel-assistant/{binding_id}" in paths
    assert "/v1/miniapp/channel-activity" in paths
    assert "/v1/miniapp/content/generate" in paths
    assert "/v1/miniapp/content/drafts" in paths
    assert "/v1/miniapp/search/groups" in paths
    assert "/v1/miniapp/search/channels" in paths
    assert not any(
        marker in path
        for path in paths
        for marker in ("/parser", "/campaigns", "/schedules", "/reactions", "/module-policies")
    )
