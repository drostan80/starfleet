"""Local file audit — SCOPE.md §5.2/§6.10, BUILD_PLAN.md B.3b.

Two genuinely different mechanisms, done inside one pass per service
(`_audit_sonarr`/`_audit_radarr`) and bundled behind one mutation
(`auditLocalFiles`), because they answer the same underlying question
("is the database actually correct?") from two different angles:

1. **Current-state reconciliation** — pure API, no filesystem access
   at all, stays fully inside §6.10's "query the API, never scan the
   filesystem" rule. Sonarr's `episode?includeEpisodeFile=true` and
   Radarr's own `movie?tmdbId=` (which already embeds `movieFile`)
   both report *current* truth about what files each service actually
   has right now — a genuine, independent cross-check against B.3's
   own `/history`-based polling (an event-log reconstruction that
   could in principle drift: retention limits, a missed event).
   Corrects `available_via_sonarr`/`available_via_radarr` + the
   matching `file_path_*` directly, same columns `availability.py`
   writes, same "not a changed_by-tracked field" reasoning (no
   require_client()).

2. **Orphan/untracked discovery** — the one piece that genuinely needs
   to read the filesystem, since a file Sonarr/Radarr never associated
   with anything has no API record to query in the first place. This is a
   deliberate, narrow exception to §6.10 — not a reversal of it — the
   same "rare, explicit, user-triggered utility" shape §9 already
   anticipated. Report-only: an orphan file has no existing LCARS row
   to correct (`pending_review`'s own `entity_type`/`entity_id` shape
   doesn't fit an orphan any better), and an untracked remote show
   isn't auto-created — both are returned in the mutation's own result
   for the user to act on by hand (attach/`addShow`), never written
   automatically. Needs LCARS's own container to have the identical
   media volume mount Sonarr/Radarr already have (§11.3's B.3b
   addendum) — gracefully skipped, not an error, if a show's own
   reported path isn't accessible (e.g. the mount hasn't been added
   yet).

Not called by Ops's own automatic loop, same reasoning
`backfill_file_availability()` (B.3) already established for a
manually-triggered, potentially-slow operation — `ops
audit-local-files` is the deliberate trigger. Resolved 2026-08-09
directly with the user, after B.3's own commit; SCOPE.md §5.2's own
addendum has the full design history.
"""

import logging
import os
import re

from lcars import radarr_client, service_health, sonarr_client, util
from lcars.config import get_current

logger = logging.getLogger("lcars.local_audit")

# Sonarr/Radarr's own supported media extensions (a deliberately small,
# common set) — filters out subtitle/nfo/image files that legitimately
# sit alongside a video file without being one themselves, so those
# never get reported as false-positive orphans.
_VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv"}

# Matches both of the user's own real naming templates (standard and
# anime) — both always carry "S{season:00}E{episode:00}" somewhere in
# the filename regardless of what else surrounds it (episode title,
# custom formats, media info, absolute number, release group). No
# per-template detection needed — one anchored pattern handles both,
# and files predating a template change besides. A file this can't
# match still gets reported (season/episode left null), not skipped —
# a parse failure is itself a finding, not silence.
_SEASON_EPISODE_RE = re.compile(r"[Ss](\d{2,})[Ee](\d{2,})")


def audit_local_files(conn) -> dict:
    """Runs both services' reconciliation + discovery passes, returns
    the combined result (schema.graphql's LocalFileAuditResult)."""
    sonarr = _audit_sonarr(conn)
    radarr = _audit_radarr(conn)
    return {
        "episodes_corrected": sonarr["episodes_corrected"],
        "shows_corrected": radarr["shows_corrected"],
        "orphan_files": sonarr["orphan_files"] + radarr["orphan_files"],
        "untracked_shows": sonarr["untracked_shows"] + radarr["untracked_shows"],
    }


def _show_id_for_tvdb(conn, tvdb_id: int) -> str | None:
    row = conn.execute(
        "SELECT show_id FROM show_external_id WHERE service = 'tvdb' AND external_id = ?",
        (str(tvdb_id),),
    ).fetchone()
    return row["show_id"] if row else None


