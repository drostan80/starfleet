"""Stage 8 part: the AniList history (GDPR export) — times converted from Japan time, ranges
expanded, the mess and today's confirmations left out."""

from lcars import rebuild_history as rh


def test_japan_time_is_converted_to_utc():
    assert rh.utc("2026-08-02 02:24:45") == "2026-08-01T17:24:45Z"


def test_an_episode_range_is_every_episode_in_it():
    assert rh.episodes_of("3 - 8") == [3, 4, 5, 6, 7, 8]
    assert rh.episodes_of("12") == [12]
    assert rh.episodes_of("") == []


def test_a_list_date_is_read_and_an_empty_one_is_none():
    assert rh.day(20230513) == "2023-05-13T00:00:00Z"
    assert rh.day(20140700) == "2014-07-01T00:00:00Z"
    assert rh.day(0) is None and rh.day(20140000) is None
