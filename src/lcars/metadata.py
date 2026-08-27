"""On-demand external metadata fetch — SCOPE.md §4 Phase A / §5.1/§5.2/
§5.4/§5.5/§5.8, BUILD_PLAN.md A.8.

Orchestrates the "immediate one-shot metadata fetch (poster, synopsis,
cast, episode list, external ids) on show creation" Phase A's own intro
text describes. Per the user's explicit direction (2026-08-08, asked
directly rather than assuming A.4/A.7's original "client fetches, LCARS
reconciles" framing carried over): LCARS itself makes these calls —
that framing was only ever a Data-shaped transitional stage, not the
end state ("ultimately all compute and fetch will be handled on the
server by lcars, so might as well built it this way now"). Data keeps
its own separate, pre-existing Sonarr/AniList access for now (A.17,
its own repo) — this module doesn't change that, it's a second,
independent path other clients (or Data itself, later) can also use.

Best-effort throughout (confirmed 2026-08-08): a service being
unreachable never blocks show creation, and a failure in one source
branch (AniList/Sonarr/Radarr) doesn't stop the others from still
running. Each branch's whole body runs under `_guarded` — deliberately
catching bare `Exception`, not just each client's own error class: a
malformed/unexpected API response should never crash addShow either,
only skip that one branch. Failures are never silent: each one opens/
extends a pending_review entry (entity_type='show', reusing §5.6's
existing value-chain-accumulation mechanism rather than inventing a
second "something needs attention" channel) so a human can see it via
the same pendingReviews surface as everything else — and retry, by
simply calling fetch_and_populate() again (exposed as its own
standalone `refreshShowMetadata` mutation, resolvers.py), the same
function addShow already calls inline. "No silent fail... manual fix
by user is also an option" (the user's own words) is satisfied by this
pairing: visible via pending_review, retriable via the mutation. Every
write here is upsert-shaped specifically so a retry after a partial
failure is always safe to just re-run in full.

`season`'s auto-creation here (season_number=1, manual_override=True)
is confirmed in the same round: the anilistId/malId used came from
whatever the caller already supplied on addShow's input — the same
class of "a human already confirmed this" information setSeasonMapping
treats as manual_override. No franchise auto-creation (confirmed
separately, same round): franchise stays a deliberately-created
grouping (§5.9), never a universal per-show wrapper — "no auto
franchise if the system can still confirm ... mapping change first" —
satisfied as-is, since franchise_member (§5.9) already links to
existing show/season/episode rows without needing them restructured;
nothing here needs to change for a later franchise relationship to be
recorded.
"""

import json
from collections.abc import Callable
from datetime import datetime

from lcars import (
    anilist_client,
    fribb,
    ids,
    pending_review,
    radarr_client,
    season_mapping,
    season_ranges,
    service_health,
    sonarr_client,
    tmdb_client,
    util,
)
from lcars.config import get_current

# A.19 — mirrors shows.py's own _EXTERNAL_ID_URL_TEMPLATES/
# _TMDB_URL_TEMPLATES exactly (migration 7196ca889757's show_external_id
# shape; moved there from resolvers.py at B.11d), duplicated here
# rather than imported: shows.py imports this module (create_show()
# calls fetch_and_populate() inline), so the reverse import would be
# circular.
_TMDB_TV_URL_TEMPLATE = "https://www.themoviedb.org/tv/{id}"


def fetch_and_populate(conn, show_id: str) -> None:
    """Best-effort, never raises — see module docstring. Call once
    inline from addShow (creation) or any time after (retry/refresh)."""
    row = conn.execute("SELECT * FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        raise ValueError(f"no such show: {show_id}")
    show = dict(row)

    if show["tracking_space"] == "anime":
        # A.24 (2026-08-09) — identity BEFORE metadata. §5.1 mandates an
        # AniList link for every anime show, but nothing enforced it and
        # `_fetch_anilist` below silently no-ops without one. Data's own
        # bridge (A.17) never sends an anilistId ("data's own add flow
        # often doesn't [have it]", its lcars_client.py) — so every anime
        # show it added landed permanently bare: no synopsis, poster,
        # cast, studio or duration, and no pending_review either, because
        # the missing link was treated as caller input rather than a
        # failure. A.20 made this sharper still: Fribb already resolves
        # the right AniList id into the *season* row moments later, one
        # table away from the code that needed it. This closes the loop
        # by resolving the show-level link first, from the same dataset,
        # so the fetch below just works on this run rather than never.
        _guarded(conn, show, "anilist", _ensure_anilist_link)
        show = dict(conn.execute("SELECT * FROM show WHERE id = ?", (show["id"],)).fetchone())
        _guarded(conn, show, "anilist", _fetch_anilist)
    else:
        # A.19 — AniList already covers duration for anime (its own
        # `duration` field, above); everything else gets it from TMDB.
        _guarded(conn, show, "tmdb", _fetch_tmdb_duration)
    if show["media_shape"] == "episodic":
        _guarded(conn, show, "sonarr", _fetch_sonarr)
    elif show["media_shape"] == "movie":
        _guarded(conn, show, "radarr", _fetch_radarr)
    if show["tracking_space"] == "anime":
        # B.4 — deliberately its own guarded call *after* the Sonarr fetch
        # above, not alongside _fetch_anilist earlier: on a show's very
        # first fetch (addShow), episode rows don't exist yet until
        # _fetch_sonarr just ran — reconciling air dates any earlier would
        # find nothing to match against on every first-ever fetch, only
        # correcting on the *next* refresh cycle a full day later. A
        # per-season loop against each season's own anilist_id besides, a
        # genuinely different id than _fetch_anilist's single show-level
        # one (see _reconcile_air_dates's own docstring).
        _guarded(conn, show, "anilist", _reconcile_air_dates)

    # B.1, §6.7/§11.2 — stamped unconditionally, regardless of which (if
    # any) branch above actually succeeded: this is an "attempt" marker,
    # not a "succeeded" one, matching every _guarded branch's own
    # best-effort/never-blocks philosophy. A genuine failure is already
    # visible via pending_review and retriable any time via
    # refreshShowMetadata — this stamp only prevents Ops's own daily pass
    # (Query.dueForMetadataRefresh) from re-attempting a show that was
    # just fetched, addShow's own inline call included, so a show created
    # today isn't immediately re-fetched by Ops's next poll the same day.
    conn.execute(
        "UPDATE show SET metadata_last_refreshed_at = ? WHERE id = ?",
        (util.now_utc_iso(), show_id),
    )


def _guarded(conn, show: dict, service: str, fn: Callable[[object, dict], None]) -> None:
    """Runs `fn(conn, show)`, catching anything at all — a malformed
    response should skip this one branch, never crash addShow or block
    the other branches. Deliberately broad (bare Exception), not just
    each client's own *Error class — see module docstring.

    **Deliberately NOT B.6's service-health choke point** — tried that
    first, reverted before commit: every one of this function's callers
    (`_ensure_anilist_link`/`_fetch_anilist`/`_reconcile_air_dates`/
    `_fetch_sonarr`/`_fetch_radarr`) has its own legitimate no-HTTP
    early-return path (missing external id, service not configured, no
    season carries an `anilist_id` yet...), so "`fn` returned without
    raising" does not mean "a request completed" — recording success
    here would have logged a false `ok` for a service never actually
    contacted this call, caught by a real, reproduced test failure
    (`test_add_show_fetch_failure_logs_pending_review_and_refresh_retries`:
    a genuine `_fetch_anilist` failure got silently overwritten back to
    `ok` by the very next `_guarded` call, `_reconcile_air_dates`,
    whose own no-mapped-season early return trivially "succeeded").
    §6.7's health tracking instead hooks each function's own actual
    client call directly — see `_fetch_anilist`/`_reconcile_air_dates`/
    `_fetch_sonarr`/`_fetch_radarr`'s own docstrings/comments."""
    try:
        fn(conn, show)
    except Exception as e:
        pending_review.open_or_extend(
            conn, "show", show["id"], "metadata_fetch", service, None, str(e)
        )


def _external_id(conn, show_id: str, service: str) -> str | None:
    row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = ?",
        (show_id, service),
    ).fetchone()
    return row["external_id"] if row else None


# --- AniList (tracking_space = anime) ---------------------------------------


