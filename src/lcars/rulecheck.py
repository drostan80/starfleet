"""Read-only rule check of an LCARS database against RULEBOOK.md (PLAN-CODE phase 1).

One check per rule (R#). It is the acceptance test for every later phase and
for the rebuilt database: the code and the data are done when it reports no
violation. It never writes: the database is opened read-only.

Two kinds of finding:
  - violation: the data breaks the rule as written;
  - check: the rule can't be decided from the database alone (e.g. R2.16
    depends on whether the user set the status), listed for review.

A rule whose data model isn't built yet (spans, levels, individual seasons,
PLAN-CODE phase 2) is reported as "not checkable yet" rather than passing.
Scope: tracked shows only; the skip list (untracked, status skipped) is the
browse/add filter and is only checked by R3.5.
"""

import argparse
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

SAMPLE_SIZE = 10
_TITLE = "COALESCE(sh.display_title_override, sh.title_english, sh.title_romaji, sh.title_native)"


@dataclass
class Finding:
    rule: str
    title: str
    kind: str  # "violation" | "check" | "not checkable yet"
    count: int = 0
    samples: list[str] = field(default_factory=list)
    note: str = ""


def open_readonly(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has(conn, table: str, column: str | None = None) -> bool:
    if column is None:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            is not None
        )
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _finding(conn, rule, title, kind, sql, params=(), fmt=None, note="") -> Finding:
    rows = conn.execute(sql, params).fetchall()
    fmt = fmt or (lambda r: " · ".join(str(v) for v in tuple(r)))
    return Finding(rule, title, kind, len(rows), [fmt(r) for r in rows[:SAMPLE_SIZE]], note)


def _not_yet(rule, title, note) -> Finding:
    return Finding(rule, title, "not checkable yet", note=note)


# ── §1 Database divisions ──────────────────────────────────────────────


def check_every_episode_numbered(conn):
    return _finding(
        conn,
        "R1.0",
        "Every episode has an absolute number",
        "violation",
        f"SELECT {_TITLE} AS show, e.season, e.episode FROM episode e"
        " JOIN show sh ON sh.id = e.show_id"
        " WHERE sh.tracked = 1 AND e.absolute_number IS NULL ORDER BY show, e.season, e.episode",
        fmt=lambda r: f"{r['show']} S{r['season']}E{r['episode']}",
    )


def check_season_zero_redistributed(conn):
    if _has(conn, "season_span"):
        return _finding(
            conn,
            "R1.8",
            "No episode left outside every season/level (season 0 redistributed)",
            "violation",
            f"SELECT {_TITLE} AS show, e.season, e.episode, e.absolute_number FROM episode e"
            " JOIN show sh ON sh.id = e.show_id WHERE sh.tracked = 1 AND NOT EXISTS ("
            "  SELECT 1 FROM season z JOIN season_span sp ON sp.season_id = z.id"
            "  WHERE z.show_id = e.show_id"
            "  AND e.absolute_number BETWEEN sp.abs_from AND sp.abs_to)",
            fmt=lambda r: f"{r['show']} S{r['season']}E{r['episode']} (abs {r['absolute_number']})",
        )
    return _finding(
        conn,
        "R1.8",
        "No episode left outside every season/level (season 0 redistributed)",
        "violation",
        f"SELECT {_TITLE} AS show, e.season, e.episode FROM episode e"
        " JOIN show sh ON sh.id = e.show_id"
        " WHERE sh.tracked = 1 AND e.season_id IS NULL ORDER BY show, e.season, e.episode",
        fmt=lambda r: f"{r['show']} S{r['season']}E{r['episode']}",
        note="Before phase 2: an episode with no season link (TVDB season 0 today).",
    )


def check_seasons_have_spans(conn):
    """R1.11 (amended 2026-09-29): a level has a span when it has episodes or a place in a
    series. A standalone movie and a list-only entry (no episodes, no number) are exempt."""
    if not _has(conn, "season_span"):
        return _not_yet("R1.11", "Every season is defined by its span(s)", "no span columns")
    return _finding(
        conn,
        "R1.11",
        "Every level with episodes has a span",
        "violation",
        f"SELECT {_TITLE} AS show, z.season_number, z.kind, z.label FROM season z"
        " JOIN show sh ON sh.id = z.show_id WHERE sh.tracked = 1"
        " AND NOT EXISTS (SELECT 1 FROM season_span sp WHERE sp.season_id = z.id)"
        " AND EXISTS (SELECT 1 FROM episode e WHERE e.season_id = z.id)",
        fmt=lambda r: f"{r['show']} S{r['season_number']} {r['kind']} {r['label'] or ''}".strip(),
    )


