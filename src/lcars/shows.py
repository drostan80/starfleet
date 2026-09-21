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

from lcars import (
    config,
    events,
    ids,
    metadata,
    radarr_client,
    service_health,
    sonarr_client,
    util,
)

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


class SequelDetectedError(ShowInputError):
    """Raised when the add flow detects that the requested show is a
    sequel of an already-tracked show.  Carries structured info so the
    client can offer a confirmation prompt ("attach as Season N of …?")
    rather than silently creating a separate show.

    Attributes:
        parent_show_id:  id of the tracked show this is a sequel of.
        parent_title:    display title of the parent.
        next_season:     the season number the sequel would occupy.
        sequel_anilist_id / sequel_mal_id:  the external ids the caller
            supplied (passed through so the client doesn't have to
            re-derive them from the original input after the prompt).
    """

    def __init__(
        self, parent_show_id: str, parent_title: str, next_season: int,
        sequel_anilist_id: int | None = None, sequel_mal_id: int | None = None,
    ):
        self.parent_show_id = parent_show_id
        self.parent_title = parent_title
        self.next_season = next_season
        self.sequel_anilist_id = sequel_anilist_id
        self.sequel_mal_id = sequel_mal_id
        import json
        payload = json.dumps({
            "parentShowId": parent_show_id,
            "parentTitle": parent_title,
            "nextSeason": next_season,
            "sequelAnilistId": sequel_anilist_id,
            "sequelMalId": sequel_mal_id,
        }, separators=(",", ":"))
        super().__init__(f"sequel_of:{payload}")


class LaterSeasonError(ShowInputError):
    """Raised when the add flow detects that the requested show is S2+
    of a franchise where *no* season is tracked yet.  Carries S1's info
    so the client can offer "Add Season 1 instead?".
    """

    def __init__(
        self, season_number: int,
        s1_anilist_id: int, s1_mal_id: int | None,
        s1_title_romaji: str | None, s1_title_english: str | None,
    ):
        self.season_number = season_number
        self.s1_anilist_id = s1_anilist_id
        self.s1_mal_id = s1_mal_id
        self.s1_title_romaji = s1_title_romaji
        self.s1_title_english = s1_title_english
        import json
        payload = json.dumps({
            "seasonNumber": season_number,
            "s1AnilistId": s1_anilist_id,
            "s1MalId": s1_mal_id,
            "s1TitleRomaji": s1_title_romaji,
            "s1TitleEnglish": s1_title_english,
        }, separators=(",", ":"))
        super().__init__(f"later_season:{payload}")


def find_sequel_parent(
    conn,
    anilist_id: int | str | None = None,
    *,
    tvdb_id: int | str | None = None,
    tmdb_id: int | str | None = None,
    imdb_id: str | None = None,
    mal_id: int | str | None = None,
) -> dict | None:
    """Check whether the given IDs belong to a show that is a sequel of
    a tracked parent.

    Returns ``{"parent_show_id", "parent_title", "next_season",
    "stub_show_id"}`` when a match is found, else ``None``.

    Tiers of lookup (stops at first hit):
      1. Local DB relations (show_relation, both directions).
      2. TVDB franchise collision — same TVDB ID = same franchise.
         Resolves TVDB from input, or via Fribb (anilist/mal→tvdb)
         or Wikidata (tmdb/imdb→tvdb), cache-only (no network).
      3. Live AniList relations — last resort, only when no local
         stub exists (no show_external_id row for any provided ID
         except tvdb, which is franchise-level and excluded from
         stub lookup).

    Matches both explicit SEQUEL relations and NULL-typed relations.
    Excludes CHARACTER, ALTERNATIVE, SUMMARY, SPIN_OFF, SIDE_STORY,
    OTHER, PARENT.

    ``next_season`` is max(existing season_number) + 1 on the parent."""
    if all(v is None for v in (anilist_id, tvdb_id, tmdb_id, imdb_id, mal_id)):
        return None

    # Find any existing stub for this show across all provided IDs.
    stub_show_id = _find_stub_show(conn, anilist_id, tvdb_id, tmdb_id, imdb_id, mal_id)

    # Tier 1: Local DB relations.
    if stub_show_id is not None:
        parent_row = _find_parent_via_local_relations(conn, stub_show_id)
        if parent_row is not None:
            return _build_sequel_result(conn, parent_row, stub_show_id)

    # Tier 2: TVDB franchise collision.
    resolved_tvdb = _resolve_tvdb_for_sequel(tvdb_id, anilist_id, mal_id, tmdb_id, imdb_id)
    if resolved_tvdb is not None:
        parent_row = _find_parent_via_tvdb(conn, resolved_tvdb, stub_show_id)
        if parent_row is not None:
            fribb_season = _resolve_fribb_season(anilist_id)
            if fribb_season is not None:
                already = conn.execute(
                    "SELECT 1 FROM season WHERE show_id = ? AND season_number = ?",
                    (parent_row["parent_id"], fribb_season),
                ).fetchone()
                if already is not None:
                    return None
                return _build_sequel_result(
                    conn, parent_row, stub_show_id, next_season=fribb_season,
                )
            return _build_sequel_result(conn, parent_row, stub_show_id)

    # Tier 3: AniList live relations (last resort, no-stub only).
    if anilist_id is not None and stub_show_id is None:
        return _find_parent_via_anilist(conn, int(anilist_id))

    return None


def _find_stub_show(conn, anilist_id, tvdb_id, tmdb_id, imdb_id, mal_id) -> str | None:
    """Find an existing show row that owns any of the provided IDs.

    Excludes tvdb_id: TVDB groups a franchise under one series ID, so
    the incoming sequel's tvdb_id IS the parent's — looking it up here
    would find the parent and then self-exclude it in the TVDB collision
    tier, defeating the purpose."""
    for service, value in (
        ("anilist", anilist_id), ("tmdb", tmdb_id),
        ("imdb", imdb_id), ("mal", mal_id),
    ):
        if value is None:
            continue
        row = conn.execute(
            "SELECT show_id FROM show_external_id"
            " WHERE service = ? AND external_id = ?",
            (service, str(value)),
        ).fetchone()
        if row is not None:
            return row["show_id"]
    return None


def _resolve_tvdb_for_sequel(tvdb_id, anilist_id, mal_id, tmdb_id, imdb_id) -> str | None:
    """Best-effort TVDB ID resolution from available IDs, cache-only."""
    if tvdb_id is not None:
        return str(tvdb_id)

    # Fribb: anilist/mal → tvdb (anime shows).
    dataset = _try_load_fribb_dataset()
    if dataset is not None:
        from lcars import fribb as fribb_mod
        if anilist_id is not None:
            index = fribb_mod.build_anilist_index(dataset)
            result = fribb_mod.resolve_tvdb_id_for_anilist(index, int(anilist_id))
            if result is not None:
                return str(result)
        if mal_id is not None:
            mal_index = fribb_mod.build_mal_index(dataset)
            candidates = mal_index.get(int(mal_id), [])
            tvdb_ids = {c["tvdb_id"] for c in candidates
                        if c.get("tvdb_id") not in (None, "", "unknown")}
            if len(tvdb_ids) == 1:
                return str(tvdb_ids.pop())

    # Wikidata: tmdb/imdb → tvdb (TV shows).
    if tmdb_id is not None or imdb_id is not None:
        resolved = _try_wikidata_to_tvdb(tmdb_id, imdb_id)
        if resolved is not None:
            return resolved

    return None


