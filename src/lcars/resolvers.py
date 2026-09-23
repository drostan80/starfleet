"""Resolvers — BUILD_PLAN.md A.3, built out slice by slice, in §5's own
document order (same order A.1/A.2 were built in): Show/Episode/
WatchEvent core -> id-mapper/PendingReview -> the rest of
watch_event/episode-override/external-link mutations -> now
ShowServicePresence (§5.4) and Person/Studio/CastCredit/StudioCredit
(§5.8) — all query-only, no mutations exist for these (externally-
populated metadata, not client-created, per §5.8's own description).
The rest of the schema (Franchise/tags/FilterPreset/deletion/
export-import/stats/search/...) is still unbound — a client querying
those fields gets a clear GraphQL error (missing resolver / null on a
non-null field), not silently wrong data. Expanding further is later
A.3 work, tracked in BUILD_PLAN.md, not a hidden gap.

Field resolution: `convert_names_case=True` (passed to
make_executable_schema in server.py) handles camelCase-GraphQL-field to
snake_case-dict-key mapping automatically for every plain scalar field —
the ObjectType bindings below only exist for fields that need real
logic (relationships, computed values, paginated connections).
"""

import json
import logging
import urllib.parse

from ariadne import EnumType, MutationType, ObjectType, QueryType, SubscriptionType
from graphql import GraphQLError

from lcars import (
    anidb,
    anilist_client,
    animeschedule,
    art,
    availability,
    browse,
    browse_tmdb,
    config,
    db,
    episode_movie_link,
    events,
    export_import,
    fribb,
    fuzzy,
    identity_mismatch,
    ids,
    local_audit,
    mal_client,
    mal_reconcile,
    metadata,
    pagination,
    pending_review,
    radarr_client,
    score_sync,
    season_mapping,
    season_ranges,
    service_health,
    service_presence,
    show_backfill,
    show_merge,
    shows,
    sonarr_client,
    tvdb_backfill,
    untracked_sweep,
    util,
    watch_reconcile,
)

log = logging.getLogger(__name__)

query = QueryType()
mutation = MutationType()
subscription = SubscriptionType()
show_type = ObjectType("Show")
episode_type = ObjectType("Episode")
watch_event_type = ObjectType("WatchEvent")
show_external_id_type = ObjectType("ShowExternalId")
season_type = ObjectType("Season")
episode_numbering_mapping_type = ObjectType("EpisodeNumberingMapping")
episode_movie_link_type = ObjectType("EpisodeMovieLink")
pending_review_type = ObjectType("PendingReview")
status_change_type = ObjectType("StatusChange")
score_change_type = ObjectType("ScoreChange")
air_date_change_type = ObjectType("AirDateChange")
tracked_change_type = ObjectType("TrackedChange")
show_service_presence_type = ObjectType("ShowServicePresence")
art_asset_type = ObjectType("ArtAsset")
person_type = ObjectType("Person")
studio_type = ObjectType("Studio")
cast_credit_type = ObjectType("CastCredit")
studio_credit_type = ObjectType("StudioCredit")
season_external_id_type = ObjectType("SeasonExternalId")
episode_external_id_type = ObjectType("EpisodeExternalId")
franchise_type = ObjectType("Franchise")
franchise_entry_type = ObjectType("FranchiseEntry")
next_up_override_type = ObjectType("NextUpOverride")
next_up_entry_type = ObjectType("NextUpEntry")
tag_type = ObjectType("Tag")
show_merge_type = ObjectType("ShowMerge")


def _enum(name: str, *values: str) -> EnumType:
    """SDL enum values are UPPER_SNAKE; DB-stored values are the exact
    lowercase equivalent in every case here (verified against every
    CHECK constraint in the A.1/A.1-addendum migrations) — so a plain
    `.upper()` derivation is correct, not a coincidence to special-case."""
    return EnumType(name, {v.upper(): v for v in values})


ENUMS = [
    _enum("MediaShape", "episodic", "movie"),
    _enum("TrackingSpace", "tv", "anime"),
    _enum("ShowStatus", "watching", "planned", "paused", "completed", "dropped", "skipped"),
    _enum("EpisodeKind", "regular", "special", "ova", "bonus_movie"),
    _enum(
        "AirDateSource",
        "sonarr", "anilist", "animeschedule", "manual",
        "tvmaze", "anidb", "syoboi",
    ),
    _enum("EpisodeState", "unwatched", "watched", "skipped"),
    _enum("PersonRoleType", "voice_actor", "actor", "staff"),
    _enum("StudioRoleType", "studio", "publisher", "network"),
    _enum("PrimaryTitle", "romaji", "english", "native"),
    _enum("SeasonSource", "fribb", "manual", "unmatched"),
    _enum("NumberingScheme", "absolute", "season_episode"),
    _enum("NumberingSource", "sonarr", "anilist", "manual", "unmatched"),
    _enum("EpisodeMovieLinkSource", "tmdb_match", "manual", "unmatched"),
    _enum("ResolvedByClient", "data", "holodeck", "captains_log"),
    _enum("AvailabilityStatus", "unavailable", "downloading", "available"),
    _enum("TrackedService", "sonarr", "radarr", "anilist", "animeschedule", "mal"),
    # "unknown" is never a real DB value (service_health's own CHECK
    # constraint only allows ok/unreachable) — included here anyway
    # since it's a real value service_health.get_all() can return
    # (synthesized for a never-contacted service), and this mapping is
    # purely GraphQL<->Python, independent of what the DB will store.
    _enum("ServiceHealthStatus", "ok", "unreachable", "unknown"),
    _enum("ArtKind", "poster", "banner", "background"),
]

BINDABLES = [
    query,
    mutation,
    subscription,
    show_type,
    episode_type,
    watch_event_type,
    show_external_id_type,
    season_type,
    season_external_id_type,
    episode_external_id_type,
    episode_numbering_mapping_type,
    episode_movie_link_type,
    pending_review_type,
    status_change_type,
    score_change_type,
    air_date_change_type,
    tracked_change_type,
    show_service_presence_type,
    art_asset_type,
    person_type,
    studio_type,
    cast_credit_type,
    studio_credit_type,
    franchise_type,
    franchise_entry_type,
    next_up_override_type,
    next_up_entry_type,
    tag_type,
    show_merge_type,
    *ENUMS,
    util.datetime_scalar,
]

# §5.6 — resolving a review is one of the three interactive clients' own
# job (Data/Holodeck/Captain's Log); automated processes (sonarr_sync,
# anilist_sync, ...) can *create* pending_review entries via other
# mutations but never resolve one. Mirrors the DB's own CHECK constraint
# (migrations/versions/7196ca889757_*.py) — checked here first too, for a
# clean GraphQLError instead of a raw sqlite3.IntegrityError.
RESOLVING_CLIENTS = {"data", "holodeck", "captains_log"}


def require_client(info) -> str:
    """§5.7 addendum (2026-08-08): every history/pending_review-writing
    mutation requires the X-LCARS-Client header (set into context by the
    ASGI app, server.py) — a hard requirement, not a default-if-missing,
    per that section's own reasoning."""
    client = (info.context or {}).get("client")
    if not client:
        raise GraphQLError("the X-LCARS-Client header is required for this mutation")
    return client


def _get_show(conn, show_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM show WHERE id = ?", (show_id,)).fetchone()
    return dict(row) if row else None


def _require_show(conn, show_id: str) -> dict:
    show = _get_show(conn, show_id)
    if show is None:
        raise GraphQLError(f"no such show: {show_id}")
    return show


def _get_episode(conn, episode_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM episode WHERE id = ?", (episode_id,)).fetchone()
    return dict(row) if row else None


def _require_episode(conn, episode_id: str) -> dict:
    episode = _get_episode(conn, episode_id)
    if episode is None:
        raise GraphQLError(f"no such episode: {episode_id}")
    return episode


# -- AniList push (§6.1/§6.8, A.9) -------------------------------------------
#
# LCARS's own AniList OAuth session (config.anilist_access_token, `lcars
# anilist-login`) — a genuinely separate concern from Data's own narrow
# episode-watch-status-only direct write (§6.8), confirmed 2026-08-08: score/
# status push "is not... watch status... it goes through lcars, lcars pushes
# it". Best-effort throughout, same philosophy as A.8's metadata fetch: not
# yet authenticated is treated the same as "not configured" (silent no-op,
# not a failure); an actual push error opens/extends a pending_review entry
# rather than raising, so a score/status write itself never fails just
# because AniList happened to be unreachable at that moment.

_STATUS_TO_ANILIST = {
    "watching": "CURRENT",
    "planned": "PLANNING",
    "paused": "PAUSED",
    "completed": "COMPLETED",
    "dropped": "DROPPED",
    # "skipped" deliberately absent — no AniList equivalent; push functions
    # use .get() and early-return on None so a skipped status is silently
    # a no-op, matching the "no external push" design for SKIP.
    # deliberately no REPEATING mapping — §6.8/§6.9: setStatus/_push_show_status
    # still never auto-pushes AniList's REPEATING status (LCARS's own
    # status enum has no rewatch-specific value to map from anyway), and that
    # stays true even after markSeasonRewatch (2026-08-16, todo.md) — that's
    # a separate, explicit, caller-supplied-count mutation, not something
    # this automatic status-change push ever infers on its own.
}


def _push_season_score(conn, season: dict, fallback_show_score) -> None:
    """One season's own effective score (its own `season.score` when
    set, else the show's `score` — resolved directly with the user,
    2026-08-08, A.9: "push the score as per mapping if anilist score
    exist for season, score at that season level") to its own AniList
    `anilist_id`. No-ops on an unlinked season or before `lcars
    anilist-login` has ever been run."""
    if season["anilist_id"] is None:
        return
    cfg = config.get_current()
    if not cfg.anilist_access_token:
        return
    effective_score = season["score"] if season["score"] is not None else fallback_show_score
    if effective_score is None:
        return
    try:
        anilist_client.save_media_list_entry(
            cfg.anilist_access_token, season["anilist_id"], score=effective_score * 5
        )
    except anilist_client.AniListError as e:
        pending_review.open_or_extend(
            conn, "season", season["id"], "anilist_push", "anilist", None, str(e)
        )


def _push_show_score(conn, show_id: str, show_score) -> None:
    """Every one of the show's linked seasons — each resolves its own
    effective score via _push_season_score's own fallback rule."""
    seasons = conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND anilist_id IS NOT NULL", (show_id,)
    ).fetchall()
    for season in seasons:
        _push_season_score(conn, dict(season), show_score)


def _push_show_status(conn, show_id: str, status: str) -> None:
    """Push status to AniList for every linked season.  Each season uses
    its own per-season status when set (2.1c), falling back to the
    show-level status passed in."""
    cfg = config.get_current()
    if not cfg.anilist_access_token:
        return
    seasons = conn.execute(
        "SELECT id, anilist_id, status AS season_status"
        " FROM season WHERE show_id = ? AND anilist_id IS NOT NULL",
        (show_id,),
    ).fetchall()
    for season in seasons:
        effective = season["season_status"] or status
        anilist_status = _STATUS_TO_ANILIST.get(effective)
        if anilist_status is None:
            continue  # 'skipped' — no AniList equivalent
        try:
            anilist_client.save_media_list_entry(
                cfg.anilist_access_token, season["anilist_id"], status=anilist_status
            )
        except anilist_client.AniListError as e:
            pending_review.open_or_extend(
                conn, "season", season["id"], "anilist_push", "anilist", None, str(e)
            )


def _push_season_status(conn, season: dict, status: str) -> None:
    """Per-season AniList status push (2.1c) — pushes this one season's
    own status, rather than fanning out the show-level status to all
    seasons as _push_show_status does.  Used by setSeasonStatus."""
    if season.get("anilist_id") is None:
        return
    cfg = config.get_current()
    if not cfg.anilist_access_token:
        return
    anilist_status = _STATUS_TO_ANILIST.get(status)
    if anilist_status is None:
        return  # 'skipped' — no AniList equivalent
    try:
        anilist_client.save_media_list_entry(
            cfg.anilist_access_token, season["anilist_id"], status=anilist_status
        )
    except anilist_client.AniListError as e:
        pending_review.open_or_extend(
            conn, "season", season["id"], "anilist_push", "anilist", None, str(e)
        )


def _push_mal_season_status(conn, season: dict, status: str) -> None:
    """Per-season MAL status push (2.1c) — mirrors _push_season_status
    for MAL, using mal_id."""
    if season.get("mal_id") is None:
        return
    cfg = config.get_current()
    if not cfg.mal_access_token:
        return
    mal_status = _STATUS_TO_MAL.get(status)
    if mal_status is None:
        return  # 'skipped' — no MAL equivalent
    try:
        mal_client.update_my_list_status(
            cfg.mal_access_token, season["mal_id"], status=mal_status
        )
    except mal_client.MALError as e:
        pending_review.open_or_extend(
            conn, "season", season["id"], "mal_push", "mal", None, str(e)
        )


def _push_season_started_at(conn, season_id: str, anilist_id: int | None, started_at: str) -> None:
    """AniList push half of `_stamp_season_started_at` below — the
    write-mirror gap logged in todo.md (archive/todo.md:1013):
    `SaveMediaListEntry`'s `startedAt` takes a `FuzzyDateInput`, a
    different shape from every other param this write-mirror pushes,
    which is why it was left unwired when started_at/completed_at were
    first built (2026-08-16) even though the local column was. Same
    no-op-before-login/no-op-unlinked/best-effort/pending_review-on-
    failure shape as `_push_season_score`."""
    if anilist_id is None:
        return
    cfg = config.get_current()
    if not cfg.anilist_access_token:
        return
    try:
        anilist_client.save_media_list_entry(
            cfg.anilist_access_token, anilist_id, started_at=started_at
        )
    except anilist_client.AniListError as e:
        pending_review.open_or_extend(
            conn, "season", season_id, "anilist_push", "anilist", None, str(e)
        )


def _stamp_season_started_at(
    conn, show_id: str, season_number: int | None, watched_at: str, push: bool = True
) -> None:
    """Write-mirror function set, todo.md (2026-08-16) — LCARS's own
    local `started_at` capture, the date of this season's first-ever
    watched episode, per the user's own rule; also pushes it on to
    AniList (`_push_season_started_at` above) now that todo.md's
    FuzzyDateInput gap is closed. Written once (`WHERE started_at IS
    NULL`, checked here rather than folded into the UPDATE so the
    already-fetched row can be reused for the push) so a later
    deleteWatchEvent/re-mark of that same episode never moves the
    date, and a re-mark never re-pushes either. No-ops for a movie
    watch event (season is None, movies have no season row) or a
    season LCARS doesn't have a row for yet.

    `push=False` (only `_bulk_mark_all_aired_episodes_watched` passes
    this) covers a real corruption risk found in review: that caller's
    `watched_at` is a synthesized "now" (the moment a show got manually
    marked completed today), not a real historical watch date — pushing
    that as `startedAt` would overwrite AniList's own, possibly-genuine
    value with today's date for every season that had none locally yet.
    LCARS still captures its own local value either way (this function's
    entire local-write half is unconditional) — only the AniList push
    is suppressed for this one synthetic-date caller.

    Deliberately scoped to LCARS-originated watch mutations only
    (addWatchEvent/markSeasonWatched/markEpisodeRangeWatched, each
    calling this below) — watch_reconcile.py's own AniList-sourced
    backfill path is a separate, narrower-scoped module (B.15, "not
    the full generalized... architecture") and isn't touched here;
    logged as a known boundary in todo.md, not silently missed."""
    if season_number is None:
        return
    season = conn.execute(
        "SELECT id, anilist_id, started_at FROM season WHERE show_id = ? AND season_number = ?",
        (show_id, season_number),
    ).fetchone()
    if season is None or season["started_at"] is not None:
        return
    conn.execute(
        "UPDATE season SET started_at = ?, updated_at = ? WHERE id = ?",
        (watched_at, util.now_utc_iso(), season["id"]),
    )
    if push:
        _push_season_started_at(conn, season["id"], season["anilist_id"], watched_at)


def _push_season_completed_at(
    conn, season_id: str, anilist_id: int | None, completed_at: str
) -> None:
    """AniList push half of `_stamp_completed_at_if_highest_season`/
    `_try_complete_season` below — same todo.md FuzzyDateInput gap
    `_push_season_started_at` closes, for the other of the two fields.
    Same no-op-before-login/no-op-unlinked/best-effort/pending_review-
    on-failure shape."""
    if anilist_id is None:
        return
    cfg = config.get_current()
    if not cfg.anilist_access_token:
        return
    try:
        anilist_client.save_media_list_entry(
            cfg.anilist_access_token, anilist_id, completed_at=completed_at
        )
    except anilist_client.AniListError as e:
        pending_review.open_or_extend(
            conn, "season", season_id, "anilist_push", "anilist", None, str(e)
        )


def _stamp_completed_at_if_highest_season(conn, show_id: str, new_status: str) -> None:
    """Write-mirror function set, todo.md (2026-08-16) — the date
    `show.status` became 'completed', per the user's own rule. Written
    onto this show's highest-numbered season only, any-status/
    any-link (not restricted to AniList-linked seasons — a different,
    narrower "highest linked season" concept watch_reconcile.py's own
    reconcile_watch_progress uses for a genuinely different purpose,
    deciding which season's AniList status is authoritative to *read*
    from; the local write itself needs no AniList entry to pick a
    target, but the push below — `_push_season_completed_at` — is a
    genuine no-op on an unlinked highest season). `show.status` is
    show-wide but `completed_at` is per-season (§5.5's split-cour
    model), so only the season that's actually finishing gets stamped
    — a multi-season show's earlier, already-finished seasons keep
    whatever completed_at they already have, untouched. Written once
    (`WHERE completed_at IS NULL`); a later drop-then-complete-again
    doesn't move an already-recorded date or re-push it."""
    if new_status != "completed":
        return
    highest = conn.execute(
        "SELECT id, anilist_id, completed_at FROM season WHERE show_id = ?"
        " ORDER BY season_number DESC LIMIT 1",
        (show_id,),
    ).fetchone()
    if highest is None or highest["completed_at"] is not None:
        return
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE season SET completed_at = ?, updated_at = ? WHERE id = ?",
        (now, now, highest["id"]),
    )
    _push_season_completed_at(conn, highest["id"], highest["anilist_id"], now)


# -- bidirectional completion auto-sync (write-mirror, todo.md 2026-08-16) --
#
# User's own rule, verbatim: "season are marked complete when all episodes
# are watched, same for show, complete when all seasons are marked watched,
# if a new season is added then move back to watching." Plus: a skipped
# episode counts as done for this purpose (stays 'skipped' in the DB,
# never rewritten to 'watched' — same distinction _compute_season_episode_
# progress already draws); and a still-airing show needs a warning/confirm
# gate before a *manual* completion bulk-marks episodes, per the user's own
# "y/n" framing (setStatus's own confirmed argument, below).
#
# Two independent triggers, deliberately not unified into one function:
#   - Forward (episode watched/skipped -> season complete -> show complete):
#     _try_complete_season/_try_complete_show, called from every mutation
#     that changes episode.state.
#   - Reverse (show set completed -> bulk-mark aired episodes watched):
#     _bulk_mark_all_aired_episodes_watched, called from setStatus only.
# The forward direction naturally never fires early on an airing show (the
# "last" episode isn't actually last yet), so the warning/confirm gate is
# only needed on the reverse/manual path.


def _season_still_airing(conn, show_id: str, season_number: int) -> bool:
    """Season-scoped version of `_show_is_airing` above — that check is
    whole-show, which would wrongly block a finished earlier season
    from ever completing just because a later, still-airing season
    exists under the same show (an ordinary, common case for an
    ongoing multi-cour franchise, e.g. Ascendance of a Bookworm this
    same week)."""
    row = conn.execute(
        "SELECT 1 FROM episode"
        " WHERE show_id = ? AND season = ? AND (air_date_utc IS NULL OR air_date_utc > ?) LIMIT 1",
        (show_id, season_number, util.now_utc_iso()),
    ).fetchone()
    return row is not None


def _try_complete_season(conn, show_id: str, season_number: int, completed_at: str) -> bool:
    """Stamps this one season's `completed_at` if every one of its
    episodes is 'watched' or 'skipped', it has at least one episode
    row (an empty/never-fetched season is never "complete"), it isn't
    still airing (season-scoped, above), and it isn't already stamped
    (write-once, same guard `_stamp_completed_at_if_highest_season`
    already uses). Returns whether it actually stamped anything, so a
    caller can tell "already complete" apart from "just completed" if
    it ever needs to. Also pushes the new completed_at on to AniList
    (`_push_season_completed_at`) whenever it actually stamps — this is
    the forward/auto-complete path, so unlike
    `_stamp_completed_at_if_highest_season` (setStatus's manual path)
    it fires for every completing season, not just the highest one."""
    season = conn.execute(
        "SELECT id, anilist_id, completed_at, status FROM season"
        " WHERE show_id = ? AND season_number = ?",
        (show_id, season_number),
    ).fetchone()
    if season is None or season["completed_at"] is not None:
        return False
    # Don't override a deliberate PAUSED/DROPPED set by the user
    if season["status"] in ("paused", "dropped"):
        return False
    episodes = conn.execute(
        "SELECT state FROM episode WHERE show_id = ? AND season = ?", (show_id, season_number)
    ).fetchall()
    if not episodes or any(e["state"] not in ("watched", "skipped") for e in episodes):
        return False
    if _season_still_airing(conn, show_id, season_number):
        return False
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE season SET status = 'completed', completed_at = ?, updated_at = ? WHERE id = ?",
        (completed_at, now, season["id"]),
    )
    _push_season_completed_at(conn, season["id"], season["anilist_id"], completed_at)
    return True


def _try_complete_show(conn, show_id: str, completed_at: str) -> None:
    """DEPRECATED (2.1c) — superseded by _recompute_show_status +
    _compute_show_status.  Retained as reference; no callers remain.

    Original: The show-wide half: every episode across every season is
    'watched'/'skipped', there's at least one episode row, and nothing
    more is expected (`_show_is_airing`, whole-show — deliberately
    the unscoped check here, unlike `_try_complete_season`'s own: the
    *show* genuinely isn't done if anything anywhere is still airing).
    Tests episode state directly rather than "every season has
    completed_at set" — every pre-existing season in production has
    `completed_at IS NULL` (this field is brand new, no historical
    backfill), so that test would never fire for a single real show
    that existed before today. Stamps every season it can first (so a
    multi-season show's own seasons genuinely reflect "marked complete"
    too, per the user's literal framing), then promotes `show.status`
    — its own real `status_change` row and AniList/MAL push, same
    shape `setStatus` itself uses, since nothing else in this code path
    goes through that resolver. No-ops entirely if the show is already
    `completed` (guards against a duplicate status_change on a second,
    harmless trigger — e.g. marking an already-fully-watched season's
    stray rewatch episode) or has zero episode rows at all."""
    show = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
    if show is None or show["status"] == "completed":
        return
    has_episodes = conn.execute(
        "SELECT 1 FROM episode WHERE show_id = ? LIMIT 1", (show_id,)
    ).fetchone()
    if has_episodes is None:
        return
    any_unwatched = conn.execute(
        "SELECT 1 FROM episode WHERE show_id = ? AND state = 'unwatched' LIMIT 1", (show_id,)
    ).fetchone()
    if any_unwatched is not None:
        return
    if _show_is_airing(conn, show_id):
        return
    for season_row in conn.execute(
        "SELECT season_number FROM season WHERE show_id = ?", (show_id,)
    ).fetchall():
        _try_complete_season(conn, show_id, season_row["season_number"], completed_at)
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE show SET status = 'completed', updated_at = ? WHERE id = ?", (now, show_id)
    )
    conn.execute(
        "INSERT INTO status_change"
        " (id, show_id, previous_status, new_status, changed_at, changed_by)"
        " VALUES (?, ?, ?, 'completed', ?, 'auto_complete')",
        (ids.generate_id(conn, "c"), show_id, show["status"], now),
    )
    _push_show_status(conn, show_id, "completed")
    _push_mal_show_status(conn, show_id, "completed")


