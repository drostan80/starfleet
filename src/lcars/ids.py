"""Short, type-prefixed, human-readable ids — SCOPE.md §5.0/§11.4.

Format: `{prefix}-{6-char random string}`. Generation always checks the
random portion against the target table's own `id` column (a UNIQUE
index — the migration's PRIMARY KEY) and regenerates on the rare
collision, per §5.0.
"""

import sqlite3

import nanoid

# Crockford-style 32-symbol alphabet: 0-9 and a-z minus i, l, o, u — the
# characters most easily confused with each other or with digits (§5.0).
# Lowercase, matching §5.0's own examples ("s-a3f9k2").
ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
assert len(ALPHABET) == 32

RANDOM_LENGTH = 6

# One letter per top-level entity — the full prefix table, SCOPE.md §5.0.
# Pure join/link tables (show_external_id, show_person, show_studio,
# franchise_member, show_tag) aren't here: no prefix spent on them, always
# addressed through their parent (§5.0).
PREFIX_TABLES = {
    "s": "show",
    "e": "episode",
    "w": "watch_event",
    "p": "person",
    "d": "studio",
    "f": "franchise",
    "t": "tag",
    "r": "pending_review",
    # "x" (show_id_mapping) retired 2026-08-08 (A.4) — replaced by "z" (season)
    # below; not reassigned, see SCOPE.md §5.0.
    "n": "episode_numbering_mapping",
    "a": "show_service_presence",
    "q": "filter_preset",
    "c": "status_change",
    "o": "score_change",
    "g": "air_date_change",
    "k": "tracked_change",
    "m": "episode_movie_link",  # §5.1/§5.9 addendum, 2026-08-08
    "v": "next_up_override",  # §5.9 addendum, 2026-08-08
    "z": "season",  # §5.5, A.4, 2026-08-08 — replaces show_id_mapping ("x")
}

MAX_ATTEMPTS = 10


def generate_id(conn: sqlite3.Connection, prefix: str) -> str:
    """Generate a fresh id for `prefix`, checked against its table.

    At realistic personal-scale row counts the retry path will
    essentially never trigger (§5.0's own framing) — MAX_ATTEMPTS is a
    safety bound, not an expected code path.
    """
    if prefix not in PREFIX_TABLES:
        raise ValueError(f"unknown id prefix {prefix!r} — not in ids.PREFIX_TABLES")
    table = PREFIX_TABLES[prefix]
    for _ in range(MAX_ATTEMPTS):
        candidate = f"{prefix}-{nanoid.generate(alphabet=ALPHABET, size=RANDOM_LENGTH)}"
        # table is looked up from PREFIX_TABLES above, never caller input — not
        # a SQL-injection vector despite the f-string.
        row = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (candidate,)).fetchone()
        if row is None:
            return candidate
    raise RuntimeError(
        f"could not generate a unique id for prefix {prefix!r} after {MAX_ATTEMPTS} attempts"
    )
