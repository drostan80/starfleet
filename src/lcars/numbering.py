"""Memory Alpha's numbering engine — LCARS absolute numbers (PLAN-CODE phase 3).

RULEBOOK R1.0–R1.12, R1.2a–d, R1.8: every episode of a show gets an LCARS
absolute number, season 0 included; each TVDB season is the list of spans of
the numbers mapped to it. Sonarr/TVDB, AniDB and TVmaze numbers are mappings
only.

Build model (R1.2d, user 2026-09-28): numbers are first derived from TVDB
order + air date, then reconciled with AniDB (anime) / TVmaze (TV) as their
data comes in. `show.absolute_numbering_source` records which source numbered
the show; `tvdb` means the fallback, renumbered once the source is complete.

The rules, in the order the engine applies them:

1. **Main episodes** are the TVDB seasons' episodes (season > 0), in TVDB
   order (R1.7, R1.17). AniDB may move one out (it files it as a special:
   decimal, R1.3) or in (a TVDB season-0 episode AniDB files as a regular
   episode of a season's own entry: whole number, R1.8c). Both are listed
   for the user to confirm. Where AniDB's order disagrees with TVDB's, air
   date decides (R1.2) and the show is listed.
2. **Everything else** (season 0: specials, OVAs, films) is placed by air
   date against the main episodes (R1.8):
   - before episode 1 → `0.x` (R1.2b);
   - a film → a whole number after the episode it follows (R1.4, the R1.12
     example: film abs 17 mid-season), listed for confirmation (R1.5
     special-version films take a decimal — the user marks those);
   - inside a TVDB season's air window → decimal after the preceding
     episode (R1.8a);
   - between seasons, or after the last one → decimal after the season's
     last episode, in that gap's side piece (decimal season, R1.8d, R1.9a:
     Frieren's minis 28.01…28.10, S2 still starts at 29). A full-length one
     there (R1.3: "may take a whole number") is listed for confirmation.
   Without an air date it can't be placed: a placeholder 5000.1, 5000.2… (shown
   as x, R1.0a) until the date arrives, and listed.
3. **Numbers**: whole numbers run 1, 2, 3… over main episodes and whole
   side items. Decimals in one gap: one item → `.5`, several → `.1, .2…`
   in air order (R1.2b); ten or more → `.01, .02…`, listed.
4. **Spans** (R1.11, R1.12): a TVDB season's spans are the runs of its own
   episodes in the numbered order — anything not mapped to it breaks a run,
   so a special inside the season never counts toward it (R2.7).
   Levels for parts (AniList/MAL cours) and for side items with their own
   AniDB id are part of the plan for the rebuild; side items with no id of
   their own are listed (`no_level`), never grouped by guess (R1.15).

`plan_show` is pure (no database); `load_show` reads its inputs,
`apply_plan` writes the result. `lcars numbering <db>` runs it as a dry run.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass, field

from lcars import anidb, fribb, util
from lcars import ids as ids_module

FILM_MINUTES = 60
FULL_LENGTH_MINUTES = 15
PLACEHOLDER = 5000  # R1.0a: numbers from here up are "no air date yet" (shown as x)


@dataclass
class Item:
    id: str
    tvdb_season: int
    tvdb_episode: int
    air: str | None = None
    runtime: int | None = None
    # AniDB mapping, when known: (anime id, 1 regular / 0 special, epno)
    anidb: tuple[int, int, int] | None = None
    anidb_type: str | None = None  # the AniDB entry's type (TV Series, Movie, OVA…)


@dataclass
class Plan:
    show_id: str
    source: str  # anidb | tvmaze | tvdb
    numbers: dict[str, float | None] = field(default_factory=dict)
    # TVDB season number → spans [(from, to)]
    season_spans: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    # other levels for the rebuild: key → spans ("part:<season>:<anidb id>", "anidb:<id>")
    level_spans: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    # side piece key ("side:<season>") → AniDB ids of its items, to find its level
    side_anidb: dict[str, set[int]] = field(default_factory=dict)
    # "anidb:<id>" → (TVDB season the group sits in, spans): the mini sub-season made
    # for a season-0 piece's AniDB entry when LCARS holds no level for it (R1.13b)
    anidb_groups: dict[str, tuple[int | None, list[tuple[float, float]]]] = field(
        default_factory=dict)
    flags: list[dict] = field(default_factory=list)


def _is_film(item: Item) -> bool:
    if item.anidb_type:
        return item.anidb_type == "Movie"
    return (item.runtime or 0) >= FILM_MINUTES


def _flag(plan: Plan, kind: str, item: Item | None = None, **extra) -> None:
    entry = {"kind": kind, **extra}
    if item is not None:
        entry.update(episode_id=item.id, tvdb=f"S{item.tvdb_season}E{item.tvdb_episode}")
    plan.flags.append(entry)


def _decimals(count: int) -> list[float]:
    """Fractions for `count` items in one gap (R1.2b)."""
    if count == 1:
        return [0.5]
    step = 0.1 if count <= 9 else 0.01
    return [round(step * (i + 1), 4) for i in range(count)]


def plan_show(
    show_id: str, items: list[Item], source: str, *, main_anidb_ids: set[int] | None = None
) -> Plan:
    """Numbers and spans for one show's episodes. `main_anidb_ids`: the AniDB
    ids of the show's TVDB seasons (a season-0 episode AniDB files as a
    regular episode of one of those entries is a main episode, R1.8c)."""
    plan = Plan(show_id, source)
    main_ids = main_anidb_ids if main_anidb_ids is not None else {
        i.anidb[0] for i in items if i.tvdb_season > 0 and i.anidb and i.anidb[1] == 1
    }

    main: list[Item] = []
    side: list[Item] = []
    for it in items:
        if it.tvdb_season > 0:
            if it.anidb and it.anidb[1] == 0:
                side.append(it)
                _flag(plan, "anidb_special_in_tvdb_season", it)
            else:
                main.append(it)
        elif it.anidb and it.anidb[1] == 1 and it.anidb[0] in main_ids:
            main.append(it)
            _flag(plan, "anidb_regular_in_season_0", it)
        else:
            side.append(it)

    # 1. Main order: TVDB order; season-0 episodes AniDB files as regular
    # go where their air date puts them.
    tvdb_main = sorted((m for m in main if m.tvdb_season > 0),
                       key=lambda m: (m.tvdb_season, m.tvdb_episode))
    promoted = [m for m in main if m.tvdb_season == 0]
    main_order = tvdb_main
    anidb_keys = [(m.anidb[0], m.anidb[2]) for m in tvdb_main if m.anidb and m.anidb[1] == 1]
    order_disagrees = any(
        a[0] == b[0] and a[1] > b[1] for a, b in zip(anidb_keys, anidb_keys[1:], strict=False)
    )
    if order_disagrees:
        if all(m.air for m in tvdb_main):
            main_order = sorted(tvdb_main, key=lambda m: (m.air, m.tvdb_season, m.tvdb_episode))
            _flag(plan, "order_by_air_date")
        else:
            _flag(plan, "order_disagreement_unresolved")
    for p in sorted(promoted, key=lambda m: (m.air or "", m.tvdb_episode)):
        pos = len(main_order)
        if p.air:
            for i, m in enumerate(main_order):
                if m.air and m.air > p.air:
                    pos = i
                    break
        main_order = main_order[:pos] + [p] + main_order[pos:]

    def season_of(m: Item) -> int | None:
        if m.tvdb_season > 0:
            return m.tvdb_season
        # A promoted season-0 episode belongs to the season sharing its AniDB entry.
        for other in main_order:
            if other.tvdb_season > 0 and other.anidb and m.anidb and other.anidb[0] == m.anidb[0]:
                return other.tvdb_season
        return None

    # 2. Place side items by air date against the main order.
    before_first: list[Item] = []
    after: dict[int, list[tuple[Item, bool]]] = {}  # main index → [(item, whole)]
    between: dict[str, int | None] = {}  # side item → the season it follows (R1.8d)
    undated: list[Item] = []  # no air date: placeholder numbers (R1.0a)
    inside: dict[str, int | None] = {}  # side item → the TVDB season whose run it sits in
    for it in sorted(side, key=lambda s: (s.air or "", s.tvdb_season, s.tvdb_episode)):
        if it.tvdb_season > 0 and not it.air:
            # AniDB special inside a TVDB season, no date: stays after its TVDB predecessor.
            prev = max(
                (i for i, m in enumerate(main_order)
                 if m.tvdb_season == it.tvdb_season and m.tvdb_episode < it.tvdb_episode),
                default=None,
            )
            if prev is None:
                before_first.append(it)
            else:
                after.setdefault(prev, []).append((it, False))
                inside[it.id] = it.tvdb_season
            continue
        if not it.air:
            undated.append(it)
            _flag(plan, "no_air_date", it)
            continue
        prev = None
        for i, m in enumerate(main_order):
            if m.air and m.air <= it.air:
                prev = i
        if prev is None:
            before_first.append(it)
            if _is_film(it):
                _flag(plan, "film_placement", it, placed="before episode 1")
            continue
        nxt = next((m for m in main_order[prev + 1:] if m.air), None)
        if _is_film(it):
            whole = True
            _flag(plan, "film_placement", it, placed="whole number")
        else:
            whole = False
            if nxt is None or season_of(main_order[prev]) != season_of(nxt):
                between[it.id] = season_of(main_order[prev])
                if (it.runtime or 0) >= FULL_LENGTH_MINUTES:
                    _flag(plan, "full_length_between_seasons", it)
        if it.id not in between:
            inside[it.id] = season_of(main_order[prev])
        after.setdefault(prev, []).append((it, whole))

    # 3. Numbers.
    sequence: list[Item] = []
    counter = 0

    def emit_decimals(group: list[Item]) -> None:
        if len(group) >= 10:
            _flag(plan, "ten_or_more_in_one_gap", after_number=counter, count=len(group))
        for it, frac in zip(group, _decimals(len(group)), strict=True):
            plan.numbers[it.id] = round(counter + frac, 4)
            sequence.append(it)

    emit_decimals(before_first)
    for i, m in enumerate(main_order):
        counter += 1
        plan.numbers[m.id] = float(counter)
        sequence.append(m)
        pending: list[Item] = []
        for it, whole in after.get(i, []):
            if whole:
                emit_decimals(pending)
                pending = []
                counter += 1
                plan.numbers[it.id] = float(counter)
                sequence.append(it)
            else:
                pending.append(it)
        emit_decimals(pending)
    # R1.0a: no air date yet → 5000.1, 5000.2… (shown as x; sorts as not aired).
    step = 0.1 if len(undated) <= 9 else 0.01
    for i, it in enumerate(undated):
        plan.numbers[it.id] = round(PLACEHOLDER + step * (i + 1), 4)

    # 4. Spans: runs of each level's own items in the numbered order.
    def runs(member) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        start = end = None
        for it in sequence:
            if member(it):
                n = plan.numbers[it.id]
                start = n if start is None else start
                end = n
            elif start is not None:
                out.append((start, end))
                start = end = None
        if start is not None:
            out.append((start, end))
        return out

    main_set = {m.id for m in main_order}
    seasons = sorted({s for m in main_order if (s := season_of(m)) is not None})
    for s in seasons:
        plan.season_spans[s] = runs(lambda it, s=s: it.id in main_set and season_of(it) == s)
        season_ids = {m.anidb[0] for m in main_order
                      if season_of(m) == s and m.anidb and m.anidb[1] == 1}
        if len(season_ids) > 1:
            for aid in sorted(season_ids):
                plan.level_spans[f"part:{s}:{aid}"] = runs(
                    lambda it, s=s, aid=aid: it.id in main_set and season_of(it) == s
                    and it.anidb is not None and it.anidb[0] == aid
                )
    for season in sorted({s for s in between.values() if s is not None}):
        # The side piece after this season: its decimal season (R1.8d, R1.9a).
        plan.level_spans[f"side:{season}"] = runs(
            lambda x, season=season: between.get(x.id) == season
        )
        plan.side_anidb[f"side:{season}"] = {
            it.anidb[0] for it in side if between.get(it.id) == season and it.anidb
        }
    def add_to(spans: list, n: float) -> None:
        if spans and spans[-1][1] < n and not any(
            m.id in main_set and spans[-1][1] < plan.numbers[m.id] < n for m in main_order
        ):
            spans[-1] = (spans[-1][0], n)  # nothing of another level between: one run
        else:
            spans.append((n, n))

    for it in side:
        if plan.numbers.get(it.id) is None or it.id in between:
            continue
        if it.anidb and it.anidb[0] not in main_ids:
            aid = it.anidb[0]
            key = f"anidb:{aid}"
            if key not in plan.level_spans:
                plan.level_spans[key] = runs(
                    lambda x, aid=aid: x.anidb is not None and x.anidb[0] == aid
                    and x.id not in main_set
                )
                # R1.13b: with no level of its own in LCARS, the pieces of one AniDB
                # entry are one mini sub-season (undated ones included: their
                # placeholder numbers are in no run).
                members = sorted(
                    (x for x in side if x.anidb is not None and x.anidb[0] == aid
                     and x.id not in between and plan.numbers.get(x.id) is not None),
                    key=lambda x: plan.numbers[x.id])
                group: list[tuple[float, float]] = []
                for x in members:
                    add_to(group, plan.numbers[x.id])
                seasons_of = {inside.get(x.id) for x in members} - {None}
                if len(seasons_of) > 1:
                    _flag(plan, "anidb_group_spans_seasons", anidb_id=aid,
                          seasons=sorted(seasons_of))
                plan.anidb_groups[key] = (inside.get(members[0].id), group)
            continue
        # R1.13b: every episode belongs to a level. Minis inside a season's
        # run are that season's mini sub-season; anything else is a level of
        # its own (its own cover).
        season = inside.get(it.id)
        n = plan.numbers[it.id]
        if season and not _is_film(it) and (it.runtime or 0) < FULL_LENGTH_MINUTES:
            key = f"minis:{season}"
        else:
            key = f"piece:{it.tvdb_season}:{it.tvdb_episode}:{season or 0}"
        add_to(plan.level_spans.setdefault(key, []), n)
    return plan


# ── Database ────────────────────────────────────────────────────────────


def _external_id(conn, show_id: str, service: str) -> str | None:
    row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = ?",
        (show_id, service),
    ).fetchone()
    return row[0] if row else None


def load_show(conn: sqlite3.Connection, show_id: str) -> tuple[list[Item], str, set[int]]:
    """The show's episodes as engine items, the source that can number it
    (R1.18–19, else the `tvdb` fallback, R1.2d) and its seasons' AniDB ids."""
    show = conn.execute(
        "SELECT tracking_space FROM show WHERE id = ?", (show_id,)
    ).fetchone()
    rows = conn.execute(
        "SELECT id, COALESCE(sonarr_season, season) AS s, COALESCE(sonarr_episode, episode) AS e,"
        " air_date_utc, runtime_minutes FROM episode WHERE show_id = ?",
        (show_id,),
    ).fetchall()
    items = [Item(r[0], r[1], r[2], r[3], r[4]) for r in rows]
    if not items:
        return items, "tvdb", set()

    tvdb_id = _external_id(conn, show_id, "tvdb")
    if show[0] == "anime" and tvdb_id:
        entries = anidb._load_entries_for_tvdb(conn, tvdb_id)
        if entries:
            resolver = anidb._SeasonResolver(entries)
            fetched = {r[0] for r in conn.execute(
                "SELECT DISTINCT anidb_anime_id FROM anidb_episode WHERE anidb_epno > 0")}
            types = dict(conn.execute(
                "SELECT anidb_id, type FROM anidb_anime WHERE type IS NOT NULL"))
            confirmed = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
                "SELECT m.episode_id, m.anidb_anime_id, m.anidb_season, m.anidb_epno"
                " FROM episode_anidb_mapping m JOIN episode e ON e.id = m.episode_id"
                " WHERE e.show_id = ?", (show_id,))}
            airdates: dict[tuple, str] = {}
            complete = True
            for it in items:
                hit = confirmed.get(it.id) or resolver.resolve(it.tvdb_season, it.tvdb_episode)
                if hit is None or hit[0] not in fetched:
                    if it.tvdb_season > 0:
                        complete = False
                    continue
                it.anidb = (int(hit[0]), int(hit[1]), int(hit[2]))
                it.anidb_type = types.get(it.anidb[0])
                if not it.air:
                    if hit[0] not in {k[0] for k in airdates}:
                        for a, s, n, d in conn.execute(
                            "SELECT anidb_anime_id, anidb_season, anidb_epno, airdate"
                            " FROM anidb_episode WHERE anidb_anime_id = ?", (hit[0],)):
                            airdates[(a, s, n)] = d
                    d = airdates.get(it.anidb)
                    if d:
                        it.air = f"{d}T00:00:00Z"
            main_ids = {it.anidb[0] for it in items
                        if it.tvdb_season > 0 and it.anidb and it.anidb[1] == 1}
            return items, ("anidb" if complete else "tvdb"), main_ids
        return items, "tvdb", set()
    if show[0] == "tv":
        tvmaze_id = _external_id(conn, show_id, "tvmaze")
        if tvmaze_id and tvmaze_id.isdigit():
            have = {(r[0], r[1]) for r in conn.execute(
                "SELECT season, episode FROM tvmaze_episode WHERE tvmaze_show_id = ?",
                (int(tvmaze_id),))}
            mains = [(it.tvdb_season, it.tvdb_episode) for it in items if it.tvdb_season > 0]
            if mains and all(m in have for m in mains):
                return items, "tvmaze", set()
    return items, "tvdb", set()