def _compute_show_status(conn, show_id: str) -> str | None:
    """Derive what show.status should be from episode state and season
    statuses.  Returns the status string, or None if it can't determine
    one (no seasons / no episodes → caller should keep current status).

    Rules (user's own, 2026-09-02):
    1. COMPLETED — episode-derived: every episode across every season is
       'watched' or 'skipped', at least one episode exists, and the show
       is not airing (same preconditions as _try_complete_show).
    2. Otherwise — season-status-derived: the highest-numbered season's
       status, with one exception: if the highest season is PLANNED and
       at least one other season exists, the show is WATCHING (the user
       is watching the show overall, just planning the next season).

    **2026-09-20 fix**: Rule 2 alone had no defense against a season's own
    `status` being wrong — found live via `anilist_reconcile`/
    `mal_reconcile` repeatedly writing a remote-reported "completed" onto
    a season that genuinely still had real, already-aired, unwatched
    episodes (both platforms can flip a currently-airing entry's own
    status as their episode-count metadata catches up). Rule 1 already
    guards the *episode-derived* completion this way; Rule 2 needs the
    same guard for the *season-status-derived* path, or a stale/wrong
    season.status can claim completion Rule 1 would never have granted —
    and worse, a resulting show.status='completed' auto-marks every aired
    episode watched (_bulk_mark_all_aired_episodes_watched), fabricating
    watch history for episodes the user hasn't actually seen."""
    # Gather non-special seasons (season 0 = specials, excluded from
    # status derivation — specials don't represent show progress).
    seasons = conn.execute(
        "SELECT season_number, status FROM season"
        " WHERE show_id = ? AND season_number > 0"
        " ORDER BY season_number",
        (show_id,),
    ).fetchall()
    if not seasons:
        return None

    # Rule 1: episode-derived COMPLETED (single authority for completion)
    has_episodes = conn.execute(
        "SELECT 1 FROM episode WHERE show_id = ? LIMIT 1", (show_id,)
    ).fetchone()
    if has_episodes is not None:
        any_unwatched = conn.execute(
            "SELECT 1 FROM episode WHERE show_id = ? AND state = 'unwatched' LIMIT 1",
            (show_id,),
        ).fetchone()
        if any_unwatched is None and not _show_is_airing(conn, show_id):
            return "completed"

    # Rule 2: highest season's status, with PLANNED exception
    highest = seasons[-1]
    highest_status = highest["status"] or "planned"  # null = inherits, treat as planned
    if highest_status == "planned" and len(seasons) > 1:
        return "watching"
    if highest_status == "completed":
        real_gap = conn.execute(
            "SELECT 1 FROM episode WHERE show_id = ? AND season = ?"
            " AND state = 'unwatched' AND air_date_utc IS NOT NULL AND air_date_utc <= ?"
            " LIMIT 1",
            (show_id, highest["season_number"], util.now_utc_iso()),
        ).fetchone()
        if real_gap is not None:
            return "watching"
    return highest_status


def _recompute_show_status(
    conn, show_id: str, changed_by: str, *, _from_bulk_mark: bool = False, _skip_push: bool = False
) -> None:
    """Derive show.status from seasons/episodes and apply side effects
    when it changes.  The single place that updates show.status after
    the initial setStatus/setSeasonStatus write — all callers converge
    here instead of maintaining separate writers.

    _from_bulk_mark: True when called from inside _bulk_mark_all_aired_
    episodes_watched's call tree, to prevent the cycle: recompute →
    COMPLETED → bulk-mark → recompute.  When True, skips the bulk-mark
    and per-season _try_complete_season calls (the caller already handled
    episode state).

    _skip_push: True when called from watch_reconcile.py's own
    _apply_remote_list (2026-09-20) — a real incident, not theoretical:
    that caller already does its own one-directional "hub" push onward
    to the *other* service (never back to the one the change came from),
    the correct behavior for a remote-sourced change. Without this flag,
    this function's own unconditional _push_show_status/
    _push_mal_show_status ran *again* on top of that — a pointless
    self-push back to the service the status was just read from, plus a
    genuine duplicate push to the other one. On the first real run after
    this module's status-derivation fix landed, a backlog of shows
    corrected all at once, and each one paid for 2-3x the throttled
    AniList calls it needed (anilist_client's rate limiter sleeps
    synchronously, blocking LCARS's single worker) — enough to make the
    whole server unresponsive for several minutes, not just this feature.
    Every other caller (setSeasonStatus, addWatchEvent, etc.) still needs
    the push here, since none of them push anywhere themselves."""
    show = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
    if show is None:
        return
    old_status = show["status"]
    if old_status == "skipped":
        return  # tombstone — never overwrite via derivation
    new_status = _compute_show_status(conn, show_id)
    if new_status is None or new_status == old_status:
        return

    now = util.now_utc_iso()
    conn.execute(
        "UPDATE show SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, show_id),
    )
    conn.execute(
        "INSERT INTO status_change"
        " (id, show_id, previous_status, new_status, changed_at, changed_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ids.generate_id(conn, "c"), show_id, old_status, new_status, now, changed_by),
    )
    if not _skip_push:
        _push_show_status(conn, show_id, new_status)
        _push_mal_show_status(conn, show_id, new_status)

    if new_status == "completed" and not _from_bulk_mark:
        _stamp_completed_at_if_highest_season(conn, show_id, new_status)
        _bulk_mark_all_aired_episodes_watched(conn, show_id)
        for season_row in conn.execute(
            "SELECT season_number FROM season WHERE show_id = ?", (show_id,)
        ).fetchall():
            _try_complete_season(conn, show_id, season_row["season_number"], now)


def _bulk_mark_all_aired_episodes_watched(conn, show_id: str) -> None:
    """Reverse direction — user's own rule: "if show is marked as
    completed then mark all episodes watched." Only touches episodes
    that have genuinely aired (a real, past `air_date_utc`) and are
    currently `unwatched` — never an already-`skipped` episode (stays
    skipped, counts as done for completion purposes without being
    rewritten — same rule `_try_complete_season` applies) and never an
    episode with no air date or a future one, unconditionally,
    regardless of `confirmed`: marking something "watched" that hasn't
    aired would be false no matter how the caller answered the warning
    prompt (setStatus's own `confirmed` argument only gates *whether
    the status change itself proceeds*, not what this function is
    willing to mark). Pushes each touched season's progress, same as
    every other real watch mutation — an auto-completed show whose
    AniList entry still shows old progress would be its own new
    inconsistency otherwise. `started_at` is captured locally
    (`_stamp_season_started_at`) but deliberately NOT pushed to AniList
    from here (`push=False`) — `now` is a synthesized "marked completed
    today" timestamp, not a real historical watch date, and pushing it
    would silently overwrite AniList's own possibly-genuine `startedAt`
    with today's date for every season that had none locally yet.

    No equivalent suppression for `completed_at`, deliberately: this
    same `now` (passed on as `completed_at` to the `_try_complete_season`
    calls setStatus's own caller loop makes right after this function
    returns) genuinely *is* the correct value there, not a guess — the
    migration's own definition is "the date show.status became
    'completed'", and that's exactly what just happened, this instant.
    `started_at` and `completed_at` differ in kind here: one estimates a
    past event this function has no real record of, the other stamps
    the event this function's own caller is causing right now."""
    now = util.now_utc_iso()
    to_mark = conn.execute(
        "SELECT id, season, episode FROM episode"
        " WHERE show_id = ? AND state = 'unwatched'"
        " AND air_date_utc IS NOT NULL AND air_date_utc <= ?",
        (show_id, now),
    ).fetchall()
    touched_seasons = set()
    for ep in to_mark:
        conn.execute(
            "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ids.generate_id(conn, "w"), show_id, ep["season"], ep["episode"], now, now),
        )
        conn.execute(
            "UPDATE episode SET state = 'watched', updated_at = ? WHERE id = ?", (now, ep["id"])
        )
        touched_seasons.add(ep["season"])
    for season_number in touched_seasons:
        _stamp_season_started_at(conn, show_id, season_number, now, push=False)
        _push_show_episode_progress(conn, show_id, season_number)
        _push_mal_show_episode_progress(conn, show_id, season_number)  # MAL mirror


def _reopen_show_if_completed(conn, show_id: str, changed_by: str) -> None:
    """Forward-direction counterpart to the above, triggered from the
    *addition* of a new season rather than a watch event: "if a new
    season is added then move back to watching," user's own words.
    Wired into `setSeasonMapping`'s own new-season-row branch only —
    checked the other four `INSERT INTO season` call sites
    (`season_mapping.py` x2, `metadata.py` x2, both automated
    Sonarr/Fribb discovery paths) and deliberately left them
    un-hooked: an automated background sweep silently flipping
    `show.status` and pushing to AniList is a materially different,
    riskier kind of unattended write than the client-driven mutations
    this whole write-mirror already covers, and isn't what was asked
    for. Logged as a known boundary, same treatment as `started_at`'s
    own watch_reconcile.py boundary above."""
    show = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
    if show is None or show["status"] != "completed":
        return
    now = util.now_utc_iso()
    conn.execute("UPDATE show SET status = 'watching', updated_at = ? WHERE id = ?", (now, show_id))
    conn.execute(
        "INSERT INTO status_change"
        " (id, show_id, previous_status, new_status, changed_at, changed_by)"
        " VALUES (?, ?, 'completed', 'watching', ?, ?)",
        (ids.generate_id(conn, "c"), show_id, now, changed_by),
    )
    _push_show_status(conn, show_id, "watching")
    _push_mal_show_status(conn, show_id, "watching")


def _push_season_rewatch(conn, season: dict, repeat_count: int) -> None:
    """Write-mirror function set, todo.md (2026-08-16) — the "build it
    anyway" rewatch function: pushes AniList's REPEATING status plus
    `repeat` for this one season's own AniList entry. Purely a push,
    given an explicit count by its caller — LCARS has no local
    rewatch-count column of its own yet to derive this from. Checked
    what the read side does with REPEATING before shipping this, not
    assumed: `_ANILIST_TO_STATUS` (watch_reconcile.py) maps it to
    `watching`, so `reconcile_watch_progress`'s next poll harmlessly
    confirms the show as watching (correct — a rewatch genuinely is
    active watching), never drops/loses the status. Same
    no-op-before-login/no-op-unlinked and best-effort/
    pending_review-on-failure shape as _push_season_score."""
    if season["anilist_id"] is None:
        return
    cfg = config.get_current()
    if not cfg.anilist_access_token:
        return
    try:
        anilist_client.save_media_list_entry(
            cfg.anilist_access_token, season["anilist_id"], status="REPEATING", repeat=repeat_count
        )
    except anilist_client.AniListError as e:
        pending_review.open_or_extend(
            conn, "season", season["id"], "anilist_push", "anilist", None, str(e)
        )


def _compute_season_episode_progress(conn, show_id: str, season_number: int) -> int:
    """AniList's `progress` is one scalar high-water mark — confirmed
    live via schema introspection, 2026-08-16: `MediaList` has no
    per-episode field at all, `progress: Int` is the *only*
    representation of "how far watched" AniList's data model can hold.
    Matches that model exactly: the highest episode number marked
    'watched' or 'skipped' in this season, not required to be
    contiguous from episode 1 (skip counts as passed, not watched,
    matching watch_reconcile.py's own read-side "not the same thing as
    never watched" distinction).

    User's own call, 2026-08-16, after confirming the above live:
    "realistically I do not jump episodes so why not, for now, adopt
    the anilist schema" — a genuine out-of-order watch (episode 1 and
    3 watched, 2 not) pushes 3, and reconcile_watch_progress's own
    read-back would then treat episode 2 as watched too on its next
    poll, writing a watch_event LCARS itself never recorded (same
    failure shape as the HELL MODE/etc. read-back bugs, todo.md
    2026-08-15). Accepted as a rare, explicit trade-off for now, not
    guarded against — revisit (e.g. a read-back guard comparing against
    what LCARS itself last pushed) if it ever actually bites."""
    row = conn.execute(
        "SELECT MAX(episode) AS furthest FROM episode"
        " WHERE show_id = ? AND season = ? AND state IN ('watched', 'skipped')",
        (show_id, season_number),
    ).fetchone()
    return row["furthest"] or 0


def _push_season_progress(conn, season: dict) -> None:
    """Recomputes and pushes this one season's own episode-watched
    high-water mark to AniList — same recompute-then-push shape
    _push_season_score already uses, not a delta (a delta would need
    AniList's own prior progress value and this season's real episode
    count, neither reliably available locally today — see
    _compute_season_episode_progress's own docstring). No per-season
    AniList episode-count guard: this assumes the same one-LCARS-
    season-maps-to-one-AniList-media 1:1 shape watch_reconcile.py's own
    read side already assumes uncritically — a real, already-logged
    architectural gap (see todo.md's "hierarchical season subdivision"
    idea) for split-cour shows, not newly introduced here."""
    if season["anilist_id"] is None:
        return
    cfg = config.get_current()
    if not cfg.anilist_access_token:
        return
    progress = _compute_season_episode_progress(conn, season["show_id"], season["season_number"])
    try:
        anilist_client.save_media_list_entry(
            cfg.anilist_access_token, season["anilist_id"], progress=progress
        )
    except anilist_client.AniListError as e:
        pending_review.open_or_extend(
            conn, "season", season["id"], "anilist_push", "anilist", None, str(e)
        )


def _push_show_episode_progress(conn, show_id: str, season_number: int) -> None:
    """Looks up the one season row for this show+season_number and
    pushes its recomputed progress — the shared entry point every
    watch/unwatch/skip mutation below calls (addWatchEvent,
    deleteWatchEvent, markSeasonWatched, markEpisodeRangeWatched,
    markEpisodeSkipped): "episode watched"/"episode un-watched" are the
    same push operation, not two, since progress is always recomputed
    fresh from current episode.state rather than incremented/
    decremented (todo.md's write-mirror enumeration originally listed
    them as two separate gaps; they collapse into one function here).
    No-ops silently if this show has no season row at all (e.g. a
    movie's watch event, season is None)."""
    if season_number is None:
        return
    season = conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND season_number = ?",
        (show_id, season_number),
    ).fetchone()
    if season is None:
        return
    _push_season_progress(conn, dict(season))


# -- MAL push (§6.1/§6.9, B.10) ----------------------------------------------
#
# Same best-effort/pending_review-on-failure shape as the AniList push section
# above, kept as its own parallel set of functions rather than folded into
# the AniList ones — the two services key on the same season.mal_id/
# anilist_id pair and get called from the same three resolvers below, but
# have genuinely different score scales (÷2 integer vs ×5 float) and status
# enums, so a shared function would need to branch on service anyway.
#
# Deliberately has no episode-progress counterpart to _push_show_episode_
# progress above — the user's write-mirror request (2026-08-15/16) was
# scoped to AniList specifically, not MAL. Noted explicitly so the
# asymmetry reads as a scope decision, not an oversight.

_STATUS_TO_MAL = {
    # Maps the five pushable statuses; 'skipped' is deliberately absent
    # (no MAL equivalent) — push functions use .get() and early-return on
    # None, same pattern as _STATUS_TO_ANILIST.
    "watching": "watching",
    "planned": "plan_to_watch",
    "paused": "on_hold",
    "completed": "completed",
    "dropped": "dropped",
    # deliberately no is_rewatching mapping here either — same §6.8/§6.9
    # "rewatching never auto-toggles" policy _STATUS_TO_ANILIST already
    # documents, MAL's own is_rewatching/num_times_rewatched fields are
    # simply never sent (mal_client.update_my_list_status's own docstring).
}


def _push_mal_season_score(conn, season: dict, fallback_show_score) -> None:
    """Mirrors _push_season_score above — same fallback rule (season's
    own score, else the show's), same no-op-before-mal-login guard —
    but MAL's score is 0-10 integer (§6.1: "÷2"), not AniList's 0-100
    float, so the effective 0-20 quarter-point value is rounded, not
    just scaled."""
    if season["mal_id"] is None:
        return
    cfg = config.get_current()
    if not cfg.mal_access_token:
        return
    effective_score = season["score"] if season["score"] is not None else fallback_show_score
    if effective_score is None:
        return
    try:
        mal_client.update_my_list_status(
            cfg.mal_access_token, season["mal_id"], score=round(effective_score / 2)
        )
    except mal_client.MALError as e:
        pending_review.open_or_extend(conn, "season", season["id"], "mal_push", "mal", None, str(e))


def _push_mal_show_score(conn, show_id: str, show_score) -> None:
    """Every one of the show's mal_id-linked seasons — mirrors
    _push_show_score above."""
    seasons = conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND mal_id IS NOT NULL", (show_id,)
    ).fetchall()
    for season in seasons:
        _push_mal_season_score(conn, dict(season), show_score)


def _push_mal_show_status(conn, show_id: str, status: str) -> None:
    """Push status to MAL for every linked season.  Each season uses
    its own per-season status when set (2.1c), falling back to the
    show-level status passed in."""
    cfg = config.get_current()
    if not cfg.mal_access_token:
        return
    seasons = conn.execute(
        "SELECT id, mal_id, status AS season_status"
        " FROM season WHERE show_id = ? AND mal_id IS NOT NULL",
        (show_id,),
    ).fetchall()
    for season in seasons:
        effective = season["season_status"] or status
        mal_status = _STATUS_TO_MAL.get(effective)
        if mal_status is None:
            continue  # 'skipped' — no MAL equivalent
        try:
            mal_client.update_my_list_status(
                cfg.mal_access_token, season["mal_id"], status=mal_status
            )
        except mal_client.MALError as e:
            pending_review.open_or_extend(
                conn, "season", season["id"], "mal_push", "mal", None, str(e)
            )


def _push_mal_season_progress(conn, season: dict) -> None:
    """Mirrors _push_season_progress (AniList) — pushes this season's own
    recomputed episode-watched high-water mark to MAL's
    `num_watched_episodes`, keyed on mal_id. 2026-08-26 — the MAL
    counterpart the module comment above _push_mal_season_score noted was
    deliberately unbuilt, now built for the bidirectional sync (user's
    "watch episodes... need to be reflected on MAL")."""
    if season["mal_id"] is None:
        return
    cfg = config.get_current()
    if not cfg.mal_access_token:
        return
    progress = _compute_season_episode_progress(conn, season["show_id"], season["season_number"])
    try:
        mal_client.update_my_list_status(
            cfg.mal_access_token, season["mal_id"], num_watched_episodes=progress
        )
    except mal_client.MALError as e:
        pending_review.open_or_extend(conn, "season", season["id"], "mal_push", "mal", None, str(e))


def _push_mal_show_episode_progress(conn, show_id: str, season_number: int) -> None:
    """MAL counterpart to _push_show_episode_progress — same one-season
    lookup, same no-op on a movie/absent season."""
    if season_number is None:
        return
    season = conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND season_number = ?",
        (show_id, season_number),
    ).fetchone()
    if season is None:
        return
    _push_mal_season_progress(conn, dict(season))


def _get_episode_numbering_mapping(conn, mapping_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM episode_numbering_mapping WHERE id = ?", (mapping_id,)
    ).fetchone()
    return dict(row) if row else None


