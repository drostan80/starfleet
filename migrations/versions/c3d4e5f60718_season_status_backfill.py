"""season status backfill — derive per-season status from episode state

Step 2.1c: now that show.status is derived from season statuses, every
season needs a meaningful status value.  The initial migration
(b2c3d4e5f607) copied show.status onto every season, but that doesn't
reflect reality for multi-season shows where earlier seasons are fully
watched and the current season is in progress.

This migration:
1. Sets season.status = 'completed' for any season where all episodes
   are watched/skipped and no episode is still airing (future air_date
   or NULL air_date).
2. Sets season.status = 'watching' for seasons that have at least one
   watched/skipped episode but aren't fully done.
3. Leaves remaining seasons (no watched episodes) with their current
   status (typically 'planned' from the initial backfill).
4. Recomputes show.status from the derived rule: all episodes done +
   not airing = completed; else highest season's status, with PLANNED
   + len>1 → watching.

No AniList/MAL push — this is a local-only migration backfill.

Revision ID: c3d4e5f60718
Revises: b2c3d4e5f607
Create Date: 2026-09-02
"""

from alembic import op

revision = "c3d4e5f60718"
down_revision = "b2c3d4e5f607"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Mark seasons as 'completed' where all episodes are watched/skipped,
    #    the season has at least one episode, and no episode is still airing.
    #    A season is "still airing" if any episode has NULL air_date or
    #    air_date in the future.
    conn.execute(
        op.inline_literal(
            """
            UPDATE season SET status = 'completed', updated_at = datetime('now')
            WHERE id IN (
                SELECT s.id FROM season s
                WHERE s.season_number > 0
                AND EXISTS (
                    SELECT 1 FROM episode e
                    WHERE e.show_id = s.show_id AND e.season = s.season_number
                )
                AND NOT EXISTS (
                    SELECT 1 FROM episode e
                    WHERE e.show_id = s.show_id AND e.season = s.season_number
                    AND e.state NOT IN ('watched', 'skipped')
                )
                AND NOT EXISTS (
                    SELECT 1 FROM episode e
                    WHERE e.show_id = s.show_id AND e.season = s.season_number
                    AND (e.air_date_utc IS NULL OR e.air_date_utc > datetime('now'))
                )
            )
            """
        )
    )

    # 2. Mark seasons as 'watching' where they have at least one watched/skipped
    #    episode but aren't fully completed (still have unwatched episodes).
    conn.execute(
        op.inline_literal(
            """
            UPDATE season SET status = 'watching', updated_at = datetime('now')
            WHERE id IN (
                SELECT s.id FROM season s
                WHERE s.season_number > 0
                AND s.status != 'completed'
                AND EXISTS (
                    SELECT 1 FROM episode e
                    WHERE e.show_id = s.show_id AND e.season = s.season_number
                    AND e.state IN ('watched', 'skipped')
                )
                AND EXISTS (
                    SELECT 1 FROM episode e
                    WHERE e.show_id = s.show_id AND e.season = s.season_number
                    AND e.state = 'unwatched'
                )
            )
            """
        )
    )

    # 3. Recompute show.status from season statuses.
    #    Rule: if all episodes watched/skipped + not airing → completed.
    #    Otherwise: highest season's status, with PLANNED + len>1 → watching.
    #
    # 3a. Shows that should be 'completed' (all episodes done, not airing)
    conn.execute(
        op.inline_literal(
            """
            UPDATE show SET status = 'completed', updated_at = datetime('now')
            WHERE id IN (
                SELECT s.show_id FROM season s
                WHERE s.season_number > 0
                GROUP BY s.show_id
                HAVING COUNT(*) > 0
            )
            AND EXISTS (
                SELECT 1 FROM episode e WHERE e.show_id = show.id
            )
            AND NOT EXISTS (
                SELECT 1 FROM episode e
                WHERE e.show_id = show.id AND e.state = 'unwatched'
            )
            AND NOT EXISTS (
                SELECT 1 FROM episode e
                WHERE e.show_id = show.id
                AND (e.air_date_utc IS NULL OR e.air_date_utc > datetime('now'))
            )
            AND show.status != 'completed'
            """
        )
    )

    # 3b. Shows where highest season is PLANNED and len > 1 → watching
    conn.execute(
        op.inline_literal(
            """
            UPDATE show SET status = 'watching', updated_at = datetime('now')
            WHERE show.status != 'completed'
            AND show.status != 'watching'
            AND id IN (
                SELECT s1.show_id FROM season s1
                WHERE s1.season_number > 0
                AND s1.season_number = (
                    SELECT MAX(s2.season_number) FROM season s2
                    WHERE s2.show_id = s1.show_id AND s2.season_number > 0
                )
                AND COALESCE(s1.status, 'planned') = 'planned'
                AND (
                    SELECT COUNT(*) FROM season s3
                    WHERE s3.show_id = s1.show_id AND s3.season_number > 0
                ) > 1
            )
            """
        )
    )

    # 3c. Shows where highest season has a non-planned status that differs
    #     from current show.status (and show isn't completed from 3a)
    conn.execute(
        op.inline_literal(
            """
            UPDATE show SET status = (
                SELECT COALESCE(s1.status, 'planned') FROM season s1
                WHERE s1.show_id = show.id AND s1.season_number > 0
                ORDER BY s1.season_number DESC LIMIT 1
            ), updated_at = datetime('now')
            WHERE show.status != 'completed'
            AND id IN (
                SELECT s1.show_id FROM season s1
                WHERE s1.season_number > 0
                AND s1.season_number = (
                    SELECT MAX(s2.season_number) FROM season s2
                    WHERE s2.show_id = s1.show_id AND s2.season_number > 0
                )
                AND COALESCE(s1.status, 'planned') != 'planned'
                AND COALESCE(s1.status, 'planned') != show.status
            )
            """
        )
    )


def downgrade() -> None:
    # Revert season statuses back to matching show status
    op.execute(
        "UPDATE season SET status = ("
        "  SELECT show.status FROM show WHERE show.id = season.show_id"
        ")"
    )
