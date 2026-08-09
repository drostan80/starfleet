"""ops's own argparse entry point (ops/cli.py) — B.1's `ops run`, plus
B.3's `ops backfill-availability` (the manual, one-time counterpart to
Ops's own automatic availability polling — see availability.py's and
schema.graphql's own docstrings for the full rationale). Same
patch-load_config/patch-run_forever style test_cli.py already
established for lcars's own CLI.
"""

from unittest.mock import AsyncMock, patch

import pytest

from ops import cli, config


def test_no_subcommand_defaults_to_run(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ops"])
    cfg = config.Config(lcars_bearer_token="test-token")
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch("ops.cli.run_forever", new_callable=AsyncMock) as run_forever,
    ):
        cli.main()
    run_forever.assert_called_once()


def test_run_requires_bearer_token_first(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ops", "run"])
    with patch("ops.config.load_config", return_value=config.Config()):
        with pytest.raises(SystemExit, match="lcars_bearer_token"):
            cli.main()


def test_backfill_availability_requires_bearer_token_first(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ops", "backfill-availability"])
    with patch("ops.config.load_config", return_value=config.Config()):
        with pytest.raises(SystemExit, match="lcars_bearer_token"):
            cli.main()


def test_backfill_availability_calls_the_client_and_prints_the_result(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ops", "backfill-availability"])
    cfg = config.Config(lcars_bearer_token="test-token")
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch(
            "ops.cli.LcarsClient.backfill_file_availability",
            new_callable=AsyncMock,
            return_value={"episodesUpdated": 3, "showsUpdated": 1},
        ) as backfill,
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
    ):
        cli.main()
    backfill.assert_called_once()
    out = capsys.readouterr().out
    assert "will block LCARS's other requests" in out  # the deliberate warning
    assert "3 episode(s)" in out
    assert "1 show(s)" in out
