"""Air-date candidates and the per-season schedule choice (user, 2026-10-04).

Every source that knows when an episode airs leaves a *candidate* (source, channel, date) for it
in `episode_air_candidate`: Sonarr's raw date, TVmaze, AniDB (date only), Syoboi (one schedule per
TV station), and — recorded as they are read — AniList and animeschedule.net. Nothing here
decides a date by itself.

A season with a row in `season_air_choice` follows that source/channel: `apply_choice` writes its
dates, and the automatic overwriting writers (AniList reconcile, animeschedule, Syoboi rewire) leave
the season alone (`season_is_locked` / `LOCKED_SQL`). A season without a row keeps the existing
rule (`airdate_priority.should_apply`: a different source wins only by an earlier date). A `manual`
date (setEpisodeAirDate) outranks a choice, as it outranks everything. Fill-gap writers only fill
empty dates — except in a season that follows a chosen schedule, where `apply_choice` fills an
episode its source has no date for with the earliest candidate until the source has one.

Since 2026-10-07 (user) every source's schedule is **refreshed constantly**
(`refresh_show_schedules`) and each candidate carries its **precision** (a time, or a date only:
`air_time`), the value it first
reported (`first_air_date_utc`) and a log of its later changes (`air_candidate_change`). Selection:
the earliest *timed* candidate (a date-only one only when no timed exists; a timed one replaces a
date-only one within three days), applied when a candidate **changes or appears** — never recomputed
from scratch, so a source that moves later (a real reschedule) is not undone by a stale earlier
one. For a TV show (not anime) Sonarr's date is the pick. A chosen schedule wins over all of it
(a `manual` date over that).
"""

from __future__ import annotations

import logging
from collections import Counter

from lcars import air_time, airdate_priority, ids, status_rules, util

log = logging.getLogger(__name__)

# Values `episode.air_date_source` accepts (its CHECK constraint) — a candidate can only become a
# stored date from one of these.
_STORABLE_SOURCES = frozenset(
    {"sonarr", "anilist", "animeschedule", "tvmaze", "anidb", "syoboi", "tvdb"}
)

SOURCE_LABELS = {
    "anilist": "AniList",
    "animeschedule": "AnimeSchedule",
    "tvmaze": "TVmaze",
    "anidb": "AniDB (date only)",
    "syoboi": "Syoboi (Japanese TV)",
    "sonarr": "Sonarr / TVDB",
}

LOCKED_SQL = air_time.LOCKED_SQL


def season_choice(conn, season_id: str):
    return conn.execute(
        "SELECT * FROM season_air_choice WHERE season_id = ?", (season_id,)
    ).fetchone()


def episode_is_locked(conn, episode_id: str) -> bool:
    """Does this episode's season follow a chosen schedule? (Python-side twin of LOCKED_SQL.)"""
    return conn.execute(
        "SELECT 1 FROM season_air_choice c JOIN episode e ON e.season_id = c.season_id"
        " WHERE e.id = ?",
        (episode_id,),
    ).fetchone() is not None


def _upsert_candidate(
    conn, episode_id: str, source: str, channel: str, air_date_utc: str, precision: str,
    local_date: str | None, now: str,
) -> dict | None:
    """Remember what `source` says; returns an event when the value is new or changed, None when
    the source repeats itself. The first value a source reports is kept (`first_air_date_utc`:
    the baseline of the calendar's change icons); a later change is logged
    (`air_candidate_change`). A candidate that existed before the baseline did (NULL) is seeded
    with its current value, so correcting stale data (Sonarr's dates were never refreshed) is not
    counted as the source changing its mind."""
    old = conn.execute(
        "SELECT air_date_utc, precision, first_air_date_utc FROM episode_air_candidate"
        " WHERE episode_id = ? AND source = ? AND channel = ?",
        (episode_id, source, channel),
    ).fetchone()
    event = {
        "episode_id": episode_id, "source": source, "channel": channel,
        "old": None if old is None else old["air_date_utc"], "new": air_date_utc,
        "precision": precision, "local_date": local_date,
    }
    if old is None:
        conn.execute(
            "INSERT INTO episode_air_candidate (episode_id, source, channel, air_date_utc,"
            " precision, local_date, first_air_date_utc, fetched_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (episode_id, source, channel, air_date_utc, precision, local_date, air_date_utc, now),
        )
        return event
    changed = old["air_date_utc"] != air_date_utc or old["precision"] != precision
    # A source that gains (or loses) an air time — TVmaze's noon placeholder becoming 21:00, a
    # series getting an air time in TVDB — has not moved its schedule: the baseline is re-taken.
    rebaseline = old["precision"] != precision
    conn.execute(
        "UPDATE episode_air_candidate SET air_date_utc = ?, precision = ?, local_date = ?,"
        " first_air_date_utc = CASE WHEN ? THEN ? ELSE COALESCE(first_air_date_utc, ?) END,"
        " fetched_at = ? WHERE episode_id = ? AND source = ? AND channel = ?",
        (air_date_utc, precision, local_date, rebaseline, air_date_utc, air_date_utc, now,
         episode_id, source, channel),
    )
    if not changed:
        return None
    if old["first_air_date_utc"] is not None and not rebaseline:
        conn.execute(
            "INSERT INTO air_candidate_change (id, episode_id, source, channel,"
            " previous_air_date_utc, new_air_date_utc, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ids.generate_id(conn, "g"), episode_id, source, channel, old["air_date_utc"],
             air_date_utc, now),
        )
    return event


