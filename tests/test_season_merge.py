"""Season-level merge — W3(b): merging a child show (that is actually
season N of a parent) into the parent, and reversing that merge.
Also covers franchise collision detection.
"""

import json
from unittest import mock

import pytest

from lcars import db, show_merge


@pytest.fixture(autouse=True)
def _reset_db():
    db.close()
    yield
    db.close()


def _make_db(tmp_path):
    """Minimal schema + fixtures for season merge tests."""
    c = db.connect(tmp_path / "test.db")
    c.executescript("""
        CREATE TABLE show (
            id TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 's-'),
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
            id TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'z-'),
            show_id TEXT NOT NULL REFERENCES show (id),
            season_number INTEGER NOT NULL,
            part_number INTEGER NOT NULL DEFAULT 1,
            label TEXT,
            anilist_id INTEGER,
            mal_id INTEGER,
            source TEXT NOT NULL CHECK (source IN ('fribb', 'manual', 'unmatched', 'auto')),
            matched INTEGER NOT NULL DEFAULT 0 CHECK (matched IN (0, 1)),
            manual_override INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
            last_reconciled_at TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01',
            score REAL,
            started_at TEXT,
            completed_at TEXT,
            abs_start INTEGER,
            abs_end INTEGER,
            status TEXT CHECK (status IS NULL OR status IN
                ('watching','completed','planned','paused','dropped','skipped')),
            UNIQUE (show_id, season_number, part_number)
        );
        CREATE INDEX ix_season_show_id ON season (show_id);
        CREATE TABLE season_external_id (
            season_id TEXT NOT NULL REFERENCES season (id),
            service TEXT NOT NULL,
            external_id TEXT NOT NULL,
            name TEXT,
            url TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            UNIQUE (season_id, service)
        );
        CREATE TABLE episode (
            id TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'e-'),
            show_id TEXT NOT NULL REFERENCES show (id),
            season INTEGER NOT NULL,
            episode INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'regular',
            absolute_number REAL,
            air_date_utc TEXT,
            air_date_source TEXT,
            state TEXT NOT NULL DEFAULT 'unwatched',
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01',
            season_id TEXT REFERENCES season (id),
            title TEXT,
            UNIQUE (show_id, season, episode)
        );
        CREATE TABLE watch_event (
            id TEXT PRIMARY KEY,
            show_id TEXT NOT NULL,
            season INTEGER,
            episode INTEGER,
            watched_at TEXT NOT NULL DEFAULT '2026-01-01',
            platform TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            FOREIGN KEY(show_id, season, episode)
                REFERENCES episode (show_id, season, episode)
        );
        CREATE TABLE show_external_id (
            show_id TEXT NOT NULL REFERENCES show (id),
            service TEXT NOT NULL,
            external_id TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            PRIMARY KEY (show_id, service)
        );
        CREATE TABLE show_service_presence (
            show_id TEXT NOT NULL REFERENCES show (id),
            service TEXT NOT NULL,
            external_id TEXT,
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            PRIMARY KEY (show_id, service)
        );
        CREATE TABLE show_merge (
            id TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'y-'),
            winner_show_id TEXT NOT NULL REFERENCES show (id),
            loser_show_id TEXT NOT NULL REFERENCES show (id),
            matched_on TEXT NOT NULL,
            manifest TEXT NOT NULL,
            merged_at TEXT NOT NULL,
            reversed_at TEXT,
            reversed_by_client TEXT
        );
        CREATE TABLE pending_review (
            id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            field TEXT NOT NULL,
            previous_value TEXT,
            proposed_value_chain TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01',
            resolved_at TEXT,
            resolved_by_client TEXT,
            resolution_note TEXT
        );
        CREATE TABLE _id_counter (
            prefix TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL DEFAULT 1
        );
    """)
    c.commit()
    return c


