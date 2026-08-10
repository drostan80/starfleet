"""`episode_movie_link` automatic `tmdb_match` derivation — SCOPE.md
§5.1's movie <-> `bonus_movie` addendum, BUILD_PLAN.md B.8b.

**Added by the 2026-08-09 audit**: §5.1 specifies the full
reconciliation ("internal ids are the source of truth, external ids
map onto them, a best guess applies immediately with a
`pending_review` entry on ambiguity, manual override wins once set"),
but only the `tmdb_match` enum value and A.3's manual `setEpisodeMovie
Link` mutation ever existed — this module is the automatic half that
was never built.

**Candidate pool, confirmed with the user (2026-08-09, B.8b) rather
than assumed**: checked the actual Sonarr API shape first
(`sonarr_client.py`) before designing anything — a season-0 episode
carries no title and no TMDB-comparable id at all (Sonarr is
TVDB-native; LCARS's own `episode` table has no title column either,
confirmed against `schema.graphql`), so literal id- or title-matching
from the episode side is impossible, not merely ambiguous. Candidates
are instead narrowed via `show_relation` (§5.9) — every tracked
`media_shape = 'movie'` show already linked to the `bonus_movie`
episode's *parent* show, either direction (the same graph A.21's own
AniList-relation auto-derivation, `metadata._link_relation`, already
populates independently of this module). Deliberately **not** a broad
fuzzy title match across every tracked movie show
(`service_presence.py`'s own approach, B.7): that field is
suggestion-only (a false positive just means declining a slightly-
wrong offered presence flag); `episode_movie_link` auto-applies a
real, one-to-one identity write, the opposite risk profile. Exactly
one relation-graph candidate -> apply immediately. Zero -> `unmatched`
(a real, first-seen outcome, not an error — self-healing if a relation
edge appears later). More than one -> **flag, don't guess**
(`pending_review`), same "simply flagging those which aren't clear"
philosophy the user gave animeschedule.net's own matcher (B.5).

**`bonus_movie` promotion is explicitly NOT this module's job** —
BUILD_PLAN.md's own B.8b entry left this as an open question ("via
`setEpisodeKind` (A.25) or its own automatic path"), resolved by
checking A.25's own text rather than assumed either way: "Sonarr
cannot distinguish `special` from `ova` or `bonus_movie` at all... so
automatic classification honestly stops there" (SCOPE.md §5.2). With
no episode-level title or any other signal to classify on, there is
no automatic path to build — `setEpisodeKind` (A.25, manual) stays the
only route a `bonus_movie` row is ever created through. This module
only processes rows that already carry that kind.

**Availability sync, the other real gap `availability.py`'s own B.3
docstring names**: "A Radarr event... updating a linked `bonus_movie`-
kind episode (via `episode_movie_link`) is deliberately NOT
[availability.py's] job... this module only populates what B.8b will
need." Radarr's history poll only ever touches a standalone movie
`show` row's own `available_via_radarr`/`file_path_radarr` (§5.2); for
a matched link (whether `tmdb_match` or `manual` — the episode doesn't
care how the identity was established, only that it *is*), this
module mirrors those two fields from the linked movie show onto the
`bonus_movie` episode's own matching columns. Same "only write on a
real change" idempotency B.7's `_upsert_presence` had to learn the
hard way (module note there) — applied here from the start rather
than re-discovered.

**Cadence**: every piece here is a pure internal SQL join/reconcile —
no outbound HTTP call anywhere in this module, so no `service_health`
hook applies (B.6's tracked-service list stays exactly the four real
external services). Same "negligible-cost, no external dependency"
profile as `service_presence.refresh_local_presence` (B.7), so this
rides Ops's existing hourly tick alongside it rather than getting a
new interval — the established "no new interval unless a real
technical constraint forces one" precedent (B.1/B.4/B.5/B.7).
"""

import json
import logging

from lcars import ids, pending_review, util

