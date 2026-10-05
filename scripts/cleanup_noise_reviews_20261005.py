#!/usr/bin/env python3
"""One-time cleanup of review-page noise (2026-10-05): open reviews that nothing can be answered
on, or that later work made obsolete. Each rule below names a class, resolves its members with
a `resolution_note` saying why, and leaves everything else open. The sources that opened them
were changed in the same release (v0.4.1) so they do not come back.

Rules (an open review is resolved when ...):
  conflict      an anilist/mal id "conflict" whose levels are all of ONE show (one entry over
                several TVDB seasons — Urusei Yatsura — is not a conflict)
  width         field season_subdivision (the AniList width sweep: 173 items, none actionable;
                the comparison is now `lcars rulecheck` R1.11w)
  unmatched     a season "anilist_id" review from Fribb whose value is "unmatched" (a TVDB
                season with no AniList entry is normal: information)
  airdate       an air_date_utc review from AniList ("a delay past the current…" on a
                downloaded episode) or from animeschedule (a change that was applied)
  transient     an anilist_push / mal_push / metadata_fetch review whose every message is a
                network blip or rate limit ("Could not connect…", "HTTP 429")
  scheme        an episode-numbering "scheme" review (LCARS owns its numbering, R1.2a)
  nomatch       a show-level "no AniList match for tvdb id" review (AniList ids live on seasons)
  clamp         a list read-back "holds progress X (LCARS N)" where X is the entry's own
                episode count (a service never holds more than that)
  ambiguous     an animeschedule "matched N candidate episode rows" review
Then every open chain is de-duplicated (consecutive identical entries collapsed).

Usage (labelled snapshot first):
    python3 cleanup_noise_reviews_20261005.py                 # dry run (default)
    python3 cleanup_noise_reviews_20261005.py --apply
    python3 cleanup_noise_reviews_20261005.py --db /path/to/lcars.db
"""

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime

DEFAULT_DB = "/db/lcars.db"
TRANSIENT = ("could not connect", "http 429", "http 5", "timed out", "timeout",
             "temporary failure", "connection")


def _open(conn, where: str, params=()):
    return conn.execute(
        "SELECT id, entity_type, entity_id, field, source, previous_value, proposed_value_chain"
        f" FROM pending_review WHERE resolved_at IS NULL AND {where}", params
    ).fetchall()


def _chain(row) -> list[str]:
    return json.loads(row["proposed_value_chain"])


def classify(conn) -> dict[str, list]:
    """rule name -> the open reviews it would resolve."""
    found: dict[str, list] = {}

    # conflict: every level behind the id is one show
    conflict = []
    for row in _open(conn, "field IN ('anilist_id_conflict', 'mal_id_conflict')"):
        service = "anilist" if row["field"].startswith("anilist") else "mal"
        level = conn.execute("SELECT id FROM season WHERE id = ?", (row["entity_id"],)).fetchone()
        if level is None:
            continue
        ext = conn.execute(
            "SELECT external_id FROM season_external_id WHERE season_id = ? AND service = ?",
            (row["entity_id"], service)).fetchone()
        if ext is None:
            continue
        shows = {r[0] for r in conn.execute(
            "SELECT z.show_id FROM season_external_id sei JOIN season z ON z.id = sei.season_id"
            " WHERE sei.service = ? AND sei.external_id = ?", (service, ext[0]))}
        if len(shows) == 1 and None not in shows:
            conflict.append(row)
    found["conflict"] = conflict

    found["width"] = _open(conn, "field = 'season_subdivision' AND source = 'anilist_width_check'")
    found["unmatched"] = [
        r for r in _open(conn, "field = 'anilist_id' AND source = 'fribb'"
                               " AND entity_type = 'season'") if _chain(r) == ["unmatched"]]
    found["airdate"] = [
        r for r in _open(conn, "field = 'air_date_utc'")
        if r["source"] == "animeschedule"
        or (r["source"] == "anilist" and "a delay past the current" in " ".join(_chain(r)))]
    found["transient"] = [
        r for r in _open(conn, "field IN ('anilist_push', 'mal_push', 'metadata_fetch')")
        if all(any(m in msg.lower() for m in TRANSIENT) for msg in _chain(r))]
    found["scheme"] = _open(conn, "field = 'scheme'")
    found["nomatch"] = [
        r for r in _open(conn, "field = 'anilist_id' AND source = 'fribb' AND entity_type = 'show'")
        if _chain(r) and _chain(r)[0].startswith("no AniList match for tvdb id")]
    clamp = []
    for r in _open(conn, "field = 'list_readback_differs'"):
        m = re.search(r"holds progress (\d+) \(LCARS (\d+)\)", " ".join(_chain(r)))
        total = conn.execute("SELECT episode_total FROM season WHERE id = ?",
                             (r["entity_id"],)).fetchone()
        if m and total and total[0] and int(m.group(1)) == min(int(m.group(2)), total[0]):
            clamp.append(r)
    found["clamp"] = clamp
    found["ambiguous"] = _open(conn, "field = 'animeschedule_episode_match'")
    return found


NOTES = {
    "conflict": "one list entry over several seasons of one show — not a conflict (v0.4.0)",
    "width": "AniList width sweep: not actionable here; now a rulecheck item (R1.11w)",
    "unmatched": "a TVDB season with no AniList entry is normal — information only",
    "airdate": "air-date noise (a delay on a downloaded episode, or an applied automatic change)",
    "transient": "a network blip / rate limit, retried automatically — nothing to answer",
    "scheme": "LCARS owns its numbering (R1.2a): the Sonarr scheme is no longer reviewed",
    "nomatch": "AniList ids live on seasons (R1.23); a show without one is information only",
    "clamp": "the service holds at most the entry's own episode count (expected clamp)",
    "ambiguous": "animeschedule match ambiguity is logged, no longer reviewed",
}


def run(conn, apply: bool) -> dict:
    found = classify(conn)
    result = {"counts": {k: len(v) for k, v in found.items()}, "applied": apply,
              "open_before": conn.execute(
                  "SELECT COUNT(*) FROM pending_review WHERE resolved_at IS NULL").fetchone()[0]}
    deduped = 0
    if apply:
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with conn:
            for rule, rows in found.items():
                for row in rows:
                    conn.execute(
                        "UPDATE pending_review SET resolved_at = ?, resolution_note = ?"
                        " WHERE id = ? AND resolved_at IS NULL",
                        (now, f"cleanup 2026-10-05 ({rule}): {NOTES[rule]}", row["id"]))
            for row in _open(conn, "1 = 1"):
                chain = _chain(row)
                collapsed = [v for i, v in enumerate(chain) if i == 0 or v != chain[i - 1]]
                if collapsed != chain:
                    conn.execute("UPDATE pending_review SET proposed_value_chain = ? WHERE id = ?",
                                 (json.dumps(collapsed), row["id"]))
                    deduped += 1
    result["chains_deduplicated"] = deduped
    result["open_after"] = conn.execute(
        "SELECT COUNT(*) FROM pending_review WHERE resolved_at IS NULL").fetchone()[0]
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--apply", action="store_true", help="resolve (default: dry run)")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    result = run(conn, args.apply)
    print(("APPLIED" if result["applied"] else "DRY RUN (nothing written)")
          + f": open {result['open_before']} -> "
          + (str(result["open_after"]) if result["applied"] else
             str(result["open_before"] - sum(result["counts"].values())) + " (would be)"))
    for rule, n in result["counts"].items():
        print(f"  {rule:10} {n}")
    if result["applied"]:
        print(f"  chains de-duplicated: {result['chains_deduplicated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