def check_unplaced_extras(conn):
    """The exempt levels of R1.11, listed as information: standalone movies, and a series'
    OVAs/specials/parts that only hold a list entry. A film of a series with no place is the
    one to look at."""
    if not _has(conn, "season_span"):
        return _not_yet("R1.11i", "Levels with no span", "no span columns")
    return _finding(
        conn,
        "R1.11i",
        "Levels with no span and no episodes (exempt): films of a series, for a look",
        "check",
        f"SELECT {_TITLE} AS show, z.kind, z.label, z.anilist_id FROM season z"
        " JOIN show sh ON sh.id = z.show_id WHERE sh.tracked = 1 AND sh.media_shape = 'episodic'"
        " AND z.kind IN ('special', 'individual_season') AND z.anilist_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM season_span sp WHERE sp.season_id = z.id)"
        " AND NOT EXISTS (SELECT 1 FROM episode e WHERE e.season_id = z.id)",
        fmt=lambda r: f"{r['show']} · {r['label'] or r['kind']} · anilist {r['anilist_id']}",
        note="exempt from R1.11; listed because a film with a place in a series needs a span",
    )


# Named exceptions of R1.12: shows numbered by Memory Alpha + Fribb as they are (TVDB ids).
R1_12_ACCEPTED_TVDB = {"102261": "Monogatari series (Memory Alpha + Fribb)"}


def check_spans_do_not_overlap(conn):
    if _has(conn, "season_span"):
        # Level-aware: a part sits inside its TVDB season (checked by
        # R1.10 below); levels side by side — same show, same parent, or
        # both without one — must not share an absolute number. Not overlaps
        # (R1.12/R1.13a/R1.13c, 2026-09-29): a special or film inside another
        # special's or film's span; the Monogatari series (named exception).
        accepted = ",".join("?" * len(R1_12_ACCEPTED_TVDB))
        return _finding(
            conn,
            "R1.12",
            "Spans of one level don't overlap",
            "violation",
            f"SELECT {_TITLE} AS show, a.decimal_season_number AS a_n, sa.abs_from AS a0,"
            " sa.abs_to AS a1, b.decimal_season_number AS b_n, sb.abs_from AS b0,"
            " sb.abs_to AS b1"
            " FROM season a JOIN season b ON b.show_id = a.show_id AND a.id < b.id"
            "  AND b.parent_id IS a.parent_id"
            " JOIN season_span sa ON sa.season_id = a.id"
            " JOIN season_span sb ON sb.season_id = b.id"
            " JOIN show sh ON sh.id = a.show_id"
            " WHERE sh.tracked = 1 AND sa.abs_from <= sb.abs_to AND sb.abs_from <= sa.abs_to"
            " AND NOT (a.kind = 'special' AND b.kind = 'special' AND"
            "  ((sa.abs_from <= sb.abs_from AND sb.abs_to <= sa.abs_to)"
            "   OR (sb.abs_from <= sa.abs_from AND sa.abs_to <= sb.abs_to)))"
            " AND NOT EXISTS (SELECT 1 FROM show_external_id t WHERE t.show_id = sh.id"
            f"  AND t.service = 'tvdb' AND t.external_id IN ({accepted}))",
            tuple(R1_12_ACCEPTED_TVDB),
            fmt=lambda r: (
                f"{r['show']} S{r['a_n']:g} ({r['a0']:g}–{r['a1']:g})"
                f" × S{r['b_n']:g} ({r['b0']:g}–{r['b1']:g})"
            ),
        )
    return _finding(
        conn,
        "R1.12",
        "Spans of different seasons of a show don't overlap",
        "violation",
        f"SELECT {_TITLE} AS show, a.season_number AS a_sn, a.abs_start AS a0, a.abs_end AS a1,"
        " b.season_number AS b_sn, b.abs_start AS b0, b.abs_end AS b1"
        " FROM season a JOIN season b ON b.show_id = a.show_id AND a.id < b.id"
        "  AND a.season_number <> b.season_number"
        " JOIN show sh ON sh.id = a.show_id"
        " WHERE sh.tracked = 1 AND a.abs_start IS NOT NULL AND b.abs_start IS NOT NULL"
        "  AND a.abs_start <= b.abs_end AND b.abs_start <= a.abs_end",
        fmt=lambda r: (
            f"{r['show']} S{r['a_sn']} ({r['a0']}–{r['a1']}) × S{r['b_sn']} ({r['b0']}–{r['b1']})"
        ),
    )


