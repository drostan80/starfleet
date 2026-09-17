"""Sequel detection in the add flow — prevents creating orphan stubs
when adding a sequel season from browse.

When AniList browse adds an entry whose AniList ID is a SEQUEL of an
already-tracked show (via ``show_relation``), the flow should raise
``SequelDetectedError`` instead of creating a separate show.
"""

from unittest.mock import patch

import pytest

from lcars import db, shows


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
            primary_title TEXT NOT NULL DEFAULT 'romaji',
            status TEXT NOT NULL DEFAULT 'watching',
            tracked INTEGER NOT NULL DEFAULT 1,
            poster_url TEXT,
            banner_url TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE season (
            id TEXT PRIMARY KEY,
            show_id TEXT NOT NULL REFERENCES show (id),
            season_number INTEGER,
            status TEXT,
            anilist_id INTEGER,
            mal_id INTEGER,
            source TEXT,
            matched INTEGER DEFAULT 0,
            manual_override INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01'
        );
        CREATE TABLE show_external_id (
            show_id TEXT NOT NULL REFERENCES show (id),
            service TEXT NOT NULL,
            external_id TEXT NOT NULL,
            url TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            UNIQUE (service, external_id)
        );
        CREATE TABLE show_relation (
            show_id TEXT NOT NULL REFERENCES show (id),
            related_show_id TEXT NOT NULL REFERENCES show (id),
            relation_type TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            UNIQUE (show_id, related_show_id)
        );
        CREATE TABLE _id_counter (
            prefix TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL DEFAULT 1
        );

        -- S1: tracked show with AniList 152682
        INSERT INTO show (id, title_romaji, title_english, primary_title, tracked)
        VALUES ('s-parent', 'Sasaki to Pii-chan', 'Sasaki and Peeps', 'english', 1);
        INSERT INTO show_external_id (show_id, service, external_id, url)
        VALUES ('s-parent', 'anilist', '152682', 'https://anilist.co/anime/152682');
        INSERT INTO season (id, show_id, season_number, anilist_id)
        VALUES ('z-seas01', 's-parent', 1, 152682);

        -- S2: untracked stub with AniList 176314 (sequel)
        INSERT INTO show (id, title_romaji, title_english, primary_title, tracked)
        VALUES ('s-stub', 'Sasaki to Pii-chan Season 2', 'Sasaki and Peeps Season 2', 'english', 0);
        INSERT INTO show_external_id (show_id, service, external_id, url)
        VALUES ('s-stub', 'anilist', '176314', 'https://anilist.co/anime/176314');

        -- Relation: parent has SEQUEL -> stub
        INSERT INTO show_relation (show_id, related_show_id, relation_type)
        VALUES ('s-parent', 's-stub', 'SEQUEL');
    """)
    c.commit()
    return c


class TestFindSequelParent:
    def test_finds_sequel_of_tracked_show(self, conn):
        result = shows.find_sequel_parent(conn, 176314)
        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["parent_title"] == "Sasaki and Peeps"
        assert result["next_season"] == 2
        assert result["stub_show_id"] == "s-stub"

    def test_returns_none_for_unknown_anilist_id(self, conn):
        assert shows.find_sequel_parent(conn, 999999) is None

    def test_returns_none_for_none(self, conn):
        assert shows.find_sequel_parent(conn, None) is None

    def test_matches_null_relation_type(self, conn):
        """NULL relation_type (common in existing data) should still match."""
        conn.execute(
            "UPDATE show_relation SET relation_type = NULL"
            " WHERE show_id = 's-parent' AND related_show_id = 's-stub'"
        )
        conn.commit()
        result = shows.find_sequel_parent(conn, 176314)
        assert result is not None
        assert result["parent_show_id"] == "s-parent"

    def test_returns_none_for_non_sequel_relation(self, conn):
        # Change the relation to PREQUEL — should not match.
        conn.execute(
            "UPDATE show_relation SET relation_type = 'PREQUEL'"
            " WHERE show_id = 's-parent' AND related_show_id = 's-stub'"
        )
        conn.commit()
        assert shows.find_sequel_parent(conn, 176314) is None

    def test_returns_none_for_side_story_relation(self, conn):
        conn.execute(
            "UPDATE show_relation SET relation_type = 'SIDE_STORY'"
            " WHERE show_id = 's-parent' AND related_show_id = 's-stub'"
        )
        conn.commit()
        assert shows.find_sequel_parent(conn, 176314) is None

    def test_detects_sequel_even_when_already_tracked(self, conn):
        """Sequel detection works regardless of the child's tracked status —
        the old tracked=0 gate was the bug that created duplicates."""
        conn.execute("UPDATE show SET tracked = 1 WHERE id = 's-stub'")
        conn.commit()
        result = shows.find_sequel_parent(conn, 176314)
        assert result is not None
        assert result["parent_show_id"] == "s-parent"

    def test_returns_none_when_parent_is_untracked(self, conn):
        conn.execute("UPDATE show SET tracked = 0 WHERE id = 's-parent'")
        conn.commit()
        assert shows.find_sequel_parent(conn, 176314) is None

    def test_next_season_is_3_when_two_seasons_exist(self, conn):
        conn.execute(
            "INSERT INTO season (id, show_id, season_number, anilist_id)"
            " VALUES ('z-seas02', 's-parent', 2, 160000)"
        )
        conn.commit()
        result = shows.find_sequel_parent(conn, 176314)
        assert result["next_season"] == 3


    def test_finds_sequel_via_reverse_prequel_relation(self, conn):
        """When the relation is stored as child → PREQUEL → parent
        (instead of parent → SEQUEL → child), detection still works.
        This is the actual Sasaki prod case."""
        # Replace forward SEQUEL with reverse PREQUEL
        conn.execute("DELETE FROM show_relation")
        conn.execute(
            "INSERT INTO show_relation (show_id, related_show_id, relation_type)"
            " VALUES ('s-stub', 's-parent', 'PREQUEL')"
        )
        conn.commit()
        result = shows.find_sequel_parent(conn, 176314)
        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["parent_title"] == "Sasaki and Peeps"
        assert result["next_season"] == 2

    def test_returns_none_for_reverse_non_prequel(self, conn):
        """A reverse relation that isn't PREQUEL shouldn't match."""
        conn.execute("DELETE FROM show_relation")
        conn.execute(
            "INSERT INTO show_relation (show_id, related_show_id, relation_type)"
            " VALUES ('s-stub', 's-parent', 'SIDE_STORY')"
        )
        conn.commit()
        assert shows.find_sequel_parent(conn, 176314) is None


class TestAniListFallback:
    """When no local show has the anilist_id, find_sequel_parent should
    fall back to a live AniList query."""

    def test_detects_sequel_via_anilist_when_no_local_show(self, conn, monkeypatch):
        """No stub in DB, but AniList returns a PREQUEL relation
        pointing to a tracked show."""
        # Remove the stub entirely — simulates prod after cleanup.
        conn.execute(
            "DELETE FROM show_relation"
            " WHERE show_id = 's-stub' OR related_show_id = 's-stub'"
        )
        conn.execute("DELETE FROM show_external_id WHERE show_id = 's-stub'")
        conn.execute("DELETE FROM show WHERE id = 's-stub'")
        conn.commit()

        fake_media = {
            "relations": {
                "edges": [
                    {
                        "relationType": "PREQUEL",
                        "node": {"id": 152682, "idMal": 52482,
                                 "title": {"romaji": "Sasaki to Pii-chan"}},
                    },
                ]
            }
        }
        from lcars import anilist_client
        monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: fake_media)

        result = shows.find_sequel_parent(conn, 176314)
        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["parent_title"] == "Sasaki and Peeps"
        assert result["next_season"] == 2
        assert result["stub_show_id"] is None  # no local stub

    def test_returns_none_when_anilist_prequel_not_tracked(self, conn, monkeypatch):
        """AniList returns a PREQUEL but that show isn't tracked locally."""
        conn.execute(
            "DELETE FROM show_relation"
            " WHERE show_id = 's-stub' OR related_show_id = 's-stub'"
        )
        conn.execute("DELETE FROM show_external_id WHERE show_id = 's-stub'")
        conn.execute("DELETE FROM show WHERE id = 's-stub'")
        conn.execute("UPDATE show SET tracked = 0 WHERE id = 's-parent'")
        conn.commit()

        fake_media = {
            "relations": {
                "edges": [
                    {"relationType": "PREQUEL",
                     "node": {"id": 152682}},
                ]
            }
        }
        from lcars import anilist_client
        monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: fake_media)

        assert shows.find_sequel_parent(conn, 176314) is None

    def test_returns_none_when_anilist_fetch_fails(self, conn, monkeypatch):
        """AniList error should gracefully return None."""
        conn.execute(
            "DELETE FROM show_relation"
            " WHERE show_id = 's-stub' OR related_show_id = 's-stub'"
        )
        conn.execute("DELETE FROM show_external_id WHERE show_id = 's-stub'")
        conn.execute("DELETE FROM show WHERE id = 's-stub'")
        conn.commit()

        from lcars import anilist_client
        monkeypatch.setattr(
            anilist_client, "fetch_media",
            lambda *a, **kw: (_ for _ in ()).throw(anilist_client.AniListError("rate limited")),
        )

        assert shows.find_sequel_parent(conn, 176314) is None

    def test_returns_none_when_anilist_has_no_prequel(self, conn, monkeypatch):
        """AniList returns relations but none are PREQUEL."""
        conn.execute(
            "DELETE FROM show_relation"
            " WHERE show_id = 's-stub' OR related_show_id = 's-stub'"
        )
        conn.execute("DELETE FROM show_external_id WHERE show_id = 's-stub'")
        conn.execute("DELETE FROM show WHERE id = 's-stub'")
        conn.commit()

        fake_media = {
            "relations": {
                "edges": [
                    {"relationType": "SIDE_STORY",
                     "node": {"id": 152682}},
                ]
            }
        }
        from lcars import anilist_client
        monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: fake_media)

        assert shows.find_sequel_parent(conn, 176314) is None


