# Decision pending: the identity-mismatch check (2026-10-04)

Status: **nothing changed in the code for this.** The weekly half of the crash is fixed (commit `1f1f838`, local branch
`dev-airing-sources`, not deployed); the identity-check half is left as it was until you decide. Everything below was
measured **read-only on a copy of the production database** (taken 2026-10-04 from tiny) with Fribb's current dataset.

## 1. What the check is for

`identity_mismatch.check_anilist_id_mismatch` runs as the last step of the hourly sweep in ops. For every tracked season
that has its own AniList id, it asks Fribb (the anime id crosswalk) "which AniList id belongs at this place in this
show?" and opens a **review** when the answer differs from what LCARS stores. It exists because of a real incident
(Tantei wa mou, Shindeiru S2 had been linked to a different show's AniList entry, 2026-09-21).

## 2. What is going wrong

- **The crash.** The check looks up "the Nth season in Fribb's list" using the LCARS season number. Levels with **no season
  number** (specials and similar) make that comparison fail: `'<' not supported between 'NoneType' and 'int'`
  (`identity_mismatch.py`, `_resolve_by_position`). The whole check stops at the first such level.
- **Since when / how often.** The rebuilt database has many such levels, so the check has failed on every hourly sweep since
  the cutover (2026-09-30). The sweep logs one error per hour. Nothing after it in the sweep is lost (it is the last
  step), and nothing is flagged wrongly — it simply does not run.
- **The same levels also broke the weekly reconcile** (`reconcileSeasonMapping`, 382 failures in 48 h). That one is fixed.

## 3. Why simply guarding the crash is not safe

The obvious fix is one line: skip a level whose season number is empty. I tried it on the production copy. The check then
runs to the end and **flags 58 seasons**. Almost all look false:

- **27 are `part` levels** (a season split into parts, each with its own AniList id). The check compares each part
  against the same position in Fribb's list as its siblings, so at most one of them can ever agree.
- **31 are whole TVDB seasons.** The check assumes *LCARS season number = position in Fribb's list*. That held
  before the rebuild. The rebuilt data has a row for **every TVDB season**, including seasons that have no AniList entry,
  so the positions no longer line up. Example: Gintama has TVDB seasons 1–10 in LCARS but Fribb lists only 5 entries, so
  "season 5" in LCARS is "5th entry" in Fribb: a different season.

Consequence if the guard alone were deployed: 58 new reviews now, and the same class of false positive again whenever the
data changes. This is the same shape of problem as the 2026-09-21 flood, when the check was pulled the same night.

## 4. The alternative I measured (not built)

Compare by the **TVDB season number**, which both sides record, instead of by position:

> For a whole TVDB season (kind `tvdb_season`, with a number) that has an AniList id, take the Fribb entries for the show's
> TVDB id whose own TVDB season number equals this season's number (ignoring movies/specials/OVAs). If exactly one AniList
> id comes out and it differs from the stored one → open the review. If several come out (a split-cour season) or none,
> **no opinion**. Part levels and levels without a number are not checked.

Result on the production copy, 1,373 whole seasons with an AniList id:

| Outcome | Seasons |
|---|---|
| Fribb has exactly one entry for that TVDB season and it **agrees** with the stored id | 1,251 |
| Fribb has several entries for it (split cours) — no opinion; stored id is one of them | 6 |
| Fribb has **no entry for that TVDB season** — no opinion | 115 |
| **Disagrees** | **1** |

The one disagreement: **R.O.D -READ OR DIE- S1** — stored AniList **208**, Fribb says **209**. I have **not** checked
which is right.

### How the 58 flags from the guard-only version look under this method

Of the 31 whole-season flags, 19 actually **agree** under the TVDB-number method, 11 have **no
Fribb entry for that TVDB season** (no opinion), and 1 genuinely disagrees (R.O.D). Of the 27 part flags, 25 are
split-cour parts (no opinion) and 2 disagree (Chitose-kun wa Ramune Bin no Naka S1 part 2, Ooi! Tonbo S1 part 3), but
part levels would not be checked under this proposal.

#### Whole seasons (31)

| Show | LCARS level | Stored AniList | Position method says | By TVDB season number |
|---|---|---|---|---|
| BLEACH | S1 (completed) | 269 | 116674 | no Fribb entry for this TVDB season |
| Bakemonogatari | S5 (completed) | 21745 | 173533 | no Fribb entry for this TVDB season |
| Bungou Stray Dogs | S2 (planned) | 103223 | 21679 | agrees |
| Dungeon ni Deai wo Motomeru no wa Machigatteiru  | S5 (skipped) | 170732 | 155211 | agrees |
| FLCL | S2 (dropped) | 21746 | 146472 | no Fribb entry for this TVDB season |
| FLCL | S3 (skipped) | 21748 | 146473 | no Fribb entry for this TVDB season |
| Gintama | S1 (paused) | 918 | 9969 | no Fribb entry for this TVDB season |
| Gintama | S5 (skipped) | 9969 | 101925 | agrees |
| Gokushufudou | S2 (completed) | 154982 | 132193 | agrees |
| Hokuto no Ken | S1 (completed) | 967 | 1356 | no Fribb entry for this TVDB season |
| JoJo no Kimyou na Bouken (TV) | S3 (skipped) | 21450 | 20799 | agrees |
| JoJo no Kimyou na Bouken (TV) | S4 (skipped) | 102883 | 21450 | agrees |
| JoJo no Kimyou na Bouken (TV) | S6 (skipped) | 190327 | 131942 | agrees |
| Mushoku Tensei: Isekai Ittara Honki Dasu | S3 (completed) | 178789 | 146065 | agrees |
| Pocket Monsters | S1 (dropped) | 527 | 1564 | no Fribb entry for this TVDB season |
| Pocket Monsters | S6 (skipped) | 1564 | 17873 | agrees |
| Pocket Monsters | S10 (skipped) | 1565 | 112153 | agrees |
| R.O.D -READ OR DIE- | S1 (completed) | 208 | 209 | DISAGREES (209) |
| Re:Zero kara Hajimeru Isekai Seikatsu | S3 (completed) | 163134 | 119661 | agrees |
| Re:Zero kara Hajimeru Isekai Seikatsu | S4 (completed) | 189046 | 163134 | agrees |
| SPY×FAMILY | S2 (completed) | 158927 | 142838 | agrees |
| SPY×FAMILY | S3 (completed) | 177937 | 158927 | agrees |
| Saint Seiya | S4 (completed) | 1257 | 3515 | no Fribb entry for this TVDB season |
| Saint Seiya | S5 (completed) | 1253 | 6171 | no Fribb entry for this TVDB season |
| Saint Seiya | S6 (completed) | 3515 | 20928 | no Fribb entry for this TVDB season |
| Saint Seiya | S7 (completed) | 6171 | 97919 | no Fribb entry for this TVDB season |
| Shokugeki no Souma | S4 (completed) | 109963 | 100773 | agrees |
| Shokugeki no Souma | S5 (completed) | 114043 | 109963 | agrees |
| Tensei Shitara Slime Datta Ken | S3 (completed) | 156822 | 116742 | agrees |
| Tensei Shitara Slime Datta Ken | S4 (completed) | 182205 | 156822 | agrees |
| Yuuki Yuuna wa Yuusha de Aru | S3 (skipped) | 122292 | 97769 | agrees |

#### Part levels (27)

| Show | LCARS level | Stored AniList | Position method says | By TVDB season number |
|---|---|---|---|---|
| 86: Eighty Six | S1 part 2 (completed) | 131586 | 116589 | stored id is one of several (split cours) — no opinion (116589,131586) |
| BLACK LAGOON | S1 part 2 (completed) | 1519 | 889 | stored id is one of several (split cours) — no opinion (889,1519) |
| Bungou Stray Dogs | S1 part 2 (completed) | 21679 | 21311 | stored id is one of several (split cours) — no opinion (21311,21679) |
| Chitose-kun wa Ramune Bin no Naka | S1 part 2 (planned) | 198727 | 180082 | DISAGREES (180082) |
| Dead Mount Death Play | S1 part 2 (completed) | 162803 | 157198 | stored id is one of several (split cours) — no opinion (157198,162803) |
| Dr. STONE | S4 part 1 (completed) | 199221 | 162670 | stored id is one of several (split cours) — no opinion (172019,189117,199221) |
| Enen no Shouboutai | S3 part 2 (completed) | 179062 | 149118 | stored id is one of several (split cours) — no opinion (149118,179062) |
| Gokushufudou | S1 part 2 (completed) | 132193 | 125426 | stored id is one of several (split cours) — no opinion (125426,132193) |
| Hataraku Maou-sama! | S2 part 2 (completed) | 155168 | 130592 | stored id is one of several (split cours) — no opinion (130592,155168) |
| Honzuki no Gekokujou: Shisho ni Naru Tame ni wa  | S1 part 2 (completed) | 171110 | 108268 | stored id is one of several (split cours) — no opinion (108268,113693,121176,171110) |
| Honzuki no Gekokujou: Shisho ni Naru Tame ni wa  | S1 part 3 (completed) | 113693 | 108268 | stored id is one of several (split cours) — no opinion (108268,113693,121176,171110) |
| Honzuki no Gekokujou: Shisho ni Naru Tame ni wa  | S1 part 4 (completed) | 121176 | 108268 | stored id is one of several (split cours) — no opinion (108268,113693,121176,171110) |
| Jormungand | S1 part 2 (completed) | 13331 | 12413 | stored id is one of several (split cours) — no opinion (12413,13331) |
| Mahoutsukai no Yome | S2 part 2 (completed) | 166452 | 154364 | stored id is one of several (split cours) — no opinion (154364,166452) |
| Mushishi | S2 part 2 (completed) | 20751 | 20595 | stored id is one of several (split cours) — no opinion (20595,20751) |
| Mushoku Tensei: Isekai Ittara Honki Dasu | S1 part 2 (completed) | 127720 | 108465 | stored id is one of several (split cours) — no opinion (108465,127720) |
| Mushoku Tensei: Isekai Ittara Honki Dasu | S2 part 1 (completed) | 146065 | 127720 | stored id is one of several (split cours) — no opinion (146065,166873) |
| Mushoku Tensei: Isekai Ittara Honki Dasu | S2 part 2 (completed) | 166873 | 127720 | stored id is one of several (split cours) — no opinion (146065,166873) |
| Nageki no Bourei wa Intai Shitai | S1 part 2 (completed) | 185801 | 175019 | stored id is one of several (split cours) — no opinion (175019,185801) |
| Ooi! Tonbo | S1 part 3 (completed) | 178729 | 164440 | DISAGREES (164440) |
| Re:Zero kara Hajimeru Isekai Seikatsu | S2 part 2 (completed) | 119661 | 108632 | stored id is one of several (split cours) — no opinion (108632,119661) |
| SAKAMOTO DAYS Part 2 | S1 part 1 (dropped) | 184237 | 177709 | stored id is one of several (split cours) — no opinion (177709,184237) |
| SPY×FAMILY | S1 part 2 (completed) | 142838 | 140960 | stored id is one of several (split cours) — no opinion (140960,142838) |
| SYNDUALITY Noir | S1 part 2 (completed) | 169559 | 154643 | stored id is one of several (split cours) — no opinion (154643,169559) |
| Shokugeki no Souma | S3 part 2 (completed) | 100773 | 99255 | stored id is one of several (split cours) — no opinion (99255,100773) |
| Sugar Apple Fairy Tale | S1 part 2 (completed) | 163079 | 139821 | stored id is one of several (split cours) — no opinion (139821,163079) |
| Tensei Shitara Slime Datta Ken | S2 part 2 (completed) | 116742 | 108511 | stored id is one of several (split cours) — no opinion (108511,116742) |

## 5. Options

| | What changes | Consequence |
|---|---|---|
| **A. Leave it** | Nothing. | The check stays dead (one logged error an hour, nothing flagged, nothing lost). A wrong AniList id on a season is only caught by you noticing. The Tantei-type incident is not guarded. |
| **B. Guard only** | Skip levels with no number. | 58 mostly false reviews now; more later. Not recommended. |
| **C. Compare by TVDB season number** (section 4) | The check's comparison changes; only whole numbered seasons are checked. | On today's data: **1 review** (R.O.D). Protects against the Tantei-type mistake for seasons Fribb knows. Blind to seasons Fribb has no entry for (115 today) and to split cours. Needs tests and your sign-off on the new rule. |
| **D. Turn the check off** | Remove it from the sweep. | Same protection as A, no logged error. |

## 6. Things I have not verified

- Whether the R.O.D disagreement is a real mistake in LCARS or in Fribb.
- Whether Fribb's own TVDB season number is reliable for every show. The 1,251 clean agreements suggest it is, but they
  are agreements, not proof.
- What the check would have flagged *before* the rebuild on this data (the old data is gone from the live database).
- Whether ops should treat this one failing step differently (today a failure here is logged as a failed sweep every hour,
  which can hide a real failure in the same log).

## 7. Questions to think about

1. Do you want a check like this at all, now that the data comes from the rebuild? What would you want it to catch?
2. Is "no opinion" acceptable for the 115 seasons Fribb has no entry for, or should those be listed somewhere?
3. Should a flagged season be a review you answer (as before), or only a note?
4. For split cours (several Fribb entries on one TVDB season), is there a way you would want them checked?

## 8. Where things are

- Weekly half fix: commit `1f1f838` (`dueForSeasonReconciliation` skips levels with no season number), test in
  `tests/test_server.py`.
- Identity check code: `src/lcars/identity_mismatch.py` (unchanged).
- Short note: `NEXT_UP.md`, item "identity-mismatch check crashes".
- The prototype of section 4 was a throwaway script, not committed; I can rebuild it as a proper change with tests when you decide.
