"""Airdate source precedence — the single source of truth for whether
an automatic source's candidate date is allowed to overwrite what's
already stored.

Redesigned 2026-09-22, replacing the old fixed hierarchy (manual >
syoboi > animeschedule > anilist > sonarr > anidb > tvmaze). That
hierarchy meant a higher-ranked source's date always won, even when it
was simply tracking a *different, later* real broadcast than a
lower-ranked source's already-correct, already-aired date (confirmed
live: "The World Is Dancing" episode 13 — Syoboi's own tracked TV
slot airs Thursdays, but the file that actually got grabbed and
watched came from a Monday release Syoboi's feed doesn't cover at
all; AniDB had the real date, but AniDB ranks below Syoboi, so nothing
could ever correct it).

Two rules now, in order:

1. `manual` is absolute — nothing automatic ever overwrites it.
2. Same source re-asserting a *different* value for something it
   already holds: always apply, regardless of whether the new value is
   earlier or later. This is the source correcting/rescheduling its
   own previously-tracked slot (Syoboi's own broadcast time moving,
   AniList's `airingSchedule` changing) — a real reschedule (a sports
   pre-emption pushing the same slot back a week) can legitimately move
   *later*, and the source that owns that slot is the one authority on
   whether it moved.
3. A *different* source disagreeing: prefer whichever real date is
   *earlier*. An earlier confirmed broadcast is the one that actually
   happened (and is what gets grabbed and watched); a later date from
   an unrelated source is almost always a separate/regional broadcast,
   never a correction, and must never be treated as an "update" that
   pushes an episode's date back into the future. This is exactly what
   prevents the failure mode above: only the *same* source moving its
   *own* value can move a date later — a different source proposing a
   later date is a competing slot, not new information, and loses.

One exception to rule 3: `sonarr`. Sonarr's own raw date is usually a
bare TVDB placeholder, not a confirmed broadcast — it isn't "a real
competing slot" the way Syoboi/AniDB/AniList/animeschedule are to each
other, it's closer to "no real data yet." Any of those curated sources
may correct a `sonarr`-sourced date in *either* direction, same as
overwriting a NULL. (Confirmed by a real, pre-existing, deliberately
preserved behavior: AniList correcting an obviously-wrong far-past
Sonarr seed date forward — `test_anilist_air_date_reconciliation_
corrects_a_sonarr_seeded_date`.)
"""

# Sources no automatic writer may ever overwrite.
PROTECTED_SOURCES: frozenset[str] = frozenset({"manual"})

# A source whose own raw date is treated as "no real data yet" rather
# than a genuine competing broadcast — any different source may replace
# it in either direction. See module docstring's exception to rule 3.
_WEAK_SOURCES: frozenset[str] = frozenset({"sonarr"})


def should_apply(
    candidate_source: str,
    candidate_date: str | None,
    existing_source: str | None,
    existing_date: str | None,
) -> bool:
    """Should `candidate_source`'s `candidate_date` overwrite what's
    currently stored (`existing_source`/`existing_date`)?

    Every automatic writer should call this before overwriting an
    episode's air date. Dates are ISO strings — plain string comparison
    is correct as long as both sides are UTC ISO 8601 (true everywhere
    this is called)."""
    if existing_source is None or existing_date is None:
        return True  # nothing there yet — any real value beats nothing
    if existing_source in PROTECTED_SOURCES:
        return False
    if candidate_date is None:
        return False
    if candidate_source == existing_source:
        return candidate_date != existing_date
    if existing_source in _WEAK_SOURCES:
        return candidate_date != existing_date
    return candidate_date < existing_date
