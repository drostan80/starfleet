"""CLI entry point — BUILD_PLAN.md A.9's anilist-login bootstrap, plus
the pre-existing serve command's subcommand-shape confirmation."""

from unittest.mock import patch

import pytest

from lcars import cli, config


def test_no_subcommand_serves_with_defaults(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lcars"])
    with patch("uvicorn.run") as run:
        cli.main()
    run.assert_called_once_with("lcars.server:create_app", factory=True, host="0.0.0.0", port=8000)


def test_serve_subcommand_with_flags(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lcars", "serve", "--host", "127.0.0.1", "--port", "9000"])
    with patch("uvicorn.run") as run:
        cli.main()
    run.assert_called_once_with(
        "lcars.server:create_app", factory=True, host="127.0.0.1", port=9000
    )


def test_anilist_login_requires_client_credentials_first(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lcars", "anilist-login"])
    with patch("lcars.config.load_config", return_value=config.Config()):
        with pytest.raises(SystemExit, match="anilist_client_id"):
            cli.main()


def test_anilist_login_walks_through_the_pin_flow_and_saves(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["lcars", "anilist-login"])
    cfg = config.Config(anilist_client_id="cid", anilist_client_secret="csecret")
    with (
        patch("lcars.config.load_config", return_value=cfg),
        patch("builtins.input", return_value="pastedcode"),
        patch("lcars.anilist_client.exchange_code", return_value="newtoken") as exchange,
        patch("lcars.config.save_anilist_token") as save,
    ):
        cli.main()
    exchange.assert_called_once_with("cid", "csecret", "pastedcode")
    save.assert_called_once_with("newtoken")
    assert "anilist.co/api/v2/oauth/authorize?client_id=cid" in capsys.readouterr().out


def test_mal_login_requires_client_id_first(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lcars", "mal-login"])
    with patch("lcars.config.load_config", return_value=config.Config()):
        with pytest.raises(SystemExit, match="mal_client_id"):
            cli.main()


def test_mal_login_walks_through_the_pkce_flow_and_saves(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["lcars", "mal-login"])
    cfg = config.Config(mal_client_id="mcid")  # no mal_client_secret set — the real shape
    with (
        patch("lcars.config.load_config", return_value=cfg),
        patch("builtins.input", return_value="pastedcode"),
        patch("lcars.mal_client.generate_code_verifier", return_value="the-verifier"),
        patch("lcars.mal_client.exchange_code", return_value=("acc-1", "ref-1")) as exchange,
        patch("lcars.config.save_mal_tokens") as save,
    ):
        cli.main()
    exchange.assert_called_once_with("mcid", None, "pastedcode", "the-verifier")
    save.assert_called_once_with("acc-1", "ref-1")
    out = capsys.readouterr().out
    assert "myanimelist.net/v1/oauth2/authorize?response_type=code&client_id=mcid" in out
    assert "code_challenge=the-verifier" in out
