# Next up

Current version: **v0.3.2** (live since 2026-09-30, rebuilt DB). Record: PLAN-DATA.md "Cutover (2026-09-30)",
HANDOFF-2026-09-30-CUTOVER.md. The sections further down (v0.2.x) are history.

## Open work — consolidated 2026-09-30

### A. Left over from the rebuild

**STATE 2026-10-05 night: v0.4.5 IS LIVE on tiny (UI: page auto-refresh on every page, pull-down + corner refresh button for touch and the app; snapshot `lcars.db.bak-20261005-pre-0.4.5`; the Android app bundles the UI, so it needs an APK REBUILD to get it, the user tests it another time). v0.4.4 is below.** v0.4.4 (tag v0.4.4, CI green, snapshot `lcars.db.bak-20261005-pre-0.4.4`; the compose backup is `starfleet.yml.bak-0.4.2-20261005`). It carries v0.4.3 (stale reviews close themselves, divided seasons keep no list id, nginx re-resolves lcars), provisional episodes (R1.2g), Syoboi runs per level (R1.22a), every source's schedule as a choice, Syoboi id seeding and links on every external id. Verified after the deploy: migration applied, DNS inside lcars/ops, web 302 (login), no nginx errors, first pass made 108 provisional episodes on 30 shows, 1,370 level Syoboi ids, 0 external ids without a link. **Prod data steps done at the deploy:** part order fixed (Bookworm S1, Dr. STONE S4, SAKAMOTO DAYS Part 2), Kusuriya S3's duplicate id dropped, the stale reviews closed (0 open), Bleach now reads dropped (S1 stays completed), MAL 54344 removed (it was a plain plan-to-watch on a skipped level). Earlier today: v0.4.0, v0.4.1, v0.4.2 and the starfleet.yml DNS fix. **To watch:** Syoboi options should rise above 79 shows once the next sync has fetched the 108 new level TIDs; the AniDB drip resuming after 00:00 UTC; no review noise returning.

**Rulecheck triage — review page batch APPLIED on prod 10-05 (snapshot `lcars.db.bak-20261005-pre-review-batch`; scripts/review_batch_20261005.py): 34 → 9 violations.** Done: 20 levels' episodes marked watched (you watched those specials), With Vengeance S1 completed, Magic Repo Man's AniList id moved to S1, Mushi-shi's duplicate special removed. Kept as you decided: 4 planned levels with watched episodes stay planned (R2.14), 4 completed levels stay as they are. **Still open, need a code change, not data:** R1.8 Strawberry 100% S0E6 (12.4) and SAO S0E23 (51.5), and R1.12 Mushi-shi part 1 (27–37 swallows the special at 37): Memory Alpha rewrites special/part spans from the AniDB entry on every pass (`numbering._apply_level_spans`), so a hand-edited span is undone (rehearsed). Fix = a manual episode→level override that numbering honours, or correct AniDB mapping.
- ✅ **Page auto-refresh on every page (user 10-05) — DEPLOYED v0.4.5 (web); the app needs an APK rebuild**: `ui/src/js/auto-refresh.js`. A watch/unwatch anywhere fires `starfleet:refresh-after-watch` on every open page (BroadcastChannel from api.js), and every 60 s / on returning to the tab `starfleet:auto-refresh` fires (not while hidden or typing). Backlog, list, grabs, downloads and the show page reload on it (quiet; the show page re-renders only if its data changed, scroll kept); the calendar keeps its 10 s poll; reviews, add, settings do not auto-refresh (they hold a selection or a form).

- ✅ **Syoboi runs per level, matched at episode level (user 10-05; RULEBOOK R1.22a) — DEPLOYED v0.4.4 10-05**: a Syoboi TID is one broadcast run (per cour/season, like AniList):
      229 of 263 multi-level shows stored the FIRST season's TID at show level, so the running season was never fetched. `syoboi_levels.py`: the id is held per level
      (`season_external_id`, seeded from ARM through the level's AniList id: 1,370 on the prod copy), the sync fetches every watching/planned level's TID (108 new, 3 batches,
      11 s), and nothing trusts an id for which episodes a run covers: each level is matched to the broadcasts at episode level by air date (≤3 days; one TID shared by several
      levels is split by count offset). The schedule options, the air-date gap fill and the provisional episodes all read that one mapping; a Syoboi badge per season level.
      Prod copy after: provisional episodes 137 on 42 shows (was 113/31), Syoboi options on 84 shows (was 79; Apothecary S3 now has its stations).
- ✅ **Syoboi id auto-fill + links (user 10-05) — DEPLOYED v0.4.4 10-05**: the id is seeded each Memory Alpha pass from ARM, but only through a show-level AniList id
      (74 shows have one; R1.23 puts them on seasons), so new shows rarely filled: now also through the show's latest season/part ARM maps (prod copy: 8 more, 993 total;
      the rest are films/shows ARM has no Syoboi entry for). Links: the show page links a badge only from `show_external_id.url`, empty on 984/985 Syoboi, 1,055/1,061 AniDB,
      1,687/1,712 TVmaze rows; `external_urls.fill_missing` fills every row each pass (3,465 on the prod copy; TVmaze "-1" = no show stays empty) and the page falls back to
      the service template when a row has none.
- ✅ **Schedule chooser review (user 10-05) — DEPLOYED v0.4.4 10-05**: "I thought I said all schedules … haven't seen Syoboi in the choices". Cause 1: candidates were
      rebuilt only when the ↻ button was clicked (22 shows on prod had any; Syoboi on 2 although 985 shows have a Syoboi id). Now every watching/planned anime still airing
      (103 on prod, 0.1 s) is collected in the Memory Alpha pass (`air_sources.collect_for_airing`) and a season's chosen schedule is re-applied there so it keeps following
      its source. Cause 2: Syoboi's candidates needed an AniDB episode mapping; now also by Syoboi's own numbering when it fits the TVDB season (same check as the
      provisional episodes), one option per station. Prod copy: Syoboi options on 79 shows (Goblin: AniList, Sonarr, 5 Syoboi stations). AniList stays one schedule per
      entry (one airing schedule per AniList media), recorded when read (daily refresh of watching shows).
- ✅ **Provisional episodes from Syoboi — DEPLOYED v0.4.4 10-05 (user 10-05; RULEBOOK R1.2g)** — TVDB lagged several running anime (two
      episodes aired, only the placeholder E1 on the page: couldn't open or mark E2). `provisional_episodes.py`: for tracked anime (watching/planned)
      with a Syoboi id, when Syoboi's numbering fits the latest TVDB season, the next episodes are added from its numbered broadcasts (last aired +3,
      at most 6 past TVDB's last, never past AniDB's count), flagged `episode.provisional` (new migration f8a1b2c3d4e5), dated by Syoboi, TBA; the
      Sonarr sync adopts a row when TVDB lists the same number (watched stays); skipped numbers go. Memory Alpha pass step + refresh button; "provisional"
      tag on the episode row. Prod copy: 113 episodes on 31 shows (Goblin E3–E5), all numbered, no new rulecheck violation (R1.11w +3 "for a look").
      v0.4.3 (stale reviews close themselves, divided seasons keep no list id, nginx re-resolves lcars) is tagged, CI green, NOT deployed:
      folded into v0.4.4 with the prod data steps still to run then (scripts/levels_cleanup_20261005.py, Bleach show-level "dropped", MAL 54344 removal).
- ✅ **Levels follow the episodes — DEPLOYED v0.4.2 on 2026-10-05 (user OK; snapshot lcars.db.bak-20261005-pre-0.4.2; first pass made 44 parts, 4 empty rows left: Cyberpunk S2, Ghost in the Shell S2–S4 now skipped) (RULEBOOK R1.10a)** — Kusuriya's fake "S4" (AniList
      200927 = S3 part 2) came from `ensure_fribb_season_rows` making a level per entry from Fribb's order; the rules existed only in the one-off
      rebuild. Replaced by `level_reconcile.py` (+ `level_parts.py`, shared with the manual "⤵ make part of season N" button and the
      `makeSeasonPart` mutation): in the Memory Alpha pass an entry no level places is placed by its episodes (start date in Japan time must be
      an episode's air date; no date → by count): a part of the TVDB season (leftover levels and TV entries held by special levels converted in
      place), the season's own entry when it is the only one, or a season level only (planned) when TVDB has no such episodes. Positional creation
      and the positional claim in `season_mapping` removed; rulecheck R1.10a added. Prod copy dry run: 38 parts + 1 link, 5 leftovers (Cyberpunk S2
      legit future season; JoJo S7; Ghost in the Shell S2–S4 have no TVDB series), 10 entries "left" with reasons (R.O.D swap, Yuki Yuna, JoJo…). User 10-05: cours whose dates miss but whose counts fill the season are parts too (group count rule: Dungeon S4 11+11, Yuki Yuna S2 6+6, JoJo S5 12+26) → 44 parts, 4 left (JJK 209895 TVDB has no episodes; Dragon Warrior 2229 vs 2819; Nobody's Boy Remi 2829; R.O.D swap). Bleach (dropped): its seasons 2–17 are set skipped BEFORE the deploy so the new TYBW parts come out skipped; Ghost in the Shell S2–S4 skipped too.
      Rehearsal on a fresh prod copy: no new rulecheck violations (R1.10a 6 → 3), no foreign-key breaks, second pass changes nothing; captured
      list writes: 4 new (AoT 104578 COMPLETED, Bleach 116674 PLANNING, progress 12 for 99147 and 20632) — see the report.
- ✅ **[DEPLOYED v0.4.0 10-05; cleanup script run on prod]** **Special levels re-created on every Memory Alpha pass (found 10-05) — FIXED IN DEV, not deployed (commit 794acc1; cleanup script in the ship checklist)** — 53 new rows per pass, ~3,450 a day; prod `season` is
      21,798 rows with 18,863 auto specials (healthy ≈ 4,900); one piece exists 323 times. Cause: `numbering._special_level`
      looks under the season but `_nest_in_minis_groups` moves pieces into their minis group, so every pass makes a new one.
      No external effect (no list ids, no episodes). Cleanup looks safe (14,048 extras, none with episodes/ids/scores).
      Fix + cleanup script done (prod copy: 18,988 → 5,044 rows, three further passes add nothing). Details: `AUDIT-2026-10-05.md` §1.
- ✅ **[DEPLOYED v0.4.0 10-05]** **Memory Alpha pass exceeds ops's 120 s — BOTH fixes in dev, not deployed (user 10-05: cover ourselves)**: wasted AniList calls removed (−53 s: 94 s → 41 s on one copy; on today's fresh prod copy a pass is ~78 s because a Syoboi incremental sync adds ~44 s — rate-limited batches — and AniDB ~17 s; so a real pass is ~80–100 s) and ops timeout 300 s. Was: — 53 s of a 94 s pass is `ensure_fribb_season_rows` making 26
      throttled AniList calls and then creating nothing; plus AniDB ≈ 20 s, renumber 9 s (shrinks after the cleanup above). Options
      in `AUDIT-2026-10-05.md` §2; waiting for your pick.
- ✅ **[DEPLOYED v0.4.0 10-05]** **Film mechanism built (user OK 10-05; commit e088ddc)** — each list-only AniList film level (Fribb type MOVIE, not skipped, no span) gets one
      `bonus_movie` episode (date/length from AniDB, else AniList; watched when the level is completed) so the numbering puts it between the
      seasons by date and writes the level's span; no twin level, no guessed AniDB entry. Prod copy: 3 films (Aldnoah.Zero Re+ → abs 25, the two
      Captain Harlock films → 0.1/0.2 because that show has no main episodes yet). Runs in the Memory Alpha pass before the numbering.
      First pass after deploy creates the 3 episodes (+1 watch event each for completed levels, dated that day).
- ✅ **[DEPLOYED v0.4.0 10-05]** **List sync for one entry over several seasons built (user 10-05; commit after e088ddc)** — Urusei Yatsura 1293: levels of one show sharing an
      id are one entry; progress by episode across them; status never pushed, the list's status mirrored onto the last level (my reading of
      "mirror last AniList status" — tell me if you meant something else); the 8 old id-conflict reviews close themselves. Prod copy: group of 4,
      195 episodes, matches AniList, nothing changed.
- ✅ **[DEPLOYED v0.4.0 10-05]** **Rulecheck: seven new checks built (user 10-05; commit bad53b4)** — R1.13b one level per label (55 duplicate groups before the cleanup, 0
      after), R1.13c episodes held by two levels (421, a "look at"), R1.2b decimals (0 wrong), R1.0a placeholders (0) + undated with real
      numbers (325, a "look at"), R1.10b part order (3), R1.0b air-date source (0). 19 → 26 rules checked.
- ✅ **[DONE 10-05: levels follow episodes v0.4.2 + leftover_fixes script]** **Cutover-leftover decisions recorded (user 10-05)** — A1 best guesses (parts/season ids/leave; table in
      `DECISION-cutover-leftovers-2026-10-05.md`), A2 films integral (needs your OK on creating a film episode per list-only film), A3 list
      only, Battle Angel keep both. Patch after the drip (dry run first).
- ✅ **[DEPLOYED v0.4.0, 10-05 — 'dev only' wording below is history]** **Air-date noise: 10-minute buffer — done in dev** (user 10-05): two sources < 10 min apart change nothing and open no
      review (`airdate_priority.within_tolerance`, also in the Syoboi SQL and the AniList reconcile). Confirm the 10 minutes.
