"""Small shared timestamp helpers — util.py.

add_days() is new (A.10, §6.2's adaptive next-date formula); the
DateTime scalar and now_utc_iso()/utc_iso_offset() are already
exercised indirectly throughout test_server.py, so only add_days()
(and, added B.1, start_of_today_utc()) get dedicated direct coverage
here.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from lcars import util


def test_add_days_advances_by_the_given_number_of_days():
    assert util.add_days("2026-01-01T00:00:00Z", 7) == "2026-01-08T00:00:00Z"


def test_add_days_crosses_a_month_boundary():
    assert util.add_days("2026-01-30T12:00:00Z", 3) == "2026-02-02T12:00:00Z"


def test_add_days_accepts_negative_days():
    assert util.add_days("2026-01-08T00:00:00Z", -7) == "2026-01-01T00:00:00Z"


def test_add_days_with_zero_is_a_no_op():
    assert util.add_days("2026-01-01T00:00:00Z", 0) == "2026-01-01T00:00:00Z"


def test_start_of_today_utc_is_local_midnight_in_the_given_timezone():
    # B.1, §6.7/§6.13 — home_timezone's first real consumer: the returned
    # UTC instant, converted back into that same timezone, must land on
    # exactly 00:00:00 — not a UTC-midnight boundary bucketed by mistake.
    tz = "Pacific/Auckland"  # UTC+12/+13 — far enough from UTC that a bug
    # bucketing by UTC midnight instead of local midnight would be caught
    # regardless of what time this test happens to run.
    result = util.start_of_today_utc(tz)
    local = datetime.fromisoformat(result.replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
    assert (local.hour, local.minute, local.second) == (0, 0, 0)


def test_start_of_today_utc_is_within_the_last_24_hours():
    result = util.start_of_today_utc("Europe/Dublin")
    now = datetime.now(UTC)
    parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    assert parsed <= now
    assert (now - parsed).total_seconds() < 24 * 3600
