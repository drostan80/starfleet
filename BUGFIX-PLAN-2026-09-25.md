# Bug-fix session plan — 2026-09-25

Sources: `HANDOFF-2026-09-23.md`, `NEXT_UP.md`, the 09-22 and 09-23 session
transcripts, the design docs in `~/repos/starfleet-archive/` (SCOPE.md, claude-plans),
and Claude memory. Production is on **v0.2.62**. Nothing here has been started.

---

## 0. Decisions (answered 2026-09-25)

1. **Status of an auto-created, already-aired, unwatched season**: follows the
   *previous* season. Previous season watched (completed/watching) or paused →
   `paused`; previous dropped → `dropped`. **Either way, nothing is pushed to
   external services** (AniList, MAL, Sonarr...). Future (not yet released) seasons →
   `planned` (09-23 rule, unchanged).
2. **Session goal**: establish what works and what doesn't, and fix what can be
   fixed. The plan-violation sweep (step 6) is part of that.
3. **Orphaned FK rows**: delete them.

## 1. Design rules every fix must follow

Written from your own words and decisions. SCOPE.md §3 #5 ("LCARS push-only") is
superseded wherever these rules disagree with it.

| Rule | Source |
|---|---|
| **Absolute episode order is the spine.** Memory Alpha/AniDB sets the order; everything else maps onto it. An LCARS season number is never a TVDB season number. | Your 09-23 message; D1–D3 memo |
| **AniList is authoritative.** On a score disagreement, AniList's value wins. | 09-22: "accept scores from anilist, this is intended" |
| **MAL is a mirror of AniList.** A MAL id follows AniList's pairing (Fribb), and MAL status/score/progress follow the AniList entry. | Memory `mal-mirrored-from-anilist-nothing-unique` |
| **Identity comes only from ID crosswalks** (Fribb, Wikidata, Anime-Lists). Never from a title search. No match means the item stays unlinked and a review opens. | v0.2.62 root cause; 09-23 |
| **Missing evidence is not evidence.** Having no candidate never clears a stored link. | v0.2.58 incident |
| **Purge order: Sonarr → LCARS → lists.** | Memory `junk-purge-order-sonarr-first` |

## 2. Rules for how the work is done (from the 09-23 mistakes)

- Every code fix comes with a regression test that **fails on the old code**.
- Investigation and verification scripts run on a **copy** of the prod DB, never on
  the live one, even when they look read-only.
- Take a prod DB snapshot before every deploy or data operation
  (`/opt/appdata/lcars/db/lcars.db.bak-20260925-<what>`).
- Check CI through **job conclusions** (`gh run view <id> --json jobs`).
- Deploy with the process in memory. SSH is exactly `ssh tiny@192.168.0.152`.
- A change to **when** a job runs gets its own review before it ships.
- "Fixed" means observed in prod or in the browser. UI fixes are done only once
  **you** have confirmed them. Anything else gets reported as "unverified".

---

## 3. The session, in order

The first three items come first because they are still damaging the lists every
time a sweep runs.

### Step 0: Pre-flight (read-only)
Two days and several ops passes have gone by since the handoff. Check the prod
version (v0.2.62?), the open review count, and ops/LCARS errors since 09-23. Then
copy the prod DB locally for all investigation work.

### Step 1: Sonarr "TBA" placeholder episodes get marked watched
- **Symptom**: 6 unreleased S2s each had one `2x1 TBA` placeholder, dated to the S1
  premiere and marked watched. That made the seasons look watched and completed.
- **Unknown**: which code writes these. `grep TBA src/` finds nothing, so the writer
  is generic.
- **Investigate** (on a DB copy): the history tables record the writer of each row.
  Look up the `watch_event`/`status_change` rows for those 6 episodes (who wrote
  them, and when).
  - Suspects: a completion sweep, mark-show-watched, or a progress import that maps
    S2 onto S1's list entry.
- **Fix**: nothing marks an episode watched if it hasn't aired, has no air date, or
  has a placeholder title. Also find why the placeholder got S1's premiere date.
- **Data**: clear the bad watched state on the 6 (snapshot first).
- **Done when**: the next sweep in prod leaves them unwatched.

### Step 2: Auto-created seasons copy the show's status
- **Where**: `season_ranges.inherit_season_status`. Four call sites:
  `season_ranges.py:372, 384, 497` and `show_backfill.py:482`.
