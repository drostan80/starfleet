# Next up

Working assumption: the system runs as-is; from here it's debugging, new
functions, and reshaping things to taste. Full design/build history archived
to `~/repos/starfleet-archive` (`SCOPE.md`, `BUILD_PLAN.md`, `KICKOFF_PROMPT.md`,
old `todo.md`, plus the two `~/.claude/plans/` files they reference) —
`archive/<file>:<line>` below points into it for the full story.

## Verified (confirmed via real daily use, 2026-08-18 — resurfacing is a new bug, not a reopen)

- [x] Merged-away duplicate shows leaking back onto the calendar — found live 2026-08-20 (Kaiju
      Girl Caramelise / Otome Kaijuu Caraméliser, merged via `applyShowMerge`; the demoted loser
      `s-vdphq9` kept showing up because it sat at `status: planned`, which
      `_HIDDEN_LCARS_STATUSES` never hides — only Paused/Completed/Dropped did, and `tracked` was
      never part of that check at all. `episodesInRange`/`Show.tracked` needed no server change
      (`tracked` was already queryable, confirmed live against v0.1.32); fixed entirely
      client-side in `~/repos/data`: `lcars_client.py`'s `episodes_in_range()` now requests
      `show.tracked`, `app.py`'s `_synthesize_lcars_row` carries it as `lcarsTracked` (fail-open
      `True` when absent, same convention as a missing `lcarsStatus`), and
      `_is_hidden_from_calendar` now hides on `lcarsTracked is False` independent of status. Two
      new tests in `test_app_list_status.py` cover both the hide case and the fail-open-on-absence
      case. Needs a Data restart to pick up — the on-disk episode cache predates the `tracked`
      field and fails open (visible) until the next successful `episodesInRange` fetch overwrites
      it. Built 2026-08-20, committed 2026-08-25 (`~/repos/data` commit `7fcedef`) — had sat
      uncommitted in the working tree since build.
- [x] B.21's Sonarr/Radarr writes (add-show, auto-unmonitor-on-drop).
- [x] v0.1.18/v0.1.19 AniList write-mirror (status/score/episode-progress/delete/rewatch) +
      completion auto-sync.
- [x] Calendar backlog counter / mark-watched display (B.11g).

## Build

