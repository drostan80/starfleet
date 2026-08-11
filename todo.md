# Bugs

- [x] trying to mark a show from yesterday that I watch today as watched and...nothing happen
  — fixed: the non-anime path only pushes to LCARS, and `_queue_lcars_watched` silently
  returned nothing whenever the show wasn't already in Data's tvdb->LCARS-id cache — same
  root cause as the Tomb Raider King AniList-duplicate bug below, same day. Now falls back to
  a live LCARS search (shared with that fix). Verified live: Ghost in the Shell S01E06's
  automatic mark-watched (a separate, already-working path) really did reach LCARS
  (`state: WATCHED`, real `watch_event` row), confirming the bug was specific to the manual
  `w` binding. `~/repos/data` commit `8b9abab`.

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
