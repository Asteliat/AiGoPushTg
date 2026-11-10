from app.services.challenge_targets import (
    BotVerificationTarget,
    extract_bot_target,
    is_bot_username,
    looks_like_verified_reply,
)


def test_extracts_bot_and_payload_from_button_url() -> None:
    target = extract_bot_target(
        text="Перейдите в бота и нажмите Start",
        button_urls=["https://t.me/JoinCaptchaBot?start=ce2f1a"],
    )
    assert target == BotVerificationTarget(username="JoinCaptchaBot", payload="ce2f1a")
    assert target.start_command == "/start ce2f1a"
    assert target.mention == "@JoinCaptchaBot"


def test_button_url_wins_over_text_mention() -> None:
    target = extract_bot_target(
        text="Напишите @SomeOtherBot",
        button_urls=["t.me/RealCheckBot?start=abc"],
    )
    assert target is not None
    assert target.username == "RealCheckBot"
    assert target.payload == "abc"


def test_extracts_tg_resolve_link() -> None:
    target = extract_bot_target(text="tg://resolve?domain=LolsBot&start=42")
    assert target == BotVerificationTarget(username="LolsBot", payload="42")


def test_extracts_plain_mention_of_bot() -> None:
    target = extract_bot_target(text="Перейдите в @JoinCaptchaBot и нажмите Start")
    assert target == BotVerificationTarget(username="JoinCaptchaBot", payload=None)
    assert target.start_command == "/start"


def test_ignores_invite_links_and_people() -> None:
    assert extract_bot_target(text="https://t.me/+abcdef") is None
    assert extract_bot_target(text="Спросите у @ivan_petrov") is None
    assert extract_bot_target(text=None) is None
    assert extract_bot_target(text="") is None


def test_ignores_bot_mention_inside_another_link() -> None:
    assert extract_bot_target(text="https://t.me/somebot/123") is not None  # channel-ish but ends with bot
    assert extract_bot_target(text="https://example.com/@notabotperson") is None


def test_bot_username_requires_bot_suffix() -> None:
    assert is_bot_username("JoinCaptchaBot")
    assert is_bot_username("@lolsbot")
    assert not is_bot_username("lols")
    assert not is_bot_username(None)
    assert not is_bot_username("")


def test_verified_reply_phrases() -> None:
    assert looks_like_verified_reply("Вы подтверждены, добро пожаловать!")
    assert looks_like_verified_reply("Проверка пройдена")
    assert looks_like_verified_reply("You are verified ✅")
    assert not looks_like_verified_reply("Выберите смайлик, чтобы продолжить")
    assert not looks_like_verified_reply("")
    assert not looks_like_verified_reply(None)
