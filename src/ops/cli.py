"""argparse entry point — mirrors lcars/cli.py's own convention
(itself matching aniq's, CLAUDE.md)."""

import argparse
import asyncio
import logging

from ops import config
from ops.lcars_client import LcarsClient
from ops.scheduler import run_forever


def _require_bearer_token(cfg: config.Config) -> None:
    if not cfg.lcars_bearer_token:
        raise SystemExit(
            "lcars_bearer_token must be set first — in ops.ini's [ops] section, or "
            "OPS_LCARS_BEARER_TOKEN/OPS_LCARS_BEARER_TOKEN_FILE (SCOPE.md §11.2/§8)."
        )


def _cmd_run(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = config.load_config()
    _require_bearer_token(cfg)

    async def _main() -> None:
        async with LcarsClient(cfg.lcars_url, cfg.lcars_bearer_token) as client:
            await run_forever(
                client,
                cfg.poll_interval_seconds,
                cfg.monthly_poll_interval_seconds,
                cfg.anilist_activity_poll_interval_seconds,
                cfg.mal_reconcile_poll_interval_seconds,
                cfg.memory_alpha_poll_interval_seconds,
            )

    asyncio.run(_main())


def _cmd_backfill_availability(args: argparse.Namespace) -> None:
    """§5.2/§6.7, B.3 — the manual, one-time trigger for
    backfillFileAvailability: never called by `ops run`'s own loop,
    only by hand, at a moment of the operator's own choosing, because
    it blocks LCARS's single request-handling thread for real
    seconds-to-minutes while it walks each configured service's entire
    history (SCOPE.md §5.2's "Resolved 2026-08-09 (B.3)" note).

    Real bug, live-caught 2026-08-11 (B.11f's first live test surfaced
    it — every episode downloaded before the deployed instance's first
    ever poll had never been checked at all, `availableCheckedAt: null`
    for 20/36 real episodes): this used the client's default 10s
    timeout despite the docstring/print statement right above both
    already promising "seconds to minutes on a large library" — this
    command had never actually been run against a real, sizeable
    library before that first live run, so the mismatch went
    unnoticed. `_cmd_backfill_shows` above already learned this same
    lesson (`timeout=3600.0`, "the long client timeout below rather
    than the default 10s every other command here uses") — this
    matches it rather than being a new decision."""
    logging.basicConfig(level=logging.INFO)
    cfg = config.load_config()
    _require_bearer_token(cfg)
    print(
        "Backfilling file availability from Sonarr's/Radarr's full history — this "
        "will block LCARS's other requests for the duration (real seconds to minutes "
        "on a large library). Run this at a quiet moment, not while anyone else is using it."
    )

    async def _main() -> None:
        async with LcarsClient(cfg.lcars_url, cfg.lcars_bearer_token, timeout=3600.0) as client:
            result = await client.backfill_file_availability()
            print(
                f"Done: {result['episodesUpdated']} episode(s), "
                f"{result['showsUpdated']} show(s) updated."
            )

    asyncio.run(_main())


def _cmd_audit_local_files(args: argparse.Namespace) -> None:
    """§5.2/§6.10, B.3b — the manual trigger for auditLocalFiles: never
    called by `ops run`'s own loop, only by hand, at a moment of the
    operator's own choosing. Prints both report-only finding lists
    (orphan files, untracked remote shows) in full — the whole point of
    an audit is a human reading what it found, not just a count.

    Real bug, live-caught 2026-08-20 (NEXT_UP.md): this used the
    client's default 10s timeout despite blocking LCARS's single
    request-handling thread for a real whole-library walk — a manual
    run fixing Lioness/Lanterns after a Sonarr anime->series
    root-folder move hit `httpx.ReadTimeout`/`LcarsError: Timed out
    talking to LCARS` here, even though the mutation had actually
    committed successfully server-side (confirmed after the fact via
    `available_checked_at` + cross-checking Sonarr/the filesystem
    directly) — the CLI just gave up waiting and never printed the
    summary. Same class of bug `_cmd_backfill_availability` above
    already fixed for its own command; matches its `timeout=3600.0`
    rather than being a new decision."""
    logging.basicConfig(level=logging.INFO)
    cfg = config.load_config()
    _require_bearer_token(cfg)
    print(
        "Auditing local files against Sonarr's/Radarr's current state — this will "
        "block LCARS's other requests for the duration. Run this at a quiet moment, "
        "not while anyone else is using it."
    )

    async def _main() -> None:
        async with LcarsClient(cfg.lcars_url, cfg.lcars_bearer_token, timeout=3600.0) as client:
            result = await client.audit_local_files()
            print(
                f"Done: {result['episodesCorrected']} episode(s), "
                f"{result['showsCorrected']} show(s) corrected."
            )
            if result["orphanFiles"]:
                print(f"\n{len(result['orphanFiles'])} orphan file(s) found (not attached):")
                for f in result["orphanFiles"]:
                    season_episode = (
                        f"S{f['parsedSeason']:02d}E{f['parsedEpisode']:02d}"
                        if f["parsedSeason"] is not None
                        else "season/episode unparseable"
                    )
                    print(f"  [{season_episode}] {f['path']}")
            if result["untrackedShows"]:
                print(f"\n{len(result['untrackedShows'])} untracked remote show(s) found:")
                for s in result["untrackedShows"]:
                    print(f"  [{s['service']}] {s['title']} ({s['externalId']}) — {s['path']}")

    asyncio.run(_main())


def _cmd_reconcile_arr_state(args: argparse.Namespace) -> None:
    """NEXT_UP.md follow-up (2026-09-19) — manual trigger for
    reconcileArrState, run before turning on the automatic loop the
    first time: this applies pause/resume status changes (each with a
    real AniList/MAL push) as soon as it runs, so a first look here
    before Ops's own availability loop starts calling it unattended
    every tick is worth the extra step. Unlike auditLocalFiles, this
    isn't a slow whole-filesystem walk — the default client timeout is
    fine."""
    logging.basicConfig(level=logging.INFO)
    cfg = config.load_config()
    _require_bearer_token(cfg)

    async def _main() -> None:
        async with LcarsClient(cfg.lcars_url, cfg.lcars_bearer_token) as client:
            result = await client.reconcile_arr_state()
            print(
                f"Done: {result['showsCreated']} show(s) created "
                f"({result['showsCreateFailed']} failed — see Reviews), "
                f"{result['episodesCorrected']} episode(s)/{result['showsCorrected']} "
                "show(s) corrected."
            )
            if result["pausedShowIds"]:
                ids = ", ".join(result["pausedShowIds"])
                print(f"\nPaused ({len(result['pausedShowIds'])}): {ids}")
            if result["resumedShowIds"]:
                ids = ", ".join(result["resumedShowIds"])
                print(f"\nResumed ({len(result['resumedShowIds'])}): {ids}")

    asyncio.run(_main())


def _cmd_preview_show_backfill(args: argparse.Namespace) -> None:
    """§5.1/§5.2, B.11d — dry-run, no writes: prints exactly what
    `ops backfill-shows` would create right now. Always run this
    first."""
    logging.basicConfig(level=logging.INFO)
    cfg = config.load_config()
    _require_bearer_token(cfg)

    async def _main() -> None:
        async with LcarsClient(cfg.lcars_url, cfg.lcars_bearer_token) as client:
            preview = await client.preview_show_backfill()
            if not preview:
                print("Nothing to backfill — every Sonarr/Radarr item is already tracked.")
                return
            print(f"{len(preview)} untracked show(s) would be created:")
            for item in preview:
                print(
                    f"  [{item['service']}] {item['title']} ({item['externalId']}) — "
                    f"{item['trackingSpace']}/{item['mediaShape']}"
                )

    asyncio.run(_main())


def _cmd_backfill_shows(args: argparse.Namespace) -> None:
    """§5.1/§5.2, B.11d — the real run: one addShow-equivalent per
    untracked Sonarr/Radarr item, throttled between anime-classified
    adds (real AniList calls) to stay inside AniList's rate budget —
    a genuinely large real library (verified live: 1644 items, ~1350
    anime-classified) can take multiple *hours*, not minutes, hence
    the long client timeout below rather than the default 10s every
    other command here uses. Always shows the same preview `ops
    preview-show-backfill` would, and requires an explicit typed
    confirmation before writing anything — the same "dry run first,
    show it, then write" shape auditLocalFiles' own report-only
    findings already establish, just with the write itself, so a human
    reads the list before it becomes real rows.

    Idempotent/resumable by design (show_backfill.py's own module
    docstring) — if this single HTTP call itself times out or the
    connection drops before the server finishes (a real risk at this
    scale even with a generous timeout), re-running this same command
    picks up exactly where it left off; every show already created is
    excluded from the next run's own candidate list. Not a bug to work
    around, the intended recovery path."""
    logging.basicConfig(level=logging.INFO)
    cfg = config.load_config()
    _require_bearer_token(cfg)

    async def _main() -> None:
        async with LcarsClient(cfg.lcars_url, cfg.lcars_bearer_token, timeout=3600.0) as client:
            preview = await client.preview_show_backfill()
            if not preview:
                print("Nothing to backfill — every Sonarr/Radarr item is already tracked.")
                return
            print(f"{len(preview)} untracked show(s) will be created:")
            for item in preview:
                print(
                    f"  [{item['service']}] {item['title']} ({item['externalId']}) — "
                    f"{item['trackingSpace']}/{item['mediaShape']}"
                )
            confirmed = input(f"\nType 'yes' to create these {len(preview)} show(s): ").strip()
            if confirmed != "yes":
                print("Cancelled — nothing was created.")
                return
            print(
                "Backfilling — this will block LCARS's other requests for the duration "
                "(real minutes, throttled against AniList's own rate budget for anime "
                "shows). Run this at a quiet moment, not while anyone else is using it."
            )
            result = await client.backfill_untracked_shows()
            print(f"\nDone: {len(result['created'])} show(s) created.")
            for s in result["created"]:
                print(f"  [{s['service']}] {s['title']} -> {s['showId']}")
            if result["promoted"]:
                print(
                    f"\n{len(result['promoted'])} existing untracked stub(s) promoted "
                    "in place (not new rows — a relation the AniList sweep or Sonarr "
                    "catalog also independently found):"
                )
                for s in result["promoted"]:
                    print(f"  [{s['service']}] {s['title']} -> {s['showId']}")
            if result["failed"]:
                print(f"\n{len(result['failed'])} item(s) failed:")
                for f in result["failed"]:
                    print(f"  [{f['service']}] {f['title']}: {f['error']}")

    asyncio.run(_main())


def main() -> None:
    parser = argparse.ArgumentParser(prog="ops", description="Starfleet's background scheduler.")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="run the polling loop (the default)")
    run.set_defaults(func=_cmd_run)

    backfill = subparsers.add_parser(
        "backfill-availability",
        help="one-time full Sonarr/Radarr history walk — run manually, blocks while it runs",
    )
    backfill.set_defaults(func=_cmd_backfill_availability)

    audit = subparsers.add_parser(
        "audit-local-files",
        help="reconcile + discover local files against Sonarr/Radarr — run manually",
    )
    audit.set_defaults(func=_cmd_audit_local_files)

    reconcile_arr = subparsers.add_parser(
        "reconcile-arr-state",
        help="untracked-show create + monitored<->status reconcile — also runs automatically",
    )
    reconcile_arr.set_defaults(func=_cmd_reconcile_arr_state)

    preview_backfill = subparsers.add_parser(
        "preview-show-backfill",
        help="dry-run: list untracked Sonarr/Radarr shows that would be created — run manually",
    )
    preview_backfill.set_defaults(func=_cmd_preview_show_backfill)

    backfill_shows = subparsers.add_parser(
        "backfill-shows",
        help="create every untracked Sonarr/Radarr show in LCARS — run manually, asks to confirm",
    )
    backfill_shows.set_defaults(func=_cmd_backfill_shows)

    # No subcommand at all ("ops" alone) still means "run" — same
    # by-hand default lcars/cli.py's own main() already established,
    # argparse subparsers don't support a default sub-command directly.
    args = parser.parse_args()
    if args.command is None:
        args = run.parse_args([])
        args.func = _cmd_run

    args.func(args)


if __name__ == "__main__":
    main()
