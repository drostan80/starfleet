"""The one add check (PLAN-CODE phase 5.1) — RULEBOOK R3.1–R3.7a, R4.7, R5.1–R5.3.

Everything added — by you (browse/add), by Sonarr (webhook, catalog sweep), by
your AniList/MAL lists, or found through a relation — goes through `classify`,
which decides exactly one of:

- `already_tracked`   the season is in LCARS (matched at season level, R1.23);
- `new_season`        TVDB season N of a tracked show (R3.1);
- `link_season`       TVDB season N exists without an AniList/MAL id: this is its id;
- `part`              another AniList/MAL cour of TVDB season N (R1.10);
- `special`           a TVDB season-0 piece (film, OVA…) of a tracked show (R1.13a);
- `span`              one entry whose episodes fill several whole TVDB seasons (Fribb gives the
                      TVDB id but no single season; the anime-lists episode mapping says which):
                      its ids go on each of those levels, one entry over several seasons
                      (R1.22, `list_sync.group_levels`), when none of them is skipped or holds
                      other ids — otherwise it is yours to say;
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

import json
from dataclasses import dataclass, field

from lcars import fribb, fuzzy

# Where a candidate comes from.
USER = "user"  # browse/add: you decide, your status
SONARR = "sonarr"  # Sonarr webhook / catalog sweep: a TVDB id from Sonarr
LIST = "list"  # your AniList/MAL list (R4.7)
RELATION = "relation"  # found through an AniList relation / Fribb (R3.5, R3.6)


# classify's reason when neither Fribb nor the anime-lists episode mapping names one TVDB season
NO_SEASON = "no single TVDB season by Fribb or the anime-lists episode mapping"


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
    season_numbers: list[int] = field(default_factory=list)  # a `span`: the TVDB seasons it fills


def _season_by_list_id(conn, anilist_id, mal_id, exclude_season_id=None):
    """R1.23: AniList/MAL ids are season level. `exclude_season_id`: an
    individual season being joined to a show isn't its own match (8.8)."""
    skip = exclude_season_id or ""
    for service, value in (("anilist", anilist_id), ("mal", mal_id)):
        if value is None:
            continue
        row = conn.execute(
            "SELECT z.id, z.show_id FROM season z JOIN season_external_id x ON x.season_id = z.id"
            " WHERE x.service = ? AND x.external_id = ? AND z.id != ?",
            (service, str(value), skip),
        ).fetchone()
        if row is None:
            col = "anilist_id" if service == "anilist" else "mal_id"
            row = conn.execute(
                f"SELECT id, show_id FROM season WHERE {col} = ? AND id != ?", (int(value), skip)
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


def spanned_tvdb_seasons(conn, entries: list[dict], tvdb_id) -> list[int]:
    """The TVDB seasons (> 0) the entry's episodes are placed in by the anime-lists episode
    mapping (AniDB season 1 ranges: Rayearth's 49 episodes are TVDB S1 1–20 and S2 21–49 — Fribb
    gives it the TVDB id and no season). [] when the mapping says nothing."""
    found: set[int] = set()
    for e in entries:
        anidb = e.get("anidb_id")
        if anidb in (None, "", "unknown"):
            continue
        row = conn.execute(
            "SELECT id, tvdb_id FROM anime_list_entry WHERE anidb_id = ?", (int(anidb),)
        ).fetchone()
        if row is None or str(row["tvdb_id"]) != str(tvdb_id):
            continue
        found.update(m[0] for m in conn.execute(
            "SELECT DISTINCT tvdb_season FROM anime_list_mapping WHERE entry_id = ?"
            " AND anidb_season = 1 AND tvdb_season > 0 AND start IS NOT NULL", (row["id"],)))
    return sorted(found)


def span_levels(conn, show_id: str, numbers: list[int]):
    """(levels by TVDB season, seasons LCARS lacks, seasons already holding a list id, seasons
    that are skipped) for the seasons a span fills."""
    levels = {
        n: conn.execute(
            "SELECT * FROM season WHERE show_id = ? AND season_number = ? AND kind = 'tvdb_season'",
            (show_id, n)).fetchone()
        for n in numbers
    }
    missing = [n for n, z in levels.items() if z is None]
    taken = [n for n, z in levels.items() if z is not None and (
        z["anilist_id"] is not None or z["mal_id"] is not None or conn.execute(
            "SELECT 1 FROM season_external_id WHERE season_id = ?"
            " AND service IN ('anilist', 'mal')", (z["id"],)).fetchone() is not None)]
    skipped = [n for n, z in levels.items() if z is not None and z["status"] == "skipped"]
    return levels, missing, taken, skipped


def _span_decision(conn, show, tvdb_id: int, numbers: list[int]) -> Decision:
    """An entry that fills several whole TVDB seasons: placed on each as one entry when every one
    exists, is not skipped and holds no other id; otherwise yours to say, with why."""
    label = ", ".join(f"S{n}" for n in numbers)
    levels, missing, taken, skipped = span_levels(conn, show["id"], numbers)
    title = _show_titles(show)[0]
    head = f"its episodes fill TVDB seasons {label} of {title}"

    def lst(ns):
        return ", ".join(f"S{n}" for n in ns)

    if missing:
        return Decision("needs_user", show["id"], tvdb_id=tvdb_id, season_numbers=numbers,
                        reason=f"{head}, but {lst(missing)} isn't in LCARS",
                        proposal="individual season until TVDB has them — or don't add")
    if taken:
        return Decision("needs_user", show["id"], tvdb_id=tvdb_id, season_numbers=numbers,
                        reason=f"{head}, but {lst(taken)} already holds another entry",
                        proposal="individual season — or don't add")
    if skipped:
        return Decision("needs_user", show["id"], tvdb_id=tvdb_id, season_numbers=numbers,
                        reason=f"{head}; {lst(skipped)} is skipped, so as one entry its list "
                               "progress would count only the seasons that are not skipped",
                        proposal=f"attach it to {label} anyway — or don't add")
    return Decision("span", show["id"], levels[numbers[-1]]["id"], tvdb_id,
                    reason=f"one entry over TVDB seasons {label} (anime-lists episode ranges)",
                    season_numbers=numbers)


def classify(conn, c: Candidate, dataset: list[dict], *, exclude_season_id=None) -> Decision:
    existing = _season_by_list_id(conn, c.anilist_id, c.mal_id, exclude_season_id)
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
                        "needs_user", show["id"], season["id"],
                        reason="TVDB doesn't list it yet (Fribb)",
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
        # Fribb names no season, or several: the episode ranges of the anime-lists mapping say
        # which TVDB seasons the entry fills (RULEBOOK R1.22, user 10-06: place by episode range)
        seasons = set(sorted(seasons) if len(seasons) > 1
                      else spanned_tvdb_seasons(conn, entries, tvdb_id))
        if len(seasons) > 1 and 0 not in seasons:
            return _span_decision(conn, show, tvdb_id, sorted(seasons))
    if len(seasons) != 1:
        return Decision("needs_user", show["id"], tvdb_id=tvdb_id, reason=NO_SEASON,
                        proposal=f"which season of {_show_titles(show)[0]}? (\"season 2\" in the"
                                 " note, with your TVDB id)")
    (n,) = seasons
    return place_in_season(conn, c, show, tvdb_id, n)


def place_in_season(conn, c: Candidate, show, tvdb_id: int, n: int) -> Decision:
    """Where an entry goes once its one TVDB season `n` is known (by Fribb, by the anime-lists
    ranges, or by you)."""
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


def review(
    conn, key: str, decision: Decision, source: str, candidate: Candidate | None = None
) -> None:
    """A decision you must take (R3.7a) or a candidate the rules don't add,
    kept for you as an actionable review (R4.8b)."""
    from lcars import pending_review, reviews  # deferred: keep this module light

    field_name = f"add_check:{decision.kind}"
    value = f"{decision.reason} — {decision.proposal}" if decision.proposal else decision.reason
    if pending_review.already_resolved_with(conn, "show", key, field_name, value):
        return  # you already decided this one
    open_row = conn.execute(
        "SELECT proposed_value_chain FROM pending_review WHERE entity_type = 'show'"
        " AND entity_id = ? AND field = ? AND resolved_at IS NULL",
        (key, field_name),
    ).fetchone()
    if open_row is not None and value in json.loads(open_row[0]):
        return  # already waiting for you
    choices = ["individual", "dont_add"]
    if decision.show_id and decision.season_id:
        choices = ["add_to_show", *choices]
    if decision.season_numbers and decision.show_id:
        _levels, missing, taken, _skipped = span_levels(conn, decision.show_id,
                                                         decision.season_numbers)
        if not missing and not taken:  # only a skipped season stands in the way: yours to say
            choices = ["attach_span", *choices]
    choices.append("use_tvdb_id")  # R3.7d: a TVDB id you have goes in the note
    payload = {"show_id": decision.show_id, "season_id": decision.season_id,
               "tvdb_id": decision.tvdb_id, "span_seasons": decision.season_numbers}
    if candidate is not None:
        payload.update(anilist_id=candidate.anilist_id, mal_id=candidate.mal_id,
                       titles=candidate.titles, media_type=candidate.media_type,
                       status=LIST_STATUS.get((candidate.status or "").upper()))
        if decision.tvdb_id:  # 8.8.3: the facts side by side, fetched once
            from lcars import tvdb_vetting

            payload.update(tvdb_vetting.evidence(
                conn, tvdb_vetting.entry_facts(candidate.anilist_id, candidate.titles),
                [(f"TVDB {decision.tvdb_id}", int(decision.tvdb_id))]))
    reviews.open_review(conn, "show", key, field_name, source, value, choices, payload,
                        show_id=decision.show_id)


def create_individual_season(conn, c: Candidate, status: str | None = None) -> str:
    """R3.2, R3.6, R3.6c/d, R4.7: a season with no TVDB show yet — its own
    AniList/MAL ids, mirrored like any season (R3.6b), planned unless your
    list says otherwise; it joins a show once TVDB has it (proposed to you)."""
    from lcars import ids, season_ranges, util

    season_id = ids.generate_id(conn, "z")
    now = util.now_utc_iso()
    title = c.titles[0] if c.titles else None
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, kind, anilist_id, mal_id, label,"
        " source, status, status_set_manually, list_sync, created_at, updated_at)"
        " VALUES (?, NULL, NULL, 'individual_season', ?, ?, ?, 'manual', ?, 1, 1, ?, ?)",
        (season_id, c.anilist_id, c.mal_id, title, status or "planned", now, now),
    )
    season_ranges.upsert_season_external_id(conn, season_id, c.anilist_id, c.mal_id, now)
    return season_id


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
    fx = status_rules.Effects()
    for i, row in enumerate(seasons):
        season = conn.execute("SELECT * FROM season WHERE id = ?", (row["id"],)).fetchone()
        want = "planned" if i == len(seasons) - 1 else "skipped"
        status_rules._set(conn, season, want, "sonarr", False, fx)
    status_rules.recompute_show(conn, show_id, "sonarr")
    from lcars import sonarr_sync

    sonarr_sync.apply(conn, fx.seasons)  # skipped → unmonitored, latest → future (R5.6/R5.8)


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
    conn.execute(  # 8.8.5: this id came from Sonarr's own series
        "UPDATE show_external_id SET source = 'sonarr' WHERE show_id = ? AND service = 'tvdb'"
        " AND source IS NULL", (show_id,))
    apply_sonarr_initial_statuses(conn, show_id)
    conn.commit()
    return "new_show", show_id


