# Starfleet / LCARS Rulebook

The user's rules for the LCARS data model, statuses, automation and mirroring.
**This document is the authority.** Code, data fixes and plans are checked against it.
Nothing here is derived by Claude: every rule is the user's wording (lightly
tidied). Clarifications are only added once the user has answered them, and are
marked `[clarified YYYY-MM-DD]`. Open points live in §9 until answered.

The rules are written for anime. Other sources of authority may apply to TV
series; notes for TV series sit alongside (marked **TV:**). If it is not clear how a
rule applies to TV series, ask — do not assume.

- Source: user, 2026-09-27 (restated in full after the 09-26 stop).
- **Today's rules are the baseline.** Where code or data differ — because it was
  done wrong or because the rules improved — the code and data change; old
  behaviour is never a reason to keep something. (user, 2026-09-27)
- **Every code change is validated by the user first.** Before changing code,
  Claude explains the change and its consequences (what behaviour changes, what
  data it touches or will touch, what is pushed to AniList/MAL/Sonarr, what
  breaks or stops) and waits for the user's approval. (user, 2026-09-27)
- Maintained by: Claude, on the user's instruction. Every change goes in §10.

---

## 1. Database divisions

### 1.1 Episode — the core unit

- **R1.1** The core unit is the episode.
- **R1.0 Every episode has an absolute number**, set per these rules — this
  was always the rule. `[clarified 2026-09-27]` No episode may be left without
  one; if some are, it was set up wrong before.
- **R1.0a No air date yet** `[clarified 2026-09-28]`: an episode without an air date
  still gets a number — a placeholder shown as **x** until its air date arrives; the
  number stored is **5000.1, 5000.2…** (higher than any real episode number, so it
  sorts as not aired yet; any number from 5000 up is a placeholder).
- **R1.2** Episodes are ordered by **absolute episode number**, derived from AniDB,
  Fribb and other sources. On disagreement, or where disambiguation is needed,
  order by **air date-time**.
  - **TV:** absolute-number source of truth is TVmaze (see R1.19).
- **R1.2a LCARS keeps its own absolute numbering** `[clarified 2026-09-27, Q-E]`.
  This is what Memory Alpha is for. A whole number inserted (e.g. a film between
  seasons) shifts every later episode; each source's own numbering (TVDB
  SxxEyy / TVDB absolute, AniDB epno, AniList/MAL per-entry episode numbers) is
  kept only as a **mapping** to the LCARS episode.
- **R1.2b Decimal scheme** `[clarified 2026-09-27, Q-F]`: items in one gap are
  numbered `.1`, `.2`, … after the preceding whole number (two specials between
  ep 5 and ep 6 → 5.1, 5.2). **One item alone in a gap → `.5`** (5.5) — it looks
  better. `[clarified 2026-09-28, Q-U]` An item before ep 1 is 0.x — Frieren's pre-air
  3-episode block shown as one film is S1E0.5 → abs **0.5**.
- **R1.3** Specials, OVAs, minis… take either a **whole** absolute number or a
  **decimal** one, depending on logic:
  - an OVA/special following a season may take a whole number;
  - minis attached to each episode of a season may take a decimal;
  - AniDB and other sources often make this distinction;
  - an OVA can also take a decimal if it slides into the story as an aside but was
    not aired the same way.
- **R1.4** Films/movies that are an integral part of the story take a **whole**
  absolute number.
- **R1.5** Special-version movies (e.g. Frieren eps 1–3 shown as one full-length
  movie in Japan with a different ED) take a **decimal** (in the example, 0.5).

### 1.2 Season

- **R1.6** Episodes are organised in seasons. A season is a group of episodes
  coherently organised storywise and aired in a consistent space of time. Some
  seasons may be divided into parts that other sources consider seasons — this is
  the first main reconciliation needed.
- **R1.7** Sonarr/TVDB season division is the first guide (anime and TV series).
- **R1.8 Season 0 must not exist in our system.** `[clarified 2026-09-27]` Season
  0 exists in TVDB (a source bucket, kept only as that source's numbering); every
  episode in it must get an absolute number and be mapped per R1.8a–c. TVDB/Sonarr put side mini-series,
  OVAs and specials into season 0; each of those episodes must follow the episode
  rules and be redistributed (this largely decides decimals vs whole numbers):
  - **R1.8a** if its air time falls **within** a previously set season, its
    absolute number may take a **decimal**;
  - **R1.8b** if it falls **between** established seasons' air timeframes, it may
    take a **whole** number;
  - **R1.8c** exceptions are dictated only by other sources' numbering (AniDB for
    anime), and the user may confirm them.
  - **R1.8d Minis between seasons take decimals** `[clarified 2026-09-28, Q-V]`:
    they are not full-blown episodes but are still tracked in place, within their
    decimal season (side piece, R1.9a), numbered as decimals after the last
    episode of the season before (Frieren: 28.1, 28.2 … — hundredths when ten or
    more, 28.01 … 28.10); the next season keeps its number (Frieren S2 starts at 29).
- **R1.9 Season naming** (apart from season 0) follows the TVDB main numbering
  `S00E00` (the numbering, not the id).