def record_candidate(
    conn, episode_id: str, source: str, channel: str, air_date_utc: str | None,
    precision: str = air_time.TIME, local_date: str | None = None,
) -> dict | None:
    """Remember what `source` (and `channel`, '' when it has no such notion) says. Idempotent;
    returns the change event (None when the value is the one already held)."""
    if not air_date_utc:
        return None
    return _upsert_candidate(
        conn, episode_id, source, channel, air_date_utc, precision, local_date, util.now_utc_iso()
    )


# Candidate sources derived from tables LCARS already stores: rebuilt for a show each time.
# (AniList and animeschedule.net are live reads, recorded where they are read, and kept.)
_DERIVED_SOURCES = ("sonarr", "tvmaze", "anidb", "syoboi")

# Each query yields (episode_id, source, channel, air_date_utc, precision, local_date).
_TVMAZE_NO_TIME = (
    "(te.airtime IS NULL OR te.airtime = '' OR te.airstamp IS NULL OR te.airstamp = '')"
)
_TVMAZE_DAY = "COALESCE(NULLIF(te.airdate, ''), substr(te.airstamp, 1, 10))"
_SONARR_DAY = "COALESCE(e.air_raw_sonarr_local_date, substr(e.air_date_raw_sonarr, 1, 10))"

_DERIVED_SQL = {
    # Sonarr's time is real only when TVDB gives the series an air time (air_raw_sonarr_precision)
    "sonarr": f"""
        SELECT e.id, 'sonarr', '',
               CASE WHEN e.air_raw_sonarr_precision = 'date' THEN {_SONARR_DAY} || 'T00:00:00Z'
                    ELSE e.air_date_raw_sonarr END,
               CASE WHEN e.air_raw_sonarr_precision = 'date' THEN 'date' ELSE 'time' END,
               {_SONARR_DAY}
        FROM episode e
        WHERE e.show_id = :show AND e.kind = 'regular'
          AND e.air_date_raw_sonarr IS NOT NULL AND e.air_date_raw_sonarr != ''""",
    # TVmaze with no air time gives a noon placeholder airstamp: a date, not a time
    "tvmaze": f"""
        SELECT e.id, 'tvmaze', '',
               CASE WHEN {_TVMAZE_NO_TIME} THEN {_TVMAZE_DAY} || 'T00:00:00Z' ELSE te.airstamp END,
               CASE WHEN {_TVMAZE_NO_TIME} THEN 'date' ELSE 'time' END,
               CASE WHEN {_TVMAZE_NO_TIME} THEN {_TVMAZE_DAY} END
        FROM episode e
        JOIN show_external_id tm ON tm.show_id = e.show_id AND tm.service = 'tvmaze'
                                AND tm.external_id != '-1'
        JOIN tvmaze_episode te ON te.tvmaze_show_id = CAST(tm.external_id AS INTEGER)
                              AND te.season = e.sonarr_season AND te.episode = e.sonarr_episode
        WHERE e.show_id = :show AND e.kind = 'regular'
          AND ((te.airstamp IS NOT NULL AND te.airstamp != '')
               OR (te.airdate IS NOT NULL AND te.airdate != ''))""",
    # AniDB's date is the Japanese calendar day
    "anidb": """
        SELECT e.id, 'anidb', '', ae.airdate || 'T00:00:00Z', 'date', ae.airdate
        FROM episode e
        JOIN episode_anidb_mapping m ON m.episode_id = e.id
        JOIN anidb_episode ae ON ae.anidb_anime_id = m.anidb_anime_id
                             AND ae.anidb_season = m.anidb_season
                             AND ae.anidb_epno = m.anidb_epno
        WHERE e.show_id = :show AND e.kind = 'regular'
          AND ae.airdate IS NOT NULL AND ae.airdate != ''""",
    # One candidate per TV station: that station's first broadcast of the episode. Same join and
    # guards as `syoboi.fill_airdate_gaps` (the show's primary AniDB id, AniDB season 1).
    "syoboi": """
        SELECT e.id, 'syoboi', CAST(sp.chid AS TEXT), MIN(sp.st_time_utc), 'time', NULL
        FROM episode e
        JOIN episode_anidb_mapping m ON m.episode_id = e.id
        JOIN show_external_id ss ON ss.show_id = e.show_id AND ss.service = 'syoboi'
        JOIN show_external_id sa ON sa.show_id = e.show_id AND sa.service = 'anidb'
        JOIN syoboi_program sp ON sp.tid = CAST(ss.external_id AS INTEGER)
                              AND sp.count = m.anidb_epno
        WHERE e.show_id = :show AND e.kind = 'regular'
          AND m.anidb_season = 1 AND m.anidb_anime_id = CAST(sa.external_id AS INTEGER)
          AND sp.deleted = 0 AND sp.st_time_utc IS NOT NULL
        GROUP BY e.id, sp.chid""",
}


