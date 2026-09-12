"""Tests for Wikidata + TVmaze integration (Memory Alpha step 11)."""

import sqlite3
from unittest.mock import patch

# ── Wikidata module tests ──


class TestWikidataNormalize:
    """Test SPARQL binding normalization."""

    def test_normalizes_bindings(self):
        from lcars.wikidata import _normalize_bindings

        raw = {
            "results": {
                "bindings": [
                    {
                        "tvdb": {"type": "literal", "value": "123"},
                        "tmdb": {"type": "literal", "value": "456"},
                        "imdb": {"type": "literal", "value": "tt0123"},
                    },
                    {
                        "tvdb": {"type": "literal", "value": "789"},
                        # no tmdb/imdb
                    },
                ]
            }
        }
        records = _normalize_bindings(raw)
        assert len(records) == 2
        assert records[0] == {"tvdb": "123", "tmdb": "456", "imdb": "tt0123"}
        assert records[1] == {"tvdb": "789", "tmdb": None, "imdb": None}

    def test_skips_missing_tvdb(self):
        from lcars.wikidata import _normalize_bindings

        raw = {"results": {"bindings": [
            {"tmdb": {"value": "456"}, "imdb": {"value": "tt0123"}},
        ]}}
        assert _normalize_bindings(raw) == []


class TestWikidataIndexes:
    """Test TMDB→TVDB and IMDB→TVDB index building."""

    def test_tmdb_to_tvdb_index(self):
        from lcars.wikidata import build_tmdb_to_tvdb_index

        data = [
            {"tvdb": "100", "tmdb": "200", "imdb": "tt001"},
            {"tvdb": "101", "tmdb": None, "imdb": "tt002"},
            {"tvdb": "102", "tmdb": "203", "imdb": None},
        ]
        idx = build_tmdb_to_tvdb_index(data)
        assert idx["200"] == "100"
        assert idx["203"] == "102"
        assert "None" not in idx
        assert len(idx) == 2

    def test_imdb_to_tvdb_index(self):
        from lcars.wikidata import build_imdb_to_tvdb_index

        data = [
            {"tvdb": "100", "tmdb": "200", "imdb": "tt001"},
            {"tvdb": "101", "tmdb": None, "imdb": None},
        ]
        idx = build_imdb_to_tvdb_index(data)
        assert idx["tt001"] == "100"
        assert len(idx) == 1

    def test_first_write_wins(self):
        from lcars.wikidata import build_tmdb_to_tvdb_index

        data = [
            {"tvdb": "100", "tmdb": "200", "imdb": None},
            {"tvdb": "999", "tmdb": "200", "imdb": None},  # duplicate tmdb
        ]
        idx = build_tmdb_to_tvdb_index(data)
        assert idx["200"] == "100"  # first wins


# ── TVmaze module tests ──


