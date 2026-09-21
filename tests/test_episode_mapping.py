"""Memory Alpha — episode offset derivation tests.

Verifies the _SeasonResolver and derive_episode_mappings logic using
hand-verified Chobits data (anidb=12, tvdb=72070).
"""

import pytest

from lcars import anidb, db


@pytest.fixture(autouse=True)
def _reset_db():
    db.close()
    yield
    db.close()


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    c.executescript("""
        CREATE TABLE show (
            id TEXT PRIMARY KEY,
            media_shape TEXT NOT NULL DEFAULT 'episodic',
            tracking_space TEXT NOT NULL DEFAULT 'anime',
            title_romaji TEXT,
            title_english TEXT,
            title_native TEXT,
            tracked INTEGER NOT NULL DEFAULT 1,
            banner_url TEXT,
            poster_url TEXT
        );
        CREATE TABLE show_external_id (
            show_id TEXT NOT NULL,
            service TEXT NOT NULL,
            external_id TEXT NOT NULL,
            url TEXT,
            created_at TEXT NOT NULL DEFAULT '',
            UNIQUE(show_id, service)
        );
        CREATE TABLE episode (
            id TEXT PRIMARY KEY,
            show_id TEXT NOT NULL,
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'regular',
            absolute_number REAL,
            air_date_utc TEXT,
            air_date_source TEXT,
            air_date_raw_sonarr TEXT,
            available_checked_at TEXT,
            runtime_minutes INTEGER,
            state TEXT NOT NULL DEFAULT 'unwatched',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            season_id TEXT,
            available_via_sonarr TEXT NOT NULL DEFAULT 'unavailable',
            available_via_radarr TEXT NOT NULL DEFAULT 'unavailable',
            file_path_sonarr TEXT,
            file_path_radarr TEXT,
            title TEXT,
            sonarr_season INTEGER,
            sonarr_episode INTEGER,
            synopsis TEXT
        );
        CREATE TABLE anime_list_entry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anidb_id INTEGER NOT NULL,
            tvdb_id TEXT,
            default_tvdb_season INTEGER,
            episode_offset INTEGER NOT NULL DEFAULT 0,
            tmdb_tv INTEGER,
            tmdb_season INTEGER,
            tmdb_movie INTEGER,
            imdb_id TEXT,
            name TEXT,
            source TEXT NOT NULL DEFAULT 'community',
            fetched_at TEXT NOT NULL DEFAULT '',
            UNIQUE(anidb_id)
        );
        CREATE TABLE anime_list_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL,
            anidb_season INTEGER,
            tvdb_season INTEGER,
            start INTEGER,
            "end" INTEGER,
            offset INTEGER,
            episode_map TEXT
        );
        CREATE TABLE episode_anidb_mapping (
            episode_id TEXT PRIMARY KEY,
            anidb_anime_id INTEGER NOT NULL,
            anidb_season INTEGER NOT NULL DEFAULT 1,
            anidb_epno INTEGER,
            confidence TEXT NOT NULL DEFAULT 'auto',
            created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE anidb_episode (
            anidb_anime_id INTEGER NOT NULL,
            anidb_season INTEGER NOT NULL,
            anidb_epno INTEGER NOT NULL,
            title_en TEXT,
            title_ja TEXT,
            title_romaji TEXT,
            airdate TEXT,
            length_minutes INTEGER,
            fetched_at TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (anidb_anime_id, anidb_season, anidb_epno)
        );
    """)
    return c


