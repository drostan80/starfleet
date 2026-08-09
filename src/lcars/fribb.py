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
