# Handoff — 2026-09-28 (stopping point) — Phase 9 — READ FIRST

Previous: `HANDOFF-2026-09-27.md` (phases 2–8). Code phases 2–8.8 and **9.0** are done;
**9.1 (the rebuild script) is half built: stages 1–6 run on the real data, 7–9 not written.**
Read this file in full, then `RULEBOOK.md` in full, then `PLAN-DATA.md` §1–§3, "Rebuild order
constraints", "Phase 9.1 decisions" and the watch log.

---

## 0. How to work with this user (non-negotiable)

- **`RULEBOOK.md` is the authority**, names included. Never derive a rule. Unsure → add a
  question to its §9 and ask; answers go into the rules and the §10 changelog.
- **Every code change is validated first**: explain what changes and its consequences
  (behaviour, data touched, what reaches AniList/MAL/Sonarr, what breaks), then wait for a
  yes. Anything added after an approval is flagged as such.
- **Answer questions before acting.** Check or fix only when asked.
- **Verify before claiming.** "Fixed" = the symptom seen gone; otherwise say "unverified".
- **Read-only by default.** Prod *reads* are allowed. No write to AniList, MAL, Sonarr,
  Radarr or the prod DB until the user approves the exact write list (9.2).
- Keep `.venv/bin/pytest -q` (≈9 min, 1,548 tests) and `.venv/bin/ruff check .` green on
  every commit. Commit trailers: see the session's attribution instructions.
- **Long lists go on a review page** (artifact); URLs in a plain code block. On review pages
  **OK is the default**; the user only clicks "not OK" (+ note); no click = OK.
