"""One-time/repeatable show backfill — SCOPE.md §5.1/§5.2, BUILD_PLAN.md
B.11d, grown out of B.11's own reconnaissance (2026-08-10): LCARS's
`show` table was found completely empty (nothing had ever called
`addShow` for real), and switching Data's calendar render path (B.11f)
to LCARS-tracked shows as-is would blank the calendar entirely on day
one.

`preview_backfill()` (the dry-run Query) calls
`local_audit.find_untracked_shows_readonly()` — genuinely read-only,
no writes at all, matching every other Query's own side-effect-free
contract (same reasoning `exportData`'s own docstring gives).
`backfill_untracked_shows()` (the real Mutation) calls the fuller
`local_audit.audit_local_files()` instead, getting its own
reconciliation/orphan-walk side effects as a bonus — both share the
exact same untracked-id-comparison logic underneath
(`_untracked_sonarr_entries`/`_untracked_radarr_entries`,
local_audit.py), not two divergent implementations, so the candidate
list itself matches between a preview and the real run that follows
it (modulo whatever changed in Sonarr/Radarr in between the two
calls — an inherent, accepted gap in any "preview now, run later"
workflow).

**A third source, folded in the same day it was found missing**: the
Sonarr/Radarr sweep above only ever discovers shows Sonarr/Radarr
already know about. A show tracked on the user's own AniList list but
never added to Sonarr at all (watched via streaming, or before this
library existed) was invisible to it entirely — the user caught this
live, asking directly whether AniList-only shows would also get
picked up. Confirmed with the user: yes, every list status, folded
into this same step rather than a separate one. `_find_untracked_anilist_entries()`
below is that third source.

Per-item, this is a single `shows.create_show()` call (which is
already the *whole* `addShow` mutation body — insert, external-id
links, the inline A.8 metadata fetch: episodes, AniList link
resolution via Fribb, Fribb season reconciliation, all best-effort).
There is no separate metadata-fetch phase to sequence.

**Sonarr classification corrected the same day, live-verified against
the user's real library**: originally classified `trackingSpace` from
Sonarr's own `seriesType` flag (`== "anime"`), mirroring
metadata.py's numbering-scheme heuristic (A.22). The very first real
`previewShowBackfill` run against the user's actual Sonarr library
showed this was wrong for most of it — Frieren, DAN DA DAN, Chainsaw
Man, SPY x FAMILY, Kaiju No. 8, and dozens more all report
`seriesType: standard`, not `anime`. A `tracking_space='tv'`
misclassification isn't just cosmetic: `fetch_and_populate` only ever
calls `_ensure_anilist_link` when `tracking_space == "anime"`, so a
misclassified anime show would never get its mandated AniList link at
all (§5.1). Fixed by using a **successful Fribb tvdb->anilist
resolution** as the actual anime signal instead — the same resolution
`_ensure_anilist_link` would perform anyway once the show exists, just
done here first so `trackingSpace` (and the AniList link itself,
passed straight through as `anilistId` on the `addShow`-equivalent
input) are both correct from the very first write, and so this same
resolution can also feed the AniList-sweep dedup below without a
second, divergent Fribb pass.

**AniList-sweep dedup, two layers**: (1) an AniList list entry already
linked to an existing LCARS show — checked via
`local_audit.known_anilist_ids()`, which reads *both*
`show_external_id` (show-level) and `season.anilist_id` (per-season,
§5.5) rather than only the former, since a split-cour sequel season's
own AniList link lives at the season level. (2) an AniList entry whose
own id Fribb resolves for *any real season* of *any* show in Sonarr's
own catalog at all (`_sonarr_resolvable_anilist_ids()`, checking every
season `local_audit.all_sonarr_series_with_seasons()` reports, tracked
or not — corrected the same day from an earlier season-1-only version
after a live check showed Fribb resolves Frieren's own season 1 and
season 2 to two different AniList ids) — Fribb groups a franchise's
several AniList-side season splits under one tvdb id, so treating each
split as its own independent "untracked show" here would create a
real duplicate the moment its sibling season arrives via the Sonarr-
sourced path instead; that tvdb id is Sonarr's own sweep's job (or
LCARS's, if already tracked), never this one's. This dedup is **not
perfect** — a real, accepted residual gap found live: an AniList
entry whose id Fribb's own dataset has no mapping for at all (three of
the user's own real Frieren-titled entries, alternate-cour splits
Fribb doesn't carry under that tvdb id) still surfaces as "untracked."
Closing that fully would need fuzzy title matching across sources
(B.7's own service_presence.py scope, not this one's) — a known,
documented limitation, not silently promised away.
`format: MUSIC` entries are excluded outright (not a real "show" at
all — same exclusion `ANIME_RELATION_FORMATS`, A.21, already
establishes for relation edges); a null `format` defaults to
`episodic` (safe default, not a strong enough signal to skip the entry
entirely).

**Third guard, added after the real live run — a write-time check, not
another read-side dedup layer.** The two layers above are both
snapshots taken once before this module's own loop starts; neither can
see a stub `shows.create_show()`'s own inline metadata fetch creates
*mid-loop* (an AniList `relations` walk, A.21) for an id a *later*
candidate in the same fixed list also targets. Live-confirmed: 86
`anilist_id` collision pairs from the real backfill run, each one a
`tracked = false` relation stub and a separately-created real show
both pointing at the same AniList id. Fixed at the actual write
boundary instead — `shows.create_show()` now checks `show_external_id`
live immediately before inserting (`shows.find_existing_show`) and
promotes a matching stub in place (SCOPE.md §5.1's own documented
promotion path) rather than creating a second row. See
`backfill_untracked_shows()`'s own docstring below for the `promoted`
vs `created` split this added to the result shape.

Throttled between anime-classified adds (only those trigger AniList
calls) to stay inside AniList's 30 req/min budget — the same budget
Data's own anilist.py docstring cites as the reason its own `P`
(manual AniList sync) is deliberately not automatic. Scaled by season
count (see `_anilist_call_estimate()`) rather than a flat per-show
sleep — caught in review: a flat 2s sleep assumed one AniList call per
show, but `_fetch_anilist` (1 call) plus `_reconcile_air_dates` (1
call *per season*, its own B.4 docstring) plus this module's own
status-seed read means even a single-season show makes 3 calls, not
1. An AniList-sweep-sourced show already knows its own status (the
same `MediaListCollection` call that found it) and skips the status-
seed's own live read entirely — one fewer AniList call than a
Sonarr/Fribb-resolved anime show needs.

Idempotent/resumable by construction, no bespoke resume-state needed:
re-running this after a partial run (interrupted, rate-limited,
crashed partway) only ever sees still-untracked items, since
local_audit's own known-id lookups already exclude anything a
previous run already added.

Radarr-sourced items default to `trackingSpace: TV` (documented cut,
confirmed with the user 2026-08-10) — Radarr gives no anime signal at
all, unlike Sonarr's own Fribb-resolvability. Misclassification is
fixable per-show after the fact, not worth a second live lookup just
to classify one field.

Watched-progress is deliberately NOT backfilled (confirmed with the
user 2026-08-10) — AniList's `mediaListEntry.progress` is an absolute
episode count with no clean, general mapping onto LCARS's own
per-season numbering (the whole absolute-vs-season_episode scheme,
A.22/A.25, exists precisely because that mapping isn't uniform show to
show). Consequence, stated plainly rather than silently absorbed: a
backfilled `watching` show starts with every episode unwatched, so
`Query.backlog` (§6.3) will over-report for it until the user marks
progress by hand.
"""