def _try_load_fribb_dataset():
    """Return the Fribb dataset if cached on disk, None otherwise."""
    from lcars import fribb as fribb_mod
    if not fribb_mod.DATASET_CACHE_PATH.exists():
        return None
    try:
        return fribb_mod.load_dataset(max_age=float("inf"))
    except Exception:
        return None


def _try_wikidata_to_tvdb(tmdb_id, imdb_id) -> str | None:
    """Resolve TVDB ID via Wikidata bridge, cache-only."""
    from lcars import wikidata
    if not wikidata.CACHE_PATH.exists():
        return None
    try:
        dataset = wikidata.load_dataset(max_age=float("inf"))
    except Exception:
        return None
    if tmdb_id is not None:
        index = wikidata.build_tmdb_to_tvdb_index(dataset)
        result = index.get(str(tmdb_id))
        if result is not None:
            return result
    if imdb_id is not None:
        index = wikidata.build_imdb_to_tvdb_index(dataset)
        result = index.get(str(imdb_id))
        if result is not None:
            return result
    return None


def _resolve_fribb_season(anilist_id) -> int | None:
    """Which TVDB season does this AniList entry represent? Cache-only."""
    if anilist_id is None:
        return None
    dataset = _try_load_fribb_dataset()
    if dataset is None:
        return None
    from lcars import fribb as fribb_mod
    index = fribb_mod.build_anilist_index(dataset)
    entries = index.get(int(anilist_id), [])
    if len(entries) != 1:
        return None
    season_info = entries[0].get("season")
    if not isinstance(season_info, dict):
        return None
    tvdb_season = season_info.get("tvdb")
    if isinstance(tvdb_season, int) and tvdb_season > 0:
        return tvdb_season
    return None


def _check_later_season_pre_add(conn, anilist_id) -> None:
    """Raise LaterSeasonError if Fribb says this AniList entry is S2+
    of a franchise with no tracked seasons.  Called before creating the
    show so the client can offer to add S1 instead."""
    if anilist_id is None:
        return
    dataset = _try_load_fribb_dataset()
    if dataset is None:
        return
    from lcars import fribb as fribb_mod

    anilist_index = fribb_mod.build_anilist_index(dataset)
    entries = anilist_index.get(int(anilist_id), [])
    if len(entries) != 1:
        return
    season_info = entries[0].get("season")
    if not isinstance(season_info, dict):
        return
    tvdb_season = season_info.get("tvdb")
    if not isinstance(tvdb_season, int) or tvdb_season <= 1:
        return

    tvdb_id = entries[0].get("tvdb_id")
    if tvdb_id in (None, "", "unknown"):
        return

    # If a tracked show already owns this TVDB ID, the franchise IS
    # tracked — SequelDetectedError (or normal add) handles that path.
    tracked_owner = conn.execute(
        "SELECT 1 FROM show_external_id sei"
        " JOIN show s ON s.id = sei.show_id"
        " WHERE sei.service = 'tvdb' AND sei.external_id = ?"
        "   AND s.tracked = 1"
        " LIMIT 1",
        (str(tvdb_id),),
    ).fetchone()
    if tracked_owner is not None:
        return

    # Find S1's entry: same TVDB ID, season.tvdb == 1.
    tvdb_index = fribb_mod.build_tvdb_index(dataset)
    tvdb_entries = tvdb_index.get(tvdb_id, [])
    s1_matches = [
        e for e in tvdb_entries
        if isinstance(e.get("season"), dict)
        and e["season"].get("tvdb") == 1
        and e.get("anilist_id") not in (None, "", "unknown")
        and e["anilist_id"] != int(anilist_id)
    ]
    if len(s1_matches) != 1:
        return
    s1 = s1_matches[0]
    s1_anilist_id = s1["anilist_id"]
    s1_mal_id = s1.get("mal_id")
    if s1_mal_id in ("", "unknown"):
        s1_mal_id = None

    # Fetch S1's titles from AniList (interactive add path — one API
    # call is acceptable).
    from lcars import anilist_client
    try:
        media = anilist_client.fetch_media(s1_anilist_id)
    except Exception:
        return
    if media is None:
        return
    titles = media.get("title") or {}
    raise LaterSeasonError(
        season_number=tvdb_season,
        s1_anilist_id=s1_anilist_id,
        s1_mal_id=s1_mal_id,
        s1_title_romaji=titles.get("romaji"),
        s1_title_english=titles.get("english"),
    )


def _find_parent_via_tvdb(conn, tvdb_id_str: str, exclude_show_id: str | None) -> dict | None:
    """Find a tracked show sharing the same TVDB ID (franchise collision)."""
    params: list = [tvdb_id_str]
    exclude = ""
    if exclude_show_id is not None:
        exclude = " AND s.id != ?"
        params.append(exclude_show_id)
    return conn.execute(
        "SELECT sei.show_id AS parent_id,"
        "       s.title_english, s.title_romaji, s.primary_title"
        " FROM show_external_id sei"
        " JOIN show s ON s.id = sei.show_id"
        " WHERE sei.service = 'tvdb' AND sei.external_id = ?"
        f"   AND s.tracked = 1{exclude}"
        " LIMIT 1",
        params,
    ).fetchone()


# Relation types that are clearly NOT "this is a sequel of..."
_NON_SEQUEL_TYPES = (
    'PREQUEL', 'CHARACTER', 'ALTERNATIVE', 'SUMMARY',
    'SPIN_OFF', 'SIDE_STORY', 'OTHER', 'PARENT',
)


def _find_parent_via_local_relations(conn, related_id: str) -> dict | None:
    """Check show_relation in both directions for a tracked parent."""
    # a) Forward: someone lists us as their SEQUEL (or NULL).
    parent_row = conn.execute(
        "SELECT sr.show_id AS parent_id,"
        "       s.title_english, s.title_romaji, s.primary_title"
        " FROM show_relation sr"
        " JOIN show s ON s.id = sr.show_id"
        " WHERE sr.related_show_id = ?"
        "   AND sr.show_id != ?"
        "   AND (sr.relation_type IS NULL OR sr.relation_type NOT IN"
        f"       ({','.join('?' for _ in _NON_SEQUEL_TYPES)}))"
        "   AND s.tracked = 1",
        (related_id, related_id, *_NON_SEQUEL_TYPES),
    ).fetchone()
    if parent_row is not None:
        return parent_row
    # b) Reverse: we list someone as our PREQUEL.
    return conn.execute(
        "SELECT sr.related_show_id AS parent_id,"
        "       s.title_english, s.title_romaji, s.primary_title"
        " FROM show_relation sr"
        " JOIN show s ON s.id = sr.related_show_id"
        " WHERE sr.show_id = ?"
        "   AND sr.related_show_id != ?"
        "   AND sr.relation_type = 'PREQUEL'"
        "   AND s.tracked = 1",
        (related_id, related_id),
    ).fetchone()