def _show_id_for_tmdb_movie(conn, tmdb_id: int) -> str | None:
    row = conn.execute(
        "SELECT s.id FROM show s"
        " JOIN show_external_id sei ON sei.show_id = s.id"
        " WHERE sei.service = 'tmdb' AND sei.external_id = ? AND s.media_shape = 'movie'",
        (str(tmdb_id),),
    ).fetchone()
    return row["id"] if row else None


def _known_tvdb_ids(conn) -> set[str]:
    rows = conn.execute("SELECT external_id FROM show_external_id WHERE service = 'tvdb'")
    return {row["external_id"] for row in rows}


def _known_tmdb_movie_ids(conn) -> set[str]:
    rows = conn.execute(
        "SELECT sei.external_id FROM show_external_id sei"
        " JOIN show s ON s.id = sei.show_id"
        " WHERE sei.service = 'tmdb' AND s.media_shape = 'movie'"
    )
    return {row["external_id"] for row in rows}


def known_anilist_ids(conn) -> set[str]:
    """B.11d — every AniList id LCARS already has a link for, checking
    **both** places one can live: `show_external_id` (the show-level
    link, A.24) and `season.anilist_id` (§5.5's own per-season
    crosswalk — a split-cour sequel season has its own AniList entry,
    genuinely different from the show-level one, per
    _reconcile_air_dates's own docstring). show_backfill.py's own
    AniList-only sweep needs both checked, or a sequel season already
    tracked only at the season level would look untracked and get
    proposed as a brand new duplicate show."""
    show_level = conn.execute(
        "SELECT external_id FROM show_external_id WHERE service = 'anilist'"
    ).fetchall()
    season_level = conn.execute(
        "SELECT anilist_id FROM season WHERE anilist_id IS NOT NULL"
    ).fetchall()
    return {row["external_id"] for row in show_level} | {
        str(row["anilist_id"]) for row in season_level
    }


def all_sonarr_series_with_seasons(conn) -> list[dict]:
    """B.11d — every series Sonarr's own catalog currently reports,
    tracked in LCARS or not (unlike _known_tvdb_ids above, which is
    LCARS-side only), each with the real season numbers Sonarr itself
    lists (season 0 — specials — excluded; verified live: Sonarr's own
    `seasons` array is present on every real series response).
    show_backfill.py's own AniList-only sweep uses this to build the
    full set of AniList ids Fribb can resolve across *every* season of
    *every* Sonarr-known show — not just season 1, which a live run
    against the user's real library showed is genuinely necessary:
    Fribb resolves Frieren's own season 1 and season 2 to two
    different AniList ids (verified live), so a season-1-only check
    would have missed season 2 as "already accounted for" and let it
    through as an independent, duplicate untracked show. Read-only, no
    service_health record, no commit — same side-effect-free contract
    find_untracked_shows_readonly() above already establishes."""
    cfg = get_current()
    if not cfg.sonarr_url or not cfg.sonarr_api_key:
        return []
    try:
        with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
            all_series = client.all_series()
    except sonarr_client.SonarrError:
        return []
    return [
        {
            "tvdb_id": s["tvdbId"],
            "season_numbers": [
                season["seasonNumber"]
                for season in s.get("seasons", [])
                if season["seasonNumber"] > 0
            ]
            or [1],  # Sonarr reports no seasons array at all — fall back to the season-1 convention
        }
        for s in all_series
    ]


def _walk_video_files(root: str) -> list[str]:
    if not os.path.isdir(root):
        return []  # the mount isn't there (yet), or the path is stale — not an error
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if os.path.splitext(name)[1].lower() in _VIDEO_EXTENSIONS:
                found.append(os.path.join(dirpath, name))
    return found


