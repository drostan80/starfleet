# Data plan — a correct starting database (step 1, draft for validation, 2026-09-27)

**Status: PROPOSAL, read-only analysis so far.** Nothing was written anywhere.
Analysis copies (scratchpad, not in repo): the 09-06, 09-07, 09-04, 08-27
snapshots, and a consistent `.backup` of today's live DB (09-27 14:46).

Order agreed with the user: **this plan first** (a correct starting database),
then the code phases in `PLAN-CODE.md`, then the rebuild.

---

## 1. Is the 09-06 snapshot the right starting point?

### What the file really contains

`lcars.db.bak-20260906-141520` was made with `cp` from a WAL database, so it holds
the last checkpoint, not 14:15:

| | 09-06 file | 09-07 file |
|---|---|---|
| last status change | 09-05 22:18Z | 09-06 14:27Z |
| last watch event | **09-06 08:26Z** | 09-06 21:46Z |
| last season write | **09-06 08:53Z** | 09-06 13:12Z (the merge) |
| tracked shows | 1,989 | 1,810 (178 merged away) |

→ The 09-06 file is the state at **09-06 08:53Z**, before the 13:12Z merge.
The gap to replay starts there.

### How to judge it

The new code rebuilds all **structure** from the sources (absolute numbering,
seasons/parts/specials as spans, ids at the right level, season 0 redistributed).
So structural faults in the snapshot (38 duplicate stubs, 4 side pieces as
seasons, missing numbers) don't disqualify it: they are regenerated anyway.

What must be right at the start is **your data**: show/season statuses, watch
history, scores, which shows are tracked, and any structural decision you made
by hand.

### Findings

| Check | 08-27 | 09-04 | **09-06** |
|---|---|---|---|
| tracked shows whose status was last set by automation (AniList reconcile / MAL reconcile / auto-complete) | 60 | 71 | **72** (42 / 27 / 3) |

