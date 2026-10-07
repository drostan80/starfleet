# Next up

Compacted 2026-10-07. The full previous NEXT_UP (every v0.2.x–v0.4.x section, ship checklists, build notes) is kept unchanged in
**NEXT_UP-ARCHIVE-2026-10-07.md**. Records: PLAN-DATA.md "Cutover (2026-09-30)", HANDOFF-2026-09-30-CUTOVER.md, HANDOFF-2026-10-06.md.
Rules live in RULEBOOK.md (absolute); items marked "decision" below are open to re-discussion.

## State

- **Prod (tiny): v0.4.7 live** (2026-10-07). Alembic head on prod: `b1c2d3e4f5a6`.
- **v0.4.8: tagged** (`v0.4.8` = `5f4655d`, branch `dev-airing-sources`), CI run 37643897144 **running** at the time of writing.
  **Not deployed — deploy only after the user says CI is green** (verify by job conclusions: `test success` + `docker success`).
- Deploy recipe: snapshot (sqlite online backup inside the lcars container, name `lcars.db.bak-20261007-pre-0.4.8`) → back up `~/stacks/starfleet.yml`
  → `/-web/!` sed pair on all three image lines → `docker compose pull` / `up -d` → verify. SSH: `ssh tiny@192.168.1.77` only.
  After the deploy check: alembic head `c2d3e4f5a6b7`, ops POST 200, web 302, review `r-gv6b0m` now shows the Confirm form.
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

### v0.4.8 — tagged, awaiting CI + go
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

- **Deploy v0.4.8** once the user confirms CI green (see State).
- Kanojo no Tomodachi drift review `r-npgwej` should close itself on the first AniList pass after the v0.4.7 deploy — **not yet seen closing**.
- Review `r-gv6b0m` (a `mal_id` Fribb review created before v0.4.7) converts to the Confirm form with v0.4.8; the user then confirms it.
- `!` / `?` icons only appear for changes seen after each show's first refresh.
- **Android APK rebuild** (user: another time): the app bundles the UI — it needs a rebuild for the `!`/`?` icons, the always-visible play triangle, the all-day display, and the v0.4.5 page auto-refresh.
- Auto-refresh works in the app but not the desktop page (user 10-05, not diagnosed): candidates — stale cached JS, `viewerIsBusy()` true on desktop, only a 60 s tick with no visible stamp. Test: dispatch `starfleet:auto-refresh` in the console.
- Findings reported, no change: the 12:40 outage coincided with a Memory Alpha pass of 74 items (the user's own date import); Rayearth ep 1's file arrived ~21 h early from a ToonsHub streaming rip (no public source lists it; "marker not needed").
- A scratch preview server may still run on port 8891 (staged-icon prod copy) — stop it when the user says.
- AniDB drip: running, 200/day cap (the user's cap, not a known AniDB limit; ban seen ~250 requests per VPN IP on 09-28). Re-look at the 138 width checks and 131 Fribb-unmatched once it finishes.
- Data TUI rework.
- Key rotation at project end (Sonarr/Radarr/TMDB keys, MAL `client_id`, LCARS AniList `client_secret`); secrets out of plaintext `config.ini`.
- AniList metadata fallback is scalar-only (relations / studios / genres degrade on an outage); franchise function deferred.

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
