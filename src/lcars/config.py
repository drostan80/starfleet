"""lcars.ini + env var loading — SCOPE.md §11.2/§8.

Mirrors aniq's own config.py pattern (file < env precedence, same
configparser-based shape) rather than inventing a new one — see
CLAUDE.md's own note in the Data fork for why that pattern was kept.

Sonarr/Radarr credentials added here 2026-08-08 (A.8) — LCARS itself
now makes the on-demand metadata fetch (poster/synopsis/cast/episode
list/external ids) on show creation, per the user's explicit
direction: "ultimately all compute and fetch will be handled on the
server by lcars, so might as well built it this way now" — a deliberate
reversal of A.4/A.7's original "client fetches, LCARS just reconciles"
framing once it became clear that framing was only ever a Data-shaped
transitional stage, not the end state. A.8's own Media-by-id fetch is
AniList's public, unauthenticated GraphQL endpoint — no credential
needed there.

AniList OAuth credentials added here 2026-08-08 (A.9) — confirmed
directly: score/status push (§6.1/§6.8) is explicitly not the same
exception as episode watch-status (§6.8's narrow Data-only path); it
goes through LCARS, LCARS pushes it. `anilist_client_id`/`_secret`
reuse Data/aniq's existing registered AniList app (confirmed, not a
new one) — LCARS runs its own separate authorization (`lcars
anilist-login`, cli.py) to mint its own independent
`anilist_access_token`, stored the same plaintext-`lcars.ini`-chmod-600
way as everything else here (§8's own precedent, not a new pattern).
`home_timezone` added here 2026-08-08 (A.16) — §11.2 already lists it
alongside bearer_token/Sonarr/Radarr/AniList credentials as an
`lcars.ini` value, not a database row or GraphQL-mutable setting
(nothing in schema.graphql anticipated a "settings" type at all, so
this genuinely had no other candidate home — checked before assuming).
Its actual consumers (§6.13: "today" on the calendar, paced/catch-up
mode's cadence *reset*, the daily metadata-refresh cadence) are all
still-unbuilt Phase B/client concerns — A.10's own `pacedNextDate`
already built the *adaptive computation* §6.2 describes, which is pure
UTC timestamp arithmetic with no day-boundary bucketing in its own
text; A.16's job is the setting itself, per its own BUILD_PLAN text,
not retrofitting a consumer that doesn't exist yet. No validation of
the value (e.g. against `zoneinfo`) — no other value in this file is
validated either, consistent with the pattern already established
here.
"""

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

# Nested under a shared "starfleet" namespace, not a bare "lcars" — same
# collision-avoidance reasoning as Data's own runtime-state isolation
# (SCOPE.md §4.0 addendum): a generic top-level name risks colliding with
# some unrelated future tool using the same name.
CONFIG_PATH = Path.home() / ".config" / "starfleet" / "lcars" / "lcars.ini"

# lcars.db's default location, relative to cwd — matches alembic.ini's own
# `sqlite:///lcars.db` (§11.2) so migrations and the running app agree on
# the same file without duplicating the path in two places.
DEFAULT_DB_PATH = Path("lcars.db")


@dataclass
class Config:
    # The credential LCARS checks incoming requests against — plaintext in
    # lcars.ini, chmod 600, resolved 2026-08-08 during A.3 (SCOPE.md §8):
    # the desktop keyring pattern aniq/Data use doesn't apply to a headless
    # Docker server.
    bearer_token: str | None = None
    db_path: Path = DEFAULT_DB_PATH
    # A.8 — both optional: media_shape/tracking_space determine whether
    # either is even relevant to a given show (§5.1's file_source axis),
    # and within Phase A a show can be tracked with neither linked at all
    # (Sonarr/Radarr are "if linked", never mandatory — only AniList is,
    # for tracking_space = anime).
    sonarr_url: str | None = None
    sonarr_api_key: str | None = None
    radarr_url: str | None = None
    radarr_api_key: str | None = None
    # A.9 — client_id/secret are Data/aniq's existing registered AniList
    # app's credentials (reused, confirmed, not a new registration);
    # access_token is LCARS's own, obtained via `lcars anilist-login`
    # (cli.py) and saved with save_anilist_token() below. All three
    # optional: the score/status push is best-effort (metadata.py's own
    # A.8 precedent) — not yet authenticated just means the push is
    # skipped, same as Sonarr/Radarr "not configured" above.
    anilist_client_id: str | None = None
    anilist_client_secret: str | None = None
    anilist_access_token: str | None = None
    # A.16, §6.13 — an IANA timezone name. Used to bucket UTC-stored
    # timestamps into calendar days wherever a "day" boundary matters
    # (calendar/pacing-reset/daily-refresh, §6.13); storage itself always
    # stays UTC, this never affects how a timestamp is written.
    home_timezone: str = "Europe/Dublin"
    # A.19 — fixes the show.duration_minutes gap flagged during A.13
    # (§6.6): a plain v3 TMDB API key (read-only public metadata, no
    # OAuth), covering every non-anime show (movie or TV) — AniList
    # already covers tracking_space=anime via its own `duration` field.
    # Optional, same "not configured = same as not linked, no failure to
    # report" treatment metadata.py already gives Sonarr/Radarr.
    tmdb_api_key: str | None = None