- **R1.9a Season numbers follow TVDB first** `[clarified 2026-09-27]`: TVDB's
  main seasons keep their numbers. A level that isn't a TVDB main season (side
  pieces, minis, OVA runs redistributed from season 0) never takes the next whole
  season number: placed by air date **between** TVDB seasons N and N+1 it gets a
  decimal season number (e.g. the Frieren mini-anime, between S2 and the unaired
  S3, is **S2.5**, not S4); inside a season's air window it is a sub-season of
  that season (R1.13). Memory Alpha must be reworked to follow this.
  Numbering between two TVDB seasons `[clarified 2026-09-27]`: one side piece →
  **N.5** (preferred look; N.1 acceptable if .5 is really troublesome); two or
  more → **N.1, N.2, …** in air-date order.
- **R1.10 Sub-seasons.** When a different source divides a season further (e.g.
  AniList gives cour 1 and cour 2 of a season separate ids), the season may be
  organised into sub-seasons on its page, each named with that source's naming.
- **R1.11 All seasons are defined, in the background, by their span of absolute
  episode numbers.**
- **R1.12** A season is a span **or a list of spans** of absolute episode numbers:
  abs x → abs y, or abs a → abs d **and** abs f → abs k, where whatever lies
  between d and f is whichever episodes are mapped there by air date and other
  numbering. `[clarified 2026-09-27, Q-P]` Example: e.g. a film with a whole
  absolute number airing mid-season → season = abs 13–16 **and** abs 18–24, the
  film being abs 17.
- **R1.13** Minis can be set as sub-seasons interlocking with the main season,
  which lets them have their own cover art.
- **R1.13b Every episode belongs to a level, so every cover can be set**:
  a series of minis inside a season's run is grouped as
  that season's mini sub-season (one cover for the group); a single special or
  full-length piece is a level of its own (its own cover); minis between seasons are
  their decimal season (R1.8d). Art can be set per level, individually or for the
  decimal group. `[clarified 2026-09-28]`
- **R1.13a Film/mini inside a season's air window** `[clarified 2026-09-28, Q-R]`
  (e.g. S2 = abs 13–16 and 18–24, film abs 17): its **own level**. Sub-season or
  separate level are both acceptable technically; what matters is that it **appears
  on the show page in its air-order place** and **its art can be changed
  individually**.

