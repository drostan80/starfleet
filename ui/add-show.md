# Add Show — Build Plan

Two features: an "Add Show" page (web client), and per-season status
(LCARS schema change). Ordered so web-only work ships first.

---

## Step 0 — Decisions (resolved)

### Q1. Per-season status → **Derived**

Season has its own status. Show status is computed from season statuses:

**Rollup rules:**
- Show takes the **last (highest-numbered) season's status**
- **Exception:** if the last season is PLANNED and it's not the first
  season → show status = WATCHING (you're watching the show, planning
  the next season)
- If all episodes of all available seasons are marked watched → show
  status = COMPLETED

**First-season guarantee:** adding season N > 1 without seasons 1
through N-1 existing must either:
- **Error** with a clear message ("Season 2 cannot be added without
  Season 1 — add previous seasons first"), or
- **Route to validation** with a way to search and add previous seasons
  inline

A new season status value **UNTRACKED** is not needed — missing season
rows simply don't exist, and the error/validation path handles gaps.

**Consumer audit required:** every place reading/writing `show.status`:
- `setStatus(showId)` → becomes a fanout or is deprecated in favour of
  `setSeasonStatus(showId, seasonNumber, status)`
- Backlog query, calendar, `watch_reconcile`, AniList push,
  `pollScoreSync`, the `confirmed: Boolean` completion guard — all
  need per-season awareness
- AniList is already per-entry (= per-season), so the push maps
  cleanly to per-season status

### Q2. Greyed-out chip → **`softDeleteShow`** (option b)

Verified: `softDeleteShow` is mechanically identical to
`setTracked(showId, false)`:
- Sets `tracked = 0`, records in `tracked_change` history
- Unmonitors in Sonarr/Radarr (best-effort, `pending_review` on
  failure)
- Status left unchanged (a completed show soft-deleted stays completed)
- Reversible: `setTracked(showId, true)` re-tracks (but does NOT
  re-monitor — deliberate, requires a separate action)

**No extra consequences.** The full hard-delete chain
(`requestHardDelete` → delay → `confirmHardDelete(retype)`) stays on
the show detail page only. On the add page, clicking the active chip
on a tracked show calls `softDeleteShow` with a confirmation popover.

### Q3. Season resolution → **Manual with passive auto-detection**

Two parts:

**a) Manual "is season X of show Y" button** on every search result
card. The user can always explicitly link a candidate to an existing
show as a new season.

