"""CLI entry point — BUILD_PLAN.md A.9's anilist-login bootstrap, plus
the pre-existing serve command's subcommand-shape confirmation."""

from unittest.mock import patch

import pytest

from lcars import cli, config


def test_no_subcommand_serves_with_defaults(monkeypatch):
    monkeypatch.setattr("sys.argv", ["lcars"])
    with patch("uvicorn.run") as run:
        cli.main()
    run.assert_called_once_with(
        "lcars.server:create_app", factory=True, host="0.0.0.0", port=8000
    )


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
