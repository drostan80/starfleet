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


# --- Sonarr/Radarr/AniList fields (A.8/A.9) — file<env precedence coverage,
# never given their own dedicated tests when those fields were first added ---


def test_sonarr_radarr_load_from_file(tmp_path):
    config_path = tmp_path / "lcars.ini"
    config_path.write_text(
        "[lcars]\n"
        "sonarr_url = http://sonarr:8989\n"
        "sonarr_api_key = sonarr-key\n"
        "radarr_url = http://radarr:7878\n"
        "radarr_api_key = radarr-key\n"
    )
    cfg = load_config(config_path=config_path)
    assert cfg.sonarr_url == "http://sonarr:8989"
    assert cfg.sonarr_api_key == "sonarr-key"
    assert cfg.radarr_url == "http://radarr:7878"
    assert cfg.radarr_api_key == "radarr-key"


def test_sonarr_env_vars_override_file(tmp_path, monkeypatch):
    config_path = tmp_path / "lcars.ini"
    config_path.write_text("[lcars]\nsonarr_url = http://from-file:8989\n")
    monkeypatch.setenv("LCARS_SONARR_URL", "http://from-env:8989")
    cfg = load_config(config_path=config_path)
    assert cfg.sonarr_url == "http://from-env:8989"


def test_anilist_oauth_fields_load_from_file(tmp_path):
    config_path = tmp_path / "lcars.ini"
    config_path.write_text(
        "[lcars]\n"
        "anilist_client_id = cid\n"
        "anilist_client_secret = csecret\n"
        "anilist_access_token = tok\n"
    )
    cfg = load_config(config_path=config_path)
    assert cfg.anilist_client_id == "cid"
    assert cfg.anilist_client_secret == "csecret"
    assert cfg.anilist_access_token == "tok"


def test_anilist_access_token_env_var_overrides_file(tmp_path, monkeypatch):
    config_path = tmp_path / "lcars.ini"
    config_path.write_text("[lcars]\nanilist_access_token = from-file\n")
    monkeypatch.setenv("LCARS_ANILIST_ACCESS_TOKEN", "from-env")
    cfg = load_config(config_path=config_path)
    assert cfg.anilist_access_token == "from-env"


# --- home_timezone (A.16, §6.13) ---------------------------------------------


def test_home_timezone_defaults_to_europe_dublin(tmp_path):
    cfg = load_config(config_path=tmp_path / "does-not-exist.ini")
    assert cfg.home_timezone == "Europe/Dublin"


def test_home_timezone_loads_from_file(tmp_path):
    config_path = tmp_path / "lcars.ini"
    config_path.write_text("[lcars]\nhome_timezone = America/New_York\n")
    cfg = load_config(config_path=config_path)
    assert cfg.home_timezone == "America/New_York"


def test_home_timezone_env_var_overrides_file(tmp_path, monkeypatch):
    config_path = tmp_path / "lcars.ini"
    config_path.write_text("[lcars]\nhome_timezone = America/New_York\n")
    monkeypatch.setenv("LCARS_HOME_TIMEZONE", "Asia/Tokyo")
    cfg = load_config(config_path=config_path)
    assert cfg.home_timezone == "Asia/Tokyo"


# --- tmdb_api_key (A.19, §5.1) ------------------------------------------------


def test_tmdb_api_key_defaults_to_none(tmp_path, monkeypatch):
    monkeypatch.delenv("LCARS_TMDB_API_KEY", raising=False)
    cfg = load_config(config_path=tmp_path / "does-not-exist.ini")
    assert cfg.tmdb_api_key is None


def test_tmdb_api_key_loads_from_file(tmp_path):
    config_path = tmp_path / "lcars.ini"
    config_path.write_text("[lcars]\ntmdb_api_key = abc123\n")
    cfg = load_config(config_path=config_path)
    assert cfg.tmdb_api_key == "abc123"