class TestCreateShowSequelDetection:
    """create_show should raise SequelDetectedError when the input's
    anilist_id points at a sequel stub of a tracked show."""

    def test_raises_sequel_detected_for_stub(self, conn):
        input_data = {
            "media_shape": "episodic",
            "tracking_space": "anime",
            "primary_title": "english",
            "title_english": "Sasaki and Peeps Season 2",
            "anilist_id": 176314,
            "mal_id": 58518,
        }
        with pytest.raises(shows.SequelDetectedError) as exc_info:
            shows.create_show(conn, input_data)
        err = exc_info.value
        assert err.parent_show_id == "s-parent"
        assert err.parent_title == "Sasaki and Peeps"
        assert err.next_season == 2
        assert err.sequel_anilist_id == 176314
        assert err.sequel_mal_id == 58518
        # Error message is parseable JSON
        assert str(err).startswith("sequel_of:")

    def test_no_sequel_error_for_unrelated_anilist_id(self, conn):
        """An AniList ID with no sequel relation should not trigger detection."""
        # find_sequel_parent should return None — no stub exists for 999999
        assert shows.find_sequel_parent(conn, 999999) is None

    def test_error_message_is_parseable(self, conn):
        """SequelDetectedError's str() format is machine-parseable."""
        input_data = {
            "media_shape": "episodic",
            "tracking_space": "anime",
            "primary_title": "english",
            "title_english": "Sasaki and Peeps Season 2",
            "anilist_id": 176314,
            "mal_id": 58518,
        }
        with pytest.raises(shows.SequelDetectedError) as exc_info:
            shows.create_show(conn, input_data)
        import json
        msg = str(exc_info.value)
        assert msg.startswith("sequel_of:")
        payload = json.loads(msg[len("sequel_of:"):])
        assert payload["parentShowId"] == "s-parent"
        assert payload["parentTitle"] == "Sasaki and Peeps"
        assert payload["nextSeason"] == 2
        assert payload["sequelAnilistId"] == 176314
        assert payload["sequelMalId"] == 58518

    def test_error_handles_title_with_colons(self, conn):
        """Titles containing colons don't break the JSON error format."""
        # Rename the parent to include colons
        conn.execute(
            "UPDATE show SET title_english = 'Kusuriya no Hitorigoto: Season 2'"
            " WHERE id = 's-parent'"
        )
        conn.commit()
        import json
        err = shows.SequelDetectedError(
            "s-parent", "Kusuriya no Hitorigoto: Season 2", 2,
            sequel_anilist_id=176314, sequel_mal_id=58518,
        )
        msg = str(err)
        payload = json.loads(msg[len("sequel_of:"):])
        assert payload["parentTitle"] == "Kusuriya no Hitorigoto: Season 2"


