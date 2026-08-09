"""ops's own argparse entry point (ops/cli.py) — B.1's `ops run`, B.3's
`ops backfill-availability`, and B.3b's `ops audit-local-files` (the
manual triggers for Ops's own non-automatic, potentially-slow LCARS
operations — see availability.py's/local_audit.py's and schema.graphql's
own docstrings for the full rationale). Same patch-load_config/
patch-run_forever style test_cli.py already established for lcars's
own CLI.
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


def test_audit_local_files_requires_bearer_token_first(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ops", "audit-local-files"])
    with patch("ops.config.load_config", return_value=config.Config()):
        with pytest.raises(SystemExit, match="lcars_bearer_token"):
            cli.main()


def test_audit_local_files_calls_the_client_and_prints_findings(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ops", "audit-local-files"])
    cfg = config.Config(lcars_bearer_token="test-token")
    result = {
        "episodesCorrected": 2,
        "showsCorrected": 1,
        "orphanFiles": [
            {"showId": "s-x", "path": "/data/x/orphan.mkv", "parsedSeason": 1,
             "parsedEpisode": 3},
            {"showId": "s-y", "path": "/data/y/weird.mkv", "parsedSeason": None,
             "parsedEpisode": None},
        ],
        "untrackedShows": [
            {"service": "sonarr", "title": "New Show", "externalId": 12345, "path": "/data/new"}
        ],
    }
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch(
            "ops.cli.LcarsClient.audit_local_files",
            new_callable=AsyncMock,
            return_value=result,
        ) as audit,
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
    ):
        cli.main()
    audit.assert_called_once()
    out = capsys.readouterr().out
    assert "will block LCARS's other requests" in out
    assert "2 episode(s)" in out
    assert "1 show(s)" in out
    assert "2 orphan file(s)" in out
    assert "[S01E03] /data/x/orphan.mkv" in out
    assert "season/episode unparseable" in out
    assert "/data/y/weird.mkv" in out
    assert "1 untracked remote show(s)" in out
    assert "[sonarr] New Show (12345) — /data/new" in out


def test_audit_local_files_prints_nothing_extra_with_no_findings(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ops", "audit-local-files"])
    cfg = config.Config(lcars_bearer_token="test-token")
    result = {
        "episodesCorrected": 0,
        "showsCorrected": 0,
        "orphanFiles": [],
        "untrackedShows": [],
    }
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch(
            "ops.cli.LcarsClient.audit_local_files", new_callable=AsyncMock, return_value=result
        ),
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
    ):
        cli.main()
    out = capsys.readouterr().out
    assert "orphan file(s)" not in out
    assert "untracked remote show(s)" not in out