def _collect(conn, show_id: str) -> tuple[dict[str, int], list[dict]]:
    """Rebuild the stored-data candidates of one show, diffed against what was held: returns
    ({source: candidates}, the change events — a candidate that is new or whose value moved)."""
    now = util.now_utc_iso()
    rows: dict[tuple, tuple] = {}
    counts: dict[str, int] = {}
    for source in _DERIVED_SOURCES:
        found = conn.execute(_DERIVED_SQL[source], {"show": show_id}).fetchall()
        counts[source] = len(found)
        for r in found:
            rows[(r[0], r[1], r[2])] = (r[3], r[4], r[5])
    for key, value in _syoboi_by_numbering(conn, show_id).items():
        if key not in rows:  # never replaces a candidate the AniDB route already made
            rows[key] = value
            counts["syoboi"] += 1
    # a season that has aired fully is not touched: no new candidate, no change, no deletion
    finished = finished_seasons(conn, show_id)
    if finished:
        season_of = {r[0]: r[1] for r in conn.execute(
            "SELECT id, season FROM episode WHERE show_id = ?", (show_id,))}
        rows = {k: v for k, v in rows.items() if season_of.get(k[0]) not in finished}
    else:
        season_of = {}
    previous = {
        (r["episode_id"], r["source"], r["channel"])
        for r in conn.execute(
            "SELECT c.episode_id, c.source, c.channel FROM episode_air_candidate c"
            " JOIN episode e ON e.id = c.episode_id"
            " WHERE e.show_id = ? AND c.source IN (?, ?, ?, ?)",
            (show_id, *_DERIVED_SOURCES),
        )
    }
    events = []
    for (episode_id, source, channel), (air, precision, local) in rows.items():
        if not air:
            continue
        event = _upsert_candidate(conn, episode_id, source, channel, air, precision, local, now)
        if event is not None:
            events.append(event)
    for episode_id, source, channel in previous - set(rows):  # a source stopped reporting it
        if season_of.get(episode_id) in finished:
            continue
        conn.execute(
            "DELETE FROM episode_air_candidate WHERE episode_id = ? AND source = ? AND channel = ?",
            (episode_id, source, channel),
        )
    return counts, events


def collect_candidates(conn, show_id: str) -> dict[str, int]:
    """Rebuild the stored-data candidates for one show. Returns {source: candidates}."""
    return _collect(conn, show_id)[0]


def _syoboi_by_numbering(conn, show_id: str) -> dict[tuple, tuple]:
    """Syoboi's stations for episodes AniDB has not been mapped to (user 10-05: Syoboi never showed
    as a choice — the join above needs AniDB's numbering, which most running shows do not have
    yet). Each level is matched to Syoboi's numbered broadcasts at episode level, by air date
    (syoboi_levels.episode_map: no id is trusted to say which TID covers it); an episode's
    (TID, count) then gives one candidate per station."""
    from lcars import sonarr_match, syoboi_levels

    if sonarr_match.show_numbering_diverged(conn, show_id):
        return {}
    mapping = syoboi_levels.episode_map(conn, show_id)
    if not mapping:
        return {}
    stations: dict[tuple[int, int], list] = {}
    for tid in {t for t, _c in mapping.values()}:
        for p in conn.execute(
            "SELECT chid, count, MIN(st_time_utc) AS first FROM syoboi_program WHERE tid = ?"
            " AND deleted = 0 AND count > 0 AND st_time_utc IS NOT NULL GROUP BY chid, count",
            (tid,),
        ):
            stations.setdefault((tid, p["count"]), []).append((p["chid"], p["first"]))
    made: dict[tuple, tuple] = {}
    for episode_id, key in mapping.items():
        for chid, first in stations.get(key, []):
            made.setdefault((episode_id, "syoboi", str(chid)), (first, air_time.TIME, None))
    return made


# A season that has aired fully — every episode dated, the last one aired more than this many days
# ago — and is not planned is history: it is not refreshed (no API call is spent on it) and its
# dates never change (user 10-07; `season_states`). The grace is the one `R1.0c` gives a show's
# last aired episode.
FINISHED_GRACE_DAYS = 14

# An episode aired longer ago than this keeps its date when a source's schedule moves (anime),
# even inside a season that is still airing.
HISTORY_DAYS = 45


