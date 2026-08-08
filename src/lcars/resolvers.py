"""Resolvers — BUILD_PLAN.md A.3, vertical slice: Show/Episode/WatchEvent
and their core mutations. The rest of the schema (Person/Studio/
Franchise/tags/id-mapper/pending_review/deletion/export-import/...) is
unbound for now — a client querying those fields gets a clear GraphQL
error (missing resolver / null on a non-null field), not silently wrong
data. Expanding table-by-table is later A.3 work, tracked in
BUILD_PLAN.md, not a hidden gap.

Field resolution: `convert_names_case=True` (passed to
make_executable_schema in server.py) handles camelCase-GraphQL-field to
snake_case-dict-key mapping automatically for every plain scalar field —
the ObjectType bindings below only exist for fields that need real
logic (relationships, computed values, paginated connections).
"""

import json

from ariadne import EnumType, MutationType, ObjectType, QueryType
from graphql import GraphQLError

from lcars import db, ids, pagination, util

query = QueryType()
mutation = MutationType()
show_type = ObjectType("Show")
episode_type = ObjectType("Episode")
watch_event_type = ObjectType("WatchEvent")


def _enum(name: str, *values: str) -> EnumType:
    """SDL enum values are UPPER_SNAKE; DB-stored values are the exact
    lowercase equivalent in every case here (verified against every
    CHECK constraint in the A.1/A.1-addendum migrations) — so a plain
    `.upper()` derivation is correct, not a coincidence to special-case."""
    return EnumType(name, {v.upper(): v for v in values})


ENUMS = [
    _enum("MediaShape", "episodic", "movie"),
    _enum("TrackingSpace", "tv", "anime"),
    _enum("ShowStatus", "watching", "planned", "paused", "completed", "dropped"),
    _enum("EpisodeKind", "regular", "special", "ova", "bonus_movie"),
    _enum("AirDateSource", "sonarr", "anilist", "animeschedule", "manual"),
    _enum("EpisodeState", "unwatched", "watched", "skipped"),
    _enum("PersonRoleType", "voice_actor", "actor", "staff"),
    _enum("StudioRoleType", "studio", "publisher", "network"),
    _enum("PrimaryTitle", "romaji", "english", "native"),
    _enum("ShowIdMappingSource", "fribb", "manual", "unmatched"),
    _enum("NumberingScheme", "absolute", "season_episode"),
    _enum("NumberingSource", "sonarr", "anilist", "manual", "unmatched"),
    _enum("EpisodeMovieLinkSource", "tmdb_match", "manual", "unmatched"),
    _enum("ResolvedByClient", "data", "holodeck", "captains_log"),
]

BINDABLES = [
    query,
    mutation,
    show_type,
    episode_type,
    watch_event_type,
    *ENUMS,
    util.datetime_scalar,
]

# Best-effort deep-link templates for AddShowInput's optional external ids
# (SCOPE.md §5.4's show_external_id.url) — real-world, well-known URL
# formats, not a design question. A.8's on-demand metadata fetch may
# later refine/replace these once it exists.
EXTERNAL_ID_URL_TEMPLATES = {
    "anilist": "https://anilist.co/anime/{id}",
    "tvdb": "https://thetvdb.com/dereferrer/series/{id}",
    "tmdb": "https://www.themoviedb.org/movie/{id}",
    "imdb": "https://www.imdb.com/title/{id}/",
    "mal": "https://myanimelist.net/anime/{id}",
}


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


