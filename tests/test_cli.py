from __future__ import annotations

import json
from pathlib import Path

import pytest

from stack_pr import cli
from stack_pr.cli import _print_result, create_argparser
from stack_pr.reconcile import Manifest
from stack_pr.workflow import Options, Progress, WorkflowResult


def test_cli_only_exposes_view_and_export() -> None:
    parser = create_argparser()
    assert parser.parse_args(["view"]).command == "view"
    export = parser.parse_args(
        [
            "export",
            "-B",
            "base",
            "-H",
            "head",
            "-T",
            "master",
            "-R",
            "upstream",
            "--json",
            "--draft",
        ]
    )
    assert export.command == "export"
    assert export.base == "base"
    assert export.head == "head"
    assert export.target == "master"
    assert export.remote == "upstream"
    assert export.json_output
    assert export.draft
    for removed in ("submit", "land", "abandon", "config"):
        with pytest.raises(SystemExit):
            parser.parse_args([removed])


def test_empty_json_uses_reconcile_result_schema(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = WorkflowResult(Manifest("master", "origin", ()), None)

    _print_result(result, json_output=True, exported=False)

    assert json.loads(capsys.readouterr().out) == {
        "version": 1,
        "dry_run": True,
        "layers": [],
        "removed_managed_layers": [],
        "actions": [],
        "blockers": [],
    }


def test_export_progress_uses_stderr_and_json_is_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    progress_values: list[Progress | None] = []

    def fake_export(
        repo: Path, options: Options, *, progress: Progress | None = None
    ) -> WorkflowResult:
        progress_values.append(progress)
        if progress is not None:
            with progress("Publishing 1 layer"):
                pass
        return WorkflowResult(Manifest("master", "origin", ()), None)

    monkeypatch.setattr(cli, "repository_root", lambda path: tmp_path)
    monkeypatch.setattr(cli, "export", fake_export)
    times = iter((10.0, 11.26))
    monkeypatch.setattr(cli, "monotonic", lambda: next(times))

    assert cli.main(["export"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "No commits selected.\n"
    assert captured.err == "Publishing 1 layer... done (1.3s)\n"

    assert cli.main(["export", "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["layers"] == []
    assert captured.err == ""
    assert progress_values[0] is not None
    assert progress_values[1] is None
