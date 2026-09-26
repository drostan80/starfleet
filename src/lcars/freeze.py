"""Automation freeze (user order, 2026-09-26).

Until the data is repaired, LCARS must not create anything or change any
status on its own. While frozen (the default):

- show metadata fetch (`metadata.fetch_and_populate`, used by addShow and
  refreshShowMetadata) does nothing — it creates relation stubs and
  season rows;
- a show-level status change writes only the show's own status and its
  status_change row: no season fanout, no AniList/MAL push, no episode
  auto-mark or season completion, no Sonarr/Radarr monitor change.

Scheduled automation lives in the ops container, which is stopped
separately. Lift with LCARS_AUTOMATION_FROZEN=0 — only on the user's word.
"""

import os


def frozen() -> bool:
    return os.environ.get("LCARS_AUTOMATION_FROZEN", "1") != "0"
