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
