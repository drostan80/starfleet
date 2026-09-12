"""AniDB data — titles dump parser and HTTP API client.

The titles dump (anime-titles.dat.gz) is a free daily export of all
AniDB anime entries with titles in every available language. No API
registration required.

The HTTP API (client=memalpha) returns per-episode data: titles in
all languages, airdates (date-only), and lengths. Rate-limited to
1 request per 2 seconds; errors arrive as <error> XML with HTTP 200.

Format (titles dump): one line per title, tab-separated:
  aid|type|language|title
  type: 1=primary, 2=synonym, 3=short, 4=official, 5=kana reading
  language: x-jat=romaji, ja=Japanese, en=English, etc.
  Lines starting with # are comments.
"""

import gzip
import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TITLES_DUMP_URL = "https://anidb.net/api/anime-titles.dat.gz"

TITLES_CACHE_PATH = (
    Path.home()
    / ".local"
    / "share"
    / "starfleet"
    / "lcars"
    / "anime-titles.dat.gz"
)

# Daily dump, but we only refresh weekly (same cadence as Fribb).
TITLES_MAX_AGE_SECONDS = 7 * 24 * 3600

# Title type codes from the dump format.
TITLE_PRIMARY = 1
TITLE_SYNONYM = 2
TITLE_SHORT = 3
TITLE_OFFICIAL = 4
TITLE_KANA = 5


def _dataset_is_stale(path: Path, max_age: float) -> bool:
    return not path.exists() or time.time() - path.stat().st_mtime > max_age


def load_titles(
    *,
    cache_path: Path = TITLES_CACHE_PATH,
    max_age: float = TITLES_MAX_AGE_SECONDS,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Download (or use cached) the AniDB titles dump.

    Returns a list of dicts:
      {"anidb_id": int, "title_type": int, "lang": str, "title": str}
    """
    if not _dataset_is_stale(cache_path, max_age):
        return _parse_dump(cache_path)

    owns_client = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        response = client.get(TITLES_DUMP_URL)
        response.raise_for_status()
        raw_gz = response.content
    except httpx.HTTPError:
        if cache_path.exists():
            return _parse_dump(cache_path)
        raise
    finally:
        if owns_client:
            client.close()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(raw_gz)
    return _parse_dump(cache_path)


def _parse_dump(cache_path: Path) -> list[dict]:
    """Parse the gzipped titles dump into a list of title dicts."""
    titles = []
    with gzip.open(cache_path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) != 4:
                continue
            try:
                anidb_id = int(parts[0])
                title_type = int(parts[1])
            except ValueError:
                continue
            titles.append(
                {
                    "anidb_id": anidb_id,
                    "title_type": title_type,
                    "lang": parts[2],
                    "title": parts[3],
                }
            )
    return titles


def extract_anime_entries(titles: list[dict]) -> list[dict]:
    """Derive unique anime entries from the titles list.

    Returns one dict per anidb_id with the primary (type=1) title as
    main_title. If no primary title exists, uses the first official
    English title, then falls back to the first title found.
    """
    by_id: dict[int, list[dict]] = {}
    for t in titles:
        by_id.setdefault(t["anidb_id"], []).append(t)

    entries = []
    for anidb_id, id_titles in by_id.items():
        # Pick main title: primary (1) > official en (4+en) > first
        main = None
        for t in id_titles:
            if t["title_type"] == TITLE_PRIMARY:
                main = t["title"]
                break
        if main is None:
            for t in id_titles:
                if t["title_type"] == TITLE_OFFICIAL and t["lang"] == "en":
                    main = t["title"]
                    break
        if main is None:
            main = id_titles[0]["title"]

        entries.append({"anidb_id": anidb_id, "main_title": main})

    return entries


def ingest_to_db(conn, titles: list[dict], anime_entries: list[dict],
                 fetched_at: str) -> dict:
    """Write AniDB anime entries and titles into the database.

    Returns {"anime_count": n, "title_count": n} counts.
    """
    # Batch-insert anime entries
    anime_count = 0
    for entry in anime_entries:
        conn.execute(
            """INSERT INTO anidb_anime (anidb_id, main_title, fetched_at)
               VALUES (?, ?, ?)
               ON CONFLICT (anidb_id) DO UPDATE SET
                 main_title = excluded.main_title,
                 fetched_at = excluded.fetched_at""",
            (entry["anidb_id"], entry["main_title"], fetched_at),
        )
        anime_count += 1

    # Clear and re-insert all titles (simpler than diffing ~500k rows)
    conn.execute("DELETE FROM anidb_title")
    title_count = 0
    batch = []
    for t in titles:
        batch.append(
            (t["anidb_id"], t["title"], t["lang"], t["title_type"])
        )
        if len(batch) >= 5000:
            conn.executemany(
                """INSERT OR IGNORE INTO anidb_title
                   (anidb_id, title, lang, title_type)
                   VALUES (?, ?, ?, ?)""",
                batch,
            )
            title_count += len(batch)
            batch = []
    if batch:
        conn.executemany(
            """INSERT OR IGNORE INTO anidb_title
               (anidb_id, title, lang, title_type)
               VALUES (?, ?, ?, ?)""",
            batch,
        )
        title_count += len(batch)

    conn.commit()
    return {"anime_count": anime_count, "title_count": title_count}


def seed_anidb_external_ids(conn, fribb_dataset: list[dict]) -> int:
    """Populate show_external_id with service='anidb' from Fribb bridge.

    Uses Fribb's anilist_id → anidb_id mapping to link tracked shows.
    Returns the number of new rows inserted.
    """
    # Build anilist→anidb map from Fribb
    al_to_anidb = {}
    for entry in fribb_dataset:
        al_id = entry.get("anilist_id")
        adb_id = entry.get("anidb_id")
        if al_id and adb_id:
            al_to_anidb[al_id] = adb_id

    # Get tracked shows with AniList IDs but no AniDB external ID
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
                 AND sei2.service = 'anidb'
             )"""
    ).fetchall()

    inserted = 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for show_id, anilist_id_str in rows:
        anilist_id = int(anilist_id_str)
        anidb_id = al_to_anidb.get(anilist_id)
        if anidb_id is None:
            continue
        try:
            conn.execute(
                """INSERT INTO show_external_id
                   (show_id, service, external_id, url, created_at)
                   VALUES (?, 'anidb', ?, ?, ?)""",
                (
                    show_id,
                    str(anidb_id),
                    f"https://anidb.net/anime/{anidb_id}",
                    now,
                ),
            )
            inserted += 1
        except Exception:
            # UNIQUE constraint violation — already exists somehow
            log.debug("anidb external_id already exists for %s", show_id)
            continue

    conn.commit()
    return inserted