def check_parts_inside_their_season(conn):
    if not _has(conn, "season", "parent_id"):
        return _not_yet("R1.10", "Parts sit inside their TVDB season", "no levels yet")
    return _finding(
        conn,
        "R1.10",
        "Parts sit inside their TVDB season",
        "violation",
        f"SELECT {_TITLE} AS show, p.decimal_season_number AS n, c.label,"
        " sc.abs_from, sc.abs_to FROM season c"
        " JOIN season p ON p.id = c.parent_id"
        " JOIN season_span sc ON sc.season_id = c.id"
        " LEFT JOIN show sh ON sh.id = p.show_id"
        # Parts only (R2.18). Where a film/mini inside a season's window
        # sits is RULEBOOK §9 Q-R.
        " WHERE c.kind = 'part' AND COALESCE(sh.tracked, 1) = 1 AND NOT EXISTS ("
        "  SELECT 1 FROM season_span sp WHERE sp.season_id = p.id"
        "  AND sc.abs_from >= sp.abs_from AND sc.abs_to <= sp.abs_to)",
        fmt=lambda r: (
            f"{r['show']} S{r['n']:g} part {r['label'] or '?'}"
            f" ({r['abs_from']:g}–{r['abs_to']:g}) outside its season"
        ),
    )


def check_one_show_per_tvdb_id(conn):
    return _finding(
        conn,
        "R1.14",
        "One show per TVDB id",
        "violation",
        "SELECT x.external_id AS tvdb, GROUP_CONCAT(" + _TITLE + ", ' | ') AS shows"
        " FROM show_external_id x JOIN show sh ON sh.id = x.show_id"
        " WHERE x.service = 'tvdb' AND sh.tracked = 1"
        " GROUP BY x.external_id HAVING COUNT(*) > 1",
        fmt=lambda r: f"tvdb {r['tvdb']}: {r['shows']}",
    )


def check_list_ids_on_one_season(conn):
    """R1.22: an AniList/MAL id belongs to one level. One id on several TVDB seasons is right
    when their episodes add up to the entry's own count (Urusei Yatsura: 54 + 52 + 43 + 46 =
    195), which is what the list says the entry is."""
    if not _has(conn, "season_external_id"):
        return _not_yet(
            "R1.22", "Each AniList/MAL id belongs to one season", "no season_external_id"
        )
    return _finding(
        conn,
        "R1.22",
        "Each AniList/MAL id belongs to one season",
        "violation",
        "SELECT g.service, g.external_id, g.n FROM ("
        " SELECT sei.service, sei.external_id, COUNT(*) AS n, MAX(z.episode_total) AS total,"
        "  SUM((SELECT COUNT(*) FROM episode e WHERE e.season_id = z.id)) AS held"
        " FROM season_external_id sei JOIN season z ON z.id = sei.season_id"
        " JOIN show sh ON sh.id = z.show_id"
        " WHERE sh.tracked = 1 AND sei.service IN ('anilist', 'mal')"
        " GROUP BY sei.service, sei.external_id HAVING COUNT(*) > 1) g"
        " WHERE g.total IS NULL OR g.held != g.total",
        fmt=lambda r: f"{r['service']} {r['external_id']} on {r['n']} seasons",
    )


def check_list_ids_season_level(conn):
    return _finding(
        conn,
        "R1.23",
        "AniList/MAL ids only at season level, never on the show",
        "violation",
        f"SELECT {_TITLE} AS show, x.service, x.external_id FROM show_external_id x"
        " JOIN show sh ON sh.id = x.show_id"
        " WHERE sh.tracked = 1 AND x.service IN ('anilist', 'mal') ORDER BY show",
        fmt=lambda r: f"{r['show']}: {r['service']} {r['external_id']}",
    )


# ── §2 Status ──────────────────────────────────────────────────────────


def check_episode_watched_is_boolean(conn):
    return _finding(
        conn,
        "R2.2",
        "Episodes are watched or not (no 'skipped' episode)",
        "violation",
        f"SELECT {_TITLE} AS show, e.season, e.episode FROM episode e"
        " JOIN show sh ON sh.id = e.show_id WHERE e.state = 'skipped'",
        fmt=lambda r: f"{r['show']} S{r['season']}E{r['episode']}",
    )


def check_seasons_have_status(conn):
    return _finding(
        conn,
        "§2.2",
        "Every season of a tracked show has a status",
        "violation",
        f"SELECT {_TITLE} AS show, z.season_number FROM season z JOIN show sh ON sh.id = z.show_id"
        " WHERE sh.tracked = 1 AND z.season_number > 0 AND z.status IS NULL",
        fmt=lambda r: f"{r['show']} S{r['season_number']}",
    )