def _get_episode_movie_link(conn, link_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM episode_movie_link WHERE id = ?", (link_id,)).fetchone()
    return dict(row) if row else None


def _get_pending_review(conn, review_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM pending_review WHERE id = ?", (review_id,)).fetchone()
    return dict(row) if row else None


def _get_show_merge(conn, merge_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM show_merge WHERE id = ?", (merge_id,)).fetchone()
    return dict(row) if row else None


def _get_show_service_presence(conn, presence_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM show_service_presence WHERE id = ?", (presence_id,)
    ).fetchone()
    return dict(row) if row else None


def _get_person(conn, person_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM person WHERE id = ?", (person_id,)).fetchone()
    return dict(row) if row else None


def _get_studio(conn, studio_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM studio WHERE id = ?", (studio_id,)).fetchone()
    return dict(row) if row else None


def _get_franchise(conn, franchise_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM franchise WHERE id = ?", (franchise_id,)).fetchone()
    return dict(row) if row else None


def _require_franchise(conn, franchise_id: str) -> dict:
    franchise = _get_franchise(conn, franchise_id)
    if franchise is None:
        raise GraphQLError(f"no such franchise: {franchise_id}")
    return franchise


def _get_tag(conn, tag_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tag WHERE id = ?", (tag_id,)).fetchone()
    return dict(row) if row else None


def _require_tag(conn, tag_id: str) -> dict:
    tag = _get_tag(conn, tag_id)
    if tag is None:
        raise GraphQLError(f"no such tag: {tag_id}")
    return tag


def _get_filter_preset(conn, preset_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM filter_preset WHERE id = ?", (preset_id,)).fetchone()
    return dict(row) if row else None


def _require_filter_preset(conn, preset_id: str) -> dict:
    preset = _get_filter_preset(conn, preset_id)
    if preset is None:
        raise GraphQLError(f"no such filter_preset: {preset_id}")
    return preset


# --- Query -------------------------------------------------------------


@query.field("show")
def resolve_show(_, info, id):
    return _get_show(db.get_connection(), id)


@query.field("shows")
def resolve_shows(_, info, **page_args):
    return pagination.paginate(db.get_connection(), "show", "1 = 1", (), **page_args)


@query.field("episode")
def resolve_episode(_, info, id):
    return _get_episode(db.get_connection(), id)


@query.field("season")
def resolve_season(_, info, id):
    """B.17 — thin wrapper around `season_mapping.get_season()`, the
    single existing definition (see that function's own docstring on
    why resolvers.py doesn't keep a second copy)."""
    return season_mapping.get_season(db.get_connection(), id)


@query.field("showsByStatus")
def resolve_shows_by_status(_, info, statuses, **page_args):
    placeholders = ", ".join("?" for _ in statuses)
    return pagination.paginate(
        db.get_connection(), "show", f"status IN ({placeholders})", tuple(statuses), **page_args
    )


@query.field("episodesAiringSoon")
def resolve_episodes_airing_soon(_, info, days, **page_args):
    """§8 — episodes airing in the next N days, inclusive of right now
    and the end of the Nth day; already-aired episodes aren't "soon"."""
    now = util.now_utc_iso()
    until = util.utc_iso_offset(days)
    return pagination.paginate(
        db.get_connection(),
        "episode",
        "air_date_utc IS NOT NULL AND air_date_utc >= ? AND air_date_utc <= ?",
        (now, until),
        **page_args,
    )


@query.field("episodesInRange")
def resolve_episodes_in_range(_, info, start, end, **page_args):
    """§8, B.11c — arbitrary [start, end) episode window, half-open
    (matches Data's own calendar_nav.CalendarState.date_range()
    convention: `start <= local.date() < end`). Unlike
    episodesAiringSoon (future-only, relative to server "now"), the
    caller supplies explicit bounds so a client-side calendar can page
    arbitrarily far into the past or future — built for Data's own
    calendar render path (B.11f), which has no floor on how far back
    step_back() can go."""
    return pagination.paginate(
        db.get_connection(),
        "episode",
        "air_date_utc IS NOT NULL AND air_date_utc >= ? AND air_date_utc < ?",
        (start, end),
        **page_args,
    )


@query.field("backlog")
def resolve_backlog(_, info, include_planned=False, **page_args):
    """§6.3, B.9 — the backlog: unwatched, locally-available episodes on
    watching-status shows. Any watching show with available-but-unwatched
    episodes qualifies — the previous _show_is_airing gate excluded
    shows whose last episode had already aired (e.g. a weekly show where
    you're one episode behind after the finale airs).

    includePlanned (Android app auto-download, 2026-09-20, user request):
    a planning-status show counts as "started" the same way a watching
    show already does above — by having a real available-but-unwatched
    episode, not by comparing to air_date_utc. Deliberately simplified
    from an earlier air-date-gated version: the outer WHERE below already
    requires available_locally = 1 (an actual Sonarr/Radarr grab, not
    just tracked metadata), and the watching-show path has never had an
    air-date check either — a planning show whose episode is available
    early (a leak, a wrong/missing recorded air date, whatever the cause)
    is exactly the case the user wants included, not excluded by an extra
    gate watching shows were never subject to. Default false: every other
    caller (the web Backlog page) keeps the original watching-only
    behavior unchanged."""
    conn = db.get_connection()
    show_filter = "status = 'watching'"
    if include_planned:
        show_filter = "status = 'watching' OR status = 'planned'"
    return pagination.paginate(
        conn,
        "episode",
        "state = 'unwatched' AND available_locally = 1"
        f" AND show_id IN (SELECT id FROM show WHERE {show_filter})",
        (),
        **page_args,
    )


@query.field("pendingReviews")
def resolve_pending_reviews(_, info, include_resolved=False, **page_args):
    where = "1 = 1" if include_resolved else "resolved_at IS NULL"
    return pagination.paginate(db.get_connection(), "pending_review", where, (), **page_args)


@query.field("nextUp")
def resolve_next_up(_, info, **page_args):
    """§6.4, A.11 — one entry per `watching`-status or paced/catch-up
    show, each paired with its earliest unwatched+available episode;
    shows with no such episode are simply absent, not an error. Not a
    single-table query (§5's own pagination.py can't page this), so
    the full ordered list is computed here and handed to
    pagination.paginate_list instead — fine at personal-tracker scale
    (a few dozen candidate shows at most).

    Ordering ("same auto-default-plus-override shape as franchise
    ordering", §6.4): shows with a `next_up_override` come first, by
    their own `sortOrder` — the same "manual value wins" precedence
    used everywhere else in this project (§3 principle 6) — then every
    other show follows in the stated default, soonest-available-first
    (`episode.air_date_utc` ascending; a null air date, possible for a
    manually-linked file with no known date, sorts last within this
    group rather than first, since "soonest" doesn't apply to
    "unknown").

    **Corrected 2026-08-09 (consolidation audit)**: the intra-show
    "which episode is next" pick used `ORDER BY season ASC, episode ASC`
    from A.11 through this fix — a second, different ordering rule §6.4
    never states. §6.4 defines exactly one default, soonest-available-
    first, and it governs *both* levels. The old numbering order also
    imported an external platform's convention into internal behavior:
    Sonarr/TVDB park specials in season 0, so `season ASC` made every
    special outrank the actual premiere — verified, `nextUp` offered a
    season-0 special ahead of S1E1. Air-date order needs no `kind`
    taxonomy to get this right: a special simply falls wherever it
    actually aired. Internal air date is the source of truth here; how
    a source platform files an episode has no bearing on it (§3
    principle 5's "LCARS is unconditionally authoritative", applied to
    ordering). Same null-sorts-last rule as the cross-show level, for
    the same reason; `(season, episode)` remains only as a stable
    tiebreak between two episodes sharing one air date."""
    conn = db.get_connection()
    shows = conn.execute(
        "SELECT id FROM show WHERE status = 'watching' OR paced_cadence_days IS NOT NULL"
    ).fetchall()

    candidates = []
    for show in shows:
        episode = conn.execute(
            "SELECT * FROM episode"
            " WHERE show_id = ? AND state = 'unwatched' AND available_locally = 1"
            " ORDER BY air_date_utc IS NULL, air_date_utc ASC, season ASC, episode ASC"
            " LIMIT 1",
            (show["id"],),
        ).fetchone()
        if episode is None:
            continue
        override = conn.execute(
            "SELECT sort_order FROM next_up_override WHERE show_id = ?", (show["id"],)
        ).fetchone()
        candidates.append(
            {
                "show_id": show["id"],
                "episode": dict(episode),
                "override_sort_order": override["sort_order"] if override else None,
            }
        )

    overridden = sorted(
        (c for c in candidates if c["override_sort_order"] is not None),
        key=lambda c: c["override_sort_order"],
    )
    default = sorted(
        (c for c in candidates if c["override_sort_order"] is None),
        key=lambda c: (c["episode"]["air_date_utc"] is None, c["episode"]["air_date_utc"]),
    )
    items = [{"show_id": c["show_id"], "episode": c["episode"]} for c in overridden + default]
    return pagination.paginate_list(items, **page_args)


@query.field("dueForMetadataRefresh")
def resolve_due_for_metadata_refresh(_, info, **page_args):
    """§6.7/B.1 — Ops's own daily-pass query; a client's on-open trigger
    calls this exact same query (SCOPE.md §11.2's B.1 note) rather than
    a separate mutation. Eligibility is `status = WATCHING` **and**
    actively airing — reuses `_show_is_airing` (A.10) verbatim rather
    than re-deriving the same "any episode with a null/future
    air_date_utc" predicate a second time in raw SQL; §6.7's own text
    ("watching-status, actively-airing shows... not-airing/not-watching
    shows get no background refresh") reads as one combined filter, not
    two independent jobs. Not a single-table WHERE (same reason nextUp,
    A.11, isn't) — the airing check isn't one column comparison — so
    candidates are computed in Python first and handed to
    pagination.paginate_list, same pattern as nextUp."""
    conn = db.get_connection()
    cutoff = util.start_of_today_utc(config.get_current().home_timezone)
    candidates = conn.execute(
        "SELECT * FROM show WHERE status = 'watching'"
        " AND (metadata_last_refreshed_at IS NULL OR metadata_last_refreshed_at < ?)",
        (cutoff,),
    ).fetchall()
    due = [dict(show) for show in candidates if _show_is_airing(conn, show["id"])]
    return pagination.paginate_list(due, **page_args)


@query.field("dueForSeasonReconciliation")
def resolve_due_for_season_reconciliation(_, info, **page_args):
    """§5.5/B.2 — the weekly tier: every season of a watching+actively-
    airing show (the whole show's airing status gates ALL its seasons,
    not just the one currently airing — confirmed directly, 2026-08-09:
    "whole show airing -> all its seasons weekly"), not reconciled
    against Fribb in the last 7 days. Reuses `_show_is_airing` (A.10)
    verbatim, same combined watching+airing filter `dueForMetadataRefresh`
    (B.1) already uses — confirmed to mirror it exactly ("Airing AND
    watching (like B.1)"). Deliberately not `home_timezone`-bucketed like
    B.1's daily cutoff — §6.13 doesn't name the weekly cadence as a
    consumer, so a plain 7-days-ago cutoff (`util.utc_iso_offset(-7)`)
    is used instead. Same computed-in-Python-first shape as
    `dueForMetadataRefresh`/`nextUp` — the airing check isn't one column
    comparison, so it can't be a single SQL WHERE.

    `tracking_space = 'anime'` filter added 2026-08-12: unlike B.1's
    metadata refresh (which legitimately applies to tv shows too, via
    TMDB), Fribb/AniList season reconciliation only ever has anything
    to say about anime — a tv show can never produce a candidate. Same
    reasoning as `show_merge.py`'s own winner-side filter and
    `animeschedule.py`'s poll query. Belt-and-suspenders alongside the
    real fix in `season_mapping.reconcile_season()` itself, which is
    also reached directly from a Sonarr fetch (A.20), not just here."""
    conn = db.get_connection()
    watching_shows = conn.execute(
        "SELECT * FROM show WHERE status = 'watching' AND tracking_space = 'anime'"
    ).fetchall()
    airing_show_ids = [s["id"] for s in watching_shows if _show_is_airing(conn, s["id"])]
    if not airing_show_ids:
        return pagination.paginate_list([], **page_args)
    cutoff = util.utc_iso_offset(-7)
    placeholders = ",".join("?" for _ in airing_show_ids)
    due = conn.execute(
        f"SELECT * FROM season WHERE show_id IN ({placeholders})"
        " AND (last_reconciled_at IS NULL OR last_reconciled_at < ?)",
        (*airing_show_ids, cutoff),
    ).fetchall()
    return pagination.paginate_list([dict(s) for s in due], **page_args)


@next_up_entry_type.field("show")
def resolve_next_up_entry_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@next_up_entry_type.field("episode")
def resolve_next_up_entry_episode(obj, info):
    return obj["episode"]


@query.field("search")
def resolve_search(_, info, query, **page_args):
    """§6.5, A.12 — across all three stored title variants (not just
    whichever is primaryTitle) and synopsis. SQL `LIKE` substring
    matching, case-insensitive by SQLite's own default for ASCII —
    not fuzzy/similarity scoring: §6.5 explicitly frames this as
    *replacing* "per-client fuzzy matching" with a real search query,
    a different concern from §5.4's fuzzy service-presence matcher
    (fuzzy.py, A.7) or aninote's own vault matcher, both of which
    exist specifically to tolerate a mismatched/uncertain title
    string, not to search a query the user typed on purpose. (SCOPE.md
    itself named `difflib` as aniq's own approach here — checked the
    real code, `list_screen.py` actually uses `textual.fuzzy.Matcher`,
    not difflib at all; a factual correction, not a design question —
    noted in SCOPE.md.) Personal-tracker scale (dozens to a few
    hundred rows) doesn't call for SQLite FTS5's indexing/ranking
    machinery — a plain `LIKE` scan across an already-small table is
    both simpler and fast enough.
    """
    conn = db.get_connection()
    like = f"%{query}%"
    # `synonyms` added 2026-08-26 — a not-yet-aired sequel is often indexed on
    # AniList only under a romaji/arc-name title held here as a synonym, so a
    # user searching that name (or a show's fan/other-language name) finds it.
    where = (
        "(title_romaji LIKE ? OR title_english LIKE ? OR title_native LIKE ?"
        " OR synopsis LIKE ?"
        " OR EXISTS (SELECT 1 FROM show_synonym WHERE show_synonym.show_id = show.id"
        " AND show_synonym.synonym LIKE ?))"
    )
    return pagination.paginate(conn, "show", where, (like, like, like, like, like), **page_args)


@query.field("searchArrCandidates")
def resolve_search_arr_candidates(_, info, media_shape, title):
    """2026-08-18 — Data's own `A` disambiguation picker. Explicit
    dict-shaping below, not a bare passthrough of Sonarr's/Radarr's own
    raw response: those come back genuinely camelCase already
    (`tvdbId`/`tmdbId`, Sonarr's/Radarr's own API convention) — server.py's
    `convert_names_case=True` expects snake_case dict keys and derives
    camelCase GraphQL field names *from* them, so a raw passthrough
    would silently resolve every id field to null (looking for a
    "tvdb_id" key that was never there) rather than error, the kind of
    gap that's easy to miss without a real end-to-end test."""
    conn = db.get_connection()
    try:
        results = shows.search_arr_candidates(conn, media_shape, title)
    except shows.ShowInputError as e:
        raise GraphQLError(str(e)) from e
    return [
        {
            "title": r.get("title"),
            "year": r.get("year"),
            "tvdb_id": r.get("tvdbId"),
            "tmdb_id": r.get("tmdbId"),
            "overview": r.get("overview"),
        }
        for r in results
    ]


@query.field("searchAniList")
def resolve_search_anilist(_, info, title):
    """Search AniList's anime catalog by title — up to 10 results sorted
    by relevance.  Returns AniListCandidate dicts with explicit key
    shaping (same reasoning as searchArrCandidates above — server.py's
    convert_names_case expects snake_case keys).

    Falls back to MAL search when AniList is unreachable (2026-09-05).
    MAL results won't have ``anilist_id`` (null) — the client sends
    ``malId`` only to ``addShow``, which accepts it."""
    try:
        results = anilist_client.search_media(title)
    except anilist_client.AniListError:
        log.warning("AniList search failed for %r, trying MAL", title)
        return _search_mal_fallback(title)
    return [
        {
            "anilist_id": r["id"],
            "mal_id": r.get("idMal"),
            "title_romaji": (r.get("title") or {}).get("romaji"),
            "title_english": (r.get("title") or {}).get("english"),
            "title_native": (r.get("title") or {}).get("native"),
            "format": r.get("format"),
            "episodes": r.get("episodes"),
            "cover_image_url": (r.get("coverImage") or {}).get("large"),
            "year": (r.get("startDate") or {}).get("year"),
        }
        for r in results
    ]


def _search_mal_fallback(title: str) -> list[dict]:
    """MAL fallback for ``resolve_search_anilist`` — called when AniList
    is unreachable. Maps MAL search results to ``AniListCandidate`` shape.
    Raises ``GraphQLError`` if MAL is also down."""
    conf = config.get_current()
    if not conf.mal_client_id:
        raise GraphQLError("AniList is down and MAL client_id is not configured")
    try:
        results = mal_client.search_anime(title, conf.mal_client_id)
    except mal_client.MALError as e:
        raise GraphQLError(f"Both AniList and MAL are unavailable: {e}") from e
    items = []
    for r in results:
        alt = r.get("alternative_titles") or {}
        start = r.get("start_date") or ""
        year = int(start[:4]) if len(start) >= 4 else None
        main_pic = r.get("main_picture") or {}
        items.append(
            {
                "anilist_id": None,
                "mal_id": r.get("id"),
                "title_romaji": r.get("title"),  # MAL's title IS the romaji
                "title_english": alt.get("en") or None,
                "title_native": alt.get("ja"),
                "format": browse._MAL_FORMAT_MAP.get(
                    (r.get("media_type") or "").lower()
                ),
                "episodes": r.get("num_episodes") or None,
                "cover_image_url": main_pic.get("large") or main_pic.get("medium"),
                "year": year,
            }
        )
    return items


@query.field("showByExternalId")
def resolve_show_by_external_id(_, info, service, external_id):
    """2026-09-02 — look up a tracked show by external-service identity.
    Thin wrapper over shows.find_existing_show, exposed so the web
    client's Add page can pre-check candidates before addShowWithArr."""
    valid = {"anilist", "tvdb", "tmdb", "imdb", "mal"}
    svc = service.lower()
    if svc not in valid:
        raise GraphQLError(f"unknown service: {service} (valid: {', '.join(sorted(valid))})")
    conn = db.get_connection()
    show_id = shows.find_existing_show(conn, {f"{svc}_id": external_id})
    if show_id is None:
        return None
    show = _get_show(conn, show_id)
    if show is None or not show.get("tracked"):
        return None
    return show


@query.field("searchAniDb")
def resolve_search_anidb(_, info, title):
    """Search the local AniDB titles dump by name. Matches across all
    languages. Returns up to 10 candidates ranked by relevance."""
    conn = db.get_connection()
    return anidb.search_by_title(conn, title, limit=10)


@query.field("suggestAniDbId")
def resolve_suggest_anidb_id(_, info, show_id):
    """Suggest AniDB IDs for a tracked show by matching its titles."""
    conn = db.get_connection()
    return anidb.suggest_anidb_id(conn, show_id)


@mutation.field("linkAniDb")
def resolve_link_anidb(_, info, show_id, anidb_id, tvdb_id=None,
                       default_tvdb_season=None, episode_offset=None):
    """Manually link a show to an AniDB ID."""
    conn = db.get_connection()
    show = conn.execute(
        "SELECT id FROM show WHERE id = ?", (show_id,)
    ).fetchone()
    if not show:
        raise GraphQLError(f"show not found: {show_id}")
    anidb.link_anidb_manual(
        conn, show_id, anidb_id,
        tvdb_id=tvdb_id,
        default_tvdb_season=default_tvdb_season,
        episode_offset=episode_offset or 0,
    )
    return True


@query.field("stats")
def resolve_stats(_, info):
    """§6.6, A.13 — no ObjectType binding needed for `Stats`/
    `ScoreBucket`: every one of their fields is a plain scalar (or a
    list of a type whose own fields are plain scalars), so Ariadne's
    default dict-key resolution already handles them, same as any
    other plain field elsewhere — as long as this resolver's own
    return dict uses the right snake_case keys throughout.

    `totalShows` is a *current-library* snapshot (`tracked = 1`) —
    reads naturally as "how big is my library right now." Everything
    else here is a *lifetime* total, deliberately not filtered by
    `tracked`: untracking a show is soft and doesn't erase having
    watched it (§6.11) or scored it, so what you've already watched/
    scored shouldn't shrink just because you later untracked the show
    it came from. Genre/year/tracking-space breakdowns are explicitly
    out of scope (§6.6 itself, `genres_raw` is unnormalized).
    """
    conn = db.get_connection()

    total_shows = conn.execute("SELECT COUNT(*) AS c FROM show WHERE tracked = 1").fetchone()["c"]

    total_episodes_watched = conn.execute(
        "SELECT COUNT(*) AS c FROM episode WHERE state = 'watched'"
    ).fetchone()["c"]

    # Episodic: sum each watched episode's own runtime_minutes, falling
    # back to its show's duration_minutes when the episode has no override
    # (§5.1/§5.2, same fallback shape used throughout this project).
    episode_minutes = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(e.runtime_minutes, s.duration_minutes)), 0) AS total"
        " FROM episode e JOIN show s ON e.show_id = s.id"
        " WHERE e.state = 'watched'"
    ).fetchone()["total"]

    # Movies have no episode rows at all (§5.1) — "watched" means a
    # watch_event exists for the show at all; duration comes from the
    # show's own duration_minutes, the only runtime info a movie has.
    movie_minutes = conn.execute(
        "SELECT COALESCE(SUM(s.duration_minutes), 0) AS total"
        " FROM show s"
        " WHERE s.media_shape = 'movie'"
        " AND EXISTS (SELECT 1 FROM watch_event w WHERE w.show_id = s.id)"
    ).fetchone()["total"]

    hours_watched = (episode_minutes + movie_minutes) / 60.0

    score_rows = conn.execute(
        "SELECT score, COUNT(*) AS count FROM show"
        " WHERE score IS NOT NULL GROUP BY score ORDER BY score"
    ).fetchall()
    score_distribution = [{"score": row["score"], "count": row["count"]} for row in score_rows]

    return {
        "total_shows": total_shows,
        "total_episodes_watched": total_episodes_watched,
        "hours_watched": hours_watched,
        "score_distribution": score_distribution,
    }


@query.field("person")
def resolve_person(_, info, id):
    return _get_person(db.get_connection(), id)


@query.field("people")
def resolve_people(_, info, **page_args):
    return pagination.paginate(db.get_connection(), "person", "1 = 1", (), **page_args)


@query.field("studio")
def resolve_studio(_, info, id):
    return _get_studio(db.get_connection(), id)


@query.field("studios")
def resolve_studios(_, info, **page_args):
    return pagination.paginate(db.get_connection(), "studio", "1 = 1", (), **page_args)


@query.field("franchise")
def resolve_franchise(_, info, id):
    return _get_franchise(db.get_connection(), id)


@query.field("franchises")
def resolve_franchises(_, info, **page_args):
    return pagination.paginate(db.get_connection(), "franchise", "1 = 1", (), **page_args)


@query.field("tag")
def resolve_tag(_, info, id):
    return _get_tag(db.get_connection(), id)


@query.field("tags")
def resolve_tags(_, info, **page_args):
    return pagination.paginate(db.get_connection(), "tag", "1 = 1", (), **page_args)


@query.field("filterPreset")
def resolve_filter_preset(_, info, id):
    return _get_filter_preset(db.get_connection(), id)


@query.field("filterPresets")
def resolve_filter_presets(_, info, **page_args):
    return pagination.paginate(db.get_connection(), "filter_preset", "1 = 1", (), **page_args)


# --- Show fields ---------------------------------------------------------


def _computed_display_title(show) -> str:
    """A show's effective display title: the manual
    `display_title_override` when set (any title/synonym the user pinned,
    e.g. "Lamu" for Urusei Yatsura), else `title_{primary_title}` — the
    default `metadata.py` already picks english-first, romaji-fallback.
    Shared by the `displayTitle` field and `confirmHardDelete`'s retyped-
    title check so the two can never drift."""
    override = show["display_title_override"] if "display_title_override" in show.keys() else None
    return override or show[f"title_{show['primary_title']}"]


@show_type.field("displayTitle")
def resolve_display_title(obj, info):
    return _computed_display_title(obj)


@show_type.field("synonyms")
def resolve_synonyms(obj, info):
    """AniList `Media.synonyms` — the show's alternative titles (alt
    spellings, abbreviations, other-language/fan names), re-synced on
    every metadata fetch (metadata.py's `_sync_synonyms`). Ordered by
    insert order (rowid) for a stable list."""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT synonym FROM show_synonym WHERE show_id = ? ORDER BY rowid", (obj["id"],)
    ).fetchall()
    return [r["synonym"] for r in rows]


@show_type.field("watchedEpisodeCount")
def resolve_watched_episode_count(obj, info):
    """Count of episodes with at least one watch event."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT COUNT(DISTINCT e.id) AS c FROM episode e"
        " JOIN watch_event w ON w.show_id = e.show_id"
        " AND w.season = e.season AND w.episode = e.episode"
        " WHERE e.show_id = ?",
        (obj["id"],),
    ).fetchone()
    return row["c"] if row else 0


@show_type.field("availableEpisodeCount")
def resolve_available_episode_count(obj, info):
    """Count of episodes available locally or via Sonarr/Radarr."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM episode"
        " WHERE show_id = ? AND ("
        "   available_via_sonarr = 'available'"
        "   OR available_via_radarr = 'available'"
        "   OR available_locally = 1"
        " )",
        (obj["id"],),
    ).fetchone()
    return row["c"] if row else 0


@show_type.field("genresRaw")
def resolve_genres_raw(obj, info):
    raw = obj.get("genres_raw")
    return json.loads(raw) if raw else []


@show_type.field("episodes")
def resolve_show_episodes(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "episode", "show_id = ?", (obj["id"],), **page_args
    )


@show_type.field("watchEvents")
def resolve_show_watch_events(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "watch_event", "show_id = ?", (obj["id"],), **page_args
    )


_ARR_ADD_LINK = {
    "episodic": ("sonarr", "sonarr:add", "sonarr_public_url", "tvdb"),
    "movie": ("radarr", "radarr:add", "radarr_public_url", "tmdb"),
}


def _synthetic_arr_add_edge(show: dict, edges: list[dict]) -> dict | None:
    """2026-08-18 — the user's own correction to the first cut of this
    feature: a show not yet followed in Sonarr/Radarr shouldn't just
    show no link at all — it should offer Sonarr's/Radarr's own "Add
    New" page, pre-filled with the id LCARS already knows, so opening
    it is one browser hop from "not followed" to "reviewing the add
    screen there" — never an automatic add, LCARS/Data never call
    Sonarr's/Radarr's own add endpoint themselves, same as every other
    link this field returns.

    Sonarr's and Radarr's own "Add New" search box accepts `tvdb:<id>`/
    `tmdb:<id>` directly (the same lookup their indexer search already
    does internally) — `?term=` pre-fills and runs that search rather
    than landing on an empty box.

    Not persisted: computed fresh on every read from data this
    connection already fetched (the show's own confirmed `tvdb`/`tmdb`
    show_external_id row — §5.4, written by create_show/
    create_show_with_arr_add), so it self-heals the moment a real
    `sonarr`/`radarr` link exists (write_arr_external_id) with zero
    extra bookkeeping. `service` is `sonarr:add`/`radarr:add` —
    distinct from a real followed `sonarr`/`radarr` link so a client
    can label the two differently (ShowExternalId.service is plain,
    open-ended String by design, schema's own docstring, for exactly
    this). Silently omitted (no synthetic edge) whenever any part of
    this is unknown — not configured, or LCARS has no tvdb_id/tmdb_id
    for this show yet — same "no answer beats a guessed one" shape
    every other external link here already follows.

    Uses `sonarr_public_url`/`radarr_public_url`, NOT `sonarr_url`/
    `radarr_url` — same real bug `write_arr_external_id` (shows.py) was
    just fixed for: the latter is LCARS's own outbound-API address
    (this deployment's docker-network hostname), unreachable from any
    browser this URL is actually handed to.

    Title-search fallback, 2026-08-18 — a real gap hit live: most of
    this library's shows were tracked long before Sonarr/Radarr
    integration existed at all (AniList-only `addShow`, no tvdb_id/
    tmdb_id ever captured — Akame ga Kill!, the case that surfaced
    this), and the monthly catalog sweep only assigns one when the show
    is genuinely already *in* Sonarr's/Radarr's own library — never for
    a show that plainly isn't. Refusing every one of those a link at
    all (the original id-only cut of this) is a real regression for
    most of the library, not a rare edge case. When no tvdb_id/tmdb_id
    is known, this falls back to `?term=<title>` — the exact same
    free-text search box Sonarr's/Radarr's own UI already offers if the
    user typed the title in by hand, not a guessed id (the thing "no
    answer beats a guessed one" above is actually protecting against);
    the user still lands on real search results to confirm themselves,
    never an auto-selected entry."""
    arr_shape = _ARR_ADD_LINK.get(show["media_shape"])
    if arr_shape is None:
        return None
    service, add_service, url_config_key, id_service = arr_shape
    base_url = getattr(config.get_current(), url_config_key)
    if not base_url:
        return None
    services_present = {e["node"]["service"] for e in edges}
    if service in services_present:
        return None
    known_id = next(
        (e["node"]["external_id"] for e in edges if e["node"]["service"] == id_service), None
    )
    if known_id is not None:
        term = f"{id_service}:{known_id}"
    else:
        title = show["title_english"] or show["title_romaji"] or show["title_native"]
        if not title:
            return None
        term = title
    url = f"{base_url.rstrip('/')}/add/new?term={urllib.parse.quote(term, safe=':')}"
    return {
        "node": {
            "show_id": show["id"],
            "service": add_service,
            "external_id": known_id if known_id is not None else term,
            "url": url,
            "created_at": util.now_utc_iso(),
        },
        "cursor": f"synthetic:{add_service}",
    }


@show_type.field("externalIds")
def resolve_show_external_ids(obj, info, **page_args):
    # Filter out tombstone rows (tvmaze external_id='-1') written by
    # drip-fetch to mark "not found" — they satisfy the drip's NOT
    # EXISTS gate but are not real links to show in the UI.
    connection = pagination.paginate(
        db.get_connection(), "show_external_id",
        "show_id = ? AND NOT (service = 'tvmaze' AND external_id = '-1')",
        (obj["id"],), **page_args
    )
    synthetic = _synthetic_arr_add_edge(obj, connection["edges"])
    if synthetic is not None:
        connection["edges"] = [*connection["edges"], synthetic]
    return connection


@show_type.field("statusHistory")
def resolve_show_status_history(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "status_change", "show_id = ?", (obj["id"],), **page_args
    )


@show_type.field("scoreHistory")
def resolve_show_score_history(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "score_change", "show_id = ?", (obj["id"],), **page_args
    )


@show_type.field("trackedHistory")
def resolve_show_tracked_history(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "tracked_change", "show_id = ?", (obj["id"],), **page_args
    )


@show_type.field("seasons")
def resolve_show_seasons(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "season", "show_id = ?", (obj["id"],), **page_args
    )


@show_type.field("episodeNumberingMapping")
def resolve_show_episode_numbering_mapping_field(obj, info):
    row = (
        db.get_connection()
        .execute("SELECT * FROM episode_numbering_mapping WHERE show_id = ?", (obj["id"],))
        .fetchone()
    )
    return dict(row) if row else None


@show_type.field("linkedFromEpisode")
def resolve_show_linked_from_episode(obj, info):
    """media_shape = MOVIE only — the reverse direction of
    Episode.linkedMovieShow (§5.1/§5.9 addendum)."""
    conn = db.get_connection()
    link = conn.execute(
        "SELECT episode_id FROM episode_movie_link WHERE movie_show_id = ?", (obj["id"],)
    ).fetchone()
    if link is None:
        return None
    return _get_episode(conn, link["episode_id"])


@show_type.field("servicePresence")
def resolve_show_service_presence(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "show_service_presence", "show_id = ?", (obj["id"],), **page_args
    )


@show_type.field("cast")
def resolve_show_cast(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "show_person", "show_id = ?", (obj["id"],), **page_args
    )


@show_type.field("studioCredits")
def resolve_show_studio_credits(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "show_studio", "show_id = ?", (obj["id"],), **page_args
    )


@show_type.field("relatedShows")
def resolve_show_related_shows(obj, info, **page_args):
    """show_relation is directed, stored as-ingested (§5.9), but read as
    undirected here — either direction counts as a link, same treatment
    franchise auto-derivation itself gives the graph."""
    return pagination.paginate(
        db.get_connection(),
        "show",
        "id IN (SELECT related_show_id FROM show_relation WHERE show_id = ?)"
        " OR id IN (SELECT show_id FROM show_relation WHERE related_show_id = ?)",
        (obj["id"], obj["id"]),
        **page_args,
    )


@show_type.field("franchiseMemberships")
def resolve_show_franchise_memberships(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "franchise_member", "show_id = ?", (obj["id"],), **page_args
    )


@show_type.field("nextUpOverride")
def resolve_show_next_up_override(obj, info):
    row = (
        db.get_connection()
        .execute("SELECT * FROM next_up_override WHERE show_id = ?", (obj["id"],))
        .fetchone()
    )
    return dict(row) if row else None


@show_type.field("tags")
def resolve_show_tags(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(),
        "tag",
        "id IN (SELECT tag_id FROM show_tag WHERE show_id = ?)",
        (obj["id"],),
        **page_args,
    )


@show_type.field("artAssets")
def resolve_show_art_assets(obj, info):
    return art.get_art_assets_for_show(db.get_connection(), obj["id"])


@show_type.field("posterUrl")
def resolve_show_poster_url(obj, info):
    """Selected art_asset poster → existing show.poster_url fallback.

    Only queries art_asset when the show-detail page requests it (not on
    list/calendar pages where N shows would fire N extra queries).  The
    heuristic: if the client also asked for artAssets in this selection
    set, they're on the show page — check the table; otherwise return
    the plain column.  Gracefully handles pre-migration (table missing).
    """
    # Fast path for list/calendar — no art_asset lookup
    field_node = info.field_nodes[0]
    parent_set = field_node.loc.source.body if field_node.loc else ""
    if "artAssets" not in parent_set:
        return obj.get("poster_url")
    try:
        conn = db.get_connection()
        url = art.get_selected_url(conn, obj["id"], None, "poster")
        return url or obj.get("poster_url")
    except Exception:
        return obj.get("poster_url")


@show_type.field("bannerUrl")
def resolve_show_banner_url(obj, info):
    """Selected art_asset banner → existing show.banner_url fallback.

    Same show-page-only gating as posterUrl above.
    """
    field_node = info.field_nodes[0]
    parent_set = field_node.loc.source.body if field_node.loc else ""
    if "artAssets" not in parent_set:
        return obj.get("banner_url")
    try:
        conn = db.get_connection()
        url = art.get_selected_url(conn, obj["id"], None, "banner")
        return url or obj.get("banner_url")
    except Exception:
        return obj.get("banner_url")


def _resolve_episode_kind_poster_url(obj, episode_kind: str) -> str | None:
    """Shared body for specialPosterUrl/ovaPosterUrl/bonusMoviePosterUrl
    below. No fast-path/slow-path split like posterUrl/bannerUrl above —
    these are manual-only (df50e70a1faa), never touched by the
    automated fetch cascade, so there's no denormalised show column and
    no staleness concern the dual-path complexity exists to solve; the
    live art_asset lookup is always authoritative and is only paid for
    when a client actually asks for this rarely-used field."""
    try:
        return art.get_selected_url(
            db.get_connection(), obj["id"], None, "poster", episode_kind=episode_kind,
        )
    except Exception:
        return None


@show_type.field("specialPosterUrl")
def resolve_show_special_poster_url(obj, info):
    return _resolve_episode_kind_poster_url(obj, "special")


@show_type.field("ovaPosterUrl")
def resolve_show_ova_poster_url(obj, info):
    return _resolve_episode_kind_poster_url(obj, "ova")


@show_type.field("bonusMoviePosterUrl")
def resolve_show_bonus_movie_poster_url(obj, info):
    return _resolve_episode_kind_poster_url(obj, "bonus_movie")


# --- Episode fields ------------------------------------------------------


@episode_type.field("show")
def resolve_episode_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@episode_type.field("seasonEntity")
def resolve_episode_season_entity(obj, info):
    if obj.get("season_id") is None:
        return None
    return season_mapping.get_season(db.get_connection(), obj["season_id"])


@episode_type.field("linkedMovieShow")
def resolve_episode_linked_movie_show(obj, info):
    """kind = BONUS_MOVIE only — §5.1/§5.9 addendum."""
    conn = db.get_connection()
    link = conn.execute(
        "SELECT movie_show_id FROM episode_movie_link WHERE episode_id = ?", (obj["id"],)
    ).fetchone()
    if link is None or link["movie_show_id"] is None:
        return None
    return _get_show(conn, link["movie_show_id"])


@episode_type.field("watchEvents")
def resolve_episode_watch_events(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(),
        "watch_event",
        "show_id = ? AND season = ? AND episode = ?",
        (obj["show_id"], obj["season"], obj["episode"]),
        **page_args,
    )


@episode_type.field("airDateHistory")
def resolve_episode_air_date_history(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "air_date_change", "episode_id = ?", (obj["id"],), **page_args
    )


@episode_type.field("anidbMapping")
def resolve_episode_anidb_mapping(obj, info):
    """Memory Alpha — AniDB mapping + per-episode data for this episode.

    JOINs episode_anidb_mapping → anidb_episode to return titles/airdate
    in one hop.  Returns None when unmapped or when anidb_episode hasn't
    been fetched yet for that anime.
    """
    conn = db.get_connection()
    row = conn.execute(
        """SELECT m.anidb_anime_id, m.anidb_season, m.anidb_epno,
                  ae.title_en, ae.title_ja, ae.title_romaji,
                  ae.airdate, ae.length_minutes
           FROM episode_anidb_mapping m
           LEFT JOIN anidb_episode ae
             ON ae.anidb_anime_id = m.anidb_anime_id
             AND ae.anidb_season = m.anidb_season
             AND ae.anidb_epno = m.anidb_epno
           WHERE m.episode_id = ?""",
        (obj["id"],),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


# --- WatchEvent fields -----------------------------------------------------


@watch_event_type.field("show")
def resolve_watch_event_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


# --- ShowExternalId fields ---------------------------------------------------
#
# Found missing in the 2026-08-08 audit pass: no tests had ever requested
# ShowExternalId.show, so this fell through both the earlier field-by-field
# DB-column audit (show_id IS a real column, just not named "show") and every
# existing test (which only ever asked for service/externalId/url).


@show_external_id_type.field("show")
def resolve_show_external_id_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


# --- Season / EpisodeNumberingMapping / EpisodeMovieLink fields ------------


@season_type.field("posterUrl")
def resolve_season_poster_url(obj, info):
    """Selected season art → selected show art → show.poster_url column."""
    try:
        conn = db.get_connection()
        # 1. Season-specific selected poster
        url = art.get_selected_url(conn, obj["show_id"], obj["id"], "poster")
        if url:
            return url
        # 2. Show-level selected poster
        url = art.get_selected_url(conn, obj["show_id"], None, "poster")
        if url:
            return url
    except Exception:
        pass
    # 3. Fall back to show's own poster_url column
    show = _get_show(conn, obj["show_id"])
    return show["poster_url"] if show else None


@season_type.field("artAssets")
def resolve_season_art_assets(obj, info):
    return art.get_art_assets_for_season(db.get_connection(), obj["id"])


@season_type.field("show")
def resolve_season_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@season_type.field("externalIds")
def resolve_season_external_ids(obj, info):
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT service, external_id, name, url FROM season_external_id WHERE season_id = ?",
        (obj["id"],),
    ).fetchall()
    return [dict(r) for r in rows]


@episode_type.field("externalIds")
def resolve_episode_external_ids(obj, info):
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT service, external_id, season_number, episode_number"
        " FROM episode_external_id WHERE episode_id = ?",
        (obj["id"],),
    ).fetchall()
    return [dict(r) for r in rows]


@episode_numbering_mapping_type.field("show")
def resolve_episode_numbering_mapping_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@episode_movie_link_type.field("episode")
def resolve_episode_movie_link_episode(obj, info):
    return _get_episode(db.get_connection(), obj["episode_id"])


@episode_movie_link_type.field("movieShow")
def resolve_episode_movie_link_movie_show(obj, info):
    if obj.get("movie_show_id") is None:
        return None
    return _get_show(db.get_connection(), obj["movie_show_id"])


# --- PendingReview fields --------------------------------------------------


@pending_review_type.field("proposedValueChain")
def resolve_proposed_value_chain(obj, info):
    return json.loads(obj["proposed_value_chain"])


# --- History table fields ---------------------------------------------------
#
# Found missing in the 2026-08-08 audit pass, same class of bug as
# ShowExternalId.show above: none of these four types ever got an
# ObjectType binding at all, so their show/episode relationship fields
# were unresolvable — not caught earlier because every existing test only
# ever asked for the scalar/enum fields (previousStatus, changedBy, etc.),
# never the nested show/episode itself.


@status_change_type.field("show")
def resolve_status_change_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@score_change_type.field("show")
def resolve_score_change_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@air_date_change_type.field("episode")
def resolve_air_date_change_episode(obj, info):
    return _get_episode(db.get_connection(), obj["episode_id"])


@tracked_change_type.field("show")
def resolve_tracked_change_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


# --- ShowServicePresence / Person / Studio / CastCredit / StudioCredit fields (§5.4/§5.8) --


@show_service_presence_type.field("show")
def resolve_show_service_presence_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@person_type.field("credits")
def resolve_person_credits(obj, info, **page_args):
    """Queryable from the person side too (§5.8) — every show_person row
    for this person, across the tracked library."""
    return pagination.paginate(
        db.get_connection(), "show_person", "person_id = ?", (obj["id"],), **page_args
    )


@studio_type.field("credits")
def resolve_studio_credits(obj, info, **page_args):
    """Same reasoning as Person.credits (§5.8)."""
    return pagination.paginate(
        db.get_connection(), "show_studio", "studio_id = ?", (obj["id"],), **page_args
    )


@cast_credit_type.field("show")
def resolve_cast_credit_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@cast_credit_type.field("person")
def resolve_cast_credit_person(obj, info):
    return _get_person(db.get_connection(), obj["person_id"])


@studio_credit_type.field("show")
def resolve_studio_credit_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@studio_credit_type.field("studio")
def resolve_studio_credit_studio(obj, info):
    return _get_studio(db.get_connection(), obj["studio_id"])


# --- Franchise / FranchiseEntry fields (§5.9) -------------------------------


@franchise_type.field("members")
def resolve_franchise_members(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "franchise_member", "franchise_id = ?", (obj["id"],), **page_args
    )


@franchise_entry_type.field("franchise")
def resolve_franchise_entry_franchise(obj, info):
    return _get_franchise(db.get_connection(), obj["franchise_id"])


@franchise_entry_type.field("show")
def resolve_franchise_entry_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@next_up_override_type.field("show")
def resolve_next_up_override_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@tag_type.field("shows")
def resolve_tag_shows(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(),
        "show",
        "id IN (SELECT show_id FROM show_tag WHERE tag_id = ?)",
        (obj["id"],),
        **page_args,
    )


# --- Mutation --------------------------------------------------------------


@mutation.field("addShow")
def resolve_add_show(_, info, input):
    """B.11d — the actual creation logic (insert, external-id links,
    the inline A.8 metadata fetch) now lives in shows.create_show(),
    extracted so show_backfill.py can call it directly without a
    GraphQL request context — this resolver is a thin wrapper,
    unchanged behavior/shape.

    Write-mirror gap #1 (todo.md, 2026-08-15/16) closed here, not in
    shows.create_show() itself: status-at-creation is a client-facing
    "I just added this show" event, distinct from show_backfill.py's
    own _seed_status_from_anilist (that path *reads* an existing
    AniList status in, deliberately never pushes). create_show()'s own
    fetch_and_populate may attach an anilist_id via Fribb during
    creation, so the push happens after, against the show's real final
    status (always 'planned' today — AddShowInput has no status field
    — but this reads it back rather than hardcoding, so a future status
    field/_promote_stub's own pre-existing status both push correctly
    without any change here)."""
    conn = db.get_connection()
    try:
        show_id = shows.create_show(conn, input)
    except shows.ShowInputError as e:
        raise GraphQLError(str(e)) from e
    show = _get_show(conn, show_id)
    _push_show_status(conn, show_id, show["status"])
    return show


@mutation.field("addShowWithArr")
def resolve_add_show_with_arr(_, info, input):
    """B.21 — thin wrapper around shows.create_show_with_arr_add(), same
    shape as resolve_add_show above (including the same status-at-
    creation push) plus the richer AddShowResult. See schema.graphql's
    own addShowWithArr docstring and shows.py's B.21 section docstring
    for the full design — in particular, why a Sonarr/Radarr-side
    failure here raises (ShowInputError -> GraphQLError) rather than
    following every AniList push's own best-effort/pending_review
    shape."""
    conn = db.get_connection()
    try:
        show_id, arr_result = shows.create_show_with_arr_add(conn, input)
    except shows.ShowInputError as e:
        raise GraphQLError(str(e)) from e
    show = _get_show(conn, show_id)
    _push_show_status(conn, show_id, show["status"])
    return {
        "show": show,
        # snake_case keys — convert_names_case=True (server.py) maps these to
        # AddShowResult's camelCase fields automatically, same as every plain
        # dict-of-DB-row return elsewhere in this file; a camelCase key here
        # would silently resolve to null on every field (caught by the real
        # non-nullable-field GraphQL error test_add_show_with_arr's own tests
        # surfaced, not assumed correct).
        "sonarr_series_created": arr_result["sonarr_created"],
        "radarr_movie_created": arr_result["radarr_created"],
        "matched_title": arr_result["matched_title"],
        "matched_tvdb_id": arr_result["matched_tvdb_id"],
        "matched_tmdb_id": arr_result["matched_tmdb_id"],
    }


@mutation.field("skipShow")
def resolve_skip_show(_, info, input):
    """Browse "skip" — creates a minimal stub (status='skipped', tracked=0)
    or marks an existing untracked stub as skipped.  No metadata fetch,
    no Sonarr/Radarr, no AniList/MAL push.  If the show is already tracked,
    returns it unchanged (use setStatus instead)."""
    conn = db.get_connection()
    try:
        show_id = shows.skip_show(conn, input)
    except shows.ShowInputError as e:
        raise GraphQLError(str(e)) from e
    return _get_show(conn, show_id)


@mutation.field("refreshShowMetadata")
def resolve_refresh_show_metadata(_, info, show_id):
    """A.8 — the manual-retry half of the "best effort and system to
    try again... manual fix by user is also an option" policy
    (confirmed 2026-08-08): calls the exact same fetch_and_populate()
    addShow already calls inline, callable independently any time
    (e.g. after seeing a metadata_fetch pending_review entry, once
    whatever was unreachable is back). No require_client() — same
    reasoning as reconcileSeasonMapping/refreshShowServicePresence:
    nothing here writes to a changed_by-style column."""
    conn = db.get_connection()
    _require_show(conn, show_id)
    metadata.fetch_and_populate(conn, show_id)
    conn.commit()
    return _get_show(conn, show_id)


@mutation.field("pollFileAvailability")
def resolve_poll_file_availability(_, info):
    """§5.2/§6.7, B.3 — the global availability sweep (availability.py):
    polls Sonarr's/Radarr's own /history since each service's own
    checkpoint, no per-item argument (one shared history feed covers
    every tracked show at once). No require_client() — same reasoning
    as refreshShowMetadata/reconcileSeasonMapping: nothing here writes
    to a changed_by-style column, this is passive/informational the same
    way show_service_presence's own refresh is (§5.4). A never-polled
    service seeds its checkpoint to "now" and does no work here — see
    availability.py's own module docstring and backfillFileAvailability
    below for the deliberate full-history counterpart."""
    conn = db.get_connection()
    return availability.poll_file_availability(conn)


@mutation.field("backfillFileAvailability")
def resolve_backfill_file_availability(_, info):
    """§5.2/§6.7, B.3 — the manual, one-time counterpart to
    pollFileAvailability above: walks each configured service's entire
    history, ignoring any existing checkpoint. Not called by Ops's own
    automatic loop, only by the explicit `ops backfill-availability`
    CLI command — genuinely blocks LCARS's single request-handling
    thread for real seconds-to-minutes while it runs, by design (see
    its own schema.graphql docstring). Same passive/no require_client()
    reasoning as pollFileAvailability."""
    conn = db.get_connection()
    return availability.backfill_file_availability(conn)


@mutation.field("auditLocalFiles")
def resolve_audit_local_files(_, info):
    """§5.2/§6.10, B.3b — local_audit.py's own two-pass audit: a
    pure-API current-state reconciliation (no require_client() — same
    passive/not-a-changed_by-column reasoning as pollFileAvailability/
    backfillFileAvailability) plus a filesystem-reading orphan/
    untracked-show discovery pass. Not called by Ops's own automatic
    loop, only by `ops audit-local-files` — see its own schema.graphql
    docstring for the full rationale."""
    conn = db.get_connection()
    return local_audit.audit_local_files(conn)


@mutation.field("auditLocalFilesForShow")
def resolve_audit_local_files_for_show(_, info, show_id):
    """NEXT_UP.md, 2026-08-19 — the per-show scope of auditLocalFiles
    above; see schema.graphql's own docstring for the full rationale.
    `_require_show` raises for an unknown show_id — local_audit.py's
    own function trusts a valid id and just returns the empty result
    for every other "nothing to correct against" case (no link, service
    not configured, id unknown to Sonarr/Radarr itself)."""
    conn = db.get_connection()
    _require_show(conn, show_id)
    return local_audit.audit_local_files_for_show(conn, show_id)


@mutation.field("reconcileArrState")
def resolve_reconcile_arr_state(_, info):
    """NEXT_UP.md follow-up (2026-09-19) — see schema.graphql's own
    docstring for the full rationale. local_audit.reconcile_arr_state()
    does the read-heavy Sonarr/Radarr work and returns pause/resume
    *intent* as plain show_id lists (`to_pause`/`to_resume`) rather than
    writing show.status itself — that write needs `_apply_status_change`
    (AniList/MAL push, season fanout, arr re-monitor-on-resume), which
    lives in this module, not local_audit.py (which resolvers.py already
    imports — the reverse import would be circular). Applied here, one
    call each, same "changed_by" convention availability.py's webhook
    handlers use for a non-client-initiated write. No require_client() —
    same passive reasoning auditLocalFiles already has; Ops's own loop
    calls this with no client identity of its own to assert.

    Each apply is its own try/except — one show_id failing (e.g.
    deleted between local_audit's own read and this applying it) must
    not abort the rest of the batch, same "one bad entry doesn't block
    the others" shape this whole feature already uses throughout
    (availability.py's webhook handlers, local_audit's own
    _create_from_untracked_entry)."""
    conn = db.get_connection()
    raw = local_audit.reconcile_arr_state(conn)
    paused_ids: list[str] = []
    for show_id in raw["to_pause"]:
        try:
            _apply_status_change(conn, show_id, "paused", "reconcile")
            paused_ids.append(show_id)
        except GraphQLError:
            log.exception("reconcileArrState: failed to pause %s", show_id)
    resumed_ids: list[str] = []
    for show_id in raw["to_resume"]:
        row = conn.execute(
            "SELECT status_before_pause FROM show WHERE id = ?", (show_id,)
        ).fetchone()
        target = (row["status_before_pause"] if row else None) or "watching"
        try:
            _apply_status_change(conn, show_id, target, "reconcile")
            resumed_ids.append(show_id)
        except GraphQLError:
            log.exception("reconcileArrState: failed to resume %s", show_id)
    return {
        "episodes_corrected": raw["episodes_corrected"],
        "shows_corrected": raw["shows_corrected"],
        "shows_created": raw["shows_created"],
        "shows_create_failed": len(raw["create_failures"]),
        "paused_show_ids": paused_ids,
        "resumed_show_ids": resumed_ids,
    }


@query.field("previewShowBackfill")
def resolve_preview_show_backfill(_, info):
    """§5.1/§5.2, B.11d — dry-run, no writes. See show_backfill.py's
    own module docstring for the full rationale."""
    conn = db.get_connection()
    return show_backfill.preview_backfill(conn)


@query.field("untrackedShowFindings")
def resolve_untracked_show_findings(_, info, **page_args):
    """§5.2, B.11e — the persisted result of pollUntrackedShows below,
    not a recomputation (a Query stays side-effect free)."""
    return pagination.paginate(
        db.get_connection(), "untracked_show_finding", "1 = 1", (), **page_args
    )


@mutation.field("backfillUntrackedShows")
def resolve_backfill_untracked_shows(_, info):
    """§5.1/§5.2, B.11d — the real run. No require_client(): each
    created show's own addShow-equivalent write path
    (shows.create_show()) already carries no changed_by-style column
    of its own (addShow itself never required one either); the one
    history write this does make (status_change, when an anime show's
    initial status is seeded from AniList) uses its own distinct
    'show_backfill' changed_by value rather than a client header this
    call has no requester context to supply anyway. Not called by
    Ops's own automatic loop, only by `ops backfill-shows` — see its
    own schema.graphql docstring for the full rationale."""
    conn = db.get_connection()
    return show_backfill.backfill_untracked_shows(conn)


@mutation.field("pollUntrackedShows")
def resolve_poll_untracked_shows(_, info):
    """§5.2, B.11e — untracked_sweep.py's own recurring sweep. No
    require_client() — same passive/not-a-changed_by-column reasoning
    every other poll* mutation already established; called by Ops's own
    automatic loop, not a human-triggered one-shot like
    backfillUntrackedShows above."""
    conn = db.get_connection()
    return untracked_sweep.sweep_untracked_shows(conn)


# -- B.14 cross-service show-merge --------------------------------------


@query.field("showMerges")
def resolve_show_merges(_, info, **page_args):
    """§5.0/B.14 — the persisted, reviewable merge log. A Query, so it
    stays side-effect free — same "not a recomputation" split
    untrackedShowFindings above already establishes against its own
    poll* mutation."""
    return pagination.paginate(db.get_connection(), "show_merge", "1 = 1", (), **page_args)


@mutation.field("pollShowMerges")
def resolve_poll_show_merges(_, info):
    """§5.0, B.14 — show_merge.py's own cross-service duplicate
    *discovery* sweep. No require_client() — same passive/not-a-
    changed_by-column reasoning every other poll* mutation already
    established; called by Ops's own automatic loop (monthly tier,
    real N×M cost — see ops/scheduler.py's own run_monthly_once).

    2026-08-12: no longer merges anything itself — see show_merge.py's
    own "Auto-merge retired" module-docstring note for why. Only opens
    pending_review entries; applyShowMerge below is what actually acts
    on one."""
    conn = db.get_connection()
    result = show_merge.sweep_show_merges(conn)
    return {
        "candidates_found": result["candidates_found"],
        "reviews_opened": result["reviews_opened"],
    }


@mutation.field("applyShowMerge")
def resolve_apply_show_merge(_, info, winner_id, loser_id, matched_on):
    """2026-08-12 — the human-triggered action pollShowMerges used to
    take automatically before real false positives were found live
    (show_merge.py's own module docstring has the full story). Requires
    RESOLVING_CLIENTS, same restriction resolvePendingReview already
    has — this genuinely is resolving a reviewed value discrepancy
    (which show, if any, this loser should merge into), unlike
    reverseShowMerge's own deliberately-unrestricted "undo a specific
    action" shape."""
    conn = db.get_connection()
    client = require_client(info)
    if client not in RESOLVING_CLIENTS:
        raise GraphQLError(
            f"{client!r} cannot apply a show merge — only {sorted(RESOLVING_CLIENTS)} can (§5.6)"
        )
    try:
        merge_id = show_merge.apply_show_merge(conn, winner_id, loser_id, matched_on, client)
    except ValueError as e:
        raise GraphQLError(str(e)) from e
    return _get_show_merge(conn, merge_id)


@mutation.field("reverseShowMerge")
def resolve_reverse_show_merge(_, info, id):
    """§5.0, B.14 — a human-triggered corrective action (unlike
    pollShowMerges above), so require_client() applies, same as
    resolvePendingReview. Any client may reverse a merge — no
    RESOLVING_CLIENTS-style restriction: this isn't reviewing a value
    discrepancy, it's undoing a specific automatic action, and nothing
    in the B.14 design discussion restricted who can do that."""
    conn = db.get_connection()
    client = require_client(info)
    try:
        show_merge.reverse_show_merge(conn, id, client)
    except ValueError as e:
        raise GraphQLError(str(e)) from e
    return _get_show_merge(conn, id)


@mutation.field("resolveFranchiseMerge")
def resolve_resolve_franchise_merge(
    _, info, review_id, action,
    correct_parent_id=None, correct_season=None,
    corrected_tvdb_id=None, corrected_anilist_id=None,
):
    conn = db.get_connection()
    client = require_client(info)
    if client not in RESOLVING_CLIENTS:
        raise GraphQLError(
            f"{client!r} cannot resolve a franchise merge — only "
            f"{sorted(RESOLVING_CLIENTS)} can"
        )

    review = conn.execute(
        "SELECT * FROM pending_review WHERE id = ?"
        " AND field IN ('franchise_auto_merge', 'franchise_season_collision')",
        (review_id,),
    ).fetchone()
    if review is None:
        raise GraphQLError(f"no such franchise merge review: {review_id}")
    if review["resolved_at"] is not None:
        raise GraphQLError(f"review {review_id} already resolved")

    is_season_collision = review["field"] == "franchise_season_collision"
    chain = json.loads(review["proposed_value_chain"])
    # The last chain entry is the parent_id (used as the dedup key).
    parent_id_from_chain = chain[-1]
    now = util.now_utc_iso()

    if is_season_collision:
        # Season collision reviews have no merge — only confirm (with correct
        # season) or reject.
        child_id = review["entity_id"]

        if action == "confirm":
            if correct_season is None:
                raise GraphQLError("confirm on a season collision requires correctSeason")
            merge_id = show_merge.merge_season_into_show(
                conn, correct_parent_id or parent_id_from_chain, child_id, correct_season,
                "manual season correction (tvdb collision)",
            )
            conn.execute(
                "UPDATE pending_review SET resolved_at = ?, resolved_by_client = ?,"
                " resolution_note = ? WHERE id = ?",
                (now, client, f"confirmed as S{correct_season}", review_id),
            )
            conn.commit()
            return _get_show_merge(conn, merge_id)

        if action == "reject":
            _correct_child_ids(
                conn, child_id, corrected_tvdb_id, corrected_anilist_id, now
            )
            conn.execute(
                "UPDATE pending_review SET resolved_at = ?, resolved_by_client = ?,"
                " resolution_note = ? WHERE id = ?",
                (now, client, "rejected — IDs corrected", review_id),
            )
            conn.commit()
            # No merge to return — return the parent show's merge row if any,
            # or raise.
            existing = conn.execute(
                "SELECT id FROM show_merge WHERE winner_show_id = ? AND loser_show_id = ?"
                " ORDER BY merged_at DESC LIMIT 1",
                (parent_id_from_chain, child_id),
            ).fetchone()
            if existing:
                return _get_show_merge(conn, existing["id"])
            raise GraphQLError(
                "rejected season collision (no merge existed to return)"
            )

        raise GraphQLError(
            f"season collision reviews support confirm/reject, not {action!r}"
        )

    # franchise_auto_merge — a merge exists.
    merge_row = conn.execute(
        "SELECT id FROM show_merge"
        " WHERE winner_show_id = ? AND loser_show_id = ? AND reversed_at IS NULL"
        " ORDER BY merged_at DESC LIMIT 1",
        (parent_id_from_chain, review["entity_id"]),
    ).fetchone()
    if merge_row is None:
        raise GraphQLError(f"no unreversed merge found for review {review_id}")
    merge_id = merge_row["id"]

    if action == "confirm":
        conn.execute(
            "UPDATE pending_review SET resolved_at = ?, resolved_by_client = ?,"
            " resolution_note = ? WHERE id = ?",
            (now, client, "confirmed", review_id),
        )
        conn.commit()
        return _get_show_merge(conn, merge_id)

    if action == "redirect":
        if correct_season is None:
            raise GraphQLError("redirect requires correctSeason")
        redirect_parent = correct_parent_id or parent_id_from_chain
        show_merge.reverse_season_merge(conn, merge_id, client)
        child_id = review["entity_id"]
        new_merge_id = show_merge.merge_season_into_show(
            conn, redirect_parent, child_id, correct_season,
            f"manual redirect from {parent_id_from_chain}",
        )
        conn.execute(
            "UPDATE pending_review SET resolved_at = ?, resolved_by_client = ?,"
            " resolution_note = ? WHERE id = ?",
            (now, client, f"redirected to {redirect_parent} S{correct_season}", review_id),
        )
        conn.commit()
        return _get_show_merge(conn, new_merge_id)

    if action == "reject":
        show_merge.reverse_season_merge(conn, merge_id, client)
        child_id = review["entity_id"]
        _correct_child_ids(
            conn, child_id, corrected_tvdb_id, corrected_anilist_id, now
        )
        conn.execute(
            "UPDATE pending_review SET resolved_at = ?, resolved_by_client = ?,"
            " resolution_note = ? WHERE id = ?",
            (now, client, "rejected — IDs corrected", review_id),
        )
        conn.commit()
        return _get_show_merge(conn, merge_id)

    raise GraphQLError(f"unknown action: {action!r} (expected confirm/redirect/reject)")


def _correct_child_ids(conn, child_id, corrected_tvdb_id, corrected_anilist_id, now):
    """Apply corrected external IDs on a rejected franchise merge child."""
    if corrected_tvdb_id is not None:
        conn.execute(
            "UPDATE show_external_id SET external_id = ? WHERE show_id = ? AND service = 'tvdb'",
            (corrected_tvdb_id, child_id),
        )
    if corrected_anilist_id is not None:
        existing = conn.execute(
            "SELECT 1 FROM show_external_id WHERE show_id = ? AND service = 'anilist'",
            (child_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE show_external_id SET external_id = ?"
                " WHERE show_id = ? AND service = 'anilist'",
                (str(corrected_anilist_id), child_id),
            )
        else:
            conn.execute(
                "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                " VALUES (?, 'anilist', ?, ?, ?)",
                (child_id, str(corrected_anilist_id),
                 f"https://anilist.co/anime/{corrected_anilist_id}", now),
            )


@show_merge_type.field("winnerShow")
def resolve_show_merge_winner_show(obj, info):
    return _get_show(db.get_connection(), obj["winner_show_id"])


@show_merge_type.field("loserShow")
def resolve_show_merge_loser_show(obj, info):
    return _get_show(db.get_connection(), obj["loser_show_id"])


@show_merge_type.field("manifest")
def resolve_show_merge_manifest(obj, info):
    """Flattened to a human-readable string list for GraphQL — same
    "keep it plainly inspectable, don't invent nested types for an
    audit blob" convention proposedValueChain (§5.6) already uses for
    pending_review. moved's own per-table detail (exact ids/keys) is
    what reverseShowMerge actually replays; this rendering is for a
    human reviewing the log, not a machine consumer."""
    parsed = json.loads(obj["manifest"])
    lines = []
    for table, entries in parsed["moved"].items():
        if not entries:
            continue
        count = entries if isinstance(entries, bool) else len(entries)
        lines.append(f"moved {table}: {count}")
    lines.extend(f"skipped: {s}" for s in parsed["skipped"])
    return lines


@mutation.field("pollAnimeSchedule")
def resolve_poll_anime_schedule(_, info):
    """§6.7, B.5 — animeschedule.py's own global RSS sweep. No
    require_client() — same passive/not-a-changed_by-column reasoning
    as pollFileAvailability/auditLocalFiles above."""
    conn = db.get_connection()
    return animeschedule.poll_anime_schedule(conn)


@mutation.field("pollSeasonSubdivision")
def resolve_poll_season_subdivision(_, info):
    """S5 — season_ranges.check_subdivision_widths's global AniList
    width sweep. No require_client() — same passive reasoning as
    pollAnimeSchedule: not a user-facing write, no changed_by column."""
    conn = db.get_connection()
    return season_ranges.check_subdivision_widths(conn)


@mutation.field("pollScoreSync")
def resolve_poll_score_sync(_, info):
    """2026-08-27 — score_sync drift sweeps for both AniList and MAL.
    Runs check_anilist_score_drift then check_mal_score_drift in sequence
    (both read-heavy, no contention), returns combined counts.  No
    require_client() — same passive/Ops-internal reasoning as
    pollSeasonSubdivision: opens pending_review for human confirmation
    rather than applying anything automatically."""
    conn = db.get_connection()
    al = score_sync.check_anilist_score_drift(conn)
    mal = score_sync.check_mal_score_drift(conn)
    # snake_case keys: convert_names_case=True (server.py) maps
    # anilistChecked -> anilist_checked. The camelCase keys this used to
    # return resolved to null, so every call errored after the sweep had
    # already run (2026-09-23; ops logged "sweep failed" on every tick).
    return {
        "anilist_checked": al["checked"],
        "anilist_flagged": al["flagged"],
        "mal_checked": mal["checked"],
        "mal_flagged": mal["flagged"],
    }


@mutation.field("pollIdentityMismatch")
def resolve_poll_identity_mismatch(_, info):
    """2026-09-21 — the AniList-ID-vs-Fribb identity mismatch sweep
    (identity_mismatch.py), found via the real Tantei/Milky-Holmes
    incident. No require_client() — same passive/Ops-internal reasoning
    as pollScoreSync: opens pending_review for human confirmation rather
    than applying anything automatically."""
    conn = db.get_connection()
    result = identity_mismatch.check_anilist_id_mismatch(conn)
    return {"checked": result["checked"], "flagged": result["flagged"]}


@mutation.field("pollLocalServicePresence")
def resolve_poll_local_service_presence(_, info):
    """§5.4/§6.7, B.7 — service_presence.py's own local rollup. No
    require_client() — same passive/not-a-changed_by-column reasoning
    refreshShowServicePresence itself already established."""
    conn = db.get_connection()
    return {"shows_updated": service_presence.refresh_local_presence(conn)}


@mutation.field("pollCatalogServicePresence")
def resolve_poll_catalog_service_presence(_, info):
    """§5.4/§6.7, B.7 — service_presence.py's own Sonarr/Radarr catalog
    sweep. Same passive reasoning as pollLocalServicePresence above."""
    conn = db.get_connection()
    return {"shows_updated": service_presence.refresh_catalog_presence(conn)}


@mutation.field("backfillTvdbIds")
def resolve_backfill_tvdb_ids(_, info):
    """2026-08-18 — tvdb_backfill.py's own Fribb reverse-lookup sweep.
    Same passive reasoning as pollLocalServicePresence above (no
    require_client(), no changed_by column — a bulk sweep, not a
    targeted human decision). No live outbound Sonarr/Radarr call
    (unlike pollCatalogServicePresence) — Ops rides its own hourly
    tick for this, not the monthly one; see schema.graphql's own
    docstring for the full cost reasoning."""
    conn = db.get_connection()
    return {"shows_updated": tvdb_backfill.backfill_tvdb_ids(conn)}


@mutation.field("backfillShowPosters")
def resolve_backfill_show_posters(_, info):
    """NEXT_UP.md, 2026-09-19 — see schema.graphql's own docstring for
    the full rationale. No require_client() — same passive, not-a-
    targeted-human-decision reasoning backfillTvdbIds already has;
    real outbound calls per show, so unlike that one this isn't on
    Ops's automatic loop, only `ops backfill-posters`."""
    conn = db.get_connection()
    result = metadata.backfill_show_posters(conn)
    return {
        "shows_checked": result["shows_checked"],
        "posters_filled": result["posters_filled"],
        "failed": result["failed"],
    }


@mutation.field("reconcileEpisodeMovieLinks")
def resolve_reconcile_episode_movie_links(_, info):
    """§5.1/§6.7, B.8b — episode_movie_link.py's own automatic
    tmdb_match derivation. No require_client() — same passive/not-a-
    changed_by-column reasoning as pollLocalServicePresence above."""
    conn = db.get_connection()
    return episode_movie_link.reconcile_episode_movie_links(conn)


@mutation.field("refreshMalTokenIfDue")
def resolve_refresh_mal_token_if_due(_, info):
    """§6.9, B.10 — the proactive weekly-ish renewal job BUILD_PLAN.md's
    own B.10 text calls for ("build the proactive refresh-token renewal
    job now, not later"). Self-gating internally (same "a checkpoint
    decides, not a separate due-query" shape pollAnimeSchedule/
    pollFileAvailability already use) rather than B.1/B.2's per-item
    due-query-list shape — this isn't a list of per-show items, it's one
    global credential, so Ops calls it unconditionally every hourly
    tick and almost every call is a cheap no-op. Not yet authenticated
    (no mal_client_id or mal_refresh_token at all) is the same "not
    configured" no-op every other best-effort integration here gets.

    **A real fix specific to this mutation, not present anywhere else
    in this codebase before now**: `config.get_current()` returns a
    process-wide singleton set once at startup (config.py's own
    docstring) — persisting the refreshed tokens to `lcars.ini` via
    `save_mal_tokens()` alone would leave every push this same running
    process makes afterward still reading the *stale* in-memory
    access_token until a restart. Both the file and the live
    singleton's own attributes are updated here."""
    conn = db.get_connection()
    cfg = config.get_current()
    if not cfg.mal_client_id or not cfg.mal_refresh_token:
        return {"refreshed": False}
    cutoff = util.utc_iso_offset(-7)
    due = cfg.mal_token_refreshed_at is None or cfg.mal_token_refreshed_at < cutoff
    if not due:
        return {"refreshed": False}
    try:
        access_token, refresh_token = mal_client.refresh_access_token(
            cfg.mal_client_id, cfg.mal_client_secret, cfg.mal_refresh_token
        )
    except mal_client.MALError as e:
        service_health.record_failure(conn, "mal", str(e))
        conn.commit()
        return {"refreshed": False}
    service_health.record_success(conn, "mal")
    config.save_mal_tokens(access_token, refresh_token)
    cfg.mal_access_token = access_token
    cfg.mal_refresh_token = refresh_token
    cfg.mal_token_refreshed_at = util.now_utc_iso()
    conn.commit()
    return {"refreshed": True}


@mutation.field("reconcileWatchProgress")
def resolve_reconcile_watch_progress(_, info):
    """B.15, live-caught 2026-08-12 — see watch_reconcile.py's own
    module docstring for the full scope/reasoning. No require_client()
    here, same "bulk sweep, not a targeted human decision" reasoning
    pollFileAvailability/auditLocalFiles already use — changed_by on
    every status_change row this writes is the fixed literal
    "anilist_reconcile" (changed_by is open-ended TEXT by design,
    §5.7, already documented as covering process names like
    "sonarr_sync"/"anilist_sync", not just client identities)."""
    conn = db.get_connection()
    return watch_reconcile.reconcile_watch_progress(conn)


@mutation.field("pollAnilistActivity")
def resolve_poll_anilist_activity(_, info):
    """B.5.3, 2026-08-13 — see watch_reconcile.py's own module-level
    comment for the full design rationale. No require_client() — same
    "bulk sweep, not a targeted human decision" reasoning
    pollFileAvailability/reconcileWatchProgress already use; whatever
    it triggers (reconcile_watch_progress) already stamps its own fixed
    "anilist_reconcile" changed_by, unaffected by this mutation's own
    caller."""
    conn = db.get_connection()
    return watch_reconcile.poll_anilist_activity(conn)


@mutation.field("pollMalList")
def resolve_poll_mal_list(_, info):
    """MAL → LCARS reverse sync (2026-08-26) — the MAL half of the
    bidirectional mirror. Unlike pollAnilistActivity there's no cheap
    activity-feed pre-check (MAL has none), so this fetches the whole
    list and diffs every time (mal_reconcile.py). No require_client() —
    same "bulk sweep, not a targeted human decision" reasoning; the
    reconcile stamps its own fixed 'mal_reconcile' changed_by."""
    conn = db.get_connection()
    return mal_reconcile.reconcile_mal_progress(conn)


@mutation.field("pollMemoryAlpha")
def resolve_poll_memory_alpha(_, info):
    """Memory Alpha ops loop (2026-09-07) — refreshes Anime-Lists XML +
    AniDB titles dump if stale (weekly), re-derives episode mappings on
    data change, drip-fetches per-episode data from AniDB HTTP API
    (5 shows/tick), fills episode.title gaps. No require_client() —
    same passive reasoning as pollAnimeSchedule."""
    conn = db.get_connection()
    return anidb.poll_memory_alpha(conn)


@query.field("recommendedAvailabilityPollIntervalSeconds")
def resolve_recommended_availability_poll_interval_seconds(_, info):
    conn = db.get_connection()
    return availability.recommended_poll_interval_seconds(conn)


@query.field("opsTierDue")
def resolve_ops_tier_due(_, info, tier, interval_seconds):
    """See schema.graphql. Due when no checkpoint exists or the last
    completion is older than `interval_seconds`."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT last_completed_at FROM ops_tier_checkpoint WHERE tier = ?", (tier,)
    ).fetchone()
    if row is None:
        return True
    return row["last_completed_at"] <= util.utc_iso_offset_hours(-interval_seconds / 3600)


@mutation.field("markOpsTierCompleted")
def resolve_mark_ops_tier_completed(_, info, tier):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO ops_tier_checkpoint (tier, last_completed_at) VALUES (?, ?)"
        " ON CONFLICT (tier) DO UPDATE SET last_completed_at = excluded.last_completed_at",
        (tier, util.now_utc_iso()),
    )
    conn.commit()
    return True


@query.field("serviceHealth")
def resolve_service_health(_, info):
    """§6.7, B.6 — service_health.py's own get_all(), already shaped as
    one entry per TrackedService with an "unknown" placeholder
    synthesized for a never-contacted service."""
    conn = db.get_connection()
    return service_health.get_all(conn)


def _grab_file_paths_sonarr(conn, grabbed: list[dict]) -> dict[int, tuple[str | None, str | None]]:
    """Batch-looks up (file_path_sonarr, show_id) for a list of Sonarr
    grab records.

    Returns a dict of record-index → (file_path_sonarr, show_id) — path
    is None when the episode isn't in LCARS or hasn't been imported yet;
    show_id is returned alongside it (2026-09-19, mpv watched-status
    reporting) since it's already resolved here for the path lookup.
    Two queries: one to resolve tvdb_id → show_id, one to fetch all
    matching episode rows from that set of shows.  Both run against the
    LCARS DB the resolver already has open — no extra connections."""
    keys: list[tuple[str, int | None, int | None]] = []
    for r in grabbed:
        tvdb_id = str((r.get("series") or {}).get("tvdbId", "") or "")
        ep = r.get("episode") or {}
        keys.append((tvdb_id, ep.get("seasonNumber"), ep.get("episodeNumber")))

    tvdb_id_set = list({k[0] for k in keys if k[0]})
    if not tvdb_id_set:
        return {}

    rows = conn.execute(
        "SELECT external_id, show_id FROM show_external_id"
        " WHERE service = 'tvdb' AND external_id IN ({})".format(
            ",".join("?" * len(tvdb_id_set))
        ),
        tvdb_id_set,
    ).fetchall()
    tvdb_to_show = {str(r_["external_id"]): r_["show_id"] for r_ in rows}

    show_ids = list(set(tvdb_to_show.values()))
    if not show_ids:
        return {}

    placeholders = ",".join("?" * len(show_ids))
    ep_rows = conn.execute(
        "SELECT show_id, sonarr_season, sonarr_episode, file_path_sonarr"
        " FROM episode"
        f" WHERE show_id IN ({placeholders}) AND sonarr_season IS NOT NULL"
        " AND file_path_sonarr IS NOT NULL",
        show_ids,
    ).fetchall()
    fp_map: dict[tuple[str, int, int], str] = {
        (r_["show_id"], r_["sonarr_season"], r_["sonarr_episode"]): r_["file_path_sonarr"]
        for r_ in ep_rows
    }

    result: dict[int, tuple[str | None, str | None]] = {}
    for i, (tvdb_id, s_num, e_num) in enumerate(keys):
        show_id = tvdb_to_show.get(tvdb_id)
        if show_id and s_num is not None and e_num is not None:
            result[i] = (fp_map.get((show_id, s_num, e_num)), show_id)
    return result


def _grab_file_paths_radarr(conn, grabbed: list[dict]) -> dict[int, tuple[str | None, str | None]]:
    """Batch-looks up (file_path_radarr, show_id) for a list of Radarr
    grab records.

    Radarr movies live in LCARS as single-show entries whose file path is
    stored on the `show` row itself (set by local_audit / availability
    webhook), not on an episode row.  Resolves tmdb_id → show_id →
    show.file_path_radarr. show_id is returned alongside the path (2026-
    09-19, mpv watched-status reporting) — already resolved here for the
    path lookup, just not previously surfaced."""
    tmdb_ids: list[str] = []
    for r in grabbed:
        tmdb_id = str((r.get("movie") or {}).get("tmdbId", "") or "")
        tmdb_ids.append(tmdb_id)

    tmdb_id_set = list({t for t in tmdb_ids if t})
    if not tmdb_id_set:
        return {}

    rows = conn.execute(
        "SELECT external_id, show_id FROM show_external_id"
        " WHERE service = 'tmdb' AND external_id IN ({})".format(
            ",".join("?" * len(tmdb_id_set))
        ),
        tmdb_id_set,
    ).fetchall()
    tmdb_to_show = {str(r_["external_id"]): r_["show_id"] for r_ in rows}

    show_ids = list(set(tmdb_to_show.values()))
    if not show_ids:
        return {}

    # Movie path lives on the show row (set by local_audit / availability
    # webhook), not on episode.file_path_radarr (that column is only used
    # for the bonus-movie-episode linking case in episode_movie_link.py).
    show_rows = conn.execute(
        "SELECT id, file_path_radarr FROM show"
        " WHERE id IN ({}) AND file_path_radarr IS NOT NULL".format(
            ",".join("?" * len(show_ids))
        ),
        show_ids,
    ).fetchall()
    show_to_fp: dict[str, str] = {r_["id"]: r_["file_path_radarr"] for r_ in show_rows}

    result: dict[int, tuple[str | None, str | None]] = {}
    for i, tmdb_id in enumerate(tmdb_ids):
        show_id = tmdb_to_show.get(tmdb_id)
        if show_id:
            result[i] = (show_to_fp.get(show_id), show_id)
    return result


@query.field("recentGrabs")
def resolve_recent_grabs(_, info, service: str, page: int = 1, page_size: int = 20):
    """2026-08-27 — recent grab events from Sonarr or Radarr, used by
    Data's G screen.  Proxies Sonarr/Radarr's own /history endpoint
    (already used by availability.py's sweep) filtered to 'grabbed'
    events only — imports, deletes, and rejected grabs are noise here.

    Returns empty list (no error) when the service is not configured or
    the client raises — the screen degrades gracefully rather than
    blowing up the whole query.  No require_client(): read-only, Ops-
    internal, same reasoning serviceHealth uses.

    airDate: episode.airDateUtc from Sonarr; null for Radarr (movies don't
    have a single "air date" in the same sense, and the Radarr history record
    doesn't reliably surface release dates).  filePath/showId: both looked up
    in the LCARS DB via _grab_file_paths_{sonarr,radarr} (showId added
    2026-09-19 for mpv watched-status reporting) — both null when the show
    isn't tracked in LCARS; filePath alone can still be null on a tracked
    show whose file hasn't been imported yet."""
    cfg = config.get_current()
    conn = db.get_connection()
    try:
        if service == "sonarr":
            if not cfg.sonarr_url or not cfg.sonarr_api_key:
                return []
            with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
                data = client.history_page(page=page, page_size=page_size)
            grabbed = [r for r in (data.get("records") or []) if r.get("eventType") == "grabbed"]
            fp_by_idx = _grab_file_paths_sonarr(conn, grabbed)
            return [
                {
                    "service": "sonarr",
                    "title": (r.get("series") or {}).get("title") or r.get("sourceTitle", ""),
                    "release_title": r.get("sourceTitle", ""),
                    "date": r.get("date", ""),
                    "quality": ((r.get("quality") or {}).get("quality") or {}).get("name"),
                    "season_number": (r.get("episode") or {}).get("seasonNumber"),
                    "episode_number": (r.get("episode") or {}).get("episodeNumber"),
                    "air_date": (r.get("episode") or {}).get("airDateUtc"),
                    "file_path": fp_by_idx.get(i, (None, None))[0],
                    "show_id": fp_by_idx.get(i, (None, None))[1],
                }
                for i, r in enumerate(grabbed)
            ]
        if service == "radarr":
            if not cfg.radarr_url or not cfg.radarr_api_key:
                return []
            with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
                data = client.history_page(page=page, page_size=page_size)
            grabbed = [r for r in (data.get("records") or []) if r.get("eventType") == "grabbed"]
            fp_by_idx = _grab_file_paths_radarr(conn, grabbed)
            return [
                {
                    "service": "radarr",
                    "title": (r.get("movie") or {}).get("title") or r.get("sourceTitle", ""),
                    "release_title": r.get("sourceTitle", ""),
                    "date": r.get("date", ""),
                    "quality": ((r.get("quality") or {}).get("quality") or {}).get("name"),
                    "season_number": None,
                    "episode_number": None,
                    "air_date": None,
                    "file_path": fp_by_idx.get(i, (None, None))[0],
                    "show_id": fp_by_idx.get(i, (None, None))[1],
                }
                for i, r in enumerate(grabbed)
            ]
    except (sonarr_client.SonarrError, radarr_client.RadarrError):
        pass
    return []


@query.field("browseSeasonalAnime")
def resolve_browse_seasonal_anime(_, info, season, year, page=1):
    conn = db.get_connection()
    return browse.fetch_seasonal_browse(conn, season, year, page)


@query.field("browseTmdb")
def resolve_browse_tmdb(_, info, year, month, media_type="ALL", page=1):
    conn = db.get_connection()
    return browse_tmdb.fetch_tmdb_browse(conn, year, month, media_type, page)


@mutation.field("setStatus")
def resolve_set_status(_, info, show_id, status, confirmed=False):
    """Show-level status — thin wrapper around `_apply_status_change`
    (extracted 2026-09-19 so the Sonarr/Radarr-reconcile mutation can
    apply a status change with the exact same side effects — AniList/MAL
    push, season fanout, completed-episode auto-mark — as this resolver,
    same shape resolve_add_show's own docstring already established for
    shows.create_show). `confirmed` — auto-sync warning gate, todo.md
    2026-08-16, user's own "y/n" framing: setting COMPLETED on a show
    that's still airing (`_show_is_airing`) refuses with a GraphQLError
    unless `confirmed: true` is also passed."""
    conn = db.get_connection()
    client = require_client(info)
    if status == "completed" and not confirmed and _show_is_airing(conn, show_id):
        raise GraphQLError(
            f"{show_id} still has an episode with no known air date, or one that hasn't aired "
            "yet — mark it completed anyway? pass confirmed: true to proceed"
        )
    return _apply_status_change(conn, show_id, status, client)


def _apply_status_change(conn, show_id: str, status: str, changed_by: str):
    """The real work behind resolve_set_status above — factored out so
    the Sonarr/Radarr reconcile mutation (B.5.1 follow-up, NEXT_UP.md)
    can drive a status change with `changed_by="sonarr"`/`"radarr"` and
    get every side effect a client-driven setStatus gets: AniList/MAL
    push, season fanout, completed-episode auto-mark, arr monitor sync.
    No `require_client`/airing-confirmation gate here — those are
    request-shaped concerns resolve_set_status itself already handles;
    a caller with no GraphQL `info` (the reconcile path) has already
    decided `status` is correct by the time it gets here.

    `status_before_pause` (migration 038b4fbb1ec7): captured on entering
    paused/dropped from an active status, so a later resume — whether
    the user manually or the reconcile pass restoring from Sonarr's own
    `monitored` flag flipping back to true — knows what to resume *to*
    rather than guessing WATCHING. Cleared on any transition away from
    paused/dropped, manual or automatic — the status actually being set
    now always wins over whatever was remembered.

    Arr monitor sync, both directions: entering paused/dropped
    unmonitors in Sonarr/Radarr (`_unmonitor_in_arr_on_drop`, existing);
    leaving it re-monitors (`_remonitor_in_arr_on_resume`, new here
    2026-09-19 — deliberately NOT `shows.ensure_arr_monitored`, see that
    function's own docstring for why: its add-if-missing branch would
    silently re-add and full-search a show that's missing from Sonarr
    for an unrelated reason) — closes a real gap: without this half, the
    reconcile pass pausing a show on `monitored=false` and a user then
    resuming it in LCARS left Sonarr/Radarr still unmonitored, so the
    very next reconcile tick would silently flip the show right back to
    paused."""
    row = conn.execute(
        "SELECT status, status_before_pause FROM show WHERE id = ?", (show_id,)
    ).fetchone()
    if row is None:
        raise GraphQLError(f"no such show: {show_id}")
    previous_status = row["status"]
    was_paused = previous_status in ("paused", "dropped")
    now_paused = status in ("paused", "dropped")
    now = util.now_utc_iso()
    # Fanout: only stomp the highest season's status — earlier seasons
    # keep their own deliberate per-season status (user rule: "show
    # level only stomp last season if needed").
    conn.execute(
        "UPDATE season SET status = ?, updated_at = ?"
        " WHERE show_id = ? AND season_number = ("
        "   SELECT MAX(season_number) FROM season"
        "   WHERE show_id = ? AND season_number > 0"
        " )",
        (status, now, show_id, show_id),
    )
    status_before_pause = row["status_before_pause"]
    if now_paused and not was_paused:
        status_before_pause = previous_status
    elif not now_paused and was_paused:
        status_before_pause = None
    # Write show.status directly (this is the explicit-set path, not derived)
    conn.execute(
        "UPDATE show SET status = ?, status_before_pause = ?, updated_at = ? WHERE id = ?",
        (status, status_before_pause, now, show_id),
    )
    conn.execute(
        "INSERT INTO status_change"
        " (id, show_id, previous_status, new_status, changed_at, changed_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ids.generate_id(conn, "c"), show_id, previous_status, status, now, changed_by),
    )
    _push_show_status(conn, show_id, status)  # §6.1/§6.8, A.9 — best-effort
    _push_mal_show_status(conn, show_id, status)  # §6.1/§6.9, B.10 — best-effort
    _stamp_completed_at_if_highest_season(conn, show_id, status)  # write-mirror, todo.md
    if status == "completed":
        _bulk_mark_all_aired_episodes_watched(conn, show_id)  # auto-sync, todo.md
        for season_row in conn.execute(
            "SELECT season_number FROM season WHERE show_id = ?", (show_id,)
        ).fetchall():
            _try_complete_season(conn, show_id, season_row["season_number"], now)
    conn.commit()
    if now_paused and not was_paused:
        _unmonitor_in_arr_on_drop(conn, show_id)
    elif not now_paused and was_paused:
        _remonitor_in_arr_on_resume(conn, show_id)
    return _get_show(conn, show_id)


@mutation.field("setScore")
def resolve_set_score(_, info, show_id, score):
    """§6.1 — clamps to [0, 20] then rounds to the nearest quarter-point,
    silently, rather than rejecting out-of-range/off-grid input."""
    conn = db.get_connection()
    client = require_client(info)
    row = conn.execute("SELECT score FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        raise GraphQLError(f"no such show: {show_id}")
    clamped = max(0.0, min(20.0, score))
    rounded = round(clamped * 4) / 4
    now = util.now_utc_iso()
    conn.execute("UPDATE show SET score = ?, updated_at = ? WHERE id = ?", (rounded, now, show_id))
    conn.execute(
        "INSERT INTO score_change (id, show_id, previous_score, new_score, changed_at, changed_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ids.generate_id(conn, "o"), show_id, row["score"], rounded, now, client),
    )
    _push_show_score(conn, show_id, rounded)  # §6.1/§6.8, A.9 — best-effort
    _push_mal_show_score(conn, show_id, rounded)  # §6.1/§6.9, B.10 — best-effort
    conn.commit()
    return _get_show(conn, show_id)


@mutation.field("setDisplayTitle")
def resolve_set_display_title(_, info, show_id, title):
    """2026-08-26 — pins a show's display title to any string the user
    recognizes (a synonym like "Lamu" for Urusei Yatsura, a romaji/native
    title, or a hand-typed one), overriding the write-once
    `primary_title`-derived default without touching it. `title: null` or
    an all-whitespace string clears the override (falls back to
    `title_{primary_title}`). Local-only — no AniList/MAL push: display
    title is a purely local presentation choice, unlike score/status."""
    conn = db.get_connection()
    _require_show(conn, show_id)
    override = title.strip() if title and title.strip() else None
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE show SET display_title_override = ?, updated_at = ? WHERE id = ?",
        (override, now, show_id),
    )
    conn.commit()
    return _get_show(conn, show_id)


@mutation.field("refreshTitlesFromAniList")
def resolve_refresh_titles_from_anilist(_, info, show_id):
    """2026-08-27 — on-demand AniList title re-sync for one show.
    show_backfill.refresh_titles_from_anilist() handles the fetch and
    write; we raise GraphQLError when it returns None (no AniList link
    or id unknown on AniList's side)."""
    require_client(info)
    conn = db.get_connection()
    _require_show(conn, show_id)
    result = show_backfill.refresh_titles_from_anilist(conn, show_id)
    if result is None:
        raise GraphQLError(
            f"Show {show_id} has no AniList link, or AniList doesn't know the linked id."
        )
    return dict(result)


@mutation.field("setSeasonScore")
def resolve_set_season_score(_, info, season_id, score):
    """A.9, §6.1/§6.5's season-level score granularity (§5.5 addendum)
    — same clamp/round as setScore, but scoped to one season and its
    own AniList entry only, not every season the show has."""
    conn = db.get_connection()
    season = season_mapping.get_season(conn, season_id)
    if season is None:
        raise GraphQLError(f"no such season: {season_id}")
    clamped = max(0.0, min(20.0, score))
    rounded = round(clamped * 4) / 4
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE season SET score = ?, updated_at = ? WHERE id = ?", (rounded, now, season_id)
    )
    season["score"] = rounded
    _push_season_score(conn, season, fallback_show_score=None)  # A.9 — best-effort
    _push_mal_season_score(conn, season, fallback_show_score=None)  # B.10 — best-effort
    conn.commit()
    return season_mapping.get_season(conn, season_id)


@mutation.field("setSeasonStatus")
def resolve_set_season_status(_, info, season_id, status=None, confirmed=False):
    """Per-season status — updates season.status and recomputes the
    derived show.status (2.1c).  status=None clears the per-season
    override (falls back to show-level status for derivation).

    Completion guard (2.3): setting COMPLETED on a season that's still
    airing (has unaired or undated episodes) requires confirmed=True,
    same pattern as setStatus's own guard."""
    conn = db.get_connection()
    client = require_client(info)
    season = season_mapping.get_season(conn, season_id)
    if season is None:
        raise GraphQLError(f"no such season: {season_id}")
    if status == "completed" and not confirmed:
        if _season_still_airing(conn, season["show_id"], season["season_number"]):
            raise GraphQLError(
                f"Season {season['season_number']} of {season['show_id']} still has an episode "
                "with no known air date, or one that hasn't aired yet — mark it completed anyway? "
                "pass confirmed: true to proceed"
            )
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE season SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, season_id),
    )
    # Push this season's own status to AniList/MAL
    effective = status or conn.execute(
        "SELECT status FROM show WHERE id = ?", (season["show_id"],)
    ).fetchone()["status"]
    if effective:
        _push_season_status(conn, season, effective)
        _push_mal_season_status(conn, season, effective)
    # Recompute derived show.status
    _recompute_show_status(conn, season["show_id"], client)
    conn.commit()
    return season_mapping.get_season(conn, season_id)


@mutation.field("markSeasonRewatch")
def resolve_mark_season_rewatch(_, info, season_id, repeat_count):
    """Write-mirror function set, todo.md (2026-08-16) — pure push, no
    local column to update (see _push_season_rewatch's own docstring
    for why); the season row itself is returned unchanged."""
    conn = db.get_connection()
    season = season_mapping.get_season(conn, season_id)
    if season is None:
        raise GraphQLError(f"no such season: {season_id}")
    _push_season_rewatch(conn, season, repeat_count)
    conn.commit()
    return season_mapping.get_season(conn, season_id)


def _unmonitor_in_arr_on_drop(conn, show_id: str) -> None:
    """B.21, auto-sync — the missing half of what Data's own (now-being-
    deleted) `_prompt_unmonitor` used to do interactively: unmonitoring
    a dropped show in Sonarr/Radarr, now server-side and automatic, no
    prompt. Called from both `setTracked(false)` and `softDeleteShow`,
    only on a real `1 -> 0` tracked transition (each caller's own guard).

    Best-effort — same "push as a side effect of the state change,
    never blocks the local write" shape `_push_show_status` already has
    for `setStatus`, NOT `confirmHardDelete`'s abort-before-commit
    treatment: the local `tracked = 0` write has already committed by
    the time this runs (called after `conn.commit()` in both callers),
    so an unreachable Sonarr/Radarr here shouldn't block the user from
    dropping a show they already dropped locally. A failure opens a
    `pending_review` (field `sonarr_unmonitor`/`radarr_unmonitor`)
    rather than silently vanishing.

    Sonarr: unmonitor at *both* the series level and every entry in
    `series["seasons"]`, in one `PUT` — grounded directly in todo.md's
    own live-checked note from this same investigation (both levels
    `monitored: false` is the real, confirmed state Data's interactive
    prompt used to produce for two real shows). Radarr: movie-level
    `monitored: false` only, no season concept for a movie.

    `setTracked(true)` deliberately does NOT re-monitor — symmetric
    with `cancelHardDelete`'s own "doesn't re-track, a separate call
    does that if wanted" precedent; re-tracking in LCARS is not the
    same decision as wanting Sonarr/Radarr to resume actively grabbing
    it. Not a gap, an explicit non-behavior."""
    cfg = config.get_current()
    tvdb_row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
        (show_id,),
    ).fetchone()
    if tvdb_row and cfg.sonarr_url and cfg.sonarr_api_key:
        try:
            with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
                series = client.series_by_tvdb_id(int(tvdb_row["external_id"]))
                if series is not None:
                    series["monitored"] = False
                    for season_entry in series.get("seasons", []):
                        season_entry["monitored"] = False
                    client.update_series(series)
            service_health.record_success(conn, "sonarr")
        except sonarr_client.SonarrError as e:
            service_health.record_failure(conn, "sonarr", str(e))
            pending_review.open_or_extend(
                conn, "show", show_id, "sonarr_unmonitor", "sonarr", None, str(e)
            )
    tmdb_row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tmdb'",
        (show_id,),
    ).fetchone()
    if tmdb_row and cfg.radarr_url and cfg.radarr_api_key:
        try:
            with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
                movie = client.movie_by_tmdb_id(int(tmdb_row["external_id"]))
                if movie is not None:
                    movie["monitored"] = False
                    client.update_movie(movie)
            service_health.record_success(conn, "radarr")
        except radarr_client.RadarrError as e:
            service_health.record_failure(conn, "radarr", str(e))
            pending_review.open_or_extend(
                conn, "show", show_id, "radarr_unmonitor", "radarr", None, str(e)
            )
    conn.commit()  # this function's own writes (service_health/pending_review) — called
    # after the caller's own tracked=0 commit, so it commits its own side effects itself


