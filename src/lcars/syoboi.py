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

Change detection:
  proginfo.xml — RSS 2.0 feed at cal.syoboi.jp/proginfo.xml reporting
  broadcast-data update times.  Used as a lightweight "something changed"
  pulse: if the feed's newest item is newer than our last sync cursor,
  we call ProgLookup with LastUpdate to retrieve only the changed rows.

No documented rate limit, but we pace at 2s between requests (429s
observed at 1s pacing).
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
    conn.commit()  # unconditional: a no-op write still holds the lock
    if filled:
        log.info("Filled %d episode airdate gaps from Syoboi", filled)
    return filled


def _rewire_condition() -> str:
    """The single shared WHERE fragment for 'is this candidate Syoboi
    date allowed to overwrite what's stored', reused by the preview
    count, the audit-trail insert, and the actual UPDATE below so the
    three queries can never drift apart (they used to be three
    independent copies of the same `NOT IN (...)` list — this is the
    2026-09-22 fix for exactly that kind of drift).

    Mirrors `airdate_priority.should_apply()`'s rule exactly: `manual`
    is absolute; a row already sourced `syoboi` always takes the new
    value (this is Syoboi re-asserting/correcting its own tracked slot
    — the `!=` check below already excludes a no-op); a row sourced
    `sonarr` (a raw TVDB placeholder, not a real competing broadcast —
    see `should_apply()`'s own exception) may be corrected in either
    direction too; any other source only loses if Syoboi's date is
    strictly earlier (never later — a later Syoboi slot for the same
    episode is generally a *different*, not-yet-happened broadcast, not
    a correction, per `airdate_priority.py`'s own docstring on this
    exact failure mode). `tests/test_airdate_priority.py`'s parity test
    checks this SQL fragment's behavior against `should_apply()`
    directly so the two can't silently disagree again."""
    return (
        "episode.air_date_source != 'manual'"
        " AND episode.air_date_utc != sp_min.earliest_utc"
        " AND (episode.air_date_source IN ('syoboi', 'sonarr')"
        "      OR sp_min.earliest_utc < episode.air_date_utc)"
    )


def rewire_airdates(conn, *, dry_run: bool = False) -> dict:
    """Overwrite existing airdates with Syoboi's minute-accurate JST times.

    Unlike fill_airdate_gaps (NULL-only), this replaces a lower-priority
    source's date with Syoboi's own earliest-broadcast time where we
    have a confident join — see `_rewire_condition()` for the exact
    rule (`airdate_priority.should_apply()`'s "same source always
    updates, different source only wins earlier" logic, expressed as
    SQL since this is a bulk UPDATE, not a per-row Python loop).

    Same multi-entry collision guard as fill_airdate_gaps.

    Returns {updated: int, by_source: {old_source: count}}.
    If dry_run=True, returns counts without writing.
    """
    condition = _rewire_condition()

    # Count what would change, broken down by old source
    preview = conn.execute(
        f"""SELECT episode.air_date_source, COUNT(*) AS n
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
             AND {condition}
           GROUP BY episode.air_date_source"""
    ).fetchall()

    by_source = {row["air_date_source"]: row["n"] for row in preview}
    total = sum(by_source.values())

    if dry_run or total == 0:
        return {"updated": 0, "would_update": total, "by_source": by_source}

    # Record air_date_change rows before the bulk UPDATE (preserves the
    # schedule-change signal for future calendar annotations).
    _record_rewire_changes(conn, condition)

    cursor = conn.execute(
        f"""UPDATE episode SET
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
             AND {condition}"""
    )
    updated = cursor.rowcount
    conn.commit()
    log.info("Rewired %d episode airdates to Syoboi (was: %s)", updated, by_source)
    return {"updated": updated, "by_source": by_source}


def _record_rewire_changes(conn, condition: str) -> None:
    """Insert air_date_change rows for episodes about to be rewired.

    Best-effort — a failure here doesn't block the rewire itself.
    """
    try:
        from lcars import ids

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rows = conn.execute(
            f"""SELECT episode.id, episode.air_date_utc, episode.air_date_source,
                       sp_min.earliest_utc
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
                 AND {condition}"""
        ).fetchall()

        for row in rows:
            conn.execute(
                """INSERT INTO air_date_change
                   (id, episode_id, previous_air_date_utc, new_air_date_utc,
                    previous_source, new_source, changed_at, changed_by)
                   VALUES (?, ?, ?, ?, ?, 'syoboi', ?, 'system')""",
                (
                    ids.generate_id(conn, "g"),
                    row["id"],
                    row["air_date_utc"],
                    row["earliest_utc"],
                    row["air_date_source"],
                    now,
                ),
            )
    except Exception:
        log.warning("Failed to record air_date_change rows for rewire", exc_info=True)


