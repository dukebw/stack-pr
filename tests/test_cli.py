from __future__ import annotations

import json

import pytest

from stack_pr.cli import _print_result, create_argparser
from stack_pr.reconcile import Manifest
from stack_pr.workflow import WorkflowResult


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
