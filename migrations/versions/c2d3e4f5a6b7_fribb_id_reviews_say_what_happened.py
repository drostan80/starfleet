"""Open Fribb id reviews say what happened and offer Confirm

2026-10-07 (user). The review a Fribb id change opens (`anilist_id` / `mal_id`, source `fribb`) held
only the new id ("62922") and a Dismiss button. It now says what Fribb did ("Fribb added MAL ID 62922
for this season") and offers Confirm. This rewrites the ones still open the same way; resolved ones are
history and stay as they were. Data only.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-10-07 00:00:02.000000
"""

import json
from collections.abc import Sequence

from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | Sequence[str] | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LABELS = {"anilist_id": "AniList", "mal_id": "MAL"}


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.exec_driver_sql(
        "SELECT r.id, r.field, r.entity_id, r.previous_value, r.proposed_value_chain, z.show_id"
        " FROM pending_review r LEFT JOIN season z ON z.id = r.entity_id"
        " WHERE r.resolved_at IS NULL AND r.source = 'fribb' AND r.entity_type = 'season'"
        " AND r.field IN ('anilist_id', 'mal_id') AND (r.choices IS NULL OR r.choices = '[]')"
    ).fetchall()
    for rid, field, season_id, previous, chain_json, show_id in rows:
        try:
            chain = json.loads(chain_json or "[]")
        except ValueError:
            continue
        new = chain[-1] if chain else None
        if new is None or not str(new).isdigit():
            continue  # not an id: a different kind of finding (the dataset was unreachable...)
        label = _LABELS[field]
        if previous in (None, ""):
            message = f"Fribb added {label} ID {new} for this season"
        else:
            message = f"Fribb changed this season's {label} ID from {previous} to {new}"
        conn.exec_driver_sql(
            "UPDATE pending_review SET proposed_value_chain = ?, choices = ?, payload = ?,"
            " show_id = COALESCE(show_id, ?) WHERE id = ?",
            (json.dumps([message]), json.dumps([{"id": "confirm", "label": "Confirm"}]),
             json.dumps({"season_id": season_id, "field": field, "previous": previous,
                         "new": int(new)}), show_id, rid),
        )


def downgrade() -> None:
    pass