def _remonitor_in_arr_on_resume(conn, show_id: str) -> None:
    """`_apply_status_change`'s counterpart to `_unmonitor_in_arr_on_drop`
    above, for the "leaving paused/dropped" transition — 2026-09-19,
    part of the monitored<->status reconcile (NEXT_UP.md). Deliberately
    NOT `shows.ensure_arr_monitored`: that function's own "not currently
    in Sonarr/Radarr at all" branch calls add_series/add_movie with
    searchForMissingEpisodes/searchForMovie — appropriate for its actual
    callers (sequel-attach, season creation, both explicit "track this"
    actions), wrong here, where a show resuming from paused was already
    supposed to be a no-op push, not "also re-add it and kick off a
    full back-catalog search" if it happens to be missing from Sonarr
    for an unrelated reason. This only ever flips `monitored` on an
    *existing* arr entry — a show with no entry stays untouched, same
    "nothing to correct against" posture every other best-effort helper
    here already has. Same shape as `_unmonitor_in_arr_on_drop`
    otherwise: best-effort, opens a pending_review (field
    `sonarr_remonitor`/`radarr_remonitor`) on a real service failure
    rather than silently vanishing."""
    cfg = config.get_current()
    tvdb_row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
        (show_id,),
    ).fetchone()
    if tvdb_row and cfg.sonarr_url and cfg.sonarr_api_key:
        try:
            with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
                series = client.series_by_tvdb_id(int(tvdb_row["external_id"]))
                if series is not None:
                    series["monitored"] = True
                    for season_entry in series.get("seasons", []):
                        season_entry["monitored"] = True
                    client.update_series(series)
            service_health.record_success(conn, "sonarr")
        except sonarr_client.SonarrError as e:
            service_health.record_failure(conn, "sonarr", str(e))
            pending_review.open_or_extend(
                conn, "show", show_id, "sonarr_remonitor", "sonarr", None, str(e)
            )
    tmdb_row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tmdb'",
        (show_id,),
    ).fetchone()
    if tmdb_row and cfg.radarr_url and cfg.radarr_api_key:
        try:
            with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
                movie = client.movie_by_tmdb_id(int(tmdb_row["external_id"]))
                if movie is not None:
                    movie["monitored"] = True
                    client.update_movie(movie)
            service_health.record_success(conn, "radarr")
        except radarr_client.RadarrError as e:
            service_health.record_failure(conn, "radarr", str(e))
            pending_review.open_or_extend(
                conn, "show", show_id, "radarr_remonitor", "radarr", None, str(e)
            )
    conn.commit()