Also in 09-06:
- 22 tracked shows whose status differs from their last logged change (changed
  by a code path that didn't log it).
- 220 tracked shows with **no status history at all** (created by paths that
  never logged a status).
- 1,518 seasons flagged `manual_override`. That flag was also set by automated
  adds (a "caller-supplied id" counted as manual), so it **cannot** tell your own
  decisions apart. Your real manual structure decisions have to be found another
  way (merge/split mutations you ran, reviews you resolved).

**Going earlier doesn't help:** the older snapshots hold the same kinds of
automated status writes, only slightly fewer, and every earlier day adds replay.
Mass automation (Sonarr "reconcile" pause/resume, hub ping-pong) starts
**09-19**; before that, automated status changes are a few dozen per day,
all logged.

### Season statuses have no provenance (applies to any snapshot)

`season.status` only exists since 09-03. Migration `c3d4e5f60718` (09-02) filled
it by copying each show's status onto its seasons, then setting `completed` /
`watching` from episode states; it also re-derived show statuses without logging
them (likely the 22 unlogged differences). `setSeasonStatus` never logged
anything. In 09-06: 1,773 seasons of tracked shows, 1,593 equal to their show's
status, 176 differ, 55 touched after 09-03, 133 shows with several seasons.

→ **09-06 is fine for show statuses and watch history, but season statuses must be
rebuilt whichever snapshot is used** (earlier ones don't have the column at all).
Since statuses live on seasons (R2.13), this matters most. Proposed sources, for
your approval:
- **anime:** each AniList entry is a season; its status history is in your
  AniList activity feed, which was purely yours until LCARS started pushing
  (mid-August, §6.8 of the old design) — after that, LCARS pushes are separated
  by `list_baseline` times;
- **TV:** no external source → seeded from the show status + watch history, then
  your review (TV multi-season shows only).

### Watch history inside the snapshot (08-16 → 09-06)

The completion auto-sync shipped on 08-16, so bursts before the cutoff are
reviewed too (same-timestamp groups of ≥3 events):

| Cause | Bursts | Events | Shows | Treatment |
|---|---|---|---|---|
| Trakt import (08-18) | 1 | 13,112 | 419 | your import — kept |
| completion via Data | 52 | 543 | 52 | valid (R2.7: completing marks every episode) |
| completion via Holodeck | 4 | 19 | 4 | valid |
| auto-complete | 4 | 154 | 4 | **suspect** — reviewed (e.g. Inspector Gadget) |
| nothing logged | 18 | 450 | 39 | **to review** (e.g. Tonbo!, Urusei Yatsura) |

**Recommendation: start from 09-06 (08:53Z state)**, with one review list for
you before anything else:
- the 72 automation-set statuses (some are genuine: a change you made on
  AniList/MAL that LCARS pulled is valid under R4.3);
- the 22 unlogged status differences;
- the 220 shows with no history (status only needs checking against R2.13 after
  the rebuild).

## Decisions (user, 2026-09-27)

- Start from 09-06 (08:53Z state). ✔
- Reads of AniList activity and Sonarr/Radarr added dates: yes. **Writes only
  once alignment is complete.** ✔
- Season statuses rebuilt (anime from AniList activity, TV from show status +
  watch history + review) — accounting for LCARS's own pushes (§2.6). ✔
- Rule of thumb for everything since 09-06: **you were only watching airing
  shows.**

## 2. Deriving the gap (09-06 08:53Z → cutover)

### 2.0 Older (not airing) shows — snapshot status, then the rules

(user, 2026-09-27)
- **Start from the 09-06 status**, then **align it to the new rules** by
  derivation, e.g.:
  - a show marked watching whose episodes are all watched (so every season
    completed) → completed (R2.15, R2.13);
  - S3 dropped, S4–S6 also dropped → S4–S6 skipped (R2.16), show dropped (R2.13).
- **Changes you made to older shows/seasons since 09-06 are discarded.** They
  were corrections of bad data, but we can't tell whether that bad data predated
  09-06 or came from the confusion.
- They are **tracked**: wherever the status re-derived from the snapshot + rules
  does **not** match a change you explicitly made in between, it goes on a
  **reconciliation list** for you to double-check.

### 2.0b Sanity check against an older snapshot (before the schema change)

- Reference: `lcars.db.bak-20260826-pre-v0.1.38-seasonschema` (08-26, effective
  state 08-26 13:33Z; 1,982 tracked shows, 1,323 anime + 659 TV). Each anime
  AniList entry was its own show, so its status is **season level** and mostly
  correct; it has duplicates (126 TVDB ids shared by several shows) and the
  show-TVDB / season-AniList confusion.
- (08-15 is older but has anime only; TV came with the 08-18 Trakt import.)
- Use: for **older, not-airing shows**, map each 08-26 AniList-entry show to its
  season in the rebuilt DB (by AniList/MAL id), align the 08-26 status to the new
  rules, and compare with the starting status. Differences → the same
  reconciliation list. A check only; it doesn't overwrite anything.

From today's live DB (read-only copy):

| Evidence | Count | Treatment |
|---|---|---|
| Show status changes by you (holodeck / data / captains_log) | 40 on 33 shows | **not replayed blindly**: some were you correcting automation's mistakes, or reacting to a swap. Shows **airing at the time** → replayed as best guess, aligned with the rules. Older shows → **discarded** (see §2.0), only tracked for the reconciliation list |
| Show status changes by automation | 5,323 on 302 shows | not replayed, **except** changes you made on AniList/MAL yourself, found through the AniList activity feed (R4.3). MAL has no activity feed, only a per-entry `updated_at`: weaker evidence |
| Watch events, single (one episode at a time) | 147 on 53 shows | split by the rule of thumb: on a show **airing at the time** → replayed (with or without a player tag); on an older show → likely a status adjustment or a backfill, **not** replayed as a watch, listed for you. Also checked against reconcile run times and the activity feed |
| Watch events in same-timestamp bursts | 112 in 10 bursts | reviewed one by one (below) |
| Score change | 1 (holodeck) | replayed |
| Shows created, now tracked | 77 | each goes through the R3.1 check |
| Shows created, untracked (stubs) | 171 | dropped (R3.5) |
| **Season** status changes you made | **not logged anywhere** | see §2.3 |

### 2.1 Watch-event bursts since 09-06 (to review)

| When (UTC) | Episodes | Show | What caused it |
|---|---|---|---|
| 09-06 13:30 | 9 | Mob Psycho 100 | to check |
| 09-15 12:35 | 24 (4 shows) | HEAD START AT BIRTH … | to check |
| 09-17 14:18 | 12 | Sasaki and Peeps | MAL reconcile |
| 09-19 10:34 | 33 | Bakemonogatari | to check |
| 09-23 10:10 | 14 | Haruhi Suzumiya | AniList reconcile |
| 09-24 09:52 | 4 | Star Trek: Strange New Worlds | auto-complete |
| 09-26 15:15 | 14 | Slime | AniList reconcile (after the stop) |
| + 3 smaller | | | |

A burst can be yours (marking a whole season watched) or automation's; you
decide per line.

### 2.2 Independent check: AniList activity feed (read-only)

AniList keeps a timestamped activity for every progress/status change on your
account ("watched episode 5 of X", "completed X"…). Reading it since 09-06:
- confirms the airing-show watches (you said you weren't watching older shows,
  so the real gap is mostly airing shows, one episode a week each);
- catches watches made outside LCARS;
- gives **season-level** evidence (AniList entries are seasons).
LCARS's own pushes also show up there. They are separated by matching
`list_baseline.updated_at` (the time LCARS pushed) against the activity time.
**Only reads AniList.**

### 2.3 Season statuses since 09-06

`setSeasonStatus` never wrote a history row, so your season-level changes after
09-06 exist only as the current value in the live DB, mixed with automation.
Evidence available: the AniList activity feed (§2.2) and your confirmation for the
airing shows. Code plan gets a season status history (see PLAN-CODE 0.2) so this
can't happen again.

### 2.4 Shows created since 09-06 (77 tracked)

| Created | Kind | Count | Notes |
|---|---|---|---|
| 09-17/18/21 | anime series | 49 | 46 in Sonarr; Sonarr reconcile auto-create |
| 09-17/18 | anime movies | 5 | no TVDB id |
| 09-11/18/19 | TV series | 4 | 3 with watch events |
| 09-19 | TV movies (Radarr) | 19 | Radarr reconcile auto-create |

Each is checked against Sonarr/Radarr's own **added date** (read-only API): added by
you after 09-06 → added per R5.2/R5.3; already in Sonarr before and only
auto-created by the reconcile pass → listed for you.

### 2.5 Skipped list — saved and reapplied

The live DB holds **178 skipped shows** (untracked rows with status `skipped`,
created by "skip" in browse/add); **159 were made after 09-06** and would be lost
by the rollback. No season is skipped. The list is exported from the latest DB
(ids: AniList/MAL/TVDB/TMDB + title), checked against the rules (a skipped
entry is not followed, R2.10), then reapplied to the rebuilt DB. It is your
work of the last weeks and is treated as correct.

### 2.6 LCARS's own pushes to AniList/MAL during the confusion

LCARS pushed wrong values to AniList/MAL (cascades, reconcile ping-pong, the
09-26 15:15Z run). So AniList/MAL are **not** trusted as they stand, and an AniList
activity only counts as your evidence if it is yours:
- the push log (`list_baseline.updated_at`) only goes back to **09-26 05:30Z**
  (2,969 rows), so it can't separate earlier pushes;
- instead each AniList activity is matched by time against LCARS's own events
  (status changes, watch events, season writes, within seconds):
  - no LCARS event near it → **you, on AniList directly** → evidence;
  - matches an LCARS event from Data/Holodeck/Captain's Log → your action via
    LCARS → evidence;
  - matches an automated LCARS event (reconcile, auto-complete, cascade) →
    **LCARS's push** → discarded;
- MAL: only a per-entry `updated_at`, same matching, weaker evidence;
- at the end, the aligned values are written back to AniList/MAL, which also
  repairs what was wrongly pushed (after your review of the write list).

### 2.7 Gap end

The live DB was still written today (WAL at 09:43; web and webhooks are running).
The gap is re-derived at cutover time, not frozen now.

## 3. Rebuild (after the code is approved and built)

1. Copy of 09-06 (`sqlite3 .backup`).
2. Apply your review decisions from §1 and §2.
3. Run the new engines: numbering (AniDB / TVmaze **with specials**), levels and
   spans, status rules, the add check. No hand-edited rows.
4. Replay the gap through the normal code paths (not raw inserts).
5. `rulecheck` must pass.
6. Diff against the live AniList/MAL lists and Sonarr monitoring → you review
   the exact list of external writes.
7. Apply; external writes last, in capped batches.

### Rebuild order constraints (phase 3, 2026-09-28)

- **Source data first.** The 09-06 snapshot has no AniDB episode / anime-lists /
  TVmaze data: copy those source tables (`anidb_*`, `anime_list_*`, `tvmaze_*`,
  `syoboi_*`) from live (`sources-20260928.db`, plus the AniDB fetch of 09-28 and a
  TVmaze-with-specials fetch) and recompute everything derived from them
  (`episode_anidb_mapping`, …). User: "adding all those sources is essential".
- **Same-TVDB consolidation before any Sonarr read or numbering.** After phase 3.1 a
  TVDB id held by several shows gets no Sonarr episodes and no availability (R1.14).
  The 09-06 snapshot has **126** such ids (live: 0). Consolidation must also move the
  merged episodes onto TVDB's season/episode — the siblings' own numbering restarted
  at 1, which is why **923** episodes (280 with files) have no Sonarr coordinates
  stored. Only 1 show has LCARS seasons that differ from Sonarr's, and none of its
  episodes lack coordinates.
- **Then a Sonarr read (captures TVDB season/episode and `tvdb_absolute`), then the
  numbering engine.**
- **`season.status_set_manually` from your own decisions** (phase 4): every row is 0
  after the migration. Set it to 1 for every season status that is yours (the accepted
  season-status review, your AniList activity, your LCARS changes in the gap). Without
  it the R2.16 warning never fires, and phase 7's R2.10 delete (auto-added planned →
  skipped → removed from AniList/MAL) would treat your own planned seasons as
  auto-added.
- **Show-level status changes replayed through R2.13a before deriving.** On live, 216
  shows are `dropped` with no season carrying it. *Corrected 2026-09-28:* all 216 are TV
  shows from the **08-18 Trakt import** (last status change `trakt_import`, 08-18) — so
  they are in the 09-06 base, not gap changes, and none was in the accepted season review. The replay must put each such change on the show's last non-skipped
  season; deriving first would silently turn them `planned`. (09-06 base: engine and
  rulecheck agree, 16 shows differ — 10 of them "last season planned → show planned".)
- **AniDB:** every entry for watching and planned shows is fetched (09-28, 506 raw
  answers in `anidb_xml/`); 1,081 remain for other shows — see RULEBOOK R1.2d.

## Access so far

On tiny, read-only: copied four snapshot files, and made a temporary
`sqlite3 .backup` of the live DB in `/tmp` on tiny, copied it, then deleted it.
Nothing else was touched.

## Worth knowing (no action taken)

The live DB is still being written (WAL modified 09-27 09:43). v0.2.70 is
running: every episode you mark watched still runs the old cascade and pushes to
AniList/MAL. Deploying the v0.2.71 freeze (show status changes without cascade)
is your call.

## Needs you

- Agree 09-06 (08:53Z state) as the starting point?
- OK to read the AniList activity feed and the Sonarr/Radarr "added" dates
  (read-only) to build the review lists?
- Then: the review lists (§1 and §2.1–2.4) are produced for you to go through.

## Review lists — built 2026-09-27 (read-only)

Page: https://claude.ai/artifact/XMJyKsTFySwzgqq3MthvSa (private; your overrides
are saved in its `decisions` collection, read back by Claude).
Sources: 09-06 snapshot, today's live `.backup`, Sonarr/Radarr catalogs (GET only,
via the lcars container), AniList activity 08-01 → 09-27 (2,046 entries, public
read). Nothing written anywhere.

