# Next up

Working assumption: the system runs as-is; from here it's debugging, new
functions, and reshaping things to taste. Full design/build history archived
to `~/repos/starfleet-archive` (`SCOPE.md`, `BUILD_PLAN.md`, `KICKOFF_PROMPT.md`,
old `todo.md`, plus the two `~/.claude/plans/` files they reference) —
`archive/<file>:<line>` below points into it for the full story.

## Verified (confirmed via real daily use, 2026-08-18 — resurfacing is a new bug, not a reopen)

- [x] B.21's Sonarr/Radarr writes (add-show, auto-unmonitor-on-drop).
- [x] v0.1.18/v0.1.19 AniList write-mirror (status/score/episode-progress/delete/rewatch) +
      completion auto-sync.
- [x] Calendar backlog counter / mark-watched display (B.11g).

## Build

- [ ] Wire the AniList push for `season.started_at`/`completed_at` — columns exist, push
      isn't wired (`FuzzyDateInput` shape unhandled). (archive/todo.md:1013)
- [x] Add a real mutation for `tracking_space` — `setTrackingSpace(showId, trackingSpace)`,
      `~/repos/starfleet` v0.1.23 (deployed) + `t` in `~/repos/data`'s show-detail view,
      2026-08-18. (archive/todo.md:1209)
- [ ] A client-facing way to correct episode-*number* misalignment (which Sonarr season/episode
      slot an episode is filed under) without a direct DB edit — the air-date half of this line
      is done (`a`/`A` above); no mutation exists for renumbering an episode itself yet.
      (archive/todo.md:1209)
- [x] Show-detail view: seasons with all episodes, mark watched per-episode and per-season,
      move status/score at the show/season level individually — built in `~/repos/data`
      (`show_detail_screen.py`, `enter` on the show browser), 2026-08-18.
- [ ] Show-detail view: mark a movie watched from the screen itself — gated for now, `w` has
      no episode row to act on for a `MOVIE` show; needs `Show.watchEvents`-based undo too,
      not just the episode-scoped lookup the episodic path reuses.
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

## Cutover (not started)

- [ ] Switch actual daily use over to Data, once proven reliable day-to-day.
- [ ] Archive aniq — don't delete, keep as emergency fallback.
- [ ] Data becomes the permanent front-end under its own name.
- [ ] Rotate Sonarr/Radarr keys, MAL client_id, LCARS's own AniList client_secret (all
      pasted in chat during build, intentionally left live until now).

## Ideas / open questions

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