def _ensure_anilist_link(conn, show: dict) -> None:
    """§5.1's "anime **mandates** an AniList link, no exceptions" — made
    true rather than aspirational, A.24 (2026-08-09). Resolves the
    show-level `anilist` `show_external_id` from the show's tvdb id via
    the Fribb dataset when it's missing, using season 1 (the same
    convention `addShow`'s own `anilistId` input and `_upsert_season`
    already assume for "the show's" AniList entry).

    Never overwrites an existing link — a caller-supplied id is a human
    saying so, and outranks a dataset lookup (§3 principle 6). When the
    show has no tvdb id either, or Fribb has no match, a `pending_review`
    is opened rather than an error raised: hard-rejecting would break
    Data's bridge on every anime add, and §3 principle 1's "apply, then
    flag, never gate" governs here as everywhere else.
    """
    if _external_id(conn, show["id"], "anilist") is not None:
        return
    tvdb_id_str = _external_id(conn, show["id"], "tvdb")
    if tvdb_id_str is None:
        pending_review.open_or_extend(
            conn,
            "show",
            show["id"],
            "anilist_id",
            "anilist",
            None,
            "anime show has neither an AniList nor a tvdb id — cannot resolve (§5.1)",
        )
        conn.commit()
        return

    dataset = fribb.load_dataset()
    index = fribb.build_tvdb_index(dataset)
    candidate = fribb.resolve_season_candidate(index, int(tvdb_id_str), 1)
    anilist_id, _mal_id = fribb.extract_ids(candidate)
    if anilist_id is None:
        pending_review.open_or_extend(
            conn,
            "show",
            show["id"],
            "anilist_id",
            "fribb",
            None,
            f"no AniList match for tvdb id {tvdb_id_str} (§5.1 requires one)",
        )
        conn.commit()
        return

    conn.execute(
        "INSERT OR IGNORE INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'anilist', ?, ?, ?)",
        (show["id"], str(anilist_id), f"https://anilist.co/anime/{anilist_id}", util.now_utc_iso()),
    )
    conn.commit()


def _sync_synonyms(conn, show_id: str, synonyms: list, now: str) -> None:
    """AniList's `Media.synonyms` -> the `show_synonym` child table,
    delete-then-insert so a re-fetch stays idempotent and picks up any
    synonyms AniList has since added (unlike the write-once title
    columns, this list legitimately grows). Deduplicated and blank-
    stripped here; `UNIQUE(show_id, synonym)` is the backstop. Only ever
    touches this one show's own rows — never another show's, and never a
    manual `display_title_override` (a separate `show` column)."""
    conn.execute("DELETE FROM show_synonym WHERE show_id = ?", (show_id,))
    seen: set[str] = set()
    for raw in synonyms:
        synonym = (raw or "").strip()
        if not synonym or synonym in seen:
            continue
        seen.add(synonym)
        conn.execute(
            "INSERT INTO show_synonym (show_id, synonym, created_at) VALUES (?, ?, ?)",
            (show_id, synonym, now),
        )


def _fetch_anilist(conn, show: dict) -> None:
    anilist_id_str = _external_id(conn, show["id"], "anilist")
    if anilist_id_str is None:
        # tracking_space = anime mandates an AniList link (§5.1) — the
        # caller should have supplied anilistId on addShow's input.
        # Nothing to fetch without it; this is a caller-input gap, not
        # an unreachable-service failure, so no pending_review entry.
        return
    # §6.7, B.6 — hooked right here, not in _guarded (that function's own
    # docstring has the full "no-HTTP early-return" reasoning): this is
    # the actual outbound call, the only point that can honestly say
    # "AniList was/wasn't reachable" this call.
    try:
        media = anilist_client.fetch_media(int(anilist_id_str))
    except anilist_client.AniListError as e:
        service_health.record_failure(conn, "anilist", str(e))
        conn.commit()
        raise
    service_health.record_success(conn, "anilist")
    conn.commit()
    if media is None:
        return

    now = util.now_utc_iso()
    genres = media.get("genres")
    conn.execute(
        "UPDATE show SET"
        "  poster_url = COALESCE(?, poster_url),"
        "  banner_url = COALESCE(?, banner_url),"
        "  synopsis = COALESCE(?, synopsis),"
        "  genres_raw = COALESCE(?, genres_raw),"
        "  total_episodes = COALESCE(?, total_episodes),"
        "  duration_minutes = COALESCE(?, duration_minutes),"
        "  updated_at = ?"
        " WHERE id = ?",
        (
            (media.get("coverImage") or {}).get("large"),
            media.get("bannerImage"),
            media.get("description"),
            json.dumps(genres) if genres else None,
            media.get("episodes"),
            media.get("duration"),
            now,
            show["id"],
        ),
    )

    _sync_synonyms(conn, show["id"], media.get("synonyms") or [], now)

    _upsert_season(conn, show["id"], 1, int(anilist_id_str), media.get("idMal"))

    for studio in (media.get("studios") or {}).get("nodes") or []:
        if studio.get("id") is not None and studio.get("name"):
            _link_studio(conn, show["id"], studio, "studio")

    for edge in (media.get("characters") or {}).get("edges") or []:
        character_name = ((edge.get("node") or {}).get("name") or {}).get("full")
        voice_actors = edge.get("voiceActors") or []
        if voice_actors and (voice_actors[0].get("name") or {}).get("full"):
            _link_person(conn, show["id"], voice_actors[0], "voice_actor", character_name)

    for edge in (media.get("relations") or {}).get("edges") or []:
        node = edge.get("node") or {}
        is_trackable = node.get("format") in anilist_client.ANIME_RELATION_FORMATS
        if node.get("id") is not None and is_trackable:
            _link_relation(conn, show["id"], node)


