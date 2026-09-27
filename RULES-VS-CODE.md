# Rules vs code — discrepancies (first pass, 2026-09-27)

Checked against `RULEBOOK.md` (rule ids R#, questions Q-x in its §9).
Read-only pass over `src/lcars` at `e02919e` (v0.2.71 tag). Only verified facts
are listed; each is tagged **violates R#** or **depends on Q-x**. No fix is
proposed here; the code plan and data plan come after the Q answers.

## A. Episode numbering and season 0

| # | Code today | Rule | Tag |
|---|---|---|---|
| A1 | Season 0 kept: `episode.season = 0` for specials; `_ensure_seasons` skips 0 (`metadata.py:1733`), so specials have `season_id = NULL`; `_compute_show_status` excludes season 0 (`resolvers.py:658`). | R1.0, R1.8 — every episode (season 0 included) has an abs number and is mapped; TVDB season 0 is source numbering only | violates R1.0, R1.8 |
| A2 | Absolute number comes from Sonarr/TVDB `absoluteEpisodeNumber` (`metadata.py:1386`); AniDB is only compared, into `pending_review` (`anidb.py:945`). | R1.2, R1.18 — AniDB + Fribb are the source of truth (anime) | violates R1.18 |
| A3 | Anything without a source number gets `preceding + n/10` by air date (`metadata.py:1687`): always a decimal, never a whole number between seasons, film never whole. | R1.3–R1.5, R1.8a/b | violates R1.4, R1.8b; scheme depends on Q-E, Q-F |
| A4 | TV absolute numbers: same Sonarr path; TVmaze is used for air dates only. | R1.19 — TVmaze is the TV source | violates R1.19 |
| A5 | Films integral to the story are separate `show` rows (`media_shape = movie`), linked by `episode_movie_link` only when also a Sonarr special. | R1.4 — film takes an abs number inside the show | violates R1.4 |

## B. Season / sub-season / show structure

| # | Code today | Rule | Tag |
|---|---|---|---|
| B1 | A season is one span: `season.abs_start` / `abs_end` INTEGER. | R1.11–R1.12 — one or more spans, decimal-capable | violates R1.12 |
| B2 | Parts are rows `(season_number, part_number)`; there is no row for the TVDB season itself above its parts, so the TVDB-season level cannot hold its own status. | R2.7 caveat — every level with an id holds status; R1.10 | violates R2.7 caveat; depends on Q-H |
| B3 | `episode.season_id` is a single FK; episode → part vs → TVDB season vs → other source's id is not modelled separately (only `episode_external_id` per service). | R1.16 — episode mapped to show, season, part, other-source season | partial; depends on Q-H |
| B4 | AniList/MAL ids stored at show level too (`show_external_id` service `anilist`/`mal`, written by `create_show` `shows.py:708`, `_create_relation_stub` `metadata.py:1021`). | R1.23 — AniList is always season level | violates R1.23 |
| B5 | No "individual season" (season without a TVDB show): `season.show_id NOT NULL`; anime shows are created with no TVDB id instead. | R3.2, R3.6, R3.6a | violates R3.2, R3.6 |
| B6 | Untracked **stub shows** created for AniList relations (`_create_relation_stub`, `tracked = 0`, `metadata.py:979`). | R3.5 — related, different TVDB id, not tracked → not added at all | violates R3.5, R3.5a |

## C. Status

| # | Code today | Rule | Tag |
|---|---|---|---|
| C1 | `new_season_status` (`season_ranges.py:93`): finished + previous watching/completed/paused → **paused**; previous dropped → **dropped**; unfinished → planned. | R2.16 — previous completed/watching/planned → planned; paused/dropped/skipped → skipped | violates R2.16, R2.9 |
| C2 | `inherit_season_status` (`season_ranges.py:38`): show paused/dropped but in Sonarr → **planned**; else inherits paused/dropped. | R2.16 | violates R2.16 |
| C3 | Auto-created seasons get `list_sync = 0`: never pushed to AniList/MAL (`auto_season_fields`). | R4.5 — an LCARS change is mirrored | violates R4.5, R2.16 (planned seasons are mirrored; only skipped is not) |
| C4 | `_compute_show_status` (`resolvers.py:658`): highest season planned + more than one season → **watching**; skipped seasons not excluded; highest by `season_number`. | R2.13, R2.17 — last non-skipped season; new planned season → show planned | violates R2.13, R2.17 |
| C5 | `_recompute_show_status` never changes a `skipped` show ("tombstone", `resolvers.py:759`). | R2.13 — show derived from seasons | violates R2.13 |
| C6 | `setStatus` on a show writes `show.status` directly and overwrites only the highest season (`resolvers.py:3364`). | R2.13a — a show pick applies to the last non-skipped season | violates R2.13a |
| C7 | `_reopen_show_if_completed` → **watching**, only on the manual `setSeasonMapping` path (`resolvers.py:843`). | R2.17 — new planned season → show planned | violates R2.17 |
| C8 | Season planned + episode watched → nothing (`addWatchEvent` `resolvers.py:4061` only tries completion). | R2.14 — → watching | violates R2.14 |
| C9 | Manual completed marks only **aired** episodes watched (`_bulk_mark_all_aired_episodes_watched`); season completion refused while airing unless confirmed. | R2.7 — all episodes of the level watched; R3.4 warning mentions unaired episodes | violates R2.7 |
| C10 | Completion keyed on `episode.season` (TVDB number), not per id level (part, special, AniList entry). Progress pushed to AniList = `MAX(episode)` within the season number (`watch_reconcile.py:131`) — wrong for a part-2 id starting at S02E14. | R2.7 caveat, R2.15, R1.16 | violates R2.15 |
| C11 | Episode `state` has a third value `skipped`, counted as done for completion. | R2.2 — watched is a boolean | violates R2.2 |
| C12 | Auto-complete refuses a season set paused/dropped (`_try_complete_season`). | R2.15 — all watched → completed, paused/dropped too | violates R2.15 |
| C13 | `status_before_pause` restores the pre-pause status on resume. | not in rules (R2.13a replaces it) | to remove? |

## D. Adding

| # | Code today | Rule | Tag |
|---|---|---|---|
| D1 | `create_show` does not require a TVDB id and inserts `status = 'planned'` for every add path. | R3.1, R3.2 | violates R3.2 |
| D2 | Sonarr add (webhook / `reconcile_arr_state` → `create_show`): all seasons end up planned (`is_users_own_season` → inherit → planned; others planned via C1). | R5.3 — several seasons, none tracked → show planned, all seasons skipped | violates R5.3 (latest season planned, earlier skipped) |
| D3 | AniList/MAL list entries not in LCARS are only recorded as findings (`untracked_sweep.py`); adding is the manual `backfillUntrackedShows`. | R4.7 — added through the normal check | violates R4.7 |
| D4 | `pollMemoryAlpha` step 2g auto-merges shows sharing a TVDB id into one show as a new season (`anidb.py:1765`, `show_merge.detect_franchise_collisions`); target season = next number, not decided per episode. This is the 09-06 merge of 178 shows. | R1.17 — reconciliation at episode level; R3.1 | violates R1.17 |
| D5 | Relation types: `_propose_sequel_seasons` looks at SEQUEL only; stubs are made for other relation types too. | R3.5a — relation type irrelevant; TVDB id decides | violates R3.5a |

## E. Mirroring (AniList / MAL)

| # | Code today | Rule | Tag |
|---|---|---|---|
| E1 | Conflict model is a `list_baseline` (last agreed value) per entry, not a timestamp comparison. | R4.10 — remote change timestamp vs reconciliation timestamp | violates R4.10 |
| E2 | AniList activity poll and MAL poll are independent loops (`ops/scheduler.py`); no "propagate before the next check" ordering. | R4.9 | violates R4.9 |
| E3 | `skipped` is not pushed (maps to nothing) — matches R4.6. An existing remote entry for a now-skipped season is left as is. | R2.10, R4.6 — auto-added planned → skipped is deleted remotely | violates R2.10 |
| E4 | AniList `REPEATING` → LCARS watching. | R4.6a — rewatching → watching | consistent |
| E5 | A season with ids claimed by two tracked seasons is excluded and flagged. | R1.22 | consistent |

## F. Sonarr

| # | Code today | Rule | Tag |
|---|---|---|---|
| F1 | Sonarr monitored flag no longer changes LCARS (removed 09-26). | R5.4 | consistent |
| F2 | Plain `addShow` does not add to Sonarr; only `addShowWithArr` does. | R5.5 | violates R5.5 |
| F3 | Resume from paused/dropped re-monitors the series **and every season** (`_remonitor_in_arr_on_resume`), not future episodes only; skipped does not unmonitor. | R5.6, R5.8 | violates R5.6, R5.8 (per season: dropped season + later ones only) |
| F4 | Status change to planned/watching on a show not in Sonarr: nothing is added (resume path only flips existing entries). | R5.6 | R5.9 — pre-existing show never auto-added | consistent |
| F5 | `ensure_arr_monitored` (setSeasonMapping new season) **adds** the show to Sonarr if missing, with `monitor: future` + search. | R5.9 — pre-existing show never auto-added | violates R5.9 |

## Added after answers (2026-09-27)

| # | Code today | Rule | Tag |
|---|---|---|---|
| G1 | No "skipped = not followed" behaviour: skipped seasons still get episodes, air dates, availability, calendar/next-up. | R2.10 | violates R2.10 |
| G2 | No cascade from TVDB season to parts (with completed parts kept) and no season status aggregated from parts. | R2.18 | violates R2.18 |
| G3 | Episode numbering is not LCARS-owned: Sonarr's absolute number is stored as the LCARS number. | R1.2a | violates R1.2a |
| G4 | Decimal synthesis: `.1` steps match R1.2b, but whole numbers are never inserted between seasons (see A3). | R1.2b, R1.8b | partial |

| G5 | Earlier seasons found late are created planned (C1/C2), and seasons with no Sonarr episodes are not fetched for numbering. | R2.19 | violates R2.19 |
| G6 | Unmonitor on drop is whole-series (`_unmonitor_in_arr_on_drop` sets every season false). | R5.8 — dropped season and later only | violates R5.8 |

## Not in the rules (to confirm they stay)

- Automated show-merge detection (review-only now, `pollShowMerges`).
- Scores (the user said scoring rules are separate).
- Paced mode, `tracked = 0` soft delete, hard delete.