def _insert_parent(conn):
    """Parent show with S1 (abs 1-24) and S2 (abs 25-48)."""
    conn.executescript("""
        INSERT INTO show (id, title_romaji, tracked)
        VALUES ('s-par001', 'Dr. STONE', 1);

        INSERT INTO season (id, show_id, season_number, source, abs_start, abs_end, status)
        VALUES ('z-ps0001', 's-par001', 1, 'fribb', 1, 24, 'completed');
        INSERT INTO season (id, show_id, season_number, source, abs_start, abs_end, status)
        VALUES ('z-ps0002', 's-par001', 2, 'fribb', 25, 48, 'completed');

        INSERT INTO show_external_id (show_id, service, external_id)
        VALUES ('s-par001', 'tvdb', '355774');
        INSERT INTO show_external_id (show_id, service, external_id)
        VALUES ('s-par001', 'anilist', '105333');
    """)

    for i in range(1, 25):
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, absolute_number, season_id)"
            " VALUES (?, 's-par001', 1, ?, ?, 'z-ps0001')",
            (f"e-p1e{i:03d}", i, float(i)),
        )
    for i in range(1, 25):
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, absolute_number, season_id)"
            " VALUES (?, 's-par001', 2, ?, ?, 'z-ps0002')",
            (f"e-p2e{i:03d}", i, float(24 + i)),
        )
    conn.commit()


def _insert_child_with_episodes(conn):
    """Child show — actually S3 of the parent, with 11 episodes."""
    conn.executescript("""
        INSERT INTO show (id, title_romaji, tracked)
        VALUES ('s-chi001', 'Dr. STONE: NEW WORLD', 1);

        INSERT INTO season (id, show_id, season_number, source, anilist_id, mal_id, status)
        VALUES ('z-cs0001', 's-chi001', 1, 'fribb', 206867, 54611, 'watching');

        INSERT INTO season_external_id (season_id, service, external_id)
        VALUES ('z-cs0001', 'anilist', '206867');
        INSERT INTO season_external_id (season_id, service, external_id)
        VALUES ('z-cs0001', 'mal', '54611');

        INSERT INTO show_external_id (show_id, service, external_id)
        VALUES ('s-chi001', 'tvdb', '355774');
        INSERT INTO show_external_id (show_id, service, external_id)
        VALUES ('s-chi001', 'mal', '54611');
    """)

    for i in range(1, 12):
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, absolute_number, season_id)"
            " VALUES (?, 's-chi001', 1, ?, ?, 'z-cs0001')",
            (f"e-c1e{i:03d}", i, float(48 + i)),
        )
    conn.commit()


def _insert_child_stub(conn):
    """Child show — stub with season row but no episodes."""
    conn.executescript("""
        INSERT INTO show (id, title_romaji, tracked)
        VALUES ('s-chi001', 'Dr. STONE: NEW WORLD', 0);

        INSERT INTO season (id, show_id, season_number, source, anilist_id, status)
        VALUES ('z-cs0001', 's-chi001', 1, 'auto', 206867, 'planned');

        INSERT INTO show_external_id (show_id, service, external_id)
        VALUES ('s-chi001', 'tvdb', '355774');
    """)
    conn.commit()


def _insert_child_no_season(conn):
    """Child show — no season row at all."""
    conn.executescript("""
        INSERT INTO show (id, title_romaji, tracked)
        VALUES ('s-chi001', 'Dr. STONE: NEW WORLD', 1);

        INSERT INTO show_external_id (show_id, service, external_id)
        VALUES ('s-chi001', 'tvdb', '355774');
    """)
    conn.commit()