def propagate_cross_ids(conn, fribb_dataset: list[dict],
                        wikidata_dataset: list[dict] | None = None) -> dict:
    """Propagate known cross-database IDs into show_external_id.

    Full-graph propagation — every database has at least one path to
    every other.  The edges, in execution order:

    ── Anime (tracking_space = 'anime') ──

    Phase 1 — TVDB→AniDB (anime_list_entry reverse):
      For shows that enter via Sonarr with only a TVDB ID, reverse-
      lookup anime_list_entry.tvdb_id → anidb_id.  This seeds the
      AniDB row so phase 2 can cascade through Fribb.

    Phase 2a — AniList→MAL/TMDB/IMDB (Fribb, keyed by anilist_id):
      The primary path for anime already in the system.

    Phase 2b — AniDB→AniList/MAL/TMDB/IMDB (Fribb, keyed by anidb_id):
      Fallback for anime with AniDB but no AniList (e.g. after phase 1
      seeded AniDB from TVDB).  Catches the Sonarr-add scenario.

    Phase 3 — AniDB→TVDB/TMDB/IMDB (anime_list_entry):
      TVDB: existing edge.  TMDB/IMDB: new — anime-lists carries
      tmdb_tv and imdb_id alongside the TVDB mapping.

    ── TV/Movie (tracking_space != 'anime') ──

    Phase 4 — TMDB/IMDB→TVDB (Wikidata SPARQL bridge):
      Existing edge for non-anime shows.

    ── Cross-phase cascade ──

    TVmaze is intentionally NOT a source/target here — TVmaze externals
    aren't stored locally, so propagation from TVmaze requires a live API
    call, which the tvmaze drip-fetch already does at lookup time
    (backfilling TVDB/IMDB/TMDB from TVmaze's externals dict). The
    bidirectional loop closes via pipeline ordering: this function fills
    TVDB/IMDB → next tick's drip finds shows by those IDs → TVmaze
    lookup backfills the reverse direction.

    Strictly insert-only — never overwrites existing rows, so manual
    corrections via amendShowArrLink are preserved.

    Returns counts per service of newly inserted rows.
    """
    import sqlite3

    from lcars import fribb as fribb_mod

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    counts = {"anidb": 0, "anilist": 0, "mal": 0,
              "tvdb": 0, "tmdb": 0, "imdb": 0}

    def _insert(show_id, service, ext_id, url):
        try:
            conn.execute(
                """INSERT INTO show_external_id
                   (show_id, service, external_id, url, created_at)
                   SELECT ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (
                     SELECT 1 FROM show_external_id
                     WHERE show_id = ? AND service = ?
                   )""",
                (show_id, service, str(ext_id), url, now,
                 show_id, service),
            )
            return conn.total_changes
        except sqlite3.IntegrityError:
            return 0

    # ── Build Fribb indexes ──
    al_index: dict[int, dict] = {}
    for entry in fribb_dataset:
        al_id = entry.get("anilist_id")
        if al_id:
            al_index[al_id] = entry

    anidb_index = fribb_mod.build_anidb_index(fribb_dataset)

    # ── Phase 1: TVDB→AniDB (anime_list_entry reverse) ──
    # Anime shows with TVDB but no AniDB — the Sonarr-add scenario.
    tvdb_only_anime = conn.execute(
        """SELECT s.id, tvdb.external_id AS tvdb_id
           FROM show s
           JOIN show_external_id tvdb
             ON tvdb.show_id = s.id AND tvdb.service = 'tvdb'
           WHERE s.tracked = 1 AND s.tracking_space = 'anime'
             AND NOT EXISTS (
               SELECT 1 FROM show_external_id t
               WHERE t.show_id = s.id AND t.service = 'anidb'
             )"""
    ).fetchall()

    for show_id, tvdb_id_str in tvdb_only_anime:
        # One TVDB can map to many AniDB entries (season splits).
        # "Never guess" discipline: only seed when exactly one distinct
        # anidb_id exists for this TVDB — ambiguous mappings are skipped
        # rather than risk cascading a wrong ID through Fribb.
        ale_rows = conn.execute(
            """SELECT DISTINCT anidb_id FROM anime_list_entry
               WHERE tvdb_id = ?""",
            (tvdb_id_str,),
        ).fetchall()
        if len(ale_rows) == 1 and ale_rows[0][0]:
            anidb_id = ale_rows[0][0]
            before = conn.total_changes
            _insert(show_id, "anidb", anidb_id,
                    f"https://anidb.net/anime/{anidb_id}")
            if conn.total_changes > before:
                counts["anidb"] += 1

    # ── Phase 2: Fribb-based propagation for all anime ──
    # Re-query to pick up any AniDB rows just seeded in phase 1.
    shows = conn.execute(
        """SELECT s.id, s.media_shape,
                  al.external_id AS anilist_id,
                  adb.external_id AS anidb_id
           FROM show s
           LEFT JOIN show_external_id al
             ON al.show_id = s.id AND al.service = 'anilist'
           LEFT JOIN show_external_id adb
             ON adb.show_id = s.id AND adb.service = 'anidb'
           WHERE s.tracked = 1 AND s.tracking_space = 'anime'
             AND (al.external_id IS NOT NULL
                  OR adb.external_id IS NOT NULL)"""
    ).fetchall()

    for show_id, _media_shape, anilist_id_str, anidb_id_str in shows:
        # Phase 2a: AniList-rooted Fribb lookup (primary path)
        fribb = None
        if anilist_id_str:
            anilist_id = int(anilist_id_str)
            fribb = al_index.get(anilist_id)

        if fribb:
            # ── MAL ──
            if fribb.get("mal_id"):
                mal_id = fribb["mal_id"]
                before = conn.total_changes
                _insert(show_id, "mal", mal_id,
                        f"https://myanimelist.net/anime/{mal_id}")
                if conn.total_changes > before:
                    counts["mal"] += 1

            # ── TMDB ──
            if fribb.get("themoviedb_id"):
                tmdb = fribb["themoviedb_id"]
                tmdb_id = tmdb.get("tv") or tmdb.get("movie")
                if tmdb_id:
                    kind = "tv" if tmdb.get("tv") else "movie"
                    url = f"https://www.themoviedb.org/{kind}/{tmdb_id}"
                    before = conn.total_changes
                    _insert(show_id, "tmdb", tmdb_id, url)
                    if conn.total_changes > before:
                        counts["tmdb"] += 1

            # ── IMDB ──
            imdb_ids = fribb.get("imdb_id") or []
            if isinstance(imdb_ids, list) and imdb_ids:
                imdb_id = imdb_ids[0]
                before = conn.total_changes
                _insert(show_id, "imdb", imdb_id,
                        f"https://www.imdb.com/title/{imdb_id}/")
                if conn.total_changes > before:
                    counts["imdb"] += 1

        # Phase 2b: AniDB-rooted Fribb gap-fill (runs alongside 2a, not
        # as a fallback — _insert is insert-only, so running both is safe
        # and catches cases where 2a's Fribb entry is sparse or missing
        # some IDs that the AniDB-keyed entry has).
        if anidb_id_str:
            resolved = fribb_mod.resolve_ids_for_anidb(
                anidb_index, int(anidb_id_str))

            if resolved["anilist_id"]:
                before = conn.total_changes
                _insert(show_id, "anilist", resolved["anilist_id"],
                        f"https://anilist.co/anime/{resolved['anilist_id']}")
                if conn.total_changes > before:
                    counts["anilist"] += 1

            if resolved["mal_id"]:
                before = conn.total_changes
                _insert(show_id, "mal", resolved["mal_id"],
                        f"https://myanimelist.net/anime/{resolved['mal_id']}")
                if conn.total_changes > before:
                    counts["mal"] += 1

            if resolved["tmdb_id"]:
                kind = resolved["tmdb_kind"] or "tv"
                before = conn.total_changes
                _insert(show_id, "tmdb", resolved["tmdb_id"],
                        f"https://www.themoviedb.org/{kind}/{resolved['tmdb_id']}")
                if conn.total_changes > before:
                    counts["tmdb"] += 1

            if resolved["imdb_id"]:
                before = conn.total_changes
                _insert(show_id, "imdb", resolved["imdb_id"],
                        f"https://www.imdb.com/title/{resolved['imdb_id']}/")
                if conn.total_changes > before:
                    counts["imdb"] += 1

        # ── Phase 3: anime_list_entry → TVDB / TMDB / IMDB ──
        if anidb_id_str:
            ale_row = conn.execute(
                """SELECT tvdb_id, tmdb_tv, imdb_id FROM anime_list_entry
                   WHERE anidb_id = ?""",
                (int(anidb_id_str),),
            ).fetchone()
            if ale_row:
                ale_tvdb, ale_tmdb, ale_imdb = ale_row

                # TVDB
                if ale_tvdb and str(ale_tvdb).isdigit():
                    before = conn.total_changes
                    _insert(show_id, "tvdb", ale_tvdb,
                            f"https://thetvdb.com/dereferrer/series/{ale_tvdb}")
                    if conn.total_changes > before:
                        counts["tvdb"] += 1

                # TMDB (from anime-lists tmdb_tv)
                if ale_tmdb:
                    before = conn.total_changes
                    _insert(show_id, "tmdb", ale_tmdb,
                            f"https://www.themoviedb.org/tv/{ale_tmdb}")
                    if conn.total_changes > before:
                        counts["tmdb"] += 1

                # IMDB (from anime-lists imdb_id)
                if ale_imdb and str(ale_imdb).strip():
                    before = conn.total_changes
                    _insert(show_id, "imdb", ale_imdb,
                            f"https://www.imdb.com/title/{ale_imdb}/")
                    if conn.total_changes > before:
                        counts["imdb"] += 1

    # ── Phase 4: TV/movie TVDB propagation (from Wikidata bridge) ──
    if wikidata_dataset:
        from lcars import wikidata
        tmdb_to_tvdb = wikidata.build_tmdb_to_tvdb_index(wikidata_dataset)
        imdb_to_tvdb = wikidata.build_imdb_to_tvdb_index(wikidata_dataset)

        tv_shows = conn.execute(
            """SELECT s.id,
                      tmdb.external_id AS tmdb_id,
                      imdb.external_id AS imdb_id
               FROM show s
               LEFT JOIN show_external_id tmdb
                 ON tmdb.show_id = s.id AND tmdb.service = 'tmdb'
               LEFT JOIN show_external_id imdb
                 ON imdb.show_id = s.id AND imdb.service = 'imdb'
               WHERE s.tracked = 1
                 AND s.tracking_space != 'anime'
                 AND NOT EXISTS (
                   SELECT 1 FROM show_external_id t
                   WHERE t.show_id = s.id AND t.service = 'tvdb'
                 )
                 AND (tmdb.external_id IS NOT NULL
                      OR imdb.external_id IS NOT NULL)"""
        ).fetchall()

        for show_id, tmdb_id, imdb_id in tv_shows:
            tvdb_id = None
            if tmdb_id:
                tvdb_id = tmdb_to_tvdb.get(tmdb_id)
            if tvdb_id is None and imdb_id:
                tvdb_id = imdb_to_tvdb.get(imdb_id)
            if tvdb_id:
                before = conn.total_changes
                _insert(show_id, "tvdb", tvdb_id,
                        f"https://thetvdb.com/dereferrer/series/{tvdb_id}")
                if conn.total_changes > before:
                    counts["tvdb"] += 1

    conn.commit()
    return counts


