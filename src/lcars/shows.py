"""The actual "create a show" logic — extracted out of the `addShow`
resolver, B.11d (`~/repos/starfleet` BUILD_PLAN.md), so `show_backfill.py`
can call it directly (once per untracked Sonarr/Radarr item, throttled)
without going through a GraphQL request at all — same "extract the
shared logic into its own module" pattern `reconcileSeasonMapping`'s
own `season_mapping.py` already established (SCOPE.md's own A.4 note).
resolvers.py's `addShow` mutation is now a thin wrapper around
`create_show()` below, unchanged in behavior/shape.

`_EXTERNAL_ID_URL_TEMPLATES`/`_TMDB_URL_TEMPLATES` are the canonical
copy as of this move (resolvers.py's own former copy removed, since
nothing else there used them once this extraction happened) —
metadata.py still keeps its own separate duplicate, same reasoning as
always: resolvers.py imports this module (to wire the mutation), so
the reverse import would be circular, and metadata.py imports neither.

Best-effort deep-link templates for AddShowInput's optional external
ids (SCOPE.md §5.4's show_external_id.url) — real-world, well-known
URL formats, not a design question. tmdb's own path segment depends on
media_shape (movie vs. tv) — caught during a full audit pass, A.19:
the original single "/movie/{id}" template would have produced a
wrong link for a tmdbId supplied on an episodic (tracking_space=tv)
show. Not a hypothetical: §5.4 itself already notes "Movies key
primarily on TMDB... but [episodic shows] carry ... TMDB ids too where
they exist."
"""

from lcars import ids, metadata, util

_EXTERNAL_ID_URL_TEMPLATES = {
    "anilist": "https://anilist.co/anime/{id}",
    "tvdb": "https://thetvdb.com/dereferrer/series/{id}",
    "imdb": "https://www.imdb.com/title/{id}/",
    "mal": "https://myanimelist.net/anime/{id}",
}
_TMDB_URL_TEMPLATES = {
    "movie": "https://www.themoviedb.org/movie/{id}",
    "episodic": "https://www.themoviedb.org/tv/{id}",
}


class ShowInputError(ValueError):
    """Raised instead of ariadne's GraphQLError — this module has no
    GraphQL request context of its own (show_backfill.py calls it
    directly, outside any resolver); resolvers.py's own addShow wrapper
    re-raises this as a GraphQLError to keep the mutation's existing
    error shape unchanged."""


def create_show(conn, input: dict) -> str:
    """§5.1, A.4/A.8 — inserts the `show` row, its external-id links,
    then runs the on-demand metadata fetch (A.8: episodes, AniList
    link resolution, Fribb season reconciliation — best-effort, never
    raises, see metadata.py's own docstring) inline, exactly as
    `addShow` always has. Returns the new show's id; callers needing
    the full row do their own lookup (resolvers.py's own `_get_show`,
    or a plain `SELECT * FROM show WHERE id = ?` — show_backfill.py
    doesn't need the full row at all, just the id, so this doesn't
    return one)."""
    primary = input["primary_title"]
    title_field = f"title_{primary}"
    if not input.get(title_field):
        raise ShowInputError(
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
            url = _EXTERNAL_ID_URL_TEMPLATES[service].format(id=value)
            conn.execute(
                "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (show_id, service, str(value), url, now),
            )

    tmdb_id = input.get("tmdb_id")
    if tmdb_id is not None:
        url = _TMDB_URL_TEMPLATES[input["media_shape"]].format(id=tmdb_id)
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'tmdb', ?, ?, ?)",
            (show_id, str(tmdb_id), url, now),
        )

    conn.commit()

    # A.8 — the on-demand metadata fetch itself, best-effort (see
    # metadata.py's own docstring: never raises, failures go to
    # pending_review instead). A separate commit rather than folding
    # into the block above: the bare show already exists and is
    # queryable even if every branch of the fetch below fails outright.
    metadata.fetch_and_populate(conn, show_id)
    conn.commit()
    return show_id
