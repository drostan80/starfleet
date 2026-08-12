# The Starfleet Database, Explained

This is a plain-English tour of LCARS's database — what's actually
built and filled today, not a plan or a wishlist. It's meant to be
**your** document: edit it, mark things up, cross things out, write
"why do we even have this?" next to a table you don't understand. When
you want something reworked, we'll use this as the starting point.

It's a companion to `SCOPE.md`/`BUILD_PLAN.md`, not a replacement —
those two explain *why* every decision was made and *when* it was
built, written for the ongoing build log. This one just answers "what
is actually in here, in plain terms" — no build history, no decision
narrative, just the shape of the data as it stands.

---

## 1. The mental model — you already understand this

You had it right: this is basically a stack of spreadsheets.

- A **table** is one spreadsheet tab. `show` is a tab, `episode` is a
  tab, and so on — 28 tabs in total, listed below.
- A **column** is one spreadsheet column — `title_romaji`, `status`,
  `score`. Every row in that tab has a value (or a blank) in that
  column.
- A **row** is one record — one specific show, one specific episode,
  one specific "I watched this" entry.
- An **ID** is that row's own name tag — a short code like `s-a3f9k2`
  (a **s**how) or `e-x82j1q` (an **e**pisode). The letter at the front
  tells you which tab it belongs to, listed in the [ID prefix
  cheat-sheet](#appendix-a--id-prefix-cheat-sheet) at the bottom.
- A **link between tables** (the database word is "foreign key") is
  exactly a VLOOKUP: the `episode` tab has a column called `show_id`
  that just holds the ID of a row over in the `show` tab. That's the
  entire mechanism — nothing more exotic than one column pointing at
  another sheet's ID column.
- A few tabs exist **only to connect two other tabs together** — e.g.
  `show_tag` has no columns of its own beyond "this show" + "this
  tag." Think of it as a sheet with two columns, both of them
  VLOOKUPs, used to say "these two rows are related." These are called
  **join tables** below.

One more term worth knowing because it comes up a lot: a column marked
**"choice"** below is one of a small, fixed list of allowed values —
like a dropdown in a spreadsheet, not free typing. `status` on a show
is a choice: `watching`, `planned`, `paused`, `completed`, or
`dropped` — nothing else is allowed in.

That's the whole toolkit. Everything below is just applying it 28
times.

---

## 2. The big picture, before the detail

Most of what matters hangs off one table: `show`. Here's the shape of
the core, simplified (the full picture has more tabs than this — see
the table of contents below):

```mermaid
erDiagram
    SHOW ||--o{ SEASON : "has"
    SHOW ||--o{ EPISODE : "has"
    SHOW ||--o{ WATCH_EVENT : "\"I watched this\" log"
    SHOW ||--o{ SHOW_EXTERNAL_ID : "linked to (AniList, TVDB, ...)"
    SHOW ||--o{ SHOW_SERVICE_PRESENCE : "found on (Sonarr, AniList, ...)"
    SHOW }o--o{ PERSON : "cast, via show_person"
    SHOW }o--o{ STUDIO : "made by, via show_studio"
    SHOW }o--o{ TAG : "tagged, via show_tag"
    SHOW }o--o{ FRANCHISE : "belongs to, via franchise_member"
    SHOW }o--o{ SHOW : "related to, via show_relation"
    EPISODE }o--o{ WATCH_EVENT : "watched via"
```

Everything else in the database is either a detail hanging off one of
these boxes (an episode's air-date history, a show's score history), a
join table making one of the many-to-many lines above possible, or a
small operational table LCARS uses to keep itself running.

---

## Table of contents

1. [The core: shows, episodes, seasons, watching](#3-the-core-shows-episodes-seasons-watching)
2. [Linking to the outside world](#4-linking-to-the-outside-world)
3. [People and studios](#5-people-and-studios)
4. [Franchises and related shows](#6-franchises-and-related-shows)
5. [Tags](#7-tags)
6. [Saved filter presets](#8-saved-filter-presets)
7. [History — who changed what, and when](#9-history--who-changed-what-and-when)
8. [The review queue](#10-the-review-queue)
9. [Cross-service duplicate merging](#11-cross-service-duplicate-merging)
10. [Ongoing discovery](#12-ongoing-discovery)
11. [Internal plumbing](#13-internal-plumbing-not-really-your-data)
12. [Appendix A — ID prefix cheat-sheet](#appendix-a--id-prefix-cheat-sheet)
13. [Appendix B — column-type glossary](#appendix-b--column-type-glossary)

---

## 3. The core: shows, episodes, seasons, watching

### `show`

**The center of everything.** One row per thing you're tracking — a
TV show or a movie. Every other tab either hangs directly off this
one, or hangs off something that does.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`s-`) | always | This show's name tag. |
| `media_shape` | Choice: `episodic`, `movie` | always | Is this a TV/anime series (has episodes) or a standalone movie? |
| `tracking_space` | Choice: `tv`, `anime` | always | Which "world" this belongs to — affects which services it syncs with (anime → AniList, tv → Trakt-style plain tracking). |
| `title_romaji` / `title_english` / `title_native` | Text | at least one | Up to three title spellings. |
| `primary_title` | Choice: `romaji`, `english`, `native` | always | Which of the three titles above to actually display. |
| `display_title` | *(not stored — computed on the fly from `primary_title`)* | | Shown in the API as a convenience; not a real column. |
| `status` | Choice: `watching`, `planned`, `paused`, `completed`, `dropped` | always | Your own tracking status. |
| `tracked` | Yes/No | always, defaults to Yes | Is this show still actively part of your library? (Turned off by soft-delete — see §6.11 in `SCOPE.md` — rather than the row being removed.) |
| `score` | Number (decimal) | optional | Your personal score, 0–20 in quarter-point steps (so it can convert cleanly to AniList's 0–100 and MAL's 0–10 scales). |
| `total_episodes` | Number | optional | Total episode count, if known. |
| `duration_minutes` | Number | optional | Typical runtime per episode. |
| `poster_url` / `banner_url` | Text (a web link) | optional | Artwork. |
| `genres_raw` | List of text | optional | Genres exactly as reported by the source (AniList/Sonarr) — separate from your own `tag` tab below, which is user-curated. |
| `synopsis` | Text | optional | Plot summary. |
| `available_via_radarr` | Choice: `unavailable`, `downloading`, `available` | always, defaults to unavailable | *(movies only)* Is the file present on disk, per Radarr? |
| `file_path_radarr` | Text | optional | *(movies only)* Where the file actually is, once available. |
| `available_checked_at` | Date & time | optional | When availability was last checked. |
| `available_locally` | Yes/No — **calculated automatically**, you never set this | always | Just mirrors `available_via_radarr` — kept as its own column so other tables can query it directly. |
| `hard_delete_requested_at` | Date & time | optional | *(movies only)* Set when someone starts the permanent-delete countdown (§6.11) — blank means no delete is pending. |
| `paced_cadence_days` | Number | optional | If set, this show is in "catch-up pacing" mode — a fake "next episode" date gets synthesized every this-many days instead of using the real air date, so you don't feel pressured to binge an already-finished show. |
| `paced_next_date` | Date & time — **calculated automatically** | | The actual computed next date, when pacing is on. |
| `metadata_last_refreshed_at` | Date & time | optional | Last time LCARS re-pulled this show's info from AniList/Sonarr/Radarr. |
| `created_at` / `updated_at` | Date & time | always | Standard record-keeping timestamps. |

**Links to:** nothing (it's the root) — everything else links *to* `show`.

**Good to know:** a show marked `tracking_space = anime` is expected
to always have an AniList link (see `show_external_id` below) — that's
a rule LCARS enforces in its own logic, not something the database
itself blocks if broken.

---

### `episode`

One row per episode of an episodic show. Movies don't get episode
rows at all — a movie's own watched/available state lives directly on
its `show` row above.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`e-`) | always | |
| `show_id` | Link → `show` | always | Which show this episode belongs to. |
| `season` | Number | always | Season number, exactly as Sonarr numbers it (0 = specials). |
| `episode` | Number | always | Episode number within that season. |
| `kind` | Choice: `regular`, `special`, `ova`, `bonus_movie` | always, defaults to `regular` | What kind of episode this is — Sonarr can only tell you "regular" vs "not," so `special`/`ova`/`bonus_movie` are usually set by hand. |
| `absolute_number` | Number (decimal) | optional | A single running count across all seasons (anime numbering — can be fractional, e.g. `12.1` for a recap episode). |
| `air_date_utc` | Date & time | optional | The best-known air date, after any correction (see `air_date_source`). |
| `air_date_source` | Choice: `sonarr`, `anilist`, `animeschedule`, `manual` | optional | Which source `air_date_utc` actually came from — Manual always wins if set, then animeschedule.net, then AniList, then Sonarr's own raw guess. |
| `air_date_raw_sonarr` | Date & time | optional | Sonarr's original, *uncorrected* date — kept around so a correction can be compared/debugged. |
| `available_via_sonarr` | Choice: `unavailable`, `downloading`, `available` | always, defaults to unavailable | Is the file present, per Sonarr? |
| `available_via_radarr` | Choice: `unavailable`, `downloading`, `available` | always, defaults to unavailable | Only ever meaningful for `kind = bonus_movie` episodes. |
| `file_path_sonarr` / `file_path_radarr` | Text | optional | Where the file is, once available. |
| `available_locally` | Yes/No — **calculated automatically** | always | True if either of the two availability columns above says "available." |
| `available_checked_at` | Date & time | optional | |
| `runtime_minutes` | Number | optional | Overrides the show's own typical runtime, for this one episode. |
| `state` | Choice: `unwatched`, `watched`, `skipped` | always, defaults to `unwatched` | Have you watched it? "Skipped" still counts as "done with" for completion purposes, but isn't the same as watched. |
| `created_at` / `updated_at` | Date & time | always | |

**Links to:** `show` (via `show_id`). Also, indirectly, to `season`
below — matched by `(show_id, season number)`, not a direct ID column.

**Good to know:** `(show_id, season, episode)` together must be
unique — you can't have two rows both claiming to be "Show X, Season
1, Episode 3."

---

### `season`

Not "a season of the show" in the everyday sense of a folder of
episodes — this table's real job is narrower: **for this one season,
which AniList/MAL entry does it correspond to?** It exists because
AniList treats every season of a show as a completely separate entry
with its own ID, while TVDB (which Sonarr uses) groups a whole
franchise under one series ID. One `show_id` alone can't hold two
different AniList IDs at once — hence a whole extra table, one row per
season.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`z-`) | always | |
| `show_id` | Link → `show` | always | |
| `season_number` | Number | always | Which season of the show this row is about. |
| `anilist_id` | Number | optional | This season's AniList media ID, once known. |
| `mal_id` | Number | optional | This season's MyAnimeList ID, once known. |
| `score` | Number (decimal) | optional | A score for just this season, if different from the show's overall score — falls back to the show's score if left blank. |
| `source` | Choice: `fribb`, `manual`, `unmatched` | always | How the AniList/MAL match was found — `fribb` means an automated dataset lookup, `manual` means a human confirmed it, `unmatched` means nothing found yet. |
| `matched` | Yes/No | always | Whether a real match currently exists. |
| `manual_override` | Yes/No | always | If Yes, the automatic matcher will never overwrite this row again — a human's answer always wins from then on. |
| `last_reconciled_at` | Date & time | optional | Last time the automatic matcher checked this row. |
| `created_at` / `updated_at` | Date & time | always | |

**Links to:** `show` (via `show_id`).

**Good to know:** `(show_id, season_number)` must be unique — one row
per season, per show.

---

### `watch_event`

The actual watch-history log — one row *every time* you mark
something watched. This is genuinely a log, not a status flag: a
rewatch adds a second row rather than overwriting the first.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`w-`) | always | |
| `show_id` | Link → `show` | always | |
| `season` / `episode` | Number | optional | Which episode this refers to — left blank for a movie's own watch event, since movies have no episode row to point at. |
| `watched_at` | Date & time | always | When you (or the system, on your behalf) watched it. |
| `platform` | Text | optional | Where you watched it, if recorded. |
| `created_at` | Date & time | always | When this row was written (usually the same moment as `watched_at`, but not guaranteed to be — e.g. a backfilled/late entry). |

**Links to:** `show`, and (for episodic shows) to the matching row in
`episode` — matched by the combination of show + season + episode
number, not a direct ID.

---

### `episode_numbering_mapping`

One row per show, recording how that show's episodes are actually
numbered — because "episode 13" can mean different things depending on
whether you're counting per-season or with one running number across
the whole series (common in anime).

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`n-`) | always | |
| `show_id` | Link → `show`, one row per show | always | |
| `scheme` | Choice: `absolute`, `season_episode` | always | Which numbering style this show actually uses. |
| `source` | Choice: `sonarr`, `anilist`, `manual`, `unmatched` | always | Where this answer came from. |
| `matched` | Yes/No | always | |
| `manual_override` | Yes/No | always | Same "human answer wins forever" rule as `season.manual_override`. |
| `created_at` / `updated_at` | Date & time | always | |

**Links to:** `show` (via `show_id`).

---

### `episode_movie_link`

A narrow fix-up table for one specific situation: sometimes a "bonus
movie" is filed as a special episode of its parent series *and* also
exists as its own standalone `show` row (because you're tracking it
separately as a movie). This table records "these two are the same
film" so availability/watched-state can line up between them.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`m-`) | always | |
| `episode_id` | Link → `episode`, one row per episode | always | The `kind = bonus_movie` episode. |
| `movie_show_id` | Link → `show` | optional | The standalone movie `show` row it's been matched to, once found. |
| `source` | Choice: `tmdb_match`, `manual`, `unmatched` | always | |
| `matched` | Yes/No | always | |
| `manual_override` | Yes/No | always | |
| `created_at` / `updated_at` | Date & time | always | |

**Links to:** `episode` and `show`.

---

## 4. Linking to the outside world

### `show_external_id`

The confirmed crosswalk: "this show, on this outside service, is this
ID." One row per (show, service) pair.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `show_id` | Link → `show` | always | *(no separate ID column — the combination of show + service **is** this row's identity, a join-table-style key.)* |
| `service` | Text (open-ended — not a fixed dropdown) | always | e.g. `tvdb`, `anilist`, `tmdb`, `imdb`, `mal`. New services don't need a database change to be added. |
| `external_id` | Text | always | The ID on that service. |
| `url` | Text | always | A direct link to that service's page for this show. |
| `created_at` | Date & time | always | |

**Links to:** `show`. One show can have several rows here — one per
service it's linked to.

---

### `show_service_presence`

Different from the table above: this one isn't "confirmed, you
manually linked it" — it's "LCARS checked, and as of the last check,
this show does or doesn't appear to be present on that service's
catalog." Purely informational, never asks you to review/confirm it.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`a-`) | always | |
| `show_id` | Link → `show` | always | |
| `service` | Text (open-ended) | always | e.g. `sonarr`, `radarr`, `anilist`, `mal`, or `local` (meaning "a file exists on disk for this"). |
| `present` | Yes/No | always | |
| `checked_at` | Date & time | always | |

**Links to:** `show`. One row per (show, service) — checking again
just updates the same row.

---

## 5. People and studios

### `person`

A cast/staff member — actors, voice actors, directors, etc. Shared
across every show they've worked on, so the same person never gets
duplicated per-show.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`p-`) | always | |
| `name` | Text | always | |
| `portrait_url` | Text | optional | |
| `external_service` / `external_id` / `external_url` | Text | optional | Where this person's own outside-source record lives (e.g. AniList's own staff ID). |
| `created_at` | Date & time | always | |

**Links to:** shows, via the join table `show_person` below.

---

### `show_person` *(join table)*

Connects a `show` to a `person`, and records what they did on that
show. A pure connector — no ID of its own.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `show_id` | Link → `show` | always | |
| `person_id` | Link → `person` | always | |
| `role_type` | Choice: `voice_actor`, `actor`, `staff` | always | |
| `character_name` | Text | optional | Which character, if a cast role. |

**Good to know:** the same person can appear more than once for the
same show (e.g. voicing two different characters), so this table
deliberately allows duplicate (show, person) pairs — there's no
uniqueness rule here.

---

### `studio`

A production company, publisher, or broadcast network — same
shared-across-shows shape as `person`.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`d-`) | always | |
| `name` | Text | always | |
| `external_service` / `external_id` / `external_url` | Text | optional | |
| `created_at` | Date & time | always | |

---

### `show_studio` *(join table)*

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `show_id` | Link → `show` | always | |
| `studio_id` | Link → `studio` | always | |
| `role_type` | Choice: `studio`, `publisher`, `network` | always | |

**Good to know:** the same studio can hold more than one role on the
same show (e.g. both animation studio and publisher) — each
combination of (show, studio, role) is its own row.

---

## 6. Franchises and related shows

### `show_relation` *(join table)*

The raw "these two shows are related" graph — sequels, spin-offs,
side stories. No extra information beyond the link itself; `franchise`
below is what actually groups related shows into an ordered
collection for display.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `show_id` | Link → `show` | always | |
| `related_show_id` | Link → `show` | always | |
| `created_at` | Date & time | always | |

---

### `franchise`

A named, curated group of related shows — e.g. "Ghost in the Shell,"
covering several separate `show` rows.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`f-`) | always | |
| `name` | Text | always | |

---

### `franchise_member` *(join table)*

Which shows belong to which franchise, and in what order.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `franchise_id` | Link → `franchise` | always | |
| `show_id` | Link → `show` | always | |
| `sort_order` | Number | always | Manual display order within the franchise. |

---

### `next_up_override`

A manual override of "what's next to watch" ordering, for one show at
a time — separate from franchise ordering above.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`v-`) | always | |
| `show_id` | Link → `show`, one row per show | always | |
| `sort_order` | Number | always | |

---

## 7. Tags

### `tag`

A user-created label — completely separate from `show.genres_raw`
(which comes from the source data, not from you).

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`t-`) | always | |
| `name` | Text, must be unique | always | |
| `created_at` | Date & time | always | |

### `show_tag` *(join table)*

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `show_id` | Link → `show` | always | |
| `tag_id` | Link → `tag` | always | |

---

## 8. Saved filter presets

### `filter_preset`

A saved search/filter setup a client (Data, Holodeck, ...) can offer
to re-run. LCARS stores it but doesn't interpret it — the filter's
actual shape is up to whichever client wrote it.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`q-`) | always | |
| `name` | Text | always | |
| `filter_json` | Text (a block of JSON) | always | The filter criteria itself — opaque to LCARS. |
| `created_at` / `updated_at` | Date & time | always | |

---

## 9. History — who changed what, and when

Four nearly-identical tables, one per kind of change worth keeping a
permanent record of. Every row is a single past change — a full
"before → after" history, not just the current value (the current
value already lives on `show`/`episode` itself).

### `status_change`, `score_change`, `tracked_change` (all about a `show`)

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`c-` / `o-` / `k-` respectively) | always | |
| `show_id` | Link → `show` | always | |
| `previous_status` / `previous_score` / `previous_tracked` | (matches the field) | optional | The value before this change — blank if this is the very first recorded value. |
| `new_status` / `new_score` / `new_tracked` | (matches the field) | always | The value after. |
| `changed_at` | Date & time | always | |
| `changed_by` | Text (open-ended — e.g. `data`, `holodeck`, `sonarr_sync`) | always | Which client or process made the change. |

### `air_date_change` (about an `episode`)

Same shape, but about an episode's air date and where that date came
from — `episode_id` instead of `show_id`, and both a previous/new date
*and* a previous/new source.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`g-`) | always | |
| `episode_id` | Link → `episode` | always | |
| `previous_air_date_utc` / `new_air_date_utc` | Date & time | new: always, previous: optional | |
| `previous_source` / `new_source` | Choice: `sonarr`, `anilist`, `animeschedule`, `manual` | new: always, previous: optional | |
| `changed_at` | Date & time | always | |
| `changed_by` | Text (open-ended) | always | |

---

## 10. The review queue

### `pending_review`

The one shared "something needs a human's eyes" mechanism, used by
every automatic-reconciliation feature in the system rather than each
one inventing its own. Nothing is ever deleted from this table, even
once resolved — it's a permanent record of every automatic decision
that needed (or got) a second opinion.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`r-`) | always | |
| `entity_type` | Text (open-ended — e.g. `show`, `episode`, `season`) | always | What *kind* of thing this review is about. |
| `entity_id` | ID of that thing | always | *Which* row, specifically. |
| `field` | Text | always | Which column/value is in question. |
| `previous_value` | Text | optional | What it was before. |
| `proposed_value_chain` | List of text | always | Every value that's been *proposed* since this entry opened — if the same field keeps getting a new automatic suggestion before anyone reviews it, each one is added here rather than overwriting the last. |
| `source` | Text (open-ended) | always | What triggered this review — e.g. `fribb`, `anilist`, `manual`. |
| `created_at` | Date & time | always | |
| `resolved_at` | Date & time | optional | Blank means still open. |
| `resolved_by_client` | Choice: `data`, `holodeck`, `captains_log` | optional | Which client resolved it. |
| `resolution_note` | Text | optional | |

