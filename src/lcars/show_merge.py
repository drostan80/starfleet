"""Cross-service show-duplicate detection and merge — SCOPE.md's own
show-merge note, BUILD_PLAN.md B.14. See the B.14 migration's own
docstring (`migrations/versions/fe1f556c14f9_*.py`) for the full
"why this exists, why now, why this shape" narrative — kept there
rather than duplicated here, same "the migration is the historical
record, the module docstring is the current behavior" split every
prior migration/module pair in this codebase already follows.

**Detection** (`find_candidate_pairs`): tracked shows with a `tvdb`
link and no `anilist` link ("losers") vs. tracked `tracking_space =
'anime'` shows with an `anilist` link and no `tvdb` link ("winners") —
exactly the two sets measured live during the B.11f investigation.
Matched via `fuzzy.best_match()` (§5.4, A.7) unchanged, same 0.72
threshold, same "return None rather than guess" shape
`service_presence.py`'s own catalog-matching sweep already reuses
verbatim. A title matching more than one winner (or vice versa) is
ambiguous, not a match — skipped silently, same "no guessing" spirit.

**Merge** (`merge_shows`): the winner keeps its own `show` row fields
(title, status, score, tracking fields) untouched — same "don't
overwrite what's already trustworthy" reasoning `_promote_stub`
(`shows.py`, B.11d) already applies to a stub's title/media_shape/
tracking_space. Every table `export_import.py`'s own
`EXPORT_IMPORT_TABLES` lists as show-scoped gets walked; a genuine
per-slot conflict (both rows already have their own season 1, say)
leaves the loser's copy in place rather than dropping it, recorded in
`manifest["skipped"]`. The loser itself is never deleted, only demoted
(`tracked = 0`) — reversible by construction, not by attempting to
replay history backwards.

`episode.show_id` and `watch_event.show_id` reference the same
(season, episode) pair from two different tables (`watch_event`'s own
FK is the composite `(show_id, season, episode) REFERENCES episode
(show_id, season, episode)`), so moving one without the other in the
same statement would leave a dangling FK target for the instant
between the two UPDATEs — `PRAGMA defer_foreign_keys = ON` (SQLite,
scoped to the current transaction, auto-resets at commit) defers all
FK checks to commit time instead of per-statement, exactly this
scenario's documented use case. No prior module in this codebase
needed this — `export_import.py`'s own bulk import avoids the problem
entirely by inserting parents before children in one direction only;
this is the first genuinely bidirectional-within-one-transaction
reparenting this schema does.

**Reversal** (`reverse_show_merge`): replays `manifest["moved"]`
backwards — every table entry records exactly what to restore (ids for
tables with their own prefixed id, natural-key tuples for pure join
tables with none), not a generic "undo the last N statements" replay.
Flips the loser back to `tracked = 1`. Does not attempt to reconstruct
anything from `manifest["skipped"]` — those rows never moved, there is
nothing to undo for them.
"""

import json
import logging

from lcars import fuzzy, ids, util

logger = logging.getLogger("lcars.show_merge")

# Every table export_import.EXPORT_IMPORT_TABLES lists as depending on
# `show` gets a merge branch below — kept in that same order for easy
# side-by-side comparison against that list, the schema's own
# authoritative "every show-scoped table" inventory. `air_date_change`
# is absent: it references `episode.id`, not `show_id`, so it moves for
# free the moment its parent episode row's `show_id` changes.