def apply_plan(conn: sqlite3.Connection, plan: Plan) -> dict:
    """Writes a plan: absolute numbers, TVDB seasons' spans (and their
    `abs_start/abs_end` as min/max for old readers), the show's numbering
    source. A reconciliation (the show was numbered before) logs every
    changed number in `absolute_number_change`."""
    now = util.now_utc_iso()
    previous_source = conn.execute(
        "SELECT absolute_numbering_source FROM show WHERE id = ?", (plan.show_id,)
    ).fetchone()[0]
    changed = 0
    for episode_id, number in plan.numbers.items():
        old = conn.execute(
            "SELECT absolute_number FROM episode WHERE id = ?", (episode_id,)
        ).fetchone()[0]
        if old == number:
            continue
        conn.execute(
            "UPDATE episode SET absolute_number = ?, updated_at = ? WHERE id = ?",
            (number, now, episode_id),
        )
        changed += 1
        if previous_source is not None:
            conn.execute(
                "INSERT INTO absolute_number_change"
                " (episode_id, show_id, old_number, new_number, source, changed_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (episode_id, plan.show_id, old, number, plan.source, now),
            )
    missing_seasons = []
    for season_number, spans in plan.season_spans.items():
        row = conn.execute(
            "SELECT id FROM season WHERE show_id = ? AND season_number = ?"
            " AND kind = 'tvdb_season'",
            (plan.show_id, season_number),
        ).fetchone()
        if row is None:
            missing_seasons.append(season_number)
            continue
        conn.execute("DELETE FROM season_span WHERE season_id = ?", (row[0],))
        conn.executemany(
            "INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
            [(row[0], a, b) for a, b in spans],
        )
        conn.execute(
            "UPDATE season SET abs_start = ?, abs_end = ?, updated_at = ? WHERE id = ?",
            (min(a for a, _ in spans), max(b for _, b in spans), now, row[0]),
        )
    _apply_level_spans(conn, plan, now)
    _nest_in_minis_groups(conn, plan.show_id, now)
    conn.execute(
        "UPDATE show SET absolute_numbering_source = ? WHERE id = ?", (plan.source, plan.show_id)
    )
    return {"changed": changed, "missing_seasons": missing_seasons}