def _reconcile_air_dates(conn, show: dict) -> None:
    """§5.2/§6.7, B.4 — AniList `airingSchedule` air-date reconciliation.
    A separate pass from `_fetch_anilist` above, its own `_guarded()`
    call in `fetch_and_populate` below, deliberately: that function's
    single `fetch_media()` call is scoped to the show-level AniList id
    (season 1's, by `_upsert_season`'s own convention); this one needs
    a *different* AniList id per season (`season.anilist_id`, B.2's own
    Fribb-resolved crosswalk — a split-cour sequel is a wholly separate
    AniList Media entry from its first cour), so it's a real per-season
    loop, not a single call.

    Rides the exact same cadence `_fetch_anilist` does — no new
    due-query/mutation (confirmed with the user, B.4): Ops's daily pass
    already selects watching+actively-airing shows only
    (`dueForMetadataRefresh`), the same set B.4 needs, so a dedicated
    poll would only duplicate that filter for no benefit.

    Fetches the *full* airingSchedule (not `notYetAired`-filtered) so
    an already-aired episode's date gets reconciled too, not just
    upcoming ones (confirmed with the user) — matching §6.7's own
    "reconciliation" framing rather than a lookahead.

    **Manual dates are protected, revised 2026-08-09 after this
    function's first draft shipped** — the user's own reasoning: only
    a genuine reschedule signal (animeschedule.net, B.5) should
    override a value they've deliberately corrected; AniList/Sonarr
    repeatedly re-asserting stale or wrong data over an already-fixed
    value is exactly the "stubborn weekly rewrite" the user flagged as
    the failure mode to avoid. So this function skips outright (no
    write, no `pending_review`) whenever the existing `air_date_source`
    is already `'manual'` — the same hard-gate shape
    `season_mapping.py`'s own `reconcile_season()` already gives
    `season.manual_override` (§3 principle 6), extended here to
    per-episode manual air dates specifically for AniList/Sonarr.
    `SCOPE.md` §6.7's own text is corrected to match.

    **Also skips `'animeschedule'`, added 2026-08-09 (B.8) — closing a
    real gap B.5 exposed**: B.5 shipped animeschedule.net as a genuine
    reconciliation source, ranked *above* AniList in §6.7's priority
    order (`Manual > animeschedule.net > AniList > Sonarr raw`). This
    function predates B.5 and only ever guarded against `'manual'`, so
    an animeschedule-sourced date survived only until this same daily
    pass next ran and silently overwrote it — a live violation of that
    priority order in already-shipped code, not a hypothetical. Same
    treatment as the manual case: skip outright, no write, no
    `pending_review` (there's nothing to flag — a lower-priority source
    correctly declining to overwrite a higher-priority one is expected
    behavior, the same reasoning the manual-date skip above already
    uses).

    **Season-split guard, same date, real and confirmed live** (not
    hypothetical) — a single TVDB season can span *multiple* separate
    AniList Media entries (Attack on Titan's own Season 3: one
    22-episode TVDB season, two AniList entries of 12 and 10 episodes
    each). `season.anilist_id` can only point at one of them, so if
    LCARS's own episode count for a season exceeds that Media entry's
    own reported `episodes` count, per-episode matching is unsafe —
    skipped entirely, with a `pending_review` opened on the *season*
    (not each individual episode) so a human can investigate and fix
    the underlying season/anilist_id mapping. The fuller fix — letting
    a human resolve that review by specifying an actual episode-range
    split (episodes X-Y are Media A, P-Q are Media B) — needs a real,
    structured way to store that mapping that doesn't exist yet
    (`pending_review`'s own resolution is free-text only); flagged as
    a genuine follow-up, deliberately not built as part of B.4.

    **Real bug found and fixed 2026-08-12**, driving the pending_review
    backlog toward zero: open_or_extend (SS5.6) only re-extends an
    *unresolved* entry -- once a human resolves one of these, the very
    next daily pass (this function rides dueForMetadataRefresh's own
    cadence, see above) finds the exact same episode-count mismatch
    again and opens a brand new entry, since nothing here remembered a
    resolved decision. Confirmed live for two real shows (Tonbo!,
    Chitose Is in the Ramune Bottle) whose season.anilist_id happens to
    carry manual_override = 1 (set via ordinary addShow/link-time
    linking, not a deliberate "yes, I accept this exact mismatch"
    review action) -- manual_override alone is NOT a safe gate here
    (tried first, reverted: it broke this file's own
    test_anilist_air_date_reconciliation_skips_a_season_that_spans_
    multiple_anilist_entries, whose Attack-on-Titan-shaped season is
    ALSO manual_override = 1 via the exact same addShow(anilistId=...)
    path, and legitimately must still get flagged -- manual_override
    there only ever means "this is confirmed to be the right AniList
    entry," never "the human has seen and accepted this specific
    episode-count gap"). The real fix instead: skip opening a *new*
    review only when a review carrying this exact message was already
    resolved (pending_review.already_resolved_with) -- the same
    "unchanged re-check stays silent" principle reconcile_season()'s
    own existing["anilist_id"] != anilist_id comparison already gives
    Fribb matches, just expressed against a resolved chain instead of
    a stored column, since this branch's "value" is a computed message,
    not a single field. A genuinely new mismatch (the counts on either
    side actually changed) produces different text and opens for real.
    """
    seasons = conn.execute(
        "SELECT id, season_number, anilist_id FROM season"
        " WHERE show_id = ? AND anilist_id IS NOT NULL",
        (show["id"],),
    ).fetchall()
    now = util.now_utc_iso()
    for season in seasons:
        # §6.7, B.6 — hooked at the actual call, same reasoning
        # _fetch_anilist gives (not _guarded, which wraps this whole
        # function and would record "ok" even for a show with zero
        # seasons carrying an anilist_id — a real, no-HTTP-at-all
        # case `_guarded`'s own docstring has the full story on).
        # One call per season in this loop: the last season checked
        # this pass is what service_health ends up showing — accepted
        # as "is it reachable right now," not changed here.
        try:
            result = anilist_client.fetch_airing_schedule(season["anilist_id"])
        except anilist_client.AniListError as e:
            service_health.record_failure(conn, "anilist", str(e))
            conn.commit()
            raise
        service_health.record_success(conn, "anilist")
        conn.commit()
        if not result or not result["nodes"]:
            continue

        anilist_episode_count = result["episodes"]
        lcars_episode_count = conn.execute(
            "SELECT COUNT(*) AS n FROM episode WHERE show_id = ? AND season = ?",
            (show["id"], season["season_number"]),
        ).fetchone()["n"]
        if anilist_episode_count is not None and lcars_episode_count > anilist_episode_count:
            reason = (
                f"season {season['season_number']} has {lcars_episode_count} episode(s) in "
                f"LCARS but AniList media {season['anilist_id']} only covers "
                f"{anilist_episode_count} — likely spans multiple AniList entries; "
                "air-date reconciliation skipped for this season"
            )
            if not pending_review.already_resolved_with(
                conn, "season", season["id"], "anilist_id", reason
            ):
                pending_review.open_or_extend(
                    conn, "season", season["id"], "anilist_id", "anilist", None, reason
                )
            continue

        for node in result["nodes"]:
            episode_row = conn.execute(
                "SELECT id, air_date_utc, air_date_source, available_via_sonarr FROM episode"
                " WHERE show_id = ? AND season = ? AND episode = ?",
                (show["id"], season["season_number"], node["episode"]),
            ).fetchone()
            if episode_row is None:
                continue  # not yet fetched into LCARS — A.8's Sonarr fetch's job, not this one's
            if episode_row["air_date_source"] in ("manual", "animeschedule"):
                continue  # hard-protected — see this function's own docstring
            new_air_date = util.unix_to_iso(node["airingAt"])
            if episode_row["air_date_utc"] == new_air_date:
                continue

            # 2026-08-15 — real, user-caught bug: "Draw This, Then Die!"
            # episode 7. AniList's `airingSchedule` is one global value
            # that can reflect an overseas-only delay while the real
            # Japan broadcast (and Sonarr's own real download) landed on
            # the original date — this function had no way to tell that
            # case apart from an ordinary schedule correction, and
            # always trusted AniList (§6.7's priority order), silently
            # overwriting a date a real downloaded file had already
            # proven correct. Distinct from Frontier Lord's own
            # early-streaming case (AniList's date *earlier* than
            # Sonarr's, correctly applied): this guard only fires when
            # AniList proposes something *later* than a date Sonarr's
            # own already-imported file backs up — the direction that
            # can never be legitimate ("aired and downloaded" cannot
            # later become "hasn't aired yet"). Flags instead of
            # applying — a human decides, same §3 principle 1 shape
            # every other genuine ambiguity in this codebase gets,
            # rather than silently trusting a source that just proved
            # itself wrong for this one episode.
            if (
                episode_row["air_date_source"] == "sonarr"
                and episode_row["available_via_sonarr"] == "available"
                and new_air_date > episode_row["air_date_utc"]
            ):
                pending_review.open_or_extend(
                    conn,
                    "episode",
                    episode_row["id"],
                    "air_date_utc",
                    "anilist",
                    episode_row["air_date_utc"],
                    f"AniList proposes {new_air_date} (a delay past the current "
                    f"{episode_row['air_date_utc']}) but a file is already downloaded at "
                    "the current date — likely a region-scoped delay that doesn't apply to "
                    "the real broadcast; not applied automatically, needs a human look",
                )
                continue

            pending_review.open_or_extend(
                conn,
                "episode",
                episode_row["id"],
                "air_date_utc",
                "anilist",
                episode_row["air_date_utc"],
                new_air_date,
            )
            conn.execute(
                "UPDATE episode SET air_date_utc = ?, air_date_source = 'anilist',"
                " updated_at = ? WHERE id = ?",
                (new_air_date, now, episode_row["id"]),
            )


