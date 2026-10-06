"""PLAN-CODE 8.8.5 — a TVDB link keeps its provenance and is re-checked when Fribb later has data
(user, 2026-10-06).

`show_external_id.source` says where a link came from (you, sonarr, fribb, anime-lists, wikidata,
tvmaze, agreed:<a>+<b>; NULL = recorded before provenance existed). Fribb is the community's
AniList→TVDB list and keeps growing: a link made while Fribb had nothing is compared with it once
it does. The rule is the one the first count used: the stored TVDB id is among the TVDB ids Fribb
gives for ANY of the show's season AniList ids; Fribb silent = nothing to say; the stored id absent
from a non-empty answer = a review `tvdb_recheck` (keep mine / Fribb's is right — I'll correct it
on the show page), once per different answer. A link you confirmed (`source = 'you'`) is never
rechecked; "keep mine" stamps it so.

Nothing is changed here: moving a show's TVDB id also moves its Sonarr series, which is the show
page's "Correct Sonarr link" and yours to do.
"""

from __future__ import annotations

import logging

from lcars import fribb, pending_review, reviews

logger = logging.getLogger(__name__)

FIELD = "tvdb_recheck"
KEEP, FIX = "keep_tvdb", "fix_tvdb"


def _season_anilist_ids(conn, show_id: str) -> list[int]:
    found: set[int] = set()
    for row in conn.execute("SELECT anilist_id FROM season WHERE show_id = ?"
                            " AND anilist_id IS NOT NULL", (show_id,)):
        found.add(int(row[0]))
    for row in conn.execute(
        "SELECT x.external_id FROM season_external_id x JOIN season z ON z.id = x.season_id"
        " WHERE z.show_id = ? AND x.service = 'anilist'", (show_id,)
    ):
        if str(row[0]).isdigit():
            found.add(int(row[0]))
    return sorted(found)


def recheck(conn, dataset=None) -> int:
    """Opens a review for every link Fribb now disagrees with. Returns how many were opened."""
    rows = conn.execute(
        "SELECT x.show_id, x.external_id, x.source FROM show_external_id x JOIN show s ON"
        " s.id = x.show_id WHERE x.service = 'tvdb' AND s.tracked = 1 AND s.media_shape ="
        " 'episodic' AND COALESCE(x.source, '') != 'you'"
    ).fetchall()
    if not rows:
        return 0
    index = fribb.build_anilist_index(dataset if dataset is not None else fribb.load_dataset())
    opened = 0
    for row in rows:
        anilist_ids = _season_anilist_ids(conn, row["show_id"])
        theirs: set[int] = set()
        for a in anilist_ids:
            theirs |= {int(e["tvdb_id"]) for e in index.get(a, [])
                       if str(e.get("tvdb_id", "")).isdigit()}
        if not theirs or int(row["external_id"]) in theirs:
            continue
        text = (f"TVDB {row['external_id']} is stored ({row['source'] or 'source not recorded'});"
                f" Fribb now gives {', '.join(str(t) for t in sorted(theirs))}")
        if pending_review.already_resolved_with(conn, "show", row["show_id"], FIELD, text):
            continue
        open_row = conn.execute(
            "SELECT 1 FROM pending_review WHERE entity_type = 'show' AND entity_id = ? AND field"
            " = ? AND resolved_at IS NULL AND proposed_value_chain LIKE ?",
            (row["show_id"], FIELD, f'%"{text}"%')).fetchone()
        if open_row:
            continue
        reviews.open_review(
            conn, "show", row["show_id"], FIELD, "fribb", text, [KEEP, FIX],
            {"tvdb_id": row["external_id"], "stored_source": row["source"],
             "fribb_ids": sorted(theirs), "anilist_ids": anilist_ids},
            show_id=row["show_id"])
        opened += 1
    if opened:
        logger.info("tvdb recheck: %d link(s) Fribb now disagrees with", opened)
    return opened


def resolve(conn, show_id: str, choice: str) -> None:
    """`keep_tvdb` stamps the link as yours (never rechecked again); `fix_tvdb` only closes the
    review — the id is moved on the show page, where Sonarr moves with it."""
    if choice == KEEP:
        conn.execute("UPDATE show_external_id SET source = 'you' WHERE show_id = ? AND service ="
                     " 'tvdb'", (show_id,))
