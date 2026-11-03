from types import SimpleNamespace

import pytest

from app.services.learning_tasks import (
    LessonFormatError,
    _button_target,
    _math_value,
    parse_lesson,
)


def test_safe_math_parser():
    assert _math_value("7 * (8 + 2)") == 70
    assert _math_value("9 / 2") == 4.5


@pytest.mark.parametrize("expression", ["__import__('os').system('id')", "2 ** 99", "1 / 0", "x + 2"])
def test_rejects_unsafe_math(expression):
    with pytest.raises(LessonFormatError):
        _math_value(expression)


def test_ignores_messages_without_explicit_marker():
    assert parse_lesson(SimpleNamespace(message="Please click to verify you are human", entities=[])) is None


def test_parses_math_task():
    message = SimpleNamespace(
        message='ATCIV-LESSON/1\n{"id":"lesson-1","type":"math","question":"12 * 7"}',
        entities=[],
    )
    assert parse_lesson(message)["answer"] == "84"


def test_parses_explicit_inline_button_instruction():
    message = SimpleNamespace(
        message='ATCIV-LESSON/1\n{"id":"button-1","type":"button","question":"Нажми кнопку: Я не бот"}',
        entities=[],
    )
    task = parse_lesson(message)
    assert task["type"] == "button"
    assert task["answer"] == "Я не бот"


@pytest.mark.parametrize("question", ["Выбери правильную", "Нажми кнопку:", "Нажми кнопку: "])
def test_rejects_ambiguous_button_instruction(question):
    with pytest.raises(LessonFormatError):
        _button_target(question)


def test_rejects_unknown_task_type():
    message = SimpleNamespace(
        message='ATCIV-LESSON/1\n{"id":"lesson-1","type":"captcha","question":"choose"}',
        entities=[],
    )
    with pytest.raises(LessonFormatError):
        parse_lesson(message)