def _existing_related_show(conn, anilist_id: str, mal_id) -> str | None:
    """Checks `show_external_id` for either id — B.11d/B.11e follow-up,
    a real bug found live in the second deployed backfill run: AniList
    sometimes splits what MAL keeps as *one* entry into several
    separate Media entries (real cases found: "Ao Haru Ride PAGE.13"/
    "Ao Haru Ride: unwritten", Saint Seiya's own two-part split, a
    SYNDUALITY short, all four Summer Pockets chapter entries — each a
    genuinely distinct `anilist_id`, all sharing one `idMal`). Each one
    independently passed the old anilist_id-only check below as "no
    existing show", so each got its own stub — 4 real collision groups,
    6 excess rows, confirmed live: `SELECT service, external_id,
    COUNT(DISTINCT show_id) ... HAVING c > 1` came back non-empty for
    `mal` even after the B.11e promotion fix, which only ever guarded
    `shows.create_show()`'s own top-level entry point, not this
    module's separate `_link_relation`/`_create_relation_stub` path.

    Mirrors `shows.find_existing_show()`'s own multi-id check, not
    imported directly — `shows.py` already imports this module, so the
    reverse would be circular; kept as its own small local check
    instead, same "metadata.py keeps its own copy" precedent this
    module's `_EXTERNAL_ID_URL_TEMPLATES` already established (see
    `shows.py`'s own module docstring). A live SELECT on the same
    connection sees an uncommitted INSERT from earlier in the same
    request (SQLite's own read-your-own-writes), so this self-heals
    within one relation walk too — two edges on the same parent
    show's own relations list resolve to one stub, not two, the same
    write-time-not-snapshot shape the B.11e fix already established."""
    row = conn.execute(
        "SELECT show_id FROM show_external_id WHERE service = 'anilist' AND external_id = ?",
        (anilist_id,),
    ).fetchone()
    if row is not None:
        return row["show_id"]
    if mal_id is not None:
        row = conn.execute(
            "SELECT show_id FROM show_external_id WHERE service = 'mal' AND external_id = ?",
            (str(mal_id),),
        ).fetchone()
        if row is not None:
            return row["show_id"]
    return None


def _link_relation(conn, show_id: str, related_media: dict) -> None:
    """§5.9 — `show_relation` is directed, written whenever a show's
    AniList data reports a relation, one row for this direction only;
    the other show's own fetch (if/when it happens) writes its own
    direction independently. `related_show_id` is a real, non-nullable
    FK (§5.9's own table definition), so a related show LCARS has never
    seen before needs a real row to point at — resolved 2026-08-09
    (A.21, asked directly): auto-create it as a `tracked = false` stub,
    the same promotion-target shape §5.1's own "Show-row promotion
    paths" already describes ("a bare tracked = false relation/
    franchise stub becomes a real tracked show by flipping tracked =
    true"). No franchise auto-creation here either way (A.8's own "no
    auto franchise" precedent, §5.9 — a relation edge is not a
    franchise membership, `franchise_member` stays a deliberate,
    separate action)."""
    related_anilist_id = str(related_media["id"])
    related_show_id = _existing_related_show(conn, related_anilist_id, related_media.get("idMal"))
    if related_show_id is None:
        related_show_id = _create_relation_stub(conn, related_media, related_anilist_id)

    now = util.now_utc_iso()
    conn.execute(
        "INSERT OR IGNORE INTO show_relation (show_id, related_show_id, created_at)"
        " VALUES (?, ?, ?)",
        (show_id, related_show_id, now),
    )


def _create_relation_stub(conn, related_media: dict, related_anilist_id: str) -> str:
    title = related_media.get("title") or {}
    romaji, english, native = title.get("romaji"), title.get("english"), title.get("native")
    # 2026-08-19 — real live bug, user-caught (Ascendance of a Bookworm: 4 of
    # its 5 parts came into LCARS through this exact function, this exact
    # relation graph): this used to prefer `romaji` whenever it existed at
    # all, `english` only as a fallback for when romaji was missing — so a
    # stub with a perfectly good English title still got stuck displaying/
    # searching under its Japanese romaji one ("Honzuki no Gekokujou:
    # Shisho ni Naru Tame ni wa Shudan wo Erandeiraremasen 2nd Season"
    # instead of "Ascendance of a Bookworm Part 2") the instant that romaji
    # field happened to be non-empty, which AniList populates for nearly
    # everything. `primary_title` is a write-once choice — `_promote_stub`
    # (shows.py) deliberately never revisits it, and no metadata refresh
    # does either — so this was the one place to get it right. English,
    # when AniList actually provides one, is what a user searching/browsing
    # in English types and recognizes; romaji is the honest fallback for
    # the (common, legitimate) case where no official English title exists
    # at all, not the default over one that does.
    if english:
        primary_title = "english"
    elif romaji:
        primary_title = "romaji"
    elif native:
        primary_title = "native"
    else:
        # AniList's own schema guarantees at least a romaji title exists for
        # any real Media — an edge with none at all isn't a usable stub.
        raise ValueError(f"AniList relation {related_anilist_id} has no title at all")

    show_id = ids.generate_id(conn, "s")
    now = util.now_utc_iso()
    media_shape = "movie" if related_media.get("format") == "MOVIE" else "episodic"
    conn.execute(
        "INSERT INTO show"
        " (id, media_shape, tracking_space, title_romaji, title_english, title_native,"
        "  primary_title, status, tracked, created_at, updated_at)"
        " VALUES (?, ?, 'anime', ?, ?, ?, ?, 'planned', 0, ?, ?)",
        (show_id, media_shape, romaji, english, native, primary_title, now, now),
    )
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'anilist', ?, ?, ?)",
        (show_id, related_anilist_id, f"https://anilist.co/anime/{related_anilist_id}", now),
    )
    if related_media.get("idMal") is not None:
        mal_id = str(related_media["idMal"])
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, 'mal', ?, ?, ?)",
            (show_id, mal_id, f"https://myanimelist.net/anime/{mal_id}", now),
        )
    return show_id


def _upsert_season(
    conn, show_id: str, season_number: int, anilist_id: int | None, mal_id: int | None
) -> None:
    """A caller-supplied id at addShow time is the same class of
    "a human already confirmed this" information setSeasonMapping
    treats as manual_override — see module docstring. Deliberately
    does NOT touch an already-manual_override row (§3 principle 6),
    same protection reconcileSeasonMapping (A.4) gives it."""
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT id, manual_override FROM season WHERE show_id = ? AND season_number = ?",
        (show_id, season_number),
    ).fetchone()
    if existing is not None:
        if existing["manual_override"]:
            return
        conn.execute(
            "UPDATE season SET anilist_id = ?, mal_id = ?, source = 'manual',"
            "     matched = 1, manual_override = 1, updated_at = ?"
            " WHERE id = ?",
            (anilist_id, mal_id, now, existing["id"]),
        )
        # S2 dual-write (see season_ranges.py)
        season_ranges.upsert_season_external_id(conn, existing["id"], anilist_id, mal_id, now)
        return
    season_id = ids.generate_id(conn, "z")
    conn.execute(
        "INSERT INTO season"
        " (id, show_id, season_number, anilist_id, mal_id, source, matched,"
        "  manual_override, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'manual', 1, 1, ?, ?)",
        (season_id, show_id, season_number, anilist_id, mal_id, now, now),
    )
    # S2 dual-write (see season_ranges.py)
    season_ranges.upsert_season_external_id(conn, season_id, anilist_id, mal_id, now)


def _link_studio(conn, show_id: str, studio: dict, role_type: str) -> None:
    """Upsert-by-AniList-id so the same real-world studio isn't
    duplicated across shows that share one (§5.8's external_service/
    external_id columns exist for exactly this)."""
    now = util.now_utc_iso()
    anilist_id = str(studio["id"])
    existing = conn.execute(
        "SELECT id FROM studio WHERE external_service = 'anilist' AND external_id = ?",
        (anilist_id,),
    ).fetchone()
    if existing is not None:
        studio_id = existing["id"]
    else:
        studio_id = ids.generate_id(conn, "d")
        conn.execute(
            "INSERT INTO studio (id, name, external_service, external_id, external_url, created_at)"
            " VALUES (?, ?, 'anilist', ?, ?, ?)",
            (studio_id, studio["name"], anilist_id, f"https://anilist.co/staff/{anilist_id}", now),
        )
    conn.execute(
        "INSERT OR IGNORE INTO show_studio (show_id, studio_id, role_type) VALUES (?, ?, ?)",
        (show_id, studio_id, role_type),
    )