Findings used for the proposals:
- AniList activity since 09-06: **1,667 LCARS pushes**, 92 yours (65 watches
  logged in LCARS, 19 made on AniList directly, 8 via Data/Holodeck/Captain's Log).
  An LCARS watch reaches AniList ~298 s later; automation comes in dense clusters.
- Single watch events since 09-06: 142 of 147 on shows airing at the time.
- New shows: 47 were your adds through LCARS add-with-Sonarr/Radarr on 09-17/18;
  21 TV items were auto-created from Sonarr/Radarr (18 movies already in Radarr
  before 09-06); 7 anime aren't in Sonarr/Radarr at all.
- Snapshot statuses set by automation: 37 kept (AniList pulls before 08-16, or you
  had just changed them on AniList), 35 to check (27 MAL, 5 AniList, 3
  auto-complete).

## Your review decisions — read back 2026-09-27 (119 lines)

Stored in the review page's `decisions` collection (copy in scratchpad).
Lines you didn't touch keep my proposal.

**Watch events since 09-06**
- All single watches on airing shows: **valid**, replayed (48 shows).
- Kaiju No. 8 S0E5–7: the season of minis being watched; the watch **may be on the
  wrong episode** → realigned by absolute numbering (R1.0, R1.13), then replayed.
- The Dangers in My Heart S0E1 (the film): not finished → not replayed.
- Haruhi S0E1: invalid.
- Bursts: Bakemonogatari, HEAD START AT BIRTH group (4 shows), Mob Psycho 100,
  Haruhi → **invalid**; Ludwig (2 episodes, 09-11) → **valid**.

**Status changes since 09-06**: your notes give the authoritative status. The
recurring rule is "completed, unless a later season exists → that season planned
→ show planned" (R2.13, R2.16, R2.17), applied by the rebuild:
- completed: A Knight's Tale*, A Silent Voice, Delicious in Dungeon, From Dusk
  Till Dawn, HEAD START AT BIRTH, I Left My A-Rank Party…, Nisekoi*, The Rising
  of the Shield Hero*, Neagley, Tommy & Tuppence, Ludwig (all aired seasons),
  A Livid Lady's Guide, Love Unseen…, Oh Boy…, My Stepmother…
  (* = unless a later season is planned)
- Blue Box, Reincarnated as a Sword: past seasons completed, next (unaired)
  season planned → show planned.
- watching: Last Seen, S.W.A.T. Exiles (first episode just watched), The Dangers
  in My Heart (film being watched).
- dropped: Cyberpunk: Edgerunners.
- 100 Girlfriends: completed if the last episode has aired.

**Snapshot statuses (09-06)**
- Kaiju No. 8: season of minis = watching, next real season = planned (the
  confusion comes from the minis; aligned by absolute numbering).
- Slime Season 2 Part 2: **duplicate** (check at season level whether episodes are
  still airing); Slime overall: watching unless the current season is confirmed
  completed and a future season planned.
- 100 Girlfriends, The Frontier Lord…: completed since.
- Tomb Raider King: watching, may be completed (to check).
- SAKAMOTO DAYS: **dropped** (the 09-06 14:10 "completed" on AniList was not you).

**Shows created since 09-06**
- 6 anime not in Sonarr/Radarr (The Guy She Was Interested In…, Historié, Kaketa
  Tsuki no Mercedes, Kekkaishi no Ichirinka, Kyoufu Collector, Majutsu wo
  Kiwamete…) → **add through the normal check**.
- S.W.A.T. Exiles → add, actively watched.
- 17 Urusei Yatsura movies + The Dog Stars (already in Radarr before 09-06) →
  "no add to Sonarr" (see question below).
- The other 51 as proposed (add again through the normal check).

**Skipped list**: untouched → all 178 kept.

### Correction to the AniList classification

Your SAKAMOTO DAYS note showed LCARS pushes classed as "you on AniList
directly": completions seconds apart (09-20 18:26, 09-26 05:46), the 09-26
15:15–15:19 run, and 09-06 14:10 after the unlogged merge. Tightened rule:
two or more changes within 60 s, or inside a known LCARS run window, count as
LCARS; only isolated, spaced-out changes count as yours.

### Follow-ups (user, 2026-09-27)

- **Slime Season 2 Part 2** — checked: correct at **season level** (AniList 116742
  = TVDB S3, 12 eps, abs 37–48, aired 2021-07→09, 12/12 watched → completed);
  **duplicate at show level** (tracked show row `s-1tcd6r`, same AniList 116742
  and TVDB 352408 as `s-hyj69b`, 0 episodes) → dropped by the rebuild (R1.14).
  Also: S5 ended 09-25, 24/24 watched → completed; "S6" = AniList 161802
  *Visions of Coleus* is a side piece → placed by air date as specials with
  their own abs numbers (R1.8, R1.13), not a season.
- **Urusei Yatsura movies (17) + The Dog Stars** — answer (a): kept in LCARS as
  tracked, status **skipped**, no Sonarr/Radarr change; each gets its absolute
  number by the rules (films placed by air date in their show, R1.4/R1.8).

## Season-status evidence — built 2026-09-27 (read-only)

New sections on the review page ("Season statuses to check" 161, "sources agree"
1,539), one line per season of a tracked episodic show in the 09-06 snapshot.
Proposal = first available of: all episodes watched (428) → AniList entry
untouched since before 08-16, i.e. purely yours (1,030 of your 1,607 entries
qualify) → your own AniList activity (tightened classification) → 08-26
snapshot (47) → 09-06 status (125, no other evidence). "To check" = sources
disagree or you left a note on the show. Your earlier 119 decisions are kept.

### Season statuses — accepted (user, 2026-09-27)

All 1,700 season proposals accepted as shown (no overrides). With this, every
input for the starting database is decided: 09-06 base, season statuses, gap
replay list, new shows, skipped list, Slime/Urusei Yatsura follow-ups.

Live note (gap, after this review): *As a Reincarnated Aristocrat, I'll Use My
Appraisal Skill to Rise in the World* **Season 3 ep 1** airs and is watched on
09-27 → that season goes planned → watching (R2.14). The gap is re-derived at
cutover, so this and any later watch is picked up then.

## Rule check baseline (lcars rulecheck, 2026-09-27)

| Rule | 09-06 snapshot | today (09-27 copy) |
|---|---|---|
| R1.0 episode without absolute number | 10,211 | 10,211 |
| R1.8 episode outside every season (season 0 etc.) | 11,584 | 1,393 |
| R1.11 season without a span | 1,463 | 1,281 |
| R1.12 overlapping spans | 0 | 0 |
| R1.14 several shows per TVDB id | 126 | 0 |
| R1.22 AniList/MAL id on several seasons | 25 | 0 |
| R1.23 AniList/MAL id on the show | 1,651 | 2,220 |
| §2.2 season without status | 4 | 1 |
| R2.13 show status ≠ last non-skipped season | 16 | **444** |
| R2.15 all watched but not completed | 19 | **891** |
| R2.7 completed with unwatched episodes | 37 | 15 |
| R2.16 planned after paused/dropped/skipped (check) | 0 | 2 |
| R3.2 tracked series without TVDB id | 126 | 56 |
| R3.5 untracked stub shows | 1,100 | 1,376 |

Most structural counts (R1.x, R3.x) are fixed by the rebuild engines, not by hand;
status counts (R2.x) by the status engine + the review decisions.

After phase 2 (09-06 snapshot, upgraded, 2026-09-28): everything above unchanged except
R1.8, now checked against spans instead of the season link: **16,188** (every episode
not inside a span; only 310 of 1,917 seasons have one until the rebuild sets them).
R1.12 is now level-aware (siblings don't overlap) — 0; new R1.10 (parts inside their
TVDB season) — 0 (no parts exist yet). The migration changed none of the user's data
(season/show/episode/watch/external-id tables identical on the old columns).

## TVDB alignment (2026-09-27, read-only checks; for confirmation)

**Rule:** the **show-level TVDB id** of every season comes from today's data (the
latest fixes), **verified** (R3.7). The **season placement inside the show** does
**not** come from today: the 09-06 13:12Z merge put side pieces in as seasons
(Eromanga Sensei OVA → S2, ROOM CAMP → Laid-Back Camp S5, Haganai Episode 0 → S4,
Mushoku/Food Wars OVAs, Frieren mini-anime → S4…). Placement is decided per
episode by Memory Alpha (R1.17, R1.2c).