**b) Passive auto-detection** using LCARS's `search` query (which
searches across `titleRomaji`, `titleEnglish`, `titleNative`,
`synonyms`, and `displayTitle`): when a candidate title is a close
enough match to an existing tracked show (e.g. "Romaji Title Season 3"
matches "Romaji Title", or "Romaji Title Arc Title" matches "Romaji
Title"), the add page **prompts** for the mapping: "Is this Season X
of Show Y?" with a one-click confirm.

Matching heuristic: strip common season/part suffixes and
number/ordinal patterns from the candidate title, search LCARS for the
base title, flag matches above a threshold. Not perfect — but catches
the obvious cases and the manual button handles the rest.

### Q4. Status determines arr behavior → **LCARS change needed**

The user's chosen status at add time controls whether and how the show
is added to Sonarr/Radarr:

| Status    | Arr behavior                                    |
|-----------|------------------------------------------------|
| PLANNED   | Add to arr, monitored (follow + auto-download) |
| WATCHING  | Add to arr, monitored                          |
| PAUSED    | Add to arr, **unmonitored** (track but don't download) |
| COMPLETED | **Don't add to arr** — use `addShow` not `addShowWithArr` |
| DROPPED   | **Don't add to arr** — use `addShow` not `addShowWithArr` |

**Current state:** `_ensure_in_arr` (shows.py:372) always sets
`monitored: True` and `searchForMissingEpisodes: True`. No parameter
to control this.

**LCARS changes needed:**
- Add `monitored: Boolean = true` to `AddShowWithArrInput`
- Pass through to `_ensure_in_arr`'s Sonarr/Radarr payload
- When `monitored = false`: set `monitored: false` on the series/movie
  AND `searchForMissingEpisodes: false` / `searchForMovie: false`

**Web client logic:** status picker on the add card decides which
mutation to call:
- PLANNED / WATCHING → `addShowWithArr` (monitored)
- PAUSED → `addShowWithArr` with `monitored: false`
- COMPLETED / DROPPED → `addShow` (no arr at all). Can be added to
  arr later if a future season comes up as PLANNED/WATCHING.

`addShow` needs the same `status` parameter discussion — currently
hardcodes `status = 'planned'`. For COMPLETED/DROPPED adds, the web
client calls `addShow` then `setStatus`.

### Q5. AniList relations → **Auto-add season as PLANNED**

When AniList metadata fetch populates `show_relation` (via
`_link_relation` in metadata.py) and detects a sequel relation for an
already-tracked show:

- **Auto-create** the new season row as PLANNED (using
  `setSeasonMapping` + `setSeasonStatus`)
- **Exception:** if the show is DROPPED → add the season as DROPPED
  too, or skip entirely (whichever is simpler — skip is simpler, lean
  towards skip)

**On the add page:** searching for a show that was already auto-added
as PLANNED via this mechanism should:
- Show it as "already tracked — Season N: PLANNED"
- Selecting it should detect the relationship and prompt "Is this
  Season X of Show Y?" with a confirm

**LCARS change needed:** `_link_relation` in metadata.py currently only
creates `show_relation` rows (directed edges). It would need to also:
- Check if the related show is already tracked
- If it's a sequel relation and the tracked show has season data: auto-
  create a season row with `status = PLANNED`
- This happens during `refreshShowMetadata` / ops daily pass, not as a
  separate poll

**Open question:** AniList relation types — `_link_relation` currently
writes all relations with no type distinction (the `show_relation`
table has no `relation_type` column). To know "this is a sequel" vs
"this is a side story" vs "this is an adaptation", LCARS would need to
either store the relation type or inspect it at link time. The AniList
API returns `relationType` (SEQUEL, PREQUEL, SIDE_STORY, etc.) —
`_link_relation` receives the full `related_media` dict which carries
this. **Needs a `relation_type` column on `show_relation`, or at
minimum a filter in the auto-season-add logic to only act on SEQUEL.**

---

## Batch 1 — Add Show page (web, minimal LCARS changes)

### 1.1 `api.js` — new query + mutation wrappers

```js
searchArrCandidates(mediaShape, title) → ArrCandidate[]
addShowWithArr(input)                  → AddShowResult
addShow(input)                         → Show
softDeleteShow(showId)                 → Show
```

Plus existing: `setStatus`, `setSeasonMapping`, `fetchShow`, `gql`.

### 1.2 LCARS — `monitored` parameter on `AddShowWithArrInput`

- Add `monitored: Boolean = true` to `AddShowWithArrInput`
- Pass through to `_ensure_in_arr`'s Sonarr/Radarr payload
- `monitored: false` → `searchForMissingEpisodes: false` /
  `searchForMovie: false`

Small change, one field, one conditional in `_ensure_in_arr`.

### 1.3 `add.html` — the page

**Layout:**
- Nav bar (shared with other pages)
- Top bar: four buttons — **Add Anime**, **Add TV**, **Add Movie**,
  **Add Anime Movie**
  - Anime = `{mediaShape: EPISODIC, trackingSpace: ANIME}`
  - TV = `{mediaShape: EPISODIC, trackingSpace: TV}`
  - Movie = `{mediaShape: MOVIE, trackingSpace: TV}`
  - Anime Movie = `{mediaShape: MOVIE, trackingSpace: ANIME}`
- Search input: text field, enter-to-search
- Results grid: candidate cards

**Candidate card:**
- Title + year (bold)
- Overview/synopsis (truncated, 2–3 lines)
- Status picker row: five chips — PLANNED, WATCHING, PAUSED, COMPLETED,
  DROPPED — all greyed out initially
- "Season of…" link/button (Q3 — manual sequel mapping, see 1.5)

**Card interactions:**

*Adding a new show:*
- Click a status chip → loading spinner on the card
- PLANNED/WATCHING → `addShowWithArr({..., monitored: true})`
- PAUSED → `addShowWithArr({..., monitored: false})`
- COMPLETED/DROPPED → `addShow({...})` then `setStatus(showId, status)`
- On success: chip becomes coloured, card marked as tracked
- Default status is PLANNED (LCARS's `create_show` default), so
  PLANNED adds need no follow-up `setStatus` call

*Already-tracked show (detected via `addShowWithArr` error or
pre-check):*
- Card shows current status chip as active
- Clicking a different chip → `setStatus(showId, newStatus)`
- Clicking the active chip → confirmation popover "Remove {title}
  from library?" → `softDeleteShow(showId)` → all chips grey out

### 1.4 Auto-detection of existing shows (Q3b — passive matching)

After `searchArrCandidates` returns results, for each candidate:

1. Strip season/part/ordinal suffixes from the candidate title
   (regex: ` Season \d+`, ` Part \d+`, ` \d+nd Season`, etc.)
2. Search LCARS with the stripped base title using the existing
   `search(query)` query
3. If a match is found: annotate the card with a banner —
   **"Possible season of: {Show Title}"** with a confirm button

On confirm → pivot to the **season-add inline flow** (Batch 3.1):
show the existing show's seasons, pre-fill a new season form.

On dismiss → treat as a new show, normal add flow.

This runs client-side after the initial search, as a non-blocking
enrichment pass. The search results appear immediately; the "possible
season of" annotations fade in as LCARS search calls return.

### 1.5 Manual "Season of…" button

Every candidate card has a "Season of…" link. Clicking it:
1. Opens a search-within-LCARS overlay (reuses the LCARS `search`
   query)
2. User types or selects an existing tracked show
3. Pivots to the season-add inline flow (Batch 3.1) for that show

### 1.6 Candidate poster art

`ArrCandidate` has no poster URL. Options in priority order:
1. **Enrich in LCARS** — Sonarr/Radarr search results include
   `images[]`. Pass through a poster URL on `ArrCandidate`. Small
   LCARS change: add `posterUrl: String` to `ArrCandidate` type,
   extract from `images` in the search resolver. **Do this in Batch 1.**
2. Fallback: text-only cards with title/year/overview.

### 1.7 Nav integration

Add "Add" link/button to the shared nav bar across all pages.
Route: `add.html`.

---

## Batch 2 — Per-season status (LCARS schema change)

### 2.1 LCARS — schema + migration

**`schema.graphql`:**
- Add `status: ShowStatus` field to `Season` type (nullable — null
  means season was added before this feature, treated as same as
  show.status for rollup purposes)
- Add mutation: `setSeasonStatus(showId: ID!, seasonNumber: Int!,
  status: ShowStatus!): Season!`
- Add mutation: `clearSeasonStatus(showId: ID!, seasonNumber: Int!):
  Season!` (resets to null)

**Migration:**
- `ALTER TABLE season ADD COLUMN status TEXT DEFAULT NULL`
- Back-fill: for each show with exactly one season, set that season's
  status to the show's current status. For multi-season shows: set all
  seasons to the show's current status (can be corrected per-season
  afterwards). This seeds the rollup to match the current state.

**`show.status` becomes derived:**

```python
def compute_show_status(seasons):
    if not seasons:
        return current_show_status  # no seasons = legacy, keep as-is

    last = max(seasons, key=lambda s: s.season_number)

    # Exception: last season is PLANNED and it's not the only season
    # → show is WATCHING (watching the show, planning next season)
    if last.status == PLANNED and len(seasons) > 1:
        return WATCHING

    # All episodes of all available seasons watched → COMPLETED
    if all_episodes_watched(seasons):
        return COMPLETED

    # Otherwise: last season's status
    return last.status
```

`show.status` column stays in the DB as a **cache/materialized value**
recomputed on every `setSeasonStatus` call. Existing consumers keep
reading it without changes; the derived logic just keeps it in sync.

### 2.2 Season-number gap validation

Adding season N without seasons 1..N-1 existing:

- `setSeasonMapping(showId, seasonNumber=3, ...)` when seasons 1 and 2
  don't exist → **error**: "Cannot add Season 3 — Seasons 1, 2 do not
  exist. Add previous seasons first."
- The error is surfaced inline on the add page / show page (not
  deferred to Reviews)
- The inline flow for adding missing seasons: a prompt with a search
  for each missing season, or a bulk "add seasons 1–2 as COMPLETED"
  shortcut

### 2.3 LCARS — consumer updates

With `show.status` as a derived/cached column, most consumers need no
changes — they read `show.status` which is kept in sync. But:

- **`setStatus(showId, status)`**: repurpose as "set ALL seasons to
  this status" fanout (for quick bulk changes), or deprecate. Keep for
  backward compat with TUI client.
- **Completion guard** (`confirmed: Boolean` on `setStatus`): becomes
  per-season — `setSeasonStatus(COMPLETED)` on a season with unaired
  episodes requires `confirmed: true`
- **AniList push**: already season-scoped for score; status push
  should use `season.status` directly (clean fit)
- **Calendar/backlog queries**: read `show.status` (the cached
  derived value) — no changes needed for filtering. Badge colour in
  the web client should use season status when available for more
  precision.

### 2.4 Web — season status picker on show page

The show detail page already displays seasons. Add:
- A status chip row per season (same component as the show-level
  status picker)
- Show-level status shown as derived (non-editable, computed label)
- Click a season chip to set its status via `setSeasonStatus`
- Visual: season status chips are the primary interaction; show
  status is a summary badge

### 2.5 Web — season status in calendar/backlog

- Calendar episode cards: use `episode.seasonEntity.status ??
  episode.show.status` for the status colour
- Backlog: include episodes from seasons with `status = WATCHING`
  regardless of show-level status
- Add `status` to the `seasonEntity` fragment in all episode queries

---

## Batch 3 — Season-2 resolution (inline at add time)

All validation and resolution happens on the add page itself, not
deferred to Reviews.

### 3.0 The two sequel shapes

**Same TVDB series (most common for anime):** TVDB groups all seasons
under one series ID. Sonarr already tracks it. `addShowWithArr` will
**reject** with `"already tracked (show <id>)"` because the tvdbId
matches. This is a new season, not a new show.

**Different TVDB series (rare — spin-off, reboot):** Sonarr sees it as
a separate series. `addShowWithArr` succeeds, creates a new LCARS
show. AniList relations (via metadata fetch) auto-populate
`show_relation` and, with the Q5 enhancement, auto-add the season as
PLANNED.

### 3.1 Add page — "already tracked" inline resolution

When `addShowWithArr` rejects with "already tracked (show s-xxxxxx)":

1. Parse the show ID from the error
2. Fetch the show via `fetchShow(showId)`
3. The candidate card **transforms into a resolution card**:
   - Shows the existing show's poster, title, current status
   - Lists its known seasons with their status and AniList/MAL mappings
   - Header: "This show is already tracked — add a new season?"

4. **Season form** (inline on the card):
   - Season number: pre-filled with `(highest existing season + 1)`,
     editable
   - AniList ID: text input
   - MAL ID: text input (optional)
   - Status picker: five chips, default PLANNED

5. **"Add Season" button** executes in sequence:
   - `setSeasonMapping(showId, seasonNumber, anilistId, malId)`
   - `reconcileSeasonMapping(showId, seasonNumber)` — auto-validate
     against Fribb immediately
   - `setSeasonStatus(showId, seasonNumber, chosenStatus)` (Batch 2)

6. **Reconciliation result** shown immediately:
   - ✅ "Fribb confirms AniList mapping" — done
   - ⚠️ "Fribb suggests a different AniList ID: XXXXX" — two buttons:
     **Keep mine** / **Use Fribb's**. Keeping theirs sets
     `manualOverride`. Using Fribb's calls `setSeasonMapping` again.
   - ℹ️ "Fribb has no data for this season yet" — expected for new
     announcements, informational only
   - ❌ "Season already exists" — shows existing mapping, offers to
     update it

7. After resolution, the card shows the updated season list with the
   new season highlighted.

### 3.2 Auto-detection from search results (Q3b integration)

The passive matching from 1.4 feeds directly into 3.1:
- "Possible season of: Show Title" banner on a candidate card
- Clicking confirm pre-fills the resolution card with the matched
  show
- The user still confirms season number and AniList ID

The "Season of…" manual button (1.5) also pivots into 3.1.

### 3.3 Show page — season management

For users navigating directly to a show page (not via search):

- **"Add Season" button** in the seasons section
- Inline form: season number (auto-incremented), AniList ID, MAL ID
- Same flow as 3.1 steps 5–7 (setSeasonMapping → reconcile → status)
- **"Reconcile" button** per existing season → re-runs
  `reconcileSeasonMapping`, shows result inline
- Per-season AniList/MAL ID display with inline edit
- Visual indicator for pending reconciliation mismatches

### 3.4 Season mapping correction (inline)

When reconciliation flags a mismatch, the user resolves it **on the
same page**:

- The season row shows the conflict: "LCARS: AniList ID 12345,
  Fribb suggests: 67890"
- **Keep mine** (sets `manualOverride`) / **Use Fribb's** (calls
  `setSeasonMapping` with Fribb's value)
- If a `PendingReview` was created server-side by
  `reconcileSeasonMapping`, the inline resolution also calls
  `resolvePendingReview` to clear it

For reviews filed by automated ops runs (not triggered by user
action), the Reviews page remains the catch-all.

### 3.5 LCARS — `showByExternalId` query (optional, improves UX)

A query to detect already-tracked candidates **before** calling
`addShowWithArr`:

```graphql
showByExternalId(service: String!, externalId: String!): Show
```

Exposes the existing `find_existing_show` logic (shows.py:59) via
GraphQL. Allows the add page to mark candidates as "already tracked"
immediately on search results, without waiting for an error.

Not strictly required — error handling from `addShowWithArr` works —
but makes the UX feel intentional rather than reactive.

---

## Batch 4 — Auto-season from AniList relations (Q5)

### 4.1 LCARS — relation type on `show_relation`

`show_relation` currently has no `relation_type` column. AniList
provides `relationType` (SEQUEL, PREQUEL, SIDE_STORY, ALTERNATIVE,
CHARACTER, SUMMARY, etc.) in the relation data. Changes:

- `ALTER TABLE show_relation ADD COLUMN relation_type TEXT`
- Update `_link_relation` in metadata.py to store `relationType`
  from the AniList response
- Expose on `Show.relatedShows` (or a new `ShowRelation` edge type
  carrying the relation type)

### 4.2 LCARS — auto-add sequel seasons

In `_link_relation` (metadata.py), after creating/finding the
relation:

- If `relationType == "SEQUEL"` and the **current show** (being
  fetched) is already tracked:
  - Check if this sequel's AniList ID matches any existing season
    row on this show
  - If not: auto-create a season row with the next season number,
    `anilistId` from the relation, `status = PLANNED`
  - If the show's (derived) status is DROPPED: **skip** (don't auto-
    add planned seasons to a dropped show)
- Fires during `refreshShowMetadata` / ops daily pass — naturally
  catches new announcements as AniList updates

### 4.3 Web — auto-added seasons visible on add page

When searching for a show that was auto-added as a PLANNED season:

- The candidate's tvdbId matches the existing tracked show → triggers
  the "already tracked" resolution flow (3.1)
- The existing show's season list now includes the auto-added PLANNED
  season
- The user can confirm, adjust the season number, or update the status

---

## Batch 5 — Polish & future

### 5.1 Candidate card posters (if not done in Batch 1)

Add `posterUrl: String` to `ArrCandidate` in LCARS schema — pass
through from Sonarr/Radarr's `images[]` in the search response.

### 5.2 Browse untracked

Second tab on the add page: "Untracked in Sonarr/Radarr" — surfaces
`untrackedShowFindings` (B.11e) with the same card + status picker
pattern. Uses `addShow` (not `addShowWithArr`) since these are already
in Sonarr/Radarr.

### 5.3 Manual show relation management (if needed)

```graphql
addShowRelation(showId: ID!, relatedShowId: ID!, relationType: String): Show!
removeShowRelation(showId: ID!, relatedShowId: ID!): Boolean!
```

Low priority — AniList auto-detection covers the common case, and
Batch 4.2 handles sequel auto-add.

---

## Summary — dependency graph

```
Batch 1 (web + small LCARS changes, ships first)
  ├─ 1.1  api.js wrappers
  ├─ 1.2  LCARS: monitored param on AddShowWithArrInput
  ├─ 1.3  add.html — page, cards, status picker
  ├─ 1.4  auto-detect existing shows (title matching)
  ├─ 1.5  manual "Season of…" button
  ├─ 1.6  candidate poster art (LCARS ArrCandidate enrichment)
  └─ 1.7  nav link

Batch 2 (LCARS schema change, blocks on season status)
  ├─ 2.1  schema + migration (season.status)
  ├─ 2.2  season-number gap validation
  ├─ 2.3  consumer updates (setStatus fanout, AniList push)
  ├─ 2.4  web: show page season status picker
  └─ 2.5  web: calendar/backlog season status inheritance

Batch 3 (web, depends on Batch 2)
  ├─ 3.1  add page "already tracked" inline resolution
  ├─ 3.2  auto-detection from search results → inline resolution
  ├─ 3.3  show page season management + add season
  ├─ 3.4  inline season mapping correction
  └─ 3.5  LCARS: showByExternalId query (optional)

Batch 4 (LCARS, independent of Batch 3)
  ├─ 4.1  LCARS: relation_type on show_relation
  ├─ 4.2  LCARS: auto-add sequel seasons as PLANNED
  └─ 4.3  web: auto-added seasons visible in add flow

Batch 5 (polish)
  ├─ 5.1  candidate card posters (if not in B1)
  ├─ 5.2  browse untracked tab
  └─ 5.3  manual show relation mutations
```

---

## Further questions — none blocking

All five step-0 questions are resolved. The remaining open items are
implementation details that can be decided at build time:

- **Gap validation UX** (2.2): "add seasons 1–2 as COMPLETED" bulk
  shortcut vs one-at-a-time — shape depends on how common the case is
- **Rollup edge cases** (2.1): show with S1=COMPLETED, S2=PAUSED →
  show=PAUSED (last season's status). S1=WATCHING, S2=PLANNED →
  show=WATCHING (exception rule). S1=DROPPED, S2=PLANNED →
  show=PLANNED (last season's status, no exception since S1 isn't
  being watched). These can be validated with real data once the
  schema is in.
- **`_link_relation` AniList relation type** (4.1): which relation
  types beyond SEQUEL should trigger auto-add? Probably none — PREQUEL
  would already be tracked, SIDE_STORY/ALTERNATIVE are not seasons.