# ── Applying an automatic decision (R3.4, R3.5a) ───────────────────────

AUTOMATIC = ("link_season", "new_season", "part", "special", "span")


def _status_after(previous: str | None) -> str:
    """R2.16 against the level it follows."""
    from lcars import status_rules

    return "skipped" if previous in status_rules.STOP_FOLLOWING else "planned"


def _insert_level(conn, show_id, season_number, part_number, kind, parent_id, c, status):
    from lcars import ids, season_ranges, util

    season_id = ids.generate_id(conn, "z")
    now = util.now_utc_iso()
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, part_number, kind, parent_id,"
        " anilist_id, mal_id, source, status, list_sync, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'auto', ?, 0, ?, ?)",
        (season_id, show_id, season_number, part_number, kind, parent_id,
         c.anilist_id, c.mal_id, status, now, now),
    )
    season_ranges.upsert_season_external_id(conn, season_id, c.anilist_id, c.mal_id, now)
    return season_id


def _attach_span(conn, d: Decision, c: Candidate, now: str) -> str:
    """The entry's ids on every TVDB season it fills: one list entry over several levels
    (`list_sync.group_levels`); returns the last level, which speaks for it."""
    from lcars import season_ranges

    last = None
    for n in d.season_numbers:
        row = conn.execute(
            "SELECT id FROM season WHERE show_id = ? AND season_number = ?"
            " AND kind = 'tvdb_season'", (d.show_id, n)).fetchone()
        conn.execute(
            "UPDATE season SET anilist_id = COALESCE(anilist_id, ?),"
            " mal_id = COALESCE(mal_id, ?), updated_at = ? WHERE id = ?",
            (c.anilist_id, c.mal_id, now, row["id"]))
        season_ranges.upsert_season_external_id(conn, row["id"], c.anilist_id, c.mal_id, now)
        last = row["id"]
    return last


