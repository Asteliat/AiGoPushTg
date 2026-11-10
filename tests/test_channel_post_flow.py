"""Golden-path test for ChannelMonitor.process_post.

process_post is the most intricate function of the worker: it claims a post, resolves
the linked discussion, decides whether the post must wait for comments, asks the AI,
publishes the comment and records the outcome. This test drives that whole path with
in-memory fakes so the behaviour is locked before any further refactoring.
"""
from types import SimpleNamespace

import pytest

from app.storage.models import AIAgent, AppSetting, ChannelAnalysis, ChannelBinding, ChannelPostState
from app.workers import channel_monitor as monitor_module
from app.workers.channel_monitor import ChannelMonitor
from tests.fakes import FakeRedis, FakeSessionFactory, FakeTelethonClient, make_account

GROUP_ID = 900
GROUP_PEER_ID = -100900
ROOT_MESSAGE_ID = 77


class Harness:
    def __init__(self, *, publication_status: str = "approved", reply_mode: str = "automatic"):
        self.account = make_account(
            7,
            ai_enabled=True,
            ai_agent_id=2,
            channel_reply_mode=reply_mode,
            channel_reply_probability=100,
            channel_reply_cooldown_seconds=0,
            channel_chat_replies_enabled=True,
        )
        self.binding = ChannelBinding(
            id=11,
            account_id=7,
            channel_id=500,
            title="Тестовый канал",
            enabled=True,
            reply_enabled=True,
            reply_probability_override=None,
            reply_cooldown_seconds_override=None,
        )
        self.agent = AIAgent(
            id=2,
            name="GPT",
            enabled=True,
            api_key="key",
            channel_reply_probability_override=None,
            channel_reply_cooldown_seconds_override=None,
        )
        self.analysis = ChannelAnalysis(
            id=33,
            account_id=7,
            channel_id=500,
            post_id=42,
            channel_title="Тестовый канал",
            publication_status=publication_status,
            published_comment_id=None,
            discussion_group_id=None,
            publication_error="",
        )
        self.factory = FakeSessionFactory(
            {
                ("Account", 7): self.account,
                ("AIAgent", 2): self.agent,
                ("AppSetting", "ai_global_enabled"): AppSetting(
                    key="ai_global_enabled", value="true"
                ),
            }
        )
        self.state = ChannelPostState(
            account_id=7,
            channel_id=500,
            post_id=42,
            channel_title="Тестовый канал",
            post_text="Текст публикации",
            status="processing",
            attempts=1,
            last_error="",
            next_retry_at=None,
        )
        # Answers are routed by model, in call order.
        self.factory.scalars_by_entity = {
            "ChannelBinding": [self.binding, self.binding, self.binding],
            "ChannelPostState": [None, self.state, self.state, "done"],
            "ChannelAnalysis": [self.analysis, self.analysis],
        }
        self.redis = FakeRedis()
        self.client = FakeTelethonClient()
        self.group = SimpleNamespace(id=GROUP_ID, megagroup=True, broadcast=False)
        self.root = SimpleNamespace(
            id=ROOT_MESSAGE_ID,
            peer_id=SimpleNamespace(channel_id=GROUP_ID),
            replies=SimpleNamespace(replies=3),
        )
        self.client._handle_request = self._discussion_request
        self.client.comments = [
            SimpleNamespace(message="Первый комментарий", out=False, sender_id=101),
            SimpleNamespace(message="Второй комментарий", out=False, sender_id=102),
            SimpleNamespace(message="Третий комментарий", out=False, sender_id=103),
        ]
        self.logs: list[tuple[str, str]] = []
        self.sent_messages: list[tuple[object, str, int | None]] = []
        self.monitor = ChannelMonitor(
            self.client,
            account_id=7,
            redis=self.redis,
            send_log=self._send_log,
            route_chat_reply=self._route_reply,
        )

    async def _send_log(self, text: str, category: str = "main") -> bool:
        self.logs.append((category, text))
        return True

    async def _route_reply(self, *_args, **_kwargs) -> None:
        return None

    async def _discussion_request(self, request):
        return SimpleNamespace(chats=[self.group], messages=[self.root])

    def patch_world(self, monkeypatch, *, analysis_result):
        monkeypatch.setattr(monitor_module, "SessionFactory", self.factory)
        monkeypatch.setattr(monitor_module.utils, "get_peer_id", lambda _group: GROUP_PEER_ID)
        monkeypatch.setattr(
            monitor_module, "analyze_channel_post", lambda *a, **k: _return(analysis_result)
        )

        async def send_message(entity, text, reply_to=None):
            self.sent_messages.append((entity, text, reply_to))
            return SimpleNamespace(id=555)

        self.client.send_message = send_message  # type: ignore[method-assign]

        async def get_messages(_entity, *, ids=None, limit=None, reply_to=None):
            return None

        self.client.get_messages = get_messages  # type: ignore[method-assign]


