"""On-demand external metadata fetch — SCOPE.md §4 Phase A / §5.1/§5.2/
§5.4/§5.5/§5.8, BUILD_PLAN.md A.8.

Orchestrates the "immediate one-shot metadata fetch (poster, synopsis,
cast, episode list, external ids) on show creation" Phase A's own intro
text describes. Per the user's explicit direction (2026-08-08, asked
directly rather than assuming A.4/A.7's original "client fetches, LCARS
reconciles" framing carried over): LCARS itself makes these calls —
that framing was only ever a Data-shaped transitional stage, not the
end state ("ultimately all compute and fetch will be handled on the
server by lcars, so might as well built it this way now"). Data keeps
its own separate, pre-existing Sonarr/AniList access for now (A.17,
its own repo) — this module doesn't change that, it's a second,
independent path other clients (or Data itself, later) can also use.

Best-effort throughout (confirmed 2026-08-08): a service being
unreachable never blocks show creation, and a failure in one source
branch (AniList/Sonarr/Radarr) doesn't stop the others from still
running. Each branch's whole body runs under `_guarded` — deliberately
catching bare `Exception`, not just each client's own error class: a
malformed/unexpected API response should never crash addShow either,
only skip that one branch. Failures are never silent: each one opens/
extends a pending_review entry (entity_type='show', reusing §5.6's
existing value-chain-accumulation mechanism rather than inventing a
second "something needs attention" channel) so a human can see it via
the same pendingReviews surface as everything else — and retry, by
simply calling fetch_and_populate() again (exposed as its own
standalone `refreshShowMetadata` mutation, resolvers.py), the same
function addShow already calls inline. "No silent fail... manual fix
by user is also an option" (the user's own words) is satisfied by this
pairing: visible via pending_review, retriable via the mutation. Every
write here is upsert-shaped specifically so a retry after a partial
failure is always safe to just re-run in full.

`season`'s auto-creation here (season_number=1, manual_override=True)
is confirmed in the same round: the anilistId/malId used came from
whatever the caller already supplied on addShow's input — the same
class of "a human already confirmed this" information setSeasonMapping
treats as manual_override. No franchise auto-creation (confirmed
separately, same round): franchise stays a deliberately-created
grouping (§5.9), never a universal per-show wrapper — "no auto
franchise if the system can still confirm ... mapping change first" —
satisfied as-is, since franchise_member (§5.9) already links to
existing show/season/episode rows without needing them restructured;
nothing here needs to change for a later franchise relationship to be
recorded.
"""

import json
from collections.abc import Callable

from lcars import (
    anilist_client,
    ids,
    pending_review,
    radarr_client,
    season_mapping,
    sonarr_client,
    tmdb_client,
    util,
)
from lcars.config import get_current

# A.19 — mirrors resolvers.py's own EXTERNAL_ID_URL_TEMPLATES/
# TMDB_URL_TEMPLATES exactly (migration 7196ca889757's show_external_id
# shape), duplicated here rather than imported: resolvers.py imports
# this module, so the reverse import would be circular.
_TMDB_TV_URL_TEMPLATE = "https://www.themoviedb.org/tv/{id}"


def fetch_and_populate(conn, show_id: str) -> None:
    """Best-effort, never raises — see module docstring. Call once
    inline from addShow (creation) or any time after (retry/refresh)."""
    row = conn.execute("SELECT * FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such show: {show_id}")
    show = dict(row)

    if show["tracking_space"] == "anime":
        _guarded(conn, show, "anilist", _fetch_anilist)
    else:
        # A.19 — AniList already covers duration for anime (its own
        # `duration` field, above); everything else gets it from TMDB.
        _guarded(conn, show, "tmdb", _fetch_tmdb_duration)
    if show["media_shape"] == "episodic":
        _guarded(conn, show, "sonarr", _fetch_sonarr)
    elif show["media_shape"] == "movie":
        _guarded(conn, show, "radarr", _fetch_radarr)


def _guarded(conn, show: dict, service: str, fn: Callable[[object, dict], None]) -> None:
    """Runs `fn(conn, show)`, catching anything at all — a malformed
    response should skip this one branch, never crash addShow or block
    the other branches. Deliberately broad (bare Exception), not just
    each client's own *Error class — see module docstring."""
    try:
        fn(conn, show)
    except Exception as e:  # noqa: BLE001 — deliberate, see docstring
        pending_review.open_or_extend(
            conn, "show", show["id"], "metadata_fetch", service, None, str(e)
        )