def find_candidate_pairs(conn) -> list[tuple[str, str, str]]:
    """Returns `(loser_show_id, winner_show_id, matched_on)` triples —
    live-computed every sweep, never persisted (a merged pair stops
    matching the loser-side query the moment it's demoted, so there's
    no dedup state to maintain between sweeps)."""
    losers = conn.execute(
        """
        SELECT id, title_romaji, title_english, title_native FROM show
        WHERE tracked = 1
          AND EXISTS (SELECT 1 FROM show_external_id WHERE show_id = show.id AND service = 'tvdb')
          AND NOT EXISTS (
              SELECT 1 FROM show_external_id WHERE show_id = show.id AND service = 'anilist'
          )
        """
    ).fetchall()
    winners = conn.execute(
        """
        SELECT id, title_romaji, title_english, title_native FROM show
        WHERE tracked = 1 AND tracking_space = 'anime'
          AND EXISTS (
              SELECT 1 FROM show_external_id WHERE show_id = show.id AND service = 'anilist'
          )
          AND NOT EXISTS (
              SELECT 1 FROM show_external_id WHERE show_id = show.id AND service = 'tvdb'
          )
        """
    ).fetchall()
    if not losers or not winners:
        return []

    def titles(row) -> list[str]:
        return [row[f] for f in ("title_romaji", "title_english", "title_native") if row[f]]

    winner_titles_by_title: dict[str, list[str]] = {}
    all_winner_titles: list[str] = []
    for winner in winners:
        for t in titles(winner):
            winner_titles_by_title.setdefault(t, []).append(winner["id"])
            all_winner_titles.append(t)

    pairs = []
    for loser in losers:
        matched_title = fuzzy.best_match(titles(loser), all_winner_titles)
        if matched_title is None:
            continue
        winner_ids = set(winner_titles_by_title.get(matched_title, []))
        if len(winner_ids) != 1:
            continue  # ambiguous — more than one winner shares this exact title
        winner_id = next(iter(winner_ids))
        pairs.append((loser["id"], winner_id, f"fuzzy title match against {matched_title!r}"))
    return pairs


