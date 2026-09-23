"""Fribb/anime-lists dataset — SCOPE.md §5.5, BUILD_PLAN.md A.4.

Resolves AniList/MAL ids for a specific (tvdb_id, season_number) pair by
downloading (and caching) the community-maintained Fribb/anime-lists
dataset. Ported from Data's own real, production-tested `mapping.py`
(~/repos/data/src/data/mapping.py, forked from aniq) — the matching
logic itself is deliberately unchanged from that reference, including
the single-candidate short-circuit's own hard-won behavior (see its
docstring below): season-tag checking was tried live and reverted after
real-library data showed it caused far more regressions than fixes.

Adapted for LCARS's own execution model (§11.2: sync resolvers, one
shared connection, no threading) — a sync `httpx.Client` here, not
Data's `httpx.AsyncClient`. Also returns the whole matched candidate
dict rather than just `anilist_id`, since `season` (§5.5) stores both
`anilist_id` and `mal_id` — the Fribb dataset carries both under one
entry, confirmed by inspecting a live sample (2026-08-08): no separate
MAL-specific matching pass is needed.
"""

import json
import time
from pathlib import Path

import httpx

DATASET_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"

# Nested under the shared "starfleet" namespace, "lcars" sub-namespace —
# same convention as config.py's CONFIG_PATH (§4.0 addendum's
# collision-avoidance reasoning), mirrors Data's own
# ~/.local/share/starfleet/data/anime-lists.json cache under its own
# sibling sub-namespace.
DATASET_CACHE_PATH = Path.home() / ".local" / "share" / "starfleet" / "lcars" / "anime-lists.json"

# Matches §5.5's own weekly-reconciliation-cadence framing — id-mapping
# data changes far less often than episode/schedule metadata, so a
# week-old on-disk cache is fine and avoids a network round trip on
# every call. (The actual *scheduled* weekly pass is Phase B, §4 — this
# is just the cache TTL the on-demand A.4 path also benefits from.)
DATASET_MAX_AGE_SECONDS = 7 * 24 * 3600

# Sentinel values the dataset itself uses for "no id of this kind" —
# alongside a real absence (key missing/None) — confirmed via a live
# sample fetch, 2026-08-08.
_MISSING = (None, "", "unknown")

# In-process parse cache, keyed by (cache path, file mtime) — added
# 2026-08-09 in the consolidation audit. `anime-list-full.json` is
# multi-megabyte, and A.20 made reconciliation automatic: one full
# read+parse (and, at the call site, one full index build) happened per
# *season* of every show fetched. Measured 0.30s for a five-season show
# on a 3.2MB synthetic set; the real dataset is larger. That cost lands
# inside a sync resolver on the single shared connection (§11.2), whose
# whole justification is that nothing blocks the event loop for long.
# Memoized here at the source rather than hoisted to one caller,
# because Phase B's B.2 (weekly reconciliation across *every* show) is
# the real hammer and would otherwise repeat the same waste per show.
# Keyed on mtime so a refreshed download is picked up immediately
# without any explicit invalidation.
_parse_cache: dict[tuple[str, int], list[dict]] = {}
_index_cache: dict[int, dict[int, list[dict]]] = {}
# 2026-08-18 — the reverse of _index_cache above, same "keyed on the
# dataset object's identity" memoization; see build_anilist_index()'s
# own docstring for why this exists as a genuinely separate index
# rather than a lookup derived from build_tvdb_index()'s output.
_anilist_index_cache: dict[int, dict[int, list[dict]]] = {}
# 2026-09-06 — keyed by mal_id, same structure as the anilist index.
# Used by browse's MAL-fallback path to recover tvdb/anilist/imdb IDs
# when AniList is down and only MAL IDs are available.
_mal_index_cache: dict[int, dict[int, list[dict]]] = {}
# 2026-09-12 — keyed by anidb_id, same structure.  Used by
# propagate_cross_ids to bridge AniDB→AniList/MAL/TMDB/IMDB for
# shows that enter the system via TVDB (Sonarr) and have no AniList.
_anidb_index_cache: dict[int, dict[int, list[dict]]] = {}


def _dataset_is_stale(path: Path, max_age: float) -> bool:
    return not path.exists() or time.time() - path.stat().st_mtime > max_age


def load_dataset(
    *,
    cache_path: Path = DATASET_CACHE_PATH,
    max_age: float = DATASET_MAX_AGE_SECONDS,
    client: httpx.Client | None = None,
) -> list[dict]:
    """One-time/on-demand download, not live polling (§4 Phase A) — a
    fresh-enough on-disk cache is used as-is, only refetched once
    stale. Falls back to a stale cache on a network error rather than
    raising, same as Data's own reference behavior — the mapper stays
    useful (if slightly out of date) even when GitHub is unreachable.
    """
    if not _dataset_is_stale(cache_path, max_age):
        return _read_cached(cache_path)

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        response = client.get(DATASET_URL)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError:
        if cache_path.exists():
            return _read_cached(cache_path)
        raise
    finally:
        if owns_client:
            client.close()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data))
    return data