def apply_decision(conn, d: Decision, c: Candidate) -> str | None:
    """Writes an automatic decision (`AUTOMATIC`); returns the season id.
    Numbering (spans) and the show's status follow on the next pass."""
    from lcars import season_ranges, status_rules, util

    if d.kind not in AUTOMATIC:
        return None
    now = util.now_utc_iso()
    if d.kind == "link_season":
        conn.execute(
            "UPDATE season SET anilist_id = COALESCE(anilist_id, ?), mal_id = COALESCE(mal_id, ?),"
            " updated_at = ? WHERE id = ?",
            (c.anilist_id, c.mal_id, now, d.season_id),
        )
        season_ranges.upsert_season_external_id(conn, d.season_id, c.anilist_id, c.mal_id, now)
        return d.season_id
    if d.kind == "span":
        return _attach_span(conn, d, c, now)
    if d.kind == "new_season":
        status = status_rules.new_season_status(conn, d.show_id, d.season_number)
        season_id = _insert_level(conn, d.show_id, d.season_number, 1, "tvdb_season", None,
                                  c, status)
    elif d.kind == "part":
        parent = conn.execute("SELECT * FROM season WHERE id = ?", (d.season_id,)).fetchone()
        parts = conn.execute(
            "SELECT COUNT(*) FROM season WHERE parent_id = ? AND kind = 'part'", (parent["id"],)
        ).fetchone()[0]
        if parts == 0 and (parent["anilist_id"] is not None or parent["mal_id"] is not None):
            # The TVDB season held its first cour's ids: that cour becomes
            # part 1, so every AniList/MAL id sits on exactly one level (R1.22).
            first = Candidate(c.origin, anilist_id=parent["anilist_id"], mal_id=parent["mal_id"])
            conn.execute(
                "UPDATE season SET anilist_id = NULL, mal_id = NULL, updated_at = ? WHERE id = ?",
                (now, parent["id"]),
            )
            conn.execute(
                "DELETE FROM season_external_id WHERE season_id = ?"
                " AND service IN ('anilist', 'mal')", (parent["id"],),
            )
            _insert_level(conn, parent["show_id"], parent["season_number"], 1, "part",
                          parent["id"], first, parent["status"])
            parts = 1
        season_id = _insert_level(conn, parent["show_id"], parent["season_number"], parts + 1,
                                  "part", parent["id"], c, _status_after(parent["status"]))
    else:  # special: its place and number come from Memory Alpha (R1.8, R1.13a)
        last = status_rules.last_season(conn, d.show_id)
        season_id = _insert_level(conn, d.show_id, None, 1, "special", None, c,
                                  _status_after(last["status"] if last else None))
    status_rules.recompute_show(conn, d.show_id, status_rules.AUTO)  # R2.17, not pushed
    from lcars import sonarr_sync

    row = conn.execute("SELECT status FROM season WHERE id = ?", (season_id,)).fetchone()
    sonarr_sync.apply(conn, [(season_id, None, row[0])])  # R5.6/R5.8 for that season
    return season_id


