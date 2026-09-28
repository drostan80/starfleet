# Handoff — 2026-09-28 — Phase 9 (dry run, rebuild, cutover) — READ FIRST

Previous: `HANDOFF-2026-09-27.md` (phases 2–8). The rebuild's code is done (phases 2–8.8).
This file is everything needed to run **phase 9** safely. Read it in full, then
`RULEBOOK.md` in full, then `PLAN-DATA.md` §1–§3 and "Rebuild order constraints".

---

## 0. How to work with this user (non-negotiable)

- **`RULEBOOK.md` is the authority**, names included. Never derive a rule. Unsure →
  add a question to its §9 and ask; answers go into the rules and the §10 changelog.
- **Every code change is validated first**: explain what changes and its consequences
  (behaviour, data touched, what reaches AniList/MAL/Sonarr, what breaks), then wait for
  a yes. Anything added after an approval is flagged as such.
- **Answer questions before acting.** When the user asks something, answer it; check or
  fix only when asked.
- **Verify before claiming.** "Fixed" means the symptom was seen gone (browser/prod);
  otherwise say "unverified".
- **Read-only by default.** Prod *reads* are allowed. **No write to AniList, MAL, Sonarr,
  Radarr or the prod DB until the user approves the exact write list** (phase 9's point).
- Keep `.venv/bin/pytest -q` (≈8 min, 1,539 tests) and `.venv/bin/ruff check .` green on
  every commit. Commit trailer lines: see the session's attribution instructions.
- **Long lists go on a review page** (artifact), never the terminal. URLs in a plain code
  block. On review pages **OK is the default**; the user only clicks "not OK" (+ note);
  reading back, no click = OK.
- **Log every watch the user mentions** in `PLAN-DATA.md` → "Watches during the rebuild"
  (table near the end) and commit it. They must survive the cutover.
- **SSH is always `ssh tiny@192.168.1.77`** (ISP changed 09-28; never `drostan@`, never the
  bare `tiny` hostname, never the old 192.168.0.152).
- **AniDB fetch is paused by the user.** Remind them on **2026-09-29** to resume at **max
  200 requests/day from one IP** (`ANIDB_DAILY_CAP=200` in `anidb.py`). Never rotate IPs or
  VPN exits to dodge AniDB bans (flood-protection circumvention; risks the `memalpha`
  client). Bans came at ~250 requests per IP regardless of pacing.
- **Test Shuttle (`./dev.sh`) reads the prod `lcars.ini`.** Since 9.0 it captures (the
  default) unless that file says `external_writes = send` — which it will after cutover.
  Prefer the safe UI server (§6) anyway.

## 1. Where things are

- Repo `~/repos/starfleet`, branch **`rulebook-rebuild`** — 68 commits ahead of `main`,
  **not pushed, not deployed**. Migration head **`f0a1b2c3d4e5`**.
- Data TUI `~/repos/data`, branch **`rulebook-rebuild`** (0258d2c review labels, 683e328
  default host 192.168.1.77). Not pushed. The user plans a Data **rework after cutover**
  (it has drifted); don't polish Data now.
- Documents (repo root): `RULEBOOK.md` (rules, §9 questions — all answered as of 09-28),
  `RULES-VS-CODE.md`, `PLAN-CODE.md` (phases + done-notes), `PLAN-DATA.md` (starting point,
  every review decision, TVDB decisions, rulecheck baseline, watch log).
- Review data + scripts (outside the public repo): `~/starfleet-review-2026-09-27/`
  - `sources-20260928.db` — live source tables (AniDB / anime-lists / TVmaze / Syoboi …)
  - `anidb_xml/` — 506 raw AniDB answers (all watching + planned shows); 1,081 remain
  - `activity_classified.json`, `anilist_*.json`, `arr_catalog.json`, `tvdb_*.json` — inputs
    to the review decisions
  - `tools/` — `ui-server.sh` (safe UI server), `shot.py` (headless Firefox check),
    `ui-dev.db` (lcars-dev.db migrated + numbered, for UI checks only)
- Review pages with the user's decisions (read with `ArtifactData list`, collection
  `decisions`): phase 5 https://claude.ai/artifact/3gXa5fHHeScXr66SvCLKJF ; data review
  https://claude.ai/artifact/XMJyKsTFySwzgqq3MthvSa . Decisions are also transcribed in
  PLAN-DATA.md.
