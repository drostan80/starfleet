"""R1.14a — the one gate every automatic TVDB id goes through (user, 2026-09-30).

Enforced, not only checked:
- **R1.14** one show per TVDB id: an id another show already holds is never written to a
  second show. (Not a database constraint: the rebuild and the merge tools work on states
  where two shows share one — the 09-06 base had 126.)
- **R3.2a** a film show never carries a TVDB *series* id (its link is `tvdb_movie`);
- **R3.7 / R3.7e** an id from a single source (Fribb, anime-lists, Wikidata, TVmaze) is not
  written: it becomes a review — yes / no / no, here is the right one. You are then the
  second, independent source.
- **R3.7c** two independent sources agreeing on the id attach it (user, 2026-10-06): every offer
  is remembered (`tvdb_offer`); Fribb and anime-lists count as ONE source (Fribb is built partly
  from anime-lists); the id is written when two sources agree, no source has offered another id for
  the show, and the TVDB title (Sonarr's lookup) matches the show's — otherwise it stays a review.
- Every written id records where it came from (`show_external_id.source`, 8.8.5).

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

# Fribb is built partly from anime-lists: the two do not confirm each other (user, 2026-10-06)
INDEPENDENCE = {"fribb": "community", "anime-lists": "community"}

REVIEW_FIELD = "tvdb_candidate"
CONFIRM, REJECT, OTHER = "link_tvdb", "reject_tvdb", "link_other_tvdb"
URL = "https://thetvdb.com/dereferrer/series/{}"


def _record_offer(conn, show_id: str, value: str, source: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO tvdb_offer (show_id, tvdb_id, source, created_at)"
        " VALUES (?, ?, ?, ?)", (show_id, value, source, util.now_utc_iso()))


def _agreement(conn, show_id: str, value: str) -> list[str] | None:
    """The independent source groups that offered `value` for this show — None unless at least
    two did and no source offered another id (any disagreement goes to you, R3.7)."""
    rows = conn.execute("SELECT tvdb_id, source FROM tvdb_offer WHERE show_id = ?",
                        (show_id,)).fetchall()
    if any(r["tvdb_id"] != value for r in rows):
        return None
    groups = sorted({INDEPENDENCE.get(r["source"], r["source"]) for r in rows})
    return groups if len(groups) >= 2 else None


def _title_fits(conn, show_id: str, value: str) -> bool:
    """R3.7: the TVDB name (Sonarr's lookup) matches the show's titles; unknown = no."""
    from lcars import add_check, tvdb_vetting

    facts = tvdb_vetting.tvdb_facts(conn, int(value))
    show = conn.execute("SELECT * FROM show WHERE id = ?", (show_id,)).fetchone()
    if not facts or show is None:
        return False
    return add_check.titles_match(add_check._show_titles(show), facts.get("titles") or [])


def _write(conn, show_id: str, value: str, source: str) -> None:
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at, source)"
        " VALUES (?, 'tvdb', ?, ?, ?, ?)",
        (show_id, value, URL.format(value), util.now_utc_iso(), source))


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
    if confirmed:
        _write(conn, show_id, value, source)
        return WRITTEN
    _record_offer(conn, show_id, value, source)
    agreed = _agreement(conn, show_id, value)  # R3.7c
    if agreed is not None and _title_fits(conn, show_id, value):
        _write(conn, show_id, value, "agreed:" + "+".join(agreed))
        pending_review.close_obsolete(
            conn, "show", show_id, REVIEW_FIELD,
            f"two independent sources agree on TVDB {value} ({', '.join(agreed)}) — attached")
        return WRITTEN
    text = f"TVDB {value} suggested by {source} only — is it this show?"
    if agreed is not None:
        text = (f"TVDB {value} suggested by {', '.join(agreed)}, but its title doesn't match "
                "this show (or can't be read) — is it this show?")
    # answered once (yes / no / another id), never asked again for the same text
    if not pending_review.already_resolved_with(conn, "show", show_id, REVIEW_FIELD, text):
        reviews.open_review(
            conn, "show", show_id, REVIEW_FIELD, source, text,
            [CONFIRM, REJECT, OTHER], {"tvdb_id": value, "source": source},
            show_id=show_id)
    return REVIEW


def link_by_hand(conn, show_id: str, tvdb_id) -> str:
    """R3.7d: a TVDB id you typed on the show page. You are the confirming source, so it is
    written without a review — but R1.14 (one show per TVDB id) and R3.2a (no series id on a
    film) still refuse it, and an id the show already holds is replaced only when the show is not
    in Sonarr (moving its Sonarr series is the show page's "Correct Sonarr link"). Returns WRITTEN
    or EXISTS (nothing to change); raises `reviews.ReviewError` with the reason otherwise."""
    value = str(tvdb_id).strip()
    if not value.isdigit() or int(value) <= 0:
        raise reviews.ReviewError(f"{tvdb_id!r} is not a TVDB series id")
    current = conn.execute("SELECT external_id FROM show_external_id WHERE show_id = ? AND"
                           " service = 'tvdb'", (show_id,)).fetchone()
    if current is None:
        outcome = offer(conn, show_id, value, "you", confirmed=True)
    elif current[0] == value:
        return EXISTS
    else:
        if conn.execute("SELECT 1 FROM show_external_id WHERE show_id = ? AND service ="
                        " 'sonarr'", (show_id,)).fetchone():
            raise reviews.ReviewError(
                f"this show is in Sonarr under TVDB {current[0]}: use 'Correct Sonarr link' on its "
                "page to change the id (it moves the series in Sonarr too)")
        conn.execute("DELETE FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
                     (show_id,))
        outcome = offer(conn, show_id, value, "you", confirmed=True)
        if outcome != WRITTEN:  # refused: put the old id back
            conn.execute(
                "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                " VALUES (?, 'tvdb', ?, ?, ?)",
                (show_id, current[0], URL.format(current[0]), util.now_utc_iso()))
    if outcome == HELD:
        raise reviews.ReviewError(f"TVDB {value} already belongs to another show (R1.14)")
    if outcome == FILM:
        raise reviews.ReviewError("a film has no TVDB series id (R3.2a)")
    if outcome == INVALID:
        raise reviews.ReviewError(f"{tvdb_id!r} is not a TVDB series id")
    return outcome


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
    outcome = offer(conn, show_id, value, "you", confirmed=True)  # provenance: you
    if outcome not in (WRITTEN, EXISTS):
        raise reviews.ReviewError(f"TVDB {value} not linked: {outcome.replace('_', ' ')}")