import time

from lcars import anilist_client, fribb, ids, local_audit, shows, util
from lcars.config import get_current

# AniList's own 30 req/min budget is exactly 2.0s/call — a small margin
# above that rather than the bare minimum, so a genuinely-timed call
# right at the boundary doesn't tip over it.
ANILIST_SECONDS_PER_CALL = 2.1

# The reverse of resolvers.py's own _STATUS_TO_ANILIST (A.9's push-
# direction map) — REPEATING has no direct target there either (LCARS
# has no rewatch-specific status), so this maps it back onto watching:
# actively watching it again is still watching, the closest real
# meaning available.
_ANILIST_STATUS_TO_SHOW_STATUS = {
    "CURRENT": "watching",
    "PLANNING": "planned",
    "PAUSED": "paused",
    "COMPLETED": "completed",
    "DROPPED": "dropped",
    "REPEATING": "watching",
}

# AniList's own `format` enum, mapped onto LCARS's two-value
# MediaShape. Every value in ANIME_RELATION_FORMATS (A.21,
# anilist_client.py) except MUSIC, which module docstring's own note
# explains is excluded outright rather than mapped at all.
_ANILIST_FORMAT_TO_MEDIA_SHAPE = {
    "TV": "episodic",
    "TV_SHORT": "episodic",
    "SPECIAL": "episodic",
    "OVA": "episodic",
    "ONA": "episodic",
    "MOVIE": "movie",
}


