from app.bot.ui import log_ids_from_value


def test_log_chat_ids_parse_multiple_positive_and_negative_ids() -> None:
    assert log_ids_from_value("123, -1001234567890; 123, invalid, 0") == [123, -1001234567890]


def test_log_chat_ids_empty_value_returns_empty_list() -> None:
    assert log_ids_from_value("") == []