class TestMergeSeasonIntoShow:
    def test_merge_with_episodes(self, tmp_path):
        """Child's S1 episodes become S3 on the parent."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        merge_id = show_merge.merge_season_into_show(
            conn, "s-par001", "s-chi001", 3, "tvdb collision"
        )
        assert merge_id.startswith("y-")

        # Child is demoted.
        child = conn.execute("SELECT tracked FROM show WHERE id = 's-chi001'").fetchone()
        assert child["tracked"] == 0

        # Season row moved to parent as S3.
        s3 = conn.execute(
            "SELECT * FROM season WHERE show_id = 's-par001' AND season_number = 3"
        ).fetchone()
        assert s3 is not None
        assert s3["id"] == "z-cs0001"
        assert s3["anilist_id"] == 206867

        # All 11 episodes renumbered to S3 on parent.
        eps = conn.execute(
            "SELECT * FROM episode WHERE show_id = 's-par001' AND season = 3"
            " ORDER BY episode"
        ).fetchall()
        assert len(eps) == 11
        assert eps[0]["absolute_number"] == 49.0
        assert eps[0]["season_id"] == "z-cs0001"

        # No episodes left on the child.
        child_eps = conn.execute(
            "SELECT COUNT(*) as c FROM episode WHERE show_id = 's-chi001'"
        ).fetchone()
        assert child_eps["c"] == 0

        # show_external_id: 'mal' moved (parent didn't have it), 'tvdb' skipped.
        manifest = json.loads(
            conn.execute("SELECT manifest FROM show_merge WHERE id = ?", (merge_id,)).fetchone()[
                "manifest"
            ]
        )
        assert "mal" in manifest["moved"]["show_external_id"]
        assert any("tvdb" in s for s in manifest["skipped"])

    def test_merge_stub_no_episodes(self, tmp_path):
        """Stub child (season row, no episodes) merges cleanly."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_stub(conn)

        show_merge.merge_season_into_show(
            conn, "s-par001", "s-chi001", 3, "tvdb collision"
        )

        s3 = conn.execute(
            "SELECT * FROM season WHERE show_id = 's-par001' AND season_number = 3"
        ).fetchone()
        assert s3 is not None
        assert s3["id"] == "z-cs0001"

    def test_merge_no_season_row(self, tmp_path):
        """Child with no season row at all — a season is created on parent."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_no_season(conn)

        merge_id = show_merge.merge_season_into_show(
            conn, "s-par001", "s-chi001", 3, "tvdb collision"
        )

        s3 = conn.execute(
            "SELECT * FROM season WHERE show_id = 's-par001' AND season_number = 3"
        ).fetchone()
        assert s3 is not None
        assert s3["source"] == "auto"

        manifest = json.loads(
            conn.execute("SELECT manifest FROM show_merge WHERE id = ?", (merge_id,)).fetchone()[
                "manifest"
            ]
        )
        assert manifest["moved"]["season_created"]  # season ID string, truthy

    def test_merge_target_season_exists_on_parent(self, tmp_path):
        """When parent already has the target season, episodes are assigned
        to the existing season row."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        # Merge child into season 2 (which parent already has).
        merge_id = show_merge.merge_season_into_show(
            conn, "s-par001", "s-chi001", 2, "tvdb collision"
        )

        # Child's season row should NOT have replaced the parent's S2.
        s2 = conn.execute(
            "SELECT id FROM season WHERE show_id = 's-par001' AND season_number = 2"
        ).fetchone()
        assert s2["id"] == "z-ps0002"  # parent's original

        # Child's episodes that don't conflict should have been moved.
        # Parent S2 has eps 1-24; child S1 has eps 1-11. All collide.
        # So all 11 should be skipped.
        manifest = json.loads(
            conn.execute("SELECT manifest FROM show_merge WHERE id = ?", (merge_id,)).fetchone()[
                "manifest"
            ]
        )
        assert len(manifest["moved"]["episodes"]) == 0
        assert any("already has this slot" in s for s in manifest["skipped"])

    def test_merge_moves_watch_events(self, tmp_path):
        """Watch events follow their episodes during renumbering."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        # Add a watch event on child's S1E1.
        conn.execute(
            "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
            " VALUES ('w-wat001', 's-chi001', 1, 1, '2026-01-15', '2026-01-15')"
        )
        conn.commit()

        show_merge.merge_season_into_show(
            conn, "s-par001", "s-chi001", 3, "tvdb collision"
        )

        we = conn.execute("SELECT * FROM watch_event WHERE id = 'w-wat001'").fetchone()
        assert we["show_id"] == "s-par001"
        assert we["season"] == 3
        assert we["episode"] == 1

    def test_merge_into_self_raises(self, tmp_path):
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        with pytest.raises(ValueError, match="cannot merge a show into itself"):
            show_merge.merge_season_into_show(
                conn, "s-par001", "s-par001", 3, "test"
            )

    def test_merge_nonexistent_show_raises(self, tmp_path):
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        with pytest.raises(ValueError, match="no such show"):
            show_merge.merge_season_into_show(
                conn, "s-par001", "s-nopexx", 3, "test"
            )


class TestReverseSeasonMerge:
    def test_reverse_restores_episodes(self, tmp_path):
        """Full round-trip: merge then reverse, everything back in place."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        merge_id = show_merge.merge_season_into_show(
            conn, "s-par001", "s-chi001", 3, "tvdb collision"
        )

        show_merge.reverse_season_merge(conn, merge_id, "test_client")

        # Child is re-promoted.
        child = conn.execute("SELECT tracked FROM show WHERE id = 's-chi001'").fetchone()
        assert child["tracked"] == 1

        # Season row back on child as S1.
        child_s1 = conn.execute(
            "SELECT * FROM season WHERE show_id = 's-chi001' AND season_number = 1"
        ).fetchone()
        assert child_s1 is not None
        assert child_s1["id"] == "z-cs0001"

        # No S3 on parent.
        parent_s3 = conn.execute(
            "SELECT 1 FROM season WHERE show_id = 's-par001' AND season_number = 3"
        ).fetchone()
        assert parent_s3 is None

        # All 11 episodes back on child S1.
        eps = conn.execute(
            "SELECT * FROM episode WHERE show_id = 's-chi001' ORDER BY episode"
        ).fetchall()
        assert len(eps) == 11
        assert eps[0]["season"] == 1
        assert eps[0]["season_id"] == "z-cs0001"

    def test_reverse_created_season_is_deleted(self, tmp_path):
        """When the merge created a new season row, reversal deletes it."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_no_season(conn)

        merge_id = show_merge.merge_season_into_show(
            conn, "s-par001", "s-chi001", 3, "tvdb collision"
        )

        show_merge.reverse_season_merge(conn, merge_id, "test_client")

        s3 = conn.execute(
            "SELECT 1 FROM season WHERE show_id = 's-par001' AND season_number = 3"
        ).fetchone()
        assert s3 is None

    def test_reverse_restores_watch_events(self, tmp_path):
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        conn.execute(
            "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
            " VALUES ('w-wat001', 's-chi001', 1, 1, '2026-01-15', '2026-01-15')"
        )
        conn.commit()

        merge_id = show_merge.merge_season_into_show(
            conn, "s-par001", "s-chi001", 3, "tvdb collision"
        )
        show_merge.reverse_season_merge(conn, merge_id, "test_client")

        we = conn.execute("SELECT * FROM watch_event WHERE id = 'w-wat001'").fetchone()
        assert we["show_id"] == "s-chi001"
        assert we["season"] == 1

    def test_reverse_restores_external_ids(self, tmp_path):
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        merge_id = show_merge.merge_season_into_show(
            conn, "s-par001", "s-chi001", 3, "tvdb collision"
        )

        # 'mal' was moved to parent.
        mal_on_parent = conn.execute(
            "SELECT 1 FROM show_external_id WHERE show_id = 's-par001' AND service = 'mal'"
        ).fetchone()
        assert mal_on_parent is not None

        show_merge.reverse_season_merge(conn, merge_id, "test_client")

        # 'mal' back on child.
        mal_on_child = conn.execute(
            "SELECT 1 FROM show_external_id WHERE show_id = 's-chi001' AND service = 'mal'"
        ).fetchone()
        assert mal_on_child is not None
        mal_on_parent = conn.execute(
            "SELECT 1 FROM show_external_id WHERE show_id = 's-par001' AND service = 'mal'"
        ).fetchone()
        assert mal_on_parent is None

    def test_reverse_already_reversed_raises(self, tmp_path):
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        merge_id = show_merge.merge_season_into_show(
            conn, "s-par001", "s-chi001", 3, "tvdb collision"
        )
        show_merge.reverse_season_merge(conn, merge_id, "test_client")

        with pytest.raises(ValueError, match="already reversed"):
            show_merge.reverse_season_merge(conn, merge_id, "test_client")

    def test_reverse_wrong_type_raises(self, tmp_path):
        """reverse_season_merge rejects a non-season_merge manifest."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        # Manually insert a show_merge with a non-season_merge manifest.
        conn.execute(
            "INSERT INTO show_merge (id, winner_show_id, loser_show_id, matched_on,"
            " manifest, merged_at) VALUES"
            " ('y-fake01', 's-par001', 's-chi001', 'test',"
            " '{\"moved\": {}, \"skipped\": []}', '2026-01-01')"
        )
        conn.commit()

        with pytest.raises(ValueError, match="not a season_merge"):
            show_merge.reverse_season_merge(conn, "y-fake01", "test_client")


