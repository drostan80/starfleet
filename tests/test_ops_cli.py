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


def test_backfill_availability_uses_a_long_client_timeout(monkeypatch):
    # Regression test, real bug live-caught 2026-08-11 (B.11f's first
    # live test): this used to construct LcarsClient with the default
    # 10s timeout despite its own docstring/print statement already
    # promising "seconds to minutes on a large library" — a real
    # deployed-instance run timed out after exactly 10s. Matches
    # _cmd_backfill_shows's own timeout=3600.0, same reasoning.
    monkeypatch.setattr("sys.argv", ["ops", "backfill-availability"])
    cfg = config.Config(lcars_bearer_token="test-token")
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch("ops.cli.LcarsClient", autospec=True) as lcars_client_cls,
    ):
        instance = lcars_client_cls.return_value
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.backfill_file_availability = AsyncMock(
            return_value={"episodesUpdated": 0, "showsUpdated": 0}
        )
        cli.main()
    lcars_client_cls.assert_called_once_with("http://lcars:8000", "test-token", timeout=3600.0)


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
            {"showId": "s-x", "path": "/data/x/orphan.mkv", "parsedSeason": 1, "parsedEpisode": 3},
            {
                "showId": "s-y",
                "path": "/data/y/weird.mkv",
                "parsedSeason": None,
                "parsedEpisode": None,
            },
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
        patch("ops.cli.LcarsClient.audit_local_files", new_callable=AsyncMock, return_value=result),
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
    ):
        cli.main()
    out = capsys.readouterr().out
    assert "orphan file(s)" not in out
    assert "untracked remote show(s)" not in out


def test_audit_local_files_uses_a_long_client_timeout(monkeypatch):
    # Regression test, real bug live-caught 2026-08-20 (NEXT_UP.md): this
    # used to construct LcarsClient with the default 10s timeout despite
    # blocking LCARS's single request-handling thread for a real
    # whole-library walk — a real manual run hit httpx.ReadTimeout even
    # though the mutation had already committed successfully server-side.
    # Same fix as test_backfill_availability_uses_a_long_client_timeout
    # above, matches _cmd_backfill_shows's own timeout=3600.0.
    monkeypatch.setattr("sys.argv", ["ops", "audit-local-files"])
    cfg = config.Config(lcars_bearer_token="test-token")
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch("ops.cli.LcarsClient", autospec=True) as lcars_client_cls,
    ):
        instance = lcars_client_cls.return_value
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.audit_local_files = AsyncMock(
            return_value={
                "episodesCorrected": 0,
                "showsCorrected": 0,
                "orphanFiles": [],
                "untrackedShows": [],
            }
        )
        cli.main()
    lcars_client_cls.assert_called_once_with("http://lcars:8000", "test-token", timeout=3600.0)


# --- B.11d: preview-show-backfill / backfill-shows --------------------------

_PREVIEW = [
    {
        "service": "sonarr",
        "title": "New Show",
        "externalId": 12345,
        "trackingSpace": "ANIME",
        "mediaShape": "EPISODIC",
    }
]


