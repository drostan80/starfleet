"""The shared pending_review write path — SCOPE.md §5.6/§3 principle 1.

Extracted out of resolvers.py 2026-08-08 (A.8) — originally built there
as part of A.4's `reconcileSeasonMapping`, but A.8's own best-effort
metadata-fetch failure logging (metadata.py) needs the exact same
value-chain-accumulation behavior, and a resolvers.py<->metadata.py
circular import isn't worth introducing just to share one function.
"""

import json

from lcars import ids, util


def open_or_extend(
    conn, entity_type: str, entity_id: str, field: str, source: str, previous_value, new_value
) -> None:
    """§5.6/§3 principle 1: value-chain accumulation on repeated
    automatic changes to the same field before a human resolves the
    entry, rather than opening a duplicate entry or silently
    overwriting. `previousValue` is nullable (schema-legal — "no value
    before this chain opened"); every entry actually IN the chain must
    be a real string (`[String!]!`, non-null elements), so a `None`
    `new_value` (e.g. A.4's "no candidate found" case) is represented
    as the literal string "unmatched" rather than a null list element —
    caught by a real GraphQL null-in-non-null-list error while writing
    A.4's own tests, not a hypothetical. `new_value` doesn't have to be
    a data value at all — A.8's best-effort fetch-failure logging
    passes an error message string here, same shape, same reasoning
    ("later human awareness" applies just as much to "this kept
    failing" as to "this kept disagreeing").
    """
    previous_str = None if previous_value is None else str(previous_value)
    new_str = "unmatched" if new_value is None else str(new_value)
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT id, proposed_value_chain FROM pending_review"
        " WHERE entity_type = ? AND entity_id = ? AND field = ? AND resolved_at IS NULL",
        (entity_type, entity_id, field),
    ).fetchone()
    if existing is not None:
        chain = json.loads(existing["proposed_value_chain"])
        chain.append(new_str)
        conn.execute(
            "UPDATE pending_review SET proposed_value_chain = ? WHERE id = ?",
            (json.dumps(chain), existing["id"]),
        )
        return
    review_id = ids.generate_id(conn, "r")
    conn.execute(
        "INSERT INTO pending_review"
        " (id, entity_type, entity_id, field, previous_value, proposed_value_chain,"
        "  source, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            review_id,
            entity_type,
            entity_id,
            field,
            previous_str,
            json.dumps([new_str]),
            source,
            now,
        ),
    )


def already_resolved_with(conn, entity_type: str, entity_id: str, field: str, value) -> bool:
    """2026-08-12 — for a write path whose "value" is a derived message
    rather than a stored column (metadata.py's own season-split guard,
    `_reconcile_air_dates`), so `open_or_extend`'s own "same value as
    what's already stored" comparisons (`reconcile_season()`'s
    `existing["anilist_id"] != anilist_id`) have nothing to compare
    against. Without this, a resolved review for a persistently-true
    condition (the underlying counts never change) reopens on every
    single pass — confirmed live as a real bug: Tonbo!/Chitose's
    season-split reviews would have regenerated within a day of being
    resolved through Data's own review screen (B.18), pure busywork.
    True only when the *most recently resolved* entry for this
    (entity_type, entity_id, field) proposed exactly `value` as its
    last chain entry — a genuinely different value (the mismatch
    itself changed) is not treated as already-resolved, so a real new
    disagreement still surfaces normally."""
    new_str = "unmatched" if value is None else str(value)
    row = conn.execute(
        "SELECT proposed_value_chain FROM pending_review"
        " WHERE entity_type = ? AND entity_id = ? AND field = ? AND resolved_at IS NOT NULL"
        " ORDER BY resolved_at DESC LIMIT 1",
        (entity_type, entity_id, field),
    ).fetchone()
    if row is None:
        return False
    chain = json.loads(row["proposed_value_chain"])
    return bool(chain) and chain[-1] == new_str
