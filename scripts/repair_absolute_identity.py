#!/usr/bin/env python3
"""Repair shows whose identity was derived from LCARS season numbers
instead of absolute episode order (the 2026-09-23 Slime bug — see
`lcars/sonarr_match.py`'s module docstring).

Needs the v0.2.55+ code (run inside the lcars container). Per show:

1. Capture Sonarr's raw TVDB coordinates onto every episode, matched by
   absolute order (`sonarr_match.find_episode`).
2. Drop Memory Alpha AniDB mappings that disagree with Anime-Lists
   resolved from those real coordinates (or that were derived from LCARS
   numbering on a diverged show with no coordinates), then re-derive.
   'air_date_confirmed' locks are dropped too, *unless* Sonarr's own raw
   date independently confirms them (a lock made against an AniList-
   written date, like Slime S4's, is not).
3. Re-reconcile every non-manual season from Memory Alpha; report (never
   touch) manual_override seasons whose stored ids disagree.
4. For seasons whose AniList id changed, restore AniList-written air dates
   that sit more than the drift threshold (60 days) from Sonarr's raw date —
   the ones written through the wrong link; a small AniList correction from
   an earlier, correct link is kept (logged in air_date_change).
5. Re-seed derived per-episode external ids (stale rows dropped).
6. Re-run the per-show local file audit.

Usage (inside the container):
    python3 repair_absolute_identity.py --db /db/lcars.db --show s-hyj69b --dry-run
    python3 repair_absolute_identity.py --db /db/lcars.db --show s-hyj69b --apply

--dry-run runs everything against a temporary copy and prints the report.
"""

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

from lcars import (
    anidb,
    config,
    db,
    ids,
    local_audit,
    metadata,
    season_mapping,
    season_ranges,
    sonarr_client,
    sonarr_match,
    util,
)

CHANGED_BY = "repair_absolute_identity"


def _snapshot(conn, show_id):
    seasons = {
        r["season_number"]: (r["anilist_id"], r["mal_id"], r["manual_override"])
        for r in conn.execute(
            "SELECT season_number, anilist_id, mal_id, manual_override FROM season"
            " WHERE show_id = ?",
            (show_id,),
        )
    }
    episodes = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT e.id, e.season, e.episode, e.absolute_number, e.sonarr_season,"
            " e.sonarr_episode, e.air_date_utc, e.air_date_source, e.file_path_sonarr,"
            " e.available_via_sonarr, m.anidb_anime_id, m.anidb_epno, m.confidence"
            " FROM episode e LEFT JOIN episode_anidb_mapping m ON m.episode_id = e.id"
            " WHERE e.show_id = ?",
            (show_id,),
        )
    }
    return seasons, episodes


def _capture_coords(conn, client, show_id):
    tvdb = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
        (show_id,),
    ).fetchone()
    if tvdb is None:
        return "no tvdb link"
    series = client.series_by_tvdb_id(int(tvdb["external_id"]))
    if series is None:
        return "not in Sonarr"
    siblings = sonarr_match.sibling_show_ids_for_tvdb(conn, tvdb["external_id"])
    show_ids = siblings if show_id in siblings else [show_id]
    matched = unmatched = 0
    for ep in client.episodes(series["id"], include_episode_file=True):
        if sonarr_match.find_episode(conn, show_ids, ep) is not None:
            matched += 1
        else:
            unmatched += 1
    conn.commit()
    return f"{matched} Sonarr episode(s) matched, {unmatched} with no LCARS row"