- Prod snapshots on tiny: `/opt/appdata/lcars/db/` (the **09-06** file is
  `lcars.db.bak-20260906-141520`; it was a `cp` of a WAL DB, so it is the **09-06 08:53Z**
  state — see PLAN-DATA §1). Live DB copy: on tiny `sqlite3 lcars.db ".backup /tmp/x.db"`,
  `scp` it, **delete the /tmp copy on tiny**.
- Prod: v0.2.70 running (container `lcars`, web `starfleet_web`); ops container stopped;
  v0.2.71 (freeze) tagged, never deployed. v0.2.70 **still pushes to AniList/MAL** when the
  user watches — the user knows; the watch log covers the gap.
- Deploy process (memory `starfleet-deploy-process`): tag push → GHCR image → SSH to tiny →
  bump the pinned tag in `starfleet.yml`. Merging `rulebook-rebuild` / pushing / tagging /
  deploying each need the user's yes.

## 2. What the new code is (module map)

| Area | Module | Notes |
|---|---|---|
| Absolute numbering, levels, spans | `numbering.py` (`lcars numbering <db> [--apply]`) | TVDB order + air date, reconciled with AniDB/TVmaze (R1.2d); decimals; 5000.x placeholders (R1.0a); every episode in a level (R1.13b) |
| Status engine | `status_rules.py` | R2.7, R2.13–R2.19, R2.13b (Q-X confirmed); `NeedsConfirmation` → GraphQL "confirmed: true" |
| Add check | `add_check.py` | `classify` / `apply_decision`, individual seasons (R3.6) |
| TVDB vetting | `tvdb_vetting.py` | 8.8: guesses confirmed or reviewed; hard stops; `join` of an individual season |
| Same-TVDB consolidation | `consolidation.py` (`lcars consolidation`) | merge plan + `apply_group` |
| Reviews with choices | `reviews.py` | `resolveReviewChoice`; kinds: add_check:needs_user, same_tvdb_show, remote_completed, later_planned, tvdb_link |
| Sonarr per season | `sonarr_sync.py` | R5.5–R5.10 |
| Lists per level | `list_sync.py` | pushes, deletes of auto-added-then-skipped (R2.10) |
| List reconcile | `watch_reconcile.py`, `mal_reconcile.py` | `list_baseline` hub; later change wins; R4.8a (Q-Y: no review for aired-unwatched) |
| Rule check | `rulecheck.py` (`lcars rulecheck <db> [--json]`) | 17 checks, exit 1 on violation |

Config: `list_adds_enabled` (default **False**) gates list→LCARS adds (R4.7).

## 3. Phase 9 — what it is (PLAN-CODE "Phase 9", PLAN-DATA §3)

Rebuild the database from the 09-06 snapshot with the new engines, replay the user's own
changes since, **capture** every external write instead of sending it, let the user review
the exact write list, then deploy once and send the writes in capped batches.

Two pieces do not exist yet and come first. **Both are code → explain + get a yes.**

### 9.0 Capture switch — DONE 2026-09-28 (see PLAN-CODE "9.0 done")

Default is **capture**: nothing reaches AniList/MAL/Sonarr/Radarr unless `send` is set
(`lcars.ini` `external_writes = send`, or `LCARS_EXTERNAL_WRITES=send`). Queue:
`lcars captured <db> list` / `send --limit N`. Prod's `lcars.ini` gets `send` only when the
user approves the write list (9.2). Test Shuttle is safe again on this branch (it captures).
The original design notes follow for reference.


Today **nothing stops external writes**: `list_sync.push`, `delete_if_auto_skipped`, the
reconcile's "missing on the list → add", `sonarr_sync.apply`, and the add paths write
whenever a token/URL is configured. The dry run must run with real credentials for reads
but **record** writes.