@mutation.field("setTracked")
def resolve_set_tracked(_, info, show_id, tracked):
    conn = db.get_connection()
    client = require_client(info)
    row = conn.execute("SELECT tracked FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        raise GraphQLError(f"no such show: {show_id}")
    now = util.now_utc_iso()
    was_tracked = bool(row["tracked"])
    conn.execute(
        "UPDATE show SET tracked = ?, updated_at = ? WHERE id = ?", (int(tracked), now, show_id)
    )
    conn.execute(
        "INSERT INTO tracked_change"
        " (id, show_id, previous_tracked, new_tracked, changed_at, changed_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ids.generate_id(conn, "k"), show_id, row["tracked"], int(tracked), now, client),
    )
    conn.commit()
    if was_tracked and not tracked:
        _unmonitor_in_arr_on_drop(conn, show_id)  # B.21, auto-sync — best-effort
    return _get_show(conn, show_id)


@mutation.field("setTrackingSpace")
def resolve_set_tracking_space(_, info, show_id, tracking_space):
    """2026-08-18 — see this mutation's own SDL docstring for the real
    incident that flagged the gap. Same shape as resolve_set_episode_kind
    above: no history table (§5.7's four are status/score/air_date/
    tracked, trackingSpace isn't among them), a plain field flip — the
    AniList-linking follow-up an actual reclassification needs is
    refreshShowMetadata's job, a separate, already-existing call, not
    this one's."""
    conn = db.get_connection()
    now = util.now_utc_iso()
    cur = conn.execute(
        "UPDATE show SET tracking_space = ?, updated_at = ? WHERE id = ?",
        (tracking_space, now, show_id),
    )
    if cur.rowcount == 0:
        raise GraphQLError(f"no such show: {show_id}")
    conn.commit()
    return _get_show(conn, show_id)


# -- §6.2 paced/catch-up mode (A.10) -----------------------------------------


def _show_is_airing(conn, show_id: str) -> bool:
    """§6.2 — "restricted to completed/non-airing shows only". Not
    LCARS's own `show.status` (a show can be user-marked WATCHING and
    still be fully released — the normal paced-mode case, bingeing at
    a self-imposed pace) — this checks the show's real content: any
    episode with no known air date yet, or one still in the future,
    means new content is still coming, so no synthetic date should
    ever compete with it. A movie has no episode rows at all (§5.1),
    so it's always "non-airing" by this definition."""
    row = conn.execute(
        "SELECT 1 FROM episode"
        " WHERE show_id = ? AND (air_date_utc IS NULL OR air_date_utc > ?) LIMIT 1",
        (show_id, util.now_utc_iso()),
    ).fetchone()
    return row is not None


@mutation.field("enablePacedMode")
def resolve_enable_paced_mode(_, info, show_id, cadence_days=7):
    conn = db.get_connection()
    _require_show(conn, show_id)
    if _show_is_airing(conn, show_id):
        raise GraphQLError(
            f"{show_id} still has unreleased episodes — paced mode is for "
            "completed/non-airing shows only (§6.2)"
        )
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE show SET paced_cadence_days = ?, updated_at = ? WHERE id = ?",
        (cadence_days, now, show_id),
    )
    conn.commit()
    return _get_show(conn, show_id)