class TestTvdbCollision:
    """W1 — TVDB franchise collision tier: same TVDB ID = same franchise."""

    @pytest.fixture()
    def tvdb_conn(self, conn):
        """Add TVDB external IDs to the fixture data."""
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url)"
            " VALUES ('s-parent', 'tvdb', '394073', 'https://thetvdb.com/series/394073')"
        )
        conn.commit()
        return conn

    def test_finds_parent_via_shared_tvdb_id(self, tvdb_conn):
        """When no show_relation exists but the input carries the same
        TVDB ID as the parent, the TVDB collision tier finds it."""
        # Remove the relation so tier 1 misses.
        tvdb_conn.execute("DELETE FROM show_relation")
        tvdb_conn.commit()

        # Stub s-stub has anilist 176314 but no relation → tier 1 misses.
        # tvdb_id=394073 matches s-parent → tier 2 hits.
        result = shows.find_sequel_parent(
            tvdb_conn, 176314, tvdb_id=394073,
        )
        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["parent_title"] == "Sasaki and Peeps"
        assert result["next_season"] == 2
        assert result["stub_show_id"] == "s-stub"

    def test_tvdb_only_no_anilist_id(self, tvdb_conn):
        """A show added with only a TVDB ID (no anilist_id) should still
        detect the parent — this is the onTmdbChipClick case."""
        result = shows.find_sequel_parent(
            tvdb_conn, tvdb_id=394073,
        )
        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["stub_show_id"] is None

    def test_tvdb_collision_excludes_self(self, tvdb_conn):
        """When the stub itself is the only tracked show with this TVDB
        ID, it must not match itself as the parent."""
        # Remove the parent's TVDB ID, give it to the stub instead.
        tvdb_conn.execute(
            "DELETE FROM show_external_id"
            " WHERE show_id = 's-parent' AND service = 'tvdb'"
        )
        tvdb_conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url)"
            " VALUES ('s-stub', 'tvdb', '394073', NULL)"
        )
        tvdb_conn.execute("UPDATE show SET tracked = 1 WHERE id = 's-stub'")
        tvdb_conn.execute("DELETE FROM show_relation")
        tvdb_conn.commit()

        # s-stub owns anilist 176314 → stub_show_id = 's-stub'.
        # TVDB 394073 → only s-stub has it, but it's excluded → no match.
        result = shows.find_sequel_parent(
            tvdb_conn, 176314, tvdb_id=394073,
        )
        assert result is None

    def test_tvdb_collision_requires_tracked_parent(self, tvdb_conn):
        """TVDB collision should only match tracked parents."""
        tvdb_conn.execute("UPDATE show SET tracked = 0 WHERE id = 's-parent'")
        tvdb_conn.execute("DELETE FROM show_relation")
        tvdb_conn.commit()

        result = shows.find_sequel_parent(
            tvdb_conn, tvdb_id=394073,
        )
        assert result is None

    def test_local_relations_still_take_precedence(self, tvdb_conn):
        """Tier 1 (local relations) should short-circuit before TVDB collision."""
        # Both tiers would match — relation should win.
        result = shows.find_sequel_parent(
            tvdb_conn, 176314, tvdb_id=394073,
        )
        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["stub_show_id"] == "s-stub"