def check_show_status_derived(conn):
    rows = conn.execute(
        f"SELECT sh.id, {_TITLE} AS show, sh.status FROM show sh"
        " WHERE sh.tracked = 1 AND EXISTS (SELECT 1 FROM season z WHERE z.show_id = sh.id"
        "  AND z.season_number > 0)"
    ).fetchall()
    bad = []
    for r in rows:
        seasons = conn.execute(
            "SELECT status FROM season WHERE show_id = ? AND season_number > 0"
            " AND kind = 'tvdb_season' ORDER BY season_number"
            if _has(conn, "season", "kind")
            else "SELECT status FROM season WHERE show_id = ? AND season_number > 0"
            " ORDER BY season_number, part_number"
            if _has(conn, "season", "part_number")
            else "SELECT status FROM season WHERE show_id = ? AND season_number > 0"
            " ORDER BY season_number",
            (r["id"],),
        ).fetchall()
        statuses = [s["status"] for s in seasons]
        live = [s for s in statuses if s != "skipped"]
        expected = live[-1] if live else "skipped"
        if _has(conn, "show", "skip_picked") and conn.execute(
            "SELECT skip_picked FROM show WHERE id = ?", (r["id"],)
        ).fetchone()[0]:
            # R2.13b: skipped picked on the show.
            watched = {"watching", "completed", "paused", "dropped"}
            if not live or not watched & set(statuses):
                expected = "skipped"
            elif expected == "completed":
                expected = "dropped"
        if expected is not None and r["status"] != expected:
            bad.append(f"{r['show']}: show {r['status']}, last non-skipped season {expected}")
    return Finding(
        "R2.13",
        "Show status = its last non-skipped season's status",
        "violation",
        len(bad),
        bad[:SAMPLE_SIZE],
    )


def _season_episode_counts(conn):
    return (
        f"SELECT {_TITLE} AS show, z.season_number, z.status,"
        " COUNT(e.id) AS n, SUM(e.state = 'watched') AS w"
        " FROM season z JOIN show sh ON sh.id = z.show_id"
        " JOIN episode e ON e.show_id = z.show_id AND e.season = z.season_number"
        " WHERE sh.tracked = 1 AND z.season_number > 0"
        " GROUP BY z.id"
    )


def _level_counts(conn):
    """Per level (season, part, special) of a tracked show: (id, show, label, status, episodes,
    watched) counted the way the engine counts them — the level's own episodes by its spans
    (R1.11–R1.13), not every episode of its TVDB season number."""
    from lcars import status_rules

    if not _has(conn, "season_span"):
        return []
    out = []
    for lvl in conn.execute(
            f"SELECT z.*, {_TITLE} AS show_title FROM season z JOIN show sh ON sh.id = z.show_id"
            " WHERE sh.tracked = 1").fetchall():
        eps = status_rules.level_episodes(conn, lvl)
        if not eps:
            continue
        label = (f"S{lvl['season_number']}" if lvl["season_number"] is not None
                 else lvl["label"] or lvl["kind"])
        if lvl["kind"] == "part":
            label += f" part {lvl['part_number']}"
        out.append((lvl, lvl["show_title"], label, len(eps),
                    sum(1 for e in eps if e["state"] == "watched")))
    return out


def check_planned_with_watched_episode(conn):
    title = "A planned season with an episode watched is watching"
    bad = [f"{show} {label} ({w}/{n} watched)" for lvl, show, label, n, w in _level_counts(conn)
           if lvl["status"] == "planned" and 0 < w < n]
    return Finding("R2.14", title, "violation", len(bad), bad[:SAMPLE_SIZE])


def check_all_watched_is_completed(conn):
    """R2.15 with R2.15a: a level with every episode watched is completed — when its
    episode count is confirmed (an unconfirmed one, an airing season, stays watching)."""
    from lcars import status_rules

    title = "A season with every episode watched is completed (its count confirmed, R2.15a)"
    bad = []
    for lvl, show, label, n, w in _level_counts(conn):
        if w != n or (lvl["status"] or "") in ("completed", "skipped"):
            continue
        if "episode_total" not in lvl.keys() or status_rules.episode_count_confirmed(conn, lvl, n):
            bad.append(f"{show} {label} is {lvl['status']} ({w}/{n})")
    return Finding("R2.15", title, "violation", len(bad), bad[:SAMPLE_SIZE])