def _parse_season_episode(filename: str) -> tuple[int | None, int | None]:
    match = _SEASON_EPISODE_RE.search(filename)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _untracked_sonarr_entries(all_series: list[dict], known_tvdb_ids: set[str]) -> list[dict]:
    """The actual untracked-detection logic, pure — shared by
    _audit_sonarr's own combined pass and find_untracked_shows_readonly()
    below (B.11d), so there's exactly one implementation of "is this
    Sonarr series untracked" rather than two that could drift."""
    return [
        {
            "service": "sonarr",
            "title": series["title"],
            "external_id": series["tvdbId"],
            "path": series.get("path"),
            # B.11d — Sonarr's own seriesType, kept for reference (not
            # currently the anime signal show_backfill.py's own
            # classification uses — see fribb-resolution note there —
            # but the same seriesType == "anime" signal metadata.py's
            # numbering-scheme derivation, A.22, still reads).
            "series_type": series.get("seriesType"),
            # B.11d — every real season number Sonarr itself lists
            # (season 0 excluded), so show_backfill.py's own
            # classification can Fribb-resolve every season, not just
            # season 1 — see all_sonarr_series_with_seasons's own
            # docstring for why season 1 alone isn't enough.
            "season_numbers": [
                season["seasonNumber"]
                for season in series.get("seasons", [])
                if season["seasonNumber"] > 0
            ]
            or [1],
        }
        for series in all_series
        if str(series["tvdbId"]) not in known_tvdb_ids
    ]


def _untracked_radarr_entries(all_movies: list[dict], known_tmdb_ids: set[str]) -> list[dict]:
    """Same as _untracked_sonarr_entries above, for Radarr."""
    return [
        {
            "service": "radarr",
            "title": movie["title"],
            "external_id": movie["tmdbId"],
            "path": movie.get("path"),
        }
        for movie in all_movies
        if str(movie["tmdbId"]) not in known_tmdb_ids
    ]


def find_untracked_shows_readonly(conn) -> list[dict]:
    """B.11d — show_backfill.py's own previewShowBackfill Query needs
    exactly the untracked-detection half of audit_local_files(), and
    nothing else: no availability reconciliation writes, no
    filesystem-reading orphan-file walk, no service_health record, no
    commit. A Query must stay side-effect free (same reasoning
    exportData's own docstring already gives for why it's a Query, not
    a Mutation) — audit_local_files() itself is correctly a Mutation
    precisely because it does write. Shares its actual matching logic
    with _audit_sonarr/_audit_radarr via _untracked_sonarr_entries/
    _untracked_radarr_entries above, not a second, divergent
    implementation — just a separate, lighter-weight caller. A
    Sonarr/Radarr connection failure here is swallowed the same
    "nothing new to report" way _audit_sonarr's own except-branch
    would otherwise still commit a service_health failure record for —
    this function makes no commit at all, so it can't record one
    either; the next real auditLocalFiles/backfillUntrackedShows call
    records it properly."""
    cfg = get_current()
    found: list[dict] = []

    if cfg.sonarr_url and cfg.sonarr_api_key:
        try:
            with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
                all_series = client.all_series()
        except sonarr_client.SonarrError:
            all_series = []
        found += _untracked_sonarr_entries(all_series, _known_tvdb_ids(conn))

    if cfg.radarr_url and cfg.radarr_api_key:
        try:
            with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
                all_movies = client.all_movies()
        except radarr_client.RadarrError:
            all_movies = []
        found += _untracked_radarr_entries(all_movies, _known_tmdb_movie_ids(conn))

    return found