def _read_cached(cache_path: Path) -> list[dict]:
    """Parse-once-per-file-version — see `_parse_cache`'s own note."""
    key = (str(cache_path), cache_path.stat().st_mtime_ns)
    cached = _parse_cache.get(key)
    if cached is None:
        cached = json.loads(cache_path.read_text())
        _parse_cache.clear()  # only ever one dataset version worth keeping
        _parse_cache[key] = cached
    return cached


def build_tvdb_index(dataset: list[dict]) -> dict[int, list[dict]]:
    """Keyed by tvdb_id — TVDB groups a franchise's seasons under one
    series id, so one key commonly maps to several dataset entries
    (one per AniList-side season split).

    Memoized on the dataset object's identity (2026-08-09 audit): the
    per-season reconciliation A.20 introduced rebuilt this full index
    once per season, on top of re-parsing the file. Identity is the
    right key precisely because `load_dataset` now returns the *same*
    list object for an unchanged file — a new download produces a new
    object and therefore a new index, with no explicit invalidation."""
    cached = _index_cache.get(id(dataset))
    if cached is not None:
        return cached
    index: dict[int, list[dict]] = {}
    for entry in dataset:
        if entry.get("tvdb_id") in _MISSING or entry.get("anilist_id") in _MISSING:
            continue
        index.setdefault(entry["tvdb_id"], []).append(entry)
    _index_cache.clear()  # same one-version-at-a-time policy as _parse_cache
    _index_cache[id(dataset)] = index
    return index


def build_anilist_index(dataset: list[dict]) -> dict[int, list[dict]]:
    """Keyed by anilist_id — the reverse of build_tvdb_index() above,
    same dataset, same memoization shape. 2026-08-18, resolveTvdbIds
    (tvdb_backfill.py): a show tracked only via AniList (never added to
    Sonarr) has no tvdb_id at all, and the *forward* index/
    resolve_season_candidate() can't help — both are keyed by tvdb_id,
    which is exactly the thing missing. This is the same Fribb rows
    LCARS already downloads/caches for the forward direction, just
    indexed the other way — no new dataset, no new network call.

    Not derived from build_tvdb_index()'s own output: a franchise's
    tvdb_id can appear under several dataset entries (season splits),
    each with its *own* anilist_id — walking the tvdb-keyed index back
    out would silently collapse that structure. Building straight from
    `dataset` keeps every entry's own anilist_id/tvdb_id pairing
    intact, which resolve_tvdb_id_for_anilist() below needs to detect a
    genuinely ambiguous case (see its own docstring)."""
    cached = _anilist_index_cache.get(id(dataset))
    if cached is not None:
        return cached
    index: dict[int, list[dict]] = {}
    for entry in dataset:
        if entry.get("anilist_id") in _MISSING or entry.get("tvdb_id") in _MISSING:
            continue
        index.setdefault(entry["anilist_id"], []).append(entry)
    _anilist_index_cache.clear()  # same one-version-at-a-time policy as _index_cache
    _anilist_index_cache[id(dataset)] = index
    return index


def resolve_tvdb_id_for_anilist(index: dict[int, list[dict]], anilist_id: int) -> int | None:
    """The single tvdb_id every dataset entry under this anilist_id
    agrees on, or None — never a guess. Two ways this returns None:
    genuinely unknown (anilist_id isn't in the dataset at all), or
    known but ambiguous (a franchise's own AniList entry is split
    across multiple tvdb_ids in Fribb's own data — rare, but real, and
    picking the first would risk a wrong link for a browser deep-link
    feature whose entire point is precision). Same "return None rather
    than guess" discipline resolve_season_candidate() above already
    established for the forward direction."""
    candidates = index.get(anilist_id, [])
    if not candidates:
        return None
    tvdb_ids = {c["tvdb_id"] for c in candidates}
    if len(tvdb_ids) != 1:
        return None
    return tvdb_ids.pop()