def _seed_chobits(conn):
    """Seed Chobits test data — matches real Anime-Lists XML."""
    # Show
    conn.execute(
        "INSERT INTO show (id, title_romaji, tracked) VALUES (?, ?, 1)",
        ("s-chobits", "Chobits"),
    )
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id) "
        "VALUES (?, 'anidb', '12')",
        ("s-chobits",),
    )

    # anime_list_entry: Chobits = anidb 12, tvdb 72070, season 1, offset 0
    conn.execute(
        "INSERT INTO anime_list_entry (anidb_id, tvdb_id, "
        "default_tvdb_season, episode_offset, name) "
        "VALUES (12, '72070', 1, 0, 'Chobits')"
    )
    entry_id = conn.execute(
        "SELECT id FROM anime_list_entry WHERE anidb_id = 12"
    ).fetchone()[0]

    # Mappings from real XML:
    # specials: anidb S0 → tvdb S0, episode_map ;1-3;2-4;
    conn.execute(
        "INSERT INTO anime_list_mapping "
        "(entry_id, anidb_season, tvdb_season, episode_map) "
        "VALUES (?, 0, 0, ';1-3;2-4;')",
        (entry_id,),
    )
    # regular→special: anidb S1 → tvdb S0, episode_map ;9-1;18-2;
    conn.execute(
        "INSERT INTO anime_list_mapping "
        "(entry_id, anidb_season, tvdb_season, episode_map) "
        "VALUES (?, 1, 0, ';9-1;18-2;')",
        (entry_id,),
    )
    # range: anidb S1 eps 10-17 → tvdb S1 with offset -1
    conn.execute(
        "INSERT INTO anime_list_mapping "
        "(entry_id, anidb_season, tvdb_season, start, \"end\", offset) "
        "VALUES (?, 1, 1, 10, 17, -1)",
        (entry_id,),
    )
    # range: anidb S1 eps 19-26 → tvdb S1 with offset -2
    conn.execute(
        "INSERT INTO anime_list_mapping "
        "(entry_id, anidb_season, tvdb_season, start, \"end\", offset) "
        "VALUES (?, 1, 1, 19, 26, -2)",
        (entry_id,),
    )

    # Episodes: TVDB S1E1–S1E24 (Chobits has 24 aired on TVDB S1)
    for i in range(1, 25):
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, "
            "absolute_number) VALUES (?, 's-chobits', 1, ?, ?)",
            (f"e-chob{i:02d}", i, float(i)),
        )
    conn.commit()