def _external_id(conn, show_id: str, service: str) -> str | None:
    row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = ?",
        (show_id, service),
    ).fetchone()
    return row["external_id"] if row else None


# --- AniList (tracking_space = anime) ---------------------------------------


def _fetch_anilist(conn, show: dict) -> None:
    anilist_id_str = _external_id(conn, show["id"], "anilist")
    if anilist_id_str is None:
        # tracking_space = anime mandates an AniList link (§5.1) — the
        # caller should have supplied anilistId on addShow's input.
        # Nothing to fetch without it; this is a caller-input gap, not
        # an unreachable-service failure, so no pending_review entry.
        return
    media = anilist_client.fetch_media(int(anilist_id_str))
    if media is None:
        return

    now = util.now_utc_iso()
    genres = media.get("genres")
    conn.execute(
        "UPDATE show SET"
        "  poster_url = COALESCE(?, poster_url),"
        "  banner_url = COALESCE(?, banner_url),"
        "  synopsis = COALESCE(?, synopsis),"
        "  genres_raw = COALESCE(?, genres_raw),"
        "  total_episodes = COALESCE(?, total_episodes),"
        "  duration_minutes = COALESCE(?, duration_minutes),"
        "  updated_at = ?"
        " WHERE id = ?",
        (
            (media.get("coverImage") or {}).get("large"),
            media.get("bannerImage"),
            media.get("description"),
            json.dumps(genres) if genres else None,
            media.get("episodes"),
            media.get("duration"),
            now,
            show["id"],
        ),
    )

    _upsert_season(conn, show["id"], 1, int(anilist_id_str), media.get("idMal"))

    for studio in (media.get("studios") or {}).get("nodes") or []:
        if studio.get("id") is not None and studio.get("name"):
            _link_studio(conn, show["id"], studio, "studio")

    for edge in (media.get("characters") or {}).get("edges") or []:
        character_name = ((edge.get("node") or {}).get("name") or {}).get("full")
        voice_actors = edge.get("voiceActors") or []
        if voice_actors and (voice_actors[0].get("name") or {}).get("full"):
            _link_person(conn, show["id"], voice_actors[0], "voice_actor", character_name)

    for edge in (media.get("relations") or {}).get("edges") or []:
        node = edge.get("node") or {}
        is_trackable = node.get("format") in anilist_client.ANIME_RELATION_FORMATS
        if node.get("id") is not None and is_trackable:
            _link_relation(conn, show["id"], node)


def _link_relation(conn, show_id: str, related_media: dict) -> None:
    """§5.9 — `show_relation` is directed, written whenever a show's
    AniList data reports a relation, one row for this direction only;
    the other show's own fetch (if/when it happens) writes its own
    direction independently. `related_show_id` is a real, non-nullable
    FK (§5.9's own table definition), so a related show LCARS has never
    seen before needs a real row to point at — resolved 2026-08-09
    (A.21, asked directly): auto-create it as a `tracked = false` stub,
    the same promotion-target shape §5.1's own "Show-row promotion
    paths" already describes ("a bare tracked = false relation/
    franchise stub becomes a real tracked show by flipping tracked =
    true"). No franchise auto-creation here either way (A.8's own "no
    auto franchise" precedent, §5.9 — a relation edge is not a
    franchise membership, `franchise_member` stays a deliberate,
    separate action)."""
    related_anilist_id = str(related_media["id"])
    existing_show = conn.execute(
        "SELECT show_id FROM show_external_id WHERE service = 'anilist' AND external_id = ?",
        (related_anilist_id,),
    ).fetchone()
    if existing_show is not None:
        related_show_id = existing_show["show_id"]
    else:
        related_show_id = _create_relation_stub(conn, related_media, related_anilist_id)

    now = util.now_utc_iso()
    conn.execute(
        "INSERT OR IGNORE INTO show_relation (show_id, related_show_id, created_at)"
        " VALUES (?, ?, ?)",
        (show_id, related_show_id, now),
    )