class TestMultiIdStubLookup:
    """W1 — _find_stub_show finds stubs via any provided ID."""

    def test_finds_stub_via_tvdb_id(self, conn):
        """When the stub has a TVDB ID and the parent shares a different
        external ID, the TVDB tier still finds the parent."""
        conn.execute("DELETE FROM show_relation")
        # Give parent a TVDB ID.
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url)"
            " VALUES ('s-parent', 'tvdb', '394073', NULL)"
        )
        # Give stub a MAL ID so _find_stub_show can locate it.
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url)"
            " VALUES ('s-stub', 'mal', '58518', NULL)"
        )
        conn.commit()

        # No anilist_id, but mal_id finds the stub, tvdb_id finds the parent.
        result = shows.find_sequel_parent(
            conn, tvdb_id=394073, mal_id=58518,
        )
        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["stub_show_id"] == "s-stub"

    def test_returns_none_when_all_ids_none(self, conn):
        assert shows.find_sequel_parent(conn) is None

    def test_fall_through_from_tier1_to_tier2(self, conn):
        """When a stub exists but has no show_relation, TVDB tier fires."""
        conn.execute("DELETE FROM show_relation")
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url)"
            " VALUES ('s-parent', 'tvdb', '394073', NULL)"
        )
        conn.commit()

        # Stub s-stub exists (anilist 176314), no relation → tier 1 misses.
        # tvdb_id=394073 → tier 2 finds s-parent.
        result = shows.find_sequel_parent(
            conn, 176314, tvdb_id=394073,
        )
        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["stub_show_id"] == "s-stub"


