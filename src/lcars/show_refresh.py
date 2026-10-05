"""The show page's "refresh show data" button (user, 2026-10-04).

One click, one show: the metadata fetch LCARS already has (Sonarr episodes, AniList, TMDB, AniList
air-date reconcile), then the episode data its tracking space uses — TVmaze for TV, AniDB for
anime (shared with the drip's limits: ban back-off, daily cap, nothing re-asked within 24 h) —
then Syoboi's schedule for an anime that has a Syoboi title, then every source's schedule is
stored as candidates and any schedule the user chose for a season is re-applied. Art is fetched by
the client afterwards (the existing manual fetch, which ignores the "not found" cache).

Each step reports what it did or why it did nothing; a step failing never stops the next ones.
Everything stays in this one request: a single anime is a handful of calls, never a sweep.
"""

from __future__ import annotations

import logging
import time

from lcars import (
    air_sources,
    anidb,
    freeze,
    metadata,
    numbering,
    provisional_episodes,
    syoboi,
    tvmaze,
)

log = logging.getLogger(__name__)


def _external_id(conn, show_id: str, service: str) -> str | None:
    row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = ?",
        (show_id, service),
    ).fetchone()
    return row["external_id"] if row else None


def refresh_show_data(conn, show_id: str) -> dict:
    """Returns {"steps": [{"name", "ok", "detail"}], "candidates": int}."""
    show = conn.execute("SELECT * FROM show WHERE id = ?", (show_id,)).fetchone()
    if show is None:
        raise ValueError(f"no such show: {show_id}")
    steps: list[dict] = []

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "ok": ok, "detail": detail})

    if freeze.frozen():
        step("Refresh", False, "automation is frozen on this server — nothing was fetched")
        return {"steps": steps, "candidates": 0}

    # 1. what LCARS already knows how to fetch for one show
    try:
        metadata.fetch_and_populate(conn, show_id)
        conn.commit()
        step("Metadata and episodes", True, "Sonarr, AniList and TMDB re-read")
    except Exception as e:
        log.exception("refresh %s: metadata step failed", show_id)
        step("Metadata and episodes", False, str(e))

    anime = show["tracking_space"] == "anime"
    fetched_anidb = False

    # 2. the episode source for this kind of show
    if not anime and show["media_shape"] == "episodic":
        tvmaze_id = _external_id(conn, show_id, "tvmaze")
        if tvmaze_id in (None, "-1"):
            step("TVmaze", False, "this show has no TVmaze id")
        else:
            try:
                now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                episodes = tvmaze.fetch_episodes(int(tvmaze_id), specials=True)
                if episodes:
                    count = tvmaze.ingest_episodes(
                        conn, int(tvmaze_id), episodes, now, store_specials=True
                    )
                    tvmaze.mark_specials_fetched(conn, int(tvmaze_id), now)
                    step("TVmaze", True, f"{count} episodes read")
                else:
                    step("TVmaze", False, "TVmaze returned no episodes")
            except Exception as e:
                log.exception("refresh %s: TVmaze step failed", show_id)
                step("TVmaze", False, str(e))
    elif anime:
        aids = anidb.anidb_ids_for_show(conn, show_id)
        if not aids:
            step("AniDB", False, "this show has no AniDB id")
        else:
            try:
                stats = anidb.refresh_anime_now(conn, aids)
                fetched_anidb = stats["fetched"] > 0
                parts = [f"{stats['fetched']} anime fetched ({stats['episodes_stored']} episodes)"]
                if stats["recent"]:
                    parts.append(f"{stats['recent']} already fetched in the last 24 h")
                if stats["left"]:
                    parts.append(f"{stats['left']} more left for the next click (or the drip)")
                if stats["skipped"]:
                    parts.append(f"{stats['skipped']} could not be read")
                if stats["refused"]:
                    parts.append(stats["refused"])
                if stats["banned"]:
                    parts.append("AniDB banned this server — stopped")
                unread = stats["skipped"] > 0 and stats["fetched"] == 0
                step("AniDB", not (stats["banned"] or stats["refused"] or unread),
                     "; ".join(parts))
            except Exception as e:
                log.exception("refresh %s: AniDB step failed", show_id)
                step("AniDB", False, str(e))

        tid = _external_id(conn, show_id, "syoboi")
        if tid and tid.isdigit():
            try:
                stats = syoboi.batch_fetch_and_ingest(conn, [int(tid)])
                conn.commit()
                syoboi.ensure_channels(conn)  # station names for the chooser (monthly)
                step("Syoboi", True, f"{stats['programs_stored']} broadcasts read")
            except Exception as e:
                log.exception("refresh %s: Syoboi step failed", show_id)
                step("Syoboi", False, str(e))

    if anime:  # the next episodes TVDB doesn't list yet, from Syoboi's numbered broadcasts
        try:
            made = provisional_episodes.sync_show(conn, show_id)
            conn.commit()
            if made["created"] or made["removed"]:
                step("Provisional episodes", True,
                     f"{made['created']} added from Syoboi, {made['removed']} removed"
                     " (until TVDB lists them)")
        except Exception as e:
            log.exception("refresh %s: provisional episodes failed", show_id)
            step("Provisional episodes", False, str(e))

    # 3. new data may fill empty dates and (after AniDB) changes the numbering
    try:
        anidb.fill_airdate_gaps_anidb(conn)
        tvmaze.fill_airdate_gaps(conn)
        syoboi.fill_airdate_gaps(conn)
        if fetched_anidb:
            numbering.renumber_show(conn, show_id)
            conn.commit()
    except Exception as e:
        log.exception("refresh %s: gap fill / renumber failed", show_id)
        step("Dates and numbering", False, str(e))

    # 4. every schedule we now know, and the user's chosen ones re-applied
    try:
        counts = air_sources.collect_candidates(conn, show_id)
        applied = air_sources.apply_all_choices(conn, show_id)
        conn.commit()
        total = sum(counts.values())
        detail = f"{total} stored-source dates listed"
        if applied["applied"]:
            detail += f"; {applied['applied']} dates updated from the schedule you chose"
        step("Schedules", True, detail)
        return {"steps": steps, "candidates": total}
    except Exception as e:
        log.exception("refresh %s: schedule step failed", show_id)
        step("Schedules", False, str(e))
        return {"steps": steps, "candidates": 0}