def _fribb_tvdb_index() -> dict:
    return fribb.build_tvdb_index(fribb.load_dataset())


def _classify_sonarr(entry: dict, tvdb_index: dict) -> dict:
    """See module docstring's own "Sonarr classification corrected"
    note — a successful Fribb resolution is the anime signal, not
    Sonarr's own seriesType. Checks *every* real season Sonarr reports
    for `trackingSpace` (a show whose season 1 doesn't resolve but a
    later season does is still anime — live-verified: this genuinely
    happens), but the show-level `anilistId` passed to create_show()
    stays season-1-only, matching `_ensure_anilist_link`'s own
    established convention exactly (a season-1 miss there just leaves
    the show unlinked at creation, same as a normal addShow without
    one — not this function's job to diverge from that)."""
    season_numbers = entry.get("season_numbers") or [1]
    season_one_anilist_id, _mal_id = fribb.extract_ids(
        fribb.resolve_season_candidate(tvdb_index, entry["external_id"], 1)
    )
    is_anime = season_one_anilist_id is not None or any(
        fribb.extract_ids(fribb.resolve_season_candidate(tvdb_index, entry["external_id"], n))[0]
        is not None
        for n in season_numbers
        if n != 1
    )
    return {
        "media_shape": "episodic",
        "tracking_space": "anime" if is_anime else "tv",
        "title_romaji": entry["title"],
        "primary_title": "romaji",
        "tvdb_id": entry["external_id"],
        "anilist_id": season_one_anilist_id,
    }


def _classify_radarr(entry: dict) -> dict:
    # See module docstring's own "no anime signal" note.
    return {
        "media_shape": "movie",
        "tracking_space": "tv",
        "title_romaji": entry["title"],
        "primary_title": "romaji",
        "tmdb_id": entry["external_id"],
    }


def _classify_anilist(entry: dict) -> dict:
    return {
        "media_shape": _ANILIST_FORMAT_TO_MEDIA_SHAPE.get(entry["format"], "episodic"),
        "tracking_space": "anime",
        "title_romaji": entry["title"],
        "primary_title": "romaji",
        "anilist_id": entry["external_id"],
    }


def _classify(entry: dict, tvdb_index: dict) -> dict:
    """One candidate entry (local_audit's own untracked_shows shape,
    or this module's own AniList-sweep shape) to a
    shows.create_show()-ready input dict."""
    if entry["service"] == "sonarr":
        return _classify_sonarr(entry, tvdb_index)
    if entry["service"] == "radarr":
        return _classify_radarr(entry)
    return _classify_anilist(entry)