def _create_relation_stub(conn, related_media: dict, related_anilist_id: str) -> str:
    title = related_media.get("title") or {}
    romaji, english, native = title.get("romaji"), title.get("english"), title.get("native")
    if romaji:
        primary_title = "romaji"
    elif english:
        primary_title = "english"
    elif native:
        primary_title = "native"
    else:
        # AniList's own schema guarantees at least a romaji title exists for
        # any real Media — an edge with none at all isn't a usable stub.
        raise ValueError(f"AniList relation {related_anilist_id} has no title at all")

    show_id = ids.generate_id(conn, "s")
    now = util.now_utc_iso()
    media_shape = "movie" if related_media.get("format") == "MOVIE" else "episodic"
    conn.execute(
        "INSERT INTO show"
        " (id, media_shape, tracking_space, title_romaji, title_english, title_native,"
        "  primary_title, status, tracked, created_at, updated_at)"
        " VALUES (?, ?, 'anime', ?, ?, ?, ?, 'planned', 0, ?, ?)",
        (show_id, media_shape, romaji, english, native, primary_title, now, now),
    )
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'anilist', ?, ?, ?)",
        (show_id, related_anilist_id, f"https://anilist.co/anime/{related_anilist_id}", now),
    )
    if related_media.get("idMal") is not None:
        mal_id = str(related_media["idMal"])
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'mal', ?, ?, ?)",
            (show_id, mal_id, f"https://myanimelist.net/anime/{mal_id}", now),
        )
    return show_id


def _upsert_season(
    conn, show_id: str, season_number: int, anilist_id: int | None, mal_id: int | None
) -> None:
    """A caller-supplied id at addShow time is the same class of
    "a human already confirmed this" information setSeasonMapping
    treats as manual_override — see module docstring. Deliberately
    does NOT touch an already-manual_override row (§3 principle 6),
    same protection reconcileSeasonMapping (A.4) gives it."""
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT id, manual_override FROM season WHERE show_id = ? AND season_number = ?",
        (show_id, season_number),
    ).fetchone()
    if existing is not None:
        if existing["manual_override"]:
            return
        conn.execute(
            "UPDATE season SET anilist_id = ?, mal_id = ?, source = 'manual',"
            "     matched = 1, manual_override = 1, updated_at = ?"
            " WHERE id = ?",
            (anilist_id, mal_id, now, existing["id"]),
        )
        return
    season_id = ids.generate_id(conn, "z")
    conn.execute(
        "INSERT INTO season"
        " (id, show_id, season_number, anilist_id, mal_id, source, matched,"
        "  manual_override, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'manual', 1, 1, ?, ?)",
        (season_id, show_id, season_number, anilist_id, mal_id, now, now),
    )


def _link_studio(conn, show_id: str, studio: dict, role_type: str) -> None:
    """Upsert-by-AniList-id so the same real-world studio isn't
    duplicated across shows that share one (§5.8's external_service/
    external_id columns exist for exactly this)."""
    now = util.now_utc_iso()
    anilist_id = str(studio["id"])
    existing = conn.execute(
        "SELECT id FROM studio WHERE external_service = 'anilist' AND external_id = ?",
        (anilist_id,),
    ).fetchone()
    if existing is not None:
        studio_id = existing["id"]
    else:
        studio_id = ids.generate_id(conn, "d")
        conn.execute(
            "INSERT INTO studio (id, name, external_service, external_id, external_url, created_at)"
            " VALUES (?, ?, 'anilist', ?, ?, ?)",
            (studio_id, studio["name"], anilist_id, f"https://anilist.co/staff/{anilist_id}", now),
        )
    conn.execute(
        "INSERT OR IGNORE INTO show_studio (show_id, studio_id, role_type) VALUES (?, ?, ?)",
        (show_id, studio_id, role_type),
    )


