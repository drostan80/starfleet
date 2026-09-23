"""`show_service_presence` automatic refresh — SCOPE.md §5.4/§6.7,
BUILD_PLAN.md B.7.

A.7 (Phase A) built the reconciliation *mechanism*
(`refreshShowServicePresence(showId, service, candidateTitles)`) but
deliberately left it caller-supplied and on-demand only — LCARS itself
made no outbound HTTP calls there, and no schedule drove it. §5.4's
own text names Ops as what finally drives this "on a live clock." This
module is that: two genuinely different pieces, each solving a
different half of the gap, confirmed with the user rather than
assumed to both need the same shape.

**`local`, B.7's actually-unowned gap (per the 2026-08-09 audit that
created this step)**: a pure SQL aggregate over `episode.available_
locally`/`show.available_locally` (both already-generated columns,
B.3) — no external dependency, no client, negligible cost. Rides
Ops's existing hourly tick (`refresh_local_presence`).

**Sonarr/Radarr catalog matching, real N×M cost**: fetches a whole
service catalog (`sonarr_client.all_series()`/`radarr_client.
all_movies()` — the same client methods `local_audit.py`, B.3b,
already established LCARS calling directly for exactly this kind of
bulk listing) and fuzzy-matches every tracked show of the matching
`media_shape` against every catalog title. **Confirmed with the user,
2026-08-09**: this is genuinely expensive per-call (the same class of
concern that produced B.3's seed-and-skip design and
`backfillFileAvailability`'s manual-only carve-out), so it does *not*
ride the hourly tick — it's an unconditional, no-per-show-due-gating
sweep on its own slower cadence, reusing B.2's existing monthly
interval directly (no new config value — the same "no new interval
unless a real technical constraint forces one" precedent B.1/B.4/B.5's
own cadence decisions already established; "the tier itself is the
correctness boundary" is B.2's own monthly-tier framing, reused
verbatim here) (`refresh_catalog_presence`).

**AniList/MAL presence refresh is NOT built here — a real, checked
blocker, not a design choice deferred by assumption**: `anilist_
client.py` has no search/catalog-listing endpoint at all (only
`fetch_media(anilist_id)`, a single-entry lookup by a known id — no
way to ask "does anything matching this title exist"), and `mal_
client.py` doesn't exist yet (B.10, not built). Flagged as a genuine,
separate gap — not silently skipped, not attempted with a half-built
substitute.

Both halves use `_upsert_presence`, which writes B.6's own "record
success/failure at the actual outbound call" convention for Sonarr/
Radarr too (`refresh_catalog_presence`'s own sonarr/radarr helpers) —
and reuses `fuzzy.best_match()` (§5.4, A.7) unchanged, same threshold,
same show-titles-vs-candidate-titles shape the existing on-demand
mutation already established. Same passive/no-`pending_review`
treatment §5.4 already gives the on-demand mutation — a weak match
just means `present` stays/goes `false`, silently, self-healing like
a dead poster URL; there is no "correct" value being overwritten here
to flag.

**Deep-link backfill, added 2026-08-18**: the Sonarr/Radarr catalog
fetch this module already pays for every match against was, until now,
thrown away the moment `_matches` returned a bool. `addShowWithArr`
(B.21) started writing a real `show_external_id` deep link
(`shows.write_arr_external_id`) for shows added through it, but that
left the ~1600 already-tracked shows this repo actually has — added
via the older `addShow`, or tracked from before `addShowWithArr`
existed — with no titleSlug to link from at all, permanently, since
nothing ever re-runs their original add path. This sweep already has
the one thing that was missing (a fuzzy match against the live
catalog, on a real recurring cadence) — `_matches` now returns the
matched catalog entry's title instead of a bool, so its titleSlug
can be pulled straight out of the same response and handed to
`write_arr_external_id` alongside the presence write. Same
`INSERT OR IGNORE`/idempotent shape: a show already linked from
`addShowWithArr` is a silent no-op here every month after; a show
this sweep matches for the first time gets a real link exactly once.
"""

import logging

from lcars import fuzzy, ids, radarr_client, service_health, sonarr_client, util
from lcars import shows as shows_module
from lcars.config import get_current

logger = logging.getLogger("lcars.service_presence")


def refresh_local_presence(conn) -> int:
    """Returns the count of `show_service_presence` rows that actually
    changed (new row or a real flip) — same "count real changes, not
    every check" convention every other Phase B poller already uses."""
    shows = conn.execute("SELECT id, media_shape FROM show WHERE tracked = 1").fetchall()
    updated = 0
    for show in shows:
        if show["media_shape"] == "episodic":
            row = conn.execute(
                "SELECT 1 FROM episode WHERE show_id = ? AND available_locally = 1 LIMIT 1",
                (show["id"],),
            ).fetchone()
            present = row is not None
        else:  # movie
            row = conn.execute(
                "SELECT available_locally FROM show WHERE id = ?", (show["id"],)
            ).fetchone()
            present = bool(row["available_locally"])
        if _upsert_presence(conn, show["id"], "local", present):
            updated += 1
    conn.commit()
    return updated


def refresh_catalog_presence(conn) -> int:
    """Sonarr (episodic shows) + Radarr (movie shows) — see module
    docstring for the full cadence/cost reasoning. Best-effort per
    service, same "one service's failure never blocks the other"
    philosophy every other Phase B poller already uses."""
    updated = _refresh_sonarr_presence(conn) + _refresh_radarr_presence(conn)
    conn.commit()
    return updated


