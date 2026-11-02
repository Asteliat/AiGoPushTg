from app.services.ai_responses import passes_reply_probability


def test_zero_probability_never_passes():
    assert not passes_reply_probability(0, 1)
    assert not passes_reply_probability(0, 100)


def test_one_percent_passes_only_on_first_roll():
    assert passes_reply_probability(1, 1)
    assert not passes_reply_probability(1, 2)
    assert not passes_reply_probability(1, 100)


def test_two_percent_passes_only_on_first_two_rolls():
    assert passes_reply_probability(2, 1)
    assert passes_reply_probability(2, 2)
    assert not passes_reply_probability(2, 3)


def test_one_hundred_percent_always_passes():
    for roll in (1, 2, 50, 99, 100):
        assert passes_reply_probability(100, roll)


def test_probability_is_clamped_to_percentage_range():
    assert not passes_reply_probability(-5, 1)
    assert passes_reply_probability(150, 100)