def _find_parent_via_anilist(conn, anilist_id: int) -> dict | None:
    """Fetch relations from AniList and check if any PREQUEL target is
    tracked locally.  Best-effort: AniList errors return None (no
    sequel detected), same as a missing stub."""
    import logging

    from lcars import anilist_client
    log = logging.getLogger(__name__)
    try:
        media = anilist_client.fetch_media(anilist_id)
    except Exception:
        log.debug("AniList fetch failed for %d during sequel check — skipping", anilist_id)
        return None
    if media is None:
        return None
    edges = (media.get("relations") or {}).get("edges") or []
    # Look for PREQUEL relations (this show's prequel = the parent).
    prequel_anilist_ids = []
    for edge in edges:
        rtype = edge.get("relationType")
        if rtype == "PREQUEL":
            node = edge.get("node") or {}
            if node.get("id"):
                prequel_anilist_ids.append(str(node["id"]))
    if not prequel_anilist_ids:
        return None
    # Check if any of those prequel anilist_ids belong to a tracked show.
    placeholders = ",".join("?" for _ in prequel_anilist_ids)
    parent_row = conn.execute(
        "SELECT sei.show_id AS parent_id,"
        "       s.title_english, s.title_romaji, s.primary_title"
        " FROM show_external_id sei"
        " JOIN show s ON s.id = sei.show_id"
        f" WHERE sei.service = 'anilist' AND sei.external_id IN ({placeholders})"
        "   AND s.tracked = 1"
        " LIMIT 1",
        prequel_anilist_ids,
    ).fetchone()
    if parent_row is None:
        return None
    # stub_show_id is None — no local show for this anilist_id yet.
    return _build_sequel_result(conn, parent_row, stub_show_id=None)


def _build_sequel_result(
    conn, parent_row, stub_show_id: str | None, *, next_season: int | None = None,
) -> dict:
    """Common result builder for all sequel-detection tiers."""
    parent_id = parent_row["parent_id"]
    primary = parent_row["primary_title"] or "romaji"
    parent_title = parent_row[f"title_{primary}"] or parent_row["title_romaji"] or parent_id

    if next_season is None:
        max_row = conn.execute(
            "SELECT MAX(season_number) AS mx FROM season"
            " WHERE show_id = ? AND season_number > 0",
            (parent_id,),
        ).fetchone()
        next_season = (max_row["mx"] or 1) + 1

    return {
        "parent_show_id": parent_id,
        "parent_title": parent_title,
        "next_season": next_season,
        "stub_show_id": stub_show_id,
    }


def find_existing_show(conn, input: dict) -> str | None:
    """Live check against `show_external_id` for any identity `input`
    carries (anilist/tvdb/tmdb/imdb/mal) — B.11d follow-up, real bug
    found in the live backfill run: `create_show` used to insert
    unconditionally, so two independent callers targeting the same
    external id within one run (a relation-stub auto-created by one
    show's own metadata fetch, `metadata._create_relation_stub`, and a
    separately-classified candidate for that same id later in the same
    loop) each got their own `show` row — 86 real anilist_id collision
    pairs confirmed live (`s-7zvsg2`/`s-e1a4yk`, both anilist 108992,
    "Mebius Dust"). show_backfill.py's own dedup (`known_anilist_ids`/
    `_sonarr_resolvable_anilist_ids`) is a snapshot taken once before
    its loop starts, so it can't see writes the loop itself makes — a
    read-side cache can't be the correctness guarantee; the write
    boundary has to be. Public (not `_`-prefixed): show_backfill.py
    calls it too, to tell a genuine new row apart from a promoted one
    for its own result reporting."""
    for service, key in (
        ("anilist", "anilist_id"),
        ("tvdb", "tvdb_id"),
        ("tmdb", "tmdb_id"),
        ("imdb", "imdb_id"),
        ("mal", "mal_id"),
    ):
        value = input.get(key)
        if value is None:
            continue
        row = conn.execute(
            "SELECT show_id FROM show_external_id WHERE service = ? AND external_id = ?",
            (service, str(value)),
        ).fetchone()
        if row is not None:
            return row["show_id"]
    return None


def _promote_stub(conn, show_id: str, input: dict) -> str:
    """SCOPE.md §5.1's own "Show-row promotion paths": a bare
    tracked = false relation/franchise stub becomes a real tracked show
    by flipping tracked = true on the *existing* row — no re-creation,
    identity/relation data carries over. Only reached when
    `find_existing_show` above matched a stub (`create_show` raises
    instead for a match against an already-tracked show — see there).

    Deliberately does NOT overwrite the stub's own title/media_shape/
    tracking_space — that data came from AniList itself at stub-creation
    time (`metadata._create_relation_stub`), at least as trustworthy as
    whatever this caller supplied; only *adds* external-id links `input`
    carries that the stub doesn't already have (a tvdb_id a Sonarr-
    sourced backfill candidate carries that a bare AniList-relation stub
    never would, say), then re-runs the same on-demand metadata fetch
    the stub's own creation never got — `fetch_and_populate`'s own
    docstring already calls this safe "any time after (retry/
    refresh)"."""
    now = util.now_utc_iso()
    conn.execute("UPDATE show SET tracked = 1, updated_at = ? WHERE id = ?", (now, show_id))
    existing_services = {
        row["service"]
        for row in conn.execute(
            "SELECT service FROM show_external_id WHERE show_id = ?", (show_id,)
        ).fetchall()
    }
    for service, key in (
        ("anilist", "anilist_id"),
        ("tvdb", "tvdb_id"),
        ("imdb", "imdb_id"),
        ("mal", "mal_id"),
    ):
        value = input.get(key)
        if value is not None and service not in existing_services:
            url = _EXTERNAL_ID_URL_TEMPLATES[service].format(id=value)
            conn.execute(
                "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (show_id, service, str(value), url, now),
            )
    tmdb_id = input.get("tmdb_id")
    if tmdb_id is not None and "tmdb" not in existing_services:
        url = _TMDB_URL_TEMPLATES[input["media_shape"]].format(id=tmdb_id)
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'tmdb', ?, ?, ?)",
            (show_id, str(tmdb_id), url, now),
        )
    conn.commit()
    # events.py, 2026-08-25 — "genuinely new to the client" from the
    # outside the moment `tracked` flips to 1, same as create_show's own
    # brand-new-row case below; published after this commit (unlike
    # availability.py's own narrower race, there's nothing left in this
    # function that could still fail and roll this UPDATE back).
    events.publish("show_created", show_id)
    metadata.fetch_and_populate(conn, show_id)
    conn.commit()
    return show_id


