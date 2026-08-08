"""argparse entry point — matches aniq's own convention (CLAUDE.md,
aniq repo: "argparse (matches sonarr-cal's convention)").

`anilist-login` added 2026-08-08 (A.9) — LCARS's own AniList OAuth
bootstrap, interactive/one-time (§6.8, §8): reuses Data/aniq's existing
registered AniList app (`anilist_client_id`/`_secret`, already in
lcars.ini per config.py's own docstring) to mint LCARS's own
independent access token, same PIN-redirect flow Data's own login
already uses (no local callback server needed).
"""

import argparse

import uvicorn

from lcars import anilist_client, config


def _cmd_serve(args: argparse.Namespace) -> None:
    uvicorn.run("lcars.server:create_app", factory=True, host=args.host, port=args.port)


def _cmd_anilist_login(args: argparse.Namespace) -> None:
    cfg = config.load_config()
    if not cfg.anilist_client_id or not cfg.anilist_client_secret:
        raise SystemExit(
            "anilist_client_id and anilist_client_secret must be set first — in lcars.ini's "
            "[lcars] section, or LCARS_ANILIST_CLIENT_ID/LCARS_ANILIST_CLIENT_SECRET (§8)."
        )
    print("Open this URL, authorize, and copy the code AniList shows you:")
    print(anilist_client.authorize_url(cfg.anilist_client_id))
    code = input("Paste the code here: ").strip()
    token = anilist_client.exchange_code(cfg.anilist_client_id, cfg.anilist_client_secret, code)
    config.save_anilist_token(token)
    print("Saved to lcars.ini — LCARS can now push scores/status to AniList.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="lcars", description="Starfleet's GraphQL server.")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="run the GraphQL server (the default)")
    serve.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    serve.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    serve.set_defaults(func=_cmd_serve)

    anilist_login = subparsers.add_parser(
        "anilist-login", help="interactively authorize LCARS's own AniList push access (A.9)"
    )
    anilist_login.set_defaults(func=_cmd_anilist_login)

    # No subcommand at all ("lcars" alone, e.g. the Dockerfile's CMD) still
    # means "serve", with serve's own --host/--port defaults — argparse
    # subparsers don't support a default sub-command directly, so this is
    # handled by hand. `lcars --host/--port` directly (no "serve") isn't
    # preserved: nothing in this repo (Dockerfile, docs, tests) ever called
    # it that way, so there's no real usage to stay compatible with —
    # `lcars serve --host/--port` going forward, a normal subcommand shape.
    args = parser.parse_args()
    if args.command is None:
        args = serve.parse_args([])
        args.func = _cmd_serve

    args.func(args)


if __name__ == "__main__":
    main()