def test_tmdb_api_key_env_var_overrides_file(tmp_path, monkeypatch):
    config_path = tmp_path / "lcars.ini"
    config_path.write_text("[lcars]\ntmdb_api_key = abc123\n")
    monkeypatch.setenv("LCARS_TMDB_API_KEY", "env-key")
    cfg = load_config(config_path=config_path)
    assert cfg.tmdb_api_key == "env-key"


# --- Docker secrets / mounted secret files (A.23, 2026-08-09 consolidation
# audit) — `<VAR>_FILE` wins over everything, same convention the official
# postgres/mysql Docker images use, resolved after the user flagged
# plaintext-lcars.ini-only as not fitting a headless Docker deployment well.


def test_bearer_token_file_env_var_wins_over_plain_env_and_file(tmp_path, monkeypatch):
    config_path = tmp_path / "lcars.ini"
    save_bearer_token("from-file", config_path=config_path)
    monkeypatch.setenv("LCARS_BEARER_TOKEN", "from-plain-env")
    secret_file = tmp_path / "bearer_token_secret"
    secret_file.write_text("from-secret-file\n")  # trailing newline, as a real mount would have
    monkeypatch.setenv("LCARS_BEARER_TOKEN_FILE", str(secret_file))
    cfg = load_config(config_path=config_path)
    assert cfg.bearer_token == "from-secret-file"  # stripped, and beats the plain env var


def test_secret_file_env_var_strips_trailing_whitespace(tmp_path, monkeypatch):
    monkeypatch.delenv("LCARS_TMDB_API_KEY", raising=False)
    secret_file = tmp_path / "tmdb_key"
    secret_file.write_text("  abc123  \n")
    monkeypatch.setenv("LCARS_TMDB_API_KEY_FILE", str(secret_file))
    cfg = load_config(config_path=tmp_path / "does-not-exist.ini")
    assert cfg.tmdb_api_key == "abc123"


def test_no_secret_file_env_var_falls_back_to_plain_precedence(tmp_path, monkeypatch):
    config_path = tmp_path / "lcars.ini"
    config_path.write_text("[lcars]\nsonarr_api_key = from-file\n")
    monkeypatch.delenv("LCARS_SONARR_API_KEY_FILE", raising=False)
    monkeypatch.delenv("LCARS_SONARR_API_KEY", raising=False)
    cfg = load_config(config_path=config_path)
    assert cfg.sonarr_api_key == "from-file"


def test_every_secret_field_supports_file_env_var(tmp_path, monkeypatch):
    """Every credential this project treats as a secret (not sonarr_url/
    radarr_url/home_timezone, which aren't secrets) gets `_FILE` support —
    a systematic sweep, same "check every field, don't assume" practice
    this project has used throughout, rather than trusting bearer_token's
    own coverage generalizes."""
    fields = {
        "LCARS_BEARER_TOKEN": "bearer_token",
        "LCARS_SONARR_API_KEY": "sonarr_api_key",
        "LCARS_RADARR_API_KEY": "radarr_api_key",
        "LCARS_ANILIST_CLIENT_ID": "anilist_client_id",
        "LCARS_ANILIST_CLIENT_SECRET": "anilist_client_secret",
        "LCARS_ANILIST_ACCESS_TOKEN": "anilist_access_token",
        "LCARS_TMDB_API_KEY": "tmdb_api_key",
    }
    for env_var, attr in fields.items():
        monkeypatch.delenv(env_var, raising=False)
        secret_file = tmp_path / f"{attr}_secret"
        secret_file.write_text(f"value-for-{attr}")
        monkeypatch.setenv(f"{env_var}_FILE", str(secret_file))
    cfg = load_config(config_path=tmp_path / "does-not-exist.ini")
    for env_var, attr in fields.items():
        assert getattr(cfg, attr) == f"value-for-{attr}", attr
        monkeypatch.delenv(f"{env_var}_FILE", raising=False)