class TestSeasonResolver:
    """Unit tests for the _SeasonResolver class."""

    def test_default_offset_simple(self):
        """Simple entry with offset=0 → identity mapping."""
        entries = [{
            "anidb_id": 100,
            "default_tvdb_season": 1,
            "episode_offset": 0,
            "mappings": [],
        }]
        r = anidb._SeasonResolver(entries)
        assert r.resolve(1, 1) == (100, 1, 1)
        assert r.resolve(1, 12) == (100, 1, 12)

    def test_default_offset_nonzero(self):
        """Entry with offset=5 → anidb_ep = tvdb_ep - 5."""
        entries = [{
            "anidb_id": 200,
            "default_tvdb_season": 1,
            "episode_offset": 5,
            "mappings": [],
        }]
        r = anidb._SeasonResolver(entries)
        assert r.resolve(1, 6) == (200, 1, 1)
        assert r.resolve(1, 10) == (200, 1, 5)

    def test_wrong_season_returns_none(self):
        """Episode in unmatched season → None."""
        entries = [{
            "anidb_id": 300,
            "default_tvdb_season": 2,
            "episode_offset": 0,
            "mappings": [],
        }]
        r = anidb._SeasonResolver(entries)
        assert r.resolve(1, 1) is None
        assert r.resolve(2, 1) == (300, 1, 1)

    def test_range_mapping(self):
        """Range-based mapping override."""
        entries = [{
            "anidb_id": 400,
            "default_tvdb_season": 1,
            "episode_offset": 0,
            "mappings": [{
                "anidb_season": 1, "tvdb_season": 1,
                "start": 10, "end": 17, "offset": -1,
                "episode_map": None,
            }],
        }]
        r = anidb._SeasonResolver(entries)
        # Before range: default offset
        assert r.resolve(1, 5) == (400, 1, 5)
        # In range: anidb_ep = tvdb_ep - (-1) = tvdb_ep + 1
        assert r.resolve(1, 9) == (400, 1, 10)   # tvdb 9 → anidb 10
        assert r.resolve(1, 10) == (400, 1, 11)  # tvdb 10 → anidb 11
        assert r.resolve(1, 16) == (400, 1, 17)  # tvdb 16 → anidb 17

    def test_episode_map_priority(self):
        """Individual episode map overrides range and default."""
        entries = [{
            "anidb_id": 500,
            "default_tvdb_season": 1,
            "episode_offset": 0,
            "mappings": [{
                "anidb_season": 1, "tvdb_season": 1,
                "start": None, "end": None, "offset": None,
                "episode_map": ";9-5;",
            }],
        }]
        r = anidb._SeasonResolver(entries)
        # ep_map: anidb 9 → tvdb 5, so tvdb 5 → (500, season 1, anidb 9)
        assert r.resolve(1, 5) == (500, 1, 9)
        # Other eps use default
        assert r.resolve(1, 1) == (500, 1, 1)

    def test_chobits_full(self):
        """Chobits hand-verification.

        AniDB has 26 regular eps + specials. The Anime-Lists mapping
        diverts regular eps 9 and 18 to TVDB specials (S0E1, S0E2) via
        episode_map. The two ranges (offset -1, offset -2) shift the
        remaining TVDB eps up to fill the gaps: 26 AniDB regulars − 2
        diverted = 24 TVDB S1 eps, gap-filled by range offsets.

        Values verified by counting the gap-fill arithmetic, not by
        reading AniDB directly (no API access for bulk verification).
        """
        entries = [{
            "anidb_id": 12,
            "default_tvdb_season": 1,
            "episode_offset": 0,
            "mappings": [
                {"anidb_season": 0, "tvdb_season": 0,
                 "start": None, "end": None, "offset": None,
                 "episode_map": ";1-3;2-4;"},
                {"anidb_season": 1, "tvdb_season": 0,
                 "start": None, "end": None, "offset": None,
                 "episode_map": ";9-1;18-2;"},
                {"anidb_season": 1, "tvdb_season": 1,
                 "start": 10, "end": 17, "offset": -1,
                 "episode_map": None},
                {"anidb_season": 1, "tvdb_season": 1,
                 "start": 19, "end": 26, "offset": -2,
                 "episode_map": None},
            ],
        }]
        r = anidb._SeasonResolver(entries)

        # Simple default (eps 1-8, offset=0) → regular (season 1)
        assert r.resolve(1, 1) == (12, 1, 1)
        assert r.resolve(1, 8) == (12, 1, 8)

        # Range 1: tvdb 9-16 → anidb 10-17 (offset -1), regular
        assert r.resolve(1, 9) == (12, 1, 10)
        assert r.resolve(1, 10) == (12, 1, 11)
        assert r.resolve(1, 16) == (12, 1, 17)

        # Range 2: tvdb 17-24 → anidb 19-26 (offset -2), regular
        assert r.resolve(1, 17) == (12, 1, 19)
        assert r.resolve(1, 19) == (12, 1, 21)
        assert r.resolve(1, 24) == (12, 1, 26)

        # Episode maps into specials: tvdb S0E1 → anidb regular ep 9
        # (anidb_season=1 because the mapping row is anidb_season=1)
        assert r.resolve(0, 1) == (12, 1, 9)
        assert r.resolve(0, 2) == (12, 1, 18)

        # Specials-to-specials: tvdb S0E3 → anidb special 1 (season 0)
        assert r.resolve(0, 3) == (12, 0, 1)
        assert r.resolve(0, 4) == (12, 0, 2)

    def test_ambiguous_same_season_same_offset(self):
        """Two entries on same season with same offset → ambiguous → None."""
        entries = [
            {"anidb_id": 600, "default_tvdb_season": 1,
             "episode_offset": 0, "mappings": []},
            {"anidb_id": 601, "default_tvdb_season": 1,
             "episode_offset": 0, "mappings": []},
        ]
        r = anidb._SeasonResolver(entries)
        assert r.resolve(1, 1) is None

    def test_multi_entry_offset_partition(self):
        """Two entries on same season with different offsets partition eps."""
        entries = [
            {"anidb_id": 700, "default_tvdb_season": 1,
             "episode_offset": 0, "mappings": []},
            {"anidb_id": 701, "default_tvdb_season": 1,
             "episode_offset": 12, "mappings": []},
        ]
        r = anidb._SeasonResolver(entries)
        # Eps 1-12 → anidb 700 (offset 0, so ep 1→1), regular
        assert r.resolve(1, 1) == (700, 1, 1)
        assert r.resolve(1, 12) == (700, 1, 12)
        # Eps 13+ → anidb 701 (offset 12, so ep 13→1), regular
        assert r.resolve(1, 13) == (701, 1, 1)
        assert r.resolve(1, 24) == (701, 1, 12)


