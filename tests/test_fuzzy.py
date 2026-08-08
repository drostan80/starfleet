"""Fuzzy title matching — SCOPE.md §5.4, BUILD_PLAN.md A.7.

Pure-function coverage for lcars/fuzzy.py. The mutation that wires this
into show_service_presence rows (refreshShowServicePresence) is covered
end-to-end in tests/test_server.py, same split as fribb.py/test_fribb.py.
"""

from lcars import fuzzy


def test_normalize_title_lowercases_and_strips_punctuation():
    assert fuzzy.normalize_title("SPY x FAMILY!") == "spy x family"
    assert fuzzy.normalize_title("Blue_Miburo-Season_2") == "blue miburo season 2"
    assert fuzzy.normalize_title("  Golden   Kamuy  ") == "golden kamuy"


def test_best_match_exact_normalized_match_wins_over_everything():
    result = fuzzy.best_match(["Golden Kamuy"], ["Golden Kamuy", "Golden Kamuy 2"])
    assert result == "Golden Kamuy"


def test_best_match_falls_back_to_fuzzy_above_threshold():
    result = fuzzy.best_match(["Golden Kamuy"], ["Golden Kamui"])  # one-letter typo
    assert result == "Golden Kamui"


def test_best_match_returns_none_below_threshold_rather_than_guessing():
    result = fuzzy.best_match(["Golden Kamuy"], ["Attack on Titan"])
    assert result is None


def test_best_match_returns_none_with_no_candidates_or_no_show_titles():
    assert fuzzy.best_match(["Golden Kamuy"], []) is None
    assert fuzzy.best_match([], ["Golden Kamuy"]) is None
    assert fuzzy.best_match([], []) is None


def test_best_match_checks_every_show_title_variant():
    # only the native title variant matches any candidate — romaji/english
    # are both real but different from what's offered
    show_titles = ["Golden Kamuy", "The Golden Kamuy Show", "ゴールデンカムイ"]
    result = fuzzy.best_match(show_titles, ["ゴールデンカムイ"])
    assert result == "ゴールデンカムイ"


def test_best_match_ignores_blank_entries():
    assert fuzzy.best_match(["Golden Kamuy", None, ""], ["Golden Kamuy"]) == "Golden Kamuy"
    assert fuzzy.best_match(["Golden Kamuy"], ["Golden Kamuy", None, ""]) == "Golden Kamuy"


def test_best_match_respects_a_custom_threshold():
    # "Kamuy" vs "Totally Different Title" is a weak ratio — passes only
    # once the threshold is lowered far enough to accept it
    assert fuzzy.best_match(["Kamuy"], ["Totally Different Title"], threshold=0.99) is None