# ---------------------------------------------------------------------------
# Title-matching search — find AniDB IDs for shows not in Fribb/Anime-Lists
# ---------------------------------------------------------------------------

def search_by_title(conn, query: str, *, limit: int = 10) -> list[dict]:
    """Search anidb_title for anime matching a title query.

    Searches across all languages and title types. Returns matches
    ranked by relevance:
      1. Exact match (case-insensitive)
      2. Starts-with match
      3. Contains match

    Each result includes the anidb_id, main_title, matched title/lang,
    whether an anime_list_entry exists, and a verification URL.
    """
    query_lower = query.lower().strip()
    if not query_lower:
        return []

    # Single query: LIKE is good enough for ~100k rows on SQLite
    rows = conn.execute(
        """SELECT DISTINCT t.anidb_id, t.title, t.lang, t.title_type,
                  a.main_title,
                  (SELECT 1 FROM anime_list_entry ale
                   WHERE ale.anidb_id = t.anidb_id) AS has_mapping
           FROM anidb_title t
           JOIN anidb_anime a ON t.anidb_id = a.anidb_id
           WHERE t.title LIKE ?
           LIMIT 200""",
        (f"%{query_lower}%",),
    ).fetchall()

    # Rank results
    scored = []
    seen_ids = set()
    for anidb_id, title, lang, title_type, main_title, has_mapping in rows:
        title_lower = title.lower()
        if title_lower == query_lower:
            score = 0  # exact
        elif title_lower.startswith(query_lower):
            score = 1  # starts-with
        else:
            score = 2  # contains

        # Prefer primary/official titles over synonyms/short
        if title_type in (TITLE_PRIMARY, TITLE_OFFICIAL):
            score -= 0.1

        # Prefer English/romaji matches
        if lang in ("en", "x-jat"):
            score -= 0.05

        # Deduplicate by anidb_id — keep best match
        if anidb_id in seen_ids:
            continue
        seen_ids.add(anidb_id)

        scored.append({
            "anidb_id": anidb_id,
            "main_title": main_title,
            "matched_title": title,
            "matched_lang": lang,
            "has_tvdb_mapping": bool(has_mapping),
            "url": f"https://anidb.net/anime/{anidb_id}",
            "search_url": f"https://anidb.net/anime/?adb.search={query}",
        })

    scored.sort(key=lambda x: (
        x["anidb_id"] not in seen_ids,  # always False, just for type
        0 if x["matched_title"].lower() == query_lower else
        1 if x["matched_title"].lower().startswith(query_lower) else 2,
    ))

    return scored[:limit]


