"""lcars.ini + env var loading — BUILD_PLAN.md A.3, mirrors aniq's own
test_config.py pattern (file < env precedence, chmod 600 on save)."""

from pathlib import Path

from lcars.config import Config, load_config, save_bearer_token


def test_defaults_when_no_file_and_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv("LCARS_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("LCARS_DB_PATH", raising=False)
    cfg = load_config(config_path=tmp_path / "does-not-exist.ini")
    assert cfg == Config()


def test_save_then_load_round_trips(tmp_path, monkeypatch):
    monkeypatch.delenv("LCARS_BEARER_TOKEN", raising=False)
    config_path = tmp_path / "lcars.ini"
    save_bearer_token("secret-token", config_path=config_path)
    cfg = load_config(config_path=config_path)
    assert cfg.bearer_token == "secret-token"


def test_save_bearer_token_chmods_owner_only(tmp_path):
    config_path = tmp_path / "lcars.ini"
    save_bearer_token("secret-token", config_path=config_path)
    mode = config_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_env_var_overrides_file(tmp_path, monkeypatch):
    config_path = tmp_path / "lcars.ini"
    save_bearer_token("from-file", config_path=config_path)
    monkeypatch.setenv("LCARS_BEARER_TOKEN", "from-env")
    cfg = load_config(config_path=config_path)
    assert cfg.bearer_token == "from-env"


def test_db_path_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("LCARS_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("LCARS_DB_PATH", "/tmp/somewhere/lcars.db")
    cfg = load_config(config_path=tmp_path / "does-not-exist.ini")
    assert cfg.db_path == Path("/tmp/somewhere/lcars.db")