def season_states(conn, show_id: str) -> dict[int, str]:
    """Where each season (by `episode.season`) of the show stands, for what is worth refreshing
    (user 10-07: keep the schedules of what is airing or will air up to date):

    - `airing`: an episode is dated in the future or aired within the last 14 days, or has no
      date yet;
    - `planned`: the season's status is planned — a season you mean to watch, which is where a
      future season sits whatever date it wrongly holds;
    - `history`: aired fully and not planned — not refreshed, no API call, its dates never change.
    """
    cutoff = util.utc_iso_offset(-FINISHED_GRACE_DAYS)
    planned = {
        r["season_number"] for r in conn.execute(
            "SELECT season_number FROM season WHERE show_id = ? AND kind = 'tvdb_season'"
            " AND status = 'planned'", (show_id,))
    }
    airing: dict[int, bool] = {}
    for r in conn.execute(
        "SELECT season, air_date_utc, COALESCE(air_aired_at, air_date_utc) AS at FROM episode"
        " WHERE show_id = ? AND kind = 'regular' AND season > 0", (show_id,),
    ):
        now_airing = r["air_date_utc"] is None or r["at"] >= cutoff
        airing[r["season"]] = airing.get(r["season"], False) or now_airing
    return {
        number: "airing" if is_airing else ("planned" if number in planned else "history")
        for number, is_airing in airing.items()
    }


def finished_seasons(conn, show_id: str) -> set[int]:
    """The seasons of the show that have aired fully (history)."""
    return {n for n, state in season_states(conn, show_id).items() if state == "history"}


def has_airing_season(conn, show_id: str) -> bool:
    """Does the show have a season that is airing or planned?"""
    return any(state != "history" for state in season_states(conn, show_id).values())


def refresh_cadence(conn, show_id: str) -> str | None:
    """How often a *planned* show is owed its schedules (user 10-07): daily while a season is
    airing, weekly while the only open seasons are planned, never when every season is history."""
    states = set(season_states(conn, show_id).values())
    if "airing" in states:
        return "daily"
    if "planned" in states:
        return "weekly"
    return None


# A Sonarr/TVDB date this far from a date a broadcast-aware source holds is not applied over it.
IMPLAUSIBLE_GAP_DAYS = 60

# ── selection: which candidate becomes the stored date ──────────────────────────────────────────


def _is_anime(conn, show_id: str) -> bool:
    row = conn.execute("SELECT tracking_space FROM show WHERE id = ?", (show_id,)).fetchone()
    return row is not None and row["tracking_space"] == "anime"


def _write_date(
    conn, episode_id: str, anime: bool, source: str, air: str, precision: str,
    local_date: str | None, changed_by: str, now: str,
) -> None:
    """Write a candidate as the episode's stored date, with the audit row when the value moves."""
    prev = conn.execute(
        "SELECT air_date_utc, air_date_source FROM episode WHERE id = ?", (episode_id,)
    ).fetchone()
    if prev["air_date_utc"] != air:
        conn.execute(
            "INSERT INTO air_date_change (id, episode_id, previous_air_date_utc, new_air_date_utc,"
            " previous_source, new_source, changed_at, changed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ids.generate_id(conn, "g"), episode_id, prev["air_date_utc"], air,
             prev["air_date_source"], source, now, changed_by),
        )
    is_date = precision == air_time.DATE and local_date is not None
    conn.execute(
        "UPDATE episode SET air_date_utc = ?, air_date_source = ?, air_precision = ?,"
        " air_local_date = ?, air_aired_at = ?, updated_at = ? WHERE id = ?",
        (air, source, air_time.DATE if is_date else None, local_date if is_date else None,
         air_time.aired_at(local_date, anime) if is_date else None, now, episode_id),
    )


def _best(cands: list[dict]) -> dict | None:
    """The likeliest schedule when nothing else decides: the earliest timed candidate, a
    date-only one only when no timed one exists."""
    timed = [c for c in cands if c["precision"] == air_time.TIME]
    pool = timed or cands
    return min(pool, key=lambda c: c["air_date_utc"]) if pool else None


def _tv_pick(cands: list[dict]) -> dict | None:
    """A TV show (not anime): Sonarr's date wins (user 10-07). A timed candidate still replaces a
    Sonarr date-only value that falls within three days of it (a precision upgrade)."""
    sonarr = [c for c in cands if c["source"] == "sonarr"]
    timed_sonarr = [c for c in sonarr if c["precision"] == air_time.TIME]
    if timed_sonarr:
        return timed_sonarr[0]
    best_timed = _best([c for c in cands if c["precision"] == air_time.TIME])
    if sonarr:  # Sonarr has a date only
        if best_timed is not None and airdate_priority.within_upgrade_window(
            best_timed["air_date_utc"], sonarr[0]["air_date_utc"]
        ):
            return best_timed
        return sonarr[0]
    return _best(cands)