def _nest_in_minis_groups(conn, show_id: str, now: str) -> int:
    """R1.13c: a mini sits inside its "Season N minis" group, which is its parent level, so the
    group and its members are not siblings (and their spans, one inside the other, do not
    overlap as siblings). A member is a level of the same parent whose spans all lie inside the
    group's run (its first to its last number: the members sit in the gaps between its spans)."""
    nested = 0
    groups = conn.execute(
        "SELECT id, parent_id FROM season WHERE show_id = ? AND kind = 'special'"
        " AND label GLOB 'Season [0-9]* minis'",
        (show_id,),
    ).fetchall()
    for g in groups:
        gspans = conn.execute(
            "SELECT abs_from, abs_to FROM season_span WHERE season_id = ?", (g[0],)
        ).fetchall()
        if not gspans:
            continue
        gmin, gmax = min(a for a, _ in gspans), max(b for _, b in gspans)
        for m in conn.execute(
            "SELECT id FROM season WHERE show_id = ? AND id != ? AND kind = 'special'"
            " AND parent_id IS ? AND label IS NOT NULL AND label NOT GLOB 'Season [0-9]* minis'",
            (show_id, g[0], g[1]),
        ).fetchall():
            mine = conn.execute(
                "SELECT abs_from, abs_to FROM season_span WHERE season_id = ?", (m[0],)
            ).fetchall()
            if mine and all(gmin <= lo and hi <= gmax for lo, hi in mine):
                conn.execute(
                    "UPDATE season SET parent_id = ?, updated_at = ? WHERE id = ?",
                    (g[0], now, m[0]),
                )
                nested += 1
    return nested


