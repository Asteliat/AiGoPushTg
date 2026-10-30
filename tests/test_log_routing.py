from app.bot.ui import log_destinations_from_value, log_ids_from_value


def test_log_chat_ids_parse_multiple_positive_and_negative_ids() -> None:
    assert log_ids_from_value("123, -1001234567890; 123, invalid, 0") == [123, -1001234567890]


def test_legacy_chat_ids_are_destinations_without_topics() -> None:
    assert log_destinations_from_value("123,-1001234567890") == [
        (123, None),
        (-1001234567890, None),
    ]


def test_forum_topic_destinations_parse_and_deduplicate() -> None:
    assert log_destinations_from_value("-1004351902275:108, -1004351902275:109; -1004351902275:108") == [
        (-1004351902275, 108),
        (-1004351902275, 109),
    ]


def test_invalid_topic_destinations_are_ignored() -> None:
    assert log_destinations_from_value("0:12, -100123:0, -100123:-1, abc:12, 123:2:3") == []


def test_log_chat_ids_empty_value_returns_empty_list() -> None:
    assert log_ids_from_value("") == []