def _audit_sonarr(conn) -> dict:
    cfg = get_current()
    empty = {"episodes_corrected": 0, "orphan_files": [], "untracked_shows": []}
    if not cfg.sonarr_url or not cfg.sonarr_api_key:
        return empty
    known_tvdb_ids = _known_tvdb_ids(conn)
    episodes_corrected = 0
    orphan_files: list[dict] = []
    untracked_shows: list[dict] = []
    now = util.now_utc_iso()

    try:
        with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
            all_series = client.all_series()
            untracked_shows = _untracked_sonarr_entries(all_series, known_tvdb_ids)

            for series in all_series:
                tvdb_id = str(series["tvdbId"])
                if tvdb_id not in known_tvdb_ids:
                    # Already captured in untracked_shows above — not this
                    # pass's job to walk a folder LCARS has nothing to
                    # match against.
                    continue

                show_id = _show_id_for_tvdb(conn, series["tvdbId"])
                episodes = client.episodes(series["id"], include_episode_file=True)

                known_file_paths: set[str] = set()
                for ep in episodes:
                    row = conn.execute(
                        "SELECT id, available_via_sonarr FROM episode"
                        " WHERE show_id = ? AND season = ? AND episode = ?",
                        (show_id, ep["seasonNumber"], ep["episodeNumber"]),
                    ).fetchone()
                    if row is None:
                        continue  # not yet fetched into LCARS — A.8's job, not this audit's
                    episode_file = ep.get("episodeFile")
                    if ep["hasFile"] and episode_file:
                        known_file_paths.add(episode_file["path"])
                        if row["available_via_sonarr"] != "available":
                            conn.execute(
                                "UPDATE episode SET available_via_sonarr = 'available',"
                                " file_path_sonarr = ?, available_checked_at = ? WHERE id = ?",
                                (episode_file["path"], now, row["id"]),
                            )
                            episodes_corrected += 1
                    elif not ep["hasFile"] and row["available_via_sonarr"] == "available":
                        conn.execute(
                            "UPDATE episode SET available_via_sonarr = 'unavailable',"
                            " file_path_sonarr = NULL, available_checked_at = ? WHERE id = ?",
                            (now, row["id"]),
                        )
                        episodes_corrected += 1

                series_path = series.get("path")
                if series_path:
                    for path in _walk_video_files(series_path):
                        if path in known_file_paths:
                            continue
                        season, episode = _parse_season_episode(os.path.basename(path))
                        orphan_files.append(
                            {
                                "show_id": show_id,
                                "path": path,
                                "parsed_season": season,
                                "parsed_episode": episode,
                            }
                        )
    except sonarr_client.SonarrError as e:
        logger.exception("Sonarr local audit failed partway through — partial results kept")
        service_health.record_failure(conn, "sonarr", str(e))
    else:
        service_health.record_success(conn, "sonarr")

    conn.commit()
    return {
        "episodes_corrected": episodes_corrected,
        "orphan_files": orphan_files,
        "untracked_shows": untracked_shows,
    }


def _audit_radarr(conn) -> dict:
    cfg = get_current()
    empty = {"shows_corrected": 0, "orphan_files": [], "untracked_shows": []}
    if not cfg.radarr_url or not cfg.radarr_api_key:
        return empty
    try:
        with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
            all_movies = client.all_movies()
    except radarr_client.RadarrError as e:
        logger.exception("Radarr local audit failed to list movies — skipped this pass")
        service_health.record_failure(conn, "radarr", str(e))
        conn.commit()
        return empty
    service_health.record_success(conn, "radarr")

    known_tmdb_ids = _known_tmdb_movie_ids(conn)
    untracked_shows = _untracked_radarr_entries(all_movies, known_tmdb_ids)
    shows_corrected = 0
    orphan_files: list[dict] = []
    now = util.now_utc_iso()

    for movie in all_movies:
        tmdb_id = str(movie["tmdbId"])
        if tmdb_id not in known_tmdb_ids:
            # Already captured in untracked_shows above.
            continue

        show_id = _show_id_for_tmdb_movie(conn, movie["tmdbId"])
        row = conn.execute(
            "SELECT available_via_radarr FROM show WHERE id = ?", (show_id,)
        ).fetchone()
        movie_file = movie.get("movieFile")
        known_file_paths: set[str] = set()
        if movie.get("hasFile") and movie_file:
            known_file_paths.add(movie_file["path"])
            if row["available_via_radarr"] != "available":
                conn.execute(
                    "UPDATE show SET available_via_radarr = 'available', file_path_radarr = ?,"
                    " available_checked_at = ? WHERE id = ?",
                    (movie_file["path"], now, show_id),
                )
                shows_corrected += 1
        elif not movie.get("hasFile") and row["available_via_radarr"] == "available":
            conn.execute(
                "UPDATE show SET available_via_radarr = 'unavailable', file_path_radarr = NULL,"
                " available_checked_at = ? WHERE id = ?",
                (now, show_id),
            )
            shows_corrected += 1

        movie_path = movie.get("path")
        if movie_path:
            for path in _walk_video_files(movie_path):
                if path in known_file_paths:
                    continue
                season, episode = _parse_season_episode(os.path.basename(path))
                orphan_files.append(
                    {
                        "show_id": show_id,
                        "path": path,
                        "parsed_season": season,
                        "parsed_episode": episode,
                    }
                )

    conn.commit()
    return {
        "shows_corrected": shows_corrected,
        "orphan_files": orphan_files,
        "untracked_shows": untracked_shows,
    }
