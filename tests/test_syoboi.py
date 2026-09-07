"""Tests for Syoboi Calendar integration (arm.py + syoboi.py)."""

import json
import sqlite3
from unittest import mock

# ── ARM tests ──

class TestArmLoadDataset:
    """arm.load_dataset caching and download."""

    def test_load_from_cache(self, tmp_path):
        from lcars import arm

        cache_file = tmp_path / "arm.json"
        data = [{"anilist_id": 1, "syobocal_tid": 100}]
        cache_file.write_text(json.dumps(data))

        with mock.patch.object(arm, "CACHE_PATH", cache_file):
            with mock.patch.object(arm, "_parse_cache", {}):
                result = arm.load_dataset()

        assert len(result) == 1
        assert result[0]["anilist_id"] == 1

    def test_missing_triggers_download(self, tmp_path):
        from lcars import arm

        cache_file = tmp_path / "arm.json"
        # File doesn't exist → triggers download
        fresh_data = [{"anilist_id": 2, "syobocal_tid": 200}]

        with mock.patch.object(arm, "CACHE_PATH", cache_file):
            with mock.patch.object(arm, "_parse_cache", {}):
                with mock.patch("lcars.arm.httpx.get") as mock_get:
                    mock_resp = mock.Mock()
                    mock_resp.json.return_value = fresh_data
                    mock_resp.raise_for_status = mock.Mock()
                    mock_get.return_value = mock_resp
                    result = arm.load_dataset()

        assert len(result) == 1
        assert result[0]["anilist_id"] == 2
        assert cache_file.exists()


class TestArmBuildIndex:
    def test_anilist_to_syoboi_index(self):
        from lcars import arm

        dataset = [
            {"anilist_id": 1, "syobocal_tid": 100, "mal_id": 10},
            {"anilist_id": 2, "mal_id": 20},  # no syobocal_tid
            {"anilist_id": 3, "syobocal_tid": 300},
        ]
        with mock.patch.object(arm, "_al_to_syoboi_cache", {}):
            index = arm.build_anilist_to_syoboi_index(dataset)

        assert index == {1: 100, 3: 300}
        assert 2 not in index


class TestArmSeedSyoboiIds:
    def test_seeds_new_ids(self):
        from lcars import arm

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE show (
                id TEXT PRIMARY KEY, tracked INTEGER DEFAULT 1,
                tracking_space TEXT DEFAULT 'anime'
            );
            CREATE TABLE show_external_id (
                show_id TEXT, service TEXT, external_id TEXT,
                url TEXT, created_at TEXT,
                UNIQUE(show_id, service)
            );
            INSERT INTO show VALUES ('s-001', 1, 'anime');
            INSERT INTO show VALUES ('s-002', 1, 'anime');
            INSERT INTO show_external_id VALUES ('s-001', 'anilist', '1', '', '');
            INSERT INTO show_external_id VALUES ('s-002', 'anilist', '2', '', '');
        """)

        dataset = [
            {"anilist_id": 1, "syobocal_tid": 100},
            {"anilist_id": 2, "syobocal_tid": 200},
        ]
        with mock.patch.object(arm, "_al_to_syoboi_cache", {}):
            inserted = arm.seed_syoboi_external_ids(conn, dataset)

        assert inserted == 2
        rows = conn.execute(
            "SELECT * FROM show_external_id WHERE service = 'syoboi'"
        ).fetchall()
        assert len(rows) == 2
        ids = {r["external_id"] for r in rows}
        assert ids == {"100", "200"}

    def test_skips_existing(self):
        from lcars import arm

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE show (
                id TEXT PRIMARY KEY, tracked INTEGER DEFAULT 1,
                tracking_space TEXT DEFAULT 'anime'
            );
            CREATE TABLE show_external_id (
                show_id TEXT, service TEXT, external_id TEXT,
                url TEXT, created_at TEXT,
                UNIQUE(show_id, service)
            );
            INSERT INTO show VALUES ('s-001', 1, 'anime');
            INSERT INTO show_external_id VALUES ('s-001', 'anilist', '1', '', '');
            INSERT INTO show_external_id VALUES ('s-001', 'syoboi', '100', '', '');
        """)

        dataset = [{"anilist_id": 1, "syobocal_tid": 100}]
        with mock.patch.object(arm, "_al_to_syoboi_cache", {}):
            inserted = arm.seed_syoboi_external_ids(conn, dataset)

        assert inserted == 0