- [x] Wire the AniList push for `season.started_at`/`completed_at` — columns exist, push
      isn't wired (`FuzzyDateInput` shape unhandled). (archive/todo.md:1013) 2026-08-25:
      `anilist_client.save_media_list_entry` takes `started_at`/`completed_at` (ISO-8601 UTC
      TEXT, same as the local columns), converts to AniList's `FuzzyDateInput`
      (`_fuzzy_date_input`, the shape that had left this unbuilt). `resolvers.py`:
      `_push_season_started_at`/`_push_season_completed_at`, same no-op-before-login/
      no-op-unlinked/best-effort/pending_review-on-failure shape as `_push_season_score`.
      Wired into every path that already stamps the local column — the three watch
      mutations (via `_stamp_season_started_at`) and both completion triggers
      (`_stamp_completed_at_if_highest_season`, setStatus's manual path; `_try_complete_season`,
      the forward auto-complete path) — each only pushes on the write-once transition, never a
      re-mark. One deliberate exception: `_bulk_mark_all_aired_episodes_watched` (setStatus's
      reverse "mark all aired episodes watched" bulk path) still stamps `started_at` locally
      but passes `push=False` — its `watched_at` is a synthesized "marked completed today"
      timestamp, not a real historical watch date, and pushing it would silently overwrite a
      genuine AniList `startedAt` with today's date — `completed_at` gets no such
      suppression, deliberately: it's the date the status change is happening right now, not a
      guess (see that function's own docstring for the started_at/completed_at asymmetry). 9
      new tests (`test_anilist_client.py`: FuzzyDateInput conversion for both fields,
      omitted-unless-given, x3; `test_server.py`: push on first watch, no re-push on a later
      watch, no push when unlinked, highest-season-only completed_at push, forward-path
      completed_at push, the two-season bulk-path case confirming completed_at still pushes
      while started_at never does, x6) + 6 existing push tests updated to filter out the
      now-additional started_at/completed_at calls their own assertions didn't expect. Full
      suite 865 passed, ruff clean. Deployed as v0.1.34, 2026-08-25.
- [x] Add a real mutation for `tracking_space` — `setTrackingSpace(showId, trackingSpace)`,
      `~/repos/starfleet` v0.1.23 (deployed) + `t` in `~/repos/data`'s show-detail view,
      2026-08-18. (archive/todo.md:1209)
- [x] A client-facing way to correct episode-*number* misalignment (which Sonarr season/episode
      slot an episode is filed under) without a direct DB edit — the air-date half of this line
      was already done (`a`/`A` above); 2026-08-25 closed the rest. `~/repos/starfleet`: new
      mutation `setEpisodeNumber(episodeId, season, episode)` — rejects (never swaps/shifts) if
      the target slot is already occupied by a different episode, no-ops if unchanged, re-points
      any existing `watch_event` rows to the new (season, episode) (the composite FK they're
      keyed against, §5.3), resolves `seasonEntity` against an existing `season` row for the new
      number or leaves it unmatched (never auto-creates one). Real durability problem found
      *while designing* the mutation, not assumed: `episode.season`/`episode.episode` are the
      exact columns `metadata.py`'s `_fetch_sonarr` uses to recognize an already-known row — a
      mutation that only wrote those would get silently undone on the next Sonarr sync (Sonarr
      still reports the episode under its old number, the lookup no longer finds the renumbered
      row, a phantom duplicate gets inserted at the original slot). Fixed with two new immutable
      columns, `sonarr_season`/`sonarr_episode` (migration `36bbe45d39f3`), capturing Sonarr's
      own raw numbering once at first fetch; `_fetch_sonarr`'s existing-row lookup now matches
      on these instead, so a correction survives every future sync. Backfilled from
      `season`/`episode` for existing rows on the single-show fetch path only — excluded (left
      NULL) for any show currently multi-show-tvdb-routed (`_fetch_sonarr_multi_show`,
      Bookworm-style), since those rows' `season`/`episode` were already locally-derived
      per-part numbers, never Sonarr's raw ones, and backfilling them would have stamped a wrong
      value under a column whose whole contract is "what Sonarr actually reports." Two boundaries
      surfaced during build, deliberately logged rather than solved here (both would need a
      dedicated "was this show ever multi-show-routed" marker LCARS doesn't have): a show that
      stops being multi-show-routed after this migration (unlinked, merged) and reaches the
      single-show fetch path for the first time isn't guaranteed a correct match from the legacy
      `sonarr_season IS NULL` fallback (documented on the fallback itself, `metadata.py`); and a
      cross-season `setEpisodeNumber` move to a season with no existing row yet can have its
      unmatched `seasonEntity` silently re-pointed back at the *old* season by `_fetch_sonarr`'s
      own `season_id IS NULL` fallback on the next real sync (documented on the mutation itself,
      `schema.graphql`) — create the target season row first (`setSeasonMapping`) to avoid it.
      The write itself needs `PRAGMA defer_foreign_keys = ON` for the transaction — updating
      `episode.season`/`episode` (the watch_event composite FK's own parent columns) fails with
      `FOREIGN KEY constraint failed` otherwise regardless of statement order; verified
      experimentally that the pragma resets automatically at the next commit/rollback, so it's
      safe to set unconditionally in the resolver with no manual reset needed. `~/repos/data`:
      `n` in `show_detail_screen.py` (episode row only), prompting for a new slot in any of three
      accepted formats (`S1E6`, `1x6`, `1 6`) via `_parse_episode_number`;
      `LcarsClient.set_episode_number()`. 21 new tests across both repos (10 `test_server.py` —
      7 on the mutation itself, 2 on `_fetch_sonarr`'s resync-safety, 1 exercising the real
      mutation end to end against a Sonarr-fetched episode and resync rather than just the
      columns via raw SQL — plus 11 `data`-side: 5 `_parse_episode_number` cases, 6 screen-level
      prompt/error/wiring tests). `~/repos/starfleet` full suite 890 passed;
      `~/repos/data` full suite 495 passed. Ruff clean both repos. `~/repos/starfleet` deployed as
      v0.1.34, 2026-08-25 (same tag as items 1 and 3 above) — DB snapshotted first
      (`lcars.db.bak-20260825-episode-renumber-v0.1.33-pre`), migration `36bbe45d39f3` applied
      cleanly on container start, confirmed against a live GraphQL query afterward.
      `~/repos/data`'s own half had sat uncommitted in the working tree since build — committed
      2026-08-25 (`~/repos/data` commit `f9800b4`). (archive/todo.md:1209)
- [x] ~~`show_merge.py` has the same composite-FK ordering bug `setEpisodeNumber` needed
      `PRAGMA defer_foreign_keys` to avoid~~ — **false positive, logged 2026-08-25, retracted the
      same day.** Claimed by analogy while designing `setEpisodeNumber`, without actually
      re-reading `show_merge.py` first. It already handles this correctly, and has since the
      original B.14 build (`25000de`, predates this session entirely): `merge_shows` and
      `reverse_show_merge` both open with `BEGIN IMMEDIATE` + `PRAGMA defer_foreign_keys = ON`,
      with their own docstring comment explaining the exact same episode/watch_event reasoning
      this entry re-derived from scratch. `test_merge_shows_moves_episodes_and_matching_
      watch_events` (`test_show_merge.py`) exercises exactly this path and passes — confirmed by
      running the full 23-test file, not just reading the code. No fix needed; nothing was
      broken. Left here (struck through, not deleted) as the honest record rather than quietly
      dropping a line that turned out wrong.
- [x] Per-show manual audit trigger from the show-detail view — user's own ask, 2026-08-19,
      after the Anna Pigeon `auditLocalFiles` fix (v0.1.32) landed: that mutation is still
      whole-library-only, no `showId` scope, so fixing one show's stale Sonarr/Radarr path means
      re-walking everything. Needs a scoped LCARS mutation (`auditLocalFiles(showId: ID)` or a
      new single-show variant reusing `_audit_sonarr`/`_audit_radarr`'s per-series/per-movie
      correction logic) plus a trigger key in `~/repos/data`'s `show_detail_screen.py`. 2026-08-25:
      new mutation `auditLocalFilesForShow(showId: ID!)`, `~/repos/starfleet`. `sonarr_client`/
      `radarr_client` already had the single-record lookups needed
      (`series_by_tvdb_id`/`movie_by_tmdb_id`) — no new client methods. `local_audit.py`: the
      whole-library `_audit_sonarr`/`_audit_radarr` loop bodies factored into
      `_audit_sonarr_series`/`_audit_radarr_movie`, reused verbatim by the new
      `audit_local_files_for_show()` for the correction half (one implementation, not two that
      could drift) — routed by the show's own `mediaShape`. Real gap caught in review before
      shipping: the shared functions' own orphan-file filesystem walk can only ever compare
      against episodes LCARS has already fetched, so a real file for a not-yet-fetched episode
      would report as a false-positive orphan — tolerable on the whole-library CLI report an
      operator skims, not on a single keypress fired from the exact show being actively
      inspected; added a `walk_orphans` flag (default `True`, unchanged whole-library behavior)
      and the scoped path passes `False` — `orphanFiles`/`untrackedShows` are always `[]` for
      this mutation, deliberately narrower than `auditLocalFiles`, not a smaller version of the
      same thing. `~/repos/data`: `x` in `show_detail_screen.py` (show row only, mirrors `O`'s
      own scope), `LcarsClient.audit_local_files_for_show()`; `_load`/`_render_options` gained
      an optional `status_override` param (the one action here whose whole point is a summary
      the plain post-reload HINT reset would otherwise wipe). 21 new tests across both repos
      (13 `test_local_audit.py`, 2 `test_server.py` wiring, 6 `data`-side client/screen).
      `~/repos/starfleet` full suite 880 passed; `~/repos/data` full suite 484 passed. Ruff
      clean both repos. `~/repos/starfleet` deployed as v0.1.34, 2026-08-25 (same tag as item 1
      above and the episode-renumbering item below). `~/repos/data`'s own half had sat
      uncommitted in the working tree since build — committed 2026-08-25 (`~/repos/data` commit
      `f34c512`).
- [x] `ops audit-local-files`'s 10s `LcarsClient` HTTP timeout was too short for a real
      whole-library run — found live, 2026-08-20 (see original entry for the exact repro).
      2026-08-25: fixed the same way `_cmd_backfill_availability` already fixed the identical
      bug for its own command (that fix predates this session, B.11f) — `_cmd_audit_local_files`
      now constructs its `LcarsClient` with `timeout=3600.0` instead of the 10s default, matching
      `_cmd_backfill_shows`'s own precedent. One new regression test,
      `test_audit_local_files_uses_a_long_client_timeout`, mirroring
      `test_backfill_availability_uses_a_long_client_timeout` exactly. `~/repos/starfleet` full
      suite 891 passed, ruff clean. Deployed as v0.1.35, 2026-08-25 (same tag as the subscription
      push entry below).
- [x] LCARS→clients webhook push (user's own idea, 2026-08-17, parked for their own research —
      see the old parked entry this replaces), designed and built 2026-08-25 after the user
      brought it back with two concrete cases: mirror the missing/downloading/available episode
      transitions LCARS already receives from Sonarr/Radarr webhooks, and push a newly-added show
      into a client's calendar immediately. Discussed with the user first, not built straight
      off the idea: "webhook" turned out to mean two different shapes — LCARS POSTing to a URL a
      client runs its own listener for (mirrors the Sonarr→LCARS direction exactly, but Data has
      never run a listener and never needed to accept inbound connections before), or a GraphQL
      subscription over WebSocket, where the client connects *out* to LCARS and holds the
      connection open, same direction every other call it already makes. Laid out safest/
      resource-cost/quickest-to-ship for both plus the separate "missed event while
      disconnected" question; user chose subscription + "fine to miss" (no replay/backlog for a
      client that wasn't connected when an event fired — every consumer already has its own
      periodic poll as the real source of truth).

      `~/repos/starfleet`: new module `lcars/events.py` — a tiny in-process pub/sub (topic ->
      set of `asyncio.Queue`s), fire-and-forget, bounded queues (a stalled subscriber drops new
      events rather than blocking the publisher or growing unbounded). New `type Subscription`
      in schema.graphql: `episodeAvailabilityChanged` (fires only on a genuine status
      transition, not a same-status re-poll/re-webhook — `availability.py`'s
      `_apply_episode_availability`/`_apply_episode_availability_multi_show` now compare against
      the row's prior value before publishing) and `showCreated` (fires from `shows.py`'s
      `create_show`/`_promote_stub`, both real "genuinely new to the client" moments, right
      after their own commit). `ariadne.asgi.GraphQL` wired with `GraphQLTransportWSHandler`
      explicitly (the current graphql-transport-ws subprotocol, not ariadne's own default
      `GraphQLWSHandler`, which speaks the older/deprecated one).

      Real security gap found and closed while building this, not assumed: `BearerTokenMiddleware`
      only ever checked `scope["type"] == "http"`, silently letting *any other* scope type
      through unauthenticated — harmless while GraphQL only ever answered plain HTTP, but would
      have left the new WS endpoint wide open the moment `type Subscription` existed. Now covers
      `websocket` scope too (reads the same `Authorization` header via `Headers(scope=scope)` —
      `Request(scope, receive)` asserts `scope["type"] == "http"` internally, an undocumented
      constraint found by hitting it), denying a bad/missing token with `websocket.close` code
      4401 before the handshake ever completes.

      Two more real findings from advisor review, both fixed: the migration's own claim that a
      publish-before-commit race in `_apply_episode_availability` "self-corrects via `_get_episode`"
      was wrong — `~/repos/data`'s actual handler never re-queries the episode, it re-fetches the
      whole visible window instead; docstring corrected to describe what actually consumes the
      event. And a reconnect-resubscribe gap: `subscribe_events` re-sends the same subscription
      ids after a dropped connection, which is correct (graphql-transport-ws ids are scoped per
      connection) but wasn't actually tested — added a two-good-connections test confirming both
      get subscribed, not just the first.

      Cleanup-on-disconnect verified, not assumed: `websockets`'s own reconnecting client wraps
      each connection in `async with`, so a cancelled task properly closes the socket via
      `__aexit__`; a real end-to-end test confirms the server-side `events._subscribers` queue
      is actually deregistered after a client disconnects, not just on an explicit `aclose()`.

      Real regression found and fixed before it ever shipped: the subscription worker is a
      persistent, intentionally-never-completing connection — using `self.run_worker(...)` for
      it (matching every other background job in `~/repos/data`'s `on_mount`) would have hung
      `app.workers.wait_for_complete()` forever in *every* existing test file that configures an
      `lcars_client`, since that bare call awaits every tracked Worker unconditionally. Switched
      to a bare `asyncio.Task` (outside Textual's Worker tracking entirely), explicitly cancelled
      in a new `on_unmount` handler — confirmed against the whole existing `~/repos/data` suite,
      not just the new tests.

      `~/repos/data`: new dependency `websockets>=13` (a genuinely new runtime dependency, not
      already vendored anywhere in this client). `LcarsClient.subscribe_events()` — hand-rolls
      the graphql-transport-ws message protocol (connection_init/ack, subscribe/next/error/ping-
      pong) over `websockets.connect`'s own built-in reconnect-with-backoff iterator, rather than
      a hand-rolled retry loop. `app.py`'s `_lcars_subscription_worker` doesn't care *what*
      changed, just that something did — every event reuses the exact same
      `_trigger_lcars_window_refresh()` call the periodic polls already make, one source of
      truth for "what the calendar shows," not a second rendering path.

      27 new tests across both repos: `~/repos/starfleet` — 6 `test_events.py`, 6
      `test_subscriptions.py` (real WS end-to-end: auth accept/reject, handshake, delivery via a
      real webhook POST and a real `addShow` mutation — not raw DB pokes — plus the disconnect-
      cleanup test above), 2 `test_availability.py` (publish-on-genuine-change-only, both
      directions). `~/repos/data` — 7 `test_lcars_client.py` (`subscribe_events`'s own message
      handling, ping/pong, error tolerance, reconnect + resubscribe), 4
      `test_app_lcars_subscription_wiring.py`. `~/repos/starfleet` full suite 905 passed,
      `~/repos/data` full suite 505 passed, ruff clean both repos.

      `~/repos/starfleet` deployed as v0.1.35, 2026-08-25 (same tag as the ops audit-local-files
      timeout fix above; DB snapshot skipped — no migration in this deploy, `36bbe45d39f3` had
      already shipped with v0.1.34). Verified live on `tiny`: `lcars`/`ops` containers recreated
      cleanly (0 restarts), `curl .../graphql` returns 401 (auth middleware enforcing on both
      HTTP and WS scopes as intended), and a direct in-network request from `ops` confirmed
      connectivity. `ops`'s first sweep attempts logged `Could not connect to LCARS` — all
      timestamped to the exact container-start instant, before `lcars` had finished its own
      startup (both containers recreated in the same `docker compose up`); `ops`'s existing
      retry-next-interval handling absorbed it with no crash/restart, confirmed resolved by the
      following direct connectivity check. Pre-existing race on every deploy that recreates both
      containers together, not a regression from this change.

      `~/repos/data` runs editable-installed straight out of this checkout (`data` alias ->
      `.venv/bin/data`, no separate deploy step) — `websockets>=13` confirmed installed
      (17.0.1) and `LCARS_SUBSCRIPTIONS` importable, 2026-08-25.

      Subscription verified live end-to-end, 2026-08-25: the real `DataApp` on_mount connects to
      production, subscribes to both topics, and its worker task stays alive/awaiting (the
      `run_worker`-hang regression is confirmed gone). Auth enforced — wrong bearer → HTTP 403 at
      handshake. Actual event *delivery* not manufactured: Sonarr queue was empty so nothing
      fired naturally, and per the user that last hop is left to the existing WS end-to-end tests
      rather than a production-polluting trigger.
- [x] Show-detail view: seasons with all episodes, mark watched per-episode and per-season,
      move status/score at the show/season level individually — built in `~/repos/data`
      (`show_detail_screen.py`, `enter` on the show browser), 2026-08-18.
- [x] Show-detail view: mark a movie watched from the screen itself — gated for now, `w` has
      no episode row to act on for a `MOVIE` show; needs `Show.watchEvents`-based undo too,
      not just the episode-scoped lookup the episodic path reuses. 2026-08-25: turned out
      `~/repos/starfleet` already had everything this needed — `Show.watchEvents`, its
      resolver, and `addWatchEvent(showId, season: null, episode: null)`'s movie support all
      pre-existed — so this was `~/repos/data`-only, **no server change, no deploy**.
      `show_seasons_and_episodes` now also fetches `watchEvents(last: 1)`, flattened to a
      single `watchEventId` (same "last = most recently inserted" ordering
      `latest_watch_event_id` already relies on for episodes). `show_detail_screen.py`: `w`/`u`
      on the show row now call `addWatchEvent`/`deleteWatchEvent` directly for a MOVIE show
      (`_mark_movie_watched`/`_unmark_movie_watched`, `DetailRow.movie_watch_event_id`); the
      show row itself now renders a ✓/○ watched icon for movies, same convention
      `_format_episode_row` already uses. Real semantics question surfaced during build and
      put to the user rather than guessed: marking a movie watched does NOT auto-complete its
      status or push to AniList (LCARS's own auto-complete, `_try_complete_season`/
      `_try_complete_show`, only fires when `addWatchEvent`'s `season is not None` — always
      `None` for a movie) — user's explicit call was to leave that asymmetry alone for now,
      status stays a separate `m`-leader move; documented on `_mark_movie_watched` itself so a
      future reader doesn't mistake "✓ but still Planned" for a bug. 8 new tests
      (`test_lcars_client.py`: watchEventId flattening, present/absent; `test_show_detail_
      screen.py`: mark/unmark send the right mutation, no-op unmark with no event, three
      `build_rows` icon tests) all passing, ruff clean, full `~/repos/data` suite 478 passed.
      Had sat uncommitted in the working tree since build — committed 2026-08-25 (`~/repos/data`
      commit `0421285`).
- [x] Manual schedule editing from the show-detail view: set weekly time + first air date per
      season (`A`), per-episode offset from the original schedule (`a`, +1 week/+2 days…),
      offer to shift all subsequent episodes when one moves — `~/repos/data`
      `show_detail_screen.py`, 2026-08-18. `A` refuses on season 0 (Sonarr's specials bucket,
      no real weekly cadence there) — use `a` per-episode for those.
- [x] PC.2, Trakt half — one-time historical import from a Trakt export zip run: 366 shows
      created, 62 matched to existing shows, 419 status writes, 13,112 watch events (real
      historical timestamps) + 10,211 synthesized episode rows. Sword Art Online excluded
      (already AniList-tracked). `scripts/import_trakt_history.py`, 2026-08-18.
- [x] PC.2, AniList-score half — historical import of personal AniList scores into LCARS's own
      missing score fields (AniList primary, per PC.2). 2026-08-26: `scripts/import_anilist_scores.py`,
      built to the same one-time-script shape as `import_trakt_history.py` (real `--dry-run` against
      a throwaway DB copy, `--apply`, per-pass counts, idempotent — only ever touches rows whose
      score is currently NULL, never overwrites a manual `setScore`). Direct SQL, no outbound write:
      routing through `setScore`/`setSeasonScore` would push every imported score *back* to the
      user's live AniList+MAL accounts (the data is coming *from* AniList), same reasoning the Trakt
      script's docstring already spells out. Two passes: season (`season.score` per `anilist_id` —
      the canonical granularity `_push_season_score` reads; no `score_change` row, that table is
      show-level only) then show (`show.score` — the Data client's headline "Score" — set only when
      a show's linked seasons' scores are unambiguous, divergent shows reported and left for manual;
      one `score_change` row per show at `changed_by='anilist_import'`, symmetry with Trakt's
      `trakt_import`). Three data-corruption traps handled explicitly: AniList returns `0` (not null)
      for an unscored entry → treated as absent; the `* 5` POINT_100 assumption in the existing push
      is now *asserted* (`fetch_score_format` == `POINT_100`) before any write, aborting with a
      clear "real bug to fix" message otherwise; POINT_100÷5 lands off LCARS's quarter-point grid so
      every value is re-rounded with `setScore`'s own `round(x*4)/4` (lossy round-trip, documented).
      `anilist_client`: `score` added to `fetch_my_anime_list` (additive, like `progress` was) +
      new `fetch_score_format`. 12 new tests (`test_import_anilist_scores.py` x8 against a migrated
      DB, `test_anilist_client.py` x2 + 2 existing updated), full suite 915 passed, ruff clean.
      **Applied to production 2026-08-26.** Ran on `tiny` as a throwaway container off the live
      0.1.35 image with the updated `lcars` package overlaid via `PYTHONPATH` (the deployed image
      predates the `anilist_client` changes) and the real `config/`+`db/` volumes mounted — so
      `config.load_config()` picked up the real AniList token automatically. Same
      stop→apply→restart pattern the earlier direct-DB data fixes used: snapshot
      (`lcars.db.bak-20260826-anilist-scores`), `docker stop lcars ops` (SQLite — no concurrent
      writer), `--apply`, `docker start lcars ops`. **scoreFormat assertion passed → the account
      really is POINT_100, so the existing push's `* 5` was never wrong** (the one thing that would
      have been a separate bug). Results: 1122 AniList entries with a real score → 1109 seasons
      filled (of 1425 linked; 316 linked-but-unscored-on-AniList left NULL), 1018 anime shows
      filled (of 2373; 1320 with no scored AniList season left NULL), 35 divergent shows'
      `show.score` left blank per the user's explicit call ("when divergent, score the season
      accordingly, the show as a whole stays un-scored") — their per-season scores still filled.
      `already_scored: 0` on both passes confirmed nothing was overwritten (this was genuinely the
      first time scores flowed *in*). 1018 `score_change` rows written at `changed_by='anilist_import'`.
      Verified on-disk post-apply (counts + Frieren spot-check: show.score blank, S1=20.0/S2=18.5/
      S3=NULL) and LCARS confirmed back up serving 200s. No LCARS server deploy needed; the
      `anilist_client` additions (`score` field + `fetch_score_format`) ship with the next tag,
      the import script rode along as a one-off and isn't baked into the image.
- [x] PC.2, MAL-legacy-scores half — resolved as *not needed*, 2026-08-26 (user's explicit call):
      nothing is unique to MAL. The user mirrors MAL *from* AniList with a separate external tool,
      so every MAL score already has an AniList origin — the AniList-score half above imported all
      of it, and a "scores unique to MAL" import would find an empty set. This completes PC.2's
      score import entirely (Trakt history + AniList scores done, MAL-unique a confirmed no-op).
      Forward note: LCARS is intended to absorb the AniList→MAL mirroring itself over time — its
      B.10 MAL push (`_push_mal_show_score`/`_push_mal_show_status`) already mirrors *new*
      score/status changes to MAL on `setScore`/`setStatus`; the separate tool still covers the
      bulk/historical mirror for now. That growth is a *push/write* feature, distinct from PC.2's
      one-time *import*, so it lives under Ideas below, not here.
- [x] AniList `synonyms` field — last gap in LCARS's AniList read coverage, plus a per-show
      display-title override (user's request 2026-08-26). Deployed v0.1.36. **Synonyms**: AniList
      `Media.synonyms` → new `show_synonym` child table (delete-then-insert re-sync in metadata.py,
      `UNIQUE(show_id, synonym)`; the list legitimately grows, unlike the write-once title columns),
      exposed as `Show.synonyms`. Fed into the three places it earns its keep: aninote note-matching
      candidate titles (`~/repos/data`, on-demand `show_detail` fetch at `n`-press), `service_presence`'s
      fuzzy Sonarr/Radarr catalog matcher (the user's own case — a planned sequel sitting in Sonarr
      under an arc/other-language name), and the `search` query (EXISTS subquery). No merge handling
      (merge only demotes a loser, FK stays intact; winner re-syncs from AniList); added to
      export_import + hard-delete purge. **Display title**: nullable `show.display_title_override`
      + `setDisplayTitle` mutation; `displayTitle` = override if set, else `title_{primary_title}`
      (default unchanged: english-first, romaji fallback). `_computed_display_title` shared by the
      field and confirmHardDelete's retyped-title check. `~/repos/data`: `T` in show-detail opens a
      picker over the show's titles+synonyms+custom+clear. Local-only, no AniList/MAL push. Migration
      `b3f9c2a7d1e4`. LCARS suite 921 passed, Data 516 passed, ruff clean, `~/repos/starfleet`
      `e0360f2` / `~/repos/data` `36e04a3`. **Verified live post-deploy**: score data intact
      (1018 shows / 1109 seasons), `show_synonym` correctly empty (go-forward capture), then
      refreshed Urusei Yatsura (anilist 1293) → synonyms `["...","Lamu","Lamù",...]` returned by the
      real read path, and `setDisplayTitle` → it now displays as "Lamu" (the user's own example).
      DB snapshot `lcars.db.bak-20260826-pre-v0.1.36-synonyms` taken before deploy. This tag also
      carried the earlier PC.2 `anilist_client` score additions (score field + fetch_score_format).
- [x] `tracking_space` multi-place + MAL side of AniList/MAL drift detection — both closed
      together by the MAL bidirectional sync, deployed v0.1.37, 2026-08-26. **No tracking_space
      schema change** (user's call): a show already syncs to whichever services its seasons carry
      ids for (`anilist_id` / `mal_id`), and that id-presence *is* the multi-place mechanism, both
      directions. **Scope: status + episode progress only** — no score reverse-sync exists in
      *either* direction (AniList→LCARS score reconciliation was never built either; only the
      forward push of score to both on a local `setScore` is live, unchanged). Built at once:
      forward (`mal_client.update_my_list_status` gains `num_watched_episodes`;
      `_push_mal_show_episode_progress` wired next to every AniList progress push — status+score
      MAL push already existed, B.10); reverse (`mal_client.fetch_my_list` paged +
      `mal_reconcile.reconcile_mal_progress`, the `pollMalList` mutation, its own hourly Ops loop —
      MAL has no activity feed so it diffs the whole list each tick). Hub: `watch_reconcile`'s apply
      core extracted to `_apply_remote_list`, reused by both reconcilers (shared dup-id-exclusion /
      highest-season / unaired-episode hardening — no drift); after a change lands in LCARS it's
      pushed *onward to the other service only* (AniList→MAL, MAL→AniList) via direct client calls
      (no circular import). Loop-safe: status/progress are exact (no lossy scale), only real diffs
      propagate, converged state pushes nothing. A stale MAL can only push progress *forward* (never
      un-watches). 14 new tests, full suite 935 passed. **Verified live post-deploy**: controlled
      manual `pollMalList` before enabling the Ops loop — first run applied 17 status + 54 episode
      backfills across 1373 seasons (0 conflicts; MAL was a close mirror, slightly ahead on ~2
      dozen shows, all mirrored onward to AniList), second run 0 changes (converged, no
      oscillation); then ops brought to 0.1.37, loops running clean. Snapshot
      `lcars.db.bak-20260826-pre-v0.1.37-mal`. `~/repos/starfleet` `9efa01e`.
- [~] Hierarchical season subdivision for legitimate cross-source granularity mismatches
      (Bookworm/Mushoku Tensei-style cases). (archive/todo.md:1175) **Model chosen + design note
      written, 2026-08-26 — awaiting decisions before build.** User's chosen model: LCARS is source
      of truth and subdivides its seasons to the *finest* linked source (so seasons are followable
      individually like AniList, and MAL's split cours 1/2 come out right), with **absolute-episode
      ranges** as the reconciliation key — AniList/MAL mapped per-range, Sonarr/TMDB linked at show
      level only with episode sync routing by absolute number into the right sub-season. Foundation
      partly exists (`episode.absolute_number`, `availability._route_episode_availability`,
      `metadata._fetch_sonarr_multi_show`) but at the *show* level (Bookworm = sibling `show` rows
      sharing a tvdb id), not the *season* level this needs. Design note (8 open decisions +
      illustrative schema + slice plan) published as an artifact:
      https://claude.ai/code/artifact/01f25b72-836c-40ef-86d0-d89d0a46acd5 . **Not built this
      session — deliberately** (advisor + reality): the feature is a single non-deployable change
      (schema + reconcile + Sonarr sync + the just-shipped MAL/AniList paths all move together),
      the chosen model is a target not a migration spec, and one part of it ("Sonarr/TMDB show-level
      only") collides with the 2026-08-25 multi-show-sibling routing. The three blocking decisions
      to settle first: **D1** sub-season representation vs `season_number INTEGER`/
      `UNIQUE(show_id,season_number)`; **D2** whether Bookworm's 4 sibling shows migrate into one
      (fate of `_fetch_sonarr_multi_show`/`_route_episode_availability`, 8 tests); **D3** per-source
      id mapping when `season.anilist_id`/`mal_id` (single-valued, load-bearing in the shared
      `_apply_remote_list`) can't express it — likely a new `season_external_id` table. Once D1-D3
      are answered, Slice 1 is an inert schema migration.
      **Slice 1 shipped 2026-08-26 (v0.1.38, migration `c7a1e2f4b8d3`)** — inert schema foundation:
      `season.abs_start`/`abs_end` (D3 range), `season.parent_season`/`sub_ordinal` (D1 fallback),
      and a new `season_external_id(season_id, service, external_id)` table (external_id NOT unique
      across seasons, so a coarse source's one entry links every fine season in its range). All
      nullable/empty, nothing reads it yet (season.anilist_id/mal_id stay live); wired into
      export_import + hard-delete purge; merge handling deferred to Slice 3. Deployed inert,
      verified live (schema present, data intact). **Remaining slices** (each gated on its own
      decisions D4-D8): S2 populate ranges + backfill existing shows; S3 point `_apply_remote_list`
      at `season_external_id` (rerun MAL+AniList reconcile suite) + merge handling; S4 season-level
      Sonarr range routing + collapse Bookworm's 4 sibling shows into one (D2); S5 subdivision
      trigger + expose sub-seasons in Data. Carry-forward safeguard: a coarse-source entry is only
      "complete" when every fine season in its range is. **Remaining decisions settled 2026-08-26:**
      D4 range source = AniList episode counts primary, Sonarr/TVDB fallback (future/out-of-scope:
      scene numbering); D5 migration = hybrid (backfill known cases now, lazy for the long tail);
      D7 trigger = flag-for-confirmation via pending_review, never auto-split. D6 (Sonarr→sub-season
      routing) and D8 (audit shipped paths) are implementation, folded into S4/S3. Design note
      artifact updated to match.
      **Slice 2 built 2026-08-27** — range population + hybrid backfill (D4/D5). Three parts:
      (a) `scripts/backfill_season_ranges.py` (`--dry-run`/`--apply`): fills `season.abs_start`/
      `abs_end` from observed integer `episode.absolute_number` values (MIN/MAX per season — the
      real data, not accumulated AniList counts); mirrors `season.anilist_id`/`mal_id` into
      `season_external_id`; prints validation report: numbering gaps (range width vs observed
      episode count), AniList width validation (batch-fetches AniList's per-entry `episodes` count
      and compares to range width — flags Mushoku Tensei / Fire Force shape), and range integrity
      (ascending, non-overlapping, gap-free ranges + no cross-boundary episodes). Season 0 gets
      NULL (specials have no meaningful absolute numbering). (b) Dual-write hooks in the three
      sites that write `season.anilist_id`/`mal_id` (`season_mapping.reconcile_season`,
      `metadata._upsert_season`, `resolvers.setSeasonMapping`) →
      `season_ranges.upsert_season_external_id()` so the mapping table stays current going forward
      without S3 needing its own backfill. (c) Lazy range-fill hook after
      `_synthesize_absolute_numbers` in both `_fetch_sonarr` and `_fetch_sonarr_multi_show` →
      `season_ranges.fill_season_ranges()` for D5's long-tail coverage. New module
      `season_ranges.py`. 26 tests (`test_season_ranges.py`, including 5 AniList-mocked width
      validation tests + TV-show exclusion), full suite green (961). Still inert — nothing
      reads `abs_start`/`abs_end` or `season_external_id` yet (S3 switches reconcile reads).
      **Slice 3 built 2026-08-27** — point `_apply_remote_list` at `season_external_id` +
      merge handling. Three parts: (a) `watch_reconcile._apply_remote_list`: `id_key` param
      replaced by `service` (`"anilist"`/`"mal"`); the `season.{anilist_id,mal_id}` column
      lookup replaced by a `season_external_id` JOIN — season.anilist_id/mal_id stay live (D3
      decision: dual-write keeps them in sync, retirement is a separate later decision). The
      dup-id guard field name changes from `"anilist_id_conflict"` → `"anilist_id_conflict"` (same
      string, `{service}_id_conflict`). (b) `_push_status_onward`/`_push_progress_onward`: same
      column-→-table read switch; no more `id_col` f-string interpolation. (c) `show_merge.py`:
      comment clarifying `season_external_id` follows season moves automatically via FK (season.id
      is unchanged by `UPDATE season SET show_id`); in the skipped-season block (winner already has
      this season_number), the loser's ghost season's `season_external_id` rows are now explicitly
      deleted — without this, they would permanently trigger the dup-id guard on every future
      reconcile run. 2 new merge tests (`test_show_merge.py`). All test helpers that inserted
      `season.anilist_id`/`mal_id` via raw SQL now also write to `season_external_id`
      (`test_watch_reconcile._season`, `test_mal_reconcile._add_season`, the two
      `test_server.py` tests that bypassed `setSeasonMapping`, and two inline fixtures in
      `test_watch_reconcile.py` that set `mal_id` via UPDATE). Full suite 963 passed, ruff
      clean. **Deployed as v0.1.40, 2026-08-27.** The S2 backfill had already been applied to
      the live DB before v0.1.39 shipped (1424 AniList + 1384 MAL rows in `season_external_id`),
      so S3's read switch landed on real data immediately. Both containers (lcars/ops) recreated
      cleanly (0 restarts), `{"data":{"__typename":"Query"}}` confirmed live. No migration in
      this deploy (schema unchanged from c7a1e2f4b8d3).
      **Slice 4a built 2026-08-27** — abs-range routing in `_route_episode_availability`.
      New `_apply_episode_availability_by_abs_range`: finds the season whose `abs_start..abs_end`
      contains the incoming `absoluteEpisodeNumber`, computes the relative per-season episode number
      (`abs − abs_start + 1`), and delegates to `_apply_episode_availability`. Updated
      `_route_episode_availability` tries range routing first whenever `absoluteEpisodeNumber` is
      present; falls back to raw season/episode matching for single-show shows with no abs ranges
      (overwhelmingly common); falls back to `_apply_episode_availability_multi_show` as last resort
      for multi-show shows without ranges. Works uniformly for both the pre-collapse 4-sibling shape
      (4 shows, each season_number=1 with a range) and the post-collapse single-show shape (1 show,
      4 ranged seasons) — the range lookup spans whatever shows share the tvdb_id regardless of
      count. 3 new tests (single-show/multi-season, multi-show siblings, fall-through-no-range).
      49 availability tests pass. Full suite 1012 passed, ruff clean.
      **Slice 4b built + applied 2026-08-27** — `scripts/collapse_bookworm.py` collapses
      Bookworm's 4 sibling show rows into winner `s-2k4jb6` (Part 1) with seasons 2/3/4 for the
      three losers. Dry-run verified on production DB copy first; applied live on v0.1.41 with
      containers stopped + DB snapshotted (`lcars.db.bak-20260827-pre-s4b-bookworm-collapse`).
      Verified post-apply: 4 seasons (abs 1–14, 15–26, 27–36, 37–60), 64 episodes, 55 watch
      events, 0 loser show rows, winner→OVA relation kept, dead-stub→winner repointed. Bookworm
      is now one show with 4 seasons — `_fetch_sonarr_multi_show` and
      `_apply_episode_availability_multi_show` are now dead code for this show (range routing takes
      over via S4a). DB shipped as part of v0.1.41 (no schema change).
      **S5 (v0.1.42, 2026-08-27)**: subdivision trigger + abs-range GraphQL fields.
      `season_ranges.check_subdivision_widths` ported `_fetch_anilist_episode_counts` from the
      backfill script into the live module, then batch-fetches AniList episode counts for all ranged
      seasons and opens/extends `pending_review` (field `season_subdivision`) when width ≠ AniList
      count. Idempotent via `pending_review.open_or_extend` + `already_resolved_with`. Skips airing
      seasons (null episodes) and dead AniList ids (separate concern). `pollSeasonSubdivision`
      mutation + `SeasonSubdivisionPollResult` type in schema; resolver delegates to
      `check_subdivision_widths`. Ops: `poll_season_subdivision()` method + `run_season_subdivision_
      once()` wired into `run_daily_and_weekly_once` (same hourly tick as `pollAnimeSchedule`).
      `absStart`/`absEnd` nullable Int fields added to `type Season` — auto-resolved by
      `convert_names_case=True`, no extra resolver code. 7 new tests; 974 total pass.
      **Score reverse-sync — AniList direction (v0.1.43, 2026-08-27)**: new
      `src/lcars/score_sync.py` module. `check_anilist_score_drift(conn)` batch-fetches the
      viewer's full AniList list (one call; same dataset `fetch_my_anime_list` already returns
      for the reconcile sweep), compares each season's AniList score against what LCARS would have
      pushed (`round(effective_lcars × 5)`), and opens a `pending_review` (field "score",
      entity_type "season") when they differ. Scale guard: compares `int(anilist_score)` against
      `round(effective_lcars * 5)` so that quarter-point pushes AniList snaps to integer don't
      produce false positives. Handles "LCARS has no score but AniList does" (inbound new score)
      and "AniList score changed after LCARS pushed" (drift) the same way. `already_resolved_with`
      guard: if the human already resolved this exact proposed LCARS-scale value, no re-open.
      `proposed_value_chain[-1]` is the LCARS-scale score (e.g. "18.0") Data reads and passes to
      `setSeasonScore` before calling `resolvePendingReview` — no new mutation needed.
      `pollScoreSync` mutation + `ScoreSyncPollResult { anilistChecked anilistFlagged }` type.
      Ops: `poll_score_sync()` method + `run_score_sync_once()` wired into
      `run_daily_and_weekly_once` (same hourly tick). 13 new tests.
      **Score reverse-sync — MAL direction (v0.1.45, 2026-08-27)**: `check_mal_score_drift(conn)`
      in `score_sync.py`. Pulls the viewer's full MAL list (`mal_client.get_user_anime_list`), joins
      against `season.mal_id`, compares `round(effective_lcars / 2)` against the stored MAL integer
      (0–10 scale). Banker's-rounding guard: LCARS 17.0 → `round(17/2) = round(8.5) = 8` (rounds
      to even in Python) — MAL stores 8 → consistent, no false positive. Opens `pending_review`
      (field "score") on drift; same `already_resolved_with` + `open_or_extend` guards as the
      AniList side. `ScoreSyncPollResult` extended with `malChecked: Int!` and `malFlagged: Int!`;
      `pollScoreSync` now covers both directions in one call. Skips quietly when
      `mal_access_token` not configured. 5 new test classes.
      **Data review `a: apply score` (2026-08-27, `~/repos/data`)**: `review_screen.py`. When the
      focused pending_review has `entityType == "season"` and `field == "score"`, pressing `a`
      prompts for an optional note (blank = no note, Enter applies immediately, no cancel path),
      then calls `setSeasonScore(entityId, proposed)` + `resolvePendingReview`. Score-type hint
      shown in the detail panel; silently no-ops for non-score reviews. 5 new tests.
      **Drop `parent_season`/`sub_ordinal` (migration 28cbe21e9b6e, v0.1.47, 2026-08-27)**:
      both columns were dead (zero rows written; D1 ended up using real `season_number` directly).
      Safe recreate pattern: `CREATE TABLE season_new` (full schema minus the two columns),
      `INSERT INTO season_new SELECT …`, `DROP TABLE season`, `ALTER TABLE season_new RENAME TO
      season` — avoids SQLite 3.26.0+ FK-rewrite behaviour that corrupts `episode`/
      `season_external_id` FK text when the original table is renamed first. `PRAGMA foreign_keys
      = OFF` wraps the whole block. `downgrade()` adds both columns back as nullable.
      **S5 done, 2026-08-28**: checked what Data actually renders for Bookworm post-collapse —
      `show_seasons_and_episodes` fetches `seasonNumber` (not `absStart`/`absEnd`); the 4 seasons
      display as distinct numbered seasons in the show-detail screen, which is correct and fully
      navigable. No client work needed — sub-season display is not required for usability.
- [x] Recent grabs screen (`G` key, v0.1.47 / `~/repos/data`, 2026-08-27): two-column modal
      (Sonarr left, Radarr right), 20 events per page, `[` goes back (older), `]` goes forward
      (only active when page > 1), `r` resets to page 1 + reloads, `q`/Esc closes. Both columns
      share a single page counter. New `recentGrabs(service, page, pageSize)` query in LCARS schema
      + resolver (`resolve_recent_grabs`) — proxies Sonarr/Radarr `/history?eventType=grabbed` via
      the existing `sonarr_client`/`radarr_client`, returns empty list on unconfigured service or
      API error. Two Ariadne quirks hit in deploy: (1) `pageSize` arg arrives as `page_size` in the
      Python resolver (Ariadne converts all GraphQL camelCase args to snake_case before dispatch);
      (2) `convert_names_case=True` in `server.py` means resolver return-dict keys must also be
      snake_case (`release_title`, `season_number`, `episode_number`) even though the GraphQL
      response is camelCase. Both fixed by v0.1.47. `GrabsScreen(ModalScreen)` in `grabs_screen.py`;
      `DataApp` wires `G` binding + `action_recent_grabs`. `LcarsClient.recent_grabs()` sends the
      GraphQL query and maps camelCase response keys. `asyncio.gather` runs both service fetches
      concurrently. 17 tests (7 pure-helper, 10 screen-behaviour). Deployed as v0.1.45→v0.1.47
      (three deploys: schema, pageSize fix, dict-key fix).

      **Redesign (v0.1.49 / `~/repos/data` commit `66c859b`, 2026-08-27)**: Static columns
      replaced with selectable `OptionList` widgets; each row now shows title + `SxxExx` (TV) +
      `air: YYYY-MM-DD` (if known) + `dl: YYYY-MM-DD HH:MM`; release title, quality, and the
      ↳ line all stripped. Enter on a highlighted row opens the file in mpv via `player.launch()`;
      items with no file path (not yet imported) show a `[no file]` marker and Enter writes a brief
      status note instead of launching. LCARS `GrabEvent` type: added `airDate: String` (episode
      `airDateUtc` for Sonarr; null for Radarr) and `filePath: String` (batch-looked up from the
      LCARS episode table — `_grab_file_paths_sonarr`: two queries per call, tvdb_id→show_id then
      show_id+sonarr_season+sonarr_episode→file_path_sonarr; `_grab_file_paths_radarr`: analogous
      via tmdb_id→file_path_radarr; both null-safe, return None when not imported). 21 tests;
      `~/repos/starfleet` 1004 passed, `~/repos/data` 21/21 grabs tests passed. Deployed as
      v0.1.49.
- [ ] B.5.3a — scoped/targeted reconcile instead of always sweeping the full AniList list;
      good-to-have, not urgent.
- [x] Open-in-Sonarr/Radarr/AniList, correctly this time: LCARS stores real deep links
      (`show_external_id`, `Show.externalIds`) — written on `addShowWithArr` and backfilled by
      the existing monthly Sonarr/Radarr catalog sweep (`pollCatalogServicePresence`, B.7) so
      already-tracked shows get linked too, not just new adds. `o` in `~/repos/data`'s
      show-detail view opens whichever link is picked in the system browser — Data never calls
      Sonarr/Radarr/AniList itself, only LCARS. Deployed as v0.1.24, 2026-08-18.
- [x] Not-yet-followed Sonarr/Radarr shows offer the real "Add New" page (pre-filled via
      `?term=tvdb:{id}`/`tmdb:{id}`) via `o` instead of no link at all — synthetic, non-persisted
      `Show.externalIds` edge (`service: "sonarr:add"`/`"radarr:add"`), self-healing once a real
      link exists. Never auto-adds. `~/repos/starfleet`/`~/repos/data`, deployed as v0.1.25/26,
      2026-08-18.
- [x] `sonarr_public_url`/`radarr_public_url` (config.py) — real bug caught live right after
      deploying the above: deep/add links were built from `sonarr_url`/`radarr_url`, LCARS's own
      *internal* docker-network outbound-API addresses, unreachable from any real browser. Now a
      separate, explicit config value; not configured means no link at all rather than a guessed
      one. Set on `tiny` to `http://192.168.0.152:{8989,7878}` and curl-confirmed reachable.
      Deployed as v0.1.26, 2026-08-18.
- [x] `O` — a dedicated "open this show's Sonarr page directly" shortcut, no picker, alongside
      the existing lowercase `o` (lists every known service) — real gap when all you want from a
      show is Sonarr specifically. `~/repos/data` only, no server change: reachable from all
      three places a show can be selected (the calendar, the list browser, the show-detail
      screen). New `links.py` — `pick_sonarr_link` (prefers the real followed-in-Sonarr edge
      over the synthetic not-yet-added `sonarr:add` one) and `rewrite_host` (swaps a URL's host
      for `Config.home_server_host`, leaving path/query untouched — the real Sonarr title slug is
      information only LCARS has post-thin-client) — both pure, shared by all three call sites
      rather than three hand-rolled copies. New `LcarsClient.external_ids()` (none of the three
      call sites already hold a url-bearing `externalIds` edge for their own rows). New
      `Config.home_server_host` field (`[network]` section / `DATA_HOME_SERVER_HOST` env var) —
      the network address *this* Data instance reaches the home stack through, distinct from
      LCARS's own server-side `sonarr_public_url` (one value for every client; this is the
      per-instance override) — hardcoded default for now per the user's own "for now let's
      hardcode the local 192.168.0.152" call. 2026-08-20 built, 2026-08-25 committed
      (`~/repos/data` commit `7d5fb99`) — had sat uncommitted in the working tree since build.
- [x] `backfillTvdbIds` — TV-only tvdb_id backfill via a reverse lookup (anilist_id -> tvdb_id)
      against the same Fribb dataset already cached for the forward direction (A.4/B.2). Upgrades
      `?term=<title>` to the more precise `?term=tvdb:{id}` for shows never added to Sonarr.
      Never guesses (skips a genuinely ambiguous Fribb entry). Rides Ops's hourly tick, not a new
      interval. Movies NOT covered — no anilist->tmdb path exists in this codebase, a real gap.
      `~/repos/starfleet`, deployed as v0.1.28 and manually triggered once — 936 shows backfilled
      with a real tvdb_id in one pass, 2026-08-18.
- [x] Add-link title fallback: most of the library predates Sonarr/Radarr integration and has no
      tvdb_id/tmdb_id captured at all (found live via Akame ga Kill!) — `sonarr:add`/`radarr:add`
      now falls back to `?term=<title>` (Sonarr's/Radarr's own free-text search) instead of
      refusing a link entirely. `~/repos/starfleet`, deployed as v0.1.27, 2026-08-18.
- [x] `o` was silently doing nothing on a real browser-open failure — `_open_link` called
      `webbrowser.open` unguarded, unlike every other action in the screen; fixed to report the
      real error on the status line instead. Also: `o` only exists on the show-detail screen, not
      the calendar/list view — by original design, not a bug. `~/repos/data`, 2026-08-18.
- [x] Trakt watch history ported in (PC.2's TV half) — see PC.2 below.
- [x] `m`'s leader hint letters (watching/later/paused/completed/dropped) were being consumed as
      Rich markup, not a color issue — literal `[w]`/`[l]`/`[p]`/`[c]`/`[d]` text got parsed as
      style tags (`[c]` happened to resolve to Rich's own real `conceal`, genuinely invisible;
      `[d]` to `dim`). Now real markup: bold orange1 on just the target letter, brackets escaped
      back to literal characters. `~/repos/data`, 2026-08-18.
- [x] `A` (add show) now shows a real candidate picker instead of silently auto-picking Sonarr's
      first search result — new read-only `searchArrCandidates` query (LCARS) + a numbered
      `[title (year)]` picker in Data, auto-selecting only when there's exactly one match. The
      chosen candidate's exact tvdbId is what gets added — no re-search, no risk of a different
      show being matched. Still TV/anime only (Sonarr) — a Radarr/movie version of `A` remains
      unbuilt. `~/repos/starfleet`/`~/repos/data`, deployed as v0.1.29, 2026-08-18.
- [x] `A` (add show) can now add **movies to Radarr**, not just series to Sonarr (user's request
      2026-08-26: "make sure movies added go to Radarr with the radarr setup"). `~/repos/data`
      only, no server change — the whole movie half was already built server-side in B.21
      (`shows.py` `_ensure_in_arr`/`_resolve_arr_candidate` route `media_shape=movie` to Radarr via
      `radarr_root_folder`/`radarr_quality_profile_id`) and in `add_show_with_arr`/
      `searchArrCandidates` (both already take `media_shape`); the only gap was `action_add_show`
      hardcoding EPISODIC. Now asks `Movie? [y/N]` alongside `Anime? [y/N]` — a movie searches
      Radarr and adds there (tmdbId), a series still Sonarr (tvdbId); only the id matching the
      media type is passed (a tmdbId on an episodic add links the wrong thing, shows.py's own
      warning). Anime asked for both — an anime film is `tracking_space=anime` too (drives AniList
      linking, never the Radarr root folder, which Radarr has just one of). Config on `tiny`
      already set: `radarr_root_folder = /data/media/movies`, quality profile 7. 4 new tests + all
      existing add-show sequences updated, `~/repos/data` full suite 510 passed, ruff clean,
      committed `a729bae`. **Verified live 2026-08-26** — the user did a real movie add through
      `A`, confirmed working: this is the first time B.21's whole Radarr add path
      (`client.add_movie`) has ever executed against the live Radarr (the Sonarr half had been
      proven since 2026-08-18 daily use). No LCARS deploy needed; `~/repos/data` runs from source.
- [x] Ascendance of a Bookworm audit, user-caught ("it is all wrong in lcars"), three real bugs:
      (1) `watch_reconcile.py`'s `reconcile_watch_progress` trusted AniList's raw progress/status
      wholesale with no air-date check — on 2026-08-15 it marked six not-yet-aired episodes
      watched and flipped "Adopted Daughter of an Archduke" to `completed` while it was still
      weekly-releasing, overwriting a correct manual fix from hours earlier. Third hardening fix
      in that module (see its own docstring for #1/#2) — both the episode backfill and the
      show-level completion status now exclude any episode with a real, future `air_date_utc`;
      self-heals once it airs. (2) Duplicate Part 1 show row: `s-ebqx1f` (untracked stub, 0
      episodes) and `s-2k4jb6` (real tracked data) both held anilist id 108268 — orphaned
      2026-08-12 when a pending_review got resolved by hand instead of via `applyShowMerge`.
      Worse, Part 2/3/Ryoushu's `show_relation` rows pointed at the dead stub as "Part 1", not
      the real show. `applyShowMerge`'s overlap guard correctly refused the merge outright
      ("winner already has this service") since both sides held the same anilist id — worked
      as designed, not a gap; unlinked the duplicate anilist id from the loser first (a genuine
      no-op, the winner already carried the identical id) so the merge had nothing to conflict
      on, then merged clean — fixed the inbound relation edges, reversible via
      `reverseShowMerge`. Also nulled `anilist_id`/`mal_id` on the demoted loser's own leftover
      season row (`setSeasonMapping`, still held the same anilist id post-merge, would've
      tripped `reconcile_watch_progress`'s Fix 1 "two seasons sharing one anilist_id" guard and
      silently excluded *both* Part 1 seasons from every future reconcile). (3) 3 of 2311
      anilist deep links (incl. this show's) had a literal unsubstituted
      `https://anilist.co/anime/$eid` — a one-off manual-mutation typo from the same 2026-08-12
      session, not a code bug; fixed via `linkShowExternalId`. Code fix deployed as v0.1.30;
      data fixes applied live the same session, 2026-08-19. Known remaining gap, not fixed:
      Part 1 (`s-2k4jb6`) carries 4 unwatched `special`-kind episodes (season 0, Sonarr/TVDB's
      flat-series specials bucket) under an otherwise `completed` show — genuinely ambiguous
      whether they belong here or to the separate OVA AniList entry (`s-5gbznf`, still
      untracked); the existing "Hierarchical season subdivision" open item above is the right
      place for that call, not a guess made here.
- [x] Follow-up, same session: user still couldn't find 4 of Bookworm's 5 parts searching
      "ascenda" — real, catalog-wide bug, not a search issue. `show.primaryTitle` is a
      write-once pointer column (`romaji`/`english`/`native`, resolved via
      `title_{primary_title}`) decided once at creation and never revisited.
      `_create_relation_stub` (metadata.py, the path that created 4 of Bookworm's 5 parts as
      AniList relation edges) preferred romaji whenever it existed at all, english only as a
      fallback — so a stub with a real English title still displayed/searched under its
      Japanese romaji one. 904 shows catalog-wide affected (had a real `title_english` but
      `primary_title='romaji'`). Fixed the precedence (english wins when AniList provides one)
      and one-time backfilled all 904 rows directly on the DB — required a brief lcars/ops stop
      (`journal_mode=delete`, one persistent app connection, no mutation exists for editing
      `primary_title` post-creation) after user confirmation; DB snapshotted first
      (`lcars.db.bak-20260819-primary-title-backfill-pre`). `~/repos/starfleet`, deployed as
      v0.1.31, 2026-08-19.
- [x] Follow-up, same session: user moved a show's save location in Sonarr (Anna Pigeon), asked
      whether LCARS picks it up automatically — it didn't, for two separate reasons. (1) Sonarr's
      `Rename` event (what a root-folder move fires) is deliberately unhandled by both the
      webhook and the `/history` poller (availability.py, by original design — not this bug).
      (2) The one mechanism that *does* re-read Sonarr's current state, `auditLocalFiles`
      (local_audit.py), turned out to have a real bug of its own: `_audit_sonarr`/`_audit_radarr`
      only corrected `file_path_sonarr`/`file_path_radarr` when `available_via_sonarr`/`_radarr`
      itself was about to flip — a file that stayed `available` throughout a root-folder move
      (Anna Pigeon's exact case, confirmed live against Sonarr before touching anything) never
      got its stale path corrected, contradicting the module's own docstring. Fixed both
      (path now compared and corrected independently of the status flip) and ran
      `auditLocalFiles` for real: **2299 episodes + 69 shows corrected** catalog-wide — this had
      been silently broken for far more than just Anna Pigeon. `auditLocalFiles` itself stays
      manual-trigger-only (unchanged, deliberate — not on Ops's automatic loop); still no per-
      show scope exists for it. Also surfaced (pre-existing, unrelated, not acted on): 214 orphan
      files (mostly a large legacy library, `Those Obnoxious Aliens`/Urusei Yatsura 1981, not yet
      fully fetched into LCARS) and 5 untracked Urusei Yatsura movies — report-only findings for
      the user to act on by hand, not this session's job. `~/repos/starfleet`, deployed as
      v0.1.32, 2026-08-19; DB snapshotted first (`lcars.db.bak-20260819-audit-path-fix-v0.1.31-pre`).
- [x] 2026-08-24 — user reported a review saying AniList "temporarily disabled", plus asked to
      clear the 47-entry open review queue. Confirmed live against AniList directly: a real,
      transient AniList-side outage ("The AniList API has been temporarily disabled due to
      severe stability issues"), not an LCARS bug — 38 of the 47 open `pending_review` entries
      were `metadata_fetch` failures from that outage window (2026-08-23 ~23:00 UTC), the other
      9 were normal `air_date_utc` audit entries (weekly schedule slips `_reconcile_air_dates`
      had already applied). AniList was back up by the time this ran: retried all 38 via
      `refreshShowMetadata` (real data — synopsis/poster/genres — confirmed populated on
      spot-check), cross-checked all 9 air-date entries against AniList's live `airingSchedule`
      (exact match), then resolved all 47 via `resolvePendingReview`. Queue is empty.
      Along the way, re-audited "Ascendance of a Bookworm" per the user's specific callout —
      not the air-date/duplicate-show bugs already fixed in the 2026-08-19 audit above, a third,
      new one: Part 1 (`s-2k4jb6`, the *winner* of that 2026-08-12 merge) had `title_english`
      NULL and `title_romaji` holding the English string `"Ascendance of a Bookworm"` instead of
      the real romaji — confirmed against AniList id 108268 directly. Root cause: this show was
      originally added before an AniList link existed (pre-A.20), and unlike poster/synopsis/etc,
      title fields are caller-input-at-creation only — `_fetch_anilist` (metadata.py) never
      touches them once set, and no mutation exists to correct them after creation, same gap the
      v0.1.31 `primary_title` backfill was patching around, not this exact field-corruption
      shape. Fixed the same way as that backfill: DB snapshotted first
      (`lcars.db.bak-20260824-bookworm-part1-title-fix-pre`), brief `lcars`/`ops` stop, direct
      `UPDATE` to the real AniList values (`title_romaji`/`title_english`/`primary_title`),
      restarted, verified live via GraphQL. No code changed — pure data fix, no new deploy.
      **Known wider gap, investigated not fixed (user asked to scope only, not backfill)**:
      sampled 60 of the 1457 other `tracking_space = 'anime'` shows with `title_english IS NULL`
      that carry an AniList link, checked each against real AniList title data — ~22% (extrapolates
      to roughly 300 shows) share Part 1's exact corruption (`title_romaji` holds the real English
      title verbatim, AniList genuinely has an English title LCARS never stored under
      `title_english`); a further ~47% (~700 shows) have a *correct* `title_romaji` but are
      simply missing a real AniList English title that was never fetched (lower priority — not
      wrong data, just incomplete); the remaining ~32% (~470 shows) are legitimately titleless in
      English on AniList — nothing to fix. A real fix needs a proper per-show AniList
      title re-fetch + comparison (the two categories can't be told apart by string heuristics
      alone, confirmed against the sample), i.e. new code/tooling, not a blind mass `UPDATE` —
      scoped here as a `~/repos/starfleet` follow-up, not attempted this session.
- [x] 2026-08-24, same session — user flagged Bookworm's "Adopted Daughter of an Archduke"
      episode 19 as showing `Missing` in Data despite Sonarr already having the file (imported
      2026-08-22). Traced it to a real, general `availability.py` bug, not a one-off: Bookworm's
      parts share one flat Sonarr series (tvdb 366263) with continuous absolute numbering across
      all parts, so Sonarr's raw "episode 55" *is* this part's own "episode 19" — a translation
      only `absolute_number` survives, each sibling's season/episode restarting at 1
      independently. metadata.py's `_fetch_sonarr_multi_show` (2026-08-15) already knew to route
      by `absolute_number`, but only ever fills a row once (`available_checked_at IS NULL`),
      deferring every later update to `availability.py`'s ongoing poller/webhook as "more
      authoritative" — and *those* had no equivalent routing, just a naive raw
      `show_id + season + episode` match that can never land on a shared-tvdb sibling's own
      restarted numbering. Once an episode's first fetch set `available_checked_at`, no later
      Sonarr grab/import could ever reach it again — silently stuck forever, for every future
      episode of every multi-part franchise sharing one tvdb id this way. Fixed properly, not
      just patched: `_show_ids_for_tvdb` (plural)/`_route_episode_availability`/
      `_apply_episode_availability_multi_show` added to `availability.py`, wired into both
      `_poll_sonarr` and `apply_sonarr_webhook`, matching by `absoluteEpisodeNumber` (already
      embedded in Sonarr's `/history`/webhook payloads, just unused before this) whenever a tvdb
      id resolves to more than one LCARS show; the overwhelmingly common single-show path is
      unchanged. 8 new tests (`test_availability.py`) cover multi-show routing, the
      no-absolute-number skip case (mirrors specials' own scope boundary in
      `_fetch_sonarr_multi_show`), and confirm single-show behavior is untouched — full suite
      856 passed. Data fix applied immediately, live: episode 19's stale `available_checked_at`
      reset by hand (brief `lcars`/`ops` stop, single-row update, restart, same pattern as the
      title fix above) then `refreshShowMetadata` re-ran to let the app's own already-correct
      fetch-side logic fill it properly — confirmed `AVAILABLE` with the real file path.
      `~/repos/starfleet`, deployed as v0.1.33.

## TBC / ongoing (validate as you go)

- [ ] Daily use — Data is the active front-end; declaring it "done" is ongoing, no fixed bar.
      aniq stays untouched as emergency fallback until you're 100% happy, costs nothing.
- [ ] Verify Sonarr/Radarr `/add/new?term=...` prefill — host:port curl-confirmed; exact
      query-param contract still assumed, not clicked through behind auth. Validate next time
      you naturally use `A` to add a show.
- [ ] Rotate Sonarr/Radarr keys, MAL client_id, LCARS's own AniList client_secret — in
      progress in the background; reminder only, no action needed from this list.

## Ideas / open questions

- [x] LCARS absorbs the AniList↔MAL mirroring the user currently runs as a separate external tool
      (2026-08-26). **Done, 2026-08-28 (confirmed)**: status + episode progress both ways
      (AniList↔LCARS↔MAL, v0.1.37); score reverse-sync AniList direction (v0.1.43,
      `check_anilist_score_drift` → `pending_review` → Data `setSeasonScore`); score reverse-sync
      MAL direction (v0.1.45, `check_mal_score_drift`, same `pollScoreSync` + ops hourly loop).
      Both directions, all three dimensions — retiring the external tool is now unblocked.
- [x] Catalog-wide `title_english`/`title_romaji` backfill — done 2026-08-26,
      `scripts/backfill_titles.py` (`~/repos/starfleet` `10c0983`). Batch-fetches AniList's own
      `title{romaji,english,native}` for the 1457 anime shows with an AniList link but NULL
      `title_english`, and — only when AniList actually has an English title — overwrites all three
      title fields + sets `primary_title='english'`; titleless-on-AniList shows left untouched,
      `display_title_override` never touched, `primary_title` written in the same UPDATE so the
      CHECK always sees consistent state. Applied to production: **278 corrupted** (title_romaji
      held the English string) fixed, **676 missing-English** filled, **503 legitimately titleless**
      left alone, 0 not-found — matching the 2026-08-24 audit's ~300/~700/~470 projection almost
      exactly (the dry-run counts were the audit re-verification). Snapshot
      `lcars.db.bak-20260826-pre-title-backfill`; stop/apply/restart; override preserved, verified
      live. Same one-time-script pattern as `backfill_synonyms.py`/`import_anilist_scores.py`.
- [x] `refreshTitlesFromAniList(showId)` mutation — built (confirmed 2026-08-28):
      `schema.graphql:1480` + `resolvers.py:2540`. Per-show AniList title re-pull, same
      corrected-precedence logic as the catalog backfill script.
- [ ] Move all secrets/passwords to a safer place (plaintext `config.ini` today) — deferred,
      reminder only; in progress in the background alongside key rotation.
- [ ] Design the HTML client's UI properly.
- [ ] HTML client: add a login/password gate, keep it safe.
- [ ] Add `watchedEpisodeCount: Int!` and `availableEpisodeCount: Int!` to the `Show` type —
      needed by the web client calendar cards to display accurate per-show progress and
      availability without a separate query. `watchedEpisodeCount` = count of episodes with at
      least one WatchEvent; `availableEpisodeCount` = count of episodes whose
      `availableViaSonarr`/`availableViaRadarr` is AVAILABLE (whichever applies to the
      show's mediaShape).
- [ ] HTML client video playback: local player vs. browser — undecided (native mpv only
      works from the media machine itself, browser `<video>` has real format limits).
- [ ] **New ShowStatus: SKIP** — a lightweight tombstone status meaning "seen, not interested".
      Distinct from DROPPED (started and abandoned) and PLANNING (intend to watch). A show at
      SKIP would need no Sonarr/Radarr entry; just a stub in LCARS so the same title doesn't
      resurface as a suggestion again. AniList/MAL push behaviour: probably no push (the external
      lists don't have an equivalent "skip" bucket). May also need a `skipList` query field so
      clients can browse what has been dismissed.
- [ ] **Upcoming-show catalog / proactive discovery** — two overlapping shapes, both worth
      exploring before committing to one:
      - *Passive import*: an Ops loop (daily or weekly) pulls upcoming seasons from AniList
        (seasonal endpoint) and/or TMDB (discover with `first_air_date_year`, `with_status=0`)
        and ingests them as LCARS stubs — `tracked = false`, status = null or a new UPCOMING
        pseudo-status, minimal metadata (title, external IDs, poster, premiere date, episode
        count if known). The user then browses these stubs in the web client's "Upcoming" page,
        can promote to PLANNING (full add + Sonarr/Radarr prompt) or dismiss as SKIP. Past
        seasons not yet in LCARS stay addable via the existing `addShow`/backfill flow.
      - *Active search*: the web client gets a "Browse / Discover" page sourcing directly from
        TMDB (covers both anime and non-anime in one API, simpler than juggling AniList +
        TMDB). Results are grouped by premiere year/season (Winter/Spring/Summer/Fall) or by
        release month, displaying cover art, title, and air date. Per entry, two actions:
        - **Plan** → calls existing `addShow` (or a new `planShow` shortcut) — full LCARS row,
          prompts/auto-triggers Sonarr or Radarr add.
        - **Skip** → adds a lightweight LCARS stub (title, TMDB link, poster, air dates) under
          the new SKIP status; no Sonarr/Radarr interaction, no episode/season correctness
          required at stub time.
      The two shapes are complementary: passive import fills the inbox automatically; active
      search handles targeted lookup. Either requires the SKIP status above and a stub-ingestion
      path in LCARS that doesn't enforce the same completeness as a full `addShow`.
- [ ] **Web client — Statistics page** (details TBD). Likely surface: episode/show counts by
      status, watch history over time (episodes watched per day/week/month), score distribution,
      genre breakdown, total runtime. Needs `watchedEpisodeCount` and date-bucketed watch-event
      aggregates from LCARS — design the queries when building the page.
- [ ] Open question: should linkage to external DBs (TMDB, AniList…) hang off `show` or
      `episode`? First read: `season` already answers most of this.
- [ ] Open question: does `show.studio` deserve an id-prefix like other entities, for
      "browse by studio"?
- [ ] If a tracked show gets delayed, check livechart.me/feeds/headlines (or its /search)
      for a matching headline — manual only, not automated.
- [ ] animeschedule.net's real API v3 (`/anime/{slug}`, `/timetables/{airType}`) as a second
      schedule source, if it ever becomes worth building — its RSS feed is a confirmed dead
      end.
- [ ] AniList indexes a not-yet-aired season under its romaji title only — handle case-by-case
      as each season airs, not automated.
- [ ] Fix the Tailscale ACL blocking SSH to the deploy host as `drostan` — deferred, LAN IP
      workaround is fine for now.
- [x] Client changes now sent to LCARS immediately, not buffered (2026-08-26, `~/repos/data`
      `7148d70`). The queue-then-flush buffer dated from when Data wrote to Sonarr/AniList directly;
      Data only talks to LCARS now, so `w`/`m`-status/`S` flush the moment they're made instead of
      waiting up to 5 min for the `_check_pending_flush` tick (quit/`b`/`P` still flush too). The
      on-disk queue stays as the **offline-reliability fallback** — a change that can't reach LCARS
      is saved and retried exactly as before, only the online latency changed (answer to the "unless
      there is a reason to keep the buffer?" question: yes, keep it — but only for offline, not as a
      routine delay). Two correctness pieces came with it: (1) every flush path now funnels through a
      reentrancy-guarded `_flush_pending_lcars_change` (`_is_syncing`) — `flush_queue` sends each
      watch_event then clears it, so overlapping flushes would insert a duplicate watch_event; the
      guard serializes them and also closes a pre-existing timer-vs-`P` race; (2) the pending marker
      had doubled as the optimistic-display source, so on a successful send the new status/score/
      episode-state is now written into the local confirmed model too (else the row snapped back to
      the stale value until the next refresh). Data-only, no LCARS change, no deploy — live on next
      Data launch (runs from source). Full Data suite 516 passed.
