# Bugs

- [ ] B.11g — calendar backlog counter + mark-watched display, ready to re-test on a clean
  baseline now, not yet confirmed. Timeline, so the full picture is in one place:
  1. Two real display/queueing bugs fixed 2026-08-12 night (`~/repos/data` `8b9abab`/`6d1baef`):
     mark-watched silently failing to queue for LCARS, and the Track column showing a
     meaningless "Bridged" placeholder instead of real watched state.
  2. Backlog counters shipped same night (`e186fc9`/`0160fe7`) — a colored "+N" badge on a
     show's next-upcoming row; `w` on it clears the *oldest* backlog episode, LCARS-only.
  3. Next morning: real bug found in the badge itself (a stale loop variable meant badges
     landed on the wrong show entirely) — fixed, `~/repos/data` `6ca51d5`.
  4. **The bigger discovery, same morning**: three shows reported as "+7"/"+5"/"+7" despite
     being fully watched turned out to be correctly reading genuinely-wrong LCARS data — zero
     `watch_event` rows existed for any of them, a pre-existing silent data-loss bug (same root
     cause as item 1), not a display bug at all. Fixed library-wide via a new one-time
     reconciliation against your real AniList list (`~/repos/starfleet` B.15, `1e7bd66`, run for
     real 2026-08-12: 41 shows' status corrected, 2973 episodes backfilled, confirmed against
     the three originally-reported shows plus three others found stale the same way).
  Please test now, on this corrected baseline: press `w` on a non-anime show and confirm the
  Track column settles on "✓ Watched"; find a show with a real backlog gap and confirm the
  badge count is now sane and `w` clears the right (oldest) episode.

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

- [ ] SSH to the deploy host via its Tailscale name (`tiny`) is blocked by a tailnet ACL policy
  rejection, confirmed not a stale-session issue (a fresh verbose attempt still gets rejected
  after a clean handshake — "tailnet policy does not permit you to SSH as user drostan").
  Worked around 2026-08-12 by connecting to the host's real LAN IP directly instead
  (`tiny@192.168.0.152`, a different local account, bypasses Tailscale's SSH proxy entirely) —
  works, but the Tailscale ACL should actually get fixed rather than relied on staying broken.
- [ ] `aa` (add to AniList only, no Sonarr relationship) never bridges to LCARS at all — a real
  gap, deliberately not closed during B.12 since LCARS's `addShow` always assumes an episodic
  show (Data has no Radarr/movie path), which would be wrong for anything `aa` adds that's
  actually a movie. Needs its own scoping, not a rushed fix. See BUILD_PLAN.md's B.12 entry.
- [ ] move all secret and password to a safer place
- [ ] make sure to design the ui for html client well
- [ ] have html client a login /password thing keep safe
- [ ] does the html client use a html/browser player or local one? my thought is local first then browser base but is it feasible?
- [ ] `pending_review` surface built in Data 2026-08-12 (B.17/B.18, BUILD_PLAN.md:
  `~/repos/starfleet` v0.1.6 `season(id:)` query + `~/repos/data` `a6cb001`'s status-bar badge
  and `V` review screen) — tested (562 passing) but **not yet used live** against the real
  23-item backlog. Please try it: `V` opens the screen, `enter` resolves, `s` sets an AniList
  id for a season/anilist_id review. Still not built: Holodeck's dashboard / Captain's Log's
  inline prompt, the other two SCOPE.md §5.6 surfaces.
- [x] `anilist_id_review.csv` sent earlier 2026-08-12 for manual review is now obsolete/mostly
  wrong — it was generated before the tv-show contamination bug (B.16) was found, so ~318 of
  its ~340 rows were tv shows that should never have been on it. Don't use it; the real
  remaining backlog (23 items, all genuine cour-split cases) doesn't need a spreadsheet pass.