09-06 vs today, per AniList season: 1,147 identical; 167 same TVDB show but a
different season number (the merge — placement re-derived, above); 35 had no TVDB
id on 09-06 and gained one; **0 conflicting TVDB ids**.

Verification of today's TVDB ids (1,595 AniList seasons of tracked shows):
- **1,439 agree with Fribb** → accepted.
- 5 disagree, settled on TVDB itself:
  - LCARS right (Fribb id deleted on TVDB): DARK MACHINE THE ANIMATION 480909,
    Uncle's Obsession With Cute Things 480889;
  - **LCARS wrong → corrected**: Mouse Cursor… 482380 (doesn't exist) → **473125**;
    Gensou Suikoden 427736 ("Don: Gokudou Suikoden", 1992) → **482279** (Suikoden:
    The Anime, 2026-10-03); Kekkaishi no Ichirinka 429767 ("Kits 'N' Cruisin'",
    1998) → **467053**.
- 151 with no Fribb TVDB (44 TVDB ids) → TVDB name check: 38 match;
  **wrong, link removed** (standalone films / wrong series): Your Name. (197, doesn't
  exist), Perfect Blue (72933 "Nowhere Man"), The Girl Who Leapt Through Time
  (81248 "In Treatment"), Dead Leaves (251785 "Wainy Days"), Historié (335920
  "Norsk Historie") → correct id looked up, else user; **to confirm**: Magical Girl
  Raising Project restart on the 2016 series 316815.
- All 5 wrong ids are title-search matches from the 09-17/18 adds or cross-id
  propagation — the R3.7 check is added to the add path (PLAN-CODE 5.1).

## Individual seasons (R3.6c)

Candidates (added since 09-13, no TVDB link): Narumi's Week at Work, Kaketa Tsuki no
Mercedes (series) — each first gets a TVDB lookup; individual only if TVDB has
nothing yet. 5 films in the same list (To You in the Beyond, GROTESQQQUE, Rakuen
Tsuihou: Kokoro no Resonance, Medalist Movie, ghost – end of night) are films,
placed by R1.4 / franchise, not seasons. The 55 older series without a TVDB id are
**not** individual seasons: TVDB lookup, else your review (R3.3).

### TVDB checks for the user (2026-09-27, review page "TVDB:" sections)

User asked to verify personally (links to AniList and TVDB on every line):
- **55 series without a TVDB id**: 12 with an own TVDB series (name + year match),
  24 specials/OVAs that belong to their AniList parent's TVDB show (placed by air
  date, R1.8), 19 with nothing reliable → user decides (R3.3).