class TestDetectFranchiseCollisions:
    def test_detects_tvdb_collision_and_merges(self, tmp_path):
        """Two shows sharing a TVDB ID: the child is merged into the parent."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        with mock.patch(
            "lcars.show_merge._determine_target_season", return_value=3
        ):
            result = show_merge.detect_franchise_collisions(conn)

        assert result["collisions_found"] >= 1
        assert result["merges_performed"] == 1

        # Child is demoted.
        child = conn.execute("SELECT tracked FROM show WHERE id = 's-chi001'").fetchone()
        assert child["tracked"] == 0

        # No pending_review created (franchise_auto_merge reviews removed as non-actionable).
        review = conn.execute(
            "SELECT * FROM pending_review WHERE entity_id = 's-chi001'"
            " AND field = 'franchise_auto_merge'"
        ).fetchone()
        assert review is None

    def test_skips_already_merged(self, tmp_path):
        """If a merge already exists for this pair, don't re-merge."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        with mock.patch(
            "lcars.show_merge._determine_target_season", return_value=3
        ):
            r1 = show_merge.detect_franchise_collisions(conn)
            assert r1["merges_performed"] == 1

            # Second run: already merged, should skip.
            r2 = show_merge.detect_franchise_collisions(conn)
            assert r2["merges_performed"] == 0

    def test_no_collision_when_single_show(self, tmp_path):
        """A TVDB ID belonging to only one show is not a collision."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        # No child — only one show with this TVDB.

        result = show_merge.detect_franchise_collisions(conn)
        assert result["collisions_found"] == 0
        assert result["merges_performed"] == 0

    def test_promotes_parent_when_none_tracked(self, tmp_path):
        """Case 3: both untracked — oldest is promoted via _promote_stub."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_stub(conn)
        # Make parent also untracked.
        conn.execute("UPDATE show SET tracked = 0 WHERE id = 's-par001'")
        conn.commit()

        def fake_promote(c, show_id, inp):
            c.execute(
                "UPDATE show SET tracked = 1, updated_at = '2026-09-17' WHERE id = ?",
                (show_id,),
            )
            c.commit()
            return show_id

        with mock.patch(
            "lcars.show_merge._determine_target_season", return_value=3
        ), mock.patch(
            "lcars.shows._promote_stub", side_effect=fake_promote
        ):
            result = show_merge.detect_franchise_collisions(conn)

        # Oldest show (parent, most content) got promoted.
        parent = conn.execute("SELECT tracked FROM show WHERE id = 's-par001'").fetchone()
        assert parent["tracked"] == 1

        # Child got merged.
        assert result["merges_performed"] == 1


    def test_season_collision_opens_correction_review(self, tmp_path):
        """Case 2: target season exists on parent, episodes collide — opens
        franchise_season_collision review without merging or demoting."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        # Force target season to 2 (which parent already has with eps 1-24).
        # Child has eps 1-11 which all collide with parent S2 eps 1-24.
        with mock.patch(
            "lcars.show_merge._determine_target_season", return_value=2
        ):
            result = show_merge.detect_franchise_collisions(conn)

        # No merge performed — only a review opened.
        assert result["merges_performed"] == 0

        # Child is NOT demoted.
        child = conn.execute("SELECT tracked FROM show WHERE id = 's-chi001'").fetchone()
        assert child["tracked"] == 1

        # No show_merge row created.
        merge = conn.execute(
            "SELECT 1 FROM show_merge WHERE loser_show_id = 's-chi001'"
        ).fetchone()
        assert merge is None

        # The review asks for the correct season number.
        review = conn.execute(
            "SELECT * FROM pending_review WHERE entity_id = 's-chi001'"
            " AND field = 'franchise_season_collision'"
        ).fetchone()
        assert review is not None
        chain = json.loads(review["proposed_value_chain"])
        assert chain[0] == "s-par001"

    def test_merged_pair_not_remerged(self, tmp_path):
        """After a franchise merge, re-running detection does not
        re-merge the same pair (guarded by existing show_merge row)."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        with mock.patch(
            "lcars.show_merge._determine_target_season", return_value=3
        ):
            r1 = show_merge.detect_franchise_collisions(conn)
        assert r1["merges_performed"] == 1

        # Re-run: the existing show_merge row prevents a duplicate merge.
        with mock.patch(
            "lcars.show_merge._determine_target_season", return_value=3
        ):
            r2 = show_merge.detect_franchise_collisions(conn)
        assert r2["merges_performed"] == 0