class TestTvmazeTombstone:
    """Verify tombstone prevents re-fetching."""

    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE tvmaze_episode (
                tvmaze_show_id INTEGER NOT NULL,
                season INTEGER NOT NULL,
                episode INTEGER NOT NULL,
                title TEXT,
                airdate TEXT,
                airstamp TEXT,
                airtime TEXT,
                runtime_minutes INTEGER,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (tvmaze_show_id, season, episode)
            )
        """)

    def test_tombstone_prevents_reselection(self):
        from lcars.tvmaze import _write_tvmaze_tombstone

        now = "2026-09-07T12:00:00Z"
        _write_tvmaze_tombstone(self.conn, 12345, now)

        # Tombstone is (12345, 0, 0) — the NOT EXISTS check in drip
        # looks for any tvmaze_episode row for that show_id
        row = self.conn.execute(
            "SELECT * FROM tvmaze_episode WHERE tvmaze_show_id = 12345"
        ).fetchone()
        assert row is not None
        assert row[1] == 0  # season
        assert row[2] == 0  # episode


class TestTvmazeIngest:
    """Test episode ingestion."""

    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE tvmaze_episode (
                tvmaze_show_id INTEGER NOT NULL,
                season INTEGER NOT NULL,
                episode INTEGER NOT NULL,
                title TEXT,
                airdate TEXT,
                airstamp TEXT,
                airtime TEXT,
                runtime_minutes INTEGER,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (tvmaze_show_id, season, episode)
            )
        """)

    def test_ingests_episodes(self):
        from lcars.tvmaze import ingest_episodes

        episodes = [
            {"season": 1, "number": 1, "name": "Pilot",
             "airdate": "2020-01-01", "airstamp": "2020-01-02T01:00:00+00:00",
             "airtime": "20:00", "runtime": 42},
            {"season": 1, "number": 2, "name": "Episode 2",
             "airdate": "2020-01-08", "airstamp": "2020-01-09T01:00:00+00:00",
             "airtime": "20:00", "runtime": 42},
        ]
        count = ingest_episodes(self.conn, 100, episodes, "2026-09-07T12:00:00Z")
        assert count == 2

        rows = self.conn.execute(
            "SELECT * FROM tvmaze_episode WHERE tvmaze_show_id = 100"
        ).fetchall()
        assert len(rows) == 2

    def test_skips_null_numbering(self):
        from lcars.tvmaze import ingest_episodes

        episodes = [
            {"season": None, "number": None, "name": "Special",
             "airdate": "2020-06-01", "airstamp": "2020-06-02T00:00:00+00:00",
             "airtime": "", "runtime": 60},
            {"season": 1, "number": 1, "name": "Real Ep",
             "airdate": "2020-01-01", "airstamp": "2020-01-02T01:00:00+00:00",
             "airtime": "20:00", "runtime": 42},
        ]
        count = ingest_episodes(self.conn, 100, episodes, "2026-09-07T12:00:00Z")
        assert count == 1  # only the non-null one


class TestTvmazeAirdateFill:
    """Test airdate gap filling from TVmaze."""

    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        # Minimal schema for the join
        self.conn.execute("""
            CREATE TABLE show (id TEXT PRIMARY KEY, tracked INTEGER DEFAULT 1)
        """)
        self.conn.execute("""
            CREATE TABLE show_external_id (
                show_id TEXT, service TEXT, external_id TEXT,
                url TEXT, created_at TEXT,
                UNIQUE(show_id, service)
            )
        """)
        self.conn.execute("""
            CREATE TABLE episode (
                id TEXT PRIMARY KEY,
                show_id TEXT, season INTEGER, episode INTEGER,
                kind TEXT DEFAULT 'regular',
                air_date_utc TEXT,
                air_date_source TEXT,
                sonarr_season INTEGER, sonarr_episode INTEGER
            )
        """)
        self.conn.execute("""
            CREATE TABLE tvmaze_episode (
                tvmaze_show_id INTEGER, season INTEGER, episode INTEGER,
                title TEXT, airdate TEXT, airstamp TEXT, airtime TEXT,
                runtime_minutes INTEGER, fetched_at TEXT,
                PRIMARY KEY (tvmaze_show_id, season, episode)
            )
        """)

        # Insert test data
        self.conn.execute(
            "INSERT INTO show VALUES ('s-test01', 1)")
        self.conn.execute(
            "INSERT INTO show_external_id VALUES ('s-test01', 'tvmaze', '100', '', '')")
        # Episode with NULL airdate
        self.conn.execute("""
            INSERT INTO episode (id, show_id, season, episode, kind,
                                 air_date_utc, sonarr_season, sonarr_episode)
            VALUES ('e-test01', 's-test01', 1, 1, 'regular', NULL, 1, 1)
        """)
        # Episode with existing airdate (should NOT be overwritten)
        self.conn.execute("""
            INSERT INTO episode (id, show_id, season, episode, kind,
                                 air_date_utc, air_date_source,
                                 sonarr_season, sonarr_episode)
            VALUES ('e-test02', 's-test01', 1, 2, 'regular',
                    '2020-01-08T00:00:00Z', 'sonarr', 1, 2)
        """)
        # TVmaze episodes (with airstamp — UTC times differ from local airdate)
        self.conn.execute("""
            INSERT INTO tvmaze_episode
            (tvmaze_show_id, season, episode, title, airdate,
             airstamp, airtime, runtime_minutes, fetched_at)
            VALUES
            (100, 1, 1, 'Pilot', '2020-01-01', '2020-01-02T01:00:00Z', '20:00', 42, '2026-09-07'),
            (100, 1, 2, 'Ep 2', '2020-01-08', '2020-01-09T01:00:00Z', '20:00', 42, '2026-09-07')
        """)
        self.conn.commit()

    def test_fills_null_airdates(self):
        from lcars.tvmaze import fill_airdate_gaps

        filled = fill_airdate_gaps(self.conn)
        assert filled == 1  # only the NULL one

        row = self.conn.execute(
            "SELECT air_date_utc, air_date_source FROM episode WHERE id = 'e-test01'"
        ).fetchone()
        assert row[0] == "2020-01-02T01:00:00Z"  # UTC airstamp, not local airdate
        assert row[1] == "tvmaze"

    def test_does_not_overwrite_existing(self):
        from lcars.tvmaze import fill_airdate_gaps

        fill_airdate_gaps(self.conn)

        row = self.conn.execute(
            "SELECT air_date_utc, air_date_source FROM episode WHERE id = 'e-test02'"
        ).fetchone()
        assert row[0] == "2020-01-08T00:00:00Z"
        assert row[1] == "sonarr"  # unchanged


