"""animeschedule.net air-date reconciliation — SCOPE.md §5.2/§6.7,
BUILD_PLAN.md B.5.

A global sweep, not a per-show due-query — unlike B.1/B.4's `dueFor
MetadataRefresh`/`fetch_and_populate` shape, `animeschedule_client`'s
own docstring explains why: the raw RSS feed is a small rolling window
covering every currently-airing anime worldwide, not just LCARS's own
tracked shows, and rotates faster than a day. So the whole feed is
fetched and matched against LCARS's own candidate set every call,
same "one call covers everything, no per-item argument" shape B.3's
`poll_file_availability` already established — `pollAnimeSchedule`
rides Ops's existing hourly tick (scheduler.py's `run_daily_and_
weekly_once`) rather than getting a new interval.

Candidate shows: `status = 'watching'` and actively airing (the same
predicate `resolvers._show_is_airing` uses, deliberately re-implemented
here rather than imported — resolvers.py already imports this module,
so the reverse import would be circular) and `tracking_space = 'anime'`
— animeschedule.net is an anime-only catalog, no point matching a `tv`
show's title against it.

Matching reuses `fuzzy.best_match()` (§5.4, A.7) verbatim, same
threshold, same "return None rather than guess" philosophy — an RSS
item title is treated as the single target, every candidate show's
title variant as the pool, exactly the shape that function already
handles. **Confirmed 2026-08-09 (B.5)**: restricting the candidate
pool to watching+airing shows only (not every tracked show) was the
user's own explicit design call ("keeping the search to tracked shows
should elucidate the vast majority of cases") — most of the global
feed's volume is shows LCARS doesn't track at all, and `best_match`
returning `None` for those is the expected, silent common case, not
logged or flagged.

Once a show is confidently matched, resolving *which* episode row the
item's episode number refers to still needs disambiguation a single
title match can't give: a show can have more than one season, only
one of which is actually airing right now. Restricting to that show's
own currently-airing season(s) before matching on episode number
mirrors `_show_is_airing`'s own per-episode predicate, applied at
season grain. Exactly one candidate episode row -> apply. Zero or
more-than-one -> **flag, don't guess** (`pending_review`, per the
user's own "simply flagging those which aren't clear will suffice") —
the same show-level `pending_review` shape `metadata._guarded` (A.8)
already uses for its own "couldn't cleanly resolve this" case
(`field = "metadata_fetch"`), here `field =
"animeschedule_episode_match"`. This is a deliberate coverage limit,
not just disambiguation: an already-fully-released season (no episode
left with a null/future date) never matches here even if the feed
reports one of its dates — B.4's own AniList reconciliation already
covers already-aired corrections; missing one here is preferred over
risking a misapplied write across a cour boundary.

Every automatic write is idempotent (re-seeing an already-correct
value is a genuine no-op, not logged) — but the raw feed's own rolling
window means the *same* item is re-processed on the order of a dozen
times before it rotates out (module docstring above), so the flagged
path guards separately: `_flag()` only calls `pending_review.
open_or_extend` when the finding actually differs from the review's
own last chain entry, otherwise it's treated as "unchanged" too. §5.6's
value-chain accumulation exists to record "the automatic source
changed its mind again," not "the same feed item was re-read on an
hourly tick" — a real bug caught in review, before this shipped, not a
hypothetical.

**Manual-override exemption, confirmed 2026-08-09 (B.5)** — the
question §6.7 explicitly left open when B.4 built AniList's own hard
manual-date gate: animeschedule.net **does** override
`air_date_source = 'manual'`, per the user's own B.4 reasoning restated
and confirmed directly for this case ("the overwrite and log is
intended for when a new information about an air date is logged...
those data are likely to come from animeschedule"). Every such
overwrite still opens/extends a `pending_review` — visible and
auditable, never silent — same as every other automatic write this
project makes.
"""

import json
import logging

from lcars import animeschedule_client, fuzzy, pending_review, util

logger = logging.getLogger("lcars.animeschedule")


