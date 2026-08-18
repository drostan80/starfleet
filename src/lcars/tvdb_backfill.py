"""Fribb reverse-lookup tvdb_id backfill — SCOPE.md §5.1/§5.5, 2026-08-18.

**The real gap this closes**, found live via a user report ("o still
opens anilist perfectly but not sonarr" — Akame ga Kill!): most of this
library was tracked long before Sonarr/Radarr integration existed at
all (AniList-only `addShow`), so most shows have no `tvdb_id` captured
— `resolvers.py`'s own `_synthetic_arr_add_edge` (the Sonarr/Radarr
"Add New" link `Show.externalIds` offers) falls back to a `?term=
<title>` free-text search for exactly this reason. That fallback works,
but a real `tvdb_id` upgrades it to the more precise `?term=tvdb:{id}`
— and the user's own original goal (a "clean database") was to have
these ids captured at all, not just work around their absence.

**Why this was never going to fill itself in**: the only two existing
paths that ever write a `tvdb` show_external_id row both require the
show to already be *in* Sonarr's own live library first (`addShowWithArr`'s
own lookup, or `pollCatalogServicePresence`'s monthly catalog match,
B.7) — genuinely correct for a show that's there, but silent for one
that isn't, which is most of this library's history.

**The actual mechanism**: `fribb.py`'s own dataset (already downloaded
and cached for the *forward* tvdb->anilist direction, A.4/B.2) carries
`anilist_id` and `tvdb_id` in the same row — `fribb.build_anilist_index()`/
`resolve_tvdb_id_for_anilist()` walk it the other way, same "return
None rather than guess" discipline the forward direction already uses
(genuinely ambiguous — a franchise split across multiple tvdb_ids in
Fribb's own data — is a real, accepted None, not a wrong link). No new
network dependency: same dataset, same cache.

**Movies are a real, known gap, not silently skipped**: Fribb's dataset
has no tmdb_id field at all, and this codebase's only tmdb lookup
(`tmdb_client.find_by_tvdb_id`) needs a tvdb_id as input — there is no
anilist->tmdb path today. Movie-shaped shows keep the `?term=<title>`
fallback only; a real backfill for them would need new work (a
title-based TMDB search, with its own real risk of a wrong match) not
built here.

**Cadence**: no live outbound Sonarr/Radarr call at all (unlike
`pollCatalogServicePresence`) — the candidate query is plain SQL, and
`fribb.load_dataset()` is a cache hit except once every 7 days (its own
on-disk/in-process caching). Negligible enough to ride Ops's hourly
tick (`run_daily_and_weekly_once`, scheduler.py) rather than B.2's
real-N×M-cost monthly one, same "no external HTTP call, cheap enough to
share" reasoning `reconcileEpisodeMovieLinks` already gets. Safe to
call anytime and repeatedly: the candidate query itself excludes any
show that already has a `tvdb` link, so a show this pass can't resolve
(no Fribb entry yet, or genuinely ambiguous) is retried next hour for
free — no separate due-tracking of its own — and a show this pass does
resolve is never reconsidered.
"""

from lcars import fribb, shows


def backfill_tvdb_ids(conn) -> int:
    """Returns the count of shows that actually got a new `tvdb` link
    written — never every show checked, same "count real changes, not
    every check" convention every other Phase B poller here uses."""
    candidates = conn.execute(
        "SELECT s.id AS show_id, link.external_id AS anilist_id"
        " FROM show s"
        " JOIN show_external_id link ON link.show_id = s.id AND link.service = 'anilist'"
        " WHERE s.tracked = 1 AND s.media_shape = 'episodic'"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM show_external_id tvdb_link"
        "   WHERE tvdb_link.show_id = s.id AND tvdb_link.service = 'tvdb'"
        " )"
    ).fetchall()
    if not candidates:
        return 0

    dataset = fribb.load_dataset()
    index = fribb.build_anilist_index(dataset)

    updated = 0
    for row in candidates:
        try:
            anilist_id = int(row["anilist_id"])
        except (TypeError, ValueError):
            continue  # a real anilist show_external_id row is always numeric — defensive only
        tvdb_id = fribb.resolve_tvdb_id_for_anilist(index, anilist_id)
        if tvdb_id is None:
            continue
        if shows.write_tvdb_id(conn, row["show_id"], tvdb_id):
            updated += 1
    conn.commit()
    return updated
