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

import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta, timezone

import httpx

from lcars import air_sources, air_time, airdate_priority, status_rules, util

log = logging.getLogger(__name__)

BASE_URL = "https://cal.syoboi.jp/db.php"
RATE_LIMIT_SECONDS = 2.0  # conservative — 429s observed at 1s pacing
USER_AGENT = "LCARS-Starfleet/0.1 (https://github.com/drostan/starfleet)"

JST = timezone(timedelta(hours=9))

_last_api_call: float = 0.0

# Cloudflare has put cal.syoboi.jp behind a bot challenge before (2026-10-22: every non-browser
# request got 403, `cf-mitigated: challenge`, for about a day).  Retrying 24 batches per sweep
# only adds failures, so the first challenge pauses every Syoboi call for a while, persistently
# (a restart does not end it), and opens one review saying so.
CHALLENGE_PAUSE_HOURS = 6
_PAUSE_KEY = "syoboi_paused_until"
REVIEW_FIELD = "syoboi_challenge"

# A 429 used to sleep 5+10+15 s per batch inside the one-threaded server, freezing every request
# (nginx's /auth/check timed out: 500s, 2026-10-26).  Now the first 429 stops the sweep and pauses
# Syoboi for a short while; no sleep.  A sweep also fetches at most MAX_BATCHES_PER_SWEEP batches
# per tick and resumes the rest on the next one.
RATE_LIMIT_PAUSE_MINUTES = 15
MAX_BATCHES_PER_SWEEP = 5
_SWEEP_KEY = "syoboi_sweep"

_paused_until: str | None = None  # ISO UTC; None = not paused


class SyoboiChallenge(httpx.HTTPError):
    """Syoboi answered with Cloudflare's bot challenge (or we are paused after one).  An
    httpx.HTTPError but not an HTTPStatusError, so the per-batch handlers do not swallow it
    and the sweep stops at the first one."""


class SyoboiRateLimited(httpx.HTTPError):
    """Syoboi answered 429.  Not a SyoboiChallenge: no review, only the short pause."""


def _is_challenge(resp: httpx.Response) -> bool:
    return resp.status_code == 403 and (
        resp.headers.get("cf-mitigated") == "challenge" or "Just a moment" in resp.text[:2000])


class SyoboiPaused(SyoboiChallenge):
    """The pause after an earlier challenge is still running: no request was made."""


def _check_not_paused() -> None:
    global _paused_until
    if _paused_until is not None:
        if util.now_utc_iso() < _paused_until:
            raise SyoboiPaused(f"Syoboi paused until {_paused_until} after a Cloudflare challenge")
        _paused_until = None


def _hit_challenge() -> SyoboiChallenge:
    global _paused_until
    _paused_until = util.utc_iso_offset_hours(CHALLENGE_PAUSE_HOURS)
    return SyoboiChallenge("Syoboi answered with a Cloudflare bot challenge (HTTP 403)")


def _load_pause(conn) -> None:
    """Take the pause stored by an earlier run (a restart must not forget it)."""
    global _paused_until
    row = conn.execute("SELECT value FROM web_setting WHERE key = ?", (_PAUSE_KEY,)).fetchone()
    if row and row["value"] > util.now_utc_iso():
        _paused_until = row["value"]
    elif row:
        conn.execute("DELETE FROM web_setting WHERE key = ?", (_PAUSE_KEY,))


def _record_challenge(conn) -> None:
    """Persist the pause and tell you once (extending the open review if one is still open)."""
    from lcars import reviews

    until = _paused_until or util.utc_iso_offset_hours(CHALLENGE_PAUSE_HOURS)
    conn.execute("INSERT INTO web_setting (key, value) VALUES (?, ?)"
                 " ON CONFLICT (key) DO UPDATE SET value = excluded.value", (_PAUSE_KEY, until))
    now = util.now_utc_iso()
    log.warning("Syoboi hit a Cloudflare challenge — all Syoboi calls paused until %s", until)
    reviews.open_review(
        conn, "service", "syoboi", REVIEW_FIELD, "syoboi",
        f"Syoboi hit a Cloudflare challenge at {now}; its calls are paused and will be retried "
        f"automatically in {CHALLENGE_PAUSE_HOURS} hours (at {until}). If you see this review "
        "again, check by hand what needs to be done.",
        ["acknowledge"], {"paused_until": until, "pause_hours": CHALLENGE_PAUSE_HOURS})