def create_show(conn, input: dict) -> str:
    """§5.1, A.4/A.8 — inserts the `show` row, its external-id links,
    then runs the on-demand metadata fetch (A.8: episodes, AniList
    link resolution, Fribb season reconciliation — best-effort, never
    raises, see metadata.py's own docstring) inline, exactly as
    `addShow` always has. Returns the new show's id; callers needing
    the full row do their own lookup (resolvers.py's own `_get_show`,
    or a plain `SELECT * FROM show WHERE id = ?` — show_backfill.py
    doesn't need the full row at all, just the id, so this doesn't
    return one).

    **Checks `find_existing_show` first, B.11d follow-up** — a match
    against an untracked stub promotes it in place (`_promote_stub`,
    §5.1's own documented path, see there); a match against an
    already-tracked show is a genuine duplicate-add attempt, rejected
    with `ShowInputError` rather than silently creating a second row
    for the same real-world show."""
    primary = input["primary_title"]
    title_field = f"title_{primary}"
    if not input.get(title_field):
        raise ShowInputError(
            f"primaryTitle is {primary.upper()} but {title_field.replace('_', ' ', 1)}"
            " (as camelCase) was not provided"
        )

    existing_show_id = find_existing_show(conn, input)
    if existing_show_id is not None:
        existing = conn.execute(
            "SELECT tracked FROM show WHERE id = ?", (existing_show_id,)
        ).fetchone()
        if existing is None:
            # Orphaned show_external_id row — show was deleted but the
            # external ID link survived.  Treat as no match.
            existing_show_id = None
    if existing_show_id is not None:
        # Before rejecting a tracked duplicate or promoting a stub,
        # check whether it's a sequel of a tracked show — the right
        # action is "add as Season N on the parent", regardless of
        # whether this show is tracked or not.  SequelDetectedError
        # carries the parent info so the client can prompt.
        sequel = find_sequel_parent(
            conn, input.get("anilist_id"),
            tvdb_id=input.get("tvdb_id"), tmdb_id=input.get("tmdb_id"),
            imdb_id=input.get("imdb_id"), mal_id=input.get("mal_id"),
        )
        if sequel is not None:
            raise SequelDetectedError(
                sequel["parent_show_id"],
                sequel["parent_title"],
                sequel["next_season"],
                sequel_anilist_id=input.get("anilist_id"),
                sequel_mal_id=input.get("mal_id"),
            )
        if existing["tracked"]:
            raise ShowInputError(
                "a show already exists for one of these external ids and is already"
                f" tracked (show {existing_show_id}) — refusing to create a duplicate"
            )
        return _promote_stub(conn, existing_show_id, input)

    # No local show at all — still check relations for a sequel.
    sequel = find_sequel_parent(
        conn, input.get("anilist_id"),
        tvdb_id=input.get("tvdb_id"), tmdb_id=input.get("tmdb_id"),
        imdb_id=input.get("imdb_id"), mal_id=input.get("mal_id"),
    )
    if sequel is not None:
        raise SequelDetectedError(
            sequel["parent_show_id"],
            sequel["parent_title"],
            sequel["next_season"],
            sequel_anilist_id=input.get("anilist_id"),
            sequel_mal_id=input.get("mal_id"),
        )

    _check_later_season_pre_add(conn, input.get("anilist_id"))

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
    events.publish("show_created", show_id)  # events.py, 2026-08-25 — see _promote_stub's own note

    # A.8 — the on-demand metadata fetch itself, best-effort (see
    # metadata.py's own docstring: never raises, failures go to
    # pending_review instead). A separate commit rather than folding
    # into the block above: the bare show already exists and is
    # queryable even if every branch of the fetch below fails outright.
    metadata.fetch_and_populate(conn, show_id)
    conn.commit()

    return show_id


