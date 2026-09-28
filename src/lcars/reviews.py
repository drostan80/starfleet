"""Actionable reviews (PLAN-CODE phase 5) — RULEBOOK R4.8b.

Every review offers the choices that resolve it and names the show page it's
about. `open_review` opens (or extends) one with its choices and the data the
choice needs; `resolve_choice` applies the option you picked and closes it.

Kinds with choices:
- `add_check:needs_user` — add to the proposed show (a cour of the season it
  follows) / make it an individual season until TVDB has it / don't add;
- `same_tvdb_show` — merge the shows (R1.14) / they aren't the same show (your
  note says which TVDB id is wrong).
"""

from __future__ import annotations

import json

from lcars import pending_review, util

LABELS = {
    "add_to_show": "Add to the proposed show",
    "individual": "Individual season until TVDB has it",
    "dont_add": "Don't add",
    "merge": "Merge into one show",
    "not_same": "Not the same show",
}


class ReviewError(ValueError):
    """A choice that doesn't fit the review."""


def open_review(
    conn, entity_type: str, entity_id: str, field: str, source: str, value: str,
    choices: list[str], payload: dict, show_id: str | None = None,
) -> None:
    pending_review.open_or_extend(conn, entity_type, entity_id, field, source, None, value)
    conn.execute(
        "UPDATE pending_review SET choices = ?, payload = ?, show_id = ?"
        " WHERE entity_type = ? AND entity_id = ? AND field = ? AND resolved_at IS NULL",
        (json.dumps([{"id": c, "label": LABELS[c]} for c in choices]), json.dumps(payload),
         show_id, entity_type, entity_id, field),
    )


def resolve_choice(conn, review_id: str, choice: str, client: str, note: str | None) -> None:
    from lcars import add_check, consolidation, fribb

    row = conn.execute("SELECT * FROM pending_review WHERE id = ?", (review_id,)).fetchone()
    if row is None:
        raise ReviewError(f"no such review: {review_id}")
    if row["resolved_at"] is not None:
        raise ReviewError("this review is already resolved")
    offered = [c["id"] for c in json.loads(row["choices"] or "[]")]
    if choice not in offered:
        raise ReviewError(f"{choice!r} isn't one of this review's choices {offered}")
    payload = json.loads(row["payload"] or "{}")
    field = row["field"]

    if field == "add_check:needs_user":
        candidate = add_check.Candidate(
            add_check.USER, anilist_id=payload.get("anilist_id"), mal_id=payload.get("mal_id"),
            titles=payload.get("titles") or [], media_type=payload.get("media_type"),
        )
        if choice == "add_to_show":
            prequel = conn.execute(
                "SELECT * FROM season WHERE id = ?", (payload["season_id"],)
            ).fetchone()
            tvdb_season_id = prequel["parent_id"] if prequel["kind"] == "part" else prequel["id"]
            tvdb_season = conn.execute(
                "SELECT season_number FROM season WHERE id = ?", (tvdb_season_id,)
            ).fetchone()
            add_check.apply_decision(conn, add_check.Decision(
                "part", payload["show_id"], tvdb_season_id, None, tvdb_season[0],
            ), candidate)
        elif choice == "individual":
            add_check.create_individual_season(conn, candidate, payload.get("status"))
    elif field == "same_tvdb_show" and choice == "merge":
        result = consolidation.apply_group(conn, payload["tvdb_id"], fribb.load_dataset())
        if not result.get("merged"):
            raise ReviewError(f"couldn't merge: {result.get('reason')}")

    conn.execute(
        "UPDATE pending_review SET resolved_at = ?, resolved_by_client = ?, resolution_note = ?"
        " WHERE id = ?",
        (util.now_utc_iso(), client, f"{LABELS[choice]}" + (f" — {note}" if note else ""),
         review_id),
    )
    conn.commit()
