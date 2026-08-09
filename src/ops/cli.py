"""argparse entry point — mirrors lcars/cli.py's own convention
(itself matching aniq's, CLAUDE.md)."""

import argparse
import asyncio
import logging

from ops import config
from ops.lcars_client import LcarsClient
from ops.scheduler import run_forever


def _cmd_run(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO)
    cfg = config.load_config()
    if not cfg.lcars_bearer_token:
        raise SystemExit(
            "lcars_bearer_token must be set first — in ops.ini's [ops] section, or "
            "OPS_LCARS_BEARER_TOKEN/OPS_LCARS_BEARER_TOKEN_FILE (SCOPE.md §11.2/§8)."
        )

    async def _main() -> None:
        async with LcarsClient(cfg.lcars_url, cfg.lcars_bearer_token) as client:
            await run_forever(client, cfg.poll_interval_seconds, cfg.monthly_poll_interval_seconds)

    asyncio.run(_main())


def main() -> None:
    parser = argparse.ArgumentParser(prog="ops", description="Starfleet's background scheduler.")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="run the polling loop (the default)")
    run.set_defaults(func=_cmd_run)

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
