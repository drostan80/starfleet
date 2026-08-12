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
  user action, not a background job. **Clarified 2026-08-08 (A.8),
  asked directly rather than assumed**: this text reads two ways —
  LCARS calling out itself, or a client (Data) fetching and pushing
  results in, the way A.4/A.7 originally split that work. Confirmed
  the former: "ultimately all compute and fetch will be handled on
  the server by lcars, so might as well built it this way now" — a
  deliberate reversal of A.4/A.7's own original framing once it
  became clear that framing was only ever a Data-shaped transitional
  stage, not the end state (Data "has inherited accesses and
  responsibilities that it will lose when becoming a thin client",
  per Phase B/C's own drawdown). **The one exception, confirmed
  explicitly**: pushing watch status *to* AniList/MAL (the
  OAuth-authenticated write path) stays Data/aniq's own job, entirely
  unaffected by this — this item is about metadata *fetch* only.
- **No autonomous scheduler yet** — no daily refresh loop, no weekly
  Fribb reconciliation loop, no Sonarr/Radarr/animeschedule.net
  polling running on its own clock. Those are Phase B.
- Data's side: replace the Trakt client with an LCARS client of the
  same shape. Data (inheriting aniq's current logic at the fork point)
  keeps doing what aniq does today (polling Sonarr, calling AniList,
  running its own air-date patch) but also writes the resulting state
  to LCARS. AniList dual-write already exists in the forked code and
  is unaffected. **Superseded for metadata fetch specifically by the
  clarification above** — Data's own Sonarr/AniList calls (A.17)
  remain a second, independent path (e.g. useful while Data still
  needs them for its own local features), not the one A.8's on-demand
  fetch itself relies on.
- Reliability note: aniq's existing "queue locally, retry on flush"
  pattern (`trakt_queue.py`/`watch_queue.py`), inherited by Data at the
  fork, carries over to the Data→LCARS path — a home server isn't
  guaranteed reachable from wherever Data happens to be running.
  **Implemented 2026-08-08 (A.17, `~/repos/data`)**: `lcars_queue.py`
  mirrors `watch_queue.py`'s own shape exactly for this. Real scope
  correction made mid-build, directly from the user: Data does **not**
  get a `showByExternalId`-style lookup/dedup query, and doesn't need
  one — "data will add shows only by bridging to lcars, [LCARS] is the
  one actively adding shows" (2026-08-08). Data's own Sonarr-add flow
  now bridges straight into LCARS's `addShow` at the moment of adding
  (`_track_new_show_on_lcars`), remembering the returned id locally
  (`lcars_ids.py`) — this *is* the add, not a second, independent
  lookup against something that might already exist. LCARS is
  push-only from Data's side this phase — nothing in §4's own text
  promised a read-back path yet ("LCARS-backed reads" stay Phase B) —
  so every LCARS-facing display in Data now reflects only locally-known
  state (bridged-or-not, currently-queued), a deliberate simplification
  versus Trakt's own richer live-fetched display. `trakt.py`/
  `trakt_queue.py`/`trakt_cache.py`/`trakt_status.py` and the now-
  target-less `scripts/pogdesign_import.py` (one-time PoGDesign→Trakt
  migration, already run) deleted outright.
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
- **Pre-Cutover step, required, timing otherwise flexible**: confirm
  the local file audit (§5.2/§6.10's B.3b — `auditLocalFiles`,
  reachable via `ops audit-local-files`) has actually been run at
  least once, so nothing already on disk gets silently lost or
  forgotten once aniq (and its own file-awareness) is archived. **No
  longer "likely built as a script"** — B.3b built this as a permanent
  mutation once the user confirmed they want it re-runnable, not a
  one-off tool, so this Pre-Cutover step is now a verification/
  reminder, not a build task. Distinct from §9's *ongoing* detection of
  newly-added non-service files, which stays deferred/low-priority.

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
| ~~`x-`~~ | ~~`show_id_mapping`~~ — **retired 2026-08-08 (A.4)**, see §5.5: replaced by `season`. Not reassigned — nothing was ever deployed with it, but retiring rather than reusing the letter keeps any stray historical reference (docs, old branches) unambiguous. |
| `n-` | `episode_numbering_mapping` |
| `a-` | `show_service_presence` |
| `q-` | saved filter preset |
| `c-` | `status_change` |
| `o-` | `score_change` |
| `g-` | `air_date_change` |
| `k-` | `tracked_change` |
| `m-` | `episode_movie_link` |
| `v-` | `next_up_override` |
| `z-` | `season` — added 2026-08-08 (A.4), see §5.5 |
| `u-` | `untracked_show_finding` — added 2026-08-10 (B.11e), see §5.2's own "Resolved 2026-08-10 (B.11 reconnaissance)" note |
| `y-` | `show_merge` — added 2026-08-11 (B.14), see §5.11 |

20 of 26 letters used (`x-` retired, not counted), leaving headroom
for future entities.

### 5.1 `show`

Three independent axes describe a show's identity — not one `kind`
enum:

| Axis | Values | Notes |
|---|---|---|
| `media_shape` | `episodic` \| `movie` | |
| `tracking_space` | `tv` \| `anime` | `anime` **mandates** an AniList link, no exceptions — applies orthogonally to `media_shape`, an anime movie still requires one. `tv`'s Sonarr link is optional. **Enforced for real as of 2026-08-09 (A.24)** — see below. |
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
  `available_via_radarr` (3-state `unavailable | downloading |
  available`, same B.3 refinement as `episode`'s own field, §5.2),
  `file_path_radarr` (added B.3, no Sonarr-side counterpart — a
  standalone movie show has no Sonarr availability at all),
  `available_checked_at`, and a generated `available_locally` (mirrors
  `episode`'s own shape, §5.2, just Radarr-only — no Sonarr side to OR
  against, since a movie show's own Sonarr availability doesn't apply —
  see `episode_movie_link` below for the *separate* case of the same
  film also being tracked as a `bonus_movie`-kind episode elsewhere).
  No movie-specific `skipped`
  equivalent — confirmed sufficient to reuse `status` alone
  (`dropped`/`completed` already cover "decided not to watch"/
  "watched"); `episode.state = skipped` exists specifically to clear
  per-episode backlog counters (§6.3) on an accumulating list, which a
  single movie doesn't have.

**The `anime` → AniList-link mandate, made real 2026-08-09 (A.24,
second consolidation audit)**: nothing had ever enforced it. Migration
`7196ca889757`'s own comment asserted it was "an application-layer
invariant (enforced by addShow/on-demand-fetch, A.8)" — false;
`addShow` accepted an anime show with zero external ids, and
`_fetch_anilist` merely returned early, treating the absence as caller
input rather than a failure. The concrete harm was severe and silent:
Data's bridge (A.17) never sends an `anilistId` — its own
`lcars_client.py` says so ("data's own add flow often doesn't [have
it]") — so **every anime show added through the primary production path
stayed permanently bare**: no synopsis, poster, cast, studio credits or
duration, unfixable even by `refreshShowMetadata`. A.20 made it sharper
still, resolving the correct AniList id into the *season* row moments
later without ever applying it one level up.

Resolved by making identity resolution precede metadata fetch: for an
anime show with no show-level `anilist` link, LCARS resolves one itself
from the Fribb dataset via the show's tvdb id (season 1 — the
convention `addShow`'s own `anilistId` input and `_upsert_season`
already assume), writes a real `show_external_id` row, and only then
fetches. A caller-supplied id is never overwritten (§3 principle 6).
When nothing can be resolved — no tvdb id, or no Fribb match — a
`pending_review` opens and the show is still created: hard rejection
would break Data's bridge on every anime add, and §3 principle 1's
"apply, then flag, never gate" governs here as everywhere else.

**Movie ↔ Sonarr-tracked-episode reconciliation** (added 2026-08-08,
A.2): the *same* film can exist both as a standalone Radarr-tracked
movie show and as a `bonus_movie`-kind episode inside a different,
related episodic show (Sonarr sometimes carries a tie-in movie as a
season-0 special). Reconciled the same way every other cross-source
identity question in this document is (§3.1, §5.5) — internal ids are
the source of truth, external ids (TMDB) map onto them, a best guess
applies immediately with a `pending_review` entry on ambiguity, manual
override wins once set. New table, same reconciliation shape as the
other id-mapper tables (originally written as "same shape as
`show_id_mapping`" — that table was retired 2026-08-08/A.4, replaced
by `season`; the shape reference still holds, just pointed at its
successor now):

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

**Resolved 2026-08-10 (B.8b)**: the automatic `tmdb_match` half never
existed until this step (only the enum value and A.3's manual
mutation did). Checked before designing anything: a `bonus_movie`-kind
episode carries no title and no TMDB-comparable id at all (Sonarr is
TVDB-native; `episode` itself has no title column), so matching can't
work from the episode side directly. Candidates are narrowed via
`show_relation` instead — every tracked `media_shape = 'movie'` show
already related to the episode's parent show (either direction, same
graph A.21's `_link_relation` populates) — confirmed with the user
over the alternative (a broad fuzzy title match across every tracked
movie show, `service_presence.py`'s own B.7 approach): this field
auto-applies a real identity write, unlike B.7's suggestion-only
presence flag, so the narrower, higher-confidence pool was preferred.
Exactly one candidate applies immediately; zero is a real `unmatched`
outcome; more than one opens a `pending_review`. `bonus_movie`
promotion itself is explicitly **not** part of this mechanism — A.25
already established Sonarr can't distinguish it from `special`/`ova`
at all, so there is no automatic classification signal to build
against; `setEpisodeKind` (manual) stays the only route in. See
`episode_movie_link.py`'s own module docstring and BUILD_PLAN.md's
B.8b entry for the full reasoning, including the availability-sync
half (`available_via_radarr`/`file_path_radarr` mirrored from a
matched link's movie show onto the episode — the gap §5.2's B.3 note
leaves open below).

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
  season_id             FK season (§5.5), added 2026-08-08 (A.4) —
                         alongside the plain integer `season` above,
                         not replacing it; `season` stays for raw
                         Sonarr-numbering compatibility, `season_id`
                         is the link to that season's own cross-
                         service identity (§5.5's `season` table)
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
  available_via_sonarr   unavailable | downloading | available —
                         **refined from bool to 3-state, B.3, see below**
  available_via_radarr   unavailable | downloading | available — same
                         refinement, only ever populated for
                         `bonus_movie` kind (see below)
  file_path_sonarr       the actual imported file path, Sonarr side —
                         **added B.3**, nullable, cleared when the
                         source flips back to unavailable/downloading
  file_path_radarr       same, Radarr side — **added B.3**
  available_locally      derived: available_via_sonarr = 'available'
                         OR available_via_radarr = 'available' —
                         `downloading` does not count as locally
                         available (refined, B.3)
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

**Resolved 2026-08-09 (B.3)**: `BUILD_PLAN.md`'s own B.3 line
("Sonarr/Radarr polling for file availability... queue/episode-file/
movie-file endpoints") named the general area but not the exact
mechanism, cadence, or scope — asked directly, then verified against
the user's own real, live Sonarr (4.0.19.2979) and Radarr (6.3.0.10514)
instances rather than built from documentation memory, the same
"resolve by testing, not more reading" practice §10.1 already used for
animeschedule.net.

- **Availability is 3-state, not boolean**: `unavailable | downloading
  | available`. A user's own framing settled this directly: a file
  isn't just present-or-absent — "grabbed not imported" is a real,
  useful, distinct `downloading` state, valuable specifically because
  it surfaces a show Sonarr grabbed but failed to auto-import (an
  operational problem worth seeing, not something a plain boolean can
  represent). Confirmed by real data: `episodeFileDeleted` (a file
  becoming unavailable again, not just newly available) occurred 48
  times in the last 250 history records on the user's own instance —
  genuinely common, not a hypothetical edge case, so the mechanism
  needs to handle transitions in both directions, not just
  unavailable→available.
- **Mechanism: poll Sonarr/Radarr's own `/history` (grab **and** import
  events), not re-check each tracked episode's current state.** The
  user's own reasoning: "checking availability... by looking at grab
  and import history is what makes the most sense... if something is
  grabbed out of expected timeframe it is still picked up and the
  polling of only the grabbed/imported history is bound to be easier
  and quicker" than re-scanning every tracked episode against "what
  should come." Verified directly against both real instances:
  - Sonarr's `/api/v3/history?includeEpisode=true&includeSeries=true`
    embeds the full episode (`seasonNumber`/`episodeNumber`) **and**
    series (`tvdbId`) objects on every record — real event types seen:
    `grabbed`, `downloadFolderImported` (carries `data.importedPath`),
    `episodeFileDeleted`, `downloadIgnored` (a rejected grab — no
    availability effect, skipped).
  - Radarr's `/api/v3/history?includeMovie=true` mirrors this exactly:
    `movieId`/`tmdbId` embedded, same four event types
    (`movieFileDeleted` instead of `episodeFileDeleted`).
  - **No new correlation-id column needed** — an earlier draft of this
    plan proposed storing Sonarr's/Radarr's own native episode/movie
    ids for exactly this matching purpose; verifying the real API
    first showed `includeSeries=true`/`includeMovie=true` already embed
    `tvdbId`/`tmdbId` directly, so a history event matches straight to
    the existing `show_external_id` crosswalk (§5.4) with nothing new
    to store. One real API quirk found and worth flagging: the embedded
    `movie.hasFile` field came back `null` even when `movieFileId` was
    populated — `eventType` itself (not the embedded object's own
    `hasFile`) is the authoritative signal, and that's what's used.
  - **State transitions**: `grabbed` → `downloading`;
    `downloadFolderImported` → `available` + `data.importedPath` stored
    in `file_path_sonarr`/`file_path_radarr`; `episodeFileDeleted`/
    `movieFileDeleted` → `unavailable`, path cleared;
    `downloadIgnored` → no-op. Events are processed oldest-first within
    each poll (Sonarr/Radarr return newest-first; reversed before
    applying) so a rapid delete-then-reimport (a quality upgrade —
    confirmed a real, common case in the live data, `data.reason:
    "Upgrade"` on a real `episodeFileDeleted` record) resolves to the
    correct final state, not whichever event happened to apply last by
    accident of pagination order.
  - **A new, small global checkpoint table** (not per-show — the
    history feed is one shared stream per service, not scoped to a
    show): `availability_poll_checkpoint(service PRIMARY KEY, 
    last_event_at, updated_at)`, one row each for `sonarr`/`radarr`. No
    id prefix (§5.0) — same reasoning `show_service_presence` already
    uses for its own natural/composite key, generalized here to a
    global singleton-per-service key. Not exposed via GraphQL — pure
    internal polling-mechanism bookkeeping, the same non-domain-data
    treatment `alembic_version` already gets.
  - **LCARS itself does the polling**, not Ops directly — same
    established architecture as B.1/B.2 (§11.2's B.1 note): Ops
    triggers a mutation on its own schedule; LCARS is what actually
    calls out to Sonarr/Radarr. Unlike B.1/B.2's per-show/per-season
    mutations, this one is a single global sweep (one shared history
    feed covers every tracked show at once) — no per-item "due" query
    needed on Ops's side at all; the checkpoint table's own incremental
    processing is what keeps repeat calls cheap.
- **Cadence, confirmed directly**: hourly baseline; **5 minutes** while
  a `WATCHING`-status show has an episode that just aired and isn't yet
  `available`, for up to **2 hours** since air; **15 minutes** after
  that window (until it resolves). Movies have no "just aired" moment
  the same way an episode does, so Radarr stays on the baseline hourly
  cadence — the adaptive urgency is Sonarr/episode-specific.
- **Unverified, flagged the same way §10.1 flags animeschedule.net's
  own remaining unknown**: nothing — both instances were reachable and
  tested directly this round, not left as an open research item.
- **First-ever poll of a never-polled service, revisited after B.3's
  own build, before commit**: a genuine gap this note originally left
  unaddressed — `pollFileAvailability` runs as a **sync** resolver on
  LCARS's **single** shared event loop/connection (§11.2's own DB-
  execution-model note), so a never-polled service's first call would
  walk its entire history inline (Sonarr's ~23,000 records, ~92 pages)
  and block every other client's request for the duration. Resolved by
  the user directly: **two entry points, not one**.
  - `pollFileAvailability` (Ops's own automatic call, unchanged
    signature/schema shape) never walks a never-polled service's full
    history — it seeds `availability_poll_checkpoint` to "now" and
    returns zero for that service. Every automatic call, from the very
    first one onward, stays cheap.
  - A new `backfillFileAvailability` mutation is the deliberate,
    manual counterpart: walks a configured service's *entire* history,
    ignoring any existing checkpoint. **Not** part of Ops's own
    `run_forever` loop — triggered only by a new `ops
    backfill-availability` CLI subcommand, run by the user once, at a
    moment of their own choosing, with an explicit printed warning
    that it will block LCARS while it runs. Same event-matching logic
    as the automatic path, just over a wider window — safe to re-run.
  - This is the same "rare, explicit, user-triggered utility" shape §9
    already established for filesystem-touching work below, applied
    here to a different cost (blocking-duration, not a settled-rule
    reversal) — consistent with, not a departure from, that precedent.

**Resolved 2026-08-09 (B.3b)**: the local file audit — raised by the
user directly right after B.3's own commit ("first one need to be
validated by the presence of each actual files… there should be a way
to attach files present in the path but not linked to sonarr/radarr
too"), which maps onto §4.4's own pre-Cutover "audit/log of existing
local files not currently linked" item, pulled forward once B.3's
availability columns made it buildable, then substantially reshaped
during design. Full detail in `BUILD_PLAN.md`'s own B.3b entry; the
key resolutions:
- **Two mechanisms, not one, discovered by testing rather than
  assumed**: Sonarr's `episode?includeEpisodeFile=true` and Radarr's
  own `movie?tmdbId=` (already embedding `movieFile`) both report each
  service's **current** file state directly — verified live before
  building anything. That meant two of the three cases originally
  discussed (LCARS says available but the file's gone; a file exists
  that LCARS never recorded) reduce to a **pure API comparison**,
  needing **no filesystem access at all** — §6.10's "query the API,
  never scan the filesystem" rule stays fully intact for these. Only a
  genuine orphan (Sonarr/Radarr never associated anything with the
  file at all, so there's no API record of it to compare against) has
  no API answer — that's the one piece that actually reads the
  filesystem, a deliberate, narrow exception to §6.10, not a reversal
  of it, matching exactly what §9 below already anticipated for this
  class of tool.
- **Not a one-off script — a permanent mutation** (`auditLocalFiles`),
  once the user clarified they want to keep re-running it, not just
  once before Cutover. Requires LCARS's own compose service to gain a
  real filesystem mount for the first time ever (§11.3's own B.3b
  addendum) — the orphan-discovery half gracefully no-ops (not an
  error) for any show whose path isn't accessible, so the mutation
  still works correctly (current-state reconciliation only) before
  that mount exists.
- **Orphan/untracked findings are report-only, never auto-written** —
  `pending_review`'s own `entity_type`/`entity_id` shape doesn't fit
  either case (no existing row to attach the finding to), and an
  untracked remote show is deliberately not auto-created (§5.1's
  `addShow` stays the only entry point). Both are returned directly in
  the mutation's own result for the user to act on by hand.

  **Resolved 2026-08-10 (B.11 reconnaissance)**: this "returned
  directly from a one-shot mutation call, never scheduled" shape was
  found to be a real gap while planning B.11's calendar render-path
  switch — LCARS's `show` table turned out to be completely empty
  (nothing has ever called `addShow` for real yet), and switching the
  calendar's row source to LCARS-tracked shows means any show added
  directly in Sonarr/AniList after the one-time backfill (B.11d) would
  silently never surface. Confirmed with the user: `untracked_shows`
  discovery moves onto Ops's recurring schedule and persists findings
  in a small new reviewable list (B.11e) rather than only returning
  them from a manual call — but still **never auto-`addShow`s**; the
  "deliberately not auto-created" decision in this same paragraph
  stays intact, only the "how do findings surface" half changes.
- **Untracked-show discovery is a flat list, not a folder walk** — the
  user asked for the broader "also discover untracked shows" scope,
  narrowed on review to just listing (title/external id/path): walking
  a folder LCARS has no row to cross-reference against would only
  produce noise.
- **Filename parsing**: one regex (`S(\d{2,})E(\d{2,})`), anchored,
  ignoring everything else — handles both of the user's own real
  Sonarr naming templates (standard and anime) without needing to
  detect which one produced a given file, since both always carry that
  token. A file it can't match is still reported (season/episode left
  null) rather than silently dropped — a parse failure is itself a
  finding.
- **Sequencing**: originally PC.1 (Pre-Cutover), moved into Phase B as
  its own numbered step once it became clear this is a permanent
  capability, not a throwaway script — confirmed directly with the
  user rather than assumed. PC.1's own entry now just verifies this
  has been run at least once before Cutover, not describes the
  mechanism (`BUILD_PLAN.md`'s Pre-Cutover section, updated to match).

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
**Implemented 2026-08-09 (A.25, second consolidation audit)** — both
`absolute_number` and `kind` had been specified here since A.1 but were
never written by any code. Sonarr reports `absoluteEpisodeNumber` and
A.22 already read it (to derive the numbering *scheme*, §5.5) before
discarding it, which left A.22 labelling a scheme with no numbers
behind it; every episode was stored `kind = 'regular'`, season-0
specials included. Now:
- **`absolute_number`, sourced as-is** from Sonarr's own
  `absoluteEpisodeNumber` when reported.
- **`absolute_number`, synthesized** per this section's own rule when no
  source value exists, as a **whole-show recompute after each fetch** —
  the indices are positional, so a newly-discovered special landing
  mid-season shifts every later one, and only a full recompute keeps
  them right. A real source value always supersedes a synthesized one
  (told apart by the fractional part: source values are whole numbers).
- **`kind`**: season 0 captured as `special`. Sonarr cannot distinguish
  `special` from `ova` or `bonus_movie` — it only files everything
  non-regular under season 0 — so automatic classification honestly
  stops there, and a new `setEpisodeKind` mutation covers the rest.
  That mutation also closes a real hole: `kind` had been **read-only
  across the entire API** since A.1, so a wrong value could never be
  corrected at all.

**These are deliberately *capture*, not behavior inputs.** Nothing reads
`kind` to decide anything, and §6.4's `nextUp` orders by air date
precisely so a source platform's filing convention cannot drive watch
order. Per the user (2026-08-09): the internal database is the source of
truth; AniList/Fribb/Sonarr are sources that feed and periodically
correct it, and their classifications are mapped in, never authoritative
over internal behavior.

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

**Implemented 2026-08-08 (A.7)**: `lcars/fuzzy.py` — `best_match()`/
`normalize_title()`, a close port of aniq's own real, working matcher
(`~/repos/aniq/src/aniq/notes.py`), generalized from "match a show
against aninote vault filenames" to "match a show's title variants
against any caller-supplied candidate list" (its note-vault-specific
season-suffix disambiguation dropped — not applicable here, that
specific problem is `season`/§5.5's, solved separately). New
`refreshShowServicePresence(showId, service, candidateTitles)`
mutation: the caller supplies `candidateTitles` it already fetched
from `service`'s own catalog — LCARS itself makes no outbound HTTP
calls here (no Sonarr/Radarr/AniList/MAL client code exists in this
codebase at all yet, §11.2's own config.py explicitly defers those
credentials to A.16/Phase B) — and LCARS owns the actual matching
decision against the show's stored title variants, threshold-gated.
Upserts on `(show_id, service)`, no `require_client()`/history table
(passive/informational, matches this section's own framing). The
*fetching* of each service's real catalog, and any recurring/
scheduled call into this mutation, are deliberately out of scope here
— §5.4's own text already says presence is "refreshed on the same
background-poll cadence as the rest of Phase B", i.e. Phase B's
scheduler (Ops) is what will actually drive this on a live clock;
A.7's own job was the reconciliation *mechanism*, callable on-demand,
same split as A.4's `reconcileSeasonMapping`.

**Resolved 2026-08-09 (B.7)** — the paragraph above's "on a live
clock" is now only **partially** true; a future reader should not
assume every service listed in `show_service_presence`'s own `service`
column is actually kept fresh automatically:
- **`local` and Sonarr/Radarr are covered** — `local` is a pure SQL
  rollup (`episode.available_locally`/`show.available_locally`, both
  already-generated columns, B.3) with zero external dependency, so it
  rides Ops's hourly tick. Sonarr/Radarr fetch their whole catalog
  (`sonarr_client.all_series()`/`radarr_client.all_movies()` — the
  same client methods `local_audit.py`, B.3b, already established
  LCARS calling directly) and fuzzy-match every tracked show of the
  matching `media_shape` against it. **Confirmed with the user**: this
  is genuinely expensive (N×M string comparisons), so it does *not*
  ride the hourly tick — an unconditional, no-per-show-due-gating
  sweep on B.2's own existing monthly cadence instead (no new
  interval, same "the tier itself is the correctness boundary" shape
  B.2's own monthly reconciliation already uses).
- **AniList/MAL are NOT covered — a real, checked blocker, not a
  silently-deferred choice**: `anilist_client.py` has no search/
  catalog-listing endpoint at all, only `fetch_media(anilist_id)` (a
  single-entry lookup by a known id — no way to ask "does anything
  matching this title exist"); `mal_client.py` doesn't exist yet
  (B.10, not built). Presence for these two services stays whatever
  the on-demand `refreshShowServicePresence` mutation last wrote (or
  never-checked, if it was never called) until a future step gives
  LCARS the client capability this needs.

### 5.5 id-mapper / reconciliation tables

**`show`/`season`/`episode` are three independently-identified,
independently-mappable levels — resolved 2026-08-08 (A.4), a real
modeling gap found starting that step, not caught by the earlier audit
pass.** The original `show_id_mapping` (below) assumed one `show` maps
to exactly one AniList entry — wrong: the real, working aniq/Data
`mapping.py` this project formalizes resolves AniList ids by
**`(tvdb_id, season_number)` together**, because TVDB groups a
franchise's seasons under one series id while AniList splits each
season into its own entry (a single `anilist_id` column can't hold
"season 1 → AniList X, season 2 → AniList Y" at once). `season` is the
fix: a new table, one row per season of a show, holding the per-season
cross-service identity that `show_id_mapping` incorrectly tried to
hold on `show` directly. `show_id_mapping` is retired entirely (§5.0's
prefix table) — nothing was ever deployed with it, so a clean
drop-and-replace beat carrying deprecated cruft forward.
`tvdb_id` stays exactly where it already was, on `show` via
`show_external_id` — TVDB doesn't split by season, it's one series id
with `season_number` as a sub-key, so no separate `tvdb_id` lives on
`season` itself. `episode` gets a `season_id` FK to this new table,
*alongside* keeping its existing integer `season` column (§5.2) for
raw Sonarr-numbering compatibility, unchanged. `franchise`/
`franchise_member` (§5.9) are a distinct, coexisting concept, not
superseded by this — confirmed directly: a movie needs to map to
*both* its franchise position *and* its season, not one or the other.

Two id-mapper tables, sharing one reconciliation mechanism (§3.1) —
kept separate rather than one polymorphic table, for cleaner
per-level foreign-key typing:

- **`season`** — season-level identity (which AniList/MAL entry this
  particular season of a show corresponds to). Seeded from the
  Fribb/`anime-lists` dataset, manual overrides authoritative. A row
  with **no** confident auto-derived candidate at all (not just a
  discrepancy) still goes through the same `pending_review`
  mechanism — the season stays fully usable locally in an unmapped
  state, not blocked.
- **`episode_numbering_mapping`** — episode-level numbering
  (absolute vs. season+episode) within an already-identified show.
  Populated the same way: automatic derivation attempt (Sonarr
  absolute-order info, AniList episode counts), manual override/
  fallback when derivation isn't confident.

**`episode_numbering_mapping`'s columns drafted 2026-08-08 (A.1)**,
**`season`'s drafted 2026-08-08 (A.4)** — `episode_numbering_mapping`
is one row *per show* (not per episode — "episode-level" describes
what the mapping is *about*, not its grain); `season` is one row *per
season of a show* (§5.0's `z-` prefix). "Identical shape" above means
the reconciliation-bookkeeping columns (`source`/`matched`/
`manual_override`/timestamps), not literally identical columns — the
two tables' actual subject-matter values differ:

```
season
  id                    z-
  show_id               FK show
  season_number          the Sonarr/TVDB season number this season
                          corresponds to (the authoritative value —
                          episode.season, §5.2, keeps holding it too,
                          per-row, for simplicity)
  anilist_id              derived/candidate AniList media id for THIS
                          season specifically, nullable
  mal_id                  derived/candidate MAL id for this season,
                          nullable (MAL splits by season like AniList)
  score                   this season's own personal score, nullable,
                          added 2026-08-08 (A.9) — same 0-20 scale as
                          show.score (§6.1); falls back to show.score
                          for the AniList push when unset
  source                 fribb | manual | unmatched
  matched                bool — false = no confident candidate found
                          yet (the "stays usable, unmapped" case above)
  manual_override        bool — once true, auto-derivation no longer
                          overwrites anilist_id/mal_id (§3 principle 6)
  last_reconciled_at     last time the weekly Fribb pass (§5.5 below)
                          checked this row
  created_at / updated_at
  UNIQUE (show_id, season_number)

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
asymmetric with `season`, since only the identity mapping has a stated
recurring (weekly) reconciliation cadence below; numbering is derived
once at show-add time with no periodic re-check specified.

`season` also gets a **weekly** reconciliation pass against the Fribb
dataset — deliberately slower/independent of the daily metadata-
refresh cadence, since id-mapping data changes far less often than
episode/schedule metadata.

**Implemented 2026-08-08 (A.4)**: `lcars/fribb.py` — downloads (7-day
on-disk cache, stale-cache fallback on a network error, matching §4
Phase A's "one-time/on-demand, not live polling" framing) and matches
the dataset by `(tvdb_id, season_number)`, verbatim-ported matching
logic from Data's own real `mapping.py` including its single-candidate
short-circuit (deliberately does *not* cross-check the lone
candidate's own season tag — see the module's docstring for the full
regression history that decision is based on). `reconcileSeasonMapping(
showId, seasonNumber)` is the on-demand mutation this dataset feeds:
looks up the show's `tvdb` `show_external_id`, resolves a candidate,
applies the result immediately unless `manual_override = true` (in
which case the row is left untouched and — **asked/confirmed
2026-08-08** — no `pending_review` is opened for the disagreement
either, since a review entry for something already manually decided
doesn't serve `pending_review`'s "later human awareness" purpose;
`last_reconciled_at` still updates). A genuine value change on a
non-override row, or a brand-new row with no candidate found at all,
opens/extends a `pending_review` entry (§5.6's value-chain
accumulation, generalized here into `_open_or_extend_pending_review` —
the same helper any future automatic-reconciliation mutation reuses);
an unchanged re-check does not, so repeated on-demand calls don't spam
the review queue. This mutation is what a **weekly scheduler** (Phase
B) will call repeatedly once Ops exists — not built yet, out of scope
here; A.4 only builds the reconciliation mechanism itself, on-demand.

**Cadence resolved 2026-08-09 (B.2)**: `BUILD_PLAN.md`'s own B.2 entry
only said "weekly Fribb dataset reconciliation," with no query/filter
shape settled anywhere — unlike B.1, which got a full design-question
paragraph. Asked directly rather than assumed, given the shape of the
question directly mirrors B.1's own already-resolved one. Confirmed:
**two tiers, not one**, refining "weekly" into a real cadence split:
- **Weekly**: every season belonging to a show that is both
  `status = WATCHING` **and** actively airing (A.10's `_show_is_airing`
  predicate, show-level, reused unchanged — confirmed explicitly: an
  airing show's *other*, already-finished seasons get the faster
  cadence too, not just the specific season currently airing).
  Confirmed to mirror B.1's own combined filter exactly ("Airing AND
  watching (like B.1)") — a `PLANNED`-but-airing show waits for the
  monthly sweep below, same as B.1's own metadata refresh does.
- **Monthly**: an unconditional complete sweep of every season of every
  show, regardless of status/airing — matches the second consolidation
  audit's own "B.2's weekly all-shows pass" framing (`BUILD_PLAN.md`,
  A.20's Fribb-memoization note), just corrected to the cadence this
  round actually settled on for the *unconditional* half specifically —
  see `BUILD_PLAN.md`'s B.2 entry for the resolution of that apparent
  conflict. No per-season "due" gating on this tier at all: Ops's own
  monthly timer *is* the gate, and `reconcile_season()` is already
  idempotent (only opens `pending_review` on a genuine value change),
  so re-checking an already-correct season costs little — reconfirmed
  cheap in practice by A.25's own Fribb dataset memoization.

**Weekly tier's own "due" mechanism, mechanical continuation of B.1's
resolved shape** (§11.2's B.1 note: "every Ops responsibility... needs
a real mutation/query to act through"): a new `Query.dueForSeasonReconciliation`
returns every `Season` (not `Show` — `Season.show.id`/`.seasonNumber`
already exist, §5.5, exactly what `reconcileSeasonMapping` needs, so
Ops acts on one flat list with no extra per-show round trip) belonging
to a watching+airing show, further filtered to `last_reconciled_at IS
NULL OR < now - 7 days` — reusing `season.last_reconciled_at` (A.4) the
same way B.1 reused `show.metadata_last_refreshed_at`, so a season A.20
already reconciled hours earlier (via a live Sonarr fetch) isn't
redundantly re-checked the same week. Deliberately **not**
`home_timezone`-bucketed the way B.1's daily ceiling is: §6.13 names
only "the daily metadata-refresh/on-open-cap cadence" as a
`home_timezone` consumer, not the weekly one — a plain
`util.utc_iso_offset(-7)` cutoff is used instead, no day-boundary
precision needed at week granularity.

**A real gap found and closed 2026-08-09 (A.20, consolidation audit
pass)**: A.4 above builds reconciliation for a `season` row that
already exists, but nothing ever created that row for any season
number beyond 1 (`addShow`'s own on-demand fetch, A.8, only ever
creates `season_number = 1`). Sonarr/TVDB is the only source that ever
reports a new season number exists at all — AniList has no independent
channel for this, each AniList entry is already scoped to one season
(this section's own opening paragraph) — so a multi-season show whose
season 2+ was only ever discovered via a Sonarr episode fetch had no
`season` row, and therefore no `episode.season_id` (dead FK, added
A.4, never populated by any code path), and — the concretely serious
consequence — `setScore`/`setStatus`/`setSeasonScore` (§6.1, both query
`FROM season WHERE show_id = ?`) silently never pushed to that
season's AniList entry at all. Fixed: `_fetch_sonarr` (`metadata.py`)
now reconciles every distinct season number it sees against Fribb
immediately (reusing `reconcile_season()`, extracted out of the
`reconcileSeasonMapping` resolver into `season_mapping.py` so both
share one implementation) the moment it's first discovered, and sets
`episode.season_id` on every episode row it writes — including
backfilling it on a pre-existing row that predates this fix. A Fribb
failure during this on-demand reconciliation (network unreachable, no
cache) still creates the season row, just unmatched, with its own
`pending_review` entry for the failure — it must never abort the
Sonarr episode import itself, which is `_fetch_sonarr`'s actual point.
An already-existing `season` row (any `manual_override` state) is left
exactly as `reconcile_season()` already treated it — no behavior
change for a season that was already being reconciled correctly.

**`episode_numbering_mapping`'s automatic derivation, deferred at A.4
for lack of data, built 2026-08-09 (A.22)** once A.8/A.9's own Sonarr
fetch supplied exactly that data. Heuristic, deliberately simple
(personal-tracker scale, same reasoning §6.5's plain-`LIKE` search
already leans on): Sonarr's own `seriesType = 'anime'` flag, or any
episode carrying a populated `absoluteEpisodeNumber`, means `absolute`;
anything else defaults to `season_episode`. AniList episode counts
(this section's other named signal) aren't actually used as a
derivation input — AniList has no numbering-scheme concept of its own
to derive from (each entry is already one season) — so a not-linked/
not-configured Sonarr leaves this table honestly unmatched rather than
guessing off AniList alone, same treatment `season` itself gets.
Never touches an already-`manual_override` row (§3 principle 6), same
protection `setEpisodeNumberingScheme` already gives it. Re-derived on
every Sonarr fetch (not just once at show-add time) so a scheme
misjudged early (e.g. before absolute numbering appeared in the data)
self-corrects on a later refetch, same "review, not gate" spirit as
everything else in this section — a genuine value change opens a
`pending_review` entry, an unchanged re-check does not.

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

**Implemented (A.1/A.3/A.4)**: the table (A.1), `resolvePendingReview` +
the `pendingReviews` query (both A.3, `includeResolved` defaulting to
unresolved-only), and the automatic-creation/value-chain-accumulation
mechanism (`_open_or_extend_pending_review`, A.4 — see §5.5's own
implementation note) all already exist and are tested. "Permanent
retention once resolved, never purged" holds structurally, not just by
convention: no mutation in the schema deletes a `pending_review` row at
all, resolved or not — confirmed via `tests/test_server.py`'s
`includeResolved: true` check that a resolved entry stays queryable.
The three passive/pull surfaces below are client-side work (Data/
Holodeck/Captain's Log), out of this server's own scope.

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

**How the server actually learns which client is calling — resolved
2026-08-08 (A.3)**: a real gap, never previously specified. An HTTP
header, `X-LCARS-Client`, sent on every request — transport-level,
parallel to the bearer token itself (§8, also a header, not a GraphQL
argument). Keeps every mutation's GraphQL signature focused on its own
domain concern rather than repeating a `client` argument across dozens
of unrelated mutations (would cut against §3 principle 7's "dedicated,
purpose-built" mutations — a cross-cutting concern doesn't belong
threaded through each one individually). Any mutation that writes a
history/`pending_review` row **requires** this header — a direct,
mechanical consequence of "every row records the originating
client/process" already being a firm requirement here, not a new
question: a missing header is rejected with a clear error rather than
silently defaulting to some placeholder value, which would violate
that requirement rather than satisfy it.

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

**A real gap found and closed 2026-08-09 (A.21, consolidation audit
pass)**: `show_relation` was schema-only from A.1 through A.19 — no
code anywhere ever wrote a row to it, despite `Show.relatedShows`
(A.3) being fully wired to read it. Fixed: `_fetch_anilist`
(`metadata.py`) now requests `Media.relations` (AniList's own relation
edges) alongside everything else it already fetches, and writes one
`show_relation` row per reported relation (this show's own direction
only, per this section's "directed edges, stored as-ingested" framing
above — the related show's own eventual fetch writes its own direction
independently). `related_show_id` is a real, non-nullable FK — a
relation to a show LCARS has never seen before needs a real row to
point at, resolved by asking directly rather than guessed at given the
scale of the decision: **auto-create it as a `tracked = false` stub**
(title/`media_shape` from AniList's own relation node, `tracking_space
= anime`, `status = planned`), the same promotion-target shape this
document's own §5.1 "Show-row promotion paths" already describes ("a
bare `tracked = false` relation/franchise stub becomes a real tracked
show by flipping `tracked = true`"). Only anime-shaped AniList
`format` values (`TV`/`TV_SHORT`/`MOVIE`/`SPECIAL`/`OVA`/`ONA`)
produce a stub or an edge at all — a relation to source material
(`MANGA`/`NOVEL`/...) is skipped outright, since §5.1's show model has
no place for those. No franchise auto-creation here either, unchanged
from A.8's original "no auto franchise" precedent — a relation edge is
not a franchise membership; `franchise_member` stays a deliberate,
separate action, unaffected by this.

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

### 5.11 `show_merge` — cross-service duplicate merging (B.14)

```
show_merge
  id                     y- (§5.0)
  winner_show_id         the surviving, canonical show
  loser_show_id          demoted (tracked = false), never deleted
  matched_on             how the pair was found (fuzzy title match, method + score)
  manifest               JSON: {"moved": {...per-table detail...}, "skipped": [...]}
  merged_at
  reversed_at            nullable
  reversed_by_client     nullable
```

**The gap**: B.11d's own `find_existing_show`/`_promote_stub` dedup
(§5.1) only ever catches two rows sharing the *same* external id — a
Sonarr-sourced show (tvdb-linked) and an AniList-list-sourced show
(anilist-linked) for the same real-world anime, sharing **no** id at
all, are invisible to it. Discovered live during B.11f's "dropped
shows still appear" investigation (BUILD_PLAN.md): 102 tracked shows
with a tvdb link and no anilist link, 1172 tracked anime-space shows
with an anilist link and no tvdb link, cross-referenced by normalized
title — 2 real pairs ("Black Lagoon", "Mebius Dust"). Confirmed with
the user this is genuinely new work, not a resumption of B.7
(`show_service_presence`'s periodic refresh, §5.4) — B.7's own
BUILD_PLAN.md entry explicitly scoped itself to "local + Sonarr +
Radarr" and named AniList's missing search/catalog-listing endpoint as
the reason it stopped there.

**Detection**: automatic sweep (confirmed with the user), reusing
`fuzzy.best_match()` (§5.4, A.7) unchanged against the two candidate
sets above. Real N×M cost, confirmed with the user — rides B.2's
existing monthly tier (`ops/scheduler.py`'s `run_monthly_once`), same
"no new interval" precedent B.7's own catalog-matching sweep already
established, not a fifth Ops interval.

**Merge direction**: the AniList-linked show wins (confirmed with the
user) — the winner keeps its own title/status/score/tracking-state
fields untouched, same "don't overwrite what's already trustworthy"
reasoning `_promote_stub` applies to a stub. What "wins" does *not*
mean: episode/watch/availability data migrates onto whichever row
survives regardless of which side originally held it — checked live
before assuming otherwise (Mebius Dust's Sonarr-sourced row held 8 real
episodes, its AniList-sourced row held 0). Every show-scoped table
`export_import.py`'s own `EXPORT_IMPORT_TABLES` lists gets walked; a
genuine per-slot conflict (both rows already have their own season 1,
say) leaves the loser's copy in place rather than dropping it,
recorded in `manifest["skipped"]`.

**Reversible without resurrecting anything**: the loser is demoted
(`tracked = 0`), never deleted — same vocabulary `_promote_stub`
already established for "exists, not really a real show right now."
`manifest["moved"]` records exactly what to restore per table (ids for
tables with their own prefixed id, natural-key tuples for pure join
tables with none) so `reverseShowMerge` replays it verbatim rather than
attempting a generic "undo the last N statements" mechanism.

**Reviewable, not via `pending_review`**: `pending_review` (§5.6) is
single-field/value-chain shaped — a fit for "this field changed," not
for "these two rows became one, here is the full child-row manifest."
A dedicated table is the same "structurally different event, dedicated
table" call `untracked_show_finding` (§5.2, B.11e) already made rather
than overloading `pending_review`.

**Scale, stated plainly**: this table's first real sweep only ever
touches the 2 pairs measured above — it exists to catch the *next*
ones, not to work through a backlog. See `lcars/show_merge.py`'s own
module docstring and BUILD_PLAN.md's B.14 entry for the full
implementation narrative.

**Correction, 2026-08-12 — auto-merge retired, review-gated instead.**
The automatic sweep above was never actually run against production
until this date. A dry run first (the user's own explicit "check
before running" instinct, not skipped) surfaced real false positives
at the 0.72 fuzzy threshold — completely unrelated shows matched
together on nothing more than short, coincidentally-similar titles
("The Rookie" / "THE UROTSUKI", "Inspector Gadget" / "In/Spectre",
"Alien: Earth" / "Captain Earth"). Confirmed directly with the user
rather than picking a fix alone (a stricter threshold only narrows the
false-positive rate, doesn't eliminate the risk class; auto-merge-
with-review-only-on-ambiguity, the pattern most other sweeps in this
codebase use, is wrong here specifically because a *confident* wrong
match is what broke): the sweep (`pollShowMerges`) now only opens a
`pending_review` entry per candidate — the "not via pending_review"
reasoning above still holds for the `show_merge` *log* table itself
(the completed-merge record stays its own dedicated table, still
too rich for pending_review's single-field shape), but the *decision
to merge at all* now goes through pending_review first, same as every
other reconciliation mechanism's uncertain-automatic-decision path.
A new `applyShowMerge(winnerId, loserId, matchedOn)` mutation is the
human-triggered action that actually performs a merge once a specific
pair has been reviewed and confirmed correct — requires
`RESOLVING_CLIENTS` (§5.6), resolves the matching review entry as part
of the same call, and refuses outright (rather than silently
orphaning data) if the winner has already absorbed a different loser
sharing the same external service.
Known, deliberately-unsolved gap: a *rejected* candidate isn't
remembered anywhere — the next sweep proposes the exact same pair
again, since nothing currently records "a human already said no to
this."

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

**Per-season score granularity, resolved 2026-08-08 (A.9)**: building
the actual AniList push surfaced a question A.4's season split had
never worked through — `setScore` operates on `show` as a whole, but
AniList tracks each season as its own separate list entry, so which
entry receives a push once a show has more than one season? Resolved
directly with the user, not guessed (real live-AniList-data
consequences): `season` gets its own `score` column (§5.5), same
0–20 scale. The push to a given season's AniList entry reads that
season's own `score` when set, falling back to `show.score`
otherwise — the common single-season-show case is unaffected either
way; `setSeasonScore(seasonId, score)` is the new dedicated mutation
for the per-season value, pushing to just that one season. `setScore`
itself still pushes to *every* season the show has an AniList link
for (each resolving its own effective value via the same fallback) —
there's no per-season `status`, so `setStatus` pushes the same
show-level status to every linked season uniformly.

**AniList push ownership and mechanics, resolved 2026-08-08 (A.9)**:
confirmed directly — score/status push is explicitly **not** the same
exception §6.8 carves out for Data's episode-watch-status-only direct
write; "it goes through lcars, lcars pushes it." A related real gap
found while mapping this out: §6.8 already says LCARS owns *status*
push too ("Everything else (status, score...) is LCARS's job"), but
no `BUILD_PLAN.md` step anywhere actually implemented it — confirmed
genuinely unaddressed (checked both documents fully, not from partial
memory) and folded into this same step, per the user's own call:
"if it is a real gap then add a step and build the push plan now."
LCARS's own AniList OAuth session (`anilist_client_id`/`_secret`/
`anilist_access_token`, `lcars.ini`) reuses Data/aniq's already-
registered AniList app (confirmed, not a new registration) — LCARS
runs its own separate authorization via a new `lcars anilist-login`
CLI command (the PIN-redirect flow, same as Data's own, since LCARS
is headless) to mint its own independent token. Every push is
best-effort, same philosophy as A.8's metadata fetch: not yet
authenticated is treated the same as "not configured" (silent no-op);
an actual push failure opens/extends a `pending_review` entry
(`entity_type = season`, `field = "anilist_push"`) rather than ever
failing the local `setScore`/`setSeasonScore`/`setStatus` write.

### 6.2 Paced/catch-up mode (Phase A schema, Phase B scheduling)

Restricted to completed/non-airing shows only — an airing show never
gets fake dates. Per-show configurable cadence (default weekly,
editable). **Adaptive, not pre-baked**: next synthetic date = latest
`watch_event.watched_at` + cadence interval, recomputed on every
watch. Falling behind resets from the last watch — no backlog
accumulation for paced shows specifically (that's the separate feature
below).

**Implemented 2026-08-08 (A.10)**: one nullable `show.paced_cadence_days`
column — its own presence *is* the paced-mode flag (no separate
boolean), same shape as `show.hard_delete_requested_at`. "Restricted
to completed/non-airing shows" resolved as a read of the show's real
content, not `show.status` (a show can be user-marked `watching` and
still be fully released — the normal paced-mode case, deliberately
bingeing at a self-imposed pace): "airing" means *any* episode with no
known air date yet, or one still in the future; a movie has no
episode rows at all (§5.1) so it's always eligible. `enablePacedMode
(showId, cadenceDays: Int = 7)` validates this at write time only — no
ongoing enforcement if a later fetch adds a new episode, consistent
with "scheduling itself is Phase B." `disablePacedMode(showId)`
clears it. The adaptive formula itself is a computed field,
`Show.pacedNextDate`, never stored — latest `watch_event.watched_at`
+ `pacedCadenceDays`, recomputed fresh on every query; null with no
watch event yet (no artificial delay before a first episode) or when
not in paced mode at all.

### 6.3 Backlog visualization (airing shows, Phase B)

Distinct from pacing. Surfaces in **both** Data's `B` view (inherited
from aniq at the fork) and a new calendar-native counter line under a
show's next-episode entry. Mark-watched from the counter clears
exactly one oldest episode per action, no bulk-clear.

**Resolved 2026-08-10 (B.9), split by repo rather than assumed as one
unit**: LCARS's own `Query.backlog` (unwatched, locally-available
episodes on watching-status, actively-airing shows — matches
`nextUp`'s §6.4 "locally-available" precedent, not merely aired) is
now built. The two Data-side pieces above (the counter line, mark-
watched-clears-oldest) are deliberately **not** built yet — confirmed
directly with the user: Data doesn't read any state from LCARS at all
before B.11 ("Data's role shrinks"), so building this widget against
Data's current local computation now would mean rebuilding it against
`Query.backlog` once B.11 lands. B.11's own `BUILD_PLAN.md` entry now
owns both deferred pieces.

**Resolved 2026-08-10 (B.11 scope + sequencing)**: this section's own
ambiguity about whether B.11 replaces the calendar's core render path
now or Phase C does was raised directly with the user — confirmed
**Replacing** (B.11 switches the render path itself, not Phase C).
B.11 split into numbered sub-steps given the size of that rewrite;
B.11a (LCARS read-query foundation + status bar service-health
indicators, since `Query.serviceHealth` needed no LCARS-side change)
is built and verified. B.11b's own reconnaissance surfaced two more
real gaps (no date-windowed episode query; LCARS's `show` table
completely empty) — see §5.2's own "Resolved 2026-08-10 (B.11
reconnaissance)" note and `BUILD_PLAN.md`'s B.11 entry for the full
breakdown — which grew the sub-step list to B.11c (new
`episodesInRange` query) through B.11g (this section's own deferred
counter/mark-watched pieces, previously labeled B.11c before the
regrow). During reconnaissance, Data's existing `B`-view predicate and
LCARS's `Query.backlog` predicate were also found to be genuinely
divergent (different show-shape/airing/status eligibility rules) —
surfaced to the user rather than silently reconciled; user chose to
ship the counter (now B.11g) against `Query.backlog` as-is, no
reconciliation needed.

### 6.4 Cross-show "next up" query (Phase A/B)

One aggregated query, across all `watching`-status shows **and**
paced/catch-up shows, returns the next available episode per show —
one unified "what's next" view regardless of why an episode is next.
Defaults to soonest-available-first, with manual reordering supported
on top (same auto-default-plus-override shape as franchise ordering).

**Corrected 2026-08-09 (second consolidation audit), directly by the
user**: the intra-show "which episode is next" pick below used
`(season, episode)` order from A.11 until this fix — a *second*
ordering rule this section never states. §6.4 defines exactly one
default, soonest-available-first, and it governs both levels: which
episode within a show, and which show first. The old numbering order
imported an external platform's filing convention into internal
behavior — Sonarr/TVDB park specials in season 0, so `season ASC` made
every special outrank the actual premiere (verified: `nextUp` returned
a season-0 special ahead of S1E1). The user's own framing settles why
that is wrong in principle, not just in effect: *"the source of truth
is the internal database, anilist and fribb and sonarr are used as
sources of data where needed, and mapped onto it"* — so how a source
platform files an episode has no bearing on internal ordering, and the
whole special/season-0 classification question is irrelevant to watch
order. Now `air_date_utc` ascending (nulls last, same rule as the
cross-show level), with `(season, episode)` kept only as a stable
tiebreak between two episodes sharing an air date. Needs no `kind`
taxonomy to be correct — a special falls wherever it actually aired.

**Implemented 2026-08-08 (A.11)**: a show contributes an entry if
`status = watching` or `pacedCadenceDays` is set (A.10), and it has an
unwatched, locally-available episode (§5.2) — the earliest one by
~~`(season, episode)`~~ air date (corrected above).
"Soonest-available-first" reads as
`episode.air_date_utc` ascending; `next_up_override` (already built,
A.3/§5.9 addendum) rows sort first, by their own `sortOrder`, ahead of
every default-ordered entry. Not table-backed (one computed row per
show), so it's paginated via a new `pagination.paginate_list()` rather
than the existing table-scan `paginate()`. Also surfaced and fixed a
real bug in `paginate()` itself, present since A.3 and invisible until
now: its `pageInfo`/`hasNextPage`/etc. keys were camelCase, but
`convert_names_case=True` looks them up as snake_case — every
connection's `pageInfo` had silently been `null` whenever actually
queried through GraphQL. See `BUILD_PLAN.md`'s A.11 entry for the
full account.

### 6.5 Search (Phase A)

Full-text search across titles (and reasonably synopses) is a
first-class GraphQL query, not left to per-client fuzzy matching —
centralizes what aniq's `list_screen.py` currently does locally via
`difflib`, so Holodeck/Captain's Log get equivalent search without
reimplementing it.

**Factual correction, 2026-08-08 (A.12)**: checked the real code before
implementing — `list_screen.py`'s own filtering (`_filter_rows()`)
actually uses `textual.fuzzy.Matcher`, not stdlib `difflib` at all;
this section's "via difflib" was simply wrong about which library. Not
a design question (the framing — "full-text search, not per-client
fuzzy matching" — already settles the *approach*, this was just a
wrong citation), so corrected here without asking.

**Implemented 2026-08-08 (A.12)**: plain SQL `LIKE '%query%'`
substring matching (case-insensitive by SQLite's own ASCII default)
across `title_romaji`/`title_english`/`title_native`/`synopsis` — not
fuzzy/similarity scoring, deliberately: §6.5's own "not left to
per-client fuzzy matching" framing distinguishes this from §5.4's
fuzzy service-presence matcher (`fuzzy.py`, A.7), a different concern
(tolerating an uncertain/mismatched title, not searching one the user
typed on purpose). SQLite FTS5's indexing/ranking machinery would be
over-engineering at this project's actual scale (a single user's
tracked shows — dozens to a few hundred rows); a plain scan across an
already-small table is simpler and fast enough.

### 6.6 Stats/analytics (Phase A query surface, needs Phase B data to be meaningful over time)

Full stats view: totals, hours watched (using `duration_minutes`/
`runtime_minutes`, §5.1/5.2), personal-score distribution. Explicitly
**not** in scope: genre/year/tracking-space score breakdowns (the raw,
unnormalized genre data doesn't support a meaningful breakdown anyway
— see §5.1).

**Implemented 2026-08-08 (A.13)**: `totalShows` is a *current-library*
snapshot (`tracked = 1` only); `totalEpisodesWatched`/`hoursWatched`/
`scoreDistribution` are deliberately *lifetime* totals, not filtered
by `tracked` — untracking a show is soft and doesn't erase having
watched or scored it (§6.11), so those shouldn't shrink just because
a show was later untracked. Hours: each watched episode's own
`runtime_minutes` when set, else its show's `duration_minutes`
(§5.1/§5.2's existing fallback shape); a movie (no episode rows at
all) counts as watched via any `watch_event` existing for it, using
`show.duration_minutes` as its only runtime source. Score
distribution excludes unscored shows (`score IS NULL`) rather than
representing them as a bucket.

**Known limitation, flagged during A.13, fixed 2026-08-08 (A.19)**:
`show.duration_minutes` had no mutation anywhere in the API to set it
— not built in A.1 (schema only), not populated by A.8's AniList/
Radarr fetch either (neither call requested a duration/runtime field,
though both APIs have one). This made movie hours in particular inert
in practice, since a movie has no episode-level runtime to fall back
on at all. Fixed per the user's own direction: "tmdb should have it
for every media for a one time check, anilist does have it for
animes." AniList's `duration` field now fills it for `tracking_space
= anime` (movie or episodic alike — an anime movie still goes through
AniList, §5.1); a new `tmdb_client.py` (plain v3 API-key auth, no
OAuth — read-only public metadata) fills it for everything else. A
movie's tmdb id usually already exists (§5.4 — movies key primarily on
TMDB); a non-anime TV show usually only carries a tvdb id, so the
fetch resolves TMDB's own id via a TVDB->TMDB bridge
(`/find?external_source=tvdb_id`) the first time and persists it as a
real `show_external_id` row, same upsert shape `linkShowExternalId`
itself already uses — a later refresh reads it straight back, no
re-resolution needed. Same "not configured = same as not linked, no
pending_review noise" treatment every other A.8 branch already gets;
a genuine failure (TMDB unreachable, wrong key) still opens one, same
as AniList/Sonarr/Radarr.

### 6.7 Sync & reconciliation policy (Phase B)

- **Metadata refresh**: `watching`-status, actively-airing shows get a
  daily automatic refresh, plus an on-open trigger capped to the same
  once-per-day ceiling (not stacking). Not-airing/not-`watching` shows
  get no background refresh (a monthly rolling option is named, not
  implemented). A fourth, uncapped trigger — immediate fetch on show
  creation (§4, Phase A) — is independent of this cadence.
- **Air-date source priority**: Manual > animeschedule.net (**revised
  2026-08-09 (B.5) — RSS `/jpnrss.xml`, not the REST API's
  `/timetables`; see the B.5 note below for why**) > AniList
  `airingSchedule` > Sonarr raw. **Revised 2026-08-09 (B.4) — this now
  genuinely gates for AniList/Sonarr**: the original framing here ("does
  not gate the apply... including one overwriting a manual value") was
  built as B.4's first draft, then corrected directly on the user's own
  reasoning before commit — see the B.4 note immediately below for why.
  Manual is hard-protected from AniList and Sonarr specifically, the
  same shape `season.manual_override` already gives season mapping (§3
  principle 6). **Resolved 2026-08-09 (B.5)**: animeschedule.net *does*
  get an exemption from that gate — see the B.5 note below.

  **Resolved 2026-08-09 (B.4), AniList `airingSchedule` — the first of
  these automatic sources actually built** (Sonarr raw is a one-time
  seed at episode creation, `metadata.py`'s own `_fetch_and_populate`;
  animeschedule.net is B.5, not built yet). `BUILD_PLAN.md`'s own B.4
  line named the source but not the mechanism, cadence, or per-season
  scoping — asked directly, then verified against AniList's real API
  (three separate live `Media` entries) rather than assumed:
  - **No new due-query/mutation/cadence** — folded directly into
    `metadata.fetch_and_populate()`'s existing `tracking_space =
    "anime"` branch, riding the exact same daily
    `dueForMetadataRefresh`/`refreshShowMetadata` cadence B.1 already
    built. Confirmed with the user: air dates change far less urgently
    than file availability (B.3's own reason for a dedicated adaptive
    loop), so daily is plenty, and `dueForMetadataRefresh` already
    selects the identical show set B.4 needs (watching + actively
    airing).
  - **Per-season, not per-show** — `season.anilist_id` (§5.5's own
    Fribb-resolved crosswalk), not the single show-level `anilist_id`
    `_fetch_anilist` uses for general metadata: a split-cour sequel
    season is a wholly separate AniList `Media` entry. Verified live
    against three real `Media` entries (One Piece's own single
    long-running entry, Attack on Titan S3 Part 2, Jujutsu Kaisen S2)
    that `airingSchedule.episode` always resets to 1 for that specific
    `Media` entry — matches directly onto `episode.episode` (also
    season-scoped) with no numbering-offset translation needed, an
    early hypothesis disproven by testing before it shaped the design.
  - **Full schedule, not `notYetAired`-filtered** — confirmed with the
    user: reconciles an already-aired episode's date too (e.g. one
    Sonarr originally seeded wrong), matching this section's own
    "reconciliation" framing rather than a lookahead.
  - **Manual dates hard-protected — revised after the first draft
    shipped, on the user's own reasoning**: the first draft matched
    this section's original "does not gate" text literally (unconditional
    writes, even over a manual value). Reviewed against the user
    directly: only a genuine reschedule signal (animeschedule.net, B.5
    — a real-world disruption like a sports broadcast preempting a
    timeslot) should override a value they've deliberately corrected;
    AniList/Sonarr repeatedly re-asserting stale data over an
    already-fixed value is the "stubborn weekly rewrite" failure mode
    to avoid. `_reconcile_air_dates` now skips outright (no write, no
    `pending_review`) whenever `air_date_source = 'manual'` — the same
    hard-gate shape `season_mapping.py`'s own `reconcile_season()`
    already gives `season.manual_override` (§3 principle 6), extended
    to per-episode air dates. This section's own priority-order text
    above is corrected to match. **Extended 2026-08-09 (B.8)** to also
    skip `air_date_source = 'animeschedule'`, closing a real gap B.5
    exposed: this guard predates B.5, so an animeschedule-sourced date
    (ranked *above* AniList in this section's own priority order)
    survived only until AniList's next daily pass silently overwrote
    it — a live violation in already-shipped code, found by checking
    the actual guard condition against the documented order rather
    than assumed. Same no-write/no-`pending_review` treatment as the
    manual case.
  - **Season-split guard, confirmed live, not hypothetical**: a single
    TVDB season can span *multiple* separate AniList `Media` entries —
    Attack on Titan's own Season 3 is one 22-episode TVDB season across
    two AniList entries of 12 and 10 episodes each, confirmed by
    querying both directly. `season.anilist_id` can only point at one
    of them, so per-episode matching silently misapplies dates across
    the cour boundary whenever LCARS's own episode count for a season
    exceeds that AniList entry's own reported total. Guarded: skip that
    season's reconciliation entirely, open a `pending_review` on the
    **season** row (`entity_type = "season"`, `field = "anilist_id"`)
    rather than each individual episode, so a human can investigate.
    **Deliberately not built as part of B.4, flagged as a real,
    separate follow-up**: the fuller fix — a human resolving that
    review by specifying an actual episode-range split ("episodes X-Y
    are Media A, P-Q are Media B") — needs a genuinely new, structured
    way to store a multi-entry-per-season mapping; `pending_review`'s
    own resolution is free-text only today, so it can record the
    finding but not yet the fix. Not scheduled in `BUILD_PLAN.md` —
    would extend §5.5's own season-mapping model, likely alongside a
    future B.2-adjacent revisit.
  - `pending_review.open_or_extend(conn, "episode", ..., "air_date_utc",
    "anilist", ...)` is the actual write-path for a genuine correction,
    keyed by the **episode's** own id, same shared mechanism A.4/A.8
    already use.

  **Resolved 2026-08-09 (B.5), animeschedule.net — route settled by
  live testing, not `BUILD_PLAN.md`'s own original assumption**:
  `BUILD_PLAN.md`'s B.5 line preferred the REST API v3's
  `/timetables/{airType}`, filtered by `anilist-ids`. Real, live calls
  found this endpoint returns **401 "Unauthorized. Use private
  endpoint."** with no self-serve registration path discoverable
  (`/api`, `/api/v3`, `/about/api`, `/api-docs` all confirmed 404
  live). A different endpoint, `/api/v3/anime` (including its own
  `anilist-ids` filter), *does* work with no token — but only returns a
  show-level delay flag (`delayedTimetable`/`delayedFrom`/
  `delayedUntil`), no per-episode date, confirmed insufficient to fill
  this section's air-date-source slot on its own. Asked the user
  directly rather than guess at an undocumented app-registration
  process: confirmed to fall back to RSS outright — `BUILD_PLAN.md`'s
  own already-documented fallback, accepting the fuzzy-matching
  tradeoff `§10.1` originally flagged RSS for.
  - **Raw feed only** (`/jpnrss.xml`) — its own item text ("released
    *natively*") is the Japan broadcast release, matching
    `episode.air_date_utc`'s existing semantics, the same real-world
    event AniList's `airingSchedule`/Sonarr's raw date both describe.
    `/subrss.xml`/`/dubrss.xml` describe a fansub/dub group's own,
    later release — a different event this project doesn't model, not
    fetched.
  - **Hourly, not daily — a real cadence finding, not a preference**:
    confirmed live, the raw feed is a fixed ~25-item rolling window
    that covered barely 16 hours of real releases in one snapshot —
    global release volume rotates it faster than a day, so a daily
    check (B.1/B.4's own cadence) would let the window roll over
    completely and silently miss tracked shows. `pollAnimeSchedule`
    rides Ops's existing hourly tick (`run_daily_and_weekly_once`)
    instead — no new interval, same "no new interval" precedent B.1/
    B.2/B.4 already established, just a different existing tick to
    reuse.
  - **Matching restricted to watching+actively-airing anime shows
    only** — the user's own explicit design call: "keeping the search
    to tracked shows should elucidate the vast majority of cases...
    simply flagging those which aren't clear will suffice." Reuses
    `fuzzy.best_match()` (§5.4, A.7) verbatim against this restricted
    candidate pool — same threshold, same "return None rather than
    guess" philosophy; most of the global feed's volume (shows LCARS
    doesn't track) is expected to silently match nothing, not flagged.
    A title match alone can't disambiguate *which* season/episode row
    within a multi-season show an item refers to, so episode resolution
    is further restricted to that show's own currently-airing
    season(s): exactly one candidate episode row applies the date; zero
    or more than one **flags rather than guesses**
    (`pending_review`, `entity_type = "show"`, `field =
    "animeschedule_episode_match"` — same shape `metadata._guarded`,
    A.8, already uses for its own "couldn't cleanly resolve this" case).
    **Caught in review, before commit**: the flagged path's own write
    isn't naturally idempotent the way the applied path is — the raw
    feed's rolling window means the same item re-sweeps roughly a dozen
    times on Ops's hourly tick, so an unguarded `open_or_extend` would
    duplicate an identical finding into the same review's own value
    chain that many times. Guarded locally (`animeschedule.py`'s own
    `_flag()`, not a change to shared `pending_review.py`, to avoid
    touching B.4's already-verified chain-accumulation semantics as a
    side effect): a repeat of the exact same finding is treated as a
    no-op, only a genuinely different finding extends the chain.
  - **Manual-override exemption resolved** — the question this section
    explicitly left open when B.4 built AniList/Sonarr's hard
    manual-date gate: asked directly, confirmed animeschedule.net
    *does* override `air_date_source = 'manual'`, restating the user's
    own B.4 reasoning for exactly this case ("the overwrite and log is
    intended for when a new information about an air date is
    logged... those data are likely to come from animeschedule").
    Every such overwrite still opens/extends a `pending_review` — the
    same shared mechanism, never a silent write.
  - No migration needed — `air_date_source`'s CHECK constraint already
    included `'animeschedule'` since the initial schema (§5.2).
- **Service-level health**: per-integration reachability/rate-limit
  status (Sonarr/Radarr/AniList/animeschedule.net, **+ MAL, added
  B.10** — see below), distinct from any individual show's tracking
  state. Tracked by Ops (Phase B's scheduler); surfaces in Data's
  status bar as **per-source** indicators, not one aggregate value.

  **Extended 2026-08-10 (B.10)**: MAL's own proactive refresh-token
  renewal (`refreshMalTokenIfDue`) is a real outbound HTTP call, the
  same class of thing this table exists to track — not something
  BUILD_PLAN.md's own B.10 text asked for explicitly, but the same
  "close the small consistency gap" precedent B.6/B.8b/B.9 already
  established. Scoped to the refresh call only, matching this table's
  existing AniList scope exactly (fetch/refresh paths are hooked;
  individual score/status push failures, for AniList and MAL alike,
  are not — those go through `pending_review` instead, `field =
  "anilist_push"`/`"mal_push"`).

  **Resolved 2026-08-09 (B.6)**: two genuinely open questions this
  section's own text didn't answer, both asked directly rather than
  decided alone.
  - **Reachability only, not rate-limiting** — checked first, not
    assumed: none of the four client modules (sonarr_client/
    radarr_client/anilist_client/animeschedule_client) distinguish a
    429 from any other error today. "Rate-limited?" would need real
    detection logic added to all four, plus a decision on whether
    LCARS acts on it (defers a poll) or just reports it — real scope,
    deferred, not built here.
  - **"Tracked by Ops" doesn't mean Ops holds the state** — this
    section's own phrasing genuinely conflicts with the architecture
    §11.2's B.1 resolution already settled: Ops has no client code for
    any of these four services and no database of its own, so the
    actual HTTP calls (and every reachability signal they carry)
    already happen entirely inside `lcars/`. Confirmed reading: Ops's
    regular poll cycle is what *triggers* the calls this tracks, not
    something Ops computes or stores itself. State lives in a new
    `service_health` table (LCARS), exposed via `serviceHealth`
    (`Query`) for Data's status bar to read — no `ops/` changes at all,
    since health is purely a side effect of calls Ops (and humans, via
    on-demand mutations like `refreshShowMetadata`) already make.
  - **A real design mistake, caught before commit, not hypothetical**:
    the first draft hooked `metadata._guarded` — the single wrapper all
    four AniList/Sonarr/Radarr fetch functions already funnel through —
    on the assumption that "the function returned without raising"
    meant "the service was reachable." Wrong, caught by an existing
    test going red: every one of those functions has its own
    legitimate no-HTTP early-return path (missing external id, service
    not configured, no season carries an `anilist_id` yet), so
    `_guarded` recorded a false `ok` for a service never actually
    contacted — a genuine fetch failure got silently overwritten back
    to `ok` by the very next guarded call's own trivially-succeeding
    no-op. Fixed by hooking each function's own actual outbound call
    directly instead. The rule this established: `record_success`
    means *a request completed*, never *a function returned*.
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

**Resolved 2026-08-10 (B.10), two real corrections found live during
build, not assumed from docs**:
- **No `client_secret` at all for the app type LCARS actually
  registered.** This section's own "client_id + client_secret on
  approval" assumed every app gets both, mirroring AniList's shape —
  wrong for MAL specifically. MAL's "Other" app type (the correct
  choice for a CLI/server tool, confirmed live registering the user's
  own app) is a PKCE **public client** and issues no secret; "Web"
  apps get one, but that's the wrong type here. `mal_client.py` treats
  `client_secret` as optional end-to-end — omitted from every request
  entirely when unset, never assumed present.
- **Refresh-token rotation, confirmed live, not left ambiguous**: MAL's
  own written docs don't clearly state whether the `refresh_token`
  grant issues a genuinely new token or echoes the one just used — so
  this was verified directly against the user's real tokens rather
  than assumed either way: **it rotates**, a different refresh_token
  came back from a live call. This confirms BUILD_PLAN.md's own "build
  the renewal job now" premise is technically sound — a successful
  proactive refresh genuinely does reset the 1-month clock, not just
  mint a fresh access token. `refresh_access_token()` still falls back
  to the token just used if a response ever omitted one (defensive,
  not because the behavior is actually in doubt), and both values are
  persisted together on every refresh.

Live-verified end-to-end 2026-08-10, not just against fakes: real
PKCE authorize/exchange against the user's own freshly-registered app,
a real refresh call confirming rotation (above), and a real
`update_my_list_status` push (anime id 59985 -> `completed`) confirmed
against the live API, full response object included.

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

**Resolved 2026-08-08, two gaps caught in a later audit pass (A.2 had
already written `requestHardDelete`/`cancelHardDelete`/
`confirmHardDelete` into the SDL, but neither of these was ever
actually settled here — `BUILD_PLAN.md` incorrectly claimed the first
one was already done; fixed alongside this)**:
- **Storage**: a new `show.hard_delete_requested_at` timestamp
  (nullable), set by `requestHardDelete`, cleared by
  `cancelHardDelete`. `confirmHardDelete` checks both that it's set
  *and* that the delay below has elapsed since.
- **Delay duration**: a fixed **24 hours**, not configurable. Matches
  this project's general preference for simple defaults over settings
  — §6.13's global settings currently has exactly one entry (home
  timezone); this isn't a second one.

**Implemented 2026-08-08 (A.14)**: `softDeleteShow` sets `tracked =
false` specifically, not `status` — the two stay independent axes
(§5.1); `status` is left exactly as it was. `requestHardDelete` reads
"layered: soft-delete → a delay period → ..." as a real precondition,
not just a suggested client flow — rejects unless the show is already
soft-deleted. `confirmHardDelete` cascades manually across every table
that references the show, its episodes, or its seasons (no `ON DELETE
CASCADE` anywhere in this schema, §11.2) — including two easy-to-miss
cross-show cases: `show_relation` has both `show_id` and
`related_show_id` pointing at `show`, and `episode_movie_link`'s
`movie_show_id` can point at this show from some *other* show's
episode row — that one gets unlinked (`NULL`), not deleted, since the
row belongs to a different, unrelated show. `pending_review` has no
FK at all (polymorphic `entity_type`/`entity_id`, §5.6) but still gets
cleaned up, in the spirit of "cascading to this show's episodes/
watch_events/etc." No history row for the purge itself — §3 principle
3 already frames a show's hard delete as the one case (alongside
`watch_event`) with deliberately no audit trail afterward.

### 6.12 Data export / import (Phase A)

JSON, explicitly designed as a **restore path**, not just a one-way
backup — the motivating scenario is rebuilding a fresh LCARS instance
once Trakt is gone and AniList/MAL are push-only. Includes an
explicit `schema_version` field (simple incrementing integer, no
semver) from the very first implementation. Import **rejects on any
version mismatch** with a clear error — no auto-migration logic to
build or maintain; upgrading an old export before re-importing is a
manual, out-of-band step.

**Resolved 2026-08-08 (A.3)**: a real gap — `ImportResult` (already in
`schema.graphql` from A.2) only reports `showsImported`/
`episodesImported`/`watchEventsImported`, which reads like it could
mean the scope is limited to those three tables, contradicting this
section's own "rebuilding a fresh LCARS instance" framing (which
implies everything: tags, franchises, `pending_review`, history,
id-mappings — losing those on a restore wouldn't be a real restore).
Confirmed: **full restore, all 24 tables**. `ImportResult`'s three
fields are just which counts are worth surfacing to a human as a
confirmation summary, not a scope limit on what actually gets
imported — `schema.graphql` unchanged, the other 21 tables' counts
just aren't individually reported back. Import targets a fresh/empty
database (the section's own "rebuilding a fresh instance" scenario) —
no merge/conflict-resolution logic for importing into an
already-populated one; colliding ids surface as an ordinary
`IntegrityError`, not something this handles specially.

**Implemented 2026-08-08 (A.15)**: `schema_version = 1`, the very
first value (§6.12's own "from the very first implementation"). The
24-table list (confirmed against a live `sqlite_master` query, not
counted from memory) is ordered parents-before-children — no `ON
DELETE`/`INSERT CASCADE` anywhere in this schema (§11.2), so import's
row-by-row `INSERT`s need a dependency-safe order to satisfy
`foreign_keys = ON` as they go; export reuses the same order, though
only for readability there. `episode.available_locally` (`GENERATED
ALWAYS ... STORED`) is excluded from every `INSERT`'s column list on
import — SQLite rejects an explicit value for a generated column
outright — but is still included in the exported JSON for
completeness/human-readability; the target database recomputes its
own value fresh from the columns it actually derives from. The whole
import is one transaction, confirmed by a real test: a collision on a
table ordered *after* some already-successfully-inserted rows still
rolls back those earlier rows too, not just the row that collided.

### 6.13 Global settings (Phase A)

- **Home timezone**: a single configurable setting (default
  `Europe/Dublin`), not a fixed UTC boundary. Used consistently
  everywhere a "day" needs a boundary: "today" on the calendar, the
  paced/catch-up mode's cadence reset (§6.2), and the daily
  metadata-refresh/on-open-cap cadence (§6.7). All stored timestamps
  (`air_date_utc`, `watched_at`, etc.) stay UTC internally — this
  setting only controls how they're bucketed into calendar days for
  display and scheduling logic, not how they're stored.

**Implemented 2026-08-08 (A.16)**: `home_timezone` lives in
`config.py`/`lcars.ini`, not a database row or a GraphQL-mutable
setting — §11.2 already lists it alongside `bearer_token`/Sonarr/
Radarr/AniList credentials as an `lcars.ini` value (checked before
assuming; nothing in `schema.graphql` anticipated a "settings" type at
all, so this had no other candidate home). Its three named consumers
(the calendar, the paced/catch-up cadence *reset*, the daily
metadata-refresh cadence) are all still-unbuilt Phase B/client
concerns — A.10's `pacedNextDate` already built §6.2's own adaptive
computation, which is pure UTC timestamp arithmetic with no
day-boundary bucketing in its own text, so nothing needed retrofitting
there. No validation on the value (e.g. against `zoneinfo`) — no other
`lcars.ini` value is validated either.

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

**GraphQL**, single static bearer token auth. **Resolved 2026-08-08
(A.3)**: the "`config.ini` + `keyring`" phrase describes the pattern
*clients* (Data, and eventually Holodeck/Captain's Log) use to store
the token they present — same as aniq's own AniList/Trakt credential
handling, on a normal desktop with a real keyring daemon available.
LCARS itself is a different case, not previously addressed: it runs
headless in Docker (§11.3), where the desktop keyring backends
aniq/Data rely on generally aren't available at all. LCARS's own copy
of the token — what it checks incoming requests against — lives in
`lcars.ini`, plaintext, `chmod 600` — the same precedent aniq already
sets for its AniList/Trakt `client_id`/`client_secret` (§11.2), not a
new pattern.

**Revisited 2026-08-09 (A.23, consolidation audit pass)**: flagged
during the same pass, and against the user's own later note
(`ideas.md`: "move all secret and password to a safer place") —
plaintext-`lcars.ini`-only doesn't fit a headless Docker deployment as
well as Docker Compose's own `secrets:` mechanism, which the user
confirmed as the preferred direction over the alternatives (env-vars-
only, an external secrets manager, or just tightening the existing
file handling). Every credential this project treats as a secret
(`bearer_token`, Sonarr/Radarr/AniList/TMDB keys) now also supports a
`<VAR>_FILE` env var — the same convention the official postgres/mysql
Docker images use — pointing at a Compose-mounted secret file; it wins
over both the `lcars.ini` value and the plain env var when set (§11.2
has the full precedence chain and the reason `lcars.ini` isn't
removed: `anilist_access_token` is minted interactively by `lcars
anilist-login` and written back to it at runtime, which a read-only
secret mount can't support).

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
~~2. animeschedule.net feed technical details~~ — **fully resolved
   2026-08-09 (B.5)**, the one remaining unverified detail closed by an
   actual test call, not more doc-reading. A full **REST API v3**
   exists (`/api/v3`) — `/anime/{slug}`, `/anime` (searchable/
   filterable directly by `mal-ids`/`anilist-ids`/`anidb-ids`), and
   `/timetables/{airType}` returning structured `TimetableAnime`
   objects with explicit `episodeDate`/`episodeNumber` fields. Live
   calls found: `/anime` (including its `anilist-ids` filter) needs
   **no token at all** — but `/timetables/{airType}` returns a real
   **401 "Unauthorized. Use private endpoint,"** and no self-serve
   registration page exists (`/api`, `/api/v3`, `/about/api`,
   `/api-docs` all confirmed 404 live) — not a simple "get a free key"
   situation. `/anime`'s own data turned out insufficient for this
   project's need anyway: it's a show-level delay flag
   (`delayedTimetable`/`delayedFrom`/`delayedUntil`), no per-episode
   date. Decision, made with the user directly rather than guessed: use
   the three public RSS feeds after all (`/jpnrss.xml`/`/subrss.xml`/
   `/dubrss.xml` — RFC 2822 `pubDate`, UTC, confirmed live), only the
   **raw** one (native/Japan release, matching `episode.air_date_utc`'s
   existing semantics) actually polled. Rate limit: **120 req/min,
   global** (their own docs flag it as subject to change) — moot for
   B.5's own hourly single-fetch cadence. See §6.7's own B.5 note for
   the full reconciliation design this finding drove.
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
13. **Holodeck's own auth model** — surfaced by the user's own working
    notes (`ideas.md`, not yet a `BUILD_PLAN.md` step since P.1 hasn't
    started): "have html client a login/password thing." §1/§8's single
    static bearer token was designed around Data/Captain's Log — desktop/
    CLI clients that can hold one shared secret in `config.ini` + keyring.
    A browser client with its own per-user login/password doesn't fit
    that shape directly (there's no `user` table, §1's own "no multi-user
    support" framing) — needs its own small design pass (e.g. a
    password gate in front of the same shared bearer token, rather than
    real multi-user auth) before P.1 (§7.3) starts, not before Phase B.
14. **Holodeck's playback mechanism** — also from `ideas.md`: "does the
    html client use a html/browser player or local one? ... local first
    then browser base." Not designed at all yet — mpv IPC (§4 Phase C)
    is explicitly local-only/inherently-cannot-move-server-side for
    Data, but Holodeck is a browser client with no local mpv process of
    its own to speak to, so that precedent doesn't directly answer this
    one. Needs its own design pass before P.1 starts.

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
  uses, per §8). **Credential precedence, revisited 2026-08-09 (A.23,
  §8's own note has the full reasoning)**: `lcars.ini` value < plain
  env var < `<VAR>_FILE` env var pointing at a Docker-secret-mounted
  file — the last one wins when set. Applies to every credential field
  (`bearer_token`, Sonarr/Radarr/AniList/TMDB keys); `sonarr_url`/
  `radarr_url`/`home_timezone` aren't secrets and keep plain file<env
  precedence only.
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
- **Flagged 2026-08-09 (consolidation audit pass), for resolution at
  Phase B design time, not now**: this design's own justification —
  "nothing ever touches the connection concurrently" — holds only
  because Phase A has no autonomous background work; every DB access is
  a direct consequence of a client's own request. Ops (Phase B, B.1
  onward) introduces exactly that: background jobs needing DB access
  *and* outbound HTTP calls to external services, running independently
  of any client request. A long-running poll could block the event loop
  for every concurrent GraphQL request while it runs. Not a bug today —
  a decision whose stated premise expires the moment B.1 starts.
  Confirmed with the user: address as part of B.1's own design work
  (see `BUILD_PLAN.md`'s B.1 entry), not pulled forward into Phase A.

**Resolved 2026-08-09 (B.1)**: Ops is a **separate process, with no
database connection of its own at all** — it drives LCARS purely
through the GraphQL API, holding its own bearer token and sending
`X-LCARS-Client: ops`, the same shape Data/Holodeck/Captain's Log
already use. This dissolves the concurrency question rather than
solving it: only LCARS's own single event loop ever touches the shared
`sqlite3` connection, exactly as §11.2's original justification assumed
— Ops adds a second caller of the API, not a second caller of the
database. Three alternatives (a second in-process connection with WAL;
a dedicated worker thread/process inside the same container; wrapping
every Ops-triggered call in `asyncio.to_thread()`) were presented
alongside this one and explicitly not chosen. Reasons, not just the
outcome: **§1's own naming table already lists Ops as a distinct named
component** ("Chabrol's background scheduler"), not a mode of LCARS
itself; **§3 principle 8** already frames every LCARS-facing actor as a
peer client talking over the same API, no privileged one; and this
keeps the "nothing touches the connection concurrently" premise
literally true going forward, rather than requiring it to be re-proven
every time a new Ops responsibility is added in B.2–B.10. Cost, stated
plainly: every Ops responsibility needs a *real mutation/query* to act
through — some already exist from Phase A (`refreshShowMetadata`,
`reconcileSeasonMapping`, `refreshShowServicePresence`), others (
recording service health, per-episode availability) don't yet and
become explicit schema work in their own later B-steps, not something
Ops can shortcut by reaching into the database directly. Deployment:
one more service in the same Docker Compose stack (§11.3) alongside
`lcars`, not folded into its own container — a small, deliberate
addition to that section, not a change to it.

**B.1's own two follow-on schema questions, resolved by reuse rather
than asked separately** (both flagged as blocking by the design
review, both closeable directly from precedent already in this
document, same "low-stakes, easily revisable, resolved by close-
reading" class of call §6.11/§6.6 already made without a separate
round):
- **Per-show due-for-refresh state**: a new `show.metadata_last_
  refreshed_at` column (nullable `DateTime`), stamped at the end of
  `metadata.fetch_and_populate()` (§6.7/A.8) every time it runs,
  regardless of whether any individual source branch inside it
  succeeded — the exact same per-entity "last time this was checked"
  shape `season.last_reconciled_at` (§5.5) already established, applied
  one level up. A show just added (A.8's inline call) is therefore
  already "refreshed today" and correctly skipped by Ops's very next
  poll — no wasted re-fetch on the same day it was created.
- **The on-open trigger is not a separate mutation** — it's the same
  `dueForMetadataRefresh` query (below) called by whichever caller
  happens first, Ops's own clock or a client on open, followed by the
  existing `refreshShowMetadata` (A.8) for whatever comes back. The
  once-per-day ceiling lives entirely in the timestamp column above, so
  calling it twice on the same day — once from Ops, once from a client
  opening — is naturally idempotent no matter which fires first; no new
  mutation, no new concept, "not stacking" falls out of the shared
  state rather than needing its own guard.
- **New `Query.dueForMetadataRefresh`** (§8's "deliberately designed
  query shapes" precedent): shows where `status = WATCHING` **and**
  actively airing (reusing A.10's own `_show_is_airing` predicate — any
  episode with a null or future `air_date_utc` — verbatim, not
  re-derived; §6.7's "watching-status, actively-airing shows" reads as
  one combined filter, not two independent background jobs, confirmed
  by its own very next sentence: "Not-airing/not-watching shows get no
  background refresh") **and** not yet refreshed since the start of
  "today" in `home_timezone` (§6.13) — the first real consumer of that
  setting, exactly the cadence it was already named for. A `ShowConnection`,
  same Relay-cursor shape as every other list field (§8/A.2).

  **Real consequence of reusing `_show_is_airing`, made explicit rather
  than left implicit**: that predicate's own docstring (A.10) states "a
  movie has no episode rows at all, so it's always non-airing" — reused
  here unchanged, a `media_shape = movie` show can therefore **never**
  appear in `dueForMetadataRefresh`, regardless of `status`, permanently.
  Deliberate, not an oversight: a `watching` movie is, almost by
  definition, already released (its own `status` implies someone is
  partway through or has already watched it), so its metadata (cast/
  synopsis/poster/duration) has little reason to keep changing the way an
  actively-airing episodic show's does — the whole reason a *daily*
  cadence exists. File-availability changes for a movie are B.3's job
  (Radarr polling), a different concern from metadata content. Same
  "low-stakes, easily revisable" class of call as A.13/A.14's own
  self-flagged scope splits — reversible by simply dropping the airing
  gate for `media_shape = movie` specifically, if real use shows a movie
  genuinely needs a periodic re-fetch after all.

  A second, smaller consequence of `metadata.fetch_and_populate()`
  stamping unconditionally (above): `addShow` always creates a `planned`
  show (A.11's own test note), so every show is stamped "refreshed today"
  at creation regardless of eligibility — a show flipped to `watching`
  later the same day is correctly `dueForMetadataRefresh`-eligible only
  from the next day on. Self-correcting, one day's delay at most, and
  arguably right (it genuinely was just fetched) — noted here so a future
  audit reads it as understood, not rediscovers it.

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

**Ops's own deployment, added 2026-08-09 (B.1, §11.2)**: the same
image, a second service entry in the same compose stack, not a second
Dockerfile — `pip install .` already picks up both `lcars`/`ops` as
sibling packages under `src/` (`[tool.setuptools.packages.find]`), and
Ops needs none of `lcars`'s own build-time inputs (no `schema.graphql`
package-data, no Alembic migrations to run against a mounted volume —
it never touches the database). The compose `ops` service overrides
`command` to `ops run` instead of the image's default
`alembic upgrade head && lcars`, and gets its own environment
(`OPS_LCARS_URL` pointing at the `lcars` service's internal compose
address, `OPS_LCARS_BEARER_TOKEN`/`_FILE` — same A.23 secret-file
convention, a copy of the same value configured on the `lcars` side of
the bearer-token check, §8).

**LCARS's own filesystem mount, added 2026-08-09 (B.3b, §5.2)**: the
`lcars` compose service gains its first-ever filesystem mount — the
same media volume Sonarr/Radarr already mount, at the identical
container-internal path they use, so a show's own Sonarr/Radarr-
reported `path` is directly usable from inside the LCARS container too
with no translation. Confirmed against the user's own real stack:
Sonarr/Radarr both mount `${DATA_PATH}:/data` where `DATA_PATH=
/mnt/ranelagh/data` (their media NAS); `lcars` gets the same
`${DATA_PATH}:/data:ro` line — read-only, since nothing in LCARS ever
needs to write or delete a media file, only read one to confirm it
exists (`auditLocalFiles`'s own orphan-discovery pass, §5.2's B.3b
note). Everything else B.3/B.3b does (`pollFileAvailability`/
`backfillFileAvailability`/`auditLocalFiles`'s own reconciliation half)
stays API-only and needs no mount at all — this is strictly additive,
not a change to how those already work.

### 11.4 ID scheme

See §5.0 — short, type-prefixed, human-readable ids
(`{prefix}-{6-char Crockford-style string}`), not auto-increment
integers or UUIDs, with collision-checked generation via `nanoid`.
