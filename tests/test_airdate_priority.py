"""airdate_priority.should_apply() — the centralized rule every
automatic air-date writer (AniList reconciliation, animeschedule,
Syoboi rewire) is supposed to call before overwriting an episode's air
date. Redesigned 2026-09-22 (see the module's own docstring for the
full "The World Is Dancing" episode 13 bug this replaces): no fixed
source hierarchy any more — same source re-asserting always applies
(a genuine reschedule); a different source only wins with an earlier
date; `manual` is absolute; `sonarr`'s own raw date is treated as a
weak placeholder any other source may correct in either direction.

Also includes a parity test between this Python function and
`syoboi.rewire_airdates`'s own SQL WHERE fragment (`_rewire_condition`)
— the two used to be independently-maintained copies of "the same"
rule that silently drifted apart once (that drift, specifically the
`or src == "syoboi"` self-exclusion bug, is the direct root cause of
the live bug that prompted this whole redesign), so this test exists
to make sure they can't drift apart again.
"""

import sqlite3

import pytest

from lcars import airdate_priority, syoboi


class TestShouldApply:
    def test_nothing_stored_yet_always_applies(self):
        assert airdate_priority.should_apply("anilist", "2026-01-01T00:00:00Z", None, None)

    def test_manual_is_absolute(self):
        assert not airdate_priority.should_apply(
            "anilist", "2026-01-01T00:00:00Z", "manual", "2025-01-01T00:00:00Z"
        )
        # Even a candidate manual value from some other write path — the
        # gate itself doesn't special-case the candidate side, only the
        # existing side, matching every writer's own actual usage (a
        # manual write never goes through this gate at all — resolvers.py
        # writes it unconditionally).
        assert not airdate_priority.should_apply(
            "manual", "2026-01-01T00:00:00Z", "manual", "2025-01-01T00:00:00Z"
        )

    def test_same_source_reasserting_a_different_value_always_applies(self):
        # Earlier -> later, same source: a genuine reschedule, applies.
        assert airdate_priority.should_apply(
            "syoboi", "2026-02-01T00:00:00Z", "syoboi", "2026-01-01T00:00:00Z"
        )
        # Later -> earlier, same source: also applies (a correction).
        assert airdate_priority.should_apply(
            "syoboi", "2026-01-01T00:00:00Z", "syoboi", "2026-02-01T00:00:00Z"
        )

    def test_same_source_unchanged_value_is_a_noop(self):
        assert not airdate_priority.should_apply(
            "syoboi", "2026-01-01T00:00:00Z", "syoboi", "2026-01-01T00:00:00Z"
        )

    def test_different_source_earlier_date_wins(self):
        assert airdate_priority.should_apply(
            "anidb", "2026-01-01T00:00:00Z", "syoboi", "2026-01-04T00:00:00Z"
        )

    def test_different_source_later_date_loses(self):
        """The exact shape of the live bug this redesign fixes: a
        different source's later date must never look like an update."""
        assert not airdate_priority.should_apply(
            "syoboi", "2026-01-04T00:00:00Z", "anidb", "2026-01-01T00:00:00Z"
        )

    def test_different_source_equal_date_does_not_apply(self):
        assert not airdate_priority.should_apply(
            "anidb", "2026-01-01T00:00:00Z", "syoboi", "2026-01-01T00:00:00Z"
        )

    def test_sonarr_is_a_weak_source_corrected_either_direction(self):
        # Sonarr's own raw date is a placeholder, not a real competing
        # broadcast — any other source may correct it either way.
        assert airdate_priority.should_apply(
            "anilist", "2026-06-01T00:00:00Z", "sonarr", "2020-01-01T00:00:00Z"
        )
        assert airdate_priority.should_apply(
            "anilist", "2020-01-01T00:00:00Z", "sonarr", "2026-06-01T00:00:00Z"
        )

    def test_sonarr_candidate_against_a_real_source_only_wins_earlier(self):
        """The weak-source exception only applies when sonarr is the
        *existing* source — a sonarr candidate proposing a later date
        against a real, curated source still loses normally."""
        assert not airdate_priority.should_apply(
            "sonarr", "2026-06-01T00:00:00Z", "syoboi", "2026-01-01T00:00:00Z"
        )
        assert airdate_priority.should_apply(
            "sonarr", "2025-01-01T00:00:00Z", "syoboi", "2026-01-01T00:00:00Z"
        )

    def test_candidate_none_never_applies(self):
        assert not airdate_priority.should_apply(None, None, "syoboi", "2026-01-01T00:00:00Z")


