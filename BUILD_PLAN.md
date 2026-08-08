# Starfleet — build order plan

Ordered, step-by-step build sequence. This document sequences work;
it doesn't re-explain *why* — every step links back to the `SCOPE.md`
section that decided it. Read `SCOPE.md` first (or alongside); this
is its operational sibling, not a replacement for it.

**How to use this**: work top to bottom. Each step is checkboxable —
tick it off as it's actually done, don't reorder unless a step
explicitly says it can run in parallel. If a step surfaces a question
`SCOPE.md` doesn't answer, that's a real gap — stop and resolve it
(update `SCOPE.md`, note the resolution) rather than guessing and
moving on.

---

## Phase 0 — Repository setup

- [x] **0.1 — Create `~/repos/starfleet`.** This *is* the deferred
  on-disk rename (`SCOPE.md` §10.5, Naming section) — done, this repo
  is the result. The old `~/repos/Chabrol` discussion repo is left
  untouched as a historical record; nothing further to do with it
  unless told otherwise.
- [x] **0.2 — Initialize the LCARS project scaffold.** Python project
  (`pyproject.toml`), matching aniq's own tooling conventions where
  reasonable (`SCOPE.md` §11.1). Set up Alembic for schema migrations
  (§11.2). No app code yet — just the skeleton a Python project needs.
- [x] **0.3 — Fork aniq into a new sibling repo, `~/repos/data`,
  becoming Data.** `SCOPE.md` §4.0 / §10.3 — the confirmed first real
  build action. A clean, one-time copy of aniq's current codebase, not
  a git remote/submodule relationship. From this point on:
  - The real `~/repos/aniq` is **never modified again**, at any point,
    for any reason — read-only reference only, for the rest of this
    roadmap (§4.0, a hard constraint, not a guideline).
  - `~/repos/data` is freely modifiable — this is where all
    LCARS-client integration work happens.
  - Done 2026-08-08: 64 tracked files copied (current disk content,
    including two then-uncommitted edits) into a fresh `git init`
    (no shared history with aniq), pushed to `drostan80/data`
    (private). Package/CLI mechanically renamed `aniq`→`data`
    (resolved during this step — see `SCOPE.md` §4.0 addendum for the
    runtime-state-isolation reasoning: nested config/cache paths under
    `starfleet/`, distinct keyring service name, so Data's credentials
    can never collide with aniq's live ones). Verified with a fresh
    venv: ruff clean, 557/558 tests pass — the one failure is a
    pre-existing date-dependent flake, documented in Data's own
    `.claude/docs/backlog.md`, not fixed here per the "clean copy"
    policy.
- [x] **0.4 — Set up CI.** GitHub Actions on the LCARS repo: hybrid
  trigger — every push to `main` builds/tests only (no publish); an
  explicit tag/release builds *and* publishes the image (§11.3).
  Registry: GitHub Container Registry (ghcr.io), proposed default, not
  separately confirmed — adjust if Docker Hub or something else is
  actually wanted.
  - Done 2026-08-08, built together with 0.5 below (see its note for
    why the order flipped). `.github/workflows/ci.yml`: push to
    `main`/PRs → lint (ruff) + `pytest` + `docker build` (no push);
    a `v*` tag → same, plus publish to
    `ghcr.io/drostan80/starfleet:{version,latest}`. GHCR proceeded as
    the default per §11.3, not re-challenged. **Verified for real**:
    pushed, watched the Actions run (`gh run watch`) — both jobs green,
    including the docker build actually succeeding on GitHub's runner
    (no docker/podman available locally to check any other way). The
    tag-publish path itself is implemented but **not yet exercised** —
    deliberately not tagging a `v0.1.0` release this early, since
    there's no real app behind the placeholder `CMD` yet (0.5's own
    note). Exercise it once there's something worth actually
    publishing.
- [x] **0.5 — Scaffold the Docker image**, targeting deployment as an
  additional service inside the existing Sonarr docker-compose stack
  (§11.3). Doesn't need to actually deploy yet — just needs to build.
  - Done 2026-08-08, **ahead of 0.4** (reordered, not skipped): 0.4's
    CI needs a Dockerfile to build against, so a broken/no-op docker
    job wasn't a real option. `Dockerfile`: `python:3.12-slim` (pinned
    to the `requires-python` floor, not the 3.14 the local dev venv
    happens to run), non-root user, `lcars.db`/`lcars.ini` deliberately
    not baked in (runtime state, §11.2). `CMD` is an explicit
    placeholder (`import lcars; print(...)`) rather than pointing at a
    real ASGI entrypoint — none exists yet, that's Phase A (§8).
    Build itself verified via 0.4's CI run, not locally (no docker/
    podman in this environment).