def check_completed_has_all_watched(conn):
    title = "A completed season has every episode watched"
    bad = [f"{show} {label} ({w}/{n} watched)" for lvl, show, label, n, w in _level_counts(conn)
           if lvl["status"] == "completed" and w < n]
    return Finding("R2.7", title, "violation", len(bad), bad[:SAMPLE_SIZE])


def check_after_paused_dropped_skipped(conn):
    return _finding(
        conn,
        "R2.16",
        "Seasons after a paused/dropped/skipped season are skipped",
        "check",
        f"SELECT {_TITLE} AS show, prev.season_number AS p_sn, prev.status AS p_st,"
        " z.season_number, z.status FROM season z JOIN show sh ON sh.id = z.show_id"
        " JOIN season prev ON prev.show_id = z.show_id AND prev.season_number = ("
        "  SELECT MAX(season_number) FROM season WHERE show_id = z.show_id"
        "  AND season_number < z.season_number AND season_number > 0)"
        " WHERE sh.tracked = 1 AND prev.status IN ('paused', 'dropped', 'skipped')"
        "  AND z.status IN ('planned')",
        fmt=lambda r: f"{r['show']} S{r['p_sn']} {r['p_st']} → S{r['season_number']} {r['status']}",
        note="A violation only when the later season was auto-added; a status you set stands.",
    )


def check_skipped_not_on_lists(conn):
    if not _has(conn, "list_baseline"):
        return _not_yet("R2.10", "Skipped seasons are not on AniList/MAL", "no list_baseline table")
    return _finding(
        conn,
        "R2.10",
        "Skipped seasons are not on AniList/MAL",
        "violation",
        f"SELECT {_TITLE} AS show, z.season_number, lb.service, lb.external_id FROM season z"
        " JOIN show sh ON sh.id = z.show_id"
        " JOIN season_external_id sei ON sei.season_id = z.id AND sei.service IN ('anilist', 'mal')"
        " JOIN list_baseline lb ON lb.service = sei.service"
        "  AND lb.external_id = CAST(sei.external_id AS INTEGER)"
        " WHERE z.status = 'skipped' AND lb.status IS NOT NULL",
        fmt=lambda r: f"{r['show']} S{r['season_number']} on {r['service']} {r['external_id']}",
    )


# ── §3 Adding ──────────────────────────────────────────────────────────


def check_tracked_shows_have_tvdb(conn):
    return _finding(
        conn,
        "R3.2",
        "Every tracked series is linked to a TVDB id (or is an individual season)",
        "violation",
        f"SELECT {_TITLE} AS show, sh.tracking_space FROM show sh"
        " WHERE sh.tracked = 1 AND sh.media_shape = 'episodic' AND NOT EXISTS ("
        "  SELECT 1 FROM show_external_id x WHERE x.show_id = sh.id"
        "  AND x.service IN ('tvdb', 'tvdb_movie'))"  # a film's TVDB movie id is its TVDB link
        " ORDER BY show",
        fmt=lambda r: f"{r['show']} ({r['tracking_space']})",
    )


def check_no_stub_shows(conn):
    return _finding(
        conn,
        "R3.5",
        "No untracked stub shows (only the skip list is untracked)",
        "violation",
        f"SELECT {_TITLE} AS show, sh.status FROM show sh"
        " WHERE sh.tracked = 0 AND sh.status <> 'skipped' ORDER BY show",
        fmt=lambda r: f"{r['show']} ({r['status']})",
    )


# ── Added 2026-10-05 (audit): rules decidable from the database that had no check ──────────


def check_one_level_per_label(conn):
    """R1.13b/c — a special level (a piece, a "Season N minis" group, an AniDB mini sub-season) is
    found by its label, so it exists once per show and parent. Memory Alpha made a new copy of every
    piece nested in a minis group on every pass (53 a pass, ~3,450 a day, 09-30 → 10-05) and nothing
    in the rulecheck noticed. Id-less levels only: one made beside a level holding an AniList id
    (AniDB data not there yet) is a one-off, not this."""
    return _finding(
        conn,
        "R1.13b",
        "A special level exists once per label under one parent",
        "violation",
        f"SELECT {_TITLE} AS show, z.label, COUNT(*) AS copies FROM season z"
        " JOIN show sh ON sh.id = z.show_id"
        " WHERE sh.tracked = 1 AND z.kind = 'special' AND z.label IS NOT NULL"
        " AND z.anilist_id IS NULL AND z.mal_id IS NULL"
        " GROUP BY z.show_id, IFNULL(z.parent_id, ''), z.label HAVING COUNT(*) > 1"
        " ORDER BY copies DESC, show",
        fmt=lambda r: f"{r['show']} · {r['label']} × {r['copies']}",
    )


