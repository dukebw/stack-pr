from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from stack_pr.reconcile import Manifest, ManifestLayer, parse_result

SHIM = Path(__file__).parent.parent / "benchmarks" / "gh_shim.py"
BRANCH = "benchlogin/stack/11111111111111111111111111111111"


def _invoke(manifest: Manifest, *, status: str) -> object:
    result = subprocess.run(
        [
            sys.executable,
            "-E",
            str(SHIM),
            "stack",
            "reconcile",
            "--input",
            "-",
            "--json",
        ],
        input=manifest.to_json(),
        text=True,
        capture_output=True,
        check=True,
        env={"SP_GH_STATUS": status, "PATH": "/usr/bin:/bin"},
    )
    return json.loads(result.stdout)


def _manifest() -> Manifest:
    return Manifest(
        "master",
        "origin",
        (ManifestLayer(BRANCH, "Title", "Body", draft=False),),
    )


def test_shim_result_parses_as_reconcile_protocol() -> None:
    parsed = parse_result(_invoke(_manifest(), status="create"))

    assert parsed.layers[0].branch == BRANCH
    assert parsed.layers[0].status == "create"
    assert parsed.blockers == []
    assert parsed.has_native_stack


def test_shim_reports_blockers() -> None:
    parsed = parse_result(_invoke(_manifest(), status="blocked"))

    assert parsed.layers == ()
    assert parsed.blockers == ["desired pull requests belong to multiple stacks"]


def test_shim_reports_protocol_version() -> None:
    result = subprocess.run(
        [sys.executable, "-E", str(SHIM), "stack", "reconcile", "--protocol-version"],
        text=True,
        capture_output=True,
        check=True,
        env={"PATH": "/usr/bin:/bin"},
    )

    assert result.stdout.strip() == "2"


def test_shim_answers_login_lookup() -> None:
    result = subprocess.run(
        [sys.executable, "-E", str(SHIM), "api", "user", "--jq", ".login"],
        text=True,
        capture_output=True,
        check=True,
        env={"SP_GH_LOGIN": "someone", "PATH": "/usr/bin:/bin"},
    )

    assert result.stdout.strip() == "someone"
