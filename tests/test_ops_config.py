"""ops.ini + env var loading — B.1. Mirrors lcars/config.py's own
test_config.py coverage for the same file<env<env`_FILE` precedence
(A.23) — a real duplicate implementation (SCOPE.md §11.2's B.1 note
explains why it isn't a shared import), so it needs its own real
coverage, not an assumption that lcars/config.py's tests generalize.
"""

from ops.config import load_config


def test_defaults_with_no_config_file_or_env(tmp_path, monkeypatch):
    monkeypatch.delenv("OPS_LCARS_URL", raising=False)
    monkeypatch.delenv("OPS_LCARS_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("OPS_LCARS_BEARER_TOKEN_FILE", raising=False)
    monkeypatch.delenv("OPS_POLL_INTERVAL_SECONDS", raising=False)
    cfg = load_config(config_path=tmp_path / "does-not-exist.ini")
    assert cfg.lcars_url == "http://lcars:8000"
    assert cfg.lcars_bearer_token is None
    assert cfg.poll_interval_seconds == 3600


def test_loads_from_file(tmp_path):
    config_path = tmp_path / "ops.ini"
    config_path.write_text(
        "[ops]\nlcars_url = http://lcars.internal:9000\n"
        "lcars_bearer_token = from-file\npoll_interval_seconds = 600\n"
    )
    cfg = load_config(config_path=config_path)
    assert cfg.lcars_url == "http://lcars.internal:9000"
    assert cfg.lcars_bearer_token == "from-file"
    assert cfg.poll_interval_seconds == 600


def test_plain_env_overrides_file(tmp_path, monkeypatch):
    config_path = tmp_path / "ops.ini"
    config_path.write_text("[ops]\nlcars_bearer_token = from-file\n")
    monkeypatch.delenv("OPS_LCARS_BEARER_TOKEN_FILE", raising=False)
    monkeypatch.setenv("OPS_LCARS_BEARER_TOKEN", "from-plain-env")
    cfg = load_config(config_path=config_path)
    assert cfg.lcars_bearer_token == "from-plain-env"


def test_secret_file_env_var_wins_over_plain_env_and_file(tmp_path, monkeypatch):
    config_path = tmp_path / "ops.ini"
    config_path.write_text("[ops]\nlcars_bearer_token = from-file\n")
    monkeypatch.setenv("OPS_LCARS_BEARER_TOKEN", "from-plain-env")
    secret_file = tmp_path / "bearer_token_secret"
    secret_file.write_text("from-secret-file\n")  # trailing newline, as a real mount would have
    monkeypatch.setenv("OPS_LCARS_BEARER_TOKEN_FILE", str(secret_file))
    cfg = load_config(config_path=config_path)
    assert cfg.lcars_bearer_token == "from-secret-file"  # stripped, and beats the plain env var


def test_poll_interval_env_var_overrides_file(tmp_path, monkeypatch):
    config_path = tmp_path / "ops.ini"
    config_path.write_text("[ops]\npoll_interval_seconds = 600\n")
    monkeypatch.setenv("OPS_POLL_INTERVAL_SECONDS", "120")
    cfg = load_config(config_path=config_path)
    assert cfg.poll_interval_seconds == 120