@mutation.field("disablePacedMode")
def resolve_disable_paced_mode(_, info, show_id):
    conn = db.get_connection()
    _require_show(conn, show_id)
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE show SET paced_cadence_days = NULL, updated_at = ? WHERE id = ?", (now, show_id)
    )
    conn.commit()
    return _get_show(conn, show_id)


@show_type.field("pacedNextDate")
def resolve_show_paced_next_date(obj, info):
    if obj.get("paced_cadence_days") is None:
        return None
    conn = db.get_connection()
    row = conn.execute(
        "SELECT MAX(watched_at) AS latest FROM watch_event WHERE show_id = ?", (obj["id"],)
    ).fetchone()
    if row["latest"] is None:
        return None
    return util.add_days(row["latest"], obj["paced_cadence_days"])


# -- §6.11 deletion policy (A.14) --------------------------------------------

HARD_DELETE_DELAY_DAYS = 24 / 24  # 24 hours, §6.11 — expressed via util.add_days' own day unit


@mutation.field("softDeleteShow")
def resolve_soft_delete_show(_, info, show_id):
    """§3 principle 3/§6.11 — "a status/tracked-flag change, never a
    row removal." Sets `tracked = false` specifically (not `status`):
    the two are independent axes elsewhere in this schema (§5.1), and
    `tracked` is already the field this project's own stats surface
    (A.13) treats as "no longer part of the current library" — the
    natural fit for "soft-deleted." `status` is left exactly as it
    was, so e.g. a `completed` show soft-deleted still shows as
    `completed`, not silently rewritten to some deletion-specific
    value. Mechanically identical to setTracked(false) — its own
    dedicated mutation regardless (§3 principle 7), since it's the
    named entry point the deletion *flow* itself uses."""
    conn = db.get_connection()
    client = require_client(info)
    row = conn.execute("SELECT tracked FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        raise GraphQLError(f"no such show: {show_id}")
    now = util.now_utc_iso()
    conn.execute("UPDATE show SET tracked = 0, updated_at = ? WHERE id = ?", (now, show_id))
    conn.execute(
        "INSERT INTO tracked_change"
        " (id, show_id, previous_tracked, new_tracked, changed_at, changed_by)"
        " VALUES (?, ?, ?, 0, ?, ?)",
        (ids.generate_id(conn, "k"), show_id, row["tracked"], now, client),
    )
    conn.commit()
    if row["tracked"]:
        _unmonitor_in_arr_on_drop(conn, show_id)  # B.21, auto-sync — best-effort
    return _get_show(conn, show_id)


@mutation.field("requestHardDelete")
def resolve_request_hard_delete(_, info, show_id):
    """§6.11's own "layered: soft-delete -> a delay period -> ..."
    sequence read as a real precondition, not just a suggested client
    flow — rejects unless the show is already soft-deleted
    (`tracked = false`). Idempotent otherwise: calling this again
    while already pending just resets the 24-hour timer, rather than
    erroring. No require_client() — no dedicated history table exists
    for this field (only the four §5.7 tables do), same reasoning
    enablePacedMode/disablePacedMode (A.10) already established."""
    conn = db.get_connection()
    show = _require_show(conn, show_id)
    if show["tracked"]:
        raise GraphQLError(
            f"{show_id} must be soft-deleted first (softDeleteShow) before "
            "requesting a hard delete (§6.11)"
        )
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE show SET hard_delete_requested_at = ?, updated_at = ? WHERE id = ?",
        (now, now, show_id),
    )
    conn.commit()
    return _get_show(conn, show_id)


@mutation.field("cancelHardDelete")
def resolve_cancel_hard_delete(_, info, show_id):
    """Harmless no-op if nothing was pending — same "just clear it"
    shape as disablePacedMode (A.10). Deliberately does not re-track
    the show (§6.11 frames this as reversing *requestHardDelete*
    specifically, not the earlier soft-delete step too) — a separate
    setTracked(true) call re-tracks it, if that's also wanted."""
    conn = db.get_connection()
    _require_show(conn, show_id)
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE show SET hard_delete_requested_at = NULL, updated_at = ? WHERE id = ?",
        (now, show_id),
    )
    conn.commit()
    return _get_show(conn, show_id)


def _delete_from_anilist_before_purge(conn, show_id: str) -> None:
    """Write-mirror gap #4, todo.md — every one of this show's linked
    seasons gets its own AniList list entry removed, looped the same
    shape _push_show_status already loops seasons in (a split-cour show
    has one AniList entry per linked season, not one per show — tested
    explicitly, not just assumed from the loop shape being familiar).
    No-ops entirely (silently, not even a call attempted) when there's
    no token configured — same "not linked/not logged in" convention
    every push in this file already uses, not a new one. Raises
    GraphQLError on the *first* failure (unreachable AniList, or any
    other AniListError) rather than continuing through the rest of the
    seasons — resolve_confirm_hard_delete's own docstring has the "why
    not best-effort" reasoning; nothing before this point has committed
    anything, so an abort here leaves the DB exactly as it was."""
    cfg = config.get_current()
    if not cfg.anilist_access_token:
        return
    seasons = conn.execute(
        "SELECT id, anilist_id FROM season WHERE show_id = ? AND anilist_id IS NOT NULL",
        (show_id,),
    ).fetchall()
    for season in seasons:
        try:
            entry_id = anilist_client.fetch_my_list_entry_id(
                cfg.anilist_access_token, season["anilist_id"]
            )
            if entry_id is not None:
                anilist_client.delete_media_list_entry(cfg.anilist_access_token, entry_id)
        except anilist_client.AniListError as e:
            raise GraphQLError(
                f"couldn't remove season {season['id']} (anilist {season['anilist_id']}) from"
                f" AniList, aborting the hard delete entirely so nothing is left inconsistent:"
                f" {e}"
            ) from e