def _anilist_ids_for_anidb(anidb_id: int) -> set[int] | None:
    """The AniList ids Fribb gives an AniDB entry; None when there is no dataset
    (those levels wait for the next pass — never read as "has no AniList id")."""
    try:
        dataset = fribb.load_dataset()
    except Exception:
        return None
    return {e["anilist_id"] for e in fribb.build_anidb_index(dataset).get(anidb_id, [])
            if e.get("anilist_id") not in (None, "")}


def _write_spans(conn, season_id: str, spans, now: str) -> None:
    conn.execute("DELETE FROM season_span WHERE season_id = ?", (season_id,))
    conn.executemany(
        "INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
        [(season_id, a, b) for a, b in spans],
    )
    conn.execute(
        "UPDATE season SET abs_start = ?, abs_end = ?, updated_at = ? WHERE id = ?",
        (min(a for a, _ in spans), max(b for _, b in spans), now, season_id),
    )


def _apply_level_spans(conn, plan: Plan, now: str) -> None:
    """Spans of the other levels: a part (an AniList/MAL cour) and a season-0
    piece with its own id get theirs by their AniDB entry (via Fribb); the
    side piece between two TVDB seasons (R1.8d, R1.9a) is its decimal season,
    created here when missing (one side piece → N.5)."""
    from lcars import status_rules

    for key, spans in plan.level_spans.items():
        if not spans:
            continue
        kind, *rest = key.split(":")
        if kind in ("part", "anidb"):
            ids = _anilist_ids_for_anidb(int(rest[-1]))
            if ids is None or (kind == "part" and not ids):
                continue
            row = None
            if ids:
                marks = ",".join("?" * len(ids))
                level = "part" if kind == "part" else "special"
                row = conn.execute(
                    f"SELECT id FROM season WHERE show_id = ? AND kind = ?"
                    f" AND anilist_id IN ({marks})",
                    (plan.show_id, level, *ids),
                ).fetchone()
            if row is not None:
                _write_spans(conn, row[0], spans, now)
            elif kind == "anidb" and key in plan.anidb_groups:
                season_number, group = plan.anidb_groups[key]
                _apply_anidb_group(conn, plan.show_id, int(rest[-1]), season_number, group, now)
        elif kind in ("minis", "piece"):
            _apply_own_level(conn, plan.show_id, kind, rest, spans, now)
        elif kind == "side":
            after = int(rest[0])
            row = None
            # A side piece with its own id (a mini-anime on AniList) is that
            # level — never a second one beside it.
            list_ids = set()
            for anidb_id in plan.side_anidb.get(key, ()):
                list_ids |= _anilist_ids_for_anidb(anidb_id) or set()
            if list_ids:
                marks = ",".join("?" * len(list_ids))
                row = conn.execute(
                    "SELECT id FROM season WHERE show_id = ? AND kind = 'special'"
                    f" AND anilist_id IN ({marks})",
                    (plan.show_id, *list_ids),
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE season SET decimal_season_number = COALESCE("
                        "decimal_season_number, ?) WHERE id = ?",
                        (after + 0.5, row[0]),
                    )
            if row is None:
                row = conn.execute(
                    "SELECT id FROM season WHERE show_id = ? AND kind = 'special'"
                    " AND parent_id IS NULL AND anilist_id IS NULL"
                    " AND decimal_season_number > ? AND decimal_season_number < ?",
                    (plan.show_id, after, after + 1),
                ).fetchone()
            if row is None:
                before = conn.execute(
                    "SELECT status FROM season WHERE show_id = ? AND kind = 'tvdb_season'"
                    " AND season_number = ?",
                    (plan.show_id, after),
                ).fetchone()
                status = ("skipped" if before is not None
                          and before[0] in status_rules.STOP_FOLLOWING else "planned")
                season_id = ids_module.generate_id(conn, "z")
                conn.execute(
                    "INSERT INTO season (id, show_id, season_number, kind, decimal_season_number,"
                    " label, source, status, list_sync, created_at, updated_at)"
                    " VALUES (?, ?, NULL, 'special', ?, ?, 'auto', ?, 0, ?, ?)",
                    (season_id, plan.show_id, after + 0.5, f"Side piece after season {after}",
                     status, now, now),
                )
                row = (season_id,)
            _write_spans(conn, row[0], spans, now)


def renumber_show(conn: sqlite3.Connection, show_id: str) -> Plan:
    """Plan and apply one show (the caller commits)."""
    items, source, main_ids = load_show(conn, show_id)
    plan = plan_show(show_id, items, source, main_anidb_ids=main_ids)
    apply_plan(conn, plan)
    return plan


def renumber_all(conn: sqlite3.Connection) -> dict:
    """Every tracked show — Memory Alpha's reconciliation pass (R1.2d)."""
    stats = {"shows": 0, "anidb": 0, "tvmaze": 0, "tvdb": 0}
    for (show_id,) in conn.execute("SELECT id FROM show WHERE tracked = 1").fetchall():
        plan = renumber_show(conn, show_id)
        stats["shows"] += 1
        stats[plan.source] += 1
    conn.commit()
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="lcars numbering", description="Memory Alpha numbering (dry run unless --apply)"
    )
    parser.add_argument("database")
    parser.add_argument("--show", action="append", help="show id (repeatable); default all tracked")
    parser.add_argument("--apply", action="store_true", help="write the result")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.database)
    conn.execute("PRAGMA foreign_keys = ON")
    show_ids = args.show or [r[0] for r in conn.execute("SELECT id FROM show WHERE tracked = 1")]
    out = []
    for show_id in show_ids:
        items, source, main_ids = load_show(conn, show_id)
        plan = plan_show(show_id, items, source, main_anidb_ids=main_ids)
        if args.apply:
            apply_plan(conn, plan)
        out.append(plan)
    if args.apply:
        conn.commit()
    if args.json:
        print(json.dumps([asdict(p) for p in out], ensure_ascii=False, indent=1))
    else:
        by_source: dict[str, int] = {}
        flags: dict[str, int] = {}
        for p in out:
            by_source[p.source] = by_source.get(p.source, 0) + 1
            for f in p.flags:
                flags[f["kind"]] = flags.get(f["kind"], 0) + 1
        print(f"{len(out)} shows by source: {by_source}")
        print(f"flags: {flags}")
    return 0


