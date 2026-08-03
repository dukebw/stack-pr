from __future__ import annotations

import json
from pathlib import Path

import pytest

from stack_pr import cli
from stack_pr.cli import _print_result, create_argparser
from stack_pr.reconcile import Manifest, ManifestLayer, parse_result
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


def workflow_result(action: str) -> WorkflowResult:
    branch = "octocat/stack/11111111111111111111111111111111"
    manifest = Manifest(
        "master", "origin", (ManifestLayer(branch, "Exact subject", "Body", False),)
    )
    reconcile_result = parse_result(
        {
            "version": 1,
            "layers": [
                {
                    "branch": branch,
                    "status": "open",
                    "action": action,
                    "pr": {
                        "number": 42,
                        "url": "https://github.test/pull/42",
                        "head_sha": "abc",
                    },
                }
            ],
            "removed_managed_layers": [],
            "actions": [] if action == "none" else [{"type": action}],
            "blockers": [],
            "stack": {"url": "https://github.test/stacks/1"},
        }
    )
    return WorkflowResult(manifest, reconcile_result)


def test_view_renders_synced_stack_state(capsys: pytest.CaptureFixture[str]) -> None:
    _print_result(workflow_result("none"), json_output=False, exported=False)

    output = capsys.readouterr().out
    assert "Plan (top to bottom):" in output
    assert "synced    #42 Exact subject" in output
    assert output.endswith("Stack is up to date with local commits.\n")


def test_view_renders_pending_stack_state(capsys: pytest.CaptureFixture[str]) -> None:
    _print_result(workflow_result("update"), json_output=False, exported=False)

    output = capsys.readouterr().out
    assert "update    #42 Exact subject" in output
    assert output.endswith(
        "Stack is not up to date; run `stack-pr export` to synchronize it.\n"
    )


def test_export_renders_applied_action_and_synced_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_result(workflow_result("update"), json_output=False, exported=True)

    output = capsys.readouterr().out
    assert "Result (top to bottom):" in output
    assert "updated   #42 Exact subject" in output
    assert "Stack is up to date with local commits." in output