# ── Your AniList/MAL list entries (R4.7, phase 5.4) ────────────────────


def add_list_entry(conn, c: Candidate, dataset: list[dict]) -> Decision:
    """An entry on your list that LCARS doesn't track goes through the add
    check (R4.7: as a season). Writes nothing unless `list_adds_enabled`
    (off until the phase 9 dry run) — the decision is returned either way."""
    from lcars import config, shows, status_rules

    d = classify(conn, c, dataset)
    if not config.get_current().list_adds_enabled:
        return d
    season_id = None
    if d.kind in AUTOMATIC:
        season_id = apply_decision(conn, d, c)
    elif d.kind == "needs_user":
        review(conn, f"anilist:{c.anilist_id}", d, "list", c)
    elif d.kind == "individual_season":
        season_id = create_individual_season(conn, c, LIST_STATUS.get((c.status or "").upper()))
    elif d.kind == "new_show":
        title = c.titles[0] if c.titles else str(c.anilist_id)
        show_id = shows.create_show(conn, {
            "media_shape": "movie" if (c.media_type or "").upper() == "MOVIE" else "episodic",
            "tracking_space": "anime", "primary_title": "romaji", "title_romaji": title,
            "anilist_id": c.anilist_id, "mal_id": c.mal_id, "tvdb_id": d.tvdb_id,
            "skip_sequel_check": True,
        })
        found = _season_by_list_id(conn, c.anilist_id, c.mal_id)
        season_id = found["id"] if found is not None else None
        if season_id is None:
            status = LIST_STATUS.get((c.status or "").upper())
            if status:
                status_rules.set_show_status(conn, show_id, status, "list", confirmed=True)
    status = LIST_STATUS.get((c.status or "").upper())
    if season_id is not None and status:
        # R4.7: added with the status you set on your list.
        status_rules.set_level_status(conn, season_id, status, "list", confirmed=True)
    conn.commit()
    return d


# AniList/MAL list status → LCARS (R4.6a: rewatching is watching for now).
LIST_STATUS = {
    "CURRENT": "watching", "WATCHING": "watching", "REPEATING": "watching",
    "PLANNING": "planned", "PLAN_TO_WATCH": "planned",
    "COMPLETED": "completed", "PAUSED": "paused", "ON_HOLD": "paused", "DROPPED": "dropped",
}