def _sonarr_resolvable_anilist_ids(conn, tvdb_index: dict) -> set[int]:
    """Every AniList id Fribb can resolve for *any* real season of
    *any* show in Sonarr's own catalog (tracked or not) — see
    all_sonarr_series_with_seasons's own docstring (local_audit.py)
    for why every season needs checking, not just season 1 (Frieren's
    own season 1 and season 2 resolve to two different AniList ids,
    verified live)."""
    ids: set[int] = set()
    for series in local_audit.all_sonarr_series_with_seasons(conn):
        for season_number in series["season_numbers"]:
            anilist_id, _mal_id = fribb.extract_ids(
                fribb.resolve_season_candidate(tvdb_index, series["tvdb_id"], season_number)
            )
            if anilist_id is not None:
                ids.add(anilist_id)
    return ids


def _find_untracked_anilist_entries_by_source(conn, tvdb_index: dict) -> dict:
    """Same computation as _find_untracked_anilist_entries() below, but
    also reports whether AniList actually succeeded this pass — B.11e
    follow-up, same reasoning
    local_audit.find_untracked_shows_readonly_by_source()'s own
    docstring gives: a bare `except AniListError: return []` is
    indistinguishable from "genuinely nothing untracked" to a caller
    that needs to decide whether an *absence* means "resolved" or
    "couldn't check." No access token configured reports success
    (`True`) — deliberate, stable, safe to prune against; a real
    AniListError against a configured token reports `False`.
    _find_untracked_anilist_entries() itself stays a thin wrapper
    around this, unchanged for every existing caller."""
    cfg = get_current()
    if not cfg.anilist_access_token:
        return {"entries": [], "reported": True}
    try:
        my_list = anilist_client.fetch_my_anime_list(cfg.anilist_access_token)
    except anilist_client.AniListError:
        return {"entries": [], "reported": False}

    known_anilist_ids = local_audit.known_anilist_ids(conn)
    sonarr_resolvable_ids = _sonarr_resolvable_anilist_ids(conn, tvdb_index)

    entries = []
    for item in my_list:
        if item["format"] == "MUSIC":
            continue
        if str(item["anilist_id"]) in known_anilist_ids:
            continue
        if item["anilist_id"] in sonarr_resolvable_ids:
            continue
        entries.append(
            {
                "service": "anilist",
                "title": item["title"],
                "external_id": item["anilist_id"],
                "format": item["format"],
                "status": item["status"],
            }
        )
    return {"entries": entries, "reported": True}


def _find_untracked_anilist_entries(conn, tvdb_index: dict) -> list[dict]:
    """See module docstring's own "AniList-sweep dedup" note for the
    full reasoning. Read-only (one MediaListCollection fetch, plus
    Sonarr catalog reads for the dedup set below, no writes) — safe to
    call from both preview_backfill() and backfill_untracked_shows().

    Not a perfect dedup — a real, accepted residual gap found live: an
    AniList list entry whose title/id Fribb's own dataset simply has
    no mapping for at all (verified live: three of the user's own real
    Frieren-titled entries, alternate-cour splits Fribb doesn't carry
    under that tvdb id) will still surface here as "untracked" even
    though a same-franchise show already exists via the Sonarr path.
    Closing that fully would need fuzzy title matching across sources
    (B.7's own service_presence.py scope, not this one's) — documented
    as a known limitation rather than silently promised away. Thin
    wrapper around _find_untracked_anilist_entries_by_source() above
    (B.11e follow-up) — every existing caller here only ever needed
    the flat list."""
    return _find_untracked_anilist_entries_by_source(conn, tvdb_index)["entries"]