- **35 that gained a TVDB id**: strict check. Films turned out to be the problem:
  some are legitimately inside their show (TVDB lists them as specials:
  Evangelion Death & Rebirth, Mugen Train, Dreaming Girl, SHIROBAKO Movie, Time of
  Eve), others were linked to unrelated series or stored a TVDB *movie* id as a
  series id (Your Name. = movie 197, Perfect Blue → movie 3807, The Girl Who Leapt
  Through Time → movie 1384, Dead Leaves → movie 16387, 5 Centimeters → movie 3000,
  VIRGIN PUNK → movie 367630; Harlock Riddle of Arcadia, Ninja Scroll → user).
- **5 Fribb disagreements** and **MahoIku restart**: user checks via the links.

### TVDB decisions — user, 2026-09-27 (authoritative)

Rows without a note = my proposal confirmed. Every id below was re-read on TVDB
(series and movie) before recording. Full data: ~/starfleet-review-2026-09-27/.

**Films → TVDB movie ids:** 5 Centimeters per Second 3000 · VIRGIN PUNK Clockwork
Girl 367630 · Your Name. 197 · The Girl Who Leapt Through Time 1384 · Perfect Blue
3807 · Dead Leaves 16387 · Kuro no Sumika -Chronus- 48409 · Ghost in the Shell
(1995) 4549 · Voices of a Distant Star 9702 · Rescue ME! 102515 · THE UROTSUKI
101794. Urotsukidoji: Legend of the Overfiend: no TVDB id (AniList 2341 is the
3-episode OVA; TVDB only has the 1989 film cut 11857) → own show, R3.3.

**Series ids confirmed:** HEAD START AT BIRTH 450837 · I Parry Everything 441727 ·
Possibly the Greatest Alchemist 449884 · Re:Monster 439755 · Dark Gathering 422090 ·
A Wild Last Boss Appeared! 453694 · The Brilliant Healer's New Life 447246 · The
Dangers in My Heart 422981 · Ōoku 432839 · Onmyoji 425298 · Captain Harlock (1978)
80886 (Riddle of the Arcadia Episode belongs to it) · Ninja Scroll → 72842 (film in
the series show) · Gantz: Second Stage → 78916 · Aki-Sora 467259 · Henkei Shoujo
331278 · Huckleberry Finn Monogatari 327702 · Little Women (1981) 281234 · Chou
Futsuu-ken Chiba Densetsu 443355 (The Legend of Super Normal Pref. Chiba, 2024) ·
Zeikin de Katta Hon 474749.

**Belong to a parent show (specials/seasons placed by air date):** Frieren minis →
424536, decimal season number (R1.9a) · The Irregular at Magic High School: Get to
Know… → 278329 · Rurouni Kenshin 3rd Season → 413578 (new season, planned) ·
Shagahai ReLIFE Kenkyuujo → 299508 (OVA after the final arc) · Narumi's Week at Work
→ 423075 (Kaiju No. 8) · Dungeon Meshi: Senshi no Kantan Cooking! → Dungeon Meshi.

**Own show, no TVDB id (R3.3 exception, user-checked):** Muramata-san no Himitsu,
Uji ni wa Monogatari ga Aru, Urotsukidoji: Legend of the Overfiend (OVA).

**Remove from LCARS and AniList/MAL** (added by mistake): Huhuan Shaonü Special,
TENSAI BANPAKU Opening Animation, Sword Art OFFline, **Magical Girl Raising
Project: restart** (not followed; its show goes on the skip list).

**New add after the rebuild:** Books Bought with Tax (live action, TVDB 474758) →
its own show, planned (normal add path).

→ No individual seasons remain from existing data (Narumi goes to Kaiju No. 8).

## Phase 5 review — your decisions (2026-09-28, review page 3gXa5fHHeScXr66SvCLKJF)

Read back from the page's `decisions` collection (228 entries).

- **Numbering, 6 shows:** all OK. Solo Leveling note: an episode with no air date must
  still get a number — placeholder shown as **x** (stored with a placeholder marker since
  the field is numeric), else 0.1 → RULEBOOK R1.0a.
- **Merges, 181 shows (126 TVDB ids):** all OK (no click = OK). Notes: s-2f6eth and
  s-xt01q8 are the second cour of the show above them (→ part, as planned).
- **Films, 19:** all fold in.
- **From your AniList list, 22:**

