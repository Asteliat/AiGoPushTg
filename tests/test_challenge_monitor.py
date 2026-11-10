from types import SimpleNamespace

import pytest

from app.services.challenge_monitor import (
    ChallengeMode,
    extract_button_labels,
    find_verification_emoji,
    format_failed_log,
    format_solved_log,
    is_challenge_prompt,
    is_directed_to_account,
    parse_challenge_mode,
    select_verification_index,
    solve_verification,
)
from app.workers.challenge_watcher import AccountIdentity


# ----------------------------------------------------------------- detection


def test_detects_russian_button_verification() -> None:
    assert is_challenge_prompt("Rita, а ты точно не бот? Нажми на кнопку!")


def test_detects_emoji_verification() -> None:
    assert is_challenge_prompt("Выберите смайлик, чтобы подтвердить, что вы человек")


def test_detects_english_verification() -> None:
    assert is_challenge_prompt("Please click the button to verify you are human")


def test_detects_prompt_from_button_captions_only() -> None:
    assert is_challenge_prompt("", ["Я не бот", "Правила"])


def test_ignores_unrelated_bot_message() -> None:
    assert not is_challenge_prompt("Добро пожаловать в группу! Правила закреплены.")


def test_reads_button_labels_without_clicking() -> None:
    class Button:
        def __init__(self, text):
            self.text = text

    class Message:
        buttons = [[Button("Я не бот"), Button("Правила")]]

    assert extract_button_labels(Message()) == ["Я не бот", "Правила"]


# ----------------------------------------------------------------- targeting


def _directed(**overrides) -> bool:
    arguments = {
        "prompt_text": "",
        "reply_to_sender_id": None,
        "account_user_id": 4242,
        "account_username": "rita",
        "account_names": ("Рита Петрова", "Рита", "Петрова"),
        "mentioned": False,
    }
    arguments.update(overrides)
    return is_directed_to_account(**arguments)


def test_prompt_replies_to_our_message() -> None:
    assert _directed(prompt_text="Нажми на кнопку «Я не бот».", reply_to_sender_id=4242)


def test_prompt_mentions_us_by_username() -> None:
    assert _directed(prompt_text="@rita, подтвердите, что вы человек")


def test_prompt_mentions_us_by_name() -> None:
    assert _directed(prompt_text="рита, а ты точно не бот? Нажми на кнопку!")


def test_name_match_is_case_insensitive_in_cyrillic() -> None:
    assert _directed(
        prompt_text="РИТА, подтвердите, что вы человек",
        account_names=("Рита Петрова",),
        account_username=None,
    )


def test_prompt_marks_us_as_entity_mention() -> None:
    assert _directed(prompt_text="Подтвердите вход", mentioned=True)


def test_foreign_prompt_is_not_ours() -> None:
    assert not _directed(
        prompt_text="Иван, подтвердите, что вы человек",
        reply_to_sender_id=999,
    )


def test_empty_prompt_without_signals_is_not_ours() -> None:
    assert not _directed(prompt_text=None)


def test_short_names_never_match_random_words() -> None:
    assert not _directed(
        prompt_text="Нажми на кнопку «Я не бот»",
        account_names=("Ян",),
        account_username=None,
    )


# ----------------------------------------------------------------- solving


def test_single_button_is_pressed() -> None:
    assert select_verification_index(["Я не бот"], "Нажмите на кнопку ниже") == 0


def test_confirmation_button_wins_among_extra_buttons() -> None:
    labels = ["Правила", "✅ Подтвердить", "Пожаловаться"]
    assert select_verification_index(labels, "Подтвердите, что вы человек") == 1


def test_destructive_buttons_are_never_pressed() -> None:
    assert select_verification_index(["Отмена", "Жалоба"], "Нажмите на кнопку") is None


def test_emoji_from_prompt_selects_matching_button() -> None:
    labels = ["🐱", "🐶", "🐸"]
    assert select_verification_index(labels, "Выберите смайлик 🐶") == 1


def test_emoji_keyboard_without_hint_presses_first_candidate() -> None:
    assert select_verification_index(["🐱", "🐶"], "Выберите смайлик") == 0


def test_number_from_prompt_selects_matching_button() -> None:
    labels = ["3", "5", "7"]
    assert select_verification_index(labels, "Сколько будет 2+3? Нажмите ответ") == 1


def test_ambiguous_prompt_is_not_guessed() -> None:
    assert select_verification_index(["Москва", "Казань"], "Выберите правильный ответ") is None


def test_solve_verification_maps_rows_and_columns() -> None:
    rows = [["🐱", "🐶"], ["🐸"]]
    assert solve_verification(rows, "Выберите смайлик 🐸") == (1, 0)


def test_find_verification_emoji_skips_variation_selector() -> None:
    assert find_verification_emoji("Выберите ✅ или ❌") == "✅"
    assert find_verification_emoji("Нажмите на кнопку") is None


# ----------------------------------------------------------------- settings


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("auto", ChallengeMode.AUTO),
        ("notify", ChallengeMode.NOTIFY),
        ("off", ChallengeMode.OFF),
        ("true", ChallengeMode.AUTO),
        ("false", ChallengeMode.OFF),
        ("  ON ", ChallengeMode.AUTO),
        ("unexpected", ChallengeMode.AUTO),
        (None, ChallengeMode.AUTO),
    ],
)
def test_parse_challenge_mode(stored: str | None, expected: ChallengeMode) -> None:
    assert parse_challenge_mode(stored) is expected


# ----------------------------------------------------------------- identity


def test_account_identity_collects_name_parts() -> None:
    account = SimpleNamespace(
        id=7,
        display_name="Rita Petrova",
        telegram_user_id=4242,
        username="rita",
    )
    identity = AccountIdentity.from_account(account)
    assert identity.user_id == 4242
    assert identity.names == ("Rita Petrova", "Rita", "Petrova")


def test_account_identity_without_username_or_name() -> None:
    account = SimpleNamespace(
        id=8,
        display_name="",
        telegram_user_id=100,
        username=None,
    )
    identity = AccountIdentity.from_account(account)
    assert identity.name == "Аккаунт #8"
    assert identity.names == ()
    assert identity.username is None


# ----------------------------------------------------------------- logging


def test_solved_log_names_account_group_bot_and_button() -> None:
    log = format_solved_log(
        account_id=7,
        account_name="Rita",
        chat_title="My group",
        sender_name="LolsBot",
        button_text="Я не бот",
    )
    assert "Проверка Telegram пройдена" in log
    assert "Rita · #7" in log
    assert "Я не бот" in log


def test_failed_log_reports_reason_and_buttons() -> None:
    log = format_failed_log(
        account_id=7,
        account_name="Rita",
        chat_title="My group",
        sender_name="LolsBot",
        reason="не удалось выбрать кнопку",
        button_labels=["1", "2"],
    )
    assert "Проверка Telegram не пройдена" in log
    assert "не удалось выбрать кнопку" in log
    assert "1, 2" in log