# ── Change detection via proginfo.xml ──────────────────────────────────

PROGINFO_URL = "https://cal.syoboi.jp/proginfo.xml"

# Back the cursor off by this many seconds to cover rows written during
# the previous fetch window (Syoboi's LastUpdate is server-side, but our
# fetches take time and events can be written mid-batch).
_CURSOR_BACKOFF_SECONDS = 300  # 5 minutes


def fetch_proginfo(*, client: httpx.Client | None = None,
                   timeout: float = 30.0) -> datetime | None:
    """Poll proginfo.xml for the latest broadcast-data update time.

    Returns the newest item's pubDate as a UTC datetime, or None if the
    feed is unparseable / unreachable.  Fail-open: callers should treat
    None as "something may have changed" and proceed with the full sync.
    """
    owns_client = client is None
    c = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        _rate_limit()
        resp = c.get(PROGINFO_URL, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception:
        log.warning("proginfo.xml fetch/parse failed — treating as changed")
        return None
    finally:
        if owns_client:
            c.close()

    # RSS 2.0: channel/lastBuildDate or the newest item/pubDate
    # Try lastBuildDate first (channel-level), fall back to items.
    last_build = root.findtext(".//channel/lastBuildDate")
    if last_build:
        parsed = _parse_rss_date(last_build)
        if parsed:
            return parsed

    # Fall back to newest item pubDate
    newest = None
    for item in root.findall(".//item/pubDate"):
        parsed = _parse_rss_date(item.text or "")
        if parsed and (newest is None or parsed > newest):
            newest = parsed
    return newest


def _parse_rss_date(date_str: str) -> datetime | None:
    """Parse an RSS 2.0 date (RFC 822 / RFC 2822) into a UTC datetime."""
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(date_str.strip())
        return dt.astimezone(UTC)
    except Exception:
        return None


def get_sync_cursor(conn) -> str | None:
    """Derive the sync cursor from the latest last_update in syoboi_program.

    Returns a JST timestamp string (Syoboi's native format) suitable for
    passing to ProgLookup's LastUpdate parameter, backed off by
    _CURSOR_BACKOFF_SECONDS.  Returns None if no data exists yet.
    """
    row = conn.execute(
        "SELECT MAX(last_update) AS max_lu FROM syoboi_program"
    ).fetchone()
    if not row or not row["max_lu"]:
        return None

    # last_update is in JST: "YYYY-MM-DD HH:MM:SS"
    try:
        dt = datetime.strptime(row["max_lu"], "%Y-%m-%d %H:%M:%S")
        dt = dt - timedelta(seconds=_CURSOR_BACKOFF_SECONDS)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        log.warning("Unparseable sync cursor: %s", row["max_lu"])
        return None


def incremental_sync(conn, *, force: bool = False) -> dict:
    """Change-driven Syoboi sync: poll proginfo.xml, then ProgLookup
    with LastUpdate for changed rows only.

    1. Check proginfo.xml — if feed's latest update <= our cursor and
       not force, skip (nothing changed).
    2. Fetch all tracked TIDs' changed programmes via ProgLookup with
       LastUpdate range.
    3. Also fetch any NEW TIDs that have no events yet (first-time sync).
    4. Ingest everything.

    Returns {changed: bool, programs_stored: int, new_tids_fetched: int,
             titles_stored: int, cursor_before: str, cursor_after: str}.
    """
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cursor_before = get_sync_cursor(conn)
    result = {
        "changed": False,
        "programs_stored": 0,
        "new_tids_fetched": 0,
        "titles_stored": 0,
        "cursor_before": cursor_before,
        "cursor_after": cursor_before,
    }

    # ── Pulse check ──
    if cursor_before and not force:
        feed_latest = fetch_proginfo()
        if feed_latest is not None:
            # Convert cursor (JST) to UTC for comparison
            try:
                cursor_dt = datetime.strptime(
                    cursor_before, "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=JST).astimezone(UTC)
                if feed_latest <= cursor_dt:
                    log.debug(
                        "proginfo.xml unchanged (feed=%s, cursor=%s) — skipping",
                        feed_latest.isoformat(), cursor_dt.isoformat(),
                    )
                    return result
            except ValueError:
                pass  # fall through to full sync
        # feed_latest is None → fail-open, proceed with sync

    result["changed"] = True

    # ── Gather tracked TIDs ──
    all_tid_rows = conn.execute(
        """SELECT sei.external_id AS tid
           FROM show_external_id sei
           JOIN show s ON sei.show_id = s.id
           WHERE sei.service = 'syoboi'
             AND s.tracked = 1
             AND s.tracking_space = 'anime'"""
    ).fetchall()
    all_tids = [int(r["tid"]) for r in all_tid_rows]

    if not all_tids:
        return result

    client = httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        # ── Incremental: changed programmes since cursor ──
        if cursor_before:
            # ProgLookup LastUpdate range: "from-" means "since"
            last_update_range = f"{cursor_before}-"
            incremental_programs = _fetch_programs_all_tids(
                all_tids, client=client, last_update=last_update_range,
            )
            if incremental_programs:
                stored = ingest_programs(conn, incremental_programs, now_iso)
                result["programs_stored"] += stored
                log.info(
                    "Syoboi incremental: %d changed programmes across %d tracked TIDs",
                    stored, len(all_tids),
                )

                # Also fetch titles for TIDs that had changes
                changed_tids = list({p["tid"] for p in incremental_programs})
                _fetch_and_ingest_titles(
                    conn, changed_tids, now_iso, client, result
                )

        # ── New TIDs: shows with a Syoboi TID but no events yet ──
        new_tid_rows = conn.execute(
            """SELECT sei.external_id AS tid
               FROM show_external_id sei
               JOIN show s ON sei.show_id = s.id
               WHERE sei.service = 'syoboi'
                 AND s.tracked = 1
                 AND s.tracking_space = 'anime'
                 AND NOT EXISTS (
                   SELECT 1 FROM syoboi_program sp
                   WHERE sp.tid = CAST(sei.external_id AS INTEGER)
                 )"""
        ).fetchall()
        new_tids = [int(r["tid"]) for r in new_tid_rows]

        if new_tids:
            new_programs = _fetch_programs_all_tids(
                new_tids, client=client,
            )
            if new_programs:
                stored = ingest_programs(conn, new_programs, now_iso)
                result["programs_stored"] += stored
                result["new_tids_fetched"] = len(new_tids)
                log.info(
                    "Syoboi new TIDs: %d → %d programmes",
                    len(new_tids), stored,
                )
            _fetch_and_ingest_titles(
                conn, new_tids, now_iso, client, result
            )
    finally:
        client.close()

    # Update cursor
    result["cursor_after"] = get_sync_cursor(conn)
    return result


def _fetch_programs_all_tids(
    tids: list[int], *,
    client: httpx.Client,
    last_update: str | None = None,
    batch_size: int = 50,
) -> list[dict]:
    """Fetch programmes for all TIDs in batches, optionally filtered
    by LastUpdate range.  Returns aggregated list of programme dicts.

    TIDs are de-duplicated first: several shows can share one TID, and
    Syoboi answers a TID list containing a duplicate with 400 Bad Request
    (the whole first batch failed on every tick until 2026-09-25)."""
    tids = list(dict.fromkeys(tids))
    all_programs: list[dict] = []
    for i in range(0, len(tids), batch_size):
        batch = tids[i:i + batch_size]
        try:
            programs = fetch_programs(
                batch, client=client, last_update=last_update,
            )
            all_programs.extend(programs)
        except httpx.HTTPStatusError as e:
            log.warning(
                "Syoboi ProgLookup batch failed (TIDs %d-%d): %s",
                i, min(i + batch_size, len(tids)), e,
            )
    return all_programs


def _fetch_and_ingest_titles(
    conn, tids: list[int], now_iso: str,
    client: httpx.Client, result: dict,
    batch_size: int = 50,
) -> None:
    """Fetch and ingest title metadata for a list of TIDs (de-duplicated,
    same 400-on-duplicate reason as _fetch_programs_all_tids)."""
    tids = list(dict.fromkeys(tids))
    for i in range(0, len(tids), batch_size):
        batch = tids[i:i + batch_size]
        try:
            titles = fetch_titles(batch, client=client)
            stored = ingest_titles(conn, titles, now_iso)
            result["titles_stored"] += stored
        except httpx.HTTPStatusError as e:
            log.warning("Syoboi TitleLookup batch failed: %s", e)