def test_preview_show_backfill_requires_bearer_token_first(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ops", "preview-show-backfill"])
    with patch("ops.config.load_config", return_value=config.Config()):
        with pytest.raises(SystemExit, match="lcars_bearer_token"):
            cli.main()


def test_preview_show_backfill_prints_the_list(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ops", "preview-show-backfill"])
    cfg = config.Config(lcars_bearer_token="test-token")
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch(
            "ops.cli.LcarsClient.preview_show_backfill",
            new_callable=AsyncMock,
            return_value=_PREVIEW,
        ) as preview,
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
    ):
        cli.main()
    preview.assert_called_once()
    out = capsys.readouterr().out
    assert "1 untracked show(s) would be created" in out
    assert "[sonarr] New Show (12345) — ANIME/EPISODIC" in out


def test_preview_show_backfill_prints_nothing_to_backfill_when_empty(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ops", "preview-show-backfill"])
    cfg = config.Config(lcars_bearer_token="test-token")
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch("ops.cli.LcarsClient.preview_show_backfill", new_callable=AsyncMock, return_value=[]),
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
    ):
        cli.main()
    out = capsys.readouterr().out
    assert "Nothing to backfill" in out


def test_backfill_shows_requires_bearer_token_first(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ops", "backfill-shows"])
    with patch("ops.config.load_config", return_value=config.Config()):
        with pytest.raises(SystemExit, match="lcars_bearer_token"):
            cli.main()


def test_backfill_shows_does_nothing_to_backfill_when_empty(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ops", "backfill-shows"])
    cfg = config.Config(lcars_bearer_token="test-token")
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch("ops.cli.LcarsClient.preview_show_backfill", new_callable=AsyncMock, return_value=[]),
        patch("ops.cli.LcarsClient.backfill_untracked_shows", new_callable=AsyncMock) as backfill,
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
    ):
        cli.main()
    out = capsys.readouterr().out
    assert "Nothing to backfill" in out
    backfill.assert_not_called()  # never even asked to confirm


def test_backfill_shows_cancels_without_writing_on_a_non_yes_answer(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ops", "backfill-shows"])
    cfg = config.Config(lcars_bearer_token="test-token")
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch(
            "ops.cli.LcarsClient.preview_show_backfill",
            new_callable=AsyncMock,
            return_value=_PREVIEW,
        ),
        patch("ops.cli.LcarsClient.backfill_untracked_shows", new_callable=AsyncMock) as backfill,
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
        patch("builtins.input", return_value="n"),
    ):
        cli.main()
    out = capsys.readouterr().out
    assert "Cancelled — nothing was created" in out
    backfill.assert_not_called()


def test_backfill_shows_confirms_and_calls_the_client(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ops", "backfill-shows"])
    cfg = config.Config(lcars_bearer_token="test-token")
    result = {
        "created": [{"showId": "s-x", "service": "sonarr", "title": "New Show"}],
        "promoted": [],
        "failed": [],
    }
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch(
            "ops.cli.LcarsClient.preview_show_backfill",
            new_callable=AsyncMock,
            return_value=_PREVIEW,
        ),
        patch(
            "ops.cli.LcarsClient.backfill_untracked_shows",
            new_callable=AsyncMock,
            return_value=result,
        ) as backfill,
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
        patch("builtins.input", return_value="yes"),
    ):
        cli.main()
    backfill.assert_called_once()
    out = capsys.readouterr().out
    assert "1 show(s) created" in out
    assert "[sonarr] New Show -> s-x" in out


def test_backfill_shows_prints_promoted_items(monkeypatch, capsys):
    """B.11d follow-up — a stub promoted in place (not a new row) gets
    reported separately from `created`, not silently folded in."""
    monkeypatch.setattr("sys.argv", ["ops", "backfill-shows"])
    cfg = config.Config(lcars_bearer_token="test-token")
    result = {
        "created": [],
        "promoted": [{"showId": "s-z", "service": "anilist", "title": "Existing Stub"}],
        "failed": [],
    }
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch(
            "ops.cli.LcarsClient.preview_show_backfill",
            new_callable=AsyncMock,
            return_value=_PREVIEW,
        ),
        patch(
            "ops.cli.LcarsClient.backfill_untracked_shows",
            new_callable=AsyncMock,
            return_value=result,
        ),
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
        patch("builtins.input", return_value="yes"),
    ):
        cli.main()
    out = capsys.readouterr().out
    assert "0 show(s) created" in out
    assert "1 existing untracked stub(s) promoted in place" in out
    assert "[anilist] Existing Stub -> s-z" in out


def test_backfill_shows_prints_failed_items(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["ops", "backfill-shows"])
    cfg = config.Config(lcars_bearer_token="test-token")
    result = {
        "created": [],
        "promoted": [],
        "failed": [{"service": "sonarr", "title": "Bad Show", "error": "boom"}],
    }
    with (
        patch("ops.config.load_config", return_value=cfg),
        patch(
            "ops.cli.LcarsClient.preview_show_backfill",
            new_callable=AsyncMock,
            return_value=_PREVIEW,
        ),
        patch(
            "ops.cli.LcarsClient.backfill_untracked_shows",
            new_callable=AsyncMock,
            return_value=result,
        ),
        patch("ops.cli.LcarsClient.aclose", new_callable=AsyncMock),
        patch("builtins.input", return_value="yes"),
    ):
        cli.main()
    out = capsys.readouterr().out
    assert "1 item(s) failed" in out
    assert "[sonarr] Bad Show: boom" in out
