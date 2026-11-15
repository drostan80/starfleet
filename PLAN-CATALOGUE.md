# PLAN — Catalogue ("Daystrom", working name)

Status: **PROPOSAL, nothing built.** Written 2026-11-15. Every item marked **[RULE-PROPOSAL]**
is a proposed rule: it becomes a rule only when the user approves it into RULEBOOK.md.
Nothing here writes to `lcars.db`, the lists, Sonarr/Radarr or any outside service until approved.

## 1. Purpose

A second, separate database, the **catalogue**, holding the barest data needed to discover and
start adding anime fast. `lcars.db` stays the only place validated data lives.

- Fast, complete, local discovery of new shows (no outside call per search).
- "Newly added" lists (new series, new seasons) from a `first_seen` date.
- Far fewer outside calls on add: the catalogue already holds IDs, season data and one card image.
- A reconciled base that Memory Alpha can read for its cross-source mapping.

Not a goal: faster pages for shows already in Starfleet (they already read `lcars.db`).

## 2. Name

Working name **Daystrom** (the Daystrom Institute is the Federation's technology archive; Memory
Alpha is the library, LCARS the hub, Data the TUI). Alternatives: Starbase, Archive.
The name is one constant (`catalogue.db`, package `src/lcars/catalogue/`), cheap to rename now,
costly later.

## 3. Shape

Two databases, one-way flow:

| | Catalogue (`catalogue.db`) | Starfleet (`lcars.db`) |
|---|---|---|
| Holds | series, season, links, external IDs, one card image, tier | everything validated: episodes, air dates, full art set, links |
| Episodes | **none** | yes, loaded on add through the existing validated path |
| Written by | catalogue fetchers and refresh only | as today |
| Art | one cover per season/series (see §5) | full copy on add (card, banner, season art) |

- Flow is **catalogue -> Starfleet only**. The catalogue refresh never overwrites validated data.
- The catalogue gets an `in_starfleet` flag by ID so discovery can mark or hide what you have.
- A failed or bad catalogue fill cannot touch the library: it is a different file.

## 4. Tables (catalogue.db)

- `tvdb_series` (tvdb_id, name, status, first_aired, last_aired, genres, overview, imdb_id, tmdb_id, updated)
- `tvdb_season` (series, number, type, episode_count, first_aired, last_aired)
- `anilist_media` (anilist_id, mal_id, anidb_id, titles, format, episodes, status, start/end date, season, relations, updated)
- `link` (anilist_id <-> tvdb_series/season, episode range, source, tier, checks_json, validated_at)
- `ext_id` (entity, service, id)
- `art` (entity, kind cover/banner/poster, url, local_path, hash)
- `source_row` (raw rows from Fribb/anime-lists, for audit)
- `first_seen`, `last_seen` on every entity.

Episode-level tables do not exist here. Counts come from the season records.

## 5. Art

- Catalogue keeps, per entity, the **large cover** (AniList large cover / TVDB poster) **and the banner
  if one exists**, stored locally. Estimated 6 to 8 GB for about 20k entries. Fanart is not fetched.
- On add, the full art set is copied into Starfleet storage (card, banner, season art), through the
  existing art fetch with its negative cache and throttle.
- **Decided 2026-11-15:** large cover + banner if it exists. Check free disk before phase 3.

## 6. Sources and cost

- **Seed (no API calls):** Fribb and anime-lists (`src/lcars/fribb.py`, `anime_lists.py` already
  exist) give the cross-ID spine and the candidate links, flagged unvalidated.
- **AniList:** about 400 paged requests (50 per page), 5 to 20 minutes, a one-off; then a daily
  incremental by last-update.
- **TVDB (series + seasons only):** roughly 20k requests, 1 to 3 hours as a slow drip, with back-off
  on 429. Daily incremental via the updates endpoint.
- **Not fetched:** TVDB episodes (6 to 12 h of drip avoided), AniList episodes (AniList has none),
  AniDB episodes (ban risk avoided).
- **Open check before any bulk TVDB fill:** TVDB's licence terms on bulk copying. Not yet read.

## 6b. Terms check (2026-11-15) — BLOCKS the bulk fetches in phases 3 and 4

- **AniList** (API terms of use, docs repo): "Using the AniList API as a backup or data storage service
  is strictly prohibited." and "**Hoarding** or mass collection of data from the AniList API is strictly
  prohibited." Also bars use in competing anime/manga list or tracker services. (anilist.co/terms itself
  returned 403 to the fetch; the text is from the official ApiV2-GraphQL-Docs repo, terms-of-use.md.)
  => the ~400-request bulk AniList fetch and the bulk cover/banner download are **not allowed**.
