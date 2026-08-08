"""lcars.ini + env var loading — SCOPE.md §11.2/§8.

Mirrors aniq's own config.py pattern (file < env precedence, same
configparser-based shape) rather than inventing a new one — see
CLAUDE.md's own note in the Data fork for why that pattern was kept.

Only the fields A.3's vertical slice actually needs are here
(bearer_token, db_path). Sonarr/Radarr/AniList/MAL credentials and
home_timezone (§6.13) get added alongside the BUILD_PLAN.md steps that
actually use them (A.16, Phase B integrations) — not pulled forward
early, same discipline A.1/A.2 already held to.
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
    cfg.bearer_token = os.environ.get("LCARS_BEARER_TOKEN", cfg.bearer_token)
    env_db_path = os.environ.get("LCARS_DB_PATH")
    if env_db_path is not None:
        cfg.db_path = Path(env_db_path)
    return cfg


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
