# Code plan — bring LCARS to RULEBOOK.md (draft for validation, 2026-09-27)

**Status: PROPOSAL. Nothing here is built.** Every item needs the user's yes
before code is touched (RULEBOOK "Every code change is validated by the user
first"). Discrepancy ids (A1, C4…) refer to `RULES-VS-CODE.md`; rule ids (R#) to
`RULEBOOK.md`.

## Order (user, 2026-09-27)

**Step 1 is the data plan** (`PLAN-DATA.md`): agree the starting database and how
the gap is derived. Then the phases below, in order.

## How this is delivered

- **Ops stays stopped, v0.2.71 stays undeployed.** Nothing ships piecemeal:
  partial rule engines running on live data is how the last three weeks broke.
- Work happens on a branch, tested against a **copy** of the 09-06 snapshot.
- **One cutover:** the new code and the rebuilt database (see the data plan)
  are validated together, then deployed once.
- **No external writes on deploy.** Pushes to AniList/MAL/Sonarr stay off until
  the user has reviewed a dry-run diff of exactly what would be sent (phase 9).
- `feat/sequel-only-stubs` (adf4951) is **abandoned** (user yes, 2026-09-27). It was
  never merged or deployed: it limited stub creation to sequel/prequel relations.
  Dropping it changes nothing live; the stub creation it narrowed is removed
  entirely in 5.2 (R3.5). Only its tests are lost; 5.2 gets its own.
- Untouched by this plan: scores, paced mode, soft/hard delete, art, the
  rewatch schema (R4.6a), the franchise/universe layer (R1.20–21).

Each change lists: **fixes** · **files** · **behaviour** · **data** ·
**external** (AniList/MAL/Sonarr) · **stops/breaks**.

---

## Phase 0 — Supporting changes found while planning — DONE 2026-09-27 (e72c968)

**0.1 TVmaze specials** (approved 2026-09-27). `tvmaze.py:117` calls `/shows/{id}/episodes`, which leaves
out specials (1 special among 26,534 fetched rows), and `tvmaze.py:141` skips
specials. TV season 0 (712 episodes on the dev copy) therefore has no TVmaze
data. Fix: fetch with `?specials=1` and keep specials. The regular TVmaze fetch is
complete (all 456 tracked TV shows, 14,837 of 15,794 TV episodes matched) but is
used for air dates only, never for numbering (→ phase 3).
- data: adds TVmaze rows only. external: TVmaze reads. stops: nothing.

**0.2 Season status history** (approved 2026-09-27). `setSeasonStatus` writes no history, so your
season-level changes can't be told apart from automation (found while deriving
the gap). Add a `season_status_change` log (same shape as `status_change`).
Not a rule, a support for R2.x auditing — needs your yes.

**0.3 Names follow the rulebook.** Code names that clash with rulebook terms are
renamed (after your check):
- **"franchise collision / franchise merge"** (`show_merge.detect_franchise_collisions`,
  `resolveFranchiseMerge`, review fields `franchise_auto_merge`,
  `franchise_season_collision`). It has nothing to do with a franchise (R1.20): it
  finds **two LCARS show rows with the same TVDB id** and folds one into the other
  as a season. Name **same-TVDB show consolidation** (approved 2026-09-27)
  (`consolidate_same_tvdb_shows`, `resolveTvdbConsolidation`). What it does today,
  and what breaks the rules:
  - picks a parent (tracked, lowest episode numbers, oldest) and, when no show is
    tracked, **promotes a stub to tracked** by itself (R3.5);
  - moves **all** the child's episodes into **one** season: Fribb's TVDB season for
    the child's AniList id, else next after the parent's numbers, else last+1 —
    so OVAs, films and specials became whole seasons (R1.4, R1.8);
  - renumbers episodes to `(target season, original episode number)`, not by
    episode mapping (R1.17);
  - runs unattended in `pollMemoryAlpha` every tick (the 09-06 13:12Z merge).
  The goal (one show per TVDB id, R1.14) is right; per 5.3 the code is replaced by
  the episode-level add check, and any leftover duplicate goes to review.
- "stub" (not a rulebook term) disappears with 5.2.
- episode "skipped" state (not a rulebook term; episodes are only watched or not,
  R2.2) disappears with 2.5.
- A full pass over GraphQL/UI labels for other clashes is part of phase 8.

## Phase 1 — Rule validator (read-only, built first) — DONE 2026-09-27

`lcars rulecheck <db> [--json]`, `src/lcars/rulecheck.py`, 17 checks; exit 1 on any
violation. Baseline results are in PLAN-DATA.md.