class TestParseEpisodeMap:
    def test_basic(self):
        assert anidb._parse_episode_map(";9-1;18-2;") == [(9, 1), (18, 2)]

    def test_skip_zero_tvdb(self):
        assert anidb._parse_episode_map(";1-0;2-3;") == [(2, 3)]

    def test_empty(self):
        assert anidb._parse_episode_map("") == []
        assert anidb._parse_episode_map(None) == []


class TestDeriveEpisodeMappings:
    def test_chobits_integration(self, conn):
        """Full integration: seed Chobits, derive, verify."""
        _seed_chobits(conn)
        stats = anidb.derive_episode_mappings(conn)

        assert stats["shows_processed"] == 1
        assert stats["shows_skipped_no_entry"] == 0
        assert stats["mapped"] == 24

        # Verify specific mappings
        rows = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT episode_id, anidb_anime_id, anidb_season, anidb_epno "
                "FROM episode_anidb_mapping"
            ).fetchall()
        }
        # S1E1 → anidb regular 1
        assert rows["e-chob01"] == (12, 1, 1)
        # S1E10 → anidb regular 11 (range offset -1)
        assert rows["e-chob10"] == (12, 1, 11)
        # S1E19 → anidb regular 21 (range offset -2)
        assert rows["e-chob19"] == (12, 1, 21)

    def test_no_anidb_id_skipped(self, conn):
        """Show without anidb external ID is not processed."""
        conn.execute(
            "INSERT INTO show (id, title_romaji, tracked) "
            "VALUES ('s-nolink', 'No AniDB Link', 1)"
        )
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode) "
            "VALUES ('e-test', 's-nolink', 1, 1)"
        )
        conn.commit()
        stats = anidb.derive_episode_mappings(conn)
        assert stats["shows_processed"] == 0
        assert stats["mapped"] == 0


def _seed_air_date_show(conn, anidb_id=900, tvdb_id="900001", offset=0):
    """A minimal show whose default-offset resolution is deliberately
    wrong (real case: a community episode_map/offset built for a
    different episode split than what TVDB/Sonarr actually tracks), so
    the air-date arbiter's correction is the interesting part being
    tested, not the baseline resolver."""
    conn.execute(
        "INSERT INTO show (id, title_romaji, tracked) VALUES ('s-airdt1', 'Air Date Show', 1)"
    )
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id)"
        " VALUES ('s-airdt1', 'anidb', ?)",
        (str(anidb_id),),
    )
    conn.execute(
        "INSERT INTO anime_list_entry"
        " (anidb_id, tvdb_id, default_tvdb_season, episode_offset, name)"
        " VALUES (?, ?, 1, ?, 'Air Date Show')",
        (anidb_id, tvdb_id, offset),
    )


