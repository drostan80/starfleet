"""Anime-Lists (ScudLee) dataset — AniDB ↔ TVDB episode offset mapping.

Downloads and caches the community-maintained XML from
github.com/Anime-Lists/anime-lists. Contains AniDB→TVDB season mapping,
episode offsets, and per-episode overrides for 10,736+ anime entries.

Same caching pattern as fribb.py — weekly refresh, stale-cache fallback.
"""

import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

DATASET_URL = (
    "https://raw.githubusercontent.com/Anime-Lists/anime-lists"
    "/master/anime-list.xml"
)

DATASET_CACHE_PATH = (
    Path.home()
    / ".local"
    / "share"
    / "starfleet"
    / "lcars"
    / "anime-lists.xml"
)

DATASET_MAX_AGE_SECONDS = 7 * 24 * 3600

_parse_cache: dict[tuple[str, int], ET.Element] = {}


def _dataset_is_stale(path: Path, max_age: float) -> bool:
    return not path.exists() or time.time() - path.stat().st_mtime > max_age


def load_xml(
    *,
    cache_path: Path = DATASET_CACHE_PATH,
    max_age: float = DATASET_MAX_AGE_SECONDS,
    client: httpx.Client | None = None,
) -> ET.Element:
    """Download (or use cached) Anime-Lists XML, return parsed root."""
    if not _dataset_is_stale(cache_path, max_age):
        return _read_cached(cache_path)

    owns_client = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        response = client.get(DATASET_URL)
        response.raise_for_status()
        raw = response.text
    except httpx.HTTPError:
        if cache_path.exists():
            return _read_cached(cache_path)
        raise
    finally:
        if owns_client:
            client.close()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(raw)
    return _parse_xml(cache_path, raw)


def _read_cached(cache_path: Path) -> ET.Element:
    key = (str(cache_path), cache_path.stat().st_mtime_ns)
    cached = _parse_cache.get(key)
    if cached is not None:
        return cached
    raw = cache_path.read_text()
    return _parse_xml(cache_path, raw)


def _parse_xml(cache_path: Path, raw: str) -> ET.Element:
    root = ET.fromstring(raw)
    key = (str(cache_path), cache_path.stat().st_mtime_ns)
    _parse_cache.clear()
    _parse_cache[key] = root
    return root


def parse_entries(root: ET.Element) -> list[dict]:
    """Extract all <anime> entries from the XML root.

    Returns a list of dicts, each with:
      anidb_id, tvdb_id, default_tvdb_season, episode_offset,
      tmdb_tv, tmdb_season, tmdb_movie, imdb_id, name, mappings
    """
    entries = []
    for anime in root.findall("anime"):
        anidb_id_raw = anime.get("anidbid")
        if not anidb_id_raw or not anidb_id_raw.isdigit():
            continue

        entry = {
            "anidb_id": int(anidb_id_raw),
            "tvdb_id": anime.get("tvdbid"),
            "default_tvdb_season": _int_or_none(anime.get("defaulttvdbseason")),
            "episode_offset": int(anime.get("episodeoffset", "0")),
            "tmdb_tv": _int_or_none(anime.get("tmdbtv")),
            "tmdb_season": _int_or_none(anime.get("tmdbseason")),
            "tmdb_movie": _int_or_none(anime.get("tmdbid")),
            "imdb_id": anime.get("imdbid"),
            "name": None,
            "mappings": [],
        }

        name_el = anime.find("name")
        if name_el is not None:
            entry["name"] = name_el.text

        mapping_list = anime.find("mapping-list")
        if mapping_list is not None:
            for m in mapping_list.findall("mapping"):
                mapping = {
                    "anidb_season": _int_or_none(m.get("anidbseason")),
                    "tvdb_season": _int_or_none(m.get("tvdbseason")),
                    "start": _int_or_none(m.get("start")),
                    "end": _int_or_none(m.get("end")),
                    "offset": _int_or_none(m.get("offset")),
                    "episode_map": (m.text or "").strip() or None,
                }
                entry["mappings"].append(mapping)

        entries.append(entry)

    return entries


def _int_or_none(val: str | None) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except ValueError:
        return None


def ingest_to_db(conn, entries: list[dict], fetched_at: str) -> dict:
    """Write parsed Anime-Lists entries into the database.

    Returns {"inserted": n, "updated": n} counts.
    """
    inserted = 0
    updated = 0

    for entry in entries:
        # Upsert anime_list_entry
        existing = conn.execute(
            "SELECT id FROM anime_list_entry WHERE anidb_id = ?",
            (entry["anidb_id"],),
        ).fetchone()

        if existing:
            entry_id = existing[0]
            conn.execute(
                """UPDATE anime_list_entry
                   SET tvdb_id = ?, default_tvdb_season = ?,
                       episode_offset = ?, tmdb_tv = ?, tmdb_season = ?,
                       tmdb_movie = ?, imdb_id = ?, name = ?,
                       fetched_at = ?
                   WHERE id = ?""",
                (
                    entry["tvdb_id"],
                    entry["default_tvdb_season"],
                    entry["episode_offset"],
                    entry["tmdb_tv"],
                    entry["tmdb_season"],
                    entry["tmdb_movie"],
                    entry["imdb_id"],
                    entry["name"],
                    fetched_at,
                    entry_id,
                ),
            )
            # Clear old mappings and re-insert
            conn.execute(
                "DELETE FROM anime_list_mapping WHERE entry_id = ?",
                (entry_id,),
            )
            updated += 1
        else:
            cursor = conn.execute(
                """INSERT INTO anime_list_entry
                   (anidb_id, tvdb_id, default_tvdb_season, episode_offset,
                    tmdb_tv, tmdb_season, tmdb_movie, imdb_id, name,
                    fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry["anidb_id"],
                    entry["tvdb_id"],
                    entry["default_tvdb_season"],
                    entry["episode_offset"],
                    entry["tmdb_tv"],
                    entry["tmdb_season"],
                    entry["tmdb_movie"],
                    entry["imdb_id"],
                    entry["name"],
                    fetched_at,
                ),
            )
            entry_id = cursor.lastrowid
            inserted += 1

        # Insert mappings
        for m in entry["mappings"]:
            conn.execute(
                """INSERT INTO anime_list_mapping
                   (entry_id, anidb_season, tvdb_season, start, "end",
                    offset, episode_map)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry_id,
                    m["anidb_season"],
                    m["tvdb_season"],
                    m["start"],
                    m["end"],
                    m["offset"],
                    m["episode_map"],
                ),
            )

    conn.commit()
    return {"inserted": inserted, "updated": updated}
