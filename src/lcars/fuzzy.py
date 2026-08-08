"""Fuzzy title matching — SCOPE.md §5.4, BUILD_PLAN.md A.7.

§5.4's own text already settles the algorithm choice ("fuzzy title
search... above a similarity threshold, same difflib-style scoring
aniq's own fuzzy matching already uses, no new dependency") — this is
a close port of aniq's real, working `best_match()`/`normalize_title()`
(~/repos/aniq/src/aniq/notes.py), generalized from "match a show
against aninote vault filenames" to "match a show's title variants
against any caller-supplied list of candidate titles". The
exact-normalized-match-first, then-threshold-gated-fuzzy-fallback
shape, and the "return None rather than guess" philosophy, are both
preserved verbatim — aniq's own docstring there states the same
reasoning §5.4 gives for this table: a false positive is worse than a
missed match. `expected_season`'s note-vault-specific season-suffix
disambiguation is dropped — nothing in §5.4 describes season-aware
service-presence matching (that specific problem is `season`/§5.5's,
solved separately by the Fribb dataset, not this general-purpose
matcher).
"""

import re
from difflib import SequenceMatcher

# Starting point, carried over from aniq's own tuned value (notes.py) — that
# number was tuned against a specific personal Obsidian vault's filenames,
# not against any real service catalog, so it's a reasonable default to
# start from rather than a validated-for-this-domain constant. Service
# catalog titles (Sonarr/Radarr/AniList/MAL) are official show names, likely
# cleaner than personal vault filenames — revisit once this runs against
# real catalog data (Phase B).
MATCH_THRESHOLD = 0.72

_NON_WORD = re.compile(r"[^a-z0-9 ]+")


def normalize_title(text: str) -> str:
    """Lowercase, underscores/hyphens -> spaces, strip anything else
    that isn't alphanumeric-or-space, collapse whitespace. Verbatim
    port of aniq's own normalize_title()."""
    text = text.lower().replace("_", " ").replace("-", " ")
    text = _NON_WORD.sub("", text)
    return " ".join(text.split())


def best_match(
    show_titles: list[str], candidate_titles: list[str], threshold: float = MATCH_THRESHOLD
) -> str | None:
    """The candidate_titles entry (original casing, not normalized)
    that best matches any of show_titles. Tries an exact normalized
    match first; falls back to fuzzy (stdlib difflib.SequenceMatcher)
    only if nothing matches exactly, and only above `threshold` —
    returns None rather than guess. Verbatim algorithm shape from
    aniq's own best_match(), season-disambiguation dropped (not
    applicable here, see module docstring)."""
    if not candidate_titles or not show_titles:
        return None
    normalized_candidates = {c: normalize_title(c) for c in candidate_titles if c}
    normalized_show_titles = [normalize_title(t) for t in show_titles if t]

    for show_title in normalized_show_titles:
        for candidate, normalized in normalized_candidates.items():
            if normalized == show_title:
                return candidate

    best_candidate: str | None = None
    best_ratio = 0.0
    for show_title in normalized_show_titles:
        for candidate, normalized in normalized_candidates.items():
            ratio = SequenceMatcher(None, show_title, normalized).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_candidate = candidate
    if best_ratio >= threshold:
        return best_candidate
    return None