# ── Syoboi tests ──

class TestJstToUtc:
    def test_evening(self):
        from lcars.syoboi import _jst_to_utc
        assert _jst_to_utc("2023-10-06 22:30:00") == "2023-10-06T13:30:00Z"

    def test_after_midnight(self):
        from lcars.syoboi import _jst_to_utc
        # 01:30 JST = 16:30 UTC previous day
        assert _jst_to_utc("2023-10-07 01:30:00") == "2023-10-06T16:30:00Z"

    def test_noon(self):
        from lcars.syoboi import _jst_to_utc
        assert _jst_to_utc("2023-10-06 12:00:00") == "2023-10-06T03:00:00Z"


class TestParseCount:
    def test_normal(self):
        from lcars.syoboi import _parse_count
        assert _parse_count("5") == 5

    def test_empty(self):
        from lcars.syoboi import _parse_count
        assert _parse_count("") is None
        assert _parse_count(None) is None

    def test_non_numeric(self):
        from lcars.syoboi import _parse_count
        assert _parse_count("SP1") is None


class TestIngestPrograms:
    def test_basic_ingest(self):
        from lcars import syoboi

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE syoboi_program (
                pid INTEGER PRIMARY KEY,
                tid INTEGER NOT NULL,
                chid INTEGER NOT NULL,
                count INTEGER,
                st_time_jst TEXT, ed_time_jst TEXT,
                st_time_utc TEXT, ed_time_utc TEXT,
                subtitle TEXT,
                flag INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0,
                revision TEXT, last_update TEXT,
                fetched_at TEXT NOT NULL
            )
        """)

        programs = [
            {
                "pid": 1001, "tid": 100, "chid": 71, "count": 1,
                "st_time_jst": "2023-10-06 22:30:00",
                "ed_time_jst": "2023-10-06 23:00:00",
                "st_time_utc": "2023-10-06T13:30:00Z",
                "ed_time_utc": "2023-10-06T14:00:00Z",
                "subtitle": "旅立ちの章",
                "flag": 0, "deleted": 0,
                "revision": "1", "last_update": "2023-10-01 00:00:00",
            },
            {
                "pid": 1002, "tid": 100, "chid": 71, "count": 2,
                "st_time_jst": "2023-10-13 22:30:00",
                "ed_time_jst": "2023-10-13 23:00:00",
                "st_time_utc": "2023-10-13T13:30:00Z",
                "ed_time_utc": "2023-10-13T14:00:00Z",
                "subtitle": "第2話",
                "flag": 0, "deleted": 0,
                "revision": "1", "last_update": "2023-10-01 00:00:00",
            },
        ]

        stored = syoboi.ingest_programs(conn, programs, "2023-10-01T00:00:00Z")
        assert stored == 2

        rows = conn.execute("SELECT * FROM syoboi_program ORDER BY pid").fetchall()
        assert len(rows) == 2
        assert rows[0]["subtitle"] == "旅立ちの章"

    def test_upsert(self):
        from lcars import syoboi

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE syoboi_program (
                pid INTEGER PRIMARY KEY,
                tid INTEGER NOT NULL,
                chid INTEGER NOT NULL,
                count INTEGER,
                st_time_jst TEXT, ed_time_jst TEXT,
                st_time_utc TEXT, ed_time_utc TEXT,
                subtitle TEXT,
                flag INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0,
                revision TEXT, last_update TEXT,
                fetched_at TEXT NOT NULL
            )
        """)

        prog = {
            "pid": 1001, "tid": 100, "chid": 71, "count": 1,
            "st_time_jst": "2023-10-06 22:30:00",
            "ed_time_jst": "2023-10-06 23:00:00",
            "st_time_utc": "2023-10-06T13:30:00Z",
            "ed_time_utc": "2023-10-06T14:00:00Z",
            "subtitle": "旧", "flag": 0, "deleted": 0,
            "revision": "1", "last_update": "2023-10-01",
        }
        syoboi.ingest_programs(conn, [prog], "t1")

        # Update subtitle
        prog["subtitle"] = "新"
        syoboi.ingest_programs(conn, [prog], "t2")

        row = conn.execute("SELECT subtitle FROM syoboi_program WHERE pid = 1001").fetchone()
        assert row["subtitle"] == "新"