def suggest_anidb_id(conn, show_id: str) -> list[dict]:
    """Suggest AniDB IDs for a show by matching its titles.

    Tries romaji, English, and native titles from the show row, plus
    any synonyms. Also tries base titles (before colon/subtitle) for
    better matching on sequels and specials. Returns deduplicated
    candidates from search_by_title.
    """
    show = conn.execute(
        """SELECT title_romaji, title_english, title_native
           FROM show WHERE id = ?""",
        (show_id,),
    ).fetchone()
    if not show:
        return []

    # Also check synonyms
    synonyms = conn.execute(
        "SELECT synonym FROM show_synonym WHERE show_id = ?",
        (show_id,),
    ).fetchall()

    # Build list of titles to try, including base titles (before colon)
    titles_to_try = []
    raw_titles = [t for t in show if t]
    raw_titles.extend(row[0] for row in synonyms if row[0])

    for title in raw_titles:
        titles_to_try.append(title)
        # Also try base title before colon/subtitle
        for sep in (":", " - "):
            if sep in title:
                base = title.split(sep)[0].strip()
                if len(base) >= 3 and base not in titles_to_try:
                    titles_to_try.append(base)

    # Search with each title variant, collect unique results
    candidates = {}
    for title in titles_to_try:
        results = search_by_title(conn, title, limit=5)
        for r in results:
            if r["anidb_id"] not in candidates:
                candidates[r["anidb_id"]] = r

    return list(candidates.values())[:10]