def _recovered(conn) -> None:
    """Syoboi answered normally: a later challenge opens a fresh review."""
    from lcars import pending_review

    conn.execute("DELETE FROM web_setting WHERE key = ?", (_PAUSE_KEY,))
    pending_review.close_obsolete(conn, "service", "syoboi", REVIEW_FIELD,
                                  "Syoboi answers again without a challenge")


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
         timeout: float = 60.0) -> ET.Element:
    """Make a rate-limited GET to the Syoboi API, return parsed XML root."""
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        _check_not_paused()
        _rate_limit()
        resp = client.get(
            BASE_URL,
            params=params,
            headers={"User-Agent": USER_AGENT},
        )
        if _is_challenge(resp):
            raise _hit_challenge()
        if resp.status_code == 429:
            global _paused_until
            _paused_until = util.utc_iso_offset_hours(RATE_LIMIT_PAUSE_MINUTES / 60)
            log.warning("Syoboi 429 — all Syoboi calls paused until %s", _paused_until)
            raise SyoboiRateLimited("Syoboi answered 429 (too many requests)")
        resp.raise_for_status()
        return ET.fromstring(resp.text)
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


def fetch_channels(*, client: httpx.Client | None = None) -> dict[int, str]:
    """Syoboi's TV stations: {ChID: name} (ChLookup, no ids = all)."""
    root = _get({"Command": "ChLookup"}, client=client)
    channels = {}
    for item in root.findall(".//ChItem"):
        chid = item.findtext("ChID")
        name = (item.findtext("ChName") or "").strip()
        if chid and chid.isdigit() and name:
            channels[int(chid)] = name
    return channels


