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
id is never overwritten by `offer`; `replace` is the one deliberate exception (TheTVDB itself
deleted the id and re-created the show, phase 10).
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
DELETED = "deleted_on_tvdb"  # TheTVDB deleted this id (tvdb_replace.py): never offered again

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
    if not confirmed and is_deleted_id(conn, value):
        # Fribb, anime-lists and TVmaze keep giving an id TheTVDB deleted for a while: writing it
        # would make the stale id come back after it was removed
        logger.info("tvdb guard: %s from %s for %s was deleted on TheTVDB — not offered", value,
                    source, show_id)
        return DELETED
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
        from lcars import tvdb_vetting  # 8.8.3: the facts side by side, fetched once

        facts = tvdb_vetting.evidence(
            conn, tvdb_vetting.show_entry_facts(conn, show_id),
            [(f"TVDB {value} (suggested by {source})", int(value))])
        reviews.open_review(
            conn, "show", show_id, REVIEW_FIELD, source, text,
            [CONFIRM, REJECT, OTHER], {"tvdb_id": value, "source": source, **facts},
            show_id=show_id)
    return REVIEW


def is_deleted_id(conn, tvdb_id) -> bool:
    """Has Sonarr flagged this TVDB id as deleted (a `tvdb_replace` review exists for it)?"""
    return conn.execute(
        "SELECT 1 FROM pending_review WHERE field = 'tvdb_replace' AND"
        " CAST(json_extract(payload, '$.tvdb_id') AS TEXT) = ? LIMIT 1",
        (str(tvdb_id),)).fetchone() is not None


def clear(conn, show_id: str, old_id) -> str:
    """"Log without a TVDB id for now" (phase 10): the deleted id is removed from the show —
    a stale id is worse than none — and Sonarr is left alone. A later hourly pass finds the
    replacement and asks again (R1.14 and the two-source rule apply to it as to any id). Returns
    the sentence the resolved review keeps; the caller commits."""
    cur = conn.execute("DELETE FROM show_external_id WHERE show_id = ? AND service = 'tvdb' AND"
                       " external_id = ?", (show_id, str(old_id)))
    if cur.rowcount:
        return (f"TVDB {old_id} removed from the show; it has no TVDB id for now (Sonarr "
                "unchanged) and will be corrected when a replacement is found")
    return "the show holds no TVDB id (already removed); nothing to change"


def replace(conn, show_id: str, old_id, new_id, source: str) -> None:
    """The one deliberate way a show's TVDB id changes without you typing it on the show page
    (PLAN-CODE phase 10): TheTVDB deleted `old_id` and re-created the show as `new_id`
    (`tvdb_replace.py`; the Sonarr series has already moved). R1.14 and R3.2a still refuse, the
    show must still hold `old_id` (or none — logged without an id —, or already `new_id`), and
    the source is recorded (`you` when you chose it, `recreated:<old id>` when LCARS did on its
    own). Raises `reviews.ReviewError`; the caller commits."""
    old, new = str(old_id), str(new_id)
    if not new.isdigit() or int(new) <= 0:
        raise reviews.ReviewError(f"{new_id!r} is not a TVDB series id")
    current = conn.execute("SELECT external_id FROM show_external_id WHERE show_id = ? AND"
                           " service = 'tvdb'", (show_id,)).fetchone()
    if current is not None and current[0] not in (old, new):
        raise reviews.ReviewError(f"the show's TVDB id is {current[0]} now, not {old} — look at "
                                  "the show page")
    other = conn.execute("SELECT show_id FROM show_external_id WHERE service = 'tvdb' AND"
                         " external_id = ? AND show_id != ?", (new, show_id)).fetchone()
    if other is not None:
        raise reviews.ReviewError(f"TVDB {new} already belongs to show {other[0]} (R1.14)")
    show = conn.execute("SELECT media_shape FROM show WHERE id = ?", (show_id,)).fetchone()
    if show is None or show[0] == "movie":
        raise reviews.ReviewError("a film has no TVDB series id (R3.2a)")
    if current is None:  # logged without an id earlier (`clear`)
        _write(conn, show_id, new, source)
    else:
        conn.execute("UPDATE show_external_id SET external_id = ?, url = ?, source = ? WHERE"
                     " show_id = ? AND service = 'tvdb'", (new, URL.format(new), source, show_id))


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
