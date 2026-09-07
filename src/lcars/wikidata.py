"""Wikidata SPARQL bridge — TV series cross-database ID lookup.

Downloads the TVDB↔TMDB↔IMDB bridge from Wikidata's free SPARQL
endpoint and caches it as compact JSON (weekly refresh, same cadence
as Fribb/Anime-Lists).  ~46k TV series, ~12 MB compressed.

This is the TV/movie backbone of Memory Alpha, analogous to how
Anime-Lists XML + Fribb JSON serve the anime side.

Wikidata properties:
  P4835 = TheTVDB series ID
  P4983 = TMDB TV series ID
  P345  = IMDb ID
"""

import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

# TV series query — requires TVDB (that's the gap we're filling),
# optionally pulls TMDB and IMDB.
TV_SERIES_QUERY = """\
SELECT ?tvdb ?tmdb ?imdb WHERE {
  ?item wdt:P31/wdt:P279* wd:Q5398426 .
  ?item wdt:P4835 ?tvdb .
  OPTIONAL { ?item wdt:P4983 ?tmdb }
  OPTIONAL { ?item wdt:P345 ?imdb }
}"""

CACHE_PATH = (
    Path.home()
    / ".local"
    / "share"
    / "starfleet"
    / "lcars"
    / "wikidata-tv-bridge.json"
)

MAX_AGE_SECONDS = 7 * 24 * 3600  # weekly, same as Fribb

# User-Agent per Wikidata policy — repo URL, not personal email.
USER_AGENT = "LCARS-Starfleet/0.1 (https://github.com/drostan/starfleet)"

# In-process index caches, keyed on file mtime like Fribb.
_parse_cache: dict[tuple[str, int], list[dict]] = {}
_tmdb_index_cache: dict[int, dict[str, str]] = {}
_imdb_index_cache: dict[int, dict[str, str]] = {}


def _dataset_is_stale(path: Path, max_age: float) -> bool:
    return not path.exists() or time.time() - path.stat().st_mtime > max_age


def load_dataset(
    *,
    cache_path: Path = CACHE_PATH,
    max_age: float = MAX_AGE_SECONDS,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Download (or use cached) the Wikidata TV bridge.

    Returns a list of compact dicts: {"tvdb": "...", "tmdb": "...", "imdb": "..."}
    where tmdb/imdb may be None.
    """
    if not _dataset_is_stale(cache_path, max_age):
        return _read_cached(cache_path)

    owns_client = client is None
    client = client or httpx.Client(timeout=120.0)
    try:
        response = client.get(
            SPARQL_ENDPOINT,
            params={"query": TV_SERIES_QUERY, "format": "json"},
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        raw = response.json()
    except httpx.HTTPError:
        if cache_path.exists():
            log.warning("Wikidata SPARQL fetch failed, using stale cache")
            return _read_cached(cache_path)
        raise
    finally:
        if owns_client:
            client.close()

    # Normalize SPARQL bindings to compact records
    records = _normalize_bindings(raw)
    log.info("Wikidata TV bridge: %d records fetched", len(records))

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(records))
    return records


def _normalize_bindings(raw: dict) -> list[dict]:
    """Convert SPARQL JSON response to compact [{tvdb, tmdb, imdb}]."""
    records = []
    for binding in raw.get("results", {}).get("bindings", []):
        tvdb = binding.get("tvdb", {}).get("value")
        if not tvdb:
            continue
        tmdb = binding.get("tmdb", {}).get("value")
        imdb = binding.get("imdb", {}).get("value")
        records.append({
            "tvdb": str(tvdb),
            "tmdb": str(tmdb) if tmdb else None,
            "imdb": str(imdb) if imdb else None,
        })
    return records


def _read_cached(cache_path: Path) -> list[dict]:
    """Parse-once-per-file-version, same memoization as Fribb."""
    key = (str(cache_path), cache_path.stat().st_mtime_ns)
    cached = _parse_cache.get(key)
    if cached is None:
        cached = json.loads(cache_path.read_text())
        _parse_cache.clear()
        _parse_cache[key] = cached
    return cached


def build_tmdb_to_tvdb_index(dataset: list[dict]) -> dict[str, str]:
    """Index: TMDB ID (str) → TVDB ID (str). Memoized on dataset identity."""
    cached = _tmdb_index_cache.get(id(dataset))
    if cached is not None:
        return cached
    index: dict[str, str] = {}
    for rec in dataset:
        if rec.get("tmdb") and rec.get("tvdb"):
            # First-write-wins — duplicates are rare in Wikidata
            index.setdefault(rec["tmdb"], rec["tvdb"])
    _tmdb_index_cache.clear()
    _tmdb_index_cache[id(dataset)] = index
    return index


def build_imdb_to_tvdb_index(dataset: list[dict]) -> dict[str, str]:
    """Index: IMDB ID (str) → TVDB ID (str). Memoized on dataset identity."""
    cached = _imdb_index_cache.get(id(dataset))
    if cached is not None:
        return cached
    index: dict[str, str] = {}
    for rec in dataset:
        if rec.get("imdb") and rec.get("tvdb"):
            index.setdefault(rec["imdb"], rec["tvdb"])
    _imdb_index_cache.clear()
    _imdb_index_cache[id(dataset)] = index
    return index
