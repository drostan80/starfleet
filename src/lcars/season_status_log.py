"""Season status history (PLAN-CODE 0.2, 2026-09-27).

Every write of `season.status` records a `season_status_change` row with who
made it, so the user's own season-level changes can be told apart from
automation's (after 09-06 they couldn't: nothing was logged).
"""

from lcars import ids, util


def set_status(conn, season_id: str, new_status, changed_by: str) -> bool:
    """Write `season.status` and log it. No-op (no row, no log) when the
    status is unchanged. Returns whether it changed."""
    row = conn.execute(
        "SELECT show_id, status FROM season WHERE id = ?", (season_id,)
    ).fetchone()
    if row is None or row["status"] == new_status:
        return False
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE season SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, season_id),
    )
    record(conn, season_id, row["show_id"], row["status"], new_status, changed_by, now)
    return True


def record(
    conn, season_id: str, show_id: str, previous, new, changed_by: str, changed_at=None
) -> None:
    """Log one change that the caller has already written."""
    if previous == new:
        return
    conn.execute(
        "INSERT INTO season_status_change"
        " (id, season_id, show_id, previous_status, new_status, changed_at, changed_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ids.generate_id(conn, "j"), season_id, show_id, previous, new,
         changed_at or util.now_utc_iso(), changed_by),
    )