def _link_person(conn, show_id: str, voice_actor: dict, role_type: str, character_name) -> None:
    """Upsert-by-AniList-id, same reasoning as _link_studio. The
    show_person guard checks (show_id, person_id, character_name)
    together, not just (show_id, person_id) like show_studio's plain
    `INSERT OR IGNORE` — a person can voice more than one character in
    the same show (§5.8's own "no composite natural-key PK" note), so
    collapsing on person alone would silently drop a second real role;
    keying on the full triple still makes a retry after a prior
    failure safe to just re-run without duplicating any one row."""
    now = util.now_utc_iso()
    anilist_id = str(voice_actor["id"])
    name = voice_actor["name"]["full"]
    existing = conn.execute(
        "SELECT id FROM person WHERE external_service = 'anilist' AND external_id = ?",
        (anilist_id,),
    ).fetchone()
    if existing is not None:
        person_id = existing["id"]
    else:
        person_id = ids.generate_id(conn, "p")
        conn.execute(
            "INSERT INTO person (id, name, external_service, external_id, external_url, created_at)"
            " VALUES (?, ?, 'anilist', ?, ?, ?)",
            (person_id, name, anilist_id, f"https://anilist.co/staff/{anilist_id}", now),
        )
    already_linked = conn.execute(
        "SELECT 1 FROM show_person WHERE show_id = ? AND person_id = ? AND character_name IS ?",
        (show_id, person_id, character_name),
    ).fetchone()
    if already_linked is None:
        conn.execute(
            "INSERT INTO show_person (show_id, person_id, role_type, character_name)"
            " VALUES (?, ?, ?, ?)",
            (show_id, person_id, role_type, character_name),
        )


# --- TMDB (everything except tracking_space = anime) — A.19 -----------------


def _fetch_tmdb_duration(conn, show: dict) -> None:
    """Fills `show.duration_minutes` for every non-anime show, movie or
    TV — AniList already covers anime via its own `duration` field
    (see `_fetch_anilist` above). A movie's tmdb id usually already
    exists (§5.4: movies key primarily on TMDB, native to Radarr); a
    non-anime TV show usually only carries a tvdb id (native to
    Sonarr), so this resolves TMDB's id via `find_by_tvdb_id` first and
    persists the result as a real `show_external_id` row (same
    upsert-by-show_id+service shape `linkShowExternalId` itself uses,
    resolvers.py) — a retry/refresh afterward reads it straight back,
    no need to re-resolve."""
    cfg = get_current()
    if not cfg.tmdb_api_key:
        return  # not configured — same as "not linked", not a failure to report

    tmdb_id_str = _external_id(conn, show["id"], "tmdb")
    with tmdb_client.TmdbClient(cfg.tmdb_api_key) as client:
        if tmdb_id_str is None:
            tmdb_id_str = _resolve_and_store_tmdb_id(conn, show, client)
            if tmdb_id_str is None:
                return
        tmdb_id = int(tmdb_id_str)
        if show["media_shape"] == "movie":
            runtime = client.movie_runtime(tmdb_id)
        else:
            runtime = client.tv_episode_runtime(tmdb_id)

    if runtime:
        conn.execute(
            "UPDATE show SET duration_minutes = COALESCE(?, duration_minutes), updated_at = ?"
            " WHERE id = ?",
            (runtime, util.now_utc_iso(), show["id"]),
        )


def _resolve_and_store_tmdb_id(
    conn, show: dict, client: "tmdb_client.TmdbClient"
) -> str | None:
    """Only the episodic (TV) side has a bridge to resolve through — a
    movie with no tmdb id at all has nothing this function can do
    about it (no title-search feature exists, out of A.19's own
    scope); `_fetch_tmdb_duration` above already no-ops for that case
    the same way every other "nothing to look up" branch in this file
    does."""
    if show["media_shape"] != "episodic":
        return None
    tvdb_id_str = _external_id(conn, show["id"], "tvdb")
    if tvdb_id_str is None:
        return None
    tmdb_id = client.find_by_tvdb_id(int(tvdb_id_str))
    if tmdb_id is None:
        return None
    now = util.now_utc_iso()
    conn.execute(
        "INSERT OR IGNORE INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'tmdb', ?, ?, ?)",
        (show["id"], str(tmdb_id), _TMDB_TV_URL_TEMPLATE.format(id=tmdb_id), now),
    )
    return str(tmdb_id)


# --- Sonarr (media_shape = episodic) ----------------------------------------