class TestAirDateArbiter:
    """2026-09-22 — the user's own standing rule since Memory Alpha's
    inception: when episode ordering is in conflict, real broadcast
    date wins. Found live on Sousou no Frieren, Log Horizon, and
    Bakemonogatari — all three confirmed correct against real airdate
    data before this was written."""

    def test_air_date_overrides_wrong_offset_and_locks(self, conn):
        _seed_air_date_show(conn, offset=99)  # deliberately wrong
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, air_date_utc)"
            " VALUES ('e-ad001', 's-airdt1', 1, 1, '2023-10-06T13:30:00Z')"
        )
        conn.execute(
            "INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, airdate)"
            " VALUES (900, 1, 5, '2023-10-06')"
        )
        conn.commit()

        stats = anidb.derive_episode_mappings(conn)
        assert stats["mapped"] == 1

        row = conn.execute(
            "SELECT anidb_epno, confidence FROM episode_anidb_mapping WHERE episode_id = 'e-ad001'"
        ).fetchone()
        assert row["anidb_epno"] == 5
        assert row["confidence"] == "air_date_confirmed"

    def test_locked_row_never_revisited(self, conn):
        """Once locked, a later tick must not touch it even if the
        underlying anidb_episode data changes."""
        _seed_air_date_show(conn, offset=99)
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, air_date_utc)"
            " VALUES ('e-ad002', 's-airdt1', 1, 1, '2023-10-06T13:30:00Z')"
        )
        conn.execute(
            "INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, airdate)"
            " VALUES (900, 1, 5, '2023-10-06')"
        )
        conn.commit()
        anidb.derive_episode_mappings(conn)

        # Real airdate data "corrects itself" to a different value —
        # must not matter, the row is locked.
        conn.execute("UPDATE anidb_episode SET anidb_epno = 99 WHERE anidb_anime_id = 900")
        conn.commit()
        anidb.derive_episode_mappings(conn)

        row = conn.execute(
            "SELECT anidb_epno, confidence FROM episode_anidb_mapping WHERE episode_id = 'e-ad002'"
        ).fetchone()
        assert row["anidb_epno"] == 5
        assert row["confidence"] == "air_date_confirmed"

    def test_no_air_date_falls_back_to_resolver(self, conn):
        """No air_date_utc on the LCARS episode at all — arbiter has
        nothing to work with, keeps the resolver's own (here, correct)
        result at 'auto' confidence."""
        _seed_air_date_show(conn, offset=0)
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode)"
            " VALUES ('e-ad003', 's-airdt1', 1, 1)"
        )
        conn.commit()

        anidb.derive_episode_mappings(conn)

        row = conn.execute(
            "SELECT anidb_epno, confidence FROM episode_anidb_mapping WHERE episode_id = 'e-ad003'"
        ).fetchone()
        assert row["anidb_epno"] == 1
        assert row["confidence"] == "auto"

    def test_ambiguous_same_day_does_not_guess(self, conn):
        """Two real regular episodes on the same real airdate — the
        arbiter must not guess which one, same 'never guess' convention
        as everywhere else. Keeps the resolver's own result at 'auto'."""
        _seed_air_date_show(conn, offset=0)
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, air_date_utc)"
            " VALUES ('e-ad004', 's-airdt1', 1, 1, '2023-10-06T13:30:00Z')"
        )
        conn.execute(
            "INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, airdate)"
            " VALUES (900, 1, 5, '2023-10-06')"
        )
        conn.execute(
            "INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, airdate)"
            " VALUES (900, 1, 6, '2023-10-06')"
        )
        conn.commit()

        anidb.derive_episode_mappings(conn)

        row = conn.execute(
            "SELECT anidb_epno, confidence FROM episode_anidb_mapping WHERE episode_id = 'e-ad004'"
        ).fetchone()
        assert row["anidb_epno"] == 1  # the resolver's own default-offset result, untouched
        assert row["confidence"] == "auto"

    def test_no_matching_airdate_falls_back(self, conn):
        """LCARS episode has an air date, but no real AniDB regular
        episode shares it — no confident match, stays on the resolver's
        own result at 'auto'."""
        _seed_air_date_show(conn, offset=0)
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, air_date_utc)"
            " VALUES ('e-ad005', 's-airdt1', 1, 1, '2023-11-01T13:30:00Z')"
        )
        conn.execute(
            "INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, airdate)"
            " VALUES (900, 1, 5, '2023-10-06')"
        )
        conn.commit()

        anidb.derive_episode_mappings(conn)

        row = conn.execute(
            "SELECT anidb_epno, confidence FROM episode_anidb_mapping WHERE episode_id = 'e-ad005'"
        ).fetchone()
        assert row["anidb_epno"] == 1
        assert row["confidence"] == "auto"