def _distrusted_anilist_seasons(conn, show_id: str) -> set[str]:
    """Seasons whose AniList schedule an open review says is probably another entry's (the
    wrong-entry guard, `metadata._reconcile_air_dates`): kept as a candidate to look at and
    choose, never applied by the automatic rule."""
    return {
        r[0] for r in conn.execute(
            "SELECT r.entity_id FROM pending_review r JOIN season z ON z.id = r.entity_id"
            " WHERE r.entity_type = 'season' AND r.field = 'anilist_id' AND r.resolved_at IS NULL"
            " AND z.show_id = ?", (show_id,))
    }


def _candidates_by_episode(conn, show_id: str) -> dict[str, list[dict]]:
    """The candidates the automatic rule may pick from, per episode."""
    distrusted = _distrusted_anilist_seasons(conn, show_id)
    by_episode: dict[str, list[dict]] = {}
    for c in conn.execute(
        "SELECT c.*, e.season_id FROM episode_air_candidate c JOIN episode e ON e.id = c.episode_id"
        " WHERE e.show_id = ? AND e.kind = 'regular'", (show_id,),
    ):
        if c["source"] == "anilist" and c["season_id"] in distrusted:
            continue
        by_episode.setdefault(c["episode_id"], []).append(dict(c))
    return by_episode


def auto_apply(conn, show_id: str, events: list[dict]) -> dict:
    """The automatic rule over the candidates (user 10-07). Skips a `manual` date, a skipped
    season (R2.10) and a season that follows a chosen schedule (`apply_choice` handles it).
    An empty date takes the best candidate. Otherwise TV takes Sonarr's date; anime reacts to a
    candidate that **changed or appeared**: it replaces the stored date by
    `airdate_priority.should_apply` (the same source moving its own value always applies, either
    way; a different source only by an earlier one, a timed one over a date-only one)."""
    stats = {"filled": 0, "changed": 0}
    anime = _is_anime(conn, show_id)
    now = util.now_utc_iso()
    by_episode = _candidates_by_episode(conn, show_id)
    changed_ids = {e["episode_id"] for e in events}
    history = util.utc_iso_offset(-HISTORY_DAYS)
    finished = finished_seasons(conn, show_id)
    rows = conn.execute(
        "SELECT id, season, season_id, air_date_utc, air_date_source, air_precision, air_aired_at"
        " FROM episode WHERE show_id = ? AND kind = 'regular'", (show_id,),
    ).fetchall()
    choice_seasons = {
        r[0] for r in conn.execute(
            "SELECT c.season_id FROM season_air_choice c JOIN season z ON z.id = c.season_id"
            " WHERE z.show_id = ?", (show_id,))
    }
    events_by_episode: dict[str, list[dict]] = {}
    for event in events:
        events_by_episode.setdefault(event["episode_id"], []).append(event)
    for row in rows:
        cands = by_episode.get(row["id"])
        if not cands or row["season_id"] in choice_seasons or row["season"] in finished:
            continue
        if row["air_date_source"] == "manual" or not status_rules.episode_followed(conn, row["id"]):
            continue
        stored_empty = row["air_date_utc"] is None
        if not anime:
            pick = _tv_pick(cands)
            if pick is None:
                continue
            same = (row["air_date_utc"] == pick["air_date_utc"]
                    and row["air_date_source"] == pick["source"]
                    and (row["air_precision"] or air_time.TIME) == pick["precision"])
            if not same:
                _write_date(conn, row["id"], anime, pick["source"], pick["air_date_utc"],
                            pick["precision"], pick["local_date"], "system", now)
                stats["filled" if stored_empty else "changed"] += 1
            continue
        if stored_empty:
            pick = _best(cands)
            if pick is not None:
                _write_date(conn, row["id"], anime, pick["source"], pick["air_date_utc"],
                            pick["precision"], pick["local_date"], "system", now)
                stats["filled"] += 1
            continue
        if row["id"] not in changed_ids:
            continue
        if (row["air_aired_at"] or row["air_date_utc"]) < history:
            continue  # long aired: the file, not a schedule, is the truth; history is not rewritten
        ordered = sorted(
            events_by_episode[row["id"]],
            key=lambda e: (e["precision"] != air_time.TIME, e["new"]),
        )
        current = (row["air_date_utc"], row["air_date_source"],
                   row["air_precision"] or air_time.TIME)
        for e in ordered:
            if (e["source"] in airdate_priority.WEAK_SOURCES
                    and current[1] not in airdate_priority.WEAK_SOURCES
                    and (air_time.hours_apart(e["new"], current[0]) or 0)
                    > IMPLAUSIBLE_GAP_DAYS * 24):
                # a TVDB/Sonarr date months from what a broadcast-aware source holds is another
                # link's dates (Kanojo no Tomodachi's were 15 years early), not a reschedule
                continue
            if airdate_priority.should_apply(
                e["source"], e["new"], current[1], current[0], e["precision"], current[2]
            ):
                _write_date(conn, row["id"], anime, e["source"], e["new"], e["precision"],
                            e["local_date"], "system", now)
                current = (e["new"], e["source"], e["precision"])
                stats["changed"] += 1
    return stats