logger = logging.getLogger("lcars.episode_movie_link")


def reconcile_episode_movie_links(conn) -> dict:
    """The `reconcileEpisodeMovieLinks` mutation's own implementation.
    Returns `{"matched", "flagged", "unmatched", "availability_synced"}`
    — counts of rows actually changed this sweep, same "count real
    changes, not every check" convention every other Phase B poller
    uses."""
    matched = flagged = unmatched = 0
    episodes = conn.execute(
        "SELECT e.id, e.show_id FROM episode e"
        " LEFT JOIN episode_movie_link l ON l.episode_id = e.id"
        " WHERE e.kind = 'bonus_movie' AND (l.id IS NULL OR l.manual_override = 0)"
    ).fetchall()
    for episode in episodes:
        candidates = _candidate_movie_shows(conn, episode["show_id"])
        if len(candidates) == 1:
            if _apply_link(conn, episode["id"], candidates[0]["id"]):
                matched += 1
        elif not candidates:
            if _apply_unmatched(conn, episode["id"]):
                unmatched += 1
        else:
            if _flag_ambiguous(conn, episode["id"], candidates):
                flagged += 1

    availability_synced = _sync_linked_availability(conn)
    conn.commit()
    return {
        "matched": matched,
        "flagged": flagged,
        "unmatched": unmatched,
        "availability_synced": availability_synced,
    }