def merge_shows(conn, winner_id: str, loser_id: str, matched_on: str) -> str:
    """The real operation — see module docstring. Returns the new
    `show_merge` row's id. Raises ValueError (GraphQL-free by design,
    same layering `export_import.py`/`show_backfill.py` already use —
    resolvers.py owns the GraphQLError wrapping) if either id doesn't
    exist or either show isn't currently tracked."""
    winner = conn.execute("SELECT id, tracked FROM show WHERE id = ?", (winner_id,)).fetchone()
    loser = conn.execute("SELECT id, tracked FROM show WHERE id = ?", (loser_id,)).fetchone()
    if winner is None:
        raise ValueError(f"no such show: {winner_id}")
    if loser is None:
        raise ValueError(f"no such show: {loser_id}")
    if winner_id == loser_id:
        raise ValueError("cannot merge a show into itself")
    if not winner["tracked"] or not loser["tracked"]:
        raise ValueError("both shows must be tracked to merge")

    now = util.now_utc_iso()
    moved: dict = {}
    skipped: list[str] = []

    # `defer_foreign_keys` only stays effective for the transaction it's
    # set within — sqlite3's default (deferred/autocommit-between-
    # statements) mode means a bare PRAGMA here, with no transaction
    # explicitly open yet, gets silently reset the moment the *next*
    # statement runs as its own autocommitted one (confirmed empirically:
    # a single interleaved SELECT was enough to reproduce the exact
    # "FOREIGN KEY constraint failed" this whole PRAGMA exists to avoid).
    # BEGIN IMMEDIATE first keeps one real transaction open around every
    # statement below, so the PRAGMA's effect actually lasts until commit.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("PRAGMA defer_foreign_keys = ON")

    # show_external_id — UNIQUE(show_id, service)
    winner_services = {
        r["service"]
        for r in conn.execute(
            "SELECT service FROM show_external_id WHERE show_id = ?", (winner_id,)
        ).fetchall()
    }
    moved_services = []
    for row in conn.execute(
        "SELECT service FROM show_external_id WHERE show_id = ?", (loser_id,)
    ).fetchall():
        if row["service"] in winner_services:
            skipped.append(f"show_external_id[{row['service']}]: winner already has this service")
            continue
        conn.execute(
            "UPDATE show_external_id SET show_id = ? WHERE show_id = ? AND service = ?",
            (winner_id, loser_id, row["service"]),
        )
        moved_services.append(row["service"])
    moved["show_external_id"] = moved_services

    # show_service_presence — PK(show_id, service)
    winner_presence = {
        r["service"]
        for r in conn.execute(
            "SELECT service FROM show_service_presence WHERE show_id = ?", (winner_id,)
        ).fetchall()
    }
    moved_presence = []
    for row in conn.execute(
        "SELECT service FROM show_service_presence WHERE show_id = ?", (loser_id,)
    ).fetchall():
        if row["service"] in winner_presence:
            skipped.append(
                f"show_service_presence[{row['service']}]: winner already has this service"
            )
            continue
        conn.execute(
            "UPDATE show_service_presence SET show_id = ? WHERE show_id = ? AND service = ?",
            (winner_id, loser_id, row["service"]),
        )
        moved_presence.append(row["service"])
    moved["show_service_presence"] = moved_presence

    # season — UNIQUE(show_id, season_number). A conflicting season_number
    # keeps the winner's own season row; any loser episode for that season
    # gets repointed onto it below rather than left dangling at its old
    # (now-orphaned-on-the-loser) season_id.
    winner_seasons = {
        r["season_number"]: r["id"]
        for r in conn.execute(
            "SELECT id, season_number FROM season WHERE show_id = ?", (winner_id,)
        ).fetchall()
    }
    moved_seasons = []
    season_repoint: dict[int, str] = {}
    for row in conn.execute(
        "SELECT id, season_number FROM season WHERE show_id = ?", (loser_id,)
    ).fetchall():
        if row["season_number"] in winner_seasons:
            skipped.append(
                f"season[{row['season_number']}]: winner already has this season,"
                f" left on loser as {row['id']}"
            )
            season_repoint[row["season_number"]] = winner_seasons[row["season_number"]]
            continue
        conn.execute("UPDATE season SET show_id = ? WHERE id = ?", (winner_id, row["id"]))
        moved_seasons.append(row["id"])
    moved["season"] = moved_seasons

    # episode — UNIQUE(show_id, season, episode). A conflicting (season,
    # episode) leaves the loser's own row in place. Every moved episode's
    # original season_id is recorded (even when unchanged) so reversal can
    # restore it exactly without re-deriving the season conflict logic.
    winner_episode_keys = {
        (r["season"], r["episode"])
        for r in conn.execute(
            "SELECT season, episode FROM episode WHERE show_id = ?", (winner_id,)
        ).fetchall()
    }
    moved_episodes = []
    moved_watch_events = []
    for row in conn.execute(
        "SELECT id, season, episode, season_id FROM episode WHERE show_id = ?", (loser_id,)
    ).fetchall():
        key = (row["season"], row["episode"])
        if key in winner_episode_keys:
            skipped.append(
                f"episode[s{row['season']}e{row['episode']}]: winner already has this episode"
            )
            continue
        original_season_id = row["season_id"]
        new_season_id = original_season_id
        if original_season_id is not None and row["season"] in season_repoint:
            new_season_id = season_repoint[row["season"]]
        conn.execute(
            "UPDATE episode SET show_id = ?, season_id = ? WHERE id = ?",
            (winner_id, new_season_id, row["id"]),
        )
        moved_episodes.append([row["id"], original_season_id])
        watch_event_exists = conn.execute(
            "SELECT 1 FROM watch_event WHERE show_id = ? AND season = ? AND episode = ?",
            (loser_id, row["season"], row["episode"]),
        ).fetchone()
        if watch_event_exists is not None:
            conn.execute(
                "UPDATE watch_event SET show_id = ?"
                " WHERE show_id = ? AND season = ? AND episode = ?",
                (winner_id, loser_id, row["season"], row["episode"]),
            )
            moved_watch_events.append([row["season"], row["episode"]])
    moved["episode"] = moved_episodes
    moved["watch_event"] = moved_watch_events

    # episode_movie_link — movie_show_id, no natural per-slot conflict
    # (many episodes can each link to the same bonus-movie show).
    link_ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM episode_movie_link WHERE movie_show_id = ?", (loser_id,)
        ).fetchall()
    ]
    if link_ids:
        conn.execute(
            "UPDATE episode_movie_link SET movie_show_id = ? WHERE movie_show_id = ?",
            (winner_id, loser_id),
        )
    moved["episode_movie_link"] = link_ids

    # show_relation — PK(show_id, related_show_id), both columns -> show.
    # Both directions of the loser's own relations get walked; a relation
    # that would become a self-relation (loser was already directly
    # related to the winner) or would duplicate one the winner already has
    # is dropped, not silently overwritten.
    moved_relations: list[list[str]] = []
    for row in conn.execute(
        "SELECT related_show_id FROM show_relation WHERE show_id = ?", (loser_id,)
    ).fetchall():
        related = row["related_show_id"]
        if related == winner_id:
            skipped.append(f"show_relation[{loser_id}->{related}]: would self-relate the winner")
            continue
        exists = conn.execute(
            "SELECT 1 FROM show_relation WHERE show_id = ? AND related_show_id = ?",
            (winner_id, related),
        ).fetchone()
        if exists is not None:
            skipped.append(
                f"show_relation[{loser_id}->{related}]: winner already has this relation"
            )
            continue
        conn.execute(
            "UPDATE show_relation SET show_id = ? WHERE show_id = ? AND related_show_id = ?",
            (winner_id, loser_id, related),
        )
        moved_relations.append(["from", related])
    for row in conn.execute(
        "SELECT show_id FROM show_relation WHERE related_show_id = ?", (loser_id,)
    ).fetchall():
        other = row["show_id"]
        if other == winner_id:
            skipped.append(f"show_relation[{other}->{loser_id}]: would self-relate the winner")
            continue
        exists = conn.execute(
            "SELECT 1 FROM show_relation WHERE show_id = ? AND related_show_id = ?",
            (other, winner_id),
        ).fetchone()
        if exists is not None:
            skipped.append(f"show_relation[{other}->{loser_id}]: winner already has this relation")
            continue
        conn.execute(
            "UPDATE show_relation SET related_show_id = ?"
            " WHERE show_id = ? AND related_show_id = ?",
            (winner_id, other, loser_id),
        )
        moved_relations.append(["to", other])
    moved["show_relation"] = moved_relations

    # episode_numbering_mapping — UNIQUE(show_id)
    winner_has_mapping = (
        conn.execute(
            "SELECT 1 FROM episode_numbering_mapping WHERE show_id = ?", (winner_id,)
        ).fetchone()
        is not None
    )
    loser_has_mapping = (
        conn.execute(
            "SELECT 1 FROM episode_numbering_mapping WHERE show_id = ?", (loser_id,)
        ).fetchone()
        is not None
    )
    moved["episode_numbering_mapping"] = False
    if loser_has_mapping:
        if winner_has_mapping:
            skipped.append("episode_numbering_mapping: winner already has one")
        else:
            conn.execute(
                "UPDATE episode_numbering_mapping SET show_id = ? WHERE show_id = ?",
                (winner_id, loser_id),
            )
            moved["episode_numbering_mapping"] = True

    # next_up_override — UNIQUE(show_id)
    winner_has_override = (
        conn.execute("SELECT 1 FROM next_up_override WHERE show_id = ?", (winner_id,)).fetchone()
        is not None
    )
    loser_has_override = (
        conn.execute("SELECT 1 FROM next_up_override WHERE show_id = ?", (loser_id,)).fetchone()
        is not None
    )
    moved["next_up_override"] = False
    if loser_has_override:
        if winner_has_override:
            skipped.append("next_up_override: winner already has one")
        else:
            conn.execute(
                "UPDATE next_up_override SET show_id = ? WHERE show_id = ?", (winner_id, loser_id)
            )
            moved["next_up_override"] = True

    # status_change / score_change / tracked_change — append-only history,
    # no natural per-slot conflict, each row has its own prefixed id.
    for table in ("status_change", "score_change", "tracked_change"):
        row_ids = [
            r["id"]
            for r in conn.execute(
                f"SELECT id FROM {table} WHERE show_id = ?", (loser_id,)
            ).fetchall()
        ]
        if row_ids:
            conn.execute(f"UPDATE {table} SET show_id = ? WHERE show_id = ?", (winner_id, loser_id))
        moved[table] = row_ids

    # show_person — no natural-key constraint at all (a person can hold
    # more than one role in the same show), so every row moves unconditionally.
    persons = [
        [r["person_id"], r["role_type"], r["character_name"]]
        for r in conn.execute(
            "SELECT person_id, role_type, character_name FROM show_person WHERE show_id = ?",
            (loser_id,),
        ).fetchall()
    ]
    if persons:
        conn.execute("UPDATE show_person SET show_id = ? WHERE show_id = ?", (winner_id, loser_id))
    moved["show_person"] = persons

    # show_studio — PK(show_id, studio_id, role_type)
    winner_studio_keys = {
        (r["studio_id"], r["role_type"])
        for r in conn.execute(
            "SELECT studio_id, role_type FROM show_studio WHERE show_id = ?", (winner_id,)
        ).fetchall()
    }
    moved_studios = []
    for row in conn.execute(
        "SELECT studio_id, role_type FROM show_studio WHERE show_id = ?", (loser_id,)
    ).fetchall():
        key = (row["studio_id"], row["role_type"])
        if key in winner_studio_keys:
            skipped.append(
                f"show_studio[{row['studio_id']}/{row['role_type']}]:"
                " winner already has this credit"
            )
            continue
        conn.execute(
            "UPDATE show_studio SET show_id = ? WHERE show_id = ? AND studio_id = ?"
            " AND role_type = ?",
            (winner_id, loser_id, row["studio_id"], row["role_type"]),
        )
        moved_studios.append([row["studio_id"], row["role_type"]])
    moved["show_studio"] = moved_studios

    # franchise_member — PK(franchise_id, show_id)
    winner_franchise_ids = {
        r["franchise_id"]
        for r in conn.execute(
            "SELECT franchise_id FROM franchise_member WHERE show_id = ?", (winner_id,)
        ).fetchall()
    }
    moved_franchises = []
    for row in conn.execute(
        "SELECT franchise_id FROM franchise_member WHERE show_id = ?", (loser_id,)
    ).fetchall():
        if row["franchise_id"] in winner_franchise_ids:
            skipped.append(f"franchise_member[{row['franchise_id']}]: winner already a member")
            continue
        conn.execute(
            "UPDATE franchise_member SET show_id = ? WHERE show_id = ? AND franchise_id = ?",
            (winner_id, loser_id, row["franchise_id"]),
        )
        moved_franchises.append(row["franchise_id"])
    moved["franchise_member"] = moved_franchises

    # show_tag — PK(show_id, tag_id)
    winner_tag_ids = {
        r["tag_id"]
        for r in conn.execute(
            "SELECT tag_id FROM show_tag WHERE show_id = ?", (winner_id,)
        ).fetchall()
    }
    moved_tags = []
    for row in conn.execute(
        "SELECT tag_id FROM show_tag WHERE show_id = ?", (loser_id,)
    ).fetchall():
        if row["tag_id"] in winner_tag_ids:
            skipped.append(f"show_tag[{row['tag_id']}]: winner already has this tag")
            continue
        conn.execute(
            "UPDATE show_tag SET show_id = ? WHERE show_id = ? AND tag_id = ?",
            (winner_id, loser_id, row["tag_id"]),
        )
        moved_tags.append(row["tag_id"])
    moved["show_tag"] = moved_tags

    # Demote the loser — data preserved in place, no deletion. Deliberately
    # does not touch the loser's own title/media_shape/tracking_space, same
    # "don't overwrite what's already trustworthy" reasoning _promote_stub
    # (shows.py, B.11d) applies to a stub.
    conn.execute("UPDATE show SET tracked = 0, updated_at = ? WHERE id = ?", (now, loser_id))
    conn.execute("UPDATE show SET updated_at = ? WHERE id = ?", (now, winner_id))

    manifest = json.dumps({"moved": moved, "skipped": skipped})
    merge_id = ids.generate_id(conn, "y")
    conn.execute(
        """
        INSERT INTO show_merge
            (id, winner_show_id, loser_show_id, matched_on, manifest, merged_at,
             reversed_at, reversed_by_client)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
        """,
        (merge_id, winner_id, loser_id, matched_on, manifest, now),
    )
    conn.commit()
    return merge_id