**Links to:** anything, generically — `entity_type` + `entity_id`
together point at a row in whichever table `entity_type` names. This
is the one table in the whole database that doesn't use a normal,
single-target link, because it has to be able to talk about *any*
other table.

---

## 11. Cross-service duplicate merging

### `show_merge`

A record of every time two `show` rows that turned out to be the same
real show (one found via Sonarr, one found via an AniList sweep, say)
got automatically merged into one. Nothing is ever deleted here
either — the losing row is kept, just marked untracked, so a merge can
always be undone.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`y-`) | always | |
| `winner_show_id` | Link → `show` | always | The row that survived. |
| `loser_show_id` | Link → `show` | always | The row that got folded into the winner (kept, but marked not-tracked). |
| `matched_on` | Text | always | How the two were matched (e.g. title similarity). |
| `manifest` | Text (a block of JSON) | always | Exactly what moved from loser to winner, and what was left behind on a genuine conflict — this is what lets a merge be reversed precisely. |
| `merged_at` | Date & time | always | |
| `reversed_at` | Date & time | optional | Blank means the merge still stands. |
| `reversed_by_client` | Text | optional | |

---

## 12. Ongoing discovery

### `untracked_show_finding`

A running, self-updating list of things that exist on Sonarr/Radarr/
AniList but aren't tracked in LCARS at all yet. Unlike `pending_review`
above, nothing here needs manually resolving — a row simply
disappears on its own once the show either gets added for real, or
genuinely stops existing on its source. This table is not backed up
by export/import, since it rebuilds itself automatically the next time
it runs.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `id` | ID (`u-`) | always | |
| `service` | Choice: `sonarr`, `radarr`, `anilist` | always | |
| `external_id` | Text | always | |
| `title` | Text | always | |
| `path` | Text | optional | |
| `tracking_space` | Choice: `tv`, `anime` | always | |
| `media_shape` | Choice: `episodic`, `movie` | always | |
| `first_seen_at` / `last_seen_at` | Date & time | always | |
| `created_at` / `updated_at` | Date & time | always | |