class TestFribbResolution:
    """W1 — Fribb dataset resolves anilist→tvdb and season number."""

    FRIBB_DATASET = [
        {
            "anilist_id": 176314,
            "tvdb_id": 394073,
            "mal_id": 58518,
            "season": {"tvdb": 2},
        },
        {
            "anilist_id": 152682,
            "tvdb_id": 394073,
            "mal_id": 52482,
            "season": {"tvdb": 1},
        },
    ]

    @pytest.fixture()
    def tvdb_conn(self, conn):
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url)"
            " VALUES ('s-parent', 'tvdb', '394073', NULL)"
        )
        conn.execute("DELETE FROM show_relation")
        conn.commit()
        return conn

    def _patch_fribb(self, monkeypatch):
        from lcars import fribb as fribb_mod
        monkeypatch.setattr(fribb_mod, "DATASET_CACHE_PATH",
                            type(fribb_mod.DATASET_CACHE_PATH)("/dev/null"))
        monkeypatch.setattr(fribb_mod, "load_dataset",
                            lambda **kw: self.FRIBB_DATASET)

    def test_anilist_to_tvdb_via_fribb(self, tvdb_conn, monkeypatch):
        """anilist_id only → Fribb resolves tvdb_id → TVDB collision finds parent."""
        self._patch_fribb(monkeypatch)
        result = shows.find_sequel_parent(tvdb_conn, 176314)
        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["next_season"] == 2

    def test_fribb_season_number_used(self, tvdb_conn, monkeypatch):
        """Fribb's season.tvdb should be used as next_season, not max+1."""
        self._patch_fribb(monkeypatch)
        result = shows.find_sequel_parent(tvdb_conn, 176314)
        assert result is not None
        assert result["next_season"] == 2

    def test_fribb_season_already_exists_returns_none(self, tvdb_conn, monkeypatch):
        """If the resolved Fribb season already exists on the parent,
        it's a duplicate — not a sequel."""
        self._patch_fribb(monkeypatch)
        tvdb_conn.execute(
            "INSERT INTO season (id, show_id, season_number)"
            " VALUES ('z-seas02', 's-parent', 2)"
        )
        tvdb_conn.commit()
        result = shows.find_sequel_parent(tvdb_conn, 176314)
        assert result is None

    def test_mal_to_tvdb_via_fribb(self, tvdb_conn, monkeypatch):
        """mal_id only → Fribb resolves tvdb_id → TVDB collision finds parent."""
        self._patch_fribb(monkeypatch)
        tvdb_conn.execute("DELETE FROM show_external_id WHERE show_id = 's-stub'")
        tvdb_conn.execute("DELETE FROM show WHERE id = 's-stub'")
        tvdb_conn.commit()
        result = shows.find_sequel_parent(tvdb_conn, mal_id=58518)
        assert result is not None
        assert result["parent_show_id"] == "s-parent"


