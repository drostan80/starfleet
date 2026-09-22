"""Art asset selection — show column denormalisation (v0.2.3.x fix).

When an art asset is selected/deselected at the show level, the URL
must be written back to ``show.banner_url`` / ``show.poster_url`` so
that fast-path resolvers (calendar, list views) return the correct art
without an ``art_asset`` table lookup.
"""

import pytest

from lcars import art, db


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
            show_id TEXT NOT NULL REFERENCES show (id)
        );
        CREATE TABLE art_asset (
            id TEXT PRIMARY KEY,
            show_id TEXT NOT NULL REFERENCES show (id),
            season_id TEXT REFERENCES season (id),
            kind TEXT NOT NULL,
            source TEXT NOT NULL,
            url TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            language TEXT,
            source_score INTEGER,
            selected INTEGER NOT NULL DEFAULT 0,
            episode_kind TEXT NOT NULL DEFAULT 'regular',
            created_at TEXT NOT NULL DEFAULT '2026-01-01',
            UNIQUE (show_id, season_id, kind, source, url, episode_kind)
        );

        INSERT INTO show (id, poster_url, banner_url)
        VALUES ('s-test01', 'old-poster.jpg', 'old-banner.jpg');

        INSERT INTO season (id, show_id)
        VALUES ('n-seas01', 's-test01');
    """)
    # art.py calls ids.generate_id which needs the _id_counter table
    c.execute("""
        CREATE TABLE IF NOT EXISTS _id_counter (
            prefix TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL DEFAULT 1
        )
    """)
    c.commit()
    return c


class TestSelectAssetWritesBackToShow:
    def test_banner_selection_updates_show_banner_url(self, conn):
        aid = art.upsert_asset(conn, "s-test01", None, "banner", "anilist",
                               "https://cdn/new-banner.jpg")
        conn.commit()
        art.select_asset(conn, aid)

        row = conn.execute("SELECT banner_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["banner_url"] == "https://cdn/new-banner.jpg"

    def test_poster_selection_updates_show_poster_url(self, conn):
        aid = art.upsert_asset(conn, "s-test01", None, "poster", "anilist",
                               "https://cdn/new-poster.jpg")
        conn.commit()
        art.select_asset(conn, aid)

        row = conn.execute("SELECT poster_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["poster_url"] == "https://cdn/new-poster.jpg"

    def test_background_selection_updates_show_banner_url(self, conn):
        """Background assets feed the banner slot in the UI."""
        aid = art.upsert_asset(conn, "s-test01", None, "background", "tvdb",
                               "https://cdn/bg.jpg")
        conn.commit()
        art.select_asset(conn, aid)

        row = conn.execute("SELECT banner_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["banner_url"] == "https://cdn/bg.jpg"

    def test_season_level_selection_does_not_touch_show(self, conn):
        aid = art.upsert_asset(conn, "s-test01", "n-seas01", "banner", "tvdb",
                               "https://cdn/season-banner.jpg")
        conn.commit()
        art.select_asset(conn, aid)

        row = conn.execute("SELECT banner_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["banner_url"] == "old-banner.jpg"

    def test_reselection_updates_to_new_url(self, conn):
        a1 = art.upsert_asset(conn, "s-test01", None, "banner", "anilist",
                               "https://cdn/banner-a.jpg")
        a2 = art.upsert_asset(conn, "s-test01", None, "banner", "tvdb",
                               "https://cdn/banner-b.jpg")
        conn.commit()
        art.select_asset(conn, a1)
        art.select_asset(conn, a2)

        row = conn.execute("SELECT banner_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["banner_url"] == "https://cdn/banner-b.jpg"


class TestDeselectAssetClearsShow:
    def test_deselect_clears_show_banner_url(self, conn):
        aid = art.upsert_asset(conn, "s-test01", None, "banner", "anilist",
                               "https://cdn/new-banner.jpg")
        conn.commit()
        art.select_asset(conn, aid)
        art.deselect_asset(conn, aid)

        row = conn.execute("SELECT banner_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["banner_url"] is None

    def test_deselect_season_level_does_not_touch_show(self, conn):
        aid = art.upsert_asset(conn, "s-test01", "n-seas01", "poster", "tvdb",
                               "https://cdn/season-poster.jpg")
        conn.commit()
        art.select_asset(conn, aid)
        art.deselect_asset(conn, aid)

        row = conn.execute("SELECT poster_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["poster_url"] == "old-poster.jpg"


class TestAutoSelectBestWritesBack:
    def test_auto_select_writes_show_columns(self, conn):
        art.upsert_asset(conn, "s-test01", None, "banner", "tvdb",
                         "https://cdn/auto-banner.jpg", source_score=100)
        art.upsert_asset(conn, "s-test01", None, "poster", "anilist",
                         "https://cdn/auto-poster.jpg")
        conn.commit()

        art.auto_select_best(conn, "s-test01")

        row = conn.execute(
            "SELECT banner_url, poster_url FROM show WHERE id = 's-test01'"
        ).fetchone()
        assert row["banner_url"] == "https://cdn/auto-banner.jpg"
        assert row["poster_url"] == "https://cdn/auto-poster.jpg"


class TestDeleteAsset:
    def test_delete_removes_row(self, conn):
        aid = art.upsert_asset(conn, "s-test01", None, "poster", "tvdb",
                               "https://cdn/stale-poster.jpg")
        conn.commit()

        art.delete_asset(conn, aid)

        row = conn.execute("SELECT 1 FROM art_asset WHERE id = ?", (aid,)).fetchone()
        assert row is None

    def test_delete_of_selected_asset_clears_show_column(self, conn):
        aid = art.upsert_asset(conn, "s-test01", None, "poster", "tvdb",
                               "https://cdn/stale-poster.jpg")
        conn.commit()
        art.select_asset(conn, aid)

        art.delete_asset(conn, aid)

        row = conn.execute("SELECT poster_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["poster_url"] is None

    def test_delete_of_unselected_asset_leaves_show_column_untouched(self, conn):
        selected_id = art.upsert_asset(conn, "s-test01", None, "poster", "anilist",
                                       "https://cdn/good-poster.jpg")
        stale_id = art.upsert_asset(conn, "s-test01", None, "poster", "tvdb",
                                    "https://cdn/stale-poster.jpg")
        conn.commit()
        art.select_asset(conn, selected_id)

        art.delete_asset(conn, stale_id)

        row = conn.execute("SELECT poster_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["poster_url"] == "https://cdn/good-poster.jpg"

    def test_delete_unknown_asset_raises(self, conn):
        with pytest.raises(ValueError):
            art.delete_asset(conn, "h-nonexi")


def _is_selected(conn, asset_id):
    return conn.execute(
        "SELECT selected FROM art_asset WHERE id = ?", (asset_id,)
    ).fetchone()["selected"] == 1


class TestEpisodeKindArtIsolatedFromRegular:
    """episode_kind (migration df50e70a1faa, 2026-09-22) — a distinct,
    manual-only cover shared by all SPECIAL/OVA/BONUS_MOVIE episodes of
    a show. Must never interfere with the regular show.poster_url/
    banner_url slot's own selection, deselection, or auto-selection."""

    def test_special_selection_does_not_touch_show_poster_url(self, conn):
        aid = art.upsert_asset(conn, "s-test01", None, "poster", "manual",
                               "https://cdn/special-cover.jpg", episode_kind="special")
        conn.commit()
        art.select_asset(conn, aid)

        row = conn.execute("SELECT poster_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["poster_url"] == "old-poster.jpg"  # unchanged

    def test_special_and_regular_can_both_be_selected_in_same_slot(self, conn):
        regular_id = art.upsert_asset(conn, "s-test01", None, "poster", "anilist",
                                      "https://cdn/regular.jpg")
        special_id = art.upsert_asset(conn, "s-test01", None, "poster", "manual",
                                      "https://cdn/special.jpg", episode_kind="special")
        conn.commit()
        art.select_asset(conn, regular_id)
        art.select_asset(conn, special_id)

        assert _is_selected(conn, regular_id)
        assert _is_selected(conn, special_id)

    def test_selecting_a_new_special_only_deselects_the_previous_special(self, conn):
        regular_id = art.upsert_asset(conn, "s-test01", None, "poster", "anilist",
                                      "https://cdn/regular.jpg")
        art.select_asset(conn, regular_id)
        special_a = art.upsert_asset(conn, "s-test01", None, "poster", "manual",
                                     "https://cdn/special-a.jpg", episode_kind="special")
        art.select_asset(conn, special_a)
        special_b = art.upsert_asset(conn, "s-test01", None, "poster", "manual",
                                     "https://cdn/special-b.jpg", episode_kind="special")
        conn.commit()

        art.select_asset(conn, special_b)

        assert _is_selected(conn, regular_id)  # untouched by the special-slot reselection
        assert not _is_selected(conn, special_a)
        assert _is_selected(conn, special_b)

    def test_deselecting_a_special_does_not_clear_show_poster_url(self, conn):
        regular_id = art.upsert_asset(conn, "s-test01", None, "poster", "anilist",
                                      "https://cdn/regular.jpg")
        art.select_asset(conn, regular_id)
        special_id = art.upsert_asset(conn, "s-test01", None, "poster", "manual",
                                      "https://cdn/special.jpg", episode_kind="special")
        conn.commit()
        art.select_asset(conn, special_id)

        art.deselect_asset(conn, special_id)

        row = conn.execute("SELECT poster_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["poster_url"] == "https://cdn/regular.jpg"

    def test_get_selected_url_defaults_to_regular(self, conn):
        aid = art.upsert_asset(
            conn, "s-test01", None, "poster", "anilist", "https://cdn/regular.jpg",
        )
        conn.commit()
        art.select_asset(conn, aid)

        assert art.get_selected_url(conn, "s-test01", None, "poster") == "https://cdn/regular.jpg"
        assert art.get_selected_url(
            conn, "s-test01", None, "poster", episode_kind="special"
        ) is None

    def test_get_selected_url_returns_the_special_cover(self, conn):
        aid = art.upsert_asset(conn, "s-test01", None, "poster", "manual",
                               "https://cdn/special.jpg", episode_kind="special")
        conn.commit()
        art.select_asset(conn, aid)

        assert art.get_selected_url(conn, "s-test01", None, "poster", episode_kind="special") \
            == "https://cdn/special.jpg"

    def test_auto_select_best_does_not_pick_a_special_for_the_regular_slot(self, conn):
        art.upsert_asset(conn, "s-test01", None, "poster", "manual",
                         "https://cdn/special.jpg", episode_kind="special", source_score=999)
        art.upsert_asset(conn, "s-test01", None, "poster", "anilist",
                         "https://cdn/regular.jpg")
        conn.commit()

        art.auto_select_best(conn, "s-test01")

        row = conn.execute("SELECT poster_url FROM show WHERE id = 's-test01'").fetchone()
        assert row["poster_url"] == "https://cdn/regular.jpg"

    def test_auto_select_best_selects_the_special_within_its_own_slot(self, conn):
        art.upsert_asset(conn, "s-test01", None, "poster", "manual",
                         "https://cdn/special.jpg", episode_kind="special")
        conn.commit()

        art.auto_select_best(conn, "s-test01")

        selected = conn.execute(
            "SELECT selected FROM art_asset WHERE show_id = 's-test01' AND episode_kind = 'special'"
        ).fetchone()
        assert selected["selected"] == 1
