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
            await run_forever(client, cfg.poll_interval_seconds, cfg.monthly_poll_interval_seconds)

    asyncio.run(_main())


def _cmd_backfill_availability(args: argparse.Namespace) -> None:
    """§5.2/§6.7, B.3 — the manual, one-time trigger for
    backfillFileAvailability: never called by `ops run`'s own loop,
    only by hand, at a moment of the operator's own choosing, because
    it blocks LCARS's single request-handling thread for real
    seconds-to-minutes while it walks each configured service's entire
    history (SCOPE.md §5.2's "Resolved 2026-08-09 (B.3)" note)."""
    logging.basicConfig(level=logging.INFO)
    cfg = config.load_config()
    _require_bearer_token(cfg)
    print(
        "Backfilling file availability from Sonarr's/Radarr's full history — this "
        "will block LCARS's other requests for the duration (real seconds to minutes "
        "on a large library). Run this at a quiet moment, not while anyone else is using it."
    )

    async def _main() -> None:
        async with LcarsClient(cfg.lcars_url, cfg.lcars_bearer_token) as client:
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
    an audit is a human reading what it found, not just a count."""
    logging.basicConfig(level=logging.INFO)
    cfg = config.load_config()
    _require_bearer_token(cfg)
    print(
        "Auditing local files against Sonarr's/Radarr's current state — this will "
        "block LCARS's other requests for the duration. Run this at a quiet moment, "
        "not while anyone else is using it."
    )

    async def _main() -> None:
        async with LcarsClient(cfg.lcars_url, cfg.lcars_bearer_token) as client:
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