def link_anidb_manual(conn, show_id: str, anidb_id: int,
                      *, tvdb_id: str | None = None,
                      default_tvdb_season: int | None = None,
                      episode_offset: int = 0) -> dict:
    """Manually link a show to an AniDB ID.

    Creates:
      - show_external_id row (service='anidb')
      - anime_list_entry row (source='manual') if tvdb mapping provided

    Returns the created/updated records summary.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result = {"external_id": False, "anime_list_entry": False}

    # Add show_external_id
    existing = conn.execute(
        """SELECT 1 FROM show_external_id
           WHERE show_id = ? AND service = 'anidb'""",
        (show_id,),
    ).fetchone()

    if not existing:
        conn.execute(
            """INSERT INTO show_external_id
               (show_id, service, external_id, url, created_at)
               VALUES (?, 'anidb', ?, ?, ?)""",
            (show_id, str(anidb_id),
             f"https://anidb.net/anime/{anidb_id}", now),
        )
        result["external_id"] = True
    else:
        conn.execute(
            """UPDATE show_external_id
               SET external_id = ?, url = ?
               WHERE show_id = ? AND service = 'anidb'""",
            (str(anidb_id), f"https://anidb.net/anime/{anidb_id}",
             show_id),
        )
        result["external_id"] = True

    # Add anime_list_entry if TVDB mapping provided
    if tvdb_id is not None:
        existing_entry = conn.execute(
            "SELECT id FROM anime_list_entry WHERE anidb_id = ?",
            (anidb_id,),
        ).fetchone()

        if existing_entry:
            conn.execute(
                """UPDATE anime_list_entry
                   SET tvdb_id = ?, default_tvdb_season = ?,
                       episode_offset = ?, source = 'manual',
                       fetched_at = ?
                   WHERE anidb_id = ?""",
                (tvdb_id, default_tvdb_season, episode_offset,
                 now, anidb_id),
            )
        else:
            # Get anime name from anidb_anime if available
            name_row = conn.execute(
                "SELECT main_title FROM anidb_anime WHERE anidb_id = ?",
                (anidb_id,),
            ).fetchone()
            name = name_row[0] if name_row else None

            conn.execute(
                """INSERT INTO anime_list_entry
                   (anidb_id, tvdb_id, default_tvdb_season,
                    episode_offset, name, source, fetched_at)
                   VALUES (?, ?, ?, ?, ?, 'manual', ?)""",
                (anidb_id, tvdb_id, default_tvdb_season,
                 episode_offset, name, now),
            )
        result["anime_list_entry"] = True

    conn.commit()
    return result


# ---------------------------------------------------------------------------
# Episode offset derivation — compute AniDB absolute episode numbers
# ---------------------------------------------------------------------------

def _parse_episode_map(text: str) -> list[tuple[int, int]]:
    """Parse ';anidb_ep-tvdb_ep;...' into [(anidb_ep, tvdb_ep), ...].

    Pairs where tvdb_ep is 0 mean "unmapped" — we skip those.
    """
    pairs = []
    if not text:
        return pairs
    for chunk in text.strip().strip(";").split(";"):
        chunk = chunk.strip()
        if not chunk or "-" not in chunk:
            continue
        parts = chunk.split("-", 1)
        try:
            anidb_ep = int(parts[0])
            tvdb_ep = int(parts[1])
        except ValueError:
            continue
        if tvdb_ep != 0:  # 0 means unmapped/absent on TVDB
            pairs.append((anidb_ep, tvdb_ep))
    return pairs


class _SeasonResolver:
    """Resolves TVDB episodes → AniDB episode numbers for one TVDB show.

    Built from all anime_list_entries sharing a TVDB ID. Handles:
      - Individual episode maps (highest priority)
      - Range-based mapping overrides
      - Default entry-level offset (fallback)
    """

    def __init__(self, entries: list[dict]):
        # entries: list of {anidb_id, default_tvdb_season, episode_offset,
        #                    mappings: [{anidb_season, tvdb_season, start,
        #                                end, offset, episode_map}]}

        # Index entries by tvdb_season → list of entries
        self._season_entries: dict[int, list[dict]] = {}
        for e in entries:
            s = e.get("default_tvdb_season")
            if s is not None:
                self._season_entries.setdefault(s, []).append(e)

        # Detect ambiguous seasons (multiple entries, same season,
        # with overlapping offset=0 — can't tell which entry owns which ep)
        self._ambiguous_seasons: set[int] = set()
        for s, elist in self._season_entries.items():
            if len(elist) <= 1:
                continue
            offsets = {e["episode_offset"] for e in elist}
            if len(offsets) < len(elist):
                # Two entries with same offset on same season → ambiguous
                self._ambiguous_seasons.add(s)

        # Pre-parse episode maps and range mappings.
        # Episode maps are indexed globally (by tvdb_season) because a
        # mapping's tvdb_season can differ from the entry's default season
        # (e.g., regular eps mapped into specials or vice versa).
        # Ranges are indexed per-entry since they apply within the entry's
        # season context.
        # Global ep_map: (tvdb_season, tvdb_ep) → (anidb_id, anidb_season, anidb_epno)
        # First-write-wins: if two entries claim the same (tvdb_s, tvdb_ep),
        # keep the first and log a collision rather than silently overwriting.
        self._global_ep_map: dict[
            tuple[int, int], tuple[int, int, int]
        ] = {}
        self._ep_map_collisions: list[tuple] = []
        self._entry_ranges: dict[int, list[tuple]] = {}
        for e in entries:
            aid = e["anidb_id"]
            ranges = []
            for m in e.get("mappings", []):
                tvdb_s = m.get("tvdb_season")
                if tvdb_s is None:
                    continue
                anidb_s = m.get("anidb_season", 1)
                if m.get("episode_map"):
                    for anidb_ep, tvdb_ep in _parse_episode_map(
                        m["episode_map"]
                    ):
                        key = (tvdb_s, tvdb_ep)
                        if key in self._global_ep_map:
                            self._ep_map_collisions.append(
                                (key, self._global_ep_map[key],
                                 (aid, anidb_s, anidb_ep))
                            )
                        else:
                            self._global_ep_map[key] = (
                                aid, anidb_s, anidb_ep,
                            )
                if m.get("start") is not None and m.get("offset") is not None:
                    # Range in AniDB space → TVDB space
                    tvdb_start = m["start"] + m["offset"]
                    tvdb_end = m["end"] + m["offset"]
                    ranges.append((tvdb_s, tvdb_start, tvdb_end, m["offset"],
                                   aid, anidb_s))
            self._entry_ranges[aid] = ranges

    def resolve(
        self, tvdb_season: int, tvdb_ep: int
    ) -> tuple[int, int, int] | None:
        """Return (anidb_anime_id, anidb_season, anidb_epno) or None.

        anidb_season distinguishes regular episodes (1) from specials (0).
        """
        # Priority 1: individual episode maps (checked globally)
        hit = self._global_ep_map.get((tvdb_season, tvdb_ep))
        if hit is not None:
            return hit

        if tvdb_season in self._ambiguous_seasons:
            return None

        entries = self._season_entries.get(tvdb_season, [])
        if not entries:
            return None

        # Priority 2: range-based mappings
        for e in entries:
            for r_tvdb_s, r_start, r_end, r_offset, r_aid, r_as in (
                self._entry_ranges.get(e["anidb_id"], [])
            ):
                if r_tvdb_s == tvdb_season and r_start <= tvdb_ep <= r_end:
                    return (r_aid, r_as, tvdb_ep - r_offset)

        # Priority 3: default offset — pick the entry whose season matches
        if len(entries) == 1:
            e = entries[0]
            return (e["anidb_id"], 1, tvdb_ep - e["episode_offset"])

        # Multiple entries on same season with different offsets:
        # the offset partitions the range. Entry with offset O covers
        # TVDB eps > O (relative to previous entry's boundary).
        # Sort by offset ascending and pick the last one whose offset
        # boundary allows this episode.
        sorted_entries = sorted(entries, key=lambda x: x["episode_offset"])
        for e in reversed(sorted_entries):
            # Episode must be > offset (offset episodes belong to prev entry)
            if tvdb_ep > e["episode_offset"]:
                return (e["anidb_id"], 1, tvdb_ep - e["episode_offset"])

        # Fallback to first entry
        e = sorted_entries[0]
        return (e["anidb_id"], 1, tvdb_ep - e["episode_offset"])


def derive_episode_mappings(conn) -> dict:
    """Derive AniDB episode numbers for all tracked anime episodes.

    Walks every tracked anime show with an AniDB external ID, finds the
    matching Anime-Lists entry, resolves each regular episode to its AniDB
    absolute episode number, and writes the result to episode_anidb_mapping.

    Returns stats: {mapped, skipped, mismatches, shows_processed,
                    shows_skipped_no_entry}.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stats = {
        "mapped": 0,
        "skipped": 0,
        "mismatches": 0,
        "shows_processed": 0,
        "shows_skipped_no_entry": 0,
        "mismatch_details": [],
    }

    # Get tracked anime shows with AniDB external IDs
    shows = conn.execute(
        """SELECT s.id, sei.external_id
           FROM show s
           JOIN show_external_id sei ON sei.show_id = s.id
             AND sei.service = 'anidb'
           WHERE s.tracked = 1 AND s.tracking_space = 'anime'"""
    ).fetchall()

    # Cache: tvdb_id → list of anime_list_entry dicts (with mappings)
    _tvdb_cache: dict[str, list[dict]] = {}

    for show_id, anidb_id_str in shows:
        anidb_id = int(anidb_id_str)

        # Get the primary anime_list_entry for this anidb_id
        entry_row = conn.execute(
            """SELECT id, anidb_id, tvdb_id, default_tvdb_season,
                      episode_offset
               FROM anime_list_entry WHERE anidb_id = ?""",
            (anidb_id,),
        ).fetchone()
        if not entry_row:
            stats["shows_skipped_no_entry"] += 1
            continue

        tvdb_id = entry_row[2]
        if not tvdb_id or not tvdb_id.isdigit():
            stats["shows_skipped_no_entry"] += 1
            continue

        # Get all entries for this TVDB ID (for multi-season resolution)
        if tvdb_id not in _tvdb_cache:
            _tvdb_cache[tvdb_id] = _load_entries_for_tvdb(conn, tvdb_id)

        resolver = _SeasonResolver(_tvdb_cache[tvdb_id])

        # Get all regular episodes for this show
        episodes = conn.execute(
            """SELECT id, season, episode, absolute_number
               FROM episode
               WHERE show_id = ? AND kind = 'regular'""",
            (show_id,),
        ).fetchall()

        for ep_id, season, ep_num, existing_abs in episodes:
            result = resolver.resolve(season, ep_num)
            if result is None:
                stats["skipped"] += 1
                continue

            anidb_anime_id, anidb_season, anidb_epno = result
            confidence = "auto"

            # Write to mapping table
            conn.execute(
                """INSERT INTO episode_anidb_mapping
                   (episode_id, anidb_anime_id, anidb_season, anidb_epno,
                    confidence, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (episode_id) DO UPDATE SET
                     anidb_anime_id = excluded.anidb_anime_id,
                     anidb_season = excluded.anidb_season,
                     anidb_epno = excluded.anidb_epno,
                     confidence = excluded.confidence""",
                (ep_id, anidb_anime_id, anidb_season, anidb_epno,
                 confidence, now),
            )
            stats["mapped"] += 1

            # Compare with existing absolute_number (regulars only)
            if (
                anidb_season == 1
                and existing_abs is not None
                and int(existing_abs) != anidb_epno
            ):
                stats["mismatches"] += 1
                if len(stats["mismatch_details"]) < 50:
                    stats["mismatch_details"].append({
                        "show_id": show_id,
                        "episode_id": ep_id,
                        "season": season,
                        "episode": ep_num,
                        "existing_abs": existing_abs,
                        "derived_anidb": anidb_epno,
                        "anidb_anime_id": anidb_anime_id,
                    })

        stats["shows_processed"] += 1

    conn.commit()
    return stats


