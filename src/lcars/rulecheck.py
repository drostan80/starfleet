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
    if _has(conn, "season_span"):
        sql = (
            f"SELECT {_TITLE} AS show, z.season_number FROM season z"
            " JOIN show sh ON sh.id = z.show_id WHERE sh.tracked = 1"
            " AND NOT EXISTS (SELECT 1 FROM season_span sp WHERE sp.season_id = z.id)"
        )
    elif _has(conn, "season", "abs_start"):
        sql = (
            f"SELECT {_TITLE} AS show, z.season_number FROM season z"
            " JOIN show sh ON sh.id = z.show_id WHERE sh.tracked = 1 AND z.season_number > 0"
            " AND (z.abs_start IS NULL OR z.abs_end IS NULL)"
        )
    else:
        return _not_yet("R1.11", "Every season is defined by its span(s)", "no span columns")
    return _finding(
        conn,
        "R1.11",
        "Every season is defined by its span(s)",
        "violation",
        sql,
        fmt=lambda r: f"{r['show']} S{r['season_number']}",
    )


def check_spans_do_not_overlap(conn):
    if _has(conn, "season_span"):
        # Level-aware: a part sits inside its TVDB season (checked by
        # R1.10 below); levels side by side — same show, same parent, or
        # both without one — must not share an absolute number.
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
            " WHERE sh.tracked = 1 AND sa.abs_from <= sb.abs_to AND sb.abs_from <= sa.abs_to",
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
    if not _has(conn, "season_external_id"):
        return _not_yet(
            "R1.22", "Each AniList/MAL id belongs to one season", "no season_external_id"
        )
    return _finding(
        conn,
        "R1.22",
        "Each AniList/MAL id belongs to one season",
        "violation",
        "SELECT sei.service, sei.external_id, COUNT(*) AS n FROM season_external_id sei"
        " JOIN season z ON z.id = sei.season_id JOIN show sh ON sh.id = z.show_id"
        " WHERE sh.tracked = 1 AND sei.service IN ('anilist', 'mal')"
        " GROUP BY sei.service, sei.external_id HAVING COUNT(*) > 1",
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


def check_planned_with_watched_episode(conn):
    return _finding(
        conn,
        "R2.14",
        "A planned season with an episode watched is watching",
        "violation",
        f"SELECT * FROM ({_season_episode_counts(conn)})"
        " WHERE status = 'planned' AND w > 0 AND w < n",
        fmt=lambda r: f"{r['show']} S{r['season_number']} ({r['w']}/{r['n']} watched)",
    )


def check_all_watched_is_completed(conn):
    return _finding(
        conn,
        "R2.15",
        "A season with every episode watched is completed",
        "violation",
        f"SELECT * FROM ({_season_episode_counts(conn)}) WHERE n > 0 AND w = n"
        " AND COALESCE(status, '') <> 'completed'",
        fmt=lambda r: f"{r['show']} S{r['season_number']} is {r['status']} ({r['w']}/{r['n']})",
        note="Before phase 2 counted per TVDB season number; per level (part, special) after.",
    )


def check_completed_has_all_watched(conn):
    return _finding(
        conn,
        "R2.7",
        "A completed season has every episode watched",
        "violation",
        f"SELECT * FROM ({_season_episode_counts(conn)}) WHERE status = 'completed' AND w < n",
        fmt=lambda r: f"{r['show']} S{r['season_number']} ({r['w']}/{r['n']} watched)",
    )


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
        "  SELECT 1 FROM show_external_id x WHERE x.show_id = sh.id AND x.service = 'tvdb')"
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


CHECKS: list[Callable[[sqlite3.Connection], Finding]] = [
    check_every_episode_numbered,
    check_season_zero_redistributed,
    check_seasons_have_spans,
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