def build_mal_index(dataset: list[dict]) -> dict[int, list[dict]]:
    """Keyed by mal_id — same structure as build_anilist_index() above.
    Used by the browse MAL-fallback path: when AniList is down, browse
    data only carries MAL IDs, so this index recovers tvdb_id, anilist_id,
    and imdb_id from the Fribb dataset without any network call.

    Unlike the anilist/tvdb indexes, this does NOT require tvdb_id to be
    present — the primary value is recovering anilist_id (restoring the
    AniList link button), with tvdb_id/imdb_id as bonuses when available.
    Entries with only a mal_id (no anilist, no tvdb) still aren't useful,
    so we require at least one of anilist_id or tvdb_id."""
    cached = _mal_index_cache.get(id(dataset))
    if cached is not None:
        return cached
    index: dict[int, list[dict]] = {}
    for entry in dataset:
        if entry.get("mal_id") in _MISSING:
            continue
        # Need at least one recoverable ID to be useful
        has_anilist = entry.get("anilist_id") not in _MISSING
        has_tvdb = entry.get("tvdb_id") not in _MISSING
        if not has_anilist and not has_tvdb:
            continue
        index.setdefault(entry["mal_id"], []).append(entry)
    _mal_index_cache.clear()
    _mal_index_cache[id(dataset)] = index
    return index


def mal_for_anilist(dataset: list[dict], anilist_id: int) -> int | None:
    """The one MAL id Fribb pairs with `anilist_id`, or None (unknown or
    ambiguous). AniList is authoritative and MAL mirrors it (project
    rule), so this is how a season's MAL id is derived from its AniList
    id. Scans every entry, tvdb-linked or not."""
    mals = {
        e["mal_id"]
        for e in dataset
        if e.get("anilist_id") == anilist_id and e.get("mal_id") not in _MISSING
    }
    return mals.pop() if len(mals) == 1 else None


def anilist_ids_for_mal(dataset: list[dict], mal_id: int) -> set[int]:
    """Every AniList id Fribb pairs with `mal_id` (empty when unknown)."""
    return {
        e["anilist_id"]
        for e in dataset
        if e.get("mal_id") == mal_id and e.get("anilist_id") not in _MISSING
    }


def build_anidb_index(dataset: list[dict]) -> dict[int, list[dict]]:
    """Keyed by anidb_id — bridges AniDB→AniList/MAL/TMDB/IMDB via Fribb.

    Used by propagate_cross_ids for shows that enter the system with only
    a TVDB ID (e.g. added via Sonarr): TVDB → anime_list_entry → AniDB,
    then this index recovers all other IDs without needing AniList as root.
    Same memoization and "never guess" discipline as the other indexes."""
    cached = _anidb_index_cache.get(id(dataset))
    if cached is not None:
        return cached
    index: dict[int, list[dict]] = {}
    for entry in dataset:
        if entry.get("anidb_id") in _MISSING:
            continue
        # Need at least one recoverable ID to be useful
        has_anilist = entry.get("anilist_id") not in _MISSING
        has_mal = entry.get("mal_id") not in _MISSING
        if not has_anilist and not has_mal:
            continue
        index.setdefault(entry["anidb_id"], []).append(entry)
    _anidb_index_cache.clear()
    _anidb_index_cache[id(dataset)] = index
    return index


def resolve_ids_for_anidb(
    index: dict[int, list[dict]], anidb_id: int
) -> dict[str, int | str | None]:
    """Resolve anilist_id, mal_id, tmdb_id, and imdb_id from an AniDB ID.

    Same "never guess" discipline — if the anidb_id maps to multiple
    conflicting values for a field, that field is None."""
    candidates = index.get(anidb_id, [])
    if not candidates:
        return {"anilist_id": None, "mal_id": None, "tmdb_id": None,
                "tmdb_kind": None, "imdb_id": None}

    anilist_ids = {c["anilist_id"] for c in candidates
                   if c.get("anilist_id") not in _MISSING}
    anilist_id = anilist_ids.pop() if len(anilist_ids) == 1 else None

    mal_ids = {c["mal_id"] for c in candidates
               if c.get("mal_id") not in _MISSING}
    mal_id = mal_ids.pop() if len(mal_ids) == 1 else None

    # TMDB: Fribb stores as {"tv": id} or {"movie": id}
    tmdb_id = None
    tmdb_kind = None
    for c in candidates:
        tmdb = c.get("themoviedb_id")
        if tmdb:
            tid = tmdb.get("tv") or tmdb.get("movie")
            if tid:
                tmdb_id = tid
                tmdb_kind = "tv" if tmdb.get("tv") else "movie"
                break

    # IMDB: Fribb stores as a list
    imdb_list = candidates[0].get("imdb_id") or []
    imdb_id = imdb_list[0] if isinstance(imdb_list, list) and imdb_list else None

    return {"anilist_id": anilist_id, "mal_id": mal_id, "tmdb_id": tmdb_id,
            "tmdb_kind": tmdb_kind, "imdb_id": imdb_id}