def _link_person(conn, show_id: str, voice_actor: dict, role_type: str, character_name) -> None:
    """Upsert-by-AniList-id, same reasoning as _link_studio. The
    show_person guard checks (show_id, person_id, character_name)
    together, not just (show_id, person_id) like show_studio's plain
    `INSERT OR IGNORE` — a person can voice more than one character in
    the same show (§5.8's own "no composite natural-key PK" note), so
    collapsing on person alone would silently drop a second real role;
    keying on the full triple still makes a retry after a prior
    failure safe to just re-run without duplicating any one row."""
    now = util.now_utc_iso()
    anilist_id = str(voice_actor["id"])
    name = voice_actor["name"]["full"]
    existing = conn.execute(
        "SELECT id FROM person WHERE external_service = 'anilist' AND external_id = ?",
        (anilist_id,),
    ).fetchone()
    if existing is not None:
        person_id = existing["id"]
    else:
        person_id = ids.generate_id(conn, "p")
        conn.execute(
            "INSERT INTO person (id, name, external_service, external_id, external_url, created_at)"
            " VALUES (?, ?, 'anilist', ?, ?, ?)",
            (person_id, name, anilist_id, f"https://anilist.co/staff/{anilist_id}", now),
        )
    already_linked = conn.execute(
        "SELECT 1 FROM show_person WHERE show_id = ? AND person_id = ? AND character_name IS ?",
        (show_id, person_id, character_name),
    ).fetchone()
    if already_linked is None:
        conn.execute(
            "INSERT INTO show_person (show_id, person_id, role_type, character_name)"
            " VALUES (?, ?, ?, ?)",
            (show_id, person_id, role_type, character_name),
        )


# --- TMDB (everything except tracking_space = anime) — A.19 -----------------


def _fetch_tmdb_duration(conn, show: dict) -> None:
    """Fills `show.duration_minutes` for every non-anime show, movie or
    TV — AniList already covers anime via its own `duration` field
    (see `_fetch_anilist` above). A movie's tmdb id usually already
    exists (§5.4: movies key primarily on TMDB, native to Radarr); a
    non-anime TV show usually only carries a tvdb id (native to
    Sonarr), so this resolves TMDB's id via `find_by_tvdb_id` first and
    persists the result as a real `show_external_id` row (same
    upsert-by-show_id+service shape `linkShowExternalId` itself uses,
    resolvers.py) — a retry/refresh afterward reads it straight back,
    no need to re-resolve."""
    cfg = get_current()
    if not cfg.tmdb_api_key:
        return  # not configured — same as "not linked", not a failure to report

    tmdb_id_str = _external_id(conn, show["id"], "tmdb")
    with tmdb_client.TmdbClient(cfg.tmdb_api_key) as client:
        if tmdb_id_str is None:
            tmdb_id_str = _resolve_and_store_tmdb_id(conn, show, client)
            if tmdb_id_str is None:
                return
        tmdb_id = int(tmdb_id_str)
        if show["media_shape"] == "movie":
            runtime = client.movie_runtime(tmdb_id)
        else:
            runtime = client.tv_episode_runtime(tmdb_id)

    if runtime:
        conn.execute(
            "UPDATE show SET duration_minutes = COALESCE(?, duration_minutes), updated_at = ?"
            " WHERE id = ?",
            (runtime, util.now_utc_iso(), show["id"]),
        )


def _resolve_and_store_tmdb_id(conn, show: dict, client: "tmdb_client.TmdbClient") -> str | None:
    """Only the episodic (TV) side has a bridge to resolve through — a
    movie with no tmdb id at all has nothing this function can do
    about it (no title-search feature exists, out of A.19's own
    scope); `_fetch_tmdb_duration` above already no-ops for that case
    the same way every other "nothing to look up" branch in this file
    does."""
    if show["media_shape"] != "episodic":
        return None
    tvdb_id_str = _external_id(conn, show["id"], "tvdb")
    if tvdb_id_str is None:
        return None
    tmdb_id = client.find_by_tvdb_id(int(tvdb_id_str))
    if tmdb_id is None:
        return None
    now = util.now_utc_iso()
    conn.execute(
        "INSERT OR IGNORE INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'tmdb', ?, ?, ?)",
        (show["id"], str(tmdb_id), _TMDB_TV_URL_TEMPLATE.format(id=tmdb_id), now),
    )
    return str(tmdb_id)


# --- Sonarr (media_shape = episodic) ----------------------------------------


def _availability_from_sonarr_episode(ep: dict) -> tuple[str, str | None]:
    """2026-08-15 — real bug found live: a freshly-Sonarr-linked show
    whose real import history predates the link showed every episode
    "missing" even with real files on disk. `_fetch_sonarr`'s own
    `INSERT INTO episode` never touched `available_via_sonarr`/
    `file_path_sonarr` at all, so they silently took the schema default
    (`'unavailable'`) — and the regular availability poller
    (`availability.py`'s `poll_file_availability`, checkpoint-based on
    Sonarr's own `/history` log) can never retroactively discover an
    import that happened before the show was linked at all, so nothing
    ever self-healed it either.

    The fix needs no second API call: `client.episodes(...,
    include_episode_file=True)` (already built for B.3b's
    `auditLocalFiles`, `local_audit.py` — same `hasFile`/`episodeFile`
    derivation reused here, not reinvented) embeds Sonarr's own
    *current* file state directly on the exact same response this
    function already fetches to create the episode rows in the first
    place."""
    if ep.get("hasFile") and ep.get("episodeFile"):
        return "available", ep["episodeFile"]["path"]
    return "unavailable", None


