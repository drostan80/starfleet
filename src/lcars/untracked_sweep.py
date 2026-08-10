"""Ongoing untracked-show sweep — SCOPE.md §5.2's own "Resolved
2026-08-10 (B.11 reconnaissance)" note, BUILD_PLAN.md B.11e.

B.11d's `previewShowBackfill` already computes exactly the right list
(`show_backfill.preview_backfill()`: every Sonarr/Radarr catalog item
LCARS doesn't track, plus every AniList-list entry not already
accounted for by either) — but only as a one-shot Query result, gone
the moment the caller stops looking at it. Once B.11f switches Data's
calendar over to LCARS-tracked shows only, a show added directly in
Sonarr or AniList after B.11d's one-time backfill would otherwise
silently never surface again. This module runs that same computation
on Ops's own schedule and persists the result to `untracked_show_finding`
so a human can review it at their own convenience — not just at the
instant a manual call happens to run.

**Still never auto-`addShow`s** — that decision (§5.1) is untouched;
only how findings surface changes. **Deliberately minimal, no
resolve/dismiss mutation** unlike `pending_review` — a finding is only
ever removed by no longer appearing in a fresh sweep (the show got
tracked by some other path, or genuinely disappeared from its source),
matching "small new reviewable list" rather than a second
review-and-resolve workflow. Easy to add a dismiss action later if it
turns out to be needed; not built speculatively now.

Upserts by the natural `(service, external_id)` key, same shape
`show_service_presence` already established (a prefixed id *and* a
natural-key UNIQUE constraint side by side) — `first_seen_at` only ever
set once, `last_seen_at`/`title`/`path`/`tracking_space`/`media_shape`
refreshed every sweep a finding is still current. Anything no longer in
the fresh sweep's own result is deleted outright, not soft-marked —
there is no "resolved" state to preserve here, unlike `pending_review`.
"""

from lcars import ids, show_backfill, util


def sweep_untracked_shows(conn) -> dict:
    """The real run (`pollUntrackedShows`) — recomputes
    `show_backfill.preview_backfill()` and reconciles it against
    `untracked_show_finding`. Returns `{"found", "new_findings",
    "resolved_findings"}`, the same "count real changes, for the caller
    to log" convention every other `ops` sweep already returns."""
    now = util.now_utc_iso()
    current = show_backfill.preview_backfill(conn)
    current_keys = {(item["service"], str(item["external_id"])) for item in current}

    existing_keys = {
        (row["service"], row["external_id"])
        for row in conn.execute(
            "SELECT service, external_id FROM untracked_show_finding"
        ).fetchall()
    }

    new_findings = 0
    for item in current:
        key = (item["service"], str(item["external_id"]))
        if key in existing_keys:
            conn.execute(
                "UPDATE untracked_show_finding SET"
                "  title = ?, path = ?, tracking_space = ?, media_shape = ?,"
                "  last_seen_at = ?, updated_at = ?"
                " WHERE service = ? AND external_id = ?",
                (
                    item["title"],
                    item["path"],
                    item["tracking_space"],
                    item["media_shape"],
                    now,
                    now,
                    key[0],
                    key[1],
                ),
            )
        else:
            conn.execute(
                "INSERT INTO untracked_show_finding"
                " (id, service, external_id, title, path, tracking_space, media_shape,"
                "  first_seen_at, last_seen_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ids.generate_id(conn, "u"),
                    key[0],
                    key[1],
                    item["title"],
                    item["path"],
                    item["tracking_space"],
                    item["media_shape"],
                    now,
                    now,
                    now,
                    now,
                ),
            )
            new_findings += 1

    resolved_keys = existing_keys - current_keys
    resolved_findings = 0
    for service, external_id in resolved_keys:
        conn.execute(
            "DELETE FROM untracked_show_finding WHERE service = ? AND external_id = ?",
            (service, external_id),
        )
        resolved_findings += 1

    conn.commit()
    return {
        "found": len(current),
        "new_findings": new_findings,
        "resolved_findings": resolved_findings,
    }