class TestFillAirdateGaps:
    def test_fills_null_from_syoboi(self):
        from lcars import syoboi

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE show (id TEXT PRIMARY KEY);
            CREATE TABLE show_external_id (
                show_id TEXT, service TEXT, external_id TEXT,
                url TEXT, created_at TEXT
            );
            CREATE TABLE episode (
                id TEXT PRIMARY KEY, show_id TEXT, season INTEGER,
                episode INTEGER, kind TEXT,
                air_date_utc TEXT,
                air_date_source TEXT CHECK (air_date_source IN
                    ('sonarr','anilist','animeschedule','manual','tvmaze','anidb','syoboi')),
                sonarr_season INTEGER, sonarr_episode INTEGER,
                created_at TEXT, updated_at TEXT, state TEXT DEFAULT 'unwatched',
                UNIQUE(show_id, season, episode)
            );
            CREATE TABLE episode_anidb_mapping (
                episode_id TEXT, anidb_anime_id INTEGER,
                anidb_season INTEGER, anidb_epno INTEGER
            );
            CREATE TABLE syoboi_program (
                pid INTEGER PRIMARY KEY,
                tid INTEGER NOT NULL,
                chid INTEGER NOT NULL,
                count INTEGER,
                st_time_jst TEXT, ed_time_jst TEXT,
                st_time_utc TEXT, ed_time_utc TEXT,
                subtitle TEXT,
                flag INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0,
                revision TEXT, last_update TEXT,
                fetched_at TEXT NOT NULL
            );

            INSERT INTO show VALUES ('s-001');
            INSERT INTO show_external_id VALUES ('s-001', 'syoboi', '100', '', '');
            INSERT INTO show_external_id VALUES ('s-001', 'anidb', '1000', '', '');

            -- Episode with NULL airdate
            INSERT INTO episode VALUES (
                'e-000001', 's-001', 1, 1, 'regular',
                NULL, NULL, 1, 1,
                '2023-01-01', '2023-01-01', 'unwatched'
            );
            -- Episode with existing airdate (should NOT be overwritten)
            INSERT INTO episode VALUES (
                'e-000002', 's-001', 1, 2, 'regular',
                '2023-10-13T00:00:00Z', 'sonarr', 1, 2,
                '2023-01-01', '2023-01-01', 'unwatched'
            );

            -- AniDB mappings (both under primary anidb_anime_id 1000)
            INSERT INTO episode_anidb_mapping VALUES ('e-000001', 1000, 1, 1);
            INSERT INTO episode_anidb_mapping VALUES ('e-000002', 1000, 1, 2);

            -- Syoboi broadcasts for ep 1 on two channels
            INSERT INTO syoboi_program VALUES (
                1001, 100, 71, 1,
                '2023-10-06 22:30:00', '2023-10-06 23:00:00',
                '2023-10-06T13:30:00Z', '2023-10-06T14:00:00Z',
                NULL, 0, 0, '1', '', '2023-10-01'
            );
            INSERT INTO syoboi_program VALUES (
                1002, 100, 72, 1,
                '2023-10-07 01:00:00', '2023-10-07 01:30:00',
                '2023-10-06T16:00:00Z', '2023-10-06T16:30:00Z',
                NULL, 0, 0, '1', '', '2023-10-01'
            );
            -- Syoboi broadcast for ep 2 (should not overwrite existing)
            INSERT INTO syoboi_program VALUES (
                1003, 100, 71, 2,
                '2023-10-13 22:30:00', '2023-10-13 23:00:00',
                '2023-10-13T13:30:00Z', '2023-10-13T14:00:00Z',
                NULL, 0, 0, '1', '', '2023-10-01'
            );
        """)

        filled = syoboi.fill_airdate_gaps(conn)
        assert filled == 1

        # Check ep 1 got earliest broadcast
        ep1 = conn.execute(
            "SELECT air_date_utc, air_date_source FROM episode WHERE id = 'e-000001'"
        ).fetchone()
        assert ep1["air_date_utc"] == "2023-10-06T13:30:00Z"
        assert ep1["air_date_source"] == "syoboi"

        # Check ep 2 was NOT overwritten
        ep2 = conn.execute(
            "SELECT air_date_utc, air_date_source FROM episode WHERE id = 'e-000002'"
        ).fetchone()
        assert ep2["air_date_utc"] == "2023-10-13T00:00:00Z"
        assert ep2["air_date_source"] == "sonarr"

    def test_skips_deleted_broadcasts(self):
        from lcars import syoboi

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE show (id TEXT PRIMARY KEY);
            CREATE TABLE show_external_id (
                show_id TEXT, service TEXT, external_id TEXT,
                url TEXT, created_at TEXT
            );
            CREATE TABLE episode (
                id TEXT PRIMARY KEY, show_id TEXT, season INTEGER,
                episode INTEGER, kind TEXT,
                air_date_utc TEXT,
                air_date_source TEXT CHECK (air_date_source IN
                    ('sonarr','anilist','animeschedule','manual','tvmaze','anidb','syoboi')),
                sonarr_season INTEGER, sonarr_episode INTEGER,
                created_at TEXT, updated_at TEXT, state TEXT DEFAULT 'unwatched',
                UNIQUE(show_id, season, episode)
            );
            CREATE TABLE episode_anidb_mapping (
                episode_id TEXT, anidb_anime_id INTEGER,
                anidb_season INTEGER, anidb_epno INTEGER
            );
            CREATE TABLE syoboi_program (
                pid INTEGER PRIMARY KEY,
                tid INTEGER NOT NULL,
                chid INTEGER NOT NULL,
                count INTEGER,
                st_time_jst TEXT, ed_time_jst TEXT,
                st_time_utc TEXT, ed_time_utc TEXT,
                subtitle TEXT,
                flag INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0,
                revision TEXT, last_update TEXT,
                fetched_at TEXT NOT NULL
            );

            INSERT INTO show VALUES ('s-001');
            INSERT INTO show_external_id VALUES ('s-001', 'syoboi', '100', '', '');
            INSERT INTO show_external_id VALUES ('s-001', 'anidb', '1000', '', '');
            INSERT INTO episode VALUES (
                'e-000001', 's-001', 1, 1, 'regular',
                NULL, NULL, 1, 1,
                '2023-01-01', '2023-01-01', 'unwatched'
            );
            INSERT INTO episode_anidb_mapping VALUES ('e-000001', 1000, 1, 1);

            -- Only deleted broadcast
            INSERT INTO syoboi_program VALUES (
                1001, 100, 71, 1,
                '2023-10-06 22:30:00', '2023-10-06 23:00:00',
                '2023-10-06T13:30:00Z', '2023-10-06T14:00:00Z',
                NULL, 0, 1, '1', '', '2023-10-01'
            );
        """)

        filled = syoboi.fill_airdate_gaps(conn)
        assert filled == 0

    def test_multi_entry_collision_guard(self):
        """S2 ep 1 (different anidb_anime_id, same epno=1) must NOT get S1's airdate."""
        from lcars import syoboi

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE show (id TEXT PRIMARY KEY);
            CREATE TABLE show_external_id (
                show_id TEXT, service TEXT, external_id TEXT,
                url TEXT, created_at TEXT
            );
            CREATE TABLE episode (
                id TEXT PRIMARY KEY, show_id TEXT, season INTEGER,
                episode INTEGER, kind TEXT,
                air_date_utc TEXT,
                air_date_source TEXT CHECK (air_date_source IN
                    ('sonarr','anilist','animeschedule','manual','tvmaze','anidb','syoboi')),
                sonarr_season INTEGER, sonarr_episode INTEGER,
                created_at TEXT, updated_at TEXT, state TEXT DEFAULT 'unwatched',
                UNIQUE(show_id, season, episode)
            );
            CREATE TABLE episode_anidb_mapping (
                episode_id TEXT, anidb_anime_id INTEGER,
                anidb_season INTEGER, anidb_epno INTEGER
            );
            CREATE TABLE syoboi_program (
                pid INTEGER PRIMARY KEY,
                tid INTEGER NOT NULL,
                chid INTEGER NOT NULL,
                count INTEGER,
                st_time_jst TEXT, ed_time_jst TEXT,
                st_time_utc TEXT, ed_time_utc TEXT,
                subtitle TEXT,
                flag INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0,
                revision TEXT, last_update TEXT,
                fetched_at TEXT NOT NULL
            );

            INSERT INTO show VALUES ('s-001');
            -- Primary AniDB is 1000 (S1), Syoboi TID 100 (maps to S1)
            INSERT INTO show_external_id VALUES ('s-001', 'syoboi', '100', '', '');
            INSERT INTO show_external_id VALUES ('s-001', 'anidb', '1000', '', '');

            -- S1 E1: anidb_anime_id 1000, epno 1 — matches primary
            INSERT INTO episode VALUES (
                'e-s1e1', 's-001', 1, 1, 'regular',
                NULL, NULL, 1, 1,
                '2023-01-01', '2023-01-01', 'unwatched'
            );
            INSERT INTO episode_anidb_mapping VALUES ('e-s1e1', 1000, 1, 1);

            -- S2 E1: anidb_anime_id 2000, epno 1 — different entry, same epno!
            INSERT INTO episode VALUES (
                'e-s2e1', 's-001', 2, 1, 'regular',
                NULL, NULL, 2, 1,
                '2023-01-01', '2023-01-01', 'unwatched'
            );
            INSERT INTO episode_anidb_mapping VALUES ('e-s2e1', 2000, 1, 1);

            -- Syoboi broadcast: TID 100, count 1 (S1's episode 1)
            INSERT INTO syoboi_program VALUES (
                1001, 100, 71, 1,
                '2023-10-06 22:30:00', '2023-10-06 23:00:00',
                '2023-10-06T13:30:00Z', '2023-10-06T14:00:00Z',
                NULL, 0, 0, '1', '', '2023-10-01'
            );
        """)

        filled = syoboi.fill_airdate_gaps(conn)
        # Only S1 E1 should be filled, NOT S2 E1
        assert filled == 1

        s1 = conn.execute(
            "SELECT air_date_utc FROM episode WHERE id = 'e-s1e1'"
        ).fetchone()
        assert s1["air_date_utc"] == "2023-10-06T13:30:00Z"

        s2 = conn.execute(
            "SELECT air_date_utc FROM episode WHERE id = 'e-s2e1'"
        ).fetchone()
        assert s2["air_date_utc"] is None  # Must remain NULL


class TestRewireAirdates:
    def _make_db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE show (id TEXT PRIMARY KEY);
            CREATE TABLE show_external_id (
                show_id TEXT, service TEXT, external_id TEXT,
                url TEXT, created_at TEXT
            );
            CREATE TABLE episode (
                id TEXT PRIMARY KEY, show_id TEXT, season INTEGER,
                episode INTEGER, kind TEXT,
                air_date_utc TEXT,
                air_date_source TEXT CHECK (air_date_source IN
                    ('sonarr','anilist','animeschedule','manual','tvmaze','anidb','syoboi')),
                sonarr_season INTEGER, sonarr_episode INTEGER,
                created_at TEXT, updated_at TEXT, state TEXT DEFAULT 'unwatched',
                UNIQUE(show_id, season, episode)
            );
            CREATE TABLE episode_anidb_mapping (
                episode_id TEXT, anidb_anime_id INTEGER,
                anidb_season INTEGER, anidb_epno INTEGER
            );
            CREATE TABLE syoboi_program (
                pid INTEGER PRIMARY KEY,
                tid INTEGER NOT NULL,
                chid INTEGER NOT NULL,
                count INTEGER,
                st_time_jst TEXT, ed_time_jst TEXT,
                st_time_utc TEXT, ed_time_utc TEXT,
                subtitle TEXT,
                flag INTEGER DEFAULT 0,
                deleted INTEGER DEFAULT 0,
                revision TEXT, last_update TEXT,
                fetched_at TEXT NOT NULL
            );

            INSERT INTO show VALUES ('s-001');
            INSERT INTO show_external_id VALUES ('s-001', 'syoboi', '100', '', '');
            INSERT INTO show_external_id VALUES ('s-001', 'anidb', '1000', '', '');

            -- Ep with sonarr date (wrong, will be corrected)
            INSERT INTO episode VALUES (
                'e-001', 's-001', 1, 1, 'regular',
                '2023-10-07T00:00:00Z', 'sonarr', 1, 1,
                '', '', 'unwatched'
            );
            -- Ep with manual date (must NOT be overwritten)
            INSERT INTO episode VALUES (
                'e-002', 's-001', 1, 2, 'regular',
                '2023-10-14T00:00:00Z', 'manual', 1, 2,
                '', '', 'unwatched'
            );
            -- Ep already correct from syoboi (skip)
            INSERT INTO episode VALUES (
                'e-003', 's-001', 1, 3, 'regular',
                '2023-10-20T13:30:00Z', 'syoboi', 1, 3,
                '', '', 'unwatched'
            );

            INSERT INTO episode_anidb_mapping VALUES ('e-001', 1000, 1, 1);
            INSERT INTO episode_anidb_mapping VALUES ('e-002', 1000, 1, 2);
            INSERT INTO episode_anidb_mapping VALUES ('e-003', 1000, 1, 3);

            -- Syoboi broadcasts
            INSERT INTO syoboi_program VALUES (
                1001, 100, 71, 1,
                '2023-10-06 22:30:00', '2023-10-06 23:00:00',
                '2023-10-06T13:30:00Z', '2023-10-06T14:00:00Z',
                NULL, 0, 0, '1', '', 't'
            );
            INSERT INTO syoboi_program VALUES (
                1002, 100, 71, 2,
                '2023-10-13 22:30:00', '2023-10-13 23:00:00',
                '2023-10-13T13:30:00Z', '2023-10-13T14:00:00Z',
                NULL, 0, 0, '1', '', 't'
            );
            INSERT INTO syoboi_program VALUES (
                1003, 100, 71, 3,
                '2023-10-20 22:30:00', '2023-10-20 23:00:00',
                '2023-10-20T13:30:00Z', '2023-10-20T14:00:00Z',
                NULL, 0, 0, '1', '', 't'
            );
        """)
        return conn

    def test_rewires_sonarr_not_manual(self):
        from lcars import syoboi

        conn = self._make_db()
        result = syoboi.rewire_airdates(conn)

        # Only e-001 (sonarr) should be updated — manual and syoboi skipped
        assert result["updated"] == 1
        assert result["by_source"] == {"sonarr": 1}

        ep1 = conn.execute(
            "SELECT air_date_utc, air_date_source FROM episode WHERE id='e-001'"
        ).fetchone()
        assert ep1["air_date_utc"] == "2023-10-06T13:30:00Z"
        assert ep1["air_date_source"] == "syoboi"

        ep2 = conn.execute(
            "SELECT air_date_utc, air_date_source FROM episode WHERE id='e-002'"
        ).fetchone()
        assert ep2["air_date_utc"] == "2023-10-14T00:00:00Z"
        assert ep2["air_date_source"] == "manual"

    def test_dry_run_no_writes(self):
        from lcars import syoboi

        conn = self._make_db()
        result = syoboi.rewire_airdates(conn, dry_run=True)

        assert result["would_update"] == 1
        assert result["updated"] == 0

        # Nothing changed
        ep1 = conn.execute("SELECT air_date_source FROM episode WHERE id='e-001'").fetchone()
        assert ep1["air_date_source"] == "sonarr"
