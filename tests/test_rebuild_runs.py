"""R1.12: a level's spans are runs of its own episodes, broken around another level's."""

from lcars import rebuild


def test_contiguous_numbers_are_one_span():
    assert rebuild._runs([1, 2, 3], [1, 2, 3, 4]) == [(1, 3)]


def test_a_film_inside_splits_the_season_in_two_spans():
    # the rulebook's example: S2 = 13–16 and 18–24, the film is 17
    season = [*range(13, 17), *range(18, 25)]
    every = sorted([*season, 17])
    assert rebuild._runs(season, every) == [(13, 16), (18, 24)]


def test_decimals_inside_break_the_run_too():
    part = [50, 51, 52, 53, 54]
    assert rebuild._runs(part, sorted([*part, 53.5])) == [(50, 53), (54, 54)]


def test_unsorted_input_and_empty():
    assert rebuild._runs([3, 1, 2], [1, 2, 3]) == [(1, 3)]
    assert rebuild._runs([], [1, 2]) == []