def resolve_ids_for_mal(
    index: dict[int, list[dict]], mal_id: int
) -> dict[str, int | str | None]:
    """Resolve tvdb_id, anilist_id, and imdb_id from a MAL ID via Fribb.

    Returns a dict with keys ``tvdb_id``, ``anilist_id``, ``imdb_id``
    (each nullable). Same "never guess" discipline as
    resolve_tvdb_id_for_anilist — if the mal_id maps to multiple
    conflicting tvdb_ids, tvdb_id is None; likewise for anilist_id."""
    candidates = index.get(mal_id, [])
    if not candidates:
        return {"tvdb_id": None, "anilist_id": None, "imdb_id": None}
    tvdb_ids = {c["tvdb_id"] for c in candidates if c.get("tvdb_id") not in _MISSING}
    tvdb_id = tvdb_ids.pop() if len(tvdb_ids) == 1 else None
    anilist_ids = {c["anilist_id"] for c in candidates if c.get("anilist_id") not in _MISSING}
    anilist_id = anilist_ids.pop() if len(anilist_ids) == 1 else None
    # imdb_id: Fribb stores as a list; take first from the first candidate
    imdb_list = candidates[0].get("imdb_id") or []
    imdb_id = imdb_list[0] if isinstance(imdb_list, list) and imdb_list else None
    return {"tvdb_id": tvdb_id, "anilist_id": anilist_id, "imdb_id": imdb_id}


def resolve_season_candidate(
    index: dict[int, list[dict]], tvdb_id: int, season_number: int
) -> dict | None:
    """Disambiguates by season number when one tvdb_id has multiple
    dataset entries; returns None (never guesses) when that still
    doesn't narrow it to exactly one. Verbatim port of Data's own
    `resolve_anilist_id` (mapping.py) other than returning the whole
    candidate dict, so the caller can also read `mal_id` off it."""
    candidates = index.get(tvdb_id, [])
    if not candidates:
        return None
    if len(candidates) == 1:
        # Deliberately does NOT check the single candidate's season tag
        # against season_number — see mapping.py's own comment (Data,
        # forked from aniq) for the full history: tried live 2026-07-09
        # to fix a real regression (Clevatess), reverted the same day
        # after real-library data showed it caused 28 series/season
        # combos to regress to unresolved, far more than it fixed. A
        # season-tagged single candidate very often IS the right match
        # for another season number too (specials mapping to the main
        # entry, or a show one source splits into two seasons that
        # AniList tracks as one continuous entry). Preserved verbatim.
        return candidates[0]
    season_matches = [c for c in candidates if c.get("season", {}).get("tvdb") == season_number]
    if len(season_matches) == 1:
        return season_matches[0]
    return None


def enumerate_real_seasons(candidates: list[dict]) -> list[dict] | None:
    """The real, ordered list of a show's seasons per Fribb — every
    non-special, non-movie candidate for a tvdb_id, sorted by
    `(season.tvdb, episode_offset.tvdb)` (release order within a
    split-cour franchise, e.g. SPY×FAMILY's "Part I"/"Part II" both
    tagged `season.tvdb: 1`, distinguished only by `episode_offset`).

    2026-09-21 — factored out of `identity_mismatch._resolve_by_position`
    (which only *verified* an existing LCARS season's `anilist_id`
    against this ordering) so `season_ranges.ensure_fribb_season_rows`
    can use the exact same ordering to *create* the LCARS season rows
    in the first place. Necessary because `season_ranges.
    ensure_all_season_rows` only ever creates a season row reactively,
    from `episode.season` values Sonarr already synced — a real Fribb
    season with no synced episodes yet (found live: SPY×FAMILY's real
    "Season 2", anilist 158927, never got an LCARS row at all) never
    gets created, and no amount of smarter *matching* logic fixes a
    season that was never created. This is the shared, single source
    of truth for "how many real seasons does this show have, in what
    order" that both season-row creation and season-identity
    verification must agree on.

    Returns `None` on an ambiguous tie (two candidates landing on the
    same sort key) — same "never guess" convention as
    `resolve_season_candidate`."""
    numbered = [
        c
        for c in candidates
        if c.get("type") not in ("SPECIAL", "MOVIE") and (c.get("season") or {}).get("tvdb", 0) > 0
    ]

    def sort_key(c: dict) -> tuple[int, int]:
        season_tvdb = c["season"]["tvdb"]
        offset = (c.get("episode_offset") or {}).get("tvdb") or 0
        return (season_tvdb, offset)

    numbered.sort(key=sort_key)
    keys = [sort_key(c) for c in numbered]
    if len(keys) != len(set(keys)):
        return None  # two candidates share a sort position — genuinely ambiguous, don't guess
    return numbered


def extract_ids(candidate: dict | None) -> tuple[int | None, int | None]:
    """(anilist_id, mal_id) from a resolved candidate, or (None, None)
    if there wasn't one — both nullable on `season` (§5.5)."""
    if candidate is None:
        return None, None
    anilist_id = candidate.get("anilist_id")
    mal_id = candidate.get("mal_id")
    anilist_id = None if anilist_id in _MISSING else anilist_id
    mal_id = None if mal_id in _MISSING else mal_id
    return anilist_id, mal_id