def ensure_channels(conn, *, max_age_days: int = 30) -> int:
    """Keep station names (for the schedule chooser) fresh; one cheap call a month at most.
    Returns the number stored, 0 when the table was fresh or Syoboi could not be read."""
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    newest = conn.execute("SELECT MAX(fetched_at) FROM syoboi_channel").fetchone()[0]
    if newest is not None and newest >= util.add_days(now, -max_age_days):
        return 0
    try:
        channels = fetch_channels()
    except (httpx.HTTPError, ET.ParseError):
        log.warning("Syoboi ChLookup failed", exc_info=True)
        return 0
    for chid, name in channels.items():
        conn.execute(
            "INSERT INTO syoboi_channel (chid, name, fetched_at) VALUES (?, ?, ?)"
            " ON CONFLICT (chid) DO UPDATE SET name = excluded.name,"
            " fetched_at = excluded.fetched_at",
            (chid, name, now),
        )
    conn.commit()
    return len(channels)


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
    overwrites existing airdates, and skips a season that follows a chosen schedule.
    Stamps air_date_source = 'syoboi'.

    For multi-entry shows (multiple AniDB anime IDs under one Sonarr
    show), we restrict to episodes whose anidb_anime_id matches the
    show's primary AniDB external_id.  This prevents season 2+ episodes
    (whose anidb_epno resets to 1) from colliding with season 1's
    Syoboi broadcast times.
    """
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
             AND episode.air_date_utc IS NULL
             AND NOT {air_time.LOCKED_SQL}
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
    time_rule = (
        f" ABS(strftime('%s', episode.air_date_utc) - strftime('%s', sp_min.earliest_utc))"
        f"   > {airdate_priority.AIR_DATE_TOLERANCE_SECONDS}"
        " AND (episode.air_date_source IN ('syoboi', 'sonarr', 'tvdb')"
        "      OR sp_min.earliest_utc < episode.air_date_utc)"
    )
    # a stored date-only value: a Syoboi time within three days of it is its time arriving (a
    # precision upgrade, `airdate_priority.within_upgrade_window`); otherwise the ordinary rule
    date_rule = (
        f" (ABS(strftime('%s', episode.air_date_utc) - strftime('%s', sp_min.earliest_utc))"
        f"   <= {(air_time.UPGRADE_WINDOW_DAYS * 24 + 24) * 3600}"
        "  OR episode.air_date_source IN ('syoboi', 'sonarr', 'tvdb')"
        "  OR sp_min.earliest_utc < episode.air_date_utc)"
    )
    return (
        f"{status_rules.followed_sql()} AND "  # R2.10: skipped seasons aren't followed
        "episode.air_date_source != 'manual'"
        f" AND NOT {air_sources.LOCKED_SQL}"  # a season following a chosen schedule is left alone
        " AND episode.air_date_utc != sp_min.earliest_utc"
        " AND ((COALESCE(episode.air_precision, 'time') = 'time' AND" + time_rule + ")"
        "      OR (episode.air_precision = 'date' AND" + date_rule + "))"
    )


def rewire_airdates(conn, *, dry_run: bool = False, only_airing: bool = False) -> dict:
    """Overwrite existing airdates with Syoboi's minute-accurate JST times.

    Unlike fill_airdate_gaps (NULL-only), this replaces a lower-priority
    source's date with Syoboi's own earliest-broadcast time where we
    have a confident join — see `_rewire_condition()` for the exact
    rule (`airdate_priority.should_apply()`'s "same source always
    updates, different source only wins earlier" logic, expressed as
    SQL since this is a bulk UPDATE, not a per-row Python loop).

    Same multi-entry collision guard as fill_airdate_gaps. `only_airing` leaves every season
    that has aired fully alone (its dates never change).

    Returns {updated: int, by_source: {old_source: count}}.
    If dry_run=True, returns counts without writing.
    """
    condition = _rewire_condition()
    if only_airing:
        # a season that has aired fully is history: its dates never change (user 10-07)
        condition += (
            " AND EXISTS (SELECT 1 FROM episode x WHERE x.show_id = episode.show_id"
            "  AND x.season = episode.season AND x.kind = 'regular'"
            "  AND (x.air_date_utc IS NULL OR COALESCE(x.air_aired_at, x.air_date_utc)"
            f"       >= '{util.utc_iso_offset(-air_sources.FINISHED_GRACE_DAYS)}'))"
        )

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
             air_date_source = 'syoboi',
             air_precision = NULL, air_local_date = NULL, air_aired_at = NULL
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
        _check_not_paused()
        _rate_limit()
        resp = c.get(PROGINFO_URL, headers={"User-Agent": USER_AGENT})
        if _is_challenge(resp):
            raise _hit_challenge()
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except SyoboiChallenge:
        raise
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


def _lookup_range_since(jst_stamp: str) -> str:
    """ProgLookup's LastUpdate range "since": Syoboi reads `YYYYMMDD_HHMMSS-`.  It answers any
    other spelling (our stored `YYYY-MM-DD HH:MM:SS`) with an empty list and no error, so from
    09-08 to 10-23 every change-driven sweep fetched nothing (checked live 2026-10-23: the same
    20 shows gave 0 programmes with the dashed form, 81 with this one)."""
    stamp = datetime.strptime(jst_stamp, "%Y-%m-%d %H:%M:%S")
    return stamp.strftime("%Y%m%d_%H%M%S") + "-"


def incremental_sync(conn, *, force: bool = False) -> dict:
    """`_incremental_sync` behind the challenge guard: a Cloudflare challenge stops the sweep,
    pauses Syoboi for CHALLENGE_PAUSE_HOURS and opens one review; while paused nothing is
    requested; the first normal answer afterwards closes the review."""
    _load_pause(conn)
    try:
        result = _incremental_sync(conn, force=force)
    except SyoboiPaused:
        return {"changed": False, "paused": True, "programs_stored": 0, "new_tids_fetched": 0,
                "titles_stored": 0, "cursor_before": None, "cursor_after": None}
    except SyoboiRateLimited:
        # Short pause, kept across a restart; the sweep state (what is left) is already saved.
        conn.execute("INSERT INTO web_setting (key, value) VALUES (?, ?)"
                     " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (_PAUSE_KEY, _paused_until))
        conn.commit()
        return {"changed": False, "paused": True, "programs_stored": 0, "new_tids_fetched": 0,
                "titles_stored": 0, "cursor_before": None, "cursor_after": None}
    except SyoboiChallenge:
        _record_challenge(conn)
        return {"changed": False, "paused": True, "programs_stored": 0, "new_tids_fetched": 0,
                "titles_stored": 0, "cursor_before": None, "cursor_after": None}
    _recovered(conn)
    return result


def _incremental_sync(conn, *, force: bool = False) -> dict:
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

    sweep = _load_sweep(conn)

    # ── Pulse check (not while a sweep is unfinished: it resumes from its saved list) ──
    if cursor_before and not force and sweep is None:
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

    if sweep is None:
        sweep = _start_sweep(conn, cursor_before)
        if sweep is None:
            return result
        _save_sweep(conn, sweep)

    client = httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        # At most MAX_BATCHES_PER_SWEEP batches (programmes + titles) per tick, so the one thread
        # is never held for long; the rest waits in the saved sweep.  Each batch is ingested as
        # it arrives, so a 429 mid-sweep loses nothing already fetched.
        for _ in range(MAX_BATCHES_PER_SWEEP):
            phase = "changed" if sweep["changed"] else "new" if sweep["new"] else None
            if phase is None:
                break
            batch, sweep[phase] = sweep[phase][:50], sweep[phase][50:]
            _save_sweep(conn, sweep)  # a failed batch is not retried forever; it is dropped
            programs = _fetch_programs_all_tids(
                batch, client=client, last_update=sweep["since"] if phase == "changed" else None,
            )
            if programs:
                stored = ingest_programs(conn, programs, now_iso)
                result["programs_stored"] += stored
                if phase == "new":
                    result["new_tids_fetched"] += len(batch)
            _fetch_and_ingest_titles(
                conn, list({p["tid"] for p in programs}) if phase == "changed" else batch,
                now_iso, client, result,
            )
    except (SyoboiChallenge, SyoboiRateLimited):
        # The batch in flight is not done: put it back at the head of its list.
        sweep[phase] = batch + sweep[phase]
        _save_sweep(conn, sweep)
        raise
    finally:
        client.close()

    if not sweep["changed"] and not sweep["new"]:
        conn.execute("DELETE FROM web_setting WHERE key = ?", (_SWEEP_KEY,))
        conn.commit()
    result["cursor_after"] = get_sync_cursor(conn)
    return result


def _priority_order(conn, tids) -> list[int]:
    """Fetch order: watching shows first, then planned, then the rest; within each, the TID
    whose next broadcast is soonest first (today, then tomorrow, ...), TIDs with no coming
    broadcast last; the TID number breaks ties so the order is stable."""
    rank: dict[int, int] = {}
    for status, tid in conn.execute(
        "SELECT s.status, x.external_id FROM show_external_id x JOIN show s ON s.id = x.show_id"
        " WHERE x.service = 'syoboi' UNION ALL"
        " SELECT s.status, x.external_id FROM season_external_id x"
        " JOIN season z ON z.id = x.season_id JOIN show s ON s.id = z.show_id"
        " WHERE x.service = 'syoboi'"
    ):
        if str(tid).isdigit():
            r = 0 if status == "watching" else 1 if status == "planned" else 2
            rank[int(tid)] = min(rank.get(int(tid), 2), r)
    today_jst = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_jst.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # today's shows count as today
    nxt = {r["tid"]: r["t"] for r in conn.execute(
        "SELECT tid, MIN(st_time_utc) AS t FROM syoboi_program"
        " WHERE deleted = 0 AND st_time_utc >= ? GROUP BY tid", (start,))}
    return sorted(dict.fromkeys(tids), key=lambda t: (
        rank.get(t, 2), t not in nxt, nxt.get(t, ""), t))


def _start_sweep(conn, cursor_before: str | None) -> dict | None:
    """The tracked TIDs, split into changed-since-cursor and never-fetched, in priority order."""
    from lcars import syoboi_levels  # a run per level, not per show (syoboi_levels.py)

    all_tids = {int(r["tid"]) for r in conn.execute(
        """SELECT sei.external_id AS tid
           FROM show_external_id sei
           JOIN show s ON sei.show_id = s.id
           WHERE sei.service = 'syoboi'
             AND s.tracked = 1
             AND s.tracking_space = 'anime'""")}
    all_tids |= set(syoboi_levels.fetchable_tids(conn))
    if not all_tids:
        return None
    have = {r[0] for r in conn.execute("SELECT DISTINCT tid FROM syoboi_program")}
    new = [t for t in all_tids if t not in have]
    return {
        "since": _lookup_range_since(cursor_before) if cursor_before else None,
        "changed": _priority_order(conn, all_tids) if cursor_before else [],
        "new": _priority_order(conn, new),
    }


def _load_sweep(conn) -> dict | None:
    row = conn.execute("SELECT value FROM web_setting WHERE key = ?", (_SWEEP_KEY,)).fetchone()
    return json.loads(row["value"]) if row else None


def _save_sweep(conn, sweep: dict) -> None:
    conn.execute("INSERT INTO web_setting (key, value) VALUES (?, ?)"
                 " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (_SWEEP_KEY, json.dumps(sweep)))
    conn.commit()


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