Proposed shape (for the user to approve): a config/env `external_writes = send | capture`
(default `send` in prod, `capture` for the dry run) checked at the **choke points**, so no
caller can bypass it:
- AniList: `anilist_client._graphql_request` when the query is a `mutation` (covers
  `save_media_list_entry`, `delete_media_list_entry`, any score/other mutation);
- MAL: `mal_client` PATCH/PUT/DELETE (`update_my_list_status`, `delete_my_list_status`);
- Sonarr / Radarr: `_post`, `_put`, `_delete` in `sonarr_client.py` / `radarr_client.py`
  (series add/update/delete, episode monitor, commands);
- In capture mode: append `{service, method, path/mutation, payload, entity, at}` to a
  table (e.g. `captured_write`) and return a plausible success shape so the flow continues;
  `list_baseline` must **not** record a captured write as agreed.
- Tests: each choke point captures and sends nothing (mock transport asserts zero calls).
Grep for any other outbound write before trusting it (`httpx.post`, `.patch(`, `.put(`,
`.delete(` across `src/lcars`).

### 9.1 progress (2026-09-28, in progress)

- User answers: PLAN-DATA "Phase 9.1 decisions" (216 Trakt drops → last *aired* season;
  "mine" = everything reviewed since step 0; two runs; labelled snapshots; no watching
  until live).
- Inputs frozen in `~/starfleet-rebuild/`: `snapshot-20260906-0853Z.db`,
  `live-20260928T1951Z.db` (both read-only). Review decisions exported from both pages to
  `~/starfleet-review-2026-09-27/decisions-final/`; `normalize_decisions.py` turns them into
  `rebuild-inputs/decisions.json` (explicit actions; PLAN-DATA transcription wins over
  ambiguous notes — 2 differences flagged).
- `src/lcars/rebuild.py` (`lcars rebuild`): stages base + sources done and run
  (`~/starfleet-rebuild/run1/01-base.db`, `02-sources.db`). Next: stage 3 structure.
- Findings to report: local lcars.ini has tiny's old IP (use env overrides
  `LCARS_SONARR_URL=http://192.168.1.77:8989`, `LCARS_RADARR_URL=http://192.168.1.77:7878`);
  freeze ON by default (rebuild lifts it on its copy; prod at cutover = user's call);
  consolidation link-status bug fixed; reconcile progress backfill ignores the baseline
  (fix needs a yes before cutover; until then list polls stay off after cutover).

### 9.1 Rebuild script (the judgment-heavy part)

Write it as a repeatable script (e.g. `lcars rebuild <snapshot> <out> [--sources …]`), run
only on copies. Order (PLAN-DATA "Rebuild order constraints" — they exist for reasons):
1. `sqlite3 .backup` copy of the 09-06 snapshot; `alembic upgrade head` on the copy.
2. **Source tables from live** (`anidb_*`, `anime_list_*`, `tvmaze_*`, `syoboi_*` from
   `sources-20260928.db` + `anidb_xml/`), recompute what derives from them
   (`episode_anidb_mapping` …). Also a TVmaze-with-specials fetch (reads).
3. **Apply the user's review decisions** (PLAN-DATA §1–§2 and the phase 5 table: 181 merges
   OK, 19 films fold in, the 22 adds with per-item notes — e.g. 204363 skip + remove from
   AniList, 213658 remove from AniList + TVDB 273005 on the skip list, 208766 = TVDB 475021,
   185657/217434 → add to the proposed show).
4. **Same-TVDB consolidation before any Sonarr read or numbering** (126 shared TVDB ids in
   09-06; merged episodes move onto TVDB S/E — 923 episodes, 280 with files, lack Sonarr
   coordinates otherwise).
5. **Sonarr read** (TVDB S/E + `tvdb_absolute`), **then** `numbering` (levels, spans).
6. **`season.status_set_manually = 1` for every status that is the user's** (accepted
   season-status review, their AniList activity, their LCARS changes in the gap). All rows
   are 0 after the migration; without this, R2.16 warnings never fire and R2.10 would
   **delete the user's own planned seasons from AniList/MAL** as "auto-added".