def sync_stored_precision(conn, show_id: str) -> int:
    """A stored date that is a candidate's own value takes the candidate's precision (Sonarr's
    date turns out to be date-only once its series is read with its air time)."""
    anime = _is_anime(conn, show_id)
    done = 0
    for r in conn.execute(
        "SELECT e.id, c.precision, c.local_date, c.air_date_utc FROM episode e"
        " JOIN episode_air_candidate c ON c.episode_id = e.id AND c.source = e.air_date_source"
        "  AND c.channel = '' AND c.air_date_utc = e.air_date_utc"
        " WHERE e.show_id = ? AND (COALESCE(e.air_precision, 'time') != c.precision"
        "  OR (c.precision = 'date'"
        "      AND COALESCE(e.air_local_date, '') != COALESCE(c.local_date, '')))",
        (show_id,),
    ).fetchall():
        is_date = r["precision"] == air_time.DATE and r["local_date"]
        conn.execute(
            "UPDATE episode SET air_precision = ?, air_local_date = ?, air_aired_at = ?"
            " WHERE id = ?",
            (air_time.DATE if is_date else None, r["local_date"] if is_date else None,
             air_time.aired_at(r["local_date"], anime) if is_date else None, r["id"]),
        )
        done += 1
    return done


def refresh_show_schedules(conn, show_id: str) -> dict:
    """One show's schedules, brought up to date from what is stored: candidates rebuilt (and
    diffed), the automatic rule applied, every chosen schedule re-applied (user 10-07)."""
    if not has_airing_season(conn, show_id):
        return {"candidates": 0, "events": 0, "filled": 0, "changed": 0,
                "chosen": {"applied": 0, "unchanged": 0, "kept_manual": 0, "no_date": 0,
                           "fallback": 0}}
    counts, events = _collect(conn, show_id)
    auto = auto_apply(conn, show_id, events)
    chosen = apply_all_choices(conn, show_id)
    sync_stored_precision(conn, show_id)
    return {"candidates": sum(counts.values()), "events": len(events),
            "filled": auto["filled"], "changed": auto["changed"], "chosen": chosen}


def collect_for_airing(conn) -> dict:
    """The Memory Alpha step (user 10-05, widened 10-07): every tracked show, anime or not, that is
    watching or planned and still airing — an episode with no date or one dated in the last 45
    days or later — gets `refresh_show_schedules`: its candidates rebuilt from the stored data, the
    automatic rule applied, and a season's chosen schedule re-applied so it keeps following its
    source."""
    cutoff = util.utc_iso_offset(-45)
    total = {"shows": 0, "candidates": 0, "dates_updated": 0}
    for row in conn.execute(
        "SELECT sh.id FROM show sh WHERE sh.tracked = 1 AND sh.media_shape = 'episodic'"
        " AND sh.status IN ('watching', 'planned') AND EXISTS ("
        "  SELECT 1 FROM episode e WHERE e.show_id = sh.id AND e.kind = 'regular'"
        "  AND (e.air_date_utc IS NULL OR e.air_date_utc > ?))", (cutoff,),
    ).fetchall():
        result = refresh_show_schedules(conn, row["id"])
        total["shows"] += 1
        total["candidates"] += result["candidates"]
        total["dates_updated"] += (
            result["chosen"].get("applied", 0) + result["filled"] + result["changed"])
    conn.commit()
    return total


def set_choice(
    conn, season_id: str, source: str, channel: str = "", changed_by: str = "schedule_choice"
) -> dict:
    """Make a season follow `source`/`channel`, and write its dates from it now."""
    channel = channel or ""
    has = conn.execute(
        "SELECT 1 FROM episode_air_candidate c JOIN episode e ON e.id = c.episode_id"
        " WHERE e.season_id = ? AND c.source = ? AND c.channel = ? LIMIT 1",
        (season_id, source, channel),
    ).fetchone()
    if has is None:
        raise ValueError(f"no {source} schedule is known for this season")
    if source not in _STORABLE_SOURCES:
        raise ValueError(f"{source} cannot be chosen")
    conn.execute(
        "INSERT INTO season_air_choice (season_id, source, channel, chosen_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT (season_id) DO UPDATE SET source = excluded.source,"
        " channel = excluded.channel, chosen_at = excluded.chosen_at",
        (season_id, source, channel, util.now_utc_iso()),
    )
    return apply_choice(conn, season_id, changed_by)


def clear_choice(conn, season_id: str) -> None:
    """Stop following a chosen schedule. Dates stay as they are; the automatic rule resumes."""
    conn.execute("DELETE FROM season_air_choice WHERE season_id = ?", (season_id,))


