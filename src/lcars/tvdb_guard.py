"""R1.14a — the one gate every automatic TVDB id goes through (user, 2026-09-30).

Enforced, not only checked:
- **R1.14** one show per TVDB id: an id another show already holds is never written to a
  second show. (Not a database constraint: the rebuild and the merge tools work on states
  where two shows share one — the 09-06 base had 126.)
- **R3.2a** a film show never carries a TVDB *series* id (its link is `tvdb_movie`);
- **R3.7 / R3.7e** an id from a single source (Fribb, anime-lists, Wikidata, TVmaze) is not
  written: it becomes a review — yes / no / no, here is the right one. You are then the
  second, independent source.

What a caller that already has two agreeing sources, or your own word, passes is
`confirmed=True`: the R1.14 and R3.2a refusals still apply. A show that already has a TVDB
id is never overwritten here.
"""

from __future__ import annotations

import logging

from lcars import pending_review, reviews, util

logger = logging.getLogger(__name__)

WRITTEN = "written"
EXISTS = "exists"  # the show already has one; never overwritten
HELD = "held_by_other_show"  # R1.14
FILM = "film"  # R3.2a
REVIEW = "review"  # R3.7e
INVALID = "invalid"

REVIEW_FIELD = "tvdb_candidate"
CONFIRM, REJECT, OTHER = "link_tvdb", "reject_tvdb", "link_other_tvdb"
URL = "https://thetvdb.com/dereferrer/series/{}"


def offer(conn, show_id: str, tvdb_id, source: str, *, confirmed: bool = False) -> str:
    """A TVDB series id found for `show_id` by `source`. Returns what happened (see the
    module constants); writes only when every rule allows it."""
    value = str(tvdb_id).strip() if tvdb_id is not None else ""
    if not value.isdigit() or int(value) <= 0:
        return INVALID
    if conn.execute("SELECT 1 FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
                    (show_id,)).fetchone():
        return EXISTS
    other = conn.execute(
        "SELECT show_id FROM show_external_id WHERE service = 'tvdb' AND external_id = ?"
        " AND show_id != ?", (value, show_id)).fetchone()
    if other is not None:
        logger.info("tvdb guard: %s from %s for %s is held by %s (R1.14)", value, source,
                    show_id, other[0])
        return HELD
    show = conn.execute("SELECT media_shape FROM show WHERE id = ?", (show_id,)).fetchone()
    if show is None:
        return INVALID
    if show[0] == "movie":
        logger.info("tvdb guard: series id %s from %s refused for film %s (R3.2a)", value,
                    source, show_id)
        return FILM
    if not confirmed:
        text = f"TVDB {value} suggested by {source} only — is it this show?"
        # answered once (yes / no / another id), never asked again for the same id
        if not pending_review.already_resolved_with(conn, "show", show_id, REVIEW_FIELD, text):
            reviews.open_review(
                conn, "show", show_id, REVIEW_FIELD, source, text,
                [CONFIRM, REJECT, OTHER], {"tvdb_id": value, "source": source},
                show_id=show_id)
        return REVIEW
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'tvdb', ?, ?, ?)", (show_id, value, URL.format(value), util.now_utc_iso()))
    return WRITTEN


def resolve(conn, show_id: str, payload: dict, choice: str, note: str | None) -> None:
    """A `tvdb_candidate` review answered (R3.7e). Raises `reviews.ReviewError` (the
    review stays open) when the id can't be written as the rules stand."""
    import re

    if choice == REJECT:
        return
    if choice == CONFIRM:
        value = payload["tvdb_id"]
    else:
        m = re.search(r"\d+", note or "")
        if not m:
            raise reviews.ReviewError("put the right TVDB id in the note")
        value = m.group(0)
    outcome = offer(conn, show_id, value, "you", confirmed=True)
    if outcome not in (WRITTEN, EXISTS):
        raise reviews.ReviewError(f"TVDB {value} not linked: {outcome.replace('_', ' ')}")