- **TVDB** (api-information + tos pages): no explicit bulk or caching rule, but "you may not make
  excessive calls to the API, as determined by us"; licence is "personal, non-commercial use";
  cannot redistribute the Data; the API licence "does not give you the right to use or display
  images"; attribution with a link to TheTVDB is required for end users.
  => a 20k-series sweep is a grey area decided by TVDB; bulk art copies are not covered by the licence.
- **Consequence for the plan:** the catalogue's source must be the community datasets (Fribb,
  anime-lists, and a check of the licence of anime-offline-database), NOT bulk API sweeps. AniList and
  TVDB are called per show, on add or when the user opens an entry, as Starfleet does today. Art is
  copied per added show only. Needs the user's decision before phases 3 and 4 are rewritten.

## 7. Tiers

| Tier | Meaning | Who sets it |
|---|---|---|
| `barebone` | not reconciled, or reconciled with problems | default |
| `auto_validated` | independent sources agree on every check in §8 | the reconcile job |
| `hand_validated` | you reviewed and approved it | you |

The two validated labels stay separate so the machine's passes can always be audited. Anything that
fails any check stays `barebone` until real work is done on it. Nothing leaves a tier silently:
every change is logged with the check that caused it.

## 8. Auto-validation criteria (approved 2026-11-15; goes into RULEBOOK.md before phase 5)

A link (AniList media <-> TVDB season) is `auto_validated` only if **all** hold:

1. **Independent agreement:** Fribb, the AniList relation chain and TVDB agree on the mapping.
   Fribb alone is never enough.
2. **Season count** agrees across the sources for the show.
3. **Episode count** of the season agrees between AniList and TVDB (exact, no tolerance).
4. **Start date** agrees within 3 days between AniList and TVDB.
5. **Finished and in the past:** status finished/ended on both, last air date before today minus
   30 days. No airing, announced, hiatus or unreleased entries.
6. **No known hard case:** not a split-cour, not a sequel-as-season (episode-atomic rule), not
   flagged by the sequel audit, no TVDB id replacement pending, not a special/OVA/film link.
7. **Unique:** the TVDB season maps to exactly one AniList media for that episode range.

Thresholds (3 days, 30 days) are proposals for you to change.
A show is `auto_validated` only if every season link of it is.

## 9. Promotion on add

- `auto_validated` / `hand_validated`: the add reads season-level data from the catalogue, loads
  episodes from TVDB through the normal validated path, then runs the existing checks.
- `barebone`: the add shows the problem list and falls into the existing review flow.
- Episodes are never copied from the catalogue (it has none). Optional later: pre-fill settled
  shows in the background, only if the add step feels slow.

## 10. Build phases (each ends with your validation before the next)

1. **Skeleton and name:** `catalogue.db`, migrations, config path, empty tables, tests. No data.
2. **Seed from Fribb / anime-lists:** ID spine and candidate links, all `barebone`.
3. **AniList fetch:** media rows and covers, resumable, with back-off; daily incremental.
4. **TVDB fetch (series + seasons):** after the licence check; slow drip; daily incremental.
5. **Reconcile job:** §8 checks, tiers, per-link `checks_json`; sample review page of
   `auto_validated` links in the "no click = OK" style; **no promotion counts as final until you
   have looked at the sample.**
6. **Discovery page:** local search/filter over the catalogue, "newly added" strip, marks shows
   already in Starfleet.
7. **Add flow:** add from a catalogue entry (uses stored IDs and season data).
8. **Memory Alpha reads `link`** as its basis (separate plan, after validation).

## 11. Risks

- **Drift** between two copies of the truth: prevented by the one-way flow and the `in_starfleet` flag.
- **Fribb errors:** why §8 needs independent agreement.
- **Disk:** about 6 to 8 GB with large covers and banners; check free space on tiny before phase 3.
- **Rate limits / bans:** drips with back-off; AniDB is not called at all.
- **Scope:** several sessions; phases 1 to 3 are the cheap, useful start.

## 12. Decisions

1. Name: **Daystrom** (approved 2026-11-15).
2. Catalogue art: **large cover + banner if it exists** (decided 2026-11-15).
3. §8 criteria and thresholds (3 days, 30 days): **approved 2026-11-15**; to be written into RULEBOOK.md as rules before phase 5.
4. TVDB licence terms checked **before** phase 4 (approved 2026-11-15).
5. File location: built **here** (dev machine); once populated, decide where it lives on tiny and copy it there (decided 2026-11-15).