def apply_choice(
    conn, season_id: str, changed_by: str = "schedule_choice", skip_finished: bool = False
) -> dict:
    """Write the chosen schedule's dates onto the season's episodes, from now on until the choice
    is cleared (`refresh_show_schedules` re-applies it after every refresh, so a missed week moves
    the dates). A `manual` date stays; a season that isn't followed (skipped, R2.10) is left
    alone. An episode the chosen schedule has **no date for** keeps a date it has and, when it has
    none, takes the earliest candidate there is — until the chosen source has a date for it
    (user 10-07)."""
    choice = season_choice(conn, season_id)
    stats = {"applied": 0, "unchanged": 0, "kept_manual": 0, "no_date": 0, "fallback": 0}
    if choice is None:
        return stats
    show_id = conn.execute("SELECT show_id FROM season WHERE id = ?", (season_id,)).fetchone()
    anime = show_id is not None and _is_anime(conn, show_id[0])
    finished = (finished_seasons(conn, show_id[0])
                if skip_finished and show_id is not None else set())
    rows = conn.execute(
        "SELECT e.id, e.season, e.air_date_utc, e.air_date_source, e.air_precision,"
        "  c.air_date_utc AS cand, c.precision AS cand_precision, c.local_date AS cand_local"
        " FROM episode e"
        " LEFT JOIN episode_air_candidate c ON c.episode_id = e.id"
        "  AND c.source = ? AND c.channel = ?"
        " WHERE e.season_id = ? AND e.kind = 'regular' ORDER BY e.season, e.episode",
        (choice["source"], choice["channel"], season_id),
    ).fetchall()
    now = util.now_utc_iso()
    for row in rows:
        if row["season"] in finished:
            stats["unchanged"] += 1  # a season that has aired fully: re-applying changes nothing
        elif row["air_date_source"] == "manual":
            stats["kept_manual"] += 1
        elif not status_rules.episode_followed(conn, row["id"]):
            stats["unchanged"] += 1
        elif row["cand"] is None:
            stats["no_date"] += 1
            if row["air_date_utc"] is None:
                skip_anilist = season_id in _distrusted_anilist_seasons(conn, show_id[0])
                fallback = _best([
                    dict(c) for c in conn.execute(
                        "SELECT * FROM episode_air_candidate WHERE episode_id = ?", (row["id"],))
                    if not (skip_anilist and c["source"] == "anilist")])
                if fallback is not None:
                    _write_date(conn, row["id"], anime, fallback["source"],
                                fallback["air_date_utc"], fallback["precision"],
                                fallback["local_date"], changed_by, now)
                    stats["fallback"] += 1
        elif (row["air_date_utc"] == row["cand"] and row["air_date_source"] == choice["source"]
              and (row["air_precision"] or air_time.TIME) == row["cand_precision"]):
            stats["unchanged"] += 1
        else:
            _write_date(conn, row["id"], anime, choice["source"], row["cand"],
                        row["cand_precision"], row["cand_local"], changed_by, now)
            stats["applied"] += 1
    return stats


def apply_all_choices(conn, show_id: str) -> dict:
    """Re-apply every chosen schedule of a show (after new candidates arrived)."""
    total = {"applied": 0, "unchanged": 0, "kept_manual": 0, "no_date": 0, "fallback": 0}
    for row in conn.execute(
        "SELECT c.season_id FROM season_air_choice c JOIN season z ON z.id = c.season_id"
        " WHERE z.show_id = ?",
        (show_id,),
    ).fetchall():
        for key, value in apply_choice(conn, row["season_id"], skip_finished=True).items():
            total[key] += value
    return total


def _channel_labels(conn) -> dict[str, str]:
    """Syoboi station names (syoboi.ensure_channels); an unknown station shows as 'ch N'."""
    rows = conn.execute("SELECT chid, name FROM syoboi_channel")
    return {str(r["chid"]): r["name"] for r in rows}


def _label(source: str, channel: str, names: dict[str, str]) -> str:
    base = SOURCE_LABELS.get(source, source)
    if source == "syoboi" and channel:
        return f"{base} · {names.get(channel) or 'ch ' + channel}"
    return f"{base} · {channel}" if channel else base


