"""argparse entry point — matches aniq's own convention (CLAUDE.md,
aniq repo: "argparse (matches sonarr-cal's convention)")."""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="lcars", description="Starfleet's GraphQL server.")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    args = parser.parse_args()

    uvicorn.run("lcars.server:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