def _load_entries_for_tvdb(conn, tvdb_id: str) -> list[dict]:
    """Load all anime_list_entries for a TVDB ID, with their mappings."""
    entry_rows = conn.execute(
        """SELECT id, anidb_id, tvdb_id, default_tvdb_season, episode_offset
           FROM anime_list_entry WHERE tvdb_id = ?""",
        (tvdb_id,),
    ).fetchall()

    entries = []
    for eid, aid, _tvdb, dts, eo in entry_rows:
        mapping_rows = conn.execute(
            """SELECT anidb_season, tvdb_season, start, "end", offset,
                      episode_map
               FROM anime_list_mapping WHERE entry_id = ?""",
            (eid,),
        ).fetchall()
        mappings = [
            {
                "anidb_season": r[0],
                "tvdb_season": r[1],
                "start": r[2],
                "end": r[3],
                "offset": r[4],
                "episode_map": r[5],
            }
            for r in mapping_rows
        ]
        entries.append({
            "anidb_id": aid,
            "default_tvdb_season": dts,
            "episode_offset": eo,
            "mappings": mappings,
        })

    return entries


# ---------------------------------------------------------------------------
# AniDB HTTP API — per-anime episode fetch (client=memalpha)
# ---------------------------------------------------------------------------

ANIDB_API_URL = "http://api.anidb.net:9001/httpapi"
ANIDB_CLIENT = "memalpha"
ANIDB_CLIENT_VER = 1
ANIDB_RATE_LIMIT_SECONDS = 2.1  # slightly above 2s to stay safe

# Module-level timestamp for rate limiting across calls within one tick.
_last_api_call: float = 0.0


def _anidb_api_params(anidb_id: int) -> dict:
    return {
        "request": "anime",
        "client": ANIDB_CLIENT,
        "clientver": str(ANIDB_CLIENT_VER),
        "protover": "1",
        "aid": str(anidb_id),
    }


# AniDB epno type → our anidb_season convention
_EPNO_TYPE_TO_SEASON = {
    1: 1,  # regular
    2: 0,  # special
}


def fetch_anime_episodes(
    anidb_id: int, *, client: httpx.Client | None = None
) -> list[dict] | str | None:
    """Fetch per-episode data from the AniDB HTTP API.

    Returns:
      - list[dict]: episodes on success (may be empty if anime has
        only credits/trailers)
      - "BANNED": ban/client error — caller must abort remaining shows
      - "NOT_FOUND": AniDB returned a non-ban error (anime missing,
        deleted, etc.) — safe to tombstone, won't resolve on retry
      - None: transport/parse error — DO NOT tombstone, retry next tick
    """
    global _last_api_call

    # Rate limiting
    elapsed = time.time() - _last_api_call
    if elapsed < ANIDB_RATE_LIMIT_SECONDS:
        time.sleep(ANIDB_RATE_LIMIT_SECONDS - elapsed)

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        response = client.get(
            ANIDB_API_URL, params=_anidb_api_params(anidb_id)
        )
        _last_api_call = time.time()
        response.raise_for_status()
    except httpx.HTTPError as e:
        _last_api_call = time.time()
        log.warning("AniDB HTTP error for aid=%d: %s", anidb_id, e)
        return None  # transport error — retry next tick
    finally:
        if owns_client:
            client.close()

    # AniDB returns errors as XML <error> with HTTP 200
    body = response.text
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        log.warning("AniDB unparseable response for aid=%d", anidb_id)
        return None  # parse error — retry next tick

    if root.tag == "error":
        error_text = (root.text or "").strip().lower()
        if "banned" in error_text or "client" in error_text:
            log.error("AniDB ban/client error: %s", root.text)
            return "BANNED"
        log.info("AniDB error for aid=%d: %s", anidb_id, root.text)
        return "NOT_FOUND"  # permanent — safe to tombstone

    return _parse_episodes_xml(root)


def _parse_episodes_xml(root: ET.Element) -> list[dict]:
    """Extract episode data from the <anime> XML response."""
    episodes = []
    eps_el = root.find("episodes")
    if eps_el is None:
        return episodes

    for ep_el in eps_el.findall("episode"):
        epno_el = ep_el.find("epno")
        if epno_el is None:
            continue

        epno_type = int(epno_el.get("type", "1"))
        anidb_season = _EPNO_TYPE_TO_SEASON.get(epno_type)
        if anidb_season is None:
            # Skip credits (type 3), trailers (type 4), etc.
            continue

        epno_text = (epno_el.text or "").strip()
        # Strip letter prefix: S1→1, C1→1, T1→1
        epno_digits = epno_text.lstrip("SCTscto")
        try:
            epno = int(epno_digits)
        except ValueError:
            continue

        # Titles
        title_en = None
        title_ja = None
        title_romaji = None
        for title_el in ep_el.findall("title"):
            lang = title_el.get("{http://www.w3.org/XML/1998/namespace}lang",
                                title_el.get("lang", ""))
            text = (title_el.text or "").strip()
            if not text:
                continue
            if lang == "en" and title_en is None:
                title_en = text
            elif lang == "ja" and title_ja is None:
                title_ja = text
            elif lang == "x-jat" and title_romaji is None:
                title_romaji = text

        # Airdate and length
        airdate_el = ep_el.find("airdate")
        airdate = (airdate_el.text.strip()
                   if airdate_el is not None and airdate_el.text else None)

        length_el = ep_el.find("length")
        length = None
        if length_el is not None and length_el.text:
            try:
                length = int(length_el.text.strip())
            except ValueError:
                pass

        episodes.append({
            "anidb_season": anidb_season,
            "anidb_epno": epno,
            "title_en": title_en,
            "title_ja": title_ja,
            "title_romaji": title_romaji,
            "airdate": airdate,
            "length_minutes": length,
        })

    return episodes


