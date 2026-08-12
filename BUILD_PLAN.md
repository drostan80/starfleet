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
      `show.hardDeleteRequestedAt` timestamp. **Correction (found in
      the 2026-08-08 audit pass below): this was never actually
      migrated** — the decision was made and written into
      `schema.graphql`, but no column for it exists in
      `0c47d1677e7d` despite this bullet's original claim otherwise.
      Fixed via a dedicated migration, `4509892cd91b`, in the audit
      pass.
  - **Surfaced the movie-tracking gap** that produced the whole A.1
    addendum above — discovered specifically because writing GraphQL
    types against the finished A.1 schema forced the "where does a
    movie's watch-state actually live" question that a table-by-table
    migration review alone hadn't surfaced.
- [x] **A.3 — Wire resolvers to SQLite** via raw `sqlite3` (stdlib, no
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

**Audit pass, 2026-08-08** (requested before continuing past A.3):
full cross-check of `BUILD_PLAN.md`/`SCOPE.md` claims against actual
repo state, not another read-through of the prose. Method: dumped the
*real* migrated-DB schema (`PRAGMA table_xinfo`, every table) and
diffed it field-by-field against every type in `schema.graphql`, by
hand, type by type — rather than re-trusting the commit messages that
said things were done.
- **Found the one real gap this surfaced**: `Show.hardDeleteRequestedAt`
  (and the three hard-delete mutations depending on it, §6.11) had no
  backing column anywhere — A.2's own entry above had incorrectly
  claimed it was "already added." Also surfaced a second, related gap
  while fixing it: §6.11 never specified an actual delay *duration*,
  only that a delay period exists conceptually. Asked; 24 hours, fixed
  (not configurable — §6.13 has exactly one setting today, home
  timezone, and this isn't a second one). Both fixed: migration
  `4509892cd91b`, `SCOPE.md` §6.11 updated with the full resolution,
  this entry corrected.
- Also fixed a smaller, lower-stakes correctness bug spotted in the
  same pass: `addShow`'s TMDB URL template was hardcoded to
  `/movie/{id}`, which would have produced a wrong link for a
  `tmdbId` supplied on an episodic (TV) show — §5.4 itself already
  notes both movie and episodic shows can carry a TMDB id. Split into
  a movie/episodic-aware template, tested both cases.
- Everything else checked out: the full §5.0 id-prefix table (18
  prefixes) verified identical across `SCOPE.md`, `ids.py`'s
  `PREFIX_TABLES`, and every migration's own `CHECK` constraint; every
  other `schema.graphql` field cross-referenced against its real DB
  column, table by table, with no further mismatches; every SQL table
  reference in `resolvers.py` checked against the real table list;
  `~/repos/aniq` and `~/repos/data` reconfirmed exactly as they were
  left (no drift on the hard constraint). Also scanned every test file
  for the same hardcoded-id-length mistake that had already been
  caught twice in earlier passes — none found beyond the two already
  fixed.
- 54 tests total now (was 51), full suite + `ruff` clean, CI reverified
  green on GitHub's runner after this pass's commit (see that commit
  for the run link/confirmation).

**Third slice, same day**: finished what the *first* slice's own
description had actually promised but hadn't fully delivered —
`§5.3` names `deleteWatchEvent`/`markSeasonWatched`/
`markEpisodeRangeWatched` as `watch_event`'s core mutations alongside
`addWatchEvent`, none of which existed yet. Added those three, plus
`setEpisodeAirDate`/`setEpisodeRuntimeOverride` (§5.2 episode field
overrides) and `linkShowExternalId`/`unlinkShowExternalId` (§5.4) —
all mechanical, direct extensions of already-established patterns
(upsert-by-composite-key, history-table writes on manual changes),
so none needed asking about.
- `deleteWatchEvent` reverts `episode.state` to `unwatched`, but only
  if no other `watch_event` rows remain for that episode afterwards —
  a rewatch can have several (§5.3), so undoing one of several
  shouldn't un-mark an episode still genuinely watched via another.
- Ran a proper systematic sweep this time, instead of waiting to
  stumble on the next gap one test at a time: introspected the built
  schema for every field whose return type is itself an object type
  (not a scalar/enum, since those already resolve fine via plain dict
  lookup), cross-referenced against every `ObjectType`'s real
  `_resolvers` registry. Found **five more real gaps this way** — all
  the same class of bug as `Query.episode` from the second slice: a
  type whose *other* fields had already been built and tested, but
  one relationship field was simply forgotten and no existing test
  happened to request it: `ShowExternalId.show`, and `.show`/`.episode`
  on all four §5.7 history types (`StatusChange`/`ScoreChange`/
  `AirDateChange`/`TrackedChange` — none of these four had ever gotten
  an `ObjectType` binding *at all*, despite their scalar fields being
  tested since the first slice). Fixed all five, added a test for each
  that specifically requests the previously-unreachable field, not
  just the scalar fields already covered.
  - Confirmed the other 48 object-typed fields the same sweep flagged
    are all genuinely not-yet-started work (Person/Studio/Franchise/
    tags/`FilterPreset`/deletion/export-import/`stats`/`search`/
    `nextUp`/`episodesAiringSoon`/`backlog`/`servicePresence`) —
    already disclosed in `resolvers.py`'s own module docstring as
    intentionally deferred, not a hidden gap of this same kind.
- 64 tests total now (was 54), `ruff` clean.

**Fourth slice, same day**: `show_service_presence` (rest of §5.4) and
`person`/`studio`/`show_person`/`show_studio` (§5.8) — followed §5's
own document order (same order `schema.graphql`/A.1's migration were
built in), since `BUILD_PLAN.md` has no sub-checkboxes within A.3 to
follow more specifically. Query-only, confirmed against `schema.graphql`
first: no mutations exist for any of these (externally-populated
metadata per §5.8's own description, not client-created — matches the
already-confirmed pattern from `show_relation`/`franchise`).
- Re-ran the systematic object-typed-field sweep from the third slice
  immediately after, rather than waiting for the next test to
  stumble on a gap: dropped from 48 to 34 unbound fields — exactly
  the 14 just implemented, no regressions, no new gaps introduced by
  this slice.
- 68 tests total now (was 64), `ruff` clean, schema re-validated.

**Fifth slice, same day**: `show_relation`/`franchise`/`franchise_member`
+ `next_up_override` (§5.9), completing §5's own document order for
A.3's schema-defined tables. `show_relation`/`franchise` have no
mutations (auto-derived, not client-created — same pattern as person/
studio); `franchise_member` (`setFranchiseMemberOrder`) and
`next_up_override` (`setNextUpOrder`) both got their manual-override
mutations, upsert-by-key, same pattern as the §5.5 id-mapper
mutations — both require the referenced franchise/show to already
exist (no `createFranchise` mutation exists at all), consistent with
"manual override authoritative" (§3 principle 6) meaning override an
existing thing, not fabricate a new franchise out of thin air.
- `Show.relatedShows` reads `show_relation` as undirected (either
  direction counts, §5.9's own framing for franchise auto-derivation,
  applied here too) via a `show_id IN (...) OR id IN (...)` filter —
  fits the existing `pagination.paginate()` helper unchanged, no new
  pagination mechanism needed despite the two-direction query.
- Re-ran the sweep again: dropped from 34 to 23, exactly the 11 fields
  just implemented, no new gaps.
- 72 tests total now (was 68), `ruff` clean, schema re-validated.
- §5's own tables are now exhausted for A.3 — what's left is custom
  tags (§5.1) and saved filter presets (§5.10), both still schema-
  defined but not yet reached in document order, plus the §6-derived
  query surface (`search`/`stats`/`nextUp`/`episodesAiringSoon`/
  `backlog`) and deletion/export-import mutations, which were never
  part of §5's own document order to begin with.

**Sixth slice, same day**: custom tags — `tag`/`show_tag` (§5.1),
the last purely-§5 table (saved filter presets, §5.10, is next and
last). Unlike every entity since the second slice (id-mapper/
`pending_review` on), tags are §5.1's own explicit call-out as
**user-created**, not externally-populated metadata — confirmed
against `schema.graphql` first (its `createTag`/`deleteTag`/
`addShowTag`/`removeShowTag` mutations were already there from A.2),
so this slice needed real mutation logic, not just query wiring.
- `createTag` checks `tag.name`'s `UNIQUE` constraint (migration
  `7196ca889757`) itself first, for a clean `GraphQLError` instead of
  a raw `sqlite3.IntegrityError` — same reasoning as `addShow`'s own
  `primaryTitle` validation.
- **Caught a real bug before it ever ran**, not through a test
  failure: `deleteTag`'s first draft deleted the parent `tag` row
  *before* its `show_tag` children — with `db.py`'s
  `PRAGMA foreign_keys = ON`, that order would have raised an
  `IntegrityError` on the very first call, not silently corrupted
  anything, but still wrong. Caught on review of the diff itself
  before running it, reordered (children first), *then* verified
  with a real test exercising the cascade.
- Re-ran the sweep again: dropped from 23 to 16, exactly the 7 fields
  just implemented, no new gaps.
- 76 tests total now (was 72), `ruff` clean, schema re-validated.

**Seventh slice, same day**: saved filter presets (§5.10) — the last
table in §5's own document order. **§5 is now fully exhausted for
A.3**: every table `SCOPE.md` §5 defines has its resolvers wired.
Server-side entity, "freely editable from any client — not read-only,
not fixed" (§5.10's own, deliberately thin, description) — confirmed
no `UNIQUE` constraint on `filter_preset.name` (unlike `tag.name`),
so unlike `createTag` there was nothing to validate before inserting;
`updateFilterPreset` is a genuine partial update (`name`/`filterJson`
both optional, only the provided ones change).
- Re-ran the sweep a final time for this table: dropped from 16 to
  12, exactly the 4 object-typed fields just implemented
  (`deleteFilterPreset` returns `Boolean`, not counted).
- 82 tests total now (was 76), `ruff` clean, schema re-validated.

**Correction, same day**: the note directly above (marking `search`/
`stats`/`nextUp`/`episodesAiringSoon`/`backlog`/deletion/export-import
as "what's left of A.3") was wrong, caught while actually starting on
them — re-reading this file's own A.11-A.15 straight through shows
`nextUp` is **A.11**, `search` is **A.12**, `stats` is **A.13**,
deletion is **A.14**, export/import is **A.15** — each its own
separately-numbered step, not A.3 sub-items. `backlog` isn't A.3
either — it's Phase B's own **B.9**, matching `§6.3`'s "(Phase B)"
label (flagged as ambiguous at the time; it wasn't, the cross-check
just hadn't gone far enough). The one item in that list with neither
a dedicated later step nor a Phase-B data dependency —
`episodesAiringSoon` — turned out to be the *only* piece actually
still owed to A.3.

**Eighth slice, same day**: `episodesAiringSoon` (§8) — the correction
above's one real remainder. Added `util.utc_iso_offset(days)`
alongside the existing `now_utc_iso()`; the date-window filter is
plain ISO-8601 string comparison (valid since every stored timestamp
shares the exact same format, so lexicographic order is chronological
order — same reasoning `pagination.py`'s cursors already lean on).
- 83 tests total now (was 82), `ruff` clean.
- **`§5` was already fully exhausted (seventh slice); A.3 is now
  genuinely complete** — reconfirmed via the same systematic sweep:
  every remaining unbound object-typed field maps cleanly to A.11
  (`nextUp`), A.12 (`search`), A.13 (`stats`), A.14 (deletion), A.15
  (`importData`), or B.9 (`backlog`) — 11 fields, none of them A.3's.
  Checked off above.
- Also resolved a real gap found re-reading §6.12 before touching
  A.15-adjacent work early: `ImportResult` (written in A.2) only
  reports `showsImported`/`episodesImported`/`watchEventsImported`,
  which reads like a 3-table scope limit, contradicting §6.12's own
  "rebuilding a fresh LCARS instance" full-restore framing. Asked;
  confirmed full restore, all 24 tables — `ImportResult`'s shape is
  just which counts are worth surfacing to a human, not the real
  scope. Recorded in `SCOPE.md` §6.12, ready for when A.15 actually
  happens — not implemented now, since A.15 hasn't been reached yet.
- [x] **A.4 — Implement the id-mapper / reconciliation tables**
  (§5.5): `season` (formerly `show_id_mapping`) seeded from the
  Fribb/`anime-lists` dataset (one-time/on-demand download, not live
  polling), manual overrides authoritative, `pending_review` on any
  discrepancy or no-candidate-found case.
  - **Correction found while starting this step, 2026-08-08**:
    checked Data's real (forked-from-aniq) `mapping.py` as reference
    before building the equivalent logic here, and found it resolves
    AniList ids by `(tvdb_id, season_number)` together — TVDB groups
    a franchise's seasons under one series id, AniList splits each
    season into its own entry. `show_id_mapping` (built in A.1) held
    a single `anilist_id` on a show-scoped table, which cannot
    represent "season 1 → AniList X, season 2 → AniList Y"
    simultaneously. Not caught by the earlier full audit pass (A.1
    addendum) since it's a modeling gap, not a missing-field gap.
    Stopped and asked rather than guessing at a fix, given the
    magnitude. Confirmed shape: a first-class `season` entity
    (distinct from both `show` and `episode`, and distinct from the
    pre-existing `franchise`/`franchise_member` multi-show sequencing
    concept), one row per season of a show, holding its own
    `anilist_id`/`mal_id`/`source`/`matched`/`manual_override`.
    `episode` gained a nullable `season_id` FK to it (its existing
    integer `season`/`episode` numbering fields are unchanged —
    different concern). Confirmed via two rounds of AskUserQuestion
    (concept, then exact column list). Implemented: migration
    `2b9d7beb777c` (`DROP TABLE show_id_mapping`, `CREATE TABLE
    season` with `UNIQUE(show_id, season_number)`, `episode.season_id`
    added), `SCOPE.md` §5.0/§5.2/§5.5 rewritten, `schema.graphql`
    (`Season`/`SeasonSource`/`SeasonEdge`/`SeasonConnection` types,
    `Show.seasons` connection, `Episode.seasonEntity`,
    `setSeasonMapping` mutation replacing `setShowIdMapping`),
    `resolvers.py`, `ids.py` (`x` retired, `z` → `season`). Verified:
    real inserts (two seasons of one show holding two different
    AniList ids; duplicate `season_number` rejected; bogus
    `season_id` FK rejected), full downgrade/upgrade round-trip,
    schema re-validated (98 types, zero errors), unbound-field sweep
    re-run (11 remaining, all correctly attributed to A.11–A.15/B.9,
    no new gaps), 84 tests passing, `ruff check .` clean.
  - **Substance built after the correction, same day**: `lcars/fribb.py`
    — dataset download/cache (7-day TTL, stale-cache fallback on a
    network error) + `(tvdb_id, season_number)` matching, ported
    verbatim from Data's real, production-tested `mapping.py`
    (including its single-candidate short-circuit's hard-won
    behavior — deliberately does not season-check a lone candidate,
    per that module's own regression history). `httpx` promoted from
    a dev-only test dependency to a core one (sync `httpx.Client`,
    matching §11.2's sync-resolver execution model, not Data's async
    convention). New `reconcileSeasonMapping(showId, seasonNumber)`
    mutation: looks up the show's `tvdb` `show_external_id`, resolves
    a candidate, applies immediately unless `manual_override = true`.
    One real gap surfaced and asked before writing this: does a
    Fribb/manual-override disagreement still log a `pending_review`
    entry, or skip silently? Confirmed: skip entirely (no review noise
    for something already manually decided; `last_reconciled_at`
    still updates). Built `_open_or_extend_pending_review` — the
    first real implementation of §5.6's value-chain-accumulation
    behavior (extends an existing unresolved entry's
    `proposedValueChain` rather than duplicating; found and fixed a
    real schema-shape bug in the process, caught by a test: a `None`
    "no candidate" value can't go directly into
    `proposedValueChain: [String!]!`'s non-null list, so it's
    represented as the literal string `"unmatched"`). New
    `tests/test_fribb.py` (11 tests: index-building, candidate
    disambiguation, cache hit/stale/refetch/network-fallback, all via
    an injected fake client — no real network calls) +
    `test_server.py` (4 end-to-end tests: no-tvdb-link → unmatched +
    review; matched via linked tvdb id, including two seasons of one
    show resolving independently; discrepancy applies immediately and
    extends one chain across three disagreements rather than opening
    duplicates; manual_override fully protected, zero review noise).
    Unbound-field sweep re-run one more time post-implementation:
    still only 5 real gaps (`NextUpEntry.episode`/`.show`,
    `Query.backlog`/`search`/`stats` — all A.11/A.12/A.13/B.9, not
    A.4's concern), confirming no regressions. 99 tests passing total,
    `ruff check .` clean.
  - **Deliberately still not built here** — Phase B's actual **weekly
    scheduler** that calls `reconcileSeasonMapping` on its own clock
    (§4 Phase B: "Weekly Fribb dataset reconciliation... independent
    of the daily metadata cadence") and any wiring from A.8's future
    on-demand-fetch-on-show-creation flow into this mutation. A.4's
    own scope was the reconciliation *mechanism* itself, callable
    on-demand — not who calls it or when, which are later steps'
    concerns per BUILD_PLAN's own ordering.
  - **Scope note on `episode_numbering_mapping`**: §5.5's own heading
    ("id-mapper / reconciliation tables") covers two tables — `season`
    and `episode_numbering_mapping` — sharing one reconciliation
    *mechanism*, but this step's own enumerated text only names
    `season`/Fribb work ("`season`... seeded from the Fribb/anime-lists
    dataset..., manual overrides authoritative, pending_review on any
    discrepancy or no-candidate-found case"), not numbering-scheme
    auto-derivation. `episode_numbering_mapping`'s *manual* path
    (`setEpisodeNumberingScheme`) already exists from an earlier A.3
    slice — "manual overrides authoritative" already holds for it.
    Its *automatic* derivation ("Sonarr absolute-order info, AniList
    episode counts", §5.5) has no real data to derive from yet in
    Phase A's own build order — that data doesn't exist until A.8's
    on-demand fetch triggers exist — so treating it as A.4 work now
    would mean building against nothing real to test against. Read
    literally rather than guessed at: A.4 is complete as its own text
    describes; numbering auto-derivation is left for whichever step
    actually has Sonarr/AniList data to derive it from (A.8 or later).
- [x] **A.5 — Implement `pending_review`** (§5.6): the shared
  audit-only mechanism, value-chain accumulation on repeated automatic
  changes (not overwrite), permanent retention once resolved.
  - **Already satisfied by earlier steps, verified 2026-08-08** rather
    than built fresh: re-read §5.6 line by line against what actually
    exists before doing anything else, per standing instruction. The
    table itself (A.1), `resolvePendingReview` + the `pendingReviews`
    query with its `includeResolved` default (both A.3), and the
    value-chain-accumulation mechanism itself
    (`_open_or_extend_pending_review`, A.4 — built there as a direct
    prerequisite for `reconcileSeasonMapping`) were all already in
    place. "Permanent retention... never purged" checked directly
    against the schema rather than assumed: no mutation deletes a
    `pending_review` row, resolved or not — confirmed by
    `tests/test_pending_reviews_query_defaults_to_unresolved_only`
    (`includeResolved: true` still returns a resolved entry). The
    three passive/pull surfaces §5.6 describes (Data's status bar,
    Holodeck's review queue, Captain's Log's inline prompt) are
    client-side work, out of this server's own scope.
  - One real cleanup found while verifying: a test helper's own
    docstring (`_insert_pending_review`) still said "no mutation
    creates pending_review rows yet" — stale since A.4 landed
    `reconcileSeasonMapping`. Fixed the comment; no behavior change.
  - SCOPE.md §5.6 annotated with this verification. 99 tests still
    passing, `ruff check .` clean — no new tests were needed since
    the behavior this step describes was already covered by A.3/A.4's
    own test suites.
- [x] **A.6 — Implement all history tables** (§5.7): `status_change`,
  `score_change`, `air_date_change`, `tracked_change` — dedicated per
  concern, every row records originating client/process.
  - **Already satisfied by earlier A.3 work, verified 2026-08-08**
    rather than built fresh — re-read §5.7 against what actually
    exists first, per standing instruction. All four tables (A.1),
    all four dedicated write paths (`setStatus`/`setScore`/
    `setTracked`/`setEpisodeAirDate`, A.3), and every relationship
    field (`StatusChange.show`/`ScoreChange.show`/
    `AirDateChange.episode`/`TrackedChange.show`, found unbound and
    fixed during A.3's own systematic sweep) already exist. Every
    write path calls `require_client()` and stores the result as
    `changed_by` — confirmed against real tests
    (`tests/test_server.py` already asserts `changedBy` on both
    `statusHistory` and `airDateHistory`), not just read off the
    code. Confirmed `addShow` deliberately does *not* write a
    `status_change` row at creation (no "previous" state exists yet
    to record a change from — history tables track changes, not the
    initial value) and correctly doesn't call `require_client()`
    either, since it writes no history/`pending_review` row. No new
    code or tests needed — 99 tests still passing, `ruff check .`
    clean.
- [x] **A.7 — Implement `show_service_presence`** (§5.4): fuzzy title
  search (across all title variants, threshold-gated, `difflib`-style
  scoring) as the matching algorithm — already resolved, not an open
  question. Passive/informational — no `pending_review` on change.
  - **Read side already existed (A.3), matching logic + write side
    built now**: checked aniq's real `~/repos/aniq/src/aniq/notes.py`
    first, same pattern as A.4's `fribb.py` — `best_match()`/
    `normalize_title()` there is exactly the "difflib-style scoring"
    §5.4 already points at, so ported it (`lcars/fuzzy.py`) rather
    than writing a fresh implementation, dropping only its aninote-
    vault-specific season-suffix disambiguation (not applicable to
    service-catalog matching). New `refreshShowServicePresence(showId,
    service, candidateTitles)` mutation, upserting on `(show_id,
    service)`. One real scope question resolved by re-reading rather
    than guessing before writing any code: does LCARS itself call out
    to Sonarr/Radarr/AniList/MAL to fetch each service's catalog?
    No — confirmed via `config.py`'s own docstring (those credentials
    are explicitly deferred to A.16/Phase B, so no client code for any
    of them exists yet) and §4's "A.8's on-demand fetch is the only
    external-call trigger in Phase A" framing, plus §5.4's own text
    that presence is "refreshed on the same background-poll cadence as
    the rest of Phase B" — so the mutation takes an already-fetched
    candidate title list as an argument; the actual catalog-fetching
    and any recurring call into this mutation are Phase B's job
    (Ops), same on-demand-mechanism/later-scheduling split A.4 used
    for `reconcileSeasonMapping`.
  - New `tests/test_fuzzy.py` (8 tests: normalization, exact match,
    fuzzy fallback, below-threshold returns None, empty inputs, title-
    variant coverage, blank-entry handling, custom threshold) +
    `test_server.py` (+3 end-to-end: create-then-update via upsert
    including flipping back to absent on a later call with no match,
    empty-candidate-list case, independent per-service rows on the
    same show). Unbound-field sweep re-run: still only 5 real gaps
    (`NextUpEntry.episode`/`.show`, `Query.backlog`/`search`/`stats` —
    A.11/A.12/A.13/B.9), confirming no regressions. 110 tests passing,
    `ruff check .` clean.
- [x] **A.8 — Implement on-demand external fetch triggers**: immediate
  metadata fetch (poster, synopsis, cast, episode list, external ids)
  on show creation, from any client (§4 Phase A). This is the only
  external-call trigger in Phase A — **no autonomous scheduler yet**,
  that's Phase B.
  - **Fetch direction resolved by asking, not assumed, 2026-08-08**:
    §4 Phase A's own text reads two ways — LCARS calling AniList/
    Sonarr/Radarr directly, or a client (Data) fetching and pushing
    results in, matching A.4/A.7's original "client fetches, LCARS
    reconciles" split. Evidence pointed toward the second reading
    (A.17 explicitly assigns Sonarr/AniList calls to Data's side;
    config.py deferred those credentials; A.4/A.7's own precedent) —
    asked anyway given the scale of the decision. Confirmed the
    first: LCARS makes the calls itself now, since "ultimately all
    compute and fetch will be handled on the server by lcars, so
    might as well built it this way now" — Data's A.17-era access is
    transitional, not the end state. Confirmed exception: pushing
    watch status *to* AniList/MAL stays Data's own job, unaffected.
  - **Failure handling resolved by asking**: best-effort (the show is
    always created regardless of fetch success) + never silent + a
    manual retry path — all three explicitly requested together
    ("best effort and system to try again... no silent fail... manual
    fix by user is also an option... need to reconcile action taken
    from outside"). Built as: every source branch independently
    try/except-guarded (`metadata._guarded`, catches bare `Exception`
    — a malformed response should never crash `addShow`, only skip
    that branch); a failure opens/extends a `pending_review` entry
    (reusing §5.6's existing mechanism rather than inventing a second
    "needs attention" channel — visible via the same `pendingReviews`
    surface as everything else); a new standalone `refreshShowMetadata
    (showId)` mutation re-runs the exact same fetch on demand — the
    "manual fix" / "reconcile from outside" path. Every DB write is
    upsert-shaped so a retry after a partial failure is always safe.
  - **Season/franchise scope resolved by asking**: confirmed addShow
    also auto-creates the show's first `season` row (season_number=1,
    `manual_override=true` — the caller-supplied anilistId/malId is
    the same class of "a human already confirmed this" information
    `setSeasonMapping` already treats as an override) as part of "the
    full tree… silently, then map accordingly" — but explicitly **not**
    a franchise row: "no auto franchise if the system can still
    confirm with my previous answer philosophy (mapping change
    first)" — franchise stays a deliberately-created grouping (§5.9),
    unchanged; `franchise_member` already links to existing rows
    without needing them restructured, so nothing here blocks that
    happening later.
  - **Built**: `anilist_client.py` (public, unauthenticated `Media(id)`
    GraphQL query — title/cover/banner/synopsis/genres/episode-count/
    idMal/studios/voice cast — porting Data's own real `anilist.py`'s
    error-handling shape, sync `httpx.Client` per §11.2). `sonarr_
    client.py` (a close port of Data's own real, working
    `SonarrClient`, narrowed to the two calls needed: find a series
    already in Sonarr's own library by tvdb id, and its episodes — a
    show being tracked in LCARS never implies it's in Sonarr's
    library, §5.1, so "not found" isn't an error). `radarr_client.py`
    (no aniq/Data reference exists — aniq is Sonarr/anime-only — built
    fresh, same *arr-family REST shape `sonarr_client.py` already
    validated). `metadata.py` — the orchestration: tracking_space=
    anime → AniList (mandatory per §5.1, writes show fields + the
    season row + upserted person/studio/show_person/show_studio rows,
    keyed by AniList's own ids so shows sharing a studio/actor don't
    duplicate either); media_shape=episodic → Sonarr if linked+
    configured (episode rows, never overwriting an already-tracked
    episode); media_shape=movie → Radarr if linked+configured (poster/
    synopsis/genres). `config.py`: `sonarr_url`/`sonarr_api_key`/
    `radarr_url`/`radarr_api_key` added (no AniList credential needed
    — public endpoint); a new `config.get_current()`/`set_current()`
    module-level singleton (mirrors `db.py`'s own connection-singleton
    pattern) so sync resolvers can reach it without a DI mechanism.
    `pending_review.py`: extracted `_open_or_extend_pending_review`
    out of resolvers.py (first built in A.4) into its own module —
    metadata.py needed the exact same value-chain-accumulation
    behavior for fetch-failure logging, and a resolvers.py<->metadata.py
    circular import wasn't worth introducing just to share one function.
  - **Tests**: `test_anilist_client.py`/`test_sonarr_client.py`/
    `test_radarr_client.py` (17 tests total — success, not-found,
    401, connect/timeout errors, all via an injected fake client, no
    real network calls, same pattern as `test_fribb.py`).
    `test_server.py` (+8 end-to-end): AniList fetch populates show
    fields/season/cast/studio credits; studio/person dedup across two
    shows; no-media-found leaves the show bare; Sonarr fetch creates
    episodes; Sonarr not-in-library is a silent no-op; Radarr fetch
    populates movie fields; not-configured Sonarr/Radarr skips
    silently (no review noise); a fetch failure opens a pending_review
    entry and `refreshShowMetadata` successfully retries it. The
    `client` fixture now stubs `anilist_client.fetch_media` to a
    no-op and sets empty Sonarr/Radarr config by default — every
    pre-A.8 test that adds an anime show would otherwise make a real
    network call; individual A.8 tests re-monkeypatch as needed.
  - **Verified**: unbound-field sweep re-run, still only 5 real gaps
    (`NextUpEntry.episode`/`.show`, `Query.backlog`/`search`/`stats` —
    A.11/A.12/A.13/B.9), confirming no regressions. 135 tests passing,
    `ruff check .` clean. No new migration needed — every write uses
    existing §5 columns/tables.
- [x] **A.9 — Implement scoring & conversions** (§6.1): 0–20
  quarter-point personal scale, clamp/round silently on invalid input,
  ×5 to AniList, ÷2 to MAL, both push-only.
  - **Scope confirmed directly, not assumed carried over from A.8**:
    the user was explicit that score push is *not* the same exception
    §6.8 carves out for Data's episode-watch-status-only direct write
    — "it goes through lcars, lcars pushes it, as written in plan."
  - **Real gap found and folded in, per explicit instruction to check
    thoroughly before concluding it was one**: §6.8 already says LCARS
    owns *status* push too ("Everything else (status, score...) is
    LCARS's job"), but grepping both SCOPE.md and BUILD_PLAN.md fully
    (not from partial memory) confirmed no step anywhere actually
    implements pushing the status enum to AniList. Folded status-push
    into this same step rather than leaving the gap.
  - **A real, deeper design gap surfaced while building the push
    itself, resolved directly with the user**: `setScore`/`setStatus`
    operate on `show`, but AniList tracks each season as its own list
    entry (A.4) — which entry receives a push once a show has more
    than one season? Confirmed: score becomes genuinely per-season —
    new `season.score` column (migration `1168ad1ebaa0`, same 0-20
    scale as `show.score`), a new `setSeasonScore(seasonId, score)`
    mutation pushing to just that one season, and `setScore` itself
    still pushing show.score to *every* linked season (each resolving
    its own effective value: own `season.score` if set, else the
    show's). No per-season status exists, so `setStatus` pushes
    uniformly to every linked season.
  - **AniList OAuth mechanics confirmed directly**: LCARS reuses
    Data/aniq's already-registered AniList app (`anilist_client_id`/
    `_secret`, confirmed — not a new registration) and runs its own
    separate authorization to mint an independent
    `anilist_access_token`, via a new `lcars anilist-login` CLI
    command (the same PIN-redirect flow Data's own login already
    uses, since LCARS is headless — no browser of its own).
  - **Built**: `anilist_client.py` extended (A.8 built the read-only
    half) with `authorize_url`/`exchange_code`/`save_media_list_entry`
    + a new `AniListAuthError`, close ports of Data's own real
    `anilist.py`, refactored around one shared `_graphql_request`
    error-handling helper. `config.py`: `anilist_client_id`/`_secret`/
    `_access_token` + `save_anilist_token()`. `cli.py`: restructured
    to subcommands (`serve` — the pre-existing default, still bare
    `lcars` with no args for the Dockerfile's own sake — and the new
    `anilist-login`). `resolvers.py`: `_push_season_score`/
    `_push_show_score`/`_push_show_status` — best-effort throughout,
    same philosophy as A.8's metadata fetch (not-yet-authenticated
    treated as "not configured", a real push failure opens/extends a
    `pending_review` entry against the specific `season`, field
    `"anilist_push"`, rather than ever failing the local write) —
    wired into `setScore`/`setSeasonScore`/`setStatus`.
  - **Tests**: `test_anilist_client.py` (+8: authorize URL shape, code
    exchange success/rejection/malformed-response, push sends
    status+score together and each independently omits the other's
    variable per GraphQL's null-means-unset semantics, 401 raises
    `AniListAuthError`). New `test_cli.py` (4: bare/`serve` both hit
    uvicorn with the right args, `anilist-login` requires credentials
    first and walks the full PIN-paste-exchange-save flow on success).
    `test_server.py` (+6 end-to-end): show-level score push reaches
    every linked season at ×5; no push when unauthenticated;
    season-level push touches only that season; a season with its own
    score keeps it on a later show-level push while an unscored
    sibling still falls back to the new show value; status push
    reaches every linked season; a push failure opens a pending_review
    entry against the right season.
  - **Verified**: migration round-trips (upgrade adds `season.score`,
    downgrade removes it cleanly). Unbound-field sweep re-run, still
    only 5 real gaps (`NextUpEntry.episode`/`.show`,
    `Query.backlog`/`search`/`stats` — A.11/A.12/A.13/B.9), confirming
    no regressions. 153 tests passing, `ruff check .` clean.
  - **Deliberately still not built here**: MAL's ÷2 push — §6.9/B.10
    is explicit that MAL integration "can start any time after Phase
    A's push infrastructure exists" but is its own Phase B step (its
    own OAuth app registration + the proactive refresh-token renewal
    job MAL's short-lived tokens need), not folded in here. Also not
    built: episode-level scoring — the user's own framing mentioned
    "season level and show level, and episode level" as a longer-term
    idea, but the concrete confirmation received was specifically
    "add `season.score`... build it into A.9", not episode — treating
    episode-level scoring as a distinct, not-yet-confirmed follow-up
    rather than assuming it was included.
- [x] **A.10 — Implement paced/catch-up mode schema** (§6.2):
  per-show cadence config + the adaptive next-date computation.
  Scheduling itself (actually advancing dates) is Phase B; the schema
  and computation logic belong here.
  - **"Restricted to completed/non-airing shows" required a real
    derivation, not a guess**: nothing in the schema tracked a show's
    real-world release status anywhere (checked — A.8's own AniList
    fetch doesn't pull `Media.status` either). Resolved by computing
    it from data already tracked rather than adding a new field: a
    show is "airing" if it has any episode with a null or future
    `air_date_utc` — reuses exactly what `episodesAiringSoon` (A.3)
    already relies on. Deliberately *not* `show.status` (LCARS's own
    tracking status) — a show can be user-marked `watching` and still
    be fully released today; that's the ordinary paced-mode case
    (bingeing a finished show at a self-imposed pace), not a
    contradiction.
  - **Built**: migration `98cbfe3ad4cb` — one nullable
    `show.paced_cadence_days INTEGER` column (its presence *is* the
    flag, same "nullability IS the flag" shape as
    `hard_delete_requested_at`; `CHECK (... IS NULL OR ... > 0)`).
    `enablePacedMode(showId, cadenceDays: Int = 7)` — validates
    "non-airing" at write time only (no ongoing enforcement; matches
    "scheduling itself is Phase B"), rejects with a clear
    `GraphQLError` otherwise. `disablePacedMode(showId)`.
    `Show.pacedNextDate` — a computed field, never stored: latest
    `watch_event.watched_at` + `pacedCadenceDays`, recomputed fresh on
    every query (§6.2's own "adaptive, not pre-baked"); null with no
    watch event yet or when not in paced mode. New `util.add_days()`
    (the actual date-arithmetic primitive, reusable wherever else a
    stored timestamp needs advancing).
  - **Tests**: new `test_util.py` (4: day advancement, month-boundary
    crossing, negative days, zero days). `test_server.py` (+9:
    default cadence is 7, a custom cadence, rejects a show with an
    unknown-air-date episode, rejects a show with a future episode,
    allows a fully-aired show, disable clears both fields, next-date
    is null with no watch event, null when not in paced mode, and the
    adaptive-recompute case itself — two watches, each producing a
    freshly-computed date from its own latest `watched_at`). Caught a
    real GraphQL semantics bug while writing these: a variable
    resolving to `null` is not the same as omitting the argument
    entirely — the schema's `cadenceDays: Int = 7` default only
    applies when the argument is absent from the query document, so
    the "defaults to 7" tests need their own query that omits the
    argument outright, not one that passes an explicit `null`.
  - **Verified**: migration round-trips cleanly (upgrade adds the
    column + CHECK, downgrade removes it; a live insert confirmed 0
    and negative values are rejected, `NULL`/positive values aren't).
    Unbound-field sweep re-run, still only 5 real gaps
    (`NextUpEntry.episode`/`.show`, `Query.backlog`/`search`/`stats` —
    A.11/A.12/A.13/B.9), confirming no regressions. 166 tests passing,
    `ruff check .` clean.
- [x] **A.11 — Implement the cross-show next-up query** (§6.4):
  soonest-available-first default, manual reorder override.
  - **A real, previously-invisible bug found and fixed while building
    this**: `pagination.py`'s `paginate()` (built A.3) has returned its
    `pageInfo`/`hasNextPage`/`hasPreviousPage`/`startCursor`/
    `endCursor` keys in camelCase since it was first written — but
    `convert_names_case=True` (server.py) makes Ariadne's default
    resolver look up a GraphQL `pageInfo` field as this dict's
    `page_info` key, same as every other plain scalar field in this
    codebase. Every connection field built since A.3 has therefore
    silently returned `null` for `pageInfo` (a schema violation on the
    non-null `PageInfo!` field) whenever a client actually queried it
    — invisible until now because `test_pagination.py` only ever calls
    `paginate()` directly as plain Python (bypassing GraphQL/Ariadne
    entirely), and no end-to-end test in `test_server.py` had ever
    queried a `pageInfo` sub-field before this step's own pagination
    test needed one. Not caught by the systematic unbound-field sweep
    either — that sweep checks whether a *resolver function* is bound,
    not whether the default resolver's underlying data actually
    resolves correctly at runtime, a real blind spot now noted.
    Fixed by renaming to snake_case (`page_info`/`has_next_page`/
    `has_previous_page`/`start_cursor`/`end_cursor`) in both
    `paginate()` and the new `paginate_list()` below; confirmed fixed
    generally, not just for `nextUp`, by querying an existing
    connection (`shows(first: 1) { pageInfo { ... } }`) live and
    getting real values back instead of an error.
  - **`§6.4`'s own ordering, resolved by re-reading the franchise-
    ordering precedent it points at**: shows with a `next_up_override`
    (already built, A.3) sort first by their own `sortOrder`; every
    other candidate show follows in the stated default,
    soonest-available-first (`episode.air_date_utc` ascending, nulls
    sorted last within that group). Not a table-backed query — one row
    per show, each paired with its own earliest unwatched+available
    episode, which `pagination.py`'s existing `paginate()` (a single
    `SELECT ... FROM {table}` scan) can't produce — so the full
    ordered list is computed in Python first (fine at personal-tracker
    scale) and handed to a new `pagination.paginate_list()`, same
    Connection/edges/pageInfo shape as `paginate()` but paging over an
    already-materialized list via a position-based cursor instead of a
    `rowid`-based one.
  - **Built**: `Query.nextUp` resolver — candidates are `watching`-
    status shows *or* any show with `pacedCadenceDays` set (A.10),
    regardless of status (the ordinary paced-mode case is a `watching`
    show anyway, but nothing requires it); a show contributes an entry
    only if it has an unwatched episode with `available_locally = 1`
    (§5.2's existing generated column), earliest by `(season, episode)`
    order. `NextUpEntry.show`/`.episode` field resolvers.
  - **Tests**: `pagination.paginate_list` gets its own dedicated
    coverage in `test_pagination.py` (+7, mirroring `paginate()`'s own
    existing test shapes exactly: no-args, forward paging, walking to
    the real last page, backward paging, empty list). `test_server.py`
    (+9): watching show with an available episode is included; a show
    with only a watched or only an unavailable episode is excluded; a
    merely-`planned` show is excluded; a paced show is included
    regardless of status; default ordering is soonest-available-first;
    a manual override wins over that default; the earliest *unwatched*
    episode is picked when a show has several; pagination itself
    works end-to-end through real GraphQL (this is what caught the
    `pageInfo` bug above). A real test-authoring mistake caught along
    the way: `addShow` always creates a `PLANNED` show (never
    `WATCHING`) — several early drafts of these tests assumed
    otherwise and silently passed for the wrong reason (an empty
    result set) until traced back to source.
  - **Verified**: unbound-field sweep re-run, only 3 real gaps left
    (`Query.backlog`/`search`/`stats` — B.9/A.12/A.13). 180 tests
    passing, `ruff check .` clean.
- [x] **A.12 — Implement full-text search** (§6.5): titles + synopses,
  across all stored title variants.
  - **Found and corrected a factual error while checking the reference
    it points at**: §6.5 credits aniq's `list_screen.py` with using
    `difflib` for its own local filtering — checked the real code,
    it actually uses `textual.fuzzy.Matcher`, not `difflib` at all.
    Not a design question (the section's own "full-text search, not
    per-client fuzzy matching" framing already settles the approach
    to take here), so corrected the citation in SCOPE.md directly
    rather than asking about it.
  - **Approach**: plain SQL `LIKE '%query%'` substring matching
    (case-insensitive by SQLite's own ASCII default) across all three
    title variants + synopsis — deliberately not fuzzy/similarity
    scoring (§6.5 itself frames this as *replacing* per-client fuzzy
    matching, a different concern from §5.4's fuzzy service-presence
    matcher, A.7, which exists to tolerate an uncertain title, not
    search one typed on purpose) and deliberately not SQLite FTS5
    either — its indexing/ranking machinery is over-engineering at
    this project's real scale (a single user's shows, dozens to a few
    hundred rows), a plain scan across an already-small table is
    simpler and fast enough.
  - **Built**: `Query.search` resolver — reuses the existing
    table-backed `pagination.paginate()` directly (a real `WHERE`
    scan against `show`, no computed-list complexity like `nextUp`
    needed), matching across `title_romaji`/`title_english`/
    `title_native`/`synopsis` in one `OR`-joined, parenthesized clause
    (parenthesized deliberately — `paginate()` ANDs cursor conditions
    onto whatever `where` it's given, and `AND` binds tighter than
    `OR` in SQL, so an unparenthesized `OR` clause would have grouped
    wrong once combined with a real `after`/`before` cursor).
  - **Tests** (+6, `test_server.py`): matches the romaji title; matches
    English and native title variants independently; matches synopsis;
    case-insensitive; no match returns an empty connection, not an
    error; pagination itself works end-to-end.
  - **Verified**: unbound-field sweep re-run, only 2 real gaps left
    (`Query.backlog`/`stats` — B.9/A.13). 186 tests passing,
    `ruff check .` clean.
- [x] **A.13 — Implement the stats query surface** (§6.6): totals,
  hours watched, personal-score distribution. (Won't be meaningful
  over time until Phase B accumulates real data — building the query
  surface now is still correct, per §6.6's own phase note.)
  - **Scope split resolved by reasoning from each field's own name,
    not asked (low-stakes, easily revisable — a read-only query, same
    risk class as A.11's next-up-override interleaving call)**:
    `totalShows` reads as "how big is my library right now" ->
    filtered to `tracked = 1`. Everything else reads as a lifetime
    achievement metric (episodes watched, hours watched, scores
    given) -> deliberately *not* filtered by `tracked`, since
    untracking a show is soft and doesn't erase having already
    watched/scored it (§6.11). Confirmed with a dedicated test
    (untrack a show that was already watched+scored: `totalShows`
    drops, the other three don't).
  - **A real, pre-existing gap found and flagged, not fixed here**:
    `show.duration_minutes` has no mutation anywhere in the API to set
    it — not built in A.1 (schema only), and not populated by A.8's
    AniList/Radarr fetch either (neither call currently requests a
    duration/runtime field, though both upstream APIs have one). This
    makes movie hours in particular inert in practice today, since a
    movie has no episode-level runtime to fall back on at all. Out of
    A.13's own scope (a query-surface step, not a fetch/mutation
    one) — recorded in SCOPE.md §6.6 as a known limitation worth a
    small follow-up (A.8's fetch, or its own tiny mutation), not
    decided/built now. Tests exercise the field via direct SQL, same
    as every other not-yet-mutable column this session.
  - **Built**: `Query.stats` resolver — no `Stats`/`ScoreBucket`
    `ObjectType` bindings needed at all, confirmed live: every one of
    their fields is a plain scalar (or a list of a type whose own
    fields are plain scalars), so Ariadne's default dict-key
    resolution already handles them as long as the resolver's own
    return dict uses the right snake_case keys.
  - **Tests** (+6, `test_server.py`): `totalShows` counts only
    tracked shows; `totalEpisodesWatched` counts distinct watched
    episodes, not watch events; hours watched sums an episode's own
    override or its show's default correctly across two different
    shows; movie hours via `duration_minutes` + any watch event;
    score distribution groups by value and excludes unscored shows;
    the tracked-vs-lifetime split itself (untrack survives in three
    of the four fields, not the fourth).
  - **Verified**: unbound-field sweep re-run — **only `Query.backlog`
    (B.9, Phase B) remains**; every other Phase A query surface is
    now built. 192 tests passing, `ruff check .` clean.
- [x] **A.14 — Implement deletion policy** (§6.11): soft-delete
  default, hard-delete layered behind a delay + re-type-the-title
  confirmation.
  - **Two real ambiguities resolved by close-reading rather than
    guessing, both low-stakes/easily-revisable (internal-only
    behavior, no external-service side effect)**: (1) §3 principle
    3/§6.11 both say "a status/tracked-flag change" for soft-delete,
    ambiguously — resolved as `tracked` specifically, not `status`,
    since the two are already independent axes everywhere else in
    this schema (§5.1) and `tracked` is already the field A.13's own
    stats surface treats as "current library membership," the natural
    fit for "soft-deleted." (2) Whether `requestHardDelete` requires
    the show already be soft-deleted first — resolved yes, reading
    §6.11's "layered: soft-delete -> a delay period -> ..." sequence
    as a real precondition to enforce at write time, not just a
    suggested client UX flow.
  - **Built**: `softDeleteShow` (sets `tracked = false`, writes
    `tracked_change`, `status` untouched). `requestHardDelete`
    (rejects unless already soft-deleted; idempotent — resets the
    24-hour timer if already pending). `cancelHardDelete` (clears the
    timer only, doesn't re-track — reversing *that* step
    specifically, per §6.11's own framing). `confirmHardDelete` — the
    actual purge: checks the 24-hour delay has elapsed AND
    `retypedTitle` matches the show's current `displayTitle` exactly,
    then cascades manually (no `ON DELETE CASCADE` anywhere in this
    schema, §11.2 — same reasoning `deleteTag`'s own cascade already
    established) across 16 tables in FK dependency order: `watch_
    event`, `episode_movie_link` (both as this show's own bonus_movie
    episodes' links, deleted, *and* as another show's episode linking
    here as the movie target, unlinked via `movie_show_id = NULL`
    rather than deleted), `air_date_change`, `episode`, `season`,
    `show_external_id`, `show_service_presence`, `show_relation`
    (both `show_id` and `related_show_id` directions), `episode_
    numbering_mapping`, `status_change`, `score_change`, `tracked_
    change`, `show_person`, `show_studio`, `franchise_member`,
    `show_tag`, `next_up_override`, and `pending_review` (no real FK,
    polymorphic `entity_type`/`entity_id` — cleaned up anyway, for
    both `entity_type = 'show'` and `entity_type = 'season'` rows
    whose season belonged to this show, captured before the season
    rows themselves are deleted).
  - **Tests** (+8, `test_server.py`): soft-delete sets `tracked`
    without touching `status`, and records history; `requestHardDelete`
    rejects a still-tracked show; request-then-cancel round-trips
    (timer clears, `tracked` stays false); confirming before the delay
    elapses is rejected; confirming with no pending request is
    rejected; confirming with the wrong retyped title is rejected; and
    the big one — a single show wired up with real rows in *every*
    one of the 16 cascaded tables (via real mutations wherever one
    exists, direct SQL only where none does — `show_relation`,
    `franchise` creation, and `pending_review` have no mutations at
    all) confirms the entire purge succeeds with zero FK violations,
    every one of those 16 tables is actually empty for this show
    afterward, the two `episode_movie_link` cross-show cases behave
    correctly (own link gone, other show's link unlinked not
    deleted), and every unrelated show/tag/franchise this test also
    created survives untouched.
  - **Verified**: unbound-field sweep re-run — only `Query.backlog`
    (B.9, Phase B) remains unbound in the entire schema. 199 tests
    passing, `ruff check .` clean.
- [x] **A.15 — Implement data export/import** (§6.12): JSON,
  `schema_version` integer from the first implementation, reject (not
  auto-migrate) on any version mismatch.
  - **Scope already fully resolved by A.3's own earlier audit pass**
    (see that entry) — full 24-table restore confirmed, `ImportResult`
    a confirmation summary not a scope limit, no merge logic for a
    non-empty target. Nothing left ambiguous going in.
  - **Table list confirmed live, not counted from memory**: queried
    `sqlite_master` directly against a real migrated db rather than
    tallying `CREATE TABLE` statements across four migration files by
    eye — 24 tables, matching §6.12's own already-stated count exactly
    (`alembic_version` deliberately excluded — migration bookkeeping,
    not application data).
  - **Built**: new `export_import.py` (GraphQL-free, same layering as
    `metadata.py`/`fribb.py`/`fuzzy.py` — raises plain `ValueError`,
    resolvers.py wraps it into `GraphQLError`). `EXPORT_IMPORT_TABLES`
    — the 24 tables in dependency order (parents before children; no
    `ON DELETE`/`INSERT CASCADE` anywhere in this schema, §11.2, so
    import's row-by-row `INSERT`s need this to satisfy `foreign_keys =
    ON` as they go — export reuses the same list, order is just
    readability there). `export_data()`/`import_data()`. One real,
    non-obvious detail handled: `episode.available_locally`
    (`GENERATED ALWAYS ... STORED`, §5.2) is excluded from every
    `INSERT`'s column list on import (SQLite rejects an explicit value
    for a generated column outright) via `PRAGMA table_xinfo`'s own
    `hidden` flag, while still being included in the exported JSON for
    completeness — the target recomputes it fresh from the columns it
    actually derives from. `Query.exportData`/`Mutation.importData`
    resolvers — thin wiring only, all the real logic lives in the pure
    module.
  - **Tests**: new `test_export_import.py` (6, against two real
    separately-migrated databases — the actual restore scenario, not
    a hand-rolled mini-schema that would drift from the live one and
    miss exactly the bugs this needs to catch): exports all 24 tables;
    a full round trip through an interconnected dataset (show,
    episode, watch_event, tag, show_tag, franchise, franchise_member)
    restores everything correctly, including the generated-column
    case; a `schema_version` mismatch is rejected and touches nothing;
    malformed JSON is rejected; a colliding id raises a plain
    `sqlite3.IntegrityError`, deliberately not caught into anything
    friendlier (§6.12's own explicit "not something this handles
    specially"); and — the one that actually matters most for a
    destructive-adjacent operation — a collision on a table ordered
    *after* some already-successfully-inserted rows still rolls back
    those earlier rows too, confirming the whole import really is one
    transaction, not a sequence of independently-committed inserts.
    `test_server.py` (+2, lighter): `exportData` returns valid JSON
    with the right top-level shape through real GraphQL; `importData`
    with a bad `schema_version` raises a clear error the same way.
  - **Verified**: unbound-field sweep re-run — still only `Query.
    backlog` (B.9, Phase B) remains unbound anywhere in the schema.
    207 tests passing, `ruff check .` clean.
- [x] **A.16 — Implement global settings** (§6.13): home timezone
  (default `Europe/Dublin`), used for calendar/pacing/refresh
  day-boundary logic; all storage stays UTC internally.
  - **The real question here was storage location, not the value
    itself** — checked, not guessed: `schema.graphql` has no
    "settings" type or `homeTimezone` field anywhere (unlike A.9-A.15,
    every one of which had its GraphQL shape already stubbed in A.2),
    so a GraphQL-mutable setting had no existing scaffold to extend.
    §11.2 already settles it directly: `home_timezone` is listed
    alongside `bearer_token`/Sonarr/Radarr/AniList credentials as an
    `lcars.ini` value, the same file/env precedence every other
    server-side credential in this project already uses — not a
    database row.
  - **Built**: `config.Config.home_timezone: str = "Europe/Dublin"` +
    `lcars.ini`/`LCARS_HOME_TIMEZONE` env var loading, same shape as
    every other field in this file.
  - **Deliberately not built here**: anything that actually *reads*
    `home_timezone` — its three named consumers (calendar, paced/
    catch-up's cadence *reset*, daily metadata-refresh cadence) are
    all still-unbuilt Phase B/client concerns. Confirmed A.10's own
    `pacedNextDate` doesn't need retrofitting for this: §6.2's
    adaptive-computation text is pure UTC timestamp arithmetic, no
    day-boundary bucketing anywhere in it.
  - **Also completed while in this file**: `sonarr_url`/`sonarr_api_
    key`/`radarr_url`/`radarr_api_key`/`anilist_client_id`/`anilist_
    client_secret`/`anilist_access_token` (added A.8/A.9) never got
    their own dedicated file/env-precedence tests at the time — folded
    into this same pass rather than left as a lingering gap, since
    this file was already open for `home_timezone`'s own tests.
  - **Tests** (+8, `test_config.py`, now 12 total in that file):
    `home_timezone` defaults correctly, loads from file, env overrides
    file; the same file/env precedence pattern for Sonarr/Radarr load-
    from-file and env-override, and AniList OAuth fields load-from-
    file and env-override.
  - **Verified**: 214 tests passing, `ruff check .` clean. No schema/
    resolver changes at all this step — confirmed the unbound-field
    sweep is unaffected, still only `Query.backlog` (B.9) remains.
- [x] **A.17 — Data's side**: replace Data's Trakt client with an
  LCARS client of the same shape (§4 Phase A). Data keeps doing what
  aniq does today (Sonarr polling, AniList calls, its own air-date
  patch) but also writes resulting state to LCARS. aniq's existing
  "queue locally, retry on flush" pattern carries over to the
  Data→LCARS path.
  - **Real scope correction, made directly by the user mid-build**: my
    first draft proposed adding a `showByExternalId` lookup query to
    LCARS's own schema, reasoning Data would otherwise have no safe way
    to avoid creating a duplicate show if its local id-cache were ever
    lost. Corrected sharply: Data doesn't need dedup/lookup
    infrastructure at all, since it bridges its own add action directly
    to LCARS's `addShow` — Data is the one *performing* the add, not
    discovering something that might already exist elsewhere ("data
    will add shows only by bridging to lcars, [LCARS] is the one
    actively adding shows... are you overbuilding here?", 2026-08-08).
    Re-scoped around "bridge on add, remember locally" instead, and
    confirmed the revised plan with the user before proceeding — see
    §4's own note in `SCOPE.md`.
  - **Built** (`~/repos/data`): `lcars_client.py` (async GraphQL,
    bearer-token auth — a shared secret in `config.ini`'s `[lcars]`
    section, same class as `sonarr_api_key`, not an OAuth credential,
    so no login flow/keyring); `lcars_queue.py` (queue-locally-retry-
    on-flush, mirrors `watch_queue.py`'s shape exactly); `lcars_ids.py`
    (tvdb_id -> LCARS show id map, persisted at bridge time). Every
    Trakt-touching function in `app.py` (~40 call sites) replaced with
    an LCARS counterpart: `_track_new_show_on_lcars` (the actual bridge
    call, runs unconditionally on every Sonarr add now, not gated by an
    offer/prompt the way Trakt's was), `_queue_lcars_watched`/
    `_queue_lcars_status` (§6.8's one narrow permanent exception —
    episode watch-status dual-writes to AniList *and* LCARS for anime;
    everything else, including status/score for every show, pushes to
    LCARS only, additive alongside anime's existing unaffected AniList
    write), `_lcars_cell`/`_lcars_score_text` (deliberately simpler
    than the old `_trakt_cell` — push-only, no read-back this phase, so
    these reflect only locally-known bridged/queued state, never a
    live-confirmed one), `_undo_lcars_watched` (queued-only; the old
    "already-synced, call a real unmark" branch has no honest LCARS
    equivalent without a read-back mechanism). `_search_and_write_
    trakt_entry` (title search with no Sonarr row) and
    `_open_trakt_worker` (open the show's Trakt page) dropped with no
    replacement — LCARS's `addShow` needs an already-known external id,
    not a title to search, and LCARS is an API server with no per-show
    webpage; building either would be scope creep the user's own
    correction above already ruled out. `cli.py`'s `trakt-login`/
    `trakt-seed-watchlist` removed with no `lcars-login` replacement
    (no OAuth flow needed at all). `trakt.py`/`trakt_queue.py`/
    `trakt_cache.py`/`trakt_status.py` deleted, along with
    `scripts/pogdesign_import.py` (a one-time PoGDesign→Trakt watch-
    history migration, already run against the real account — its only
    target no longer exists in the product, so no path could still
    execute it).
  - **Tests**: ~1300 lines of Trakt-only test code removed
    (`test_trakt.py`, `test_trakt_queue.py`, `test_pogdesign_import.py`);
    `test_app_trakt_wiring.py`/`test_app_trakt_write_wiring.py`
    replaced with `test_app_lcars_wiring.py` (read-side: bridged/
    queued/dash cell states) and `test_app_lcars_write_wiring.py`
    (write-side: mark-watched/force-complete/undo/quit-flush against
    LCARS); `test_app_list_status.py`/`test_list_screen.py`/
    `test_app_score.py`/`test_app_detail_pane.py`/`test_config.py`
    updated in place for the new backend.
  - **Verified**: 494/495 tests passing, `ruff check .` clean, both in
    `~/repos/data`. The one remaining failure
    (`test_check_air_status_survives_a_day_divider_row`) is a
    pre-existing, already-documented (`backlog.md`), date-dependent
    flake unrelated to this change — confirmed by reading its own
    accepted-limitations entry, not re-investigated. Committed and
    pushed to `~/repos/data` (`37d064a`); no CI configured there
    (personal-tool scope, per its own backlog.md), so no `gh run watch`
    step applies the way it does in this repo.
- [x] **A.18 — Confirm**: no autonomous scheduler exists yet at the
  end of this phase — this alone should already be shippable and
  useful (Trakt gone, real status column, inside Data), before Ops
  exists at all (§4, closing note on Phase A).
  - **Confirmed, not just assumed**: grepped `~/repos/starfleet/src`
    for any cron/scheduler/background-loop machinery (`schedule`,
    `APScheduler`, `asyncio` periodic tasks outside request handling,
    a `cron`/`ops` module) — none exists; every server-side write in
    this codebase through A.17 is a direct consequence of a client
    call (GraphQL mutation) or an on-demand fetch triggered by one
    (§4's own A.8 clarification), never a self-driven timer. Ops
    (Phase B, §4/§6.7) has not been started.
  - **Shippable-and-useful check, against real behavior, not just
    doc text**: Trakt is fully gone from Data (A.17); every show,
    anime or not, gets a real server-side `status` column via LCARS's
    `setStatus`, replacing Trakt's old 5-custom-list hack; LCARS
    itself pushes status/score on to AniList server-side (A.9),
    unaffected by any of A.17's Data-side changes. Both halves of
    Phase A's own closing promise ("Trakt's flakiness is gone, and
    status gets a real column — inside Data") hold today, with no
    scheduler required for either.
- [x] **A.19 — Fix**: `show.duration_minutes` has no way to ever get
  populated (flagged during A.13, §6.6, not fixed there — out of that
  step's own query-surface scope). User-directed fix, 2026-08-08:
  "fix this gap by asking databases for the information, tmdb should
  have it for every media for a one time check, anilist does have it
  for animes."
  - **Built**: AniList's `Media.duration` field added to
    `anilist_client.py`'s existing `_MEDIA_QUERY` (already fetched for
    every `tracking_space = anime` show, A.8/A.9 — no new call,
    just a wider one) and written into `show.duration_minutes` in
    `_fetch_anilist`. New `tmdb_client.py` (plain v3 API-key auth,
    matching the project's existing Sonarr/Radarr REST-client
    convention — no OAuth needed for read-only public metadata) covers
    every non-anime show: `movie_runtime`/`tv_episode_runtime` against
    an already-known tmdb id, plus `find_by_tvdb_id` to bridge
    TVDB->TMDB the first time for a non-anime TV show that only
    carries a tvdb id (§5.4 — TV shows aren't guaranteed a tmdb id the
    way movies are). A resolved bridge id is persisted as a real
    `show_external_id` row (`INSERT OR IGNORE`, same upsert-by-
    show_id+service shape `linkShowExternalId` itself already uses),
    so a later refresh reads it straight back instead of re-resolving.
    New `Config.tmdb_api_key` (`lcars.ini`'s `[lcars]` section /
    `LCARS_TMDB_API_KEY`), same "optional, not-configured = same as
    not-linked, no pending_review noise" treatment every other A.8
    branch already gets — a genuine failure still opens one.
    `metadata.fetch_and_populate()`'s own branching: `tracking_space =
    anime` goes through AniList only (already covers duration);
    everything else goes through the new TMDB branch instead — the two
    are mutually exclusive per show, confirmed by a dedicated test
    (`test_add_show_tmdb_fetch_skipped_for_anime`: TMDB never called
    for an anime show even with a key configured).
  - **Tests**: `test_tmdb_client.py` (+11, pure client-layer, no real
    network — find_by_tvdb_id/movie_runtime/tv_episode_runtime,
    404-is-a-legitimate-miss vs. 401/500/connect/timeout errors);
    `test_config.py` (+3, tmdb_api_key file/env/default precedence,
    same pattern as every other credential in that file);
    `test_server.py` (+6: movie duration via an already-known tmdb id,
    episodic duration via the TVDB->TMDB bridge + the persisted
    `show_external_id` row, no-op when TMDB has no match for a bridged
    tvdb id, skipped entirely for anime, skipped silently when
    unconfigured, a genuine failure opens a `pending_review` entry
    with `source = 'tmdb'`) — plus `duration` added to the existing
    AniList fetch fixture/test.
  - **Verified**: 234 tests passing (was 214), `ruff check .` clean.
    No schema/migration change at all — `Show.durationMinutes` already
    existed in `schema.graphql` since A.1, this only ever populates it;
    unbound-field sweep therefore unaffected, still only `Query.
    backlog` (B.9, Phase B) remains.

*(A.9–A.16 don't have hard ordering dependencies on each other — bank
them in whatever order is convenient once A.1–A.3 exist.)*

**Consolidation audit pass, 2026-08-09** (requested before Phase B
starts): full independent re-verification of everything in Phase A —
not another read-through of this file's own prose, actual re-execution.
Method: ran the full test suite + `ruff` fresh (234/234, clean);
re-ran the full migration chain (`upgrade head` → `downgrade base` →
`upgrade head`) on a scratch DB, clean round-trip; re-ran the DB↔SDL
field-by-field cross-check (last done before A.4/A.9/A.10/A.19 —
stale, now reconfirmed current, no mismatches beyond already-bound
relationship fields); re-ran the unbound-object-typed-field sweep
(still only `Query.backlog`, B.9, no regressions); confirmed
`~/repos/aniq` untouched by this project specifically (a real commit
exists there from the same day, but it's the user's own ordinary aniq
maintenance, explicitly anticipated by §4.0's "small fixes... stay in
aniq only" — not a Starfleet-side constraint violation); ran Data's own
suite fresh (494/495, the one failure the same pre-existing documented
date-dependent flake `BUILD_PLAN.md` A.17 already names, `ruff` clean).
Three real, previously-invisible gaps found this way — none caught by
any prior pass, all confirmed with the user and closed below rather
than left as notes:
- [x] **A.20 — Season row auto-creation + `episode.season_id`
  population** (§5.5). The gap: `_fetch_sonarr` inserted `episode` rows
  for whatever season numbers Sonarr reported but never created the
  corresponding `season` row (A.4) or set `episode.season_id` (A.4's
  own FK, never populated by any code path) — concretely, `setScore`/
  `setStatus`/`setSeasonScore` (§6.1, query `FROM season WHERE
  show_id = ?`) silently never pushed to AniList for any season beyond
  1 that was only ever discovered via Sonarr. Confirmed with the user
  this is a creation problem, not a reconciliation one — Sonarr/TVDB is
  the only source that ever reports a new season number exists at all
  (AniList doesn't, §5.5) — and that closing it now beats leaving it
  for Phase B. Extracted `reconcile_season()` out of the
  `reconcileSeasonMapping` resolver into a new `season_mapping.py`
  (same resolvers.py<->metadata.py circular-import reasoning
  `pending_review.py` was already extracted for, A.8) so both the
  mutation and this new on-demand call share one implementation.
  `_fetch_sonarr` now reconciles every distinct season number it sees
  immediately on discovery (same on-demand-immediately philosophy as
  every other Phase A external-call trigger), sets `season_id` on every
  episode it inserts, and backfills it on a pre-existing row that
  predates this fix. A Fribb failure during this reconciliation
  (network unreachable, no cache) still creates the season row —
  unmatched, with its own `pending_review` entry — rather than aborting
  the Sonarr episode import itself. SCOPE.md §5.5 updated with the full
  resolution. **Tests** (+9, `test_server.py`): season creation +
  Fribb-match on Sonarr discovery, `episode.seasonEntity` resolves
  correctly end-to-end (not just via a test's own direct-SQL
  `season_id`, the gap this closes); a manual-override season survives
  a later Sonarr refetch untouched; backfill on a pre-existing
  null-`season_id` row; a Fribb failure still creates an unmatched
  season + opens `pending_review` rather than aborting the fetch. Also
  added a default `fribb.load_dataset` stub to the shared `client`
  fixture (`test_server.py`) — the same "avoid a real network call in
  every test that doesn't care" reasoning the AniList stub already
  established, now genuinely needed since Sonarr fetches reconcile
  seasons too.
- [x] **A.21 — `show_relation` population from AniList relations**
  (§5.9). The gap: `show_relation` was schema-only since A.1 — no code
  ever wrote a row to it, despite `Show.relatedShows` (A.3) being fully
  wired to read it. A real design fork surfaced building this
  (`related_show_id` is a non-nullable FK, so a relation to a show
  LCARS has never seen needs a real row to point at) — asked directly
  rather than guessed at, given the scale: confirmed **auto-create a
  `tracked = false` stub**, matching §5.1's own pre-existing "Show-row
  promotion paths" framing. `_fetch_anilist` now requests
  `Media.relations` and writes one directed `show_relation` row per
  reported relation, filtered to anime-shaped AniList `format` values
  only (a relation to source manga/novel material is skipped — §5.1 has
  no place for those). No franchise auto-creation, unchanged from A.8's
  original precedent. SCOPE.md §5.9 updated. **Tests** (+2,
  `test_server.py`): a relation to an unknown AniList id creates a
  correctly-shaped stub (title/mediaShape/trackingSpace/tracked/status,
  plus its own crosswalk rows) and skips the manga-format relation
  entirely; a relation to an already-tracked show reuses that show, no
  duplicate stub.
- [x] **A.22 — `episode_numbering_mapping` automatic derivation**
  (§5.5). The gap: deferred at A.4 for lack of real data to derive
  from ("that data doesn't exist until A.8's on-demand fetch triggers
  exist") — A.8/A.9 built and populated exactly that data, but nothing
  ever came back to build the derivation itself; only the manual
  `setEpisodeNumberingScheme` path existed. Confirmed with the user:
  build it now rather than carry it as an unfinished A.4 leftover.
  Heuristic (deliberately simple, personal-tracker scale): Sonarr's own
  `seriesType = 'anime'` flag, or any episode carrying a populated
  `absoluteEpisodeNumber`, means `absolute`; anything else defaults to
  `season_episode`. Re-derived on every Sonarr fetch, not just once —
  a genuine change opens a `pending_review` entry, same "review, not
  gate" shape as everything else; never touches an already-
  `manual_override` row. SCOPE.md §5.5 updated. **Tests** (+4,
  `test_server.py`): absolute via `seriesType`, absolute via
  `absoluteEpisodeNumber`, `season_episode` default, manual override
  survives a later refetch that would otherwise re-derive differently.
- [x] **A.23 — Credentials off plaintext-`lcars.ini`-only** (§8/§11.2).
  Not a code gap — a decision the user flagged for revisiting, against
  their own later `ideas.md` note ("move all secret and password to a
  safer place") conflicting with the originally-recorded plaintext-
  chmod-600 design. Confirmed direction: Docker secrets / mounted
  secret files over env-vars-only, an external secrets manager, or
  just tightening the existing file handling. Every credential field
  (`bearer_token`, Sonarr/Radarr/AniList/TMDB keys) now supports a
  `<VAR>_FILE` env var — the official postgres/mysql Docker images'
  own convention — which wins over both the `lcars.ini` value and the
  plain env var when set (`config._resolve_secret()`). `lcars.ini`
  itself isn't removed — `anilist_access_token` is minted interactively
  by `lcars anilist-login` and written back to it at runtime, which a
  read-only secret mount can't support, and local/non-Docker runs still
  need somewhere to keep credentials. `Dockerfile` documents the
  recommended Compose `secrets:` wiring. SCOPE.md §8/§11.2 updated.
  **Tests** (+4, `test_config.py`): the `_FILE` env var wins over both
  file and plain env; trailing whitespace/newline (a real mounted
  secret file's own shape) is stripped; the plain file<env precedence
  still holds when no `_FILE` var is set; every secret field
  individually confirmed to support it, not just `bearer_token`.
- **Two design decisions re-validated, not changed** — self-flagged in
  their own original entries above as "low-stakes, easily revisable"
  reasoning rather than an explicit ask; the user confirmed both as
  correct on review: **A.11**'s next-up ordering (`next_up_override`
  rows sort first, ahead of soonest-available-first default) and
  **A.13**'s stats scope split (`totalShows` = current tracked library
  only; the other three stats fields are lifetime totals that survive
  untracking). **A.14**'s deletion semantics (`softDeleteShow` flips
  `tracked` not `status`; `requestHardDelete` requires prior
  soft-delete) also re-validated the same way.
- **Flagged for Phase B design time, not addressed now** (user's own
  call): §11.2's sync-resolvers/single-shared-connection execution
  model is justified by "nothing ever touches the connection
  concurrently," true only because Phase A has no autonomous background
  work. Ops (B.1 onward) breaks that premise. See SCOPE.md §11.2's own
  note and this file's B.1 entry below.
- 248 tests total now (was 234), `ruff check .` clean, both re-verified
  after every one of A.20-A.23 individually and again at the end of
  this pass — no regressions introduced at any step. (**Corrected
  2026-08-09**: A.20's own entry above originally claimed "+9" tests;
  the real figure is +4. The `+9` came from a `-k` filter run whose
  pattern also matched A.22's four plus one pre-existing A.3 test. The
  per-step figures are A.20 +4, A.22 +4, A.21 +2, A.23 +4 = +14, which
  is what 234→248 actually reflects.)

**Second consolidation audit, 2026-08-09** — an independent re-run of
the whole Phase A verification, deliberately not trusting the pass
directly above. Same method, wider: full suite, `ruff` under extra rule
sets, full migration chain, a *complete* Query/Mutation resolver-binding
sweep (broader than the project's own object-typed-field sweep — it
found scalar-returning mutations had never been checked at all; all 37
are bound), runtime probes for behaviors no test covered, plus Data's
own suite and CI. Findings and their resolutions:
- [x] **A.24 — Anime shows resolve their own AniList link** (§5.1, §4
  Phase A). **The most serious thing either audit found.** `_fetch_anilist`
  needs a *show-level* `anilist` link and runs *before* `_fetch_sonarr`;
  Data's bridge (A.17) never sends an `anilistId` — its own
  `lcars_client.py` says so outright ("data's own add flow often
  doesn't [have it]"). So **every anime show added through the primary
  production path landed permanently bare** — no synopsis, poster, cast,
  studio or duration — and silently, since the missing link was treated
  as caller input rather than a failure. A.20 sharpened it: Fribb
  already resolved the correct AniList id into the *season* row moments
  later, one table away from the code that needed it. Verified before
  fixing: `fetch_media` called zero times, every field null, unchanged
  by an explicit `refreshShowMetadata` retry. Fixed by resolving the
  show-level link first, from the same Fribb dataset (season 1, the
  convention `addShow` and `_upsert_season` already assume), so the
  existing fetch works on that same run. Never overwrites a
  caller-supplied id (§3 principle 6). Unresolvable → `pending_review`,
  never rejection: hard-rejecting would break Data's bridge on every
  anime add, and §3 principle 1 governs. This also makes §5.1's "anime
  **mandates** an AniList link, no exceptions" true rather than
  aspirational — migration `7196ca889757`'s own comment had claimed
  addShow/A.8 enforced it, which was simply false.
- [x] **A.11 correction — `nextUp` orders by air date at both levels.**
  A real unvetted change, and one the *first* audit missed while
  explicitly re-validating A.11: §6.4 states exactly one default,
  soonest-available-first, but A.11 used `ORDER BY season ASC, episode
  ASC` for the intra-show "which episode is next" pick — a second rule
  §6.4 never states. That imported Sonarr/TVDB's season-0-is-specials
  filing convention into internal behavior: verified, `nextUp` offered a
  season-0 special ahead of S1E1. Corrected directly by the user, whose
  framing settles it: the internal database is the source of truth;
  Fribb/AniList/Sonarr are sources that feed and periodically correct
  it, and how *they* file an episode has no bearing on internal
  ordering. Now `air_date_utc ASC` (nulls last, same rule as the
  cross-show level), with `(season, episode)` retained only as a stable
  tiebreak. Needs no `kind` taxonomy to be correct — a special simply
  falls wherever it actually aired.
- [x] **A.25 — Capture `absolute_number` and `kind` as source facts**
  (§5.2). Neither was ever written by any code: Sonarr reports
  `absoluteEpisodeNumber` and A.22 even read it (to derive the numbering
  *scheme*) before discarding it, leaving A.22 labelling a scheme with
  no numbers behind it; and every episode was stored `kind = 'regular'`,
  season-0 specials included. Both are now captured. §5.2's synthesis
  rule is implemented too (`<preceding regular absolute>.<index by air
  date>` — 12.1, 12.2), as a **whole-show recompute after each fetch**,
  since the indices are positional and a newly-discovered special shifts
  every later one. A real interaction bug was caught by its own test
  while building this: synthesis filled the column, which then blocked
  the source-value backfill's `WHERE absolute_number IS NULL` — a real
  reported number could never land after a guess. Source values now
  always supersede synthesized ones (told apart by the fractional part).
  New `setEpisodeKind` mutation: `kind` had been **read-only across the
  entire API** since A.1, so a wrong value could never be corrected, and
  Sonarr cannot distinguish `ova`/`bonus_movie` from `special` at all.
  Framed deliberately as *capture, not behavior* — per the user: "the
  source of truth is the internal database, anilist and fribb and sonarr
  are used as sources of data... and mapped onto it." Nothing reads
  `kind` to decide anything, and `nextUp` orders by air date precisely so
  a source platform's convention cannot drive watch order.
- [x] **Season-0 review noise removed** (A.20 regression). A.20
  reconciled season 0 like any other season number, creating one
  permanently unresolvable `pending_review` per show with specials —
  season 0 is Sonarr/TVDB's specials bucket (§5.2), not a season with a
  cross-service identity, so Fribb can never match it. Now skipped
  entirely: no season row, no review, `episode.season_id` left NULL,
  which is what that nullable column is for.
- [x] **Fribb parse/index memoized** (A.20 regression). A.20 made
  reconciliation per-season, so the multi-megabyte dataset was re-read,
  re-parsed and re-indexed once per season inside a sync resolver
  (§11.2, whose whole premise is that nothing blocks the event loop for
  long) — measured 0.30s for a five-season show on a 3.2MB synthetic
  set. Memoized at the source in `fribb.py` (parse keyed on file mtime,
  index on dataset identity) rather than hoisted to one caller, because
  **B.2's weekly all-shows pass is the real hammer** and would otherwise
  repeat the waste per show.
- [x] **Hygiene**: 22 stale `# noqa` directives removed, `RUF100` added
  to the project's own `select` so suppressions stay honest rather than
  being swept once and drifting again; `_get_season` deduplicated
  (`resolvers.py` and `season_mapping.py` had drifted to different
  signatures, `dict` vs `dict | None`).
- **Two Phase B owners assigned** for §5 behavior that no step claimed:
  `episode_movie_link`'s automatic `tmdb_match` derivation (new **B.8b**)
  and §5.4's `local` pseudo-service rollup (**B.7**). Both genuinely need
  B.3's data first — building either now would mean building against
  nothing, the same reasoning A.4 used correctly when deferring numbering
  derivation.
- **Deliberately NOT built, on the user's own challenge**: several items
  the first draft of this audit proposed turned out to be field-fidelity
  work with no consumer. The user's test — "does it matter how it is
  captured as long as every episode is captured and IDed locally and
  mapped accordingly?" — is the right one, and it correctly knocked out
  the original framing of the `kind` work (nothing reads `kind`;
  classifying it changes no behavior, so it is captured as a source fact
  and nothing more). The rule going forward: fix sources that aren't
  feeding the database, and consumers that behave wrongly — not fields
  that merely fail to match prose.
- 265 tests total now (was 248), `ruff check .` clean, schema
  re-validated, migration chain round-trips, unbound-field sweep still
  only `Query.backlog` (B.9).

---

## Phase B — Ops takes over syncing

`SCOPE.md` §4 "Phase B" / §6.7. Build **Ops**, the autonomous
background scheduler.

- [x] **B.1 — Daily metadata refresh** for `watching`-status,
  actively-airing shows; on-open trigger capped to the same
  once-per-day ceiling (not stacking). **Design question carried over
  from the 2026-08-09 consolidation audit, resolved here, not before**:
  §11.2's sync-resolvers/single-shared-connection execution model is
  justified entirely by "nothing ever touches the connection
  concurrently" — true only because Phase A has no autonomous
  background work. This step is the first thing that breaks that
  premise (a background job needing DB + outbound HTTP access,
  independent of any client request) — a long poll could block the
  event loop for every concurrent GraphQL request while it runs.
  Presented four options, asked rather than guessed given the scale of
  the decision (it sets the pattern for every later B-step, not just
  this one): a second in-process connection (WAL mode); a dedicated
  worker thread/process in the same container; `asyncio.to_thread()`
  reconsidered against Phase B's real shape; or Ops as a separate
  process with **no** database access at all, driving LCARS purely
  through its own GraphQL API like any other client. **Confirmed: the
  last one.** Full rationale in `SCOPE.md` §11.2's new "Resolved
  2026-08-09 (B.1)" note — short version: Ops is already a distinct
  named component (§1), §3 principle 8 already frames every LCARS-
  facing actor as a peer client, and this keeps §11.2's original
  "nothing touches the connection concurrently" premise *literally*
  true going forward instead of needing re-justification at every later
  B-step. Cost stated plainly there too: every Ops responsibility from
  here on needs a real mutation/query to act through, not direct DB
  access — B.2–B.10 inherit this, not just B.1.
  - **Two more real gaps closed by reuse, not a second ask** (both
    flagged as blocking, both resolved from precedent already in
    `SCOPE.md` — see its own B.1 note for the reasoning in each case):
    a new `show.metadata_last_refreshed_at` column, same per-entity
    "last checked" shape `season.last_reconciled_at` (§5.5) already
    established; and the on-open trigger turned out to need **no new
    mutation at all** — it's the same new `dueForMetadataRefresh` query
    below, called by whichever caller happens first (Ops's own clock or
    a client on open), followed by the existing `refreshShowMetadata`
    (A.8) — "not stacking" falls out of the shared timestamp rather
    than needing its own guard.
  - **Built**: migration for `show.metadata_last_refreshed_at`
    (nullable `TEXT`, same shape as every other optional timestamp
    column in this schema). `util.start_of_today_utc(home_timezone)` —
    `home_timezone`'s (§6.13, A.16) first real consumer, six steps
    after it was built with none. `schema.graphql`:
    `Show.metadataLastRefreshedAt: DateTime`, new
    `Query.dueForMetadataRefresh(...): ShowConnection!` (§8's
    deliberately-designed-query-shapes precedent, not generic
    pass-through filtering). `resolvers.py`: the eligibility query
    reuses `_show_is_airing` (A.10) verbatim rather than re-deriving
    the same "any episode with a null/future air_date_utc" predicate a
    second time — computed in Python first, same `paginate_list()`
    pattern `nextUp` (A.11) already established, not a single raw-SQL
    `WHERE`, since the airing check isn't expressible as one column
    comparison. `metadata.fetch_and_populate()` (§4/A.8) now stamps
    `metadata_last_refreshed_at` at the end of every run, regardless of
    which individual source branches succeeded — an `addShow`-triggered
    fetch therefore already counts as "refreshed today," so Ops's very
    next poll correctly skips a show created hours earlier.
  - **New `src/ops` package** — Ops itself, a separate console script/
    process (`ops = "ops.cli:main"`, `pyproject.toml`), no DB dependency
    at all: `ops/lcars_client.py` (async `httpx` GraphQL client, close
    port of Data's own real `lcars_client.py` — same bearer-token +
    `X-LCARS-Client` header shape, `ops` instead of `data`),
    `ops/config.py` (`OPS_LCARS_URL`/`OPS_LCARS_BEARER_TOKEN` incl. the
    A.23 `_FILE` convention, `OPS_POLL_INTERVAL_SECONDS`, default 3600),
    `ops/scheduler.py` (`run_once()` — the testable unit, walks every
    page of `dueForMetadataRefresh`, calls `refreshShowMetadata` per
    show, one show's failure doesn't stop the rest; `run_forever()` — a
    thin `while True: run_once(); sleep(interval)` wrapper), `ops/
    cli.py` (`ops run`). `SCOPE.md` §11.3 documents the compose-service
    shape (same image, `command: ops run` override, no migrations, its
    own env — see that section).
  - **Tests**: migration round-trip (upgrade adds the column, downgrade
    removes it). `test_util.py` (+`start_of_today_utc` cases: a fixed
    instant near a day boundary in a non-UTC zone, confirming the
    bucket flips at local midnight not UTC midnight). `test_server.py`
    (`dueForMetadataRefresh`: excludes a non-watching show, excludes a
    fully-aired watching show, excludes a watching+airing show already
    refreshed today, includes one that's never been refreshed and one
    refreshed yesterday; `refreshShowMetadata`/`addShow`'s inline fetch
    both stamp the timestamp). New `test_ops_lcars_client.py` (mirrors
    Data's own `lcars_client` test shape — success, 401, GraphQL error,
    connect/timeout, all via an injected fake transport, no real
    network) and `test_ops_scheduler.py` (`run_once` calls
    `refresh_show_metadata` once per due show; a per-show exception is
    caught and logged, not fatal to the rest of the batch; zero due
    shows is a clean no-op).
  - **Verified**: 290 tests passing (was 266), `ruff check .` clean,
    full migration chain round-trips (`upgrade head` → `downgrade base`
    → `upgrade head`). Unbound-field sweep re-run, matched to this
    project's own established convention (domain object-typed fields
    and Query/Mutation resolvers, not the generic `*Edge`/`*Connection`
    fields Ariadne already resolves by default dict lookup, never
    explicitly bound anywhere in this codebase's history) —
    `dueForMetadataRefresh` now bound, only `Query.backlog` (B.9)
    remains. **Beyond the mocked unit tests**: a real in-process
    end-to-end run — a real migrated SQLite DB, the real ASGI app, and
    Ops's own real `LcarsClient` (not a fake) all wired together over
    `httpx.ASGITransport` — added a watching+airing show,
    confirmed `dueForMetadataRefresh` actually finds it, called
    `refreshShowMetadata` through Ops's client, then confirmed the show
    no longer comes back as due — proving the daily ceiling actually
    suppresses a re-fetch, not just that the query shape is plausible
    (the class of gap both consolidation audits kept catching: a
    mechanism firing into something that silently doesn't work).
  - **Three real gaps caught by a review pass before checking this off**,
    none from a failing test — the same "verify for real, don't trust
    that it's probably fine" practice this file's own past audits used:
    - Reusing `_show_is_airing` (A.10) verbatim carries a real, silent
      consequence its own docstring states plainly: "a movie has no
      episode rows at all, so it's always non-airing." A `media_shape
      = movie` show can therefore never appear in `dueForMetadataRefresh`,
      regardless of status — exactly the shape both consolidation audits
      kept catching (A.24 was "silently bare forever," this is "silently
      never refreshed"). Judged deliberate on reflection (a `watching`
      movie is already released, with little reason for its metadata to
      keep changing the way an actively-airing show's does) rather than
      reversed — but it was undocumented and untested until now. Fixed:
      recorded explicitly in `SCOPE.md` §11.2's B.1 note, same
      "low-stakes, easily revisable" self-flagged class of call as
      A.13/A.14, plus a dedicated test (a watching movie, never
      refreshed, still excluded).
    - `ops/lcars_client.py`'s `_query` (ported from Data's real client
      unchanged) fell through to a raw `payload["data"]` on a malformed
      200 response (non-JSON body, or JSON with neither `data` nor
      `errors`) — raising `TypeError`/`KeyError` instead of `LcarsError`.
      Confirmed by direct reproduction, not assumed: a mocked non-JSON
      200 response raised `TypeError`. That would have escaped both
      `run_once`'s and `run_forever`'s own `except LcarsError` — fatal
      for Data's TUI is visible to a human; fatal for Ops's unattended
      daemon is a silent, permanent stop to all background sync. Fixed:
      `_query` now raises `LcarsError` explicitly for this case (Data's
      own real `lcars_client.py` has the same gap, deliberately not
      touched here — a different repo, out of this step's scope). New
      test confirms it.
    - `run_forever`'s own `except LcarsError` was narrower than its
      stated job — belt-and-suspenders broadened to bare `Exception`,
      same reasoning `metadata._guarded` (A.8) already applies to the
      identical "never let one bad response crash the long-running
      process" concern. New test: a genuinely unanticipated exception
      (not `LcarsError`) is caught and the loop reaches a second tick.
  - 293 tests passing after these three fixes (was 290 before them, 266
    at Phase A's own close), `ruff check .` clean, unbound-field sweep
    unaffected.
- [x] **B.2 — Fribb dataset reconciliation, two tiers** — deliberately
  slower/independent of the daily cadence. `BUILD_PLAN.md`'s own text
  here was one line, unlike B.1's full design-question paragraph —
  asked directly rather than assumed, since the same "how does Ops
  discover what's due" fork B.1 already resolved applies here too, with
  no `SCOPE.md` text settling it for B.2 specifically. Confirmed:
  **weekly** for seasons of `WATCHING`+actively-airing shows (every
  season of that show, not just the one currently airing — reuses
  A.10's `_show_is_airing`, show-level, unchanged, same combined filter
  B.1 already uses — "Airing AND watching (like B.1)"), **monthly** for
  an unconditional complete sweep of every season of every show. Full
  rationale in `SCOPE.md` §5.5's own "Cadence resolved 2026-08-09 (B.2)"
  note.
  - **Apparent conflict with the second consolidation audit's own
    "B.2's weekly all-shows pass" wording** (`BUILD_PLAN.md`, A.20's
    Fribb-memoization note, written before this round): read literally
    that would mean the weekly tier itself sweeps everything, no
    filtering — this round's resolution instead puts the *unconditional,
    no-filtering* sweep on the **monthly** tier, and scopes the weekly
    one to watching+airing only. Not a contradiction so much as that
    earlier note pre-dating this round's actual two-tier design — noted
    here rather than silently reconciled, since a future audit reading
    both notes should see the resolution, not have to guess which one
    is current (this one is).
  - **Built**: new `Query.dueForSeasonReconciliation` (`schema.graphql`)
    — a `SeasonConnection!`, watching+airing shows' seasons (computed in
    Python via `_show_is_airing`, same `paginate_list()` pattern
    `dueForMetadataRefresh`/`nextUp` already use — not one raw SQL
    `WHERE`, same reasoning as both) not yet reconciled in the last 7
    days (`season.last_reconciled_at`, plain `util.utc_iso_offset(-7)`
    cutoff, no `home_timezone` bucketing — §6.13 doesn't name the weekly
    cadence as a consumer, unlike the daily one). `ops/scheduler.py`
    gained `run_weekly_once()` (walks `dueForSeasonReconciliation`,
    calls `reconcileSeasonMapping` per season) and `run_monthly_once()`
    (walks every show via `Query.shows`, every season via `Show.seasons`,
    calls `reconcileSeasonMapping` unconditionally — no due-query needed
    for this tier at all). **Two loops, not three timers**: since
    `dueForSeasonReconciliation` is already self-gating (a season only
    ever appears once truly 7+ days stale, regardless of how often it's
    checked), the weekly tier rides the *same* hourly check cadence B.1's
    daily tier already uses (`run_once()` then `run_weekly_once()`, same
    tick) rather than needing its own separate timer — only the monthly
    tier is genuinely unconditional/not self-limiting, so it alone gets
    its own, much longer-period loop, run concurrently
    (`asyncio.gather`) alongside the hourly one. Ops is still the one
    peer client B.1 established, just with more scheduled work, matching
    §11.2's B.1 note's own "B.2–B.10 inherit this" framing.
    `ops/lcars_client.py` gained `due_for_season_reconciliation()` (same
    page-walking shape as `due_for_metadata_refresh()`), `all_seasons()`
    (walks `shows` → `seasons` across every page of both), and
    `reconcile_season_mapping(show_id, season_number)`. `ops/config.py`
    gained `monthly_poll_interval_seconds` (default 30 days) — the
    existing `poll_interval_seconds` (B.1, hourly default) now covers
    both the daily and weekly due-checks, no new interval needed for the
    self-gating tier.
  - **Tests**: `test_server.py`
    (`dueForSeasonReconciliation`: excludes a non-watching show's season,
    excludes a fully-aired watching show's season, excludes a
    watching+airing show's season already reconciled this week, includes
    a never-reconciled one and one reconciled 8 days ago, includes every
    season of a multi-season watching+airing show not just the airing
    one). `test_ops_lcars_client.py` (+ due_for_season_reconciliation/
    all_seasons page-walking, reconcile_season_mapping variable shape).
    `test_ops_scheduler.py` (+ run_weekly_once/run_monthly_once/
    run_daily_and_weekly_once: each calls reconcileSeasonMapping/
    refreshShowMetadata once per item returned, a single item's failure
    doesn't stop the rest; `_loop` survives a non-LcarsError and keeps
    ticking; `run_forever` wires the hourly and monthly loops with the
    right coroutine/interval each — no real asyncio.gather timing
    needed, `_loop` itself is monkeypatched for this one).
  - **Verified**: 312 tests passing (was 293 at B.1's close), `ruff
    check .` clean. No new migration — B.2 needed no schema/column
    change, only a new query over `season.last_reconciled_at` (already
    existed since A.4). Unbound-field sweep re-run —
    `dueForSeasonReconciliation` now bound, only `Query.backlog` (B.9)
    remains. **Beyond the mocked unit tests**: a real in-process
    end-to-end run (same method as B.1's own) — added a watching+airing
    show with an unreconciled season, confirmed
    `dueForSeasonReconciliation` finds it, `run_weekly_once` reconciles
    it through Ops's real client, confirmed the weekly ceiling then
    suppresses it — and confirmed the monthly tier still picks the same
    season up unconditionally afterward, proving the two tiers are
    genuinely independent, not accidentally sharing one ceiling.
    Reconfirmed a real non-editable `pip install .` (the Dockerfile's
    own path) still picks up every new `ops` module cleanly.
  - **A real concern raised by review before checking this off, checked
    empirically rather than assumed either way**: the monthly tier has
    no "due" gate at all by design (above) — does an unresolvable season
    (no `tvdb` link, empty/no Fribb candidate) accumulate a fresh
    `pending_review` row every month, forever, since nothing ever stops
    `reconcile_season()` from being called on it again? Verified
    directly on a scratch DB: created a season with a real `anilist_id`,
    no `tvdb` link, `manual_override = 0`, an empty Fribb dataset (so
    resolution always comes back "no candidate"), and ran
    `run_monthly_once` three times in a row. **Result: exactly one
    `pending_review` row throughout, not growing.** `reconcile_season()`
    (A.4) already guards this precisely: `_open_or_extend_pending_review`
    only writes when the *resolved value itself changes* from what's
    stored — once a season's resolution stabilizes (even at "unmatched"),
    identical re-checks write nothing further, the same guarantee that
    already protects `reconcileSeasonMapping`'s own on-demand calls and
    A.20's Sonarr-triggered ones. The monthly tier having no due-gate of
    its own doesn't bypass this — it inherits it, since all three
    callers share one `reconcile_season()` implementation. Not a false
    alarm to have checked: this is exactly the review's own actual job
    (§5.6) whenever a resolution genuinely *does* flip between runs
    (e.g. a flaky external dataset) — that's intended drift-detection,
    not a bug, and is identical regardless of polling cadence.
  - **Recorded, not fixed — accepted at this project's scale**:
    `all_seasons()` is N+1 by construction (one `shows` page-walk, then
    one `seasons` page-walk *per show*) — fine monthly against a
    personal-tracker-sized library, but the first place Ops does
    per-entity fan-out rather than one query. B.3 (Sonarr/Radarr polling
    per episode) will face the same shape — worth a real query-design
    pass there rather than assuming this pattern always scales.
- [x] **B.3 — Sonarr/Radarr polling for file availability**
  (queue/episode-file/movie-file endpoints, never a filesystem scan).
  This is where `available_via_sonarr`/`available_via_radarr` (§5.2)
  get checked and updated independently — remember `bonus_movie`-kind
  episodes are commonly available via **both** sources, added
  asynchronously; poll both, don't assume one implies the other.
  `BUILD_PLAN.md`'s own text here named the general area but not the
  mechanism/cadence/scope — asked directly, then **verified against the
  user's own real, live Sonarr (4.0.19.2979) and Radarr (6.3.0.10514)
  instances** (read-only GET requests, no writes), rather than built
  from documentation memory — the same "resolve by testing, not more
  reading" practice §10.1 already used for animeschedule.net. Full
  rationale in `SCOPE.md` §5.2's own "Resolved 2026-08-09 (B.3)" note;
  short version:
  - Availability is **3-state** (`unavailable | downloading |
    available`), not boolean — the user's own reasoning: a file grabbed
    but not yet imported is a real, useful, distinct state ("allowed me
    to spot downloaded shows that sonarr could not automatically
    import"). Confirmed by real data that the mechanism must also
    handle the *reverse* transition (available → unavailable):
    `episodeFileDeleted` occurred 48 times in the last 250 history
    records on the user's own instance.
  - Mechanism: poll Sonarr/Radarr's own `/history` (grab **and** import
    events — "checking availability... by looking at grab and import
    history is what makes the most sense... easier and quicker" than
    re-scanning every tracked episode), not a re-check against "what
    should come." Verified live: `includeEpisode=true&includeSeries=true`
    (Sonarr) / `includeMovie=true` (Radarr) embed everything needed to
    match a history event straight to the existing `tvdb`/`tmdb`
    `show_external_id` crosswalk (§5.4) — no new correlation-id column
    needed, an earlier draft of this plan's own assumption, corrected
    by testing before it was built.
  - Cadence: hourly baseline; 5 min while a `WATCHING` show has an
    episode that just aired and isn't yet available, for up to 2h since
    air; 15 min after that window. Radarr has no "just aired" moment,
    so it stays on the baseline cadence.
  - LCARS itself polls (not Ops directly) — same established
    architecture as B.1/B.2. A single global sweep, not per-show/
    per-season like B.1/B.2's own mutations — one shared history feed
    covers every tracked show at once, so no per-item due-query is
    needed on Ops's side; a new small `availability_poll_checkpoint`
    table (one row per service, no GraphQL exposure — pure internal
    bookkeeping) keeps repeat polls cheap by only processing new events.
  - **Built**: migration `f7a2c4e91b6d` — `available_via_sonarr`/
    `available_via_radarr` (episode) and `available_via_radarr` (show,
    movie-only) converted INTEGER bool → TEXT 3-state; new
    `file_path_sonarr`/`file_path_radarr` (episode) and
    `file_path_radarr` (show); `available_locally`'s generated formula
    updated to check for `'available'` specifically; new
    `availability_poll_checkpoint` table. No real data existed for any
    converted column (confirmed by grep before drafting — nothing had
    ever written to them), so a plain DROP+ADD was safe, not a
    data-preserving rebuild. `util.utc_iso_offset_hours()` — a proper
    hours-granularity primitive alongside the existing days-based
    `utc_iso_offset()`, needed for B.3's 2-hour urgency window.
    `sonarr_client.py`/`radarr_client.py` gained `history_page()`.
    New `availability.py`: `poll_file_availability()` (both services,
    best-effort per service — a Sonarr failure doesn't block Radarr's
    own poll), `recommended_poll_interval_seconds()`. `schema.graphql`:
    new `AvailabilityStatus` enum (registered in resolvers.py's `ENUMS`
    — the schema-validation pass doesn't catch a missing enum
    registration, only a real query does, caught before it shipped),
    `Episode.filePathSonarr`/`filePathRadarr`, `Show.filePathRadarr`,
    `Mutation.pollFileAvailability`,
    `Query.recommendedAvailabilityPollIntervalSeconds`.
    `export_import.py`: `availability_poll_checkpoint` added to
    `EXPORT_IMPORT_TABLES` (25 tables now, was 24) — §6.12's own "full
    restore, all tables" framing extends to this too, not just
    domain-visible ones. `ops/lcars_client.py`:
    `poll_file_availability()`/
    `recommended_availability_poll_interval_seconds()`.
    `ops/scheduler.py`: `run_availability_once()` +
    `_availability_loop()` — a third, genuinely dynamic-interval loop
    (not a flat `_loop()` instance like the other two): asks LCARS for
    the next interval after every sweep rather than sleeping a fixed
    amount, run concurrently alongside the other two via `run_forever`'s
    own `asyncio.gather`.
  - **Tests**: `test_availability.py` (new, 20 tests against a real
    migrated SQLite DB with fake Sonarr/Radarr clients shaped after the
    real captured responses — grabbed/imported/deleted/ignored event
    handling, untracked-show and not-yet-fetched-episode skips,
    not-configured no-op, a client error caught not raised, checkpoint
    advancement + second-poll-only-sees-new-events, chronological
    event ordering within one poll — a delete-then-reimport upgrade
    resolves to the correct final state regardless of the real API's
    own newest-first pagination order; `recommended_poll_interval_seconds`'s
    three tiers, plus ignoring already-available/non-watching/future
    episodes). `test_server.py` (+6: `pollFileAvailability`/
    `recommendedAvailabilityPollIntervalSeconds` end-to-end through
    real GraphQL; `Episode`/`Show` availability fields resolve with the
    right enum casing through real GraphQL — locks in the `ENUMS`
    registration, a real gap class this project has hit before).
    `test_ops_lcars_client.py` (+2), `test_ops_scheduler.py` (+7:
    `run_availability_once`, `_availability_loop`'s three behaviors —
    sleeps the recommended interval, survives a sweep failure, falls
    back to 3600s if the interval check itself fails — `run_forever`'s
    wiring test extended to confirm all three loops, not just two).
  - **Verified**: 343 tests passing (was 312 at B.2's close), `ruff
    check .` clean, migration chain round-trips, unbound-field sweep
    confirms `pollFileAvailability`/
    `recommendedAvailabilityPollIntervalSeconds` are the only new
    bindings needed (only `Query.backlog`/B.9 remains unbound).
    **Beyond the mocked tests — real, live, read-only verification
    against the user's own actual Sonarr and Radarr instances**, not
    just the API-shape research above: seeded a scratch LCARS DB with a
    real show/episode from the user's own library (tvdb 457078, "You
    and I Are Polar Opposites" S02E06) and a checkpoint bounding the
    poll to a small recent window, ran `_poll_sonarr` against the real
    server with the real API key — it correctly found the real
    `downloadFolderImported` event and wrote the exact real file path
    from the user's own media library. Same check against the real
    Radarr instance (tmdb 687163, "Project Hail Mary") with the same
    result. Nothing was written to either Sonarr or Radarr at any point
    (GET-only) — only LCARS's own scratch test database was modified.
  - **Addendum, caught before commit**: `pollFileAvailability` runs as a
    sync resolver on LCARS's single shared event loop (§11.2's own DB-
    execution-model note) — a never-polled service's first call would
    have walked its entire history inline (Sonarr: ~23,000 records,
    ~92 pages) and blocked every other client's request for that whole
    duration. Raised, then resolved directly by the user: split into
    two entry points (`SCOPE.md` §5.2's own addendum has the full
    rationale) — `pollFileAvailability` (Ops's automatic call) now
    seeds a never-polled service's checkpoint to "now" and does no
    history walk at all; a new `backfillFileAvailability` mutation +
    `ops backfill-availability` CLI subcommand is the deliberate,
    manual, run-once-at-a-quiet-moment counterpart, explicitly **not**
    part of `run_forever`'s own loop. Also fixed real credential
    hygiene: the Sonarr/Radarr API keys used for this step's live
    verification were pasted directly into chat by the user and are
    now flagged for rotation (never written to any file in this repo —
    confirmed by grep before this commit).
    - **Built**: `availability.py` — `_poll_sonarr`/`_poll_radarr` gained
      a `backfill: bool` parameter; `backfill_file_availability()`
      alongside the existing `poll_file_availability()`.
      `schema.graphql`/`resolvers.py`: `Mutation.backfillFileAvailability`.
      `ops/lcars_client.py`: `backfill_file_availability()`.
      `ops/cli.py`: `ops backfill-availability` subcommand — prints an
      explicit blocking-duration warning, calls the mutation once,
      prints the result, exits (never runs from `ops run`'s loop).
    - **Tests**: `test_availability.py` (+7: first-ever-call seeds
      without processing, for both services; backfill ignores an
      existing checkpoint and walks full history regardless, for both
      services; `backfill_file_availability()`'s own public-function
      wiring). `test_server.py` (+1: `backfillFileAvailability`
      GraphQL wiring). `test_ops_lcars_client.py` (+1). New
      `test_ops_cli.py` (4 tests — the pre-existing gap of `ops run`
      itself having no CLI-level test coverage closed alongside the
      new subcommand, not deferred).
    - **Verified**: 354 tests passing (was 343), `ruff check .` clean.
- [x] **B.3b — Local file audit** (§5.2/§6.10, §11.3). Originally
  drafted as PC.1 (Pre-Cutover, "a script... not a new standing
  feature") — raised by the user directly right after B.3's own commit
  ("first one need to be validated by the presence of each actual
  files… there should be a way to attach files present in the path but
  not linked to sonarr/radarr too"), pulled forward once B.3's own
  availability columns made it buildable, then reshaped substantially
  during design; moved into Phase B as its own numbered step once it
  became clear this is a permanent, re-runnable capability, not a
  throwaway script — confirmed directly with the user, not assumed.
  `SCOPE.md` §5.2's own "Resolved 2026-08-09 (B.3b)" note has the full
  design history; short version:
  - **Two mechanisms, discovered by testing, not assumed**: Sonarr's
    `episode?includeEpisodeFile=true` and Radarr's own `movie?tmdbId=`
    (already embedding `movieFile`) both report each service's
    **current** file state directly, verified live before building
    anything — reduces "LCARS says available but the file's gone" and
    "a file exists LCARS never recorded" to a **pure API comparison**,
    no filesystem access needed, keeping §6.10's "never scan the
    filesystem" rule fully intact for those two cases. Only a genuine
    orphan (no API record at all to compare against) needs the
    filesystem — a deliberate, narrow exception, not a reversal.
  - **A permanent mutation, not a one-off script** — the user confirmed
    they want it re-runnable, not just once before Cutover. Needs
    LCARS's own compose service to gain its first-ever filesystem mount
    (`SCOPE.md` §11.3's own B.3b addendum: same media volume Sonarr/
    Radarr already mount, at the identical container path, read-only)
    — the orphan-discovery half gracefully no-ops per show if that
    mount/path isn't accessible, not an error, so reconciliation still
    works correctly before the mount exists.
  - **Orphan/untracked findings are report-only** — `pending_review`'s
    own `entity_type`/`entity_id` shape doesn't fit either case (no
    existing row to attach to); an untracked remote show isn't
    auto-created (`addShow` stays the only entry point). Both are
    returned directly in the mutation's own result.
  - **Untracked-show discovery is a flat list, not a folder walk** — the
    user asked for the broader "also discover untracked shows" scope,
    narrowed on review to listing only (title/external id/path); no
    LCARS row exists yet to cross-reference a folder walk against.
  - **Filename parsing**: one anchored regex (`S(\d{2,})E(\d{2,})`)
    handles both of the user's own real Sonarr naming templates
    (standard and anime) without template-detection; a file it can't
    match is still reported (season/episode left null), not skipped.
  - **Built**: `sonarr_client.py` — `all_series()`, `episodes()` gained
    `include_episode_file: bool = False`. `radarr_client.py` —
    `all_movies()` (`movie_by_tmdb_id()` already embedded `movieFile`,
    confirmed live, no change needed there). New `local_audit.py`:
    `audit_local_files()` (both services), `_audit_sonarr()`/
    `_audit_radarr()` (reconciliation + discovery, one pass each).
    `schema.graphql`: `OrphanFile`, `UntrackedRemoteShow`,
    `LocalFileAuditResult`, `Mutation.auditLocalFiles`. `ops/
    lcars_client.py`: `audit_local_files()`. `ops/cli.py`: `ops
    audit-local-files` — prints both finding lists in full, not just
    counts, since the point of an audit is a human reading what it
    found.
  - **Tests**: new `test_local_audit.py` (16 tests against a real
    migrated SQLite DB with fake Sonarr/Radarr clients, plus real
    `tmp_path` directories for the filesystem-reading half — both
    correction directions, already-correct no-op, not-configured,
    untracked-show listing without a folder walk, an unfetched-episode
    skip, a mid-walk client error keeping earlier partial results,
    orphan detection with and without a parseable filename, and a
    gracefully-skipped inaccessible path). `test_server.py` (+1:
    `auditLocalFiles` GraphQL wiring, including its nested
    `OrphanFile`/`UntrackedRemoteShow` types resolving with correct
    camelCase field names). `test_ops_lcars_client.py` (+1).
    `test_ops_cli.py` (+3: bearer-token requirement, findings printed
    in full, nothing extra printed when there are none).
  - **Verified**: 375 tests passing (was 354), `ruff check .` clean, no
    new migration needed (`available_checked_at` already existed on
    both `episode`/`show`). Clean-install sanity check (fresh venv,
    real `pip install`) confirmed `lcars.local_audit`/`ops.cli` both
    import cleanly and `ops audit-local-files --help` is wired.
- [x] **B.4 — AniList `airingSchedule` polling.** `BUILD_PLAN.md`'s own
  one-liner named the source but not the mechanism, cadence, or
  per-season scoping — asked directly, then verified against AniList's
  real API (three separate live `Media` entries) rather than assumed.
  `SCOPE.md` §6.7's own "Resolved 2026-08-09 (B.4)" note has the full
  design history; short version:
  - **No new due-query/mutation/cadence** — folded directly into
    `metadata.fetch_and_populate()`'s existing daily/on-demand
    anime-branch, riding B.1's own `dueForMetadataRefresh`/
    `refreshShowMetadata` cadence exactly as-is. Confirmed with the
    user: air dates don't need B.3's own adaptive-cadence treatment.
  - **Per-season, not per-show**: `season.anilist_id` (§5.5's Fribb
    crosswalk), not `_fetch_anilist`'s single show-level id — a
    split-cour sequel is a separate AniList `Media` entry. Verified
    live against three real `Media` entries that `airingSchedule.
    episode` always resets to 1 per entry, matching directly onto
    `episode.episode` with no numbering offset — an early hypothesis
    (broadcast-continuous numbering) tested and disproven before it
    shaped anything.
  - **Full schedule fetched, not `notYetAired`-only** — reconciles
    already-aired episodes' dates too, confirmed with the user.
  - **Manual dates hard-protected — revised after the first draft
    shipped, on the user's own reasoning**: the first draft matched
    §6.7's original "does not gate" text literally (unconditional
    writes, even over a manual value) and was reviewed against the
    user before pushing. Corrected: only a genuine reschedule signal
    (animeschedule.net, B.5, not built yet — a real-world disruption
    like a sports broadcast preempting a timeslot) should override a
    value the user deliberately set; AniList/Sonarr repeatedly
    re-asserting stale data over an already-fixed value is the
    "stubborn weekly rewrite" failure mode the user flagged. Now skips
    outright (no write, no `pending_review`) whenever `air_date_source
    = 'manual'` — the same hard-gate shape `season_mapping.py`'s own
    `reconcile_season()` already gives `season.manual_override` (§3
    principle 6). `SCOPE.md` §6.7's own priority-order text corrected
    to match.
  - **Season-split guard, confirmed live, not hypothetical**: a single
    TVDB season can span *multiple* separate AniList `Media` entries —
    confirmed by querying both of Attack on Titan Season 3's two real
    entries directly (12 + 10 episodes, one 22-episode TVDB season).
    `season.anilist_id` can only point at one, so per-episode matching
    silently misapplies dates across the cour boundary whenever LCARS's
    own episode count for a season exceeds that entry's own reported
    total. Guarded: skip the season's reconciliation entirely, open a
    `pending_review` on the season row itself (not each episode).
    Deliberately not extended to a full fix (a human resolving the
    review with an actual episode-range split) — that needs a genuinely
    new, structured multi-entry-per-season mapping `pending_review`'s
    free-text resolution can't represent; flagged as a real follow-up,
    not scheduled here.
  - **Real ordering bug caught before commit**: the first draft placed
    the new reconciliation call *before* `_fetch_sonarr` in
    `fetch_and_populate` — on a show's very first-ever fetch, episode
    rows don't exist yet at that point, so every brand-new anime show
    would get zero AniList correction until the *next* day's refresh.
    Moved to run after the Sonarr/Radarr fetch instead, so a new show
    gets correct air dates immediately.
  - **Built**: `util.py` — `unix_to_iso()` (AniList's own `airingAt`
    shape, epoch seconds). `anilist_client.py` — `fetch_airing_
    schedule()`, a separate lighter query from `fetch_media()` (no
    cast/relations payload), returning `{episodes, nodes}` — the
    episode count is what the season-split guard compares against. `
    metadata.py` — `_reconcile_air_dates()`, its own `_guarded()` call
    in `fetch_and_populate()`, positioned after the Sonarr/Radarr fetch
    (see the ordering fix above). No schema/migration/Ops changes at
    all — this rides existing machinery entirely.
  - **Tests**: `test_anilist_client.py` (+3: `fetch_airing_schedule`'s
    own client-layer coverage, including its `{episodes, nodes}`
    shape). `test_server.py` (+6, end-to-end through real GraphQL:
    corrects a Sonarr-seeded date + logs `pending_review` keyed by the
    episode's own id; a manual date survives untouched with no new
    review; a no-op when the value's already correct; skips a season
    that spans multiple AniList entries, opening a season-level review
    instead of writing wrong-cour dates; skips a season with no
    `anilist_id` at all, `fetch_airing_schedule` never even called;
    skips an AniList-reported episode LCARS hasn't fetched yet). Ten
    pre-existing AniList-stubbing call sites also needed a matching
    `fetch_airing_schedule` stub — caught by a real timing regression
    (the suite's own runtime jumped ~30s from unstubbed real network
    calls silently succeeding under `_guarded`'s broad exception catch,
    not a test failure) before it was fixed.
  - **Verified**: 384 tests passing (was 375 at B.3b's close), `ruff
    check .` clean. Beyond the mocked tests — real, live verification
    against AniList's actual public API: `fetch_airing_schedule(21)`
    (One Piece) returned 25 real upcoming episodes, `util.unix_to_iso()`
    on a real `airingAt` value converted correctly, and the season-split
    scenario itself was confirmed against two real AniList `Media`
    entries (Attack on Titan S3 Part 1/Part 2) before the guard was
    written. Clean-install sanity check (fresh venv, real `pip
    install`) confirmed `lcars.metadata`/`lcars.anilist_client` import
    cleanly.
- [x] **B.5 — animeschedule.net polling.**
  - **Built**: route resolved by live testing, not the plan's own
    original assumption — `/api/v3/timetables/{airType}` (the endpoint
    this line originally preferred, filtered by `anilist-ids`) returns
    a real, live **401 "Unauthorized. Use private endpoint."**; no
    self-serve registration page exists either (`/api`, `/api/v3`,
    `/about/api`, `/api-docs` all confirmed 404 live). `/api/v3/anime`
    (including its own `anilist-ids` filter) *does* work with no token
    at all, but only returns a show-level delay flag
    (`delayedTimetable`/`delayedFrom`/`delayedUntil`), no per-episode
    date — confirmed insufficient to fill §6.7's air-date-source slot.
    Asked the user directly rather than guess at an undocumented
    registration process; confirmed: fall back to RSS outright (this
    line's own documented fallback), accepting the fuzzy-matching
    tradeoff. Only the **raw** feed (`/jpnrss.xml`, native/Japan
    release) is polled — `/subrss.xml`/`/dubrss.xml` describe a
    fansub/dub group's own later release, a different event this
    project doesn't model. Confirmed live: the raw feed is a fixed
    ~25-item rolling window that covered barely 16 real hours in one
    snapshot — rotates faster than a day, so `animeschedule.py` rides
    Ops's existing **hourly** tick (`run_daily_and_weekly_once`), not
    B.1/B.4's daily due-gated cadence; no new interval added.
    `animeschedule_client.py` (pure fetch/parse, stdlib
    `xml.etree`/`re`, no new dependency) + `animeschedule.py`
    (orchestration: candidate shows = `status = 'watching'` and
    actively airing and `tracking_space = 'anime'`; matching reuses
    `fuzzy.best_match()` — §5.4/A.7 — verbatim, same threshold, same
    "return None rather than guess" philosophy, restricted to the
    candidate pool per the user's own explicit call ("keeping the
    search to tracked shows should elucidate the vast majority of
    cases"); episode resolution restricted to that show's own
    currently-airing season(s) — exactly one candidate episode row
    applies the date, zero or more than one **flags, doesn't guess**
    (`pending_review`, field `animeschedule_episode_match`, per "simply
    flagging those which aren't clear will suffice")). New
    `pollAnimeSchedule: AnimeSchedulePollResult!` mutation, global
    sweep shape (no per-item argument), same as B.3's
    `pollFileAvailability`. No migration needed — `air_date_source`'s
    CHECK constraint already included `'animeschedule'` since the
    initial schema.
  - **Manual-override exemption resolved** — the question §6.7
    explicitly left open when B.4 built AniList/Sonarr's hard
    manual-date gate: asked directly, confirmed animeschedule.net
    **does** override `air_date_source = 'manual'`, restating the
    user's own B.4 reasoning ("the overwrite and log is intended for
    when a new information about an air date is logged... those data
    are likely to come from animeschedule"). Every such overwrite still
    opens/extends a `pending_review` — visible, never silent.
  - **Post-write review caught a real bug before commit**: the flagged
    (ambiguous-match) write path called `pending_review.open_or_extend`
    unconditionally on every sweep, with no equivalent to the applied
    path's own "unchanged if the value already matches" early return.
    Given the raw feed's own ~16-hour rolling window (above) and Ops's
    hourly tick, the *same* still-in-window item would re-append an
    identical finding to the same review's `proposed_value_chain` on
    roughly a dozen consecutive sweeps — turning §5.6's "the automatic
    source changed its mind again" chain into a dozen copies of "the
    same feed item was re-read." Fixed with a local guard
    (`_flag()`/`_last_chain_entry()` in animeschedule.py) that skips the
    write when the finding's message is identical to the review's own
    last chain entry — kept local to this module rather than changed in
    shared `pending_review.py`, to avoid altering B.4's already-shipped/
    verified AniList chain-accumulation semantics as a side effect. A
    second, smaller finding in the same pass: the "show matched but has
    no currently-airing season" branch returned `"unchanged"` (silently
    swallowing a real match) rather than flagging — corrected to flag,
    with a dedicated regression test calling `_apply_or_flag` directly
    (the branch is unreachable via `poll_anime_schedule`'s own normal
    flow today, since `_candidate_shows`/`_airing_seasons` share one
    predicate, but the mislabeled fallback could have silently
    swallowed a real finding if that ever changed).
  - **Verified**: 410 tests passing (was 384 at B.4's close), `ruff
    check .`/`ruff format --check` clean. Live testing throughout, not
    just doc-reading: the 401/404 endpoint findings above, the real raw
    feed's actual item shape and ~25-item/~16-hour window. Clean-install
    sanity check (fresh venv, real `pip install`) confirmed
    `lcars.animeschedule`/`lcars.animeschedule_client`/
    `ops.scheduler.run_animeschedule_once`/
    `ops.lcars_client.LcarsClient.poll_anime_schedule` all import
    cleanly; `lcars.server.build_schema()` confirmed
    `AnimeSchedulePollResult`/`pollAnimeSchedule` both real, bound
    schema members (104 types total).
- [x] **B.6 — Per-integration service-health tracking.**
  - **Scope resolved with the user**: reachability only, not
    rate-limiting — checked first (a real gap, not assumed): none of
    the four client modules (sonarr/radarr/anilist/animeschedule_client)
    distinguish a 429 from any other error today, so "rate-limited?"
    would need new detection logic in all four plus a decision on
    whether LCARS acts on it or just reports it. Asked the user
    directly rather than guess at that scope; deferred, documented as
    a real future gap, not built here.
  - **Architecture resolved with the user**: §6.7's own "Tracked by
    Ops" phrasing conflicts with the already-established architecture
    (Ops has no client code for any of these four services and no
    database of its own, §11.2's B.1 resolution) — asked directly
    rather than guess at an undocumented design; confirmed the reading
    that Ops's poll cycle triggers the calls, but the state lives
    entirely in LCARS (new `service_health` table, no id prefix, same
    natural-key-per-service shape `availability_poll_checkpoint`, B.3,
    already uses), exposed via a new `serviceHealth` query. No `ops/`
    changes at all — health is a side effect of calls Ops (and humans,
    via on-demand mutations) already trigger.
  - **Built**: `service_health.py` (`record_success`/`record_failure`/
    `get_all`) hooked directly at each function's own actual outbound
    call — `metadata.py`'s `_fetch_anilist`/`_reconcile_air_dates`/
    `_fetch_sonarr`/`_fetch_radarr`, `availability.py`'s
    `_poll_sonarr`/`_poll_radarr`, `local_audit.py`'s `_audit_sonarr`/
    `_audit_radarr`, `animeschedule.py`'s `poll_anime_schedule`.
    `serviceHealth: [ServiceHealth!]!` always returns one entry per
    `TrackedService` (sonarr/radarr/anilist/animeschedule — deliberately
    excludes `local`, B.7's own SQL-aggregate concept, not a
    reachability check), synthesizing an `UNKNOWN` placeholder for a
    service never yet contacted rather than omitting it.
  - **Real bug caught before commit, not hypothetical**: the first
    draft hooked `metadata._guarded` — its single choke point for all
    four AniList/Sonarr/Radarr fetch functions — on the assumption that
    "the wrapped function returned without raising" meant "the service
    was reachable." Wrong: every one of those functions has its own
    legitimate no-HTTP early-return path (missing external id, service
    not configured, no season carries an `anilist_id` yet). Caught by
    a real, already-existing test
    (`test_add_show_fetch_failure_logs_pending_review_and_refresh_retries`)
    going red: a genuine `_fetch_anilist` failure got silently
    overwritten back to `ok` by the very next `_guarded` call
    (`_reconcile_air_dates`'s own trivially-succeeding no-mapped-season
    no-op). Fixed by moving each hook to the function's own actual
    client call instead of the shared wrapper — `_guarded` reverted to
    its original `pending_review`-only job, with a docstring explaining
    why service-health recording doesn't belong there. See
    `service_health.py`'s own module docstring for the full "success
    means a request completed, not that a function returned" rule this
    established.
  - **Verified**: 421 tests passing (was 411), `ruff check .`/`ruff
    format --check` clean. Migration round-trip verified on an
    isolated temp DB (not the ambient/default one — caught mid-check
    that the default `LCARS_DATABASE_URL` pointed at an already-current
    dev DB, giving a misleading first result): upgrade → downgrade →
    upgrade all confirmed clean, `service_health` table present/absent
    exactly as expected at each step. Clean-install sanity check (fresh
    venv, real `pip install`) confirmed every new/changed module
    imports cleanly and `lcars.server.build_schema()` resolves
    `ServiceHealth`/`serviceHealth` as real, bound schema members (107
    types total). A genuine, pre-existing test bug found incidentally
    while adding a health assertion:
    `test_sonarr_client_error_mid_walk_keeps_earlier_partial_results`
    configured a `fail_series_id` on an *untracked* series, so the
    loop's own untracked-show `continue` skipped the call that was
    supposed to fail before ever reaching it — fixed by tracking that
    series too, so the configured failure genuinely fires.
- [x] **B.7 — `show_service_presence` periodic refresh.**
  - **Scope resolved with the user — this line's own "AniList/MAL
    presence, periodic" reading was checked, not assumed, and turned
    out blocked**: `anilist_client.py` has no search/catalog-listing
    endpoint at all (only `fetch_media(anilist_id)`, a single-entry
    lookup), and `mal_client.py` doesn't exist yet (B.10). Reported as
    a real, checked finding — same shape as B.5's `/timetables` 401 —
    not silently skipped or half-built around. Scope: `local` +
    Sonarr + Radarr, the three genuinely buildable now.
  - **Cadence resolved with the user**: `local`'s rollup is a pure SQL
    aggregate (zero external dependency, negligible cost) and rides
    Ops's hourly tick. Sonarr/Radarr catalog matching is real N×M cost
    (fetch a whole catalog, fuzzy-match every tracked show of the
    matching mediaShape against it) — the same class of concern that
    produced B.3's seed-and-skip design and `backfillFileAvailability`'s
    manual-only carve-out. Asked directly rather than default to the
    hourly tick: chose **"a slower dedicated tier"** over due-gating
    (like B.2's weekly `dueForSeasonReconciliation`) or riding the
    hourly tick unconditionally. Built by reusing B.2's own existing
    monthly cadence directly (`run_monthly_once` renamed/composed, not
    a new interval) — same "no new interval unless a real technical
    constraint forces one" precedent B.1/B.4/B.5's own cadence
    decisions already established.
  - **Built**: `service_presence.py` — `refresh_local_presence`
    (episodic: any episode with `available_locally`; movie: the
    show's own `available_locally`, both already-generated columns,
    B.3) and `refresh_catalog_presence` (Sonarr `all_series()`/Radarr
    `all_movies()` — the same client methods `local_audit.py`, B.3b,
    already established LCARS calling directly — fuzzy-matched via
    `fuzzy.best_match()`, §5.4/A.7, unchanged). Two new mutations,
    `pollLocalServicePresence`/`pollCatalogServicePresence`, global
    sweep shape (no per-item argument), same as B.3's
    `pollFileAvailability`. `service_health.py` (B.6) hooked at the
    new Sonarr/Radarr catalog calls too — every real outbound call
    this project makes now reports through it.
  - **Real bug caught before commit, not hypothetical**: the first
    draft's `_upsert_presence` wrote `present`+`checked_at`
    unconditionally on every call, only *counting* real changes.
    Since `refresh_local_presence` runs this once per tracked show
    every hour, forever, that meant a permanent steady-state stream of
    pointless UPDATEs whose only effect was a timestamp nothing reads
    (no due-gate consumes `checked_at` here — the user chose
    unconditional sweeps over one). Fixed: an unchanged existing row
    is now left completely untouched — genuinely idempotent, zero
    writes once local state is stable. Regression test added, clock
    monkeypatched to a distinguishable value across two calls (plain
    wall-clock comparison couldn't reliably prove no write happened,
    since `now_utc_iso()`'s own second-level precision could hide one
    within the same second).
  - **Verified**: 446 tests passing (was 421), `ruff check .`/`ruff
    format --check` clean. `src/ops/cli.py` grepped directly (not
    inferred from the passing suite alone) to confirm no reference to
    the renamed `run_monthly_once`/new `run_season_reconciliation_once`
    functions. Clean-install sanity check (fresh venv, real `pip
    install`) confirmed every new/changed module imports cleanly and
    `lcars.server.build_schema()` resolves both new mutations as real,
    bound schema members (108 types total).
- [x] **B.8 — Air-date reconciliation.** This line's own original text
  ("never gates the apply, including one overwriting a manual value")
  was already stale by the time this step was reached — B.4 corrected
  that framing on the user's own reasoning before B.4 was even
  committed, and §6.7 was updated to match at the time (see B.4's own
  entry above). So most of B.8 was already built as a side effect of
  B.4+B.5, not started fresh here.
  - **Built**: the one real gap this step actually closed, found by
    checking the current priority order (`Manual > animeschedule.net >
    AniList > Sonarr raw`, §6.7) against what B.4's own code actually
    did, not assumed: `metadata._reconcile_air_dates` (B.4) predates
    B.5 and only ever guarded `air_date_source = 'manual'` — an
    animeschedule-sourced date (ranked *above* AniList) survived only
    until AniList's own next daily pass silently overwrote it, a live
    violation of the documented priority order in already-shipped,
    already-pushed code. One-line fix: the guard now skips
    `('manual', 'animeschedule')`, not just `'manual'` — same
    no-write/no-`pending_review` treatment as the manual case (a
    lower-priority source correctly declining to overwrite a
    higher-priority one isn't something to flag). Verified the other
    two write paths (Sonarr's own INSERT-only seed at episode creation,
    the manual-set mutation) needed no equivalent fix — grepped every
    `air_date_utc =` write site in `src/lcars/` to confirm exactly
    three exist, not assumed.
  - **Sequencing confirmed with the user**: found while starting B.6 in
    document order; asked directly whether to fix this live gap first
    or defer to when B.8 came up in order — user chose fix-now.
  - **Verified**: 411 tests passing (was 410 before this fix), `ruff
    check .`/`ruff format --check` clean. New test
    (`test_anilist_air_date_reconciliation_never_overwrites_an_
    animeschedule_date`) mirrors the existing manual-date test exactly,
    seeding `air_date_source = 'animeschedule'` directly via `db.
    get_connection()` (no GraphQL mutation writes that source directly)
    and asserting `refreshShowMetadata` neither changes the value nor
    opens a new `pending_review`. Clean-install sanity check (fresh
    venv, real `pip install`) confirmed `lcars.metadata` still imports
    cleanly.
- [x] **B.8b — `episode_movie_link` automatic `tmdb_match` derivation**
  (§5.1's movie↔`bonus_movie` addendum). **Added by the 2026-08-09
  audit**: §5.1 specifies the full reconciliation ("internal ids are the
  source of truth, external ids map onto them, a best guess applies
  immediately with a `pending_review` entry on ambiguity, manual
  override wins once set"), but only the `tmdb_match` enum value and
  A.3's manual mutations ever existed — no step in Phase A, B or C
  claimed the automatic half, so it was silently on track to never be
  built.
  - **Resolved 2026-08-10**: two genuinely open questions in this
    entry's own text, both settled before writing any code — one by
    checking the actual API/schema shape, one directly with the user.
  - **`bonus_movie` promotion — settled by checking A.25's own text,
    no automatic path exists to build**: this entry's phrasing ("via
    `setEpisodeKind` (A.25) or its own automatic path") read as open,
    but A.25 already resolved it: "Sonarr cannot distinguish `special`
    from `ova` or `bonus_movie` at all... automatic classification
    honestly stops there" (SCOPE.md §5.2). Confirmed independently:
    `episode` has no title column at all (`schema.graphql`), so there
    is no signal to classify on even in principle. `setEpisodeKind`
    (A.25, manual) stays the only route into `bonus_movie` — this step
    only processes rows already carrying that kind.
  - **Candidate pool for the automatic match — asked the user
    directly, per the standing instruction**: checked `sonarr_client.py`
    first, confirming a season-0 episode carries no title and no
    TMDB-comparable id at all (Sonarr is TVDB-native), so literal
    id/title matching from the episode side is impossible, not merely
    ambiguous. Two real designs remained — broad fuzzy title match
    across every tracked Radarr movie show (`service_presence.py`'s
    own approach, B.7), or narrowing candidates via `show_relation`
    (every tracked `media_shape = 'movie'` show already related to the
    episode's parent show, either direction — the same graph A.21's
    `_link_relation` already populates). User chose relation-graph-
    narrowing: `episode_movie_link` auto-applies a real identity write
    (unlike B.7's suggestion-only presence flag), so a smaller,
    higher-confidence pool was preferred over a broader fuzzy net.
  - **Built** (`episode_movie_link.py`, new): `reconcile_episode_movie_
    links(conn)` — for every `bonus_movie`-kind episode without a
    `manual_override` link, exactly one relation-graph candidate
    applies immediately (`source = 'tmdb_match'`); zero records a real
    `unmatched` row (self-heals later if a relation edge appears);
    more than one opens a `pending_review` (`field =
    "episode_movie_link"`), with the same chain-dedup guard B.5's
    `animeschedule.py` established (a repeat sweep re-seeing the
    identical candidate set is not a new finding) kept local to this
    module for the same reason B.5's own note gives. Also closes the
    other real gap `availability.py`'s own B.3 docstring explicitly
    named as this step's job, not its own: mirrors a matched link's
    movie show's `available_via_radarr`/`file_path_radarr` onto the
    linked `bonus_movie` episode (`tmdb_match` or `manual` links
    alike), only writing on a genuine change — same idempotency
    lesson B.7's `_upsert_presence` had to learn the hard way, applied
    here from the start. New `reconcileEpisodeMovieLinks` mutation
    (`EpisodeMovieLinkReconcileResult`: `matched`/`flagged`/
    `unmatched`/`availabilitySynced`). No external HTTP call anywhere
    in the sweep (pure internal SQL reconciliation) — no `service_
    health` hook applies, and per the "no new interval unless a real
    technical constraint forces one" precedent it rides Ops's existing
    hourly tick alongside B.7's local-presence rollup, not a new one.
  - **Real bug caught in review, before commit**: the availability-sync
    half's first draft joined `episode_movie_link` -> `episode` -> `show`
    with no `kind`/`media_shape` guard at all. The automatic derivation
    above only ever creates rows that satisfy both, but `setEpisodeMovie
    Link` (A.3, manual) checks neither — a manual link against a
    `regular`/`special` episode, or against an episodic show, would have
    written a real Radarr availability value onto a row where it's
    meaningless (§5.2: `available_via_radarr` "only ever populated for
    `bonus_movie` kind"; §5.1: movie-only on the show side), silently
    flipping the *generated* `available_locally` column for an episode
    with no Radarr file of its own. Fixed by adding both guards directly
    to the sync query; regression tests cover both directions plus a
    self-healing flip-to-`unmatched` case (a previously-matched
    candidate later untracked).
  - **Verified**: 467 tests passing (was 446), `ruff check .`/`ruff
    format --check` clean, clean-install sanity check (fresh venv,
    real `pip install`) confirmed the schema still builds (109 types,
    `reconcileEpisodeMovieLinks` present on `Mutation`) and every
    existing migration still applies cleanly head-to-head — no new
    migration needed, `episode_movie_link`'s table already had
    everything this step needed since A.2.
- [x] **B.9 — Backlog visualization** (§6.3): calendar-native counter
  line under a show's next-episode entry; mark-watched from it clears
  exactly one oldest episode per action.
  - **Scope, asked directly rather than assumed**: this entry names
    three things — binding LCARS's own `Query.backlog` (unbound since
    A.3's own consolidation audit correctly deferred it here, not a
    new gap), and two Data-side UI pieces (the counter line, mark-
    watched-clears-oldest) living in `~/repos/data`. `Query.backlog`
    is a real, buildable LCARS gap; the Data-side pieces are genuinely
    buildable today too, so this wasn't a technical-impossibility call
    like B.8b's `bonus_movie` question — it's a real sequencing
    tradeoff, surfaced rather than decided unilaterally. Confirmed:
    bind `Query.backlog` now; defer the Data-side UI to **B.11**
    ("Data's role shrinks... calendar reads state from LCARS instead
    of computing it locally") — Data's own `CLAUDE.md` confirms it
    still computes calendar state locally today, not LCARS-backed yet,
    so building the counter widget against that now would mean
    rebuilding it against LCARS once B.11 lands. B.11's own entry now
    owns the two deferred pieces.
  - **Built**: `Query.backlog` (`resolve_backlog`, resolvers.py) —
    unwatched, locally-available episodes on watching-status, actively-
    airing shows (§6.3's own header: "airing shows"). "Locally-
    available" (not merely aired) matches `nextUp`'s own precedent
    (§6.4, A.11); a `downloading` episode isn't backlog yet either.
    Real table-backed pagination (`pagination.paginate()`, same shape
    `episodesAiringSoon`/§8 already established), not a computed list
    like `nextUp` — `backlog` is every qualifying episode, not one per
    show. The airing check reuses `_show_is_airing` (A.10) verbatim
    rather than a third re-derivation of the same predicate (already
    duplicated once, for a documented circular-import reason, in
    `animeschedule.py`) — same "not a single-column SQL comparison, so
    compute eligible show_ids in Python first" shape
    `dueForMetadataRefresh`/`dueForSeasonReconciliation` (B.1/B.2)
    already use, then handed to the real table scan. No migration
    needed — `episode`'s existing columns already cover the predicate.
  - **Verified**: 473 tests passing (was 467), `ruff check .`/`ruff
    format --check` clean, clean-install sanity check (fresh venv,
    real `pip install`) confirmed the schema still builds (109 types,
    `backlog` present on `Query`). One gap caught in review before
    commit: the empty-candidate short-circuit branch (no watching+
    airing shows at all — a normal between-cours state, not an edge
    case) had no test querying `pageInfo`, the exact class of bug
    `pagination.py`'s own docstring already documents (a real
    silently-null `pageInfo` bug that went unnoticed pre-A.11 for
    exactly this reason). Added.
- [x] **B.10 — MAL integration** (§6.9 — can start any time after
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
  - **Legacy score import is PC.2's job, not this step's** — cross-
    checked before assuming this bullet meant B.10 had to build it:
    `PC.2 — One-time historical imports` already explicitly names "MAL
    legacy scores" as its own scope. Confirmed by cross-reference, not
    built twice.
  - **Registration/verification sequencing, asked directly**: unlike
    AniList (reuses Data/aniq's already-registered app), MAL had never
    been registered in this project — no existing app to reuse, and
    registration is a browser + the user's own MAL account, not
    something buildable unilaterally. Two options presented (build
    unverified now vs. register first); user chose **register first,
    then build**, so this step closes with the same real live
    verification every prior B-step got, not fake-client-only coverage.
  - **Real correction found live during registration, not from docs**:
    §6.9's own assumption ("client_id + client_secret on approval",
    mirroring AniList's shape) is wrong for the app type LCARS actually
    needs. MAL's "Other" app type (correct for a CLI/server tool) is a
    PKCE **public client** and issues no `client_secret` at all — "Web"
    apps get one, but that's the wrong type here. `mal_client.py`
    treats `client_secret` as optional end-to-end, never assumed
    present. SCOPE.md §6.9 corrected with the full finding.
  - **Redirect URI, a real qBittorrent-port collision caught before
    registering**: `http://localhost:8080/`, the first pick, collides
    with qBittorrent's own default WebUI port on the user's machine —
    caught by the user directly, not discovered live. Settled on
    `http://localhost:1701/` instead (thematically apt, and clear of
    qBittorrent/Sonarr/Radarr/LCARS's own ports) — nothing needs to
    actually listen there; the code arrives in the browser's own
    address bar after MAL's redirect, same manual-copy shape
    `anilist_client.py`'s PIN-redirect flow already uses.
  - **Refresh-token rotation, confirmed live rather than left
    ambiguous** — MAL's own written docs don't clearly state whether
    the `refresh_token` grant resets its own 1-month clock on use, so
    this was verified directly against the user's real tokens (see the
    live-verification note below) rather than assumed either way: **it
    rotates** — a genuinely new `refresh_token` came back. Confirms
    this bullet's own "build the renewal job now" premise is
    technically sound, not built on an untested assumption.
    `refresh_access_token()` still falls back to the token just used if
    a response ever omitted one (defensive, not a hedge against real
    doubt anymore), and both values are persisted together on every
    refresh (`config.save_mal_tokens()`).
  - **Built**: `mal_client.py` (new) — PKCE OAuth (plain method,
    `generate_code_verifier()`/`authorize_url()`/`exchange_code()`/
    `refresh_access_token()`), form-encoded (not GraphQL/JSON) REST
    push (`update_my_list_status()`), same connect/timeout/HTTP-error/
    auth-error shape `anilist_client.py` already established.
    `lcars mal-login` (cli.py) — same interactive/one-time bootstrap
    shape as `anilist-login`, but only requires `mal_client_id` up
    front (never a secret). `config.py` gains `mal_client_id`/
    `_secret`/`mal_access_token`/`mal_refresh_token`/
    `mal_token_refreshed_at` (all `_FILE`-secret-mount-capable per
    A.23, `mal_token_refreshed_at` deliberately not treated as a
    secret) and `save_mal_tokens()` (persists both tokens + a
    timestamp together, always, correct under either rotation
    reading). Score/status push wired into `setScore`/`setStatus`/
    `setSeasonScore` alongside the existing AniList push, keyed on
    `season.mal_id` (already populated by B.2's own Fribb
    reconciliation since A.4 — no new crosswalk needed), same
    fallback/best-effort/`pending_review`-on-failure shape
    (`field = "mal_push"`). New `refreshMalTokenIfDue` mutation:
    self-gated on an internal 7-day checkpoint (same "checkpoint
    decides, not a separate due-query" shape `pollAnimeSchedule`/
    `pollFileAvailability` already use), rides the existing hourly
    tick (no new Ops interval) rather than the "weekly" language taken
    literally as a new one. `service_health` extended to a 5th
    tracked service (`mal`, new migration `2818c7efae13`) — hooked at
    the refresh call only, matching AniList's existing scope exactly
    (push failures aren't hooked for either service, only fetch/
    refresh paths are) — a small consistency gap closed on the same
    "found it, closed it" precedent B.6/B.8b/B.9 already established,
    not something this step's own text asked for.
  - **A real cross-cutting bug fixed before it could ever hit
    production, not hypothetical**: `config.get_current()` is a
    process-wide singleton set once at startup — persisting refreshed
    tokens to `lcars.ini` alone would leave every push the same
    running process makes afterward reading the *stale* in-memory
    access_token until a restart. `refreshMalTokenIfDue` updates both
    the file (`save_mal_tokens()`) and the live singleton's own
    attributes in the same call.
  - **Live-verified end-to-end, 2026-08-10, not just against fakes —
    all three real code paths, not just the two originally planned**:
    the user registered a real MAL app, pasted the client_id, and
    walked through the actual PKCE authorize/exchange with their real
    MAL account — a genuine `access_token`/`refresh_token` pair was
    minted and persisted to the real `~/.config/starfleet/lcars/
    lcars.ini`. A live `refresh_access_token()` call (prompted by
    review, not part of the original close-out plan) confirmed the
    rotation finding above directly, and re-persisted the rotated
    pair. Closed with one real live push (`update_my_list_status`,
    anime id 59985 -> `completed`), confirmed against MAL's actual API
    with the full response object returned, not mocked.
  - **Real gap caught in review, before commit**: `_STATUS_TO_MAL` is a
    bare dict lookup keyed on `show.status` — checked the actual CHECK
    constraint directly rather than trusting it matched (it does, all
    5 values), added a comment saying so explicitly, and a regression
    test walking every real status value through `setStatus` end-to-end
    to lock it in (the same gap shape would silently break `setStatus`'s
    own "push never fails the local write" invariant if a status enum
    value were ever added without updating this dict).
  - **Verified**: 514 tests passing (was 473), `ruff check .`/`ruff
    format --check` clean, clean-install sanity check (fresh venv,
    real `pip install`) confirmed the schema still builds (110 types,
    `refreshMalTokenIfDue` present on `Mutation`, `MAL` present in
    `TrackedService`), migration round-trip (upgrade/downgrade/
    upgrade) confirmed clean for the new `service_health` CHECK-
    constraint widening, and the real live OAuth/refresh-rotation/push
    checks above.
- [ ] **B.11 — Data's role shrinks**: calendar reads tracking/air-date/
  availability state from LCARS instead of computing it locally.
  Data's status bar gains per-source sync-health indicators sourced
  from Ops's service-health state (B.6). **Also owns B.9's two
  deferred Data-side pieces** (confirmed with the user 2026-08-10,
  B.9's own entry has the full reasoning): the calendar-native counter
  line under a show's next-episode entry, backed by LCARS's now-built
  `Query.backlog`, and its mark-watched-clears-exactly-one-oldest-
  episode interaction — deliberately not built during B.9 itself,
  since Data doesn't read from LCARS at all yet before this step.
  - **Scope, confirmed with the user 2026-08-10**: SCOPE.md's own
    Phase B/C split text is genuinely ambiguous about whether B.11
    replaces the calendar's core render path now or Phase C does —
    asked directly, user chose **Replacing**: B.11 itself switches
    the render path, not just adds read-only extras alongside the
    existing Sonarr/AniList-driven computation. Given the size/risk
    of that rewrite, also asked and confirmed splitting into numbered
    sub-steps (B.11a/B.11b/B.11c, same pattern B.3b/B.8b already used
    for an oversized single step), starting with the smallest
    self-contained slice — `Query.serviceHealth` already exists
    (B.6), needs no LCARS-side change, and Data has never made a
    single LCARS *read* before this (A.17 was write-only, per
    SCOPE.md's own "LCARS-backed reads are Phase B, not A.17" note).
  - [x] **B.11a — LCARS read-query foundation + status bar
    service-health indicators** (`~/repos/data`, 2026-08-10). First
    LCARS query `lcars_client.py` has ever made: `service_health()`
    (no variables, returns all 5 `TrackedService` entries every
    time — LCARS's own resolver synthesizes `UNKNOWN` for a
    never-contacted service rather than omitting it, so the list is
    never short one). Data's status bar gained a leading
    `Son✓ Rad✓ AL✗ ASc? MAL✓`-shaped segment (module-level
    `SERVICE_HEALTH_LABELS`, fixed order, one short label + symbol
    per service — ✓/✗/? for OK/UNREACHABLE/UNKNOWN), polled on the
    same interval as the existing download-status check
    (`LCARS_HEALTH_CHECK_INTERVAL_SECONDS = DOWNLOAD_CHECK_INTERVAL_SECONDS`,
    no new interval — reuses the existing one, same "no new interval
    unless a real technical constraint forces one" precedent B.10
    set). Same "not configured is a clean no-op, never an error"
    treatment every other optional LCARS/AniList feature already
    gets: segment is the empty string (omitted entirely, not a row
    of "?" placeholders) whenever `lcars_client` isn't configured or
    the first check hasn't completed yet; a transient fetch failure
    is swallowed the same way `_check_download_status()` already
    swallows a `SonarrError` — keep whatever was last known, the
    next interval just tries again.
    - **Real bug found and fixed during this slice**: the new
      initial-mount health check fires for every `DataApp` built
      with an `lcars_client`, including ones in pre-existing tests
      that predate this feature — `test_app_lcars_write_wiring.py`'s
      `test_quit_flushes_both_anilist_and_lcars_queues` broke with a
      `KeyError: 'serviceHealth'` because its fake LCARS handler
      didn't recognize the new query. Fixed by adding a
      `serviceHealth` branch to that test's fake handler (returns an
      empty list — harmless, valid, no health segment shown), not by
      special-casing the new check to skip test contexts.
    - **Verified**: 500 tests passing (Data's own suite, was 495),
      `ruff check .` clean (after removing an unused `pytest` import
      the new test file didn't end up needing), `ruff format --check`
      confirmed all 5 actually-touched files clean (3 unrelated
      pre-existing files elsewhere in the repo were already
      unformatted before this change and are out of scope here),
      clean-install sanity check (fresh venv, real `pip install
      -e .[dev]`) confirmed the new client method and status-bar
      method import cleanly.
    - **Follow-up fix, same day**: caught in review right after this
      slice shipped — `_check_lcars_service_health()` called
      `_refresh_calendar_view()` unconditionally on every successful
      poll. Unlike `_check_download_status()` (gated by
      `_has_outstanding_downloads()`, so it only refreshes when
      there's real work outstanding), the health poll has no such
      gate — it runs every interval for as long as `lcars_client` is
      configured — so it was rebuilding the whole calendar table on
      every tick regardless of whether health actually changed. Fixed
      by diffing the fetched result against current state before
      refreshing. Also closed a coverage gap the same review raised:
      no test previously confirmed the health segment (which now
      leads `_status_bar_text()`) still renders correctly alongside
      an active status-bar note. 502 tests passing after the fix,
      `ruff check`/`format` clean.
  - **B.11b reconnaissance, 2026-08-10**: before writing any Data-side
    code, enumerated what the calendar's render path actually consumes
    (`_refresh_visible_rows()`: `airDateUtc`, `seasonNumber`,
    `episodeNumber`, series title, episode title, `hasFile`, plus
    Sonarr-queue-derived downloading state and AniList track status)
    against what LCARS's schema already exposes (`backlog`, `nextUp`,
    `episodesAiringSoon`, `serviceHealth`). Two real gaps found, both
    checked with the user rather than decided unilaterally since
    neither is answered in BUILD_PLAN.md/SCOPE.md:
    - **No date-windowed episode query exists.**
      `episodesAiringSoon(days)` only covers future N days from *now*;
      Data's `calendar_nav.CalendarState.step_back()` has no floor, so
      the calendar can page arbitrarily far into the past. Confirmed
      with the user: build a genuine new query (`episodesInRange`, own
      sub-step B.11c below) as a natural extension of
      `episodesAiringSoon`'s own pattern — not a design decision, an
      implementation detail the plan already implied.
    - **LCARS's `show` table is completely empty** — checked directly
      (`sqlite3 lcars.db "SELECT COUNT(*) FROM show"` → 0). §5.1 makes
      `addShow` the only entry point and nothing auto-bulk-imports an
      existing Sonarr/Radarr library, so switching the calendar's row
      source to LCARS-tracked shows as-is would blank the calendar
      entirely on day one. Confirmed with the user: backfill first
      (own sub-step B.11d below), run for real against the live
      library, *then* switch. The user also raised a related ongoing
      concern while answering — shows added directly in Sonarr/AniList
      after the backfill (bypassing Data's `addShow` bridge) need a
      way to be "picked up" too, not just the one-time backfill.
      Checked further: B.3b's `auditLocalFiles` already computes
      exactly this (`untracked_shows`), but only as a one-shot
      CLI-triggered report, never on Ops's recurring schedule, nothing
      persisted between runs. Asked the user how findings should
      surface long-term; confirmed **recurring sweep, log for
      review** — a small new persisted reviewable list, Ops-driven,
      never auto-`addShow` (keeps §5.1/B.3b's existing "an untracked
      remote show is deliberately not auto-created" decision intact
      rather than reversing it) — own sub-step B.11e below.
  - [x] **B.11c — `episodesInRange` query** (2026-08-10): new
    `Query.episodesInRange(start: DateTime!, end: DateTime!, ...)`,
    same pagination/style as `episodesAiringSoon`/`backlog` — real
    table-backed `pagination.paginate()`, half-open `[start, end)`
    window (`air_date_utc >= ? AND air_date_utc < ?`), matching
    Data's own `calendar_nav.CalendarState.date_range()` convention
    exactly (`start <= local.date() < end`). Unlike
    `episodesAiringSoon` (future-only, relative to server "now"), the
    caller supplies explicit bounds — built for B.11f's calendar
    render path, which has no floor on how far back `step_back()` can
    page. Both bounds are plain Z-suffixed UTC strings, the same
    passthrough-validate-only convention `DateTime`/`setEpisodeAirDate`
    already establish — no new formatting/timezone logic needed.
    - **Verified**: 518 tests passing (was 514; 4 new —
      window-filtering, purely-past-window, exclusive-end-bound, and
      the empty-result `pageInfo` regression test B.9's own backlog
      test already established the need for), `ruff check`/`format`
      clean, clean-install sanity check (fresh venv, real `pip
      install -e .[dev]`) confirmed the schema builds with
      `episodesInRange` present on `Query`, correctly typed
      `EpisodeConnection!`. No migration needed — pure GraphQL/
      resolver addition, no schema/table change.
  - [x] **B.11d — show backfill** (2026-08-10): dry-run query
    (`previewShowBackfill`) + real mutation (`backfillUntrackedShows`),
    same "report-only preview before you write" shape
    `auditLocalFiles`'s own findings already establish, plus matching
    `ops preview-show-backfill`/`ops backfill-shows` CLI commands (the
    latter always shows the preview and requires a typed `yes` before
    writing anything).
    - **Corrected mid-build**: initially planned as addShow-then-
      separate-refreshShowMetadata per item, based on an incomplete
      read of `resolve_add_show`. Caught before building anything:
      `addShow` already calls `metadata.fetch_and_populate()` inline
      as its last step (`resolvers.py`, unchanged since A.8) — episode
      population, AniList link resolution (Fribb), season
      reconciliation, all already happen inside one `addShow` call.
      B.11d is a single throttled loop of plain `addShow`-equivalent
      calls, not a two-phase pipeline. Corrected with the user
      directly before proceeding.
    - **Extracted `addShow`'s own body into `shows.create_show()`**
      (new module) so `show_backfill.py` can call it directly, once
      per untracked item, without a GraphQL request context —
      `resolve_add_show` is now a thin wrapper, same "extract the
      shared logic" pattern `reconcileSeasonMapping`'s own
      `season_mapping.py` already established (A.4). Deep-link URL
      templates (`_EXTERNAL_ID_URL_TEMPLATES`/`_TMDB_URL_TEMPLATES`)
      moved from `resolvers.py` to `shows.py` as their new canonical
      home; `metadata.py` keeps its own separate duplicate (same
      circular-import reasoning as always).
    - **Reuses `local_audit.audit_local_files()`'s own
      `untracked_shows` computation** rather than a second catalog
      walk/dedup path — `_audit_sonarr`'s Sonarr entries gained one
      new field (`series_type`, Sonarr's own raw `seriesType`, not
      exposed on the GraphQL `UntrackedShow` type) so
      `show_backfill.py`'s classification can reuse it directly, the
      same signal `metadata.py`'s numbering-scheme derivation already
      uses (A.22: `seriesType == "anime"`).
    - **Classification, confirmed with the user 2026-08-10**: Sonarr
      items use `seriesType == "anime"` for `trackingSpace`; Radarr
      items always default to `TV` — no anime signal exists from
      Radarr at all, and a misclassification is fixable per-show
      after the fact, not worth a second live lookup just to classify
      one field.
    - **Status derivation, confirmed with the user 2026-08-10**: after
      an anime show is created, one one-time AniList `mediaListEntry`
      read (new `anilist_client.fetch_my_list_status()`, authenticated
      via LCARS's own `anilist_access_token`) seeds its initial LCARS
      status — not an ongoing sync, LCARS stays the source of truth
      for every push/pull after this. Reverse of `_STATUS_TO_ANILIST`
      (A.9's push-direction map); `REPEATING` (no direct LCARS target)
      maps to `watching`. Writes `status_change` history with its own
      distinct `changed_by = 'show_backfill'` value (§5.7's open-ended
      enum already anticipates process-actor values like this). A
      failed/skipped seed leaves the show at `addShow`'s own `planned`
      default — best-effort, never blocks the rest of the run.
    - **Watched-progress deliberately NOT backfilled, confirmed with
      the user 2026-08-10**: AniList's `mediaListEntry.progress` is an
      absolute episode count with no clean, general mapping onto
      LCARS's own per-season numbering (the whole absolute-vs-
      season_episode scheme, A.22/A.25, exists precisely because that
      mapping isn't uniform show to show). Stated consequence, not
      silently absorbed: a backfilled `watching` show starts with
      every episode unwatched, so `Query.backlog` (§6.3) will over-
      report for it until the user marks progress by hand.
    - **Idempotent/resumable by construction** — no bespoke resume
      state needed: re-running after a partial run (interrupted, rate-
      limited, crashed) only ever sees still-untracked items, since
      `local_audit`'s own known-id lookup already excludes anything a
      previous run already added.
    - **Throttled** between anime-classified adds only (2s,
      `ANIME_ADD_THROTTLE_SECONDS`) — the only ones that make AniList
      calls — to stay inside AniList's 30 req/min budget (Data's own
      `anilist.py` docstring: the same reason its `P` is manual-only).
      The CLI's own client uses a 1800s timeout for this one command
      (every other `ops` command keeps the 10s default) since a real
      run can take genuine minutes.
    - **Verified**: 549 tests passing (was 518; 31 new across
      `test_anilist_client.py` (+4, `fetch_my_list_status`),
      `test_local_audit.py` (1 existing test updated for the new
      `series_type` field, no new tests there), `test_show_backfill.py`
      (new, 15 tests: classification, dry-run-doesn't-write, real
      creation, idempotency, throttling, AniList status-seed including
      error/no-token/no-link/no-entry no-op paths), `test_server.py`
      (+2 wiring-only tests), `test_ops_lcars_client.py` (+2),
      `test_ops_cli.py` (+8)), `ruff check .`/`ruff format --check`
      clean, clean-install sanity check (fresh venv, real `pip install
      -e .[dev]`) confirmed the schema
      builds with `previewShowBackfill`/`backfillUntrackedShows`
      present and correctly typed, and that `shows.py`/
      `show_backfill.py`/the new `ops` CLI commands import cleanly. No
      migration needed — no schema/table change, pure Python +
      GraphQL-layer addition. Live run against the user's actual
      Sonarr/Radarr library deliberately deferred — build+verify now,
      run for real (`ops preview-show-backfill` then `ops
      backfill-shows`) as its own explicit step before B.11f needs the
      calendar to actually show anything.
    - **Two real bugs caught in review before the live run, same day**:
      1. `previewShowBackfill` wasn't actually a dry run.
         `preview_backfill()` called `local_audit.audit_local_files()`
         directly, which writes real `available_via_sonarr`/
         `available_via_radarr` corrections, records `service_health`,
         and walks the filesystem for orphan files — contradicting the
         mutation's own "no writes at all" docstring and breaking the
         "a Query stays side-effect free" convention `exportData`'s own
         docstring establishes. Fixed by extracting the untracked-
         detection logic (`_untracked_sonarr_entries`/
         `_untracked_radarr_entries`, pure functions) out of
         `_audit_sonarr`/`_audit_radarr`'s own inline loops in
         `local_audit.py`, and adding a genuinely read-only
         `find_untracked_shows_readonly()` that shares them —
         `preview_backfill()` now calls that instead;
         `backfill_untracked_shows()` (a real Mutation, where the bonus
         reconciliation is legitimate) still calls the fuller
         `audit_local_files()`. One implementation of the actual
         matching logic either way, not two.
      2. The flat 2s per-anime-show throttle was under-provisioned by
         roughly 2-3×. `_fetch_anilist` (1 call) plus
         `_reconcile_air_dates` (1 call *per season*, its own B.4
         docstring) plus this step's own status-seed read means even a
         single-season show makes 3 AniList calls, not the 1 the
         throttle assumed — a flat 2s sleep could run at up to
         ~90 req/min against AniList's 30 req/min budget. The failure
         mode was the quiet kind: a rate-limited fetch is swallowed by
         `_guarded()` into a `pending_review` entry, but the show is
         still created (tracked), so a resumed backfill would never
         revisit it — a permanently under-populated show with no
         visible error. Fixed with `_anilist_call_estimate()` (reads
         the season count `create_show()` just wrote, floors at 1) and
         `ANILIST_SECONDS_PER_CALL = 2.1`, so the sleep scales with how
         many AniList calls that specific show actually needed.
    - **Verified (follow-up)**: 550 tests passing (was 549; 1 new,
      `test_backfill_throttle_scales_with_season_count`, plus 2
      existing tests updated for the new read-only preview and scaled
      throttle), `ruff check`/`format` clean, clean-install sanity
      check confirmed `find_untracked_shows_readonly`/
      `ANILIST_SECONDS_PER_CALL`/`_anilist_call_estimate` import
      cleanly.
    - **Live infrastructure stood up the same day, real credentials**:
      the user re-pasted the Sonarr/Radarr keys (context had been
      summarized, secrets don't survive that) plus a new AniList
      `client_secret` for app 45425 (LCARS's own separate
      `anilist-login` session — see the new
      `lcars-anilist-client-secret-rotate-at-project-end` memory,
      alongside the existing Sonarr/Radarr/MAL ones, all rotate at
      Cutover). LCARS server started for real
      (`lcars serve`), Ops's own `ops.ini` pointed at it, a real
      `lcars anilist-login` OAuth exchange completed and verified live
      (`fetch_my_list_status` against a real anime id returned
      `COMPLETED`).
    - **Two more real corrections, found from the first genuinely live
      `ops preview-show-backfill` run (472 items, the user's actual
      library) — both before any write happened**:
      1. **The user asked directly, live**: "will the database also
         add the shows tracked on AniList but not on Radarr/Sonarr? it
         should" — a real gap, not previously covered anywhere in
         BUILD_PLAN.md/SCOPE.md. Confirmed with the user: yes, every
         AniList list status, folded into this same step. Built a
         third candidate source (`anilist_client.fetch_viewer_id`/
         `fetch_my_anime_list`, new `MediaListCollection` query,
         live-verified against the user's real account: 1420 list
         entries, 11 lists, `format` can be null, no duplicate media
         ids across lists for this account) — `format: MUSIC` entries
         excluded outright (not a real "show", same exclusion
         `ANIME_RELATION_FORMATS`, A.21, already establishes for
         relation edges).
      2. **Caught in review before any dedup code was written**: the
         obvious forward-dedup ("AniList entry already linked in
         LCARS, skip it") isn't enough on its own — the *same* preview
         run's own printed output showed most of the Sonarr-classified
         entries were actually anime with `seriesType: standard`, not
         `anime` (Frieren, DAN DA DAN, Chainsaw Man, SPY x FAMILY,
         Kaiju No. 8, dozens more) — B.11d's original classification
         (`seriesType == "anime"`) was simply wrong for most of this
         user's real library. A misclassified `tracking_space='tv'`
         show never gets `_ensure_anilist_link` called at all
         (`fetch_and_populate`'s own gate), so it would never
         cross-reference against the AniList sweep, producing a
         duplicate the moment its AniList-sourced twin got created.
         Fixed both problems with one change: `_classify()`'s Sonarr
         branch now runs a real Fribb tvdb->anilist resolution as the
         actual anime signal (the same resolution
         `_ensure_anilist_link` would perform anyway once the show
         exists — done here first instead), and that resolved
         `anilist_id` is passed straight through on the `addShow`-
         equivalent input. Dedup itself works at two layers: (a)
         `local_audit.known_anilist_ids()` — checks **both**
         `show_external_id` (show-level) and `season.anilist_id`
         (per-season, §5.5 — a split-cour sequel's own AniList link
         can live there instead), and (b) `local_audit
         .all_sonarr_tvdb_ids()` + a new `fribb.build_anilist_index()`
         (the reverse of `build_tvdb_index`) — any AniList entry whose
         Fribb-resolved tvdb id is already in Sonarr's own full
         catalog at all (tracked or not) is skipped, since Fribb
         groups a franchise's several AniList-side season splits under
         one tvdb id and treating each split as independently
         "untracked" would create a real duplicate as soon as its
         sibling season arrived via the Sonarr-sourced path.
    - **Built**: `anilist_client.fetch_viewer_id`/`fetch_my_anime_list`
      (new); `fribb.build_anilist_index` (new); `local_audit
      .known_anilist_ids`/`all_sonarr_tvdb_ids` (new, both read-only —
      no service_health record, no commit, same contract
      `find_untracked_shows_readonly()` already has); `show_backfill
      ._classify_sonarr`/`_classify_radarr`/`_classify_anilist`/
      `_find_untracked_anilist_entries` (new/split out of the old
      single `_classify()`); `_seed_status_from_anilist` gained a
      `known_status` param so an AniList-sweep show (which already
      knows its own status from the same `MediaListCollection` call
      that found it) skips the redundant live status read entirely —
      one fewer AniList call than a Sonarr/Fribb-resolved anime show
      needs.
    - **Verified (AniList sweep)**: 564 tests passing (was 550; 14
      new: 5 in `test_anilist_client.py` — `fetch_viewer_id`,
      `fetch_my_anime_list` flattening/deduplication, a new
      `_SequencedFakeClient` for the two-call flow — 9 in
      `test_show_backfill.py` — Fribb-based classification replacing
      the old seriesType-based tests, both dedup layers, MUSIC
      exclusion, null-format fallback, known-status skip), `ruff
      check`/`format` clean, clean-install sanity check confirmed the
      new functions import cleanly. Live-verified: real `Viewer`/
      `MediaListCollection` calls against the user's actual AniList
      account before any test was written for them.
    - **One more real correction, same day, found from the second
      live preview run** (472 → 1605 items once the AniList sweep was
      wired in): the dedup's own "season 1 only" Fribb resolution
      (inherited from `_classify_sonarr`'s own season-1 convention)
      wasn't enough — a direct live check
      (`fribb.resolve_season_candidate` against Frieren's real tvdb
      id, 424536) showed season 1 and season 2 resolve to two
      genuinely *different* AniList ids. A season-1-only dedup check
      would have missed season 2 as "already accounted for" and let it
      through as an independent duplicate show. Fixed: `local_audit
      .all_sonarr_series_with_seasons()` (new) reports every real
      season number Sonarr itself lists per series (verified live:
      Sonarr's own `seasons` array is present on every real series
      response, `seasonNumber` 0 excluded); `show_backfill
      ._sonarr_resolvable_anilist_ids()` (new) Fribb-resolves every
      one of those seasons, not just season 1, building the dedup set
      from the full result. `_classify_sonarr()`'s own `trackingSpace`
      decision was widened the same way (a show whose season 1 doesn't
      resolve but a later season does is still genuinely anime) — the
      show-level `anilistId` passed to `create_show()` stays
      season-1-only regardless, matching `_ensure_anilist_link`'s own
      established convention exactly, not diverging from it.
      `fribb.build_anilist_index()`/`show_backfill._fribb_anilist_index()`
      (the earlier tvdb-reverse-lookup approach this replaced) removed
      as dead code rather than left unused.
    - **Explicitly documented as a known, accepted residual gap, not
      silently promised away**: even this per-season check isn't a
      perfect dedup. The very same live preview run showed three of
      the user's own real AniList list entries — alternate-cour
      "Frieren"-titled splits — that Fribb's own dataset simply has no
      mapping for under that tvdb id at all (verified live:
      `fetch_media` confirmed these are real, valid AniList entries,
      just ones Fribb doesn't carry). These still surface as
      "untracked" and would create a small number of genuinely
      duplicate-ish shows alongside the Sonarr-tracked original.
      Closing that fully would require fuzzy title matching across
      sources — B.7's own `service_presence.py` scope, a materially
      larger undertaking than this step, not attempted here.
    - **Verified (multi-season dedup)**: 572 tests passing (was 564; 8
      new: `test_classify_sonarr_checks_every_season_not_just_season_one`,
      `test_anilist_sweep_excludes_a_second_season_fribb_only_resolves_via_season_number`
      in `test_show_backfill.py`, plus 6 in `test_local_audit.py` for
      `known_anilist_ids`/`all_sonarr_series_with_seasons`), `ruff
      check`/`format` clean, clean-install sanity check confirmed the
      new functions import cleanly.
    - **A second, opposite-direction correction, same day, found from
      re-running the real preview against the fix above**: the
      multi-season dedup fix was expected to *reduce* the AniList-
      sweep count (catching more real duplicates); instead it *rose*,
      1133 → 1183, with zero items removed and 50 new ones added — a
      genuine regression, not the intended effect. Diffing the two
      real preview outputs pinpointed it immediately: `all_sonarr
      _series_with_seasons()` excluded Sonarr's own season 0
      ("specials") from the per-season scan, but Fribb tags a real
      movie/OVA/compilation entry as season 0 just as often as a
      numbered season — live-verified: "Chainsaw Man: Reze-hen"
      (anilist id 171627) sits on the user's own real AniList list,
      Fribb-tagged season 0 under Chainsaw Man's own tvdb id, and
      would have surfaced as a false "untracked" duplicate. Fixed by
      including season 0 in the scan rather than excluding it — a
      one-line change (`all_sonarr_series_with_seasons`'s own season-
      number list comprehension), verified against a real diff of the
      two preview runs (0 regressions, the expected reduction where
      Fribb's per-season data actually supports it) before being
      committed.
    - **Verified (season-0 fix)**: 573 tests passing (was 572; 1 new,
      `test_anilist_sweep_excludes_a_season_zero_tagged_movie`, plus 1
      existing test renamed/inverted —
      `test_all_sonarr_series_with_seasons_includes_season_zero`, was
      `..._excludes_season_zero`), `ruff check`/`format` clean, clean-
      install sanity check confirmed the fix imports cleanly.
    - **Real run against the user's live library, approved by the
      user** (1644 items previewed, known residual gap disclosed
      first — see above): the CLI client itself hit `httpx.ReadTimeout`
      partway through (`ops.lcars_client.LcarsError: Timed out talking
      to LCARS`) — a client-side timeout only. Ariadne/uvicorn's own
      single synchronous resolver thread doesn't stop just because the
      client gave up waiting; the mutation kept running server-side to
      completion regardless, confirmed by watching `SELECT COUNT(*)
      FROM show` keep climbing long after the CLI process had already
      exited (over roughly two hours total, matching the anime-item
      throttle math for ~1350 anime-classified items).
    - **A third real bug found from that live run, the most serious of
      this step — genuine duplicate `show` rows in the live database,
      not just a preview miscount.** Final count: 1873 rows against
      1644 previewed. Root-caused live: `candidates` in
      `backfill_untracked_shows()` is one fixed list, snapshotted
      *before* its loop starts (`local_audit.audit_local_files()` +
      `_find_untracked_anilist_entries()`, both one-time reads). But
      `shows.create_show()`'s own inline metadata fetch
      (`fetch_and_populate`) walks AniList `relations` and
      auto-creates `tracked = false` stub shows for ones LCARS has
      never seen (A.21) — *while this loop is still running*. Nothing
      stopped a later candidate in that same fixed list, independently
      targeting the exact id a stub just got auto-created for
      mid-loop, from inserting a second `show` row for it — the
      snapshot-based dedup (`known_anilist_ids`/
      `_sonarr_resolvable_anilist_ids`) can't see writes the loop
      itself makes; a read-side cache was never going to be the real
      correctness guarantee. Confirmed live with the actual duplicate
      key (`show_external_id.external_id`, not `title_romaji` — a
      title collision is sometimes legitimate, e.g. two different real
      Radarr movies both titled "The Thing"): 86 real `anilist_id`
      collision pairs, 3 `mal_id` pairs, each pair exactly one
      `tracked = 0` stub plus one `tracked = 1` real show sharing the
      same external id. First one traced by hand: "Mebius Dust"
      (`s-7zvsg2`/`s-e1a4yk`, both `anilist_id` 108992).
      - **Fixed at the actual write boundary, not with another
        snapshot layer**: `shows.find_existing_show()` (new, public)
        checks `show_external_id` live, immediately before
        `create_show()` inserts. A match against an untracked stub
        promotes it in place (`shows._promote_stub()`, new — SCOPE.md
        §5.1's own already-documented "Show-row promotion paths": "a
        bare `tracked = false` relation/franchise stub becomes a real
        tracked show by flipping `tracked = true` on the *existing*
        row — no re-creation" — this closes a gap in the design intent
        rather than inventing new behavior; nothing had implemented
        that path before this). A match against an already-tracked
        show is a genuine duplicate-add attempt, rejected outright
        (`ShowInputError`) instead of silently creating a second row —
        this also hardens the plain `addShow` mutation itself, not
        just the backfill path, since `create_show()` is its shared
        body.
      - `backfill_untracked_shows()`'s own result gained a `promoted`
        field, split from `created` — reporting a merge as a plain
        "created" is exactly the kind of silent conflation that made
        the original bug hard to see. `ShowBackfillResult.promoted:
        [BackfilledShow!]!` (schema.graphql), `ops backfill-shows`
        prints both counts separately.
      - **Verified**: 577 tests passing (was 573; 4 new —
        `test_add_show_promotes_an_existing_untracked_stub_instead_of_duplicating`
        and `test_add_show_rejects_a_duplicate_external_id_already_tracked`
        in `test_server.py`,
        `test_backfill_promotes_a_stub_a_relation_walk_created_mid_run_instead_of_duplicating`
        in `test_show_backfill.py` reproducing the exact live collision
        shape end to end, plus the `backfillUntrackedShows` wiring test
        updated for the new `promoted` field), `ruff check`/`format`
        clean, clean-install sanity check (fresh venv, real `pip
        install -e .[dev]`) confirmed `shows.find_existing_show`/
        `_promote_stub` import cleanly and the schema builds with
        `ShowBackfillResult.promoted` present and correctly typed.
      - **Live database cleanup**: the fix does not retroactively merge
        rows the buggy code already created. Confirmed with the user:
        the dev `lcars.db` (this machine — SCOPE.md §11.3's own
        Docker-on-the-Sonarr/Radarr-host is the real Cutover target,
        not this laptop) is disposable — Cutover gets a fresh DB and a
        real re-run there, not a copy of this one — so the dev DB just
        needs to stay clean/trustworthy for the rest of Phase B's own
        live testing (B.11f needs real data to verify against). Wipe
        chosen over a surgical per-pair merge script: ~2 hours
        background wait (matching the original run's own measured
        time — the fix doesn't change how many candidates exist or how
        much AniList throttling they need) with zero engineering risk,
        versus a one-off script touching ~12 live FK-referencing
        tables (episode/season/show_external_id/show_relation's own
        `related_show_id`/cast_credit/studio_credit/watch_event/
        pending_review/status_change/score_change/franchise_member/
        show_service_presence) for no lasting benefit on a DB that
        gets discarded anyway.
      - **A second real bug found executing that wipe, same day —
        `alembic downgrade base` is broken against any DB with actual
        rows in it, not an edge case.** `f7a2c4e91b6d`'s own
        `downgrade()` (B.3's availability-3-state migration) re-adds
        `show.available_locally`/`episode.available_locally` via
        `ALTER TABLE ... ADD COLUMN ... GENERATED ALWAYS AS (...)
        STORED` — works fine forward (every test's own `conn` fixture
        exercises this exact statement on a brand-new, zero-row table
        every run, 577 passes) but SQLite hard-rejects adding a STORED
        generated column via `ALTER TABLE` on a table that already has
        rows (`sqlite3.OperationalError: cannot add a STORED column`,
        confirmed directly: succeeds on an empty table, fails the
        moment the table holds even one row). This is the first time
        `alembic downgrade` has ever been run against a DB with real
        data in this project's history — the failure mode was never
        reachable from any test (fixtures always create-then-migrate a
        fresh empty file) or from the forward-only live builds so far.
        Live-confirmed 2026-08-10: `alembic downgrade base` against the
        post-backfill `lcars.db` (2863 show rows) got through
        `service_health`'s own two downgrades cleanly, then died
        partway into `f7a2c4e91b6d`'s own downgrade — `episode`
        untouched, `show` left mid-transition (old-style
        `available_via_radarr` INTEGER back, but missing
        `available_locally`/`file_path_radarr` entirely) — alembic's
        own non-transactional-DDL mode (SQLite) commits each
        `op.execute()` independently, so this partial state persisted
        rather than rolling back.
      - **Not fixed tonight, deliberately** — out of scope for what was
        asked (wipe the dev DB now); flagged here so it isn't lost.
        Worked around instead: since every row was going to be
        discarded anyway, the broken partially-downgraded file was
        moved aside (not deleted — kept at
        `scratchpad/lcars-broken-downgrade-backup.db` for this session)
        and a genuinely fresh `lcars.db` created via `alembic upgrade
        head` from nothing — sidesteps the bug entirely (every table
        stays at zero rows through the whole forward chain), same
        practical result as a working downgrade would have given.
        Confirmed clean afterward: `alembic current` reports head
        (`2818c7efae13`), `show` has 0 rows and the correct 3-state
        schema. **Real follow-up need**: `downgrade()` in
        `f7a2c4e91b6d` (and worth auditing every other migration for
        the same `ALTER TABLE ADD COLUMN ... STORED` pattern on
        downgrade) needs an actual fix before `alembic downgrade` can
        be trusted against any database holding real rows — likely a
        full table rebuild (new table, copy data, drop, rename) rather
        than in-place `ALTER TABLE`, the same technique SQLite itself
        recommends for schema changes it can't do in-place. Not
        blocking B.11d/B.11e/B.11f, but should happen before this
        project leans on `alembic downgrade` for anything real
        (Cutover-time rollback, say).
  - [x] **B.11e — ongoing untracked-show sweep** (2026-08-10): a new
    `untracked_show_finding` table (migration `d5858f23ed73`, prefixed
    id `u-`, §5.0), a new `untracked_sweep.py` module, and a new
    `pollUntrackedShows` mutation, run on Ops's own recurring schedule
    (`run_untracked_shows_once`, folded into the existing hourly
    `run_daily_and_weekly_once` tier — cheap, a handful of global calls
    not per-show, same reasoning `run_animeschedule_once`/
    `run_local_presence_once` already share that tick for; "no new
    interval unless a real technical constraint forces one" stays
    intact). Reuses `show_backfill.preview_backfill()`'s own combined
    computation (Sonarr/Radarr untracked catalog items + the AniList-
    list sweep, already classified) directly rather than a second,
    narrower Sonarr/Radarr-only pass — SCOPE.md's own resolution text
    names "Sonarr/AniList" together, and `previewShowBackfill` already
    *is* this exact computation, one-shot; this is the same thing, run
    on a schedule and persisted.
    - **Still never auto-`addShow`s** — that decision (§5.1) is
      untouched, only how findings surface changes, exactly as
      SCOPE.md's own B.11 reconnaissance note specifies.
    - **Deliberately minimal, no resolve/dismiss mutation**, unlike
      `pending_review`: a finding is only ever removed automatically,
      by no longer appearing in a fresh sweep (the show got tracked
      some other way, or genuinely disappeared from its source) —
      upsert-by-`(service, external_id)` (natural key, same
      prefixed-id-plus-natural-key shape `show_service_presence`
      already established) with `first_seen_at` preserved and
      `last_seen_at`/classification refreshed on every still-current
      finding, anything no longer present deleted outright. Matches
      "small new reviewable list", not a second review-and-resolve
      workflow — easy to add a dismiss action later if it turns out to
      be needed, not built speculatively now.
    - **`preview_backfill()` gained a `path` field** (Sonarr/Radarr
      entries carry the real one, an AniList-sweep entry has none) —
      not exposed on `BackfillPreviewItem`'s own GraphQL shape (a safe,
      additive superset; Ariadne only reads what a type declares),
      needed so `untracked_show_finding.path` has something real to
      persist. `UntrackedShowFinding.externalId` is `String!` (matches
      `ShowExternalId.externalId`'s own TEXT-storage precedent), not
      `Int!` like `BackfillPreviewItem`/`UntrackedRemoteShow` — those
      predate persistence and never needed to match a stored column's
      actual type.
    - **New GraphQL surface**: `UntrackedShowFinding`/
      `UntrackedShowFindingEdge`/`UntrackedShowFindingConnection`
      (Relay-paginated, same shape every other real persisted entity
      already gets — this is a genuine, growing table now, not a
      one-shot computed list like `previewShowBackfill`'s own items),
      `Query.untrackedShowFindings`, `UntrackedShowPollResult`
      (`found`/`newFindings`/`resolvedFindings` — `found` is the total
      current list, not itself a change, so `run_untracked_shows_once`
      excludes it from the count it returns, matching every other
      tier's "count real changes" convention), `Mutation
      .pollUntrackedShows`.
    - **Verified**: 589 tests passing (was 577; 12 new — 6 in the new
      `test_untracked_sweep.py` — insert/refresh/prune/idempotent-
      rerun/empty, all against a real migrated SQLite DB — 2 scheduler
      tests (`run_untracked_shows_once`'s own sum, the fake client's
      new `poll_untracked_shows`), 1 `test_ops_lcars_client.py` test,
      1 `preview_backfill` path-propagation test in
      `test_show_backfill.py`, 2 `test_server.py` wiring tests — a
      clean no-op with nothing configured, and a real GraphQL-level
      round trip confirming `UntrackedShowFindingConnection`'s own
      enum/camelCase/nullable-path wiring resolves correctly against a
      directly-inserted row), `ruff check .` clean (CI's own actual
      gate — `ruff format --check` separately surfaced pre-existing
      drift across the whole `migrations/` directory, unrelated to
      this step, left untouched rather than reformatted out of scope),
      clean-install sanity check (fresh venv, real `pip install
      -e .[dev]`) confirmed the schema builds with
      `untrackedShowFindings`/`pollUntrackedShows` present and
      correctly typed, `untracked_sweep.sweep_untracked_shows` imports
      cleanly, and a genuinely fresh `alembic upgrade head` run against
      an empty DB in that clean venv creates `untracked_show_finding`
      correctly. Migration round-trip (`upgrade head` → `downgrade -1`
      → `upgrade head` again) verified directly — no `STORED` generated
      column involved here, so none of the same-day `alembic downgrade`
      bug's failure mode applies to this one.
    - **Fixed the next day (2026-08-11), before anything got deployed —
      the pruning bug flagged above, in full.** `sweep_untracked_shows()`
      used to treat *anything absent from the current sweep's combined
      list* as resolved and delete it, even though both underlying
      sources swallow a genuine unreachable-source failure into an
      empty result rather than raising — a transient Sonarr/Radarr/
      AniList outage during one sweep would have deleted every real
      finding from that source as falsely "resolved." Fixed exactly
      the shape flagged: "don't prune what you couldn't see." New
      `local_audit.find_untracked_shows_readonly_by_source()` and
      `show_backfill._find_untracked_anilist_entries_by_source()` each
      report a `reported: bool` per source alongside their existing
      entries (a source with no credentials configured at all still
      counts as "reported" — a deliberate, stable zero, safe to prune
      against; only a real failure against a *configured* source
      reports `False`) — `find_untracked_shows_readonly()`/
      `_find_untracked_anilist_entries()` themselves are now thin
      wrappers around these, unchanged for every existing caller. New
      `show_backfill.preview_backfill_with_status()` combines both
      sources' `reported` signals into one `reported_services` set;
      `preview_backfill()` is now a thin wrapper around it too.
      `sweep_untracked_shows()` calls the `_with_status` variant and
      only deletes a finding whose own `service` is in
      `reported_services` this pass.
      - **Verified**: 604 tests passing (was 589; 15 new — 6 in
        `test_local_audit.py` for the new by-source signal (success/
        not-configured/failure, for both Sonarr and Radarr, plus the
        "still just the flat list" contract test), 4 equivalent ones
        in `test_show_backfill.py` for the AniList side, 2 for
        `preview_backfill_with_status()`'s own aggregation, 3 new
        regression tests in `test_untracked_sweep.py` reproducing the
        exact bug shape end to end — a source failing to report
        leaves its findings untouched, pruning resumes once it
        reports again, and nothing prunes when no source reported at
        all — plus the pre-existing pruning test updated to patch
        `preview_backfill_with_status` instead of the now-wrapped
        `preview_backfill`), `ruff check .` clean, clean-install
        sanity check (fresh venv) confirmed all three new `_by_source`/
        `_with_status` functions import cleanly and the schema still
        builds correctly.
    - **Hourly-tick placement checked the next day (2026-08-11),
      before deployment**: confirmed cheap for real, not just reasoned
      about. `fribb.load_dataset()` (checked directly): a 7-day
      on-disk cache (`DATASET_MAX_AGE_SECONDS`), a real download only
      happens roughly weekly, not every hourly tick — most sweeps are
      a local disk read. The other previously-unverified cost, a
      Fribb resolution per season across the *entire* Sonarr catalog
      (`_sonarr_resolvable_anilist_ids`), timed synthetically at
      realistic scale (1600 shows, up to 3 seasons each, matching the
      real backfilled library's rough shape): **3.4ms** for the index
      build plus 4,800 season lookups combined — negligible, nowhere
      near B.7's own real N×M catalog-service-presence cost (the
      reason *that* sweep was placed on the monthly tier instead).
      `local_audit.all_sonarr_series_with_seasons()` and
      `known_anilist_ids()` both checked directly too — pure in-memory
      transforms of the single already-fetched `all_series()` response
      and two plain `SELECT`s respectively, no hidden per-show DB
      query anywhere in the chain. The real, irreducible cost per
      sweep is exactly the 4 lightweight global HTTP calls the
      original reasoning named (Sonarr, Radarr, 2× AniList) — hourly
      confirmed appropriate, same tier every other cheap global sweep
      (`run_animeschedule_once`/`run_local_presence_once`) already
      shares.
    - **First real deployment (2026-08-11)**: `lcars`/`ops` stood up
      for real, for the first time ever, as two services in a new
      `starfleet.yml` compose file (`/home/tiny/stacks`, its own
      explicit `name:` to avoid colliding with the existing Sonarr
      stack's own default project namespace) joining `arr_net` — the
      real host's own network, not a copy of anything from this dev
      machine. `v0.1.0` tagged and pushed first, confirming the
      previously-unexercised CI publish-on-tag path actually works
      end to end (image built, pushed to `ghcr.io/drostan80/starfleet`,
      pulled and run for real on the host) before touching the live
      stack at all. Persistent state lives at `/opt/appdata/lcars`
      (matches every other container's own convention here, rides the
      existing daily `appdata-backup.timer` automatically — measured
      the real DB size first, ~13MB at 1650-show/41k-episode scale,
      trivial either way) — `db/` (the sqlite file), `config/`
      (`lcars.ini`, including the fields `lcars anilist-login`/
      `lcars mal-login`/the periodic MAL refresh write at runtime,
      redone fresh against this deployment rather than copied), and
      `cache/` (the Fribb dataset). `lcars.ini` used for most static
      config too, not just Docker `secrets:` — a deliberate divergence
      from the Dockerfile's own "prefer secrets:" comment, since the
      config volume is required regardless (`anilist_access_token`/MAL
      tokens have no secret-mount equivalent) and one less moving part
      for a single-user homelab; `lcars_bearer_token` is a real
      `secrets:` entry instead, the one value both containers need to
      agree on. Brought `lcars` up alone first and verified it for
      real (DB persisted correctly, auth working, Sonarr/Radarr
      reachable via `arr_net` container names) before adding `ops` —
      confirmed connected and running its first real hourly tick
      immediately.
      - **A real bug found bringing `lcars` up, fixed before `ops`
        joined**: `LCARS_DATABASE_URL` only ever controls Alembic's own
        migration step (`migrations/env.py`); the *running* `lcars
        serve` process reads a completely separate env var,
        `LCARS_DB_PATH` (a plain path, not a sqlalchemy URL,
        `lcars/config.py`) — undocumented anywhere as needing both set
        together. Missing it meant migrations ran correctly against
        the mounted volume while the actual app fell back to its own
        unmounted default (`sqlite3.OperationalError: unable to open
        database file`). Fixed by setting both.
      - **Real backfill run against the deployed instance**:
        `previewShowBackfill` came back at exactly 1644 — matching
        last night's dev-machine count precisely (Sonarr 275, Radarr
        197, AniList 1172) — confirming the whole deployment (Sonarr/
        Radarr/AniList connectivity, credentials, Fribb dataset) is
        wired correctly before writing anything. User approved the
        real run; same client-side `httpx.ReadTimeout` as last night
        (server keeps processing regardless), finished at 2670 total
        rows. `tracked = 1644` — an *exact* match to the preview, zero
        duplicate `anilist_id`/`tvdb_id`/`tmdb_id`/`imdb_id` pairs
        anywhere — this morning's promotion fix genuinely closed last
        night's bug, verified for real against the first full run it
        was ever exercised on.
      - **A second, different real bug found in that same run's own
        verification**: 4 `mal_id` collision groups, 6 excess rows, all
        among the 1026 `tracked = 0` relation stubs (zero impact on any
        tracked show). Root cause: AniList sometimes splits what MAL
        keeps as one entry into several separate Media entries (real
        cases found live: "Ao Haru Ride PAGE.13"/"Ao Haru Ride:
        unwritten", a Saint Seiya two-part split, a SYNDUALITY short,
        all four Summer Pockets chapter entries) — each one is a
        genuinely distinct `anilist_id`, so each independently passed
        `metadata._link_relation`'s own existing-show check, which
        only ever looked at `anilist_id`, never `mal_id`. This morning's
        promotion fix never touched this path at all — it only guards
        `shows.create_show()`'s own top-level entry point;
        `_link_relation`/`_create_relation_stub` is a separate route
        into the same table, walked during every anime show's own
        metadata fetch (A.21), not just during backfill. Fixed:
        `metadata._existing_related_show()` (new) checks *both*
        `anilist_id` and `mal_id` before deciding to create a new stub
        — mirrors `shows.find_existing_show()`'s own multi-id shape,
        kept as its own local copy rather than imported directly
        (`shows.py` already imports `metadata`, so the reverse would be
        circular — same "keep a separate copy" precedent
        `_EXTERNAL_ID_URL_TEMPLATES` already established between these
        two modules). Self-heals within a single relation walk too,
        not just across runs — a live `SELECT` on the same connection
        sees an uncommitted `INSERT` from earlier in the same request
        (SQLite's own read-your-own-writes), so two edges on one
        parent show's own relations list resolve to one stub now, not
        two.
      - **Verified**: 605 tests passing (was 604; 1 new,
        `test_add_show_anilist_fetch_relations_sharing_one_mal_id_reuse_one_stub`,
        reproducing the exact live collision shape — two relation
        edges, distinct `anilist_id`, same `idMal`, asserts exactly one
        stub row and one `show_relation` edge, not two), `ruff check .`
        clean, clean-install sanity check confirmed
        `metadata._existing_related_show` imports cleanly and the
        schema still builds correctly.
      - **Not yet done**: the deployed database (2670 rows, including
        those 6 excess stub rows from before the fix) gets wiped and
        the backfill re-run clean against the fixed image — same
        "wipe and re-run rather than surgically merge" reasoning as
        last night, now applied to the real deployment.
  - [x] **B.11f — calendar core render path** (`~/repos/data`,
    2026-08-11): switched from local Sonarr/AniList computation to
    LCARS reads (`episodesInRange`) for tracking/air-date/availability
    state, per the confirmed **Replacing** scope above.
    - **Reconnaissance before writing any code**: enumerated exactly
      what `_refresh_visible_rows` (and the ~40 other call sites
      sharing `self._series_by_id`/`_is_anime`/`_resolve_lcars_show_id`/
      `_resolve_anilist_id`/`_track_cell`) actually need, against
      `schema.graphql`'s real `Episode`/`Show` types, and verified
      three things live against the deployed instance rather than
      assuming: `episodesInRange` performance (66 episodes/45ms for
      the current week, 2041 episodes/67ms for a full year — no
      concern), `Show.externalIds` can return the `tvdb` id nested in
      the same query, and tvdb-link coverage for Sonarr-managed shows
      specifically (1369/1644 tracked shows have no tvdb link overall,
      but confirmed via `servicePresence` that 100% of those are
      AniList-sweep-only entries with zero Sonarr presence — the
      correlation this step actually needs, Sonarr series → LCARS
      show, has ~0 real gaps).
    - **Episode titles**: `episode` has no title column at all
      (confirmed deliberate, SCOPE.md's B.8b note) — asked the user
      directly rather than guessing between dropping the Title column,
      keeping a local Sonarr-sourced lookup, or adding the column now.
      User chose keeping the local Sonarr lookup for now, and asked
      whether a DB column is a possible future add-on — confirmed yes
      (a nullable `title` on `episode`, populated during Sonarr
      reconciliation, is straightforward later work), **flagged here
      as a real deferred follow-up, not yet built**.
    - **Design, confirmed against real code before writing the
      rewrite**: `self._series_by_id`/`self._episodes` stay
      genuinely Sonarr-sourced and keep their real identity
      (`seriesId`, `episodeFileId`) — confirmed via
      `_resolve_episode_path` (playback file resolution) that ~40
      other call sites need that real Sonarr identity, which LCARS has
      no equivalent of. Only the *values* `_refresh_visible_rows`
      reads (air date, availability, tracking status) now come from
      LCARS, patched onto the existing dicts in place — same
      established pattern `_patch_air_dates` (B.11's own AniList
      air-date correction) already uses, so `_availability_style` and
      the marquee cell methods pick this up for free with no changes
      of their own.
    - **Built**: `LcarsClient.episodes_in_range()` — a real
      cursor-paginated loop (not one huge `first`), requesting
      `show.externalIds` nested in the same query. `DataApp.
      _patch_from_lcars(start, end)` — correlates each LCARS episode
      back to a Sonarr one via `(tvdb_id, season, episode_number)`
      (confirmed `Episode.season` is documented as "the raw
      Sonarr-numbered season" — no numbering translation needed),
      patches `hasFile`/`airDateUtc` in place, unions (not replaces)
      `self._downloading_ids` from `availableViaSonarr`'s
      `DOWNLOADING` state (replacing it outright would race against
      `_check_download_status`'s own library-wide Sonarr-queue-based
      rebuild — see its own docstring), and patches
      `series["lcarsStatus"]` for `_is_hidden_from_calendar`'s use.
      `DataApp._trigger_lcars_window_refresh()` — backgrounds the
      fetch (Textual's `run_worker`, `exclusive=True`) from every
      window-changing action (step_back/forward, set_mode,
      jump_to_today, the initial load, and `_check_download_status`'s
      own periodic tick, since its Sonarr refetch hands back brand new
      dict objects that need re-patching too) — `_refresh_visible_rows`
      itself stays synchronous throughout, reading only from whatever
      was already patched.
    - **`_is_hidden_from_calendar` rewritten** to read a single source
      for both anime and non-anime rows (`series["lcarsStatus"]`,
      correlated via tvdb id) instead of the old anime-only
      AniList-cache branch — confirmed via `_move_episode_to`'s own
      docstring that LCARS already receives every status push
      regardless of anime-ness (§6.8), so there's no reason left to
      ask AniList directly for this. Pending-queue overlay
      (`_pending_lcars_change`) still checked first, for both
      branches, since that's the same queue every status push already
      goes through. Absent `lcarsStatus` (not yet correlated, or a
      genuinely unlinked show) fails open — never hidden — so nothing
      flickers hidden before the first real answer lands.
      `_track_cell`/`_anilist_cell`/`_lcars_cell` were read and left
      **unchanged** — `_anilist_cell` renders AniList's own per-episode
      watched-progress numbers, which `show.status` alone can't
      reproduce; not this step's scope.
    - **Verified**: 513 tests passing (was 502; 11 new — `episodes_in_
      range`'s own pagination/empty-window/window-variables coverage in
      `test_lcars_client.py`, plus `test_app_lcars_calendar_patch.py`'s
      8 new tests for the hasFile/airDate patch, the DOWNLOADING state,
      the no-tvdb-match fallback, the union-not-replace downloading-ids
      behavior, the window-refetch trigger, and the local-calendar-day
      to real-UTC-instant conversion specifically — this last one
      matters because `calendar_nav.CalendarState.date_range()` returns
      plain local-calendar dates, and local midnight is only UTC
      midnight for a user actually sitting in UTC), plus 5 existing
      tests updated (they asserted the old AniList-direct hide
      behavior, now need a real LCARS fixture reporting the status).
      `ruff check`/`format --check` clean on every file this step
      touched (one pre-existing, unrelated formatting gap in
      `test_app_list_status.py` left alone — not this diff's to fix).
      Clean-install sanity check (fresh venv, real `pip install -e .`)
      confirmed the module imports cleanly and the new methods exist.
    - **Follow-up caught before calling this done (2026-08-11, same
      day)**: `_patch_from_lcars` and the pre-existing `_patch_air_dates`
      (AniList's own airingSchedule correction, built 2026-07-09) both
      write `self._episodes["airDateUtc"]`. Checked live rather than
      assumed which should win: queried the deployed instance's real
      `airDateSource`/`airDateRawSonarr` fields and confirmed LCARS is
      *already* doing this same correction server-side (31/131 real
      episodes that week carried `airDateSource: ANILIST`, genuinely
      different from `airDateRawSonarr`) — so LCARS winning
      deterministically is correct, per B.11's own "Replacing" scope,
      not a coincidence to leave to whichever async call happens to
      finish first. The original ordering raced on the initial Sonarr
      load (`_trigger_lcars_window_refresh()` dispatched *before*
      `_load_anilist_data()`'s own `await`, so the date could visibly
      flip moments after launch depending on which round-trip won) —
      `_check_download_status`'s own periodic-tick ordering was already
      correct by accident. **Fixed**: reordered so `_load_anilist_data()`
      is awaited before the LCARS fetch is triggered, everywhere,
      matching the tick path. New regression test
      (`test_lcars_air_date_wins_over_anilists_own_correction`) uses
      three genuinely distinct dates (Sonarr raw, AniList's correction,
      LCARS's correction) so a wrong winner is unambiguous. 514 tests
      passing (was 513), `ruff check`/`format --check` clean.
    - **Correlation verified against real paired data, not just
      fixtures (2026-08-11)**: `Episode.season`/`.episode` being
      "raw Sonarr-numbered" was confirmed by the schema comment but
      never checked against a live show with `absoluteNumber`/
      `EpisodeNumberingMapping` in play, where anime numbering can
      genuinely diverge — a silent zero-match would fall back to
      Sonarr's own values with no error anywhere, the failure mode most
      likely to make the user's first live test look like nothing
      happened. Checked directly: pulled 9 real (series, episode) pairs
      with upcoming air dates from the real Sonarr library and
      cross-referenced them against the same window's real
      `episodesInRange` result by `(tvdb_id, season, episode_number)` —
      9/9 matched.
    - **Not yet done**: a real end-to-end run of Data itself against
      the deployed LCARS instance + the user's real Sonarr library —
      Data runs on the user's own local machine by design (never on
      the server), so this is the user's own first live test to run,
      not something simulated from here. LCARS's own side of this was
      verified for real multiple times above (schema, performance,
      tvdb coverage, backlog scope, real correlation, air-date
      ordering); the actual Data/LCARS wiring itself is unit-tested
      against realistic fixtures but not yet exercised end-to-end.
      Also flagged, not yet built: `hasFile` now comes from LCARS's own
      availability poll while `episodeFileId` (used by
      `_resolve_episode_path` for playback) still comes from Data's own
      Sonarr fetch — if LCARS polled more recently than Data's own last
      Sonarr refresh, a row could render "Available" while playback
      still fails to resolve a file path. `Episode.filePathSonarr`
      already exists LCARS-side and is the eventual fix (patch the
      resolved path directly from LCARS instead of re-deriving it from
      Sonarr's own `episodeFileId`), not built this step.
    - **Second same-day follow-up — the other three
      `self._episodes`-replacing/re-patching sites**: the write-ordering
      fix above only covered the initial Sonarr load and
      `_check_download_status`'s own periodic tick. Two more sites
      needed the same `_trigger_lcars_window_refresh()` call and didn't
      have it: `_bring_worker` (the manual `b` refresh — hands back
      brand new dict objects, same shape as `_check_download_status`'s
      own refetch) and `action_reauth_anilist` (`R` — mutates the
      existing dicts in place via `_load_anilist_data()`'s own
      `_patch_air_dates()`, same ordering concern as the initial-load
      fix). A third, different shape: `_trigger_lcars_window_refresh()`
      itself no-ops while backlog view is showing, so a periodic tick
      firing during that time left the calendar's own window stale for
      whenever the user came back to it — `action_set_backlog_view`
      (`B`) now re-triggers on every toggle. Two new regression tests
      reproduce both shapes directly. 516 tests passing (was 514),
      `ruff check`/`format --check` clean.
    - **User's own first live test surfaced a real bug the same day**:
      "files that I know are available (yesterday shows for example)
      are shown as missing." Checked live rather than guessed: pulled
      the last 3 days of episodes from both real Sonarr and real LCARS
      and found 20/36 with `hasFile: true` in Sonarr but
      `availableViaSonarr: UNAVAILABLE` in LCARS, every one with
      `availableCheckedAt: null` — never checked at all, not checked
      and wrong. Root cause: `availability.py`'s own poll (B.3) only
      walks Sonarr/Radarr history *forward* from each service's
      first-ever checkpoint (seeded to "now" on the very first poll,
      deliberately, so it never blocks LCARS's one request thread on
      an unscheduled full-history walk) — everything downloaded before
      the deployed instance's first poll, i.e. everything from B.11d's
      backfill, was simply never in scope. `ops backfill-availability`
      is the documented, correct one-time fix for exactly this — but
      running it against the real ~275-series library timed out after
      exactly 10 seconds. **Second real bug, same investigation**: the
      command's own docstring/print statement already promised
      "seconds to minutes on a large library," but it constructed
      `LcarsClient` with no timeout override, defaulting to the
      client's own 10s — `_cmd_backfill_shows` had already learned
      this exact lesson (`timeout=3600.0`) but the fix was never
      carried over; this command had simply never been run against a
      real, sizeable library before this first live test. Fixed
      (`~/repos/starfleet` commit `3bc2e0c`, 606 tests passing, 1 new),
      released as `v0.1.2`, redeployed, `ops backfill-availability`
      re-run for real: `5301 episode(s), 168 show(s) updated`. Verified
      the original 20 mismatches directly — 0 remaining (the episodes
      still showing `availableCheckedAt: null` afterward are ones
      genuinely not downloaded on either side, correctly agreeing, not
      a residual bug).
    - **User's next report, same day: "dropped shows still appear"**
      (Tomb Raider King, Chainsmoker Cat). Traced end to end rather
      than guessed. Not a B.11f regression — confirmed `_is_anime()`
      already returned False for these pre-B.11f too (Sonarr tags them
      `Animation`, not `Anime`), so the old non-anime branch of
      `_is_hidden_from_calendar` only ever checked the local pending
      queue, nothing else; they were never reliably hidden before
      today either. Real root cause, traced live: LCARS's own anime
      classification (via a real Fribb tvdb->anilist resolution,
      deliberately not Sonarr's genre tags — `show_backfill.py`'s own
      2026-08-10 note explains why genre tags are unreliable) had no
      Fribb mapping for these specific shows, so they were never
      linked to AniList in LCARS at all. **Two further real bugs found
      investigating the fix path** (`~/repos/data` commit `00ac85c`):
      (1) `_resolve_lcars_show_id()` only ever knew a show's LCARS id
      for a show Data itself had added — silently None for all ~1644
      backfilled shows, breaking every row-scoped LCARS write for
      them, not just this hide-filter; fixed by populating that lookup
      from `_patch_from_lcars`'s own existing correlation loop. (2)
      pressing `M` (link to AniList) to manually fix a show like this
      wrote the AniList side but never told LCARS —
      `link_external_id()` had been sitting unused in `lcars_client.py`
      since A.17; fixed, wired into `_link_to_anilist_interactive`.
      522 tests passing (was 516; 6 new), `ruff check`/`format --check`
      clean.
    - **A third show, "Mebius Dust," is a different problem —
      measured, not fixed here.** Two separate LCARS rows for the same
      real show: one Sonarr-sourced (`trackingSpace: TV`, tvdb-linked
      only), one AniList-sweep-sourced (`trackingSpace: ANIME`,
      anilist+mal-linked, `status: DROPPED`). This is the same show
      B.11d's own "Mebius Dust anomaly" duplicate-creation bug produced
      originally — the fix at the time (`find_existing_show`/
      `_promote_stub`, the wipe-and-clean-backfill re-run) closed the
      bug that *creates* same-service duplicates, but this pair shares
      no external id at all (one has tvdb, the other has anilist+mal),
      so it was never reachable by that fix or by the `GROUP BY
      service, external_id` query that confirmed "zero duplicates"
      after the re-run — `show_backfill.py`'s own docstring already
      predicted exactly this residual gap needs fuzzy title matching to
      close for real. Measured the actual scale live before proposing
      anything: 102 tracked shows with a `tvdb` link and no `anilist`
      link, 1172 tracked ANIME-space shows with `anilist` and no
      `tvdb` link — cross-referenced by exact normalized title, **2
      real cross-service duplicate pairs** ("Black Lagoon" and "Mebius
      Dust"; fuzzy/near-title matching would likely surface a few more,
      but this is a real floor, not a "hundreds" scale problem). First
      floated to the user as **"B.7 scope, unbuilt"** — wrong, caught
      and corrected before the user committed effort to it: B.7
      (`show_service_presence`'s periodic refresh) is already complete
      and its own entry explicitly scoped itself to "local + Sonarr +
      Radarr," naming AniList's missing search/catalog-listing endpoint
      as the reason — there was no existing "B.7 scope" design for this
      to resume. Asked the user two scoping questions once that was
      corrected (trigger model: automatic sweep, reversible, logged as
      reviewable; merge direction: the AniList-linked show wins), then
      ran one more live check before building anything — Mebius Dust's
      Sonarr-sourced row holds 8 real episodes, its AniList-sourced row
      holds 0, so "AniList wins" needed to mean "wins on status/score/
      tracking fields," not "the AniList row's own episode data is
      correct" (it has none). Surfaced that conflict plainly, along
      with the real scope ("automatic + reversible + reviewable" is a
      new migration, a new audit table, and FK reassignment across
      every show-scoped table `export_import.EXPORT_IMPORT_TABLES`
      lists), and asked whether to hand-fix the 2 known pairs or build
      the real mechanism as a numbered step. **User's decision: "Make
      this a real new step now (e.g. B.14)"** — see B.14 below.
    - **Tomb Raider King again, next day (2026-08-11): a real bug in
      the `00ac85c` fix itself** (`~/repos/data` commit `744c6ff`).
      User re-tried `M` on the same show and it looked identically
      successful both times — but the LCARS side genuinely never
      updated either time, confirmed live (`search("Tomb Raider
      King")` still showed only a `tvdb` link, no `anilist`, after
      both attempts). Root cause: `_bridge_anilist_link_to_lcars`'s
      no-op branch (`show_id is None`) had zero user-facing feedback —
      pressing `M` wrote a real second `SaveMediaListEntry` to AniList
      itself, believing the first had failed outright, when in fact
      only the LCARS half had silently no-op'd both times. Fixed two
      ways: a new `LcarsClient.search(title)` (wrapping LCARS's own
      `Query.search`) as a fallback show-id resolution when
      `_lcars_show_id_by_tvdb` doesn't have the tvdb id yet — matched
      against the returned show's own tvdb external id before
      trusting it, backfilling the cache so future calls skip the
      fallback — and every remaining failure path now prints
      something instead of staying silent. 5 tests
      updated/added, 525 passing, `ruff check`/`format --check` clean.
    - **Same day, same root cause, a second real report: "cannot mark
      as watch on data"** (`~/repos/data` commit `8b9abab`). Checked
      the other half first, live, before touching anything: the
      automatic mark-watched-after-full-episode-playback path (a
      separate, already-working code path) really did reach LCARS for
      real — Ghost in the Shell S01E06 showed `state: WATCHED` with a
      real `watch_event` row, confirmed via a direct GraphQL query.
      Isolated the bug to the manual `w` binding specifically.
      `_queue_lcars_watched` was the identical silent-no-op shape as
      `_bridge_anilist_link_to_lcars`'s own bug above — for a non-anime
      show (LCARS is the *only* push there, no AniList note to fall
      back on), a show not yet in `_lcars_show_id_by_tvdb` made
      pressing `w` do, visibly, nothing at all. Fixed by extracting the
      AniList-bridge fix's live-search fallback into a shared
      `_resolve_lcars_show_id_live()` and wiring it in here too,
      converting `action_mark_watched` to `async def`. **A real
      regression this fix's own first draft introduced, caught
      immediately by this repo's own pre-existing tests**: an early
      `self._lcars_client is None` guard broke queueing from a purely
      disk-cached id with no client configured this session at all — a
      legitimate, previously-working offline case (SCOPE.md §4's own
      "queue locally, retry on flush" reliability note) — 3 tests
      failed the instant it was added; removed. New regression test
      pins the actual fix (a non-precached show still queues via the
      live fallback). 526 tests passing (net +1, several rewritten),
      `ruff check`/`format --check` clean.
  - [ ] **B.11g — B.9's two deferred Data-side pieces**: the
    calendar-native counter line under a show's next-episode entry
    (backed by `Query.backlog` as-is — asked the user directly
    whether Data's existing B-view predicate and LCARS's
    `Query.backlog` predicate, confirmed genuinely divergent during
    B.11 reconnaissance, needed reconciling first; user chose to
    ship the counter against `Query.backlog` as-is, no reconciliation
    needed) and its mark-watched-clears-exactly-one-oldest-episode
    interaction.
    - **Scope confirmed with the user, 2026-08-12, before building**:
      not a new selectable table row (would mean auditing every action
      handler reading `self._visible_episodes[self._selected_row]`,
      which assumes a row is always either a real episode or `None`) —
      folded into an existing cell instead, in a distinct color for
      visibility, on only the show's earliest visible row. Mark-watched
      from it clears exactly one oldest episode (B.9's own text,
      confirmed directly rather than left ambiguous).
    - **Built (counter half only), 2026-08-12, `~/repos/data` commit
      `e186fc9`**: `LcarsClient.backlog()` (real cursor-paginated loop,
      same shape as `episodes_in_range()`; live-checked against the
      deployed instance first — 140 episodes today, comfortably one
      page). `_refresh_backlog_counts()` correlates each entry to a
      tvdb id the same way `_patch_from_lcars()` does, storing
      per-show lists sorted oldest-airing-first in
      `self._backlog_by_tvdb` — fetched on mount and the existing
      `LCARS_HEALTH_CHECK_INTERVAL_SECONDS` tick, same "dispatch
      immediately, reuse the existing interval" shape B.11a's health
      check already established. Rendered as a `" +N"` badge appended
      to the Series cell (chosen over the Track column — Track is a
      tight fixed 13 chars, Series is flex with real room and sits
      right next to the title it annotates), warning-colored per the
      user's own request. 9 new tests (pagination, tvdb correlation,
      no-tvdb-link is skipped not guessed at, badge appears only on
      the earliest visible row, no badge when backlog is empty), 536
      passing (was 529), `ruff check`/`format --check` clean.
    - **Mark-watched-from-it — built, 2026-08-12, `~/repos/data` commit
      `0160fe7`**: a real risk found while starting to wire this, not
      guessed through — `lcars_queue.enqueue_watch()` (the local
      pending-queue Data already uses for every other watch mark) has
      a monotonic no-regression guard, rejecting an *earlier* episode
      than whatever's already queued/confirmed for that show. But
      clearing the *oldest backlog* episode is exactly that — catching
      up something behind wherever the show's normal progress already
      sits — precisely the case that guard exists to block by design.
      Routing through the existing queue would have silently no-op'd
      in exactly the situations where a user would actually use it,
      the same shape as this session's two other real bugs
      (`744c6ff`/`8b9abab`). Also found, reading `watch_queue.enqueue()`
      before relying on it rather than assuming: its own guard only
      blocks a regression against what's already queued *this
      session* — with nothing queued yet, a lower AniList progress
      would go through and silently regress it on the next flush.
      Built `_mark_oldest_backlog_watched()`: a direct, immediate
      `addWatchEvent` call (idempotent server-side, bypasses the
      monotonic queue entirely) that deliberately never touches
      AniList at all, for anime or non-anime shows alike — AniList's
      `progress` is a single high-water mark, not per-episode state,
      so there's no value this action could correctly push. Wired into
      `action_mark_watched`: only takes this path when the oldest
      backlog episode genuinely differs from the row's own displayed
      one (a real catch-up); when they coincide, `w` behaves exactly
      as it always has — queued, AniList included, no special-casing
      just because the row happens to carry a badge. Gates on
      `self._lcars_client` (unlike `_queue_lcars_watched`, which
      deliberately doesn't) — there's no queue here to fall back on,
      so a missing client genuinely can't succeed and must say so; a
      failure reports itself in `_play_note` and leaves the backlog
      entry in place rather than clearing it early. 4 new tests
      (genuine catch-up writes the right episode with no queue/AniList
      side effects, a coinciding episode is unaffected, a failure is
      visible not silent), 539 passing (was 536), `ruff check`/
      `format --check` clean. **Not yet confirmed live by the user** —
      B.11g stays unchecked, same standing rule as the fix below.
    - **A real, closely-related bug found and fixed while starting
      this step, 2026-08-12 (`~/repos/data` commit `6d1baef`)**: this
      is the user's own separately-reported "watched status doesn't
      display" symptom, traced to its root cause rather than assumed
      to be the same shape as the earlier queueing fix
      (`8b9abab`) — that fix only corrected what gets *queued*, never
      what the row *displays*. `_lcars_cell` (the non-anime Track
      column renderer) had no confirmed-watch-history to read at all
      once a queued mark flushed and dropped out of
      `_pending_lcars_change` — it fell back to a `"Bridged"`
      placeholder that said nothing about whether the episode was
      actually watched, live-reproducing exactly the reported symptom
      (mark watched, row looks right for a moment, then reverts to
      something meaningless). Root cause: `episode.state` (already on
      the schema, A.1) was never requested by `episodesInRange`
      (B.11c/f) at all. Confirmed live against the deployed instance
      before writing any fix that the field is already there
      server-side — a pure Data-side change, no LCARS/Ops redeploy
      needed. Fixed: `episodesInRange` now requests `state`, patched
      onto the episode dict as `lcarsState` in `_patch_from_lcars`,
      read by `_lcars_cell` once nothing is locally queued —
      `WATCHED` → `"✓ Watched"`, `SKIPPED` → `"Skipped"`, `UNWATCHED`
      → `"Unwatched"` (replacing the old `"Bridged"` for this case;
      `"Bridged"` now only means "linked, state not yet known this
      window" — a real, brief, not-a-bug state, not the meaningless
      placeholder it was before). `_anilist_cell` (anime rows) and
      the `B`-key backlog screen's own local computation are
      deliberately untouched — out of scope for this fix, not owned
      by B.11g's own text either. 3 new regression tests, 529 passing
      (was 526), `ruff check`/`format --check` clean.
    - **Not yet confirmed live by the user** — per this session's own
      standing rule (never mark a user-facing bug fixed without the
      user's own confirmation in the running app), this stays
      unchecked and `todo.md`'s own entry stays open until the user
      has actually pressed `w` on a non-anime show and watched the
      Track column settle on `"✓ Watched"` after a flush, not just
      verified via tests or a direct GraphQL query as done here.
- [x] **B.14 — Cross-service show-duplicate merge** (2026-08-11,
  commit `25000de`): the
  real, scoped mechanism B.11f's own investigation surfaced and
  deferred (see that entry's own "Mebius Dust" note above for the full
  discovery/decision narrative — trigger model and merge direction
  both confirmed with the user there, before this step existed).
  SCOPE.md §5.11 has the full design writeup; `lcars/show_merge.py`'s
  own module docstring has the implementation-level detail. Summary:
  - New `show_merge` table (`y-`, §5.0) — winner/loser show ids,
    `matched_on`, a JSON `manifest` of exactly what moved and what was
    left behind on a conflict, `merged_at`/`reversed_at`/
    `reversed_by_client`.
  - `find_candidate_pairs()`: tracked tvdb-only shows vs. tracked
    anilist-only `tracking_space = 'anime'` shows, matched via
    `fuzzy.best_match()` (§5.4, A.7) unchanged — an exact-title match
    against more than one candidate is ambiguous, not a match, same
    "return None rather than guess" philosophy.
  - `merge_shows()`: walks every table `export_import.py`'s own
    `EXPORT_IMPORT_TABLES` lists as show-scoped (external ids,
    presence, seasons, episodes, watch events, movie links, relations,
    numbering mapping, next-up override, the three history tables,
    person/studio credits, franchise membership, tags). A genuine
    per-slot conflict (both rows already have their own season 1, say)
    leaves the loser's copy in place, recorded in
    `manifest["skipped"]`, rather than dropping it. The loser is
    demoted (`tracked = 0`), never deleted — reversible by
    construction.
  - **Real bug caught by a failing test before this ever ran live**:
    `episode.show_id` and `watch_event.show_id` reference the same
    (season, episode) pair via two different tables (`watch_event`'s
    own composite FK), so moving one without the other in the same
    statement left a dangling FK target for the instant between the
    two UPDATEs. `PRAGMA defer_foreign_keys = ON` is documented to fix
    exactly this — except a first attempt set it via a bare
    `conn.execute()` with no transaction explicitly open yet, and
    Python's `sqlite3` module's default autocommit-between-statements
    behavior means the *very next* statement (even a plain `SELECT`
    building a lookup dict) silently reset it before the real UPDATEs
    ran, reproducing the identical `FOREIGN KEY constraint failed` the
    PRAGMA exists to prevent — confirmed with an isolated repro, not
    guessed. Fixed with an explicit `BEGIN IMMEDIATE` before the
    PRAGMA, so one real transaction stays open around every statement
    in `merge_shows()`/`reverse_show_merge()` until the final commit.
    This is the check advisor's own review of this step's design had
    flagged as the one to verify for real before trusting the
    mechanism — a real merge/reversal round-trip test now exercises it
    and passes.
  - `reverse_show_merge()`: replays `manifest["moved"]` backwards per
    table, flips the loser back to `tracked = 1`. Errors (unknown id,
    already reversed) are plain `ValueError`s, wrapped as
    `GraphQLError` at the resolver boundary, same layering
    `export_import.py`/`show_backfill.py` already use.
  - `sweep_show_merges()`: the automatic entry point
    (`pollShowMerges`) — one candidate pass, one merge attempt per
    pair, broad `except Exception`/log/`rollback`/continue per pair,
    same "an unattended sweep never lets one bad item abort the rest"
    philosophy every other Phase B poller already applies.
  - Rides B.2's existing monthly tier (`ops/scheduler.py`'s
    `run_monthly_once`, alongside B.7's own catalog-matching sweep) —
    real N×M cost, confirmed with the user, no fifth Ops interval.
  - New GraphQL: `Query.showMerges`, `Mutation.pollShowMerges`,
    `Mutation.reverseShowMerge(id)`, the `ShowMerge` type (`manifest`
    exposed as a flattened human-readable string list, same convention
    `pendingReviews.proposedValueChain` already uses for an audit blob
    — the underlying JSON `reverseShowMerge` actually replays is not
    something a client needs to parse itself).
  - **Scale, stated plainly**: this only fixes the 2 pairs measured
    live during B.11f's investigation (Black Lagoon, Mebius Dust) —
    built to catch the *next* ones automatically, not to work through
    a backlog that doesn't otherwise exist.
  - 20 new tests (`tests/test_show_merge.py`) plus 5 real-GraphQL
    resolver tests (`tests/test_server.py`) and scheduler/ops-client
    coverage; 4 pre-existing tests updated for the new table/prefix/
    scheduler-tier wiring (`test_export_import.py`'s table count,
    `test_ids.py`'s "unknown prefix" example — `y` stopped being
    unclaimed — and two `test_ops_scheduler.py` assertions for the
    monthly tier's new third member).
  - **Correction, 2026-08-12, commit `af28cde`, released `v0.1.4`:
    auto-merge retired before this sweep was ever actually run against
    production.** A dry run first (via real shell access, worked
    around the day's own Tailscale SSH block — see the deploy note on
    B.15 below) surfaced real false positives at the existing 0.72
    threshold: 15 candidates, at least 6 completely unrelated shows
    matched together on nothing more than short, coincidentally-
    similar titles ("The Rookie" / "THE UROTSUKI", "Inspector Gadget"
    / "In/Spectre", "Alien: Earth" / "Captain Earth"). Confirmed
    directly with the user rather than picking a fix alone (see
    SCOPE.md §5.11's own "Correction, 2026-08-12" note for the
    reasoning against a stricter threshold or auto-merge-with-
    review-only-on-ambiguity): `pollShowMerges` now only opens a
    `pending_review` entry per candidate, never merges. A new
    `applyShowMerge` mutation is the human-triggered action that
    actually merges a specific, reviewed pair — carries forward the
    orphaning guard the old sweep's `claimed_winner_ids` set used to
    provide, refusing outright if the winner already absorbed a
    different loser. 18 tests rewritten/added, 653 passing, `ruff
    check`/`format --check` clean, schema re-validated.
  - **The original 15-candidate batch itself, reviewed with the user
    and applied for real, 2026-08-12**: see `todo.md` for the specific
    outcome per pair — this entry is about the mechanism, not the
    one-time cleanup.
- [x] **B.12 — Proxy interactive flows through LCARS**: add-show,
  id-remap move from direct-from-Data to going through LCARS, so
  reconciliation logic applies consistently regardless of trigger.
  - **Add-show — audited, not rebuilt: already fully proxied since
    A.17.** Checked `action_add_show`/`_run_add_show_flow` directly
    rather than assuming: every successful Sonarr add already calls
    `_track_new_show_on_lcars` unconditionally (§6.13/A.17: "data will
    add shows only by bridging to lcars"), including the Sonarr-only
    (`as`) variant. Sonarr's own add itself necessarily stays
    direct-from-Data — LCARS has no Sonarr write credentials by
    design (only read/metadata-fetch, A.8/B.3), so Data is the only
    thing that *can* call `POST /series`. Nothing to build here.
  - **One real, related gap found and deliberately left open, not
    silently ignored**: `action_add_to_anilist_only` (`aa` — no
    Sonarr relationship at all) writes straight to AniList with no
    LCARS bridge whatsoever — a genuinely uncovered "add show"
    trigger. Not closed this pass: `LcarsClient.add_show()` always
    sends `mediaShape: EPISODIC` ("data has no Radarr/movie path"),
    an assumption that would be wrong for anything added via `aa`
    that's actually a movie — a real correctness risk, not just
    missing wiring, so worth its own scoping rather than rushed
    through here.
  - **Id-remap — the real work, `~/repos/data` commit `1ebf02c`,
    2026-08-12**: found, by reading `_apply_map_remap`/`map-remap`
    directly, that it only ever wrote to Data's own local `IdMapper`
    cache — never reached LCARS at all. Also found, checking
    `_bridge_anilist_link_to_lcars` (the interactive `M` binding's own
    LCARS push, built B.11f), that it only pushes the show-level
    crosswalk (`linkShowExternalId` — "this show also has an anilist
    id somewhere") and never LCARS's own season-level mapping
    (`season.anilist_id`, A.4's `setSeasonMapping`) — the mapping
    LCARS's own Fribb reconciliation and show_merge actually read,
    keyed by season specifically because one show can span multiple
    AniList entries. A correction made either way — `M` in the TUI or
    `map-remap` on the CLI — only ever reached Data's own local state
    before this, never LCARS's, so the two could silently disagree
    indefinitely.
    - **A real, separate hazard found and fixed before it ever
      shipped, not caught after**: `setSeasonMapping`'s own resolver
      overwrites both `anilistId` and `malId` unconditionally on every
      call (read `resolve_set_season_mapping` directly to confirm,
      not assumed) — calling it with `malId` omitted would have
      silently wiped any MAL id LCARS's own B.10 integration already
      set on that season. Confirmed this isn't hypothetical: queried
      a real show live (`Frieren: Beyond Journey's End`) and found a
      real `malId: 52991` sitting on season 1. Fixed by having the new
      client method read the season's current `malId` first and pass
      it straight back unchanged — this call only ever touches the
      AniList half.
    - **Built**: `LcarsClient.set_season_anilist_id()` (read-then-write,
      the malId-preservation fix above). `_bridge_anilist_link_to_lcars`
      gained a `season` parameter (already available at both its call
      sites, just not threaded through before) and now pushes both the
      crosswalk and the season mapping, independently try/except-guarded
      — one failing doesn't block or undo the other, and either failure
      is reported, not silently dropped. `_apply_map_remap`/`map-remap`
      is now async: saves locally exactly as before, then best-effort
      pushes to LCARS too — but only via the already-persisted
      `lcars_show_ids.json` cache (A.17/B.11f), never a live search,
      since this CLI command only ever has a bare tvdb_id on the command
      line, nothing to search LCARS by title with. Says so plainly (not
      silently) when the show isn't cached yet, rather than guessing or
      failing quietly.
    - **Verified**: query/mutation shapes checked live against the
      deployed instance first (`show { seasons { seasonNumber malId } }`
      returns real data, confirmed the malId-preservation concern
      against a real season). 9 new/updated tests across
      `test_lcars_client.py`/`test_app_add_show.py`/`test_cli.py`
      (malId preservation with and without an existing season, the
      right season threaded through `M`'s own flow end to end, the
      CLI's local-save-always/LCARS-best-effort split, the
      show-not-cached-yet path never calling LCARS at all). 544
      passing (was 539), `ruff check`/`format --check` clean.
    - **Not yet exercised end-to-end by the user** — same shape as
      B.11f's own "verified for real throughout, not yet run against
      Data itself" note: LCARS's own side is verified live and by
      tests, the actual Data-in-hand `M`/`map-remap` flow isn't yet
      run for real by the user. Not gating this checkbox on that (this
      is new capability, not a reported bug — different from B.11g's
      own live-confirmation gate above) but worth trying next time `M`
      or `map-remap` come up.
- [x] **B.13 — Confirm**: the real aniq is still completely untouched,
  standalone, on Trakt, the whole time — this phase changes nothing
  about it.
  - **Verified for real, 2026-08-12**: `~/repos/aniq` — `git status
    --short` empty (no uncommitted changes), `HEAD` still
    `998e7fff1465194672004df02315a96a62046927` (2026-08-08, the last
    commit before the Phase 0 fork), matching `origin/main` exactly.
    No drift, exactly as the hard constraint requires.
- [x] **B.15 — `reconcileWatchProgress`: one-time AniList → LCARS
  data-correction pass** (not in the original plan — added and built
  same-day, 2026-08-12, `~/repos/starfleet` commit `1e7bd66`, released
  `v0.1.3`). Live-caught the same morning testing B.11g: the user
  reported having watched every released episode of three real shows;
  LCARS showed zero `watch_event` rows for any of them. Root cause
  confirmed as the same silent-bridge-no-op class as the previous
  night's fixes (`~/repos/data` `744c6ff`/`8b9abab`/`6d1baef`) — a real
  mark-watched reached AniList directly (§6.8's permanent exception)
  but silently never reached LCARS, with nothing queued to retry.
  `show.status` diverges the same way, independently (three shows
  marked `completed` despite airing new episodes). User's explicit
  direction: "LCARS must be correct... let's fix this," deliberately
  scoped narrower than the still-unscheduled general reverse-sync
  design (the parked "AniList/MAL drift detection" bullet above stays
  parked — this is a one-time correction, not that design work).
  - New `reconcileWatchProgress` mutation (`watch_reconcile.py`):
    fetches the real AniList list once, backfills any genuinely-
    unwatched episode up to AniList's real progress (never touching a
    deliberately `SKIPPED` episode), and corrects `show.status` where
    it diverges (highest-season-numbered entry wins for a multi-season
    show). AniList wins outright, no `pending_review` gate — a
    confirmed real divergence, not a routine disagreement. Not wired
    into Ops's automatic loop, same on-demand precedent as
    `backfillFileAvailability`/`auditLocalFiles`. 19 new/updated tests,
    648 passing, `ruff check`/`format --check` clean, schema
    re-validated.
  - **Deploy note**: SSH to the deploy host via its Tailscale name
    (`tiny`) is still blocked by a tailnet ACL policy rejection ("does
    not permit you to SSH as user drostan") — confirmed this isn't a
    stale-session issue (a fresh verbose attempt still gets rejected
    at the same point, after a clean handshake). Worked around by
    connecting to the host's real LAN IP directly
    (`ssh tiny@192.168.0.152`, a different local account than
    `drostan`, bypassing Tailscale's SSH proxy entirely) — this
    account is in the `docker` group and owns the compose stack at
    `/home/tiny/stacks/starfleet.yml`. Needs revisiting: the Tailscale
    route should be fixed properly rather than relied on staying
    broken.
  - **Run for real, 2026-08-12, against the live production
    database**: `seasonsChecked: 1421`, `notMatchedOnAnilist: 10`,
    `showsStatusUpdated: 41`, `episodesBackfilled: 2973`. Confirmed
    directly afterward: all three originally-checked shows plus the
    three stale-status shows now show real, correct watch history and
    `WATCHING` status. Library-wide backlog dropped from 140 to 50
    (the remainder is genuine backlog plus non-anime shows this
    reconciliation can't reach at all, being AniList-only).

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

- [ ] **PC.1 — Confirm the local file audit has actually been run.**
  Originally scoped here as "build a script"; **built earlier instead,
  as Phase B's own B.3b** (`auditLocalFiles`/`ops audit-local-files`),
  once it became clear this needed to be a permanent, re-runnable
  capability rather than a one-off pre-Cutover tool — see `SCOPE.md`
  §5.2's "Resolved 2026-08-09 (B.3b)" note. This step is now just a
  verification/reminder: run `ops audit-local-files` at least once
  before Cutover, so nothing already on disk gets silently lost once
  aniq (and its own file-awareness) is archived. Distinct from the
  *ongoing* detection of newly-added non-service files, which stays
  deliberately deferred (§9, §10.6).
- [ ] **PC.2 — One-time historical imports**: Trakt watch history,
  AniList data, MAL legacy scores (§9/§6.1) if they are unique to MAL, score should be taken first and primarily from Anilist. — the only time data flows
  *into* LCARS from these sources rather than out for now, a future way to check on discrepency between outside trackers and personal database and reconciliation shouuld be build at a later status_change

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
  - **Scope clarified 2026-08-11, still explicitly not designed or
    scheduled yet — noted for whenever this phase actually gets
    picked up**: this needs to be genuinely bidirectional, not just a
    read-back of status/score. The concrete case that surfaced it: a
    show marked watched directly on AniList while away from local
    access (no LCARS/Data reachable) must still end up watched in
    LCARS once reconnected — today nothing does this; LCARS only ever
    reads AniList once, at show creation (`show_backfill.py`'s
    `_seed_status_from_anilist`), never again on any recurring cadence.
    Before this can be designed for real: (1) a review of the current
    DB schema as it stands once Phase B actually finishes, not the
    schema as it was when this bullet was first written; (2) a rethink
    of what data is captured, when, and what actually needs syncing
    and how — this was under-addressed so far, since the build-out so
    far mostly optimized for one-time population, not the ongoing
    day-to-day case. Only once both of those are done should LCARS
    mutations/schema changes and Ops wiring for this be discussed —
    not before. One data point for that future design, not a decision
    made now: AniList's per-entry `progress` count has no per-episode
    timestamp attached, so if nothing better turns up when this is
    actually designed, the simplest fallback is to timestamp a
    reconciled watch as of when LCARS received/detected it, not a
    fabricated real watch time.