class TestAnidbAirdateFill:
    """Test airdate gap filling from AniDB."""

    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE episode (
                id TEXT PRIMARY KEY,
                show_id TEXT, season INTEGER, episode INTEGER,
                kind TEXT DEFAULT 'regular',
                air_date_utc TEXT, air_date_source TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE episode_anidb_mapping (
                episode_id TEXT PRIMARY KEY,
                anidb_anime_id INTEGER, anidb_season INTEGER,
                anidb_epno INTEGER, confidence TEXT, created_at TEXT
            )
        """)
        self.conn.execute("""
            CREATE TABLE anidb_episode (
                anidb_anime_id INTEGER, anidb_season INTEGER,
                anidb_epno INTEGER, title_en TEXT, title_ja TEXT,
                title_romaji TEXT, airdate TEXT, length_minutes INTEGER,
                fetched_at TEXT,
                PRIMARY KEY (anidb_anime_id, anidb_season, anidb_epno)
            )
        """)

        # Episode with NULL airdate + mapping + AniDB data
        self.conn.execute("""
            INSERT INTO episode VALUES
            ('e-an0001', 's-anime1', 1, 1, 'regular', NULL, NULL)
        """)
        self.conn.execute("""
            INSERT INTO episode_anidb_mapping VALUES
            ('e-an0001', 12345, 1, 1, 'auto', '2026-09-07')
        """)
        self.conn.execute("""
            INSERT INTO anidb_episode VALUES
            (12345, 1, 1, 'Test Ep', NULL, NULL, '2020-04-05', 24, '2026-09-07')
        """)
        self.conn.commit()

    def test_fills_anime_airdates(self):
        from lcars.anidb import fill_airdate_gaps_anidb

        filled = fill_airdate_gaps_anidb(self.conn)
        assert filled == 1

        row = self.conn.execute(
            "SELECT air_date_utc, air_date_source FROM episode WHERE id = 'e-an0001'"
        ).fetchone()
        assert row[0] == "2020-04-05T00:00:00Z"
        assert row[1] == "anidb"


class TestWikidataTvPropagation:
    """Test Wikidata-powered TVDB ID propagation for non-anime shows."""

    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE show (id TEXT PRIMARY KEY,"
            " tracked INTEGER, tracking_space TEXT, media_shape TEXT)"
        )
        self.conn.execute("""
            CREATE TABLE show_external_id (
                show_id TEXT, service TEXT, external_id TEXT,
                url TEXT, created_at TEXT,
                UNIQUE(show_id, service)
            )
        """)
        self.conn.execute(
            "CREATE TABLE anime_list_entry (id INTEGER PRIMARY KEY,"
            " anidb_id INTEGER, tvdb_id TEXT,"
            " default_tvdb_season INTEGER, episode_offset INTEGER,"
            " tmdb_tv INTEGER, tmdb_season INTEGER, tmdb_movie INTEGER,"
            " imdb_id TEXT, name TEXT,"
            " source TEXT DEFAULT 'community', fetched_at TEXT)"
        )

        # A non-anime show with TMDB but no TVDB
        self.conn.execute("INSERT INTO show VALUES ('s-tv001', 1, 'tv', 'TV')")
        self.conn.execute("INSERT INTO show_external_id VALUES ('s-tv001', 'tmdb', '456', '', '')")
        self.conn.commit()

    def test_propagates_tvdb_from_wikidata(self):
        from lcars.anidb import propagate_cross_ids

        wikidata = [{"tvdb": "123", "tmdb": "456", "imdb": "tt999"}]
        counts = propagate_cross_ids(self.conn, [], wikidata)
        assert counts["tvdb"] == 1

        row = self.conn.execute(
            "SELECT external_id FROM show_external_id"
            " WHERE show_id = 's-tv001' AND service = 'tvdb'"
        ).fetchone()
        assert row[0] == "123"

    def test_no_overwrite_existing_tvdb(self):
        from lcars.anidb import propagate_cross_ids

        # Pre-existing TVDB
        self.conn.execute("INSERT INTO show_external_id VALUES ('s-tv001', 'tvdb', '999', '', '')")
        self.conn.commit()

        wikidata = [{"tvdb": "123", "tmdb": "456", "imdb": None}]
        counts = propagate_cross_ids(self.conn, [], wikidata)
        assert counts["tvdb"] == 0  # not overwritten

        row = self.conn.execute(
            "SELECT external_id FROM show_external_id"
            " WHERE show_id = 's-tv001' AND service = 'tvdb'"
        ).fetchone()
        assert row[0] == "999"  # unchanged


class TestPropagateFullGraph:
    """Test the full ID propagation graph — TVDB→AniDB→Fribb→everything."""

    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE show (id TEXT PRIMARY KEY,"
            " tracked INTEGER, tracking_space TEXT, media_shape TEXT)"
        )
        self.conn.execute("""
            CREATE TABLE show_external_id (
                show_id TEXT, service TEXT, external_id TEXT,
                url TEXT, created_at TEXT,
                UNIQUE(show_id, service)
            )
        """)
        self.conn.execute(
            "CREATE TABLE anime_list_entry (id INTEGER PRIMARY KEY,"
            " anidb_id INTEGER, tvdb_id TEXT,"
            " default_tvdb_season INTEGER, episode_offset INTEGER,"
            " tmdb_tv INTEGER, tmdb_season INTEGER, tmdb_movie INTEGER,"
            " imdb_id TEXT, name TEXT,"
            " source TEXT DEFAULT 'community', fetched_at TEXT)"
        )
        self.conn.execute("CREATE INDEX ix_anime_list_entry_tvdb_id"
                          " ON anime_list_entry (tvdb_id)")
        self.conn.execute("CREATE INDEX ix_anime_list_entry_anidb_id"
                          " ON anime_list_entry (anidb_id)")

    def test_tvdb_to_anidb_seed(self):
        """Phase 1: Sonarr-added anime with TVDB seeds AniDB via anime_list_entry."""
        from lcars.anidb import propagate_cross_ids

        # An anime show with only TVDB
        self.conn.execute("INSERT INTO show VALUES ('s1', 1, 'anime', 'TV')")
        self.conn.execute(
            "INSERT INTO show_external_id VALUES ('s1', 'tvdb', '12345', '', '')")
        # anime_list_entry maps tvdb 12345 → anidb 9999
        self.conn.execute(
            "INSERT INTO anime_list_entry (anidb_id, tvdb_id, episode_offset)"
            " VALUES (9999, '12345', 0)")
        self.conn.commit()

        counts = propagate_cross_ids(self.conn, [])
        assert counts["anidb"] == 1

        row = self.conn.execute(
            "SELECT external_id FROM show_external_id"
            " WHERE show_id = 's1' AND service = 'anidb'"
        ).fetchone()
        assert row[0] == "9999"

    def test_anidb_rooted_fribb_fallback(self):
        """Phase 2b: AniDB-only show gets AniList/MAL/TMDB/IMDB from Fribb."""
        from lcars.anidb import propagate_cross_ids

        # An anime show with only AniDB (no AniList)
        self.conn.execute("INSERT INTO show VALUES ('s1', 1, 'anime', 'TV')")
        self.conn.execute(
            "INSERT INTO show_external_id VALUES ('s1', 'anidb', '9999', '', '')")
        self.conn.commit()

        # Fribb dataset entry keyed by anidb_id
        fribb = [{"anidb_id": 9999, "anilist_id": 111, "mal_id": 222,
                  "themoviedb_id": {"tv": 333}, "imdb_id": ["tt0004444"],
                  "tvdb_id": 555}]

        counts = propagate_cross_ids(self.conn, fribb)
        assert counts["anilist"] == 1
        assert counts["mal"] == 1
        assert counts["tmdb"] == 1
        assert counts["imdb"] == 1

        # Check all IDs were inserted
        ids = dict(self.conn.execute(
            "SELECT service, external_id FROM show_external_id"
            " WHERE show_id = 's1' ORDER BY service"
        ).fetchall())
        assert ids["anilist"] == "111"
        assert ids["mal"] == "222"
        assert ids["tmdb"] == "333"
        assert ids["imdb"] == "tt0004444"

    def test_anime_lists_tmdb_imdb_propagation(self):
        """Phase 3: anime_list_entry fills TMDB and IMDB alongside TVDB."""
        from lcars.anidb import propagate_cross_ids

        # Show with AniDB but no TMDB/IMDB
        self.conn.execute("INSERT INTO show VALUES ('s1', 1, 'anime', 'TV')")
        self.conn.execute(
            "INSERT INTO show_external_id VALUES ('s1', 'anidb', '100', '', '')")
        self.conn.commit()

        # anime_list_entry with tmdb_tv and imdb_id
        self.conn.execute(
            "INSERT INTO anime_list_entry"
            " (anidb_id, tvdb_id, episode_offset, tmdb_tv, imdb_id)"
            " VALUES (100, '200', 0, 300, 'tt0000300')")
        self.conn.commit()

        counts = propagate_cross_ids(self.conn, [])
        assert counts["tvdb"] == 1
        assert counts["tmdb"] == 1
        assert counts["imdb"] == 1

    def test_full_sonarr_cascade(self):
        """End-to-end: TVDB-only anime → AniDB → AniList/MAL/TMDB/IMDB."""
        from lcars.anidb import propagate_cross_ids

        # Sonarr adds anime with only TVDB
        self.conn.execute("INSERT INTO show VALUES ('s1', 1, 'anime', 'TV')")
        self.conn.execute(
            "INSERT INTO show_external_id VALUES ('s1', 'tvdb', '12345', '', '')")
        # anime_list_entry bridges TVDB→AniDB
        self.conn.execute(
            "INSERT INTO anime_list_entry (anidb_id, tvdb_id, episode_offset)"
            " VALUES (9999, '12345', 0)")
        self.conn.commit()

        # Fribb bridges AniDB→everything
        fribb = [{"anidb_id": 9999, "anilist_id": 111, "mal_id": 222,
                  "themoviedb_id": {"tv": 333}, "imdb_id": ["tt0004444"],
                  "tvdb_id": 12345}]

        counts = propagate_cross_ids(self.conn, fribb)

        # Phase 1 seeded AniDB, phase 2b filled the rest
        assert counts["anidb"] == 1
        assert counts["anilist"] == 1
        assert counts["mal"] == 1
        assert counts["tmdb"] == 1
        assert counts["imdb"] == 1

        # Verify all 6 services are populated
        services = [r[0] for r in self.conn.execute(
            "SELECT service FROM show_external_id"
            " WHERE show_id = 's1' ORDER BY service"
        ).fetchall()]
        assert sorted(services) == ["anidb", "anilist", "imdb", "mal", "tmdb", "tvdb"]

    def test_insert_only_never_overwrites(self):
        """Existing IDs (manual corrections) are never overwritten."""
        from lcars.anidb import propagate_cross_ids

        # Show with AniDB and manually-corrected AniList
        self.conn.execute("INSERT INTO show VALUES ('s1', 1, 'anime', 'TV')")
        self.conn.execute(
            "INSERT INTO show_external_id VALUES ('s1', 'anidb', '9999', '', '')")
        self.conn.execute(
            "INSERT INTO show_external_id VALUES ('s1', 'anilist', '999', '', '')")
        self.conn.commit()

        # Fribb says anilist should be 111, not 999
        fribb = [{"anidb_id": 9999, "anilist_id": 111, "mal_id": 222,
                  "themoviedb_id": {}, "tvdb_id": 555}]

        counts = propagate_cross_ids(self.conn, fribb)
        assert counts["anilist"] == 0  # not overwritten

        row = self.conn.execute(
            "SELECT external_id FROM show_external_id"
            " WHERE show_id = 's1' AND service = 'anilist'"
        ).fetchone()
        assert row[0] == "999"  # manual correction preserved


class TestAirstampNormalize:
    """Test airstamp normalization."""

    def test_normalizes_plus_offset(self):
        from lcars.tvmaze import _normalize_airstamp

        assert _normalize_airstamp("2020-01-02T01:00:00+00:00") == "2020-01-02T01:00:00Z"

    def test_preserves_z_suffix(self):
        from lcars.tvmaze import _normalize_airstamp

        assert _normalize_airstamp("2020-01-02T01:00:00Z") == "2020-01-02T01:00:00Z"

    def test_none_passthrough(self):
        from lcars.tvmaze import _normalize_airstamp

        assert _normalize_airstamp(None) is None


class TestDripLookupFailureMarker:
    """Verify lookup failure writes a '-1' marker so the show doesn't stall."""

    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE show (id TEXT PRIMARY KEY, tracked INTEGER, tracking_space TEXT)")
        self.conn.execute("""
            CREATE TABLE show_external_id (
                show_id TEXT, service TEXT, external_id TEXT,
                url TEXT, created_at TEXT,
                UNIQUE(show_id, service)
            )
        """)
        self.conn.execute("""
            CREATE TABLE tvmaze_episode (
                tvmaze_show_id INTEGER NOT NULL,
                season INTEGER NOT NULL,
                episode INTEGER NOT NULL,
                title TEXT, airdate TEXT, airstamp TEXT, airtime TEXT,
                runtime_minutes INTEGER, fetched_at TEXT NOT NULL,
                PRIMARY KEY (tvmaze_show_id, season, episode)
            )
        """)

        # Two non-anime shows with IMDB IDs but no tvmaze
        self.conn.execute("INSERT INTO show VALUES ('s-fail1', 1, 'tv')")
        self.conn.execute(
            "INSERT INTO show_external_id VALUES ('s-fail1', 'imdb', 'tt0000001', '', '')")
        self.conn.execute("INSERT INTO show VALUES ('s-ok1', 1, 'tv')")
        self.conn.execute(
            "INSERT INTO show_external_id VALUES ('s-ok1', 'imdb', 'tt9999999', '', '')")
        self.conn.commit()

    @patch("lcars.tvmaze.lookup_show_by_imdb")
    @patch("lcars.tvmaze.lookup_show_by_tvdb")
    @patch("lcars.tvmaze.fetch_episodes")
    def test_failed_lookup_writes_marker_and_doesnt_stall(
        self, mock_fetch, mock_tvdb, mock_imdb
    ):
        from lcars.tvmaze import drip_fetch_episodes

        # First show: lookup fails. Second: succeeds.
        mock_imdb.side_effect = [None, {"id": 42, "externals": {}}]
        mock_tvdb.return_value = None
        mock_fetch.return_value = [
            {"season": 1, "number": 1, "name": "Ep1",
             "airdate": "2020-01-01", "airstamp": "2020-01-02T01:00:00+00:00",
             "airtime": "20:00", "runtime": 42}
        ]

        stats = drip_fetch_episodes(self.conn, limit=5)
        assert stats["shows_without_tvmaze"] == 1
        assert stats["fetched"] == 1

        # Verify the failed show has a '-1' marker
        row = self.conn.execute(
            "SELECT external_id FROM show_external_id "
            "WHERE show_id = 's-fail1' AND service = 'tvmaze'"
        ).fetchone()
        assert row is not None
        assert row[0] == "-1"

        # Second run: the failed show should NOT reappear
        mock_imdb.reset_mock()
        mock_imdb.side_effect = [{"id": 43, "externals": {}}]
        mock_fetch.return_value = []
        stats2 = drip_fetch_episodes(self.conn, limit=5)
        # s-fail1 is gated out; s-ok1 already has tvmaze ID from run 1
        assert stats2["shows_without_tvmaze"] == 0