def skip_show(conn, input: dict) -> str:
    """Create a minimal stub with status='skipped', tracked=0 — the browse
    "skip" flow's write path.  No metadata fetch, no Sonarr/Radarr add,
    no AniList/MAL push.

    If an existing show row already matches one of the input's external ids:
    - tracked=1 → no-op, return the existing show_id (use setStatus instead)
    - tracked=0, status!='skipped' → flip status to 'skipped', return id
    - tracked=0, status='skipped' → already skipped, return id

    Otherwise creates a new row with status='skipped', tracked=0, and
    inserts external-id links (same shape as create_show, minus the
    metadata fetch)."""
    primary = input["primary_title"]
    title_field = f"title_{primary}"
    if not input.get(title_field):
        raise ShowInputError(
            f"primaryTitle is {primary.upper()} but {title_field.replace('_', ' ', 1)}"
            " (as camelCase) was not provided"
        )

    existing_show_id = find_existing_show(conn, input)
    if existing_show_id is not None:
        existing = conn.execute(
            "SELECT tracked, status FROM show WHERE id = ?", (existing_show_id,)
        ).fetchone()
        if existing is None:
            existing_show_id = None
    if existing_show_id is not None:
        if existing["tracked"]:
            return existing_show_id  # already tracked — no-op
        if existing["status"] != "skipped":
            now = util.now_utc_iso()
            conn.execute(
                "UPDATE show SET status = 'skipped', updated_at = ? WHERE id = ?",
                (now, existing_show_id),
            )
            conn.commit()
        return existing_show_id

    show_id = ids.generate_id(conn, "s")
    now = util.now_utc_iso()
    conn.execute(
        """
        INSERT INTO show (
            id, media_shape, tracking_space,
            title_romaji, title_english, title_native, primary_title,
            status, tracked, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'skipped', 0, ?, ?)
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
    return show_id


# --- B.21 — addShowWithArr: search-Sonarr/Radarr-then-track-in-LCARS -------
#
# One combined mutation (user's own explicit call), not separate composable
# steps — a client gets one round trip for "add this show." LCARS-configured
# root folder/quality profile defaults (config.py), not caller-supplied.
#
# The one real outbound write here (add_series/add_movie) only ever happens
# after every local check that could reject the add has already run,
# including a *second* find_existing_show() pass once the real tvdb/tmdb id
# is known — a title-only add's resolved id could belong to a different,
# already-tracked LCARS show than whatever the caller's own original
# identity fields matched (or matched nothing at all). This is what makes
# create_show_with_arr_add() deliberately not best-effort/pending_review-on-
# failure the way every AniList push in resolvers.py is: creating an
# orphan/duplicate series in Sonarr on a failed local write is a real,
# annoying-to-clean-up failure mode, worse than a blocked add.


def _lookup_arr(conn, media_shape: str, term: str) -> list[dict]:
    """Read-only Sonarr/Radarr search — the actual HTTP-calling core
    both `_resolve_arr_candidate` (below, single-result, write-path)
    and `search_arr_candidates` (2026-08-18, the picker feature's own
    read-only query, full list) build on, so the config-check/
    error-handling/service_health bookkeeping exists in exactly one
    place rather than twice. Returns `[]` when the relevant service
    isn't configured for this media_shape at all — not an error, same
    "not configured = same as not linked" every other Sonarr/Radarr
    touchpoint in this codebase already has. Raises `ShowInputError` on
    a genuine service failure (unreachable, bad api key) — a real,
    caller-visible problem, distinct from "searched fine, found
    nothing" (an empty list, not an error)."""
    cfg = config.get_current()
    if media_shape == "episodic":
        if not (cfg.sonarr_url and cfg.sonarr_api_key):
            return []
        try:
            with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
                results = client.lookup_series(term)
            service_health.record_success(conn, "sonarr")
        except sonarr_client.SonarrError as e:
            service_health.record_failure(conn, "sonarr", str(e))
            raise ShowInputError(f"couldn't search Sonarr for {term!r}: {e}") from e
        return results
    if media_shape == "movie":
        if not (cfg.radarr_url and cfg.radarr_api_key):
            return []
        try:
            with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
                results = client.lookup_movie(term)
            service_health.record_success(conn, "radarr")
        except radarr_client.RadarrError as e:
            service_health.record_failure(conn, "radarr", str(e))
            raise ShowInputError(f"couldn't search Radarr for {term!r}: {e}") from e
        return results
    return []


def _resolve_arr_candidate(conn, input: dict) -> tuple[dict | None, dict]:
    """Read-only: resolves a Sonarr/Radarr lookup candidate for `input`,
    writes nothing anywhere. Returns (candidate, arr_result) —
    arr_result is the partial AddShowResult dict the whole flow
    eventually returns (sonarr_created/radarr_created default False, no
    matched_* yet). `candidate` is None when the relevant service isn't
    configured for this show's media_shape — not an error, same
    "not configured = same as not linked" treatment every other
    Sonarr/Radarr touchpoint in this codebase already has. An exact-id
    term (already-known tvdb_id/tmdb_id) or a title-only free-text
    search that finds zero results *does* raise — a genuine "couldn't
    find this show" is worth surfacing directly rather than silently
    falling through to a plain, unlinked local add."""
    arr_result = {
        "sonarr_created": False,
        "radarr_created": False,
        "matched_title": None,
        "matched_tvdb_id": None,
        "matched_tmdb_id": None,
    }
    cfg = config.get_current()
    if input["media_shape"] == "episodic":
        if not (cfg.sonarr_url and cfg.sonarr_api_key):
            return None, arr_result
        term = (
            f"tvdb:{input['tvdb_id']}"
            if input.get("tvdb_id")
            else input[f"title_{input['primary_title']}"]
        )
        results = _lookup_arr(conn, "episodic", term)
        if not results:
            raise ShowInputError(f"no Sonarr match found for {term!r}")
        return results[0], arr_result
    if input["media_shape"] == "movie":
        if not (cfg.radarr_url and cfg.radarr_api_key):
            return None, arr_result
        term = (
            f"tmdb:{input['tmdb_id']}"
            if input.get("tmdb_id")
            else input[f"title_{input['primary_title']}"]
        )
        results = _lookup_arr(conn, "movie", term)
        if not results:
            raise ShowInputError(f"no Radarr match found for {term!r}")
        return results[0], arr_result
    return None, arr_result


def search_arr_candidates(conn, media_shape: str, title: str) -> list[dict]:
    """2026-08-18 — read-only Sonarr/Radarr search for Data's own `A`
    disambiguation picker, called *before* `addShowWithArr` rather than
    from inside it: the user picks a candidate here, then that exact
    candidate's own `tvdbId`/`tmdbId` gets passed back into
    `addShowWithArr`'s `AddShowWithArrInput` to sidestep re-searching
    entirely — already supported ("supply an exact tvdbId/tmdbId when
    it's already known to sidestep this", addShowWithArr's own schema
    docstring), so this needed no changes to the write path at all.
    Returns the raw candidate list, genuinely empty (not an error) when
    the search itself found nothing or the relevant service isn't
    configured — see `_lookup_arr`'s own docstring."""
    return _lookup_arr(conn, media_shape, title)


def _ensure_in_arr(conn, input: dict, candidate: dict) -> dict:
    """Given a resolved lookup candidate, checks whether it's already
    in Sonarr's/Radarr's own library (the existing read methods, not
    the lookup response's own id field) and adds it if not — the one
    real outbound write in this whole flow, only ever reached after
    create_show_with_arr_add()'s own local re-validation has passed.
    Returns a dict merged into the caller's arr_result, plus a
    tvdb_id/tmdb_id key the caller feeds into the local show's own
    external-id link (so create_show()'s own inline metadata fetch
    finds the show already present in Sonarr's/Radarr's library on its
    very first pass, no manual refreshShowMetadata retry needed)."""
    cfg = config.get_current()
    if input["media_shape"] == "episodic":
        try:
            with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
                existing = client.series_by_tvdb_id(candidate["tvdbId"])
                if existing is not None:
                    service_health.record_success(conn, "sonarr")
                    return {
                        "matched_title": existing.get("title"),
                        "matched_tvdb_id": existing.get("tvdbId"),
                        "tvdb_id": existing["tvdbId"],
                        "title_slug": existing.get("titleSlug"),
                    }
                is_anime = input["tracking_space"] == "anime"
                root_folder = (
                    cfg.sonarr_anime_root_folder if is_anime else cfg.sonarr_tv_root_folder
                )
                quality_profile_id = (
                    cfg.sonarr_anime_quality_profile_id
                    if is_anime
                    else cfg.sonarr_tv_quality_profile_id
                )
                if not (root_folder and quality_profile_id):
                    raise ShowInputError(
                        "Sonarr add defaults aren't fully configured for this show's"
                        " tracking space — see config.py's sonarr_anime_*/sonarr_tv_*"
                        " fields"
                    )
                payload = {
                    "title": candidate["title"],
                    "tvdbId": candidate["tvdbId"],
                    "qualityProfileId": quality_profile_id,
                    "titleSlug": candidate.get("titleSlug"),
                    "images": candidate.get("images", []),
                    "seasons": candidate.get("seasons", []),
                    "rootFolderPath": root_folder,
                    # unmonitored=true (PAUSED status) → don't monitor or auto-search;
                    # the arr instance stays hands-off until the user resumes.
                    "monitored": not input.get("unmonitored", False),
                    "seasonFolder": True,
                    "addOptions": {
                        "monitor": "future",
                        "searchForMissingEpisodes": not input.get("unmonitored", False),
                    },
                }
                created = client.add_series(payload)
            service_health.record_success(conn, "sonarr")
        except sonarr_client.SonarrError as e:
            service_health.record_failure(conn, "sonarr", str(e))
            raise ShowInputError(f"couldn't add to Sonarr: {e}") from e
        return {
            "sonarr_created": True,
            "matched_title": created.get("title"),
            "matched_tvdb_id": created.get("tvdbId"),
            "tvdb_id": created["tvdbId"],
            "title_slug": created.get("titleSlug"),
        }
    # movie -> Radarr
    try:
        with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
            existing = client.movie_by_tmdb_id(candidate["tmdbId"])
            if existing is not None:
                service_health.record_success(conn, "radarr")
                return {
                    "matched_title": existing.get("title"),
                    "matched_tmdb_id": existing.get("tmdbId"),
                    "tmdb_id": existing["tmdbId"],
                    "title_slug": existing.get("titleSlug"),
                }
            if not (cfg.radarr_root_folder and cfg.radarr_quality_profile_id):
                raise ShowInputError(
                    "Radarr add defaults aren't fully configured — see config.py's"
                    " radarr_root_folder/radarr_quality_profile_id"
                )
            payload = {
                "title": candidate["title"],
                "tmdbId": candidate["tmdbId"],
                "qualityProfileId": cfg.radarr_quality_profile_id,
                "titleSlug": candidate.get("titleSlug"),
                "images": candidate.get("images", []),
                "rootFolderPath": cfg.radarr_root_folder,
                "monitored": not input.get("unmonitored", False),
                "minimumAvailability": candidate.get("minimumAvailability") or "released",
                "addOptions": {
                    "searchForMovie": not input.get("unmonitored", False),
                },
            }
            created = client.add_movie(payload)
        service_health.record_success(conn, "radarr")
    except radarr_client.RadarrError as e:
        service_health.record_failure(conn, "radarr", str(e))
        raise ShowInputError(f"couldn't add to Radarr: {e}") from e
    return {
        "radarr_created": True,
        "matched_title": created.get("title"),
        "matched_tmdb_id": created.get("tmdbId"),
        "tmdb_id": created["tmdbId"],
        "title_slug": created.get("titleSlug"),
    }


def ensure_arr_monitored(conn, show_id: str) -> dict | None:
    """Ensure a show's Sonarr/Radarr entry exists and is monitored.

    Used by the sequel-attach flow: when a new season is added to an
    existing show, the arr instance must be monitoring that series so it
    actually grabs future episodes.

    Returns a dict describing what happened, or None if Sonarr/Radarr
    isn't configured for this show's media shape. Raises ShowInputError
    on genuine service failures (same as _ensure_in_arr)."""
    show = conn.execute(
        "SELECT media_shape, tracking_space FROM show WHERE id = ?",
        (show_id,),
    ).fetchone()
    if show is None:
        return None

    cfg = config.get_current()
    media_shape = show["media_shape"]
    tracking_space = show["tracking_space"]

    if media_shape == "episodic":
        if not (cfg.sonarr_url and cfg.sonarr_api_key):
            return None
        tvdb_row = conn.execute(
            "SELECT external_id FROM show_external_id"
            " WHERE show_id = ? AND service = 'tvdb'",
            (show_id,),
        ).fetchone()
        if tvdb_row is None:
            return None
        tvdb_id = int(tvdb_row["external_id"])
        try:
            with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
                existing = client.series_by_tvdb_id(tvdb_id)
                if existing is not None:
                    # Already in Sonarr — ensure it's monitored.
                    if not existing.get("monitored", True):
                        existing["monitored"] = True
                        client.update_series(existing)
                    service_health.record_success(conn, "sonarr")
                    return {"action": "already_in_sonarr", "monitored": True}
                # Not in Sonarr — look up and add it.
                results = client.lookup_series(f"tvdb:{tvdb_id}")
                if not results:
                    return {"action": "not_found_in_sonarr"}
                candidate = results[0]
                is_anime = tracking_space == "anime"
                root_folder = (
                    cfg.sonarr_anime_root_folder if is_anime else cfg.sonarr_tv_root_folder
                )
                quality_profile_id = (
                    cfg.sonarr_anime_quality_profile_id
                    if is_anime
                    else cfg.sonarr_tv_quality_profile_id
                )
                if not (root_folder and quality_profile_id):
                    return {"action": "sonarr_not_configured"}
                payload = {
                    "title": candidate["title"],
                    "tvdbId": candidate["tvdbId"],
                    "qualityProfileId": quality_profile_id,
                    "titleSlug": candidate.get("titleSlug"),
                    "images": candidate.get("images", []),
                    "seasons": candidate.get("seasons", []),
                    "rootFolderPath": root_folder,
                    "monitored": True,
                    "seasonFolder": True,
                    "addOptions": {
                        "monitor": "future",
                        "searchForMissingEpisodes": True,
                    },
                }
                created = client.add_series(payload)
                title_slug = created.get("titleSlug")
                if title_slug:
                    write_arr_external_id(conn, show_id, media_shape, title_slug)
            service_health.record_success(conn, "sonarr")
            return {"action": "added_to_sonarr", "monitored": True}
        except sonarr_client.SonarrError as e:
            service_health.record_failure(conn, "sonarr", str(e))
            raise ShowInputError(f"Sonarr monitoring failed: {e}") from e

    else:
        # Movie → Radarr
        if not (cfg.radarr_url and cfg.radarr_api_key):
            return None
        tmdb_row = conn.execute(
            "SELECT external_id FROM show_external_id"
            " WHERE show_id = ? AND service = 'tmdb'",
            (show_id,),
        ).fetchone()
        if tmdb_row is None:
            return None
        tmdb_id = int(tmdb_row["external_id"])
        try:
            with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
                existing = client.movie_by_tmdb_id(tmdb_id)
                if existing is not None:
                    if not existing.get("monitored", True):
                        existing["monitored"] = True
                        client.update_movie(existing)
                    service_health.record_success(conn, "radarr")
                    return {"action": "already_in_radarr", "monitored": True}
                results = client.lookup_movie(f"tmdb:{tmdb_id}")
                if not results:
                    return {"action": "not_found_in_radarr"}
                candidate = results[0]
                if not (cfg.radarr_root_folder and cfg.radarr_quality_profile_id):
                    return {"action": "radarr_not_configured"}
                payload = {
                    "title": candidate["title"],
                    "tmdbId": candidate["tmdbId"],
                    "qualityProfileId": cfg.radarr_quality_profile_id,
                    "titleSlug": candidate.get("titleSlug"),
                    "images": candidate.get("images", []),
                    "rootFolderPath": cfg.radarr_root_folder,
                    "monitored": True,
                    "minimumAvailability": candidate.get("minimumAvailability") or "released",
                    "addOptions": {"searchForMovie": True},
                }
                created = client.add_movie(payload)
                title_slug = created.get("titleSlug")
                if title_slug:
                    write_arr_external_id(conn, show_id, media_shape, title_slug)
            service_health.record_success(conn, "radarr")
            return {"action": "added_to_radarr", "monitored": True}
        except radarr_client.RadarrError as e:
            service_health.record_failure(conn, "radarr", str(e))
            raise ShowInputError(f"Radarr monitoring failed: {e}") from e


def write_arr_external_id(conn, show_id: str, media_shape: str, title_slug: str) -> None:
    """2026-08-18 — a real deep link into the *local* Sonarr/Radarr web
    UI (their own `/series/{slug}`/`/movie/{slug}` route) as a
    `show_external_id` row, same shape every tvdb/anilist/imdb/mal/tmdb
    link already gets (§5.4) — a client asking LCARS "where's this
    show in Sonarr" gets a real answer instead of guessing a URL
    itself. `sonarr`/`radarr` were never one of `_EXTERNAL_ID_URL_
    TEMPLATES`'s static id->URL templates above on purpose: unlike
    those, this needs the operator's own `sonarr_url`/`radarr_url`
    (config.py) plus a real titleSlug from Sonarr/Radarr's own response,
    not a well-known public template.

    Public (no leading underscore) since 2026-08-18: no longer only
    `addShowWithArr`'s own write paths (existing-in-Sonarr/Radarr,
    newly-created) call this — `service_presence.py`'s B.7 catalog
    sweep (`_refresh_sonarr_presence`/`_refresh_radarr_presence`) now
    calls it too, for every already-tracked show its own fuzzy match
    against the live Sonarr/Radarr catalog turns up a titleSlug for.
    That backfills the ~1600 shows `addShowWithArr` never touched
    (added via the older `addShow`, or tracked from before this existed)
    on the sweep's own monthly cadence — real coverage of the existing
    library, not just new adds going forward. A show neither path ever
    matches still has no titleSlug to link from — a real, known
    limitation, not silently incomplete: `Show.externalIds` simply
    won't carry a sonarr/radarr entry for such a show, same as any
    other unlinked external id.

    `INSERT OR IGNORE`, not upsert — same as every other external-id
    insert in this module; once a real link exists for a show+service
    there's nothing to update (the URL is a stable function of
    title_slug, which Sonarr/Radarr don't change under an existing
    entry).

    Uses `sonarr_public_url`/`radarr_public_url` (config.py), NOT
    `sonarr_url`/`radarr_url` — a real bug caught live 2026-08-18: the
    latter is LCARS's own outbound-API address (this deployment's own
    docker-network hostname, unreachable from any browser outside that
    network), and this URL is handed straight to a client's browser
    (Data's `o`). Not configured means no link is written at all — same
    "not configured = same as not linked" every other optional
    integration here already gets, not a guessed fallback."""
    cfg = config.get_current()
    if media_shape == "episodic":
        base_url, service, path = cfg.sonarr_public_url, "sonarr", "series"
    else:
        base_url, service, path = cfg.radarr_public_url, "radarr", "movie"
    if not base_url:
        return
    url = f"{base_url.rstrip('/')}/{path}/{title_slug}"
    conn.execute(
        "INSERT OR IGNORE INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (show_id, service, title_slug, url, util.now_utc_iso()),
    )
    conn.commit()


def write_tvdb_id(conn, show_id: str, tvdb_id: int) -> bool:
    """2026-08-18, tvdb_backfill.py — a plain `tvdb` show_external_id
    link, same `_EXTERNAL_ID_URL_TEMPLATES["tvdb"]` template
    `_promote_stub`/`create_show` already write inline, pulled out here
    so a third caller (the Fribb reverse-lookup backfill) doesn't need
    its own copy of the URL format. Returns whether a row was actually
    inserted (`INSERT OR IGNORE`'s own rowcount) — same "count real
    changes" convention every Phase B poller uses, since a show already
    linked (however that happened) is a silent no-op here, not
    something to recount."""
    url = _EXTERNAL_ID_URL_TEMPLATES["tvdb"].format(id=tvdb_id)
    cur = conn.execute(
        "INSERT OR IGNORE INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'tvdb', ?, ?, ?)",
        (show_id, str(tvdb_id), url, util.now_utc_iso()),
    )
    return cur.rowcount > 0


def amend_show_arr_link(
    conn,
    show_id: str,
    service: str,
    new_external_id: str,
    *,
    delete_files: bool = False,
) -> dict:
    """Correct a wrong TVDB/TMDB external ID on a show: validate the new
    ID against Sonarr/Radarr's lookup, delete the old series/movie from
    the arr, add the correct one, and update the LCARS external-ID link.

    Ordering is deliberate (advisor-reviewed):
      1. Validate new ID (read-only lookup — catches typos before anything
         is destroyed).
      2. Delete old arr entry (if one exists for the old ID).
      3. Add correct arr entry (reuses _ensure_in_arr's quality-profile /
         root-folder logic).
      4. Update LCARS show_external_id + write arr deep link.

    Each step's error reports what state things are in — "deleted from
    Sonarr but re-add failed" is a very different mess than "nothing
    happened".

    Known limitation: existing episode rows still carry
    sonarr_season/sonarr_episode captured from the *wrong* series.
    The next metadata refresh (refreshShowMetadata) will re-match
    against the new Sonarr series. A manual refresh after amend is
    recommended."""
    cfg = config.get_current()

    if service not in ("tvdb", "tmdb"):
        raise ShowInputError(
            f"amendShowArrLink only supports 'tvdb' (Sonarr) and 'tmdb' (Radarr), got '{service}'"
        )

    show = conn.execute(
        "SELECT id, media_shape, tracking_space FROM show WHERE id = ?",
        (show_id,),
    ).fetchone()
    if show is None:
        raise ShowInputError(f"show {show_id} not found")

    # The arr is a function of media_shape, not of the service badge:
    # episodic → Sonarr (keyed by tvdb); movie → Radarr (keyed by tmdb).
    if show["media_shape"] == "episodic" and service != "tvdb":
        raise ShowInputError(
            "episodic shows use Sonarr (tvdb) — amend the tvdb badge, not tmdb"
        )
    if show["media_shape"] == "movie" and service != "tmdb":
        raise ShowInputError(
            "movies use Radarr (tmdb) — amend the tmdb badge, not tvdb"
        )

    # Read old external ID (may be None if not yet linked)
    old_row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = ?",
        (show_id, service),
    ).fetchone()
    old_external_id = old_row["external_id"] if old_row else None

    result = {
        "success": False,
        "old_external_id": old_external_id,
        "new_external_id": new_external_id,
        "resolved_title": None,
        "arr_deleted": False,
        "arr_added": False,
        "message": "",
    }

    # ── Step 1: Validate new ID via lookup (read-only) ──────────────
    if service == "tvdb":
        try:
            with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
                candidates = client.lookup_series(f"tvdb:{new_external_id}")
            service_health.record_success(conn, "sonarr")
        except sonarr_client.SonarrError as e:
            service_health.record_failure(conn, "sonarr", str(e))
            result["message"] = f"Sonarr lookup failed: {e}"
            return result
        if not candidates:
            result["message"] = (
                f"TVDB ID {new_external_id} resolved to nothing in Sonarr — "
                "check the ID is correct"
            )
            return result
        candidate = candidates[0]
        result["resolved_title"] = candidate.get("title")
    else:  # tmdb
        try:
            with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
                candidates = client.lookup_movie(f"tmdb:{new_external_id}")
            service_health.record_success(conn, "radarr")
        except radarr_client.RadarrError as e:
            service_health.record_failure(conn, "radarr", str(e))
            result["message"] = f"Radarr lookup failed: {e}"
            return result
        if not candidates:
            result["message"] = (
                f"TMDB ID {new_external_id} resolved to nothing in Radarr — "
                "check the ID is correct"
            )
            return result
        candidate = candidates[0]
        result["resolved_title"] = candidate.get("title")

    # ── Step 2: Delete old arr entry (if present) ───────────────────
    if service == "tvdb" and old_external_id:
        try:
            old_id_int = int(old_external_id)
        except (ValueError, TypeError):
            old_id_int = None
        if old_id_int is not None:
            try:
                with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
                    existing = client.series_by_tvdb_id(old_id_int)
                    if existing is not None:
                        client.delete_series(existing["id"], delete_files=delete_files)
                        result["arr_deleted"] = True
                service_health.record_success(conn, "sonarr")
            except sonarr_client.SonarrError as e:
                service_health.record_failure(conn, "sonarr", str(e))
                result["message"] = (
                    f"Validated new ID (resolves to '{result['resolved_title']}') "
                    f"but failed to delete old Sonarr entry: {e}"
                )
                return result
    elif service == "tmdb" and old_external_id:
        try:
            old_id_int = int(old_external_id)
        except (ValueError, TypeError):
            old_id_int = None
        if old_id_int is not None:
            try:
                with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
                    existing = client.movie_by_tmdb_id(old_id_int)
                    if existing is not None:
                        client.delete_movie(existing["id"], delete_files=delete_files)
                        result["arr_deleted"] = True
                service_health.record_success(conn, "radarr")
            except radarr_client.RadarrError as e:
                service_health.record_failure(conn, "radarr", str(e))
                result["message"] = (
                    f"Validated new ID (resolves to '{result['resolved_title']}') "
                    f"but failed to delete old Radarr entry: {e}"
                )
                return result

    # ── Step 3: Add correct arr entry ───────────────────────────────
    # Build the same input shape _ensure_in_arr expects
    ensure_input = {
        "media_shape": show["media_shape"],
        "tracking_space": show["tracking_space"],
    }
    try:
        ensure_result = _ensure_in_arr(conn, ensure_input, candidate)
        result["arr_added"] = True
    except ShowInputError as e:
        if result["arr_deleted"]:
            result["message"] = (
                f"Old arr entry was deleted, but failed to add the correct one: {e}."
                " The LCARS external ID has NOT been updated yet."
            )
        else:
            result["message"] = (
                f"Failed to add the correct arr entry: {e}."
                " Nothing was changed."
            )
        return result

    # ── Step 4: Update LCARS external ID ────────────────────────────
    now = util.now_utc_iso()
    if service == "tvdb":
        url = _EXTERNAL_ID_URL_TEMPLATES["tvdb"].format(id=new_external_id)
    else:
        url = _TMDB_URL_TEMPLATES[show["media_shape"]].format(id=new_external_id)

    if old_row:
        conn.execute(
            "UPDATE show_external_id SET external_id = ?, url = ?"
            " WHERE show_id = ? AND service = ?",
            (str(new_external_id), url, show_id, service),
        )
    else:
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (show_id, service, str(new_external_id), url, now),
        )

    # Write arr deep link (sonarr/radarr service presence)
    title_slug = ensure_result.get("title_slug")
    if title_slug:
        # Remove old sonarr/radarr link if it existed
        arr_service = "sonarr" if service == "tvdb" else "radarr"
        conn.execute(
            "DELETE FROM show_external_id WHERE show_id = ? AND service = ?",
            (show_id, arr_service),
        )
        write_arr_external_id(conn, show_id, show["media_shape"], title_slug)

    conn.commit()

    # 2026-09-21 (user request): re-fetch metadata against the now-correct
    # link right away, rather than leaving the documented "episode rows
    # still carry sonarr_season/sonarr_episode from the wrong series until
    # a manual refresh" gap for the caller to remember. Same function
    # refreshShowMetadata calls, best-effort/never-raises (metadata.py's
    # own docstring) — a failure here shouldn't make an otherwise-successful
    # amend look like it failed.
    metadata.fetch_and_populate(conn, show_id)

    result["success"] = True
    parts = []
    if result["resolved_title"]:
        parts.append(f"Resolved to '{result['resolved_title']}'")
    if result["arr_deleted"]:
        parts.append("deleted old arr entry")
    if result["arr_added"]:
        parts.append("added correct one")
    parts.append(f"{service} ID updated to {new_external_id}")
    parts.append("metadata refreshed")
    result["message"] = "; ".join(parts) + "."
    return result