async def _return(value):
    return value


def analysis_result(**overrides):
    values = {
        "channel_title": "Тестовый канал",
        "on_topic": True,
        "comments_count": 3,
        "comments_summary": "Люди обсуждают тему поста.",
        "suggested_response": "Спасибо за разбор, присоединяюсь.",
        "should_reply": True,
        "publication_status": "approved",
        "discussion_group_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_post_is_analyzed_published_and_recorded(monkeypatch) -> None:
    harness = Harness()
    result = analysis_result()
    harness.patch_world(monkeypatch, analysis_result=result)

    await harness.monitor.process_post(
        SimpleNamespace(id=500),
        channel_id=500,
        post_id=42,
        post_text="Текст публикации",
        channel_title="Тестовый канал",
        post_message=SimpleNamespace(message="Текст публикации", photo=None),
    )

    # The comment was published as a reply to the discussion root message.
    assert len(harness.sent_messages) == 1
    entity, text, reply_to = harness.sent_messages[0]
    assert entity is harness.group
    assert text == result.suggested_response
    assert reply_to == ROOT_MESSAGE_ID

    # The analysis row got the comment id and the final status.
    assert harness.analysis.published_comment_id == 555
    assert harness.analysis.publication_status == "published"

    # The operator notification was queued and mentions the publication.
    assert harness.logs and harness.logs[-1][0] == "channels"
    assert "Тестовый канал" in harness.logs[-1][1]

    # The processing lock is released and the in-memory guard is cleared.
    assert harness.monitor.active_posts == set()


@pytest.mark.asyncio
async def test_manual_mode_prepares_a_draft_without_publishing(monkeypatch) -> None:
    harness = Harness(reply_mode="manual")
    result = analysis_result(publication_status="approved")
    harness.patch_world(monkeypatch, analysis_result=result)

    await harness.monitor.process_post(
        SimpleNamespace(id=500),
        channel_id=500,
        post_id=42,
        post_text="Текст публикации",
        channel_title="Тестовый канал",
        post_message=SimpleNamespace(message="Текст публикации", photo=None),
    )

    assert harness.sent_messages == []
    # analyze_channel_post persists its own row in production; here the result is
    # the object the pipeline downgrades from "approved" to "manual".
    assert result.publication_status == "manual"
    assert harness.logs and "черновик" in harness.logs[-1][1].lower()


@pytest.mark.asyncio
async def test_paused_account_stops_before_any_work(monkeypatch) -> None:
    harness = Harness()
    harness.account.manual_pause = True
    harness.patch_world(monkeypatch, analysis_result=analysis_result())

    await harness.monitor.process_post(
        SimpleNamespace(id=500),
        channel_id=500,
        post_id=42,
        post_text="Текст публикации",
        channel_title="Тестовый канал",
        post_message=SimpleNamespace(message="Текст публикации", photo=None),
    )

    assert harness.sent_messages == []
    assert harness.logs == []
    assert harness.monitor.active_posts == set()
