"""Airdate source priority — the single source of truth for which
automatic source wins when two disagree.

Priority (highest first):
  manual  — user correction, never overwritten by anything automatic
  syoboi  — minute-accurate JST broadcast times from Syoboi Calendar
  animeschedule — animeschedule.net RSS, backup to Syoboi
  anilist  — day-granularity, from AniList airingSchedule
  sonarr   — Sonarr's own reported dates
  anidb    — AniDB episode airdates
  tvmaze   — TVmaze episode airdates (TV/movies)

Every automatic writer should call ``outranks(new_source, existing_source)``
before overwriting: if the existing source outranks the new one, skip.
"""

# Index = priority (lower = higher priority).
_ORDER: list[str] = [
    "manual",
    "syoboi",
    "animeschedule",
    "anilist",
    "sonarr",
    "anidb",
    "tvmaze",
]

_RANK: dict[str, int] = {src: i for i, src in enumerate(_ORDER)}

# Sources that no automatic writer should ever overwrite.
PROTECTED_SOURCES: frozenset[str] = frozenset({"manual"})


def rank(source: str | None) -> int:
    """Return the priority rank of a source (lower = higher priority).

    Unknown or NULL sources return a rank below every named source,
    so any named source outranks them.
    """
    if source is None:
        return len(_ORDER)
    return _RANK.get(source, len(_ORDER))


def outranks(new_source: str, existing_source: str | None) -> bool:
    """True if *new_source* has strictly higher priority than *existing_source*.

    Use this before overwriting: ``if outranks('syoboi', row['air_date_source']): ...``
    """
    return rank(new_source) < rank(existing_source)


def source_is_protected_from(existing_source: str | None, writer: str) -> bool:
    """True if *existing_source* should NOT be overwritten by *writer*.

    This is the gate every automatic writer should check:
    skip if the existing source outranks or equals the writer.
    """
    return rank(existing_source) <= rank(writer)