def check_episode_in_exactly_one_level(conn):
    """R1.13b/c — every episode belongs to a level: the deepest level whose span holds its number.
    None is R1.8 (above); this is the other way wrong: two levels that are not parent and child
    both hold it, so it is in two places. A "check", not a violation: the rulebook only nests a
    mini in its group when every span lies inside the group's run (R1.13c)."""
    if not _has(conn, "season_span"):
        return _not_yet("R1.13c", "Every episode sits in exactly one level", "no span columns")
    return _finding(
        conn,
        "R1.13c",
        "Episodes held by two levels at once (a parent and its child aside), for a look",
        "check",
        f"SELECT {_TITLE} AS show, e.season, e.episode, e.absolute_number,"
        " COUNT(DISTINCT z.id) AS levels FROM episode e"
        " JOIN show sh ON sh.id = e.show_id"
        " JOIN season z ON z.show_id = e.show_id"
        " JOIN season_span sp ON sp.season_id = z.id"
        "  AND e.absolute_number BETWEEN sp.abs_from AND sp.abs_to"
        " WHERE sh.tracked = 1 AND NOT EXISTS (SELECT 1 FROM season c"
        "  JOIN season_span cs ON cs.season_id = c.id WHERE c.parent_id = z.id"
        "  AND e.absolute_number BETWEEN cs.abs_from AND cs.abs_to)"
        " GROUP BY e.id HAVING COUNT(DISTINCT z.id) > 1 ORDER BY show, e.absolute_number",
        fmt=lambda r: (
            f"{r['show']} S{r['season']}E{r['episode']} (abs {r['absolute_number']:g})"
            f" in {r['levels']} levels"
        ),
        note="R1.13c nests a mini in its group only when all its spans lie in the group's run; "
             "a film level and the minis group often both hold the same special.",
    )


def check_decimal_numbers(conn):
    """R1.2b (and R1.8d): the items in one gap after whole number N are N.1, N.2, … ; one alone is
    N.5; ten or more take hundredths (N.01, N.02, …). Placeholders (5000 and up) are R1.0a's."""
    gaps: dict = {}
    for row in conn.execute(
        f"SELECT e.show_id, {_TITLE} AS show, e.absolute_number AS a FROM episode e"
        " JOIN show sh ON sh.id = e.show_id WHERE sh.tracked = 1"
        " AND e.absolute_number IS NOT NULL AND e.absolute_number < 5000"
    ):
        whole = int(row["a"] + 1e-9)
        fraction = round(row["a"] - whole, 4)
        if fraction > 0:
            gaps.setdefault((row["show_id"], row["show"], whole), []).append(fraction)
    bad = []
    for (_show_id, show, whole), fractions in gaps.items():
        fractions.sort()
        n = len(fractions)
        expected = ([0.5] if n == 1 else
                    [round(i / 10, 4) for i in range(1, n + 1)] if n < 10 else
                    [round(i / 100, 4) for i in range(1, n + 1)])
        if fractions != expected:
            bad.append(f"{show} after {whole}: {', '.join(f'{whole + f:g}' for f in fractions[:6])}"
                       f" (expected {', '.join(f'{whole + f:g}' for f in expected[:6])})")
    bad.sort()
    return Finding("R1.2b", "Numbers between two whole numbers follow .1, .2… (one alone .5)",
                   "violation", len(bad), bad[:SAMPLE_SIZE])


def check_placeholder_numbers(conn):
    """R1.0a — a placeholder number (5000 and up) is for an episode with no air date yet."""
    return _finding(
        conn,
        "R1.0a",
        "A placeholder number (5000+) only for an episode with no air date",
        "violation",
        f"SELECT {_TITLE} AS show, e.season, e.episode, e.absolute_number, e.air_date_utc"
        " FROM episode e JOIN show sh ON sh.id = e.show_id"
        " WHERE sh.tracked = 1 AND e.absolute_number >= 5000 AND e.air_date_utc IS NOT NULL"
        " ORDER BY show, e.season, e.episode",
        fmt=lambda r: (f"{r['show']} S{r['season']}E{r['episode']} abs {r['absolute_number']:g}"
                       f" but dated {r['air_date_utc']}"),
    )