def preview_backfill_with_status(conn) -> dict:
    """Same computation as preview_backfill() below, but also reports
    which sources (sonarr/radarr/anilist) actually succeeded this
    pass — B.11e follow-up, a real bug found in review:
    untracked_sweep.py's own pruning needs this to tell "genuinely
    nothing untracked from this source" apart from "couldn't reach it
    this time" (see local_audit.find_untracked_shows_readonly_by_source's
    own docstring and _find_untracked_anilist_entries_by_source's own,
    above, for the full reasoning). preview_backfill() itself stays a
    thin wrapper around this — every existing caller there only ever
    needed the flat list."""
    tvdb_index = _fribb_tvdb_index()
    sonarr_radarr = local_audit.find_untracked_shows_readonly_by_source(conn)
    anilist = _find_untracked_anilist_entries_by_source(conn, tvdb_index)
    candidates = sonarr_radarr["entries"] + anilist["entries"]
    reported_services = set(sonarr_radarr["reported"])
    if anilist["reported"]:
        reported_services.add("anilist")

    preview = []
    for entry in candidates:
        classification = _classify(entry, tvdb_index)
        preview.append(
            {
                "service": entry["service"],
                "title": entry["title"],
                "external_id": entry["external_id"],
                "path": entry.get("path"),
                "tracking_space": classification["tracking_space"],
                "media_shape": classification["media_shape"],
            }
        )
    return {"items": preview, "reported_services": reported_services}


def preview_backfill(conn) -> list[dict]:
    """Dry-run: what backfill_untracked_shows() below would create —
    genuinely read-only (local_audit.find_untracked_shows_readonly()
    plus this module's own _find_untracked_anilist_entries(), neither
    of which write anything), run this first, always (the same
    report-only-before-you-act shape auditLocalFiles's own
    untracked_shows already has).

    `path` (B.11e) rides along on every item — Sonarr/Radarr entries
    carry the real one (`entry.get("path")`, same field
    `_untracked_sonarr_entries`/`_untracked_radarr_entries`,
    local_audit.py, already populate), an AniList-sweep entry has none
    (`None`, it was never in Sonarr/Radarr at all). Not currently
    exposed on `BackfillPreviewItem`'s own GraphQL shape (Ariadne only
    reads the fields a type actually declares, so this is a safe,
    additive superset) — `untracked_sweep.py`'s own sweep is the first
    real consumer, persisting it onto `untracked_show_finding.path`.

    Thin wrapper around preview_backfill_with_status() above (B.11e
    follow-up) — every existing caller here only ever needed the flat
    list, never the per-source status."""
    return preview_backfill_with_status(conn)["items"]


def _anilist_call_estimate(conn, show_id: str) -> int:
    """Roughly how many AniList calls this show's own create_show()
    (plus this module's own status-seed, when it needs a live read)
    likely just made: 1 for _fetch_anilist, 1 per season for
    _reconcile_air_dates (metadata.py's own B.4 docstring: "calls this
    once per season, not once per show"), 1 for _seed_status_from_anilist's
    own read when it needs one. Not a precise instrumentation of
    metadata.py's internals — a cheap, conservative proxy computed from
    what create_show() already wrote (the season table), used only to
    scale this module's own throttle sleep. At least 1 season assumed
    even if none were written yet (a Sonarr-fetch failure, an
    AniList-sweep-sourced show with no seasons yet, or a not-yet-aired
    show with no seasons resolved) — never throttles less than the
    single-season case, which slightly over-throttles an AniList-sweep
    show (no status-seed call needed there) rather than under-throttle
    it."""
    row = conn.execute("SELECT COUNT(*) AS n FROM season WHERE show_id = ?", (show_id,)).fetchone()
    season_count = max(row["n"] if row else 0, 1)
    return 2 + season_count


