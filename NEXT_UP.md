# Next up

Compacted 2026-10-07; State and Open refreshed 2026-10-10. The full previous NEXT_UP (every v0.2.x–v0.4.x section, ship checklists, build notes) is kept unchanged in
**NEXT_UP-ARCHIVE-2026-10-07.md**. Records: PLAN-DATA.md "Cutover (2026-09-30)", HANDOFF-2026-09-30-CUTOVER.md, HANDOFF-2026-10-06.md.
Rules live in RULEBOOK.md (absolute); items marked "decision" below are open to re-discussion.

## State

- **Prod (tiny): v0.4.9 live** (2026-10-10 ~12:40Z; tag `v0.4.9` = 6b60d8f; CI `test success` + `docker success`; snapshot `lcars.db.bak-20261010-pre-0.4.9`; compose backup `starfleet.yml.bak-0.4.8-20261010`). Alembic head `c2d3e4f5a6b7` (no migration). v0.4.9 = the Maintainerr tag system (`keep` / `ongoing` / `purge`, matched by TVDB id) + `Show.keep` / `setShowKeep` + the keep badge and reminders in the web UI (UI unverified in a browser).
- **Done on prod 10-10 after the deploy:** Sonarr tags are on (the new code's reconcile applied them itself): 147 `ongoing`, 31 `purge` (the vetted 480 GB list), none overlapping `keep`/`ongoing`; `keep` removed from Under the Banner of Heaven (undo log `/db/arr_tags_backfill_20261010.json` in the lcars container); the 37 wrong Sonarr deep-link rows fixed (1 repointed, 36 removed; all 351 rows now right). Hunter x Hunter (2011) stays dropped (user: "I'll deal with consequences if any later").
- **Maintainerr switched 10-10 ~13:00 Dublin (user's go, `--apply --execute`):** `Housekeeping` (id 7) and `Housekeeping (series)` (id 8) created; `spring cleaning` → `Checkout` / `Checkout (series)` (ids 2, 4; 15 days; the season must be unmonitored); the three `clearing house` collections (ids 1, 3, 5) are now "tag purge", 15 days. Rules executed; membership read before any action: clearing house 28 shows (all LCARS-dropped), Checkout 15 seasons (14 completed + 1 manual), Housekeeping 77 seasons (all LCARS-completed shows). Backup `/opt/appdata/maintainerr/maintainerr.sqlite.bak-20261010-pre-housekeeping`; undo = restore it.
  - **What happens next (Dublin time):** rules re-run 16:00; handler 00:00 tonight unmonitors the 77 Housekeeping seasons (files kept); Checkout deletes ~10-22 (15 seasons); clearing house deletes ~10-25/26 (28 shows, ~480 GB on disk — Maintainerr's own GB figures are ~2× the real size).
  - **Open:** *CIA S1* is a MANUAL member of `Checkout (series)` (added by hand; LCARS has the show *watching*, tagged `ongoing`) — Maintainerr never removes manual members, so it will be deleted ~10-22 unless removed by hand. *KAMUI: He's Behind You* is tagged `purge` but not in the clearing house (not in Jellyfin's listing yet).
- Deploy recipe: snapshot (sqlite online backup inside the lcars container) → back up `~/stacks/starfleet.yml` → `/-web/!` sed pair on all three image lines → `docker compose pull` / `up -d` → verify. SSH: `ssh tiny@192.168.1.77` only.
- Prod facts: `LCARS_EXTERNAL_WRITES=send`, `LCARS_AUTOMATION_FROZEN=0`.

## Shipped 2026-10-07

### v0.4.7 — air-date freshness and precision (live)
Why: the calendar must reflect correct, current air dates and times. Audit found Sonarr's date read once at insert and never again (84 stale raw dates),
TVDB/TVmaze/AniDB only filling empty dates, the TVmaze drip fetching each show once, planned shows never refreshed, and ~17k date-only values stored as real times.
- Migration `b1c2d3e4f5a6`: episode `air_precision` / `air_local_date` / `air_aired_at` (+ Sonarr raw precision); candidate `precision` / `local_date` / `first_air_date_utc`; table `air_candidate_change`.
- `air_time.py`: end of local day (UTC+9 anime, UTC-8 otherwise), shared "has aired" expression `aired_at_sql()`.
- `air_sources.py`: every source's schedule refreshed as diffed candidates; the earliest **timed** candidate is applied when one changes or appears; TV follows Sonarr;
  a chosen schedule applies from then on and fills only dates it lacks; weak sources (Sonarr/TVDB) do not override a curated date >60 days apart; anime episodes aired >45 days ago keep their date.
- Season states: **airing** (daily refresh) / **planned** (weekly) / **history** (never refreshed, never rewritten by any automatic writer, never listed in checks).
  ops sends `schedulesOnly` for PLANNED shows — lcars and ops ship together.
- UI: `!` (another source changed its date by >2 h) and `?` (the chosen schedule changed by >2 h) icons on the calendar and show page; date-only episodes shown all-day on their own local day;
  play triangle on an available episode's art always visible (80 %, full on hover; calendar and backlog).
- Kanojo no Tomodachi's undismissable drift review: drift review gets a choice and is skipped for a chosen season. `AirDateSource` enum gained TVDB (latent API bug).
- Guards (RULEBOOK R1.6a): the AniList writer skips an id another level holds and a schedule that starts before the previous season ended; the provisional plan refuses such a season; rulecheck R1.6-copy.
- Rulecheck: R1.0b-empty, R1.0b-fresh, R1.0e, R1.6-copy.
- Confirmed by the user 10-07 as **decisions (not rules)**: UTC-8 end of day for non-anime, 3-day upgrade window, TV `00:00` = no time (anime `00:00` is real), 45-day history, 60-day Sonarr guard.

### v0.4.8 — live
- Sonarr/Radarr deep links follow a renamed series: the catalog sweep updates a stale `titleSlug` (`write_arr_external_id(replace=True)`, matched by TVDB/TMDB id).
- Fribb id reviews say what happened ("Fribb added MAL ID …"), keep the links, and are **confirmed, not dismissed** (choice `confirm`; `reviews.open_review(previous=…)`; migration `c2d3e4f5a6b7`).

### Prod data actions (all with a snapshot taken first, all on tiny)
| What | Script | Snapshot |
|---|---|---|
| 13 announced sequels carrying S1's premiere date: dates cleared, 60 provisional episodes deleted | `scripts/clear_copied_season_dates_20261007.py` | `lcars.db.bak-20261007-pre-copied-season-dates` |
| Magic Knight Rayearth: AniList id placed by episode range (R1.22b) | by hand in the app / data | `lcars.db.bak-20261007-pre-rayearth` |
| 3 stale Sonarr slugs (Magic Repo Man, The Cold Sato-san…, Beast King War God Dandivine) | `scripts/fix_stale_sonarr_slugs_20261007.py` | `lcars.db.bak-20261007-pre-sonarr-slugs` |
| v0.4.7 deploy | — | `lcars.db.bak-20261007-pre-0.4.7` |

The 3 completed shows holding an unreleased S2 as COMPLETED (A-Rank Party, The Fable, Saijaku Tamer) were set to planned by the user by hand.

## Open / to watch

Priority order (user 2026-10-10):

0. 🔴 **Maintainerr alignment + tag system — BUILT in dev (v0.4.9 candidate), not deployed.** User OK 10-10 to build in this order: (1) optional deep-link cleanup, (2) tag code, (3) backfill, (4) Maintainerr switch.
   - Code (branch `dev-airing-sources`): `src/lcars/arr_tags.py` (tags `keep` yours / `ongoing` = planned+watching+paused, episodic only / `purge` = dropped, purge wins over keep+ongoing; matched by **TVDB id** (TMDB for movies), never by the stored Sonarr slug); Sonarr/Radarr client tag methods; synced on every status change (hook inside `sonarr_sync.apply`, covers all 5 callers) + on movie status changes + hourly (`reconcile_arr_state`); GraphQL `Show.keep` and `setShowKeep`; UI `ui/src/js/keep.js` (★ keep badge on the show page, reminder on add and on completion — **unverified in a browser**). 25 + 2 new tests.
   - Not needed after all: the "un-drop future-only" fix. `_remonitor_in_arr_on_resume` is only reached for movies; episodic un-drop already monitors future episodes only (via `sonarr_sync`).
   - Scripts, dry-run first: `scripts/arr_tags_backfill_20261010.py` (dry run on a prod copy + real Sonarr/Radarr: 147 × `ongoing`, 31 × `purge` = 480 GB = the vetted list, `keep` off Under the Banner of Heaven, nothing else) and `scripts/fix_arr_deep_links_20261010.py` (dry run: 1 slug repointed, 36 wrong rows removed).
   - Deploy order after the user's go: release → backfill `--apply` (tags only, log file for undo) → deep-link cleanup → Maintainerr rules: `Housekeeping` (season, unaired=false, last ep >10 d, has files, no keep/ongoing/purge tag, season monitored → UNMONITOR, 0 d) and `Checkout` (same, season NOT monitored → DELETE, 15 d); clearing house becomes "has tag purge", 15 d. Maintainerr is NOT changed before the tags exist.
   - Data findings 10-10: 8 TMDB ids shared by two tracked movie shows each (skipped by the tagger, logged); Radarr row for *Rascal Does Not Dream of a Dear Friend* matches no TMDB entry; HxH (2011, TVDB 252322) is dropped and not in Sonarr — if it is ever added, "purge wins" would purge it (needs an exemption if unwanted).
1. 🟠 **Android APK rebuild** — high on the list, not for today. The app bundles the UI: it needs a rebuild for the `!`/`?` icons, the always-visible play triangle, the all-day display, and the v0.4.5 page auto-refresh.
2. 🟠 **API hardening: a backup source for every API call** — ongoing. Partly built (this is why most data has several sources); not fully hardened because aligning sources to the exact need is hard.
   Includes the AniList metadata fallback, still scalar-only (relations / studios / genres degrade on an outage; `_fetch_mal_fallback`); franchise function deferred.
3. 🟠 **Data TUI rework** — not for today.
4. 👀 AniDB: weekly refreshes of watching/planned shows only from now on (423 anime in scope; the 200/day cap spreads them — the 10-06/07 batches come due 10-13/14 and take ~2 days, oldest first). Any ban/HTTP error → stop and ask.
5. 🔑 Key rotation at project end (Sonarr/Radarr/TMDB keys, MAL `client_id`, LCARS AniList `client_secret`; secrets out of plaintext `config.ini`) — not there yet.

- 🔴 **Add-check root cause (unexplained, not yet looked at):** on 10-05 the add check proposed "part of S1" for AniList 212144 although TVDB already held S2's 12 episodes (R1.10a says the entry goes to that TVDB season). Find why before the next sequel; explain first, wait for the yes.
- 👀 *With Vengeance* S1 lists: AniList 195209 / MAL 59961 baselines read `completed` with remote progress 0 (the old part level held 0 episodes). S1 now holds 12 watched episodes; a push of progress 12 is offered to the user, not sent.
- Auto-refresh on the desktop page: works, "not perfect but fine enough" (user 10-10) — parked.

### Closed 2026-10-10
- ✅ Legend of Earthsea S1 set completed (2 episodes watched), snapshot `lcars.db.bak-20261010-pre-earthsea`. Benidorm Is Murder: already in LCARS + Sonarr, S1 fully watched — nothing to do.
- ✅ *With Vengeance, Sincerely, Your Broken Saintess*: S1 set completed by the user; the empty "part 2" (it held S2's AniList 212144 / MAL 64180 / Syoboi 8062) and part 1 removed, ids placed on TVDB S1 (195209 / 59961 / 7531) and S2 (212144 / 64180 / 8062, watching, list sync on), AniList + MAL for S2 written back to watching, progress 1.
  Script `scripts/vengeance_levels_20261010.py`, snapshot `lcars.db.bak-20261010-pre-vengeance-s2`. Rulecheck 0 violations afterwards. (Marking part 2 completed had written completed/0 to the S2 entries at 06:35Z.)
- ✅ v0.4.8 deployed; alembic head `c2d3e4f5a6b7` confirmed on prod.
- ✅ Kanojo no Tomodachi drift review `r-npgwej`: resolved 10-07 23:57Z ("the season follows a schedule you chose").
- ✅ Review `r-gv6b0m`: dismissed by the user 10-07 14:06Z (before v0.4.8), so no Confirm form needed.
- ✅ Ops list-hub watch (item 1 of the 10-06 must-watch list): healthy.
- ✅ `!` / `?` change icons: appear fine (user).
- ✅ Occasional AniList 429 / connection errors: AniList-side, closed (the remedy is the API-hardening item above).
- ✅ Scratch preview server on 8891: not running (checked local + tiny).
- ✅ AniDB drip finished. The 138 width checks (closed as information, R1.11w, 09-30/10-06) and the 131 Fribb-unmatched (closed 10-05 "AniList ids live on seasons, R1.23; information only") are no longer reviews.
- ✅ Findings (no change): the 12:40 outage coincided with a Memory Alpha pass of 74 items (the user's own date import); Rayearth ep 1's file arrived ~21 h early from a ToonsHub streaming rip.

## Parked ideas

- **Manual date offset on a show** and a **personal / catch-up schedule** for re-watching (user 10-07: "neither for now"). The backend paced mode already exists (A.10: `enablePacedMode`, `pacedNextDate`, used only by Next Up, no UI, none enabled on prod) — it could be surfaced on the calendar.
- Streaming-source air times (Bilibili, Crunchyroll, Aniplus, Ani-One, Prime, Muse, Hi-Dive…) as extra schedule sources — cost investigated 10-07, not built.
- Unified list page (fold Lists, Backlog, Grabs, Browse, Add into one page with preset views) — the long-term direction.
- Discover page (ops pre-creates untracked stubs from upcoming AniList/TMDB seasons; promote to PLANNING or SKIP).
- Statistics page (counts by status, watch history over time, score distribution, genres, runtime).
- External-id season-level display (show season-specific AniList/MAL ids; today "AL" on S2 opens S1's page).
- Memory Alpha browse prefill + add-confirmation popup.
- Art-fetch leftovers: negative cache for unfindable art, staged/throttled auto-fetch, manual pulls take priority (memory `art-fetch-negative-cache-and-throttle-plan`).
- IMDB datasets for ID bridging; browse by studio; direct TVDB search; livechart.me delay headlines; Grabs screen: title + episode only with an mpv launch key; Data recent-downloads page.
- Service icons white until a hard refresh — parked (not appearing lately).

## Standing rules (from the user)

- Never touch prod without consent; explain before building; code changes need validation first; answer questions before checking.
- Rules are absolute (RULEBOOK.md); decisions are open to re-discussion — never mix them up.
- History seasons/shows (aired fully, not planned) are never targeted by refreshes or writers and are never surfaced in checks.
- Build on validated data only; verify before claiming fixed (observe the symptom gone, or say "unverified").
- Show the todo list with status emoji after each work round.
