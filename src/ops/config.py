"""ops.ini + env var loading — SCOPE.md §11.2's "Resolved 2026-08-09
(B.1)" note, §11.3's compose-service addendum.

Same file < env < env `_FILE` precedence `lcars/config.py`'s own
`_resolve_secret()` established (A.23) — duplicated here rather than
imported: Ops and LCARS are two independent deployables (§11.2's B.1
note deliberately puts them in separate processes/containers), so
importing `lcars.config` directly would blur a boundary this design
drew on purpose. Same "close port, not a shared import" reasoning
`ops/lcars_client.py`'s own docstring already gives for porting rather
than importing Data's real `lcars_client.py`.
"""

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

# Nested under the shared "starfleet" namespace, matching lcars/config.py's
# own collision-avoidance reasoning (SCOPE.md §4.0 addendum) rather than a
# bare, generic "ops" — a name a real risk of colliding with some unrelated
# tool's own config directory.
CONFIG_PATH = Path.home() / ".config" / "starfleet" / "ops" / "ops.ini"


@dataclass
class Config:
    # LCARS's own internal compose address by default (§11.3's B.1
    # addendum) — overridden per-environment, not a secret.
    lcars_url: str = "http://lcars:8000"
    # The same shared secret configured on LCARS's own bearer_token side
    # (§8) — Ops is a peer client, not a privileged caller (§3 principle 8).
    lcars_bearer_token: str | None = None
    # §6.7/B.1 — how often Ops sweeps dueForMetadataRefresh. The actual
    # once-per-day ceiling lives server-side (show.metadata_last_
    # refreshed_at), so this only controls how promptly a newly-due show
    # gets picked up, not correctness — an hourly default catches
    # anything due well within the same day without hammering the API.
    # Also drives B.2's weekly tier (dueForSeasonReconciliation) —
    # self-gating the same way, so it rides this same cadence rather than
    # needing its own interval (SCOPE.md §5.5's B.2 note).
    poll_interval_seconds: int = 3600
    # §5.5/B.2 — the monthly tier's own interval: an unconditional,
    # ungated sweep of every season of every show, so unlike
    # poll_interval_seconds above, this genuinely *is* the correctness
    # boundary — Ops's own timer, not a stored "due" ceiling. 30 days,
    # matching the cadence it's named for.
    monthly_poll_interval_seconds: int = 30 * 24 * 3600


def _resolve_secret(value: str | None, env_var: str) -> str | None:
    """File (lowest) < plain env var < `<env_var>_FILE`-pointed secret
    file (highest) — same convention `lcars/config.py`'s own
    `_resolve_secret()` established (A.23), applied here to
    `lcars_bearer_token`. Trailing whitespace/newline stripped, same
    reasoning: a mounted secret file almost always ends in one."""
    value = os.environ.get(env_var, value)
    file_path = os.environ.get(f"{env_var}_FILE")
    if file_path:
        value = Path(file_path).read_text().strip()
    return value


def load_config(config_path: Path = CONFIG_PATH) -> Config:
    """File < env < env `_FILE` (Docker secret) precedence — see
    _resolve_secret() above."""
    cfg = Config()
    if config_path.exists():
        parser = configparser.ConfigParser()
        parser.read(config_path)
        if parser.has_section("ops"):
            cfg.lcars_url = parser["ops"].get("lcars_url", fallback=cfg.lcars_url)
            cfg.lcars_bearer_token = parser["ops"].get("lcars_bearer_token", fallback=None)
            cfg.poll_interval_seconds = parser["ops"].getint(
                "poll_interval_seconds", fallback=cfg.poll_interval_seconds
            )
            cfg.monthly_poll_interval_seconds = parser["ops"].getint(
                "monthly_poll_interval_seconds", fallback=cfg.monthly_poll_interval_seconds
            )
    cfg.lcars_url = os.environ.get("OPS_LCARS_URL", cfg.lcars_url)  # not a secret
    cfg.lcars_bearer_token = _resolve_secret(cfg.lcars_bearer_token, "OPS_LCARS_BEARER_TOKEN")
    env_interval = os.environ.get("OPS_POLL_INTERVAL_SECONDS")
    if env_interval is not None:
        cfg.poll_interval_seconds = int(env_interval)
    env_monthly_interval = os.environ.get("OPS_MONTHLY_POLL_INTERVAL_SECONDS")
    if env_monthly_interval is not None:
        cfg.monthly_poll_interval_seconds = int(env_monthly_interval)
    return cfg
