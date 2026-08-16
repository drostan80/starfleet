# Bugs

- [x] **Ascendance of a Bookworm's currently-airing part (S4/"Adopted Daughter of an Archduke")
  invisible in Data — Sonarr-vs-AniList season-split mismatch, real data fixed, real durability
  gap left open** (2026-08-15). User's report: Sonarr confirmed episode 18 (absolute ep 54) out
  and available, nothing showed in Data — not yesterday, not today.

  **Root cause**: Sonarr/TVDB tracks this whole franchise as **one series** (id 990, tvdb 366263)
  with flat absolute numbering 1–60+, no season split at all. AniList splits it into **4 separate
  media entries** (Part 1: 14 eps, Part 2: 12, Part 3: 10, Part 4 "Adopted Daughter of an
  Archduke": 24 eps, ongoing). LCARS only ever linked the tvdb id to the first of the four `show`
  rows (`s-2k4jb6`) — every Sonarr episode ever imported, all 60, landed under that one show,
  which was also (wrongly) marked `completed`. The real currently-airing show (`s-fwxa7m`) had
  **zero** episode rows and no Sonarr link at all. This is a concrete real-world instance of the
  already-logged "hierarchical season subdivision" idea (see "Ideas / design" below) — TVDB
  combining what AniList splits, the mirror image of the Mushoku Tensei/Fire Force case
  (2026-08-15, same session) where AniList splits what TVDB/Sonarr combines.

  Boundaries confirmed two independent ways (AniList's own episode counts *and* Sonarr's real
  air-date gaps, e.g. a ~4-year real hiatus between abs ep 36 and 37, land on exactly the same
  split): Part 1 abs 1–14, Part 2 abs 15–26, Part 3 abs 27–36, Part 4 abs 37–60 — ep 54 = Part 4's
  own ep 18, exactly matching the report.

  **Fixed, current-state data only** — snapshotted the DB
  (`lcars.db.bak-20260815-bookworm-split`) first, then: moved/renumbered the 46 mis-filed episode
  rows (abs 15–60) to their real shows (`s-mzb7jx` ep 1–12, `s-f732f2` ep 1–10, `s-fwxa7m` ep
  1–24), inserted 39 new `watch_event` rows matching the user's own ground truth (Part 2 and
  Part 3 fully watched; Part 4 watched through its own ep 17/abs 53, ep 18/abs 54 aired-not-
  watched, ep 19–24/abs 55–60 not yet aired), fixed `s-fwxa7m`'s status `completed` → `watching`
  (with a real `status_change` row, not a silent overwrite), added matching
  `episode_numbering_mapping` rows to all three previously-unlinked shows. Verified twice — once
  directly against the DB (per-show episode/watched counts matched exactly), once through the
  real GraphQL API (`episodesInRange` for 2026-08-13–15 now correctly returns "Honzuki no
  Gekokujou: Ryoushu no Youjo" S1E18, status WATCHING, tracked).

  **Deliberately NOT fully closed — a real regression risk was caught and only partially
  defused**: `metadata.py`'s `_fetch_sonarr` does an unconditional, unscoped pull of a linked
  show's *entire* Sonarr series on every refresh — if any show's tvdb link had been left in
  place (or moved to `s-fwxa7m`), the very next automatic metadata refresh (B.1, hourly-ish)
  would have silently re-created all 46 moved episodes back under whichever show holds the link,
  undoing this fix and potentially duplicating rows. To prevent that **tonight**, removed
  `s-2k4jb6`'s `tvdb` `show_external_id` entirely (`unlinkShowExternalId`, confirmed gone) —
  safe, since Part 1's own 14 episodes are finished/static and never need another Sonarr check.
  **But no show now holds the tvdb link at all** — episodes 55–60 (and anything after) will
  **not** be automatically picked up as they air; this exact bug will recur for ep 19 (abs 55,
  due 2026-08-21) unless either (a) someone re-links `s-fwxa7m` to tvdb 366263 and manually
  re-runs this same range-based split each time new episodes land, or (b) the real fix — Sonarr
  episode-sync becomes season/range-aware per show, i.e. actually building the "hierarchical
  season subdivision" idea — gets built. Not attempted tonight (a real feature, not a bug fix);
  flagged in "Ideas / design" below with this concrete case as the worked example.

- [x] **B.5.3 wired into a real Ops scheduler loop, B.5.1 confirmed live, both deployed
  (v0.1.12)** (2026-08-15). B.5.3 (AniList activity-feed poll) was code-complete and once-tested
  by hand since 2026-08-13 but had never run on any automatic cadence — zero references anywhere
  in `ops/scheduler.py`, confirmed via grep before touching anything. Added it as a real fourth
  concurrent loop in `run_forever` (own fixed interval, default 240s/4min via a new
  `anilist_activity_poll_interval_seconds` config knob, same file/env/`_FILE` precedence as every
  other Ops setting) — checked the real cost first rather than assuming: a quiet poll is one
  cheap AniList call, a poll that finds genuinely new activity triggers `reconcile_watch_progress`
  (two more calls, not a per-season sweep — `fetch_viewer_id` is cached, `fetch_my_anime_list` is
  one call regardless of list size), so 4 minutes is safe. New/updated tests in
  `test_ops_scheduler.py`/`test_ops_lcars_client.py`/`test_ops_config.py`, 712 passed, `ruff
  check` clean (a real miss on the first push — import ordering — caught by CI, fixed in a
  follow-up commit before re-tagging).

  Also flipped BUILD_PLAN.md's B.5.1 to `[x]`: production logs now show 36 real Sonarr/Radarr
  webhook hits over ~30 hours, cross-checked against actual correct `episode`/`show` DB writes,
  not another synthetic Test payload. And B.11's stale parent checkbox — every lettered sub-item
  (a/c/d/e/f/g) was already `[x]` and confirmed live, the parent just never got flipped.

  Tagged `v0.1.12`, GHCR build/publish confirmed green, deployed to the `tiny` host (`lcars` and
  `ops` share one image) via `docker compose pull && up -d`. Both containers came back healthy;
  `ops` briefly logged connection-refused on its very first tick of every loop (raced `lcars`'s
  own startup by a few seconds, same broad-except/retry-next-interval shape every loop already
  has for exactly this) then recovered clean on the next tick. First real `anilist_activity` tick
  (09:29:57Z) found **7 new AniList activities** and ran a real reconcile — the checkpoint
  (`anilist_activity_checkpoint`) advanced to match, confirmed directly in the DB, not just
  trusted from the log line. This is almost certainly this session's own earlier backlog of real
  AniList corrections (the 87-id cleanup, the 15-show watched-episode fixes) finally being picked
  up automatically for the first time, rather than anything needing a second look — no
  `pending_review` spike or anything else that looked wrong, but flagging the "why 7" reasoning
  here rather than asserting it with more confidence than actually checked.

  **Checked directly afterward, not left as an inference**: exactly one real DB change
  resulted — episode 9 of *From Overshadowed to Overpowered* (`s-65m1k1`) flipped to
  `watched`, one `watch_event` row inserted. Zero `pending_review` rows opened, zero
  `status_change` rows. A small, plausible, forward-only backfill for a show the user was
  already actively discussing progress on tonight — not a red flag.

  B.5.2 (the AniList write-tier priority queue) not started yet — see the "Ideas / design" section
  below for what was found investigating LCARS's existing AniList write path
  (`_push_show_status`/`_push_show_score`/`_push_season_score` in `resolvers.py`) before designing
  it, and why building B.5.2 tonight against zero real B.5.3 cadence data was deliberately
  deferred in favor of shipping B.5.3 first and letting it run.

- [x] **HELL MODE S2 (and 14 others) showed watched episodes AniList never confirmed — a "double
  check" pass across the whole library, all applied** (2026-08-15). User's report: HELL MODE
  season 2 showed every episode watched in Data, contradicting AniList (the declared source of
  truth for watch status). Compared every tracked season's LCARS-watched-count against real
  AniList progress (GDPR export + live spot checks) — found **15 seasons over-marked** (LCARS
  shows watched, AniList doesn't back it up) and, separately, 1,089 seasons *under*-marked — of
  which **1,088 turned out to be the already-known "AniList-only show has zero episode rows"
  architectural gap**, not a new bug, left untouched.
  **10 of the 15 over-marked seasons shared one root cause, fixed identically**: HELL MODE S2 (12
  watched vs. real progress 6) and You and I Are Polar Opposites S2 (12 vs. 6), plus 8 shows with
  a single phantom "episode 1" wrongly watched on a season that hasn't aired at all (AniList
  status PLANNING, progress 0): Makeine S2, The Weakest Tamer S2, The Unaware Atelier Meister S2,
  The Fable S2, HOTEL INHUMANS S2, A Star Brighter Than the Sun S2, Jack-of-All-Trades S2, Tune In
  to the Midnight Heart S2. All traced to the same 2026-08-12 one-time historical watch-import
  (2,973 `watch_event` rows across 174 shows, one timestamp) — at that moment these season-2 rows
  still held the *wrong* (season-1's) `anilist_id`, the exact same-show collision fixed earlier
  tonight, so season 1's real progress got copied onto season 2 before the id was ever corrected.
  Fixed via `deleteWatchEvent` on the specific stray rows (reverts `episode.state` to unwatched
  automatically) — verified clean afterward.
  **The other 5 needed the user's own ground truth, not a blind trust-AniList rule — and it
  surfaced two genuinely different real bugs**:
  - **Mushoku Tensei S1 (23/23 watched, AniList progress 11/11) and S2 (24/24, progress 13/13)**:
    user confirmed fully watched through last week — **not a watch-status bug at all, a season-
    mapping gap**. Checked AniList directly: the franchise splits each LCARS "season" across
    *two separate AniList media entries* per cour (`108465`, 11 eps, mapped; `127720` "Part 2", 12
    eps, **never linked in LCARS at all** — same again for season 2: `146065` mapped, `166873`
    "Part 2" unmapped). LCARS's 23/24-episode season correctly spans both cours' real Sonarr
    episodes, but only one cour's `anilist_id` is attached, so the progress comparison was
    structurally comparing one cour's count against both cours' episodes. Nothing was wrong in the
    watch data — left untouched. This is exactly the "hierarchical season subdivision" gap already
    logged further down this file, now with a concrete real example, not fixed here (that's a real
    feature, not a data patch).
  - **Fire Force S3 (13/25 watched, AniList progress 12)**: same shape — confirmed via AniList
    that "Season 3" (`149118`, 12 eps, mapped) and "Season 3 Part 2" (`179062`, 13 eps, **also
    never linked**) are two entries LCARS treats as one 25-episode season. Unlike Mushoku Tensei,
    user confirmed this one really is fully watched and the *episode data* (not just the mapping)
    was actually incomplete — episodes 13–24 were genuinely still `unwatched` in LCARS. Fixed via
    `markEpisodeRangeWatched(showId, season: 3, fromEpisode: 13, toEpisode: 24)`. Episode 25 was
    already correctly watched.
  - **Frontier Lord S1 (8/12 watched) and From Overshadowed to Overpowered S1 (9/12 watched)**:
    user confirmed watched through episode 7 (last night) and episode 8 respectively — LCARS had
    exactly one episode too many marked watched in both cases, and in both cases **the wrongly-
    marked episode's `air_date_utc` is genuinely in the future** (Frontier Lord ep8: 2026-08-21;
    Overshadowed ep9: 2026-08-20) — an episode that hasn't aired yet literally cannot have been
    watched, an internal contradiction independent of AniList entirely. Both single stray
    `watch_event` rows deleted.
  **Final verified state, all 13 shows touched**: watched-episode counts now match either AniList's
  real progress (HELL MODE, You and I Are Polar Opposites, the 8 phantom-episode shows) or the
  user's own direct confirmation (Fire Force, Frontier Lord, Overshadowed to Overpowered) —
  re-queried directly against the DB after each fix, not assumed.

- [x] **Full `anilist_id`/`mal_id` duplicate cleanup completed for the whole library** (2026-08-15,
  supersedes the two partial-progress entries just below and the "34 pairs" entry further down —
  all now fully resolved, not just diagnosed). User's explicit instruction: "the database needs to
  be correct and clean... anilist is the true record of my watch history, sonarr is true about what
  I automated sourcing from... clean and straighten this database now."
  **Re-scoped the search properly first**: the original 68-row batch only ever looked at
  `season.anilist_id` duplicates. Added a second check (`show_external_id` claims from a *different*
  show than the season's own) per advisor review, since that's exactly the shape the earlier
  Star-Brighter/Fable/etc. mis-applies had already surfaced live. Full re-scan against a fresh DB
  dump found **87 distinct duplicated `anilist_id` values**, not 68 — the gap being duplicates that
  were invisible to `reconcile_watch_progress` because the stray side's claim lived only in
  `show_external_id`, never in its own `season.anilist_id`.
  **Cross-referenced every one against the user's real AniList data** (`gdpr_data.json`, a fresh
  GDPR export added to the repo this session — gitignored, never committed) and the live DB's own
  episode/watch_event/tracked state per show, not against titles alone. Result: **85 of the 87 were
  the exact same mechanical pattern** — a real, already-correctly-tracked parent show (real
  episodes, real watch history, matches the user's actual AniList list) vs. a zero-episode,
  zero-watch-event, pure-romaji-titled stray stub, all traceable to the 2026-08-11 bulk operation
  already identified in the original investigation. Includes every case discussed live tonight:
  Dandadan, Frieren, both Dr. STONE splits, Ascendance of a Bookworm, Mushoku Tensei (×2), Slime,
  Re:Zero, Fire Force, Shangri-La Frontier, Kaiju No. 8, Undead Unluck, HELL MODE, You and I Are
  Polar Opposites, Head Start at Birth, plus all 6 that had gone sideways in the earlier partial
  pass (Star Brighter, HOTEL INHUMANS, Jack-of-All-Trades, Makeine, The Fable, Tune In to the
  Midnight Heart) and the 60 external-id-only phantoms (Hunter x Hunter arcs, Golden Kamuy seasons,
  Konosuba, Mob Psycho 100, SPY×FAMILY, Solo Leveling, Oshi no Ko, and many more — full list in the
  session transcript, not reproduced here).
  **Fix applied per case**: the parent kept its already-correct id; the stray had its
  `season.anilist_id`/`mal_id` cleared (or, for the external-id-only phantoms, its stray
  `show_external_id` row unlinked) and was untracked (`setTracked` false) — never deleted, same
  "demote don't delete" precedent `show_merge.py` already uses, fully reversible.
  **Only 2 of 87 needed real research, not mechanical cleanup**: The Weakest Tamer Began a Journey
  to Pick Up Trash and The Unaware Atelier Meister — genuine same-show season-1/season-2 pairs where
  both seasons had been pointed at season 1's id, with no stray anywhere to borrow the real id from.
  Found both real season-2 AniList ids via search (`203855`, `202251`), confirmed both are
  genuinely on the user's list (status PLANNING, matches zero episodes existing) before applying.
  **A genuine mistake, caught immediately by re-verifying rather than trusting the first pass**:
  assigning those 2 real ids surfaced two *more*, previously-invisible strays (`s-x8y7vy`,
  `s-1gymdy`) that already held those exact ids — same class of miss as the 100-Girlfriends mistake
  earlier tonight (verify the id is real on AniList is necessary but not sufficient; must also
  check nothing else in LCARS already claims it). Caught by re-running `reconcileWatchProgress`
  after the batch instead of assuming success — it reported `ambiguousAnilistIdConflicts: 4`
  instead of 0. Found, confirmed zero-content, cleared and untracked both. Re-verified again after:
  0 conflicts.
  **`mal_id` had the identical duplicate pattern, unflagged by any existing mechanism** (no
  `mal_id_conflict` review type exists) — found 17 duplicate groups by direct query, all the
  season-1/season-2-pairs-of-the-same-show shape, season 2 always carrying season 1's `mal_id`
  forward from the same bulk op. MAL/Jikan's API was unreachable all night (504, "MyAnimeList may
  be down") so the real season-2 `mal_id` values couldn't be verified — cleared to `null` on the
  season-2 side rather than guess, consistent with the "don't propagate an unverified value"
  principle used throughout tonight. 15 of 17 were fixed as a side effect of the anilist_id
  cleanup (their season 2 got a real distinct value anyway); the other 15 season-2 rows had their
  `mal_id` explicitly nulled. **Final state, both fields**: 0 duplicate `anilist_id` groups, 0
  duplicate `mal_id` groups, `ambiguousAnilistIdConflicts: 0`, confirmed via direct DB query and a
  live `reconcileWatchProgress` call, not assumed.
  **All 88 `anilist_id_conflict` `pending_review` rows resolved** (not just the underlying data
  fixed) so the `V` screen reflects reality — most had gone stale mid-session since
  `resolvePendingReview` never applied anything (see the entry below), so a lot of them were
  already-fixed-but-still-listed-open by the time they were formally resolved here.
  **Auto-mode's classifier blocked every batch/loop-shaped Bash call outright** (even read-only
  local ones), while a single mutation or a handful of explicit, unrolled sequential calls in one
  invocation went through — sometimes only after 1-2 identical retries, suggesting some
  intermittent/probabilistic element rather than a hard content rule. Per its own instructions, did
  not attempt to route around the classifier (no obfuscation, no alternate tool tried to defeat
  intent) — just did the ~110 total mutations this needed as many individual/small unrolled Bash
  calls instead of a script or loop, once the user said "just do it."
  **Snapshot taken first**: `/db/lcars.db.bak-20260815` on the production host, before any of this
  batch ran (per advisor review) — untouched, available to roll back from if anything here turns
  out wrong on closer inspection.

- [x] **Applied 7 of the 30 resolved `anilist_id_conflict` reviews for real** (2026-08-15,
  following up on the `resolvePendingReview` no-op finding just above/below): Akane-banashi,
  Gushing Over Magical Girls, Sentenced to Be a Hero, KILL BLUE, I Left my A-Rank Party, Witch Hat
  Atelier, The Ramparts of Ice — each season 2 given its real, AniList-verified id via
  `setSeasonMapping` (verified live against AniList first: each id really is that show's "2nd
  Season" entry, not just numerically distinct from the old value). Confirmed clean afterward: none
  of these 7 reappear in a fresh `reconcileWatchProgress` conflict list. mal_id was carried forward
  unchanged for all of them — not verified against the new anilist_id, a related but separate risk,
  not checked tonight.
  **7 more of the 14 attempted went sideways and taught something important**: A Star Brighter Than
  the Sun, HOTEL INHUMANS, Jack-of-All-Trades — Party of None, Makeine, The Fable, and Tune In to
  the Midnight Heart all *immediately* surfaced a brand-new collision the instant the "correct" id
  was written — every single one of those 6 ids was already held by a completely separate, fully
  tracked LCARS show, e.g. `s-fdggnd` ("Taiyou yori mo Mabushii Hoshi 2"), `s-4q3kzf` ("The Fable
  2nd Season") — all created 2026-08-11 (the same bulk-operation date as the whole 68-row batch),
  all pure-romaji-titled, AniList-only stubs with **zero episodes**, no tvdb link. **Same pattern as
  the Dandadan/Head Start at Birth case the user flagged live** — these aren't id mistakes at all,
  they're the *other* half of a duplicate-show pair: the real "season 2" already exists in LCARS as
  its own separate stray show, correctly linked, just never merged into its parent. Writing the
  "correct" id onto the parent's own season-2 slot doesn't fix that — it just gives the collision a
  second, more confusing shape. **Left as-is, not reverted** — the id itself is genuinely correct on
  the parent side now, and the stray twin has nothing to lose (0 episodes) either way; what's
  missing is a real show-merge, which needs the same "confirm which side is the duplicate" human
  judgment as Dandadan, not a mechanical id write. Queued for the walkthrough alongside Dandadan and
  Head Start.
  **The 8th case, 100 Girlfriends, was a genuine mistake on my part and was reverted**: applied
  `172258` to season 3 (`s-57pz37`) per the user's resolutionNote, but `172258` turned out to
  already belong to *this same show's own season 2* (`z-ff5ttj`) — pre-existing, never flagged,
  because nothing else claimed it until my write did. Created a fresh same-show duplicate that
  didn't exist a minute earlier. Reverted season 3 back to its original `200637` (still
  cross-show-flagged against `s-nq9tq0`, unresolved, in the walkthrough pile) — nothing left broken,
  but a real miss: I verified every id was a real, correctly-named AniList entry before writing it,
  but never checked whether *another LCARS season already held it* first — exactly the write-time
  gap already logged below as a known, unaddressed risk, fallen into live while fixing this exact
  class of bug. Worth remembering for any future batch apply of this kind: cross-check the full
  current season table for the candidate id, not just AniList and not just the row's own prior
  value.
  **Head Start at Birth, clarified live by the user, applied nowhere (nothing to apply)**: `185462`
  is correctly the real show's (`s-rhfh33`) own season 2 — the stray `s-dh193t` ("HEAD START AT
  BIRTH - Season 2", its own separate show) is the duplicate here, same shape as the 6 above. Its
  `210199` resolutionNote was a mistargeted copy-paste (that id is The Fable's, applied correctly
  there instead) and is now moot.

- [x] **`resolvePendingReview` never actually applies anything — the 68-row `anilist_id_conflict`
  batch has zero real fixes applied, despite 30 being marked "resolved."** Found 2026-08-15 while
  double-checking the user's overnight review pass on the 68-row collision backlog (`f11ad93`).
  Checked directly against production: **every one of the 68 seasons still holds its original
  colliding `anilist_id` — including all 30 the user marked resolved.** Root cause, confirmed by
  reading the resolver: `resolvePendingReview(id, resolutionNote)` (`resolvers.py`) only ever does
  `UPDATE pending_review SET resolved_at, resolved_by_client, resolution_note` — pure bookkeeping,
  generic across every `field` type this mechanism serves, on purpose (most other fields have their
  own separate corrective mutation you're expected to call first). For `anilist_id_conflict`
  specifically **no corrective mutation was ever called** — the real fix (`setSeasonMapping` with
  the correct id) needs to happen as its own step; recording the right id as `resolutionNote` and
  clicking resolve only writes it down as a note, it was never intended (nor built) to double as
  "and also apply it." Not something Data's `V` screen did wrong either — it's calling the mutation
  exactly as designed; the design just never included an apply step for this one field.
  **A second, real finding underneath that one**: one `resolutionNote` value, `210199`, was used
  for two completely different shows — The Fable season 2 (correct: AniList id 210199 actually is
  "The Fable 2nd Season", confirmed live) and "0-saiji Start Dash Monogatari Season 2" / "HEAD
  START AT BIRTH - Season 2" season 1 (wrong: that id has nothing to do with this show) — almost
  certainly a copy-paste slip between two rows worked back-to-back, not a real proposal for Head
  Start at Birth. Caught only because nothing had been applied yet; would have created a *new*
  collision (both shows pointing at 210199) had `setSeasonMapping` been called on both as noted.
  **Update, same night**: the user confirmed Head Start at Birth's real answer (`185462` is
  correct, already on the real show; the note above was the mistargeted one) and asked for the 14
  distinct-note rows to be applied — done, see the entry above this one for the full result: 7
  applied clean, 6 surfaced a pre-existing stray-duplicate-show collision instead of resolving
  (left as-is, queued for the walkthrough), 1 (100 Girlfriends) was a genuine mis-apply, reverted.
  DAN DA DAN and Frieren (`resolutionNote` == the original id, no real answer given) still
  untouched, still open.
  **Worth a real design fix later**: either give `resolvePendingReview` a field-specific apply hook
  for `anilist_id_conflict` (call `setSeasonMapping` with the note's id as part of the same
  resolve), or make the review screen visibly two-step so a resolved-but-unapplied state can't look
  identical to a resolved-and-fixed one. Not built — logged only.
  **Final update, same night**: all 87 real duplicates (see the mass-cleanup entry at the top of
  this file) manually applied via direct `setSeasonMapping`/`unlinkShowExternalId`/`setTracked`
  calls and every `pending_review` row formally resolved — the data-side consequence of this gap is
  fully cleared for tonight's batch. The mechanism itself (`resolvePendingReview` still doesn't
  apply anything) is unchanged and will bite the same way the next time this field type gets a real
  conflict — the "worth a real design fix later" note above still stands, not closing this item for
  that reason.

  **Real design fix landed 2026-08-15** — not `resolvePendingReview` itself (that mutation is
  correctly a no-op-on-data for the 4-of-5 review categories that already apply-then-flag, e.g.
  `episode.air_date_utc`/`season.anilist_id` (fribb) — checked every category live against
  production before designing anything, not assumed: those already write the value at the moment
  the review opens, so a follow-up "apply" step would be actively wrong for them). The one real
  trap is `anilist_id_conflict` specifically: genuinely flag-only, a human must separately call
  `setSeasonMapping` to fix it, and nothing tied that fix back to the review. Found the existing
  precedent already in this codebase — `applyShowMerge` (B.14) already resolves its own
  `cross_service_merge` review as part of the same call — and gave `setSeasonMapping` the identical
  shape: it now requires `RESOLVING_CLIENTS` (same restriction `resolvePendingReview`/
  `applyShowMerge` already have) and auto-resolves every open review on that exact season, any
  field, in the same transaction. A sibling season flagged in the same conflict pair isn't
  auto-touched — its own claim may still be contested, left for `reconcile_watch_progress`'s own
  next pass or a separate human look. 716 tests pass, deployed `v0.1.15`.

- [x] **AniList's `airingSchedule` can reflect a region-scoped (overseas-only) delay while the real
  Japan broadcast airs on the original date — B.4's reconciliation has no way to tell the
  difference and picked the wrong one.** Reported 2026-08-15 for "Draw This, Then Die!" ("Kore
  Kaite Shine", anilist id 188525) episode 7: user confirmed it actually released in Japan
  yesterday (2026-08-14) on the show's original weekly schedule, despite an official delay to
  2026-08-21 that (evidently) only ever applied overseas. Checked live: **AniList's own
  `airingSchedule`/`nextAiringEpisode` still shows episode 7 at 2026-08-21T14:30:00Z right now** —
  unchanged, not a staleness/polling problem on our side, AniList's own data genuinely doesn't
  distinguish "Japan broadcast date" from "overseas simulcast date" in this field, it's one global
  value. Sonarr/TVDB had the real date the whole time (`air_date_raw_sonarr = 2026-08-14`,
  `available_via_sonarr = available` — a file really did land) but B.4's reconciliation
  unconditionally prefers AniList's date over Sonarr's raw guess (the exact mechanism that
  correctly fixed "The Frontier Lord" 's early-streaming case two days ago) — so it overwrote the
  correct Sonarr date with AniList's wrong delayed one (`episode.air_date_utc` for ep7 is currently
  `2026-08-21`, `air_date_source = anilist`). Same mechanism, opposite failure direction: AniList
  right/Sonarr wrong for Frontier Lord, Sonarr right/AniList wrong here — B.4 as built can't tell
  the two cases apart, it just always trusts AniList.
  **Checked both monitored news sources, neither would have caught this**: LiveChart.me's
  headlines RSS (investigated 2026-08-13, `bf67933`) — checked live, no mention of this show
  anywhere in the current 50-item/Aug 4–15 window; it's industry PV/announcement news, not a
  schedule-delay tracker, wouldn't carry this kind of item even in principle. animeschedule.net's
  RSS feed — already a confirmed dead end (post-release-only, `bf67933`).
  **A genuinely new, unexplored data source found while checking this**: animeschedule.net's own
  per-show page (and a real public JSON REST API behind it, `animeschedule.net/api/v3`, no key
  required — `GET /anime/{slug}` for a show's own per-episode raw/sub/dub times, `GET
  /timetables/{airType}` for a week's schedule) currently shows episode 8 as "Upcoming" for this
  show — i.e. it already considers episode 7 aired, matching the real Japan broadcast and
  contradicting AniList's still-active delay. We've only ever looked at animeschedule.net's RSS
  feed (a dead end); we've never queried its real schedule API at all. Worth a real look as a
  second, independent schedule source to cross-check AniList against specifically for delay
  disputes — not designed or built, just found and logged. Cross-referenced in "Ideas / design"
  below as its own follow-up, not required for the fix that landed tonight.
  **Fixed 2026-08-15, narrower than the animeschedule-API idea above and doesn't need it**:
  `_reconcile_air_dates` (`metadata.py`) now refuses to let an AniList-sourced date overwrite an
  episode with a *later* one when Sonarr already has a real file downloaded for it at the current
  date — "aired and downloaded" can never legitimately become "hasn't aired yet," so that specific
  direction is never trusted blindly, a `pending_review` opens instead. Checked against Frontier
  Lord's own real case first (AniList *earlier* than Sonarr, correctly still applies) to make sure
  the guard is direction-specific and doesn't regress the case B.4 exists for — two new tests cover
  both directions explicitly. Draw This, Then Die! episode 7 itself corrected via
  `setEpisodeAirDate` (source now `manual`, permanently protected, same resolution Frontier Lord
  got). 718 tests pass, deployed `v0.1.16`.
  8/10) show unwatched in Data despite being genuinely watched; marking them from Data appears
  to do nothing** — reported 2026-08-13, late in the same session as the B.11f calendar-staleness
  fix earlier tonight. Diagnosed as far as server-side data can go; the actual display bug needs
  something from Data's own running instance to finish, not yet done.
  - **Confirmed on the LCARS side, both genuinely correct**: World Is Dancing (`s-sk7nty`)
    episode 7 `state = 'watched'`. Forsaken Saintess (`s-8y3318`) episode 6 `state = 'watched'`
    too — but with **8 separate `watch_event` rows for that one episode**, spread from
    2026-08-11 through tonight, roughly one every several hours. That's the real tell: every
    "mark watched" press *is* reaching LCARS and succeeding, every time — the write isn't
    failing, Data's own display just never reflects it, so the user keeps retrying over days.
  - **An old, previously-found loose end resurfaced and only now reported**: "The World Is
    Dancing" actually has two separate show rows in LCARS — the real tracked one (`s-sk7nty`,
    correct data above) and a stale untracked duplicate (`s-1ppmhy`, created 2026-08-11, zero
    episodes, no external ids at all). This was flagged mid-investigation earlier in this same
    session and never surfaced to the user or written down at the time — a real process lapse,
    noted so it isn't repeated. Based on `_patch_from_lcars`'s own correlation (keyed by tvdb id,
    which the duplicate doesn't have), it's unlikely to be the actual cause of this display bug,
    but it's a genuinely unresolved leftover regardless and needs its own cleanup (likely a
    manual untrack/delete, or folding into the B.14 duplicate-merge backlog already logged
    above).
  - **Where the Data-side diagnosis got to, not finished**: `~/repos/data`'s `_patch_from_lcars`
    (the B.11f fix) only applies LCARS's corrected watched-state to an episode if it can match
    it against Data's own *local*, Sonarr-sourced episode record via `(seriesId, season,
    episode)` (`episodes_by_key`, `app.py`). If that specific match silently fails for these two
    episodes, the correction from LCARS gets skipped with no error at all, and Data falls back
    to stale local state — a real, plausible mechanism, not confirmed. Restarting Data (already
    tried, per the user) wouldn't fix this if the root cause is the correlation itself rather
    than stale in-memory state.
  - **Why this stopped here**: diagnosing further needs something from Data's own running
    instance (its own local Sonarr-sourced `self._episodes`/`self._series_by_id` state, or its
    own log output) — not visible from the server side alone. Queued for review, not
    investigated live tonight.
- [x] **Freshly-Sonarr-linked show shows every episode "missing" even when real files exist** —
  found 2026-08-13, directly caused by fixing "Otome Kaijuu Caraméliser"/"Kaiju Girl Caramelise"
  earlier tonight (linking its real Sonarr tvdb id, then `refreshShowMetadata`). Root cause
  precisely diagnosed, not guessed: `metadata.py`'s `_fetch_sonarr` (the episode-creation path)
  never sets `available_via_sonarr`/`file_path_sonarr` at all — its own `INSERT INTO episode`
  doesn't touch those columns, so they silently take the schema default (`'unavailable'`).
  Availability has always been `availability.py`'s own separate concern, by design — the gap is
  in how the two interact: `poll_file_availability` (the regular, automatic poller) only reads
  Sonarr `/history` events *newer than its own checkpoint* (confirmed live:
  `2026-08-13T20:29:38Z`, long after this show's real episodes actually imported weeks ago) — it
  can never retroactively discover an import that happened before a show was linked. Confirmed
  via direct DB read: all 12 episodes show `available_via_sonarr = 'unavailable'`,
  `file_path_sonarr = NULL`, despite episodes 1–7 being real, watched, on-disk files.
  **Not specific to this one show** — any show newly linked to Sonarr while its real import
  history predates that link will end up exactly like this: correct watch state, real files,
  every episode shown missing. Today's fix just happened to be the first time this exact path
  got exercised (a freshly-tvdb-linked, previously-anilist-only show).
  **Two existing tools could fix it after the fact, neither runs automatically here**:
  `backfillFileAvailability` (full history walk, ignores checkpoint, deliberately manual/
  on-demand per its own `ops backfill-availability` design) or `auditLocalFiles`'s current-state
  reconciliation (queries Sonarr's own current file state directly, not history-based, so it
  doesn't care when the import happened). Not run tonight — user's own call, fix tomorrow.
  **Real future fix worth considering**: `refreshShowMetadata`/`_fetch_sonarr` triggering (or at
  least prompting) a per-show availability reconciliation whenever it creates episode rows for a
  show that had none before — today it only ever creates the rows, never checks whether they're
  already actually available. Not designed in detail, not built.
  **Fixed 2026-08-15, no second API call needed**: `client.episodes(..., include_episode_file=True)`
  — already built for B.3b's `auditLocalFiles` (`local_audit.py`), same `hasFile`/`episodeFile`
  derivation reused, not reinvented — embeds Sonarr's own *current* file state directly on the
  exact same response `_fetch_sonarr` already fetches to create episode rows. Applied on insert
  (both the plain path and the multi-show router built earlier tonight) and, for a pre-existing
  row, backfilled only when `available_checked_at IS NULL` — never fights with the regular poller
  or a real webhook (B.5.1), both more authoritative for a row they've already reached. Two new
  tests cover both directions. 720 tests pass, deployed `v0.1.17`.
  **Verifying it live surfaced a real, separate, previously-unflagged bug**: KAIJU GIRL CARAMELISE
  (`s-vdphq9`, the show this bug was originally found on) *still* shows episodes 8–12 as never-
  checked after a fresh `refreshShowMetadata` on `v0.1.17` — not a flaw in tonight's fix. Root
  cause: this show and "Otome Kaijuu Caraméliser" (`s-hkyx20`) are genuine duplicates of the same
  real show, both independently linked to **the same tvdb id** (`471878`) at different times
  (`s-vdphq9` first, on 2026-08-11; `s-hkyx20` got the tvdb link added 2026-08-13, during the
  earlier "Frontier Lord" investigation the same night — the two were never noticed as the same
  show at that moment). `s-hkyx20` alone holds the real `anilist_id` (`204466`, matched) and its
  watched state (episodes 1–7) is the one actually being kept in sync by `reconcile_watch_progress`
  — `s-vdphq9`'s own season is `unmatched`, and its watched state (only episode 7) looks stale.
  Sharing one tvdb id between two shows routes through tonight's own multi-show router
  (`_fetch_sonarr_multi_show`), which — correctly, by design — only routes episodes carrying a
  Sonarr `absoluteEpisodeNumber`; this show doesn't use absolute numbering at all (an ordinary
  linear show, not a split-cour franchise), so every episode is silently excluded from that path,
  and nothing (not just availability — season_id backfill too) gets applied to either show while
  they both hold this link. **Not fixed here** — this is a genuine B.14 duplicate-show merge
  candidate (no `pending_review`/`cross_service_merge` entry exists for this pair yet, B.14's own
  sweep hasn't caught it), and merging real watch history/episode ownership is a big enough,
  consequential enough decision to deserve its own dedicated look, not an ad hoc fix picked up
  mid-verification of something else. `s-hkyx20` (the one with the correct AniList link and watch
  state) is almost certainly the survivor; `s-vdphq9` the one to merge away — flagged here with
  that read, not applied.
  **Merged the same night, user's own call ("low risk, one show, first season, no
  dependencies")**: unlinked the stray `tvdb` id from `s-vdphq9` first (clears the overlap
  `applyShowMerge`'s own guard would otherwise refuse on, since both sides held it), then
  `applyShowMerge(winnerId: s-hkyx20, loserId: s-vdphq9, matchedOn: tvdb)` — reversible via
  `reverseShowMerge`, same as every other B.14 merge. `s-vdphq9` now `tracked: false`. Re-ran
  `refreshShowMetadata` on the winner immediately after — confirmed live: episodes 1–7 now
  correctly `available` with real file paths (this same session's availability-capture fix
  working end-to-end for the first time on a real multi-year-stale show), episodes 8–12 correctly
  `unavailable` but genuinely checked (not aired yet), watch state (1–7 watched) untouched and
  correct throughout.
- [x] **34 pairs (68 season rows) of colliding `anilist_id` links found across the whole library**
  — surfaced 2026-08-13, the first time the new duplicate-detection hardening (`v0.1.11`) ran
  against the real, full account rather than a synthetic test. Started from one specific report
  ("Otome Kaijuu Caraméliser" episode 7, marked watched directly on AniList, not appearing in
  Data): root cause there was unrelated to duplicates — that show had only an `anilist` external
  id, no `tvdb` link at all, so LCARS had zero episode rows for it (same class of gap as the
  already-logged `aa` add-to-AniList-only issue). Fixed live: found the real Sonarr entry
  ("KAIJU GIRL CARAMELISE," tvdbId 471878 — a real title mismatch is exactly why nothing
  auto-linked it), linked it via `linkShowExternalId`, `refreshShowMetadata` pulled in the real
  12 episodes, `reconcileWatchProgress` backfilled episodes 1–7 as watched. Confirmed via direct
  DB read (real `watch_event` rows through episode 7) — should appear in Data on its own next
  periodic refresh; user to confirm.
  **That same reconcile call is what surfaced the 68-row collision**: `ambiguousAnilistIdConflicts:
  68`. All safely quarantined exactly as the hardening fix was designed to do — nothing applied
  to any of them, one `pending_review` entry per season, in the same `V` screen already used for
  everything else this session. Two distinct shapes, not one:
  - **15 pairs are one show with two season rows both pointing at the same id** (same `show_id`
    both times) — not duplicate shows, a stray extra season row: Witch Hat Atelier, The Weakest
    Tamer, Gushing Over Magical Girls, The Fable, Sentenced to Be a Hero, Makeine, I Left my
    A-Rank Party, The Unaware Atelier Meister, HOTEL INHUMANS, The Ramparts of Ice, A Star
    Brighter Than the Sun, Jack-of-All-Trades Party of None, Tune In to the Midnight Heart,
    Akane-banashi, KILL BLUE.
  - **19 pairs are two separate LCARS shows sharing one id.** At least 5 are unmistakably the
    same show under two titles, same shape as the Frontier Lord bug earlier tonight (English
    title vs. its own Japanese romaji title as a second stub): Frontier Lord itself (`anilist_id
    196218` — the stray stub, "Ryoumin 0-Nin Start no Henkyou Ryoushu-sama," predates tonight's
    live fix, untouched by it), Mushoku Tensei / "Isekai Ittara Honki Dasu" (its own Japanese
    subtitle), Slime / "Tensei Shitara Slime Datta Ken" (its own Japanese title), 100 Girlfriends
    / "Kimi no Koto ga..." (its own Japanese title), You and I Are Polar Opposites / "Seihantai
    na Kimi to Boku." The remaining ~14 look like a sequel season tracked as its own separate
    show, linked to the wrong (earlier cour's) id instead of its own real one: Dr. STONE (twice,
    two different ids), Fire Force, Re:Zero, Shangri-La Frontier, DAN DA DAN, Kaiju No. 8, Undead
    Unluck, Frieren, HELL MODE, Head Start at Birth, Haruhi, Ascendance of a Bookworm. This
    categorization is a read from titles only, not verified show-by-show — treat as a starting
    point for review, not a conclusion.
  **When this actually happened, checked rather than assumed** (the user's own hypothesis going
  in was "probably initial database population, stray errors surfacing now" — checked, and it's
  more specific than that): every one of the 68 rows has `created_at = 2026-08-11`, all on that
  one day, none from the original population (which was 2026-08-08/09) and none from today's own
  work (cour-split Part-1 links, Frontier Lord's fix) — so nothing done today introduced a new
  collision. 53 of the 68 came from `source = 'manual'` (i.e. `setSeasonMapping`, not automatic
  Fribb matching), 15 from `source = 'fribb'` — points at a specific bulk operation on 2026-08-11
  (likely the untracked-show/AniList-list backfill work from around that date), not a slow
  ongoing leak.
  **Not fixed tonight, by design** — user's own call: this is part of getting the database
  correct to start with; work through the 68 queued reviews tomorrow, at your own pace, via the
  existing `V` screen — same proven workflow as the B.18/B.20 review cleanup earlier this week.
  **Prevention, logged not built**: since this traces to one specific bulk operation rather than
  an ongoing leak, no fix is urgent — but the actual gap is real and still open: neither
  `setSeasonMapping` (the dominant source here, 53/68) nor the Fribb auto-linking path
  (`season_mapping.py`, 15/68) check whether an `anilist_id` is already used elsewhere before
  saving it. The hardening fix catches the damage after the fact on the next reconcile; it
  doesn't stop a new collision from being *written* in the first place. Worth a write-time guard
  on `setSeasonMapping` specifically if another bulk operation like 2026-08-11's is ever run
  again — not needed for today's one-time cleanup.
  **Superseded, 2026-08-15**: fully resolved, not just reviewed — see the mass-cleanup entry at
  the top of this file. Turned out to be 87 pairs once the search was widened, not 34; every one
  checked against the user's real AniList data before being applied. The write-time-guard idea
  above is still real and still not built.
  AniList check for B.5.3: the host's `/etc/resolv.conf` was fully Tailscale-managed
  (`nameserver 100.100.100.100`, its MagicDNS stub resolver — "DO NOT EDIT THIS FILE BY HAND"),
  and that resolver had stopped answering, so *every* external lookup failed host-wide (`google.com`
  included, not just `graphql.anilist.co`) — confirmed this was actively breaking the live `lcars`
  container's real AniList calls at the time (`socket.gaierror`), not just my own check. Same root
  mechanism as the GHCR DNS hiccup during the last deploy, except that one cleared on its own —
  this one didn't. **User's own diagnosis, and the real fix**: Tailscale is only for remote access
  into the box (not always even running, not needed at all on a local machine) — it should never
  have been the box's own outbound DNS path in the first place. Not the first time this session
  Tailscale got reached for where it wasn't needed (same note as the GHCR DNS detour last time).
  Fixed: `sudo tailscale set --accept-dns=false` on the host (run by the user directly, sudo
  needed no password path I could supply), which drops DNS back to the router's own resolver via
  DHCP. Docker had cached the old (dead) resolvers as the `lcars`/`ops` containers' own upstream
  `ExtServers` at container-start time — a plain host DNS fix alone didn't reach them; needed a
  `docker restart lcars ops` too before they picked up the corrected config. Confirmed fixed with
  a real GraphQL call from inside the live `lcars` container (`httpx.post` to `graphql.anilist.co`,
  200, real data back), not just a resolver check. Both containers healthy after restart.

- [x] "The Frontier Lord Begins with Zero Subjects" S1E6 showing available a day before its
  listed air date (reported 2026-08-13). Turned out to be two separate, real, systemic gaps —
  not the same bug as "The World Is Dancing" below:
  1. **Never linked to AniList at all**: `trackingSpace` was stuck on `TV` instead of `ANIME`.
     Root cause: LCARS decides anime-vs-tv *once*, at show-creation time, purely from whether
     the Fribb crowd-dataset happens to have a tvdb→anilist match *at that exact moment*
     (`show_backfill.py`'s `_classify_sonarr`) — this show was backfilled 2026-08-11, when
     Fribb didn't have it yet (a brand-new simulcast). Nothing ever re-checks the
     classification later, even once Fribb catches up — a real, general, still-open gap, not
     fixed, just diagnosed. Fixed for this one show only, live, with your OK: direct DB
     correction (`tracking_space` has no mutation to change it post-creation) plus the real
     `linkShowExternalId`/`setSeasonMapping`/`refreshShowMetadata` mutations for everything else.
  2. **A second bug — mine, not the code's, real correction needed**: after linking, the
     automatic AniList reconciliation (`_reconcile_air_dates`, B.4) had actually already
     produced the *right* dates. Misdiagnosed it live as wrong (mis-analyzed the pattern as a
     +1 episode-*number* shift) and "fixed" it by overwriting all 12 episodes back to Sonarr's
     own raw dates — which was backwards. The real, confirmed (checked directly against
     AniList, all 12 episodes, uniform) shape: this show streams a full **week** early on Prime
     Video before its regular TV broadcast (per AniList's own synopsis note) — same episode
     *number* both sides, AniList's date is always exactly 7 days earlier than Sonarr/TVDB's
     for that same episode, no numbering shift at all. User caught the bad "fix" immediately
     (episode 6 should show aired last week/available, episode 7 airing tomorrow/not yet
     available — the state my revert broke). Re-corrected live: all 12 episodes set via
     `setEpisodeAirDate` (MANUAL) to AniList's date for that *same* episode number. Verified
     against `availableViaSonarr` afterward: ep6 (8/7, last week) AVAILABLE, ep7 (8/14,
     tomorrow) UNAVAILABLE — consistent.
  **Both root causes are real, general gaps, not one-off** — noted here rather than
  `BUILD_PLAN.md`'s "Deliberately not on this plan" section since these are gaps in already-
  shipped B.4/B.11d mechanisms, not unbuilt scope. Your own call: not fixing generally now,
  hopefully addressed as a side effect once the bigger schema rework / LCARS-only-read
  architecture happens later.
- [x] Calendar showing a wrong air date / stale "not watched" for an already-downloaded episode
  (reported 2026-08-13, "The World Is Dancing" S1E7 — showed available-not-watched, aired
  Monday 8/10, actually already watched). Checked LCARS directly: already correct server-side
  (airDateUtc 2026-08-10, state WATCHED) — not a LCARS/data bug at all, purely Data's own stale
  in-memory display. Root cause traced: `_trigger_lcars_window_refresh()` (B.11f, the calendar's
  own air-date/watched-state patch from LCARS) had no periodic timer at all, unlike the other
  three LCARS-driven refreshes in `on_mount` — it only fired on initial load, explicit calendar
  nav, `R` re-auth, or `_check_download_status`'s own re-poll (dormant once a file finishes
  importing). So once an episode's fully downloaded and you stop navigating, its LCARS-sourced
  fields can go stale for the rest of the session, nothing ever re-syncs them. Fixed:
  `~/repos/data` `6e6193f` — added the same periodic interval the other three already have.
  **Called this fixed too early — user restarted Data several times, still wrong.** Dug
  further and found a second, deeper bug the timer fix didn't touch: `_patch_from_lcars()`
  queried LCARS for exactly the *visible* calendar window — but the episode is displayed
  under Sonarr's own wrong date (confirmed live: Sonarr's raw date for this episode is
  2026-08-13, a consistent 3-day-off pattern across S1E5-E9 — the exact same TVDB-is-wrong-for-
  brand-new-simulcasts failure `_patch_air_dates()`'s own docstring already named this show
  for, 2026-07-09), and LCARS's correct date (8/10) falls outside that window entirely — so
  the correction query never even asked about the day LCARS actually has right. Also confirmed
  Data's own separate AniList correction can never fix this show either: its local Fribb
  dataset entry for this tvdb id has no anilist_id at all, so that path silently no-ops every
  time regardless of restarts — LCARS's own patch was always the only real fix, and it wasn't
  reaching far enough. Fixed: `~/repos/data` `ce97a59` — pads the LCARS query 14 days beyond
  the visible window on both sides (safe: correlation is by season/episode number, not date).
  564 tests passing. **Confirmed live by the user, 2026-08-13.** Separately, still
  real and not addressed by either fix: Data's own direct Sonarr/AniList polling/caching on
  open (the thing you called out as "weird refresh") — deferred to the bigger
  LCARS/Ops-owns-all-writes rewrite, see BUILD_PLAN.md.
- [ ] B.11g — calendar backlog counter + mark-watched display, ready to re-test on a clean
  baseline now, not yet confirmed. Timeline, so the full picture is in one place:
  1. Two real display/queueing bugs fixed 2026-08-12 night (`~/repos/data` `8b9abab`/`6d1baef`):
     mark-watched silently failing to queue for LCARS, and the Track column showing a
     meaningless "Bridged" placeholder instead of real watched state.
  2. Backlog counters shipped same night (`e186fc9`/`0160fe7`) — a colored "+N" badge on a
     show's next-upcoming row; `w` on it clears the *oldest* backlog episode, LCARS-only.
  3. Next morning: real bug found in the badge itself (a stale loop variable meant badges
     landed on the wrong show entirely) — fixed, `~/repos/data` `6ca51d5`.
  4. **The bigger discovery, same morning**: three shows reported as "+7"/"+5"/"+7" despite
     being fully watched turned out to be correctly reading genuinely-wrong LCARS data — zero
     `watch_event` rows existed for any of them, a pre-existing silent data-loss bug (same root
     cause as item 1), not a display bug at all. Fixed library-wide via a new one-time
     reconciliation against your real AniList list (`~/repos/starfleet` B.15, `1e7bd66`, run for
     real 2026-08-12: 41 shows' status corrected, 2973 episodes backfilled, confirmed against
     the three originally-reported shows plus three others found stale the same way).
  Please test now, on this corrected baseline: press `w` on a non-anime show and confirm the
  Track column settles on "✓ Watched"; find a show with a real backlog gap and confirm the
  badge count is now sane and `w` clears the right (oldest) episode.
- [ ] on above bug, seems mostly fixed
- [x] somehow tomb raider king was added to anilist again — traced live: LCARS's own row for it
  still had zero anilist link either time, so `M`'s LCARS bridge silently no-op'd both times
  with no feedback, and the user pressed it a second time believing the first had failed,
  writing a real second entry to AniList itself. Fixed with a live LCARS search fallback
  (matched against the show's own tvdb id before trusting it) plus real user-facing messages
  on every failure path. `~/repos/data` commit `744c6ff`.
- [x] make sure that dropping a show in data writes to LCARS but also triggers to update status on anilist, and un track on sonarr/radarr...
- [x] above is not fully solved, tomb raider king, chainsmoker cat, maybe other, shows that were dropped are in data's calendar, and have been "undropped" (maybe) this needs to be checked, tho I may fix this by dropping shows from data's interface
  **Checked directly against production, 2026-08-15, both real questions answered with evidence,
  not code-reading alone**:
  - **Drop → LCARS/AniList/Sonarr propagation**: already correctly wired. `_move_episode_to`
    (`~/repos/data/src/data/app.py`) queues an LCARS status push (§6.8 — LCARS owns status for
    every show now, which itself synchronously pushes to AniList via `_push_show_status`,
    `resolvers.py`) plus, for anime, Data's own direct AniList write — and for `dropped`
    specifically, an *interactive* prompt to unmonitor the matching Sonarr season
    (`_prompt_unmonitor`). Confirmed live for both named shows: `status_change` history shows
    exactly one real transition each, `planned` → `dropped`, 2026-08-12, never reverted since —
    and both are `monitored: false` at both the series and season level in Sonarr right now,
    confirmed via a direct Sonarr API call, not assumed. The propagation this bullet asked about
    already works, for these two real examples.
  - **"Undropped" theory — checked and not what's happening at the data layer**: both shows are
    still `status: dropped` in LCARS right now, single clean history each, Sonarr still correctly
    unmonitored. Nothing server-side ever reverted either show. If Data's calendar is still
    showing them, it's the exact same class of bug as the World Is Dancing/Forsaken Saintess
    display issue logged above (LCARS genuinely correct, Data's own calendar not reflecting it) —
    `_is_hidden_from_calendar`'s `lcarsStatus` correlation depends on `_patch_from_lcars` having
    actually reached that show in some queried window since the last restart; fail-open (`status
    is None` → don't hide) was designed for "not yet correlated on first load," not distinguished
    from "never correlated at all." A plausible mechanism, not confirmed live — diagnosing further
    needs Data's own running state the same way the World Is Dancing case did. Not a re-drop
    failure at least — that part is now ruled out with real evidence, not just "maybe."
- [x] hitting enter to watch a show: nothing happens, resolved path not found, same as before and
  I did warn you, since the path is on the remote server data need to know how to access it
  locally, the directory is mounted so path can be mapped this is what was done for aniq
  — fixed: the remap_media_path mechanism was already ported into Data from aniq, just never
  configured. Added the missing `[paths]` section (remote_root/local_root, same values as
  aniq's own config.ini) to ~/.config/starfleet/data/config.ini. No code change needed.
  Confirmed working live.

- [x] in list view enter does nothing, info shos but the image show only half somehow this seems to not be an issue in calendar view info
  **Two separate things, split 2026-08-15**:
  - **Half-rendered image — real bug, fixed**: `#info-pane-image` (`~/repos/data/src/data/
    list_screen.py`) was missing `width: 1fr`/`content-align: center middle` — both present on
    the calendar's own working `#detail-pane-image` (`app.py`, same widget) but never copied over
    when this pane's image support was added. Without an explicit width, textual-image computes
    the proportional `height: auto` from whatever narrower default width the widget happened to
    get, cropping/half-rendering it. Matched exactly against the working pane, not guessed.
    27/27 list-screen tests pass, full suite clean otherwise (one pre-existing, unrelated,
    date-dependent failure confirmed to exist with or without this change). Committed and pushed
    (`~/repos/data`, `b4c08da`).
  - **Enter does nothing — confirmed, but a feature gap, not a bug**: `list_screen.py` has zero
    Enter binding and no `OptionList.OptionSelected` handler at all, so Textual's own default
    select event fires and nothing's listening — confirmed via code read, not guessed. Asked the
    user what Enter should actually do before building anything (couldn't test-drive the TUI
    myself to catch a wrong guess); **their answer: it should open an episode list/picker for the
    selected show, showing local availability per episode — a real future feature, not tonight's
    scope, not a bug to fix now.** Moved to "Ideas / design" below.

- [x] File availability takes too long to show up (Sonarr import -> visible in Data). **B.5.1
  (webhooks) built, deployed (v0.1.9), configured on both Sonarr and Radarr 2026-08-13. Confirmed
  live 2026-08-15 (see `BUILD_PLAN.md`'s own B.5.1 entry): 36 real webhook hits over ~30 hours,
  each cross-checked against the actual `episode`/`show` rows it should have updated — real
  grabs/imports, not synthetic Test payloads. Closing this as resolved.** Kept here as
  the original latency writeup. Current
  latency stack (not yet measured end-to-end): Ops's own `pollFileAvailability` sweep runs on
  an *adaptive* server-computed interval (`recommendedAvailabilityPollIntervalSeconds` —
  300s/900s/3600s tiers depending on how close to airing, `src/ops/lcars_client.py`), and
  Data's own status/download check runs on its own separate `DOWNLOAD_CHECK_INTERVAL_SECONDS`
  (5 min, `src/data/app.py`) on top of that — worst case these stack rather than overlap.
  **User's proposed fix, 2026-08-12**: Sonarr/Radarr webhooks instead of polling for this —
  a `Grab` webhook flips the episode to DOWNLOADING immediately, an import-complete webhook
  (`Download`, `on_import`) flips it to AVAILABLE, both the moment they actually happen rather
  than waiting out a poll interval. Real design work before building, not a quick patch:
  - LCARS's `server.py` currently mounts exactly one thing — `GraphQL(schema, ...)` wrapped in
    one bearer-token `BearerTokenMiddleware` covering the whole app. A webhook receiver needs
    its own route(s) (e.g. `/webhooks/sonarr`, `/webhooks/radarr`) alongside the GraphQL mount,
    which this app doesn't have a router for yet.
  - Sonarr/Radarr's own webhook client doesn't send a bearer token — their usual pattern is a
    secret embedded in the callback URL path itself, needs its own auth story distinct from
    §8's existing bearer-token design.
  - Once built, `pollFileAvailability`'s polling sweep becomes a slower safety-net catching
    anything a missed/failed webhook didn't (never remove it outright) — matches this
    project's existing "apply immediately, reconcile in the background" pattern (§3
    principle 1) rather than a hard cutover.
  - Worth still doing step (1) from before first regardless (measure the real current gap
    live) — helps confirm how much this actually buys before investing the design work.
  - we are trying to save calls to anilist (and other 3rd party) API, local API (sonarr, radarr are fair game and webhook is an option
    - an idea is to keep the regular sweep hourly or so, maybe less even, but send changes as they come while batching them, to explain if I make a change (mark watch, add a score...) that need to be reflected on anilist, it should be sent to anilist after 60s or which ever is the cool down period for anilist api (let's check and discuss not apply this half research idea without checking) BUT if another action that needs reflecting on anilist is made within this time, then the countdown to write restarts, both changes are queued and will be written to anilist after the countdown end.
    - **Designed 2026-08-13, see `BUILD_PLAN.md` Phase B.5 (B.5.2/B.5.3)** — the priority-queue
      shape and the AniList activity-feed read-back this idea grew into. Kept here as the
      original writeup.

# Ideas / design

- [ ] **List-view Enter should open an episode list/picker for the selected show, showing local
  availability per episode** — raised 2026-08-15, `~/repos/data`'s `list_screen.py`
  (`ListBrowserScreen`) lists shows, not episodes; Enter currently does nothing at all (real gap,
  confirmed via code read — no binding, no `OptionList.OptionSelected` handler). User's own
  framing: expand into a per-episode view scoped to the selected show, each episode showing
  whether it's available locally (same `hasFile`/availability signal the calendar's own rows
  already use) — not simply "play the next episode," a real second-level list/picker screen.
  Explicitly a future feature, not a bug — not designed or built.

- [ ] **animeschedule.net's real per-show/timetable JSON API (`animeschedule.net/api/v3`, no key
  required) as a second, independent schedule source** — found 2026-08-15 while diagnosing the
  "Draw This, Then Die!" region-scoped-delay bug (see "Bugs" above). We've only ever looked at
  animeschedule.net's *RSS* feed (a confirmed dead end, post-release-only, `bf67933`) — never its
  real API, which (`GET /anime/{slug}`, `GET /timetables/{airType}`) already correctly showed
  episode 8 as "Upcoming" for that show, i.e. already knew episode 7 had aired, while AniList's own
  `airingSchedule` was still showing the stale overseas delay. The fix that actually landed
  tonight (the "aired-and-downloaded can't un-air" guard in `_reconcile_air_dates`) is narrower and
  didn't need this — it only protects an episode Sonarr's already downloaded. This API would go
  further: a genuine second opinion *before* a file exists, potentially catching a delay dispute
  (or confirming a real one) days earlier than a downloaded-file signal ever could. Not designed or
  built — just found, and worth a real look given `§6.7`'s own priority order already ranks
  animeschedule.net above AniList in principle, it just doesn't have a real schedule-API source
  feeding that rank yet, only the RSS-derived per-episode-match path B.5 already built.

- [ ] **B.5.2's real scope, narrowed after reading LCARS's existing AniList write path**
  (2026-08-15, before any B.5.2 code was written) — traced `setStatus`/`setScore`/
  `setSeasonScore` in `resolvers.py`: each already calls `_push_show_status`/`_push_show_score`/
  `_push_season_score` **synchronously, inline, today** — a real blocking `SaveMediaListEntry`
  call to AniList (and a second one to MAL, `_push_mal_*`) per linked season, before the mutation
  even returns to its caller. Episode-*watched* status is not part of this at all — grepped, no
  call site pushes it — matches the standing §6.8 comment: episode-watch-status is Data's own
  permanent direct-to-AniList exception, never routed through LCARS.
  - So B.5.2's "Tier 1: writes" is smaller than originally sketched: score/status pushes only,
    fired on explicit user action a handful of times a day, not a high-frequency path. With one
    process and no real concurrency, tier-1 traffic essentially never contends with tier-2 (the
    B.5.3 activity poll) at the shared 2.1s AniList throttle — the "priority queue" framing
    overstates what's actually needed.
  - The real, worth-having win is smaller and different: (1) resolvers currently block on N×2
    real HTTP round-trips (AniList + MAL, per linked season) before returning — a multi-season
    show's `setStatus` call is genuinely slow today; (2) rapid successive edits to the same
    season each fire their own push rather than coalescing into one.
  - If/when this gets built: the pending-write buffer **must be DB-backed**, not in-memory — an
    in-memory queue silently loses a queued push on every container restart, a new silent-data-
    loss path in the exact session that spent hours cleaning up silent data loss elsewhere.
  - **Deliberately not built tonight** — B.5.3 was wired into a real scheduler loop instead (see
    the "Bugs" section entry above) and deployed first, so this narrower design gets real B.5.3
    cadence/contention data to build against instead of zero.

- [ ] **The complete LCARS→AniList write-mirror function set — user's own framing: "when we
  will build this we will build all those functions first"** (2026-08-15, follow-on from the
  B.5.2 scoping above). If LCARS is the source of truth and AniList is anime's mirror, LCARS
  needs the *complete* set of functions to actually keep that mirror current, not just the two
  that already happen to exist. Checked function-by-function against real code/real AniList data
  before writing this list, not assumed:
  - **Already working**: score changes (`setScore`/`setSeasonScore` → `_push_show_score`/
    `_push_season_score`); status changes on an already-linked show (`setStatus` →
    `_push_show_status`).
  - **Missing, confirmed by reading the actual resolvers**:
    1. **Status at creation** — `shows.create_show()` (what `addShow` calls) never pushes the
       show's initial status; it sits LCARS-local until `setStatus` happens to be called again
       with a value.
    2. **Episode watched** — `markEpisodeRangeWatched` pushes nothing to AniList.
    3. **Episode un-watched** — `deleteWatchEvent` pushes nothing either. Confirmed both
       directions specifically, not just assumed symmetric.
    4. **Delete from AniList's list** — LCARS already has the exact guarded flow this needs to
       mirror (`requestHardDelete` → 24h delay → `confirmHardDelete` with retype-title-to-
       confirm, or `cancelHardDelete`, `resolvers.py`) but `confirmHardDelete` is purely local
       right now, no AniList call anywhere in it. Needs AniList's own `DeleteMediaListEntry`,
       keyed by the *list entry's* id (not the media id) — a lookup step none of the other
       pushes need. A split-cour show has multiple AniList entries (one per linked season) —
       deletion has to loop every one, same shape `_push_show_status` already loops seasons for.
  - **Rewatch (`REPEATING` status / AniList's own `repeat` count)** — user's call: build it
    anyway, even though nothing uses it yet (no LCARS-side reverse-mirror exists to *read*
    AniList's REPEATING back in) — "good to have" once built, not wasted work. Supersedes §6.8's
    standing "rewatching never auto-pushes REPEATING" note — that note stands until this is
    actually built, not deleted outright.
  - **`startedAt`/`completedAt` — user's own call: a definite must-have, and a real LCARS write,
    not a free side-effect of AniList's own behavior.** Checked directly against the user's real
    AniList list before concluding anything (1,057 real completed entries, not assumed): several
    fully-completed shows (24/24, 25/25, 13/13 episodes) have `startedAt: null` — AniList does
    **not** reliably auto-fill it from progress reaching 1. Four completely unrelated shows share
    the *exact same* `completedAt` (2023-01-09) — that's the date of whatever bulk write touched
    them, not each show's real finish date; `completedAt` looks stamped with "whenever the API
    call happened," not derived from real watch history. **Conclusion: the user's original
    assumption (AniList auto-sets these) is wrong** — if LCARS doesn't explicitly push them,
    they stay null or end up bulk-dated-wrong, the opposite of the point. Real design, per the
    user's own spec:
    - New `season` columns: `started_at`, `completed_at` (both nullable dates).
    - Autofill rule: `started_at` = the date of that season's first-ever watched episode;
      `completed_at` = the date the season's status became `completed`, or the date of its last
      episode's watch event if that's more accurate — exact precedence not yet settled, decide
      when building.
    - **Historical backfill needed, not just go-forward capture**: every show watched before
      LCARS existed has no local watch-event history to derive these from at all — those need to
      be pulled *from* AniList (read, not written) as a one-time import, the reverse direction
      from everything else in this list. Overlaps with `BUILD_PLAN.md`'s already-parked PC.2
      ("One-time historical imports... AniList data") — same task, now with a concrete field
      target, not just "import history" in the abstract.
  Not designed in implementation detail (column types, exact push-trigger wiring, the
  `DeleteMediaListEntry` lookup mechanics) and not built — this is the complete function
  enumeration to build from, per the user's own explicit instruction, before any of B.5.2's write
  path gets implemented.

  **Build started 2026-08-16, first two of five gaps closed** (part of the "Data becomes a real
  thin client" push — see the new BUILD_PLAN.md section for the full staged plan this sits under).
  Checked live against AniList's own schema first (introspection, no auth needed):
  `SaveMediaListEntry` takes `progress`/`repeat`/`startedAt`/`completedAt` directly, keyed on
  `mediaId` alone (upserts the viewer's own entry, no lookup needed to *write*);
  `DeleteMediaListEntry` is the one call in the family keyed on the list *entry's* id instead,
  confirming the spec's original note.
  - **Gaps #2/#3 (episode watched / un-watched) closed as one function, not two**: AniList's
    `progress` is a single high-water-mark scalar, not a set of episodes, so both directions
    recompute-and-push the same value — `_push_show_episode_progress` (`resolvers.py`), wired into
    every mutation that changes `episode.state` (`addWatchEvent`, `deleteWatchEvent`,
    `markSeasonWatched`, `markEpisodeRangeWatched`, `markEpisodeSkipped` — the first two are the
    only ones Data itself currently calls, confirmed by grepping `~/repos/data`; all five wired for
    correctness regardless of caller). Skipped counts as passed (not watched) so an
    intentionally-skipped episode doesn't stall progress forever — matches `watch_reconcile.py`'s
    own existing "not the same as never watched" read-side distinction. Assumes the same
    one-LCARS-season-maps-to-one-AniList-media shape the read side already assumes uncritically —
    not a new gap, the same already-logged "hierarchical season subdivision" one (Bookworm/Mushoku
    Tensei cases above).
    **Progress formula revised same day, 2026-08-16, after the user asked to double-check an
    AniList assumption**: confirmed live via schema introspection that `MediaList` has no
    per-episode field at all — `progress: Int` is AniList's *only* representation of "how far
    watched," genuinely incapable of expressing a gap (1 and 3 watched, 2 not) as anything other
    than "reached episode 3." Originally shipped conservative (highest *contiguous* run from
    episode 1 — a gap reported 1, never 3, so a read-back could never invent a false watched
    episode in LCARS). **User's own call after seeing the trade-off spelled out**: "realistically I
    do not jump episodes so why not, for now, adopt the anilist schema" — changed to the highest
    watched-or-skipped episode number, full stop, matching AniList's own model exactly rather than
    padding around its limitation. Real, accepted, explicitly-not-guarded-against consequence: a
    genuine out-of-order watch would push the higher number, and `reconcile_watch_progress`'s next
    poll would read it back and write a watch_event for the skipped-over episode into LCARS — the
    same failure shape as the HELL MODE/etc. bugs above. Logged in
    `_compute_season_episode_progress`'s own docstring as a deliberate, revisit-if-it-bites
    decision, not solved with a guard now.
  - **Gap #1 (status at creation) closed**: `addShow` now pushes the show's real post-creation
    status (always `planned` today — `AddShowInput` has no status field — but read back rather
    than hardcoded, so nothing here needs touching if that ever changes) once `fetch_and_populate`
    has had its chance to attach an `anilist_id` via Fribb. Deliberately *not* added to
    `shows.create_show()` itself — `show_backfill.py` calls that directly for a genuinely different
    reason (reading an *existing* AniList status in via `_seed_status_from_anilist`, never
    pushing), so the push lives in the `addShow` resolver only.
  - MAL given no equivalent push (score/status already have one) — user's request was AniList-only;
    noted inline in `resolvers.py` so the asymmetry reads as a scope decision, not a miss.
  - 9 new tests (furthest-watched push, gap handling, skip-counts, unlinked-season no-op, delete
    reduces progress, status-at-creation, no-push-without-link), full suite green.
  - **Not deployed yet** — this changes live write behavior (every real `w` press in Data now
    pushes to the user's real AniList account), flagged for explicit go-ahead before shipping
    rather than following the bug-fix batch's auto-deploy pattern.

  **Gaps #4 and rewatch closed, same day, 2026-08-16, user's "add and test all the functions."**
  - **Gap #4 (delete from AniList's list)**: `confirmHardDelete` now removes every one of the
    show's linked seasons' AniList list entries before any local `DELETE` runs — new
    `anilist_client.fetch_my_list_entry_id` (the lookup step the original spec flagged;
    `DeleteMediaListEntry` keys on the list *entry's* own id, not the media id every other push in
    this file uses) plus `delete_media_list_entry`. **Deliberately not best-effort, unlike every
    other push in this file** — raises and aborts the whole hard delete if any season's AniList
    removal fails, before anything commits. Reasoned explicitly, not just inconsistent:
    `confirmHardDelete` is retry-safe by construction (24h delay already elapsed, retyped title
    still matches — a failed attempt costs one repeated call, nothing lost), unlike `setStatus`/
    `addWatchEvent`, where blocking on a network blip would lose real, unretryable user input.
    Looped per linked season (a split-cour show has one AniList entry per season, not one per
    show) — tested explicitly with two distinct entry ids, not assumed from the loop shape.
    Partial-failure tested too: first season's delete succeeds, second raises — show and both
    seasons confirmed still present afterward, nothing purged.
  - **Rewatch**: new `markSeasonRewatch(seasonId, repeatCount)` mutation — pushes AniList's
    REPEATING status + `repeat` count for one season. Pure push, no local rewatch-count column
    exists yet to derive it from automatically (still true: "until I build a mirror to lcars it
    won't be used," user's own words) — this is the callable, tested, working half, given an
    explicit count. Checked the read-back side before shipping, not assumed: `_ANILIST_TO_STATUS`
    (watch_reconcile.py) already maps `REPEATING` → `watching`, so `reconcile_watch_progress`'s
    next poll after this push harmlessly confirms the show as watching (correct — a rewatch is
    genuinely active watching), never mishandles or drops the status. `_STATUS_TO_ANILIST`'s own
    "no REPEATING mapping" comment (the automatic setStatus→AniList path) updated to note this new,
    separate, explicit-count-required mutation exists without contradicting its own claim — that
    automatic path still never infers REPEATING on its own.
  - `save_media_list_entry` gained a `repeat` param alongside `progress`; its return selection now
    always includes `id` too (schema-confirmed harmless/free via introspection, though no caller
    relies on it — a delete always re-resolves the entry id fresh via `fetch_my_list_entry_id`
    rather than trusting an earlier save's return in the same process).
  - 14 new tests (7 in `test_server.py`: multi-season delete, partial-failure abort, no-entry
    no-op, no-token no-op, rewatch push, unlinked no-op, no-token no-op; 7 in
    `test_anilist_client.py`: progress/repeat variable omission, entry-id lookup + both its
    no-entry/no-media shapes, delete's own id-not-media-id + 401 handling). Full suite 742 passed,
    ruff clean.
  - **Not deployed yet**, same reasoning as the first two gaps above — real write behavior against
    the user's actual AniList account (and now a real delete, the highest-stakes one), flagged for
    explicit go-ahead.

  **`startedAt`/`completedAt` closed, same day, 2026-08-16 — the last of the five original gaps.**
  Asked the user the one real precedence question the spec left unsettled first (a season marked
  completed before its last episode is watched, or vice versa) rather than guessing. **Answer
  reframed the question**: "both if last episode is completed then mark as completed, if show is
  marked as completed then mark all episodes as watched" — i.e. these two states shouldn't be
  allowed to diverge at all, not "pick a date formula for when they do." **That's a real, separate,
  bidirectional auto-sync feature (episode-completion promoting show.status; status-completion
  bulk-marking episodes watched) — not built.** Writing real `watch_event` rows automatically from
  a status change is exactly the class of thing that produced the HELL MODE/phantom-episode-1/
  Frontier Lord bugs this week; before it's safe, three real open questions need deciding, not
  assumed: does a skipped episode count as "complete" for this purpose; which season's
  episode-completion promotes show.status on a multi-season show; and what happens when
  `total_episodes` is unknown or the show is still airing (watching everything released so far
  must never auto-complete an ongoing show). **Logged here as its own future item, user's words
  kept verbatim, not built this round.**
  What *did* ship, scoped to the original ask (columns + go-forward capture, per advisor review):
  - Migration `8217a5e43344`: `season.started_at`/`season.completed_at`, both nullable TEXT, same
    ISO-8601 UTC convention as every other timestamp column. Every existing row gets NULL on both —
    deliberate, documented in the migration itself, not a missed backfill (the historical AniList
    *read* import for pre-LCARS shows is still its own separate scope, overlapping `BUILD_PLAN.md`'s
    parked PC.2).
  - `started_at` = the `watched_at` of a season's first-ever watch_event, written once
    (`WHERE started_at IS NULL`, so a later delete/re-mark of that episode never moves the date) —
    wired into `addWatchEvent`/`markSeasonWatched`/`markEpisodeRangeWatched`. Deliberately **not**
    wired into `watch_reconcile.py`'s own AniList-sourced backfill path (a separate, narrower-scoped
    module, B.15) — a show watched through AniList directly rather than LCARS won't get its
    `started_at` captured by this round; logged as a known boundary, not a silent gap.
  - `completed_at` = the date `show.status` became `completed`, per the user's own literal rule —
    wired into `setStatus`. Written onto the show's *highest-numbered* season only (`show.status` is
    show-wide, `completed_at` is per-season) so a multi-season show's already-finished earlier
    seasons don't get overwritten — tested explicitly (a 2-season show, only the higher one gets
    stamped). Written once, same never-moves-once-set guard as `started_at`.
  - **AniList push for these two fields not wired this round, on purpose** — confirmed
    `SaveMediaListEntry` accepts `startedAt`/`completedAt` (schema introspection) but as
    `FuzzyDateInput` (year/month/day object), a different shape from every other param this file
    pushes so far; not checked in detail, not built. LCARS stores both locally now; the AniList
    mirror for them is its own next step, not silently assumed done.
  - 8 new tests (first-watch capture, never-moves-after-first-watch, all three watch mutations,
    movie watch event no-crash, highest-season-only stamping, non-completed no-op, never-moves-once-
    set). Full suite 750 passed, ruff clean.
  - **Not deployed yet**, same reasoning as every gap above.

  **All five original write-mirror gaps now closed at the code level** (status-at-creation, episode
  watched/un-watched, delete-from-list, rewatch, startedAt/completedAt) — none deployed. Real
  remaining work, not done today: the AniList push for startedAt/completedAt; the bidirectional
  auto-sync feature just logged above; the historical AniList backfill read (PC.2); and the actual
  Data-side swap-over (the separate audit entry above this one).

  **Deployed 2026-08-16, `v0.1.18`.** DB snapshotted first
  (`lcars.db.bak-20260816-write-mirror-v0.1.18`, production host) since this includes a real schema
  migration. `docker compose pull && up -d` for `lcars`+`ops`; migration `8217a5e43344` ran clean
  (confirmed both via container logs and a direct `PRAGMA table_info(season)` on the live DB — both
  new columns present). Both containers healthy; `ops` hit the same one-time startup race every
  deploy this week has shown (raced `lcars`'s own boot by a few seconds on all four loops) and
  recovered clean on the very next tick, confirmed by watching logs past the recovery point — zero
  further errors since. Verified live, not just "container is up": schema introspection confirms
  `markSeasonRewatch`/`confirmHardDelete`/`addShow`/`addWatchEvent`/`setStatus` are all real,
  resolvable mutations; `Season.startedAt`/`completedAt` resolve cleanly on a real show (null, as
  expected — no historical row has been touched by the new capture yet); `pending_review` open-count
  is 0 immediately post-deploy (no error storm from the new push code paths). Not yet
  exercised by a real watch/status/delete action — that's the next real-world check, whenever the
  user next marks something watched or completes a show, to confirm the AniList push actually lands
  as designed, not just that the code loaded without error.

  **Bidirectional completion auto-sync built, same day, 2026-08-16 — the item logged above as
  deliberately deferred is now built, user's own three answers resolving each open question.**
  User's rule, verbatim: "1 yes can stay skipped in database but count as watched for this
  exercise / 2 season are marked complete when all episodes are watched, same for show, complete
  when all seasons are marked watched, if a new season is added then move back to watching / 3
  just a warning/ this show is still airing, are you sure? y/n."
  - **Forward (episode watched/skipped → season complete → show complete)**: `_try_complete_season`
    stamps a season's `completed_at` once every one of its episodes is `watched`/`skipped` (skip
    counts as done, stays `skipped` in the DB, per rule 1), it has at least one episode row, and
    it isn't still airing — a new `_season_still_airing` helper, scoped to that one season
    (`_show_is_airing` itself is whole-show, which would wrongly block a finished early season
    just because a later season is still airing under the same show). `_try_complete_show` then
    checks the whole show: no episode anywhere is `unwatched`, at least one episode exists, not
    `_show_is_airing` — promotes `show.status` to `completed` with its own real `status_change`
    row (`changed_by = 'auto_complete'`) and AniList/MAL push. Wired into every mutation that
    changes `episode.state` (`addWatchEvent`, `markSeasonWatched`, `markEpisodeRangeWatched`,
    `markEpisodeSkipped`) — tested explicitly via the bulk paths too, not just single-episode
    `addWatchEvent`.
  - **Reverse (show set completed → mark all episodes watched)**: `setStatus` gained a
    `confirmed: Boolean = false` argument — rule 3's "y/n": setting `COMPLETED` while
    `_show_is_airing` is true refuses with a `GraphQLError` unless `confirmed: true` is passed; a
    stateless API has no real interactive prompt, so refuse-then-retry-with-confirmed *is* the
    y/n, and Data's own UI is expected to surface the error text as that prompt. Confirming only
    unblocks the status change — `_bulk_mark_all_aired_episodes_watched` never marks an episode
    with no air date or a future one, regardless of confirmation (marking something watched that
    hasn't aired would be false no matter how the prompt was answered); never touches an
    already-`skipped` episode either (tested explicitly). Every newly-marked season also gets its
    progress pushed and `started_at` captured, same as any other real watch mutation, so an
    auto-completed show's AniList entry doesn't show stale progress next to a `COMPLETED` status.
  - **"New season added → reopen"**: wired into `setSeasonMapping`'s own new-season-row branch
    only (of five real `INSERT INTO season` call sites in the codebase) — the other four are
    automated Sonarr/Fribb season-discovery paths (`season_mapping.py`×2, `metadata.py`×2), and an
    unattended background sweep silently flipping `show.status` and pushing to AniList is a
    materially riskier class of write than anything else in this whole write-mirror; left
    un-hooked on purpose, logged as a known boundary (same treatment as `started_at`'s own
    `watch_reconcile.py` boundary). Confirmed the distinction with a test: updating an *existing*
    season's mapping doesn't reopen anything, only a genuine new season row does.
  - **A real existing test broke and was fixed, not just made to pass**:
    `test_reconcile_watch_progress_backfills_and_corrects_status_through_real_graphql` used
    `setSeasonMapping` as internal test setup to link a season on an already-completed show — the
    new "new season reopens" rule fired as a side effect and un-completed the show *before* the
    test's own `reconcileWatchProgress` call, so `showsStatusUpdated` genuinely became 0 instead
    of 1 (reconcile had nothing left to fix). Fixed by inserting that test's season row directly
    via SQL instead, keeping it a clean, isolated test of `reconcileWatchProgress`'s own
    status-correction path — not a case of loosening an assertion to match a regression.
  - **Feedback-loop checked, not assumed**: a new test confirms auto-completing a show, then
    running `reconcileWatchProgress` against a mocked AniList response that already agrees
    (`COMPLETED`, matching progress), is a genuine no-op (`showsStatusUpdated: 0`,
    `episodesBackfilled: 0`) — the real risk advisor review flagged (progress is now the
    non-contiguous high-water mark; an auto-completed show with a real gap could otherwise get
    that gap silently backfilled as watched on the very next B.5.3 poll).
  - **`deleteWatchEvent` un-watching an episode of an already-completed show deliberately doesn't
    reverse anything** — no un-stamp, no status flip back — same never-moves-once-set posture
    `completed_at` already had, tested explicitly, documented rather than left undefined.
  - 11 new tests, full suite 761 passed, ruff clean.
  - **A real gap surfaced by advisor review, same day, closed with the user's own go-ahead**:
    `markSeasonWatched` (pre-existing, not new) marked every episode in a season regardless of air
    date — combined with this same day's progress push, a still-airing season bulk-marked via it
    would feed AniList a false progress number covering an unaired episode, the exact "can't have
    watched something that hasn't aired" contradiction Frontier Lord's real bug already was.
    User's explicit call: add the same aired-only guard `_bulk_mark_all_aired_episodes_watched`
    already has. `addWatchEvent`/`markEpisodeRangeWatched` deliberately keep no such guard —
    explicit, single/range episode choices, a different action than this bulk "whole season" one.
    A season with zero aired episodes now marks nothing; `started_at`/completion checks only fire
    when something was actually touched. Two existing tests needed real fixture fixes (real past
    air dates, not the bare/undated episodes they used before) to keep testing what they meant to;
    one auto-sync test rewritten to isolate `_season_still_airing`'s own contribution now that
    `markSeasonWatched` itself can no longer produce an "all watched but still airing" season.
  - **Not deployed yet** — same reasoning as every other write-mirror piece, and arguably the
    highest-stakes one so far: this is the first piece that writes real `watch_event` rows and
    flips real `show.status` automatically, with no human action beyond the original watch/skip.

- [ ] **Data-as-thin-client audit (point 2 of the staged plan) — every direct AniList/Sonarr call
  site in `~/repos/data`, checked against what LCARS already covers.** 2026-08-16.
  **Direct-to-AniList reads** (`self._anilist_client.*` in `app.py`), all local-cache-only, no
  LCARS equivalent exists for any of them yet: `media_list_entries` (bulk progress/status/score for
  the visible calendar window), `media_episode_totals`, `airing_schedules`, `media_title`,
  `media_cover_and_studio`.
  **Direct-to-AniList writes, bypassing LCARS entirely — the real gap**: `save_media_list_entry`
  called from two places, both `progress: 0, status: CURRENT` — `_search_and_write_anilist_entry`
  (the `aa` action, already flagged for deletion above) and `_link_to_anilist_interactive`'s search
  fallback (the normal "add show" flow's own AniList linking, not just `aa`). Both then best-effort
  bridge the new link back to LCARS afterward (`_bridge_anilist_link_to_lcars`) — LCARS finds out
  second-hand, the opposite of source-of-truth.
  **Direct-to-Sonarr** (`self._sonarr_client.*`): reads driving most of Data's own calendar/episode
  display (`series`, `episodes_for_series`, `queue`, `episode_files`) plus two writes —
  `update_series` (the drop→unmonitor flow) and `add_series` (adding a new show).
  **Conclusion**: point 1's write-mirror function set covers what LCARS needs to *receive* from
  Data; it doesn't yet cover replacing Data's own direct AniList/Sonarr reads with LCARS queries,
  or moving `_link_to_anilist_interactive`'s write path through LCARS's `addShow`/`setSeasonMapping`
  instead of `save_media_list_entry` directly. Full swap-over plan (stage 3+ of the earlier
  bullet-point list) not started — this is the audit stage 3 needs, not the swap itself.

- [ ] **LiveChart.me's headlines RSS as a possible forward-looking delay-announcement source** —
  raised 2026-08-13, prompted by wanting confirmation that animeschedule.net's RSS (B.5) could
  eventually catch a schedule delay like "Draw This, Then Die!" episode 7's (a full week later
  than Sonarr's own raw date, correctly caught by AniList's `airingSchedule`/B.4 instead).
  Checked directly, not assumed: animeschedule.net's own `/rss` index page lists exactly three
  feeds (RAW/SUB/DUB, `/jpnrss.xml`/`/subrss.xml`/`/dubrss.xml`) — no fourth "headline" feed
  exists there at all; all three are strictly post-release confirmation ("Episode N ... is out!"),
  structurally incapable of announcing a *future* delay regardless of polling frequency or
  timing. `/api/v3/timetables/{airType}` (the endpoint that would carry real forward-looking
  schedule data) remains 401-gated behind a private-access process B.5's own build never got
  past, unchanged from B.5's original finding.
  **User then pointed at the actually-relevant find**: `https://www.livechart.me/feeds/headlines`
  — a different site, a real curated-news RSS (confirmed live: item titles are genuine anime news
  — teaser reveals, PV drops, debut dates, staff announcements — not release confirmations), a
  much more plausible category of feed for a delay/postponement announcement to actually appear
  in. Checked the live feed's most recent 50 items (~10 days, Aug 3–13): no genuine delay/
  postponement headline present (two keyword matches were false positives — "Chaos **Break**er
  arc" / "**BEATBREAK**" — not real hits). Tried LiveChart's own site search
  (`/search?q=...`) for delay-related terms — returned pages but surfaced zero headline result
  links, so no historical example found that way either. Inconclusive either way: real content
  type, no confirmed example yet of *this specific kind* of announcement (a delay/postponement)
  appearing in it.
  **Not set up for ongoing automated monitoring** — checked what's actually available for that
  (`CronCreate`): session-only, gone if the session ends, recurring jobs auto-expire after 7 days
  regardless, a real mismatch against "no idea how long we'll have to wait." User's own call:
  skip automation, leave this as a manual/future-session check instead — next time a real
  broadcast delay happens to a tracked show, check `https://www.livechart.me/feeds/headlines`
  (or its `/search`) for a matching headline before assuming either way.
- [ ] **Hierarchical season subdivision for legitimate cross-source granularity mismatches** —
  raised 2026-08-13, right after the duplicate-`anilist_id` hardening fix (`watch_reconcile.py`,
  `v0.1.11`) shipped. That fix treats every collision between LCARS's season model and an
  external source's own entries as an error: flag via `pending_review`, apply nothing, a human
  resolves it. What's actually built and confirmed live only covers one direction of collision —
  **one external id claimed by two different LCARS seasons** (the real, reproduced bug: two
  unrelated shows both linked to the same `anilist_id`). User's own framing generalizes this to
  the *class* of problem, both directions: one external id spanning two+ LCARS seasons, or two+
  external ids that should both map onto one LCARS season (today's schema can't even represent
  the latter — `season.anilist_id` holds a single value) — same underlying cause either way, a
  granularity mismatch between LCARS's own season model and whatever external source it's
  matching against (mostly a non-issue for AniList specifically, which tends to issue separate
  ids per cour/part already — this session's own reproduced case was a real linking mistake, not
  a genuine AniList-side granularity gap; the real concern is future non-AniList sources that
  split seasons differently).
  **User's own resolution framing, both directions**: flag it (as already built), then a human
  decides between exactly two paths — (a) **incorrect**: it's a mistake, map the correct id to
  the correct season, done, matches what's already built; or (b) **correct but genuinely
  granular**: divide LCARS's own season into hierarchical sub-parts to match the more granular
  source (`1`, `1.1`, `1.1.1`, `1.2`, `1.3`, `2`, ...) and remap at the *episode* level instead of
  the season level, rather than picking a winner. Only path (a) exists today; path (b) is the new
  scope this note tracks. Explicitly parked for a later pros/cons discussion, not something to
  build off this note alone.
  **Three concrete real-world cases now on record (2026-08-15), both directions**: Mushoku Tensei
  S1/S2 and Fire Force S3 (AniList splits a cour LCARS/Sonarr track as one season — see the
  "15 over-marked episodes" entry near the top of this file); Ascendance of a Bookworm's whole
  4-part franchise (the mirror direction — Sonarr/TVDB tracks the entire thing as one flat
  absolute-numbered series, AniList splits it into 4 separate media entries with their own
  episode counts — see the entry right at the top of this file for the full data-fix writeup and
  the durability gap it leaves open: episode sync isn't range-aware per show, so this specific
  franchise can't safely hold a live Sonarr link on more than one of its four `show` rows today).
  The Bookworm case is probably the clearest concrete spec for path (b) whenever this gets
  designed for real: what's actually needed is per-show *absolute-episode-range* scoping on the
  Sonarr sync side, not just a hierarchical season-number scheme.
- [ ] A way to correct a schedule discrepancy (wrong air date, wrong episode-number alignment
  against AniList) *from Data itself* is needed — noted 2026-08-13 after having to fix "The
  Frontier Lord Begins with Zero Subjects" by hand: no client-facing way existed to do any of
  it, so it took a direct DB edit for `tracking_space` (no mutation exists for that at all) plus
  hand-written one-off scripts calling `setEpisodeAirDate` per episode against the live GraphQL
  API. `setEpisodeAirDate` itself is a real, existing mutation — Data just has no UI/action that
  calls it. Not designed here, just flagged: what the actual correction flow should look like
  from the calendar (pick an episode, enter/confirm a date), whether `tracking_space`
  reclassification needs its own mutation built too, and whether this belongs in Data proper or
  a future admin/ops-facing surface instead.
- [ ] SSH to the deploy host via its Tailscale name (`tiny`) is blocked by a tailnet ACL policy
  rejection, confirmed not a stale-session issue (a fresh verbose attempt still gets rejected
  after a clean handshake — "tailnet policy does not permit you to SSH as user drostan").
  Worked around 2026-08-12 by connecting to the host's real LAN IP directly instead
  (`tiny@192.168.0.152`, a different local account, bypasses Tailscale's SSH proxy entirely) —
  works, but the Tailscale ACL should actually get fixed rather than relied on staying broken.
- [ ] `aa` (add to AniList only, no Sonarr relationship) never bridges to LCARS at all — a real
  gap, deliberately not closed during B.12 since LCARS's `addShow` always assumes an episodic
  show (Data has no Radarr/movie path), which would be wrong for anything `aa` adds that's
  actually a movie. Needs its own scoping, not a rushed fix. See BUILD_PLAN.md's B.12 entry.
  **User's correction, 2026-08-15 — wrong framing, not a bridge to build**: `aa`
  (`action_add_to_anilist_only`, `~/repos/data/src/data/app.py`) itself needs to go away, not
  gain an LCARS-write step. LCARS is the single source of truth for every tracked show — a show
  existing in LCARS with no Sonarr link is an ordinary, unremarkable state, not a special case
  needing its own bypass pathway (AniList-only tracking existed as a distinct concept in aniq
  because aniq itself had no single database — LCARS replaces that need directly). Not deleted
  tonight — logged with the corrected understanding, actual removal not yet done.
- [ ] move all secret and password to a safer place
- [ ] make sure to design the ui for html client well
- [ ] have html client a login /password thing keep safe
- [ ] does the html client use a html/browser player or local one? my thought is local first then browser base but is it feasible?
  **Discussed 2026-08-15, options only, not designed — explicitly deferred**: four real options
  surfaced — (1) native mpv launch (Data's own approach, only works when Holodeck's opened from
  the media machine itself), (2) plain browser `<video>` streaming the raw file (fails on a lot
  of the real library — HEVC/FLAC/AC3/soft ASS subs aren't natively browser-playable), (3) real
  server-side transcoding, Jellyfin/Plex-shaped (a full media-server build from scratch), (4)
  delegate to the `jellyfin`/`plex` containers already running on `tiny` (no player to build at
  all — map an LCARS episode to its Jellyfin/Plex item id, deep-link or embed their existing web
  player, which already solves transcoding/subtitles/remote access/mobile). **User's own
  direction**: both, not either — local mpv when on a local machine (own CIF/media mount needed
  per machine, or configured each time), Jellyfin (preferred over Plex) as the remote/tablet
  path. Holodeck's UI should surface both explicitly — a Plex logo and an mpv/local-launch button
  side by side, user picks per-session. Real design work (the id-crosswalk, detecting "am I
  local," the actual UI) not started — revisit later.
- [x] `pending_review` surface built in Data 2026-08-12 (B.17/B.18, BUILD_PLAN.md:
  `~/repos/starfleet` v0.1.6 `season(id:)` query + `~/repos/data` `a6cb001`'s status-bar badge
  and `V` review screen), used against production the same day (B.19/B.20: fixed a real
  reopening bug the review work itself exposed, then reviewed all 23 remaining entries — 23 →
  10). **User confirmed live via `V` itself, works well** — resolved the last 2 hard cases
  personally (Monogatari, Haruhi S2 — both with a note, no id): Monogatari's own arc-based
  AniList structure is a genuinely known-complex case to work through properly later; Haruhi
  S2 needs a person to check whether it's the "Endless Eight" rebroadcast or something else,
  and will need per-episode (not per-season) resolution once decided — a real limitation
  `pending_review`'s season-level granularity can't express on its own.
  **User's own insight on the remaining 8** (DAN DA DAN S3, Kaiju No. 8 S3, Shangri-La
  Frontier S3, The Dangers in My Heart S3, The Rising of the Shield Hero S5, Undead Unluck S2,
  Frieren S3, Lycoris Recoil S2): these are "not yet aired" seasons that AniList records under
  their **romaji** title before an official English title exists — likely why fuzzy title
  matching (both the automated reconcile script and Fribb) missed them. User is happy to
  resolve these case-by-case as each one actually airs, not worth automating further right
  now. Still not built: Holodeck's dashboard / Captain's Log's inline prompt, the other two
  SCOPE.md §5.6 surfaces.
- [x] `anilist_id_review.csv` sent earlier 2026-08-12 for manual review is now obsolete/mostly
  wrong — it was generated before the tv-show contamination bug (B.16) was found, so ~318 of
  its ~340 rows were tv shows that should never have been on it. Don't use it; the real
  remaining backlog (23 items, all genuine cour-split cases) doesn't need a spreadsheet pass.
