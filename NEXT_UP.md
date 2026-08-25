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
      it.
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
      (archive/todo.md:1209)
- [ ] `show_merge.py` has the same composite-FK ordering bug `setEpisodeNumber` above needed
      `PRAGMA defer_foreign_keys` to avoid, found by inspection while designing that mutation
      (2026-08-25), not from a live failure: merging two shows re-points a moved episode's
      `season`/`episode` before its `watch_event` rows, under immediate FK checking — raises
      `FOREIGN KEY constraint failed` whenever the moved episode actually has watch history.
      Never fired in practice because merges have so far only ever involved zero-watch-event
      stub shows. Fix is the same tool already validated for this exact class of problem: wrap
      the merge's episode-move + watch_event-repoint in one transaction with
      `PRAGMA defer_foreign_keys = ON`. No test currently covers this path.
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
      above and the episode-renumbering item below).
- [ ] `ops audit-local-files`'s 10s `LcarsClient` HTTP timeout (`ops/lcars_client.py`'s own
      default) is too short for a real whole-library run — found live, 2026-08-20: manually
      triggering it against the real library (fixing Lioness/Lanterns after a Sonarr
      anime→series root-folder move) hit `httpx.ReadTimeout`/`LcarsError: Timed out talking to
      LCARS` in the CLI, but the mutation had actually committed successfully server-side
      (confirmed after the fact via the DB's `available_checked_at` timestamp + cross-checking
      Sonarr's API and the filesystem directly) — the CLI just gave up waiting and never printed
      the summary. Needs either a longer/no timeout specifically for this command's own
      `LcarsClient` construction in `_cmd_audit_local_files`, or a way to poll/confirm completion
      after a client-side timeout instead of leaving the operator to go verify by hand.
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
- [x] Manual schedule editing from the show-detail view: set weekly time + first air date per
      season (`A`), per-episode offset from the original schedule (`a`, +1 week/+2 days…),
      offer to shift all subsequent episodes when one moves — `~/repos/data`
      `show_detail_screen.py`, 2026-08-18. `A` refuses on season 0 (Sonarr's specials bucket,
      no real weekly cadence there) — use `a` per-episode for those.
- [x] PC.2, Trakt half — one-time historical import from a Trakt export zip run: 366 shows
      created, 62 matched to existing shows, 419 status writes, 13,112 watch events (real
      historical timestamps) + 10,211 synthesized episode rows. Sword Art Online excluded
      (already AniList-tracked). `scripts/import_trakt_history.py`, 2026-08-18.
- [ ] PC.2, remaining — AniList data / MAL legacy scores historical import (AniList primary for
      scores where both exist). (archive/BUILD_PLAN.md:4720)
- [ ] AniList `synonyms` field — last gap in LCARS's AniList read coverage (aninote
      note-matching only).
- [ ] `tracking_space`: allow a show to be tracked in more than one place at once (e.g.
      AniList and MAL simultaneously).
- [ ] Hierarchical season subdivision for legitimate cross-source granularity mismatches
      (Bookworm/Mushoku Tensei-style cases). (archive/todo.md:1175)
- [ ] MAL side of AniList/MAL drift detection (AniList side already built, B.5).
- [ ] Fix the Tailscale ACL blocking SSH to the deploy host as `drostan` (workaround via LAN
      IP in place).
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
- [ ] Verify the real Sonarr/Radarr `/add/new?term=...` search-and-prefill behavior specifically
      (host:port reachability is now curl-confirmed; the exact query-param contract is still
      assumed from standard Servarr frontend convention, not yet clicked through behind auth).
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

## Cutover (not started)

- [ ] Switch actual daily use over to Data, once proven reliable day-to-day.
- [ ] Archive aniq — don't delete, keep as emergency fallback.
- [ ] Data becomes the permanent front-end under its own name.
- [ ] Rotate Sonarr/Radarr keys, MAL client_id, LCARS's own AniList client_secret (all
      pasted in chat during build, intentionally left live until now).

## Ideas / open questions

- [ ] Catalog-wide `title_english`/`title_romaji` backfill (found 2026-08-24 auditing Bookworm
      Part 1, see the Build entry above for the full breakdown) — needs a real per-show AniList
      re-fetch + comparison tool (~300 shows share Part 1's exact corruption, ~700 more are just
      missing a real English title, ~470 are legitimately titleless in English), plus a mutation
      to actually write corrected title fields post-creation, since none exists today.
- [ ] Move all secrets/passwords to a safer place (plaintext `config.ini` today).
- [ ] Design the HTML client's UI properly.
- [ ] HTML client: add a login/password gate, keep it safe.
- [ ] HTML client video playback: local player vs. browser — undecided (native mpv only
      works from the media machine itself, browser `<video>` has real format limits).
- [ ] Open question: should linkage to external DBs (TMDB, AniList…) hang off `show` or
      `episode`? First read: `season` already answers most of this.
- [ ] Open question: does `show.studio` deserve an id-prefix like other entities, for
      "browse by studio"?
- [ ] LCARS→clients webhook push — future idea, don't start unprompted.
- [ ] If a tracked show gets delayed, check livechart.me/feeds/headlines (or its /search)
      for a matching headline — manual only, not automated.
- [ ] animeschedule.net's real API v3 (`/anime/{slug}`, `/timetables/{airType}`) as a second
      schedule source, if it ever becomes worth building — its RSS feed is a confirmed dead
      end.
- [ ] AniList indexes a not-yet-aired season under its romaji title only — handle case-by-case
      as each season airs, not automated.
