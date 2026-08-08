"""Resolvers — BUILD_PLAN.md A.3, expanded beyond the original
Show/Episode/WatchEvent vertical slice to also cover the id-mapper
tables (ShowIdMapping/EpisodeNumberingMapping/EpisodeMovieLink, §5.5 +
its addendum) and PendingReview (§5.6) — natural next slice since A.4/
A.5 build on top of them. The rest of the schema (Person/Studio/
Franchise/tags/deletion/export-import/...) is still unbound — a client
querying those fields gets a clear GraphQL error (missing resolver /
null on a non-null field), not silently wrong data. Expanding
table-by-table is later A.3 work, tracked in BUILD_PLAN.md, not a
hidden gap.

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
show_id_mapping_type = ObjectType("ShowIdMapping")
episode_numbering_mapping_type = ObjectType("EpisodeNumberingMapping")
episode_movie_link_type = ObjectType("EpisodeMovieLink")
pending_review_type = ObjectType("PendingReview")


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
    show_id_mapping_type,
    episode_numbering_mapping_type,
    episode_movie_link_type,
    pending_review_type,
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

# Best-effort deep-link templates for AddShowInput's optional external ids
# (SCOPE.md §5.4's show_external_id.url) — real-world, well-known URL
# formats, not a design question. A.8's on-demand metadata fetch may
# later refine/replace these once it exists.
#
# tmdb's own path segment depends on media_shape (movie vs. tv) — caught
# during a full audit pass: the original single "/movie/{id}" template
# would have produced a wrong link for a tmdbId supplied on an episodic
# (tracking_space=tv) show. Not a hypothetical: §5.4 itself already notes
# "Movies key primarily on TMDB... but [episodic shows] carry ... TMDB ids
# too where they exist."
EXTERNAL_ID_URL_TEMPLATES = {
    "anilist": "https://anilist.co/anime/{id}",
    "tvdb": "https://thetvdb.com/dereferrer/series/{id}",
    "imdb": "https://www.imdb.com/title/{id}/",
    "mal": "https://myanimelist.net/anime/{id}",
}
TMDB_URL_TEMPLATES = {
    "movie": "https://www.themoviedb.org/movie/{id}",
    "episodic": "https://www.themoviedb.org/tv/{id}",
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


def _get_show_id_mapping(conn, mapping_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM show_id_mapping WHERE id = ?", (mapping_id,)).fetchone()
    return dict(row) if row else None


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


# --- Query -------------------------------------------------------------


@query.field("show")
def resolve_show(_, info, id):  # noqa: A002 (id shadows builtin — matches the GraphQL arg name)
    return _get_show(db.get_connection(), id)


@query.field("shows")
def resolve_shows(_, info, **page_args):
    return pagination.paginate(db.get_connection(), "show", "1 = 1", (), **page_args)


@query.field("episode")
def resolve_episode(_, info, id):  # noqa: A002
    return _get_episode(db.get_connection(), id)


@query.field("showsByStatus")
def resolve_shows_by_status(_, info, statuses, **page_args):
    placeholders = ", ".join("?" for _ in statuses)
    return pagination.paginate(
        db.get_connection(), "show", f"status IN ({placeholders})", tuple(statuses), **page_args
    )


@query.field("pendingReviews")
def resolve_pending_reviews(_, info, include_resolved=False, **page_args):
    where = "1 = 1" if include_resolved else "resolved_at IS NULL"
    return pagination.paginate(db.get_connection(), "pending_review", where, (), **page_args)


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


@show_type.field("idMapping")
def resolve_show_id_mapping_field(obj, info):
    row = db.get_connection().execute(
        "SELECT * FROM show_id_mapping WHERE show_id = ?", (obj["id"],)
    ).fetchone()
    return dict(row) if row else None


@show_type.field("episodeNumberingMapping")
def resolve_show_episode_numbering_mapping_field(obj, info):
    row = db.get_connection().execute(
        "SELECT * FROM episode_numbering_mapping WHERE show_id = ?", (obj["id"],)
    ).fetchone()
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


# --- Episode fields ------------------------------------------------------


@episode_type.field("show")
def resolve_episode_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


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


# --- WatchEvent fields -----------------------------------------------------


@watch_event_type.field("show")
def resolve_watch_event_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


# --- ShowIdMapping / EpisodeNumberingMapping / EpisodeMovieLink fields ------


@show_id_mapping_type.field("show")
def resolve_show_id_mapping_show(obj, info):
    return _get_show(db.get_connection(), obj["show_id"])


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

    tmdb_id = input.get("tmdb_id")
    if tmdb_id is not None:
        url = TMDB_URL_TEMPLATES[input["media_shape"]].format(id=tmdb_id)
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'tmdb', ?, ?, ?)",
            (show_id, str(tmdb_id), url, now),
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


# -- 5.5 id-mapper manual overrides (§3 principle 6: manual wins once set) --
#
# None of these three tables carry a changed_by-style column (unlike
# status_change/score_change/tracked_change, §5.7) — no dedicated history
# table exists for id-mapping changes either — so these mutations don't
# call require_client(): there's nowhere in the schema to put the value.


@mutation.field("setShowIdMapping")
def resolve_set_show_id_mapping(_, info, show_id, tvdb_id=None, anilist_id=None):
    conn = db.get_connection()
    _require_show(conn, show_id)
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT id FROM show_id_mapping WHERE show_id = ?", (show_id,)
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE show_id_mapping"
            " SET tvdb_id = ?, anilist_id = ?, source = 'manual',"
            "     matched = 1, manual_override = 1, updated_at = ?"
            " WHERE show_id = ?",
            (tvdb_id, anilist_id, now, show_id),
        )
        mapping_id = existing["id"]
    else:
        mapping_id = ids.generate_id(conn, "x")
        conn.execute(
            "INSERT INTO show_id_mapping"
            " (id, show_id, tvdb_id, anilist_id, source, matched, manual_override,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'manual', 1, 1, ?, ?)",
            (mapping_id, show_id, tvdb_id, anilist_id, now, now),
        )
    conn.commit()
    return _get_show_id_mapping(conn, mapping_id)


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
def resolve_resolve_pending_review(_, info, id, resolution_note=None):  # noqa: A002
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