class TestWikidataResolution:
    """W1 — Wikidata bridge resolves tmdb/imdb→tvdb for non-anime shows."""

    WIKIDATA_DATASET = [
        {"tvdb": "394073", "tmdb": "12345", "imdb": "tt9999999"},
    ]

    @pytest.fixture()
    def tvdb_conn(self, conn):
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url)"
            " VALUES ('s-parent', 'tvdb', '394073', NULL)"
        )
        conn.execute("DELETE FROM show_relation")
        conn.commit()
        return conn

    def _patch_wikidata(self, monkeypatch):
        from lcars import wikidata
        monkeypatch.setattr(wikidata, "CACHE_PATH",
                            type(wikidata.CACHE_PATH)("/dev/null"))
        monkeypatch.setattr(wikidata, "load_dataset",
                            lambda **kw: self.WIKIDATA_DATASET)

    def test_tmdb_to_tvdb_via_wikidata(self, tvdb_conn, monkeypatch):
        """tmdb_id only → Wikidata resolves tvdb_id → TVDB collision finds parent."""
        self._patch_wikidata(monkeypatch)
        tvdb_conn.execute("DELETE FROM show_external_id WHERE show_id = 's-stub'")
        tvdb_conn.execute("DELETE FROM show WHERE id = 's-stub'")
        tvdb_conn.commit()
        result = shows.find_sequel_parent(tvdb_conn, tmdb_id=12345)
        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["stub_show_id"] is None

    def test_imdb_to_tvdb_via_wikidata(self, tvdb_conn, monkeypatch):
        """imdb_id only → Wikidata resolves tvdb_id → TVDB collision finds parent."""
        self._patch_wikidata(monkeypatch)
        tvdb_conn.execute("DELETE FROM show_external_id WHERE show_id = 's-stub'")
        tvdb_conn.execute("DELETE FROM show WHERE id = 's-stub'")
        tvdb_conn.commit()
        result = shows.find_sequel_parent(tvdb_conn, imdb_id="tt9999999")
        assert result is not None
        assert result["parent_show_id"] == "s-parent"


