"""JSON export/import — SCOPE.md §6.12, BUILD_PLAN.md A.15.

A restore path, not just a one-way backup (§6.12's own framing: the
motivating scenario is rebuilding a fresh LCARS instance). Full
table restore — confirmed directly with the user during A.3
(SCOPE.md §6.12's own resolved note): `ImportResult`'s three reported
counts are a human-facing confirmation summary, not a scope limit: all
real tables are exported/imported regardless.

GraphQL-free by design (raises plain `ValueError`, not `GraphQLError`)
— same layering as `metadata.py`/`fribb.py`/`fuzzy.py`: resolvers.py
owns the GraphQL-specific error wrapping, this module owns the actual
data movement.
"""

import json

SCHEMA_VERSION = 1

# Every real table in the schema (confirmed against a live sqlite_master
# query — 26 tables as of B.14's own show_merge addition (was 25
# through B.3); alembic_version is migration bookkeeping, not
# application data, deliberately excluded. Note: untracked_show_finding
# (B.11e) is also absent from this list — pre-existing, not a B.14
# decision, not investigated here), ordered
# parents-before-children so import's row-by-row INSERTs never hit a
# foreign_keys=ON violation (no ON DELETE/INSERT CASCADE anywhere in
# this schema, §11.2 — same reasoning every other manual cascade this
# project builds already documents). One list, reused for both export
# (order is just readability there) and import (order is load-bearing
# there).
EXPORT_IMPORT_TABLES = [
    # no dependencies
    "show",
    "franchise",
    "tag",
    "person",
    "studio",
    "filter_preset",
    # global, not per-show — added B.3 (§5.2's own B.3 note)
    "availability_poll_checkpoint",
    # depend on show
    "season",
    "season_external_id",  # depends on season (range-based external mapping, 2026-08-26)
    "episode",
    "watch_event",
    "episode_movie_link",
    "show_external_id",
    "show_synonym",
    "show_service_presence",
    "show_relation",
    "episode_numbering_mapping",
    "status_change",
    "score_change",
    "air_date_change",
    "tracked_change",
    "show_merge",  # B.14 — two show FKs (winner/loser), both already listed above
    # depend on show + one other parent
    "show_person",
    "show_studio",
    "franchise_member",
    "show_tag",
    "next_up_override",
    # polymorphic, no real FK (§5.6) — last, so it never blocks anything
    "pending_review",
]


def _real_columns(conn, table: str) -> list[str]:
    """Every stored (non-generated) column — `episode.available_locally`
    (`GENERATED ALWAYS ... STORED`, §5.2) is the one example today, and
    SQLite rejects an explicit value for a generated column outright.
    `table` is always one of this module's own `EXPORT_IMPORT_TABLES`
    constants, never raw input — same non-issue as ids.py/pagination.py's
    equivalent f-string use."""
    return [row[1] for row in conn.execute(f"PRAGMA table_xinfo({table})") if row[6] == 0]


def export_data(conn) -> str:
    """The full JSON export, as a string — `schema_version` plus every
    row of every table in `EXPORT_IMPORT_TABLES`."""
    tables = {}
    for table in EXPORT_IMPORT_TABLES:
        tables[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    return json.dumps({"schema_version": SCHEMA_VERSION, "tables": tables})


def import_data(conn, raw_json: str) -> dict[str, int]:
    """Rejects on any `schema_version` mismatch (§6.12) — no
    auto-migration path. Targets a fresh/empty database (§6.12's own
    "rebuilding a fresh instance" scenario) — no merge/conflict logic;
    a colliding id surfaces as an ordinary `sqlite3.IntegrityError`,
    not something this handles specially. Returns a per-table count
    dict; the resolver narrows this down to `ImportResult`'s own three
    reported fields.
    """
    try:
        payload = json.loads(raw_json)
    except ValueError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc

    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"schema_version mismatch: expected {SCHEMA_VERSION}, got {version!r}")

    tables = payload.get("tables", {})
    counts: dict[str, int] = {}
    try:
        for table in EXPORT_IMPORT_TABLES:
            rows = tables.get(table, [])
            real_columns = _real_columns(conn, table)
            for row in rows:
                columns = [c for c in row if c in real_columns]
                placeholders = ", ".join("?" for _ in columns)
                column_list = ", ".join(columns)
                conn.execute(
                    f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})",
                    [row[c] for c in columns],
                )
            counts[table] = len(rows)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return counts