def poll_anime_schedule(conn) -> dict:
    """The `pollAnimeSchedule` mutation's own implementation. Returns
    `{"episodes_updated": int, "flagged": int}`. Best-effort: a feed
    fetch failure is logged and treated as a zero-result sweep (retried
    next tick), never raised — same reasoning `availability.py`'s own
    module docstring gives for a service being unreachable."""
    candidates = _candidate_shows(conn)
    if not candidates:
        return {"episodes_updated": 0, "flagged": 0}

    try:
        items = animeschedule_client.fetch_raw_feed()
    except animeschedule_client.AnimeScheduleError:
        logger.exception("animeschedule: feed fetch failed, will retry next sweep")
        return {"episodes_updated": 0, "flagged": 0}

    variant_to_show_id: dict[str, str] = {}
    for show in candidates:
        for title in (show["title_romaji"], show["title_english"], show["title_native"]):
            if title:
                variant_to_show_id[title] = show["id"]
    all_variants = list(variant_to_show_id)

    episodes_updated = 0
    flagged = 0
    for item in items:
        best = fuzzy.best_match([item["title"]], all_variants)
        if best is None:
            continue  # not one of our tracked, watching+airing anime shows
        show_id = variant_to_show_id[best]
        outcome = _apply_or_flag(conn, show_id, item)
        if outcome == "updated":
            episodes_updated += 1
        elif outcome == "flagged":
            flagged += 1
    if episodes_updated or flagged:
        conn.commit()
    return {"episodes_updated": episodes_updated, "flagged": flagged}


def _candidate_shows(conn) -> list[dict]:
    shows = conn.execute(
        "SELECT * FROM show WHERE status = 'watching' AND tracking_space = 'anime'"
    ).fetchall()
    return [dict(s) for s in shows if _show_is_airing(conn, s["id"])]


def _show_is_airing(conn, show_id: str) -> bool:
    """Same predicate as `resolvers._show_is_airing` — deliberately
    re-implemented, not imported, to avoid a circular import (module
    docstring)."""
    row = conn.execute(
        "SELECT 1 FROM episode"
        " WHERE show_id = ? AND (air_date_utc IS NULL OR air_date_utc > ?) LIMIT 1",
        (show_id, util.now_utc_iso()),
    ).fetchone()
    return row is not None


def _airing_seasons(conn, show_id: str) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT season FROM episode"
        " WHERE show_id = ? AND (air_date_utc IS NULL OR air_date_utc > ?)",
        (show_id, util.now_utc_iso()),
    ).fetchall()
    return [r["season"] for r in rows]


def _apply_or_flag(conn, show_id: str, item: dict) -> str:
    """Returns "updated", "flagged", or "unchanged" (an already-correct
    value, or an already-flagged finding, re-seen this sweep — not
    counted either way, same idempotent no-op shape
    `metadata._reconcile_air_dates`, B.4, already uses)."""
    seasons = _airing_seasons(conn, show_id)
    if not seasons:
        # Matched by title, but this show has no currently-airing season
        # right now — genuinely unclear (not "nothing to do"; module
        # docstring's dead-branch note explains why this flags rather
        # than silently no-ops), same treatment as the ambiguous-match
        # case below.
        return _flag(
            conn,
            show_id,
            item,
            "matched a tracked show by title but that show currently has no "
            "airing season at all — needs a human to confirm this is real",
        )
    placeholders = ",".join("?" for _ in seasons)
    matches = conn.execute(
        f"SELECT id, air_date_utc FROM episode"
        f" WHERE show_id = ? AND episode = ? AND season IN ({placeholders})",
        (show_id, item["episode"], *seasons),
    ).fetchall()
    if len(matches) != 1:
        return _flag(
            conn,
            show_id,
            item,
            f"matched {len(matches)} candidate episode row(s) across this show's "
            "currently-airing season(s) — needs a human to confirm which",
        )

    episode_row = matches[0]
    if episode_row["air_date_utc"] == item["air_date_utc"]:
        return "unchanged"

    pending_review.open_or_extend(
        conn,
        "episode",
        episode_row["id"],
        "air_date_utc",
        "animeschedule",
        episode_row["air_date_utc"],
        item["air_date_utc"],
    )
    conn.execute(
        "UPDATE episode SET air_date_utc = ?, air_date_source = 'animeschedule',"
        " updated_at = ? WHERE id = ?",
        (item["air_date_utc"], util.now_utc_iso(), episode_row["id"]),
    )
    return "updated"


def _flag(conn, show_id: str, item: dict, reason: str) -> str:
    """The ambiguous-match write path — `pending_review.open_or_extend`,
    but only when `reason` genuinely differs from the entry's own last
    chain entry. Without this guard, the raw feed's own rolling window
    (module docstring) would re-append the identical finding on every
    hourly re-sweep of the same still-in-window item, turning §5.6's
    "the automatic source changed its mind again" value-chain into a
    dozen copies of "the same feed item was re-read" — caught in
    review, before this shipped."""
    message = (
        f'episode {item["episode"]} of "{item["title"]}" reported released '
        f"{item['air_date_utc']}: {reason}"
    )
    if _last_chain_entry(conn, "show", show_id, "animeschedule_episode_match") == message:
        return "unchanged"
    pending_review.open_or_extend(
        conn, "show", show_id, "animeschedule_episode_match", "animeschedule", None, message
    )
    return "flagged"


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