class TestRewireConditionParity:
    """Constructs episode fixtures covering every branch of
    should_apply()'s decision tree, runs syoboi.rewire_airdates against
    them, and checks its outcome (updated or not) agrees with what
    should_apply() says for the same (existing_source, existing_date)
    vs (candidate_source='syoboi', candidate_date) pair. Guards against
    the two ever silently drifting apart again."""

    def _make_db(self, cases: list[tuple[str, str, str]]) -> sqlite3.Connection:
        """cases: list of (episode_id, existing_source, existing_date).
        Each gets its own show/tid/count=1 slot, with a single Syoboi
        broadcast fixed at 2026-01-10T00:00:00Z."""
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
            CREATE TABLE air_date_change (
                id TEXT PRIMARY KEY, episode_id TEXT,
                previous_air_date_utc TEXT, new_air_date_utc TEXT,
                previous_source TEXT, new_source TEXT,
                changed_at TEXT, changed_by TEXT
            );
            CREATE TABLE _id_counter (
                prefix TEXT PRIMARY KEY, next_seq INTEGER NOT NULL DEFAULT 1
            );
        """)
        for i, (episode_id, source, date) in enumerate(cases):
            show_id = f"s-{i:03d}"
            tid = 1000 + i
            conn.execute("INSERT INTO show VALUES (?)", (show_id,))
            conn.execute(
                "INSERT INTO show_external_id VALUES (?, 'syoboi', ?, '', '')",
                (show_id, str(tid)),
            )
            conn.execute(
                "INSERT INTO show_external_id VALUES (?, 'anidb', ?, '', '')",
                (show_id, str(tid)),  # anidb id doesn't need to be real, just consistent
            )
            conn.execute(
                "INSERT INTO episode VALUES"
                " (?, ?, 1, 1, 'regular', ?, ?, 1, 1, '', '', 'unwatched')",
                (episode_id, show_id, date, source),
            )
            conn.execute(
                "INSERT INTO episode_anidb_mapping VALUES (?, ?, 1, 1)",
                (episode_id, tid),
            )
            conn.execute(
                "INSERT INTO syoboi_program VALUES"
                " (?, ?, 1, 1, '2026-01-10 09:00:00', '', '2026-01-10T00:00:00Z', '', "
                "  NULL, 0, 0, '1', '', 't')",
                (i, tid),
            )
        conn.commit()
        return conn

    @pytest.mark.parametrize(
        "existing_source,existing_date",
        [
            ("syoboi", "2026-01-05T00:00:00Z"),  # same source, earlier -> should update
            ("syoboi", "2026-01-15T00:00:00Z"),  # same source, later -> should update
            ("sonarr", "2020-01-01T00:00:00Z"),  # weak source, earlier -> should update
            ("sonarr", "2026-06-01T00:00:00Z"),  # weak source, later -> should update
            ("anidb", "2026-01-15T00:00:00Z"),  # real source, syoboi earlier -> should update
            ("manual", "2020-01-01T00:00:00Z"),  # protected -> must NOT update
            ("anidb", "2026-01-01T00:00:00Z"),  # real source, syoboi later -> must NOT update
        ],
    )
    def test_rewire_matches_should_apply(self, existing_source, existing_date):
        episode_id = "e-parity"
        conn = self._make_db([(episode_id, existing_source, existing_date)])

        expected = airdate_priority.should_apply(
            "syoboi", "2026-01-10T00:00:00Z", existing_source, existing_date
        )

        syoboi.rewire_airdates(conn)

        row = conn.execute(
            "SELECT air_date_utc, air_date_source FROM episode WHERE id = ?", (episode_id,)
        ).fetchone()
        was_updated = (
            row["air_date_source"] == "syoboi" and row["air_date_utc"] == "2026-01-10T00:00:00Z"
        )
        if existing_source == "syoboi" and existing_date == "2026-01-10T00:00:00Z":
            was_updated = True  # already correct, not a meaningful "no update" case here
        assert was_updated == expected, (
            f"existing=({existing_source!r}, {existing_date!r}): "
            f"should_apply()={expected} but rewire_airdates() updated={was_updated}"
        )
