# Starfleet — project scope & design

**Status:** design complete for v1 scope. Nothing built yet — this
document is the design, not an implementation log. Rewritten from
scratch after a 33-round discussion session (2026-08-02..04), then
kept current through further rounds (34: Data/kal fork, 35: titles/
relation types/studio entity/rewatch push behavior, 36: air-date
precision, home timezone, per-show service presence, 37: implementation
stack — Python-first/Rust-later, SQLite, Docker, the id scheme, 38:
full naming pass — Starfleet/LCARS/Data/Holodeck/Ops/Captain's Log);
the full round-by-round rationale for every decision below lives in
`/home/drostan/.claude/plans/read-existing-md-until-scalable-bachman.md`
if the *why* behind something here isn't obvious from the *what*. See
`BUILD_PLAN.md` in this same directory for the step-by-step build
order, and `KICKOFF_PROMPT.md` for how to start a fresh session here.

## Naming

This project was designed under the working codename **Chabrol**
(after the film director — though the family's actual theme turned
out to be Star Trek, not French New Wave; the placeholder just stuck
around through 37 rounds of discussion before a dedicated naming round
settled it for real). Full rename:

| Working name | Production name | What it is |
|---|---|---|
| Chabrol (whole project) | **Starfleet** | The ecosystem — everything below |
| Chabrol (backend/database/API) | **LCARS** | The GraphQL server + database (§5, §8). Keeps this name regardless of implementation language — Python today, Rust later (§11.1); a ship refit doesn't get a new name. |
| Chabrol's background scheduler | **Ops** | The autonomous poller (§6.7, Phase B) |
| kal | **Data** | The TUI client, forked from aniq (§4.0, §7.1) |
| Web UI | **Holodeck** | The full-editor graphical client (§7.3) |
| CLI | **Captain's Log** (command: `cl`) | The command-line client (§7.4) |
| aniq | *(unchanged)* | The original TUI — not renamed, not part of this theme, never modified from here |

The rest of this document uses the production names throughout. The
discussion memo's historical entries still use the original working
names as they were used at the time (a new entry documents the
renaming decision itself, without rewriting the past) — see that file
for the round-by-round naming rationale.

**This repo (`~/repos/starfleet`) is the completed rename** —
§10.5's original "deferred to the last moment before starting the
project in earnest" plan, executed. The pre-project discussion repo
(`~/repos/Chabrol`) is left as-is, untouched, as a historical record;
this copy of `SCOPE.md` is the current, canonical one going forward.

---

## 1. What this is

A small, self-hosted, single-user personal database that becomes the
source of truth for "what am I following, what have I watched, and
when did/does it air" — across **anime, non-anime TV, and movies**.

- **Replaces Trakt entirely** (Cloudflare WAF flakiness, no native
  status concept, no server-side sync).
- **Sits alongside AniList**, not instead of it — LCARS pushes to
  AniList (status/score/episode progress), never treats it as an
  external source of truth to reconcile against.
- **Sits alongside MAL**, as a lower-confidence, not-yet-verified
  second push target (see §6.9).
- **aniq** (Textual TUI, separate repo, read-only reference for this
  project — never modify it from here) is the real, currently-in-daily-
  use tool this whole project is meant to eventually succeed. It stays
  untouched and keeps working exactly as it does today (Trakt +
  AniList) throughout Starfleet's build-out — see **Data** below for
  how the actual transition happens.
- **Data** (a new sibling repo, a one-time fork of aniq's codebase) is
  where all LCARS-client integration work actually happens — the
  testbed *and* the blueprint of the final front-end client, not a
  disposable prototype (§4.0). It becomes a client of LCARS, eventually
  a thin one, but keeps one narrow permanent direct write path to
  AniList (see §6.8) — the same role aniq's own drawdown would have
  played, just happening in a separate repo so aniq itself is never put
  at risk.
- Two more clients are planned as first-class peers, not afterthoughts:
  a full-editor **Holodeck** and a full **Captain's Log**.

Everything downstream of this document assumes: no multi-user support,
no plans to ever add it — every design choice (no `user` table, single
static bearer token, etc.) leans on that.

---

## 2. Why (the problems this solves)

- **Trakt's Cloudflare WAF is unreliable** — confirmed, reproducible,
  not fixable client-side. A self-hosted DB has no such dependency.
- **Trakt has no native status concept** — aniq fakes it with 5 custom
  lists today (`trakt_status.py`). A real DB just needs a status
  column; the whole list-membership-as-status subsystem goes away.
- **Air date/time is flaky and only half-patched today** — Sonarr's
  raw `airDateUtc` (from TVDB) is sometimes wrong for new simulcasts.
  aniq currently patches this anime-only, AniList-only, best-effort.
  LCARS generalizes this across all sources and all kinds, with a
  real priority order and a full audit trail (§6.7).
- **Background sync currently only happens while aniq is open** — its
  refresh timers are Textual app timers, dead the moment the TUI
  isn't running. Ops runs its own schedule independent of any client
  being open (Phase B, §4).
- **One account of record instead of three** — today "am I tracking
  this" is split across Sonarr, AniList, and Trakt, held together by
  ad hoc local caches and pending-write queues in aniq. LCARS becomes
  the one place that answer lives.

---

## 3. Core design principles

A handful of patterns recur throughout the whole design — worth
naming once here rather than re-explaining in every section below.

1. **One reconciliation pattern, used everywhere two sources can
   disagree**: attempt automatic derivation → apply the result
   immediately → log a `pending_review` entry for later human
   awareness. There is no "gate" (block-until-confirmed) mode anywhere
   in the project. A manual value is never protected from being
   auto-overwritten either — review is always after-the-fact, for
   every field, every source, no exceptions. This single mechanism
   covers: id-mapping discrepancies, episode-numbering mismatches, all
   air-date source disagreements, and MAL legacy score import.
2. **History is per-concern, not generic** — dedicated tables
   (`status_change`, `score_change`, `air_date_change`, ...) rather
   than one polymorphic audit table. Every history row and every
   `pending_review` row records which client/process made the change.
3. **Nothing is hard-deleted by default** — soft status/tracked-flag
   change first, always. The one deliberate exception is
   `watch_event`, which is genuinely hard-deletable (undo a mis-click,
   no audit value in keeping it).
4. **Images are URLs only, never local bytes** — posters, banners,
   person portraits. Dead links self-heal silently on the next
   scheduled metadata refresh; no proactive dead-link detection.
5. **LCARS is unconditionally authoritative over AniList/MAL** —
   push-only for the current build. The one-time historical import at
   cutover is the only time data flows the other way *for now*.
   **Drift detection is a deliberate later addition, not part of the
   original design**: LCARS periodically pulling AniList/MAL list
   state back and diffing it against its own, to catch the case where
   AniList/MAL got edited directly (e.g. from a phone app) while
   Starfleet itself was down or unreachable. Not designed in detail
   yet and not scheduled in Phases A–C — see §10.6 item 12 — but
   explicitly intended to happen eventually, unlike the permanently-
   parked items in §9.
6. **Auto-derive with a manual-override-is-authoritative fallback**
   shows up as its own recurring shape beyond just principle #1: id
   mapping, episode numbering, franchise sort order all follow "try to
   derive it automatically, let a manual value win once set."
7. **Dedicated, purpose-built GraphQL mutations**, not one generic
   `updateX(fields...)` — each mutation owns its own validation and
   side effects (push-to-AniList, history-table writes, etc.).
8. **Three peer clients, no privileged one** — Data (the TUI, forked
   from aniq — §4.0), Holodeck, Captain's Log all talk to the same API
   and can do the same things, except where explicitly noted
   (deletion's confirmation step is UI-shaped, not client-restricted).

---

## 4. Architecture & phased roadmap

Design the schema and API now with the end-state (a thin TUI client)
in mind, but ship in stages — each one independently useful, each one
de-risking the next.

> **Correction to an earlier draft of this doc**: Phase A was
> originally framed as "no polling, no external API calls at all."
> That's now split more precisely — see below. Phase A does make
> *on-demand, single-shot* external calls triggered by an explicit
> user action; what it doesn't have yet is an *autonomous background
> scheduler*. That distinction is what actually separates A from B.

### Phase 0 — Data: fork aniq into a build/testbed client

Before any Phase A client work starts, fork aniq's codebase into a new
sibling repo, **Data**. From that point on, all LCARS-client
integration work happens in Data — the real aniq repo is not touched
again until cutover (§4.4).

- **Fork policy: clean, one-time fork, no ongoing re-sync.** Data
  diverges from aniq permanently at the fork point. Small fixes made
  to the real aniq during the (potentially long) Starfleet/Data
  build-out stay in aniq only — there's no periodic merge/rebase
  pulling them into Data. If one turns out to matter enough in Data,
  it gets ported over manually, as a one-off, not via an ongoing sync
  process.
- **The "never modify aniq" constraint is unchanged, and applies to
  the original aniq repo specifically** — it stays a pure, untouched,
  read-only reference throughout this entire roadmap, exactly as it
  has for this whole project. Data is a *new, separate* repo;
  modifying it freely is expected and in scope for Starfleet-side work
  from the moment it's forked. The two repos are never to be confused
  with each other going forward.
- **Why fork instead of modify aniq directly**: this lets the real
  aniq keep working exactly as it does today (Trakt + AniList, all its
  current behavior, warts included) as the actual daily driver for the
  entire duration of Starfleet's build-out, with zero risk from
  in-progress, possibly-broken Starfleet-integration work. Data is
  disposable/breakable by design during that period — the real tool
  never is.
- Data is **the blueprint of the final front-end client**, not a
  disposable prototype — see Cutover (§4.4) for what "finished" means
  for it.
- **Fork executed 2026-08-08** (`BUILD_PLAN.md` 0.3). Two decisions
  made mechanically during the fork, not previously specified here:
  - **Package/CLI renamed `aniq`→`data`** at fork time (not deferred)
    — avoids two same-named `aniq` things (module, console command)
    coexisting on the same machine once both are installed.
  - **Runtime-state isolation, beyond just the repo split**: aniq's
    own config/cache-file paths and keyring service name were the
    literal string `"aniq"` (`~/.config/aniq/config.ini`,
    `~/.local/share/aniq/*.json`, `KEYRING_SERVICE = "aniq"`). A naive
    `aniq`→`data` rename alone would have fixed the collision with the
    *real* aniq, but left Data using the bare, highly generic
    `~/.config/data`/`~/.local/share/data`/keyring-service-`"data"` —
    a real risk of colliding with some unrelated future tool using the
    same generic name. Nested Data's runtime state one level under a
    shared `starfleet` namespace instead
    (`~/.config/starfleet/data/config.ini`,
    `~/.local/share/starfleet/data/*.json`), and gave the keyring
    service its own distinct name (`"starfleet-data"`) rather than
    reusing the module name. Net effect: Data's OAuth tokens/config can
    never be read from, written to, or silently overwrite aniq's live
    credentials — the "never modify aniq" constraint (point 5 above)
    is honored operationally, not just at the git-repo level.

### Phase A — schema + CRUD API + reconciliation, no autonomous scheduler

- Full data model (§5) behind a GraphQL API with dedicated mutations
  (§8).
- id-mapper / reconciliation tables (§5.5), seeded from the static
  Fribb/`anime-lists` dataset (a one-time/on-demand download, not a
  live polling API) plus manual overrides.
- `pending_review`, all history tables, saved filter presets — all
  live from day one.
- **On-demand external calls are in scope here**: adding a show
  (any client) triggers an immediate one-shot metadata fetch
  (poster, synopsis, cast, episode list, external ids) from
  AniList/Sonarr/Radarr as appropriate — a direct consequence of a
  user action, not a background job.
- **No autonomous scheduler yet** — no daily refresh loop, no weekly
  Fribb reconciliation loop, no Sonarr/Radarr/animeschedule.net
  polling running on its own clock. Those are Phase B.
- Data's side: replace the Trakt client with an LCARS client of the
  same shape. Data (inheriting aniq's current logic at the fork point)
  keeps doing what aniq does today (polling Sonarr, calling AniList,
  running its own air-date patch) but also writes the resulting state
  to LCARS. AniList dual-write already exists in the forked code and
  is unaffected.
- Reliability note: aniq's existing "queue locally, retry on flush"
  pattern (`trakt_queue.py`/`watch_queue.py`), inherited by Data at the
  fork, carries over to the Data→LCARS path — a home server isn't
  guaranteed reachable from wherever Data happens to be running.
- The real aniq is not part of this phase at all — it keeps running
  standalone against Trakt/AniList as it does today, entirely
  unaffected, for as long as Data/LCARS take to reach parity.

This alone already fixes the two concrete pains that started this
project: Trakt's flakiness is gone, and status gets a real column —
inside Data. Shippable and useful before any scheduler exists.

### Phase B — Ops takes over syncing

- A real autonomous background scheduler — **Ops** — goes in:
  - **Daily** metadata refresh for `watching`-status, actively-airing
    shows, plus an on-open trigger capped to the same once-per-day
    ceiling (not stacking).
  - **Weekly** Fribb dataset reconciliation — deliberately slower and
    independent of the daily metadata cadence.
  - Sonarr/Radarr polling for file availability (queue/episode-file/
    movie-file endpoints — never a filesystem scan).
  - AniList `airingSchedule` polling.
  - animeschedule.net polling — REST API v3 (`/timetables`, filtered
    by `anilist-ids`) preferred over its RSS feeds, per §10.1's
    research findings.
  - Per-integration service-health tracking (reachable? rate-limited?)
    as its own concept, separate from any individual show's state.
- Air-date reconciliation runs here (§6.7) — this is where the "keeps
  tracking fresh continuously, not just while a client is open" benefit
  actually lands.
- Data's role starts shrinking: the calendar can read tracking/air-
  date/availability state from LCARS instead of computing it locally.
  Data's status bar gains per-source sync-health indicators sourced
  from Ops's service-health state.
- Interactive, user-initiated flows (add show, id-remap) proxy through
  LCARS rather than staying direct-from-Data, so reconciliation logic
  applies consistently regardless of which client triggered them.
- Still no change to the real aniq — it remains standalone, on Trakt,
  untouched, the whole time.

### Phase C — Data as thin front-end + mpv/aninote bridge

Data drops its own Sonarr-polling/AniList-polling/air-date-correction
logic entirely, keeping only:
- Calendar rendering, driven by reads from LCARS.
- **mpv IPC and aninote invocation** — inherently local (a Unix
  socket to a player on the same machine, a CLI tool on the same
  machine), can never move server-side. Hard constraint, not a choice.
- Relaying user actions to LCARS.
- **The one narrow, permanent exception**: Data keeps a direct
  write path straight to AniList for episode watch-status only (§6.8)
  — writing to AniList and LCARS simultaneously, no waiting on Ops's
  own schedule. This is not something Phase C removes; it's a
  deliberate permanent design choice, independent of how thin Data
  otherwise gets. Data retains its own AniList OAuth credentials
  indefinitely for this one path.

By the end of Phase C, Data is functionally what aniq's own Phase C
drawdown would have looked like — just built and hardened in a repo
that was safe to break along the way. The real aniq is still, at this
point, sitting untouched on the side, exactly as it was at the Phase 0
fork.

### Cutover — Data replaces aniq as the daily driver

Once Data has been proven reliable through real daily use — parity
with, or better than, the real aniq — the user switches actual daily
use over to Data.

- **aniq is archived, not deleted.** It's frozen exactly as it stands
  at cutover and kept as a known-working emergency fallback in case
  Data ever breaks or has downtime — not actively maintained or
  developed further after archiving.
- **Data becomes the permanent front-end going forward, under its own
  name.** No merge-back into aniq, no rename — the two stay distinct
  repos: one archived/frozen, one active.
- **Explicitly parked, out of scope until this point is actually
  reached**: a possible one-time sync of aniq's own local watch-history
  and episode-numbering-reconciliation data into Data/LCARS, in case
  the two diverged on anything meaningful during the (no-re-sync,
  §4.0) transition period. Not designed now — worth thinking about only
  once aniq is genuinely about to be retired for good.
- **Pre-Cutover step, required, timing otherwise flexible**: a
  one-time audit/log of existing local files not currently linked via
  Sonarr/Radarr, so nothing already on disk gets silently lost or
  forgotten once aniq (and its own file-awareness) is archived. Not
  tied to a specific phase — can happen any time after the core
  build/functionality work is done, as long as it's complete before
  full rollout. Distinct from §9's *ongoing* detection of newly-added
  non-service files, which stays deferred/low-priority — this is a
  real, scheduled, one-time task. Likely built as a script
  cross-referencing a folder walk against §5.2's
  `available_via_sonarr`/`available_via_radarr` flags once those
  exist, rather than a new standing feature.

---

## 5. Data model

### 5.0 ID scheme

Every top-level entity gets a short, human-readable, type-prefixed id
— not an auto-increment integer, not a UUID.

Format: `{1-letter prefix}-{6-char random string}`, e.g. `s-a3f9k2`
for a show, `e-x82j1q` for an episode.

- **Random portion**: 6 characters from a Crockford-style 32-symbol
  alphabet (`0-9` and `A-Z` minus `I`, `L`, `O`, `U` — the characters
  most easily confused with each other or with digits), stored/
  compared case-insensitively. Gives `32^6` ≈ 1.07 billion possible
  values per prefix — far more than a personal system will ever need,
  while staying safe to hand-read or hand-type without ambiguity.
- **Collision handling**: this space is *not* astronomically large the
  way a UUID's is — birthday-paradox math means collisions become a
  real (if still small) probability once a table accumulates tens of
  thousands of rows over the system's lifetime. Generation always
  checks the random portion against a `UNIQUE` index scoped to that
  prefix and regenerates on the rare collision — standard practice for
  short-id systems (Stripe, bit.ly, and YouTube-style ids all work
  this way). At realistic personal-scale row counts, the retry path
  will essentially never actually trigger.
- **Generation**: via `nanoid` with this custom alphabet — mature
  implementations exist in both Python and Rust with identical
  behavior, so the id format itself needs no changes across the
  planned Python→Rust rewrite (§11.1).
- **Prefix table** — one letter per top-level entity. Pure join/link
  tables (`show_external_id`, `show_person`, `show_studio`,
  `franchise_member`, `show_tag`) use plain composite keys instead, no
  prefix spent on them — they're always addressed through their
  parent, never fetched by their own id.

| Prefix | Entity |
|---|---|
| `s-` | `show` |
| `e-` | `episode` |
| `w-` | `watch_event` |
| `p-` | `person` |
| `d-` | `studio` |
| `f-` | `franchise` |
| `t-` | `tag` |
| `r-` | `pending_review` |
| `x-` | `show_id_mapping` |
| `n-` | `episode_numbering_mapping` |
| `a-` | `show_service_presence` |
| `q-` | saved filter preset |
| `c-` | `status_change` |
| `o-` | `score_change` |
| `g-` | `air_date_change` |
| `k-` | `tracked_change` |
| `m-` | `episode_movie_link` |
| `v-` | `next_up_override` |

18 of 26 letters used, leaving headroom for future entities.

### 5.1 `show`

Three independent axes describe a show's identity — not one `kind`
enum:

| Axis | Values | Notes |
|---|---|---|
| `media_shape` | `episodic` \| `movie` | |
| `tracking_space` | `tv` \| `anime` | `anime` **mandates** an AniList link, no exceptions — applies orthogonally to `media_shape`, an anime movie still requires one. `tv`'s Sonarr link is optional. |
| `file_source` | independent Sonarr/Radarr availability flags, not one exclusive value | For `media_shape = episodic`: lives on the **episode** row, not the show row (per-episode, not per-show) — **validated against real library data (§10)**: confirmed correct at per-episode granularity, and refined from a single enum to two independent booleans — see §5.2, `available_via_sonarr`/`available_via_radarr`. For `media_shape = movie`: lives on **`show` itself** instead — see the movie-tracking fields below, added 2026-08-08 while drafting A.2. |

Other fields:
- **Title, stored as all available variants, not one string**:
  `title_romaji`, `title_english`, `title_native` (whichever a source
  actually provides — not every show has all three), plus a
  `primary_title` flag marking which variant is the display default.
  Full-text search (§6.5) matches across **all** stored variants, not
  just the primary one.
- `status`: `watching \| planned \| paused \| completed \| dropped` —
  unchanged 5-value enum, reused as-is for movies (no movie-specific
  simplification — `watching`/`paused` remain meaningful, e.g. paused
  mid-sitting).
- `score`: the personal 0–20 quarter-point value itself (§6.1). Not
  listed as its own bullet in an earlier pass of this section, but
  confirmed to live on `show` by both §5.7's `score_change` history
  table and §6.1's conversion rules — noted explicitly here while
  drafting A.1 so the field list is actually complete.
- `tracked`: boolean, independent of `status`. Lets LCARS know about
  a show without any intent to watch it (distinct from `planned`,
  which implies eventual intent). Relation/franchise targets can exist
  as `tracked = false` stubs.
- `total_episodes`: nullable, stored separately from the actual count
  of episode rows that currently exist (episodes are pre-populated
  from the known schedule as soon as it's reported, but the expected
  total is tracked independently and updatable).
- `duration_minutes`: default per-show runtime, used as the stats
  feature's "hours watched" multiplier unless a given episode
  overrides it (§5.2).
- `poster_url`, `banner_url`: source URLs only, no local image bytes.
- Info-card fields, cached locally: genres/tags (raw per-source
  strings, deliberately unnormalized — no canonical taxonomy layer),
  synopsis, external links. *Content warnings/age rating is explicitly
  deferred* — low value for a single-user tracker, not part of the
  active field list. Studio/publisher/network moved to its own
  queryable entity — see §5.8.
- Custom tags: flat (no hierarchy), user-defined, many-to-many
  (`show_tag`). Also covers "favorite" — no separate pinned/favorite
  boolean field; a tag does that job.
- **Movie tracking fields** (`media_shape = 'movie'` only), added
  2026-08-08 while drafting A.2, resolving a real gap A.1 missed: a
  standalone movie show gets **no `episode` row at all** — clarified
  directly by the user: a film can occupy a slot in a *franchise's*
  watch order (`franchise_member.sort_order`, §5.9 — "an episode of
  the franchise," not of its own show), but is never modeled as an
  `episode` of its own show, and is never conflated with Sonarr's own
  season-0 tracking. So the availability mechanism `episode` normally
  carries moves onto `show` itself for this case:
  `available_via_radarr`, `available_checked_at`, and a generated
  `available_locally` (mirrors `episode`'s own shape, §5.2, just
  Radarr-only — no Sonarr side to OR against, since a movie show's own
  Sonarr availability doesn't apply — see `episode_movie_link` below
  for the *separate* case of the same film also being tracked as a
  `bonus_movie`-kind episode elsewhere). No movie-specific `skipped`
  equivalent — confirmed sufficient to reuse `status` alone
  (`dropped`/`completed` already cover "decided not to watch"/
  "watched"); `episode.state = skipped` exists specifically to clear
  per-episode backlog counters (§6.3) on an accumulating list, which a
  single movie doesn't have.

**Movie ↔ Sonarr-tracked-episode reconciliation** (added 2026-08-08,
A.2): the *same* film can exist both as a standalone Radarr-tracked
movie show and as a `bonus_movie`-kind episode inside a different,
related episodic show (Sonarr sometimes carries a tie-in movie as a
season-0 special). Reconciled the same way every other cross-source
identity question in this document is (§3.1, §5.5) — internal ids are
the source of truth, external ids (TMDB) map onto them, a best guess
applies immediately with a `pending_review` entry on ambiguity, manual
override wins once set. New table, same shape as `show_id_mapping`:

```
episode_movie_link
  id                m- (see updated prefix table, §5.0)
  episode_id        FK episode, UNIQUE — the bonus_movie-kind row
  movie_show_id     FK show, nullable until matched — the standalone
                     movie show representing the same film
  source            tmdb_match | manual | unmatched
  matched           bool
  manual_override   bool
  created_at / updated_at
```

**Show-row promotion paths** (three, all distinct):
- A bare `tracked = false` relation/franchise stub becomes a real
  tracked show by flipping `tracked = true` on the *existing* row —
  no re-creation, identity/relation data carries over.
- A confirmed-but-unscheduled sequel/season (relation graph knows it
  exists, no air dates yet) gets a real `status = planned` row as soon
  as existence is confirmed — ahead of any known schedule. Episode
  rows still wait for actual schedule data.

### 5.2 `episode`

```
episode
  show_id
  season
  episode
  kind                 regular | special | ova | bonus_movie
  absolute_number       decimal-capable — see numbering below
  air_date_utc          corrected/current best-known value —
                         full date+time, not date-only, matching the
                         precision AniList's airingSchedule and Sonarr
                         actually report; supports same-day ordering
                         and "airs in Xh" countdowns
  air_date_source        sonarr | anilist | animeschedule | manual
  air_date_raw_sonarr    kept for diffing/debugging
  available_via_sonarr   bool — file available through Sonarr's own
                         copy (the episode/season-0-special file, for
                         any kind)
  available_via_radarr   bool — file available through Radarr's own
                         copy (only ever populated for `bonus_movie`
                         kind — see below)
  available_locally      derived: available_via_sonarr OR
                         available_via_radarr
  available_checked_at
  runtime_minutes        optional override of show.duration_minutes
  state                  unwatched | watched | skipped
```

- **`kind`**: specials/OVAs/bonus-movie episodes keep `season = 0`
  for source compatibility with Sonarr/TVDB/AniList's own convention,
  but get their own `kind` flag (deliberately not reusing the
  show-level `movie` `media_shape` term, to avoid confusing a
  bonus-movie-length episode *within* a series against a standalone
  Radarr-tracked film). Non-`regular` kinds still fully participate in
  the relation/franchise graph.
- **`file_source` refined from a single enum to two independent
  booleans, based on real library data** (§10, resolving the
  provisional flag on this field): a `bonus_movie`-kind episode is
  frequently available through **both** Sonarr (as the season-0
  special) **and** Radarr (as the separately-tracked film) at once —
  confirmed across three real examples (an Apothecary Diaries movie,
  the Evangelion movies, The Dangers in My Heart: The Movie), not an
  edge case. Critically, the two sources are **not added at the same
  time** — one real example was Radarr-only for a period before Sonarr
  also picked it up as a special. A single exclusive `file_source`
  value can't represent "available via both, discovered at different
  times," so the field splits into `available_via_sonarr` and
  `available_via_radarr`, checked and updated independently on each
  poll (Ops, §6.7) — same "just reflects reality, no gating" treatment
  as `show_service_presence` (§5.4), one level down in granularity. For
  `regular`/`special`/`ova` kinds only `available_via_sonarr` is ever
  meaningfully populated; for a standalone movie show
  (`media_shape = movie`, not an episode at all) only Radarr applies,
  per §6.10 — the dual-source case is specific to `bonus_movie`-kind
  episodes living inside an already-tracked episodic show.
- **Absolute numbering**: sourced as-is when a source reports an
  official value (even non-integer). When no source numbering exists,
  LCARS synthesizes one as `<preceding regular absolute number>.
  <sequential index by air/publish date>` — e.g. a special airing
  between S1E12 and S2E1 becomes `12.1`; a second one before S2E1
  becomes `12.2`.
- **`state = skipped`**: clears backlog counters without falsely
  claiming a watch. Translated to `watched` when pushed to AniList/MAL
  (neither has a skipped concept). Show-level "completed" is satisfied
  by watched-or-skipped both. Skipped gets distinct visual treatment
  and is filterable, separately from watched, in backlog/calendar
  views.
- **Season+episode vs. absolute numbering reconciliation** is per-show
  (which side uses which scheme varies — TVDB/Sonarr can itself use an
  "absolute order" scheme for a given anime) — see
  `episode_numbering_mapping`, §5.5.

### 5.3 `watch_event`

```
watch_event
  show_id
  season               nullable — see movie note below
  episode              nullable — see movie note below
  watched_at
  platform            optional — streaming platform/device, for
                       watches with no local file
```

- **`season`/`episode` are nullable, added 2026-08-08 (A.2)**: a
  movie show (`media_shape = 'movie'`) has no `episode` row to
  reference at all (§5.1's movie-tracking-fields note), so its
  `watch_event` rows carry `show_id` alone with `season`/`episode`
  both null. Episodic shows are unaffected — always non-null there.
  Standard SQL foreign-key matching already does the right thing here
  (a FK with any null column isn't enforced for that row) — no special
  schema trick needed beyond making the two columns nullable.
- One row per viewing — the mechanism rewatches work through. No
  separate `watched_at` column on `episode`; first/last-watched/watch-
  count are derived queries over this table. Show-level started/
  completed timestamps derive from it too.
- No dub/sub tracking. No completion-detail beyond binary
  watched/not (matches aniq's existing threshold-triggers-watched
  behavior).
- **Hard-deletable** — the one deliberate exception to "nothing is
  hard-deleted by default" (§3.3). Mirrors aniq's `U` undo directly.
- **Fully independent from `status`** — a new watch_event on an
  already-`completed` show does not auto-flip status back to
  `watching`; status only changes when explicitly set.
- A dedicated bulk mutation (`markSeasonWatched`/
  `markEpisodeRangeWatched`) creates one row per episode in a range in
  a single call, for catching up on episodes already seen outside
  LCARS's tracking — distinct from the "no general bulk *import*"
  rule (§9 covers historical import specifically).

### 5.4 `show_external_id` / `show_service_presence`

```
show_external_id
  show_id
  service      tvdb | anilist | tmdb | imdb | mal | ...
  external_id
  url          ready-to-use deep link, stored directly
```

Normalized crosswalk — one row per link, extensible to new services
with zero migration. `url` is stored directly (not derived per-client
from a URL template) and used as a clickable link on the info card.
Movies key primarily on TMDB (native to Radarr) but carry
IMDB/AniList/MAL ids too where they exist.

```
show_service_presence
  show_id
  service       sonarr | radarr | anilist | mal | local | ...
  present       bool
  checked_at
```

A separate, lighter-weight concept from `show_external_id` above:
`show_external_id` only has a row once LCARS has actually **linked**
to something on that service; `show_service_presence` answers a
different question — **does a plausible match for this show exist on
that service at all**, whether or not LCARS is tracking through it
yet. Resolves the long-open "per-source tracking flags" question
(§3/§6.7 originally flagged this as needing its own round): the
answer is a dedicated table, one row per show per service, refreshed
on the same background-poll cadence as the rest of Phase B — not
columns bolted onto `show`, and not folded into `show_external_id`.
`local` is included as a pseudo-service alongside the real external
ones, rolling up per-episode `available_locally` (§5.2) into one
show-level yes/no.

**Purpose**: lets a show's full cross-service picture be queried at a
glance ("in Sonarr: yes, in Radarr: no, on AniList: yes, local file:
no"), and — the actual point of tracking this — lets LCARS **offer**
to link/add a show through a service it's present on but not yet
tracked through (e.g. found in Radarr but no `show_external_id` link
to it yet: offer to add it). This is deliberately **passive,
informational data** — a service-presence change does **not** go
through `pending_review` (§5.6); it just updates quietly, self-healing
like a dead poster URL (§3.4), because there's no "correct" value being
overwritten, only a fact being refreshed. **Matching algorithm**:
fuzzy title search — across all stored title variants (`title_romaji`/
`title_english`/`title_native`, §5.1), not just the primary one —
against each service's own catalog, above a similarity threshold (same
`difflib`-style scoring aniq's own fuzzy matching already uses, no new
dependency). A weak match stays silent rather than surfacing a noisy
"maybe?" suggestion. Low-risk choice specifically *because* this
feature is suggestion-only and never auto-applies — a false positive
just means declining a slightly-wrong offered link, not silent data
corruption, unlike the id-mapper's own reconciliation (§5.5), which
does apply automatically and so leans on the more authoritative
Fribb-seeded approach instead.

### 5.5 id-mapper / reconciliation tables

Two tables, identical shape, sharing one reconciliation mechanism
(§3.1) — kept separate rather than one polymorphic table, for cleaner
per-level foreign-key typing:

- **`show_id_mapping`** — show-level identity (which AniList entry =
  which TVDB series). Seeded from the Fribb/`anime-lists` dataset,
  manual overrides authoritative. A row with **no** confident
  auto-derived candidate at all (not just a discrepancy) still goes
  through the same `pending_review` mechanism — the show stays fully
  usable locally in an unmapped state, not blocked.
- **`episode_numbering_mapping`** — episode-level numbering
  (absolute vs. season+episode) within an already-identified show.
  Populated the same way: automatic derivation attempt (Sonarr
  absolute-order info, AniList episode counts), manual override/
  fallback when derivation isn't confident.

**Columns drafted 2026-08-08 (A.1)** — both tables are one row *per
show* (not per episode — "episode-level" describes what the mapping is
*about*, not its grain), which is why each still earns its own `x-`/
`n-` id despite the 1:1 relationship to `show`. "Identical shape" above
means the reconciliation-bookkeeping columns (`source`/`matched`/
`manual_override`/timestamps), not literally identical columns — the
two tables' actual subject-matter values differ (an identity mapping
needs two foreign ids; a numbering mapping needs one scheme value):

```
show_id_mapping
  id                    x-
  show_id               FK show, UNIQUE (one row per show)
  tvdb_id                derived/candidate TVDB series id, nullable
  anilist_id              derived/candidate AniList media id, nullable
  source                 fribb | manual | unmatched
  matched                bool — false = no confident candidate found
                          yet (the "stays usable, unmapped" case above)
  manual_override        bool — once true, auto-derivation no longer
                          overwrites tvdb_id/anilist_id (§3 principle 6)
  last_reconciled_at     last time the weekly Fribb pass (§5.5 below)
                          checked this row
  created_at / updated_at

episode_numbering_mapping
  id                    n-
  show_id               FK show, UNIQUE (one row per show)
  scheme                  absolute | season_episode — which numbering
                          convention this show's episodes are keyed by
  source                 sonarr | anilist | manual | unmatched
  matched                bool, same meaning as above
  manual_override        bool, same meaning as above
  created_at / updated_at
```

No `last_reconciled_at` on `episode_numbering_mapping` — deliberately
asymmetric with `show_id_mapping`, since only the identity mapping has
a stated recurring (weekly) reconciliation cadence below; numbering is
derived once at show-add time with no periodic re-check specified.

`show_id_mapping` also gets a **weekly** reconciliation pass against
the Fribb dataset — deliberately slower/independent of the daily
metadata-refresh cadence, since id-mapping data changes far less often
than episode/schedule metadata.

### 5.6 `pending_review`

```
pending_review
  entity_type            which table/concept the reviewed field
                          belongs to — split from a single "entity"
                          column (added while drafting A.1: the
                          original sketch had no way to say *which*
                          row, just its type)
  entity_id               the specific row's id within entity_type
  field
  previous_value        value before the *first* change in a chain
  proposed_value_chain   full sequence of intermediate values since
                          the entry opened, not just the latest
  source
  created_at
  resolved_at
  resolved_by_client     data | holodeck | captains_log — corrected
                          while drafting A.1: the original list
                          included `aniq`, inconsistent with "three
                          passive/pull places" directly below (aniq
                          has no LCARS integration at all, §7.2, so it
                          can't resolve a review)
  resolution_note        optional free-text commentary
```

The single shared audit mechanism for: id-mapper discrepancies
(both tables in §5.5), all air-date source disagreements, and MAL
legacy score import. If the same field changes again automatically
before an entry is resolved, the new value still applies immediately
(unchanged behavior) but the entry's value chain grows rather than
silently overwriting — so review shows the full original → intermediate
→ current sequence. Entries are retained permanently once resolved,
never purged.

Surfaces in three passive/pull places, no push/email (accepted gap):
- Data's status bar.
- The Holodeck dashboard's foregrounded review queue (not just a
  badge).
- A Captain's Log notice on any command run, which offers an **inline
  interactive resolve prompt** on the spot (a non-interactive/
  scripting flag is worth adding once Captain's Log's scripting use
  cases are designed in detail, §10).

### 5.7 History tables

Dedicated per concern: `status_change`, `score_change`,
`air_date_change`, `tracked_change`, and any future one — not a single
generic audit table. Every row records the originating client/process
(`aniq | data | holodeck | captains_log | sonarr_sync | anilist_sync |
...`).

### 5.8 `person` / `show_person` / `studio` / `show_studio`

```
person
  id
  name
  portrait_url          URL only, same pattern as show art
  external_service
  external_id
  external_url

show_person
  show_id
  person_id
  role_type              voice_actor | actor | staff
  character_name
```

Queryable from the person side too ("what else has this person
appeared in, across the tracked library"), not just show → cast.

```
studio
  id
  name
  external_service
  external_id
  external_url

show_studio
  show_id
  studio_id
  role_type              studio | publisher | network
```

A dedicated entity, not a raw cached string on `show` — mirrors
`person`/`show_person`'s exact shape (same reasoning: queryable from
the studio side too, "what else has this studio made, across the
tracked library," not just show → studio display text). `role_type`
distinguishes the animation studio itself from a publisher/licensor or
a broadcast network, the same three concepts the old raw
"studio/publisher/network" info-card field used to collapse into one
string.

### 5.9 `show_relation` / `franchise` / `franchise_member`

```
show_relation
  show_id            the show whose AniList data reported this link
  related_show_id    FK to show — may point at a tracked = false stub
```

**Added 2026-08-08, filling a gap found while drafting A.1**: every
other passage discussing franchise auto-derivation ("the relation
graph," §5.1/§5.9/§6.10) assumed this table already existed; it had
never actually been defined. **Directed edges, stored as-ingested** —
one row per direction, written whenever a show's AniList data is
fetched and reports a relation (each show's own AniList page lists its
relations independently, so the two directions can arrive at different
times or only one direction may ever be populated). No dedup/
normalization at write time — franchise auto-derivation below treats
the graph as undirected (either direction counts as a link), consistent
with `show_service_presence`'s (§5.4) "just reflects reality" treatment
of other auto-populated, non-`pending_review` data. Composite primary
key `(show_id, related_show_id)`; no join-table id prefix spent on it,
same reasoning as the other pure link tables (§5.0).

```
franchise
  id
  name

franchise_member
  franchise_id
  show_id
  sort_order      auto-defaults to release-date order; manually
                   overridden only when intended watch order differs
                   from release order (e.g. a movie meant to be
                   watched mid-series)
```

Auto-derived from `show_relation` above (pairwise prequel/sequel/
related links, which can point at untracked stubs), manual override
authoritative. No description or franchise-level art in scope. Movies
fully participate alongside TV/anime. **Relation links stay
undifferentiated** — no per-link relation-type field (prequel vs.
side-story vs. adaptation, etc.); confirmed generic on purpose, kept
consistent with how the relation graph was already framed everywhere
else in this document. Franchise auto-derivation treats every link the
same way.

**`next_up_override`** — added 2026-08-08 while drafting A.2, filling
a gap: §6.4's cross-show next-up query was always described as having
"manual reordering supported on top (same auto-default-plus-override
shape as franchise ordering)," but unlike franchise ordering
(`franchise_member.sort_order` right above), nothing in §5 actually
stored that override — the next-up list itself is a computed query
result, not a stored entity, so it needed its own small table rather
than a column on anything else:

```
next_up_override
  id         v- (§5.0)
  show_id     FK show, UNIQUE
  sort_order
```

### 5.10 Saved filter presets

Server-side entity, freely editable from any client — not read-only,
not fixed.

---

## 6. Functional areas

### 6.1 Scoring & conversions (Phase A)

- Personal scale: **0–20, quarter-point native** (16.00, 16.25, ...).
  Out-of-range/off-grid input clamps/rounds silently at write time
  rather than being rejected.
- → AniList `POINT_100`: `× 5`, exact, push-only. Anilist's pre-existing scores
  get a one-time import offer via `pending_review` (apply, then flag,
  same as everything else).
- → MAL (0–10, integer): `÷ 2`, push-only.
- MAL's API details are verified, not a sketch (§6.9, §10.1). What's
  still deliberate is its **role**: MAL is treated as a lower-
  confidence **backup mirror of AniList**, not an independently-
  curated list — it should hold the same set of tracked shows, kept
  in sync via the same push path every time AniList is written to
  (§6.9). Formal discrepancy detection between MAL/AniList and LCARS
  isn't built yet — that's the drift-detection work called out in
  principle 5 (§3) and §10.6 item 12. Until it exists, MAL parity is
  best-effort: it holds whatever Starfleet has pushed to it, with no
  automated check that it still matches.

### 6.2 Paced/catch-up mode (Phase A schema, Phase B scheduling)

Restricted to completed/non-airing shows only — an airing show never
gets fake dates. Per-show configurable cadence (default weekly,
editable). **Adaptive, not pre-baked**: next synthetic date = latest
`watch_event.watched_at` + cadence interval, recomputed on every
watch. Falling behind resets from the last watch — no backlog
accumulation for paced shows specifically (that's the separate feature
below).

### 6.3 Backlog visualization (airing shows, Phase B)

Distinct from pacing. Surfaces in **both** Data's `B` view (inherited
from aniq at the fork) and a new calendar-native counter line under a
show's next-episode entry. Mark-watched from the counter clears
exactly one oldest episode per action, no bulk-clear.

### 6.4 Cross-show "next up" query (Phase A/B)

One aggregated query, across all `watching`-status shows **and**
paced/catch-up shows, returns the next available episode per show —
one unified "what's next" view regardless of why an episode is next.
Defaults to soonest-available-first, with manual reordering supported
on top (same auto-default-plus-override shape as franchise ordering).

### 6.5 Search (Phase A)

Full-text search across titles (and reasonably synopses) is a
first-class GraphQL query, not left to per-client fuzzy matching —
centralizes what aniq's `list_screen.py` currently does locally via
`difflib`, so Holodeck/Captain's Log get equivalent search without
reimplementing it.

### 6.6 Stats/analytics (Phase A query surface, needs Phase B data to be meaningful over time)

Full stats view: totals, hours watched (using `duration_minutes`/
`runtime_minutes`, §5.1/5.2), personal-score distribution. Explicitly
**not** in scope: genre/year/tracking-space score breakdowns (the raw,
unnormalized genre data doesn't support a meaningful breakdown anyway
— see §5.1).

### 6.7 Sync & reconciliation policy (Phase B)

- **Metadata refresh**: `watching`-status, actively-airing shows get a
  daily automatic refresh, plus an on-open trigger capped to the same
  once-per-day ceiling (not stacking). Not-airing/not-`watching` shows
  get no background refresh (a monthly rolling option is named, not
  implemented). A fourth, uncapped trigger — immediate fetch on show
  creation (§4, Phase A) — is independent of this cadence.
- **Air-date source priority**: Manual > animeschedule.net (REST API
  v3, `/timetables`, preferred over its RSS feeds — §10.1) > AniList
  `airingSchedule` > Sonarr raw. This governs which value *wins* when
  applied automatically — it does **not** gate the apply.
  Every automatic change from any source, including one overwriting a
  manual value, applies immediately and logs a `pending_review` entry
  (§3.1/§5.6).
- **Service-level health**: per-integration reachability/rate-limit
  status (Sonarr/Radarr/AniList/animeschedule.net), distinct from any
  individual show's tracking state. Tracked by Ops (Phase B's
  scheduler); surfaces in Data's status bar as **per-source**
  indicators, not one aggregate value.
- **Per-show service presence** (`show_service_presence`, §5.4):
  refreshed on the same background-poll cadence, distinct from both
  service-level health above and per-show *tracking* state — this is
  "does a match for this show exist on service X at all," independent
  of whether LCARS currently tracks through it. A show disappearing
  from Sonarr/Radarr's own library entirely (not just missing a file)
  surfaces here too — `present` flips to `false` on the next poll, no
  `pending_review` entry for this specifically, purely informational.

### 6.8 AniList write ownership — hybrid (Phase A concept, Phase C endpoint)

- Data keeps a **permanent, narrow direct-to-AniList write path** for
  episode watch-status only — writes to AniList and LCARS
  simultaneously, no timer wait. Data retains its own AniList OAuth
  credentials indefinitely for this one path.
- Everything else (status, score, retrying any failed watch-status
  write) is LCARS's job server-side; LCARS remains unconditionally
  authoritative regardless of which path wrote first.
- **Rewatching never auto-pushes AniList's `REPEATING` status.**
  Consistent with §5.3's rewatch/status independence: a new
  `watch_event` on an already-`completed` show never changes what
  LCARS pushes for status, on AniList or anywhere else. AniList shows
  `COMPLETED` throughout a rewatch unless the user explicitly sets a
  different local status.
- Interactive add-show/id-remap flows do **not** get the same direct
  fast-path — they proxy through LCARS so its reconciliation logic
  applies consistently.

### 6.9 MAL integration (verified — §10.1 research pass)

All four original assumptions confirmed against MAL's real API docs:
- **OAuth2 with a real refresh token**, confirmed, with concrete
  details: authorization at `/v1/oauth2/authorize`, token exchange at
  `/v1/oauth2/token`, **PKCE mandatory** (plain method only, 43–128
  char verifier). Access tokens last **1 hour**; refresh tokens last
  **1 month** — notably short (shorter than AniList's), a real
  operational point: LCARS needs to proactively refresh well before
  the 1-month expiry (e.g. weekly, matching other Ops cadences, §6.7)
  rather than risk a lapsed token forcing an interactive re-auth.
  Registration is free/self-service (client_id + client_secret on
  approval).
- **Status enum**: exactly `watching | completed | on_hold | dropped |
  plan_to_watch` — confirmed, maps cleanly onto LCARS's own 5-value
  enum (`on_hold`≈paused, `plan_to_watch`≈planned) exactly as assumed.
- **Score**: confirmed 0–10 integer — the `÷2` forward mapping (§6.1)
  stands as designed.
- **Rate limits**: confirmed genuinely unpublished officially, but
  the practical community-consensus safe limit is **~60 req/min
  (1 req/s)** — use that as the working default rather than treating
  it as an official number.

**New finding, not previously known**: MAL's list-status object
carries `is_rewatching` (bool) and `num_times_rewatched` (int) —
MAL's equivalent of AniList's `REPEATING` status (§6.8). Same policy
extended for consistency: **rewatching never auto-toggles these
either** — `is_rewatching` only changes on an explicit user action,
never inferred from a new `watch_event` on an already-`completed`
show, same as point 134's AniList decision.

Push-only for now, confirmed, same as AniList (§6.8's principle) —
treated as AniList's backup mirror (§6.1), same tracked-show set.
Drift detection against MAL's actual state is deferred future work,
not designed yet — see §3 principle 5 and §10.6 item 12.

### 6.10 Movies (Phase A schema, Phase B sync)

Sourced via **Radarr**, same "query the API, never scan the
filesystem" rule Sonarr already follows — file-availability check is
structurally identical to the Sonarr-episode case, just against
Radarr's movie-file/queue endpoints. Primary id TMDB, also carries
IMDB/AniList/MAL via the crosswalk. **Purely local-only** — no
external write-through, no Letterboxd/Trakt-equivalent target. Fully
participates in the relation/franchise graph. Uses the same 5-value
status enum as episodic shows, unchanged.

### 6.11 Deletion policy (Phase A)

Soft is always the default and first option — status/tracked-flag
change, full history retained. Hard delete exists but is layered:
soft-delete → a delay period → an explicit re-type-the-show's-title
confirmation before the actual purge executes. Reachable from both
Captain's Log and Holodeck — the re-type step is the friction, not a
client restriction.

### 6.12 Data export / import (Phase A)

JSON, explicitly designed as a **restore path**, not just a one-way
backup — the motivating scenario is rebuilding a fresh LCARS instance
once Trakt is gone and AniList/MAL are push-only. Includes an
explicit `schema_version` field (simple incrementing integer, no
semver) from the very first implementation. Import **rejects on any
version mismatch** with a clear error — no auto-migration logic to
build or maintain; upgrading an old export before re-importing is a
manual, out-of-band step.

### 6.13 Global settings (Phase A)

- **Home timezone**: a single configurable setting (default
  `Europe/Dublin`), not a fixed UTC boundary. Used consistently
  everywhere a "day" needs a boundary: "today" on the calendar, the
  paced/catch-up mode's cadence reset (§6.2), and the daily
  metadata-refresh/on-open-cap cadence (§6.7). All stored timestamps
  (`air_date_utc`, `watched_at`, etc.) stay UTC internally — this
  setting only controls how they're bucketed into calendar days for
  display and scheduling logic, not how they're stored.

---

## 7. Clients

### 7.1 Data (the TUI client under active development)

A one-time fork of aniq (§4.0), not a rewrite from scratch — inherits
aniq's full current behavior at the fork point, then evolves through
Phases A/B/C independently. Phased drawdown per §4: full local logic
(inherited) → LCARS-backed reads in Phase B → thin front-end +
mpv/aninote bridge + the one permanent direct AniList write path in
Phase C. Status bar gains per-source sync-health indicators once
Ops's service-health concept exists (§6.7). Freely modifiable — this
is where all Starfleet-integration build/test work happens. Becomes
the permanent daily-driver client at Cutover (§4.4).

### 7.2 aniq (frozen reference, then archived fallback)

Existing repo, read-only reference from Starfleet's side, **never
modified from here, at any point in this roadmap**. Keeps running
standalone (Trakt + AniList) as the actual daily driver throughout
Phases 0–C, completely unaffected by Data/Starfleet's build-out. At
Cutover (§4.4), archived as a known-working emergency fallback — not
deleted, not developed further.

### 7.3 Holodeck (full editor)

A first-class peer client, not read-only. Dashboard leads with two
foregrounded panels: upcoming episodes/calendar, and the pending
review queue itself (not just a badge). Backlog view and a recently-
watched activity feed are each their own dedicated screen, not
dashboard elements — mirroring Data's (inherited) calendar-first home
+ separate `B` view. Can reach the full hard-delete confirmation flow
(§6.11).

### 7.4 Captain's Log (`cl`)

One tool, no split between "day-to-day" and "admin" modes — mark
watched, set status/score, add show, resolve pending reviews,
export/import, id-remap, and broader scripting/bulk-ish queries all
live in the same tool, invoked as `cl` (e.g. `cl show mark-watched
...`). Any command run surfaces an inline pending-review resolve
prompt if items are outstanding (§5.6).

---

## 8. API shape

**GraphQL**, single static bearer token auth (stored the same way aniq
already stores AniList/Trakt credentials — `config.ini` + `keyring`).
Deliberately designed query/filter shapes (not generic pass-through
filtering) for: shows by status, episodes airing in the next N days,
the pending-review list, the backlog, full-text search, the cross-show
next-up list. Dedicated field-specific mutations
(`setStatus`, `setScore`, `addWatchEvent`, `markEpisodeSkipped`,
`markSeasonWatched`, etc.) rather than one generic `updateShow(...)`.

The actual schema (types/queries/mutations as GraphQL SDL) does not
exist yet — this document settles *that* it's GraphQL and *what*
query/mutation shapes are needed, not the schema text itself (§10).

---

## 9. Explicitly out of scope / deferred

- Content warnings/age rating data — deferred, not part of the active
  field list (low value for a single-user tracker).
- Genre-breakdown / year-breakdown / tracking-space-breakdown stats —
  not building these; would require a canonical genre taxonomy that
  isn't otherwise justified.
- A dedicated favorite/pinned flag — custom tags already cover it.
- Personal notes/review text field — a client concern (aninote is
  anime-only and client-side already; any TV/movie equivalent would
  live in a client, not the database).
- Push/email notification for pending reviews — accepted gap, three
  passive surfaces are enough for now.
- General bulk *import* beyond the planned one-time historical imports
  (Trakt, AniList, MAL) — day-to-day bulk mark-watched (§5.3) is a
  separate, in-scope capability.
- ~~Deployment/hosting/DB-engine specifics~~ — **resolved**, see §11.
- Any ongoing sync between aniq and Data during the build-out — Data
  is a clean, one-time fork with no periodic re-sync (§4.0). A
  possible one-time reconciliation of aniq's own watch-history/
  episode-numbering data into Data/LCARS is parked until aniq is
  actually about to be retired at Cutover (§4.4), not before.
- **Ongoing detection of new files added outside Sonarr/Radarr** going
  forward (e.g. personal backups/rips added later) — deliberately
  **not** a change to the "always query the API, never scan the
  filesystem" rule (§6.10 and elsewhere); would stay a rare, explicit,
  user-triggered utility (likely a Captain's Log maintenance command),
  not part of Ops's continuous polling model (§6.7). Low-priority per
  the user, not designed in detail. Distinct from the **one-time
  pre-Cutover audit of already-existing untracked files**, which is
  *not* deferred — see §4.4.

---

## 10. Next discussion plan

Most of what's left is no longer resolvable by more conversation —
it's either research/investigation work, deliberately parked topics,
or literal schema-drafting that should happen once, in its own focused
pass rather than piecemeal. Organized by what kind of work each item
actually needs:

### 10.1 Research — all resolved

~~1. `file_source` per-episode assumption~~ — **resolved** against
   three real library examples (an Apothecary Diaries movie, the
   Evangelion movies, The Dangers in My Heart: The Movie). Per-episode
   granularity confirmed correct; the field shape itself was refined
   from a single enum to two independent booleans (§5.2) after real
   data showed `bonus_movie`-kind episodes are commonly available via
   *both* Sonarr and Radarr, added asynchronously. aniq's own
   codebase was checked first and confirmed to have **zero** existing
   Radarr/movie handling — this design has no prior-art to lean on,
   it's working from real data alone. Not exhaustively tested against
   every possible edge case (the user recalled other issues existing
   historically without specifics) — treat as strong, not
   absolutely final, if a genuinely new pattern surfaces later.
~~2. animeschedule.net feed technical details~~ — **substantially
   resolved**. Real finding: a full **REST API v3** exists
   (`/api/v3`), not just RSS as originally assumed — `/anime/{slug}`,
   `/anime` (searchable/filterable directly by `mal-ids`/
   `anilist-ids`/`anidb-ids` — no fuzzy matching needed), and
   `/timetables/{airType}` (`raw`\|`sub`\|`dub`\|`all`) returning
   structured `TimetableAnime` objects with explicit `episodeDate`/
   `episodeNumber` fields — a materially better fit than RSS for this
   schema. Three public RSS feeds also confirmed live (`/jpnrss.xml`,
   `/subrss.xml`, `/dubrss.xml` — RFC 2822 `pubDate`, UTC) but with
   thin, free-text items (no structured episode number or external id)
   — kept as a documented fallback, not the primary path. Rate limit:
   **120 req/min, global** (their own docs flag it as subject to
   change). Format: JSON, lowerCamelCase, ISO 8601 UTC. **Still
   unverified, needs an actual test call rather than more doc-reading**:
   whether the read-only `/anime`/`/timetables` endpoints require an
   OAuth2 bearer token at all, and the app-registration process itself
   (free? approval needed?) — low-stakes unknowns, easy to resolve once
   building starts.
~~3. MAL API verification~~ — **fully resolved**, all four original
   assumptions confirmed accurate (OAuth2+refresh token, 5-value
   status enum, 0–10 score, unpublished-but-now-practically-known rate
   limit). See §6.9 for the complete findings, including two new
   details the original sketch didn't anticipate: refresh tokens only
   last 1 month (needs proactive renewal), and an `is_rewatching`
   field exists (same auto-toggle policy as AniList's `REPEATING`,
   point 134, now applied here too).

### 10.2 Schema-drafting work (mechanical — see `BUILD_PLAN.md`)

~~4. `episode_numbering_mapping`'s exact column list (§5.5)~~ —
   **resolved** 2026-08-08 during `BUILD_PLAN.md` A.1, see §5.5.
5. The actual GraphQL schema (SDL) — types, full query/mutation
   signatures — the *shapes* are settled (§8), the text isn't written.
~~6. `show_id_mapping`'s exact column list~~ — **resolved** alongside
   item 4, see §5.5.

~~7. `show_service_presence`'s matching algorithm~~ — **resolved**:
fuzzy title search across all stored title variants, threshold-gated,
same `difflib`-style scoring aniq already uses. See §5.4.

### 10.3 Build order plan — see `BUILD_PLAN.md`

8. ~~Write a complete build order plan~~ — **done**, see
   `BUILD_PLAN.md` in this directory. Every step cross-referenced back
   to the section of this document it implements.

### 10.4 Resolved: implementation stack

9. ~~Deployment/hosting/DB-engine~~ — **no longer parked**, resolved in
   a dedicated stack round. See §11 for the full decision (language/
   build strategy, database, hosting, build pipeline) and §5.0 for the
   id scheme. Physical host (§11.3) and the migration-tool choice
   (§11.2) are both now fully confirmed, not just proposed defaults.

### 10.5 Resolved: naming

10. ~~Naming for the whole ecosystem and its parts~~ — **resolved and
    executed**. See the "Naming" section near the top of this
    document for the full rename table and rationale. The on-disk
    rename is done — this repo, `~/repos/starfleet`, *is* that rename.

### 10.6 Deferred, not scheduled

11. Ongoing filesystem drift detection for newly-added non-service
    files (§9) — explicitly low-priority, ok to leave undesigned.
    Distinct from the pre-Cutover audit (§4.4), which **is** scheduled
    — see `BUILD_PLAN.md`.
12. **AniList/MAL drift detection** — not part of the original design
    (added 2026-08-08). Motivation: the user may edit AniList/MAL
    directly (e.g. from a phone app) during a period Starfleet is down
    or unreachable, and wants LCARS able to catch and surface that
    drift on the next sync rather than silently overwrite it with a
    stale local value on the next push. Shape not designed yet — likely
    candidate: a periodic read-only pull of AniList/MAL list state,
    diffed against LCARS's own status/score/progress per show, surfaced
    through the existing `pending_review` mechanism (§5.6) rather than
    a new one. **Unlike item 11, this is not permanently parked** —
    build push-only sync first (Phase B, §6.7/§6.8/§6.9), prove it
    stable, then design and schedule this as a later phase (see
    `BUILD_PLAN.md`'s "Deliberately not on this plan" section, which
    tracks it the same way).

---

## 11. Implementation stack

Resolved through a dedicated stack round, layered on top of the pure
data-model design in §1–§10 — full rationale in the discussion memo.

### 11.1 Language & build strategy

- **Python first**, schema-first GraphQL (hand-written SDL wired to
  resolvers — e.g. Ariadne — rather than generating the schema from
  code, e.g. Strawberry). Matches aniq's own language, and keeps
  iteration fast while functionality is still settling through real
  use, not just discussion.
- **A ground-up Rust rewrite is the deliberate long-term plan**, once
  functionality and behavior are proven stable through real daily use
  (a maturity gate, likely somewhere around/after Cutover, §4.4 —
  not a fixed date). LCARS keeps its name across the rewrite — the
  backend's identity doesn't change just because its implementation
  language does.
- Why this is cheap rather than doubled effort: the database schema
  and the GraphQL contract are both language-agnostic — clients (Data,
  Holodeck, Captain's Log) only ever see the wire contract, so a
  backend language swap is invisible to them as long as the schema
  itself doesn't change across the rewrite. The hard-won design work
  (this document) is captured independent of implementation language —
  the rewrite is a re-implementation of an already-fully-specified
  system, not a re-design. Same "iterate somewhere safe, harden once
  proven" philosophy already applied to Data (§4.0), one layer down
  the stack.

### 11.2 Database

- **SQLite.** Zero-ops, single file, trivial to back up alongside the
  JSON export (§6.12). More than enough write volume for a
  single-user personal tracker. Also the easiest engine to carry
  cleanly across the eventual Python→Rust rewrite — both ecosystems
  have mature SQLite support.
- Schema migrations: **Alembic**, confirmed. Distinct from the JSON
  export's own `schema_version` field (§6.12), which versions the
  *export format*, not the live database.
- **DB access layer: raw `sqlite3` (stdlib), no ORM** — resolved
  2026-08-08, during Phase 0 scaffolding (`BUILD_PLAN.md` 0.2).
  Alembic migrations are hand-written SQL (`op.execute(...)`), not
  autogenerated from ORM model classes. Consistent with §11.1's
  hand-write-don't-generate stance on the GraphQL SDL — applied here
  too, rather than introducing SQLAlchemy (Core or ORM) as a second,
  inconsistent generation mechanism. Resolvers work with plain
  rows/dicts.
- **Filenames**: database file `lcars.db`; config file `lcars.ini`
  (bearer token, Sonarr/Radarr/AniList credentials, home timezone
  default, etc. — same `config.ini` + `keyring` pattern aniq already
  uses, per §8).
- **DB execution model: sync resolvers, one shared connection** —
  resolved 2026-08-08 during A.3. The ORM-vs-not decision above didn't
  itself settle *how* a blocking stdlib driver (`sqlite3`) gets called
  from resolvers bound into an async ASGI app (Ariadne/uvicorn, §11.1)
  — a real gap, asked rather than assumed. Resolvers are plain sync
  Python functions; one `sqlite3` connection opens at app startup and
  is reused for its lifetime, no threading or locking. Uvicorn's
  default single worker runs one event loop on one thread, so nothing
  ever touches the connection concurrently even with multiple clients
  active at once (§3 principle 8) — their calls simply interleave
  sequentially. Each DB call briefly blocks the event loop, but local
  SQLite file operations are sub-millisecond, well within the
  personal-scale framing used throughout this document. Explicitly
  **not** `asyncio.to_thread()`-wrapped calls — considered and rejected
  as unnecessary complexity for this scale.

### 11.3 Hosting & build pipeline

- **Containerized (Docker), deployed as an additional service in the
  existing Sonarr docker-compose stack** — confirmed: same host as
  Sonarr/Radarr, joining their existing compose setup rather than a
  separate machine/stack. Resolves the "exact physical host" question
  left open earlier in the stack round.
- **CI builds the image directly from the LCARS repo** — a GitHub
  Actions workflow, **hybrid trigger**: every push to `main` builds
  and runs checks only (compiles, tests pass) without publishing —
  cheap validation, no churn on the running stack; publishing an image
  only happens on an explicit tag/release, so what's actually pulled
  into the Sonarr stack never changes underfoot unexpectedly. Registry:
  **GitHub Container Registry (ghcr.io) proposed as the default**,
  since it pairs naturally with GitHub Actions — no separate registry
  credentials to manage, `GITHUB_TOKEN` handles auth. Not separately
  confirmed; flag if Docker Hub or something else is preferred
  instead.
- A Docker image also makes the eventual Rust rewrite a clean drop-in
  swap of one image for another, with no change to how it's deployed
  or how it joins the Sonarr stack.

### 11.4 ID scheme

See §5.0 — short, type-prefixed, human-readable ids
(`{prefix}-{6-char Crockford-style string}`), not auto-increment
integers or UUIDs, with collision-checked generation via `nanoid`.