def _fetch_sonarr(conn, show: dict) -> None:
    tvdb_id_str = _external_id(conn, show["id"], "tvdb")
    if tvdb_id_str is None:
        return  # §5.1 — Sonarr's link is optional; nothing to fetch without it
    cfg = get_current()
    if not cfg.sonarr_url or not cfg.sonarr_api_key:
        return  # not configured — same as "not linked", not a failure to report

    # §6.7, B.6 — hooked at the actual outbound call, not _guarded (see
    # that function's own docstring): the two early returns above never
    # touch the network at all, so recording there would misreport them
    # as a successful Sonarr contact.
    try:
        with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
            series = client.series_by_tvdb_id(int(tvdb_id_str))
            if series is not None:
                episodes = client.episodes(series["id"], include_episode_file=True)
    except sonarr_client.SonarrError as e:
        service_health.record_failure(conn, "sonarr", str(e))
        conn.commit()
        raise
    service_health.record_success(conn, "sonarr")
    conn.commit()
    if series is None:
        return  # not (yet) in Sonarr's own library — not an error, §5.1

    # 2026-08-15 — Ascendance of a Bookworm, real live bug, user-caught:
    # Sonarr/TVDB tracks a whole multi-part franchise as one flat series
    # while AniList splits it into a separate media entry (and therefore
    # a separate LCARS show) per part. §3's "the internal database is
    # the source of truth; AniList/Fribb/Sonarr... their classifications
    # are mapped in, never authoritative over internal behavior" means
    # this show's raw Sonarr numbering must never get dumped wholesale
    # into whichever one show happens to hold this tvdb link the moment
    # more than one LCARS show shares it — that's exactly what happened
    # here before this fix: every episode, including the currently-
    # airing part's, landed under the first (and by then long-finished)
    # part, invisible in Data. See _fetch_sonarr_multi_show's own
    # docstring for the full routing rule this delegates to instead. The
    # overwhelmingly common case (exactly one show holds this tvdb id)
    # falls straight through the unchanged path below — this check costs
    # one indexed lookup and changes nothing else about it.
    sibling_ids = [
        row["show_id"]
        for row in conn.execute(
            "SELECT show_id FROM show_external_id WHERE service = 'tvdb' AND external_id = ?",
            (tvdb_id_str,),
        ).fetchall()
    ]
    if len(sibling_ids) > 1:
        _fetch_sonarr_multi_show(conn, sibling_ids, episodes)
        return

    # A.20 (2026-08-09 consolidation pass) — real gap found in the audit:
    # this function used to insert `episode` rows for whatever season
    # numbers Sonarr reported without ever creating the corresponding
    # `season` row (§5.5) or setting `episode.season_id`. Sonarr/TVDB is
    # the only source that ever tells LCARS a season number exists at all
    # (AniList doesn't — each AniList entry is already scoped to one
    # season, per §5.5's own "TVDB groups a franchise's seasons... AniList
    # splits each season" framing), so this is the one place a newly-
    # discovered season number can be reconciled the moment it appears —
    # same on-demand-immediately philosophy as every other A.8 branch,
    # rather than leaving it as a bare unmatched row for a Phase B
    # scheduler that doesn't exist yet. A season already known (existing
    # `season` row, whatever its `manual_override`) is left untouched by
    # `reconcile_season` itself — see that function's own docstring.
    season_numbers = {ep.get("seasonNumber") for ep in episodes}
    season_ids_by_number = _ensure_seasons(conn, show["id"], season_numbers)

    now = util.now_utc_iso()
    for ep in episodes:
        season_number = ep.get("seasonNumber")
        episode_number = ep.get("episodeNumber")
        if season_number is None or episode_number is None:
            continue
        season_id = season_ids_by_number.get(season_number)
        # NEXT_UP.md, 2026-08-25 (setEpisodeNumber) — matches on
        # sonarr_season/sonarr_episode, Sonarr's own raw numbering
        # captured immutably at insert time (below), not the display
        # season/episode a manual renumber may have since corrected.
        # Matching on the display columns here would silently undo
        # every renumber the moment this fetch next runs: Sonarr still
        # reports the episode under its original number, that lookup
        # would find no existing row, and a phantom duplicate would get
        # INSERTed at the original slot instead of recognizing the
        # renumbered row as already known.
        existing = conn.execute(
            "SELECT id FROM episode WHERE show_id = ? AND sonarr_season = ? AND sonarr_episode = ?",
            (show["id"], season_number, episode_number),
        ).fetchone()
        if existing is None:
            # Fallback for a row that predates sonarr_season/sonarr_episode
            # capture entirely (e.g. scripts/import_trakt_history.py's own
            # synthesized episode rows, which never set them) — match on
            # the legacy display columns instead, `sonarr_season IS NULL`-
            # guarded so this can only ever match a row genuinely never
            # captured, never one that's since been through a real
            # renumber (sonarr_season would be non-NULL there, correctly
            # failing this guard). Backfills sonarr_season/sonarr_episode
            # the moment it matches, same "capture once" shape as
            # season_id/absolute_number below — every later fetch for this
            # row uses the fast/correct lookup above instead of this
            # fallback.
            #
            # Known boundary, not fixed here: migration 36bbe45d39f3 left
            # this NULL for a show that was multi-show-tvdb-routed
            # (_fetch_sonarr_multi_show) at migration time, precisely
            # because its display season/episode are locally-derived
            # per-part numbers, not Sonarr's raw ones. If that show later
            # stops being multi-show-routed (unlinked, merged) and reaches
            # this single-show path for the first time, this fallback
            # compares Sonarr's *raw* season/episode against those
            # locally-derived display values — a coincidental match here
            # would backfill the wrong Sonarr identity onto the row.
            # Nothing re-derives true identity for that case retroactively;
            # see the migration's own docstring.
            existing = conn.execute(
                "SELECT id FROM episode WHERE show_id = ? AND season = ? AND episode = ?"
                " AND sonarr_season IS NULL",
                (show["id"], season_number, episode_number),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE episode SET sonarr_season = ?, sonarr_episode = ? WHERE id = ?",
                    (season_number, episode_number, existing["id"]),
                )
        if existing is not None:
            # A.20 — backfill season_id on a pre-existing row that predates
            # this fix (or was inserted before its season was reconciled).
            # A.25 — same for absolute_number, which no code ever wrote
            # before. Both are pure source-fact capture on a column that is
            # still empty; neither touches any state a human may have set,
            # so "never overwrite an already-tracked episode" still holds.
            conn.execute(
                "UPDATE episode SET season_id = ? WHERE id = ? AND season_id IS NULL",
                (season_id, existing["id"]),
            )
            # 2026-08-15 — same pure source-fact capture as season_id/
            # absolute_number above, gated the same way: only ever fills
            # a row that's never been checked at all (available_checked_at
            # IS NULL), never overwrites state the regular checkpoint-
            # based poller (availability.py) or a real webhook (B.5.1)
            # has already established — those are more authoritative for
            # an episode already being tracked, this is strictly for the
            # gap where neither has ever run against this row yet.
            availability_status, availability_path = _availability_from_sonarr_episode(ep)
            conn.execute(
                "UPDATE episode SET available_via_sonarr = ?, file_path_sonarr = ?,"
                " available_checked_at = ? WHERE id = ? AND available_checked_at IS NULL",
                (availability_status, availability_path, now, existing["id"]),
            )
            # 2026-08-16 — same pure source-fact capture as availability above:
            # only ever fills a row whose title has never been captured at all.
            if ep.get("title") is not None:
                conn.execute(
                    "UPDATE episode SET title = ? WHERE id = ? AND title IS NULL",
                    (ep["title"], existing["id"]),
                )
            if ep.get("absoluteEpisodeNumber") is not None:
                # Overwrites NULL *and* any previously-synthesized value:
                # §5.2 sources as-is whenever a source reports an official
                # number, and only synthesizes "when no source numbering
                # exists" — so a real value always supersedes a guess.
                # Fractional part tells them apart (see
                # _synthesize_absolute_numbers). Caught by a test: without
                # the second clause, synthesis ran first, filled the column,
                # and permanently blocked the real value from ever landing.
                conn.execute(
                    "UPDATE episode SET absolute_number = ?"
                    " WHERE id = ? AND (absolute_number IS NULL"
                    "   OR absolute_number <> CAST(absolute_number AS INTEGER))",
                    (ep["absoluteEpisodeNumber"], existing["id"]),
                )
            continue
        episode_id = ids.generate_id(conn, "e")
        availability_status, availability_path = _availability_from_sonarr_episode(ep)
        conn.execute(
            "INSERT INTO episode"
            " (id, show_id, season, season_id, episode, sonarr_season, sonarr_episode, kind,"
            "  absolute_number, air_date_utc, air_date_source, air_date_raw_sonarr,"
            "  runtime_minutes, available_via_sonarr, file_path_sonarr, available_checked_at,"
            "  title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sonarr', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                episode_id,
                show["id"],
                season_number,
                season_id,
                episode_number,
                # NEXT_UP.md, 2026-08-25 — the immutable raw-numbering
                # capture setEpisodeNumber's own resync-safety depends on
                # (see the `existing` lookup's own comment above). A brand
                # new row has no correction yet, so season/episode (display)
                # and sonarr_season/sonarr_episode (raw) start identical.
                season_number,
                episode_number,
                # A.25 — capture what the source actually says. §5.2's `kind`
                # records how the source files an episode; season 0 is
                # Sonarr/TVDB's specials bucket. Deliberately NOT a behavior
                # input: nothing reads `kind` to decide anything, and
                # `nextUp` orders by air date precisely so an external
                # platform's filing convention can't drive watch order.
                "special" if season_number == 0 else "regular",
                ep.get("absoluteEpisodeNumber"),
                ep.get("airDateUtc"),
                ep.get("airDateUtc"),
                ep.get("runtime"),
                availability_status,
                availability_path,
                # 2026-08-15 — real bug: an episode row created here with
                # no availability check recorded at all left
                # available_checked_at NULL forever unless some other
                # poll happened to reach it, which is exactly the gap
                # this whole fix closes. Stamped now precisely because a
                # real check (this one) just happened, same as every
                # other poll in this codebase already stamps it.
                now,
                ep.get("title"),  # 2026-08-16 — Data thin-client swap's own last gap
                now,
                now,
            ),
        )

    # A.22 — episode-numbering-scheme automatic derivation. Deferred at
    # A.4 for lack of real data to derive from; A.8's own Sonarr fetch
    # (this function) is exactly that data, so it's the natural place to
    # attempt it, the same "on-demand, immediately" pattern as everything
    # else here — not a separate background pass.
    _derive_episode_numbering(conn, show["id"], series, episodes)
    _synthesize_absolute_numbers(conn, show["id"])
    # S2 lazy range-fill: absolute numbers are final, derive season ranges
    # for any season that doesn't have them yet (see season_ranges.py).
    season_ranges.fill_season_ranges(conn, show["id"])


_MULTI_SHOW_MAX_CONTINUATION_GAP_DAYS = 45