def _drop_disagreeing_mappings(conn, show_id):
    tvdb = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
        (show_id,),
    ).fetchone()
    if tvdb is None:
        return 0
    resolver = anidb._SeasonResolver(anidb._load_entries_for_tvdb(conn, tvdb["external_id"]))
    diverged = sonarr_match.show_numbering_diverged(conn, show_id)
    dropped = 0
    for row in conn.execute(
        "SELECT e.id, e.sonarr_season, e.sonarr_episode, m.anidb_anime_id, m.anidb_epno,"
        "   m.confidence,"
        "   ABS(julianday(substr(e.air_date_raw_sonarr, 1, 10)) - julianday(ae.airdate))"
        "     AS raw_gap_days"
        " FROM episode e JOIN episode_anidb_mapping m ON m.episode_id = e.id"
        " LEFT JOIN anidb_episode ae ON ae.anidb_anime_id = m.anidb_anime_id"
        "   AND ae.anidb_season = m.anidb_season AND ae.anidb_epno = m.anidb_epno"
        " WHERE e.show_id = ? AND e.kind = 'regular'",
        (show_id,),
    ).fetchall():
        # A lock that Sonarr's own raw date independently confirms (same
        # ±1-day window as the arbiter) is kept even when Anime-Lists
        # disagrees — the community list can be stale (Tonbo! S2: locked
        # to AniDB 18721 by matching dates, list says 18019 eps 14-26).
        if (
            row["confidence"] == "air_date_confirmed"
            and row["raw_gap_days"] is not None
            and row["raw_gap_days"] <= 1
        ):
            continue
        if row["sonarr_season"] is None:
            drop = diverged
        else:
            result = resolver.resolve(row["sonarr_season"], row["sonarr_episode"])
            drop = result is not None and result[0] != row["anidb_anime_id"]
        if drop:
            conn.execute("DELETE FROM episode_anidb_mapping WHERE episode_id = ?", (row["id"],))
            dropped += 1
    conn.commit()
    return dropped


def _reconcile_seasons(conn, show_id):
    changed, disputed = [], []
    now = util.now_utc_iso()
    for season in conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND season_number > 0 ORDER BY season_number",
        (show_id,),
    ).fetchall():
        before = (season["anilist_id"], season["mal_id"])
        if season["manual_override"]:
            tvdb = conn.execute(
                "SELECT external_id FROM show_external_id"
                " WHERE show_id = ? AND service = 'tvdb'",
                (show_id,),
            ).fetchone()
            derived = season_mapping._derive_ids(
                conn, show_id, season["season_number"],
                int(tvdb["external_id"]) if tvdb else None, None,
            )
            if isinstance(derived, tuple) and derived[0] is not None and derived[0] != before[0]:
                disputed.append((season["season_number"], before, derived))
            continue
        after_row = season_mapping.reconcile_season(conn, show_id, season["season_number"])
        after = (after_row["anilist_id"], after_row["mal_id"])
        if after != before:
            changed.append((season["id"], season["season_number"], before, after))
            conn.execute(
                "UPDATE pending_review SET resolved_at = ?, resolved_by_client = 'captains_log',"
                " resolution_note = ?"
                " WHERE entity_type = 'season' AND entity_id = ? AND resolved_at IS NULL"
                "   AND source = 'fribb' AND field IN ('anilist_id', 'mal_id')",
                (now, f"applied by {CHANGED_BY} (2026-09-23): season identity re-derived "
                      "from Memory Alpha absolute order", season["id"]),
            )
    conn.commit()
    return changed, disputed


def _restore_air_dates(conn, season_ids):
    now = util.now_utc_iso()
    restored = 0
    for season_id in season_ids:
        for row in conn.execute(
            "SELECT id, air_date_utc, air_date_raw_sonarr FROM episode"
            " WHERE season_id = ? AND air_date_source = 'anilist'"
            "   AND air_date_raw_sonarr IS NOT NULL"
            "   AND ABS(julianday(air_date_utc) - julianday(air_date_raw_sonarr)) > ?",
            (season_id, metadata._ANILIST_SCHEDULE_MAX_DRIFT_DAYS),
        ).fetchall():
            conn.execute(
                "UPDATE episode SET air_date_utc = ?, air_date_source = 'sonarr', updated_at = ?"
                " WHERE id = ?",
                (row["air_date_raw_sonarr"], now, row["id"]),
            )
            conn.execute(
                "INSERT INTO air_date_change (id, episode_id, previous_air_date_utc,"
                " new_air_date_utc, previous_source, new_source, changed_at, changed_by)"
                " VALUES (?, ?, ?, ?, 'anilist', 'sonarr', ?, ?)",
                (ids.generate_id(conn, "g"), row["id"], row["air_date_utc"],
                 row["air_date_raw_sonarr"], now, CHANGED_BY),
            )
            restored += 1
    conn.commit()
    return restored