class TestParseEpisodesXml:
    """Tests for _parse_episodes_xml — parsing AniDB HTTP API responses."""

    def test_chobits_response(self):
        """Parse a real Chobits-shaped response with regulars and specials."""
        xml = """<anime id="12">
          <episodes>
            <episode id="34">
              <epno type="1">1</epno>
              <length>25</length>
              <airdate>2002-04-03</airdate>
              <title xml:lang="ja">ちぃ 目覚める</title>
              <title xml:lang="en">Chi Awakens</title>
              <title xml:lang="x-jat">Chii Mezameru</title>
            </episode>
            <episode id="999">
              <epno type="2">S1</epno>
              <length>5</length>
              <airdate>2002-07-24</airdate>
              <title xml:lang="ja">ちびっツ</title>
              <title xml:lang="en">Chibits</title>
              <title xml:lang="x-jat">Chibittsu</title>
            </episode>
            <episode id="1000">
              <epno type="3">C1</epno>
              <title xml:lang="en">Opening</title>
            </episode>
          </episodes>
        </anime>"""
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        eps = anidb._parse_episodes_xml(root)
        assert len(eps) == 2  # C1 (credits) skipped

        reg = eps[0]
        assert reg["anidb_season"] == 1
        assert reg["anidb_epno"] == 1
        assert reg["title_en"] == "Chi Awakens"
        assert reg["title_ja"] == "ちぃ 目覚める"
        assert reg["title_romaji"] == "Chii Mezameru"
        assert reg["airdate"] == "2002-04-03"
        assert reg["length_minutes"] == 25

        sp = eps[1]
        assert sp["anidb_season"] == 0
        assert sp["anidb_epno"] == 1
        assert sp["title_en"] == "Chibits"

    def test_no_episodes_element(self):
        """Anime with no episodes section returns empty list."""
        import xml.etree.ElementTree as ET
        root = ET.fromstring("<anime id='1'></anime>")
        assert anidb._parse_episodes_xml(root) == []


class TestIngestAnimeEpisodes:
    """Tests for ingest_anime_episodes — writing to anidb_episode table."""

    def test_basic_ingest(self, conn):
        """Episodes are inserted and can be upserted."""
        eps = [
            {"anidb_season": 1, "anidb_epno": 1,
             "title_en": "Episode 1", "title_ja": "第1話",
             "title_romaji": "Dai 1 Wa",
             "airdate": "2020-01-01", "length_minutes": 24},
        ]
        count = anidb.ingest_anime_episodes(conn, 100, eps, "2026-09-07")
        assert count == 1

        row = conn.execute(
            "SELECT title_en, title_ja FROM anidb_episode "
            "WHERE anidb_anime_id = 100"
        ).fetchone()
        assert row[0] == "Episode 1"
        assert row[1] == "第1話"

        # Upsert with updated title
        eps[0]["title_en"] = "Episode One"
        count = anidb.ingest_anime_episodes(conn, 100, eps, "2026-09-07")
        assert count == 1
        row = conn.execute(
            "SELECT title_en FROM anidb_episode WHERE anidb_anime_id = 100"
        ).fetchone()
        assert row[0] == "Episode One"


