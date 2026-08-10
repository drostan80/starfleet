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

MAL OAuth credentials added here 2026-08-10 (B.10) — same shape as
AniList's own (A.9), but `mal_client_secret` is genuinely optional
end-to-end, not just optional in this dataclass: MAL's own app
registration issued no secret at all for the "Other" (PKCE public
client) app type LCARS registered as, confirmed live, not assumed
(`mal_client.py`'s own module docstring has the correction against
SCOPE.md §6.9's original assumption). `mal_refresh_token` is new
relative to AniList's shape — AniList's access token doesn't expire in
any way this codebase handles, MAL's does (1 hour) with a genuinely
short-lived (1 month) refresh token behind it, so both halves of the
pair are persisted (`save_mal_tokens()` below) and refreshed
proactively (B.10's own `refreshMalTokenIfDue`, resolvers.py).
`mal_token_refreshed_at` is the due-gate for that job — same "small
last-checked timestamp, no new DB table" shape as everything else
proactively refreshed on a schedule elsewhere in this codebase, kept
in `lcars.ini` alongside the tokens themselves (not a DB row) since
it's a single global credential's own bookkeeping, not per-show/
per-season state.
"""

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

from lcars import util

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
    # B.10 — mal_client_secret is genuinely optional even once configured
    # (module docstring above); mal_access_token/mal_refresh_token are
    # LCARS's own, minted via `lcars mal-login` (cli.py) and kept fresh by
    # refreshMalTokenIfDue (resolvers.py). mal_token_refreshed_at gates
    # that job — a plain ISO timestamp, not validated/parsed here.
    mal_client_id: str | None = None
    mal_client_secret: str | None = None
    mal_access_token: str | None = None
    mal_refresh_token: str | None = None
    mal_token_refreshed_at: str | None = None


def _resolve_secret(value: str | None, env_var: str) -> str | None:
    """File (lowest) < plain env var < `<env_var>_FILE`-pointed secret
    file (highest) — resolved 2026-08-09, A.23 consolidation audit.
    Plaintext `lcars.ini` alone doesn't fit a headless Docker deployment
    as well as Compose's own `secrets:` mechanism, which mounts each
    secret as a file (typically under `/run/secrets/<name>`) rather than
    baking it into an env var or a file this app itself manages. A
    `<VAR>_FILE` env var pointing at that mounted file — the same
    convention the official postgres/mysql Docker images use — wins over
    everything else when set, since it's the most explicit,
    deployment-time-chosen source. `lcars.ini` isn't removed: some
    values (`anilist_access_token`, minted interactively by `lcars
    anilist-login`) are written by LCARS itself at runtime, which a
    read-only secret mount can't support — see save_anilist_token()
    below. Trailing whitespace/newline is stripped: a mounted secret
    file almost always ends in one, and a bearer token silently
    including it would fail every auth check with no obvious reason
    why."""
    value = os.environ.get(env_var, value)
    file_path = os.environ.get(f"{env_var}_FILE")
    if file_path:
        value = Path(file_path).read_text().strip()
    return value


def load_config(config_path: Path = CONFIG_PATH) -> Config:
    """File < env < env `_FILE` (Docker secret) precedence — see
    _resolve_secret() above for the A.23 addition; everything else
    matches aniq's own load_config() shape unchanged."""
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
            cfg.anilist_client_secret = parser["lcars"].get("anilist_client_secret", fallback=None)
            cfg.anilist_access_token = parser["lcars"].get("anilist_access_token", fallback=None)
            cfg.home_timezone = parser["lcars"].get("home_timezone", fallback=cfg.home_timezone)
            cfg.tmdb_api_key = parser["lcars"].get("tmdb_api_key", fallback=None)
            cfg.mal_client_id = parser["lcars"].get("mal_client_id", fallback=None)
            cfg.mal_client_secret = parser["lcars"].get("mal_client_secret", fallback=None)
            cfg.mal_access_token = parser["lcars"].get("mal_access_token", fallback=None)
            cfg.mal_refresh_token = parser["lcars"].get("mal_refresh_token", fallback=None)
            cfg.mal_token_refreshed_at = parser["lcars"].get(
                "mal_token_refreshed_at", fallback=None
            )
    cfg.bearer_token = _resolve_secret(cfg.bearer_token, "LCARS_BEARER_TOKEN")
    env_db_path = os.environ.get("LCARS_DB_PATH")
    if env_db_path is not None:
        cfg.db_path = Path(env_db_path)
    cfg.sonarr_url = os.environ.get("LCARS_SONARR_URL", cfg.sonarr_url)  # not a secret
    cfg.sonarr_api_key = _resolve_secret(cfg.sonarr_api_key, "LCARS_SONARR_API_KEY")
    cfg.radarr_url = os.environ.get("LCARS_RADARR_URL", cfg.radarr_url)  # not a secret
    cfg.radarr_api_key = _resolve_secret(cfg.radarr_api_key, "LCARS_RADARR_API_KEY")
    cfg.anilist_client_id = _resolve_secret(cfg.anilist_client_id, "LCARS_ANILIST_CLIENT_ID")
    cfg.anilist_client_secret = _resolve_secret(
        cfg.anilist_client_secret, "LCARS_ANILIST_CLIENT_SECRET"
    )
    cfg.anilist_access_token = _resolve_secret(
        cfg.anilist_access_token, "LCARS_ANILIST_ACCESS_TOKEN"
    )
    cfg.home_timezone = os.environ.get("LCARS_HOME_TIMEZONE", cfg.home_timezone)  # not a secret
    cfg.tmdb_api_key = _resolve_secret(cfg.tmdb_api_key, "LCARS_TMDB_API_KEY")
    cfg.mal_client_id = _resolve_secret(cfg.mal_client_id, "LCARS_MAL_CLIENT_ID")
    cfg.mal_client_secret = _resolve_secret(cfg.mal_client_secret, "LCARS_MAL_CLIENT_SECRET")
    cfg.mal_access_token = _resolve_secret(cfg.mal_access_token, "LCARS_MAL_ACCESS_TOKEN")
    cfg.mal_refresh_token = _resolve_secret(cfg.mal_refresh_token, "LCARS_MAL_REFRESH_TOKEN")
    # mal_token_refreshed_at isn't a secret (a plain timestamp) — same
    # non-secret treatment sonarr_url/radarr_url/home_timezone already get.
    cfg.mal_token_refreshed_at = os.environ.get(
        "LCARS_MAL_TOKEN_REFRESHED_AT", cfg.mal_token_refreshed_at
    )
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


def save_mal_tokens(access_token: str, refresh_token: str, config_path: Path = CONFIG_PATH) -> None:
    """B.10 — saved both by `lcars mal-login` (cli.py, the initial
    exchange) and by `refreshMalTokenIfDue` (resolvers.py, every
    proactive refresh) — both halves of the pair are always written
    together, never just the access token, since MAL's own refresh-
    rotation behavior is ambiguous (mal_client.py's own module
    docstring) and this is correct regardless of which way it turns
    out to behave. Also stamps `mal_token_refreshed_at` — the due-gate
    `refreshMalTokenIfDue` reads back on its next call. Same merge-not-
    overwrite/chmod-600 shape as save_bearer_token()/save_anilist_
    token() above.

    **A real tension flagged, not silently hit in production**:
    `_resolve_secret`'s env/`_FILE` precedence (A.23) means an
    env-supplied `LCARS_MAL_ACCESS_TOKEN`/`_REFRESH_TOKEN` would
    silently outrank whatever this function just wrote to `lcars.ini`
    on the *next* `load_config()` call — the exact tension this
    module's own `_resolve_secret` docstring already names for
    `anilist_access_token` ("written by LCARS itself at runtime, which
    a read-only secret mount can't support"). Not a new problem this
    function introduces, and not solved differently here — same
    accepted tradeoff, just worth restating since MAL's token
    genuinely does get rewritten on a live schedule (weekly-ish),
    unlike AniList's essentially-static one."""
    parser = configparser.ConfigParser()
    if config_path.exists():
        parser.read(config_path)
    if not parser.has_section("lcars"):
        parser.add_section("lcars")
    parser["lcars"]["mal_access_token"] = access_token
    parser["lcars"]["mal_refresh_token"] = refresh_token
    parser["lcars"]["mal_token_refreshed_at"] = util.now_utc_iso()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w") as f:
        parser.write(f)
    config_path.chmod(0o600)
