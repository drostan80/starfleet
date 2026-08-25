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
    lists — season 0 (Sonarr's own "specials" bucket) included, not
    excluded: a second live check (Chainsaw Man, real data) showed
    Fribb tags a movie/OVA/compilation entry as season 0 as often as a
    real numbered season, and the user's own AniList list carries
    exactly that entry (Chainsaw Man: Reze-hen, anilist id 171627,
    Fribb-tagged season 0 under Chainsaw Man's tvdb id) — excluding
    season 0 here left it undetected as "already accounted for."
    Verified live: Sonarr's own `seasons` array is present on every
    real series response. show_backfill.py's own AniList-only sweep
    uses this to build the full set of AniList ids Fribb can resolve
    across *every* season of *every* Sonarr-known show — not just
    season 1, which a live run against the user's real library showed
    is genuinely necessary: Fribb resolves Frieren's own season 1 and
    season 2 to two different AniList ids (verified live), so a
    season-1-only check would have missed season 2 as "already
    accounted for" and let it through as an independent, duplicate
    untracked show. Read-only, no service_health record, no commit —
    same side-effect-free contract find_untracked_shows_readonly()
    above already establishes."""
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
            "season_numbers": [season["seasonNumber"] for season in s.get("seasons", [])]
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
            # B.11d — every real season number Sonarr itself lists,
            # season 0 included, so show_backfill.py's own
            # classification can Fribb-resolve every season, not just
            # season 1 — see all_sonarr_series_with_seasons's own
            # docstring for why season 1 alone isn't enough, and why
            # season 0 isn't excluded.
            "season_numbers": [season["seasonNumber"] for season in series.get("seasons", [])]
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


def find_untracked_shows_readonly_by_source(conn) -> dict:
    """Same computation as find_untracked_shows_readonly() below, but
    also reports which sources actually succeeded this pass — B.11e
    follow-up, a real bug found in review (not from a failing test):
    untracked_sweep.py's own pruning needs to tell "genuinely nothing
    untracked here" apart from "couldn't reach this source this time,"
    which a flat list alone can't express — the original version
    conflated the two, so a transient Sonarr/Radarr outage during one
    sweep would have deleted every real finding from that source as
    falsely "resolved." find_untracked_shows_readonly() itself stays a
    thin wrapper around this, unchanged for every existing caller that
    only ever needed the flat list.

    A source with no credentials configured at all reports success
    (`True`) — that's a deliberate, stable "there is nothing to report
    from here," safe to prune findings against. Only a real connection
    failure against a *configured* source reports `False`."""
    cfg = get_current()
    found: list[dict] = []
    reported: set[str] = set()

    if cfg.sonarr_url and cfg.sonarr_api_key:
        try:
            with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
                all_series = client.all_series()
            reported.add("sonarr")
        except sonarr_client.SonarrError:
            all_series = []
        found += _untracked_sonarr_entries(all_series, _known_tvdb_ids(conn))
    else:
        reported.add("sonarr")

    if cfg.radarr_url and cfg.radarr_api_key:
        try:
            with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
                all_movies = client.all_movies()
            reported.add("radarr")
        except radarr_client.RadarrError:
            all_movies = []
        found += _untracked_radarr_entries(all_movies, _known_tmdb_movie_ids(conn))
    else:
        reported.add("radarr")

    return {"entries": found, "reported": reported}


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
    records it properly. Thin wrapper around
    find_untracked_shows_readonly_by_source() above (B.11e follow-up) —
    every existing caller here only ever needed the flat list."""
    return find_untracked_shows_readonly_by_source(conn)["entries"]


def _audit_sonarr_series(
    conn,
    client: "sonarr_client.SonarrClient",
    show_id: str,
    series: dict,
    now: str,
    walk_orphans: bool = True,
) -> tuple[int, list[dict]]:
    """The per-series correction (+ optional orphan-walk) half of
    `_audit_sonarr`'s own loop body, factored out (NEXT_UP.md,
    2026-08-19) so `audit_local_files_for_show` below can run the
    identical correction logic against a single series — one
    implementation either caller drives, not two that could drift.
    `show_id` must already be resolved (the whole-library loop has it
    via `_show_id_for_tvdb`; the single-show path already starts from
    its own `showId` argument).

    `walk_orphans=False` (only `audit_local_files_for_show` passes
    this) skips the filesystem walk entirely — real gap found in
    review: `known_file_paths` only ever gets populated from episodes
    that already have a matching LCARS `episode` row (`if row is None:
    continue`, below), so a real Sonarr file for an episode LCARS
    hasn't fetched yet would be reported as a false-positive orphan.
    Tolerable on the whole-library pass (an operator skims a bulk CLI
    report and can sanity-check it), not on a single keypress a user
    fires from the exact show they're actively inspecting because
    something looks wrong — the scoped mutation's own real point is
    the correction half (Anna Pigeon's stale-path bug), not orphan
    discovery, so this scope skips the walk (and its real filesystem
    I/O) rather than compute a result `audit_local_files_for_show`
    would just discard."""
    episodes_corrected = 0
    orphan_files: list[dict] = []
    episodes = client.episodes(series["id"], include_episode_file=True)

    known_file_paths: set[str] = set()
    for ep in episodes:
        row = conn.execute(
            "SELECT id, available_via_sonarr, file_path_sonarr FROM episode"
            " WHERE show_id = ? AND season = ? AND episode = ?",
            (show_id, ep["seasonNumber"], ep["episodeNumber"]),
        ).fetchone()
        if row is None:
            continue  # not yet fetched into LCARS — A.8's job, not this audit's
        episode_file = ep.get("episodeFile")
        if ep["hasFile"] and episode_file:
            known_file_paths.add(episode_file["path"])
            # 2026-08-19 — real live bug, user-caught (Anna
            # Pigeon): this used to only correct anything when
            # available_via_sonarr itself was about to flip, so
            # a file that stayed `available` throughout (the
            # user moved Sonarr's save location — a root-folder
            # change, still hasFile) never got its now-stale
            # file_path_sonarr touched at all, contradicting
            # this module's own docstring ("corrects... the
            # matching file_path_* directly"). The path itself
            # is just as much "current truth" as the status —
            # compared and corrected the same way.
            if (
                row["available_via_sonarr"] != "available"
                or row["file_path_sonarr"] != episode_file["path"]
            ):
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
    if walk_orphans and series_path:
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
    return episodes_corrected, orphan_files


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
                corrected, orphans = _audit_sonarr_series(conn, client, show_id, series, now)
                episodes_corrected += corrected
                orphan_files.extend(orphans)
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


def _audit_radarr_movie(
    conn, show_id: str, movie: dict, now: str, walk_orphans: bool = True
) -> tuple[int, list[dict]]:
    """The per-movie correction (+ optional orphan-walk) half of
    `_audit_radarr`'s own loop body, factored out the same way
    `_audit_sonarr_series` above is — `audit_local_files_for_show`
    below reuses it verbatim for the single-show scope.

    `walk_orphans=False` skips the filesystem walk, same knob
    `_audit_sonarr_series` has — here purely to avoid real filesystem
    I/O for a result the scoped mutation doesn't return at all, not
    because of that function's own false-positive risk: a movie's
    `known_file_paths` is built straight from Radarr's own current
    `movieFile` (`movie.get(...)` below), not gated behind any LCARS-
    side per-episode fetch state the way a series' per-episode lookup
    is, so there's no equivalent "not yet fetched" false positive to
    worry about here."""
    shows_corrected = 0
    orphan_files: list[dict] = []
    row = conn.execute(
        "SELECT available_via_radarr, file_path_radarr FROM show WHERE id = ?", (show_id,)
    ).fetchone()
    movie_file = movie.get("movieFile")
    known_file_paths: set[str] = set()
    if movie.get("hasFile") and movie_file:
        known_file_paths.add(movie_file["path"])
        # Mirrors _audit_sonarr_series's own 2026-08-19 fix — a movie that
        # stays `available` throughout a Radarr root-folder move needs its
        # stale file_path_radarr corrected too, not just a status flip.
        if (
            row["available_via_radarr"] != "available"
            or row["file_path_radarr"] != movie_file["path"]
        ):
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
    if walk_orphans and movie_path:
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
    return shows_corrected, orphan_files


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
        corrected, orphans = _audit_radarr_movie(conn, show_id, movie, now)
        shows_corrected += corrected
        orphan_files.extend(orphans)

    conn.commit()
    return {
        "shows_corrected": shows_corrected,
        "orphan_files": orphan_files,
        "untracked_shows": untracked_shows,
    }


def audit_local_files_for_show(conn, show_id: str) -> dict:
    """Per-show scope of `audit_local_files()` above — NEXT_UP.md,
    2026-08-19 user ask (see schema.graphql's own
    `auditLocalFilesForShow` docstring for the full rationale): fixing
    one show's stale Sonarr/Radarr path used to mean re-walking the
    whole library. Reuses `_audit_sonarr_series`/`_audit_radarr_movie`
    verbatim for the correction half — the identical per-series/
    per-movie logic the whole-library pass runs, just against one
    show's own single Sonarr/Radarr record
    (`series_by_tvdb_id`/`movie_by_tmdb_id` — a single-record lookup,
    not `all_series()`/`all_movies()`'s full-catalog fetch) instead of
    every tracked show's. Routed by the show's own `media_shape`,
    mirroring `_show_id_for_tmdb_movie`'s own `media_shape = 'movie'`
    split.

    `orphan_files` is always `[]` here — `walk_orphans=False` on both
    calls below, deliberately: real gap caught in review,
    `_audit_sonarr_series`'s own filesystem walk would report a real
    Sonarr file for an episode LCARS hasn't fetched yet as a false-
    positive orphan (see that function's own docstring), tolerable on
    a whole-library CLI report an operator skims, not on a single
    keypress fired from the exact show being actively inspected. The
    scoped mutation's own point is the correction half anyway (Anna
    Pigeon's stale-path bug, not orphan discovery) — orphan/untracked
    discovery stays `auditLocalFiles`'s job.

    Caller (resolvers.py's `resolve_audit_local_files_for_show`) is
    responsible for confirming `show_id` actually exists first
    (`_require_show`) — raising on an unknown show is a GraphQLError
    concern, not this module's; every other "nothing to correct
    against" case here (service not configured, no matching tvdb/tmdb
    link yet, Sonarr/Radarr doesn't know this id) returns the same
    all-zero/empty result rather than erroring, same report-only
    posture the whole-library pass already has for an inaccessible
    filesystem mount. `untracked_shows` is always `[]` too — an
    already-tracked show can never also be its own untracked
    finding."""
    empty = {
        "episodes_corrected": 0,
        "shows_corrected": 0,
        "orphan_files": [],
        "untracked_shows": [],
    }
    show = conn.execute("SELECT media_shape FROM show WHERE id = ?", (show_id,)).fetchone()
    if show is None:
        return empty  # resolver already validated existence — defensive only
    cfg = get_current()
    now = util.now_utc_iso()

    if show["media_shape"] == "movie":
        if not cfg.radarr_url or not cfg.radarr_api_key:
            return empty
        tmdb_row = conn.execute(
            "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tmdb'",
            (show_id,),
        ).fetchone()
        if tmdb_row is None:
            return empty
        try:
            with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
                movie = client.movie_by_tmdb_id(int(tmdb_row["external_id"]))
        except radarr_client.RadarrError as e:
            logger.exception("Radarr single-show audit failed — show %s", show_id)
            service_health.record_failure(conn, "radarr", str(e))
            conn.commit()
            return empty
        service_health.record_success(conn, "radarr")
        if movie is None:
            conn.commit()
            return empty
        shows_corrected, _orphans = _audit_radarr_movie(
            conn, show_id, movie, now, walk_orphans=False
        )
        conn.commit()
        return {
            "episodes_corrected": 0,
            "shows_corrected": shows_corrected,
            "orphan_files": [],
            "untracked_shows": [],
        }

    if not cfg.sonarr_url or not cfg.sonarr_api_key:
        return empty
    tvdb_row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
        (show_id,),
    ).fetchone()
    if tvdb_row is None:
        return empty
    try:
        with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
            series = client.series_by_tvdb_id(int(tvdb_row["external_id"]))
            if series is None:
                episodes_corrected = 0
            else:
                # Must run inside the `with` — _audit_sonarr_series calls
                # client.episodes() itself, and the client is closed the
                # moment this block exits (SonarrClient.__exit__).
                episodes_corrected, _orphans = _audit_sonarr_series(
                    conn, client, show_id, series, now, walk_orphans=False
                )
    except sonarr_client.SonarrError as e:
        logger.exception("Sonarr single-show audit failed — show %s", show_id)
        service_health.record_failure(conn, "sonarr", str(e))
        conn.commit()
        return empty
    service_health.record_success(conn, "sonarr")
    conn.commit()
    return {
        "episodes_corrected": episodes_corrected,
        "shows_corrected": 0,
        "orphan_files": [],
        "untracked_shows": [],
    }