def _fetch_sonarr_multi_show(conn, sibling_ids: list[str], episodes: list[dict]) -> None:
    """2026-08-15 — one Sonarr/tvdb id shared by more than one LCARS
    show (a franchise TVDB tracks as one flat series but AniList splits
    into separate parts, one LCARS show per part — Ascendance of a
    Bookworm's four parts, all sharing tvdb 366263, is the real case
    that found this). §3's "the internal database is the source of
    truth... mapped in, never authoritative" means each episode must
    route to whichever sibling's own already-filed episode history it
    actually continues — decided from LCARS's own data — never by
    writing Sonarr's raw numbering into whichever show triggered this
    fetch, which is the exact bug this function replaces.

    Routing rule, per not-yet-filed regular episode: each sibling's own
    most-recently-aired already-filed episode is that sibling's
    "anchor". The new episode routes to whichever sibling has the
    closest *preceding* anchor (`anchor_air_date <= this episode's
    air_date`), provided the gap is within
    `_MULTI_SHOW_MAX_CONTINUATION_GAP_DAYS` — comfortably above a
    normal weekly-airing gap, comfortably below the real multi-month/
    multi-year hiatuses this project has now seen between two parts of
    the same franchise (Bookworm's own Part 3 -> Part 4 gap was ~4
    years). A gap past that threshold, or no sibling with any anchor at
    all yet (the very first time this franchise's split is discovered —
    that split still has to be done by a human once, same as tonight's
    real fix), is never guessed at: a `pending_review` opens instead
    (§3 principle 1 — apply what's confident, flag what isn't, never
    silently misfile). That review path is the one behavior change from
    the old code, which had no threshold and no review path at all — it
    just wrote everything into whichever show was being processed.

    Requires `absoluteEpisodeNumber` on every episode this function
    considers — the one stable, source-reported identity a split
    franchise's episodes carry across LCARS's own per-show renumbering
    (season+episode numbers restart at 1 in each sibling, so they can't
    recognize "have I already filed this one" the way the single-show
    path's own `existing` lookup does). An episode without one is left
    alone entirely — same "genuinely can't do anything" no-op every
    other branch in this module gives a show with no tvdb id at all.
    Season 0 (specials) is excluded outright, same reasoning at a
    different scale: no reliable per-episode continuity signal exists
    for a special the way a regular episode's air date gives one — a
    deliberate, documented scope boundary, not a silent gap.

    Episodes already filed under some sibling (found by `absolute_number`
    match) are treated like the single-show path's own `existing`
    branch: `season_id` backfilled in place when missing, never
    re-inserted, never moved."""
    regular_episodes = sorted(
        (
            ep
            for ep in episodes
            if ep.get("seasonNumber") not in (None, 0)
            and ep.get("episodeNumber") is not None
            and ep.get("absoluteEpisodeNumber") is not None
        ),
        key=lambda ep: ep["absoluteEpisodeNumber"],
    )

    now = util.now_utc_iso()
    touched_shows: set[str] = set()

    def _anchors() -> dict:
        # Recomputed fresh on every call rather than once up front — a
        # new episode routed earlier in this same pass must extend its
        # sibling's own anchor immediately, so a later new episode in
        # the same fetch continues from it correctly instead of every
        # new episode competing against the same stale anchor.
        result = {}
        for sid in sibling_ids:
            row = conn.execute(
                "SELECT season, episode, air_date_utc, absolute_number FROM episode"
                " WHERE show_id = ? AND air_date_utc IS NOT NULL"
                "   AND absolute_number IS NOT NULL"
                " ORDER BY absolute_number DESC LIMIT 1",
                (sid,),
            ).fetchone()
            if row is not None:
                result[sid] = dict(row)
        return result

    placeholders = ",".join("?" for _ in sibling_ids)
    for ep in regular_episodes:
        abs_number = float(ep["absoluteEpisodeNumber"])
        existing = conn.execute(
            f"SELECT id, show_id, season_id FROM episode"
            f" WHERE show_id IN ({placeholders}) AND absolute_number = ?",
            (*sibling_ids, abs_number),
        ).fetchone()
        if existing is not None:
            if existing["season_id"] is None:
                season_row = conn.execute(
                    "SELECT id FROM season WHERE show_id = ? AND season_number = ?",
                    (existing["show_id"], ep["seasonNumber"]),
                ).fetchone()
                if season_row is not None:
                    conn.execute(
                        "UPDATE episode SET season_id = ? WHERE id = ?",
                        (season_row["id"], existing["id"]),
                    )
            # 2026-08-15 — same gap/fix as the single-show path's own
            # existing-row backfill above: only ever fills a row that's
            # never been checked at all.
            availability_status, availability_path = _availability_from_sonarr_episode(ep)
            conn.execute(
                "UPDATE episode SET available_via_sonarr = ?, file_path_sonarr = ?,"
                " available_checked_at = ? WHERE id = ? AND available_checked_at IS NULL",
                (availability_status, availability_path, now, existing["id"]),
            )
            # 2026-08-16 — same pure source-fact capture as availability above.
            if ep.get("title") is not None:
                conn.execute(
                    "UPDATE episode SET title = ? WHERE id = ? AND title IS NULL",
                    (ep["title"], existing["id"]),
                )
            touched_shows.add(existing["show_id"])
            continue

        air_date = ep.get("airDateUtc")
        if air_date is None:
            pending_review.open_or_extend(
                conn,
                "show",
                sibling_ids[0],
                "sonarr_multi_show_routing",
                "sonarr",
                None,
                f"absolute episode {abs_number}: no air date, cannot route across "
                f"{len(sibling_ids)} sibling shows sharing one tvdb id",
            )
            continue

        anchors = _anchors()
        best_show_id = None
        best_gap_days = None
        for sid, anchor in anchors.items():
            if anchor["air_date_utc"] > air_date:
                continue
            gap_days = (
                datetime.fromisoformat(air_date) - datetime.fromisoformat(anchor["air_date_utc"])
            ).total_seconds() / 86400.0
            if gap_days > _MULTI_SHOW_MAX_CONTINUATION_GAP_DAYS:
                continue
            if best_gap_days is None or gap_days < best_gap_days:
                best_gap_days = gap_days
                best_show_id = sid

        if best_show_id is None:
            pending_review.open_or_extend(
                conn,
                "show",
                sibling_ids[0],
                "sonarr_multi_show_routing",
                "sonarr",
                None,
                f"absolute episode {abs_number} ({air_date}) doesn't continue any of "
                f"{len(sibling_ids)} sibling shows' own episode history within "
                f"{_MULTI_SHOW_MAX_CONTINUATION_GAP_DAYS} days — possibly a new part with "
                "no LCARS show yet, needs a human look",
            )
            continue

        anchor = anchors[best_show_id]
        new_episode_number = anchor["episode"] + 1
        season_row = conn.execute(
            "SELECT id FROM season WHERE show_id = ? AND season_number = ?",
            (best_show_id, anchor["season"]),
        ).fetchone()
        episode_id = ids.generate_id(conn, "e")
        availability_status, availability_path = _availability_from_sonarr_episode(ep)
        # sonarr_season/sonarr_episode (NEXT_UP.md, 2026-08-25) deliberately
        # left NULL here, unlike the single-show INSERT above: `season`/
        # `episode` on this path are already this function's own locally
        # derived per-part numbering (`anchor["episode"] + 1`), not a literal
        # Sonarr per-series season/episode — Sonarr's own raw identity for
        # this flat multi-part series is `abs_number` (absolute_number),
        # already what this path's own `existing` lookup above matches on
        # and unaffected by anything setEpisodeNumber ever touches. Nothing
        # here needs the sonarr_season/sonarr_episode resync-safety net.
        conn.execute(
            "INSERT INTO episode"
            " (id, show_id, season, season_id, episode, kind, absolute_number,"
            "  air_date_utc, air_date_source, air_date_raw_sonarr, runtime_minutes,"
            "  available_via_sonarr, file_path_sonarr, available_checked_at, title,"
            "  created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'regular', ?, ?, 'sonarr', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                episode_id,
                best_show_id,
                anchor["season"],
                season_row["id"] if season_row is not None else None,
                new_episode_number,
                abs_number,
                air_date,
                air_date,
                ep.get("runtime"),
                availability_status,
                availability_path,
                now,
                ep.get("title"),  # 2026-08-16 — Data thin-client swap's own last gap
                now,
                now,
            ),
        )
        touched_shows.add(best_show_id)

    conn.commit()
    for sid in touched_shows:
        _synthesize_absolute_numbers(conn, sid)
        # S2 lazy range-fill (see season_ranges.py)
        season_ranges.fill_season_ranges(conn, sid)