def _refresh_sonarr_presence(conn) -> int:
    cfg = get_current()
    if not cfg.sonarr_url or not cfg.sonarr_api_key:
        return 0  # not configured — same as "not linked", not a failure to report
    shows = conn.execute(
        "SELECT id, title_romaji, title_english, title_native FROM show"
        " WHERE tracked = 1 AND media_shape = 'episodic'"
    ).fetchall()
    if not shows:
        return 0
    try:
        with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
            catalog = client.all_series()
    except sonarr_client.SonarrError as e:
        logger.exception("Sonarr catalog fetch failed — presence sweep skipped this pass")
        service_health.record_failure(conn, "sonarr", str(e))
        conn.commit()
        return 0
    service_health.record_success(conn, "sonarr")
    by_title = {series["title"]: series for series in catalog if series.get("title")}
    by_tvdb = {str(series.get("tvdbId")): series for series in catalog if series.get("tvdbId")}
    tvdb_of = dict(conn.execute(
        "SELECT show_id, external_id FROM show_external_id WHERE service = 'tvdb'"
    ).fetchall())

    updated = 0
    for show in shows:
        matched_title = _matches(conn, show, list(by_title))
        present = matched_title is not None
        if _upsert_presence(conn, show["id"], "sonarr", present):
            updated += 1
        # Backfill (B.7, 2026-08-18) — a deep link for a show addShowWithArr
        # never wrote one for. 2026-09-23: presence stays fuzzy
        # (informational), but the hard link is written only for an ID
        # match — the series' tvdbId must equal the show's own tvdb id. A
        # fuzzy title hit used to become a link to whatever Sonarr series
        # had a similar name.
        series = by_tvdb.get(tvdb_of.get(show["id"]) or "")
        if series is not None and series.get("titleSlug"):
            shows_module.write_arr_external_id(conn, show["id"], "episodic", series["titleSlug"])
    return updated


def _refresh_radarr_presence(conn) -> int:
    cfg = get_current()
    if not cfg.radarr_url or not cfg.radarr_api_key:
        return 0
    shows = conn.execute(
        "SELECT id, title_romaji, title_english, title_native FROM show"
        " WHERE tracked = 1 AND media_shape = 'movie'"
    ).fetchall()
    if not shows:
        return 0
    try:
        with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
            catalog = client.all_movies()
    except radarr_client.RadarrError as e:
        logger.exception("Radarr catalog fetch failed — presence sweep skipped this pass")
        service_health.record_failure(conn, "radarr", str(e))
        conn.commit()
        return 0
    service_health.record_success(conn, "radarr")
    by_title = {movie["title"]: movie for movie in catalog if movie.get("title")}
    by_tmdb = {str(movie.get("tmdbId")): movie for movie in catalog if movie.get("tmdbId")}
    tmdb_of = dict(conn.execute(
        "SELECT show_id, external_id FROM show_external_id WHERE service = 'tmdb'"
    ).fetchall())

    updated = 0
    for show in shows:
        matched_title = _matches(conn, show, list(by_title))
        present = matched_title is not None
        if _upsert_presence(conn, show["id"], "radarr", present):
            updated += 1
        # 2026-09-23 — link only on an ID match (tmdbId), same as Sonarr above.
        movie = by_tmdb.get(tmdb_of.get(show["id"]) or "")
        if movie is not None and movie.get("titleSlug"):
            shows_module.write_arr_external_id(conn, show["id"], "movie", movie["titleSlug"])
    return updated


def _matches(conn, show, catalog_titles: list[str]) -> str | None:
    """The catalog_titles entry (original casing) that best matched
    this show, or None — since 2026-08-18 both callers need the actual
    matched title back (to look its titleSlug up for the deep-link
    backfill above), not just a bool.

    Matches against the show's AniList `synonyms` too (2026-08-26), not
    just its three canonical titles — a planned sequel is often sitting
    in Sonarr/Radarr under an arc-name or other-language title that only
    exists here as a synonym, exactly the case the user flagged."""
    show_titles = [show[f] for f in ("title_romaji", "title_english", "title_native") if show[f]]
    show_titles += [
        r["synonym"]
        for r in conn.execute(
            "SELECT synonym FROM show_synonym WHERE show_id = ?", (show["id"],)
        )
    ]
    return fuzzy.best_match(show_titles, catalog_titles)


def _upsert_presence(conn, show_id: str, service: str, present: bool) -> bool:
    """Returns True if this call actually changed something (a newly
    created row, or a real flip on an existing one) — and, caught in
    review before commit, only *writes* on those same two cases. An
    unchanged existing row is left untouched entirely (no `checked_at`
    bump), deliberately: `refresh_local_presence` runs this once per
    tracked show every hour forever, so a write-every-time version
    would mean a steady-state stream of pointless UPDATEs whose only
    effect is a timestamp nothing currently reads (there's no due-gate
    consuming `checked_at` here — the user chose unconditional sweeps
    over one). Genuinely idempotent now: a stable local state produces
    zero writes on every tick after the first."""
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT id, present FROM show_service_presence WHERE show_id = ? AND service = ?",
        (show_id, service),
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO show_service_presence (id, show_id, service, present, checked_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ids.generate_id(conn, "a"), show_id, service, present, now),
        )
        return True
    changed = bool(existing["present"]) != present
    if not changed:
        return False
    conn.execute(
        "UPDATE show_service_presence SET present = ?, checked_at = ? WHERE id = ?",
        (present, now, existing["id"]),
    )
    return changed
