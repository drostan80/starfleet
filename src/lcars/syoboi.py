"""Syoboi Calendar (しょぼいカレンダー) — anime broadcast schedule API.

Japan-focused anime broadcast database with per-channel, per-episode
event data.  Times are JST (Asia/Tokyo).  XML API at cal.syoboi.jp/db.php.

Primary value: accurate broadcast times (to the minute) for anime
episodes, sourced from actual Japanese TV schedules.  Multiple
broadcasts per episode (different stations) are retained, with the
earliest first-run broadcast derived as the canonical airtime.

API calls:
  ProgLookup — broadcast events (supports multi-TID, ranges, incremental)
  TitleLookup — title metadata + per-episode Japanese subtitles
  ChLookup — channel list (288 channels)

No documented rate limit, but we pace at 1s between requests.
"""

import logging
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://cal.syoboi.jp/db.php"
RATE_LIMIT_SECONDS = 2.0  # conservative — 429s observed at 1s pacing
USER_AGENT = "LCARS-Starfleet/0.1 (https://github.com/drostan/starfleet)"

JST = timezone(timedelta(hours=9))

_last_api_call: float = 0.0


def _rate_limit():
    """Enforce minimum spacing between API calls."""
    global _last_api_call
    elapsed = time.time() - _last_api_call
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)
    _last_api_call = time.time()


