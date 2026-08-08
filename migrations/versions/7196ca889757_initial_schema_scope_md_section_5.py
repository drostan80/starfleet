"""initial schema — SCOPE.md section 5

Hand-written SQL, no ORM (SCOPE.md §11.2, resolved during BUILD_PLAN.md
0.2). Scope is deliberately exactly §5.0-§5.10 ("the data model") per
BUILD_PLAN.md A.1 — schema needs driven by §6 functional areas that get
their own later A-steps (pacing §6.2 -> A.10, global settings §6.13 ->
A.16, full-text search §6.5 -> A.12, etc.) are NOT included here, to
keep each step's migration traceable to its own commit.

Conventions used throughout, not repeated per table:
  - Every top-level entity's id is TEXT PRIMARY KEY, `{prefix}-{6
    chars}` (SCOPE.md §5.0). The CHECK below only verifies shape
    (dash position, total length) — alphabet-membership and collision
    checking are the nanoid-based generator's job (§5.0), not the DB's.
  - Booleans are INTEGER CHECK (col IN (0, 1)) — SQLite has no native
    boolean type.
  - Timestamps are TEXT, ISO-8601 UTC (e.g. "2026-08-08T12:34:56Z") —
    SQLite has no native datetime type either; TEXT keeps the DB
    hand-inspectable, consistent with the id scheme's own
    hand-readability goal (§5.0). All stored timestamps are UTC
    internally regardless of any display-timezone setting (§6.13).
  - Enums are TEXT + CHECK (col IN (...)) for the *closed* enums
    SCOPE.md actually lists as fixed value sets. Fields SCOPE.md
    explicitly marks open-ended ("tvdb | anilist | ... | ...", or a
    generic `service`/`changed_by`/`source` column meant to grow
    without a migration) are plain TEXT with no CHECK.
  - Foreign-key enforcement: SQLite defaults FK checking to OFF per
    connection. Not turned on here (migrations don't run inside the
    app's own connections) — A.3 (wiring resolvers) must run
    `PRAGMA foreign_keys = ON` on every connection it opens.

Two small SCOPE.md gaps found and fixed while drafting this (full
rationale in SCOPE.md itself, not repeated here):
  - `show_relation` (§5.9) didn't exist as a table at all, despite
    being referenced everywhere as "the relation graph" that franchise
    auto-derivation reads from.
  - `pending_review` (§5.6) had no way to record *which* row a review
    entry was about (`entity` implied type only) - split into
    `entity_type` + `entity_id`; also its `resolved_by_client` enum
    listed `aniq`, inconsistent with the "three passive/pull places"
    text right below it (aniq has no LCARS integration, §7.2).

Revision ID: 7196ca889757
Revises:
Create Date: 2026-08-08 09:47:10.656530

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7196ca889757'
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _id_check(column: str, prefix: str) -> str:
    """`{prefix}-{6 chars}` shape check — see module docstring."""
    return (
        f"length({column}) = 8 AND substr({column}, 1, 2) = '{prefix}-'"
    )


def upgrade() -> None:
    # -- 5.1 show ------------------------------------------------------
    op.execute(f"""
        CREATE TABLE show (
            id                TEXT PRIMARY KEY CHECK ({_id_check('id', 's')}),
            media_shape       TEXT NOT NULL CHECK (media_shape IN ('episodic', 'movie')),
            tracking_space    TEXT NOT NULL CHECK (tracking_space IN ('tv', 'anime')),
            -- anime mandates an AniList link (show_external_id, service='anilist') —
            -- an application-layer invariant (enforced by addShow/on-demand-fetch,
            -- A.8), not a DB constraint: SQLite CHECKs can't reference another
            -- table, and a trigger would fight the natural create-then-fetch flow.
            title_romaji      TEXT,
            title_english     TEXT,
            title_native      TEXT,
            primary_title     TEXT NOT NULL CHECK (primary_title IN ('romaji', 'english', 'native')),
            status            TEXT NOT NULL CHECK (status IN ('watching', 'planned', 'paused', 'completed', 'dropped')),
            tracked           INTEGER NOT NULL DEFAULT 1 CHECK (tracked IN (0, 1)),
            score             REAL,  -- personal 0-20 quarter-point scale, §6.1; nullable, not every show is scored
            total_episodes    INTEGER,
            duration_minutes  INTEGER,
            poster_url        TEXT,
            banner_url        TEXT,
            -- Source-provided, unnormalized display strings (§5.1) — distinct from
            -- the user-curated `tag`/`show_tag` entity below. JSON array of strings.
            genres_raw        TEXT,
            synopsis          TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL,
            CHECK (
                (primary_title = 'romaji' AND title_romaji IS NOT NULL) OR
                (primary_title = 'english' AND title_english IS NOT NULL) OR
                (primary_title = 'native' AND title_native IS NOT NULL)
            )
        )
    """)
    op.execute("CREATE INDEX ix_show_status ON show (status)")
    op.execute("CREATE INDEX ix_show_tracking_space ON show (tracking_space)")

    # -- 5.2 episode -----------------------------------------------------
    op.execute(f"""
        CREATE TABLE episode (
            id                    TEXT PRIMARY KEY CHECK ({_id_check('id', 'e')}),
            show_id               TEXT NOT NULL REFERENCES show (id),
            season                INTEGER NOT NULL,
            episode               INTEGER NOT NULL,
            kind                  TEXT NOT NULL CHECK (kind IN ('regular', 'special', 'ova', 'bonus_movie')),
            -- Decimal-capable (e.g. a synthesized "12.1") — REAL, not TEXT, so
            -- ordering/range queries work natively; nullable until known/synthesized.
            absolute_number       REAL,
            air_date_utc          TEXT,
            air_date_source       TEXT CHECK (air_date_source IN ('sonarr', 'anilist', 'animeschedule', 'manual')),
            air_date_raw_sonarr   TEXT,
            available_via_sonarr  INTEGER NOT NULL DEFAULT 0 CHECK (available_via_sonarr IN (0, 1)),
            available_via_radarr  INTEGER NOT NULL DEFAULT 0 CHECK (available_via_radarr IN (0, 1)),
            -- Generated, not app-maintained — SQLite computes this itself so it can
            -- never drift from the two source flags (§5.2: "derived: ... OR ...").
            available_locally     INTEGER GENERATED ALWAYS AS (available_via_sonarr OR available_via_radarr) STORED,
            available_checked_at  TEXT,
            runtime_minutes       INTEGER,
            state                 TEXT NOT NULL DEFAULT 'unwatched' CHECK (state IN ('unwatched', 'watched', 'skipped')),
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            UNIQUE (show_id, season, episode)
        )
    """)
    op.execute("CREATE INDEX ix_episode_show_id ON episode (show_id)")
    op.execute("CREATE INDEX ix_episode_air_date_utc ON episode (air_date_utc)")
    op.execute("CREATE INDEX ix_episode_state ON episode (state)")

    # -- 5.3 watch_event ---------------------------------------------------
    op.execute(f"""
        CREATE TABLE watch_event (
            id          TEXT PRIMARY KEY CHECK ({_id_check('id', 'w')}),
            show_id     TEXT NOT NULL,
            season      INTEGER NOT NULL,
            episode     INTEGER NOT NULL,
            watched_at  TEXT NOT NULL,
            platform    TEXT,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (show_id, season, episode) REFERENCES episode (show_id, season, episode)
        )
    """)
    op.execute("CREATE INDEX ix_watch_event_show_id ON watch_event (show_id, season, episode)")

    # -- 5.4 show_external_id / show_service_presence -----------------------
    op.execute("""
        CREATE TABLE show_external_id (
            show_id      TEXT NOT NULL REFERENCES show (id),
            service      TEXT NOT NULL,  -- tvdb | anilist | tmdb | imdb | mal | ... (open-ended)
            external_id  TEXT NOT NULL,
            url          TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            PRIMARY KEY (show_id, service)
        )
    """)

    op.execute(f"""
        CREATE TABLE show_service_presence (
            id          TEXT PRIMARY KEY CHECK ({_id_check('id', 'a')}),
            show_id     TEXT NOT NULL REFERENCES show (id),
            service     TEXT NOT NULL,  -- sonarr | radarr | anilist | mal | local | ... (open-ended)
            present     INTEGER NOT NULL CHECK (present IN (0, 1)),
            checked_at  TEXT NOT NULL,
            UNIQUE (show_id, service)
        )
    """)

    # -- 5.9 (part 1) show_relation — gap-filled, see module docstring ------
    # Placed before 5.5 despite the document order because show_id_mapping
    # doesn't depend on it; kept here so every *_id_mapping/*_change table
    # below reads in one unbroken run. No prefix (pure link table, §5.0).
    op.execute("""
        CREATE TABLE show_relation (
            show_id          TEXT NOT NULL REFERENCES show (id),
            related_show_id  TEXT NOT NULL REFERENCES show (id),
            created_at       TEXT NOT NULL,
            PRIMARY KEY (show_id, related_show_id)
        )
    """)

    # -- 5.5 id-mapper / reconciliation tables -------------------------------
    op.execute(f"""
        CREATE TABLE show_id_mapping (
            id                   TEXT PRIMARY KEY CHECK ({_id_check('id', 'x')}),
            show_id              TEXT NOT NULL UNIQUE REFERENCES show (id),
            tvdb_id              INTEGER,
            anilist_id           INTEGER,
            source               TEXT NOT NULL CHECK (source IN ('fribb', 'manual', 'unmatched')),
            matched              INTEGER NOT NULL DEFAULT 0 CHECK (matched IN (0, 1)),
            manual_override      INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
            last_reconciled_at   TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL
        )
    """)

    op.execute(f"""
        CREATE TABLE episode_numbering_mapping (
            id                TEXT PRIMARY KEY CHECK ({_id_check('id', 'n')}),
            show_id           TEXT NOT NULL UNIQUE REFERENCES show (id),
            scheme            TEXT NOT NULL CHECK (scheme IN ('absolute', 'season_episode')),
            source            TEXT NOT NULL CHECK (source IN ('sonarr', 'anilist', 'manual', 'unmatched')),
            matched           INTEGER NOT NULL DEFAULT 0 CHECK (matched IN (0, 1)),
            manual_override   INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
    """)

    # -- 5.6 pending_review ---------------------------------------------------
    op.execute(f"""
        CREATE TABLE pending_review (
            id                     TEXT PRIMARY KEY CHECK ({_id_check('id', 'r')}),
            entity_type             TEXT NOT NULL,
            entity_id               TEXT NOT NULL,
            field                   TEXT NOT NULL,
            previous_value          TEXT,
            proposed_value_chain    TEXT NOT NULL,  -- JSON array
            source                  TEXT NOT NULL,  -- fribb | sonarr | anilist | animeschedule | mal_legacy_import | manual | ... (open-ended)
            created_at              TEXT NOT NULL,
            resolved_at             TEXT,
            resolved_by_client      TEXT CHECK (resolved_by_client IN ('data', 'holodeck', 'captains_log')),
            resolution_note         TEXT
        )
    """)
    op.execute("CREATE INDEX ix_pending_review_entity ON pending_review (entity_type, entity_id)")
    op.execute("CREATE INDEX ix_pending_review_unresolved ON pending_review (resolved_at) WHERE resolved_at IS NULL")

    # -- 5.7 history tables -----------------------------------------------------
    # `changed_by` is the open client/process enum from §5.7 itself
    # (aniq | data | holodeck | captains_log | sonarr_sync | anilist_sync | ...)
    # — plain TEXT, no CHECK, since §5.7 explicitly leaves it open-ended.
    op.execute(f"""
        CREATE TABLE status_change (
            id                TEXT PRIMARY KEY CHECK ({_id_check('id', 'c')}),
            show_id           TEXT NOT NULL REFERENCES show (id),
            previous_status   TEXT CHECK (previous_status IN ('watching', 'planned', 'paused', 'completed', 'dropped')),
            new_status        TEXT NOT NULL CHECK (new_status IN ('watching', 'planned', 'paused', 'completed', 'dropped')),
            changed_at        TEXT NOT NULL,
            changed_by        TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_status_change_show_id ON status_change (show_id)")

    op.execute(f"""
        CREATE TABLE score_change (
            id                TEXT PRIMARY KEY CHECK ({_id_check('id', 'o')}),
            show_id           TEXT NOT NULL REFERENCES show (id),
            previous_score    REAL,
            new_score         REAL NOT NULL,
            changed_at        TEXT NOT NULL,
            changed_by        TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_score_change_show_id ON score_change (show_id)")

    op.execute(f"""
        CREATE TABLE air_date_change (
            id                     TEXT PRIMARY KEY CHECK ({_id_check('id', 'g')}),
            episode_id             TEXT NOT NULL REFERENCES episode (id),
            previous_air_date_utc  TEXT,
            new_air_date_utc       TEXT NOT NULL,
            previous_source        TEXT CHECK (previous_source IN ('sonarr', 'anilist', 'animeschedule', 'manual')),
            new_source             TEXT NOT NULL CHECK (new_source IN ('sonarr', 'anilist', 'animeschedule', 'manual')),
            changed_at             TEXT NOT NULL,
            changed_by             TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_air_date_change_episode_id ON air_date_change (episode_id)")

    op.execute(f"""
        CREATE TABLE tracked_change (
            id                 TEXT PRIMARY KEY CHECK ({_id_check('id', 'k')}),
            show_id            TEXT NOT NULL REFERENCES show (id),
            previous_tracked   INTEGER CHECK (previous_tracked IN (0, 1)),
            new_tracked        INTEGER NOT NULL CHECK (new_tracked IN (0, 1)),
            changed_at         TEXT NOT NULL,
            changed_by         TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_tracked_change_show_id ON tracked_change (show_id)")

    # -- 5.8 person / show_person / studio / show_studio -----------------------
    op.execute(f"""
        CREATE TABLE person (
            id                 TEXT PRIMARY KEY CHECK ({_id_check('id', 'p')}),
            name               TEXT NOT NULL,
            portrait_url       TEXT,
            external_service   TEXT,
            external_id        TEXT,
            external_url       TEXT,
            created_at         TEXT NOT NULL
        )
    """)
    # No composite natural-key PK: a person can hold more than one role/
    # character in the same show (e.g. voicing two characters) — a genuinely
    # many-valued relationship, so this relies on SQLite's implicit rowid
    # rather than forcing an artificial uniqueness constraint on it.
    op.execute("""
        CREATE TABLE show_person (
            show_id          TEXT NOT NULL REFERENCES show (id),
            person_id        TEXT NOT NULL REFERENCES person (id),
            role_type        TEXT NOT NULL CHECK (role_type IN ('voice_actor', 'actor', 'staff')),
            character_name   TEXT
        )
    """)
    op.execute("CREATE INDEX ix_show_person_show_id ON show_person (show_id)")
    op.execute("CREATE INDEX ix_show_person_person_id ON show_person (person_id)")

    op.execute(f"""
        CREATE TABLE studio (
            id                 TEXT PRIMARY KEY CHECK ({_id_check('id', 'd')}),
            name               TEXT NOT NULL,
            external_service   TEXT,
            external_id        TEXT,
            external_url       TEXT,
            created_at         TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE show_studio (
            show_id     TEXT NOT NULL REFERENCES show (id),
            studio_id   TEXT NOT NULL REFERENCES studio (id),
            role_type   TEXT NOT NULL CHECK (role_type IN ('studio', 'publisher', 'network')),
            PRIMARY KEY (show_id, studio_id, role_type)
        )
    """)
    op.execute("CREATE INDEX ix_show_studio_studio_id ON show_studio (studio_id)")

    # -- 5.9 (part 2) franchise / franchise_member -----------------------------
    op.execute(f"""
        CREATE TABLE franchise (
            id    TEXT PRIMARY KEY CHECK ({_id_check('id', 'f')}),
            name  TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE franchise_member (
            franchise_id  TEXT NOT NULL REFERENCES franchise (id),
            show_id       TEXT NOT NULL REFERENCES show (id),
            sort_order    INTEGER NOT NULL,
            PRIMARY KEY (franchise_id, show_id)
        )
    """)
    op.execute("CREATE INDEX ix_franchise_member_show_id ON franchise_member (show_id)")

    # -- 5.1 custom tags: tag / show_tag ---------------------------------------
    op.execute(f"""
        CREATE TABLE tag (
            id          TEXT PRIMARY KEY CHECK ({_id_check('id', 't')}),
            name        TEXT NOT NULL UNIQUE,
            created_at  TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE show_tag (
            show_id  TEXT NOT NULL REFERENCES show (id),
            tag_id   TEXT NOT NULL REFERENCES tag (id),
            PRIMARY KEY (show_id, tag_id)
        )
    """)
    op.execute("CREATE INDEX ix_show_tag_tag_id ON show_tag (tag_id)")

    # -- 5.10 saved filter presets ----------------------------------------------
    # filter_json's exact shape intentionally isn't pinned down further than
    # "serialized filter criteria" — the real shape follows A.2's actual
    # GraphQL filter args, not invented ahead of them.
    op.execute(f"""
        CREATE TABLE filter_preset (
            id           TEXT PRIMARY KEY CHECK ({_id_check('id', 'q')}),
            name         TEXT NOT NULL,
            filter_json  TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    for table in (
        "filter_preset",
        "show_tag",
        "tag",
        "franchise_member",
        "franchise",
        "show_studio",
        "studio",
        "show_person",
        "person",
        "tracked_change",
        "air_date_change",
        "score_change",
        "status_change",
        "pending_review",
        "episode_numbering_mapping",
        "show_id_mapping",
        "show_relation",
        "show_service_presence",
        "show_external_id",
        "watch_event",
        "episode",
        "show",
    ):
        op.execute(f"DROP TABLE {table}")