def _apply_own_level(conn, show_id: str, kind: str, rest: list[str], spans, now: str) -> None:
    """R1.13b: a season's mini sub-season, or one special's own level — found
    by its label, created when missing (status follows its season, R2.16)."""
    if kind == "minis":
        season_number = int(rest[0])
        label = f"Season {season_number} minis"
    else:
        tvdb_s, tvdb_e, season_number = (int(x) for x in rest)
        title = conn.execute(
            "SELECT title FROM episode WHERE show_id = ? AND COALESCE(sonarr_season, season) = ?"
            " AND COALESCE(sonarr_episode, episode) = ?",
            (show_id, tvdb_s, tvdb_e),
        ).fetchone()
        code = f"S{tvdb_s:02d}E{tvdb_e:02d}"
        label = code + (f" {title[0]}" if title and title[0] else "")
    # Found by the whole code (a piece is "S00E106 …", never a level whose code
    # merely starts the same: S00E10 …), a mini sub-season by its exact label.
    _write_spans(conn, _special_level(conn, show_id, season_number, label,
                                      label if kind == "minis" else code, now), spans, now)


def _special_level(conn, show_id: str, season_number, label: str, match: str, now: str) -> str:
    """The id-less `special` level labelled `match` (alone, or as the first word)
    under the TVDB season `season_number`, created when missing (R1.13b; its
    status follows the season before it, R2.16)."""
    from lcars import status_rules

    parent = None
    if season_number:
        parent = conn.execute(
            "SELECT id, status FROM season WHERE show_id = ? AND season_number = ?"
            " AND kind = 'tvdb_season'",
            (show_id, season_number),
        ).fetchone()
    row = conn.execute(
        "SELECT id FROM season WHERE show_id = ? AND kind = 'special' AND anilist_id IS NULL"
        " AND parent_id IS ? AND (label = ? OR substr(label, 1, ?) = ?)"
        " ORDER BY created_at, id",
        (show_id, parent[0] if parent else None, match, len(match) + 1, match + " "),
    ).fetchone()
    if row is None:
        # The level may sit one step deeper: `_nest_in_minis_groups` (R1.13c) moves a piece
        # into its "Season N minis" group after it is created. Looking only under the season
        # missed it, so every pass made a new copy (53 per pass on prod, 10-05); the code of a
        # piece ("S00E10") and the label of a group are unique per show, so any parent will do.
        row = conn.execute(
            "SELECT id FROM season WHERE show_id = ? AND kind = 'special' AND anilist_id IS NULL"
            " AND (label = ? OR substr(label, 1, ?) = ?) ORDER BY created_at, id",
            (show_id, match, len(match) + 1, match + " "),
        ).fetchone()
    if row is not None:
        return row[0]
    status = ("skipped" if parent is not None
              and parent[1] in status_rules.STOP_FOLLOWING else "planned")
    season_id = ids_module.generate_id(conn, "z")
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, kind, parent_id,"
        " decimal_season_number, label, source, status, list_sync, created_at, updated_at)"
        " VALUES (?, ?, NULL, 'special', ?, ?, ?, 'auto', ?, 0, ?, ?)",
        (season_id, show_id, parent[0] if parent else None,
         season_number if parent else 0.5, label, status, now, now),
    )
    return season_id


def _apply_anidb_group(conn, show_id: str, anidb_id: int, season_number, spans, now: str) -> None:
    """R1.13b: the pieces of one AniDB entry that LCARS holds no level for are that
    entry's mini sub-season, named after it (under the season they sit in)."""
    title = conn.execute(
        "SELECT main_title FROM anidb_anime WHERE anidb_id = ?", (anidb_id,)).fetchone()
    label = title[0] if title else f"AniDB {anidb_id}"
    _write_spans(conn, _special_level(conn, show_id, season_number, label, label, now),
                 spans, now)
