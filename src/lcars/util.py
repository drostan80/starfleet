"""Small shared helpers — timestamps and the `DateTime` GraphQL scalar.

All stored timestamps are TEXT, ISO-8601 UTC, "Z"-suffixed (SCOPE.md
§5's own convention, e.g. "2026-08-08T12:34:56Z") — every table's
created_at/updated_at/watched_at/etc. columns already hold exactly this
format, so the DateTime scalar below is close to a passthrough: it just
validates rather than reformats.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ariadne import ScalarType
from graphql import GraphQLError


def now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_iso_offset(days: int) -> str:
    """now_utc_iso(), `days` days ahead (or behind, if negative) — same
    "Z"-suffixed format, so it's directly comparable to any stored
    timestamp via plain string comparison (all of them share this exact
    format, so lexicographic order is chronological order)."""
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_iso_offset_hours(hours: float) -> str:
    """Same as utc_iso_offset() above, but at hour granularity — B.3's
    2-hour "just aired" window (§6.7) doesn't fit cleanly as a fraction
    of a day the way every other consumer of utc_iso_offset() does."""
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def unix_to_iso(timestamp: int) -> str:
    """A Unix epoch-seconds timestamp (AniList's own `airingAt` shape,
    §5.2/§6.7, B.4) to the same Z-suffixed UTC string every stored
    timestamp uses — verified live against AniList's real API before
    being written: `airingSchedule.nodes.airingAt` is seconds, not
    milliseconds."""
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    try:
        # datetime.fromisoformat handles the "Z" suffix natively (3.12+,
        # matches pyproject.toml's requires-python floor).
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise GraphQLError(f"not a valid ISO-8601 DateTime: {value!r}") from exc


def start_of_today_utc(home_timezone: str) -> str:
    """The start of "today" in `home_timezone` (§6.13), expressed as the
    same Z-suffixed UTC string every stored timestamp uses — so it's
    directly comparable via plain string comparison, same as every other
    consumer of this format (pagination.py's cursors, utc_iso_offset()).

    §6.13's home_timezone was built (A.16) with no consumer at all;
    B.1's daily-metadata-refresh ceiling is its first real one — "not
    stacking" buckets by the user's own day boundary, not a UTC one, per
    §6.7's own framing.
    """
    now_local = datetime.now(ZoneInfo(home_timezone))
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_days(iso_timestamp: str, days: int) -> str:
    """`iso_timestamp` (any stored Z-suffixed value) advanced by `days`
    — paced/catch-up mode's own adaptive next-date formula (§6.2, A.10):
    latest watch_event.watched_at + cadence interval."""
    dt = _parse(iso_timestamp)
    return (dt + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


datetime_scalar = ScalarType("DateTime")


@datetime_scalar.serializer
def serialize_datetime(value: str) -> str:
    # Already an ISO-8601 string straight from the DB — validate, don't
    # reformat, so a raw passthrough round-trips exactly.
    _parse(value)
    return value


@datetime_scalar.value_parser
def parse_datetime_value(value: str) -> str:
    _parse(value)
    return value
