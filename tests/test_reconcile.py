from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from stack_pr.git import StackPrError
from stack_pr.reconcile import (
    Manifest,
    ManifestLayer,
    parse_result,
    preflight,
    reconcile,
)


def test_manifest_json_is_exact() -> None:
    manifest = Manifest(
        base="master",
        remote="origin",
        layers=(
            ManifestLayer(
                branch="octocat/stack/11111111111111111111111111111111",
                title="Exact subject",
                body="Exact body\nwith lines",
                draft=False,
            ),
        ),
    )
    assert json.loads(manifest.to_json()) == {
        "version": 1,
        "base": "master",
        "remote": "origin",
        "layers": [
            {
                "branch": "octocat/stack/11111111111111111111111111111111",
                "title": "Exact subject",
                "body": "Exact body\nwith lines",
                "draft": False,
            }
        ],
    }


def test_reconcile_subprocess_uses_native_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        input_value = kwargs.get("input")
        calls.append((args, input_value if isinstance(input_value, str) else None))
        if args[:2] == ["git", "version"]:
            return subprocess.CompletedProcess(args, 0, "git version 2.54.0\n", "")
        if args[-1] == "--protocol-version":
            return subprocess.CompletedProcess(
                args,
                0,
                "1\n",
                "",
            )
        output = json.dumps(
            {
                "version": 1,
                "layers": [
                    {
                        "branch": "octocat/stack/11111111111111111111111111111111",
                        "status": "create",
                    }
                ],
            }
        )
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = Manifest(
        "master",
        "origin",
        (
            ManifestLayer(
                "octocat/stack/11111111111111111111111111111111",
                "Title",
                "",
                False,
            ),
        ),
    )
    preflight(tmp_path)
    result = reconcile(tmp_path, manifest, dry_run=True)

    replayed = [
        (args, payload)
        for args, payload in calls
        if args[:3] == ["gh", "stack", "reconcile"] and "--input" in args
    ]
    assert [args for args, _payload in replayed] == [
        ["gh", "stack", "reconcile", "--input", "-", "--json", "--dry-run"]
    ]
    assert json.loads(replayed[0][1] or "") == manifest.to_dict()
    assert result.layers[0].status == "create"


def test_reconcile_rejects_wrong_protocol_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, '{"version":3,"layers":[]}', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(StackPrError, match="unsupported protocol version"):
        reconcile(tmp_path, Manifest("master", "origin", ()), dry_run=True)


def test_parse_result_uses_native_queued_status_and_reopen_action() -> None:
    queued = "octocat/stack/11111111111111111111111111111111"
    reopened = "octocat/stack/22222222222222222222222222222222"
    result = parse_result(
        {
            "version": 1,
            "layers": [
                {
                    "branch": queued,
                    "status": "queued",
                    "action": "none",
                    "pr": {"number": 10, "queued": True},
                },
                {
                    "branch": reopened,
                    "status": "closed",
                    "action": "reopen",
                    "pr": {"number": 11, "queued": False},
                },
            ],
            "blockers": [
                {
                    "branch": queued,
                    "message": "queued PR #10 blocks a structural change",
                }
            ],
        }
    )

    assert result.layers[0].status == "queued"
    assert result.layers[0].blocked_reason == (
        "queued PR #10 blocks a structural change"
    )
    assert result.layers[1].status == "reopen"
    assert result.layers[1].number == 11


def test_parse_result_includes_removed_managed_layers() -> None:
    branch = "octocat/stack/11111111111111111111111111111111"

    result = parse_result(
        {
            "version": 1,
            "layers": [],
            "removed_managed_layers": [
                {
                    "branch": branch,
                    "status": "open",
                    "action": "remove",
                    "pr": {"number": 10, "url": "https://example.test/pull/10"},
                }
            ],
        }
    )

    assert len(result.removed_layers) == 1
    assert result.removed_layers[0].branch == branch
    assert result.removed_layers[0].status == "remove"
    assert result.removed_layers[0].number == 10


def test_blocked_dry_run_returns_structured_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    branch = "octocat/stack/11111111111111111111111111111111"

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        output = json.dumps(
            {
                "version": 1,
                "dry_run": True,
                "layers": [
                    {
                        "branch": branch,
                        "status": "queued",
                        "action": "blocked",
                    }
                ],
                "blockers": [{"branch": branch, "message": "queued"}],
            }
        )
        return subprocess.CompletedProcess(args, 5, output, "queued\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = reconcile(
        tmp_path,
        Manifest("master", "origin", (ManifestLayer(branch, "Title", "", False),)),
        dry_run=True,
    )

    assert result.layers[0].status == "queued"
    assert result.blockers == ["queued"]


def test_preflight_rejects_extension_without_protocol_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["git", "version"]:
            return subprocess.CompletedProcess(args, 0, "git version 2.54.0\n", "")
        return subprocess.CompletedProcess(args, 0, "dev\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(StackPrError, match="does not support"):
        preflight(tmp_path)


@pytest.mark.parametrize(
    ("reported", "allowed"),
    [
        ("git version 2.39.5 (Apple Git-154)", False),
        ("git version 2.40.0", True),
        ("git version 2.54.0 (Apple Git-157)", True),
    ],
)
def test_preflight_gates_on_git_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reported: str,
    allowed: bool,
) -> None:
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = reported + "\n" if args[:2] == ["git", "version"] else "1\n"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    if allowed:
        preflight(tmp_path)
    else:
        with pytest.raises(StackPrError, match="too old"):
            preflight(tmp_path)
