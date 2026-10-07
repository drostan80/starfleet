"""Air-date precision and time zones (RULEBOOK R1.0e, user 2026-10-07).

An air date is either a **time** (an instant, stored UTC: AniList, Syoboi, animeschedule, TVmaze
with a real air time, Sonarr when TVDB gives the series an air time) or a **date** (a calendar day
in the broadcaster's own time zone, no instant: AniDB, TVDB's own episode list, TVmaze when it has
no air time — its `airstamp` is then a noon placeholder —, Sonarr when the series has no air time,
and an episode TVmaze says has no air time, i.e. a streaming release that drops all at once).

A date-only episode keeps `air_date_utc` = that date at 00:00Z (what it always held), plus
`air_precision = 'date'`, `air_local_date` and `air_aired_at`: the **end of its local day**, the
moment it counts as aired. The zone is Japan (JST, +9, no daylight saving) for anime — AniDB, TVDB
and Sonarr date an anime by its Japanese day — and the latest US zone (UTC-8) for everything else,
so a date-only episode is never called aired too early. Every "has it aired" comparison reads
`AIRED_AT` (`aired_at_sql`); every "which day" comparison for a date reads `air_local_date` — never
a slice of the UTC string, which for a 25:00 JST broadcast is the day before its Japanese date.
"""

from __future__ import annotations

import datetime as dt

TIME = "time"
DATE = "date"

JST = dt.timezone(dt.timedelta(hours=9))

# A time and a date within this many days of each other are the same broadcast: a timed source
# replaces a date-only one inside it (a precision upgrade), not outside it (a different slot).
UPGRADE_WINDOW_DAYS = 2

# A source's own change of at least this many hours is shown on the calendar / show page.
CHANGE_ICON_HOURS = 2


def aired_at_sql(alias: str = "episode") -> str:
    """The SQL expression for the moment an episode counts as aired: the end of its local day for
    a date-only episode, its air time otherwise."""
    prefix = f"{alias}." if alias else ""
    return f"COALESCE({prefix}air_aired_at, {prefix}air_date_utc)"


def date_instant(local_date: str) -> str:
    """What `air_date_utc` holds for a date-only value: that day at 00:00Z."""
    return f"{local_date}T00:00:00Z"


def aired_at(local_date: str, anime: bool) -> str:
    """The end of `local_date` in its zone, as UTC: 24:00 JST is 15:00Z on that day; the end of a
    US-Pacific (standard time) day is 08:00Z the next day."""
    day = dt.date.fromisoformat(local_date)
    if anime:
        return f"{day.isoformat()}T15:00:00Z"
    return f"{(day + dt.timedelta(days=1)).isoformat()}T08:00:00Z"


def jst_date(air_date_utc: str) -> dt.date:
    """The calendar date in Japan of an instant (AniList and AniDB count a start/air date so)."""
    moment = dt.datetime.strptime(air_date_utc[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.UTC)
    return moment.astimezone(JST).date()


def day_in_japan(air_date_utc: str | None, precision: str | None, local_date: str | None):
    """The Japanese calendar day an anime episode airs: the stored local date for a date-only
    value, the JST date of the instant for a time. None without a date."""
    if not air_date_utc:
        return None
    if precision == DATE and local_date:
        return dt.date.fromisoformat(local_date)
    return jst_date(air_date_utc)


def parse(air_date_utc: str) -> dt.datetime:
    return dt.datetime.strptime(air_date_utc[:19], "%Y-%m-%dT%H:%M:%S")


def hours_apart(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    return abs((parse(a) - parse(b)).total_seconds()) / 3600


# SQL fragment for the overwriting writers: "this episode's season follows a chosen schedule".
LOCKED_SQL = (
    "EXISTS (SELECT 1 FROM season_air_choice _c WHERE _c.season_id = episode.season_id)"
)


def aired_at_from_date_sql(day: str, show_id: str = "episode.show_id") -> str:
    """SQL twin of `aired_at`: the moment a date-only value (the SQL expression `day`, 'YYYY-MM-DD')
    of an episode of `show_id` counts as aired."""
    return (
        f"CASE WHEN (SELECT tracking_space FROM show WHERE id = {show_id}) = 'anime'"
        f" THEN {day} || 'T15:00:00Z'"
        f" ELSE strftime('%Y-%m-%dT%H:%M:%SZ', datetime({day}, '+1 day', '+8 hours')) END"
    )
