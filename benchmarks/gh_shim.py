"""Stand-in for the `gh` CLI, used by the tier-1 benchmark.

Answers exactly the calls stack-pr makes, mirroring the reconcile protocol the
real `dukebw/gh-stack` extension speaks. The real extension makes one GraphQL
round trip per layer; `SP_GH_LAYER_DELAY_MS` reproduces that serial cost without
touching the network.

Env:
    SP_GH_STATUS          "create" (unblocked) or "blocked"
    SP_GH_LOGIN           login returned by `gh api user`
    SP_GH_LAYER_DELAY_MS  milliseconds slept per layer per reconcile call
    SP_GH_CALL_LOG        path to append one JSON record per invocation
    SP_GH_VERIFY          "1" to record which layer branches exist in the cwd
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROTOCOL_VERSION = 1


def _record(entry: dict[str, object]) -> None:
    path = os.environ.get("SP_GH_CALL_LOG")
    if not path:
        return
    with Path(path).open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def _branches_present(branches: list[str]) -> dict[str, bool]:
    present = {}
    for branch in branches:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
            check=False,
        )
        present[branch] = result.returncode == 0
    return present


def _blocked_result() -> dict[str, object]:
    return {
        "version": PROTOCOL_VERSION,
        "dry_run": True,
        "layers": [],
        "removed_managed_layers": [],
        "actions": [],
        "blockers": [
            {
                "code": "multiple_stacks",
                "message": "desired pull requests belong to multiple stacks",
            }
        ],
        "stack": None,
    }


def _layered_result(manifest: dict[str, object]) -> dict[str, object]:
    layers = manifest.get("layers") or []
    if not isinstance(layers, list):
        raise SystemExit("manifest layers must be a list")
    built = []
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict):
            raise SystemExit("manifest layer must be an object")
        branch = layer["branch"]
        number = index + 10
        built.append(
            {
                "branch": branch,
                "status": "create",
                "action": "create",
                "number": number,
                "url": f"https://github.test/pull/{number}",
                "pr": {
                    "number": number,
                    "url": f"https://github.test/pull/{number}",
                    "head_sha": "",
                },
            }
        )
    return {
        "version": PROTOCOL_VERSION,
        "dry_run": True,
        "layers": built,
        "removed_managed_layers": [],
        "actions": [{"type": "create", "branch": entry["branch"]} for entry in built],
        "blockers": [],
        "stack": {"url": "https://github.test/stacks/1"},
    }


def _reconcile(args: list[str]) -> int:
    delay_ms = int(os.environ.get("SP_GH_LAYER_DELAY_MS", "0"))
    manifest: dict[str, object] = {}
    if "--input" in args:
        raw = sys.stdin.read()
        if raw.strip():
            manifest = json.loads(raw)
    raw_layers = manifest.get("layers")
    layer_count = len(raw_layers) if isinstance(raw_layers, list) else 0
    if delay_ms:
        time.sleep(delay_ms / 1000 * layer_count)

    status = os.environ.get("SP_GH_STATUS", "create")
    if status == "blocked":
        payload = _blocked_result()
    elif status == "create":
        payload = _layered_result(manifest)
    else:
        raise SystemExit(f"unknown SP_GH_STATUS {status!r}")

    entry = {
        "dry_run": "--dry-run" in args,
        "layers": layer_count,
        "delay_ms": delay_ms,
    }
    if os.environ.get("SP_GH_VERIFY") == "1" and isinstance(raw_layers, list):
        branches = [layer["branch"] for layer in raw_layers if isinstance(layer, dict)]
        entry["branches_present"] = _branches_present(branches)
    _record(entry)
    print(json.dumps(payload))
    return 0


def main(argv: list[str]) -> int:
    if argv[:2] == ["stack", "reconcile"]:
        if "--protocol-version" in argv:
            print(PROTOCOL_VERSION)
            return 0
        return _reconcile(argv)
    if argv[:2] == ["api", "user"]:
        print(os.environ.get("SP_GH_LOGIN", "benchlogin"))
        return 0
    raise SystemExit(f"gh shim does not implement: {argv}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