def check_undated_have_placeholders(conn):
    """R1.0a, the other way: an episode with no air date takes a placeholder. TV episodes that
    TVDB/TVmaze order but have not dated keep a real number today — listed to look at, not a
    violation, until you say which way the rule reads for them."""
    return _finding(
        conn,
        "R1.0a-b",
        "Episodes with no air date that hold a real number, not a placeholder",
        "check",
        f"SELECT {_TITLE} AS show, e.season, e.episode, e.absolute_number FROM episode e"
        " JOIN show sh ON sh.id = e.show_id"
        " WHERE sh.tracked = 1 AND e.air_date_utc IS NULL AND e.absolute_number < 5000"
        " ORDER BY show, e.season, e.episode",
        fmt=lambda r: f"{r['show']} S{r['season']}E{r['episode']} abs {r['absolute_number']:g}",
        note="Mostly TV episodes numbered from TVDB order with no date yet.",
    )


def check_parts_in_span_order(conn):
    """R1.10 (order) — a TVDB season's parts are numbered in the order of their spans (part 1 is
    the one that starts first). A part with no span yet cannot be placed and is left out."""
    if not _has(conn, "season_span"):
        return _not_yet("R1.10b", "Parts are numbered in span order", "no span columns")
    parts: dict = {}
    for row in conn.execute(
        f"SELECT p.parent_id, {_TITLE} AS show, p.part_number AS pn,"
        " (SELECT MIN(abs_from) FROM season_span s WHERE s.season_id = p.id) AS start,"
        " (SELECT MAX(abs_to) FROM season_span s WHERE s.season_id = p.id) AS finish,"
        " (SELECT season_number FROM season z WHERE z.id = p.parent_id) AS sn"
        " FROM season p JOIN show sh ON sh.id = p.show_id"
        " WHERE p.kind = 'part' AND sh.tracked = 1 ORDER BY p.parent_id, p.part_number"
    ):
        parts.setdefault(row["parent_id"], []).append(row)
    bad = []
    for group in parts.values():
        if any(p["start"] is None for p in group):
            continue
        by_span = [p["pn"] for p in sorted(group, key=lambda p: (p["start"], p["pn"]))]
        if by_span != sorted(p["pn"] for p in group):
            now = ", ".join(f"{p['pn']}={p['start']:g}–{p['finish']:g}" for p in group)
            bad.append(f"{group[0]['show']} S{group[0]['sn']}: {now}")
    bad.sort()
    return Finding("R1.10b", "Parts are numbered in the order of their spans", "violation",
                   len(bad), bad[:SAMPLE_SIZE])


def check_levels_follow_episodes(conn):
    """R1.10a — an AniList/MAL entry sits where its episodes are: a TVDB-season level that holds
    a list id but has no episode — and is not the one season right after TVDB's last (a season
    it doesn't have yet) — is a level made from an order and not from the episodes (Durarara!!'s
    "S4" when TVDB stops at 2). Kusuriya's "S4" looks like the next season; the entry-count
    check R1.11w shows it (S3 holds 24 episodes for a 12-episode entry)."""
    return _finding(
        conn, "R1.10a", "A level holding a list id has episodes, or is TVDB's next season",
        "violation",
        f"SELECT {_TITLE} AS show, z.season_number AS season, z.anilist_id AS anilist_id"
        " FROM season z JOIN show sh ON sh.id = z.show_id"
        " WHERE z.kind = 'tvdb_season' AND sh.tracked = 1 AND z.anilist_id IS NOT NULL"
        " AND z.status != 'skipped' AND z.season_number > 0"
        " AND z.season_number != (SELECT COALESCE(MAX(e.season), 0) + 1 FROM episode e"
        "   WHERE e.show_id = z.show_id AND e.season > 0)"
        " AND NOT EXISTS (SELECT 1 FROM episode e WHERE e.show_id = z.show_id"
        "   AND (e.season_id = z.id OR e.season = z.season_number))"
        " ORDER BY show, season",
        fmt=lambda r: f"{r['show']} S{r['season']}: AniList {r['anilist_id']}",
        note="The entry belongs to a part of the TVDB season its episodes sit in "
             "(level_reconcile.py places it; or use the show page's part button).",
    )


def check_dated_have_a_source(conn):
    """R1.0b — every stored air date records which source it came from (the schedule chooser and
    the earliest-wins rule both read it)."""
    return _finding(
        conn,
        "R1.0b",
        "Every dated episode has an air-date source",
        "violation",
        f"SELECT {_TITLE} AS show, e.season, e.episode FROM episode e"
        " JOIN show sh ON sh.id = e.show_id"
        " WHERE sh.tracked = 1 AND e.air_date_utc IS NOT NULL AND e.air_date_source IS NULL"
        " ORDER BY show, e.season, e.episode",
        fmt=lambda r: f"{r['show']} S{r['season']}E{r['episode']}",
    )