Worked example (user's):

```
show X
  season 1
    ep1  (abs 1)
    ep2  (abs 2)
    ep2.5 (abs 2.5)
    …
    ep12 (abs 12)
  film (abs 13)
  season 2                          ← defined as abs 14 … end of season 2
    season 2 part 1                 ← own AniList id; abs 14–26
      ep1  (abs 14)
      …
      ep13 (abs 26)
    season 2 part 2 electric boogaloo   ← own AniList id; starts abs 27
      ep1 (S02E14) (abs 27)
```

> The counts in the example are illustrative (the user may have miscounted); the
> principle is R1.11–R1.12. `[clarified 2026-09-27, Q-P]`

### 1.3 Show

- **R1.14** A show is an assembly of seasons, **aligned to a TVDB id**.
- **R1.15** A show contains numbered episodes and seasons that carry their own ids
  in other databases (AniList, MAL… give ids to individual seasons or parts — those
  are **season level**). The show level also has ids.
- **R1.16** A show can reference the list of seasons and episodes it is composed
  of, but since the episode is the core unit, the better view is: **each episode is
  mapped to a show id**. An episode can be mapped to a show, to a place in a season,
  to a place in a season part, and/or to a place in a differently-named season in
  another database.
- **R1.17 Reconciliation ALWAYS happens at episode level**, per the
  absolute-numbering source of truth, tie-broken and aligned by air date-time,
  primarily aligned to the TVDB main season spread (season 0 redistributed per
  R1.8), then further divided and mapped to align and reconcile the other sources.
- **R1.18** Absolute-numbering source of truth: **anime** — AniDB + Fribb…;
- **R1.19** **TV** — TVmaze.
- **R1.2d No authoritative data yet** `[clarified 2026-09-28, Q-T]`: TVDB order +
  air date may be used, carefully — the numbering still follows every rule above
  (season 0 episodes get their numbers too, R1.8). It is **reconciled once the
  authoritative source (AniDB / TVmaze) has the data**.
  **Build model** `[clarified 2026-09-28]`: absolute numbers are first derived from
  TVDB order + air date (best guess), **then reconciled with AniDB as its data comes
  in**. The rebuilt database may ship before AniDB data is complete — an exception
  to the clean-data start, accepted by the user because ~1,330 AniDB entries are still
  missing after a month of fetching; it also exercises the fallback and the
  reconciliation. AniDB fetching is paced safely, in this order: shows missing data
  that are watching → planned → the rest → a re-check of shows with existing data.
- **R1.2c Memory Alpha is the authority that sets every episode's absolute
  number** (season 0 included), from AniDB + Fribb (anime) and TVmaze with
  specials (TV), air date deciding conflicts. Sonarr/TVDB/AniDB/AniList numbers
  are mappings only. `[confirmed 2026-09-27]` (Not yet true in code: today the
  number is Sonarr's and Memory Alpha only compares — PLAN-CODE phase 3.)
- **Memory Alpha (existing design, from NEXT_UP.md — the user's standing rule since
  its inception):** Memory Alpha is the layer that keeps the database straight:
  AniDB is the ordering authority for anime; Anime-Lists/Fribb give the
  AniDB↔TVDB↔AniList↔MAL bridges; when episode ordering genuinely conflicts,
  the **real broadcast/release date wins** over a community mapping's
  offset/episode_map. Cross-id propagation must never overwrite a manual
  correction.

### 1.4 Franchise (not properly constructed yet)

- **R1.20** A franchise is a collection of movies, TV shows, anime… related in some
  way. Examples:
  - two separate shows that are spin-offs (S.W.A.T. and S.W.A.T. Exiles);
  - anime and their movies not tracked in the TVDB show id;
  - cross-media stories.
- **R1.21** A further meta category, **Universe**, may be created where different
  franchises cross paths (ONE PIECE crossover episodes / shared characters with
  Toriko; Dragon Ball Z × Toriko — Toriko a different show, Dragon Ball a different
  franchise).

### 1.5 Ids and levels

- **R1.22** The database may hold ids at any and all levels, as long as they follow
  the rules above. All external ids must be mapped according to the rules.
- **R1.23** Always be explicit about which level is meant.
  - **AniList is always season level** (even when it calls them shows); most anime
    DBs are.
  - **TVDB is always show level.**
  - A one-season show is often confusing, but the levels do not change.

---

## 2. Status

### 2.1 Episode level — three independent booleans

An episode can be unaired but watched (pre-air showing, leak…).

- **R2.1 aired / not aired** — automatic.
- **R2.2 watched / not watched** — manual.
- **R2.3 available / not available** — the episode exists as a file on the server.

### 2.2 Season and show level

| Status | Picker? | Set by |
|---|---|---|
| tracked | no (caveat: skipped) | automatic |
| airing | no | automatic |
| watching | yes | manual, + one automatic exception (R2.14) |
| completed | yes | automatic (all eps watched) or manual |
| planned | yes | manual, or automatic for unaired seasons |
| paused | yes | manual (following new seasons → skipped, R2.16) |
| dropped | yes | manual (following new seasons → skipped, R2.16) |
| skipped | yes | manual, or automatic for a new season after paused/dropped/skipped |

- **R2.4 tracked** — appears in a show that is itself tracked (is in the database).
  Automatic. Not a picker status (caveat: see skipped).
- **R2.5 airing** — current date is between the air date of the first episode
  mapped to the season and the air date of the last episode mapped to it.
  Automatic. Not a picker status.
- **R2.6 watching** — the season is actively being watched. Picker status. **Set
  manually only**, with one exception: R2.14 (planned season + an episode
  watched). `[clarified 2026-09-27, Q-B]`
- **R2.7 completed**
  - automatic when all episodes within are watched;
  - may be set manually → **sets all its episodes watched** (mirrors AniList, which
    marks every episode of a season watched when the season is set completed);
  - specials and movies not in the season's list of mapped episodes — even inside
    its air-date window — are completed individually and separately;
  - **CAVEAT — any level with an id:** watch status applies at whatever level holds
    an id (TVDB season, AniList part 1 / part 2…). Every episode mapped to that id
    is marked watched. So marking TVDB season X watched marks AniList part 1 and
    part 2 watched, and all their episodes.
- **R2.8 planned**
  - manual: any show/season the user wants to watch and track but has not watched;
  - automatic: seasons of tracked shows that are not aired yet and not
    paused/dropped/skipped (per the auto rules).
- **R2.9 paused** — tracked, not being watched or planned yet; tracking continues.
  Set manually. Following new seasons are **skipped** (R2.16). `[clarified
  2026-09-27, Q-A: "skipped everywhere"]`
- **R2.9b dropped** — tracked; the user decided not to follow it in future;
  tracking continues. Set manually. Following new seasons are **skipped** (R2.16).
  `[clarified 2026-09-27, Q-A]`
- **R2.10 skipped** — originally to clear future seasons from the add/browse view.
  Applied to seasons following a dropped or paused season. Intended to help sort
  the mass of wrongly added seasons. `[clarified 2026-09-27, Q-C, Q-J]`
  - **Skipped = not a followed season.** Essentially *not in the database*, but
    kept so that numbering aligns and so it does not show in future/airing lists
    (add, browse).
  - The user can pick it (picker status).
  - It triggers **none** of: episode/air-date fetching, availability, calendar /
    next-up, counting toward the show.
  - **Exception:** its episodes are fetched only when a later season is added as
    not skipped and the show needs them for absolute numbering.
  - **Never on external databases** (AniList/MAL): skipped is not mirrored.
  - **Where skipped shows/seasons appear** `[clarified 2026-09-28]`: in **browse and
    add**, where they can be filtered in or out, so the user can change a skipped
    status to something else. **Nowhere else** (calendar, next-up, backlog, airing
    lists…).
  - **Which seasons can be skipped automatically:** a not-yet-tracked season, or
    one auto-added as planned (Q-J3), or one you set planned, after a warning (Q-J4). A season **auto-added as planned** that the user then moves to
    skipped is **deleted from AniList/MAL**.

### 2.3 Sonarr status (relationship detailed in §5)

- **R2.11 Sonarr tracked** — the show is in Sonarr.
- **R2.12 monitored** — Sonarr monitors the show and downloads new episodes. As a
  rule, **future episodes only**.

### 2.4 Auto rules

- **R2.13 Show status = status of its last season that is not skipped.** If all
  seasons (or the only season) are skipped, the show can be set skipped.
  "Last season" = the TVDB season (its status aggregated from its parts, R2.18).
  `[clarified 2026-09-27, Q-H]`
- **R2.13a Setting a status on the show** applies it to the **last non-skipped
  season**; the show is then derived per R2.13. `[clarified 2026-09-27, Q-B2]`
- **R2.13b Skipped picked on the show** `[clarified 2026-09-28, Q-W]`: R2.13a applies
  (the last non-skipped season becomes skipped), **but the show as a whole is
  dropped** — the one exception to "a show has the status of its last non-skipped
  season". Detailed by the user (2026-09-28, sub-questions in Q-W2):
  - no season watched → the show is **skipped**;
  - earlier seasons completed / dropped / paused and a later season tracked → the
    show takes the status of the last non-skipped season, **except completed, which
    shows as dropped** at show level;
  - earlier seasons don't change; future seasons are auto-skipped;
  - changing the show's status afterwards: (1) a warning says the show has skipped
    seasons; (2) completed or watching affects only the last season; any other
    status leaves the latest season skipped;
  - watching an episode of a skipped season → that season **watching**; earlier
    skipped seasons stay skipped; show → **watching**; a warning may say you're
    watching a season out of order with earlier seasons unwatched / skipped.
    (Extends R2.14 to skipped seasons.)
  - Answers to Q-W2 `[clarified 2026-09-28]`:
    (a) a season **in progress** (watching) when you pick skipped on the show → a
    warning: "a season is in progress; proceeding switches it to dropped —
    continue?"; if yes, that season → **dropped**, later seasons → skipped.
    (b) afterwards, **planned** picked on the show works like watching (and
    completed): it goes on the show's last season; **paused or dropped** go on the
    latest non-skipped season, later seasons skipped.
    (c) the show-level "dropped" ends when you next pick a status other than
    skipped on the show or one of its seasons, or when you watch an episode of a
    skipped season.
    (d) **every season after a skipped season stays skipped unless you say
    otherwise** — watching an episode of a skipped season doesn't un-skip later
    seasons (this replaces "later seasons → planned" above).
- **R2.14** Season planned + one episode set watched → season watching. This is
  the **only** automatic path to watching (R2.6). `[clarified 2026-09-27, Q-B]`
- **R2.15** All episodes of a level with an id watched → that level completed —
  also when that level is paused or dropped. `[clarified 2026-09-27, Q-K2]`
  Conversely, a level set completed → all episodes mapped to it watched.
- **R2.16 New season found** gets:
  - previous season completed / watching / planned → **planned**;
  - previous season paused / dropped / skipped → **skipped**.
  - Applies to a season not yet tracked **and to a season auto-added as planned**:
    e.g. S3 auto-added planned, then S2 set dropped → S3 becomes **skipped** and is
    deleted from AniList/MAL. `[clarified 2026-09-27, Q-A, Q-J, Q-J3]`
    A season whose status **you** set planned also becomes skipped, **after a
    warning** in the UI (e.g. "this show has later seasons set as planned — skip
    all of them?"). `[clarified 2026-09-27, Q-J4]`
  - Example: S1 watched, S2 dropped, S3 skipped → show is **dropped** (R2.13),
    S4 auto-marks **skipped**.
- **R2.17** Last season watching → show watching; that season set completed → show
  completed; when a new (planned) season is added → show reverts to planned; and so
  on.
- **R2.19 Earlier seasons found late** (e.g. S3 added, S1–S2 exist on TVDB but not
  in LCARS; or S2 found after S3 is tracked) → **skipped**, but their episode list
  is retrieved so absolute numbering can be set (R1.0). `[clarified 2026-09-27, Q-N]`
- **R2.18 Overlapping levels (TVDB season ⊃ parts)** `[clarified 2026-09-27, Q-H]`
  - Status set on the TVDB season cascades to its parts, **except a part already
    completed stays completed**. E.g. S2 set dropped: part 1 completed → stays
    completed, part 2 → dropped; part 1 not completed → both dropped.
  - Status of the TVDB season from its parts: part 1 completed + part 2 planned →
    season **watching** → show watching (R2.13).

---

## 3. Adding shows (automatic, browse, add)

- **R3.1** Anything added (manually too) must pass tests deciding whether it is:
  a new season of an existing show, part of a season of an existing show, or the
  first season of a new show.
- **R3.2** Every addition must be linked to a **TVDB id** (show level). With no TVDB
  id, the season may be added as an **individual season**, waiting to be linked to
  a show once a TVDB id exists.
- **R3.3** One exception: a historical (already recorded) show with no TVDB id
  available. None known so far; any case must be **checked by the user, never
  automated**.
- **R3.4** A new season found automatically gets its status from the auto rules
  (R2.16). If the user finds it first, they can add it with any status and the
  rules apply from that manual status. The UI may show a message explaining the
  effect (e.g. "setting this season completed will mark x (unaired) episodes
  watched").
- **R3.5** A season found automatically (AniList relation, Fribb or other) as
  related to a previous one **but linked to a different TVDB id**:
  - that show is tracked → add it to that show with the corresponding status;
  - that show is not tracked → **do not add it at all**.
- **R3.5a** Relation type does not decide anything: a related entry is added
  automatically **only if it belongs to a show by TVDB id** — the same show, or a
  tracked show (R3.5). Everything else (other TVDB ids not tracked, crossovers…)
  is not added; it belongs to the future franchise/universe layer.
  `[clarified 2026-09-27, Q-G]`
- **R3.6** A season found automatically (AniList relation, Fribb or other) that does
  **not appear in TVDB** may be added provisionally as an **individual season**,
  **planned only**, until TVDB adds the season / maps the episodes to a show with a
  TVDB id.
- **R3.6c Scope of individual seasons** `[clarified 2026-09-27]`: only for a **new
  planned season** (e.g. found on AniList) that can't be reconciled to a TVDB id
  **yet**, because TVDB doesn't have it. Never used for anything existing today,
  except seasons added in the last two weeks (since 2026-09-13) that have no TVDB
  link. Existing shows without a TVDB id get one (lookup), or go to the user
  (R3.3); they are not turned into individual seasons.
- **R3.6d** An individual season has its own kind, `individual_season`, and becomes
  a `tvdb_season` when it joins a show. `[clarified 2026-09-28, Q-S]`
- **R3.7 TVDB links must be verified** `[clarified 2026-09-27]`: wrongly matched
  TVDB ids started much of the mess. A TVDB id is only accepted automatically
  when an independent source agrees (Fribb/anime-lists, or Sonarr's own series
  for that TVDB id) **and** the TVDB name matches the season's titles. A link
  from a title search alone is never accepted automatically; any disagreement
  goes to the user.
- **R3.7b** TVDB matching also searches the **original (Japanese) title** — e.g. Bless
  was found by ブレス. `[clarified 2026-09-28]`
- **R3.7c** An entry that will **never be on TVDB** but is a piece of a show you track
  (e.g. a special of an AniList-only show) is attached to that show as one of its
  levels, when you confirm it. `[clarified 2026-09-28]`
- **R3.7a** A TVDB id that LCARS derived but no other source confirms is shown in
  **browse** (and the add confirmation) for the user to confirm: the user is then
  the second independent source. `[clarified 2026-09-27]`
- **R3.6b** An individual season is mirrored to AniList/MAL like any other season.
  `[clarified 2026-09-27, Q-I2]`
- **R3.6a** An individual season follows the normal status rules: e.g. an episode
  marked watched → it goes to **watching** (R2.14). When a TVDB id is found or
  derived, it joins that show and the show-level logic applies from then on.
  `[clarified 2026-09-27, Q-I]`

---

## 4. Mirroring — internal ↔ external databases

### 4.1 TV databases

- **R4.1** All ids are matched. No external database mirrors LCARS for now; LCARS is
  the sole tracker.

### 4.2 Anime databases (AniList, MAL)

- **R4.2** All matching per the rules above.
- **R4.3** LCARS is the source of truth, but updates may come from any direction.
- **R4.4** AniList and MAL mirror each other wherever possible (scoring rules are
  separate).
- **R4.5** A change made in LCARS is mirrored to AniList/MAL where applicable.
- **R4.6a** AniList REPEATING / rewatching → **watching**, for now. Not expected to
  be used until a full rewatch setup exists (that will change the schema).
  `[clarified 2026-09-27, Q-M]`
- **R4.6** skipped is not set as anything remotely (never on AniList/MAL; an
  auto-added planned season moved to skipped is deleted there — R2.10). watching mirrors to watching,
  … *(full status mapping — Q-J, Q-M)*.
- **R4.7** An entry set to planning in AniList/MAL is added to LCARS through the
  normal check (§3), **as a season** (AniList/MAL are always season level): map the
  season to a TVDB id, and within it map episodes to season / sub-season / special
  run. If mapping to a TVDB id is impossible → individual season until it can be
  mapped.
- **R4.7a** A skipped season deleted from AniList/MAL that you later add there
  yourself comes back through the normal add (R4.7) with the status you set there.
  `[clarified 2026-09-27, Q-J2]`
- **R4.8 Changes propagate through LCARS.** E.g. an episode watched in MAL changes
  LCARS, and LCARS propagates it to AniList.
- **R4.8a A season set completed on AniList/MAL** `[clarified 2026-09-28]` is mirrored
  like R2.7: you watched all its episodes, so the season is completed and **all its
  episodes are marked watched, aired or not**. When that marks unaired episodes
  watched, a **review** is raised: accept the change, or revert to watching without
  marking the episodes; it links to the show page so it can be corrected there too.
- **R4.8b Every review is actionable** `[clarified 2026-09-28]`: it offers the choices
  that resolve it, and links to the show page.
- **R4.9 Flip-flop protection.** Checks of each external DB run separately. A change
  found triggers its propagation **first**, before a new check of the other
  database is made.
- **R4.10** If a propagation is blocked (API down or other), **propagation calls
  take precedence over checks**. Remote changes are compared by timestamp: a remote
  change later than the reconciliation supersedes it; an earlier one is moot.

---

## 5. Sonarr

- **R5.1** Adding a show as monitored in Sonarr goes through the normal add process
  and checks (§3). A Sonarr add is ALWAYS based on a TVDB id.
- **R5.2** Shows/seasons added in Sonarr are added to LCARS as **planned**.
- **R5.3** A show added from Sonarr that already has several seasons, none tracked
  in LCARS yet → the **latest season planned**, every previous season **skipped**
  (episodes still retrieved for absolute numbering, R2.19) → the show is planned
  by R2.13; skipped seasons are ready to be turned to watching/planned manually.
  `[clarified 2026-09-27, Q-D1]`
- **R5.4** A Sonarr status change in Sonarr has **no effect** in LCARS.
- LCARS → Sonarr:
  - **R5.5** new show added in LCARS (with TVDB id) → added to Sonarr, monitored,
    future episodes only;
  - **R5.6** season added or changed to planned/watching → Sonarr monitors future
    episodes; if the show is not in Sonarr yet it is added;
  - **R5.7** show/season changed to completed → nothing in Sonarr;
  - **R5.8** show/season changed to paused/dropped/skipped → Sonarr (if the show
    exists there) set **unmonitored**; `[clarified 2026-09-27, Q-K]` per season:
    dropping S3 unmonitors **S3 and every later season**; the season currently
    being watched (e.g. S2) is untouched, and the show status follows R2.13;
  - **R5.10** Sonarr status changes apply the same way to anime and TV series.
    `[clarified 2026-09-27, Q-L]`
  - **R5.9 No Sonarr change is retroactive.** `[clarified 2026-09-27, Q-D2]` A show
    already in LCARS and not in Sonarr is **never added to Sonarr automatically** —
    not when a season is set watching/planned, not when a new season is found.
    Adding it is manual (common early on; also covers shows watched locally, on
    DVD etc., before being added through Sonarr). The "add if missing" of R5.5/R5.6
    applies to a show newly added to LCARS. A show pre-existing in LCARS, planned
    now, not in Sonarr, is **not** created in Sonarr automatically — manual only.

---

## 6. TV-series notes

- Absolute order source: TVmaze (R1.19).
- Season 0 is redistributed by air date, as for anime (R1.8). `[clarified 2026-09-27, Q-L]`
- Sonarr rules are identical for TV and anime (R5.10).
- TVDB season numbering is the guide for both (R1.7).
- Everything else: ask before applying to TV series.

## 7. Working rules for Claude (from handoffs)

- Read-only by default; nothing applied without explicit user approval.
- No code change without the user's validation, after explaining its consequences.
- **Everything conforms to the rulebook, names included.** A function, field or
  label whose name clashes with a rulebook term is renamed; anything that can't
  conform is brought to the user to align first. (user, 2026-09-27)
- Never write to AniList/MAL until the value is known correct.
- Never derive rules; if not entirely sure, ask. Check past work and code.
- Validate data against this rulebook before building on it.

## 8. Glossary

- **Level**: episode / sub-season (source part) / season (TVDB) / show (TVDB id) /
  franchise / universe.
- **Individual season**: a season with no TVDB show yet (R3.2, R3.6, R4.7).
- **Span**: a contiguous range of absolute episode numbers; a season is one or more
  spans (R1.11–R1.12).

## 9. Open questions

Asked 2026-09-27. Answers move into the rules above and are logged in §10.

- **Q-A (R2.9 vs R2.16)** Paused/dropped say following seasons become *paused* **[ANSWERED → R2.9, R2.9b, R2.16]**
  onward; R2.16 says a new season after paused/dropped/skipped becomes *skipped*.
  Which? (a) skipped everywhere · (b) paused after paused, skipped after dropped ·
  (c) paused onward, and skipped only after skipped.
- **Q-B (R2.6 vs R2.14)** Watching is "manual only", but R2.14 sets it when an **[ANSWERED → R2.6, R2.14]**
  episode of a planned season is watched. Is R2.14 the one automatic exception?
  Does watching an episode of a paused / dropped / skipped season also make it
  watching?
- **Q-B2 (R2.13)** Show status is derived. When the user picks a status on the **[ANSWERED → R2.13a]**
  *show* (not a season), what does it apply to? (a) the last non-skipped season ·
  (b) every season not completed · (c) show pickers disabled, seasons only.
- **Q-C (R2.10)** Is skipped a picker status the user can set? For a skipped **[ANSWERED → R2.10]**
  season, which still happen: episode/air-date fetching, availability, appearing
  in calendar/next-up, counting toward the show?
- **Q-D (R5.3, R2.13, R5.6, R5.9)** (1) Sonarr add of a multi-season show: show **[OPEN — re-asked as Q-D1, Q-D2]**
  *planned* with every season skipped, while R2.13 says all-skipped → show skipped.
  Is R5.3 an exception? Are *unaired* seasons in that case planned or skipped?
  (2) "Add to Sonarr if missing" (R5.6) vs "no retroactive change" (R5.9): is it
  "a user status change adds; a background sweep never adds"?
- **Q-E (R1.3–R1.8)** In your example the film is abs 13 and S2E1 is abs 14; **[ANSWERED → R1.2a]**
  TVDB/AniDB absolute numbering would make S2E1 = 13. Confirm LCARS keeps its
  **own** absolute numbering (whole numbers inserted shift everything after),
  with each source's numbering stored only as a mapping.
- **Q-F (R1.3, R1.8a)** Decimal scheme when several items fall in one gap, e.g. **[ANSWERED → R1.2b]**
  two specials between ep 5 and ep 6: 5.1 / 5.2, or 5.5 then …? And the Frieren
  movie = 0.5 because it sits *before* ep 1 — so an item before ep N is (N-1).x?
- **Q-G (R3.5, R3.6, R1.20)** Which AniList relations count as "found as **[ANSWERED → R3.5a]**
  related": SEQUEL/PREQUEL only? SIDE_STORY, SPIN_OFF, ALTERNATIVE → franchise
  level (not added)? CHARACTER (the crossovers: Toriko → ONE PIECE) → excluded /
  universe only?
- **Q-H (R2.7 caveat, R2.13)** Levels overlap: TVDB S2 = AniList part 1 + part 2. **[ANSWERED → R2.13, R2.18]**
  If S2 is set dropped, are both parts dropped? If part 1 is completed on AniList
  and part 2 untouched, what is S2 — watching? planned? For R2.13 "last season",
  is that the TVDB season or its last part?
- **Q-I (R3.6, R4.7)** An individual season is "planned only". If you watch an **[PARTLY ANSWERED → R3.6a; follow-up Q-I2]**
  episode of it (R2.14) or AniList has it as watching, does it still become
  watching? Is an individual season pushed to AniList/MAL?
- **Q-J (R4.6)** A season goes skipped but already has an AniList/MAL entry: **[PARTLY ANSWERED → R2.10; follow-up Q-J2]**
  leave the remote entry untouched, or delete it? If the remote entry for a
  skipped season changes (e.g. you set it watching on AniList), does LCARS take
  it (un-skip)?
- **Q-K (R5.6, R5.8)** Sonarr monitoring is per series and per season. Dropping **[ANSWERED → R5.8]**
  S3 while S2 is watching: unmonitor S3 only, or the whole series? Planned/watching
  → monitor future episodes of that season only?
- **Q-K2 (R2.15)** Every episode of a paused or dropped season ends up watched: **[ANSWERED → R2.15]**
  does it become completed?
- **Q-L (TV series)** Which of §1 applies to TV? Season 0 redistribution **[ANSWERED → §6, R5.10]**
  (Christmas specials etc.) with TVmaze absolute order? Sub-seasons (none in
  TV sources I know of)? Anything TV-specific about statuses/Sonarr?
- **Q-M** AniList REPEATING / rewatching: map to watching (current code), or **[ANSWERED → R4.6a]**
  something else?
- **Q-N (R2.16, R3.4)** Earlier seasons found late: you add S3 from AniList, and **[ANSWERED → R2.19]**
  S1–S2 exist on TVDB but not in LCARS. What status do S1–S2 get — skipped (like
  R5.3)? Same when S2 is discovered after S3 is already tracked?
- **Q-D1 (R5.3, re-asked)** A show is added in Sonarr with S1–S4 already aired and **[ANSWERED → R5.3]**
  none in LCARS. Your answer says one season is added as planned, so the show is
  planned mechanically. **Which season** is the planned one: (a) the last season ·
  (b) none of the aired ones, only a future/unaired season if there is one — and
  if there is none, all skipped and the show skipped · (c) something else?
- **Q-D2 (R5.6 vs R5.9)** "Add to Sonarr if missing" vs "no retroactive change":
  is it "your status change to planned/watching adds it to Sonarr; a background
  sweep never adds a pre-existing show"?
- **Q-I2 (R3.6)** Is an individual season (no TVDB id yet) mirrored to **[ANSWERED → R3.6b]**
  AniList/MAL like any other season while it waits for a TVDB id?
- **Q-J2 (R2.10, R4.7)** A season was skipped and deleted from AniList. If you **[ANSWERED → R4.7a]**
  later add that entry on AniList yourself, it comes back through the normal add
  (R4.7) and takes the status you set on AniList — correct?
- **Q-J3 (R2.10 vs R2.16)** S3 already exists auto-added as **planned**, then you **[ANSWERED → R2.16, R2.10]**
  set S2 **dropped**. Does S3 stay planned (it already has a status), or become
  skipped?
- **Q-P (example)** "season 2 part 2 … is defined as starting at abs 14 ending at
  abs 26" — that span is part 1 in the tree; part 2 starts at abs 27. Typo? **[unclear — re-asked as Q-P2]**
- **Q-D2 (re-asked, R5.6 vs R5.9)** Show X has been in LCARS since before, not in **[ANSWERED → R5.9]**
  Sonarr. (1) Nothing changes → it stays out of Sonarr (R5.9). (2) Today you set
  one of its seasons to watching → is X added to Sonarr (R5.6)? (3) A new season
  of X is auto-found and set planned → is X added to Sonarr?
- **Q-J4 (R2.10, R2.16)** Same as Q-J3, but S3 is planned because **you** set it **[ANSWERED → R2.16]**
  planned. When you drop S2, does S3 also become skipped, or stay planned?
- **Q-P2 (worked example)** Your sentence says "season 2 part 2 … is defined as **[ANSWERED → R1.12]**
  starting at abs 14 ending at abs 26". In your tree, abs 14–26 is **part 1**
  (ep1 abs 14 … ep13 abs 26) and part 2 starts at abs 27. So should the sentence
  read "season 2 part **1** is abs 14–26" (and part 2 = abs 27 → end)?

- **Q-R (R1.9a, R1.12, R1.13)** A film or mini inside a season's air window **[ANSWERED → R1.13a]**
  (R1.12's example: S2 = abs 13–16 and 18–24, the film abs 17). R1.9a calls it a
  sub-season of S2, but S2's spans leave abs 17 out. Is the film (a) a sub-season
  under S2, whose span sits in the gap between S2's spans, or (b) its own level
  beside S2, not under it? (Parts — AniList/MAL cours — are always inside their
  TVDB season, R2.18.)
- **Q-S (naming, §7, R3.6c)** In the database each level has a kind: `tvdb_season`, **[ANSWERED → R3.6d]**
  `part`, `special`. An individual season (no TVDB show yet) is stored today as
  `tvdb_season` without a show, which clashes with its name. Give it its own kind
  (`individual_season`)? It would become `tvdb_season` when it joins a show.

- **Q-T (R1.0, R1.2, R1.18–19)** A show whose absolute-numbering source has no **[ANSWERED → R1.2d]**
  data (AniDB doesn't list its episodes yet, no TVmaze entry): R1.0 says every
  episode has a number, R1.2 names the sources. May Memory Alpha number it from
  TVDB order + air date and put it on a list for you, or leave it unnumbered and
  listed until the source has it?

- **Q-U (R1.2b, R1.5, R1.9a)** **One** special alone in a gap (e.g. a single OVA **[ANSWERED → R1.2b]**
  between ep 5 and ep 6): does it take **5.1** (R1.2b's `.1, .2…`) or **5.5**
  (like Frieren's 0.5 and the N.5 season numbers)? Two or more stay 5.1, 5.2….

- **Q-V (R1.8b)** Frieren's ten 1–2 min minis between S1 and S2: whole numbers **[ANSWERED → R1.8d]**
  (S2 starts at 39) or decimals?

- **Q-W (R2.13a, R2.10)** Picking **skipped on a show**: R2.13a puts it on the last **[ANSWERED → R2.13b]**
  non-skipped season only; the show is then derived from the season before it, so the
  show would read e.g. *completed*, not skipped. Should picking skipped on a show skip
  **every** season (the show then reads skipped), or follow R2.13a literally?

- **Q-W2 (R2.13b)** Gaps in the detailed rule: **[ANSWERED → R2.13b]**
  (a) an earlier season **watching** (in progress) or **planned** when you pick
  skipped: does the show read watching / skipped? (b) "any other status leaves the
  latest season skipped": planned / paused / dropped picked then stay at show level
  only? (c) when does the show-level "dropped" end — on your next status pick (show
  or season), or on watching an episode? (d) "later seasons → planned" when you
  watch a skipped season: all later seasons, including ones skipped after a drop
  (R2.16)?

- **Q-X (R2.13b)** Skipped picked on a show where S1 is completed and S2, S3 are **[OPEN]**
  planned: are **both** S2 and S3 skipped (every planned season after the last
  watched one), and the show reads dropped? (Built that way; asked 09-28.)
- **Q-Y (R4.8a)** A list flips a season to completed while LCARS has **aired but **[OPEN]**
  unwatched** episodes (the 09-20 flip-flop: AniList/MAL flip an airing entry as
  their episode count catches up). Today those episodes are marked watched with no
  review (only unaired ones raise one). Review there too, or is marking them right?

## 10. Changelog

- 2026-09-27 — created from the user's rule text.
- 2026-09-27 — R2.9/R2.9b kept literal ("paused onward"); skipped picker marked ?; §9 questions Q-A…Q-P added.
- 2026-09-27 — R1.0 added (every episode has an abs number, always the rule); R1.8 clarified (TVDB season 0 is source numbering only); "today's rules are the baseline" principle added.
- 2026-09-27 — code changes require the user's validation, with consequences explained first.
- 2026-09-27 — answers Q-A, Q-B, Q-B2, Q-C, Q-E, Q-F, Q-G, Q-H, Q-I, Q-J folded in:
  R1.2a, R1.2b, Memory Alpha note, R2.6, R2.9, R2.9b, R2.10, R2.13, R2.13a, R2.14,
  R2.18, R3.5a, R3.6a; R1.0 moved to §1.1. Follow-ups Q-D1, Q-D2, Q-I2, Q-J2, Q-J3.
- 2026-09-27 — answers Q-K, Q-K2, Q-L, Q-M, Q-N, Q-D1, Q-I2, Q-J2, Q-J3 folded in:
  R2.15, R2.16, R2.19, R3.6b, R4.6a, R4.7a, R5.3, R5.8, R5.10, §6. Re-asked Q-D2, Q-P2; new Q-J4.
- 2026-09-27 — Q-P2, Q-D2, Q-J4 answered: R1.12 (span list), R5.9 (never auto-add a
  pre-existing show to Sonarr), R2.16 (user-planned later seasons skipped after a
  warning). All §9 questions answered.
- 2026-09-27 — R3.6c (individual seasons: new planned seasons only) and R3.7 (TVDB links verified).
- 2026-09-27 — R3.7a (unconfirmed TVDB id shown in browse for the user to confirm); R1.9a (season numbers TVDB first, side pieces between seasons get decimal season numbers, e.g. S2.5).
- 2026-09-27 — R1.9a: one side piece N.5, several N.1, N.2…
- 2026-09-27 — naming must follow rulebook terms (§7).
- 2026-09-27 — R1.2c: Memory Alpha sets every absolute number (confirmed).
- 2026-09-28 — Q-R (film/mini inside a season window: under the season or beside it?) and Q-S (kind for individual seasons) asked.
- 2026-09-28 — Q-R answered → R1.13a (own level, shown in air order, own art); Q-S answered → R3.6d (kind `individual_season`).
- 2026-09-28 — Q-T asked (numbering fallback when AniDB/TVmaze have no data).
- 2026-09-28 — Q-T answered → R1.2d (TVDB-order fallback following all rules, reconciled when the source appears).
- 2026-09-28 — Q-U asked (single special in a gap: .1 or .5).
- 2026-09-28 — R1.2d build model (TVDB-order first, reconcile with AniDB; ship before AniDB is complete; AniDB fetch priority); Q-U answered → R1.2b (single item → .5).
- 2026-09-28 — Q-V answered → R1.8d (minis between seasons take decimals, in their decimal season; next season keeps its number).
- 2026-09-28 — Q-W asked (skipped picked on a show).
- 2026-09-28 — R2.10 (skipped appears in browse/add only, filterable), R2.13b (Q-W: skipped on a show → last season skipped, show dropped), R4.8a (remote completed mirrors R2.7, review for unaired), R4.8b (reviews actionable, link to show page).
- 2026-09-28 — R2.13b detailed by the user; Q-W2 asked (gaps).
- 2026-09-28 — Q-W2 answered → R2.13b (a)–(d).
- 2026-09-28 — R1.0a (no air date → placeholder shown as x), R1.13b (every episode in a level; mini sub-seasons; art per level), R3.7b (match on the Japanese title), R3.7c (pieces never on TVDB attach to their show).
- 2026-09-28 — R1.0a: placeholder stored as 5000.1, 5000.2… (shown as x).