- **Fix**: replace the inheritance with the rule from decision 0.1, in one function
  that all four call sites use. Seasons created as `dropped` are never pushed to the
  lists.
- **Test**: a show dropped at S1 gains S2..Sn → none of them is `dropped`, and none
  is pushed to the lists as dropped.
- **Data**: none. The 72 "planned" seasons are yours to review ("my work not yours").

### Step 3: Completing a season doesn't push MAL episode progress
- **What we know**: `setSeasonStatus` (`resolvers.py` ~3486-3504) pushes only the
  MAL *status*. The progress pusher `_push_mal_season_progress` (~1022) computes
  progress from watched episodes, so a season marked completed without per-episode
  watches shows 0/N on MAL. AniList fills this in automatically; MAL doesn't.
- **Fix**: on `completed`, push `num_watched_episodes` = the episode count **of the
  MAL entry itself** (its `num_episodes`, or the paired AniList entry's count),
  mirroring what AniList does. **Not** the LCARS season's episode count: LCARS splits
  seasons to the finest source (D1), so one LCARS season isn't always one MAL entry.
  Using its count would repeat the "LCARS number = external number" mistake. Don't
  create per-episode watch events.
- **Test**: completing a season with 0 watch events sends MAL
  `num_watched_episodes` = the MAL entry's count, including a case where the LCARS
  season and the MAL entry have different episode counts.
- **Done when**: after one real completion, MAL shows N/N.

### Step 4: The first hourly tick after a restart blocks LCARS
- **Symptom**: ops read timeouts at 10:36 and 12:33 on 09-23. LCARS held a write
  transaction for 30 s or more because the long sync pass runs inside a request
  handler.
- **Scope**: **LCARS only.** The Android app freeze is a separate, app-only bug (you
  confirmed on 09-22 that the web app was fine at the same time). It is not part of
  this session.
- **Investigate first**: find which ops call is slow (ops/LCARS logs), then decide:
  run the pass as a background job that returns right away, or commit in smaller
  chunks so the write lock is released between them.
- **This changes when a job runs**, so the design gets its own review before any code
  is written.
- **Done when**: after a deploy in prod, the first hourly tick has no ops `ReadError`
  and the UI stays responsive during it.

### Step 5: Service icons and planner banners are white until a hard refresh
- Claimed fixed three times without a browser check. **Reproduce first** with the
  `claude-in-chrome` skill: open the calendar, show and planner pages, reload
  normally several times, and capture a screenshot and the network log when icons go
  white.
- **Hypothesis to test**, not yet the cause: `icons.js` reuses fixed SVG gradient ids
  (`tg`, `tmg`). When the same SVG appears more than once on a page, `url(#tg)`
  resolves to the first copy in the document, so if that copy is hidden or removed,
  the fills break. The fix would be one unique id per instance.
- **Banners are a separate cause**: they are a CSS `--banner-url` background with no
  error handling. Fix: preload with an `Image()` and handle `onerror` with a retry or
  fallback.
- **Done when**: you confirm it.

### Step 6 (needs decision 0.2): plan-violation sweep
Search the code for anything still breaking the rules in §1:
- a title-search result (`results[0]`, first hit) used as identity;
- an LCARS season number used as an external (TVDB/Sonarr) season number;
- MAL written from anything other than the AniList pairing;
- missing evidence used as a reason to clear a link;
- Memory Alpha propagation, which is **insert-only** and never corrects a wrong id.
  That is a likely source of recurring bad data.

Output: a list of places, each with a proposed fix. You approve before any change is
made.

### Step 7: Data cleanup (after steps 1–3 are deployed)
- **List check**: re-read AniList and MAL to confirm the 09-23 push held, e.g. the 7
  completed seasons show N/N on MAL. Bungo S3 (MAL 38003) is still "watching" on
  MAL with nothing on AniList. Mirror it from AniList (MAL mirrors AniList), which
  means removing it from MAL unless AniList gets an entry.
- **FK orphans**: handle per decision 0.3. Snapshot first; check the count afterwards.

### Step 8: Close out
- Update `NEXT_UP.md`, marking only what was verified.
- Show the emoji to-do summary.

## Out of scope
- Reviewing the 72 "planned" seasons (yours to do).
- The Android app freeze (not critical).
- Key rotation (at cutover).
- All ideas and future features.
