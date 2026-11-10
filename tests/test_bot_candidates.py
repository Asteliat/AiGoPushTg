import pytest

from app.bot.candidates import candidate_at


def test_candidate_lookup_returns_the_item_by_index() -> None:
    candidates = [{"link": "a"}, {"link": "b"}]

    assert candidate_at(candidates, 0) == {"link": "a"}
    assert candidate_at(candidates, 1) == {"link": "b"}


@pytest.mark.parametrize("index", [-1, 2, 100])
def test_candidate_lookup_reports_out_of_range_indices(index: int) -> None:
    assert candidate_at([{"link": "a"}, {"link": "b"}], index) is None


def test_candidate_lookup_handles_a_forgotten_list() -> None:
    assert candidate_at([], 0) is None
