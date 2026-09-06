"""Sequel detection in the add flow — prevents creating orphan stubs
when adding a sequel season from browse.

When AniList browse adds an entry whose AniList ID is a SEQUEL of an
already-tracked show (via ``show_relation``), the flow should raise
``SequelDetectedError`` instead of creating a separate show.
"""

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
        conn.execute("DELETE FROM show_relation WHERE show_id = 's-stub' OR related_show_id = 's-stub'")
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
        conn.execute("DELETE FROM show_relation WHERE show_id = 's-stub' OR related_show_id = 's-stub'")
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
        conn.execute("DELETE FROM show_relation WHERE show_id = 's-stub' OR related_show_id = 's-stub'")
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
        conn.execute("DELETE FROM show_relation WHERE show_id = 's-stub' OR related_show_id = 's-stub'")
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