class TestAutoAttachSequel:
    """W3(a) — _auto_attach_sequel creates a season row on the parent."""

    @pytest.fixture()
    def attach_conn(self, tmp_path):
        """Richer schema for auto-attach tests (needs pending_review, season_external_id)."""
        c = db.connect(tmp_path / "attach_test.db")
        c.executescript("""
            CREATE TABLE show (
                id TEXT PRIMARY KEY,
                media_shape TEXT NOT NULL DEFAULT 'episodic',
                tracking_space TEXT NOT NULL DEFAULT 'anime',
                title_romaji TEXT,
                title_english TEXT,
                title_native TEXT,
                primary_title TEXT NOT NULL DEFAULT 'romaji',
                status TEXT NOT NULL DEFAULT 'watching',
                tracked INTEGER NOT NULL DEFAULT 1,
                poster_url TEXT, banner_url TEXT,
                created_at TEXT NOT NULL DEFAULT '2026-01-01',
                updated_at TEXT NOT NULL DEFAULT '2026-01-01'
            );
            CREATE TABLE season (
                id TEXT PRIMARY KEY,
                show_id TEXT NOT NULL REFERENCES show (id),
                season_number INTEGER NOT NULL,
                part_number INTEGER NOT NULL DEFAULT 1,
                status TEXT,
                anilist_id INTEGER,
                mal_id INTEGER,
                source TEXT NOT NULL DEFAULT 'unmatched',
                matched INTEGER DEFAULT 0,
                manual_override INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '2026-01-01',
                updated_at TEXT NOT NULL DEFAULT '2026-01-01',
                UNIQUE (show_id, season_number, part_number)
            );
            CREATE TABLE season_external_id (
                season_id TEXT NOT NULL REFERENCES season (id),
                service TEXT NOT NULL,
                external_id TEXT NOT NULL,
                name TEXT,
                created_at TEXT NOT NULL DEFAULT '2026-01-01',
                UNIQUE (season_id, service)
            );
            CREATE TABLE show_external_id (
                show_id TEXT NOT NULL REFERENCES show (id),
                service TEXT NOT NULL,
                external_id TEXT NOT NULL,
                url TEXT,
                created_at TEXT NOT NULL DEFAULT '2026-01-01',
                UNIQUE (service, external_id)
            );
            CREATE TABLE pending_review (
                id TEXT PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                field TEXT NOT NULL,
                source TEXT NOT NULL,
                previous_value TEXT,
                proposed_value_chain TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '2026-01-01',
                resolved_at TEXT,
                resolved_by_client TEXT,
                resolution_note TEXT
            );
            CREATE TABLE _id_counter (
                prefix TEXT PRIMARY KEY,
                next_seq INTEGER NOT NULL DEFAULT 1
            );

            INSERT INTO show (id, title_romaji, title_english, primary_title, tracked, status)
            VALUES ('s-parent', 'Sasaki to Pii-chan', 'Sasaki and Peeps', 'english', 1, 'watching');
            INSERT INTO season (id, show_id, season_number, source)
            VALUES ('z-seas01', 's-parent', 1, 'fribb');
        """)
        c.commit()
        return c

    def test_auto_attach_creates_season(self, attach_conn):
        from lcars import show_backfill
        err = shows.SequelDetectedError(
            "s-parent", "Sasaki and Peeps", 2,
            sequel_anilist_id=176314, sequel_mal_id=58518,
        )
        entry = {"service": "anilist", "title": "Sasaki S2"}
        result = show_backfill._auto_attach_sequel(attach_conn, err, entry)

        assert result is not None
        assert result["parent_show_id"] == "s-parent"
        assert result["season_number"] == 2
        assert result["service"] == "anilist"

        row = attach_conn.execute(
            "SELECT * FROM season WHERE show_id = 's-parent' AND season_number = 2",
        ).fetchone()
        assert row is not None
        assert row["source"] == "auto"
        assert row["anilist_id"] == 176314
        assert row["mal_id"] == 58518
        assert row["manual_override"] == 0
        assert row["status"] == "planned"

    def test_auto_attach_skips_existing_season(self, attach_conn):
        from lcars import show_backfill
        attach_conn.execute(
            "INSERT INTO season (id, show_id, season_number, source)"
            " VALUES ('z-seas02', 's-parent', 2, 'fribb')"
        )
        attach_conn.commit()

        err = shows.SequelDetectedError(
            "s-parent", "Sasaki and Peeps", 2,
            sequel_anilist_id=176314, sequel_mal_id=58518,
        )
        entry = {"service": "anilist", "title": "Sasaki S2"}
        result = show_backfill._auto_attach_sequel(attach_conn, err, entry)
        assert result is None

    def test_auto_attach_records_pending_review(self, attach_conn):
        from lcars import show_backfill
        err = shows.SequelDetectedError(
            "s-parent", "Sasaki and Peeps", 2,
            sequel_anilist_id=176314, sequel_mal_id=58518,
        )
        entry = {"service": "anilist", "title": "Sasaki S2"}
        show_backfill._auto_attach_sequel(attach_conn, err, entry)

        review = attach_conn.execute(
            "SELECT * FROM pending_review WHERE field = 'sequel_auto_attached'",
        ).fetchone()
        assert review is not None
        assert review["entity_type"] == "season"
        assert review["source"] == "backfill"

    def test_auto_attach_inherits_dropped_status(self, attach_conn):
        from lcars import show_backfill
        attach_conn.execute(
            "UPDATE show SET status = 'dropped' WHERE id = 's-parent'"
        )
        attach_conn.commit()

        err = shows.SequelDetectedError(
            "s-parent", "Sasaki and Peeps", 2,
            sequel_anilist_id=176314, sequel_mal_id=58518,
        )
        entry = {"service": "anilist", "title": "Sasaki S2"}
        show_backfill._auto_attach_sequel(attach_conn, err, entry)

        row = attach_conn.execute(
            "SELECT status FROM season WHERE show_id = 's-parent' AND season_number = 2",
        ).fetchone()
        assert row["status"] == "dropped"