def _get_episode(conn, episode_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM episode WHERE id = ?", (episode_id,)).fetchone()
    return dict(row) if row else None


# --- Query -------------------------------------------------------------


@query.field("show")
def resolve_show(_, info, id):  # noqa: A002 (id shadows builtin — matches the GraphQL arg name)
    return _get_show(db.get_connection(), id)


@query.field("shows")
def resolve_shows(_, info, **page_args):
    return pagination.paginate(db.get_connection(), "show", "1 = 1", (), **page_args)


@query.field("showsByStatus")
def resolve_shows_by_status(_, info, statuses, **page_args):
    placeholders = ", ".join("?" for _ in statuses)
    return pagination.paginate(
        db.get_connection(), "show", f"status IN ({placeholders})", tuple(statuses), **page_args
    )


# --- Show fields ---------------------------------------------------------


@show_type.field("displayTitle")
def resolve_display_title(obj, info):
    return obj[f"title_{obj['primary_title']}"]


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


@show_type.field("externalIds")
def resolve_show_external_ids(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(), "show_external_id", "show_id = ?", (obj["id"],), **page_args
    )


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


# --- Episode fields ------------------------------------------------------


@episode_type.field("show")
def resolve_episode_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


@episode_type.field("watchEvents")
def resolve_episode_watch_events(obj, info, **page_args):
    return pagination.paginate(
        db.get_connection(),
        "watch_event",
        "show_id = ? AND season = ? AND episode = ?",
        (obj["show_id"], obj["season"], obj["episode"]),
        **page_args,
    )


# --- WatchEvent fields -----------------------------------------------------


@watch_event_type.field("show")
def resolve_watch_event_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


# --- Mutation --------------------------------------------------------------


@mutation.field("addShow")
def resolve_add_show(_, info, input):  # noqa: A002 (matches the GraphQL arg name)
    conn = db.get_connection()
    primary = input["primary_title"]
    title_field = f"title_{primary}"
    if not input.get(title_field):
        raise GraphQLError(
            f"primaryTitle is {primary.upper()} but {title_field.replace('_', ' ', 1)}"
            " (as camelCase) was not provided"
        )

    show_id = ids.generate_id(conn, "s")
    now = util.now_utc_iso()
    conn.execute(
        """
        INSERT INTO show (
            id, media_shape, tracking_space,
            title_romaji, title_english, title_native, primary_title,
            status, tracked, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', 1, ?, ?)
        """,
        (
            show_id,
            input["media_shape"],
            input["tracking_space"],
            input.get("title_romaji"),
            input.get("title_english"),
            input.get("title_native"),
            primary,
            now,
            now,
        ),
    )

    for service, key in (
        ("anilist", "anilist_id"),
        ("tvdb", "tvdb_id"),
        ("tmdb", "tmdb_id"),
        ("imdb", "imdb_id"),
        ("mal", "mal_id"),
    ):
        value = input.get(key)
        if value is not None:
            url = EXTERNAL_ID_URL_TEMPLATES[service].format(id=value)
            conn.execute(
                "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (show_id, service, str(value), url, now),
            )

    conn.commit()
    return _get_show(conn, show_id)


@mutation.field("setStatus")
def resolve_set_status(_, info, show_id, status):
    conn = db.get_connection()
    client = require_client(info)
    row = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        raise GraphQLError(f"no such show: {show_id}")
    now = util.now_utc_iso()
    conn.execute("UPDATE show SET status = ?, updated_at = ? WHERE id = ?", (status, now, show_id))
    conn.execute(
        "INSERT INTO status_change"
        " (id, show_id, previous_status, new_status, changed_at, changed_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ids.generate_id(conn, "c"), show_id, row["status"], status, now, client),
    )
    conn.commit()
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
    conn.commit()
    return _get_show(conn, show_id)


@mutation.field("setTracked")
def resolve_set_tracked(_, info, show_id, tracked):
    conn = db.get_connection()
    client = require_client(info)
    row = conn.execute("SELECT tracked FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        raise GraphQLError(f"no such show: {show_id}")
    now = util.now_utc_iso()
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
    return _get_show(conn, show_id)


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
    conn.commit()
    row = conn.execute("SELECT rowid, * FROM watch_event WHERE id = ?", (watch_id,)).fetchone()
    return dict(row)


@mutation.field("markEpisodeSkipped")
def resolve_mark_episode_skipped(_, info, episode_id):
    conn = db.get_connection()
    now = util.now_utc_iso()
    cur = conn.execute(
        "UPDATE episode SET state = 'skipped', updated_at = ? WHERE id = ?", (now, episode_id)
    )
    if cur.rowcount == 0:
        raise GraphQLError(f"no such episode: {episode_id}")
    conn.commit()
    return _get_episode(conn, episode_id)