- ✅ **R.O.D swap DONE on prod 10-05 (user OK; scripts/leftover_fixes_20261005.py; snapshot lcars.db.bak-20261005-pre-leftover-fixes)**: S1 ← 209 planned (26 phantom watches removed), OVA level ← 208 completed. Same script moved the A1 season-2 entries (Kankin Kuiki 182877 completed, Sekai Saikou 169579, Skip and Loafer 185657) from special levels onto TVDB S2. User: Dragon Warrior 2819 is right (Fribb's 2229 is wrong), Nobody's Boy Remi 2828 is right (2829 wrong) — nothing to change in LCARS. (old text:) **R.O.D -READ OR DIE- S1 swapped ids** — LCARS S1 (26 eps, completed) holds 208 (the OVA); Fribb right (209 = the TV series).
      Lists unaffected. Decision on ids/statuses when the review appears (`AUDIT-2026-10-05.md` §3).
- [ ] **AniDB drip** — running (v0.3.2). 10-03 07:42Z: 200/day reached 10-01 and 10-02, no ban; left 460 backlog +
      118 refreshes ≈ done 10-05/06. 200/day is the user's cap, not a known AniDB limit (bans seen at ~250 requests
      per VPN IP on 09-28, time window unknown).
- ✅ **[FIXED v0.4.0 10-05: ops timeout 300 s + pass 100 s → 41–80 s]** **Every Memory Alpha pass times out in ops** (found 09-30): `pollMemoryAlpha` exceeds ops's 120 s timeout on all
      8 passes in 4 h. LCARS finishes the pass anyway (fetches land), so ops logs a failure each time; unverified
      whether the renumber at the end of the pass completes.
- ✅ **FIXED 10-05 (user): `~/stacks/starfleet.yml` on tiny now gives lcars and ops `dns: [192.168.1.1, 1.1.1.1, 8.8.8.8]` and a wait of up to 2 min for outside DNS before they start (backup `starfleet.yml.bak-pre-dns-20261005`; the file is not in the repo). Hit again after the 10-05 13:00 reboot (empty Docker resolver list); restart cured it. The arr/media stacks are not covered (`/etc/docker/daemon.json` `dns` would cover every container, needs root).** (old text:) **Reboot DNS race on tiny** (found 10-01): after the 06:36Z reboot, lcars/ops/web started before the host's
      `/etc/resolv.conf` had a nameserver → containers got `NO EXTERNAL NAMESERVERS DEFINED`; AniList, MAL and AniDB
      all failed for 3.5 h (drip stalled, no list sync) until the user recreated the three containers 10:12Z. Will
      recur on every reboot. Options to decide: `dns:` in starfleet.yml, or Docker waiting for network-online.
      (gluetun/qbittorrent/autobrr/prowlarr-mam/port-sync show the same marker — not checked.)
- ✅ **[DEPLOYED v0.4.0, 10-05 — 'dev only' wording below is history]** **New episodes never fetched once every known episode has aired** (found 10-04, A Tale of the Secret Saint E2).
      Cause: the daily refresh took only watching + "airing" shows (a known episode with no/future date). **Built in dev,
      not deployed** (branch `dev-airing-sources`, commit 6cff5bf): a show is also due when Sonarr's stored series status
      is continuing/upcoming or an episode aired in the last 14 days (`_show_needs_episode_refresh`; RULEBOOK R1.0c).
      On the prod copy: 44 → 55 shows refreshed daily — the 11 added are exactly the stuck ones (Reacher, Star Trek SNW,
      Ramparts of Ice, Tomb Raider King, Ranma1/2, Broken Saintess, Magical Explorer, Zombie, VERTEX FORCE, Seven Knights,
      Magic Repo Man). A manual refresh already brings in everything (checked in dev: Secret Saint E2–E13 arrive).
      Not changed (ask first): the weekly Fribb reconcile (`dueForSeasonReconciliation`) has the same blind spot; planned
      shows are never refreshed daily.
- ✅ **[DEPLOYED v0.4.0, 10-05 — 'dev only' wording below is history]** **identity-mismatch check crashes** on levels with no season number, and the weekly `reconcileSeasonMapping` failed on
      the same levels (382 failures in 48 h on prod). **Both fixed in dev, not deployed** (branch `dev-airing-sources`;
      user said apply 10-04): the weekly query skips levels with no season number; the identity check now compares Fribb's
      own TVDB season number with the LCARS TVDB-season number (whole numbered seasons only; none or several Fribb entries =
      no opinion; parts and unnumbered levels not checked). On the prod copy: 1,269 checked, **1 flagged** (R.O.D -READ OR
      DIE- S1: stored AniList 208, Fribb 209 — not yet checked which is right) instead of 58 false ones. Write-up and the
      old 58: `DECISION-identity-check-2026-10-04.md`. When deployed, that one review appears; the check also stops logging a
      failure every hour.
- ✅ **[DEPLOYED v0.4.0, 10-05 — 'dev only' wording below is history]** **Dark Sonarr icon on cards** (user 10-04, screenshot): 56 tracked shows were in Sonarr with no `sonarr` link row, so
      the icon was dim. Cause: only the monthly presence sweep wrote the link (last run 09-30 06:32, before the rebuilt
      shows existed; next ~10-30), and a show added any other way never got one. **Fixed in dev, not deployed:** the hourly
      `reconcile_arr_state` (which already holds Sonarr's/Radarr's full catalog) now writes a missing link for every show
      holding the series' tvdb id (Radarr: tmdb id); existing links untouched, no `*_public_url` = no link. Prod copy:
      330 → 387 links, no Sonarr show left unlinked; calendar in the browser: 28 lit, 1 dark (a dropped show that is not in
      Sonarr — correct). Not changed: 36 shows hold a link whose TVDB id is no longer in Sonarr (stale links; `INSERT OR
      IGNORE` never updates a link).
- ✅ **[DONE on prod at the v0.4.4 deploy: scripts/levels_cleanup_20261005.py — R1.10b 3 → 0]** **Part order** — dry run 10-05: 3 out of order (Dr. STONE S4, Bookworm S1, SAKAMOTO DAYS Part 2), 2 not judgeable (Haruhi S1,
      Mahou Shoujo Ikusei Keikaku restart S1); the drip already placed spans for 14 of the 16. Data patch ready to write (renumber by span start).
- [ ] **138 width checks** (should clear as AniDB data arrives) and **131 Fribb-unmatched** (information) — re-look
      after the drip.
- ✅ List sync for one AniList entry over several TVDB seasons (Urusei Yatsura 1293) — built in dev 10-05 (see below).
- ✅ ops daily sweep dropped connection — not reproduced in 72 h of prod logs (only Memory Alpha ReadTimeouts); closed.
- [ ] **R3.7c/d/e** — two sources agreeing on a TVDB id attach it; TVDB id entry on the show page; unconfirmed TVDB
      id → yes / no / here's-the-right-one review.
- [ ] **8.8.3 / 8.8.5** — side-by-side evidence page; link provenance re-checked when Fribb later has data.
- [ ] **7.5 / R4.9–R4.10** — AniList and MAL polls as one ordered loop; fixed time block.
- ✅ **[DONE 10-05: 28 rule checks incl. the 8 new ones and R1.10a; what it finds is listed under "Rulecheck triage"]** **Expanded rulecheck / enforcement audit** (code + DB against the rulebook).
- ✅ **[DECIDED by the user 10-05 (A1, A2, A3, Battle Angel, R.O.D, Dragon Warrior 2819, Remi 2828); the unexplained counts (51/78/67, Mushoku 0.x, Haruhi spans, Urusei films) are parked: re-raise only if rulecheck shows them]** **Cutover review leftovers** (handoff §5): Haruhi 2006/2009 cours spans; 5 Urusei Yatsura films/specials with no
      level; Battle Angel OVA no TVDB movie id; 51 unmapped AniList history entries, 78 unlinked levels, 67 held back;
      Mushoku 146065 ep 0.
- [ ] **Data TUI rework.**
- [ ] **Key rotation** (Sonarr/Radarr/TMDB, MAL client_id, AniList client_secret) — at project end.

### B. Carried over from before the rebuild
- 💤 Service icons white until a hard refresh — parked (user 10-05: not appearing lately).
- [ ] Secrets out of plaintext config; AniList metadata fallback scalar-only; franchise function.
- [ ] Ideas: unified list page, Discover, Statistics, season-level external ids, Memory Alpha browse prefill +
      add confirmation, art-fetch leftovers, IMDB datasets, studio browse, direct TVDB search, livechart
      headlines, Grabs screen tweaks, Data recent-downloads page.

