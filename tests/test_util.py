"""Small shared timestamp helpers — util.py.

add_days() is new (A.10, §6.2's adaptive next-date formula); the
DateTime scalar and now_utc_iso()/utc_iso_offset() are already
exercised indirectly throughout test_server.py, so only add_days()
gets dedicated direct coverage here.
"""

from lcars import util


def test_add_days_advances_by_the_given_number_of_days():
    assert util.add_days("2026-01-01T00:00:00Z", 7) == "2026-01-08T00:00:00Z"


def test_add_days_crosses_a_month_boundary():
    assert util.add_days("2026-01-30T12:00:00Z", 3) == "2026-02-02T12:00:00Z"


def test_add_days_accepts_negative_days():
    assert util.add_days("2026-01-08T00:00:00Z", -7) == "2026-01-01T00:00:00Z"


def test_add_days_with_zero_is_a_no_op():
    assert util.add_days("2026-01-01T00:00:00Z", 0) == "2026-01-01T00:00:00Z"
