# Handoff — 2026-09-30 — Cutover and go-live — READ FIRST

Previous: `HANDOFF-2026-09-28-PHASE9.md` (§0 working rules still apply in full: read §0 there).
Run 1 is finished and its write list reviewed and approved by the user (29 Sep evening). Today:
**run 2 on a fresh live copy → compare with run 1 → deploy → send → intake on.** The user planned
it in two sittings, about 3–5 h in all (below). Every step that touches prod or a list needs the
user's yes at that moment; approval of run 1's list is not approval to send run 2's without a look.

## 0. Working rules (short — full list in the 09-28 handoff §0)

- RULEBOOK.md is the authority; never derive a rule; ask. Answers → rulebook + §10 changelog.
- Every code change: explain consequences, wait for yes. Answer questions before acting.
- Verify before claiming. Read-only by default; prod reads allowed.
- SSH **`ssh tiny@192.168.1.77`**. Secrets (TVDB key, MAL token) are read from tiny's
  `/opt/appdata/lcars/config/lcars.ini` into an env var, never printed or stored.
- Review pages: OK by default, user clicks Not OK + note. **Say how to read a row** ("your list
  today" vs "after the write") and **never ask again what the user already confirmed** (mark it).
- Keep `.venv/bin/ruff check .` (whole repo, incl. `scripts/`) and `.venv/bin/pytest -q`
  (≈10 min, 1,660+ tests) green before any tag. Commit trailers per the session reminder.
- Show the todo list (✅/🔄/⏳/🔔) after each work round.
- **Monitor trap**: `pgrep -f "lcars rebuild"` inside a Monitor matches the Monitor's own shell
  — wait on the ledger line `"kind": "stage", "key": "writes"` (or the checkpoint file) instead.
- The user doesn't mind watch dates not matching exactly (their words, 09-29). Don't spend a
  re-run on dates.

## 1. State at stop (29 Sep, ~21:30 local)

- Repo `~/repos/starfleet`, branch **`rulebook-rebuild`**, 129 commits ahead of `main`, **not
  pushed**, last commit `a8b409d`. Full suite last green at 1,653 (before the last few small
  commits: targeted tests only since — **run the full suite first today**).
- Prod: **v0.2.70** (`lcars`, `starfleet_web`), ops stopped. Prod DB
  `/opt/appdata/lcars/db/lcars.db` (container path `/db/lcars.db`). Last tag `v0.2.71` (never
  deployed) → today's release is **`v0.2.72`** (never move a pushed tag).
- New code: `freeze.py` defaults to **frozen** unless `LCARS_AUTOMATION_FROZEN=0`;
  `external_writes` defaults to **capture** unless `lcars.ini [lcars] external_writes = send`
  (or env `LCARS_EXTERNAL_WRITES`); `list_intake_enabled` defaults **off**.
- Rebuild workspace `~/starfleet-rebuild/`: `snapshot-20260906-0853Z.db` (the 09-06 base),
  `live-20260928T1951Z.db` (live, 09-28), `old/pre-0826.db`, `run1/` (checkpoints `01-base.db`
  … `10-writes.db`, `ledger.jsonl`, `pending.jsonl`, `redirects.json`, `removed/`, `lists/`
  (AniList + MAL exports, 09-29), `writes/write_list.json`, `checks/`).
- Inputs `~/starfleet-review-2026-09-27/rebuild-inputs/`: `decisions.json`, `cleanup.json`
  (built by `tools/build_cleanup_inputs.py` — edit the script, not the JSON),
  `film_tvdb.json` (`tools/build_film_ids.py`), `list_decisions.json` (38 per-entry values),
  `anilist_history.json` (from the user's GDPR export via `tools/extract_anilist_gdpr.py`;
  the raw export contains email/IP — never copy it anywhere).
- Run 1 final write list (reviewed): AniList 70 changes + 11 adds, MAL 58 + 12, 5 deletes,
  Sonarr 67 shows, 290 captured rows. Review page https://claude.ai/artifact/JzeKCAyS3nEVtnnadx7Cyx
  (decisions in its `decisions` collection). Rulecheck open: R1.8 2 (Strawberry 100% S0E6,
  SAO S0E23 — decimal side pieces with no level), R1.11i 73 (information only).

## 2. What the rebuild is now (stages)

| # | stage | what |
|---|---|---|
| 1–2 | base, sources | 09-06 snapshot + source tables |
| 3 | structure | accepted statuses (+`season_status_overrides`), TVDB ids from live (**not for films**), TVDB decisions, folds, `shape_changes` (Memories), `fold_levels` (Chitose), film ids (`film_tvdb.json`, by show id or TMDB id) |
| 4 | sonarr | **re-anchor pre-pass** (`rebuild_reanchor`, R1.2f: rows to their TVDB episode by title + air date; Fribb places entries), Sonarr read, TVDB episodes (needs `LCARS_TVDB_API_KEY`), air dates |
| 5 | numbering | Memory Alpha; minis nested in their group (R1.13c) |
| 6 | statuses | episode totals, own entry → first part, repeated parts removed, cour spans in air order, R2.7 marks (never unaired), Trakt drops, engine |
| 7 | cleanup | statuses by id (+ `show_status skipped` = skip list: MahoIku 316815), duplicates, part merges, stubs, show-level ids |
| 8 | replay | realign, gap watches, **AniList history** (`rebuild_history`: activity < 08-15, JST→UTC), statuses, manual, scores |
| 9 | checks | rulecheck, reconciliation, 08-26 cross-check, every show page through the real server |
| 10 | writes | export both lists → per-entry decisions → list history intake (pre-mess entries never lowered) → diff (only changed fields; held back where the list would end elsewhere) → explicit deletes → Sonarr flips → baseline seeded |

Run command (from `~/starfleet-rebuild`; env per step 3 below):
```
LCARS_SONARR_URL=http://192.168.1.77:8989 LCARS_RADARR_URL=http://192.168.1.77:7878 \
  ~/repos/starfleet/.venv/bin/lcars rebuild run2 --snapshot snapshot-20260906-0853Z.db \
  --live live-<stamp>.db --inputs ~/starfleet-review-2026-09-27 --from-stage 1 --until-stage 10
```
This machine's `lcars.ini` has tiny's old IP: never edit it, pass the URLs as above.

## 3. Sitting 1 — build and check (≈1.5–2.5 h, mostly waiting)

1. **Full suite + lint** on `rulebook-rebuild`: `.venv/bin/ruff check .` and `.venv/bin/pytest -q`.
2. **Before run 2 — one build item (needs the user's yes):** the replay only takes live watches
   the 09-27 review covered (`gap_watch`). Live watches/status changes **after the 09-27 review**
   (e.g. You and I Are Polar Opposites S2E12, watched 09-27 14:20) are in no reviewed input. Build:
   read live `watch_event` / `status_change` after the review export (last `gap_watch` time),
   classify as before (the user's own via Data/Holodeck/Captain's Log/Android vs LCARS's own),
   and put them on a **short review page** (OK by default) — then replay the OK ones in stage 8.
   The user said on 09-28 they wouldn't watch until the new setup is live, so expect few.
3. **Fresh live copy** (read on tiny, copy here):
   ```
   STAMP=$(date -u +%Y%m%dT%H%MZ)
   ssh tiny@192.168.1.77 "sqlite3 /opt/appdata/lcars/db/lcars.db '.backup /tmp/live-$STAMP.db'"
   scp tiny@192.168.1.77:/tmp/live-$STAMP.db ~/starfleet-rebuild/
   ```
   Secrets into the shell (same command, never echo them):
   ```
   export LCARS_TVDB_API_KEY="$(ssh tiny@192.168.1.77 "grep -i -E '^ *tvdb_api_key' /opt/appdata/lcars/config/lcars.ini | head -1 | sed 's/^[^=]*= *//'")"
   export LCARS_MAL_ACCESS_TOKEN="$(ssh tiny@192.168.1.77 "grep -E '^ *mal_access_token' /opt/appdata/lcars/config/lcars.ini | head -1 | sed 's/^[^=]*= *//'")"
   ```
   (The local MAL token is expired — never refresh it locally: that rotates the refresh token
   prod uses. The AniList token in the local ini works.)
4. **Run 2**, stages 1–10 into `run2/` (≈45–60 min; nohup + wait on the ledger). A new run dir
   has no `lists/` or TVDB cache: both are read fresh (that's wanted).
5. **Check run 2** (≈30 min): rulecheck (expect R1.8 2 + R1.11i), `rebuild_checks` ledger lines,
   the eight-show truth (memory `season-walk-by-episode-identity`) — script
   `scratchpad/verify_try.py` pattern; `list_decision` all applied; the 38 decided entries end at
   their values (the check in the 09-29 transcript: final status/progress per decided id).
6. **Compare run 2's write list with run 1's** by `(service, id)`: same / new / gone / changed.
   Only the differences go on a page for the user. Expected differences: the post-09-27 watches,
   anything the user did on AniList since 09-29.

## 4. Sitting 2 — go live (≈1.5–2.5 h, user present, each step on their yes)

1. **Labelled snapshots**: prod DB
   `cp /opt/appdata/lcars/db/lcars.db /opt/appdata/lcars/db/lcars.db.bak-YYYYMMDD-pre-cutover-v0.2.72`
   (stop `lcars` first or use `sqlite3 .backup`), both list exports (`run2/lists/`), the rebuilt DB.
2. **Release**: merge or tag from `rulebook-rebuild` (ask the user: push the branch and tag
   `v0.2.72` from it). CI: require `test success` + `docker success`
   (`gh run view <id> --json jobs --jq '.jobs[]|"\(.name) \(.conclusion)"'`).
3. **Deploy** (memory `starfleet-deploy-process`): bump the three image lines on tiny with the
   `/-web/!` sed pair, grep all three lines. Add to the `lcars` service environment
   `LCARS_AUTOMATION_FROZEN=0` (user's decision 09-28; ops stays stopped until after the sends).
4. **Swap the DB**: stop `lcars`; put the rebuilt `run2/10-writes.db` at `/opt/appdata/lcars/db/lcars.db`
   (remove stale `-wal`/`-shm` of the old one after the backup); ownership as the old file;
   `docker compose -f ~/stacks/starfleet.yml --env-file ~/stacks/.env pull && ... up -d`.
   lcars runs `alembic upgrade head` on start (the rebuilt DB is already at head).
   Verify: show pages load (Frieren, Mushoku, Slime, Bookworm, a TV show), `captured_write`
   pending count = run 2's, `watch_event` count as run 2's.
5. **Send** — set `external_writes = send` in `lcars.ini [lcars]` only now, restart lcars, then
   in capped batches, verifying each against the lists:
   ```
   ssh tiny@192.168.1.77 'docker exec lcars lcars captured /db/lcars.db list | tail -3'
   ssh tiny@192.168.1.77 'docker exec lcars lcars captured /db/lcars.db send --limit 20'
   ```
   Order is capture order. After each batch: read the touched entries back (AniList/MAL) and
   check them against write_list.json; any error stays on its row — stop and look.
   Watch the AniList rate limit (≈90/min). MAL uses prod's token.
6. **Intake on**: once all sends are verified, `list_intake_enabled = true` in `lcars.ini`,
   restart; start `ops`; watch the first AniList/MAL polls: **nothing** should be seen as edited
   elsewhere (the baseline was seeded from the lists + the writes). Prove ops recovered by a
   processed line, not by quiet logs.
7. Labelled snapshot after. Update memory (handoff pointer) and PLAN-DATA.

## 5. Known open items (on the review page, not blockers)

- The Melancholy of Haruhi Suzumiya S1: 2006 (849) and 2009 (4382) cours both start at 0 —
  cour spans not placed.
- 5 Urusei Yatsura films/specials with no level; Battle Angel (1993 OVA) has no TVDB movie id.
- 51 AniList history entries that don't map; 78 levels not linked to a list entry; 67 held back
  (list counts a different number of episodes — status written, progress not).
- Mushoku 146065: AniList counts "Guardian Fitz" (TVDB S00E02) as episode 0 → held back.
- The 08-12 12:05:38Z burst (2,951 watch events, platform NULL) and the rebuild's own R2.7 events
  (dated the run) stay — the user doesn't mind dates.

## 6. After go-live (queued, not today)

TVDB-id guard (R1.14a), R3.7c/d/e, expanded rulecheck/enforcement audit, Data TUI rework,
rotate Sonarr/Radarr/TMDB keys, MAL client_id, AniList client_secret (memory: rotate at
project end). **AniDB: resume only after go-live** (user, 09-29): remind once when live, max 200
requests/day, restart on their yes.
