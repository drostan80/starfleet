# Bugs

- [ ] trying to mark a show from yesterday that I watch today as watched and...nothing happen
  — two real bugs found and fixed, ready to test live, not yet confirmed:
  (1) `_queue_lcars_watched` silently no-op'd for a non-cached show (`~/repos/data` commit
  `8b9abab`) — fixed what gets queued.
  (2) the actual display gap: the Track column had no confirmed-watch-history to read at all,
  so once a mark flushed and dropped out of the local pending queue it fell back to a
  meaningless "Bridged" placeholder regardless of real watched state (`~/repos/data` commit
  `6d1baef`, 2026-08-12) — fixed what the row *displays*: `episode.state` is now pulled from
  LCARS and rendered as "✓ Watched" / "Skipped" / "Unwatched".
  Please test: press `w` on a non-anime show, wait for the flush (up to 5 min, or `R`/`b`/quit
  to force it), confirm the Track column actually settles on "✓ Watched" rather than reverting
  to something else. Stays open until you confirm this live — see BUILD_PLAN.md's B.11g entry.

- [x] somehow tomb raider king was added to anilist again — traced live: LCARS's own row for it
  still had zero anilist link either time, so `M`'s LCARS bridge silently no-op'd both times
  with no feedback, and the user pressed it a second time believing the first had failed,
  writing a real second entry to AniList itself. Fixed with a live LCARS search fallback
  (matched against the show's own tvdb id before trusting it) plus real user-facing messages
  on every failure path. `~/repos/data` commit `744c6ff`.

- [x] hitting enter to watch a show: nothing happens, resolved path not found, same as before and
  I did warn you, since the path is on the remote server data need to know how to access it
  locally, the directory is mounted so path can be mapped this is what was done for aniq
  — fixed: the remap_media_path mechanism was already ported into Data from aniq, just never
  configured. Added the missing `[paths]` section (remote_root/local_root, same values as
  aniq's own config.ini) to ~/.config/starfleet/data/config.ini. No code change needed.
  Confirmed working live.

- [ ] in list view enter does nothing, info shos but the image show only half somehow this seems to not be an issue in calendar view info

# Ideas / design

- [ ] move all secret and password to a safer place
- [ ] make sure to design the ui for html client well
- [ ] have html client a login /password thing keep safe
- [ ] does the html client use a html/browser player or local one? my thought is local first then browser base but is it feasible?