| AniList | Title | Add check | Your decision | Your note |
|---|---|---|---|---|
| 1549 | 1000-nen Joou: Queen Millennia | special | ok | fold in mark watched |
| 4901 | BLACK LAGOON: Roberta's Blood Trail | special | ok | fold in mark watched |
| 7311 | Suzumiya Haruhi no Shoushitsu | special | ok | fold in mark watched |
| 10893 | Kyousougiga | special | ok | fold in mark watched |
| 18753 | Yahari Ore no Seishun Love Come wa Machigatteiru.: Kochira to Shite mo Karera Kanojora no Yukusue ni Sachi Ookaran Koto wo Negawazaru wo Enai. | special | ok | fold in mark watched |
| 20021 | Sword Art Online: Extra Edition | special | ok | fold in mark watched |
| 20728 | Nisekoi OVA | special | ok | just fold in mark unwatched |
| 21660 | Dungeon ni Deai wo Motomeru no wa Machigatteiru Darou ka: Dungeon ni Onsen wo Motomeru no wa Machigatteiru Darou ka | special | ok | just fold in mark unwatched |
| 100268 | Natsume Yuujinchou: Utsusemi ni Musubu | special | ok | fold in mark watched |
| 100643 | Made in Abyss: Fukaki Tamashii no Reimei | special | ok | fold in mark watched |
| 113811 | Honzuki no Gekokujou: Shisho ni Naru Tame ni wa Shudan wo Erandeiraremasen OVA | special | ok | fold in mark watched |
| 142876 | Dr. STONE: Ryuusui | special | ok | fold in mark watched |
| 164193 | Kien Romance | individual_season | ok | part of https://anilist.co/anime/150957/Otaku-Elf/ will never be in tvdb  |
| 165066 | Shayou (Music) | individual_season | ok | part off https://anilist.co/anime/153152/The-Dangers-in-My-Heart/ will never be in tvdb |
| 173807 | Chocolat Cadabra | individual_season | ok | music video not even linked to a show but made by a studio so... an edge case |
| 177406 | Gif ni Ted | individual_season | ok | part of https://anilist.co/anime/166828/A-Salad-Bowl-of-Eccentrics/ will never be in tvdb |
| 185657 | Skip to Loafer 2nd Season | needs_user | part |  |
| 204363 | SAKAMOTO DAYS 2nd Season | individual_season | no | correct but show is dropped this season should be deleted from anilist and set to skip here |
| 208766 | Bless | individual_season | ok | tvdbid 475021  |
| 209224 | Game Sekai Tensei <Dankatsu>: Gamer wa [Dungeon Shuukatsu no Susume] wo <Hajime kara> Play Suru | individual_season | ok |  |
| 213658 | Diamond no Ace act II: Second Season Part 2 | individual_season | no | remove from anilist too fyi tvdb is 273005 and should be skipped on opur end |
| 217434 | Mushoku Tensei III: Isekai Ittara Honki Dasu Part 2 | needs_user | part |  |

  Rebuild actions from the notes: "fold in, mark watched/unwatched" = add as the
  season-0 piece with episodes watched / unwatched; 164193, 165066, 177406 will never be on
  TVDB → attach to their AniList parent show (150957 Otaku Elf, 153152 The Dangers in My
  Heart, 166828 A Salad Bowl of Eccentrics) as a level of that show; 173807 music video →
  individual season (edge case); 204363 → skipped here, deleted from AniList (its show is
  dropped); 213658 → not added, removed from AniList, TVDB 273005 on the skip list;
  208766 → TVDB 475021; 185657 and 217434 → add to the proposed show (Skip and Loafer,
  Mushoku Tensei).

## Phase 9.1 decisions (user, 2026-09-28)

1. **The 216 Trakt drops** (TV shows imported dropped on 08-18, no season carrying it):
   dropped on the **last season that has aired** (never an unaired one), so the show
   can't revert to watching/planned; later unaired seasons → skipped (R2.16); earlier
   seasons: all watched → completed, otherwise unchanged. Follow the rules. *The user has
   another, unpolluted source for these (not practical now); they may rework these shows
   in isolation later.*
2. **Mine = everything the user reviewed during this rebuild (since step 0)**: every
   season status from an accepted review, the gap decisions, the TVDB/phase-5 decisions →
   `status_set_manually = 1`. What the engine derives afterwards is automatic.
3. **Two runs**: now (write list for review) and at cutover (fresh live copy). **Labelled
   snapshots before and after**, so a rollback is always possible. **The user won't watch
   anything until the new setup is live** — the gap ends at the last watch logged below.

4. **Freeze** (user, 2026-09-28): lift it at cutover (`LCARS_AUTOMATION_FROZEN=0`) — the
   issues that triggered it are fixed by the rebuild code.

5. **Missing episodes** (user, 2026-09-28): 835 tracked shows have no episodes and many
   lack specials (never in Sonarr). **Option A: read their episode lists straight from
   TVDB** (R1.2e) in the rebuild — stopgap and base; the AniDB drip reconciles anime
   later. Consequence accepted with the choice: completed seasons get their episodes
   marked watched (R2.7).

6. **Step A built** (user approved 2026-09-29): episode lists read from TVDB in stage 4
   (`tvdb_episodes.py`, `air_date_source = 'tvdb'` migration `b2c3d4e5f6a8`; TVDB key read
   from tiny's `lcars.ini` into an env var, never printed or stored). Run 1 stages 4–6 re-run:
   23,774 episode rows for 772 of 776 shows. Rulecheck after: R1.11 1,133 → 179; up: R1.8
   90 → 236, R1.12 9 → 38, R1.10 1 → 5, R2.7 1 → 5, R2.16 0 → 63.
7. **R2.16 fix in stage 4** (user approved 2026-09-29): a season row made for TVDB's episodes
   keeps the engine's R2.16/R2.19 status unless episodes are watched (then completed /
   watching); it was overridden to planned (63 seasons after a dropped/paused/skipped one).
8. **Pause and sanity check** (user, 2026-09-29) before stage 7: expand rulecheck; review the
   new code and the whole database for rules being *enforced*, not only followed.