---

## 13. Internal plumbing (not really "your data")

These two exist purely so LCARS can keep track of its own polling
work — there's nothing here you'd ever want to look through by hand,
and neither is included in the export/backup (both rebuild themselves
automatically the next time they run).

### `availability_poll_checkpoint`

One row per service (`sonarr`, `radarr`) — a bookmark of "how far
through that service's history have we already processed," so
checking again only looks at what's new.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `service` | Text — this **is** the row's identity, no separate ID | always | |
| `last_event_at` | Date & time | always | |
| `updated_at` | Date & time | always | |

### `service_health`

One row per outside service LCARS talks to — a running "is it
reachable right now" status Data's status bar reads.

| Column | Type | Required? | Meaning |
|---|---|---|---|
| `service` | Choice: `sonarr`, `radarr`, `anilist`, `animeschedule`, `mal` — this **is** the row's identity | always | |
| `status` | Choice: `ok`, `unreachable` | always | |
| `last_checked_at` | Date & time | always | |
| `last_success_at` | Date & time | optional | Doesn't get cleared on a single failure — keeps showing the last time contact genuinely worked, through an ongoing outage. |
| `last_error_message` | Text | optional | Cleared once things recover. |
| `updated_at` | Date & time | always | |

---

## Appendix A — ID prefix cheat-sheet

