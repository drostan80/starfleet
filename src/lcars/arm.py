"""ARM (AnimeID Relational Mapping) dataset — AniList↔MAL↔Syoboi bridge.

Kawaiioverflow's ARM maps between AniList, MAL, Annict, and Syoboi
Calendar IDs.  The JSON dump is small (~2 MB, ~37k entries) and
cached weekly like Fribb/Wikidata.

Primary use: AniList → Syoboi Calendar TID bridging for accurate
anime broadcast times.  Also carries MAL and Annict IDs but AniList
is the only useful bridge for LCARS's tracked shows.
"""

import json
import logging
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

DATASET_URL = (
    "https://raw.githubusercontent.com/kawaiioverflow/arm/master/arm.json"
)

CACHE_PATH = (
    Path.home()
    / ".local"
    / "share"
    / "starfleet"
    / "lcars"
    / "arm.json"
)

MAX_AGE_SECONDS = 7 * 24 * 3600  # weekly, same as Fribb/Wikidata

# In-process caches, keyed on file mtime like Fribb.
_parse_cache: dict[tuple[str, int], list[dict]] = {}
_al_to_syoboi_cache: dict[int, dict[int, int]] = {}


def _dataset_is_stale(path: Path | None = None,
                      max_age: float = MAX_AGE_SECONDS) -> bool:
    if path is None:
        path = CACHE_PATH
    return not path.exists() or time.time() - path.stat().st_mtime > max_age


def load_dataset(*, force_refresh: bool = False) -> list[dict]:
    """Load ARM JSON, downloading if stale or missing.

    Returns a list of dicts, each with optional keys:
      anilist_id, mal_id, annict_id, syobocal_tid
    """
    if not force_refresh and CACHE_PATH.exists():
        key = (str(CACHE_PATH), int(CACHE_PATH.stat().st_mtime))
        cached = _parse_cache.get(key)
        if cached is not None:
            return cached

    if force_refresh or _dataset_is_stale():
        log.info("ARM: downloading dataset from %s", DATASET_URL)
        resp = httpx.get(DATASET_URL, timeout=30.0, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
        log.info("ARM: cached %d entries", len(data))
    else:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))

    key = (str(CACHE_PATH), int(CACHE_PATH.stat().st_mtime))
    _parse_cache.clear()
    _parse_cache[key] = data
    return data


def build_anilist_to_syoboi_index(dataset: list[dict]) -> dict[int, int]:
    """Build an AniList ID → Syoboi Calendar TID index.

    Returns {anilist_id: syobocal_tid} for all entries that have both.
    """
    cached = _al_to_syoboi_cache.get(id(dataset))
    if cached is not None:
        return cached

    index = {}
    for entry in dataset:
        al_id = entry.get("anilist_id")
        tid = entry.get("syobocal_tid")
        if al_id and tid:
            index[al_id] = tid

    _al_to_syoboi_cache.clear()
    _al_to_syoboi_cache[id(dataset)] = index
    log.info("ARM: built AniList→Syoboi index (%d entries)", len(index))
    return index


def seed_syoboi_external_ids(conn, dataset: list[dict]) -> int:
    """Populate show_external_id with service='syoboi' from ARM bridge.

    Uses ARM's anilist_id → syobocal_tid mapping to link tracked shows.
    Returns the number of new rows inserted.
    """
    al_to_syoboi = build_anilist_to_syoboi_index(dataset)

    rows = conn.execute(
        """SELECT sei.show_id, sei.external_id
           FROM show_external_id sei
           JOIN show s ON sei.show_id = s.id
           WHERE sei.service = 'anilist'
             AND s.tracked = 1
             AND s.tracking_space = 'anime'
             AND NOT EXISTS (
               SELECT 1 FROM show_external_id sei2
               WHERE sei2.show_id = sei.show_id
                 AND sei2.service = 'syoboi'
             )"""
    ).fetchall()

    inserted = 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for show_id, anilist_id_str in rows:
        anilist_id = int(anilist_id_str)
        tid = al_to_syoboi.get(anilist_id)
        if tid is None:
            continue
        try:
            conn.execute(
                """INSERT INTO show_external_id
                   (show_id, service, external_id, url, created_at)
                   VALUES (?, 'syoboi', ?, ?, ?)""",
                (
                    show_id,
                    str(tid),
                    f"https://cal.syoboi.jp/tid/{tid}",
                    now,
                ),
            )
            inserted += 1
        except Exception:
            log.debug("syoboi external_id already exists for %s", show_id)
            continue

    conn.commit()
    if inserted:
        log.info("ARM: seeded %d Syoboi TIDs", inserted)
    return inserted