9. **Cleanup review** (user, 2026-09-29; review page K1BDJ2H8AHJCy6bP73iUvc, decisions saved in
   `~/starfleet-review-2026-09-27/decisions-cleanup-0929/`; OK by default). Agreed with no marks:
   - remove the untracked stubs (R3.5): 236 merge leftovers, 193 duplicates of a kept season, 887
     discovered entries not on the AniList list; a stub with watch history (Kaiju Girl) moves first;
     export before deleting. Basis: 1,292 of the 1,335 existed on 26 Aug (`old/pre-0826.db`), 220 of
     the 236 merge leftovers appear in earlier reviews;
   - remove the 1,323 show-level list ids a season of the same show already holds (R1.23);
   - delete the 40 orphaned watch events; count `tvdb_movie` as a TVDB link for R3.2 (rulecheck);
   - Witch on the Holy Night: create its season, put anilist 143103 + mal 50668 there;
   - delete the duplicate shows Tantei wa mou, Shindeiru. Season 2 and Sasaki and Peeps Season 2;
   - clear the wrong Sonarr links (Voices of a Distant Star → Oshi no Ko, THE UROTSUKI → The Rookie);
   - the 16 list entries + 53 specials go to the proposed parent show, except the answers below.
   **Answers (homes):** Patlabor: The New Files → OK, status **paused**; Sekai Saikou no Ansatsusha …
   2nd Season (anilist 169579) → second season of anilist 129898; Akane-banashi 2nd Season → tvdb series
   466488; R.O.D -THE TV- → tvdb 82319; Ghost in the Shell SAC 2nd GIG → **delete** ("I will see
   separately, I accept the loss"); Narumi's Week at Work → part of Kaiju No. 8 (tvdb 423075);
   Shangri-La Frontier → tvdb 421855, "2 seasons should be watched". **Answers (specials):** Railgun:
   Motto Marutto → tvdb 114921; Arifureta Prologue → tvdb 357019; Milky Byway Spring Special → tvdb series
   465427 (a movie 375859 also exists; "all in one id or 3 separate shows, I can live with both"; no
   Milky Byway id yet); AkaKill! Theater → tvdb 280329 (Akame ga Kill!); Odekake Kozame × Chiba-ken Kuro
   Ajillo → special of tvdb 435306; ROOM CAMP → part of tvdb 330692 (Laid-Back Camp).
   **Stub marks made on an earlier page version (to confirm):** 24 stubs marked Not OK with a status
   note (skip / skip-drop / drop / paused / completed), e.g. A Certain Magical Index II skip, Air Gear
   Special Trick completed, ALDNOAH.ZERO Season 2 completed (AniList says PLANNING).

10. **Cleanup built and run (2026-09-29).** `rebuild_cleanup.py`, stage 7 `cleanup`, inputs
    `rebuild-inputs/cleanup.json`. User's further answers: merge leftovers are deleted with their
    merge-audit rows (export + checkpoints are the history); Magical Index: drop the show (S1
    dropped, later seasons skipped); Arifureta Miraculous Meeting → tvdb 357019; Shangri-La: three
    seasons within tvdb 421855, S1/S2 completed, S3 **planned (not aired yet)** — replaces the
    earlier accepted "completed" (`season_status_overrides`); Kaiju Girl stub mapped onto
    tvdb 471878 (7 events, all already on the tracked show). Applied consequences: skip on a season
    with a list id may delete it from AniList/MAL (R2.10); ALDNOAH.ZERO S2 completed → a COMPLETED
    push in the write list.

11. **R2.15a — a confirmed episode count** (user, 2026-09-29, from Overgeared S01E01 completing the
    whole show on live): a level completes by itself only when every episode is watched and its
    count is confirmed (AniList total = the episodes LCARS holds, release status irrelevant; or, with
    no list entry, an ended series (Sonarr) or a later season; or set by you). Built:
    `season.episode_total`, `show.series_status`, `status_rules.episode_count_confirmed`, totals
    from the AniList/MAL list poll. **Past seasons are not reviewed**: the rebuild's stage 6 derives
    historical data with `require_confirmed=False` (output identical to before: 28,607 watched
    episodes, same statuses); the gate applies to new watches, the replay (stage 8) and refreshes.
    Known limit: TV shows not in Sonarr have no series status yet, so their latest season completes
    only when a later season exists or you set it.

12. **Replay built and run (2026-09-29), stage 8** — `rebuild_replay.py`. 128 valid live watch
    events replayed (0 for review), 30 status finals applied (10 discarded, 9 invalid watch groups
    skipped), 5 manual watches (Mushoku S3E14 = last aired, S3 completed; Overgeared S1E1 →
    watching under R2.15a), score 16.0 on The Frontier Lord, Kaiju minis S0E5–7 on the same-titled
    episodes (confirm). Slime: its "S6" (Visions of Coleus, AniList 161802) is a special level
    (`as_special`, your 09-27 answer), the show is watching. **Kaiju No. 8 (settled, user 09-29):**
    the 09-05 watch on the unaired S3E1 was episode 1 of Narumi's Week at Work (AniList 204431):
    moved to that mini with its original time (`realign_watches`), S3 planned, show planned, minis
    level watching.
    Rulecheck after stage 8: R2.15 now enforces R2.15a (0); open R1.8 28, R1.11 166, R1.12 15,
    R1.22 25, R2.7 5.

## Stage 9 findings — your decisions (2026-09-29, review "Mapping Review")

Stage 9 (checks) on run 1 found five kinds of thing; your answers and what was built:

- **Root cause of the shared-id clashes: TVDB renumbered shows after 09-06, and stage 4 matched
  old rows to Sonarr's episodes by number** (Slime S3E1 "The Visitors", 2021, was stamped TVDB
  S3E1 "Demons and Strategies", 2024). Scope (703 shows compared against Sonarr): Slime, Re:ZERO,
  SPY x FAMILY, Mushoku, Dr. STONE, Bookworm (one TVDB season of 60), Black Lagoon, Fire Force,
  Tonbo! (+ Animal Control, one episode inserted). Built: `rebuild_reanchor` (R1.2f) — rows go to
  their episode by title + air date, duplicates removed with their history, seasons follow
  (Fribb's TVDB season for each entry), the replay finds an episode the same way.
- **Your per-TVDB-season truth** (Fribb agrees in every case): Slime S1 101280, S2 108511+116742,
  S3 156822, S4 182205; Re:ZERO S1 21355, S2 108632+119661, S3 163134 (16), S4 189046 (19); SPY x
  FAMILY S1 140960+142838, S2 158927, S3 177937; Mushoku S1 108465+127720, S2 146065+166873,
  S3 178789 (+217434 when its episodes come); Dr. STONE S3 131518+162670 (22), S4 172019+189117+
  199221 (37; S04E01 "RYUSUI VS. SENKU"); Black Lagoon S1 889+1519 (24, the OVAs 4901 and the
  Omake 8440 are specials); Bookworm S1 four parts (14+12+10+24 = 60).
  Mushoku S2 also has the special "Guardian Fitz" (TVDB S00E02) that AniList counts as episode 0 of
  146065 — **AniList progress for that entry is one off: to handle when the write list is built.**
- **Chitose Is in the Ramune Bottle**: cour 1 is 13 episodes (180082, completed), cour 2 (198727)
  starts at the 13th and shows one episode on AniList so far, more likely; its unaired episode stays
  unwatched.
- **Unaired episodes** (R2.7): never marked watched; a level with one is watching. Bookworm S1/S4,
  Re:ZERO S4, Chitose S1. Seven levels with no aired episode are planned (Frieren S3, DAN DA DAN S3,
  Undead Unluck S2, Bakemonogatari S7, Chained Soldier S3, The Dangers in My Heart S3, The Ramparts
  of Ice S2 — starts 2026-10-01): `season_status_overrides`.
- **Film shows** (R3.2a): 47 of 50 carried an unrelated series' `tvdb` id and TVmaze id, added on
  live after 09-06 (Donnie Darko = Cheers). The TVDB movie id comes through the TMDB id
  (`rebuild-inputs/film_tvdb.json`, 48 films); Time of Eve: The Movie and Memories have no TMDB id:
  yours to review. Star Wars III lost its 26 episodes and 26 Trakt watches (they were "Are You Afraid
  of the Dark?").
- **Spans** (R1.11 amended, R1.12, R1.13c): minis nested in their group; a special or film inside
  another film's span is not an overlap (Fullmetal Alchemist, Attack on Titan); Monogatari stays as
  Memory Alpha + Fribb number it (named exception); a level has a span when it has episodes or a
  place in a series (standalone movies and list-only entries exempt); Monster Eater, Chainsmoker
  Cat, The Duke's Son, Tomb Raider King: the unmatched part and the AniList entry are one part.

## Your AniList history (user, 2026-09-29 evening)

- **Write list, first cut, would have erased history**: 227 entries (mostly dropped shows, last
  changed 2023–2026) hold a progress LCARS never recorded (Hunter x Hunter 118 → 0). Your answer:
  LCARS takes the list's progress; an entry last changed before the mess (08-15) is never lowered.
- **The 11 mess-era entries** (changed 08-15..09-27 and going down): the value before the mess
  (08-26 snapshot) wins. It agrees with the rebuilt copy for all but Mob Psycho 100 S3 → dropped
  at 3 (`rebuild-inputs/list_decisions.json`); You and I Are Polar Opposites S2 is **watched to 11
  only** (user, 09-30: the live S2E12 row is not a real watch; the write list lowers AniList/MAL 12 → 11, correct).
- **GDPR export** (your download, 09-29): "get everything you can from AniList and map it; if it
  gives new information trust it; only discard what is likely an artefact of the late mess and
  what conflicts with what I explicitly confirmed today". Extracted without personal data to
  `rebuild-inputs/anilist_history.json` (1,608 anime entries, 10,056 activities from 2023-01-08;
  times are Japan time). Built: `rebuild_history` in stage 8 — watch events at their real times
  (7,173, none overlapping Trakt's), rewatches, start/finish dates, scores where LCARS had none;
  activity after 08-15, unaired episodes and the levels you set today are left out; 52 entries
  that don't map are reviewed.

## Write list review (user, 2026-09-29, review page "Write List Review")

37 values corrected by the user, applied as explicit per-entry decisions
(`rebuild-inputs/list_decisions.json`, stage 10; they win over the 08-26 snapshot): the 14 announced
seasons whose only episode was a watched "TBA" placeholder are planned at 0 (Witch Hat Atelier S2,
Makeine S2, Shield Hero S5, Gushing S2, Sentenced S2, Akane-banashi S2, Lycoris Recoil S2, Tune In S2,
Delicious in Dungeon S2, Hotel Inhumans S2, KILL BLUE S2, A Star Brighter S2, Unaware Atelier S2,
Jack-of-All-Trades S2) + 'Tis Time for "Torture" S2 and Dangers in My Heart (level "S4", TVDB counts
the film; numbering kept); completed: SAO S3, Made in Abyss S2, Saint Seiya S6/S7/S9, SNAFU S2, Amagi
Brilliant Park special, Kill la Kill side piece; dropped: Natsume S1 at 13, Hanaori-san at 6, KAMUI S1
at 8 and its minis at 6, Lazarus at 2, FLCL S2, Duke of Death S1 at 1, Cyberpunk at 1; paused:
Dorohedoro S2 at 7, Hunter x Hunter S4; skipped: FLCL S3; watching: Tomb Raider King at 11; Chitose
cour 2 (198727) planned at 0. Watch dates: the user doesn't mind dates not matching (the 08-12
burst and the rebuild's own R2.7 events stay). Result: AniList 70 changes + 11 adds, MAL 58 + 12,
5 deletes, 67 Sonarr shows; every decided entry verified to end at the value given.

## Watches during the rebuild (keep until cutover)

The user keeps watching while the rebuild runs (v0.2.70 still live). All of it must
land in the final database. Three sources, reconciled at cutover (gap re-derived to the
cutover moment, PLAN-DATA §2):
1. **The user's own notes in the conversation** — logged here by Claude, every time:

   | Date | Show / season / episode | Note |
   |---|---|---|
   | 2026-09-27 | As a Reincarnated Aristocrat, I'll Use My Appraisal Skill to Rise in the World — S3 E1 | season planned → watching (R2.14) |
   | 2026-09-27 ~21:15 | Mushoku Tensei — S3, last aired episode (number not given) | S3 finished → completed. User also added the following entry on AniList: id **217434** (looks like S3 cour 2) — added on AniList directly |
   | 2026-09-27 ~22:00 (finished) | Overgeared — ep 1 | season/show → watching |
   | 2026-09-28 (reported) | Last Week Tonight with John Oliver — S13E24 | watched |
   | 2026-09-28 (reported) | Animal Control — S05E01 | watched |

2. **Live LCARS** (v0.2.70): watch events and status changes after 09-27 in the live DB
   (take a fresh `.backup` at cutover).
3. **AniList activity** since 09-27, classified as before (the user's own vs LCARS push).

Where the three disagree, the user's note wins; anything only in LCARS/AniList that
isn't on an airing show goes on the review list.
