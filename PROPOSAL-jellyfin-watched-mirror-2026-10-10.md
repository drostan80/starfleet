# Proposal: LCARS marks episodes and movies watched in Jellyfin (2026-10-10)

Status: **proposal, nothing built.** Needs your validation (RULEBOOK §7: explain the change and its
consequences first, then wait for the yes). Read-only facts below were measured on prod on 2026-10-10.

## 1. What and why

You watch through LCARS and mpv (the Grabs screen launches mpv locally), so Jellyfin never sees the play.
Anything that reads Jellyfin's watch state is then blind to what you have watched: Maintainerr's
`watched movies` rule (movie seen by user `media`) and its watched properties (`seenBy`, `lastViewedAt`,
`sw_viewedEpisodes`), and Jellyfin's own "Continue watching" / "Next up".

Proposal: LCARS, the source of truth (R4.3), writes its watched state to Jellyfin for the user `media`.
One direction only: LCARS → Jellyfin. LCARS never reads Jellyfin's watch state, so there is no loop
and no flip-flop to protect against (R4.9/R4.10 stay untouched).

## 2. What the data says

| Fact | Number |
|---|---|
| Episodes LCARS has watched | 29,998 (36,340 watch events, 1,409 shows) |
| ... of those with a file on the server (the only ones Jellyfin can mark) | **4,328** |
| Completed movies with a file | 5 |
| Watched marks LCARS makes per week lately | ~238 |
| Jellyfin's watch records today (all libraries, user `media`) | 3,429 over 3,536 items (your plays inside Jellyfin) |

So the one-off catch-up is about 4.3k episodes and 5 movies; after that it is a few dozen marks a day.

**Effect on Maintainerr of the catch-up** (the only watched-based rule today is `watched movies`):
of the 5 movies, 4 carry `keep` (Ghost in the Shell, Dead Leaves, Star Wars: Revenge of the Sith,
A Knight's Tale) and VIRGIN PUNK Clockwork Girl is already in the `watched movies` queue. **No new deletion.**
Episode rules (Housekeeping / Checkout) do not read watch state.

## 3. Draft wording for the rulebook (yours to change; I would record it as a *decision*, not a rule)

- **R4.11 (draft) LCARS marks what you watched in Jellyfin.** An episode (or movie) that LCARS holds as
  watched and that exists as a file on the server is marked played in Jellyfin for the user `media`,
  with the time you watched it. When LCARS un-watches it, Jellyfin's mark is removed. LCARS never reads
  Jellyfin's watch state and never writes anything else to Jellyfin.
- **R4.11a (draft)** Episodes are matched to Jellyfin by the show's TVDB id plus the TVDB season and episode
  numbers (the same numbering Sonarr names the files with). A match that is not exact is reported, never guessed.

## 4. Design

**Mechanism: a periodic difference sync, not a hook on every writer.** Episodes become watched in about ten
places (`addWatchEvent`, `markSeasonWatched`, a season set completed, AniList/MAL reconcile, un-watch from the
list hub ...). Hooking each is how a path gets missed. Instead, like the hourly tag reconcile:

1. A small table `jellyfin_watch_sync (episode_id, pushed_state, pushed_at)` remembers what LCARS last sent.
2. An ops task (every ~10 minutes) compares LCARS's watched state of *available* episodes/movies with it and
   sends only the differences: mark played (with `watched_at`) or mark unplayed.
3. A file that arrives later (new import) is simply a new difference on the next pass.

**Matching.** Series by `AnyProviderIdEquals=tvdb.<id>` (movies by `tmdb.<id>`), then the series' episodes
by `ParentIndexNumber` / `IndexNumber` against `episode.sonarr_season` / `sonarr_episode`. Unmatched
episodes go into a report, not a guess. Jellyfin item ids are cached per show.

**Calls (to be confirmed against the server's own OpenAPI document before any code is written):** mark
played / unplayed for a user (`POST` / `DELETE` on the user-played-items endpoint, with `datePlayed`).
Jellyfin runs version 12.1.0.

**Safety, same as the other mirrors:** best effort, never blocks a watch; `external_writes` capture mode
applies (nothing is sent in dev); a failure is logged and retried on the next pass; a service-health entry
for Jellyfin; rate-limited batches.

**Credential.** A dedicated Jellyfin API key for LCARS (Dashboard → API Keys), kept in LCARS config like the
Sonarr key and never shown in clients. The key stored in Maintainerr was rejected with HTTP 401 when I tried
it, so I will not reuse it.

## 5. Consequences

| Area | Effect |
|---|---|
| Jellyfin, user `media` | Watched episodes/movies show as played; "Continue watching" and "Next up" stop offering them. |
| Maintainerr | Its watched properties become accurate. No rule changes; no new deletion today (section 2). |
| AniList / MAL / Sonarr / Radarr | Nothing changes. |
| LCARS data | One new table; no existing table changes; one new migration. |
| Reads | LCARS only reads Jellyfin to find item ids. It does not import watch state. |
| Failure | Jellyfin down means marks are delayed, not lost (the difference sync retries). |

## 6. Plan and checks

1. **Read-only:** Jellyfin client (find series, list episodes, read user data) and a *dry-run report*: how many
   episodes would be marked, how many already are, and the unmatched list. Nothing written.
2. **One show** on prod, you look at it in Jellyfin.
3. **Catch-up** of the 4.3k (batched, resumable), then the periodic task on.
4. **Tests:** matching (numbering, specials, anime), difference sync (mark, un-mark, late file, failure and retry),
   capture mode sends nothing.
5. Deploy only on your go, snapshot first, as usual.

## 7. Decisions I need from you (with my recommendation)

1. Direction: LCARS → Jellyfin only? **Yes.**
2. Scope: only episodes and movies that have a file, and only the user `media`? **Yes.**
3. Un-watching in LCARS un-marks it in Jellyfin? **Yes.**
4. Mechanism: the periodic difference sync (above) rather than hooks? **Yes.**
5. One-off catch-up of the existing 4.3k, after a dry run and one show? **Yes.**
6. A dedicated Jellyfin API key for LCARS: will you create one and put it in LCARS's config?
7. Rulebook: record R4.11 as a rule, or as a decision (open to discussion)? **Decision.**

## 8. Open points and risks

- Anime numbering: Sonarr names files by TVDB season/episode, and `sonarr_season` / `sonarr_episode` hold the same
  numbers, but the dry run is what will show how many do not match. Specials (season 0) may need care.
- Jellyfin versions: the endpoint names differ between releases; confirmed against the running server first.
- A show present in Jellyfin without TVDB ids (as KAMUI was until today) cannot be matched until it is identified.
- Plex is also installed on tiny; this proposal does not touch it.