class TestCheckLaterSeasonPreAdd:
    """W6: _check_later_season_pre_add raises LaterSeasonError when the
    entry is S2+ of an untracked franchise, carrying S1's info."""

    def test_raises_for_s2_untracked_franchise(self, conn):
        fake_dataset = [
            {"anilist_id": 100, "tvdb_id": 9000, "mal_id": 200,
             "season": {"tvdb": 1}},
            {"anilist_id": 101, "tvdb_id": 9000, "mal_id": 201,
             "season": {"tvdb": 2}},
        ]
        fake_media = {
            "title": {"romaji": "Test S1", "english": "Test Season 1"},
        }
        with (
            patch.object(shows, "_try_load_fribb_dataset", return_value=fake_dataset),
            patch("lcars.anilist_client.fetch_media", return_value=fake_media),
        ):
            with pytest.raises(shows.LaterSeasonError) as exc_info:
                shows._check_later_season_pre_add(conn, 101)
            err = exc_info.value
            assert err.season_number == 2
            assert err.s1_anilist_id == 100
            assert err.s1_mal_id == 200
            assert err.s1_title_romaji == "Test S1"
            assert err.s1_title_english == "Test Season 1"

    def test_no_raise_for_season_1(self, conn):
        fake_dataset = [
            {"anilist_id": 100, "tvdb_id": 9000, "mal_id": 200,
             "season": {"tvdb": 1}},
        ]
        with patch.object(shows, "_try_load_fribb_dataset", return_value=fake_dataset):
            shows._check_later_season_pre_add(conn, 100)

    def test_no_raise_when_franchise_tracked(self, conn):
        conn.execute(
            "INSERT INTO show (id, tracked) VALUES ('s-tracked', 1)"
        )
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id)"
            " VALUES ('s-tracked', 'tvdb', '9000')"
        )
        conn.commit()

        fake_dataset = [
            {"anilist_id": 100, "tvdb_id": 9000, "mal_id": 200,
             "season": {"tvdb": 1}},
            {"anilist_id": 101, "tvdb_id": 9000, "mal_id": 201,
             "season": {"tvdb": 2}},
        ]
        with patch.object(shows, "_try_load_fribb_dataset", return_value=fake_dataset):
            shows._check_later_season_pre_add(conn, 101)

    def test_no_raise_when_no_fribb_data(self, conn):
        with patch.object(shows, "_try_load_fribb_dataset", return_value=None):
            shows._check_later_season_pre_add(conn, 101)

    def test_no_raise_when_anilist_id_none(self, conn):
        shows._check_later_season_pre_add(conn, None)

    def test_no_raise_for_ambiguous_s1(self, conn):
        """Multiple S1 candidates for the TVDB ID — don't guess."""
        fake_dataset = [
            {"anilist_id": 100, "tvdb_id": 9000, "mal_id": 200,
             "season": {"tvdb": 1}},
            {"anilist_id": 102, "tvdb_id": 9000, "mal_id": 202,
             "season": {"tvdb": 1}},
            {"anilist_id": 101, "tvdb_id": 9000, "mal_id": 201,
             "season": {"tvdb": 2}},
        ]
        with patch.object(shows, "_try_load_fribb_dataset", return_value=fake_dataset):
            shows._check_later_season_pre_add(conn, 101)

    def test_no_raise_for_single_entry_franchise(self, conn):
        """Only one Fribb entry for the TVDB ID (the S2 itself) — no S1
        to point to, so don't raise."""
        fake_dataset = [
            {"anilist_id": 101, "tvdb_id": 9000, "mal_id": 201,
             "season": {"tvdb": 2}},
        ]
        with patch.object(shows, "_try_load_fribb_dataset", return_value=fake_dataset):
            shows._check_later_season_pre_add(conn, 101)

    def test_error_message_prefix(self, conn):
        fake_dataset = [
            {"anilist_id": 100, "tvdb_id": 9000, "mal_id": 200,
             "season": {"tvdb": 1}},
            {"anilist_id": 101, "tvdb_id": 9000, "mal_id": 201,
             "season": {"tvdb": 2}},
        ]
        fake_media = {
            "title": {"romaji": "Test", "english": None},
        }
        with (
            patch.object(shows, "_try_load_fribb_dataset", return_value=fake_dataset),
            patch("lcars.anilist_client.fetch_media", return_value=fake_media),
        ):
            with pytest.raises(shows.LaterSeasonError) as exc_info:
                shows._check_later_season_pre_add(conn, 101)
            assert str(exc_info.value).startswith("later_season:")