7. **Replay the gap (09-06 08:53Z → cutover) through the normal code paths**, never raw
   inserts: user status changes, watch events, shows the user created, score changes. Only
   user-originated changes; automated adds R3.5 rejects are not replayed. Rule of thumb
   (user): since 09-06 they only watched airing shows; changes to *older* shows since 09-06
   are discarded (PLAN-DATA §2.0).
   - **216 shows are `dropped` with no season carrying it** — *corrected:* all are TV
     shows from the **08-18 Trakt import** (in the 09-06 base, not the gap; not in the
     season review). Handling is a 9.1 question to the user; deriving without it would
     silently turn them `planned`.
   - Include every watch in PLAN-DATA's "Watches during the rebuild" table (Mushoku S3
     finale + AniList 217434, Overgeared ep 1, Last Week Tonight S13E24, Animal Control
     S05E01 …) and anything the user adds before cutover.
8. `lcars rulecheck` must pass (baseline in PLAN-DATA).
9. **Diff** against the live AniList/MAL lists and Sonarr monitoring with capture on →
   the captured write list.

### 9.2 Review → deploy → send

- Put the write list on a review page (OK by default; group by service and kind; deletes
  and status downgrades called out). Nothing is sent before the user's yes on that list.
- Deploy: merge/push/tag/deploy each with the user's yes; swap the DB on tiny (keep the
  old one); `external_writes=capture` for the first start, check, then send the approved
  writes in **capped batches**, verifying each batch on AniList/MAL/Sonarr.
- After cutover: AniDB drip at ≤200/day (R1.2d order: watching → planned → rest →
  re-check), 8.8.3 evidence page, 8.8.5 link provenance re-checks, Data rework.

## 4. Traps (each one bit, or nearly bit, this project)

- Title-search TVDB matches linked the wrong shows (Maria Mercedes, My Sky, Villainess):
  never accept a TVDB id from a title alone (R3.7; 8.8 enforces it on the add paths — the
  rebuild must not bypass `tvdb_vetting`).
- `SeasonSource` once lacked `AUTO` → every show with an engine-made level failed to load.
  After the rebuild, load a few real show pages (Frieren, Mushoku Tensei) before cutover.
- Individual seasons have no show: anything that assumes `season.show_id` (labels, logs)
  breaks — two such bugs were found in 8.8.
- Junk purge order is Sonarr → LCARS → lists, or the untracked sweep recreates the show
  and re-pushes (memory `junk-purge-order-sonarr-first`).
- `list_baseline`: with no baseline yet, the list's value counts as the edit; the first
  run for a list writes nothing (seeding). Don't let a captured write be recorded as agreed.
- The 09-06 snapshot is a WAL `cp`: its real time is 08:53Z, not 14:15.
- `lcars-dev.db` is old (09-22) — never a source of truth.

## 5. Open items for the user (none block 9.0)

- Should Data show the single Sonarr match and ask y/n instead of auto-picking it? (asked,
  unanswered — new item if yes; Data rework is planned anyway).
- 7.5 (one ordered AniList → MAL propagation loop) is not built; the two polls run as
  separate triggers. Ask whether it's needed before cutover.
- Show header "N total" episodes uses AniList's S1 count (pre-existing, cosmetic).

## 6. Safe UI checks

```
~/starfleet-review-2026-09-27/tools/ui-server.sh      # port 8890, ui-dev.db, no list/Sonarr creds
.venv/bin/python ~/starfleet-review-2026-09-27/tools/shot.py "show.html?id=<id>" out.png "<js expr>" 8
```
`shot.py` drives headless Firefox over WebDriver BiDi (sets the scratch token in
localStorage, prints console errors and the expression, saves a viewport screenshot).
After a schema change: `LCARS_DATABASE_URL=sqlite:///…/ui-dev.db .venv/bin/alembic upgrade
head`, then restart the server. For the user: http://127.0.0.1:8890/ui/ (log in with the
dev account).