def _fetch_sonarr(conn, show: dict) -> None:
    tvdb_id_str = _external_id(conn, show["id"], "tvdb")
    if tvdb_id_str is None:
        return  # §5.1 — Sonarr's link is optional; nothing to fetch without it
    cfg = get_current()
    if not cfg.sonarr_url or not cfg.sonarr_api_key:
        return  # not configured — same as "not linked", not a failure to report

    with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
        series = client.series_by_tvdb_id(int(tvdb_id_str))
        if series is None:
            return  # not (yet) in Sonarr's own library — not an error, §5.1
        episodes = client.episodes(series["id"])

    # A.20 (2026-08-09 consolidation pass) — real gap found in the audit:
    # this function used to insert `episode` rows for whatever season
    # numbers Sonarr reported without ever creating the corresponding
    # `season` row (§5.5) or setting `episode.season_id`. Sonarr/TVDB is
    # the only source that ever tells LCARS a season number exists at all
    # (AniList doesn't — each AniList entry is already scoped to one
    # season, per §5.5's own "TVDB groups a franchise's seasons... AniList
    # splits each season" framing), so this is the one place a newly-
    # discovered season number can be reconciled the moment it appears —
    # same on-demand-immediately philosophy as every other A.8 branch,
    # rather than leaving it as a bare unmatched row for a Phase B
    # scheduler that doesn't exist yet. A season already known (existing
    # `season` row, whatever its `manual_override`) is left untouched by
    # `reconcile_season` itself — see that function's own docstring.
    season_numbers = {ep.get("seasonNumber") for ep in episodes}
    season_ids_by_number = _ensure_seasons(conn, show["id"], season_numbers)

    now = util.now_utc_iso()
    for ep in episodes:
        season_number = ep.get("seasonNumber")
        episode_number = ep.get("episodeNumber")
        if season_number is None or episode_number is None:
            continue
        season_id = season_ids_by_number.get(season_number)
        existing = conn.execute(
            "SELECT id FROM episode WHERE show_id = ? AND season = ? AND episode = ?",
            (show["id"], season_number, episode_number),
        ).fetchone()
        if existing is not None:
            # A.20 — backfill season_id on a pre-existing row that predates
            # this fix (or was inserted before its season was reconciled).
            # Never touches any other column — "never overwrite an
            # already-tracked episode's own state" still holds.
            conn.execute(
                "UPDATE episode SET season_id = ? WHERE id = ? AND season_id IS NULL",
                (season_id, existing["id"]),
            )
            continue
        episode_id = ids.generate_id(conn, "e")
        conn.execute(
            "INSERT INTO episode"
            " (id, show_id, season, season_id, episode, kind, air_date_utc,"
            "  air_date_source, air_date_raw_sonarr, runtime_minutes,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'regular', ?, 'sonarr', ?, ?, ?, ?)",
            (
                episode_id,
                show["id"],
                season_number,
                season_id,
                episode_number,
                ep.get("airDateUtc"),
                ep.get("airDateUtc"),
                ep.get("runtime"),
                now,
                now,
            ),
        )

    # A.22 — episode-numbering-scheme automatic derivation. Deferred at
    # A.4 for lack of real data to derive from; A.8's own Sonarr fetch
    # (this function) is exactly that data, so it's the natural place to
    # attempt it, the same "on-demand, immediately" pattern as everything
    # else here — not a separate background pass.
    _derive_episode_numbering(conn, show["id"], series, episodes)


def _ensure_seasons(conn, show_id: str, season_numbers: set) -> dict:
    """Reconciles a `season` row (§5.5) for every distinct season number
    just seen in a Sonarr fetch, creating one via `season_mapping.
    reconcile_season()` (A.4/A.20) for any number with no existing row
    yet. Returns {season_number: season_id}, including already-existing
    seasons untouched by this call, for the caller to attach to episode
    rows. Each reconciliation attempt is individually guarded (unlike
    the rest of this module's branch-level `_guarded`) — a Fribb dataset
    hiccup (§5.5, `fribb.load_dataset` can raise with no cache and no
    network) must not abort the Sonarr episode import itself, which is
    this function's actual point; on that failure the season row still
    gets created, just left unmatched, with its own pending_review entry
    for the failure specifically (distinct from a genuine "no candidate
    found" review — same field name, different source, so both remain
    individually traceable)."""
    result = {}
    for season_number in sorted(n for n in season_numbers if n is not None):
        row = conn.execute(
            "SELECT id FROM season WHERE show_id = ? AND season_number = ?",
            (show_id, season_number),
        ).fetchone()
        if row is not None:
            result[season_number] = row["id"]
            continue
        try:
            season = season_mapping.reconcile_season(conn, show_id, season_number)
            result[season_number] = season["id"]
        except Exception as e:  # noqa: BLE001 — same bare-except reasoning as _guarded
            season_id = ids.generate_id(conn, "z")
            now = util.now_utc_iso()
            conn.execute(
                "INSERT INTO season"
                " (id, show_id, season_number, source, matched, manual_override,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, 'unmatched', 0, 0, ?, ?)",
                (season_id, show_id, season_number, now, now),
            )
            pending_review.open_or_extend(
                conn, "season", season_id, "anilist_id", "fribb", None, str(e)
            )
            conn.commit()
            result[season_number] = season_id
    return result


