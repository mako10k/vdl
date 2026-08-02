"""HTML candidates listing does not enqueue without --enqueue."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from vdl.cli import main
from vdl.classifier import ResourceKind
from vdl.scraper import MediaCandidate


def test_get_html_lists_without_enqueue(tmp_path: Path) -> None:
    cfg_home = tmp_path / "vdlhome"
    runner = CliRunner()
    cand = MediaCandidate(
        url="https://cdn.example.com/a.mp4",
        kind="video",
        source="anchor",
        title="A",
    )

    classified = MagicMock(
        kind=ResourceKind.HTML,
        final_url="https://example.com/page",
        content_type="text/html",
    )

    with patch("vdl.classifier.ResourceProbe") as RP:
        RP.return_value.probe.return_value = classified
        with patch("vdl.service.DownloadService") as DS:
            inst = DS.return_value.__enter__.return_value
            inst.fetch_html_candidates.return_value = (
                "https://example.com/page",
                [(cand, None)],
            )
            inst.print_candidates_table = MagicMock()
            result = runner.invoke(
                main,
                ["--home", str(cfg_home), "get", "https://example.com/page"],
            )

    assert result.exit_code == 0, result.output
    assert "job_id=" not in result.output
    inst.fetch_html_candidates.assert_called()
    inst.select_candidate_indices.assert_not_called()


def test_candidates_command_exists() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["candidates", "--help"])
    assert result.exit_code == 0
    assert "enqueue" in result.output


def test_get_help_mentions_enqueue() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["get", "--help"])
    assert result.exit_code == 0
    assert "enqueue" in result.output