@mutation.field("confirmHardDelete")
def resolve_confirm_hard_delete(_, info, show_id, retyped_title):
    """The actual purge (§6.11) — succeeds only once the 24-hour delay
    has elapsed since requestHardDelete AND retypedTitle matches the
    show's current displayTitle exactly. Cascades manually, in FK
    dependency order (no ON DELETE CASCADE anywhere in this schema,
    §11.2 — same reasoning deleteTag's own cascade already
    documents), across every table that references this show, this
    show's episodes, or this show's seasons — including two easy-to-
    miss cross-show cases: show_relation has *both* show_id and
    related_show_id pointing at `show`, and episode_movie_link's
    movie_show_id can point at this show from some *other* show's
    episode row (a bonus_movie episode linking here) — that one gets
    unlinked (set NULL), not deleted, since the row itself belongs to
    a different, unrelated show. pending_review has no FK at all
    (entity_type/entity_id is polymorphic, §5.6) but still gets
    cleaned up here, in the spirit of "cascading to this show's
    episodes/watch_events/etc" — an orphaned review pointing at a
    show/season that no longer exists serves no purpose. No
    require_client()/history row for the purge itself: §3 principle 3
    frames a show's hard delete as the one case (alongside
    watch_event) with deliberately no audit trail afterward — the row
    and everything about it are gone, by design.

    **Mirrors the delete to AniList too, 2026-08-16 (write-mirror gap
    #4, todo.md) — user's own framing: "as guarded as the delete from
    lcars."** Unlike every score/status/progress push in this file
    (best-effort, never blocks the local write on failure), this one
    raises and aborts *before* any local `DELETE` runs if any linked
    season's AniList removal fails — deliberately not best-effort,
    since confirmHardDelete is retry-safe by construction (the 24h
    delay's already elapsed, the retyped title still matches — a
    failed attempt costs one repeated call, nothing lost) in a way
    setStatus/addWatchEvent aren't (blocking those on a network blip
    would lose real, unretryable user input). See
    _delete_from_anilist_before_purge's own docstring for the
    per-season loop/lookup mechanics.
    """
    conn = db.get_connection()
    show = _require_show(conn, show_id)
    if show["hard_delete_requested_at"] is None:
        raise GraphQLError(f"{show_id} has no pending hard-delete request (§6.11)")
    earliest = util.add_days(show["hard_delete_requested_at"], HARD_DELETE_DELAY_DAYS)
    if util.now_utc_iso() < earliest:
        raise GraphQLError(f"the 24-hour delay hasn't elapsed yet — try again after {earliest}")
    if retyped_title != _computed_display_title(show):
        raise GraphQLError("retypedTitle doesn't match this show's current display title")

    _delete_from_anilist_before_purge(conn, show_id)

    season_ids = [
        row["id"] for row in conn.execute("SELECT id FROM season WHERE show_id = ?", (show_id,))
    ]

    conn.execute("DELETE FROM watch_event WHERE show_id = ?", (show_id,))
    conn.execute(
        "DELETE FROM episode_movie_link WHERE episode_id IN"
        " (SELECT id FROM episode WHERE show_id = ?)",
        (show_id,),
    )
    conn.execute(
        "UPDATE episode_movie_link SET movie_show_id = NULL WHERE movie_show_id = ?", (show_id,)
    )
    conn.execute(
        "DELETE FROM air_date_change WHERE episode_id IN"
        " (SELECT id FROM episode WHERE show_id = ?)",
        (show_id,),
    )
    conn.execute("DELETE FROM episode WHERE show_id = ?", (show_id,))
    conn.execute(
        "DELETE FROM season_external_id WHERE season_id IN"
        " (SELECT id FROM season WHERE show_id = ?)",
        (show_id,),
    )
    conn.execute("DELETE FROM season WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM show_external_id WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM show_synonym WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM show_service_presence WHERE show_id = ?", (show_id,))
    conn.execute(
        "DELETE FROM show_relation WHERE show_id = ? OR related_show_id = ?", (show_id, show_id)
    )
    conn.execute("DELETE FROM episode_numbering_mapping WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM status_change WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM score_change WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM tracked_change WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM show_person WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM show_studio WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM franchise_member WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM show_tag WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM next_up_override WHERE show_id = ?", (show_id,))
    conn.execute(
        "DELETE FROM pending_review WHERE entity_type = 'show' AND entity_id = ?", (show_id,)
    )
    if season_ids:
        placeholders = ", ".join("?" for _ in season_ids)
        conn.execute(
            "DELETE FROM pending_review"
            f" WHERE entity_type = 'season' AND entity_id IN ({placeholders})",
            season_ids,
        )
    conn.execute("DELETE FROM show WHERE id = ?", (show_id,))
    conn.commit()
    return True


@mutation.field("addWatchEvent")
def resolve_add_watch_event(
    _, info, show_id, season=None, episode=None, watched_at=None, platform=None
):
    """season/episode both optional — null for a movie's watch event
    (§5.3 addendum: media_shape = MOVIE has no episode row at all)."""
    conn = db.get_connection()
    now = util.now_utc_iso()
    watched_at = watched_at or now
    watch_id = ids.generate_id(conn, "w")
    conn.execute(
        "INSERT INTO watch_event (id, show_id, season, episode, watched_at, platform, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (watch_id, show_id, season, episode, watched_at, platform, now),
    )
    if season is not None and episode is not None:
        conn.execute(
            "UPDATE episode SET state = 'watched', updated_at = ?"
            " WHERE show_id = ? AND season = ? AND episode = ?",
            (now, show_id, season, episode),
        )
    _stamp_season_started_at(conn, show_id, season, watched_at)  # write-mirror, todo.md
    if season is not None:
        _try_complete_season(conn, show_id, season, watched_at)  # auto-sync, todo.md
        _recompute_show_status(conn, show_id, "auto_complete")  # derived status, 2.1c
    conn.commit()
    _push_show_episode_progress(conn, show_id, season)  # write-mirror gap #2, todo.md
    _push_mal_show_episode_progress(conn, show_id, season)  # MAL mirror
    row = conn.execute("SELECT rowid, * FROM watch_event WHERE id = ?", (watch_id,)).fetchone()
    return dict(row)


@mutation.field("deleteWatchEvent")
def resolve_delete_watch_event(_, info, watch_event_id):
    """Hard-deletable (§5.3) — mirrors aniq's U undo directly. Also
    reverts episode.state back to unwatched, but only if no other
    watch_event rows remain for that episode afterwards — a rewatch can
    have several watch_events for the same episode (§5.3: "the
    mechanism rewatches work through"), so undoing one of several
    shouldn't un-mark an episode that's still genuinely watched via
    another entry. Movie watch events (season/episode both null) have
    no episode row to update at all."""
    conn = db.get_connection()
    row = conn.execute(
        "SELECT show_id, season, episode FROM watch_event WHERE id = ?", (watch_event_id,)
    ).fetchone()
    if row is None:
        raise GraphQLError(f"no such watch_event: {watch_event_id}")
    show_id, season, episode = row["show_id"], row["season"], row["episode"]
    conn.execute("DELETE FROM watch_event WHERE id = ?", (watch_event_id,))
    if season is not None and episode is not None:
        remaining = conn.execute(
            "SELECT 1 FROM watch_event WHERE show_id = ? AND season = ? AND episode = ? LIMIT 1",
            (show_id, season, episode),
        ).fetchone()
        if remaining is None:
            conn.execute(
                "UPDATE episode SET state = 'unwatched', updated_at = ?"
                " WHERE show_id = ? AND season = ? AND episode = ? AND state = 'watched'",
                (util.now_utc_iso(), show_id, season, episode),
            )
    conn.commit()
    _push_show_episode_progress(conn, show_id, season)  # write-mirror gap #3, todo.md
    _push_mal_show_episode_progress(conn, show_id, season)  # MAL mirror
    return True


@mutation.field("markSeasonWatched")
def resolve_mark_season_watched(_, info, show_id, season, watched_at=None):
    """Bulk mutation (§5.3) — one watch_event row per episode in the
    season, single call. Always inserts fresh rows, never skips
    already-watched episodes — rewatches are normal, not an error
    (§5.3: "one row per viewing").

    **Only ever marks episodes that have actually aired — a real, past
    `air_date_utc`, 2026-08-16, user's own explicit call.** Used to mark
    every episode in the season regardless of air date; closed once the
    auto-sync work made the consequence concrete (a real, past-air-dated
    episode `markSeasonWatched` would otherwise leave a genuinely unaired
    episode `watched`, and that state now feeds AniList's own progress
    number — the same "can't have watched something that hasn't aired"
    contradiction Frontier Lord's own real bug was). `addWatchEvent`/
    `markEpisodeRangeWatched` deliberately keep no such guard — those name
    exact episodes one at a time, a different, already-deliberate action
    unlike this bulk "watch the whole season" one. A season with zero
    aired episodes marks nothing; `started_at`/completion checks only run
    when at least one episode was actually touched, never claimed from a
    call that changed nothing."""
    conn = db.get_connection()
    now = util.now_utc_iso()
    watched_at = watched_at or now
    episodes = conn.execute(
        "SELECT episode FROM episode"
        " WHERE show_id = ? AND season = ? AND air_date_utc IS NOT NULL AND air_date_utc <= ?"
        " ORDER BY episode",
        (show_id, season, now),
    ).fetchall()
    created_ids = []
    for row in episodes:
        watch_id = ids.generate_id(conn, "w")
        conn.execute(
            "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (watch_id, show_id, season, row["episode"], watched_at, now),
        )
        created_ids.append(watch_id)
    if created_ids:
        conn.execute(
            "UPDATE episode SET state = 'watched', updated_at = ?"
            " WHERE show_id = ? AND season = ? AND air_date_utc IS NOT NULL AND air_date_utc <= ?",
            (now, show_id, season, now),
        )
        _stamp_season_started_at(conn, show_id, season, watched_at)  # write-mirror, todo.md
        _try_complete_season(conn, show_id, season, watched_at)  # auto-sync, todo.md
        _recompute_show_status(conn, show_id, "auto_complete")  # derived status, 2.1c
    conn.commit()
    _push_show_episode_progress(conn, show_id, season)  # write-mirror gap #2, todo.md
    _push_mal_show_episode_progress(conn, show_id, season)  # MAL mirror
    return [
        dict(conn.execute("SELECT rowid, * FROM watch_event WHERE id = ?", (wid,)).fetchone())
        for wid in created_ids
    ]


@mutation.field("markEpisodeRangeWatched")
def resolve_mark_episode_range_watched(
    _, info, show_id, season, from_episode, to_episode, watched_at=None
):
    conn = db.get_connection()
    now = util.now_utc_iso()
    watched_at = watched_at or now
    episodes = conn.execute(
        "SELECT episode FROM episode"
        " WHERE show_id = ? AND season = ? AND episode BETWEEN ? AND ?"
        " ORDER BY episode",
        (show_id, season, from_episode, to_episode),
    ).fetchall()
    created_ids = []
    for row in episodes:
        watch_id = ids.generate_id(conn, "w")
        conn.execute(
            "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (watch_id, show_id, season, row["episode"], watched_at, now),
        )
        created_ids.append(watch_id)
    conn.execute(
        "UPDATE episode SET state = 'watched', updated_at = ?"
        " WHERE show_id = ? AND season = ? AND episode BETWEEN ? AND ?",
        (now, show_id, season, from_episode, to_episode),
    )
    _stamp_season_started_at(conn, show_id, season, watched_at)  # write-mirror, todo.md
    _try_complete_season(conn, show_id, season, watched_at)  # auto-sync, todo.md
    _recompute_show_status(conn, show_id, "auto_complete")  # derived status, 2.1c
    conn.commit()
    _push_show_episode_progress(conn, show_id, season)  # write-mirror gap #2, todo.md
    _push_mal_show_episode_progress(conn, show_id, season)  # MAL mirror
    return [
        dict(conn.execute("SELECT rowid, * FROM watch_event WHERE id = ?", (wid,)).fetchone())
        for wid in created_ids
    ]


@mutation.field("markEpisodeSkipped")
def resolve_mark_episode_skipped(_, info, episode_id):
    conn = db.get_connection()
    now = util.now_utc_iso()
    row = conn.execute(
        "SELECT show_id, season FROM episode WHERE id = ?", (episode_id,)
    ).fetchone()
    if row is None:
        raise GraphQLError(f"no such episode: {episode_id}")
    conn.execute(
        "UPDATE episode SET state = 'skipped', updated_at = ? WHERE id = ?", (now, episode_id)
    )
    # auto-sync, todo.md — a skip counts as done for completion purposes
    # (rule #1: stays 'skipped' in the DB, never rewritten to 'watched').
    _try_complete_season(conn, row["show_id"], row["season"], now)
    _recompute_show_status(conn, row["show_id"], "auto_complete")  # derived status, 2.1c
    conn.commit()
    # write-mirror gap #2, todo.md — a skip can complete a previously-gapped
    # contiguous run (_compute_season_episode_progress counts skipped as
    # passed), so this needs the same push every real watch mutation gets.
    _push_show_episode_progress(conn, row["show_id"], row["season"])
    _push_mal_show_episode_progress(conn, row["show_id"], row["season"])  # MAL mirror
    return _get_episode(conn, episode_id)


# -- 5.2 episode field overrides -------------------------------------------


@mutation.field("setEpisodeAirDate")
def resolve_set_episode_air_date(_, info, episode_id, air_date_utc):
    """Sets air_date_source = MANUAL (schema.graphql's own doc comment)
    — §6.7's priority order means this then wins over every automatic
    source. Writes air_date_change (§5.7), same as every other manual
    field-setting mutation writes its own history table."""
    conn = db.get_connection()
    client = require_client(info)
    episode = _require_episode(conn, episode_id)
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE episode SET air_date_utc = ?, air_date_source = 'manual', updated_at = ?"
        " WHERE id = ?",
        (air_date_utc, now, episode_id),
    )
    conn.execute(
        "INSERT INTO air_date_change"
        " (id, episode_id, previous_air_date_utc, new_air_date_utc,"
        "  previous_source, new_source, changed_at, changed_by)"
        " VALUES (?, ?, ?, ?, ?, 'manual', ?, ?)",
        (
            ids.generate_id(conn, "g"),
            episode_id,
            episode["air_date_utc"],
            air_date_utc,
            episode["air_date_source"],
            now,
            client,
        ),
    )
    conn.commit()
    return _get_episode(conn, episode_id)


@mutation.field("setEpisodeNumber")
def resolve_set_episode_number(_, info, episode_id, season, episode):
    """archive/todo.md:1209, closed 2026-08-25 — schema.graphql's own
    docstring on `setEpisodeNumber` has the full rationale. Only ever
    writes `season`/`episode`/`season_id`/`updated_at` — never
    `sonarr_season`/`sonarr_episode` (metadata.py's own Sonarr-fetch
    resync-identity columns; immutable by design once captured, that's
    the entire point) or `absolute_number` (a separate, already-durable
    correction the multi-show Sonarr sync path keys on instead — no
    duplicate-row risk to guard against there, so no reason to fold it
    into this mutation).

    Reject-on-conflict, not swap/shift: a caller wanting to shift a
    whole run of episodes makes one call per episode, same two-level
    shape `a`/`A` (setEpisodeAirDate, plus `~/repos/data`'s own "shift
    all subsequent" client-side loop) already established for air-date
    correction — not duplicated here since nothing about *this*
    mutation needs a bulk variant of its own yet (no concrete case on
    record the way air-date drift had).

    `PRAGMA defer_foreign_keys` (reset automatically at the next
    commit/rollback per SQLite's own documented behavior, confirmed
    live before relying on it) is required here, not optional: the
    episode/watch_event pair below is a genuine chicken-and-egg update
    under the composite `watch_event` FK (§5.3) — updating either
    table first, with immediate per-statement FK checking, raises
    `FOREIGN KEY constraint failed` regardless of which one goes
    first, since each side transiently references a key the other
    side hasn't been written to yet mid-transaction."""
    conn = db.get_connection()
    row = _require_episode(conn, episode_id)
    show_id = row["show_id"]
    if row["season"] == season and row["episode"] == episode:
        return row  # already at this slot — no-op, not an error
    conflict = conn.execute(
        "SELECT id FROM episode WHERE show_id = ? AND season = ? AND episode = ? AND id != ?",
        (show_id, season, episode, episode_id),
    ).fetchone()
    if conflict is not None:
        raise GraphQLError(
            f"S{season:02}E{episode:02} is already occupied by episode {conflict['id']}"
            " on this show — move it out of the way first"
        )
    season_row = conn.execute(
        "SELECT id FROM season WHERE show_id = ? AND season_number = ?", (show_id, season)
    ).fetchone()
    new_season_id = season_row["id"] if season_row is not None else None
    now = util.now_utc_iso()
    conn.execute("PRAGMA defer_foreign_keys = ON")
    conn.execute(
        "UPDATE episode SET season = ?, episode = ?, season_id = ?, updated_at = ? WHERE id = ?",
        (season, episode, new_season_id, now, episode_id),
    )
    conn.execute(
        "UPDATE watch_event SET season = ?, episode = ?"
        " WHERE show_id = ? AND season = ? AND episode = ?",
        (season, episode, show_id, row["season"], row["episode"]),
    )
    conn.commit()
    return _get_episode(conn, episode_id)


@mutation.field("setEpisodeRuntimeOverride")
def resolve_set_episode_runtime_override(_, info, episode_id, runtime_minutes=None):
    """No history table for this one — §5.7 lists exactly four dedicated
    history tables (status/score/air_date/tracked) and runtime isn't
    among them, so this is a plain field update."""
    conn = db.get_connection()
    now = util.now_utc_iso()
    cur = conn.execute(
        "UPDATE episode SET runtime_minutes = ?, updated_at = ? WHERE id = ?",
        (runtime_minutes, now, episode_id),
    )
    if cur.rowcount == 0:
        raise GraphQLError(f"no such episode: {episode_id}")
    conn.commit()
    return _get_episode(conn, episode_id)


@mutation.field("setEpisodeKind")
def resolve_set_episode_kind(_, info, episode_id, kind):
    """§5.2's `kind`, manually. Sonarr only tells us "season 0", so
    A.25's auto-classification stops honestly at `special`; ova and
    bonus_movie have no reliable automatic signal and need this. No
    history table — §5.7 lists exactly four (status/score/air_date/
    tracked) and kind isn't among them, same as
    setEpisodeRuntimeOverride just above."""
    conn = db.get_connection()
    now = util.now_utc_iso()
    cur = conn.execute(
        "UPDATE episode SET kind = ?, updated_at = ? WHERE id = ?",
        (kind, now, episode_id),
    )
    if cur.rowcount == 0:
        raise GraphQLError(f"no such episode: {episode_id}")
    conn.commit()
    return _get_episode(conn, episode_id)


# -- 5.4 external links -----------------------------------------------------


@mutation.field("linkShowExternalId")
def resolve_link_show_external_id(_, info, show_id, service, external_id, url):
    """Upsert, keyed on show_id, service — matches show_external_id's
    own PRIMARY KEY (migration 7196ca889757)."""
    conn = db.get_connection()
    _require_show(conn, show_id)
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT 1 FROM show_external_id WHERE show_id = ? AND service = ?", (show_id, service)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE show_external_id SET external_id = ?, url = ?"
            " WHERE show_id = ? AND service = ?",
            (external_id, url, show_id, service),
        )
    else:
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (show_id, service, external_id, url, now),
        )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM show_external_id WHERE show_id = ? AND service = ?", (show_id, service)
    ).fetchone()
    return dict(row)


@mutation.field("unlinkShowExternalId")
def resolve_unlink_show_external_id(_, info, show_id, service):
    conn = db.get_connection()
    cur = conn.execute(
        "DELETE FROM show_external_id WHERE show_id = ? AND service = ?", (show_id, service)
    )
    conn.commit()
    return cur.rowcount > 0


@mutation.field("amendShowArrLink")
def resolve_amend_show_arr_link(
    _, info, show_id, service, new_external_id, delete_files=None
):
    """Correct a wrong TVDB/TMDB external ID — delegates to
    shows.amend_show_arr_link() which validates, deletes old arr entry,
    adds correct one, and updates LCARS link.

    Returns the snake_case dict directly — Ariadne's snake_case fallback
    resolvers handle the camelCase mapping (same pattern as
    resolve_link_show_external_id returning dict(row))."""
    conn = db.get_connection()
    try:
        return shows.amend_show_arr_link(
            conn,
            show_id,
            service,
            new_external_id,
            delete_files=bool(delete_files),
        )
    except shows.ShowInputError as e:
        return {
            "success": False,
            "old_external_id": None,
            "new_external_id": new_external_id,
            "resolved_title": None,
            "arr_deleted": False,
            "arr_added": False,
            "message": str(e),
        }


@mutation.field("refreshShowServicePresence")
def resolve_refresh_show_service_presence(_, info, show_id, service, candidate_titles):
    """A.7, §5.4 — LCARS makes no outbound HTTP calls of its own here
    (no Sonarr/Radarr/AniList/MAL client code exists yet at all —
    config.py's own docstring defers those credentials to A.16/Phase B);
    the caller supplies candidate_titles it already fetched from
    `service`'s own catalog, and `fuzzy.best_match` (ported from aniq's
    own real, working matcher) decides whether any of them counts as a
    match against this show's stored title variants. No require_client()
    and no history table — §5.4 is explicit this is passive/
    informational, never goes through pending_review, self-heals
    quietly like a dead poster URL."""
    conn = db.get_connection()
    show = _require_show(conn, show_id)
    show_titles = [
        show[f] for f in ("title_romaji", "title_english", "title_native") if show.get(f)
    ]
    present = fuzzy.best_match(show_titles, candidate_titles) is not None
    now = util.now_utc_iso()

    existing = conn.execute(
        "SELECT id FROM show_service_presence WHERE show_id = ? AND service = ?",
        (show_id, service),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE show_service_presence SET present = ?, checked_at = ? WHERE id = ?",
            (present, now, existing["id"]),
        )
        presence_id = existing["id"]
    else:
        presence_id = ids.generate_id(conn, "a")
        conn.execute(
            "INSERT INTO show_service_presence (id, show_id, service, present, checked_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (presence_id, show_id, service, present, now),
        )
    conn.commit()
    return _get_show_service_presence(conn, presence_id)


# -- 5.5 id-mapper manual overrides (§3 principle 6: manual wins once set) --
#
# None of these three tables carry a changed_by-style column (unlike
# status_change/score_change/tracked_change, §5.7) — no dedicated history
# table exists for id-mapping changes either — so these mutations don't
# call require_client(): there's nowhere in the schema to put the value.


def _mal_mirroring_anilist(anilist_id, mal_id, current_mal_id):
    """The MAL id to store alongside `anilist_id` (AniList authoritative,
    MAL mirrors it — project rule).

    2026-09-23 — 14 manual seasons had a MAL id belonging to a different
    entry (SPY×FAMILY S3 kept Season 3's MAL id after its AniList id was
    corrected): the show-page and reviews-page editors pre-fill the
    current MAL id, so correcting only AniList re-saved the stale one,
    and every MAL push for that season landed on the wrong entry.

    A MAL id the caller actually changed (differs from what's stored) is
    respected. An absent or unchanged one follows Fribb's pairing for
    `anilist_id`; with no pairing, an unchanged MAL id that Fribb says
    belongs to a *different* AniList entry is cleared rather than kept."""
    if anilist_id is None or (mal_id is not None and mal_id != current_mal_id):
        return mal_id
    try:
        dataset = fribb.load_dataset()
    except Exception:
        return mal_id  # no dataset — can't derive; keep what was given
    derived = fribb.mal_for_anilist(dataset, anilist_id)
    if derived is not None:
        return derived
    if mal_id is not None:
        owners = fribb.anilist_ids_for_mal(dataset, mal_id)
        if owners and anilist_id not in owners:
            return None
    return mal_id


@mutation.field("setSeasonMapping")
def resolve_set_season_mapping(_, info, show_id, season_number, anilist_id=None, mal_id=None):
    """Replaces setShowIdMapping (§5.5, A.4) — one show can span
    multiple AniList/MAL entries, one per season, so this is keyed on
    (show_id, season_number) rather than show_id alone. Upserts, same
    as the mutation it replaces: no other mutation creates a season
    row first (A.8's future on-demand fetch is what will, normally).

    **Auto-resolves this season's own open `pending_review` entries,
    added 2026-08-15** — real, evidenced gap: this is *the* mutation a
    human uses to act on a reviewed `season`-level discrepancy
    (`anilist_id`/`anilist_id_conflict`/etc.), but it never touched
    `pending_review` at all, so a corrected mapping and a "reviewed"
    review were two entirely separate, easy-to-forget steps — real
    confusion this same night, a batch of `anilist_id_conflict`
    reviews got marked resolved via `resolvePendingReview` alone,
    which (also confirmed this session, see that resolver's own
    docstring) never applies anything, so the underlying data was
    never actually fixed by that call. Requires `RESOLVING_CLIENTS`,
    same restriction `resolvePendingReview`/`applyShowMerge` already
    have (§5.6) — this genuinely is resolving a reviewed value, not a
    routine automated write. Same "resolves as part of the same call"
    shape `applyShowMerge` (B.14) already established for its own
    review category — reused, not invented fresh. Only *this* season's
    own reviews (whatever their `field`) — a sibling season that was
    also flagged as part of the same `anilist_id_conflict` isn't
    touched here (its own claim may or may not still be contested;
    that's for `reconcile_watch_progress`'s own next pass, or a
    separate human look, not assumed fixed by this call)."""
    conn = db.get_connection()
    client = require_client(info)
    if client not in RESOLVING_CLIENTS:
        raise GraphQLError(
            f"{client!r} cannot set a season mapping — only {sorted(RESOLVING_CLIENTS)} can (§5.6)"
        )
    _require_show(conn, show_id)
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT id, mal_id FROM season WHERE show_id = ? AND season_number = ?",
        (show_id, season_number),
    ).fetchone()
    mal_id = _mal_mirroring_anilist(
        anilist_id, mal_id, existing["mal_id"] if existing is not None else None
    )
    if existing is not None:
        conn.execute(
            "UPDATE season"
            " SET anilist_id = ?, mal_id = ?, source = 'manual',"
            "     matched = 1, manual_override = 1, updated_at = ?"
            " WHERE show_id = ? AND season_number = ?",
            (anilist_id, mal_id, now, show_id, season_number),
        )
        season_id = existing["id"]
    else:
        # 2.2 — gap validation: adding season N > 1 requires 1..N-1 to exist
        if season_number > 1:
            existing_numbers = {
                r["season_number"]
                for r in conn.execute(
                    "SELECT season_number FROM season"
                    " WHERE show_id = ? AND season_number > 0 AND season_number < ?",
                    (show_id, season_number),
                ).fetchall()
            }
            missing = sorted(set(range(1, season_number)) - existing_numbers)
            if missing:
                missing_str = ", ".join(str(n) for n in missing)
                raise GraphQLError(
                    f"Cannot add Season {season_number} — "
                    f"Season{'s' if len(missing) > 1 else ''} {missing_str} "
                    f"do{'es' if len(missing) == 1 else ''} not exist. "
                    "Add previous seasons first."
                )
        season_id = ids.generate_id(conn, "z")
        status = season_ranges.inherit_season_status(conn, show_id)
        conn.execute(
            "INSERT INTO season"
            " (id, show_id, season_number, status, anilist_id, mal_id, source, matched,"
            "  manual_override, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'manual', 1, 1, ?, ?)",
            (season_id, show_id, season_number, status, anilist_id, mal_id, now, now),
        )
        # auto-sync, todo.md — "if a new season is added then move back to
        # watching" (user's own rule); only this one of the five real
        # season-INSERT call sites, see _reopen_show_if_completed's docstring.
        _reopen_show_if_completed(conn, show_id, client)
        # Ensure Sonarr/Radarr is monitoring this series so the new
        # season's episodes actually get grabbed. If already monitored
        # this is a no-op; if unmonitored it flips monitoring on; if
        # not in Sonarr/Radarr at all it adds the series.
        try:
            shows.ensure_arr_monitored(conn, show_id)
        except shows.ShowInputError:
            # Sonarr/Radarr failure shouldn't block the season mapping
            # itself — the season row is the critical data; monitoring
            # can be retried. Log but don't raise.
            import logging
            logging.getLogger(__name__).warning(
                "ensure_arr_monitored failed for %s — season %d created"
                " but arr monitoring may need manual check",
                show_id, season_number,
            )
    # S2 dual-write (see season_ranges.py)
    season_ranges.upsert_season_external_id(conn, season_id, anilist_id, mal_id, now)
    conn.execute(
        "UPDATE pending_review"
        " SET resolved_at = ?, resolved_by_client = ?,"
        "     resolution_note = COALESCE(resolution_note, 'auto-resolved by setSeasonMapping')"
        " WHERE entity_type = 'season' AND entity_id = ? AND resolved_at IS NULL",
        (now, client, season_id),
    )
    conn.commit()
    return season_mapping.get_season(conn, season_id)