def _candidate_movie_shows(conn, show_id: str) -> list[dict]:
    """Every tracked `media_shape = 'movie'` show connected to `show_id`
    via `show_relation`, either direction — that table is written
    per-direction independently (`metadata._link_relation`'s own
    docstring), so both sides must be checked."""
    rows = conn.execute(
        "SELECT DISTINCT s.id, s.title_romaji, s.title_english, s.title_native"
        " FROM show s"
        " JOIN show_relation r"
        "   ON (r.show_id = ? AND r.related_show_id = s.id)"
        "   OR (r.related_show_id = ? AND r.show_id = s.id)"
        " WHERE s.tracked = 1 AND s.media_shape = 'movie'",
        (show_id, show_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _apply_link(conn, episode_id: str, movie_show_id: str) -> bool:
    """Returns True if this call actually wrote something (a new row,
    or a real change to an existing automatic one) — an already-correct
    `tmdb_match` link is left untouched entirely."""
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT id, movie_show_id, source FROM episode_movie_link WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    if existing is not None:
        if existing["movie_show_id"] == movie_show_id and existing["source"] == "tmdb_match":
            return False
        conn.execute(
            "UPDATE episode_movie_link SET movie_show_id = ?, source = 'tmdb_match',"
            " matched = 1, updated_at = ? WHERE id = ?",
            (movie_show_id, now, existing["id"]),
        )
        return True
    _insert_link(conn, episode_id, movie_show_id, "tmdb_match", matched=1)
    return True


def _apply_unmatched(conn, episode_id: str) -> bool:
    """Zero relation-graph candidates — a real, first-seen outcome
    (`source = 'unmatched'`), not an error. Left alone once already
    recorded, same "unchanged is not a write" shape as `_apply_link`
    above — self-heals on a later sweep if a relation edge appears."""
    existing = conn.execute(
        "SELECT id, source FROM episode_movie_link WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    if existing is not None:
        if existing["source"] == "unmatched":
            return False
        conn.execute(
            "UPDATE episode_movie_link SET movie_show_id = NULL, source = 'unmatched',"
            " matched = 0, updated_at = ? WHERE id = ?",
            (util.now_utc_iso(), existing["id"]),
        )
        return True
    _insert_link(conn, episode_id, None, "unmatched", matched=0)
    return True


def _insert_link(conn, episode_id: str, movie_show_id: str | None, source: str, matched: int):
    now = util.now_utc_iso()
    conn.execute(
        "INSERT INTO episode_movie_link"
        " (id, episode_id, movie_show_id, source, matched, manual_override,"
        "  created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
        (ids.generate_id(conn, "m"), episode_id, movie_show_id, source, matched, now, now),
    )


def _flag_ambiguous(conn, episode_id: str, candidates: list[dict]) -> bool:
    """More than one relation-graph candidate — genuinely unclear which
    film this episode is. Same chain-dedup guard `animeschedule.py`'s
    own `_flag` established (B.5): a repeat sweep finding the identical
    candidate set is not a new finding, so it's a no-op rather than
    appending a duplicate chain entry — kept local to this module
    rather than added to shared `pending_review.py`, same reasoning
    B.5's own note gives (avoid touching already-verified shared
    semantics for a narrow, module-specific guard)."""
    titles = sorted(_display_title(c) for c in candidates)
    message = (
        f"{len(candidates)} candidate movie show(s) related to this episode's parent show "
        f"via show_relation: {', '.join(titles)} — needs a human to confirm which"
    )
    if _last_chain_entry(conn, "episode", episode_id, "episode_movie_link") == message:
        return False
    pending_review.open_or_extend(
        conn, "episode", episode_id, "episode_movie_link", "tmdb_match", None, message
    )
    return True


def _display_title(show_row: dict) -> str:
    return (
        show_row["title_romaji"]
        or show_row["title_english"]
        or show_row["title_native"]
        or show_row["id"]
    )


def _last_chain_entry(conn, entity_type: str, entity_id: str, field: str) -> str | None:
    row = conn.execute(
        "SELECT proposed_value_chain FROM pending_review"
        " WHERE entity_type = ? AND entity_id = ? AND field = ? AND resolved_at IS NULL",
        (entity_type, entity_id, field),
    ).fetchone()
    if row is None:
        return None
    chain = json.loads(row["proposed_value_chain"])
    return chain[-1] if chain else None


def _sync_linked_availability(conn) -> int:
    """Mirrors a linked movie show's own `available_via_radarr`/
    `file_path_radarr` onto its matched `bonus_movie` episode — the
    gap `availability.py`'s own B.3 docstring explicitly names as
    B.8b's job, not its own. Runs for every link with a resolved
    `movie_show_id`, `tmdb_match` or `manual` alike — the episode's own
    availability doesn't depend on how the identity was established.
    `e.kind = 'bonus_movie'`/`s.media_shape = 'movie'` are both
    guarded explicitly here rather than trusted from the link row: the
    automatic derivation above only ever creates rows that satisfy
    both, but `setEpisodeMovieLink` (A.3, manual) has no such guard on
    either side, and `available_via_radarr` feeds a *generated*
    `available_locally` column (§5.2) — writing it onto a `regular`/
    `special` episode, or reading it off a non-movie show, would be a
    real, silent wrong-availability bug, not just a meaningless no-op."""
    rows = conn.execute(
        "SELECT l.episode_id, e.available_via_radarr AS ep_avail,"
        "       e.file_path_radarr AS ep_path,"
        "       s.available_via_radarr AS show_avail, s.file_path_radarr AS show_path"
        " FROM episode_movie_link l"
        " JOIN episode e ON e.id = l.episode_id"
        " JOIN show s ON s.id = l.movie_show_id"
        " WHERE l.movie_show_id IS NOT NULL"
        "   AND e.kind = 'bonus_movie' AND s.media_shape = 'movie'"
    ).fetchall()
    synced = 0
    now = util.now_utc_iso()
    for row in rows:
        if row["ep_avail"] == row["show_avail"] and row["ep_path"] == row["show_path"]:
            continue
        conn.execute(
            "UPDATE episode SET available_via_radarr = ?, file_path_radarr = ?,"
            " available_checked_at = ?, updated_at = ? WHERE id = ?",
            (row["show_avail"], row["show_path"], now, now, row["episode_id"]),
        )
        synced += 1
    return synced