def ingest_anime_episodes(
    conn, anidb_anime_id: int, episodes: list[dict], fetched_at: str
) -> int:
    """Write parsed AniDB episodes into the anidb_episode table.

    Returns number of rows written.
    """
    count = 0
    for ep in episodes:
        conn.execute(
            """INSERT INTO anidb_episode
               (anidb_anime_id, anidb_season, anidb_epno,
                title_en, title_ja, title_romaji,
                airdate, length_minutes, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (anidb_anime_id, anidb_season, anidb_epno)
               DO UPDATE SET
                 title_en = excluded.title_en,
                 title_ja = excluded.title_ja,
                 title_romaji = excluded.title_romaji,
                 airdate = excluded.airdate,
                 length_minutes = excluded.length_minutes,
                 fetched_at = excluded.fetched_at""",
            (
                anidb_anime_id,
                ep["anidb_season"],
                ep["anidb_epno"],
                ep["title_en"],
                ep["title_ja"],
                ep["title_romaji"],
                ep["airdate"],
                ep["length_minutes"],
                fetched_at,
            ),
        )
        count += 1
    conn.commit()
    return count


def _write_tombstone(conn, anidb_anime_id: int, fetched_at: str):
    """Insert a placeholder row so the NOT EXISTS check skips this anime."""
    conn.execute(
        """INSERT INTO anidb_episode
           (anidb_anime_id, anidb_season, anidb_epno, fetched_at)
           VALUES (?, 1, 0, ?)
           ON CONFLICT DO NOTHING""",
        (anidb_anime_id, fetched_at),
    )
    conn.commit()


def drip_fetch_episodes(conn, *, limit: int = 5) -> dict:
    """Fetch AniDB episodes for up to `limit` shows that need them.

    Picks shows that have AniDB mappings but no anidb_episode rows yet.
    Returns stats: {fetched, episodes_stored, skipped, banned}.
    """
    from lcars import service_health

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stats = {"fetched": 0, "episodes_stored": 0, "skipped": 0,
             "banned": False}

    # Find AniDB anime IDs that have episode mappings but no episode data
    rows = conn.execute(
        """SELECT DISTINCT m.anidb_anime_id
           FROM episode_anidb_mapping m
           WHERE NOT EXISTS (
             SELECT 1 FROM anidb_episode ae
             WHERE ae.anidb_anime_id = m.anidb_anime_id
           )
           LIMIT ?""",
        (limit,),
    ).fetchall()

    if not rows:
        return stats

    client = httpx.Client(timeout=30.0)
    try:
        for (anidb_id,) in rows:
            result = fetch_anime_episodes(anidb_id, client=client)

            if result == "BANNED":
                stats["banned"] = True
                service_health.record_failure(conn, "anidb", "client banned")
                conn.commit()
                log.error("AniDB ban detected, aborting drip fetch")
                break

            if result is None:
                # Transport/parse error — don't tombstone, retry next tick
                stats["skipped"] += 1
                continue

            if result == "NOT_FOUND":
                # Permanent error — tombstone so we don't retry
                stats["skipped"] += 1
                _write_tombstone(conn, anidb_id, now)
                continue

            if not result:
                # Empty list (all credits/trailers) — tombstone too
                stats["skipped"] += 1
                _write_tombstone(conn, anidb_id, now)
                continue

            count = ingest_anime_episodes(conn, anidb_id, result, now)
            stats["fetched"] += 1
            stats["episodes_stored"] += count
            log.info(
                "AniDB drip: aid=%d → %d episodes stored", anidb_id, count
            )
    finally:
        client.close()

    return stats


def fill_title_gaps(conn) -> int:
    """Fill episode.title IS NULL from AniDB English titles via mapping.

    Same NULL-only guard as metadata.py's Sonarr title write — never
    overwrites an existing title.
    """
    cursor = conn.execute(
        """UPDATE episode SET title = ae.title_en
           FROM episode_anidb_mapping m
           JOIN anidb_episode ae
             ON ae.anidb_anime_id = m.anidb_anime_id
             AND ae.anidb_season = m.anidb_season
             AND ae.anidb_epno = m.anidb_epno
           WHERE episode.id = m.episode_id
             AND episode.title IS NULL
             AND ae.title_en IS NOT NULL
             AND ae.title_en != ''"""
    )
    filled = cursor.rowcount
    if filled:
        conn.commit()
        log.info("Filled %d episode title gaps from AniDB", filled)
    return filled


def fill_airdate_gaps_anidb(conn) -> int:
    """Fill episode.air_date_utc IS NULL from AniDB episode airdates.

    Joins through episode_anidb_mapping → anidb_episode.  NULL-only —
    never overwrites existing airdates.  Stamps air_date_source = 'anidb'.
    """
    # Same FROM shape as fill_title_gaps — episode_anidb_mapping JOIN
    # anidb_episode, filtered to episode.id = m.episode_id.
    cursor = conn.execute(
        """UPDATE episode SET
             air_date_utc = ae.airdate || 'T00:00:00Z',
             air_date_source = 'anidb'
           FROM episode_anidb_mapping m, anidb_episode ae
           WHERE ae.anidb_anime_id = m.anidb_anime_id
             AND ae.anidb_season = m.anidb_season
             AND ae.anidb_epno = m.anidb_epno
             AND episode.id = m.episode_id
             AND episode.air_date_utc IS NULL
             AND ae.airdate IS NOT NULL
             AND ae.airdate != ''"""
    )
    filled = cursor.rowcount
    if filled:
        conn.commit()
        log.info("Filled %d episode airdate gaps from AniDB", filled)
    return filled