def schedules_for_show(conn, show_id: str) -> list[dict]:
    """Every season level of the show that has regular episodes, with each known schedule
    (source + channel) as an option the user can choose."""
    names = _channel_labels(conn)
    result = []
    seasons = conn.execute(
        "SELECT z.* FROM season z WHERE z.show_id = ?"
        " ORDER BY z.season_number, z.part_number",
        (show_id,),
    ).fetchall()
    for z in seasons:
        eps = conn.execute(
            "SELECT id, season, episode, air_date_utc, air_date_source FROM episode"
            " WHERE season_id = ? AND kind = 'regular' ORDER BY season, episode",
            (z["id"],),
        ).fetchall()
        if not eps:
            continue
        choice = season_choice(conn, z["id"])
        current = {e["id"]: e for e in eps}
        by_option: dict[tuple[str, str], list] = {}
        for c in conn.execute(
            "SELECT c.episode_id, c.source, c.channel, c.air_date_utc, c.precision, c.local_date"
            " FROM episode_air_candidate c"
            " JOIN episode e ON e.id = c.episode_id WHERE e.season_id = ? AND e.kind = 'regular'"
            " ORDER BY e.season, e.episode",
            (z["id"],),
        ):
            by_option.setdefault((c["source"], c["channel"]), []).append(c)
        options = []
        for (source, channel), cands in sorted(by_option.items()):
            episodes = [
                {
                    "episode_id": c["episode_id"],
                    "season": current[c["episode_id"]]["season"],
                    "episode": current[c["episode_id"]]["episode"],
                    "air_date_utc": c["air_date_utc"],
                    "precision": c["precision"],
                    "local_date": c["local_date"],
                    "current_air_date_utc": current[c["episode_id"]]["air_date_utc"],
                }
                for c in cands
            ]
            dates = sorted(e["air_date_utc"] for e in episodes)
            options.append({
                "source": source,
                "channel": channel,
                "label": _label(source, channel, names),
                "episode_count": len(episodes),
                "first_air_date_utc": dates[0],
                "last_air_date_utc": dates[-1],
                "date_only": all(e["precision"] == air_time.DATE for e in episodes),
                "in_use": all(e["air_date_utc"] == e["current_air_date_utc"] for e in episodes),
                "chosen": choice is not None
                          and choice["source"] == source and choice["channel"] == channel,
                "episodes": episodes,
            })
        sources_now = Counter(e["air_date_source"] for e in eps if e["air_date_source"])
        result.append({
            "season_id": z["id"],
            "season_number": z["season_number"],
            "part_number": z["part_number"],
            "episode_count": len(eps),
            "current_source": sources_now.most_common(1)[0][0] if sources_now else None,
            "chosen_source": choice["source"] if choice else None,
            "chosen_channel": choice["channel"] if choice else None,
            "options": options,
        })
    return result


# ── the calendar's change icons (user 10-07) ────────────────────────────────────────────────────


def air_change_flags(conn, episode_id: str) -> dict:
    """`!` and `?` for an episode's air date. Every source and station is compared with the
    value it **first reported** for the episode: `chosen` (`?`) when the schedule the season
    follows has moved more than two hours, `other` (`!`) when any other source or station has.
    With no schedule chosen every source is an 'other'. Counts from the first refresh after the
    baseline was taken (`first_air_date_utc`)."""
    row = conn.execute("SELECT season_id FROM episode WHERE id = ?", (episode_id,)).fetchone()
    choice = season_choice(conn, row["season_id"]) if row and row["season_id"] else None
    names = None
    details = []
    for c in conn.execute(
        "SELECT source, channel, air_date_utc, first_air_date_utc FROM episode_air_candidate"
        " WHERE episode_id = ? ORDER BY source, channel", (episode_id,),
    ):
        hours = air_time.hours_apart(c["air_date_utc"], c["first_air_date_utc"])
        if hours is None or hours <= air_time.CHANGE_ICON_HOURS:
            continue
        if names is None:
            names = _channel_labels(conn)
        details.append({
            "source": c["source"],
            "channel": c["channel"],
            "label": _label(c["source"], c["channel"], names),
            "previous_air_date_utc": c["first_air_date_utc"],
            "current_air_date_utc": c["air_date_utc"],
            "hours": hours,
            "followed": choice is not None
                        and choice["source"] == c["source"] and choice["channel"] == c["channel"],
        })
    return {
        "chosen": any(d["followed"] for d in details),
        "other": any(not d["followed"] for d in details),
        "details": details,
    }


# ── chronology (R1.6): seasons follow each other in time ────────────────────────────────────────


def previous_season_end(conn, show_id: str, season_number: int | None) -> str | None:
    """The last air date of the season before `season_number` (the real episodes of the nearest
    earlier TVDB season that has dates), or None for a first season / no dates."""
    if not season_number or season_number < 2:
        return None
    row = conn.execute(
        "SELECT MAX(air_date_utc) FROM episode WHERE show_id = ? AND kind = 'regular'"
        " AND provisional = 0 AND air_date_utc IS NOT NULL"
        " AND season = (SELECT MAX(season) FROM episode WHERE show_id = ? AND kind = 'regular'"
        "   AND provisional = 0 AND air_date_utc IS NOT NULL AND season < ? AND season > 0)",
        (show_id, show_id, season_number),
    ).fetchone()
    return row[0] if row else None


def starts_before_previous_season_ended(
    conn, show_id: str, season_number: int | None, first_date: str | None
) -> bool:
    """Would a season starting on `first_date` begin more than three days before the season
    before it ended? Not a schedule: a copy of another season's dates (user 10-07: ten planned
    shows held their first season's premiere date on a season that has not aired)."""
    end = previous_season_end(conn, show_id, season_number)
    hours = air_time.hours_apart(end, first_date)
    return bool(end and first_date and first_date < end and hours is not None and hours > 72)
