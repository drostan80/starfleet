# Decision pending: every list entry is placed by its episodes — one rule, one function (2026-10-10)

Status: **analysis and proposal; nothing built.** RULEBOOK §7: the change and its consequences are explained
here first; Claude waits for your yes. Evidence below was measured read-only (the production snapshot taken
before the With Vengeance fix, `lcars.db.bak-20261010-pre-vengeance-s2`, plus Fribb's real dataset).

## 1. What happened (With Vengeance, Sincerely, Your Broken Saintess, 10-05)

- 09-30: TVDB's season 2 (12 episodes, first aired 2026-10-01 15:00Z = 10-02 in Japan) is in LCARS with no list id.
- 10-05 10:30: AniList 212144 (the sequel, starts 2026-10-02, 12 episodes) arrives as a relation of 195209.
  The add check has no TVDB id for it (Fribb does not list it) and answers: *"TVDB doesn't list it yet —
  part of S1 after AniList 195209, or an individual season until TVDB has it."*
- You took "add to the proposed show": LCARS made an empty **part 2 of S1** holding 212144. TVDB's real S2
  stayed without an id. The wrong shape then flipped statuses for five days and, on 10-10, completing S1
  also wrote *completed, progress 0* to your S2 entries on AniList and MAL (fixed by hand that day).

The premise "TVDB doesn't list it yet" was false: LCARS already held TVDB's S2 episodes.

## 2. Root cause (verified)

R1.10a — *an entry belongs where its episodes are; its start date (a Japanese calendar day) must be the air
date of an episode of that season; otherwise placed by count* — is implemented **once**, in
`level_reconcile.plan_show`. That function only considers entries **Fribb lists** for the show's TVDB id
(`_show_entries`). Two doors let an entry into LCARS, and only one of them applies the rule:

| Door | Applies R1.10a? |
|---|---|
| Fribb lists the entry (the Memory Alpha reconciler) | yes |
| Anything else: an AniList relation, your list intake, a browse add — the add check, `add_check.classify` with **no TVDB id** | **no.** It finds the tracked show through the AniList prequel and stops at "part of S1 / individual season" without reading a single episode |

Reproduced on the pre-fix snapshot: Fribb lists only `195209` for TVDB 454916; `plan_show` returns *nothing to
do*, and `212144` is never even considered (`_show_entries` does not contain it). The stranded, episode-less part
therefore sat there until you noticed. Nothing reported it either: the planner only reports entries it looked at.

Today `lcars rulecheck` R1.10a is 0 on production, so no other stranded entry exists right now.

## 3. Proposed resolution (follows R1.10a, R1.17, R1.22, R3.5a, R3.7a)

**A. One placement function, used by both doors.** Move the placement core of `plan_show` (start date = an
episode's Japanese air date → that TVDB season; else the season whose free episodes number exactly the entry's;
else TVDB lacks the episodes → a planned season level numbered as TVDB's next season, never another number) into
one function, `place_by_episodes(conn, show, entry_facts)`, returning the TVDB season and *why*.
`plan_show` calls it; so does the add check.

**B. The add check stops proposing "part of S1" blindly.** In the no-TVDB-id branch, once the prequel gives the
show, it calls the same function with AniList's facts (start date, episode count — `facts_for`, already there):
- episodes found → `place_in_season(...)` (it exists already: *"TVDB season 2 has no AniList/MAL id yet"* → link it
  as the season's only entry, or a part if the season holds more) with the evidence in the text
  (*"its first episode aired 2026-10-02 = S02E01; 12 episodes = the season's 12"*);
- TVDB lacks the episodes → a planned season level, TVDB's next number — never "part of the last season";
- dates and counts disagree → asks, showing both.

**C. The reconciler also sees the entries LCARS already holds.** `_show_entries` takes Fribb's entries **plus**
every AniList/MAL id held by a level of that show (parts, specials, individual seasons) that is a leftover with no
episodes. Then a wrong placement, from any past or future route, is corrected by the next pass. For With
Vengeance this alone would have moved 212144 onto S2 within a day.

**D. Make "nothing placed" visible.** An entry the planner could not place is listed by `lcars rulecheck`
(R1.10a, as today) *and* by the planner's report, so a stranded entry cannot hide.

## 4. Tests (reproducing the real incident)

A fixture rebuilt from the snapshot: S1 (12 episodes), S2 (12 episodes starting 2026-10-01T15:00Z), AniList 212144
unknown to Fribb. Expected after the change: the add check proposes TVDB S2 with the evidence; the reconciler,
given a stranded part holding 212144, links it to S2; a case where TVDB lacks the season gives a planned next-season
level; disagreeing dates/counts ask. Plus the existing reconciler tests unchanged.

## 5. Consequences

- Behaviour: new AniList/MAL entries with no TVDB id are placed by their episodes instead of "part of the last
  season". Existing placed data is unchanged; no data fix is needed today.
- What is pushed: attaching an entry creates/updates its AniList/MAL list row exactly as any add does (status from
  the previous season, R2.16).
- What stops: the misleading "part of S1" proposal for a season TVDB already holds.
- Code touched: `add_check.py`, `level_reconcile.py` (extract, no behaviour change for Fribb entries), tests; no
  migration, no UI.

## 6. Decisions I need from you

1. **Auto or ask?** When two independent sources agree — the AniList prequel link **and** TVDB's own episodes
   (start date = an air date, and the count fits) — may the entry attach by itself, as R3.7c does when two sources
   agree on a TVDB id? Or always ask (with the right proposal and the evidence)? *My recommendation: ask for now;
   revisit after a few cases.*
2. **C (the reconciler also sees held entries):** yes? *Recommended — it is what makes the mapping systematic.*
3. Anything else you want the planner to report (a review, a notification), or is the rulecheck line enough?