**1.1 `lcars/rulecheck.py` + `lcars rulecheck <db>` CLI.** One check per rule:
every episode has an abs number (R1.0); no episode left only in TVDB season 0
without a LCARS season/level (R1.8); every season has ≥1 span and spans don't
overlap within a level (R1.11–12); AniList/MAL ids only at season level (R1.23);
every tracked show has a TVDB id or is an individual season (R3.2); show status
= last non-skipped season (R2.13); parts vs season consistency (R2.18); no
tracked untracked-stubs (R3.5); skipped seasons have no AniList/MAL entry in
`list_baseline` (R2.10); etc.
- behaviour: none — reads only. It is the acceptance test for every later phase
  and for the rebuilt data.
- data: none. external: none. stops: nothing.
- Not reused: last session's `rulecheck.py` (it encoded rules since restated).

## Phase 2 — Schema: levels and spans (P1, P2, P3 approved 2026-09-27)

**2.1 Spans (R1.11, R1.12; B1).** New table `season_span(season_id, abs_from REAL,
abs_to REAL)`; a level may have several rows. `season.abs_start/abs_end` dropped.
- **P1 (approved):** episode membership in a level is *derived* from spans (an
  episode belongs to every level whose span contains its abs number), instead of
  the single `episode.season_id` FK. `episode.season_id` is kept only as "its TVDB
  season" for display until removed.
- data: migration fills one span per existing season from its current range;
  real values come from the rebuild. breaks: every query using
  `abs_start/abs_end` (53 references) is rewritten.

