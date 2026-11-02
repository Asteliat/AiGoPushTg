import pytest
from pydantic import ValidationError

from app.api import miniapp
from app.api.miniapp import (ChannelAssistantPatch, ContentGenerateInput, ContentDraftPatch, InboxReplyInput, ModuleSettingsInput)
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
    assert "/v1/miniapp/inbox/{account_id}/dialogs" in paths
    assert "/v1/miniapp/inbox/{account_id}/messages/{peer_id}" in paths
    assert "/v1/miniapp/inbox/draft" in paths
    assert "/v1/miniapp/inbox/reply" in paths
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


def test_inbox_reply_requires_an_existing_message_reference():
    with pytest.raises(ValidationError):
        InboxReplyInput(account_id=1, peer_id=2, text="Hello")
    payload = InboxReplyInput(
        account_id=1, peer_id=2, peer_ref="@example",
        reply_to_message_id=10, text="Hello",
    )
    assert payload.reply_to_message_id == 10


def test_inbox_context_uses_separate_lines_for_messages():
    from types import SimpleNamespace

    context = miniapp._format_inbox_context([
        SimpleNamespace(out=False, message="Привет"),
        SimpleNamespace(out=True, message="Здравствуйте"),
        SimpleNamespace(out=False, message=None),
    ])

    assert context.splitlines() == [
        "Собеседник: Привет",
        "Аккаунт: Здравствуйте",
        "Собеседник: [медиа/вложение]",
    ]
    assert "\\n" not in context


def test_ai_agent_snapshot_is_detached_from_orm_model():
    from types import SimpleNamespace

    source = SimpleNamespace(
        enabled=True,
        api_key="secret",
        base_url="https://api.example/v1",
        model="example-model",
        timeout_seconds=45,
    )
    snapshot = miniapp._snapshot_agent(source)

    source.api_key = "changed"
    source.model = "changed-model"

    assert snapshot.api_key == "secret"
    assert snapshot.model == "example-model"
    assert snapshot.timeout_seconds == 45


def test_ai_conversation_defaults_live_inside_their_module():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "app" / "api" / "static" / "index.html").read_text(encoding="utf-8")
    conversations = html.split("function renderConversationsModule(){", 1)[1].split(
        "async function loadInboxDialogs(){", 1
    )[0]
    settings = html.split("function renderSettings(){", 1)[1].split(
        "function logCard(", 1
    )[0]

    assert 'id="global-ai"' in conversations
    assert 'id="save-settings"' in conversations
    assert "mention_reply_probability" in conversations
    assert 'id="global-ai"' not in settings
    assert 'id="save-settings"' not in settings


def test_inbox_frontend_ignores_stale_dialog_and_message_requests():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "app" / "api" / "static" / "index.html").read_text(encoding="utf-8")
    assert "inboxDialogsRequest" in html
    assert "inboxMessagesRequest" in html
    assert "aid!==state.inboxAccountId" in html
    assert "state.inboxPeer?.peer_id!==peer.peer_id" in html



def test_module_catalog_contains_the_requested_workspace_modules():
    ids = {item["id"] for item in miniapp.MODULE_CATALOG}
    assert len(ids) == 15
    assert {
        "neuro_commenting", "neuro_shilling", "neuro_chatting", "neuro_dialogs",
        "dm_mailings", "chat_mailings", "mass_tagging", "mass_reactions",
        "mass_viewing", "auto_warmup", "account_manager", "account_protection",
        "user_parser", "channel_parser", "inviter",
    } == ids


def test_module_settings_are_bounded_and_keep_isolated_account_selection():
    payload = ModuleSettingsInput(
        enabled=False, account_ids=[1, 2], delay_min_seconds=10,
        delay_max_seconds=30, probability=75, daily_limit=50,
        keywords=["billing", "support"],
    )
    assert payload.account_ids == [1, 2]
    assert payload.probability == 75
    with pytest.raises(ValidationError):
        ModuleSettingsInput(delay_min_seconds=86401)
    with pytest.raises(ValidationError):
        ModuleSettingsInput(probability=101)
    with pytest.raises(ValidationError):
        ModuleSettingsInput(daily_limit=-1)


def test_module_settings_defaults_are_not_enabled_implicitly():
    assert miniapp._module_config(None)["enabled"] is False
    assert miniapp._module_config("{broken")["account_ids"] == []


def test_modules_and_automation_are_separate_routes():
    paths = {route.path for route in miniapp.router.routes}
    assert "/v1/miniapp/modules" in paths
    assert "/v1/miniapp/modules/{module_id}" in paths
    assert "/v1/miniapp/automation/tasks" in paths
    assert "/v1/miniapp/automation/tasks/{task_id}" in paths


def test_module_catalog_does_not_enable_restricted_bulk_behaviour():
    states = {item["id"]: item["state"] for item in miniapp.MODULE_CATALOG}
    assert states["neuro_shilling"] == "restricted"
    assert states["mass_viewing"] == "restricted"
    assert states["user_parser"] == "restricted"
    assert states["inviter"] == "restricted"


def test_miniapp_has_distinct_module_and_automation_navigation():
    from pathlib import Path

    html = (Path(__file__).parents[1] / "app" / "api" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-page="modules"' in html
    assert 'data-page="automation"' in html
    assert "Модули автоматизации" not in html
    assert "function renderAutomation()" in html