def _synthesize_absolute_numbers(conn, show_id: str) -> None:
    """§5.2's synthesis rule (A.25): "When no source numbering exists,
    LCARS synthesizes one as `<preceding regular absolute number>.
    <sequential index by air/publish date>` — e.g. a special airing
    between S1E12 and S2E1 becomes `12.1`; a second one before S2E1
    becomes `12.2`."

    Whole-show recompute rather than incremental, deliberately: the
    indices are positional, so a newly-discovered special landing
    mid-season shifts every later one. Recomputing the lot after each
    fetch is the only way they stay correct, and it is cheap at this
    project's scale (one show's episodes).

    Only ever writes rows whose absolute number this function itself
    synthesized — a real source-reported value (A.25's capture, above)
    is never overwritten, and a synthesized value is always recomputed
    from scratch. The two are told apart by the fractional part: a
    source value is a whole number, a synthesized one never is.
    """
    rows = conn.execute(
        "SELECT id, absolute_number, air_date_utc, season, episode FROM episode"
        " WHERE show_id = ?"
        " ORDER BY air_date_utc IS NULL, air_date_utc ASC, season ASC, episode ASC",
        (show_id,),
    ).fetchall()

    preceding = 0.0
    index_after = 0
    now = util.now_utc_iso()
    for row in rows:
        source_number = row["absolute_number"]
        is_source_value = source_number is not None and float(source_number).is_integer()
        if is_source_value:
            preceding = float(source_number)
            index_after = 0
            continue
        index_after += 1
        synthesized = round(preceding + index_after / 10.0, 4)
        if source_number != synthesized:
            conn.execute(
                "UPDATE episode SET absolute_number = ?, updated_at = ? WHERE id = ?",
                (synthesized, now, row["id"]),
            )
    conn.commit()


def _ensure_seasons(conn, show_id: str, season_numbers: set) -> dict:
    """Reconciles a `season` row (§5.5) for every distinct season number
    just seen in a Sonarr fetch, creating one via `season_mapping.
    reconcile_season()` (A.4/A.20) for any number with no existing row
    yet. Returns {season_number: season_id}, including already-existing
    seasons untouched by this call, for the caller to attach to episode
    rows. Each reconciliation attempt is individually guarded (unlike
    the rest of this module's branch-level `_guarded`) — a Fribb dataset
    hiccup (§5.5, `fribb.load_dataset` can raise with no cache and no
    network) must not abort the Sonarr episode import itself, which is
    this function's actual point; on that failure the season row still
    gets created, just left unmatched, with its own pending_review entry
    for the failure specifically (distinct from a genuine "no candidate
    found" review — same field name, different source, so both remain
    individually traceable)."""
    result = {}
    # Season 0 is Sonarr/TVDB's specials bucket (§5.2), not a season with
    # a cross-service identity — AniList has no "season 0" entry to map
    # to, so Fribb can never match one. A.20 originally reconciled it
    # like any other number, which created one permanently unresolvable
    # pending_review per show with specials (found in the 2026-08-09
    # audit): pure noise in the queue this project routes all human
    # attention through. Skipped entirely now — `episode.season_id` stays
    # NULL for specials, which is the honest answer and exactly what that
    # nullable column is for.
    for season_number in sorted(n for n in season_numbers if n is not None and n > 0):
        row = conn.execute(
            "SELECT id FROM season WHERE show_id = ? AND season_number = ?",
            (show_id, season_number),
        ).fetchone()
        if row is not None:
            result[season_number] = row["id"]
            continue
        try:
            season = season_mapping.reconcile_season(conn, show_id, season_number)
            result[season_number] = season["id"]
        except Exception as e:
            season_id = ids.generate_id(conn, "z")
            now = util.now_utc_iso()
            conn.execute(
                "INSERT INTO season"
                " (id, show_id, season_number, source, matched, manual_override,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, 'unmatched', 0, 0, ?, ?)",
                (season_id, show_id, season_number, now, now),
            )
            pending_review.open_or_extend(
                conn, "season", season_id, "anilist_id", "fribb", None, str(e)
            )
            conn.commit()
            result[season_number] = season_id
    return result


def _derive_episode_numbering(conn, show_id: str, series: dict, episodes: list[dict]) -> None:
    """Automatic numbering-scheme derivation (§5.5), deferred at A.4 for
    lack of data, built here at A.22 once A.8's own Sonarr fetch supplies
    it. Heuristic, deliberately simple (personal-tracker scale, same
    reasoning §6.5's plain-LIKE search already leans on): Sonarr's own
    `seriesType = 'anime'` flag, or any episode actually carrying a
    populated `absoluteEpisodeNumber`, is a direct, reliable signal this
    show uses absolute numbering in Sonarr's own data (the "Sonarr
    absolute-order info" §5.5 names) — anything else defaults to
    ordinary season+episode numbering, Sonarr's own standard convention.
    AniList episode counts (§5.5's other named signal) aren't used here:
    AniList has no numbering-scheme concept of its own to cross-check
    against (each AniList entry is already one season, §5.5) — its
    episode *count* only matters for validating a scheme already derived
    from Sonarr, not for deriving one from nothing, so a not-linked/
    not-configured Sonarr leaves this table unmatched rather than
    guessing off AniList alone, same "no real data, stays honestly
    unmapped" treatment as `season`. Never touches a `manual_override`
    row (§3 principle 6) — same protection `setEpisodeNumberingScheme`
    already gives it.
    """
    existing = conn.execute(
        "SELECT * FROM episode_numbering_mapping WHERE show_id = ?", (show_id,)
    ).fetchone()
    if existing is not None and existing["manual_override"]:
        return

    is_absolute = series.get("seriesType") == "anime" or any(
        ep.get("absoluteEpisodeNumber") is not None for ep in episodes
    )
    scheme = "absolute" if is_absolute else "season_episode"
    now = util.now_utc_iso()

    if existing is not None:
        if existing["scheme"] != scheme:
            pending_review.open_or_extend(
                conn,
                "episode_numbering_mapping",
                existing["id"],
                "scheme",
                "sonarr",
                existing["scheme"],
                scheme,
            )
        conn.execute(
            "UPDATE episode_numbering_mapping"
            " SET scheme = ?, source = 'sonarr', matched = 1, updated_at = ?"
            " WHERE id = ?",
            (scheme, now, existing["id"]),
        )
    else:
        mapping_id = ids.generate_id(conn, "n")
        conn.execute(
            "INSERT INTO episode_numbering_mapping"
            " (id, show_id, scheme, source, matched, manual_override, created_at, updated_at)"
            " VALUES (?, ?, ?, 'sonarr', 1, 0, ?, ?)",
            (mapping_id, show_id, scheme, now, now),
        )
    conn.commit()


# --- Radarr (media_shape = movie) -------------------------------------------


def _fetch_radarr(conn, show: dict) -> None:
    tmdb_id_str = _external_id(conn, show["id"], "tmdb")
    if tmdb_id_str is None:
        return  # nothing to look up without a tmdb id
    cfg = get_current()
    if not cfg.radarr_url or not cfg.radarr_api_key:
        return  # not configured — same as "not linked", not a failure to report

    # §6.7, B.6 — hooked at the actual outbound call, same reasoning
    # _fetch_sonarr's own comment gives.
    try:
        with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
            movie = client.movie_by_tmdb_id(int(tmdb_id_str))
    except radarr_client.RadarrError as e:
        service_health.record_failure(conn, "radarr", str(e))
        conn.commit()
        raise
    service_health.record_success(conn, "radarr")
    conn.commit()
    if movie is None:
        return  # not (yet) in Radarr's own library — not an error, §5.1

    now = util.now_utc_iso()
    poster = next(
        (
            img.get("remoteUrl")
            for img in movie.get("images") or []
            if img.get("coverType") == "poster"
        ),
        None,
    )
    genres = movie.get("genres")
    conn.execute(
        "UPDATE show SET"
        "  poster_url = COALESCE(?, poster_url),"
        "  synopsis = COALESCE(?, synopsis),"
        "  genres_raw = COALESCE(?, genres_raw),"
        "  updated_at = ?"
        " WHERE id = ?",
        (poster, movie.get("overview"), json.dumps(genres) if genres else None, now, show["id"]),
    )