def sweep_show_merges(conn) -> dict:
    """The automatic sweep (`pollShowMerges`) — one candidate-finding pass
    plus a merge attempt per pair, same per-item error isolation as every
    other Ops-driven sweep (`service_presence.refresh_catalog_presence`'s
    own per-service isolation, `scheduler.run_once`'s own per-show
    isolation): one pair's genuine failure (a concurrent write, an
    unexpected conflict this module didn't anticipate) is logged-and-
    skipped, not allowed to abort the rest of the pass. Returns the
    Ops/human-facing counts: `candidatesFound` (before any merge attempt
    — a fresh recomputation, not cumulative) and `merged` (real successes
    this pass)."""
    pairs = find_candidate_pairs(conn)
    merged = 0
    for loser_id, winner_id, matched_on in pairs:
        try:
            merge_shows(conn, winner_id, loser_id, matched_on)
            merged += 1
        except Exception:
            # Broad, deliberately — same "an unattended sweep logs and
            # moves on, never crashes the rest of the pass" philosophy
            # scheduler._loop()/refresh_catalog_presence already apply.
            # Rolls back any partial writes this one pair's attempt made
            # before it failed — merge_shows() only commits on a clean
            # full success, so a prior pair's already-committed merge is
            # untouched by this rollback.
            logger.exception(
                "show merge failed for loser=%s winner=%s (%s), skipped this sweep",
                loser_id,
                winner_id,
                matched_on,
            )
            conn.rollback()
    return {"candidates_found": len(pairs), "merged": merged}