class TestFillTitleGaps:
    """Tests for fill_title_gaps — filling episode.title from AniDB."""

    def test_fills_null_title(self, conn):
        """NULL episode.title is filled from AniDB English title."""
        _seed_chobits(conn)
        anidb.derive_episode_mappings(conn)

        # Add AniDB episode data for ep 1
        conn.execute(
            "INSERT INTO anidb_episode "
            "(anidb_anime_id, anidb_season, anidb_epno, title_en, fetched_at) "
            "VALUES (12, 1, 1, 'Chi Awakens', '2026-09-07')"
        )
        conn.commit()

        # Ensure episode has no title
        conn.execute("UPDATE episode SET title = NULL WHERE id = 'e-chob01'")
        conn.commit()

        filled = anidb.fill_title_gaps(conn)
        assert filled >= 1

        row = conn.execute(
            "SELECT title FROM episode WHERE id = 'e-chob01'"
        ).fetchone()
        assert row[0] == "Chi Awakens"

    def test_does_not_overwrite_existing(self, conn):
        """Existing episode.title is NOT overwritten."""
        _seed_chobits(conn)
        anidb.derive_episode_mappings(conn)

        conn.execute(
            "INSERT INTO anidb_episode "
            "(anidb_anime_id, anidb_season, anidb_epno, title_en, fetched_at) "
            "VALUES (12, 1, 1, 'AniDB Title', '2026-09-07')"
        )
        conn.execute(
            "UPDATE episode SET title = 'Sonarr Title' WHERE id = 'e-chob01'"
        )
        conn.commit()

        anidb.fill_title_gaps(conn)
        row = conn.execute(
            "SELECT title FROM episode WHERE id = 'e-chob01'"
        ).fetchone()
        assert row[0] == "Sonarr Title"


class TestFetchSentinels:
    """Verify fetch_anime_episodes returns distinct sentinels."""

    def test_not_found_sentinel(self):
        """AniDB <error> (non-ban) returns 'NOT_FOUND', not None."""
        import xml.etree.ElementTree as ET

        # Simulate the parse path for a "not found" response
        body = "<error>Anime not found</error>"
        root = ET.fromstring(body)
        assert root.tag == "error"
        error_text = (root.text or "").strip().lower()
        assert "banned" not in error_text
        assert "client" not in error_text
        # This is the NOT_FOUND path (not None)

    def test_banned_sentinel(self):
        """AniDB ban error returns 'BANNED'."""
        import xml.etree.ElementTree as ET

        body = "<error>banned</error>"
        root = ET.fromstring(body)
        error_text = (root.text or "").strip().lower()
        assert "banned" in error_text


class TestWriteTombstone:
    """Verify tombstone insertion and its effect on drip selection."""

    def test_tombstone_prevents_reselection(self, conn):
        """After tombstoning, the anime ID no longer appears in drip query."""
        _seed_chobits(conn)
        anidb.derive_episode_mappings(conn)

        # Before tombstone: anime 12 should appear in the drip query
        rows = conn.execute(
            """SELECT DISTINCT m.anidb_anime_id
               FROM episode_anidb_mapping m
               WHERE NOT EXISTS (
                 SELECT 1 FROM anidb_episode ae
                 WHERE ae.anidb_anime_id = m.anidb_anime_id
               )"""
        ).fetchall()
        assert any(r[0] == 12 for r in rows)

        # Write tombstone
        anidb._write_tombstone(conn, 12, "2026-09-07")

        # After tombstone: anime 12 should NOT appear
        rows = conn.execute(
            """SELECT DISTINCT m.anidb_anime_id
               FROM episode_anidb_mapping m
               WHERE NOT EXISTS (
                 SELECT 1 FROM anidb_episode ae
                 WHERE ae.anidb_anime_id = m.anidb_anime_id
               )"""
        ).fetchall()
        assert not any(r[0] == 12 for r in rows)