def create_show_with_arr_add(conn, input: dict) -> tuple[str, dict]:
    """B.21 — addShowWithArr's own backing logic. Returns (show_id,
    arr_result); arr_result carries sonarr_created/radarr_created/
    matched_title/matched_tvdb_id/matched_tmdb_id for the resolver to
    build AddShowResult from. See this module's own B.21 section
    docstring above for the full ordering rationale."""
    primary = input["primary_title"]
    title_field = f"title_{primary}"
    if not input.get(title_field):
        raise ShowInputError(
            f"primaryTitle is {primary.upper()} but {title_field.replace('_', ' ', 1)}"
            " (as camelCase) was not provided"
        )

    existing_show_id = find_existing_show(conn, input)
    if existing_show_id is not None:
        existing = conn.execute(
            "SELECT tracked FROM show WHERE id = ?", (existing_show_id,)
        ).fetchone()
        if existing is None:
            existing_show_id = None
    if existing_show_id is not None:
        if existing["tracked"]:
            # Before refusing, check if this is a sequel — offer
            # season-attach instead of a duplicate error.
            sequel = find_sequel_parent(
                conn, input.get("anilist_id"),
                tvdb_id=input.get("tvdb_id"), tmdb_id=input.get("tmdb_id"),
                imdb_id=input.get("imdb_id"), mal_id=input.get("mal_id"),
            )
            if sequel is not None:
                raise SequelDetectedError(
                    sequel["parent_show_id"],
                    sequel["parent_title"],
                    sequel["next_season"],
                    sequel_anilist_id=input.get("anilist_id"),
                    sequel_mal_id=input.get("mal_id"),
                )
            raise ShowInputError(
                "a show already exists for one of these external ids and is already"
                f" tracked (show {existing_show_id}) — refusing to create a duplicate"
            )

    candidate, arr_result = _resolve_arr_candidate(conn, input)
    resolved_input = dict(input)
    title_slug = None
    if candidate is not None:
        resolved_input["tvdb_id" if input["media_shape"] == "episodic" else "tmdb_id"] = (
            candidate["tvdbId"] if input["media_shape"] == "episodic" else candidate["tmdbId"]
        )
        # Re-validate against the newly-resolved id BEFORE writing anything to
        # Sonarr/Radarr — see this module's own B.21 section docstring.
        recheck_id = find_existing_show(conn, resolved_input)
        if recheck_id is not None and recheck_id != existing_show_id:
            recheck = conn.execute(
                "SELECT tracked FROM show WHERE id = ?", (recheck_id,)
            ).fetchone()
            if recheck["tracked"]:
                raise ShowInputError(
                    "the resolved Sonarr/Radarr entry's own id already belongs to a"
                    f" different, already-tracked LCARS show ({recheck_id}) — refusing to"
                    " create a duplicate or an orphaned Sonarr/Radarr entry"
                )
            existing_show_id = recheck_id  # a different stub than originally matched

        add_result = _ensure_in_arr(conn, input, candidate)
        arr_result.update({k: v for k, v in add_result.items() if k in arr_result})
        if "tvdb_id" in add_result:
            resolved_input["tvdb_id"] = add_result["tvdb_id"]
        if "tmdb_id" in add_result:
            resolved_input["tmdb_id"] = add_result["tmdb_id"]
        title_slug = add_result.get("title_slug")

    if existing_show_id is not None:
        # Same sequel gate as create_show — an untracked stub that is
        # a SEQUEL of a tracked show should be offered as a season-attach,
        # not promoted to a separate tracked show.
        sequel = find_sequel_parent(
            conn, input.get("anilist_id"),
            tvdb_id=input.get("tvdb_id"), tmdb_id=input.get("tmdb_id"),
            imdb_id=input.get("imdb_id"), mal_id=input.get("mal_id"),
        )
        if sequel is not None:
            raise SequelDetectedError(
                sequel["parent_show_id"],
                sequel["parent_title"],
                sequel["next_season"],
                sequel_anilist_id=input.get("anilist_id"),
                sequel_mal_id=input.get("mal_id"),
            )
        show_id = _promote_stub(conn, existing_show_id, resolved_input)
    else:
        show_id = create_show(conn, resolved_input)
    if title_slug:
        write_arr_external_id(conn, show_id, input["media_shape"], title_slug)
    return show_id, arr_result
