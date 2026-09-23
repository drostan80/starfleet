"""Absolute episode order as the identity spine — the Slime regression.

2026-09-23, found live on "That Time I Got Reincarnated as a Slime": LCARS
subdivided TVDB's season 2 into LCARS seasons 2 and 3, so every later LCARS
season number sat one ahead of TVDB's. Everything that treated "LCARS
season N" as "TVDB season N" went wrong together: the season's AniList id
(Fribb by tvdb+season_number), Memory Alpha's AniDB mapping (Anime-Lists fed
LCARS numbers), the local-file audit (Sonarr S04 files on LCARS S4), and the
AniList air-date pass (the 2026 schedule written over the 2024 season).

Fixture, scaled down but the same shape:

    LCARS  S1 abs 1-2 | S2 abs 3 | S3 abs 4 | S4 abs 5-6 | S5 abs 7-8
    TVDB   S1 1-2     | S2 E1    | S2 E2    | S3 1-2     | S4 1-2
    AniDB  100        | 101      | 102      | 103        | 104
    AniList  -        |  -       |  -       | 3003       | 4004
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import (
    anidb,
    anilist_client,
    config,
    fribb,
    local_audit,
    metadata,
    season_mapping,
    sonarr_match,
)

TVDB = 352408
SHOW = "s-slime1"
LOSER = "s-loser1"

# LCARS (season, episode) -> (abs, tvdb season, tvdb episode, anidb id, raw sonarr date)
LAYOUT = {
    (1, 1): (1, 1, 1, 100, "2018-10-02T14:00:00Z"),
    (1, 2): (2, 1, 2, 100, "2018-10-09T14:00:00Z"),
    (2, 1): (3, 2, 1, 101, "2021-01-12T14:00:00Z"),
    (3, 1): (4, 2, 2, 102, "2021-07-06T14:00:00Z"),
    (4, 1): (5, 3, 1, 103, "2024-04-05T14:00:00Z"),
    (4, 2): (6, 3, 2, 103, "2024-04-12T14:00:00Z"),
    (5, 1): (7, 4, 1, 104, "2026-04-03T14:00:00Z"),
    (5, 2): (8, 4, 2, 104, "2026-04-10T14:00:00Z"),
}

DATASET = [
    {"anilist_id": 1001, "mal_id": 5001, "anidb_id": 100, "tvdb_id": TVDB, "type": "TV",
     "season": {"tvdb": 1}},
    {"anilist_id": 1002, "mal_id": 5002, "anidb_id": 101, "tvdb_id": TVDB, "type": "TV",
     "season": {"tvdb": 2}},
    {"anilist_id": 2002, "mal_id": 6002, "anidb_id": 102, "tvdb_id": TVDB, "type": "TV",
     "season": {"tvdb": 2}, "episode_offset": {"tvdb": 1}},
    {"anilist_id": 3003, "mal_id": 5003, "anidb_id": 103, "tvdb_id": TVDB, "type": "TV",
     "season": {"tvdb": 3}},
    {"anilist_id": 4004, "mal_id": 5004, "anidb_id": 104, "tvdb_id": TVDB, "type": "TV",
     "season": {"tvdb": 4}},
]


@pytest.fixture
def conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    db_path = tmp_path / "abs_identity.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{db_path}"},
        check=True,
        capture_output=True,
    )
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    monkeypatch.setattr(fribb, "load_dataset", lambda *a, **kw: DATASET)
    config.set_current(config.Config())
    _seed(c)
    yield c
    c.close()
    config.set_current(config.Config())


def _seed(c):
    for sid in (SHOW, LOSER):
        c.execute(
            "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
            " status, tracked, created_at, updated_at)"
            " VALUES (?, 'episodic', 'anime', 'Slime', 'romaji', 'watching', ?, 'x', 'x')",
            (sid, 1 if sid == SHOW else 0),
        )
        c.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'tvdb', ?, '', 'x')",
            (sid, str(TVDB)),
        )
    c.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'anidb', '100', '', 'x')",
        (SHOW,),
    )
    c.execute(
        "INSERT INTO show_merge (id, winner_show_id, loser_show_id, matched_on, manifest,"
        " merged_at) VALUES ('y-merge1', ?, ?, 'tvdb collision', '{}', 'x')",
        (SHOW, LOSER),
    )
    ranges = {1: (1, 2), 2: (3, 3), 3: (4, 4), 4: (5, 6), 5: (7, 8)}
    for n, (lo, hi) in ranges.items():
        c.execute(
            "INSERT INTO season (id, show_id, season_number, source, abs_start, abs_end,"
            " created_at, updated_at) VALUES (?, ?, ?, 'unmatched', ?, ?, 'x', 'x')",
            (f"z-sea00{n}", SHOW, n, lo, hi),
        )
    for (season, ep), (abs_n, _ts, _te, _aid, raw) in LAYOUT.items():
        c.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, absolute_number,"
            " air_date_utc, air_date_source, air_date_raw_sonarr, season_id,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'regular', ?, ?, 'sonarr', ?, ?, 'x', 'x')",
            (f"e-{season}{ep}xxxx", SHOW, season, ep, abs_n, raw, raw, f"z-sea00{season}"),
        )
    # Anime-Lists: TVDB coordinates -> AniDB, as the real community list has it.
    for aid, tvdb_season, offset in ((100, 1, 0), (101, 2, 0), (102, 2, 1), (103, 3, 0),
                                     (104, 4, 0)):
        c.execute(
            "INSERT INTO anime_list_entry (anidb_id, tvdb_id, default_tvdb_season,"
            " episode_offset, fetched_at) VALUES (?, ?, ?, ?, 'x')",
            (aid, str(TVDB), tvdb_season, offset),
        )
    anidb_eps = {100: [(1, "2018-10-02"), (2, "2018-10-09")], 101: [(1, "2021-01-12")],
                 102: [(1, "2021-07-06")], 103: [(1, "2024-04-05"), (2, "2024-04-12")],
                 104: [(1, "2026-04-03"), (2, "2026-04-10")]}
    for aid, eps in anidb_eps.items():
        c.execute(
            "INSERT INTO anidb_anime (anidb_id, main_title, fetched_at) VALUES (?, 'x', 'x')",
            (aid,),
        )
        for epno, date in eps:
            c.execute(
                "INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, airdate,"
                " fetched_at) VALUES (?, 1, ?, ?, 'x')",
                (aid, epno, date),
            )
    c.commit()


def _sonarr_ep(tvdb_season, tvdb_ep, abs_n, path=None):
    return {
        "seasonNumber": tvdb_season,
        "episodeNumber": tvdb_ep,
        "absoluteEpisodeNumber": abs_n,
        "hasFile": path is not None,
        "episodeFile": {"path": path} if path else None,
    }


def _capture_all(c):
    for (_s, _e), (abs_n, ts, te, _aid, _raw) in LAYOUT.items():
        assert sonarr_match.find_episode(c, [SHOW], _sonarr_ep(ts, te, abs_n)) is not None
    c.commit()


def _episode(c, season, ep):
    return c.execute(
        "SELECT * FROM episode WHERE show_id = ? AND season = ? AND episode = ?",
        (SHOW, season, ep),
    ).fetchone()


def test_merge_loser_is_not_a_sibling(conn):
    assert sonarr_match.sibling_show_ids_for_tvdb(conn, TVDB) == [SHOW]


def test_sonarr_episode_matches_by_absolute_order_not_season_number(conn):
    # Sonarr S03E01 is absolute 5 — LCARS S4E1, even though LCARS S3E1 exists.
    match = sonarr_match.find_episode(conn, [SHOW], _sonarr_ep(3, 1, 5))
    assert (match["season"], match["episode"]) == (4, 1)
    row = _episode(conn, 4, 1)
    assert (row["sonarr_season"], row["sonarr_episode"]) == (3, 1)


def test_new_sonarr_season_routes_past_the_subdivision_offset(conn):
    _capture_all(conn)
    # Sonarr's next season (S05, absolute 9 — beyond every range) lands on
    # LCARS S6, not on top of LCARS S5.
    assert sonarr_match.route_new_episode(conn, SHOW, _sonarr_ep(5, 1, 9)) == (6, 1)


def test_local_audit_puts_sonarr_files_on_the_absolute_match(conn):
    class _Client:
        def episodes(self, series_id, include_episode_file=False):
            return [_sonarr_ep(4, 1, 7, "/media/Slime - S04E01 - 007.mkv")]

    local_audit._audit_sonarr_series(
        conn, _Client(), SHOW, {"id": 1, "tvdbId": TVDB, "path": "/nowhere"}, "now",
        walk_orphans=False,
    )
    assert _episode(conn, 5, 1)["file_path_sonarr"] == "/media/Slime - S04E01 - 007.mkv"
    assert _episode(conn, 4, 1)["file_path_sonarr"] is None


def test_memory_alpha_resolves_anime_lists_with_real_tvdb_coordinates(conn):
    _capture_all(conn)
    anidb.derive_episode_mappings(conn)
    mapped = {
        (r["season"], r["episode"]): r["anidb_anime_id"]
        for r in conn.execute(
            "SELECT e.season, e.episode, m.anidb_anime_id FROM episode e"
            " JOIN episode_anidb_mapping m ON m.episode_id = e.id"
        )
    }
    assert mapped == {k: v[3] for k, v in LAYOUT.items()}


def test_memory_alpha_skips_uncaptured_rows_on_a_diverged_show(conn):
    # One captured row proves divergence; the rest have no TVDB coordinates
    # yet and must not be resolved with LCARS numbering.
    sonarr_match.find_episode(conn, [SHOW], _sonarr_ep(3, 1, 5))
    anidb.derive_episode_mappings(conn)
    rows = conn.execute("SELECT episode_id, anidb_anime_id FROM episode_anidb_mapping").fetchall()
    assert [(r["episode_id"], r["anidb_anime_id"]) for r in rows] == [("e-41xxxx", 103)]


def test_air_date_arbiter_never_locks_against_an_anilist_sourced_date(conn):
    # S4E1 carries a *2026* date written by AniList through a wrong link,
    # and no raw Sonarr date: the arbiter must not "confirm" it.
    conn.execute(
        "UPDATE episode SET air_date_utc = '2026-04-03T14:00:00Z', air_date_source = 'anilist',"
        " air_date_raw_sonarr = NULL WHERE id = 'e-41xxxx'"
    )
    conn.execute(
        "UPDATE episode SET sonarr_season = 3, sonarr_episode = 1 WHERE id = 'e-41xxxx'"
    )
    # Pretend Anime-Lists wrongly points TVDB S3 at AniDB 104 (the 2026 entry).
    conn.execute("UPDATE anime_list_entry SET default_tvdb_season = 99 WHERE anidb_id = 103")
    conn.execute("UPDATE anime_list_entry SET default_tvdb_season = 3 WHERE anidb_id = 104")
    conn.commit()
    anidb.derive_episode_mappings(conn)
    row = conn.execute(
        "SELECT anidb_anime_id, confidence FROM episode_anidb_mapping WHERE episode_id = 'e-41xxxx'"
    ).fetchone()
    assert row["confidence"] == "auto"  # not locked, so a corrected list can still fix it


def test_season_identity_comes_from_memory_alpha_not_the_season_number(conn):
    _capture_all(conn)
    anidb.derive_episode_mappings(conn)
    s4 = season_mapping.reconcile_season(conn, SHOW, 4)
    s5 = season_mapping.reconcile_season(conn, SHOW, 5)
    assert (s4["anilist_id"], s4["mal_id"]) == (3003, 5003)
    assert (s5["anilist_id"], s5["mal_id"]) == (4004, 5004)


def test_season_identity_from_tvdb_coordinates_before_any_anidb_mapping(conn):
    s4 = season_mapping.reconcile_season(conn, SHOW, 4, tvdb_coords={(3, 1), (3, 2)})
    assert s4["anilist_id"] == 3003


def test_ambiguous_evidence_leaves_the_stored_id_alone(conn):
    conn.execute("UPDATE season SET anilist_id = 3003, source = 'fribb' WHERE id = 'z-sea004'")
    conn.commit()
    # Coordinates spanning two TVDB seasons: no claim, no clearing.
    s4 = season_mapping.reconcile_season(conn, SHOW, 4, tvdb_coords={(3, 1), (4, 1)})
    assert s4["anilist_id"] == 3003


def test_anilist_air_dates_skip_a_season_whose_schedule_is_far_from_sonarr(conn, monkeypatch):
    # LCARS S4 (2024) wrongly linked to the 2026 entry — same episode count.
    conn.execute("UPDATE season SET anilist_id = 4004 WHERE id = 'z-sea004'")
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc,"
        " air_date_source, air_date_raw_sonarr, season_id, created_at, updated_at)"
        " VALUES ('e-43xxxx', ?, 4, 3, 'regular', '2024-04-19T14:00:00Z', 'sonarr',"
        " '2024-04-19T14:00:00Z', 'z-sea004', 'x', 'x')",
        (SHOW,),
    )
    conn.commit()
    monkeypatch.setattr(
        anilist_client,
        "fetch_airing_schedule",
        lambda anilist_id: {
            "episodes": 3,
            "nodes": [
                {"episode": 1, "airingAt": 1775224800},  # 2026-04-03
                {"episode": 2, "airingAt": 1775829600},  # 2026-04-10
                {"episode": 3, "airingAt": 1776434400},  # 2026-04-17
            ],
        },
    )
    metadata._reconcile_air_dates(conn, {"id": SHOW})
    conn.commit()
    assert _episode(conn, 4, 1)["air_date_utc"] == "2024-04-05T14:00:00Z"
    review = conn.execute(
        "SELECT proposed_value_chain FROM pending_review WHERE entity_id = 'z-sea004'"
    ).fetchone()
    assert "wrong AniList entry" in review["proposed_value_chain"]