def load_config(config_path: Path = CONFIG_PATH) -> Config:
    """File < env precedence, same as aniq's own load_config()."""
    cfg = Config()
    if config_path.exists():
        parser = configparser.ConfigParser()
        parser.read(config_path)
        if parser.has_section("lcars"):
            cfg.bearer_token = parser["lcars"].get("bearer_token", fallback=None)
            db_path = parser["lcars"].get("db_path", fallback=None)
            if db_path is not None:
                cfg.db_path = Path(db_path)
            cfg.sonarr_url = parser["lcars"].get("sonarr_url", fallback=None)
            cfg.sonarr_api_key = parser["lcars"].get("sonarr_api_key", fallback=None)
            cfg.radarr_url = parser["lcars"].get("radarr_url", fallback=None)
            cfg.radarr_api_key = parser["lcars"].get("radarr_api_key", fallback=None)
            cfg.anilist_client_id = parser["lcars"].get("anilist_client_id", fallback=None)
            cfg.anilist_client_secret = parser["lcars"].get(
                "anilist_client_secret", fallback=None
            )
            cfg.anilist_access_token = parser["lcars"].get("anilist_access_token", fallback=None)
            cfg.home_timezone = parser["lcars"].get("home_timezone", fallback=cfg.home_timezone)
            cfg.tmdb_api_key = parser["lcars"].get("tmdb_api_key", fallback=None)
    cfg.bearer_token = os.environ.get("LCARS_BEARER_TOKEN", cfg.bearer_token)
    env_db_path = os.environ.get("LCARS_DB_PATH")
    if env_db_path is not None:
        cfg.db_path = Path(env_db_path)
    cfg.sonarr_url = os.environ.get("LCARS_SONARR_URL", cfg.sonarr_url)
    cfg.sonarr_api_key = os.environ.get("LCARS_SONARR_API_KEY", cfg.sonarr_api_key)
    cfg.radarr_url = os.environ.get("LCARS_RADARR_URL", cfg.radarr_url)
    cfg.radarr_api_key = os.environ.get("LCARS_RADARR_API_KEY", cfg.radarr_api_key)
    cfg.anilist_client_id = os.environ.get("LCARS_ANILIST_CLIENT_ID", cfg.anilist_client_id)
    cfg.anilist_client_secret = os.environ.get(
        "LCARS_ANILIST_CLIENT_SECRET", cfg.anilist_client_secret
    )
    cfg.anilist_access_token = os.environ.get(
        "LCARS_ANILIST_ACCESS_TOKEN", cfg.anilist_access_token
    )
    cfg.home_timezone = os.environ.get("LCARS_HOME_TIMEZONE", cfg.home_timezone)
    cfg.tmdb_api_key = os.environ.get("LCARS_TMDB_API_KEY", cfg.tmdb_api_key)
    return cfg


# A.8 — module-level singleton, same pattern as db.py's _connection: resolvers
# need Sonarr/Radarr credentials to fetch metadata, but sync resolvers (§11.2)
# have no per-request DI mechanism of their own (context_value only carries
# the X-LCARS-Client header, server.py). Set once at startup (create_app()),
# read from metadata.py's fetch orchestration.
_current: Config | None = None


def set_current(cfg: Config) -> None:
    global _current
    _current = cfg


def get_current() -> Config:
    """Raises if set_current() hasn't run yet — a programming error
    (missing app startup), not a recoverable condition. Mirrors
    db.get_connection()'s own same-shaped guard."""
    if _current is None:
        raise RuntimeError("lcars.config.set_current() must be called before get_current()")
    return _current


def save_bearer_token(token: str, config_path: Path = CONFIG_PATH) -> None:
    """Merge-not-overwrite, chmod 600 — same pattern as aniq's own
    save_anilist_credentials()/save_trakt_credentials()."""
    parser = configparser.ConfigParser()
    if config_path.exists():
        parser.read(config_path)
    if not parser.has_section("lcars"):
        parser.add_section("lcars")
    parser["lcars"]["bearer_token"] = token
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w") as f:
        parser.write(f)
    config_path.chmod(0o600)


def save_anilist_token(token: str, config_path: Path = CONFIG_PATH) -> None:
    """A.9 — saved by `lcars anilist-login` (cli.py) once the OAuth
    exchange succeeds. Same merge-not-overwrite/chmod-600 shape as
    save_bearer_token() above."""
    parser = configparser.ConfigParser()
    if config_path.exists():
        parser.read(config_path)
    if not parser.has_section("lcars"):
        parser.add_section("lcars")
    parser["lcars"]["anilist_access_token"] = token
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w") as f:
        parser.write(f)
    config_path.chmod(0o600)