def reverse_show_merge(conn, merge_id: str, changed_by: str) -> dict:
    """Undoes exactly what `manifest["moved"]` recorded, table by table —
    see module docstring. Raises ValueError if `merge_id` doesn't exist or
    was already reversed. Returns the updated `show_merge` row (dict)."""
    row = conn.execute("SELECT * FROM show_merge WHERE id = ?", (merge_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such show_merge: {merge_id}")
    if row["reversed_at"] is not None:
        raise ValueError(f"show_merge {merge_id} was already reversed at {row['reversed_at']}")

    winner_id = row["winner_show_id"]
    loser_id = row["loser_show_id"]
    moved = json.loads(row["manifest"])["moved"]
    now = util.now_utc_iso()

    # Same BEGIN-before-PRAGMA requirement merge_shows() documents above.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("PRAGMA defer_foreign_keys = ON")

    if moved["show_external_id"]:
        placeholders = ",".join("?" * len(moved["show_external_id"]))
        conn.execute(
            f"UPDATE show_external_id SET show_id = ?"
            f" WHERE show_id = ? AND service IN ({placeholders})",
            (loser_id, winner_id, *moved["show_external_id"]),
        )
    if moved["show_service_presence"]:
        placeholders = ",".join("?" * len(moved["show_service_presence"]))
        conn.execute(
            f"UPDATE show_service_presence SET show_id = ?"
            f" WHERE show_id = ? AND service IN ({placeholders})",
            (loser_id, winner_id, *moved["show_service_presence"]),
        )
    if moved["season"]:
        placeholders = ",".join("?" * len(moved["season"]))
        conn.execute(
            f"UPDATE season SET show_id = ? WHERE id IN ({placeholders})",
            (loser_id, *moved["season"]),
        )
    for episode_id, original_season_id in moved["episode"]:
        conn.execute(
            "UPDATE episode SET show_id = ?, season_id = ? WHERE id = ?",
            (loser_id, original_season_id, episode_id),
        )
    for season, episode in moved["watch_event"]:
        conn.execute(
            "UPDATE watch_event SET show_id = ? WHERE show_id = ? AND season = ? AND episode = ?",
            (loser_id, winner_id, season, episode),
        )
    if moved["episode_movie_link"]:
        placeholders = ",".join("?" * len(moved["episode_movie_link"]))
        conn.execute(
            f"UPDATE episode_movie_link SET movie_show_id = ? WHERE id IN ({placeholders})",
            (loser_id, *moved["episode_movie_link"]),
        )
    for direction, other in moved["show_relation"]:
        if direction == "from":
            conn.execute(
                "UPDATE show_relation SET show_id = ? WHERE show_id = ? AND related_show_id = ?",
                (loser_id, winner_id, other),
            )
        else:
            conn.execute(
                "UPDATE show_relation SET related_show_id = ?"
                " WHERE show_id = ? AND related_show_id = ?",
                (loser_id, other, winner_id),
            )
    if moved["episode_numbering_mapping"]:
        conn.execute(
            "UPDATE episode_numbering_mapping SET show_id = ? WHERE show_id = ?",
            (loser_id, winner_id),
        )
    if moved["next_up_override"]:
        conn.execute(
            "UPDATE next_up_override SET show_id = ? WHERE show_id = ?", (loser_id, winner_id)
        )
    for table in ("status_change", "score_change", "tracked_change"):
        if moved[table]:
            placeholders = ",".join("?" * len(moved[table]))
            conn.execute(
                f"UPDATE {table} SET show_id = ? WHERE id IN ({placeholders})",
                (loser_id, *moved[table]),
            )
    for person_id, role_type, character_name in moved["show_person"]:
        conn.execute(
            "UPDATE show_person SET show_id = ? WHERE show_id = ? AND person_id = ? AND"
            " role_type = ? AND character_name IS ?",
            (loser_id, winner_id, person_id, role_type, character_name),
        )
    for studio_id, role_type in moved["show_studio"]:
        conn.execute(
            "UPDATE show_studio SET show_id = ? WHERE show_id = ? AND studio_id = ?"
            " AND role_type = ?",
            (loser_id, winner_id, studio_id, role_type),
        )
    for franchise_id in moved["franchise_member"]:
        conn.execute(
            "UPDATE franchise_member SET show_id = ? WHERE show_id = ? AND franchise_id = ?",
            (loser_id, winner_id, franchise_id),
        )
    for tag_id in moved["show_tag"]:
        conn.execute(
            "UPDATE show_tag SET show_id = ? WHERE show_id = ? AND tag_id = ?",
            (loser_id, winner_id, tag_id),
        )

    conn.execute("UPDATE show SET tracked = 1, updated_at = ? WHERE id = ?", (now, loser_id))
    conn.execute("UPDATE show SET updated_at = ? WHERE id = ?", (now, winner_id))
    conn.execute(
        "UPDATE show_merge SET reversed_at = ?, reversed_by_client = ? WHERE id = ?",
        (now, changed_by, merge_id),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM show_merge WHERE id = ?", (merge_id,)).fetchone())
