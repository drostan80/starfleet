# Kickoff prompt — paste this to start building Starfleet

Use this as the first message in a new Claude Code session, working
directory `~/repos/starfleet`. It's written to be self-contained — a
fresh session should be able to act on it without needing anything
re-explained.

---

You're picking up **Starfleet** — a self-hosted, single-user personal
database for tracking anime/TV/movies, replacing Trakt, sitting
alongside AniList/MAL, eventually superseding an existing tool called
`aniq`. The full design was worked out over 46 rounds of discussion in
a separate session and is completely finished — **read, don't
re-derive**:

1. **Read `SCOPE.md` in this directory first, in full.** This is the
   authoritative, current-state design document — data model, API
   shape, functional behavior, phased roadmap, naming, and the full
   implementation stack (language, database, hosting). Every design
   decision in this project has already been made and is recorded
   there. Don't re-litigate anything in it; if something seems
   off or you find a real gap, flag it and ask rather than silently
   deciding differently.
2. **Then read `BUILD_PLAN.md` in this directory.** This is the
   ordered, step-by-step build sequence — every step cross-referenced
   to the `SCOPE.md` section it implements. Work through it top to
   bottom, checking off steps (`- [x]`) as they're actually completed.
   Don't reorder unless a step explicitly says it can run in parallel.
3. **Only if you need deep rationale for a specific decision** ("why
   was it done this way, not some other way") — the full round-by-
   round discussion log is at
   `/home/drostan/.claude/plans/read-existing-md-until-scalable-bachman.md`.
   Treat it as an optional reference, not required reading — `SCOPE.md`
   already contains everything needed to build correctly.

## Hard constraints — non-negotiable, stated explicitly because getting these wrong is expensive to undo

- **`~/repos/aniq` is never modified, at any point, for any reason.**
  Read-only reference only. This has been the rule for this entire
  project since before any of it existed and does not change once
  building starts. If you think aniq needs a change, the answer is:
  it doesn't — fork/port to Data instead (see `SCOPE.md` §4.0).
- **Data lives in its own sibling repo, `~/repos/data`**, created by
  forking aniq (`BUILD_PLAN.md` step 0.3 — the confirmed first real
  build action, before anything else). Not a subdirectory of
  `starfleet`. Freely modifiable once it exists.
- **`~/repos/Chabrol`** (note the old placeholder name) is the
  pre-project discussion repo — left untouched as a historical record.
  Nothing to do with it; don't reference it as a working directory.
- **This is now build mode.** The design phase (which ran under a
  strict "discussion only, do not build anything" constraint) is
  over — that constraint doesn't carry forward here. You're authorized
  to write real code, create files, install dependencies, run
  migrations, etc., following `BUILD_PLAN.md`.

## Working style expected on this project

- Match `SCOPE.md`'s own level of precision — it was written
  deliberately, section by section, with real thought behind naming
  and structure. Code should reflect that same care, not looser
  conventions.
- Cross-reference `SCOPE.md` sections in commit messages / code
  comments where it clarifies *why* something is shaped the way it is
  (e.g. "two independent booleans, not one enum — see §5.2, real
  library data showed dual-source availability is common").
  `BUILD_PLAN.md` already does this at the plan level; carry it into
  the actual implementation where it helps future-you.
- If a `BUILD_PLAN.md` step turns out to need a real design decision
  `SCOPE.md` doesn't answer, stop and surface it rather than guessing
  — update `SCOPE.md` once resolved, the same way the whole design
  phase worked.
- Language/stack per `SCOPE.md` §11: Python first, schema-first
  GraphQL (Ariadne-style — hand-write the SDL, don't generate it from
  code), SQLite + Alembic, Docker. A Rust rewrite is a deliberate
  long-term plan, not a near-term concern — don't let it influence
  early implementation choices beyond "keep the schema/API contract
  clean, since that's what makes the eventual swap cheap" (§11.1).

## First concrete action

Start at `BUILD_PLAN.md`'s **Phase 0**. Step 0.1 (creating this
directory) is already done — you're in it. Continue from **0.2**.
