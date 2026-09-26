# Handoff — 2026-09-26 afternoon (v0.2.70) — READ FIRST

Previous: `HANDOFF-2026-09-26.md` (morning). This session ended with the user stopping
all data work. **The user will restate every rule in detail and re-plan the fix.
Do nothing to the data until that document exists.**

## 1. Decision for next session (user)

Start from the last stable snapshot **`/opt/appdata/lcars/db/lcars.db.bak-20260906-141520`**:
clean it, straighten it, mutate it where needed, then bring it forward to today's state —
all to the user's standards, **validated before anything is applied**. The code work
of the last 3 weeks stays; only the data is in question.

## 2. Production state

- **v0.2.70** on tiny (lcars/ops `starfleet:0.2.70`, web `0.2.70-web`), deployed 09-26.
  Content: transaction-leak fix + `db.undo_on_error`. Verified in prod: 0
  "left a transaction open" warnings over 3 pollMemoryAlpha cycles.
- Snapshot before deploy: `lcars.db.bak-20260926-pre-v0.2.70`.
- **No data was changed this session. Nothing was written to AniList/MAL/Sonarr.**
- Branch `feat/sequel-only-stubs` (adf4951): stubs only for SEQUEL/PREQUEL, never for
  an id already a season. **Based on my reading of the rules — not to be released
  until checked against the user's rewritten rules.**

## 3. Last-stable-snapshot analysis (read-only, 23 snapshots 08-27 → 09-26)

| Snapshot | Tracked | Seasons | Dup stubs | Side seasons | Next event |
|---|---|---|---|---|---|
| 08-27 | 1,980 | 1,747 | 27 | 2 | season.status column added 09-03 |
| 09-04 | 1,983 | 1,768 | 38 | 4 | quiet |
| **09-06 14:15** | **1,989** | **1,773** | **38** | **4** | last quiet point |
| 09-07 | 1,810 | 1,740 | 193 | 31 | 09-06 13:12Z: 178 tracked shows merged into parents as seasons (OVAs included); not logged in tracked_change |
| 09-15 | 1,812 | 2,604 | 146 | 4 | 09-12: 861 seasons auto-created (`unmatched`), planned 107→968 |
| 09-18 | 1,823 | 2,694 | 206 | 55 | +148 shows, +129 seasons |
| 09-19→25 | | | | | status ping-pong: ~5,700 automated show status changes; 47 shows ≥4 flips |
| 09-21 | 1,778 | 2,799 | 304 | 50 | +182 seasons (171 fribb), 63 untracked, dropped 280→367 |
| 09-26 | 1,785 | 2,803 | 323 | 50 | |

- "Dup stubs" = untracked show whose AniList id is a season; "side seasons" = S2+ whose
  crosswalk entry is non-TV with no TVDB id, or TVDB season 0.
- 09-06 is not perfect: 38 dup stubs, 4 side seasons (e.g. Frieren "S4" mini-anime,
  from the 08-11 backfill).
- **Snapshots are `cp` copies of a WAL-mode DB** → each reflects the last checkpoint.
  The 09-06 14:15 copy predates the 13:12 merge because that merge sat in the WAL.
  Future snapshots: use `sqlite3 lcars.db ".backup <file>"`.
- **Lost by rolling back to 09-06** (from today's DB, all timestamped, replayable):
  39 user status changes (holodeck/data/captains_log), 238 watch events, 77 tracked
  shows created, 1 user score change.
- Tooling (session scratchpad, not in repo): `snapstats.py` (per-snapshot metrics +
  diffs), `rulecheck.py` (read-only rule check), `retype.json` (AniList relation types
  for the 1,446 untyped relations, read-only re-read).

## 4. Findings from today (inputs only, not decisions)

- Relation types (AniList re-read): the 1,446 untyped relations were 282 SEQUEL,
  161 PREQUEL, 240 SIDE_STORY, 142 CHARACTER (the cross-franchise links, e.g.
  Toriko→ONE PIECE, TONIKAWA→Detective Conan), 213 no longer on AniList, rest other.
- Today's DB: 19 unaired seasons marked completed/watching (Frieren S3, DAN DA DAN S3,
  Sasaki and Peeps S2, NCIS S24…); 2 side pieces as seasons; 42 TVDB specials as S2+;
  375 stubs duplicating a season; 854 stubs not a direct sequel/prequel. None of these
  had a list_baseline entry (not verified against the live lists).
- The review page https://claude.ai/artifact/Vo9guuPKsXKDX3jnbWy3dF is **not usable**
  as a basis (it was built on the unvalidated DB). It holds ~12 user notes in its
  `decisions` collection that may still be worth reading.

## 5. What went wrong this session (my mistakes — don't repeat)

- **Built on an unvalidated database.** I made a status-review page from current data
  without first checking the data against the user's rules. The user had repeatedly
  said: *clean the database first, then review*. Result: two unusable review pages.
- **Grouped by transitive relations**, which chained unrelated franchises together
  through crossovers (TONIKAWA→Urusei Yatsura).
- **Derived rules instead of reading the original design.** Asked the user to confirm
  rules I had inferred, and asked questions about the results of my own assumptions.
  The user has explained the rules many times; the next session must read the user's
  rule document and the original design (`~/repos/starfleet-archive/SCOPE.md`,
  `claude-plans/`) before proposing anything.
- Proposed pushing corrected values through the hub. **Never touch AniList/MAL until
  it is known to be correct** (said four times today).
- The user is extremely frustrated: every data "fix" so far has added new bugs.
  Fix bugs; never add to them by assuming.

## 6. How to work next session

1. Wait for / read the user's detailed rule document. Do not start from memory notes.
2. Plan the rollback-and-replay from 09-06 with the user; validate on a copy; nothing
   applied without explicit approval.
3. Read-only by default. Answer questions before acting. One prod command at a time.

## 7. Evening update (freeze)

- User: **ops is cut** (container stopped) — this stops all scheduled automation
  (reconciles, AniList/MAL hub, anilist_activity, metadata refresh, Memory Alpha,
  merges, sweeps). Webhooks left as they are ("good enough for now").
  At 15:15Z, before the stop, anilist_reconcile changed Slime (watching→completed) and
  Torture Princess (→dropped), auto-created 14 Slime S0 watch events, and wrote ~30
  entries to AniList/MAL (15:15–15:19Z, see list_baseline.updated_at).
- **v0.2.71 tagged, NOT deployed** (user stopped for the day): `lcars/freeze.py`, on by
  default — no metadata fetch; a show-level status change writes only show.status +
  status_change (no season fanout, no list push, no auto-mark, no arr monitor change).
  Deploy only if the user asks; restart only `lcars web`, never a bare `up -d` (it would
  restart ops).
- Still automatic even so: watching an episode can move a season's status; the
  manual `addWatchEvent` resolver left a transaction open at 11:43Z (safety net committed).
- User's plan: roll the data back to good data from before the damage (lose only the
  last 2–4 weeks), after restating the basic rules in full.