def _jst_to_utc(jst_str: str) -> str:
    """Convert 'YYYY-MM-DD HH:MM:SS' JST string to ISO 8601 UTC.

    >>> _jst_to_utc('2023-10-06 22:30:00')
    '2023-10-06T13:30:00Z'
    >>> _jst_to_utc('2023-10-07 01:30:00')
    '2023-10-06T16:30:00Z'
    """
    dt = datetime.strptime(jst_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    utc = dt.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(params: dict, *, client: httpx.Client | None = None,
         timeout: float = 60.0, max_retries: int = 3) -> ET.Element:
    """Make a rate-limited GET to the Syoboi API, return parsed XML root."""
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        for attempt in range(max_retries):
            _rate_limit()
            resp = client.get(
                BASE_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                log.warning("Syoboi 429, backing off %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return ET.fromstring(resp.text)
        # Final attempt failed with 429
        resp.raise_for_status()
        return ET.fromstring(resp.text)  # unreachable, keeps type checker happy
    finally:
        if owns_client:
            client.close()


def fetch_programs(tids: list[int], *,
                   client: httpx.Client | None = None,
                   last_update: str | None = None) -> list[dict]:
    """Fetch broadcast events for a batch of TIDs.

    Args:
        tids: Syoboi Title IDs (up to 50 recommended per batch).
        client: Optional shared httpx.Client.
        last_update: ISO timestamp for incremental sync — only events
            modified after this time are returned.

    Returns a list of dicts with keys:
        pid, tid, chid, count, st_time_jst, ed_time_jst,
        st_time_utc, ed_time_utc, subtitle, flag, deleted,
        revision, last_update
    """
    params = {
        "Command": "ProgLookup",
        "TID": ",".join(str(t) for t in tids),
        "Count": "*",
    }
    if last_update:
        # Syoboi expects JST timestamp for LastUpdate
        params["LastUpdate"] = last_update

    root = _get(params, client=client)
    items = root.findall(".//ProgItem")

    programs = []
    for item in items:
        st_jst = item.findtext("StTime") or ""
        ed_jst = item.findtext("EdTime") or ""
        programs.append({
            "pid": int(item.findtext("PID") or 0),
            "tid": int(item.findtext("TID") or 0),
            "chid": int(item.findtext("ChID") or 0),
            "count": _parse_count(item.findtext("Count")),
            "st_time_jst": st_jst,
            "ed_time_jst": ed_jst,
            "st_time_utc": _jst_to_utc(st_jst) if st_jst else None,
            "ed_time_utc": _jst_to_utc(ed_jst) if ed_jst else None,
            "subtitle": item.findtext("SubTitle") or None,
            "flag": int(item.findtext("Flag") or 0),
            "deleted": int(item.findtext("Deleted") or 0),
            "revision": item.findtext("Revision"),
            "last_update": item.findtext("LastUpdate"),
        })

    return programs


def fetch_titles(tids: list[int], *,
                 client: httpx.Client | None = None) -> dict[int, dict]:
    """Fetch title metadata for a batch of TIDs.

    Returns {tid: {title, short_title, category, first_ch}}.
    """
    root = _get({
        "Command": "TitleLookup",
        "TID": ",".join(str(t) for t in tids),
    }, client=client)

    titles = {}
    for item in root.findall(".//TitleItem"):
        tid = int(item.findtext("TID") or 0)
        # FirstCh can be a channel name string, not always numeric
        first_ch_raw = item.findtext("FirstCh") or ""
        try:
            first_ch = int(first_ch_raw)
        except (ValueError, TypeError):
            first_ch = 0
        titles[tid] = {
            "title": item.findtext("Title") or "",
            "short_title": item.findtext("ShortTitle") or "",
            "category": int(item.findtext("Cat") or 0),
            "first_ch": first_ch,
            "first_ch_name": first_ch_raw if not first_ch_raw.isdigit() else "",
        }
    return titles


def _parse_count(val: str | None) -> int | None:
    """Parse Syoboi Count field — episode number or None if blank."""
    if not val or not val.strip():
        return None
    try:
        return int(val)
    except ValueError:
        return None


def ingest_programs(conn, programs: list[dict], fetched_at: str) -> int:
    """Write broadcast events into syoboi_program table.

    Returns number of rows written/updated.
    """
    count = 0
    for p in programs:
        if not p["pid"]:
            continue
        conn.execute(
            """INSERT INTO syoboi_program
               (pid, tid, chid, count, st_time_jst, ed_time_jst,
                st_time_utc, ed_time_utc, subtitle, flag, deleted,
                revision, last_update, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (pid) DO UPDATE SET
                 tid = excluded.tid,
                 chid = excluded.chid,
                 count = excluded.count,
                 st_time_jst = excluded.st_time_jst,
                 ed_time_jst = excluded.ed_time_jst,
                 st_time_utc = excluded.st_time_utc,
                 ed_time_utc = excluded.ed_time_utc,
                 subtitle = excluded.subtitle,
                 flag = excluded.flag,
                 deleted = excluded.deleted,
                 revision = excluded.revision,
                 last_update = excluded.last_update,
                 fetched_at = excluded.fetched_at""",
            (
                p["pid"], p["tid"], p["chid"], p["count"],
                p["st_time_jst"], p["ed_time_jst"],
                p["st_time_utc"], p["ed_time_utc"],
                p["subtitle"], p["flag"], p["deleted"],
                p["revision"], p["last_update"], fetched_at,
            ),
        )
        count += 1
    conn.commit()
    return count


def ingest_titles(conn, titles: dict[int, dict], fetched_at: str) -> int:
    """Write title metadata into syoboi_title table.

    Returns number of rows written/updated.
    """
    count = 0
    for tid, t in titles.items():
        conn.execute(
            """INSERT INTO syoboi_title
               (tid, title, short_title, category, first_ch,
                first_ch_name, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (tid) DO UPDATE SET
                 title = excluded.title,
                 short_title = excluded.short_title,
                 category = excluded.category,
                 first_ch = excluded.first_ch,
                 first_ch_name = excluded.first_ch_name,
                 fetched_at = excluded.fetched_at""",
            (tid, t["title"], t["short_title"], t["category"],
             t["first_ch"], t.get("first_ch_name", ""), fetched_at),
        )
        count += 1
    conn.commit()
    return count


def batch_fetch_and_ingest(conn, tids: list[int], *,
                           batch_size: int = 50) -> dict:
    """Fetch programmes + titles for all TIDs in batches.

    Returns {programs_stored, titles_stored, batches}.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stats = {"programs_stored": 0, "titles_stored": 0, "batches": 0}

    # Deduplicate TIDs
    seen = set()
    unique_tids = []
    for t in tids:
        if t not in seen:
            seen.add(t)
            unique_tids.append(t)
    tids = unique_tids

    client = httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        for i in range(0, len(tids), batch_size):
            batch = tids[i:i + batch_size]
            stats["batches"] += 1

            try:
                # Programmes
                programs = fetch_programs(batch, client=client)
                stored = ingest_programs(conn, programs, now)
                stats["programs_stored"] += stored
            except httpx.HTTPStatusError as e:
                log.warning("Syoboi ProgLookup batch %d failed: %s",
                            stats["batches"], e)
                continue

            titles = {}
            try:
                titles = fetch_titles(batch, client=client)
                stored = ingest_titles(conn, titles, now)
                stats["titles_stored"] += stored
            except httpx.HTTPStatusError as e:
                log.warning("Syoboi TitleLookup batch %d failed: %s",
                            stats["batches"], e)

            log.info(
                "Syoboi batch %d: %d TIDs → %d programs, %d titles",
                stats["batches"], len(batch), len(programs), len(titles),
            )
    finally:
        client.close()

    return stats


def derive_earliest_airtimes(conn) -> dict[str, str]:
    """Derive earliest non-deleted broadcast per (tid, count).

    Returns {(tid, count): utc_timestamp} for the earliest
    first-run broadcast of each episode.
    """
    rows = conn.execute(
        """SELECT tid, count, MIN(st_time_utc) AS earliest_utc
           FROM syoboi_program
           WHERE deleted = 0
             AND count IS NOT NULL
             AND st_time_utc IS NOT NULL
           GROUP BY tid, count"""
    ).fetchall()

    return {
        (row["tid"], row["count"]): row["earliest_utc"]
        for row in rows
    }


def fill_airdate_gaps(conn) -> int:
    """Fill episode.air_date_utc IS NULL from Syoboi earliest broadcasts.

    Joins through episode_anidb_mapping → show_external_id(syoboi) →
    syoboi_program, matching anidb_epno = count.  NULL-only — never
    overwrites existing airdates.  Stamps air_date_source = 'syoboi'.

    For multi-entry shows (multiple AniDB anime IDs under one Sonarr
    show), we restrict to episodes whose anidb_anime_id matches the
    show's primary AniDB external_id.  This prevents season 2+ episodes
    (whose anidb_epno resets to 1) from colliding with season 1's
    Syoboi broadcast times.
    """
    cursor = conn.execute(
        """UPDATE episode SET
             air_date_utc = sp_min.earliest_utc,
             air_date_source = 'syoboi'
           FROM episode_anidb_mapping m,
                show_external_id sei_syoboi,
                show_external_id sei_anidb,
                (SELECT tid, count, MIN(st_time_utc) AS earliest_utc
                 FROM syoboi_program
                 WHERE deleted = 0
                   AND count IS NOT NULL
                   AND st_time_utc IS NOT NULL
                 GROUP BY tid, count) sp_min
           WHERE episode.id = m.episode_id
             AND sei_syoboi.show_id = episode.show_id
             AND sei_syoboi.service = 'syoboi'
             AND sei_anidb.show_id = episode.show_id
             AND sei_anidb.service = 'anidb'
             AND m.anidb_anime_id = CAST(sei_anidb.external_id AS INTEGER)
             AND sp_min.tid = CAST(sei_syoboi.external_id AS INTEGER)
             AND sp_min.count = m.anidb_epno
             AND m.anidb_season = 1
             AND episode.air_date_utc IS NULL
             AND episode.kind = 'regular'"""
    )
    filled = cursor.rowcount
    if filled:
        conn.commit()
        log.info("Filled %d episode airdate gaps from Syoboi", filled)
    return filled


def rewire_airdates(conn, *, dry_run: bool = False) -> dict:
    """Overwrite existing airdates with Syoboi's minute-accurate JST times.

    Unlike fill_airdate_gaps (NULL-only), this replaces Sonarr/AniList/
    animeschedule/anidb dates with Syoboi earliest-broadcast times where
    we have a confident join.  Skips air_date_source='manual' (never
    overwrite user corrections) and air_date_source='syoboi' (already
    correct).

    Same multi-entry collision guard as fill_airdate_gaps.

    Returns {updated: int, by_source: {old_source: count}}.
    If dry_run=True, returns counts without writing.
    """
    # Count what would change, broken down by old source
    preview = conn.execute(
        """SELECT episode.air_date_source, COUNT(*) AS n
           FROM episode
           JOIN episode_anidb_mapping m ON m.episode_id = episode.id
           JOIN show_external_id sei_syoboi
             ON sei_syoboi.show_id = episode.show_id
            AND sei_syoboi.service = 'syoboi'
           JOIN show_external_id sei_anidb
             ON sei_anidb.show_id = episode.show_id
            AND sei_anidb.service = 'anidb'
           JOIN (SELECT tid, count, MIN(st_time_utc) AS earliest_utc
                 FROM syoboi_program
                 WHERE deleted = 0
                   AND count IS NOT NULL AND st_time_utc IS NOT NULL
                 GROUP BY tid, count) sp_min
             ON sp_min.tid = CAST(sei_syoboi.external_id AS INTEGER)
            AND sp_min.count = m.anidb_epno
           WHERE m.anidb_season = 1
             AND m.anidb_anime_id = CAST(sei_anidb.external_id AS INTEGER)
             AND episode.kind = 'regular'
             AND episode.air_date_utc IS NOT NULL
             AND episode.air_date_source NOT IN ('manual', 'syoboi')
             AND episode.air_date_utc != sp_min.earliest_utc
           GROUP BY episode.air_date_source"""
    ).fetchall()

    by_source = {row["air_date_source"]: row["n"] for row in preview}
    total = sum(by_source.values())

    if dry_run or total == 0:
        return {"updated": 0, "would_update": total, "by_source": by_source}

    cursor = conn.execute(
        """UPDATE episode SET
             air_date_utc = sp_min.earliest_utc,
             air_date_source = 'syoboi'
           FROM episode_anidb_mapping m,
                show_external_id sei_syoboi,
                show_external_id sei_anidb,
                (SELECT tid, count, MIN(st_time_utc) AS earliest_utc
                 FROM syoboi_program
                 WHERE deleted = 0
                   AND count IS NOT NULL
                   AND st_time_utc IS NOT NULL
                 GROUP BY tid, count) sp_min
           WHERE episode.id = m.episode_id
             AND sei_syoboi.show_id = episode.show_id
             AND sei_syoboi.service = 'syoboi'
             AND sei_anidb.show_id = episode.show_id
             AND sei_anidb.service = 'anidb'
             AND m.anidb_anime_id = CAST(sei_anidb.external_id AS INTEGER)
             AND sp_min.tid = CAST(sei_syoboi.external_id AS INTEGER)
             AND sp_min.count = m.anidb_epno
             AND m.anidb_season = 1
             AND episode.kind = 'regular'
             AND episode.air_date_utc IS NOT NULL
             AND episode.air_date_source NOT IN ('manual', 'syoboi')
             AND episode.air_date_utc != sp_min.earliest_utc"""
    )
    updated = cursor.rowcount
    conn.commit()
    log.info("Rewired %d episode airdates to Syoboi (was: %s)", updated, by_source)
    return {"updated": updated, "by_source": by_source}