**2.2 Levels (R1.10, R1.13, R2.7 caveat, R2.18; B2, B3).**
- **P2 (approved):** `season` gains `kind` (`tvdb_season` | `part` | `special`) and
  `parent_id`. A TVDB season is its own row; AniList/MAL parts are `part` rows
  under it; a film/OVA/special run with its own id (R1.4, R1.13, "specials and
  movies … completed individually") is a `special` row with its own span. Each
  row holds its own status.
- data: current `(season_number, part_number)` rows become `tvdb_season` + `part`
  rows. breaks: the `UNIQUE(show_id, season_number, part_number)` key; all code
  selecting seasons by `season_number` (completion, progress, status).

**2.3 Individual seasons (R3.2, R3.6, R3.6a–c; B5).** Scope (R3.6c): new planned
seasons TVDB doesn't have yet; nothing existing except seasons added since 09-13
with no TVDB link.
- **P3 (approved):** `season.show_id` becomes nullable; an individual season is a
  season row with no show, carrying its own AniList/MAL ids and episodes. LCARS
  **looks up TVDB** to find a matching show; a match is **proposed for your
  review, never attached automatically**. Once you accept, it joins that show
  (spans renumbered into the show's numbering).
- breaks: every `JOIN show` on seasons must tolerate NULL; UI needs a place to
  list them.

**2.4 AniList/MAL only at season level (R1.23; B4).** Stop writing
`show_external_id` rows for `anilist`/`mal`; lookups move to
`season_external_id`. At least 19 query sites in 9 files (shows.py,
metadata.py, season_ranges.py, show_backfill.py, show_merge.py, browse.py,
local_audit.py, tvdb_backfill.py, resolvers.py), incl. `find_existing_show`,
`_find_stub_show`, `is_users_own_season`, `_find_parent_via_anilist`,
`_ensure_anilist_link`.
- data: existing show-level rows removed in the rebuild.

**2.4b TVDB movie ids apart from TVDB series ids (R1.23, R3.7).** LCARS stores
both as `tvdb`, and several films carry a TVDB *movie* id read as a series id
(Your Name. = movie 197). Films get `tvdb_movie`; a film inside its show uses the
show's series id plus its own abs number (R1.4).

**2.5 Episode watched is a boolean (R2.2; C11).** Episodes can't be skipped; the
code still has a third `skipped` state and a `markEpisodeSkipped` mutation. Both
go; `episode.state` → `watched` (0/1). No episode is in that state today
(dev copy: 0 rows), so no data changes.
- breaks: any client button calling `markEpisodeSkipped`.

**2.6 Remove `show.status_before_pause` (C13).** Resume now just sets a status;
R2.13a applies it to the last non-skipped season.
- **Question for you (data):** nothing needs keeping from it? (It only remembered
  what to resume to.)

### Phase 2 — done 2026-09-28 (migration `f4a5b6c7d8e9`)

Additive, as agreed: the old columns (`abs_start/abs_end`, `part_number`, show-level
AniList/MAL rows) stay and are filled alongside; they go in one step before cutover.
- `season_span` (ON DELETE CASCADE); one span per season that had `abs_start/abs_end`
  (310 in the 09-06 snapshot).
- `season.kind` (default `tvdb_season`), `parent_id` (containment only: a part's TVDB
  season; CHECK: a part has a parent, a TVDB season has none), `show_id` and
  `season_number` nullable.
- **Decimal season number (user, 2026-09-28, proposed option):** `season_number` stays
  TVDB's whole number (NULL for side pieces); new `season.decimal_season_number` REAL
  (2, 2.5, 2.1…), GraphQL `Season.decimalSeasonNumber: Float`. A side piece's place is
  its decimal number (2.5 = after S2); no separate anchor column. `seasonNumber: Int!`
  stays until phase 8 moves every client; no side-piece row may exist before that.
- Unique key is now `(show_id, season_number, part_number, kind)` so a TVDB season and
  its part 1 can coexist; old code only makes TVDB seasons, so nothing changes for it.
- Transition triggers (`season_span_from_abs_*`, `season_decimal_number_*`) copy old
  writers' `abs_start/abs_end` and `season_number` into the new columns; dropped with
  the old columns.
- The migration stops if any `part_number <> 1` row exists (none do) rather than invent
  a parent.
- 2.4b: `tvdb_movie` service + URL template. No row uses it yet: the rebuild applies the
  TVDB decisions. Every TVDB reader asks for `tvdb` exactly, so it's ignored elsewhere.
- 2.5: `markEpisodeSkipped` removed (no client called it). The `episode.state` CHECK
  still allows `skipped` (table has a generated column; goes with the final step).
- 2.6: `show.status_before_pause` dropped (user: nothing to keep). Its only reader was
  the Sonarr resume path, which has been dead since 09-26 (Sonarr never changes LCARS).
- rulecheck: R1.12 level-aware, new R1.10 (parts inside their TVDB season).
- **Deferred to phase 5:** `episode.show_id` is still NOT NULL, so an individual season
  can't hold episodes yet; phase 5 (the add check) rebuilds `episode` for it.
- **Asked (RULEBOOK Q-S):** an individual season's kind. **Asked (Q-R):** whether a
  film/mini inside a season's window is under that season; R1.10 checks parts only
  until answered.
- **Before phase 5 creates an individual season:** GraphQL `Season.show: Show!` and
  `seasonNumber: Int!` would fail on it — make both nullable (or keep individual
  seasons out of Season-typed queries) first.

## Phase 3 — Numbering: LCARS absolute numbers — APPROVED 2026-09-28

Approved with the user's answers: 3.0 `individual_season` kind (R3.6d); fallback per
R1.2d; AniDB fetch ≈2 h with buffer — but first recover the earlier massive AniDB
episode fetch from intermediate snapshots/live DB; source tables (anidb_*,
anime_list_*, tvmaze_*, …) carried from live into the rebuild ("essential — part of
what works now"); reads of prod allowed; Sonarr multi-show routing deleted, not ported.

### 3.0 done: `individual_season` kind (R3.6d) in the phase 2 migration.
### 3.1 done 2026-09-28: `episode.tvdb_absolute` (TVDB's absolute number, a mapping,
captured on every Sonarr read); Sonarr matching = captured TVDB season/episode →
`tvdb_absolute` → (until 3.2) LCARS absolute number → TVDB season/episode; new episodes
keep TVDB's season/episode (R1.9a; the "subdivision offset" routing is gone); multi-show
routing deleted (metadata + availability): a TVDB id held by several shows gets nothing
filed (R1.14). Tests for the deleted routing replaced.

### 3.2 done 2026-09-28: Memory Alpha numbering engine `lcars/numbering.py` (pure
`plan_show`, `load_show`, `apply_plan`, `renumber_show/_all`, `lcars numbering <db>
[--show] [--apply] [--json]`). Runs after every Sonarr fetch and in Memory Alpha's poll
(replaces `_synthesize_absolute_numbers`, the Sonarr absolute-number overwrite and both
range fills). `show.absolute_numbering_source` (anidb | tvmaze | tvdb), change log
`absolute_number_change` (reconciliations only). Old abs→span triggers dropped.
First dry run on live + 09-28 AniDB data: 1,785 shows — anidb 160, tvmaze 434,
tvdb 1,191; flags: no_level 1,296, no_air_date 94, film_placement 88, …

#### Phase 3 — Numbering: LCARS absolute numbers (R1.0, R1.2, R1.2a–b, R1.3–R1.5, R1.8, R1.18–19; A1–A5, G3, G4)

**3.1 Memory Alpha numbering engine** (R1.2c: Memory Alpha is the authority) (`lcars/numbering.py`, replaces
`_synthesize_absolute_numbers`). Per show, orders every episode — season 0
included — by AniDB (anime) / TVmaze (TV) order, tie-broken by air date-time,
aligned to TVDB season spread; season-0 items: inside a season's air window →
`.1`, `.2`… after the preceding episode; between seasons → whole number
(shifting later ones); AniDB-specific exceptions kept and listed for you to
confirm (R1.8c). Frieren-style pre-air block → 0.5.
- data: rewrites `episode.absolute_number` for every show. Measured on the 09-22
  dev copy: anime 1 of 4,959 missing (season 0 got `.1` decimals, which change
  when whole numbers go between seasons); **TV 10,210 of 15,794 missing** — needs
  TVmaze episode data for all TV shows.
- **Consequence:** AniDB allows 1 request / 2 s; shows without `anidb_episode`
  data must be fetched first (count to be measured on the snapshot before a
  timeline is given).

**3.2 Source numbers become mappings only (R1.2a).** Sonarr/TVDB SxxEyy, TVDB
absolute, AniDB epno, AniList/MAL per-entry episode numbers live in
`episode_external_id` / `episode_anidb_mapping`. **Critical consequence:** every
lookup that treats `absolute_number` as Sonarr's must move to stored Sonarr
coordinates, or downloaded files land on the wrong episodes the moment a whole
number is inserted between seasons: `availability.py` (~L304–440, multi-show
routing), `sonarr_match.py:95`, `_fetch_sonarr_multi_show`, the integer range
fill in `season_ranges.py`, the AniDB comparison in `anidb.derive_episode_mappings`.
- breaks if skipped: file availability, grabs page, mpv launch paths.

**3.3 Films integral to the story (R1.4; A5).** A film with an abs number is an
episode (kind `bonus_movie`) of the show with a `special` level; Radarr
availability attaches to it. Standalone `show` rows for such films are folded
in by the rebuild.
- breaks: Radarr-only movie shows for those films disappear as separate shows.

## Phase 4 — Status engine (R2.x; C1–C12, G1, G2, G5)

**APPROVED 2026-09-28** (with 3.4 films moved into phase 5, next to the same-TVDB
consolidation it shares its merge work with).

**4.1 One module `lcars/status_rules.py`** holding every rule, replacing
`inherit_season_status`, `new_season_status`, `auto_season_fields`,
`is_users_own_season`, `_compute_show_status`, `_recompute_show_status`,
`_reopen_show_if_completed`, `_try_complete_season`, `_try_complete_show`,
`_bulk_mark_all_aired_episodes_watched`, `_stamp_completed_at_if_highest_season`.
- R2.16 new season: previous completed/watching/planned → planned;
  paused/dropped/skipped → skipped. Also re-skips later *auto-planned* seasons, and
  user-planned ones after the warning (R2.16 J3/J4).
- R2.19 earlier seasons found late → skipped (episodes still fetched).
- R2.13 show = last non-skipped TVDB season; all skipped → skipped. No
  "planned → watching" exception, no skipped tombstone (C4, C5).
- R2.13a show picker → last non-skipped season (C6).
- R2.14 planned + episode watched → watching, the only auto path (C8).
- R2.15 all episodes of a level watched → completed, incl. paused/dropped (C12);
  level set completed → **all** its episodes watched, unaired included (C9).
- R2.18 cascade TVDB season → parts (completed parts kept); season from parts.
- **Behaviour change you will see:** a show whose last season is planned shows
  **planned**, not watching; completing an airing season marks unaired episodes
  watched (with the R3.4 warning first).
- external: status changes feed phase 7/8 pushes (off until phase 9).

**4.2 Skipped = not followed (R2.10; G1).** Skipped seasons excluded from:
metadata/episode fetch (except R2.19 numbering fetch), availability, calendar,
next-up, backlog, airing lists, browse/add "future seasons".
- breaks: skipped seasons' episodes stop getting air-date/availability updates.

**4.3 Warnings (R3.4, R2.16/J4).** GraphQL returns a "needs confirmation" with the
effect (e.g. "this will mark 4 unaired episodes watched", "this show has later
seasons planned — skip all?"); UI shows it.

## Phase 5 — Adding (R3.x, R4.7, R5.2–R5.3; B6, D1–D5, G5)

**5.1 One normal check (R3.1)** used by every entry point: browse/add, Sonarr
webhook + `reconcile_arr_state`, AniList/MAL list entries (R4.7), relation
discovery. Decides: new season of an existing show / part of a season / first
season of a new show / individual season (no TVDB id).
- R3.2 TVDB id required, else individual season; R3.3 historical exception is
  never automated.
- R3.7 every automatic TVDB link verified: an independent source (Fribb, Sonarr's
  series) agrees and the TVDB name matches the titles; else the user decides. A
  title-search match is never accepted on its own.
- R3.5/R3.5a related entry: added only to a show by TVDB id (same show or a
  tracked show); else **not added**.
- R5.3 Sonarr add of multi-season show: latest season planned, earlier skipped.

**5.2 Remove stubs (B6).** `_create_relation_stub` and the stub-promotion paths go.
- data: existing stubs removed in the rebuild (they are `tracked = 0`).

**5.3 Remove the automatic franchise merge (D4)** in `pollMemoryAlpha` step 2g
(`show_merge.detect_franchise_collisions` → `merge_season_into_show`). Shows
sharing a TVDB id are one show by construction (R1.14) through 5.1; any collision
found goes to review, never auto-merged. `pollShowMerges` stays review-only.

**5.4 AniList/MAL list entries added automatically (R4.7; D3).** A list entry
not in LCARS goes through 5.1 as a season with the list's status.
- **Consequence:** on first run every list entry not in LCARS gets added — the
  dry run (phase 9) shows the list first.

## Phase 6 — Sonarr (R5.x; F2–F5, G6)

- **6.1** New show added to LCARS with a TVDB id → added to Sonarr, monitor future
  episodes (R5.5), from every add path (F2).
- **6.2** Season → planned/watching: monitor that season's future episodes;
  paused/dropped/skipped: unmonitor that season **and later seasons**; completed:
  nothing (R5.6–R5.8; F3, G6). Per-season, not whole-series.
- **6.3** A show already in LCARS and not in Sonarr is **never** added
  automatically (R5.9) — `ensure_arr_monitored`'s add branch removed (F5).
- **6.4** Sonarr → LCARS: only adds (R5.2–R5.4), already the case (F1).
- **Consequence:** existing Sonarr monitoring is corrected to these rules only
  through the phase 9 dry run.

## Phase 7 — AniList/MAL mirroring (R4.x; C3, C10, E1–E3)

- **7.1** Push every non-skipped season's status (drop `list_sync = 0`) (C3).
- **7.2** Skipped: never pushed; an auto-added planned season moved to skipped is
  **deleted** from AniList/MAL (R2.10; E3).
- **7.3** Progress per id level from its span: part 2 progress = episodes watched
  within part 2's span, not `MAX(episode)` of the TVDB season (C10).
- **7.4** Timestamp conflict model (R4.10; E1): propagation queue with timestamps;
  a remote change later than the last reconciliation wins, an earlier one is moot.
- **7.5** Ordering (R4.9; E2): one list-hub loop — check AniList → propagate →
  check MAL → propagate; propagation calls first when an API was down.
- **7.6** AniList ↔ MAL mirror everywhere possible (R4.4): entry on one, missing on
  the other → added there.

## Phase 8 — UI (web + Data where affected)

Sub-seasons under TVDB seasons (R1.10), specials/films as their own items,
individual seasons list, skipped hidden from airing/add/browse, confirmation
warnings (4.3), no episode "skip" action.

## Phase 9 — Dry run and cutover

- Run everything on the rebuilt DB copy with external writes **captured, not
  sent**: full list of AniList/MAL writes/deletes and Sonarr monitor changes.
- You review that list; rulecheck must pass; then one deploy, then the captured
  writes are sent in capped batches.

---

## Data plan — outline, filled in after code approval

1. Copy the 09-06 snapshot (`.backup`, not `cp`).
2. Run the new engines on it (numbering, levels, status, add check) — no
   hand-fixed rows.
3. Replay only **user-originated** changes since 09-06 (status changes, watch
   events, shows created, score change) through the normal code paths, not raw
   inserts — automated adds that R3.5 rejects are not replayed.
4. Compare with the live AniList/MAL lists; `list_baseline` timestamps separate
   LCARS's own pushes from your real remote changes (R4.10).
5. rulecheck passes → you review the diff → apply → external writes last (phase 9).

## Open for you on this plan

- 0.1 TVmaze specials fetch, 0.2 season status history, 0.3 rename to "same-TVDB
  show consolidation" — yes/no, or another name?
- The rest of the phases, one by one, once the data plan is agreed.