---

## Phase A — schema + CRUD API + reconciliation (no autonomous scheduler)

`SCOPE.md` §4 "Phase A". Everything in this phase is mechanical
drafting/implementation against an already-fully-specified design —
no open design questions left blocking it.

- [x] **A.1 — Draft the full data model** (§5.0–§5.10): every table,
  using the id scheme (§5.0, prefix table) for every top-level entity.
  Two column lists still need drafting from scratch (population
  *strategy* is settled, exact columns aren't):
  - [x] `episode_numbering_mapping`'s exact columns (§5.5, §10.2 item 4)
  - [x] `show_id_mapping`'s exact columns (§5.5, §10.2 item 6)
  - Done 2026-08-08: `migrations/versions/7196ca889757_*.py`, one
    migration, all 22 tables from §5.0–§5.10 (deliberately *not*
    including §6-derived schema — pacing/§6.2, global settings/§6.13,
    FTS/§6.5 — those belong to their own later A-steps). Verified for
    real: applied to a scratch DB, exercised the id-shape/
    `primary_title`/generated-`available_locally`/composite-FK
    constraints with actual inserts (valid rows accepted, invalid ones
    correctly rejected), then downgraded cleanly back to empty.
    Applied to the dev `lcars.db` too.
  - **Two SCOPE.md gaps found and fixed**, not silently patched over:
    - `show_relation` didn't exist as a table at all despite being
      referenced everywhere as the graph franchise auto-derivation
      reads from — asked before proceeding (directed-edges-as-ingested
      vs. undirected canonical pairs); you picked directed. Now in
      §5.9.
    - `pending_review` had no `entity_id` (only a type), and its
      `resolved_by_client` enum included `aniq`, which can't actually
      resolve anything (no LCARS integration, §7.2) — both fixed
      directly, didn't need asking (unambiguous from surrounding text).
  - Also fixed while re-reading before starting: `show.score` was
    missing from §5.1's own field list despite being required by
    §5.7/§6.1; A.3's stale "SQLAlchemy" reference (see the standalone
    fix commit before this one).
  - **A.1 addendum, found while starting A.2** (migration
    `0c47d1677e7d`): a real, non-mechanical gap — every A.1 tracking
    mechanism (`available_via_*`, `state`, `watch_event`'s FK) assumed
    an `episode` row, but §5.2 says a standalone movie show is "not an
    episode at all." Asked rather than guessed, across several rounds
    (this had real branches — episodic-row-reuse vs. show-level
    fields; then a further clarification once answered surfaced a
    second gap, movie↔`bonus_movie`-episode reconciliation, also
    asked). Resolved: `show` gets its own `available_via_radarr`/
    `available_locally` (movie-only), `watch_event.season`/`episode`
    became nullable, `episode_movie_link` added as a third
    id-mapper-shaped reconciliation table, and `next_up_override`
    added for §6.4's manual reorder (no table existed for it either).
    All in SCOPE.md §5.1/§5.3/§5.9. Verified the same way as A.1
    itself: applied to a scratch DB, exercised every new
    constraint/nullability with real inserts, downgraded cleanly.
- [x] **A.2 — Write the GraphQL SDL** (schema-first, §11.1, §8, §10.2
  item 5): types, dedicated field-specific mutations (`setStatus`,
  `setScore`, `addWatchEvent`, `markEpisodeSkipped`,
  `markSeasonWatched`, etc. — never one generic `updateShow(...)`,
  §3 principle 7), and the deliberately-designed query shapes (§8):
  shows by status, episodes airing in the next N days, pending-review
  list, backlog, full-text search, cross-show next-up.
  - Done 2026-08-08: `src/lcars/schema.graphql`, one hand-written SDL
    file, 96 types after built-ins. Covers all 24 tables from A.1 +
    its movie-tracking addendum, every mutation implied by §6.1–§6.13,
    and all six §8 query shapes. Verified for real: loaded through
    Ariadne's `make_executable_schema` + `graphql-core`'s
    `validate_schema`, zero errors; added `tests/test_schema.py` so
    this stays checked on every future change, not just today.
  - **Found and fixed a real packaging bug while verifying**: a plain
    `pip install .` (non-editable — what the Dockerfile actually does)
    silently dropped `schema.graphql`, since setuptools doesn't ship
    non-`.py` files by default. Confirmed the failure with a scratch
    install, fixed via `[tool.setuptools.package-data]`, confirmed the
    fix with the same scratch-install test.
  - **Three more real API-shape gaps found, none guessed through**:
    - Pagination — unaddressed anywhere in SCOPE.md. Asked; you chose
      Relay-style cursor connections (edges/node/pageInfo), then a
      follow-up on scope (top-level-only vs. every list field) — you
      chose every list field, no exceptions, so even small nested
      lists (a show's own episodes/cast/tags) use full connections.
    - Mutation response/error shape — also unaddressed. Asked; you
      confirmed mutations return the mutated entity directly, errors
      via GraphQL's standard top-level `errors` array, no bespoke
      per-mutation Payload type.
    - §6.11's hard-delete delay period had no persisted state to
      measure it by anywhere in §5. Asked; resolved as a new
      `show.hardDeleteRequestedAt` timestamp (already added via the
      A.1-addendum migration, `0c47d1677e7d`).
  - **Surfaced the movie-tracking gap** that produced the whole A.1
    addendum above — discovered specifically because writing GraphQL
    types against the finished A.1 schema forced the "where does a
    movie's watch-state actually live" question that a table-by-table
    migration review alone hadn't surfaced.
- [ ] **A.3 — Wire resolvers to SQLite** via raw `sqlite3` (stdlib, no
  ORM) + hand-written Alembic migrations (§11.2, resolved 2026-08-08
  during 0.2 — this line originally said SQLAlchemy, corrected here to
  match; see `migrations/README`), `lcars.db` / `lcars.ini` filenames
  (§11.2). **Deliberately scoped to a vertical slice for this pass**
  (agreed 2026-08-08 — A.3 covers all 24 tables/96 types, too large to
  respectably finish-and-verify in one go): foundation + Show/Episode/
  WatchEvent + their core mutations, fully built, tested end-to-end,
  and working. Left unchecked until every table has resolvers —
  remaining tables (Person/Studio/Franchise/tags/id-mapper/
  pending_review/deletion/export-import/...) are follow-up passes, not
  a hidden gap.
  - **Two more real architecture gaps found and asked about before any
    code was written** (recorded in `SCOPE.md` §11.2/§8):
    - Sync-vs-async DB execution: raw `sqlite3` (0.2's decision) is
      blocking, the ASGI stack (Ariadne/uvicorn) is async — never
      resolved which. Chose: sync resolvers, one shared connection
      opened at startup, no threading/locking (uvicorn's default
      single worker means nothing ever touches it concurrently).
    - LCARS's own bearer-token storage: §8's "config.ini + keyring"
      phrase turned out to describe *client*-side storage (a normal
      desktop, real keyring daemon) — LCARS itself runs headless in
      Docker, where that keyring pattern doesn't apply. Chose:
      plaintext `lcars.ini`, `chmod 600` — same precedent aniq already
      sets for its own client_id/secret, not a new pattern.
  - **A third gap, found mid-implementation**: history/`pending_review`
    rows must record the originating client (§5.7), but nothing said
    how the server learns *which* client is calling, given one shared
    bearer token. Chose an `X-LCARS-Client` HTTP header (transport-
    level, parallel to the bearer token itself) over threading a
    `client` argument through every mutation individually.
  - Built: `config.py` (lcars.ini + env, file<env precedence, mirrors
    aniq's own pattern), `ids.py` (nanoid generation + collision retry,
    §5.0), `db.py` (the shared connection), `pagination.py` (generic
    Relay cursor pagination — a real bug in its first draft, caught by
    writing `tests/test_pagination.py` before building anything on top
    of it: `hasNextPage`/`hasPreviousPage` were computed against a
    WHERE clause that already excluded the rows being checked for,
    making both always `False`), `util.py` (the `DateTime` scalar,
    UTC-timestamp helper), `resolvers.py` (Show/Episode/WatchEvent +
    `addShow`/`setStatus`/`setScore`/`setTracked`/`addWatchEvent`/
    `markEpisodeSkipped`), `server.py` (the ASGI app, bearer-token
    middleware, `X-LCARS-Client` context), `cli.py` (the `lcars`
    console script, argparse per aniq's own convention).
  - Verified for real throughout, not just written: 45 tests, including
    real end-to-end HTTP requests (`httpx.ASGITransport`, no bound
    port) against a real migrated SQLite database — auth rejection,
    the client-header requirement, score clamping/rounding, history-
    row writes, movie watch-events with null season/episode, cursor
    pagination wired end-to-end. Also fixed the Dockerfile's placeholder
    `CMD` (0.5) with the real serve command now that one exists.
  - **Second slice, same day**: `show_id_mapping`/
    `episode_numbering_mapping`/`episode_movie_link` (§5.5 + addendum,
    manual-override paths only — automated Fribb-seeded derivation is
    still A.4's job) and `pending_review` (§5.6, manual resolution
    only — automated creation is A.4/A.5/Phase B). 51 tests total now.
    - Caught a real schema inconsistency before writing any resolver
      code: `resolvePendingReview` had carried its own
      `resolvedByClient` argument since A.2, which now duplicated/
      conflicted with A.3's `X-LCARS-Client`-header decision. Asked;
      dropped the argument in favor of the header, for one consistent
      source of truth. `schema.graphql` edited accordingly.
    - Caught a real resolver gap through testing, not review:
      `Query.episode(id)` had never been bound at all (only
      `Query.show` was) — a query for it silently returned `null`
      (legal per the schema, `Episode` is nullable there) instead of
      erroring, which is exactly the kind of wrong-but-quiet result
      this project's whole "verify for real" practice exists to catch.
      Found because `Episode.linkedMovieShow` came back `null` in a
      test that expected a real value, not because of code review.
- [ ] **A.4 — Implement the id-mapper / reconciliation tables**
  (§5.5): `show_id_mapping` seeded from the Fribb/`anime-lists`
  dataset (one-time/on-demand download, not live polling), manual
  overrides authoritative, `pending_review` on any discrepancy or
  no-candidate-found case.
- [ ] **A.5 — Implement `pending_review`** (§5.6): the shared
  audit-only mechanism, value-chain accumulation on repeated automatic
  changes (not overwrite), permanent retention once resolved.
- [ ] **A.6 — Implement all history tables** (§5.7): `status_change`,
  `score_change`, `air_date_change`, `tracked_change` — dedicated per
  concern, every row records originating client/process.
- [ ] **A.7 — Implement `show_service_presence`** (§5.4): fuzzy title
  search (across all title variants, threshold-gated, `difflib`-style
  scoring) as the matching algorithm — already resolved, not an open
  question. Passive/informational — no `pending_review` on change.
- [ ] **A.8 — Implement on-demand external fetch triggers**: immediate
  metadata fetch (poster, synopsis, cast, episode list, external ids)
  on show creation, from any client (§4 Phase A). This is the only
  external-call trigger in Phase A — **no autonomous scheduler yet**,
  that's Phase B.
- [ ] **A.9 — Implement scoring & conversions** (§6.1): 0–20
  quarter-point personal scale, clamp/round silently on invalid input,
  ×5 to AniList, ÷2 to MAL, both push-only.
- [ ] **A.10 — Implement paced/catch-up mode schema** (§6.2):
  per-show cadence config + the adaptive next-date computation.
  Scheduling itself (actually advancing dates) is Phase B; the schema
  and computation logic belong here.
- [ ] **A.11 — Implement the cross-show next-up query** (§6.4):
  soonest-available-first default, manual reorder override.
- [ ] **A.12 — Implement full-text search** (§6.5): titles + synopses,
  across all stored title variants.
- [ ] **A.13 — Implement the stats query surface** (§6.6): totals,
  hours watched, personal-score distribution. (Won't be meaningful
  over time until Phase B accumulates real data — building the query
  surface now is still correct, per §6.6's own phase note.)
- [ ] **A.14 — Implement deletion policy** (§6.11): soft-delete
  default, hard-delete layered behind a delay + re-type-the-title
  confirmation.
- [ ] **A.15 — Implement data export/import** (§6.12): JSON,
  `schema_version` integer from the first implementation, reject (not
  auto-migrate) on any version mismatch.
- [ ] **A.16 — Implement global settings** (§6.13): home timezone
  (default `Europe/Dublin`), used for calendar/pacing/refresh
  day-boundary logic; all storage stays UTC internally.
- [ ] **A.17 — Data's side**: replace Data's Trakt client with an
  LCARS client of the same shape (§4 Phase A). Data keeps doing what
  aniq does today (Sonarr polling, AniList calls, its own air-date
  patch) but also writes resulting state to LCARS. aniq's existing
  "queue locally, retry on flush" pattern carries over to the
  Data→LCARS path.
- [ ] **A.18 — Confirm**: no autonomous scheduler exists yet at the
  end of this phase — this alone should already be shippable and
  useful (Trakt gone, real status column, inside Data), before Ops
  exists at all (§4, closing note on Phase A).

*(A.9–A.16 don't have hard ordering dependencies on each other — bank
them in whatever order is convenient once A.1–A.3 exist.)*

---

## Phase B — Ops takes over syncing

`SCOPE.md` §4 "Phase B" / §6.7. Build **Ops**, the autonomous
background scheduler.

- [ ] **B.1 — Daily metadata refresh** for `watching`-status,
  actively-airing shows; on-open trigger capped to the same
  once-per-day ceiling (not stacking).
- [ ] **B.2 — Weekly Fribb dataset reconciliation** — deliberately
  slower/independent of the daily cadence.
- [ ] **B.3 — Sonarr/Radarr polling for file availability**
  (queue/episode-file/movie-file endpoints, never a filesystem scan).
  This is where `available_via_sonarr`/`available_via_radarr` (§5.2)
  get checked and updated independently — remember `bonus_movie`-kind
  episodes are commonly available via **both** sources, added
  asynchronously; poll both, don't assume one implies the other.
- [ ] **B.4 — AniList `airingSchedule` polling.**
- [ ] **B.5 — animeschedule.net polling.** Prefer the **REST API v3**
  (`/timetables/{airType}`, filtered by `anilist-ids` — avoids fuzzy
  matching entirely since Chabrol already has the AniList id via
  `show_external_id`) over the RSS feeds. First sub-step: confirm
  whether the read endpoints need an OAuth2 bearer token at all, and
  register an app if so (§10.1 item 2 — the one remaining unverified
  detail, resolve by testing, not more reading). RSS
  (`/jpnrss.xml`/`/subrss.xml`/`/dubrss.xml`) stays a documented
  fallback if API access proves restricted. Respect the confirmed
  **120 req/min** global rate limit.
- [ ] **B.6 — Per-integration service-health tracking**: reachable?
  rate-limited? — its own concept, separate from any individual show's
  state.
- [ ] **B.7 — `show_service_presence` periodic refresh** — same poll
  cadence as the rest of this phase (§5.4).
- [ ] **B.8 — Air-date reconciliation**: priority Manual >
  animeschedule.net > AniList `airingSchedule` > Sonarr raw — governs
  which value *wins*, never gates the apply. Every automatic change,
  including one overwriting a manual value, applies immediately and
  logs a `pending_review` entry.
- [ ] **B.9 — Backlog visualization** (§6.3): calendar-native counter
  line under a show's next-episode entry; mark-watched from it clears
  exactly one oldest episode per action.
- [ ] **B.10 — MAL integration** (§6.9 — can start any time after
  Phase A's push infrastructure exists; doesn't have to wait for the
  rest of Phase B specifically):
  - OAuth2 app registration (free, self-service).
  - **Build the proactive refresh-token renewal job now, not later**
    — MAL's refresh tokens only last 1 month (notably short), so this
    needs to run on its own schedule (e.g. weekly, alongside other Ops
    cadences) from day one, not bolted on after a token lapses in
    production.
  - Score/status push (÷2, 5-value enum), one-time legacy score import
    via `pending_review`.
  - `is_rewatching`/`num_times_rewatched`: never auto-toggled, same
    policy as AniList's `REPEATING` (§6.8/§6.9).
  - MAL is a **backup mirror of AniList**, not independently curated
    (§6.1/§6.9) — every show pushed to AniList gets the same push to
    MAL, same tracked-show set. No discrepancy-checking logic here;
    that's the separate, later drift-detection work below.
- [ ] **B.11 — Data's role shrinks**: calendar reads tracking/air-date/
  availability state from LCARS instead of computing it locally.
  Data's status bar gains per-source sync-health indicators sourced
  from Ops's service-health state (B.6).
- [ ] **B.12 — Proxy interactive flows through LCARS**: add-show,
  id-remap move from direct-from-Data to going through LCARS, so
  reconciliation logic applies consistently regardless of trigger.
- [ ] **B.13 — Confirm**: the real aniq is still completely untouched,
  standalone, on Trakt, the whole time — this phase changes nothing
  about it.

---

## Phase C — Data as thin front-end + mpv/aninote bridge

`SCOPE.md` §4 "Phase C".

- [ ] **C.1 — Drop Data's own Sonarr-polling/AniList-polling/
  air-date-correction logic entirely.**
- [ ] **C.2 — Keep only**: calendar rendering (driven by LCARS reads),
  mpv IPC + aninote invocation (inherently local, never moves
  server-side — hard constraint), relaying user actions to LCARS.
- [ ] **C.3 — Keep the one permanent exception**: Data's direct
  write path straight to AniList for episode watch-status only (§6.8)
  — writes to AniList and LCARS simultaneously, no timer wait. Data
  retains its own AniList OAuth credentials indefinitely for this one
  path — this is not something Phase C removes.
- [ ] **C.4 — Confirm**: by the end of this phase, Data is
  functionally what aniq's own Phase C drawdown would have looked
  like — the real aniq is still untouched, sitting exactly as it was
  at the Phase 0 fork.

---

## Parallel track — Holodeck and Captain's Log

Can start any time after Phase A's GraphQL API is stable — not gated
on Phase B/C. `SCOPE.md` §7.3/§7.4.

- [ ] **P.1 — Holodeck** (full editor, §7.3): dashboard leading with
  two foregrounded panels (upcoming episodes/calendar, the pending
  review queue itself — not just a badge). Backlog view and a
  recently-watched activity feed as their own dedicated screens, not
  dashboard elements. Full hard-delete confirmation flow reachable.
- [ ] **P.2 — Captain's Log** (`cl`, §7.4): one tool, no day-to-day/
  admin split — mark watched, set status/score, add show, resolve
  pending reviews, export/import, id-remap, scripting/bulk-ish
  queries, all in one tool. Any command run surfaces an inline
  pending-review resolve prompt if items are outstanding.

---

## Pre-Cutover

`SCOPE.md` §4.4. Required, timing otherwise flexible — can happen any
time after core build/functionality work is done, just has to
complete before full rollout.

- [ ] **PC.1 — One-time audit/log of existing local files not
  currently linked via Sonarr/Radarr.** So nothing already on disk
  gets silently lost once aniq (and its own file-awareness) is
  archived. Build as a script cross-referencing a real folder walk
  against `available_via_sonarr`/`available_via_radarr` (§5.2) — no
  new standing feature, just a one-off tool run once. Distinct from
  the *ongoing* detection of newly-added non-service files, which
  stays deliberately deferred (§9, §10.6) — don't build that here.
- [ ] **PC.2 — One-time historical imports**: Trakt watch history,
  AniList data, MAL legacy scores (§9/§6.1) — the only time data flows
  *into* LCARS from these sources rather than out.

---

## Cutover

`SCOPE.md` §4.4.

- [ ] **CO.1 — Switch actual daily use to Data**, once it's proven
  reliable through real daily use — parity with, or better than, the
  real aniq.
- [ ] **CO.2 — Archive aniq — do not delete it.** Frozen exactly as
  it stands, kept as a known-working emergency fallback in case Data
  ever breaks or has downtime. Not developed further after archiving.
- [ ] **CO.3 — Data becomes the permanent front-end, under its own
  name.** No merge-back into aniq, no rename.
- [ ] **Not scheduled here, explicitly parked**: a possible one-time
  sync of aniq's own leftover watch-history/episode-numbering data
  into Data/LCARS, in case the two diverged during the no-re-sync
  build-out period. Only worth thinking about once aniq is genuinely
  about to be retired for good — not a Cutover-day task.

---

## Deliberately not on this plan

Per `SCOPE.md` §9 — don't add steps for these unless `SCOPE.md`
changes first:

- Ongoing filesystem drift detection for newly-added non-service
  files (distinct from PC.1 above, which *is* scheduled).
- Content warnings/age ratings, genre/year/tracking-space-breakdown
  stats, a dedicated favorite/pinned flag, a personal notes/review
  field, push/email notification for pending reviews, general bulk
  *import* beyond the one-time historical imports in PC.2.
- Any ongoing aniq↔Data sync during the build-out.
- **AniList/MAL drift detection** (§3 principle 5, §6.9, §10.6 item
  12) — not scheduled as a phase step yet, but unlike the items above
  this one **is** intended to eventually get its own phase: pull
  AniList/MAL list state back periodically and diff it against LCARS
  to catch out-of-band edits made while Starfleet was down. Revisit
  once Phase B's push-only sync (B.4–B.10) is proven stable in real
  use — add a numbered phase to this document when that design work
  actually happens, don't build it opportunistically before then.
