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
- [ ] PC.2 — one-time historical import: Trakt watch history, AniList data, MAL legacy scores
      (AniList primary for scores where both exist). (archive/BUILD_PLAN.md:4720)
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
- [ ] Verify the real Sonarr (`/series/{titleSlug}`, `/add/new?term=tvdb:{id}`) and Radarr
      (`/movie/{titleSlug}`, `/add/new?term=tmdb:{id}`) web UI route shapes against a live
      instance — assumed from standard Servarr frontend convention, not yet curl-checked against
      `tiny`'s own Sonarr/Radarr.
- [x] `o` was silently doing nothing on a real browser-open failure — `_open_link` called
      `webbrowser.open` unguarded, unlike every other action in the screen; fixed to report the
      real error on the status line instead. Also: `o` only exists on the show-detail screen, not
      the calendar/list view — by original design, not a bug. `~/repos/data`, 2026-08-18.
- [x] Not-yet-followed Sonarr/Radarr shows now offer the real "Add New" page (pre-filled with the
      known tvdb/tmdb id, `?term=tvdb:{id}`/`?term=tmdb:{id}`) via `o` instead of no link at all —
      a synthetic, non-persisted `Show.externalIds` edge (`service: "sonarr:add"`/`"radarr:add"`),
      computed fresh from the show's own confirmed tvdb/tmdb id and self-healing the moment a real
      link exists. Never auto-adds — opens the browser to Sonarr's/Radarr's own add screen only.
      `~/repos/starfleet`/`~/repos/data`, 2026-08-18 (not yet deployed to production).

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