class TestDetermineTargetSeason:
    def test_uses_abs_episode_ordering(self, tmp_path):
        """Child's episodes start after parent's — placed as next season."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        parent = {"id": "s-par001", "max_season": 2}
        child = {"id": "s-chi001", "max_season": 1}

        with mock.patch("lcars.shows._resolve_fribb_season", return_value=None):
            season = show_merge._determine_target_season(conn, parent, child)

        # Parent's max abs_end is S2=48, child starts at 49.
        # Highest parent season with abs_end → season_number + 1.
        parent_seasons = conn.execute(
            "SELECT season_number, abs_end FROM season"
            " WHERE show_id = 's-par001' AND abs_end IS NOT NULL"
            " ORDER BY abs_end DESC"
        ).fetchall()
        expected = parent_seasons[0]["season_number"] + 1 if parent_seasons else 3
        assert season == expected

    def test_fallback_to_max_season_plus_one(self, tmp_path):
        """No Fribb, no abs numbers — falls back to max season + 1."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_no_season(conn)

        parent = {"id": "s-par001", "max_season": 2}
        child = {"id": "s-chi001", "max_season": None}

        with mock.patch("lcars.shows._resolve_fribb_season", return_value=None):
            season = show_merge._determine_target_season(conn, parent, child)

        assert season == 3

    def test_fribb_resolution_takes_priority(self, tmp_path):
        """Fribb says this is season 5 — use that over abs ordering."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_with_episodes(conn)

        # Child needs an AniList ID for Fribb resolution to be attempted.
        conn.execute(
            "INSERT OR IGNORE INTO show_external_id"
            " (show_id, service, external_id) VALUES ('s-chi001', 'anilist', '206867')"
        )
        conn.commit()

        parent = {"id": "s-par001", "max_season": 2}
        child = {"id": "s-chi001", "max_season": 1}

        with mock.patch("lcars.shows._resolve_fribb_season", return_value=5):
            season = show_merge._determine_target_season(conn, parent, child)

        assert season == 5

    def test_works_with_sqlite_row(self, tmp_path):
        """_determine_target_season must handle sqlite3.Row (no .get())."""
        conn = _make_db(tmp_path)
        _insert_parent(conn)
        _insert_child_stub(conn)

        # Use real Row objects like detect_franchise_collisions does.
        parent = conn.execute(
            "SELECT s.id, s.tracked, s.created_at,"
            "  (SELECT MAX(z.season_number) FROM season z WHERE z.show_id = s.id) AS max_season"
            " FROM show s WHERE s.id = 's-par001'"
        ).fetchone()
        child = conn.execute(
            "SELECT s.id, s.tracked, s.created_at,"
            "  (SELECT MAX(z.season_number) FROM season z WHERE z.show_id = s.id) AS max_season"
            " FROM show s WHERE s.id = 's-chi001'"
        ).fetchone()

        with mock.patch("lcars.shows._resolve_fribb_season", return_value=None):
            season = show_merge._determine_target_season(conn, parent, child)

        assert season == 3  # parent has S1, S2 → next is 3
