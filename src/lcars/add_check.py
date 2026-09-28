"""The one add check (PLAN-CODE phase 5.1) — RULEBOOK R3.1–R3.7a, R4.7, R5.1–R5.3.

Everything added — by you (browse/add), by Sonarr (webhook, catalog sweep), by
your AniList/MAL lists, or found through a relation — goes through `classify`,
which decides exactly one of:

- `already_tracked`   the season is in LCARS (matched at season level, R1.23);
- `new_season`        TVDB season N of a tracked show (R3.1);
- `link_season`       TVDB season N exists without an AniList/MAL id: this is its id;
- `part`              another AniList/MAL cour of TVDB season N (R1.10);
- `special`           a TVDB season-0 piece (film, OVA…) of a tracked show (R1.13a);
- `new_show`          the first season of a new show, by TVDB id (R3.2);
- `individual_season` no TVDB id yet, new planned season (R3.6, R3.6c, R4.7);
- `not_added`         a related entry whose TVDB show isn't tracked, or has no
                      TVDB id at all (R3.5, R3.5a);
- `needs_user`        a TVDB link no independent source confirms (R3.7, R3.7a),
                      or anything the rules can't settle — with a proposal.

TVDB links (R3.7): accepted automatically only when an independent source agrees
— Sonarr's own series, Fribb/anime-lists, or a show you already track with that
TVDB id — **and** the titles match. A link from a title search alone, or one
LCARS derives through an AniList relation, is proposed to you (R3.7a).

`classify` only reads. Applying a decision is the caller's (and the review's).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lcars import fribb, fuzzy

# Where a candidate comes from.
USER = "user"  # browse/add: you decide, your status
SONARR = "sonarr"  # Sonarr webhook / catalog sweep: a TVDB id from Sonarr
LIST = "list"  # your AniList/MAL list (R4.7)
RELATION = "relation"  # found through an AniList relation / Fribb (R3.5, R3.6)


@dataclass
class Candidate:
    origin: str
    anilist_id: int | None = None
    mal_id: int | None = None
    tvdb_id: int | None = None  # given by the source (Sonarr, you)
    tvdb_name: str | None = None  # TVDB/Sonarr series name, when known
    titles: list[str] = field(default_factory=list)
    media_type: str | None = None  # TV, MOVIE, OVA, ONA, SPECIAL…
    status: str | None = None  # the status you or your list set
    prequel_anilist_ids: list[int] = field(default_factory=list)  # AniList relations


@dataclass
class Decision:
    kind: str
    show_id: str | None = None
    season_id: str | None = None
    tvdb_id: int | None = None
    season_number: int | None = None
    reason: str = ""
    proposal: str = ""


def _season_by_list_id(conn, anilist_id, mal_id):
    """R1.23: AniList/MAL ids are season level."""
    for service, value in (("anilist", anilist_id), ("mal", mal_id)):
        if value is None:
            continue
        row = conn.execute(
            "SELECT z.id, z.show_id FROM season z JOIN season_external_id x ON x.season_id = z.id"
            " WHERE x.service = ? AND x.external_id = ?",
            (service, str(value)),
        ).fetchone()
        if row is None:
            col = "anilist_id" if service == "anilist" else "mal_id"
            row = conn.execute(
                f"SELECT id, show_id FROM season WHERE {col} = ?", (int(value),)
            ).fetchone()
        if row is not None:
            return row
    return None


def _tracked_show_for_tvdb(conn, tvdb_id: int):
    return conn.execute(
        "SELECT sh.* FROM show sh JOIN show_external_id x ON x.show_id = sh.id"
        " WHERE x.service = 'tvdb' AND x.external_id = ? AND sh.tracked = 1",
        (str(tvdb_id),),
    ).fetchone()


def _show_titles(show) -> list[str]:
    return [t for t in (show["display_title_override"], show["title_english"],
                        show["title_romaji"], show["title_native"]) if t]


def titles_match(a: list[str], b: list[str]) -> bool:
    """R3.7's name check: one title of each side matches (exact after
    normalising, else fuzzy above `fuzzy.MATCH_THRESHOLD`), or one contains
    the other (a sequel's "Mushoku Tensei III" contains "Mushoku Tensei")."""
    if fuzzy.best_match(a, b) is not None:
        return True
    na = [fuzzy.normalize_title(t) for t in a if t]
    nb = [fuzzy.normalize_title(t) for t in b if t]
    return any(x and y and (x in y or y in x) for x in na for y in nb)


def _fribb_entries(dataset, anilist_id, mal_id) -> list[dict]:
    if anilist_id is not None:
        found = fribb.build_anilist_index(dataset).get(int(anilist_id), [])
        if found:
            return found
    if mal_id is not None:
        return [e for e in fribb.build_mal_index(dataset).get(int(mal_id), [])
                if e.get("tvdb_id") not in (None, "")]
    return []


def classify(conn, c: Candidate, dataset: list[dict]) -> Decision:
    existing = _season_by_list_id(conn, c.anilist_id, c.mal_id)
    if existing is not None:
        return Decision("already_tracked", existing["show_id"], existing["id"])

    entries = _fribb_entries(dataset, c.anilist_id, c.mal_id)
    fribb_tvdb = {e["tvdb_id"] for e in entries}
    fribb_seasons = {(e.get("season") or {}).get("tvdb") for e in entries}
    tvdb_id = c.tvdb_id
    verified_by = []
    if c.tvdb_id is not None and c.origin in (SONARR, USER):
        verified_by.append(c.origin)
    if len(fribb_tvdb) == 1:
        (only,) = fribb_tvdb
        if tvdb_id is None:
            tvdb_id = only
        if tvdb_id == only:
            verified_by.append("fribb")
        else:
            return Decision("needs_user", tvdb_id=tvdb_id,
                            reason=f"TVDB {tvdb_id} given, Fribb says {only}",
                            proposal="choose the TVDB show")
    elif len(fribb_tvdb) > 1:
        return Decision("needs_user",
                        reason=f"Fribb maps it to several TVDB ids {sorted(fribb_tvdb)}",
                        proposal="choose the TVDB show")

    if tvdb_id is None:
        # No TVDB id from any source: a related entry may still be the next
        # piece of a tracked show through its AniList prequel — LCARS derives
        # that link itself, so you confirm it (R3.7a).
        for prequel in c.prequel_anilist_ids:
            season = _season_by_list_id(conn, prequel, None)
            if season is not None:
                show = conn.execute("SELECT * FROM show WHERE id = ?",
                                    (season["show_id"],)).fetchone()
                if show is not None and show["tracked"]:
                    return Decision(
                        "needs_user", show["id"], reason="TVDB doesn't list it yet (Fribb)",
                        proposal=f"part of {_show_titles(show)[0]} after AniList {prequel}"
                                 " — or an individual season until TVDB has it")
        if c.origin == RELATION:
            return Decision("not_added", reason="no TVDB id (R3.5a)")
        return Decision("individual_season", reason="no TVDB id yet (R3.2, R3.6, R4.7)")

    show = _tracked_show_for_tvdb(conn, tvdb_id)
    if show is not None:
        verified_by.append("tracked show")
        if c.titles and not titles_match(_show_titles(show), c.titles):
            return Decision("needs_user", show["id"], tvdb_id=tvdb_id,
                            reason="titles don't match the tracked show (R3.7)",
                            proposal=f"add to {_show_titles(show)[0]}?")
    if len(set(verified_by) - {"tracked show"}) == 0:
        return Decision("needs_user", show["id"] if show else None, tvdb_id=tvdb_id,
                        reason="TVDB link from no independent source (R3.7)",
                        proposal=f"TVDB {tvdb_id}")

    if show is None:
        if c.origin == RELATION:
            return Decision("not_added", tvdb_id=tvdb_id,
                            reason="its TVDB show isn't tracked (R3.5)")
        if c.tvdb_name and c.titles and not titles_match([c.tvdb_name], c.titles):
            return Decision("needs_user", tvdb_id=tvdb_id,
                            reason=f"TVDB name '{c.tvdb_name}' doesn't match (R3.7)",
                            proposal=f"new show on TVDB {tvdb_id}?")
        return Decision("new_show", tvdb_id=tvdb_id, reason="first season of a new show")

    seasons = {s for s in fribb_seasons if s is not None}
    if len(seasons) != 1:
        return Decision("needs_user", show["id"], tvdb_id=tvdb_id,
                        reason="Fribb gives no single TVDB season",
                        proposal=f"which season of {_show_titles(show)[0]}?")
    (n,) = seasons
    if n == 0 or (c.media_type or "").upper() in ("MOVIE", "SPECIAL"):
        return Decision("special", show["id"], tvdb_id=tvdb_id, season_number=0,
                        reason="TVDB season 0 piece — placed by Memory Alpha (R1.8, R1.13a)")
    row = conn.execute(
        "SELECT id, anilist_id, mal_id FROM season WHERE show_id = ? AND season_number = ?"
        " AND kind = 'tvdb_season'",
        (show["id"], n),
    ).fetchone()
    if row is not None:
        has_ids = row["anilist_id"] is not None or row["mal_id"] is not None or conn.execute(
            "SELECT 1 FROM season_external_id WHERE season_id = ?"
            " AND service IN ('anilist', 'mal')", (row["id"],)
        ).fetchone() is not None
        if not has_ids:
            return Decision("link_season", show["id"], row["id"], tvdb_id, n,
                            reason=f"TVDB season {n} has no AniList/MAL id yet")
        return Decision("part", show["id"], row["id"], tvdb_id, n,
                        reason=f"another cour of TVDB season {n} (R1.10)")
    return Decision("new_season", show["id"], tvdb_id=tvdb_id, season_number=n,
                    reason=f"TVDB season {n} of a tracked show")


# ── Applying: a series added in Sonarr (R5.1–R5.3) ─────────────────────


def review(conn, key: str, decision: Decision, source: str) -> None:
    """A decision you must take (R3.7a) or a candidate the rules don't add,
    kept for you as a review (R4.8b: the choices are in `proposal`)."""
    from lcars import pending_review  # deferred: pending_review is a leaf, kept lazy

    pending_review.open_or_extend(
        conn, "show", key, f"add_check:{decision.kind}", source, None,
        f"{decision.reason} — {decision.proposal}" if decision.proposal else decision.reason,
    )


def apply_sonarr_initial_statuses(conn, show_id: str) -> None:
    """R5.3: a show added from Sonarr with several seasons, none tracked yet:
    the latest season planned, every earlier one skipped (episodes still
    fetched for numbering, R2.19); the show is then planned (R2.13)."""
    from lcars import status_rules

    seasons = conn.execute(
        "SELECT id FROM season WHERE show_id = ? AND kind = 'tvdb_season' AND season_number > 0"
        " ORDER BY season_number",
        (show_id,),
    ).fetchall()
    for i, row in enumerate(seasons):
        season = conn.execute("SELECT * FROM season WHERE id = ?", (row["id"],)).fetchone()
        want = "planned" if i == len(seasons) - 1 else "skipped"
        status_rules._set(conn, season, want, "sonarr", False, status_rules.Effects())
    status_rules.recompute_show(conn, show_id, "sonarr")


def add_sonarr_series(
    conn, tvdb_id: int, title: str, tracking_space: str, source: str
) -> tuple[str, str | None]:
    """A series in Sonarr that LCARS doesn't track (webhook or catalog sweep):
    through the add check; a new show is created planned, R5.3 statuses.
    Returns (decision kind, show id or None)."""
    from lcars import fribb as _fribb
    from lcars import shows

    tracked = _tracked_show_for_tvdb(conn, int(tvdb_id))
    if tracked is not None:
        return "already_tracked", tracked["id"]
    decision = classify(
        conn, Candidate(SONARR, tvdb_id=int(tvdb_id), tvdb_name=title, titles=[title]),
        _fribb.load_dataset(),
    )
    if decision.kind != "new_show":
        review(conn, f"tvdb:{tvdb_id}", decision, source)
        conn.commit()
        return decision.kind, None
    show_id = shows.create_show(conn, {
        "media_shape": "episodic",
        "tracking_space": tracking_space,
        "primary_title": "english",
        "title_english": title,
        "tvdb_id": str(tvdb_id),
        "skip_sequel_check": True,  # R1.14: its own TVDB id makes it its own show
    })
    apply_sonarr_initial_statuses(conn, show_id)
    conn.commit()
    return "new_show", show_id