# -- Season subdivision split (S6, 2026-09-03) ---------------------------------


@mutation.field("splitSeason")
def resolve_split_season(
    _, info, show_id, season_number, after_episode, new_anilist_id=None, new_mal_id=None
):
    """Split a season into two at the given episode boundary.

    Episodes 1..after_episode stay in the original season (the "lower half");
    episodes (after_episode+1)..last are moved to a new season at
    season_number+1 (renumbered starting from E1, the "upper half"). All
    subsequent seasons (season_number+2, +3, ...) are shifted up by one.

    One transaction, all-or-nothing. Uses PRAGMA defer_foreign_keys for the
    same chicken-and-egg composite-FK reason setEpisodeNumber documents.

    Does NOT call _reopen_show_if_completed — a split re-partitions
    already-watched episodes, not a genuinely new season airing. Deliberately
    diverges from setSeasonMapping's sibling behavior here."""
    conn = db.get_connection()
    client = require_client(info)
    if client not in RESOLVING_CLIENTS:
        raise GraphQLError(
            f"{client!r} cannot split a season — only {sorted(RESOLVING_CLIENTS)} can (§5.6)"
        )
    _require_show(conn, show_id)
    now = util.now_utc_iso()

    # --- 1. Validate the season and episode boundary --------------------------
    season_row = conn.execute(
        "SELECT id, abs_start, abs_end FROM season"
        " WHERE show_id = ? AND season_number = ?",
        (show_id, season_number),
    ).fetchone()
    if season_row is None:
        raise GraphQLError(f"Season {season_number} does not exist for show {show_id}")
    season_id = season_row["id"]
    old_abs_start = season_row["abs_start"]
    old_abs_end = season_row["abs_end"]

    # All episodes in this season, sorted by episode number
    episodes = conn.execute(
        "SELECT id, episode, absolute_number FROM episode"
        " WHERE show_id = ? AND season = ? ORDER BY episode",
        (show_id, season_number),
    ).fetchall()
    ep_numbers = [e["episode"] for e in episodes]
    if not ep_numbers:
        raise GraphQLError(f"Season {season_number} has no episodes to split")
    if after_episode < min(ep_numbers) or after_episode >= max(ep_numbers):
        raise GraphQLError(
            f"afterEpisode must be strictly inside the season's episode range "
            f"({min(ep_numbers)}–{max(ep_numbers)}), got {after_episode}"
        )

    lower_eps = [e for e in episodes if e["episode"] <= after_episode]
    upper_eps = [e for e in episodes if e["episode"] > after_episode]

    # --- 2. Temporarily disable FK checks for the split transaction -----------
    # The episode ↔ watch_event composite FK (§5.3) creates a chicken-and-egg
    # problem when updating both sides: episode.season/episode changes break the
    # FK from watch_event, and updating watch_event first would point at a
    # nonexistent parent.  PRAGMA defer_foreign_keys is the correct tool for
    # this (setEpisodeNumber uses it), but it requires no open transaction and
    # can be reset by server middleware.  Temporarily disabling FK checking
    # achieves the same effect and is safe because this is a single controlled
    # transaction that leaves all FK relationships consistent at commit time.
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")

    # --- 3. Shift subsequent seasons (descending to avoid UNIQUE collisions) --
    # NOTE: This shift assumes every season has part_number=1 (the default).
    # Once subdivisions exist (part_number > 1), the WHERE clause below
    # matches multiple rows per season_number and the shift will collide on
    # UNIQUE(show_id, season_number, part_number).  Subdivisions need their
    # own shift logic — revisit when part_number is used in production.
    subsequent = conn.execute(
        "SELECT season_number FROM season"
        " WHERE show_id = ? AND season_number > ? ORDER BY season_number DESC",
        (show_id, season_number),
    ).fetchall()
    shifted = len(subsequent)
    for row in subsequent:
        sn = row["season_number"]
        conn.execute(
            "UPDATE season SET season_number = ?, updated_at = ?"
            " WHERE show_id = ? AND season_number = ?",
            (sn + 1, now, show_id, sn),
        )
        conn.execute(
            "UPDATE episode SET season = ? WHERE show_id = ? AND season = ?",
            (sn + 1, show_id, sn),
        )
        conn.execute(
            "UPDATE watch_event SET season = ? WHERE show_id = ? AND season = ?",
            (sn + 1, show_id, sn),
        )

    # --- 4. Insert the new season at season_number + 1 ------------------------
    new_season_id = ids.generate_id(conn, "z")
    new_sn = season_number + 1
    conn.execute(
        "INSERT INTO season"
        " (id, show_id, season_number, status, anilist_id, mal_id, source, matched,"
        "  manual_override, created_at, updated_at)"
        " VALUES (?, ?, ?, 'planned', ?, ?, 'manual', 1, 1, ?, ?)",
        (new_season_id, show_id, new_sn, new_anilist_id, new_mal_id, now, now),
    )

    # --- 5. Move upper-half episodes to the new season (renumber from 1) ------
    upper_eps.sort(key=lambda e: e["episode"])
    for i, ep in enumerate(upper_eps, start=1):
        conn.execute(
            "UPDATE episode SET season = ?, episode = ?, season_id = ?, updated_at = ?"
            " WHERE id = ?",
            (new_sn, i, new_season_id, now, ep["id"]),
        )
        conn.execute(
            "UPDATE watch_event SET season = ?, episode = ?"
            " WHERE show_id = ? AND season = ? AND episode = ?",
            (new_sn, i, show_id, season_number, ep["episode"]),
        )

    # --- 6. Recompute abs ranges for both halves ------------------------------
    if old_abs_start is not None and old_abs_end is not None:
        lower_count = len(lower_eps)
        lower_abs_end = old_abs_start + lower_count - 1
        upper_abs_start = lower_abs_end + 1
        conn.execute(
            "UPDATE season SET abs_start = ?, abs_end = ?, updated_at = ? WHERE id = ?",
            (old_abs_start, lower_abs_end, now, season_id),
        )
        conn.execute(
            "UPDATE season SET abs_start = ?, abs_end = ?, updated_at = ? WHERE id = ?",
            (upper_abs_start, old_abs_end, now, new_season_id),
        )

    # --- 7. Wire up season_external_id for the new season ---------------------
    season_ranges.upsert_season_external_id(
        conn, new_season_id, new_anilist_id, new_mal_id, now
    )

    # --- 8. Auto-resolve open season_subdivision reviews on the original ------
    conn.execute(
        "UPDATE pending_review"
        " SET resolved_at = ?, resolved_by_client = ?,"
        "     resolution_note = 'auto-resolved by splitSeason'"
        " WHERE entity_type = 'season' AND entity_id = ? AND resolved_at IS NULL"
        "   AND field = 'season_subdivision'",
        (now, client, season_id),
    )

    # Also update the original season's season_id linkage for remaining eps
    conn.execute(
        "UPDATE episode SET season_id = ? WHERE show_id = ? AND season = ?",
        (season_id, show_id, season_number),
    )

    conn.commit()
    # Re-enable FK checking (disabled in step 2 for the split transaction)
    conn.execute("PRAGMA foreign_keys = ON")
    return {
        "lower": season_mapping.get_season(conn, season_id),
        "upper": season_mapping.get_season(conn, new_season_id),
        "seasons_shifted": shifted,
    }


# -- 5.5 id-mapper automatic reconciliation (A.4, §3 principle 1) -----------


@mutation.field("reconcileSeasonMapping")
def resolve_reconcile_season_mapping(_, info, show_id, season_number):
    """Thin GraphQL wrapper — the actual reconciliation logic moved to
    `season_mapping.reconcile_season()` during A.20 (2026-08-09), so
    A.20's own Sonarr-fetch-triggered reconciliation and this explicit
    mutation share one implementation rather than two copies drifting
    apart. See that function's docstring for the full behavior."""
    conn = db.get_connection()
    _require_show(conn, show_id)
    return season_mapping.reconcile_season(conn, show_id, season_number)


@mutation.field("setEpisodeNumberingScheme")
def resolve_set_episode_numbering_scheme(_, info, show_id, scheme):
    conn = db.get_connection()
    _require_show(conn, show_id)
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT id FROM episode_numbering_mapping WHERE show_id = ?", (show_id,)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE episode_numbering_mapping"
            " SET scheme = ?, source = 'manual', matched = 1, manual_override = 1,"
            "     updated_at = ?"
            " WHERE show_id = ?",
            (scheme, now, show_id),
        )
        mapping_id = existing["id"]
    else:
        mapping_id = ids.generate_id(conn, "n")
        conn.execute(
            "INSERT INTO episode_numbering_mapping"
            " (id, show_id, scheme, source, matched, manual_override, created_at, updated_at)"
            " VALUES (?, ?, ?, 'manual', 1, 1, ?, ?)",
            (mapping_id, show_id, scheme, now, now),
        )
    conn.commit()
    return _get_episode_numbering_mapping(conn, mapping_id)


@mutation.field("setEpisodeMovieLink")
def resolve_set_episode_movie_link(_, info, episode_id, movie_show_id):
    conn = db.get_connection()
    _require_episode(conn, episode_id)
    _require_show(conn, movie_show_id)
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT id FROM episode_movie_link WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE episode_movie_link"
            " SET movie_show_id = ?, source = 'manual', matched = 1, manual_override = 1,"
            "     updated_at = ?"
            " WHERE episode_id = ?",
            (movie_show_id, now, episode_id),
        )
        link_id = existing["id"]
    else:
        link_id = ids.generate_id(conn, "m")
        conn.execute(
            "INSERT INTO episode_movie_link"
            " (id, episode_id, movie_show_id, source, matched, manual_override,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, 'manual', 1, 1, ?, ?)",
            (link_id, episode_id, movie_show_id, now, now),
        )
    conn.commit()
    return _get_episode_movie_link(conn, link_id)


# -- 5.6 pending_review ------------------------------------------------------


@mutation.field("resolvePendingReview")
def resolve_resolve_pending_review(_, info, id, resolution_note=None):
    conn = db.get_connection()
    client = require_client(info)
    if client not in RESOLVING_CLIENTS:
        raise GraphQLError(
            f"{client!r} cannot resolve a pending_review — only "
            f"{sorted(RESOLVING_CLIENTS)} can (§5.6)"
        )
    row = conn.execute("SELECT id FROM pending_review WHERE id = ?", (id,)).fetchone()
    if row is None:
        raise GraphQLError(f"no such pending_review: {id}")
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE pending_review"
        " SET resolved_at = ?, resolved_by_client = ?, resolution_note = ?"
        " WHERE id = ?",
        (now, client, resolution_note, id),
    )
    conn.commit()
    return _get_pending_review(conn, id)


# -- 5.9 franchise / next-up manual ordering (§3 principle 6) ----------------


@mutation.field("setFranchiseMemberOrder")
def resolve_set_franchise_member_order(_, info, franchise_id, show_id, sort_order):
    """Overrides an existing membership's sort_order, or creates one —
    same upsert-by-composite-key pattern as the §5.5 id-mapper mutations.
    Both franchise_id and show_id must already exist (no createFranchise
    mutation exists at all — franchises are auto-derived, §5.9 — this
    mutation manages membership/ordering within one, not fabricates a
    new franchise out of thin air)."""
    conn = db.get_connection()
    _require_franchise(conn, franchise_id)
    _require_show(conn, show_id)
    existing = conn.execute(
        "SELECT 1 FROM franchise_member WHERE franchise_id = ? AND show_id = ?",
        (franchise_id, show_id),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE franchise_member SET sort_order = ? WHERE franchise_id = ? AND show_id = ?",
            (sort_order, franchise_id, show_id),
        )
    else:
        conn.execute(
            "INSERT INTO franchise_member (franchise_id, show_id, sort_order) VALUES (?, ?, ?)",
            (franchise_id, show_id, sort_order),
        )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM franchise_member WHERE franchise_id = ? AND show_id = ?",
        (franchise_id, show_id),
    ).fetchone()
    return dict(row)


@mutation.field("setNextUpOrder")
def resolve_set_next_up_order(_, info, show_id, sort_order):
    conn = db.get_connection()
    _require_show(conn, show_id)
    existing = conn.execute(
        "SELECT id FROM next_up_override WHERE show_id = ?", (show_id,)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE next_up_override SET sort_order = ? WHERE show_id = ?",
            (sort_order, show_id),
        )
        override_id = existing["id"]
    else:
        override_id = ids.generate_id(conn, "v")
        conn.execute(
            "INSERT INTO next_up_override (id, show_id, sort_order) VALUES (?, ?, ?)",
            (override_id, show_id, sort_order),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM next_up_override WHERE id = ?", (override_id,)).fetchone()
    return dict(row)


# -- 5.1 custom tags ----------------------------------------------------------


@mutation.field("createTag")
def resolve_create_tag(_, info, name):
    """tag.name is UNIQUE (migration 7196ca889757) — checked here first for
    a clean GraphQLError instead of a raw sqlite3.IntegrityError, same
    reasoning as addShow's own primaryTitle validation."""
    conn = db.get_connection()
    existing = conn.execute("SELECT 1 FROM tag WHERE name = ?", (name,)).fetchone()
    if existing is not None:
        raise GraphQLError(f"a tag named {name!r} already exists")
    tag_id = ids.generate_id(conn, "t")
    now = util.now_utc_iso()
    conn.execute("INSERT INTO tag (id, name, created_at) VALUES (?, ?, ?)", (tag_id, name, now))
    conn.commit()
    return _get_tag(conn, tag_id)


@mutation.field("deleteTag")
def resolve_delete_tag(_, info, tag_id):
    """Cascades to show_tag — deleting a tag is meant to remove it from
    every show it's applied to, not be blocked until manually detached
    from each one first (no ON DELETE CASCADE on the FK, §11.2 raw-SQL
    migrations don't use one, so this does the same thing explicitly).
    show_tag (the child) has to go first — db.py runs with
    `PRAGMA foreign_keys = ON`, so deleting the still-referenced parent
    tag row first would violate the FK, not silently cascade."""
    conn = db.get_connection()
    conn.execute("DELETE FROM show_tag WHERE tag_id = ?", (tag_id,))
    cur = conn.execute("DELETE FROM tag WHERE id = ?", (tag_id,))
    if cur.rowcount == 0:
        conn.rollback()
        raise GraphQLError(f"no such tag: {tag_id}")
    conn.commit()
    return True


@mutation.field("addShowTag")
def resolve_add_show_tag(_, info, show_id, tag_id):
    conn = db.get_connection()
    show = _require_show(conn, show_id)
    _require_tag(conn, tag_id)
    existing = conn.execute(
        "SELECT 1 FROM show_tag WHERE show_id = ? AND tag_id = ?", (show_id, tag_id)
    ).fetchone()
    if existing is None:
        conn.execute("INSERT INTO show_tag (show_id, tag_id) VALUES (?, ?)", (show_id, tag_id))
        conn.commit()
    return show


@mutation.field("removeShowTag")
def resolve_remove_show_tag(_, info, show_id, tag_id):
    conn = db.get_connection()
    show = _require_show(conn, show_id)
    conn.execute("DELETE FROM show_tag WHERE show_id = ? AND tag_id = ?", (show_id, tag_id))
    conn.commit()
    return show


# -- 5.10 saved filter presets -------------------------------------------------
#
# "Server-side entity, freely editable from any client — not read-only, not
# fixed" (§5.10) — no name-uniqueness constraint (unlike tag.name, migration
# 7196ca889757 confirms filter_preset.name has none), so unlike createTag
# there's nothing to validate before inserting.


@mutation.field("createFilterPreset")
def resolve_create_filter_preset(_, info, name, filter_json):
    conn = db.get_connection()
    preset_id = ids.generate_id(conn, "q")
    now = util.now_utc_iso()
    conn.execute(
        "INSERT INTO filter_preset (id, name, filter_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (preset_id, name, filter_json, now, now),
    )
    conn.commit()
    return _get_filter_preset(conn, preset_id)


@mutation.field("updateFilterPreset")
def resolve_update_filter_preset(_, info, id, name=None, filter_json=None):
    """name/filterJson are both optional — a partial update, only the
    fields actually provided change."""
    conn = db.get_connection()
    existing = _require_filter_preset(conn, id)
    new_name = name if name is not None else existing["name"]
    new_filter_json = filter_json if filter_json is not None else existing["filter_json"]
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE filter_preset SET name = ?, filter_json = ?, updated_at = ? WHERE id = ?",
        (new_name, new_filter_json, now, id),
    )
    conn.commit()
    return _get_filter_preset(conn, id)


@mutation.field("deleteFilterPreset")
def resolve_delete_filter_preset(_, info, id):
    conn = db.get_connection()
    cur = conn.execute("DELETE FROM filter_preset WHERE id = ?", (id,))
    if cur.rowcount == 0:
        raise GraphQLError(f"no such filter_preset: {id}")
    conn.commit()
    return True


# -- §6.12 data export/import (A.15) -----------------------------------------


@query.field("exportData")
def resolve_export_data(_, info):
    return export_import.export_data(db.get_connection())


@mutation.field("importData")
def resolve_import_data(_, info, json):
    conn = db.get_connection()
    try:
        counts = export_import.import_data(conn, json)
    except ValueError as e:
        raise GraphQLError(str(e)) from e
    return {
        "schema_version": export_import.SCHEMA_VERSION,
        "shows_imported": counts.get("show", 0),
        "episodes_imported": counts.get("episode", 0),
        "watch_events_imported": counts.get("watch_event", 0),
    }


# -- Art asset mutations ----------------------------------------------------


@mutation.field("fetchShowArt")
def resolve_fetch_show_art(_, info, show_id):
    conn = db.get_connection()
    show = _get_show(conn, show_id)
    if not show:
        raise GraphQLError(f"Show {show_id} not found")
    # Manual/full re-fetch — always runs the whole cascade regardless of
    # any negative-cache stamp, and takes priority over the background
    # staged auto-fetch's own AniList calls for the shared process-wide
    # throttle (anilist_client.py) — see resolve_fetch_show_art_for_
    # seasons/resolve_fetch_show_art_show_level below, the two entry
    # points that check this flag.
    with anilist_client.manual_priority():
        metadata.fetch_show_art(conn, show_id)
    # Return refreshed show
    return _get_show(conn, show_id)


@mutation.field("fetchShowArtForSeasons")
def resolve_fetch_show_art_for_seasons(_, info, show_id, season_ids):
    conn = db.get_connection()
    show = _get_show(conn, show_id)
    if not show:
        raise GraphQLError(f"Show {show_id} not found")
    # Background/staged only — a manual fetch elsewhere in the process
    # takes priority over the shared AniList throttle; skip this turn
    # rather than contend for it (the client-side staged sequence
    # retries a few seconds later on its next stage).
    if not anilist_client.manual_request_pending():
        metadata.fetch_show_art_for_seasons(conn, show_id, season_ids)
    return _get_show(conn, show_id)


@mutation.field("fetchShowArtShowLevel")
def resolve_fetch_show_art_show_level(_, info, show_id):
    conn = db.get_connection()
    show = _get_show(conn, show_id)
    if not show:
        raise GraphQLError(f"Show {show_id} not found")
    if not anilist_client.manual_request_pending():
        metadata.fetch_show_art_show_level(conn, show_id)
    return _get_show(conn, show_id)


@mutation.field("fetchEpisodeSynopses")
def resolve_fetch_episode_synopses(_, info, show_id):
    conn = db.get_connection()
    show = _get_show(conn, show_id)
    if not show:
        raise GraphQLError(f"Show {show_id} not found")
    metadata.fetch_episode_synopses(conn, show_id)
    return _get_show(conn, show_id)


@mutation.field("setShowSynopsis")
def resolve_set_show_synopsis(_, info, show_id, synopsis):
    conn = db.get_connection()
    _require_show(conn, show_id)
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE show SET synopsis = ?, updated_at = ? WHERE id = ?",
        (synopsis, now, show_id),
    )
    conn.commit()
    return _get_show(conn, show_id)


@mutation.field("setEpisodeSynopsis")
def resolve_set_episode_synopsis(_, info, episode_id, synopsis):
    conn = db.get_connection()
    _require_episode(conn, episode_id)
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE episode SET synopsis = ?, updated_at = ? WHERE id = ?",
        (synopsis, now, episode_id),
    )
    conn.commit()
    return _get_episode(conn, episode_id)


@mutation.field("fetchSynopsisCandidates")
def resolve_fetch_synopsis_candidates(_, info, show_id, episode_id=None):
    conn = db.get_connection()
    _require_show(conn, show_id)
    if episode_id is not None:
        _require_episode(conn, episode_id)
    return metadata.fetch_synopsis_candidates(conn, show_id, episode_id)


@mutation.field("selectArtAsset")
def resolve_select_art_asset(_, info, id):
    conn = db.get_connection()
    try:
        return art.select_asset(conn, id)
    except ValueError as e:
        raise GraphQLError(str(e)) from e


@mutation.field("deselectArtAsset")
def resolve_deselect_art_asset(_, info, id):
    conn = db.get_connection()
    try:
        return art.deselect_asset(conn, id)
    except ValueError as e:
        raise GraphQLError(str(e)) from e


@mutation.field("addManualArtUrl")
def resolve_add_manual_art_url(_, info, show_id, kind, url, season_id=None, episode_kind=None):
    conn = db.get_connection()
    _require_show(conn, show_id)
    episode_kind = episode_kind or "regular"
    # source_score=100 — outranks every real source (TVDB's own scores
    # top out well below this, TVmaze/MAL/AniList carry none at all), so
    # a manually-pasted URL is what auto_select_best actually picks.
    asset_id = art.upsert_asset(
        conn, show_id, season_id, kind, "manual", url, source_score=100,
        episode_kind=episode_kind,
    )
    asset = art.select_asset(conn, asset_id)
    # The negative cache only ever tracks the REGULAR slot (see
    # update_art_negative_cache's own docstring) — a special/OVA/bonus-
    # movie cover has no cache entry to clear.
    if episode_kind == "regular":
        metadata.update_art_negative_cache(conn, show_id)
    conn.commit()
    return asset


@mutation.field("deleteArtAsset")
def resolve_delete_art_asset(_, info, id):
    conn = db.get_connection()
    asset = conn.execute("SELECT show_id FROM art_asset WHERE id = ?", (id,)).fetchone()
    if not asset:
        raise GraphQLError(f"Art asset {id} not found")
    try:
        art.delete_asset(conn, id)
    except ValueError as e:
        raise GraphQLError(str(e)) from e
    return True


# -- ArtAsset field resolvers -----------------------------------------------
# kind enum: the ArtKind EnumType binding handles the DB's lowercase
# "poster"/"banner"/"background" → GraphQL POSTER/BANNER/BACKGROUND
# conversion automatically.  Only `selected` needs explicit conversion
# (SQLite stores 0/1, GraphQL expects Boolean).


@art_asset_type.field("selected")
def resolve_art_asset_selected(obj, info):
    return bool(obj.get("selected"))


# -- outbound push, "webhook push to clients" design, 2026-08-25 ------------
# See schema.graphql's own `type Subscription` docstring and
# lcars/events.py's module docstring for the full design rationale.
# Each `.source` generator below just adapts events.subscribe(topic)'s
# raw payload (an id) into the same row-dict shape resolve_show/
# resolve_episode already return, reusing _get_show/_get_episode rather
# than duplicating that lookup — a subscriber sees exactly the same
# Show/Episode object shape a query for the same id would return.


@subscription.source("episodeAvailabilityChanged")
async def source_episode_availability_changed(_, info):
    async for episode_id in events.subscribe("episode_availability_changed"):
        yield episode_id


@subscription.field("episodeAvailabilityChanged")
def resolve_episode_availability_changed(episode_id, info):
    return _get_episode(db.get_connection(), episode_id)


@subscription.source("showCreated")
async def source_show_created(_, info):
    async for show_id in events.subscribe("show_created"):
        yield show_id


@subscription.field("showCreated")
def resolve_show_created(show_id, info):
    return _get_show(db.get_connection(), show_id)