- **Log every watch the user mentions** in PLAN-DATA → "Watches during the rebuild", commit.
  (The user said they **won't watch anything until the new setup is live**.)
- **SSH: always `ssh tiny@192.168.1.77`** (ISP changed 09-28; never `drostan@`, never bare
  `tiny`, never the old 192.168.0.152).
- **AniDB: paused by the user. Remind them on 2026-09-29** to resume at **max 200 requests/day
  from one IP** (`ANIDB_DAILY_CAP=200`). Never rotate IPs/VPN exits to dodge bans.
- Show the todo list (✅/🔄/⏳/🔔) at the end of each work round.

## 1. Where things are

- Repo `~/repos/starfleet`, branch **`rulebook-rebuild`** — 84 commits ahead of `main`, **not
  pushed, not deployed**. Migration head **`a1b2c3d4e5f7`** (captured_write), before it
  `f0a1b2c3d4e5` (season_status_change.show_id nullable).
- Data TUI `~/repos/data`, branch `rulebook-rebuild` (0258d2c labels, 683e328 default host
  192.168.1.77). Not pushed. **Data gets a rework after cutover** — don't polish it now.
- Docs (repo root): `RULEBOOK.md`, `RULES-VS-CODE.md`, `PLAN-CODE.md` (phases + done-notes),
  `PLAN-DATA.md` (starting point, every decision, rulecheck baseline, watch log).
- **Rebuild workspace `~/starfleet-rebuild/`** (not in the repo):
  - `snapshot-20260906-0853Z.db` — the 09-06 base (read-only; a WAL `cp`, real state 08:53Z)
  - `live-20260928T1951Z.db` — fresh `.backup` of live, 09-28 19:51Z (read-only)
  - `run1/01-base.db … 06-statuses.db` — labelled checkpoints; `work.db`; `ledger.jsonl`
    (every decision: applied / deferred / review, per stage); `pending.jsonl` (work for later
    stages: list deletes, Urusei Yatsura films); `anilist_facts.json` (AniList facts cache)
- **Review data `~/starfleet-review-2026-09-27/`** (not in the repo):
  - `decisions-final/` — both review pages' decisions, exported 09-28 (162 + 228)
  - `normalize_decisions.py` → `rebuild-inputs/decisions.json` — every decision as an
    explicit action (PLAN-DATA's transcription wins over ambiguous notes; 2 differences
    flagged: Chiba Densetsu 443355 vs note 443365, Urotsukidoji own show vs note 17387)
  - `page_data.json`, `season_rows.json`, `anilist_list.json` (the user's list, `data` →
    `MediaListCollection`), `anidb_xml/` (506 answers), `arr_catalog.json`, `tvdb_*.json`
  - `tools/` — `ui-server.sh` (safe UI server, port 8890), `shot.py` (headless Firefox),
    `ui-dev.db`
- Review pages (decisions in their `decisions` collection): phase 5
  https://claude.ai/artifact/3gXa5fHHeScXr66SvCLKJF ; data review
  https://claude.ai/artifact/XMJyKsTFySwzgqq3MthvSa
- Prod: v0.2.70 (`lcars`, `starfleet_web` on tiny); ops container stopped; v0.2.71 tagged,
  never deployed. Prod DB `/opt/appdata/lcars/db/lcars.db`. Deploy: tag → GHCR → bump the
  pinned tag in `starfleet.yml` on tiny — each step needs the user's yes.
- **This machine's `~/.config/starfleet/lcars/lcars.ini` still has tiny's old IP.** Don't
  edit it; run the rebuild with `LCARS_SONARR_URL=http://192.168.1.77:8989
  LCARS_RADARR_URL=http://192.168.1.77:7878` (keys come from the file; never print them).

## 2. Code map

| Area | Module | Notes |
|---|---|---|
| Numbering, levels, spans | `numbering.py` (`lcars numbering`) | TVDB order + air date, reconciled with AniDB/TVmaze (R1.2d) |
| Status engine | `status_rules.py` | R2.7, R2.13–R2.19; `after_episodes_changed`; `NeedsConfirmation` |
| Add check | `add_check.py` | `classify` (now `exclude_season_id`), `apply_decision`, individual seasons |
| TVDB vetting | `tvdb_vetting.py` | 8.8 guesses/hard stops/`join`/`_merge_into` |
| Consolidation | `consolidation.py` | `apply_group(conn, tvdb, dataset, placed=None)` — `placed` overrides Fribb places; a linked loser season now keeps its status |
| Reviews | `reviews.py` | `resolveReviewChoice`; kinds incl. `tvdb_link` |
| External writes | `external_writes.py` | 9.0: `capture` (default) / `send`; `lcars captured <db> list` / `send --limit N` |
| Lists | `list_sync.py`, `watch_reconcile.py`, `mal_reconcile.py` | progress taken **only on a list change** (09-28, approved) |
| Rebuild | `rebuild.py` (`lcars rebuild`) | stages, checkpoints, ledger — §3 |
| Rule check | `rulecheck.py` (`lcars rulecheck <db>`) | 17 checks |

Config: `external_writes` (default `capture`), `list_adds_enabled` (default False),
`LCARS_AUTOMATION_FROZEN` (freeze, **on** by default in code).

## 3. The rebuild — state of 9.1

```
cd ~/starfleet-rebuild && LCARS_SONARR_URL=http://192.168.1.77:8989 LCARS_RADARR_URL=http://192.168.1.77:7878 \
  ~/repos/starfleet/.venv/bin/lcars rebuild run1 --snapshot snapshot-20260906-0853Z.db \
  --live live-20260928T1951Z.db --inputs ~/starfleet-review-2026-09-27 --from-stage N --until-stage M
```
`--from-stage N` restores checkpoint N−1 into `work.db` and drops ledger/pending lines of
stages ≥ N. The script forces `LCARS_EXTERNAL_WRITES=capture` and lifts the freeze **on
its copy only**. Stage times: 3 ≈ 5 min, 4 ≈ 2 min, 5 ≈ 10 s, 6 ≈ 5 min (network reads).

| # | Stage | Status | What it does |
|---|---|---|---|
| 1 | base | ✅ | `.backup` of the snapshot, `alembic upgrade head` |
| 2 | sources | ✅ | 8 source tables from live; 506 AniDB XML ingested |
| 3 | structure | ✅ | (a) 1,700 season statuses by 09-06 season id, marked yours; (b0) show-level TVDB ids **from live** (97 gained); (b) 101 TVDB decisions (series/movie/parent folds/own show/remove); (c) 19 films folded into their show as special levels **before** merges, film rows lose the series tvdb id; (d) merges (181 reviewed + 17 groups from confirmed links; unplaced seasons: no list id → own number, piece format → special, else inside the season airing on its start date, else next season — each `placement` in the ledger for review); (e) adds: 60 new shows via `add_checked` (vetting; the user's TVDB decision wins over Fribb), 22 phase-5 entries, Books Bought with Tax, MahoIku restart → skip list, 178 skipped; (f) TVmaze/AniDB/Syoboi show links from live (3,524) |
| 4 | sonarr | ✅ | `_fetch_sonarr` for every tracked show in the Sonarr library (705); TVDB season rows for episodes that had none (856; status from watch data); air dates from TVmaze by TVDB S/E (10,106) + AniDB |
| 5 | numbering | ✅ | Memory Alpha on 1,798 shows (tvdb 1,213 / anidb 152 / tvmaze 433); flags in ledger |
| 6 | statuses | ✅ | cour spans from Fribb offsets / AniList counts (9 done, 21 review); list-id levels linked to Memory Alpha's levels by start date (17 linked, 129 review); R2.7 marks on your completed seasons (74); 216 Trakt drops on the last aired season; snapshot follow-ups; Urusei Yatsura films → skipped levels (10 done, 7 review); `after_episodes_changed` on every show |
| 7 | cleanup | ✅ | 09-29 cleanup review (PLAN-DATA decision 9): **structure** goes in stage 3's tail (`rebuild_cleanup.structure_actions`: 82 folds by TVDB/AniList id, the skip list, a film's season, Kaiju Girl's history mapped by episode, wrong Sonarr links); **removals** in stage 7 (`stage_cleanup`: your statuses through the engine, the 2 duplicate shows, 1,379 untracked stubs, 1,245 show-level list ids, 40 orphaned watches). Every removed row exported per table in `<run>/removed/`, `redirects.json` maps each removed/folded show to its survivor (the replay must use it). Inputs: `rebuild-inputs/cleanup.json` (built by `tools/build_cleanup_inputs.py`) |
| 8 | replay | ✅ | `rebuild_replay.py`: 128 valid live watches (ids mapped via `redirects.json` and TVDB/AniList ids), 30 status finals, 5 manual watches, the score change, Kaiju minis on exact title+air date. Kaiju S3 conflict: see PLAN-DATA 12 |
| 9 | checks | ⏳ **next** | rulecheck + show pages load + reconciliation list (PLAN-DATA §2.0) + 08-26 cross-check |
| 10 | writes | ⏳ | see §4 D |

**Stages were renumbered 09-29** (cleanup inserted as 7). Run: `--from-stage 3 --until-stage 7` re-runs everything the cleanup touches (~25 min).

**Rulecheck after stage 7 (09-29):** R1.23, R3.2, R3.5 now 0 (were 1,329 / 59 / 1,335); open: R1.8 28 (26 = Star Wars show, wrong TVDB link; 2 side pieces), R1.11 166, R1.12 15, R1.22 25 (10 are real mapping errors), R2.7 5, R2.15 2.

**Rulecheck after stage 6 (older)** (`lcars rulecheck run1/06-statuses.db`): R1.0, R1.10*, R1.14,
R2.2, §2.2, R2.13, R2.14, R2.16, R2.10 clean. Open: **R1.11 1,133** and R1.8 90 (missing
episodes — §4 step A), R1.23 1,329 (list ids still on shows), R3.5 1,335 (untracked stubs),
R3.2 59, R1.22 25, R1.12 9, R2.15 2, R2.7 1, R1.10 1 (a cour span to check).

## 4. What to do next (in order)

**A. Missing episodes — DECIDED by the user (PLAN-DATA 9.1 decision 5, RULEBOOK R1.2e):
read episode lists straight from TVDB** for every tracked episodic show that has none or
lacks specials (835 shows have none; many lack season 0). Sonarr is only a TVDB proxy.
- Code: `tvdb_client.TvdbClient` has `_get`, `series_episode_synopses` (lists episodes) —
  add a plain `series_episodes(tvdb_id)` (season, number, aired, name, absoluteNumber) and
  insert missing episode rows (never overwrite Sonarr's) in **stage 4**, before numbering;
  attach to TVDB seasons (`metadata._ensure_seasons`), status from watch data as now.
  Reads only; TVDB key is in the config. This is new code → it was approved in principle
  ("option A"); still show the user the concrete change before writing it.
- Consequence the user accepted: completed seasons get their episodes marked watched (R2.7).
- Then re-run stages 4–6 and check: R1.11/R1.8 should collapse; the 129 level links and 21
  cour splits should mostly resolve (they failed for lack of episodes).

**B. Stage 7 — replay the gap** (09-06 08:53Z → cutover), through engine functions
(`status_rules`, watch-event helpers), `changed_by='rebuild'`, original timestamps, yours:
- `decisions.json` → `gap_watch` (valid shows: replay their watch events from **live**
  `watch_event` after 09-06; Kaiju minis `realign` by absolute number; bursts: only Ludwig
  09-11 valid), `gap_status` (final rules: completed / complete_aired / watching / dropped /
  replay / discard), the 1 score change (live `score_change` by holodeck), the PLAN-DATA
  watch table (5 manual watches), `snapshot` actions deferred to "rules + replay"
  (Slime, Kaiju).
- At the cutover run only: live watches after 09-27 and the user's own AniList activity.

**C. Stage 8 — checks + cleanup** (cleanup needs the user's yes, it deletes rows):
R1.23 move/remove list ids at show level (seasons hold them), R3.5 remove untracked stubs
except the skip list, R1.22 duplicates, R1.12 overlaps; rulecheck must pass; load show pages
(Frieren, Mushoku, Slime, a TV show) via the safe UI server on a copy of the result.

**D. Stage 9 — the write list**: empty `captured_write`, then from the **end state vs the
real lists** (read AniList/MAL lists, keyed by AniList/MAL/TVDB ids so run 1 vs run 2 can
be compared): one push per level whose status/progress differs (downward progress too),
deletes **only** from explicit decisions (`pending.jsonl` list_delete: 204363, 213658,
160803 MahoIku, + the TVDB "remove" shows' list ids), entries on the lists LCARS doesn't
track → listed only; Sonarr monitoring per season (`sonarr_sync.apply` over all seasons,
captured). Then record `list_baseline` from the current list values and mark both services
seeded. Export both lists' full state to JSON before any send (rollback of lists).

**E. 9.2 review page** (OK by default): the write list; every ledger `review` line
(placements, level links, cour splits, UY films, trakt reviews); the reconciliation list;
the 2 note differences. Then deploy with the user, following §5.

## 5. Cutover checklist (from the user's decisions)

- Labelled snapshots **before and after** (prod DB on tiny, the rebuilt DB, both list
  exports) so rollback is always possible.
- **Freeze lifted** at cutover (`LCARS_AUTOMATION_FROZEN=0`) — user, 09-28.
- Prod `lcars.ini`: `external_writes = send` only once the write list is approved; send in
  capped batches (`lcars captured <db> send --limit N`), verifying each batch.
- The list-progress guard is in (progress only on a list change), so the list polls may run
  after the approved sends; still: send the corrected progress first.
- **`list_intake_enabled = true`** in prod `lcars.ini` (7.5, default **off**) only once the
  approved write list has been sent and verified and `list_baseline` is seeded from the
  lists: until then nothing an outside list holds is taken into LCARS.
- Two runs: run 1 (now, `run1/`) for review; run 2 at cutover with a fresh live `.backup`.

## 6. Traps (each one bit or nearly bit this project)

- Title-search TVDB matches linked wrong shows (Maria Mercedes…): never accept a TVDB id from
  a title alone; the rebuild goes through `tvdb_vetting`.
- The 216 "show-only drops" are the **08-18 Trakt import**, not gap changes.
- Only ~700 shows are in Sonarr; the rest need TVDB directly (step A).
- `merge_shows` leaves conflicting episodes behind — the merge losers had none (checked).
- Film shows keep a series tvdb id → Sonarr refuses to file the franchise's episodes (R1.14
  check counts untracked rows too) — the fold removes it.
- The status engine's `_set` clears "yours" on derived changes (fine for automatic ones).
- Individual seasons have no show — label/log code must not assume `season.show_id`.
- `SeasonSource` must include `AUTO`; load real show pages after any rebuild.
- `lcars-dev.db` is old (09-22) — never a source of truth.

## 7. Open items for the user

- Data: show the single Sonarr match and ask y/n instead of auto-picking? (unanswered)
- 7.5 (ordered AniList → MAL loop) not built — needed before cutover?
- After cutover: AniDB drip ≤200/day, 8.8.3 evidence page, 8.8.5 link provenance, Data rework.

## 8. Safe UI checks

```
~/starfleet-review-2026-09-27/tools/ui-server.sh      # port 8890, ui-dev.db, no list/Sonarr creds
.venv/bin/python ~/starfleet-review-2026-09-27/tools/shot.py "show.html?id=<id>" out.png "<js expr>" 8
```
After a schema change: `LCARS_DATABASE_URL=sqlite:///…/ui-dev.db .venv/bin/alembic upgrade
head`, restart the server. For the user: http://127.0.0.1:8890/ui/ (dev account).