def _report(show_id, title, before, after, notes):
    (s_before, e_before), (s_after, e_after) = before, after
    print(f"\n=== {show_id}  {title}")
    for note in notes:
        print(f"  {note}")
    for n in sorted(set(s_before) | set(s_after)):
        if s_before.get(n) != s_after.get(n):
            print(f"  season {n}: anilist/mal/override {s_before.get(n)} -> {s_after.get(n)}")
    keys = ("sonarr_season", "sonarr_episode", "anidb_anime_id", "anidb_epno", "confidence",
            "air_date_utc", "air_date_source", "file_path_sonarr", "available_via_sonarr")
    changes = {}
    for eid, e in e_after.items():
        b = e_before.get(eid, {})
        for k in keys:
            if b.get(k) != e.get(k):
                changes.setdefault(k, []).append((e["season"], e["episode"], b.get(k), e.get(k)))
    for k, rows in changes.items():
        rows.sort(key=lambda r: (r[0], r[1]))
        sample = ", ".join(f"S{s}E{ep}: {str(b)[-40:]} -> {str(a)[-40:]}"
                           for s, ep, b, a in rows[:3])
        print(f"  {k}: {len(rows)} episode(s) changed  e.g. {sample}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--show", action="append", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    if args.dry_run:
        tmp = Path(tempfile.mkdtemp()) / "dry-run.db"
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        src.close()
        dst.close()
        db_path = tmp
        print(f"DRY RUN against a copy: {tmp}")

    cfg = config.load_config()
    config.set_current(cfg)
    conn = db.connect(db_path)
    if not cfg.sonarr_url or not cfg.sonarr_api_key:
        sys.exit("Sonarr is not configured")

    before = {sid: _snapshot(conn, sid) for sid in args.show}
    notes = {sid: [] for sid in args.show}
    with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
        for sid in args.show:
            notes[sid].append("coords: " + _capture_coords(conn, client, sid))
    for sid in args.show:
        dropped = _drop_disagreeing_mappings(conn, sid)
        notes[sid].append(f"mappings dropped for re-derivation: {dropped}")
    anidb.derive_episode_mappings(conn)
    for sid in args.show:
        changed, disputed = _reconcile_seasons(conn, sid)
        for _season_id, n, b, a in changed:
            notes[sid].append(f"season {n} re-derived: {b} -> {a}")
        for n, b, d in disputed:
            notes[sid].append(
                f"season {n} is manual_override {b} but Memory Alpha says {d} — NOT touched"
            )
        restored = _restore_air_dates(conn, [c[0] for c in changed])
        notes[sid].append(f"air dates restored to Sonarr's raw date: {restored}")
    season_ranges.seed_episode_external_ids(conn)
    for sid in args.show:
        audit = local_audit.audit_local_files_for_show(conn, sid)
        notes[sid].append(f"local audit: {audit.get('episodes_corrected', audit)} corrected")
    conn.commit()

    for sid in args.show:
        title = conn.execute(
            "SELECT COALESCE(title_english, title_romaji) FROM show WHERE id = ?", (sid,)
        ).fetchone()[0]
        _report(sid, title, before[sid], _snapshot(conn, sid), notes[sid])
    if args.dry_run:
        print("\n(dry run — the real database was not touched)")


if __name__ == "__main__":
    main()