### C. New (user, 2026-09-30)
- [ ] **Scoring idea** — to be explained by the user.
- ✅ **[DEPLOYED v0.4.0, 10-05 — 'dev only' wording below is history]** **Schedule setter** — **built in dev, not deployed** (branch `dev-airing-sources`, commit 84907f9 + UI commit; RULEBOOK
      R1.0b). Every source's schedule for each season is stored as a candidate (Sonarr raw, TVmaze, AniDB date-only, AniList,
      animeschedule, **each Syoboi station separately**, with station names); a 🗓 button on each season card lists them
      with weekday/time slot, date range, per-episode dates, "in use" / "chosen"; "Use this" makes the season follow it
      (dates written, air_date_change recorded, AniList/animeschedule/Syoboi-rewire leave the season alone, a manual date
      still wins), "Back to automatic" releases it. Default stays earliest-wins. Migration `e5f6a7b8c9d1` (additive).
      Limits to know: (a) no streaming-service schedules (no API; livechart not scraped); (b) AniList/animeschedule candidates
      only exist after a read (the refresh button or the normal polls); (c) TVmaze candidates exist for TV shows (and any
      show whose TVmaze episodes are stored) — the refresh does not fetch TVmaze episodes for anime, as the drip's guard says;
      (d) Syoboi station names are fetched monthly from prod (ChLookup, works from the home IP, blocked by Cloudflare from
      the dev machine's VPN IP); (e) choice is per season level, not per episode.
- ✅ **[DEPLOYED v0.4.0, 10-05 — 'dev only' wording below is history]** **Refresh show data button** — **built in dev, not deployed.** The ↻ on the show page now runs `refreshShowData`: the
      metadata fetch, then TVmaze (TV) or AniDB + Syoboi (anime), every schedule listed, chosen schedules re-applied; the page
      then fetches art (the manual fetch, ignores the not-found cache) and reloads the show; a banner lists each step.
      AniDB shares the drip's state: 24 h ban back-off, 200/day cap, an anime fetched in the last 24 h is not re-asked, and
      one click fetches at most 5 anime (a franchise can have dozens of AniDB entries — Pokémon 65, Dragon Ball Z 22 — at
      4 s each); the banner says how many are left. Dev check of the daily-pass change on the 11 newly due shows (prod copy):
      +80 episode rows (the goal), +1 open review (Broken Saintess: an AniList sequel the add check could not place), no show
      or season status changed, no season rows, no list_sync_log, no captured Sonarr/AniList/MAL writes.
      **Hotel Inhumans S2 E1 (10-04), corrected:** the plain refresh already fixes it (AniList 2026-10-04T14:45Z replaces the
      stale S1 date; checked in dev). It stayed wrong only because the daily refresh skips *planned* shows (and AniList is
      the one source allowed to move its own date later). So the button is the fix for this case. The trap itself is real for
      other sources: a wrong *early* date written by Syoboi, AniDB or TVmaze still beats a correct later one, unless that
      same source corrects it or you choose a schedule for the season (R1.0b) — a watching show is not immune.
- ✅ **Ship rehearsal on a fresh prod copy (10-05 07:15 UTC):** migration applies; cleanup 21,957 → 5,045 season rows (integrity ok); two further Memory Alpha passes add nothing; Sonarr links 330 → 387; identity check 1,269 checked, 1 flagged (R.O.D); daily refresh 33 → 53 watching shows; rulecheck 37 violations (all pre-existing kinds). Full suite 1,746 passed.
- ✅ **[DEPLOYED v0.4.1, 10-05]** **Review-page noise (user 10-05): built in dev, not yet shipped (release v0.4.1).** Prod had **353 open reviews; 348 were noise or obsolete**:
      173 AniList width checks (measured wrongly; none actionable → now `lcars rulecheck` R1.11w, 59 real count differences), 134 "unmatched"
      seasons (a TVDB season with no AniList entry is normal), 23 air-date items (AniList "a delay past the current" on downloaded episodes —
      mostly 5–10 min — and animeschedule changes that were already applied), 9 transient "Could not connect"/"HTTP 429" failures, 4 Urusei id
      conflicts, 5 singles (numbering scheme, show-level no-AniList-match, MAL progress clamp, animeschedule ambiguity). **Kept (5, real):** the
      R.O.D identity review, Broken Saintess add check, two AniList-mapping problems (a split-cour season; a schedule 5,451 days off), one MAL lower-progress.
      Sources changed so they do not return (list in the commit); the 10-minute buffer is now inclusive; identical consecutive review values no longer
      grow a chain. Cleanup: `scripts/cleanup_noise_reviews_20261005.py` (dry run default; prod copy 353 → 5). To ship: tag v0.4.1, deploy, THEN run the
      script (else the old code reopens some), snapshot first.
- 🚢 **SHIP CHECKLIST — v0.4.0 (= branch `dev-airing-sources` at 7405595), TAGGED + CI GREEN, NOT DEPLOYED (user 10-05: ship once the drip has hit 200 today; make sure it restarts tomorrow)**
      0. Before: today's drip count ≥ 200 (`anidb_episode` distinct anime with `fetched_at` ≥ today); labelled DB snapshot on tiny.
      1. **DONE 10-05: tagged `v0.4.0` on commit 7405595 (pushed, never move it); CI run 37282087623 = success (image `0.4.0` / `0.4.0-web` on GHCR).**
         Remaining: deploy per memory `starfleet-deploy-process`
         (`/-web/!` sed pair, all three image lines). Migration `e5f6a7b8c9d1` runs on start; check `alembic_version`.
      2. **Delete the duplicate special levels** (after the new image runs, so they are not re-made): labelled snapshot, then
         `scripts/cleanup_duplicate_special_levels_20261005.py` — dry run first, then `--apply`
         (`curl -fsSL …/scripts/….py | docker exec -i lcars python - [--apply]`). Expect ≈ 21,800 → ≈ 5,100 season rows.
         Then wait for one Memory Alpha pass (~20 min) and check the season count did NOT grow.
      3. **The drip must restart tomorrow:** the cap counts `fetched_at` by UTC date from the DB (a restart does not reset it) and the
         ban back-off is in-process (a restart clears it). Check after 00:00 UTC that fetches resume; leftover = refreshes of
         watching/planned shows (weekly), plus whatever backlog is left.
      4. ops timeout for Memory Alpha is now 300 s; check the proxy/read timeout in front of LCARS covers the refresh button (~1 min).
      5. **R.O.D -READ OR DIE- fix is DATA on PROD, not code** (user 10-05: remember at ship): after deploy the identity review
         appears (S1 stores 208, Fribb says 209). Swap through the app (season mapping edit): TVDB S1 ← 209 (The TV, 26 eps),
         the OVA 208 → its special level; then decide the statuses with the user (LCARS holds 26 phantom watched episodes; their
         lists say only the OVA was watched; AniList/MAL 209 are plan-to-watch). No external write is needed if statuses stay as lists have them.
      6. Refresh the stuck watching shows once (Secret Saint etc.); expect +80 episode rows and one new add-check review (Broken Saintess).
      7. After the first hourly reconcile: ~57 Sonarr links appear (dark icons light up); the hourly sweep stops logging the identity
         crash and the weekly `reconcileSeasonMapping` failures; air-date reviews for 1-minute differences stop.
      8. Confirm after a day: no new special levels, Memory Alpha pass < 120 s with no ReadTimeout in ops logs.
      9. Then (needs the drip finished): re-run the part-order dry run and write the patch; the cutover-leftover decisions
         (`DECISION-cutover-leftovers-2026-10-05.md`).

Full build history archived to `~/repos/starfleet-archive`.

> Full account of the 2026-09-23 session (changes, data operations, backups,
> plan violations, open items): **HANDOFF-2026-09-23.md**.
> 2026-09-25/26 bug-fix session: plan, decisions and findings in
> **BUGFIX-PLAN-2026-09-25.md**.

## Bug-fix session 2026-09-25/26 — v0.2.63 → v0.2.68

- [x] **Completed shows flipped completed <-> paused every hour** since 09-19
      (29 shows, ~1,300 pushes to each list): the Sonarr reconcile paused any
      unmonitored show. v0.2.63, verified in prod (0 paused since).
- [x] **Syoboi sync never ran** (duplicate TID -> 400 on every batch). v0.2.65,
      verified (programmes stored, 0 failures).
- [x] **Unaired placeholders marked watched** (undated TBA episodes of an
      unstarted season). v0.2.65 guard; 8 episodes cleared; Kaiju S3 AniList
      progress 0 (verified).
- [x] **LCARS as the hub** (user rule): `list_baseline` decides which side
      changed — list edit -> LCARS -> other list; LCARS change -> pushed,
      retried on failure. Only tracked shows sync; a shared entry is excluded.
      First run seeded both services with no writes. v0.2.67, verified quiet.
- [x] **Auto-created seasons never pushed** (`season.list_sync`); new-season
      status: future -> planned, else previous season watched/paused -> paused,
      dropped -> dropped. 193 seasons set LCARS-only.
- [x] **Completing a season pushes MAL progress** = the MAL entry's own count.
- [x] Haruhi (2009) duplicate stub purged; Haruhi S1 -> MAL 849.
- [x] 28 LCARS/AniList disagreements settled per the user; Blue Box and The
      Dangers in My Heart re-monitored and resumed.
- [x] 82 orphaned FK rows deleted (0 left).
- [x] **Transaction leak safety net** (v0.2.68/69): middleware commits what a
      request left open and logs the GraphQL operation.
- [x] **Leaking resolver found: `pollMemoryAlpha`** — helpers committed only when rows
      changed; a 0-row UPDATE still holds the write lock. v0.2.70, verified in prod
      (0 warnings over 3 cycles incl. a full dataset refresh).
- [ ] **Metadata fetch FOREIGN KEY failure** on s-qpb26y (09-26 00:02).
- [x] **Sonarr monitoring never changes LCARS** (v0.2.69, user's final call).
- [x] 30 loop-leftover seasons aligned to AniList; review queue cleared (45).
- [ ] **Air dates: Sonarr is a weak source** (Syoboi can push a correct earlier
      Sonarr date 2 days later). User: keep for now, decide next season.
- [ ] **First hourly sweep after a restart blocks LCARS** (long sync inside a
      request handler). Re-check after v0.2.68; a job-timing change needs its
      own review.
- [ ] **Icons / planner banners white until hard refresh** — still not
      reproduced (below).
- [ ] MAL-side disagreements couldn't be inspected (no token read); MAL now
      follows LCARS through the hub.
- [ ] One AniList HTTP 429 seen in lcars logs on 09-25; watch.


## 2026-09-26 afternoon — v0.2.70 + sequel/stub rule

- [x] **Failed steps leave no partial writes** (`db.undo_on_error`): metadata `_guarded`
      and every `pollMemoryAlpha` step. v0.2.70.
- [x] **User rules recorded** (memory: episode-atomic-core-rule, auto-season-push-rule-by-parent):
      sequel on the same TVDB series = season; movie sits inside the show; a different
      TVDB series = separate show linked as a franchise (franchise layer: later).
      Parent watching/completed/planned/rewatching -> planned + pushed + Sonarr future
      episodes; paused/dropped -> paused/dropped stub, never pushed. Direct
      SEQUEL/PREQUEL only.
- [x] **Stubs only for SEQUEL/PREQUEL, never for an id that is already a season** —
      branch `feat/sequel-only-stubs` (adf4951), full suite green, **not released**.
- [x] Relation types re-read from AniList (read-only): the 1,446 untyped were 443
      sequel/prequel, 240 side story, 142 character (the cross-franchise links), ...
- [ ] **User: review page** https://claude.ai/artifact/Vo9guuPKsXKDX3jnbWy3dF —
      369 sequel parents: true status, "don't add" per sequel, notes.
- [ ] Lists for approval: 207 duplicate stubs of existing seasons (remove); 860
      stubs with no sequel/prequel link (keep, unlink).
- [ ] Auto-add rule (planned + lists + Sonarr future) — build behind an off switch;
      enable only after the review + an approved simulation. The 193 LCARS-only
      seasons get the rule then (user: apply).
- [ ] First-sweep blocking: re-check now that v0.2.70 is live.
- [ ] Sasaki and Peeps FK failure — may be explained by the partial-write fix; watch
      the nightly refresh for a new `metadata_fetch` review.

---

## ❌ NOT FIXED: service icons render white/blank until hard refresh (+ planner banners)

Declared fixed three times by Claude sessions, never verified in a browser, still
broken (user report 2026-09-23). Do NOT mark this done without the user seeing
it fixed.

- What was tried: v0.2.52 nginx `no-cache` on `/ui/css/` (live, verified by
  headers, but it didn't fix the icons); `show.js` TVmaze white-background badge.
- What's known: the icons are inline SVGs / data-URI images built by JS
  (`ui/src/js/icons.js`, `buildSvcStrip`), so CSS caching can't be the cause.
  Server logs show every asset 200/304. Planner banners are CSS
  `--banner-url` backgrounds loaded straight from the AniList/TVDB CDNs, with no
  error handling or retry, so a failed load stays blank until a reload.
  `icons.js` also reuses fixed SVG gradient ids (`tg`, `tmg`).
- Next step: reproduce first. Either browser tools (`/chrome`), or the user's
  console + network screenshot (reload normally, filter Img) at the moment it
  happens, noting which icons are white. Then fix, and have the user confirm.

## Season/episode identity from absolute order — shipped v0.2.56-v0.2.58 (2026-09-23)

User-reported: Slime S04E24 and S05E24 both "airing next Friday".
Root cause: once LCARS subdivided TVDB S2 into LCARS S2+S3, every
later LCARS season sat one ahead of TVDB's, and several writers
still treated "LCARS season N" as "TVDB season N".

- [x] **Traced on prod.** `reconcile_season` (Fribb by tvdb+LCARS
      season number) moved the 2026 season's AniList id (182205) onto
      LCARS S4 (2024) on 09-12. That change was then accepted by the 09-15
      bulk cleanup, and the correct 09-21 identity_mismatch flag was
      resolved as "Fribb wrong". The AniList air-date pass then wrote
      the 2026 schedule over S4. Memory Alpha's arbiter locked S4 to
      AniDB 18884 (2026) against those AniList-written dates.
      local_audit put Sonarr S04 files on LCARS S4. Sonarr coordinates
      were never captured because a merged-away "S2 Part 2" stub still
      held the tvdb id, forcing the multi-show path.
- [x] **Code (v0.2.55 failed CI on lint; v0.2.56 shipped):** new
      `sonarr_match` (Sonarr<->LCARS by captured coords, then absolute
      number; captures coords; merge losers aren't siblings), used by
      `_fetch_sonarr`, local_audit, and the availability fallback.
      Anime-Lists now gets real TVDB coords, and the arbiter only locks
      against independent dates. `reconcile_season` derives from Memory
      Alpha (AniDB -> Fribb). AniList air dates get a >60-day drift guard
      vs Sonarr raw dates. Derived episode ids follow their source.
      11 new tests, 1396 total passing.
- [x] **Data repaired** (`scripts/repair_absolute_identity.py`, dry-run
      first, snapshot `lcars.db.bak-20260923-pre-v0.2.56`): Slime,
      Re:ZERO, SPY x FAMILY, Mushoku Tensei, Dr. STONE, Mob Psycho,
      Bakemonogatari. Verified live: only S5E24 airs 2026-09-25, and all
      scheduled passes re-run against Slime changed nothing.
- [x] **Slime S4 score confirmed 14.5 by the user** (2026-09-23). It was
      applied on 09-22 while S4 was mislinked; kept as-is.
- [x] **AniList token restored 2026-09-23** (pulled 09-21 for the score/DB
      alignment work). The value came from `lcars.ini.bak-20260921-before-token-
      pull`; the pre-restore config was saved as `...bak-20260923-before-anilist-
      token-restore`, and only that one line changed. The token is valid until
      2027-08-11. lcars and ops were restarted; the first drift run flagged
      7 seasons (open `score` reviews), including Slime S4 (LCARS 14.5 vs
      AniList 156822 = 57 -> 11.4).
- [x] **Slime S4 score pushed**: `setSeasonScore(14.5)`, read back from
      both services (AniList 156822 = 72, MAL 53580 = 7), review resolved.
- [x] **`pollScoreSync` fixed (v0.2.57)**: it returned camelCase keys, but
      `convert_names_case` expects snake_case. Verified live: 1201 AniList /
      1190 MAL seasons checked.
- [x] **ops startup race fixed (v0.2.57)**: ops waits for LCARS before
      starting its loops, and the availability loop retries in 60s instead
      of 3600s. Verified on two deploys: 0 connection errors, loops start
      about 2s after LCARS answers.
- [x] **Remaining candidates repaired (v0.2.57)**: every tracked anime show
      with uncaptured or renumbered Sonarr coords (5 more). Bookworm S4E21-24
      are now date-confirmed; its TVDB-only 08-28 slot makes no claim.
      Chitose S2 moved to AniDB 20240 (manual link beats the stale list).
      Fire Force S4 and Black Lagoon S2 got their first mappings. Tonbo! S2
      kept its date-confirmed lock (the repair now respects independently
      confirmed locks).
- [x] **v0.2.57 incident, fixed in v0.2.58**: the first weekly reconcile
      after the deploy changed 70 seasons (cleared 47 links, e.g. JoJo,
      Pokémon, NieR). This was a latent ping-pong: episode-less seasons are
      created by Fribb *position* but were reconciled by TVDB season number.
      Simulated on the pre-deploy DB, the pre-session code does the same to
      66 of the 70 (and 51 clears across all 399 seasons). v0.2.58: episode-
      less seasons follow Fribb's order, the TVDB lookup is strict (no "one
      entry -> every season" shortcut), and "no candidate" never clears a
      link. All 70 were restored from `lcars.db.bak-20260923-pre-v0.2.57`
      (140 reviews resolved with a note), verified equal to the snapshot.
      The same simulation on the final code: 0 cleared, 20 filled,
      2 replaced (Food Wars! re-aligned to the positional layout).
- [x] **Bad-link purge (user-directed, 2026-09-23)**, LCARS-local only,
      snapshot `lcars.db.bak-20260923-pre-badlink-purge`, 0 new FK violations:
      - Chitose: empty duplicate S3 (`z-kzcj41`) removed; S2 range 14->15.
      - Himekishi (`s-eecj4c`): all 73 episodes were Maria-sama ga Miteru
        (TVDB 84025). Purged, plus S2-S4, the Maria-sama TVmaze id/posters,
        and TMDB 42468 -> 284563. A Sonarr fetch now brings 1 real (TBA)
        episode.
      - The Guy She Was Interested In (`s-xfq0e3`, anime starting 2027-01):
        9 episodes of a 2021-2024 TV show purged, plus seasons
        2021/2022/2024.
      - Ramparts of Ice "S2" (`s-n8qcjp`, Sonarr link The Lost Tomb 2):
        whole show purged locally. NOT via confirmHardDelete, which would
        have deleted AniList 186497 — the user's real, completed Ramparts
        S1 (`s-vkqrm9`, intact with 15 episodes).
      - Sonarr checked: neither junk series is in it.
- [x] **Maria-sama ga Miteru removed everywhere (user: "not something I
      watched")**: AniList 444/1729/3750 and MAL 444/1729/3750 deleted. Found
      by title on AniList, and on MAL by a full-list title scan (1505
      entries); the id set was cross-checked via Fribb (TVDB 84025 plus the
      AniDB title matches, which adds 158, never on either list). Re-read
      after: 0 left on AniList (1501), MAL (1502) and in LCARS. Only AniDB/
      TVmaze *reference caches* keyed by those ids remain (dataset data, not
      tracked content).
- [x] **Review inbox audit (2026-09-23)**: 22 fribb season reviews verified
      correct (fills/realignments under the positional model) and resolved.
      The stale Ramparts conflict was resolved too (claimant purged). 14 left,
      all needing a decision.
- [x] **Monthly tier cadence persisted (v0.2.60)**: `ops_tier_checkpoint`
      + `opsTierDue`/`markOpsTierCompleted`. Ops checks hourly and runs only
      when a month has passed. Verified on the v0.2.60 deploy: the monthly
      tier logged "processed 0" and no seasons were re-reconciled.
- [x] **MAL mirrors AniList in `setSeasonMapping` (v0.2.59/60)**: an absent
      or unchanged (pre-filled) MAL id follows Fribb's pairing for the AniList
      id; a deliberately changed one is kept.
- [x] **Fribb index caches id-reuse bug (v0.2.60)**: v0.2.59's CI failed on
      a flaky test that got the previous test's index; the caches were keyed
      on id(dataset) alone. Now `is`-checked.
- [x] **MAL half-step drift false positive (v0.2.61)**: 13.0 is 6.5 on MAL,
      so 6 and 7 both count as consistent.
- [x] **Review queue cleared** (`scripts/fix_review_queue_20260923.py`,
      dry-run reviewed twice, snapshot `...pre-queue-fix`): Tantei junk show
      and Aristocrat S4/S5 purged locally; 14 split MAL ids corrected, with
      the 20 affected MAL entries re-mirrored from their AniList entries;
      8 drifted scores took AniList's values; Milky Holmes (4 AniList + 4
      MAL) removed. Verified: 0 split identities, AniList drift check 0
      flagged, real Tantei intact.
- [ ] **Seasons tracked in LCARS but absent from the user's AniList** (no
      MAL mirror pushed): Nisekoi S2, SNAFU S2, DanMachi S5, Made in Abyss
      S2, Natsume S2, SAO S4, Returner S2. Bungo S3's MAL 38003 still says
      "watching" (pushed by S2 before the fix) and has no AniList counterpart.
- [x] **Milky Holmes deleted everywhere (user: "delete all")**, in the order
      Sonarr -> LCARS -> lists: Sonarr series + 12 files (31.8 GB), 6 LCARS
      shows (s-66qdd1 + 5 relation stubs), 4 AniList + 4 MAL entries. Proven
      by running reconcileArrState afterwards: 0 shows created; nothing back
      on either list.
- [x] **Ore to Yuu-nii! fixed**: the wrong Sonarr series (My Sky: Detective
      Story, 458465) was deleted; relinked to TVDB 473179 (Me and Older
      Brother Yu) via amendShowArrLink, now added and monitored in Sonarr;
      TMDB 280953 and IMDb tt28863748 (My Sky's) removed; AniDB 7705 -> 19949.
      2 junk episodes purged; review resolved.
- [x] **ROOT CAUSE fixed (v0.2.62): no more title-guessed Sonarr links.**
      addShowWithArr took Sonarr's first title-search hit when no tvdbId was
      given. Now the tvdb id comes from the input or the ID crosswalk; with
      neither, the show is added unlinked and a `tvdb_link` review opens.
      The catalog presence sweep writes a Sonarr/Radarr link only on a
      tvdbId/tmdbId match.
- [x] **Kaketa Tsuki no Mercedes + Majutsu wo Kiwamete... fixed (same as Ore
      to Yuu-nii)**: wrong Sonarr series deleted (Maria Mercedes, My Next Life as
      a Villainess; 0 files each); 82 + 43 junk episodes purged; every wrong id
      removed; Majutsu S1 -> AniList 202503 / MAL 62978 and its Hamefura S2 row
      removed; wrong-series posters cleared. Both stay without a TVDB link until
      one exists (2027 shows). Import sweep afterwards: 0 shows created.
- [x] **Missing seasons pushed per the user's rules (2026-09-23)**: 109 added
      via setSeasonStatus (LCARS status + AniList + MAL): 92 PLANNING, 9
      PAUSED, 7 COMPLETED (all episodes), 1 DROPPED. 93 deliberately skipped:
      86 auto-created "dropped" sequels, 4 unreleased seasons of dropped shows,
      Railgun S3, DanMachi S4/S5. Verified by re-reading both lists: all 109
      present with the right status; 0 push failures.
- [ ] **setSeasonStatus(completed) doesn't push MAL episode progress**: MAL
      doesn't auto-fill it (AniList does), so completed seasons showed 0/N on
      MAL. The 7 from today were fixed by hand; the push needs
      `num_watched_episodes` = the episode count when completing.
- [ ] **Sonarr "TBA" placeholder episodes get marked watched**: 6 unreleased
      S2s each had a single 2x1 TBA placeholder, dated to the S1 premiere and
      state=watched, which made them look watched and completed. Find what
      marks them (a completion sweep or mark-show-watched?) and exclude
      unaired/TBA episodes.
- [ ] **Auto-created seasons copy the show's status** (`inherit_season_status`
      in `ensure_fribb_season_rows`, 09-21): a show dropped at S1 got S2..Sn
      as "dropped". New seasons should start PLANNED; the user decides from there.
- [ ] **First hourly tick after every restart blocks LCARS for tens of
      seconds** (ops read timeouts at 10:36 and 12:33): the long sync pass
      runs inside a request handler. Same class as the reopened app-stall
      item.
- [ ] 82 pre-existing FK violations in prod (40 orphaned `watch_event`, 39
      `episode_external_id`, 2 `show_external_id`, 1
      `episode_numbering_mapping`): orphans from earlier deletes, untouched.
- [ ] **Watch: ops hourly tier timed out once** right after the v0.2.58
      deploy (10s client read timeout; LCARS held a write transaction for
      30s+, then recovered). Now that ops no longer sleeps an hour after
      starting, the heavy first tick runs cold. Next hourly result is being
      checked.

## Global search box: Escape / esc doesn't close it — shipped v0.2.57 (2026-09-23)

- [x] `.search-overlay[hidden] { display: none; }` (the author `display:
      flex` rule beat the UA `[hidden]` rule). Escape now closes from
      anywhere while open, and the "esc" chip is a real close button. Verified
      in headless Firefox against the real `search.js`/`main.css`: all four
      close paths (Escape in the input, Escape elsewhere, the chip, the
      backdrop) fail on the old code and pass on the new.

## Special/OVA/bonus-movie cover art (2026-09-22) — its icon claim is WRONG

> ⚠️ The "root cause and fix already shipped as v0.2.52" claim below was false:
> the icons are still broken (2026-09-23). See the NOT FIXED item at the top.

User-reported, recurring: AniDB/Syoboi/TVmaze icons render blank/white
on first load, fixed by a hard refresh. Root cause and fix already
shipped as v0.2.52 (nginx's `/ui/css/` location was excluded from
`Cache-Control: no-cache` — see that section below). Two more pieces
found/added on top of that same investigation:

- [x] **A second, genuinely separate bug on the show page specifically**:
      `show.js`'s `buildExtBadge` set every SVG-icon service's badge
      background to `none`, but TVmaze's logo (like on calendar/browse/
      planner, where `.svc-tvmaze { background: #fff }` already handles
      it) needs a white backing to render correctly — the show page's
      badge renderer is a separate code path that never got the same
      treatment. Fixed with a `WHITE_BG_ICON_SVCS` set.
- [x] **New feature, user's own scope choice**: a distinct cover art
      shared across all of a show's SPECIAL/OVA/BONUS_MOVIE episodes
      (not per-individual-episode) — `art_asset.episode_kind` column
      (migration `df50e70a1faa`), reusing the existing `EpisodeKind`
      enum. Manual-only (no automated fetch source has a concept of
      "the cover for specials"): `addManualArtUrl` gains an optional
      `episodeKind` arg; new `Show.specialPosterUrl`/`ovaPosterUrl`/
      `bonusMoviePosterUrl` fields (live art_asset lookup, no
      denormalised column needed since nothing automated ever writes
      these). UI entry point: clicking a special/OVA/bonus-movie
      episode's own poster on the show page (`renderSpecialCard`) opens
      the art picker scoped to that kind; calendar/planner/backlog
      cards for those episodes also prefer the override over the
      regular show poster when one's set.
      **Found and fixed 3 real bugs in the existing episode_kind-blind
      code while adding the column**: `select_asset`/`deselect_asset`
      would have wrongly written/cleared the *regular* show.poster_url
      column when selecting/deselecting a special-kind asset;
      `auto_select_best`'s GROUP BY didn't include episode_kind, so it
      could have picked a manually-added special-kind candidate as the
      "best" for the regular slot; `update_art_negative_cache` would
      have treated a selected special-kind poster as satisfying the
      *regular* poster check. All three fixed and covered by new tests
      before they could ever fire in practice (specials are always
      selected immediately on insert today, but the queries were
      structurally wrong regardless).
- [x] New tests: `test_art.py`'s `TestEpisodeKindArtIsolatedFromRegular`
      (8 cases covering all three bugs above), 2 new GraphQL-level
      tests in `test_server.py`. Full suite green. Not yet tagged/
      deployed.

## Air-date priority redesign — shipped v0.2.51 (2026-09-22)

Found live: "The World Is Dancing" episode 13 showed a future Thursday
date (`air_date_source='syoboi'`) despite already being downloaded and
watched — real broadcast was a Monday release AniDB tracked but
Syoboi's TV-channel feed never covered at all.

- [x] **Root cause**: the old fixed hierarchy (`manual > syoboi >
      animeschedule > anilist > sonarr > anidb > tvmaze`,
      `airdate_priority.py`) meant a higher-ranked source's date always
      won, even tracking a genuinely different, later broadcast than a
      lower-ranked source's already-correct one — and `syoboi.py`'s own
      `rewire_airdates` had a bug (`or src == "syoboi"`) permanently
      excluding already-syoboi-sourced episodes from ever being
      re-evaluated at all, even when Syoboi's own underlying calendar
      data changed. `metadata.py`'s AniList reconciliation also
      hard-coded its own independent copy of the priority tuple,
      written a day *before* `airdate_priority.py` even existed and
      never migrated to use it.
- [x] **New rule** (`airdate_priority.should_apply()`, replacing the
      whole rank system): `manual` absolute; same source re-asserting a
      changed value always applies (a genuine reschedule can move
      either direction); a *different* source only wins by proposing an
      *earlier* real date — this is what makes a later cross-source
      date impossible to mistake for an "update." One exception:
      `sonarr`'s own raw date is a placeholder, not a real competing
      broadcast, so any curated source may correct it either direction
      (preserves the existing, valuable "AniList corrects an obviously
      wrong Sonarr seed date" behavior).
- [x] **All three writers migrated**: `metadata.py`'s AniList
      reconciliation, `animeschedule.py`'s RSS sweep, and
      `syoboi.py`'s `rewire_airdates` (whose bulk-SQL WHERE clause now
      shares one `_rewire_condition()` fragment across its preview/
      audit/update queries instead of three independently-maintained
      copies — the exact kind of drift that caused the original bug).
      The narrow "already-downloaded + a later cross-source date"
      `pending_review` flag (from the "Draw This, Then Die!" incident)
      is kept as a visibility case on top of the general silent rule,
      generalized from Sonarr-specific to any source.
- [x] **The live episode itself fixed manually** (`setEpisodeAirDate`,
      now `source='manual'`) using AniDB's real date, confirmed against
      the actual watch timestamp.
- [x] **New tests**: `test_airdate_priority.py` (unit tests for every
      branch of `should_apply()`, plus a parametrized parity test
      between `should_apply()` and `rewire_airdates()`'s SQL condition
      across every case — guards against the two ever silently
      disagreeing again, the same failure mode that caused the original
      bug). Two `test_server.py` tests rewritten to reflect the new
      earlier-wins-cross-source behavior (the old ones asserted the
      obsolete fixed-hierarchy behavior the redesign replaces). Full
      suite green. Tagged/deployed as v0.2.51.

---

## amendShowArrLink resolver never worked — shipped v0.2.51 (2026-09-22)

User-caught live: correcting a wrong TVDB ID always failed with
"unexpected keyword argument 'show_id'". `resolve_amend_show_arr_link`'s
parameters were camelCase (`showId`/`newExternalId`/`deleteFiles`), but
the server converts GraphQL's camelCase args to snake_case before
calling every resolver (every sibling resolver in the file uses
snake_case params) — this one didn't, so it crashed on every real call.
No test existed at any level to catch it. Renamed the parameters and
added a GraphQL-level test exercising the full correct-a-wrong-tvdb-id
flow (validate → delete old Sonarr entry → add correct one → update
LCARS link). Confirmed live post-deploy: a dry no-op call now returns a
clean error response instead of the old crash.

---

## CSS never revalidated — nginx location precedence excluded it from no-cache — shipped v0.2.52 (2026-09-22)

Real, repeatedly-reported bug (first symptom seen: AniDB/Syoboi/TVmaze
service badge icons rendering blank/white until a hard refresh — root
cause of the OTHER two icon findings above too). `ui/nginx.conf`'s
`/ui/css/` location (added only so the unauthenticated login page could
load its own stylesheet) is a more specific prefix than the protected
`/ui/` block, so nginx matched it for every CSS request site-wide — and
it never set `Cache-Control`, so browsers applied their own long-lived
heuristic caching to `main.css` instead of revalidating on every load
like JS/HTML under `/ui/` already do. Combined with a real prior gap (a
2026-09-12 commit added the `--svc-anidb`/`--svc-syoboi`/`--svc-tvmaze`
variables without bumping `main.css`'s cache-bust version), any browser
that cached the stylesheet before that fix landed could stay stuck on
broken icon styling indefinitely.

- [x] `/ui/css/` now sets `Cache-Control: no-cache`, matching the rest
      of the app — structural fix, prevents this class of bug even if a
      future CSS-only change forgets to bump the version again.
- [x] `main.css`'s version bumped (v20→v21) everywhere, forcing an
      immediate fresh fetch for everyone currently affected.
- [x] Confirmed live post-deploy: `curl -I` against `main.css` on
      `tiny` shows `Cache-Control: no-cache`.

---

## Art-fetch negative cache + staged throttle — shipped v0.2.49 (2026-09-22)

Closes the deferred item found 2026-09-20 while diagnosing the v0.2.38/
v0.2.39 freeze incident (`art-fetch-negative-cache-and-throttle-plan` in
Claude memory) — `autoFetchArt`'s trigger had been commented out ever
since, disabled rather than fixed. All three of the user's own deferred
design points, built together:

- [x] **Negative caching.** New `show.poster_art_not_found_at`/
      `banner_art_not_found_at` columns (migration `45c08e4d9cff`),
      stamped/cleared by `metadata.update_art_negative_cache` — self-
      healing, checked after every art fetch (full or staged) against
      whether a selected show-level (season_id IS NULL) asset of that
      kind currently exists (the same slot `renderHero`/`renderBanner`
      actually display). The web client's automatic per-page-load
      trigger checks these before firing; a manual re-fetch always runs
      regardless — the cache only suppresses the automatic path.
      - **Add art manually via URL** — new `addManualArtUrl` mutation
        (source='manual', outranking every real source so it's what
        gets auto-selected), a small form in the art-picker dialog.
      - **Delete a stored art candidate** — new `deleteArtAsset`
        mutation + `art.delete_asset`, a 🗑 per card in the dialog.
        Deliberately doesn't touch the negative cache — a manual
        re-fetch afterward just finds it again if it's still there.
- [x] **Staged/throttled auto-fetch.** `fetch_show_art` split into
      `fetch_show_art_for_seasons` (per-season AniList only, the actual
      2.1s/call-throttled cost) and `fetch_show_art_show_level`
      (TVDB/TVmaze/TMDB/MAL + AniList show-level fallback, each a single
      call regardless of season count). `show.js`'s `autoFetchArt` now
      stages these: current/highest AniList season immediately → after
      a ~1.5s beat, show-level cascade → after another beat, other
      seasons still missing their own art. No per-season "hidden/
      collapsed" UI concept exists in this codebase to gate stage 3 on
      (checked, not invented for this) — it gates on "missing art"
      alone.
- [x] **Manual-pull priority.** True mid-flight preemption of the
      shared process-wide AniList throttle isn't practical without a
      real priority queue — not built. Practical version instead:
      `anilist_client.manual_priority()`/`manual_request_pending()` —
      the manual "Fetch Art from Sources" resolver wraps itself in the
      former; the two staged/background mutations check the latter
      first and skip their turn entirely (retried a few seconds later
      on the next stage timer) rather than contend for the same slot.
- [x] **30 new tests** (`test_art.py`'s `TestDeleteAsset`,
      `test_art_fetch_staging.py`, 5 new GraphQL-level cases in
      `test_server.py`) plus the full existing suite green. Tagged/
      deployed as v0.2.49 (2026-09-22) — migration `45c08e4d9cff`
      applied cleanly on `tiny`, all 4 new mutations confirmed resolving
      live.

---

## Sequel detection gap for id-blind auto-created shows — shipped v0.2.49 (2026-09-22)

Prompted by the user's Jellyseerr use (adds film and occasionally TV/anime
directly to Sonarr/Radarr, bypassing LCARS's own Add flow entirely) —
raised the question of whether anything added that way could slip past
sequel detection when LCARS picks it up.

- [x] **Confirmed a real gap, then closed it.** The three id-blind
      auto-create paths (Sonarr `SeriesAdd` webhook, Radarr `MovieAdded`
      webhook, `reconcileArrState`'s untracked-show discovery) all call
      `shows.create_show` with only a `tvdb_id`/`tmdb_id` — no
      `anilist_id`, since nothing in a webhook payload or an arr catalog
      entry carries one. `create_show`'s own pre-insert
      `find_sequel_parent` call was therefore structurally blind on
      tiers 1/3 (the ones that actually catch an anime sequel) for these
      paths — only tier 2 (TVDB/Wikidata franchise collision) could ever
      fire. Not a `skipSequelCheck`-style bypass — a data-starvation
      problem.
- [x] **New `shows.flag_possible_sequel(conn, show_id)`** — called right
      after `create_show` returns from all three auto-create sites
      (`availability.py`'s `_handle_sonarr_series_add`, `local_audit.py`'s
      `_create_from_untracked_entry`; not wired into the Radarr movie
      path, which never resolves an `anilist_id` at all per its own
      docstring, so the check can only ever be a no-op there). By the
      time `create_show` returns, its own inline
      `metadata.fetch_and_populate` has already resolved the show's
      `anilist_id` (for an anime show) and written its AniList relations
      to `show_relation` — this re-runs `find_sequel_parent` now that
      that data exists and opens a `pending_review`
      (`possible_sequel_of:<parent_show_id>`) instead of raising, since
      there's no human in this loop to answer `SequelDetectedError`.
      Deduped the same way `_propose_sequel_seasons` already dedupes
      (skip if an unresolved review for this exact field exists; don't
      reopen after a human resolved it with the same value).
- [x] **Deliberately never touches the interactive Add/Browse path** —
      that path already catches this pre-insert via tier 3 (a live
      AniList query, since no local `anilist_id` exists yet to make it
      self-match), or the user explicitly chose "it isn't a sequel" via
      `skipSequelCheck`. Re-running this check unconditionally from
      inside the shared `_fetch_anilist` pipeline (the first design
      considered) would have re-flagged that exact decision through the
      review queue — the same "loops back to the same dialog" bug
      `skipSequelCheck` itself was built to fix, just via a side door.
      Hooking into the three auto-create call sites directly instead of
      the shared fetch pipeline avoids that collision entirely.
- [x] **5 new tests** (`test_flag_possible_sequel.py`) plus full existing
      suite (`test_availability.py`, `test_local_audit.py`,
      `test_sequel_detection.py`, `test_propose_sequel_seasons.py`,
      `test_show_backfill.py`) green. Tagged/deployed as v0.2.49
      (2026-09-22).

## Production freeze incident — fixed in v0.2.38-v0.2.40 (2026-09-20)

Real outages, not theoretical. Three fixes shipped same-night:

- [x] **v0.2.38**: `anilist_reconcile`/`mal_reconcile` were writing
      `show.status` directly instead of `season.status`, with no guard
      against real unwatched aired episodes — caused a live multi-week
      status oscillation for two shows. Fixed to write season-level and
      defer to the existing single-authority derivation.
- [x] **v0.2.39**: the v0.2.38 fix itself had a bug — `_recompute_show_
      status` did its own AniList+MAL push on top of the reconcile's
      already-correct one-directional onward push, doubling/tripling
      throttled AniList calls (2.1s/call, synchronous, process-wide) on
      the first run after the fix landed, when a backlog of shows
      corrected all at once. Froze the whole server for minutes; fixed by
      skipping the redundant push from that one call site.
- [x] **v0.2.40**: separately, `show.js`'s automatic art/synopsis
      auto-fetch on every page load (no memory of "already tried, found
      nothing") froze the server again — a messy stub show with many
      AniList-linked seasons and missing synopses paid the full external-
      fetch cost on every view. Disabled outright as a stopgap; the real
      fix (negative caching, staged throttle, manual trigger) is a
      separate, deliberately deferred item below.

Recovered via `docker restart lcars` twice during the incident — safe
both times, no uncommitted transaction to lose.

- [ ] **Reopened 2026-09-22**: app slowdown/stall recurred — this time
      triggered by switching pages too often/fast, not by a container
      restart. Different trigger than the original `net::ERR_CONNECTION_
      ABORTED` report (~21:11 on 2026-09-20, fixed by restarting the
      app, correlated with that night's 4 container recreates), so this
      may be a distinct symptom of the same underlying WebView/networking
      fragility rather than a repeat of the same root cause. **Not
      critical for now** — user flagged but not blocking. Full detail in
      Claude memory (`webview-connection-aborted-after-restarts`).

## Data cleanup: 3 "Season 2" shows linked to the wrong Sonarr series (2026-09-20)

Found while diagnosing the freeze above, then confirmed as a real pattern,
not a one-off — see `season-2-linking-bug-pattern` in Claude memory for
full detail:

- [x] "Tantei wa mou, Shindeiru. Season 2" (`s-qp3t5j`) — fully traced:
      linked to Sonarr's "Detective Opera Milky Holmes" (confirmed via
      episode titles/air-dates, completely unrelated show). Soft-deleted;
      hard-delete requested (elapses 2026-09-21T19:51:49Z, needs
      `confirmHardDelete(retypedTitle: "Tantei wa mou, Shindeiru. Season 2")`
      once the 24h delay passes).
- [x] "Boys Over Flowers 2" (`s-vce5mc`) and "Bones Collector" (`s-vr1pcf`)
      — user found and deleted the wrong entries in Sonarr directly; LCARS
      side soft-deleted + hard-delete requested (both elapse
      2026-09-20T19:57:14Z).
- [x] **Guard rail shipped, v0.2.41+v0.2.42 (2026-09-21)**:
      - **Prevention**: the interactive-add sequel-confirmation dialog
        (`browse.js`'s "Attach as Season N?" / "It isn't — add as new
        show") was only wired into the Browse tab — `add.html`'s own
        direct addShow/addShowWithArr call sites silently rethrew the
        raw `sequel_of:`/`later_season:` errors unhandled. Now global.
        The "it isn't a sequel" retry bypass gap flagged here was real —
        confirmed and fixed 2026-09-22, see that section below.
      - **Passive detection for an already-wrong link**: new
        `identity_mismatch.check_anilist_id_mismatch` (ops-scheduled,
        hourly tier) compares each season's stored `anilist_id` against
        Fribb's own independent tvdb->anilist resolution, flagging a
        `pending_review` on disagreement — checks every season
        regardless of `manual_override` (the real Tantei row had that
        flag wrongly set already). Never auto-corrects; repair is the
        existing `amendShowArrLink`, which now also auto-refreshes
        metadata afterward instead of leaving that as a manual step.
      - **A second signal was designed, calibrated, and rejected same
        night** — title fuzzy-match (LCARS title vs Sonarr's own series
        title) isn't discriminative: calibrated against all 336
        currently-tracked Sonarr-linked shows, the worst *known-correct*
        match (a legitimate romaji->English translation) scored 0.159
        (`SequenceMatcher` ratio); the real Tantei/Milky-Holmes bug
        scored 0.300 — *higher* than a genuinely correct match. No
        threshold separates them. Not shipped. A real, different signal
        (e.g. episode-count/air-date-pattern consistency instead of
        title strings) remains a genuinely open idea if this is picked
        up again.

## Database-correctness pass — show/season layer done, episode/AniDB layer deferred (2026-09-21)

Full hybrid-model pipeline: AniList id (season-level, never show-level) →
matched to a show-level TVDB id → cross-referenced with Fribb for season
position → cross-referenced with AniDB at episode level for
absolute-episode-range placement → human review only when that chain
still can't disambiguate.

- [x] **Show/season identity layer — done.** Full 1171-show screen against
      AniList's own real public `relations` data → 85 confirmed franchise
      pairs, every one individually classified against real media_shape/
      episode-count/tracked-state data and resolved (not just the ones
      that were easy): 36 SIDE_STORY relation-typed+demoted, 27 SEQUEL (8
      real season merges, movies relation-fixed, OVAs/specials demoted,
      one — Koori no Jouheki — traced to a stale dead Sonarr link and the
      duplicate demoted, not a merge), 6 PREQUEL, 16 PARENT all resolved
      the same way. Tracked count 1841 → 1778. Backed up before every
      write, verified on prod after every batch. User confirmed this is
      good enough to move on from.
- [x] **Episode/AniDB layer — done, 2026-09-22.** `derive_episode_mappings`
      (`anidb.py:887`) is entirely local (reads already-synced `anime_
      list_entry`/`anime_list_mapping`, no live AniDB HTTP calls) and
      already runs the full tracked-anime backlog every tick — not
      "not started," already continuously running. The real remaining
      gap was a valid signal for "is a derived value actually wrong":
      the raw-number comparison tried earlier was invalid (see below);
      the real check built instead uses the **air-date arbiter** (see
      next section) — real broadcast date as the tie-breaker, per the
      user's own standing rule since Memory Alpha's inception. Full-
      library dry run: only 23 episode mappings actually needed
      correcting out of 3944 total, all 23 verified against real air
      dates before shipping (Sousou no Frieren's 4-episode premiere
      block, Log Horizon's skipped broadcast week, Bakemonogatari's
      broken community episode_map — three different real root causes,
      not one bug). ~1963 already-correct mappings got locked as
      confirmed in the same pass.

## Phase 2 mechanism — season-number-gap bug fixed at the root (2026-09-21)

Two things originally logged here as "two separate gaps to fix" turned
out to be one root cause, per the user's own correction: Memory Alpha
was specifically designed to keep the database straight, so a bug that
looked like it needed a smarter matching algorithm should have first
been checked against whether Memory Alpha was actually doing its whole
job. It wasn't — confirmed by reading the actual code, not assumed:

- [x] **Root cause found**: `season_ranges.ensure_all_season_rows`
      (Memory Alpha's season-row creator) only ever creates a row
      *reactively*, from `episode.season` values Sonarr has already
      synced. A real Fribb-known season with no synced episodes yet
      (the SPY×FAMILY case — its real "Season 2", anilist 158927, had
      no Sonarr episodes yet) never got a row at all. No amount of
      smarter position-matching in `identity_mismatch.py`'s resolver
      could have fixed that — there was no row to match against. This
      was the actual cause of both items originally listed here (the
      "AniDB mismatch never surfaces" item turned out to be a false
      lead too — see below).
- [x] **`fribb.enumerate_real_seasons`** — the real-season ordering
      (non-special/non-movie candidates for a tvdb_id, sorted by
      `(season.tvdb, episode_offset)`, `None` on a genuine tie) factored
      out of `identity_mismatch._resolve_by_position` into `fribb.py` so
      it's the single shared source of truth both season-row *creation*
      and season-identity *verification* use — no way for them to
      disagree anymore.
- [x] **`season_ranges.ensure_fribb_season_rows`** (new, wired into
      `poll_memory_alpha`, runs every tick) — proactively creates a
      season row for every real Fribb-known position with no row at
      all yet, writing the exact matched candidate's `anilist_id`/
      `mal_id` directly rather than delegating to `season_mapping.
      reconcile_season`'s own resolver (a real regression caught by
      this function's own test: that resolver matches by raw
      `season.tvdb == season_number`, a different, incompatible
      ordering from `enumerate_real_seasons` once a gap exists — it
      would've silently written a NULL `anilist_id` for a backfilled
      position). Also refuses to create a row whose content is already
      sitting on some *other* season_number of the same show (a second
      real case this function's own test caught: a missing position can
      still have its real content already misassigned elsewhere from an
      old bug — creating a fresh row there would duplicate it, not fix
      it; that misassignment stays `identity_mismatch.py`'s job to flag
      for a human). Dry-run against the real production snapshot before
      shipping: 173 new season rows across 1003 checked shows, all
      brand-new rows, zero existing data touched, zero pending_review
      noise (these are new rows, not disagreements with something
      already stored).
- [x] **The "AniDB-vs-Sonarr episode mismatch" idea was wrong, caught
      before shipping.** Wired `derive_episode_mappings`'s existing
      mismatch stats to `pending_review`, dry-ran it against the real
      production snapshot first (same caution as the v0.2.42 flood) —
      it would have flagged **1147** episodes on the very first run.
      Investigated why: `existing_abs` (Sonarr's own absolute episode
      count) and `anidb_epno` (AniDB's own, independent absolute count)
      are two different numbering schemes that are *supposed* to differ
      whenever a show has any split-cour offset or special-episode
      interleaving — which the Chobits unit-test fixture itself proved,
      16 of its 24 episodes "mismatch" even though that fixture is
      hand-verified *correct* data. The comparison was never a valid
      "is this wrong" signal to begin with, not just a noisy one — the
      `pending_review` wiring for it was reverted rather than shipped
      with a bad threshold. If this is revisited, the real signal would
      need to be "does the AniDB cross-reference resolve to something
      implausible" (negative/zero/out-of-range), not "does it differ
      from Sonarr's number."

Both the show/season identity layer (previous section) and this
season-gap root cause are now done. Still open: the episode/AniDB
layer (see the MUST DO SOON item above) — genuinely deferred, not
fixed by any of this.

## Identity-mismatch backlog cleared, sweep re-enabled, sequel-detection bypass built (2026-09-22)

- [x] **All 31 `identity_mismatch` flags reviewed against real data,
      applied to production.** 25 resolved same-session (11 real
      corrections to `season.anilist_id`, 14 confirmed LCARS was
      already right and Fribb's own dataset was wrong — resolved as
      such so the sweep won't re-flag the same value). The remaining 6
      needed the user's own judgment — published as a doc with real
      AniList/TVDB links, then all 6 resolved once real answers came
      back: a show whose `media_shape` was wrong (movie → episodic, not
      just the wrong season id), a season split across 2 AniList parts
      that needed 2 season rows fixed, a show's own **TVDB id was
      wrong** (fixed via the real `amendShowArrLink` repair path —
      Sonarr series deleted/re-added, not a raw SQL edit, since 13 real
      episodes were misattributed to a completely unrelated show), two
      cases needing brand-new shows created (OVA related to its real
      parent as a special, TV spin-off tracked separately, planned),
      and a 2008 OVA vs. 2010 TV version of the same franchise, both
      now tracked, related as a special the same way every other
      special tonight was.
- [x] **`identity_mismatch` re-enabled in the automatic ops loop**
      (`scheduler.py`, `run_daily_and_weekly_once`) — the backlog that
      caused the original false-positive flood is fully cleared and the
      root cause is fixed (previous section), so from here it only ever
      flags genuinely new disagreements.
- [x] **The sequel-detection bypass bug — confirmed real, then fixed
      properly, not patched around.** `create_show`/`create_show_with_
      arr_add` called `find_sequel_parent` unconditionally with no way
      to skip it — confirmed live twice tonight (had to work around it
      manually to create the two new shows above) on top of the
      original code-read finding. New `skipSequelCheck` field on
      `AddShowInput`/`AddShowWithArrInput`; the "It isn't — add as new
      show" retry now actually succeeds instead of re-triggering the
      identical detection and looping back into the same dialog.
      **Also added the user's own related ask**: a third dialog option,
      "Wrong show — it's a sequel of a different show," opening a
      search picker (`pickCorrectParent`) over tracked shows and
      attaching to whichever one the human actually picks
      (`attachSequelToChosenParent`, reusing the existing
      `setSeasonMapping` path `attachSequel` already uses — no backend
      change needed for that half, since `setSeasonMapping` never went
      through `find_sequel_parent` at all).

## Episode/AniDB layer closed: the air-date arbiter (2026-09-22)

The last open piece of the whole database-correctness thread. The
user's own standing rule, stated since Memory Alpha's inception: when
episode ordering is genuinely in conflict, the real broadcast/release
date is the source of truth and wins, full stop — not the community
mapping's own offset/episode_map guess.

- [x] **`_air_date_arbiter`** (`anidb.py`) — for each episode
      `derive_episode_mappings` resolves, cross-checks the community-
      mapping result against real AniDB per-episode air dates
      (`anidb_episode.airdate`). Confident only on an exact calendar-day
      match against exactly one real AniDB *regular* episode for that
      anime — no air date on the LCARS side, no matching AniDB date, or
      more than one regular episode landing on the same real date all
      return "no opinion" and keep the community-mapping result, same
      "never guess" convention this codebase uses everywhere else.
      Deliberately date-only, not time-of-day — AniDB's own `airdate` is
      a bare date and TVDB/Sonarr's stamped time on the LCARS side
      doesn't correspond to anything AniDB records.
- [x] **Confirmed matches lock (`confidence = 'air_date_confirmed'`),
      never revisited.** `derive_episode_mappings` now checks for this
      before doing any resolution work at all, so a locked row costs
      nothing extra on repeat ticks and — critically — can never be
      silently changed again later, even if the underlying community
      mapping data itself changes. This is the literal "lock those as
      confirmed, no further changes" the user asked for.
- [x] **Full-library dry run before shipping** (same discipline as
      every other check tonight): 3944 total mappings, only **23**
      actually changed value, **~1963** already-correct ones got locked
      in the same pass. Manually verified 3 of the corrected cases
      against real air-date data before trusting the number:
      - **Bakemonogatari S5** (8 episodes) — the case that started this:
        community `episode_map` derived nonsense AniDB episode numbers
        (402-409) for a 2017 compilation release AniDB itself only has
        2 real regular episodes for. TVDB had split the same 2-day
        release into more, finer-grained episodes than AniDB tracks —
        air-date matching correctly collapses LCARS's several same-day
        episodes onto AniDB's one real episode per day instead of
        trusting an offset that was never built for this split.
      - **Sousou no Frieren S1** (off by 4) — AniDB's real data shows a
        4-episode premiere block all airing the same day, then weekly
        from episode 5; LCARS's own episode 1 airs on the weekly-episode-
        5 date, so the community mapping's naive 1:1 offset was wrong
        from the very first episode.
      - **Log Horizon S2** (off by 1, episodes 15-22) — AniDB's real
        broadcast skipped a week (a recap/clip episode LCARS still
        tracks but AniDB doesn't count as a regular numbered episode);
        every subsequent weekly episode was off by one as a result.
      - Three different real root causes, not one bug repeated — exactly
        why a general, principled arbiter was the right thing to build
        instead of three one-off patches.
      - **HUNTER×HUNTER**, flagged by an earlier, cruder implausibility
        check as a possible 4th case, was verified a false positive
        (genuinely 62 real episodes, confirmed against real 2001 air
        dates) — now locked as confirmed-correct by the same arbiter
        rather than left periodically re-checked.

Everything from the whole database-correctness thread is now done, with
no further deferred pieces.

---

## Android app auto-download controls — shipped in v0.2.31 (2026-09-20)

User feedback after using the app for real: wanted control over what
auto-download grabs and how it manages storage, rather than it being an
unconfigurable black box. Four pieces, all confirmed live on-device
against the deployed server (not just built/unit-tested):

- [x] **`backlog(includePlanned: true)`** — a planning-status show whose
      first episode has already aired is now included, matching "started"
      shows the user wants grabbed without a manual promotion to
      watching. Paused shows remain excluded (already were). Default
      `false` server-side, so the web Backlog page is unaffected.
      Confirmed live: previously-invisible episodes of several
      already-started planned shows appeared in the very next real
      backlog fetch after deploying.
- [x] **Auto-download on/off toggle** — previously no way to disable it
      short of never configuring a server. `AutoDownloadWorker.schedule()`
      now cancels the periodic work when disabled rather than leaving a
      no-op tick every 30 minutes. Confirmed live via the settings UI:
      toggling off produced a real `WM-GreedyScheduler: Cancelling work
      ID ...` in Logcat; toggling back on re-scheduled and (since
      periodic work runs immediately when constraints are already met)
      triggered an actual run within seconds.
- [x] **Time-based auto-delete** — new `watched_at` column
      (`DownloadDatabase` v3) plus `evictWatchedPastHours()`: deletes a
      watched episode a configurable number of hours after it was marked
      watched, independent of (and checked before) the existing
      size-based eviction. Disabled by default (0 = never).
- [x] **In-app Settings page access** — new `AppSettingsPlugin` bridges
      all four settings (the three above plus the existing Wi-Fi-only
      toggle) to `settings.html`, so they're reachable without leaving
      the app for the native "Change server" screen. That screen keeps
      its own Wi-Fi-only/storage-limit fields too — same SharedPreferences
      keys, so both stay in sync automatically; not a replacement.

One side effect worth knowing about: `DownloadDatabase`'s v2→v3 upgrade
drops and recreates the table (existing, intentional policy — a
re-downloadable cache, not data of record), so any app update that bumps
`DB_VERSION` will make the worker's next tick treat every already-
downloaded file as "new" again and re-enqueue it. Confirmed this doesn't
create `-1` duplicate files (`episode_id` `PRIMARY KEY` still holds), but
it does mean real re-download bandwidth/storage churn on every such
upgrade — acceptable for a hobby app, but a good reason not to bump
`DB_VERSION` casually.

Still open: the app icon is a placeholder — user is designing a real one
(needs a square PNG, 1024×1024 ideally, safe detail within the center
~66%, for Android Studio's Image Asset tool to generate the adaptive-icon
set from).

---

## Android app A0 (server + web client prep) — shipped in v0.2.27 (2026-09-19)

Build steps 5–12 of `ui/DESIGN.md` §8 done, tested, and verified live
against the deployed stack (1292 tests green pre-deploy; every A0-specific
behavior below re-checked against `tiny` post-deploy, not just assumed):

- [x] `auth.py`: `/auth/check` also accepts `Authorization: Bearer <token>`;
      `/auth/settings` GET serves `lcars_token`/`tmdb_api_key` from LCARS
      config (never from `web_setting`); `SHARED_SETTING_KEYS` emptied
      (table kept for any future genuinely-shared setting) — PUT and
      `/auth/setup`'s old `settings` import are now no-ops for those two
      keys, the dead setup-import code path was removed outright.
- [x] `nginx.conf`: `/_auth_check` forwards `Authorization`; `/` gains
      `proxy_http_version 1.1` + `Upgrade`/`Connection $connection_upgrade`
      (a `map $http_upgrade $connection_upgrade` block, not the hard-coded
      `"upgrade"` DESIGN.md sketched — that form puts `Connection: upgrade`
      on every plain GraphQL POST too, an asymmetric header pair a plain
      HTTP/1.1 backend can choke on; caught in review, not by any test
      here — no local nginx/docker to run this config against) +
      `proxy_read_timeout 3600s` for future WS passthrough (A3).
- [x] `api.js` → `fetch('/')`; `calendar.js`/`grabs.html` mpv URLs →
      `location.origin`; both `rewriteHost` call sites (`calendar.js`,
      `show.js`) → `location.hostname` directly.
- [x] `config.js`: `SHARED_KEYS` reduced to `['lcars_token','tmdb_api_key']`;
      `requireConfig`/`bootstrapConfig` need only `lcars_token` and now
      redirect to `login.html` (not `settings.html`, which has no token
      field to fill it from); `getConfig` actively deletes stale
      `lcars_url`/`home_server_host` from localStorage on every load.
      Dead `saveConfigWithSync` (no callers left once settings.html
      stopped writing these fields) removed.
      **Verified** by running the real `config.js` module (unmodified,
      dynamic import) against a shimmed `localStorage`/`location` in
      Node: a simulated pre-A0 config with stale `lcars_url`/
      `home_server_host` and a still-valid token has both stripped on
      the first `getConfig()` call and is accepted by `requireConfig`;
      a token-less config redirects to `login.html`.
- [x] **Step 9** (real-browser desktop check, run by hand 2026-09-19
      against `tiny` v0.2.27 in Firefox) — fresh-profile login →
      calendar/Sonarr link/TMDB poster fallback/mpv/download all worked,
      `starfleet_config` held only `lcars_token`/`tmdb_api_key`. Stale-profile
      regression also confirmed: a `starfleet_config` seeded by hand with
      old-style `lcars_url`/`home_server_host` (simulating a pre-A0
      browser) had both stripped and never reappeared after a real login.
      Found a real (if narrow) gap along the way: `bootstrapConfig()`
      only re-syncs from `/auth/settings` when a key is *missing*, not
      when it's present-but-wrong — a corrupted cached `lcars_token`/
      `tmdb_api_key` (e.g. from a bad manual edit, not a normal user
      path) stays stuck through any number of refreshes; only clearing
      `starfleet_config` outright forces a re-pull. Not a shipped-code
      regression since nothing writes a bad-but-present token in normal
      use, but worth knowing if a token is ever rotated server-side.
- [x] `settings.html` → only `mpv_helper_url` remains; `login.html` →
      "import existing settings" block removed (backend path it fed is
      gone too).
- [x] `dev.sh` → stopped writing `lcars_url`/`lcars_token`/
      `home_server_host`/`tmdb_api_key` into `web_setting` (now inert);
      added `LCARS_TMDB_API_KEY` env var so the dev server still serves
      the TMDB key via config the way A0 requires.
- [x] `tmdb_api_key` confirmed present in `/opt/appdata/lcars/config/lcars.ini`
      on `tiny` (user confirmed the value itself; presence also confirmed
      here via a non-destructive `grep -c`) — TMDB poster fallback keeps
      working under the new "served from config only" model.
- [x] **Step 10** (curl bearer test against deployed nginx) — both
      `GET /auth/check` and `POST /` with `Authorization: Bearer` and no
      cookie return 200 through the live nginx on `tiny`.
- [x] **Step 11** (release + deploy) — tagged/pushed `v0.2.27`, CI built
      and published both `starfleet:0.2.27` and `-web` images, DB
      snapshotted (`lcars.db.bak-20260919-android-a0-v0.2.27`), pins
      bumped on `tiny`, `pull`+`up -d`, all three containers healthy on
      the new tag, `ops`'s expected post-recreate `LcarsError` noise
      confirmed to self-clear within 20s.
- [x] **Step 12** (WS `graphql-transport-ws` end-to-end check) — a real
      Python client connected to `ws://<tiny>:8888/` with a bearer
      header, negotiated the `graphql-transport-ws` subprotocol through
      nginx, completed `connection_init`/`connection_ack`, and had a
      `subscribe` accepted with no error frame. Confirms the whole chain
      (nginx upgrade headers → LCARS WS → subprotocol negotiation) works
      live — not just that `BearerTokenMiddleware` accepts the scope.
      Did not force an actual event through (would mean mutating
      production state just to see a payload) — a real `episodeAvailabilityChanged`/
      `showCreated` payload landing client-side is still worth eyeballing
      once A3 actually subscribes to one.
---

## login.html couldn't log in when genuinely logged out — shipped in v0.2.28 (2026-09-19)

- [x] Found while testing Android against production (below): `login.html`
      statically imported `js/config.js` (for `applyAppName()`'s dev/prod
      tab-title swap), but nginx's `/ui/` location auth-gates everything
      except `login.html`/`css/`/`favicon.svg` themselves. A client with
      no session cookie at all got a 302-to-self for that import, failing
      the whole ES module graph — including the login form's own submit
      handler. Pre-existing (confirmed via git history, predates this
      session's A0 work), never hit because real sessions are long-lived.
      Fixed by inlining the few lines `login.html` needs instead of
      widening what nginx serves unauthenticated. Verified live + via
      screenshot on the Android WebView.

---

## Android app A1 (shell + VLC) — shipped in v0.2.29 (2026-09-19)

Test machine (Android Studio, JDK 21 for Gradle, `android-tools` for
`adb`) set up this session; a real device (Asus Zenfone 9, Android 14)
connected throughout. All of DESIGN.md §8's steps 13–22 built and
verified live on that device — see DESIGN.md itself for the full
per-step detail (Capacitor version actually used, the `SuperNotCalledException`
gotcha, the `<queries>` package-visibility fix, the release keystore).
Login/setup/VLC streaming/signed-release-build all confirmed working
end to end, including a real episode file streamed and played from the
live production server. Tailscale half of the LAN/Tailscale test (step
21) is blocked on the test phone's own Tailscale login, not app code —
LAN half thoroughly proven.

Steps 19-20 (TokenPlugin wiring, platform-gated play button) are now
exercised through the real deployed app UI, not just CDP — found (and
fixed) exactly the gap this note flagged: the production `ui/` bundle
predated all of A1-A4's native-Capacitor branching, so the app's play
button was silently falling through to the desktop-only mpv-helper
endpoint (`localhost:19450`) and failing. Confirmed the deployed
`calendar.js` now has `isNativePlatform`/`Vlc.play` (fetched from inside
the live authenticated WebView session, not just curl — nginx auth-gates
`/ui/js/*`), reloaded the page, and confirmed live: VLC launches natively
on the phone, desktop mpv-helper path still works unaffected in a
browser.

---

## Android app A2 (download) — shipped in v0.2.29 (2026-09-19)

DESIGN.md §8 steps 23–27 built and verified live on the same device:

- [x] Local download index — plain `SQLiteOpenHelper`, not
      `@capacitor-community/sqlite` as the plan originally named (deemed
      unnecessary third-party native surface for what's actually a small,
      fixed set of operations).
- [x] `DownloadPlugin.kt` — real bug found and fixed by testing:
      the completion broadcast receiver needed `RECEIVER_EXPORTED`, not
      `RECEIVER_NOT_EXPORTED` (DownloadManager is a different-UID system
      service; the "not exported" flag was blocking its own broadcast to
      us). See DESIGN.md for the full story.
- [x] Offline playback — `VlcPlugin` takes a local `content://` URI now,
      confirmed by actually disabling WiFi on the device and watching a
      downloaded episode play from local disk.
- [x] Downloaded a real ~190MB file, confirmed byte-exact size match,
      confirmed the download survives backgrounding the app (the actual
      reason to use `DownloadManager` over Capacitor's own `Filesystem`
      API) and the index still updates correctly on foreground return.
- [x] Web half (platform-gated download button, `downloads.html` native
      index) now deployed and exercised through the real UI — same fix as
      A1's play-button gap above (stale `ui/` bundle), same v0.2.29 deploy.

---

## Android app A3 (live updates via WebSocket) — shipped in v0.2.29 (2026-09-19)

- [x] `LcarsWsPlugin.kt`: connect, `connection_init`, subscribe,
      `notifyListeners`, bearer on the WS handshake header (OkHttp 4.12.0 —
      5.5.0 needs compileSdk 37, one more than this project's 36).
- [x] Reconnect on connectivity change (`ConnectivityManager
      .registerDefaultNetworkCallback`, exponential backoff 2s→60s cap) —
      confirmed live by disabling WiFi mid-session (`onFailure` + doubling
      backoff observed) and re-enabling it (real second `connection_ack`).
- [x] `calendar.js`'s 10s poll now only runs on non-Android platforms;
      Android registers `LcarsWs` listeners instead.
- [x] Tested live against the real deployed server: `graphql-transport-ws`
      subprotocol negotiated, both subscribes accepted with no error frame.

---

## Android app A4 (auto-download + storage management) — shipped in v0.2.30 (2026-09-20)

`AutoDownloadWorker` (WorkManager `CoroutineWorker`, unique periodic,
30min, UNMETERED-only by default) diffs the LCARS backlog against the
local download index and enqueues anything new, then evicts
oldest-watched-first once `storage_limit_gb` is exceeded. See
`ui/DESIGN.md` §8 steps 33-38 for full per-step detail. Found by live
device testing, not designed in:

- [x] **Duplicate-download bug** — an ad-hoc one-time `WorkRequest` added
      purely to force a test run raced the periodic work's own first
      execution and double-enqueued every episode (every file downloaded
      twice, `-1` suffix). Root-caused to that one-time trigger, not the
      periodic mechanism itself; removed, since `enqueueUniquePeriodicWork`
      alone already gives WorkManager's own serialization guarantee.
- [x] **Stale "pending" rows** — the completion broadcast only fires while
      the app process is alive, so a process death mid-download left a
      permanently-stale DB row; the worker now reconciles against
      `DownloadManager` directly on every tick.
- [x] **Evict/redownload infinite loop** — eviction must soft-delete
      (`markEvicted`, tombstone row) rather than hard-delete: a hard
      delete let the same tick's own backlog diff see the episode as new
      again and immediately re-enqueue it.
- [x] **`DownloadManager.remove()` doesn't delete the file** under
      `setDestinationInExternalFilesDir` — confirmed live; fixed by also
      deleting the file directly.
- [x] Wi-Fi-only downloads toggle and storage limit (GB) field, both in
      `ServerSetupActivity`, same immediate-save-on-toggle pattern.
- [x] `VlcPlugin` reports watched status natively off its own
      `onActivityResult` (SQLite write always succeeds immediately; the
      network `addWatchEvent` call is independently retried via
      `WatchEventRetryQueue` on failure, flushed every worker tick).
- [x] **Native `lcars_token` sync bug, found and fixed** — the token
      didn't reliably reach native `SharedPreferences` even though the
      WebView's own `localStorage` copy was correct, silently auth-failing
      every `AutoDownloadWorker` GraphQL call for a stretch of the previous
      evening's testing (fast ~20-40ms "SUCCESS" results were auth
      failures returning zero episodes, not real backlog fetches).
      Root cause was never pinned to a single line — two contributing
      issues were found and fixed together instead: (1) `bootstrapConfig()`
      only pushed the native token when `localStorage` itself was missing
      it, so a silent native failure had no retry path once the web side
      looked fine — `pushNativeToken()` is now its own function, called
      unconditionally on every native page load; (2) both the JS
      (`syncConfig()`'s catch) and native (`TokenPlugin.setToken()`'s
      `apply()`, fire-and-forget) sides could fail with zero observable
      trace — JS now logs `console.warn`/`console.debug`, and native uses
      `commit()` (a real success boolean) plus an immediate readback,
      rejecting the call if the write didn't verifiably land. Verified
      reliable over 5 consecutive fresh app launches the next morning
      (each showing `commit()=true readbackMatches=true` in Logcat and the
      token actually present in the prefs file afterward) — confidence
      the *observable* bug is gone even without a single root-cause line,
      since the fix closes every path that could produce the symptom.
- [x] **Full end-to-end verification, real data, zero duplicates**:
      digging through the DB/file state the next morning turned up a
      complete real run from the previous evening (20:24:46-47, moments
      before a forced second run got correctly refused by WorkManager) —
      5 real backlog episodes downloaded with 5 unique `episode_id` rows
      (no duplicates), then eviction correctly ran and evicted the 4
      oldest-by-`downloaded_at` (all unwatched, so pure oldest-first),
      leaving exactly the newest under `storage_limit_gb`. Cross-checked
      against the actual files on disk: exactly one file present, matching
      the one `complete` row; the 4 evicted rows' files were genuinely
      deleted, not orphaned. That state held unchanged for ~10 hours
      overnight with zero re-download thrashing of the evicted episodes —
      the evict/redownload loop fix (above) is confirmed solid under real
      elapsed time, not just a single forced test tick.

Also fixed along the way: the deployed `ui/` bundle predated all of
A1-A4's native-Capacitor branching (`isNativePlatform()` checks added
across earlier phases were sitting in the repo, undeployed) — the app's
play/download buttons were silently falling through to desktop-only code
paths. This is what v0.2.29 actually shipped along with A3/A4's own code,
with the token-sync fix following in v0.2.30. This same "code committed,
never deployed" class of bug is also step 33's headline duplicate-download
fix's twin — worth remembering `v*` tag + redeploy is a discrete step,
not implied by `git commit`.

All four Android app phases (A1-A4) are now shipped and verified end to
end against the real deployed server, not just native-only or CDP-driven
testing.

---

## Android app: mark-as-watched, resume, auto-refresh — shipped in v0.2.37 (2026-09-20)

Found through live use after A1-A4 shipped, not part of the original build
plan. This closes out the Android app build — the whole app (server prep,
shell, VLC, download, live updates, auto-download, and this round of
watch-tracking fixes) is now shipped and confirmed working live, end to end:

- [x] **VLC resume-last-position blocked re-watching an already-watched
      episode** — VLC's own resume feature opened straight to the last few
      seconds and closed almost instantly, no time to seek back. Fixed with
      `from_start: true` on `Vlc.play()` whenever the episode being launched
      is already watched (forces playback from 0, overriding VLC's resume
      memory); left alone for anything not yet watched. `episodeCtx()`
      gained a `watched` field; `show.js`'s three `launchMpv()` call sites
      (which bypassed `episodeCtx()` entirely) were fixed to compute it
      inline — the actual primary usage path, found only by auditing every
      call site rather than trusting the first one fixed.
- [x] **Mark-as-watched never fired on natural end-of-file completion** —
      root-caused via live device testing: VLC's own `extra_duration`
      result extra reliably returns 0 specifically on natural completion
      (fine on a manual back-press exit), silently breaking the
      position/duration threshold check. Fixed with `MediaMetadataRetriever`
      pulling a real duration up front (offline `content://` URI or
      streaming URL), used in place of VLC's own value whenever available.
      Verified end-to-end (logs → local DB → network call → server GraphQL
      state) for both offline and streaming playback, on both the test
      phone and the user's tablet.
- [x] **No auto-refresh after a watch** — calendar/backlog/show/downloads
      pages required a manual reload to show a new watched status or drop a
      just-watched episode from the backlog. `launchMpv()`'s native branch
      and `downloads.js`'s `playOffline()` now dispatch a
      `starfleet:refresh-after-watch` event ~2s after VLC returns control to
      the app (once the background `addWatchEvent` report has had time to
      land); each page listens and does a silent re-fetch. The desktop
      mpv-helper path deliberately does not dispatch this — that `fetch()`
      resolves when mpv launches, not when it exits, so there's no
      equivalent signal to hang a timer on. `grabs.html`'s separate
      `launchMpv()` also doesn't wire it: `GrabEvent` has no watched-state
      field for that page to redraw (folded into the future unified list
      page instead).
- [x] User-confirmed live end-to-end post-deploy: worked as expected.

---

## Ongoing (background)

- [ ] **Rotate keys** at cutover: Sonarr/Radarr API keys, MAL `client_id`, LCARS's own
      AniList `client_secret`. All intentionally live until Data is 100% to the user's
      liking — no fixed date.
- [ ] **Move secrets** out of plaintext `config.ini`. Deferred, reminder only.
- [ ] **AniList metadata fallback is still scalar-only**: `_fetch_mal_fallback` fills
      poster/synopsis/duration but skips relations, studios, characters, genres. Sequel
      detection (W1) now works without AniList, but other metadata paths still degrade.
- [ ] **Franchise function deferred**: SEQUEL/PREQUEL edges are show-level season chains,
      not true cross-media franchises. Needs broader definition covering TV+movies+anime.
- [x] **Score sync — re-enabled 2026-09-20, not actually still paused.** The
      AniList/MAL per-entry vs LCARS show-level scoring mismatch that paused
      it (2026-09-12, ~238 false-positive reviews) was fixed the same night
      as the production-freeze incident: `score_sync.py` now skips a season
      entirely when it never had an explicit score of its own, instead of
      falling back to comparing against the show-level score. Live in the
      automatic loop since. (This had gone stale in Claude's own memory —
      corrected 2026-09-22 after being told directly rather than caught by
      re-reading the code first.)

---

## Small fixes — shipped in v0.2.25 (2026-09-19)

- [x] **Browse sequel dialog: "It isn't — add as new show"** — third button in
      `confirmSequelAttach` (`browse.js`), calls `addShow` directly (already
      AniList+MAL only on this path, no field stripping needed).
- [x] **`fetchShowArt`: show-level AniList fallback when no season rows exist**
      (`metadata.py`) — mirrors the existing MAL show-level block, stores with
      `seasonId = NULL`.
- [x] **Fuzzy guard on Sonarr/Radarr title search** — both sites guarded:
      backend `_enrich_tvdb_via_sonarr`/`_best_matching_result` (`browse.py`,
      title + alternate titles via `difflib`) and frontend `bestMatchingCandidate`
      (`browse.js`, title-only Sørensen–Dice — GraphQL's `ArrCandidate` doesn't
      expose alternate titles). A non-matching top result now behaves like "no
      results" instead of silently attaching a wrong ID.

---

## ID correction + Sonarr/Radarr ↔ LCARS sync — shipped in v0.2.25 (2026-09-19)

- [x] **Edit every external ID on the show page** — AniList/MAL/TVDB/TMDB/IMDB
      were already editable (right-click badge → `openExtEditor`, easy to miss
      but present). Added: AniDB (dedicated form → `linkAniDb`, since it also
      seeds the TVDB-season/episode-offset `anime_list_entry` mapping the generic
      editor can't), Syoboi, TVmaze (generic editor, removed from `READONLY_SVCS`).
- [x] **Edit IDs from a review** — `franchise_season_collision` reviews (the
      actual gap; `anilist_id`/`_conflict`/`cross_service_merge` already had
      inline editors in `reviews.html`) now get a parent/child comparison panel
      with Confirm (season #) / Reject (+ optional corrected TVDB/AniList ID),
      wired to `resolveFranchiseMerge` (added to `api.js`, wasn't called from
      the frontend at all before).
- [x] **Sonarr/Radarr → LCARS**:
      a) Webhooks: `SeriesAdd`/`MovieAdded` create a tracked, PLANNED show (same
         shape as the reconcile auto-create below); `SeriesDelete`/`MovieDelete`
         unlink the `sonarr`/`radarr` deep-link row and open a review — never
         raises on an unrecognized/malformed payload (Sonarr/Radarr's webhook-
         connection Test requirement).
      b) `reconcileArrState` mutation (new, `local_audit.py`) folded into Ops's
         existing availability loop (no filesystem walk, unlike `auditLocalFiles`,
         so — unlike that one — this rides the automatic loop): untracked-show
         auto-create, availability correction (existing logic, `walk_orphans=False`),
         and `monitored` ↔ status reconcile both directions. A service that fails
         to connect contributes nothing that tick — a blip must never read as
         "pause the whole library." Resume target is remembered
         (`show.status_before_pause`, migration `038b4fbb1ec7`) rather than
         hardcoded to WATCHING, and only ever fires when `status_before_pause`
         is actually set — a show paused/dropped before this feature existed
         (NULL on every pre-migration row) is left alone even if Sonarr still
         reports `monitored=true`, rather than getting silently reactivated
         (with a real AniList/MAL push) on the very first tick. Closed a real
         pre-existing gap along the way: `_apply_status_change` (extracted from
         `resolve_set_status`) now re-monitors in Sonarr/Radarr on leaving
         paused/dropped (`_remonitor_in_arr_on_resume` — update-only, no
         add-if-missing; deliberately not `shows.ensure_arr_monitored`, whose
         add branch would re-add and full-search a show missing from Sonarr for
         an unrelated reason) — without this, resuming in LCARS left Sonarr/
         Radarr still unmonitored and the next reconcile tick would've silently
         paused it right back. Manual trigger: `ops reconcile-arr-state` (same
         mutation the automatic loop calls — useful for a first look before
         turning the loop loose on a real library).
      c) Fixing a wrong link *in Sonarr* (delete wrong series, add right one) is
         now genuinely enough — LCARS follows via (a)/(b).

---

## mpv → LCARS watched status — shipped in v0.2.25 (2026-09-19)

- [x] Every `launchMpv()` call site (`calendar.js` x5 including the service-strip
      player icon, `show.js` x3 — one of which, `buildAnidbEpRow`'s mpv button, had
      a real pre-existing arg-order bug, `launchMpv(cfg, epFilePath)`, fixed in
      passing — and `grabs.html`, the one site with no `showId` in scope until
      `GrabEvent.showId` shipped alongside it, see below) now passes
      `{showId, season, episode}` through.
- [x] `launchMpv(filePath, cfg, ctx)`: `/play` POST body gains `showId`, `season`,
      `episode`, `token: cfg.lcars_token`, `lcarsBase: cfg.lcars_url` (not
      `location.origin` as originally sketched — `cfg.lcars_url` is what the
      function already uses to build the media URL itself, and is correct even
      when the web client and LCARS aren't same-origin).
- [x] `mpv-helper.py`: `--input-ipc-server=<per-launch-socket>` on the mpv launch;
      a background thread observes `percent-pos`, tracks the session peak, and on
      normal exit with peak >= 90% (same threshold as the Android VLC client)
      POSTs `addWatchEvent(showId, season, episode, platform: "mpv-helper")`
      straight to LCARS with the bearer token. Best-effort throughout — IPC
      connect/POST failure just logs, playback is never affected. A launch
      superseded by a newer one (single-instance policy, already existed) is
      matched by an incrementing launch id, not just process-exit — so a killed
      episode never reports watched even if it had already crossed 90%.
      Smoke-tested standalone (fake IPC socket + fake LCARS endpoint) at ship
      time; **confirmed working live 2026-09-21** — real mpv playback through
      mpv-helper correctly auto-marked watched against production. Not part
      of the pytest suite — mpv-helper.py runs on the user's own client
      machine, not inside LCARS.
- [x] `GrabEvent.showId` (schema + resolver) — `_grab_file_paths_{sonarr,radarr}`
      already resolved `show_id` internally for the filePath lookup, just never
      returned it; needed so the Grabs page's launch site could report watched
      status too. Backend piece already committed.

---

## On-air indicator (calendar/backlog availability icon) — shipped in v0.2.25 (2026-09-19)

- [x] Fifth `availState()` value, **airing**: broadcast window open right now
      (`airDateUtc` through `airDateUtc + runtime`), client-only overlay — no
      schema change, no new `AvailabilityStatus` enum value. `isEpisodeAiringNow()`
      computes the window client-side each render (never a cached server field),
      falling back to a conservative default runtime when neither
      `episode.runtimeMinutes` nor `show.durationMinutes` is known, so an unknown
      runtime doesn't silently suppress the indicator for the whole broadcast day.
- [x] `runtimeMinutes`/`durationMinutes` added to `episodesInRange`'s query (not to
      `backlog`'s — backlog is `available_locally = 1` by definition, so `airing`
      can never apply there; narrower than the original sketch, deliberately).
      `show.js`'s episode rows thread `show.durationMinutes` through too.
- [x] Icon: filled circle (●), blue (`--av-airing`), 2s opacity pulse — distinct
      from ready (▶ green)/downloading (⬇ amber)/missing (⬇ red)/future (◷ gray).
      Wired into both calendar-card and planner-card icon rendering.

---

## Faster list-page cover art — shipped in v0.2.26 (2026-09-19)

Root cause was an eager/no-fallback asymmetry: anime shows get `poster_url`
reliably at add-time (AniList link mandatory, MAL fallback if AniList is
down); TV/movie shows only got one from Sonarr/Radarr's own bundled image
list (optional, no fallback) — the real art fetch (TVDB+TMDB+TVmaze+MAL)
existed but was manual-only. So most TV shows arrived at the list page with
`posterUrl = NULL`, and the client-side TMDB fallback queued hundreds of
shows through one shared 220ms-spaced request chain — the real "timeout."

- [x] **Eager fallback going forward** (`_fetch_tmdb_duration`,
      `metadata.py`) — now also pulls `poster_path` from the same TMDB
      response it already fetches for `duration_minutes` (no extra API
      call), writing `poster_url` via the same COALESCE-write pattern
      Sonarr/Radarr's own poster writes already use, so Sonarr/Radarr
      (running after this in `fetch_and_populate`) still wins when they
      have their own poster — TMDB only fills the gap.
- [x] **One-time backfill for the existing library** — new
      `backfillShowPosters` mutation (`metadata.backfill_show_posters`)
      runs the full art fetch for every tracked show still missing a
      poster, paced between shows, per-show failure isolated (never
      aborts the batch). Manual trigger only (`ops backfill-posters`),
      same reasoning as `audit-local-files`/`reconcile-arr-state` — real
      outbound calls per show, too much cost for the automatic loop.
      Safe to re-run.
- [x] **Backfill run** (2026-09-20) — by the time this was triggered, only
      4 of the original 369 tracked shows still had `poster_url IS NULL`;
      the eager TMDB fallback added alongside this feature had already
      caught the rest through normal use over the following day. 0/4
      filled: one real show (a Demon Slayer movie entry) genuinely has no
      art available from any source (TVDB/TVmaze/TMDB/MAL); the other 3
      are stub rows with no title at all in the DB, a data-completeness
      gap unrelated to this feature. Safe to re-run if either gets fixed.
- Client-side queue/timeout hardening (option 3 from the original
  diagnosis) deliberately skipped — once posters are populated
  server-side, the client fallback path barely gets exercised.

---

## Ideas / future

### Unified list page

Fold Lists, Backlog, Grabs, Browse, and Add into one page with preset filters/views.
List page works now (fix committed, pending release) but the unified redesign is the long-term direction.

### Discover page (extension of Add page)

Passive import: ops loop pre-creating untracked stubs from upcoming AniList/TMDB seasons.
User browses in "Upcoming", promotes to PLANNING or dismisses as SKIP. Nice-to-have.

### Statistics page

Episode/show counts by status, watch history over time, score distribution, genre
breakdown, total runtime. Needs date-bucketed watch-event aggregates from LCARS.

### External ID season-level display

Show season-specific AniList/MAL IDs in the external ID bar (not just show-level).
Wire during season table rework. Currently clicking "AL" on S2 goes to the S1 page.

### Memory Alpha — browse prefill + add-confirmation UI

`propagate_cross_ids` runs every tick (built). Future:
- Browse prefill: use Memory Alpha IDs to prefill service links on cards before adding.
- Add-confirmation popup: show each discovered ID for validation before committing.

### Art-fetch negative cache + throttle (found 2026-09-20, deferred)

Found while diagnosing the v0.2.38/v0.2.39 freeze incident: `autoFetchArt()`
only skips its AniList/TVDB/TMDB/TVmaze/MAL fetch cascade when a show
already has *any* stored art asset — a show whose art is genuinely
unfindable (a real confirmed case: a Demon Slayer entry from that night's
poster backfill) re-triggers the full cascade on *every* page view,
forever. User's own design, deliberately deferred:

- [ ] **Negative caching**: remember "tried, found nothing" so automatic
      per-page-load fetch stops retrying; a manual re-fetch button must
      still bypass it. Art dialog gains: (a) add art manually via a pasted
      image URL from any source, (b) delete a stored art address (for a
      stale/404'd URL — expected to be rediscovered on the next manual
      re-fetch if it's still findable).
- [ ] **Staged/throttled auto-fetch, not a hard cap**: banner + highest
      season's art first (stands in for the show meanwhile) → after a
      beat, main show art → only then search other seasons, and only
      when that season has no art yet AND is actually unhidden/visible.
- [ ] **Manual pulls take priority** over background/automatic fetches
      (both the page's own auto-fetch queue and ops's scheduled jobs).

Full detail in Claude memory: `art-fetch-negative-cache-and-throttle-plan`.

### Smaller ideas

- [ ] IMDB datasets for cross-referencing and fallback ID bridging.
- [ ] Browse / filter by studio (`show.studio` gets an id-prefix).
- [ ] Direct TVDB search (use TVDB API instead of through Sonarr).
- [ ] livechart.me headlines for delay/reschedule news on tracked shows.
- [ ] Grabs screen: show only title+episode/film title, add mpv launch key.