def _derive_episode_numbering(conn, show_id: str, series: dict, episodes: list[dict]) -> None:
    """Automatic numbering-scheme derivation (§5.5), deferred at A.4 for
    lack of data, built here at A.22 once A.8's own Sonarr fetch supplies
    it. Heuristic, deliberately simple (personal-tracker scale, same
    reasoning §6.5's plain-LIKE search already leans on): Sonarr's own
    `seriesType = 'anime'` flag, or any episode actually carrying a
    populated `absoluteEpisodeNumber`, is a direct, reliable signal this
    show uses absolute numbering in Sonarr's own data (the "Sonarr
    absolute-order info" §5.5 names) — anything else defaults to
    ordinary season+episode numbering, Sonarr's own standard convention.
    AniList episode counts (§5.5's other named signal) aren't used here:
    AniList has no numbering-scheme concept of its own to cross-check
    against (each AniList entry is already one season, §5.5) — its
    episode *count* only matters for validating a scheme already derived
    from Sonarr, not for deriving one from nothing, so a not-linked/
    not-configured Sonarr leaves this table unmatched rather than
    guessing off AniList alone, same "no real data, stays honestly
    unmapped" treatment as `season`. Never touches a `manual_override`
    row (§3 principle 6) — same protection `setEpisodeNumberingScheme`
    already gives it.
    """
    existing = conn.execute(
        "SELECT * FROM episode_numbering_mapping WHERE show_id = ?", (show_id,)
    ).fetchone()
    if existing is not None and existing["manual_override"]:
        return

    is_absolute = series.get("seriesType") == "anime" or any(
        ep.get("absoluteEpisodeNumber") is not None for ep in episodes
    )
    scheme = "absolute" if is_absolute else "season_episode"
    now = util.now_utc_iso()

    if existing is not None:
        if existing["scheme"] != scheme:
            pending_review.open_or_extend(
                conn,
                "episode_numbering_mapping",
                existing["id"],
                "scheme",
                "sonarr",
                existing["scheme"],
                scheme,
            )
        conn.execute(
            "UPDATE episode_numbering_mapping"
            " SET scheme = ?, source = 'sonarr', matched = 1, updated_at = ?"
            " WHERE id = ?",
            (scheme, now, existing["id"]),
        )
    else:
        mapping_id = ids.generate_id(conn, "n")
        conn.execute(
            "INSERT INTO episode_numbering_mapping"
            " (id, show_id, scheme, source, matched, manual_override, created_at, updated_at)"
            " VALUES (?, ?, ?, 'sonarr', 1, 0, ?, ?)",
            (mapping_id, show_id, scheme, now, now),
        )
    conn.commit()


# --- Radarr (media_shape = movie) -------------------------------------------


def _fetch_radarr(conn, show: dict) -> None:
    tmdb_id_str = _external_id(conn, show["id"], "tmdb")
    if tmdb_id_str is None:
        return  # nothing to look up without a tmdb id
    cfg = get_current()
    if not cfg.radarr_url or not cfg.radarr_api_key:
        return  # not configured — same as "not linked", not a failure to report

    with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
        movie = client.movie_by_tmdb_id(int(tmdb_id_str))
    if movie is None:
        return  # not (yet) in Radarr's own library — not an error, §5.1

    now = util.now_utc_iso()
    poster = next(
        (
            img.get("remoteUrl")
            for img in movie.get("images") or []
            if img.get("coverType") == "poster"
        ),
        None,
    )
    genres = movie.get("genres")
    conn.execute(
        "UPDATE show SET"
        "  poster_url = COALESCE(?, poster_url),"
        "  synopsis = COALESCE(?, synopsis),"
        "  genres_raw = COALESCE(?, genres_raw),"
        "  updated_at = ?"
        " WHERE id = ?",
        (poster, movie.get("overview"), json.dumps(genres) if genres else None, now, show["id"]),
    )