def backfill_untracked_shows(conn) -> dict:
    """The real run — one shows.create_show() per untracked Sonarr/
    Radarr item (local_audit.audit_local_files()'s own untracked_shows,
    not the read-only preview variant — this is a Mutation, its own
    reconciliation/orphan-walk side effects are a legitimate bonus, see
    module docstring) plus every still-untracked AniList-sweep entry.
    Returns {"created": [...], "promoted": [...], "failed": [...]} — a
    failure on one item never stops the rest of the run, same best-
    effort philosophy every other multi-item pass in this codebase
    already follows (metadata.py's own _guarded(), local_audit's own
    per-service try/except).

    **`promoted` split out from `created`, B.11d follow-up, real bug
    found in the live run**: `candidates` here is one fixed list,
    snapshotted before this loop starts. Nothing stops an *earlier*
    entry's own `shows.create_show()` call (its inline metadata fetch
    walks AniList `relations`, auto-creating `tracked = false` stub
    shows for ones LCARS has never seen — A.21) from creating a stub
    for an id a *later* entry in this same candidates list also
    targets — dedup computed once up front (`known_anilist_ids`,
    `_sonarr_resolvable_anilist_ids`) can't see writes this loop itself
    makes. `shows.create_show()` now checks live at the point of
    writing instead (`shows.find_existing_show`) and promotes the stub
    in place rather than creating a second row for it — the real fix;
    this split is just honest reporting of which one happened, since a
    silent "created" for what was actually a merge is exactly the kind
    of thing that made the original duplicate-row bug hard to see in
    the first place (confirmed live: 86 anilist_id collision pairs,
    "Mebius Dust" the first one traced down)."""
    tvdb_index = _fribb_tvdb_index()
    result = local_audit.audit_local_files(conn)
    candidates = result["untracked_shows"] + _find_untracked_anilist_entries(conn, tvdb_index)
    created = []
    promoted = []
    failed = []
    for entry in candidates:
        classification = _classify(entry, tvdb_index)
        already_existed = shows.find_existing_show(conn, classification) is not None
        try:
            show_id = shows.create_show(conn, classification)
        except shows.ShowInputError as e:
            failed.append({"service": entry["service"], "title": entry["title"], "error": str(e)})
            continue
        record = {"show_id": show_id, "service": entry["service"], "title": entry["title"]}
        (promoted if already_existed else created).append(record)
        if classification["tracking_space"] == "anime":
            known_status = entry.get("status") if entry["service"] == "anilist" else None
            _seed_status_from_anilist(conn, show_id, known_status=known_status)
            calls = _anilist_call_estimate(conn, show_id)
            if known_status is None:
                time.sleep(calls * ANILIST_SECONDS_PER_CALL)
            else:
                time.sleep((calls - 1) * ANILIST_SECONDS_PER_CALL)  # no live status read made
    return {"created": created, "promoted": promoted, "failed": failed}


def _seed_status_from_anilist(conn, show_id: str, known_status: str | None = None) -> None:
    """Best-effort, never raises past this function — a failed/skipped
    seed leaves the show at its create_show()-default 'planned', same
    "worth retrying by hand, never blocks the rest of the run"
    treatment every other best-effort branch in this codebase gets.

    `known_status` (AniList's own raw enum string, e.g. "CURRENT")
    skips the live fetch_my_list_status() read entirely — the AniList
    sweep already knows this from the same MediaListCollection call
    that found the show in the first place; only a Sonarr/Fribb-
    resolved anime show needs the live read."""
    cfg = get_current()
    if not cfg.anilist_access_token:
        return
    row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'anilist'",
        (show_id,),
    ).fetchone()
    if row is None:
        return  # no AniList link resolved (Fribb miss, or genuinely no match) — nothing to seed
    if known_status is not None:
        anilist_status = known_status
    else:
        try:
            anilist_status = anilist_client.fetch_my_list_status(
                cfg.anilist_access_token, int(row["external_id"])
            )
        except anilist_client.AniListError:
            return
        if anilist_status is None:
            return  # not on the viewer's AniList list at all — the 'planned' default stands
    new_status = _ANILIST_STATUS_TO_SHOW_STATUS.get(anilist_status)
    if new_status is None:
        return

    previous = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE show SET status = ?, updated_at = ? WHERE id = ?", (new_status, now, show_id)
    )
    conn.execute(
        "INSERT INTO status_change"
        " (id, show_id, previous_status, new_status, changed_at, changed_by)"
        " VALUES (?, ?, ?, ?, ?, 'show_backfill')",
        (ids.generate_id(conn, "c"), show_id, previous["status"], new_status, now),
    )
    conn.commit()
