"""Small shared helpers — timestamps and the `DateTime` GraphQL scalar.

All stored timestamps are TEXT, ISO-8601 UTC, "Z"-suffixed (SCOPE.md
§5's own convention, e.g. "2026-08-08T12:34:56Z") — every table's
created_at/updated_at/watched_at/etc. columns already hold exactly this
format, so the DateTime scalar below is close to a passthrough: it just
validates rather than reformats.
"""

from datetime import UTC, datetime, timedelta

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


def _parse(value: str) -> datetime:
    try:
        # datetime.fromisoformat handles the "Z" suffix natively (3.12+,
        # matches pyproject.toml's requires-python floor).
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise GraphQLError(f"not a valid ISO-8601 DateTime: {value!r}") from exc


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