def poll_memory_alpha(conn) -> dict:
    """Ops loop entry point — refresh datasets if stale, re-derive
    mappings if data changed, drip-fetch episode data from AniDB API.

    Returns combined result for the GraphQL mutation.
    """
    from lcars import anime_lists, arm, fribb, syoboi, tvmaze, wikidata

    result = {
        "datasets_refreshed": False,
        "anime_list_entries": 0,
        "anidb_titles": 0,
        "anidb_ids_seeded": 0,
        "episodes_mapped": 0,
        "ids_propagated": 0,
        "season_rows_created": 0,
        "episode_season_ids_linked": 0,
        "drip_fetched": 0,
        "drip_episodes_stored": 0,
        "title_gaps_filled": 0,
        "tvmaze_drip_fetched": 0,
        "tvmaze_episodes_stored": 0,
        "syoboi_tids_seeded": 0,
        "syoboi_programs_fetched": 0,
        "syoboi_airdates_rewired": 0,
        "airdate_gaps_filled": 0,
    }

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # ── 1. Refresh datasets only if stale ──
    al_stale = anime_lists._dataset_is_stale(
        anime_lists.DATASET_CACHE_PATH, anime_lists.DATASET_MAX_AGE_SECONDS
    )
    titles_stale = _dataset_is_stale(
        TITLES_CACHE_PATH, TITLES_MAX_AGE_SECONDS
    )
    wd_stale = wikidata._dataset_is_stale(
        wikidata.CACHE_PATH, wikidata.MAX_AGE_SECONDS
    )
    arm_stale = arm._dataset_is_stale()

    if al_stale or titles_stale or wd_stale or arm_stale:
        try:
            result["datasets_refreshed"] = True

            # Anime-Lists XML
            al_root = anime_lists.load_xml()
            al_entries = anime_lists.parse_entries(al_root)
            al_stats = anime_lists.ingest_to_db(conn, al_entries, now)
            result["anime_list_entries"] = (
                al_stats["inserted"] + al_stats["updated"]
            )

            # AniDB titles dump
            titles = load_titles()
            anime_entries = extract_anime_entries(titles)
            t_stats = ingest_to_db(conn, titles, anime_entries, now)
            result["anidb_titles"] = t_stats["title_count"]

            # Seed AniDB IDs from Fribb
            fribb_data = fribb.load_dataset()
            result["anidb_ids_seeded"] = seed_anidb_external_ids(
                conn, fribb_data
            )

            # Re-derive episode mappings
            mapping_stats = derive_episode_mappings(conn)
            result["episodes_mapped"] = mapping_stats["mapped"]

            # Wikidata TV bridge
            if wd_stale:
                wd_data = wikidata.load_dataset()
                log.info("Wikidata TV bridge: %d records", len(wd_data))

            # ARM dataset (AniList↔Syoboi bridge)
            if arm_stale:
                arm_data = arm.load_dataset()
                log.info("ARM: %d entries", len(arm_data))
        except Exception:
            log.exception("Memory Alpha dataset refresh failed")
            # Continue to drip-fetch and title-fill regardless

    # ── 2. Propagate cross-database IDs (every tick) ──
    # fribb.load_dataset() / wikidata.load_dataset() / arm.load_dataset()
    # use cached files, cheap
    try:
        fribb_data = fribb.load_dataset()
        try:
            wd_data = wikidata.load_dataset()
        except Exception:
            wd_data = None
            log.warning("Wikidata load failed, skipping TV/movie ID propagation")
        id_counts = propagate_cross_ids(conn, fribb_data, wd_data)
        result["ids_propagated"] = sum(id_counts.values())
    except Exception:
        log.exception("Memory Alpha ID propagation failed")

    # ── 2b. Seed Syoboi TIDs from ARM (every tick, idempotent) ──
    try:
        arm_data = arm.load_dataset()
        result["syoboi_tids_seeded"] = arm.seed_syoboi_external_ids(
            conn, arm_data
        )
    except Exception:
        log.exception("ARM Syoboi TID seeding failed")

    # ── 2c. Ensure season rows + backfill episode.season_id ──
    # Creates missing season rows from episode.season values (TV: direct,
    # anime: through reconcile_season with Fribb). Then links episodes to
    # their season rows via NULL-only season_id backfill. Idempotent —
    # runs every tick, catches newly imported shows and the historical
    # backlog alike. Must run before drip-fetch so that new season rows
    # exist before episode-level data gets attached.
    try:
        from lcars import season_ranges
        result["season_rows_created"] = season_ranges.ensure_all_season_rows(conn)
        result["episode_season_ids_linked"] = season_ranges.backfill_episode_season_id(conn)
    except Exception:
        log.exception("Season row / episode.season_id backfill failed")

    # ── 3. Drip-fetch episode data from AniDB API ──
    drip = drip_fetch_episodes(conn, limit=5)
    result["drip_fetched"] = drip["fetched"]
    result["drip_episodes_stored"] = drip["episodes_stored"]

    # ── 4. Fill title gaps ──
    result["title_gaps_filled"] = fill_title_gaps(conn)

    # ── 5. TVmaze drip-fetch (TV/movies, 5 shows/tick) ──
    try:
        tvmaze_drip = tvmaze.drip_fetch_episodes(conn, limit=5)
        result["tvmaze_drip_fetched"] = tvmaze_drip["fetched"]
        result["tvmaze_episodes_stored"] = tvmaze_drip["episodes_stored"]
    except Exception:
        log.exception("TVmaze drip fetch failed")

    # ── 6. Syoboi change-driven sync (proginfo.xml pulse + LastUpdate) ──
    try:
        sync = syoboi.incremental_sync(conn)
        result["syoboi_programs_fetched"] = sync["programs_stored"]
    except Exception:
        log.exception("Syoboi incremental sync failed")

    # ── 7. Fill airdate gaps from AniDB + TVmaze + Syoboi ──
    try:
        anidb_dates = fill_airdate_gaps_anidb(conn)
        tvmaze_dates = tvmaze.fill_airdate_gaps(conn)
        syoboi_dates = syoboi.fill_airdate_gaps(conn)
        result["airdate_gaps_filled"] = anidb_dates + tvmaze_dates + syoboi_dates
    except Exception:
        log.exception("Airdate gap fill failed")

    # ── 8. Rewire anime airdates to Syoboi (idempotent) ──
    try:
        rw = syoboi.rewire_airdates(conn)
        result["syoboi_airdates_rewired"] = rw["updated"]
    except Exception:
        log.exception("Syoboi airdate rewire failed")

    return result


# _syoboi_incremental_fetch removed 2026-09-08 — replaced by
# syoboi.incremental_sync() which uses proginfo.xml pulse +
# ProgLookup LastUpdate for change-driven sync (catches reschedules
# on existing shows, not just new TIDs).
