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
empty dates, so a choice never has to guard against them.
"""

from __future__ import annotations

import logging
from collections import Counter

from lcars import ids, status_rules, util

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

# SQL fragment for the overwriting writers: "this episode's season follows a chosen schedule".
LOCKED_SQL = (
    "EXISTS (SELECT 1 FROM season_air_choice _c WHERE _c.season_id = episode.season_id)"
)


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


def record_candidate(
    conn, episode_id: str, source: str, channel: str, air_date_utc: str | None
) -> None:
    """Remember what `source` (and `channel`, '' when it has no such notion) says. Idempotent."""
    if not air_date_utc:
        return
    conn.execute(
        "INSERT INTO episode_air_candidate (episode_id, source, channel, air_date_utc, fetched_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (episode_id, source, channel)"
        " DO UPDATE SET air_date_utc = excluded.air_date_utc, fetched_at = excluded.fetched_at",
        (episode_id, source, channel, air_date_utc, util.now_utc_iso()),
    )


# Candidate sources derived from tables LCARS already stores: rebuilt for a show each time.
# (AniList and animeschedule are live reads, recorded where they are read, and kept.)
_DERIVED_SOURCES = ("sonarr", "tvmaze", "anidb", "syoboi")

_DERIVED_SQL = {
    "sonarr": """
        SELECT e.id, 'sonarr', '', e.air_date_raw_sonarr
        FROM episode e
        WHERE e.show_id = :show AND e.kind = 'regular'
          AND e.air_date_raw_sonarr IS NOT NULL AND e.air_date_raw_sonarr != ''""",
    "tvmaze": """
        SELECT e.id, 'tvmaze', '', COALESCE(NULLIF(te.airstamp, ''), te.airdate || 'T00:00:00Z')
        FROM episode e
        JOIN show_external_id tm ON tm.show_id = e.show_id AND tm.service = 'tvmaze'
                                AND tm.external_id != '-1'
        JOIN tvmaze_episode te ON te.tvmaze_show_id = CAST(tm.external_id AS INTEGER)
                              AND te.season = e.sonarr_season AND te.episode = e.sonarr_episode
        WHERE e.show_id = :show AND e.kind = 'regular'
          AND ((te.airstamp IS NOT NULL AND te.airstamp != '')
               OR (te.airdate IS NOT NULL AND te.airdate != ''))""",
    "anidb": """
        SELECT e.id, 'anidb', '', ae.airdate || 'T00:00:00Z'
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
        SELECT e.id, 'syoboi', CAST(sp.chid AS TEXT), MIN(sp.st_time_utc)
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


def collect_candidates(conn, show_id: str) -> dict[str, int]:
    """Rebuild the stored-data candidates for one show. Returns {source: candidates}."""
    now = util.now_utc_iso()
    counts: dict[str, int] = {}
    for source in _DERIVED_SOURCES:
        conn.execute(
            "DELETE FROM episode_air_candidate WHERE source = ? AND episode_id IN"
            " (SELECT id FROM episode WHERE show_id = ?)",
            (source, show_id),
        )
        cursor = conn.execute(
            "INSERT OR REPLACE INTO episode_air_candidate"
            " (episode_id, source, channel, air_date_utc, fetched_at)"
            " SELECT q.*, :now FROM (" + _DERIVED_SQL[source] + ") q",
            {"show": show_id, "now": now},
        )
        counts[source] = cursor.rowcount
    return counts


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


def apply_choice(conn, season_id: str, changed_by: str = "schedule_choice") -> dict:
    """Write the chosen schedule's dates onto the season's episodes. A `manual` date stays; an
    episode the chosen schedule has no date for stays; a season that isn't followed (skipped,
    R2.10) is left alone."""
    choice = season_choice(conn, season_id)
    stats = {"applied": 0, "unchanged": 0, "kept_manual": 0, "no_date": 0}
    if choice is None:
        return stats
    rows = conn.execute(
        "SELECT e.id, e.air_date_utc, e.air_date_source, c.air_date_utc AS cand"
        " FROM episode e"
        " LEFT JOIN episode_air_candidate c ON c.episode_id = e.id"
        "  AND c.source = ? AND c.channel = ?"
        " WHERE e.season_id = ? AND e.kind = 'regular' ORDER BY e.season, e.episode",
        (choice["source"], choice["channel"], season_id),
    ).fetchall()
    now = util.now_utc_iso()
    for row in rows:
        if row["cand"] is None:
            stats["no_date"] += 1
        elif row["air_date_source"] == "manual":
            stats["kept_manual"] += 1
        elif not status_rules.episode_followed(conn, row["id"]):
            stats["unchanged"] += 1
        elif row["air_date_utc"] == row["cand"] and row["air_date_source"] == choice["source"]:
            stats["unchanged"] += 1
        else:
            if row["air_date_utc"] != row["cand"]:
                conn.execute(
                    "INSERT INTO air_date_change"
                    " (id, episode_id, previous_air_date_utc, new_air_date_utc,"
                    "  previous_source, new_source, changed_at, changed_by)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ids.generate_id(conn, "g"), row["id"], row["air_date_utc"], row["cand"],
                     row["air_date_source"], choice["source"], now, changed_by),
                )
            conn.execute(
                "UPDATE episode SET air_date_utc = ?, air_date_source = ?, updated_at = ?"
                " WHERE id = ?",
                (row["cand"], choice["source"], now, row["id"]),
            )
            stats["applied"] += 1
    return stats


def apply_all_choices(conn, show_id: str) -> dict:
    """Re-apply every chosen schedule of a show (after new candidates arrived)."""
    total = {"applied": 0, "unchanged": 0, "kept_manual": 0, "no_date": 0}
    for row in conn.execute(
        "SELECT c.season_id FROM season_air_choice c JOIN season z ON z.id = c.season_id"
        " WHERE z.show_id = ?",
        (show_id,),
    ).fetchall():
        for key, value in apply_choice(conn, row["season_id"]).items():
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
            "SELECT c.episode_id, c.source, c.channel, c.air_date_utc FROM episode_air_candidate c"
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
                "date_only": source == "anidb",
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