def check_anilist_count_vs_level(conn):
    """R1.11w — the episode count of a level's AniList entry (`episode_total`) against the
    episodes the level holds by its spans. Information only: it is what the width sweep used to
    put on the review page (173 reviews, none actionable from there). A difference is normal for
    a special, a season LCARS has not fully fetched, or an airing show; one id over several
    seasons is R1.22's."""
    if not _has(conn, "season_span"):
        return _not_yet("R1.11w", "AniList count vs the level's episodes", "no span columns")
    return _finding(
        conn,
        "R1.11w",
        "A level whose AniList episode count differs from the episodes it holds, for a look",
        "check",
        f"SELECT {_TITLE} AS show, z.season_number, z.part_number, z.kind, z.episode_total,"
        " (SELECT COUNT(DISTINCT e.id) FROM episode e JOIN season_span sp ON sp.season_id = z.id"
        "  AND e.absolute_number BETWEEN sp.abs_from AND sp.abs_to"
        "  WHERE e.show_id = z.show_id AND e.kind = 'regular') AS held"
        " FROM season z JOIN show sh ON sh.id = z.show_id"
        " WHERE sh.tracked = 1 AND z.kind IN ('tvdb_season', 'part') AND z.anilist_id IS NOT NULL"
        " AND z.episode_total IS NOT NULL AND z.status != 'skipped'"
        " AND EXISTS (SELECT 1 FROM season_span sp WHERE sp.season_id = z.id)"
        " AND (SELECT COUNT(*) FROM season o WHERE o.show_id = z.show_id"
        "  AND o.anilist_id = z.anilist_id AND o.status != 'skipped') = 1"
        " AND held <> z.episode_total ORDER BY show, z.season_number, z.part_number",
        fmt=lambda r: (f"{r['show']} S{r['season_number']}"
                       + (f" part {r['part_number']}" if r["kind"] == "part" else "")
                       + f": AniList {r['episode_total']}, holds {r['held']}"),
    )


CHECKS: list[Callable[[sqlite3.Connection], Finding]] = [
    check_every_episode_numbered,
    check_season_zero_redistributed,
    check_seasons_have_spans,
    check_unplaced_extras,
    check_spans_do_not_overlap,
    check_parts_inside_their_season,
    check_one_show_per_tvdb_id,
    check_list_ids_on_one_season,
    check_list_ids_season_level,
    check_episode_watched_is_boolean,
    check_seasons_have_status,
    check_show_status_derived,
    check_planned_with_watched_episode,
    check_all_watched_is_completed,
    check_completed_has_all_watched,
    check_after_paused_dropped_skipped,
    check_skipped_not_on_lists,
    check_tracked_shows_have_tvdb,
    check_no_stub_shows,
    check_one_level_per_label,
    check_episode_in_exactly_one_level,
    check_decimal_numbers,
    check_placeholder_numbers,
    check_undated_have_placeholders,
    check_parts_in_span_order,
    check_levels_follow_episodes,
    check_dated_have_a_source,
    check_anilist_count_vs_level,
]

NOT_IN_DATABASE = (
    "Not checked here (need the live services or a dry run, PLAN-CODE phase 9): "
    "Sonarr monitoring R5.x, AniList/MAL mirroring R4.x."
)


def run(conn) -> list[Finding]:
    return [check(conn) for check in CHECKS]


def format_text(findings: list[Finding]) -> str:
    lines = []
    for f in findings:
        if f.kind == "not checkable yet":
            lines.append(f"[ … ] {f.rule:6} {f.title} — not checkable yet: {f.note}")
            continue
        mark = "ok " if f.count == 0 else ("!! " if f.kind == "violation" else " ? ")
        lines.append(f"[{mark}] {f.rule:6} {f.title} — {f.count}")
        if f.count and f.note:
            lines.append(f"         {f.note}")
        for s in f.samples:
            lines.append(f"         · {s}")
    violations = sum(f.count for f in findings if f.kind == "violation")
    lines.append("")
    lines.append(
        f"{violations} violations, {sum(f.count for f in findings if f.kind == 'check')} to check."
    )
    lines.append(NOT_IN_DATABASE)
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lcars rulecheck", description=__doc__.splitlines()[0])
    parser.add_argument("database", help="path to an LCARS SQLite file (opened read-only)")
    parser.add_argument("--json", action="store_true", help="print findings as JSON")
    args = parser.parse_args(argv)
    findings = run(open_readonly(args.database))
    print(
        json.dumps([asdict(f) for f in findings], indent=1, ensure_ascii=False)
        if args.json
        else format_text(findings)
    )
    return 1 if any(f.kind == "violation" and f.count for f in findings) else 0