Every row's ID starts with a letter telling you which table it's
from, followed by a dash and 6 random characters (e.g. `s-a3f9k2`).
Join tables (the "just connects two other tables" ones) don't get
their own ID at all — they're always reached through the tables they
connect.

| Prefix | Table |
|---|---|
| `s-` | `show` |
| `e-` | `episode` |
| `z-` | `season` |
| `w-` | `watch_event` |
| `n-` | `episode_numbering_mapping` |
| `m-` | `episode_movie_link` |
| `a-` | `show_service_presence` |
| `p-` | `person` |
| `d-` | `studio` |
| `f-` | `franchise` |
| `v-` | `next_up_override` |
| `t-` | `tag` |
| `q-` | `filter_preset` |
| `c-` | `status_change` |
| `o-` | `score_change` |
| `g-` | `air_date_change` |
| `k-` | `tracked_change` |
| `r-` | `pending_review` |
| `y-` | `show_merge` |
| `u-` | `untracked_show_finding` |

*(No prefix / no ID column at all: `show_external_id`, `show_person`,
`show_studio`, `show_relation`, `franchise_member`, `show_tag`,
`availability_poll_checkpoint`, `service_health` — every one of these
is either a join table or a one-row-per-name internal table, always
reached through something else, never fetched by its own ID.)*

---

## Appendix B — column-type glossary

| Label used above | What it really is | Example |
|---|---|---|
| Text | Free text, any length | `"Frieren: Beyond Journey's End"` |
| Number | A whole number | `12` |
| Number (decimal) | A number that can have a fractional part | `14.5` |
| Yes/No | A true/false flag | Yes |
| Choice: `a`, `b`, `c` | Must be exactly one of a fixed list — like a spreadsheet dropdown | `watching` |
| Date & time | A specific moment, always stored in UTC regardless of your local timezone | `2026-08-12T14:30:00Z` |
| List of text | Several text values in one cell (stored as JSON underneath) | `["Adventure", "Drama"]` |
| Link → `table` | This column just holds another table's ID — the VLOOKUP mechanism | `s-a3f9k2` |
| Calculated automatically | The database fills this in itself from other columns — you never set it directly, and can't override it | |

---

## Where this document stops, on purpose

This covers every table and every column as they exist right now. It
does **not** cover: indexes, exact validation rules beyond the
"choice" lists, or anything about the GraphQL API surface built on top
of this (queries/mutations) — those live in `SCOPE.md`/
`BUILD_PLAN.md`/`schema.graphql` if you ever need that level of
detail. This document's only job is: what data exists, and how it
connects.
