"""Build disposable stack-pr fixtures for benchmarking.

A fixture is a working clone plus a local bare remote, so `fetch` costs nothing
and the measured time is stack-pr's own work. Shape is parameterised by layer
count (drives per-layer git work) and filler file count (drives working-tree
checkout cost, which is what `view`'s disposable worktree and `export`'s
`reset --hard` actually pay for).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

LOGIN = "benchlogin"

# Fixed timestamps keep generated commits byte-identical between fixture builds.
_GIT_ENV = {
    "GIT_AUTHOR_DATE": "2020-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+00:00",
}


@dataclass(frozen=True)
class Fixture:
    root: Path
    repo: Path
    remote: Path
    bin_dir: Path
    state_dir: Path
    layers: int
    fillers: int
    feature_tip: str

    @property
    def call_log(self) -> Path:
        return self.state_dir / "gh-calls.jsonl"

    @property
    def trace(self) -> Path:
        return self.state_dir / "trace.jsonl"

    def clear_state(self) -> None:
        for path in (self.call_log, self.trace):
            path.unlink(missing_ok=True)

    def restore(self) -> None:
        """Return the clone to its post-build state (export rewrites commits)."""
        if self.git("symbolic-ref", "--quiet", "--short", "HEAD") != "feature":
            _run(["git", "checkout", "-f", "-q", "feature"], cwd=self.repo)
        _run(
            ["git", "update-ref", "refs/heads/feature", self.feature_tip], cwd=self.repo
        )
        _run(["git", "reset", "-q", "--hard", self.feature_tip], cwd=self.repo)
        _run(["git", "clean", "-fdq"], cwd=self.repo)
        stray = self.git(
            "for-each-ref", "--format=%(refname)", f"refs/heads/{LOGIN}/"
        ).splitlines()
        for ref in stray:
            _run(["git", "update-ref", "-d", ref], cwd=self.repo)

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **_GIT_ENV},
        )
        return result.stdout.strip()


def _run(args: list[str], *, cwd: Path | None = None, input_text: str = "") -> None:
    subprocess.run(
        args,
        cwd=cwd,
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )


def layer_branch(index: int) -> str:
    digest = hashlib.sha256(f"bench-layer-{index}".encode()).hexdigest()[:32]
    return f"{LOGIN}/stack/{digest}"


def build(
    root: Path,
    *,
    layers: int,
    fillers: int,
    trailers: bool,
    python: Path,
    shim: Path,
) -> Fixture:
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    remote = root / "remote.git"
    repo = root / "repo"
    state_dir = root / "state"
    state_dir.mkdir()

    _run(["git", "init", "--bare", "--initial-branch=master", str(remote)])
    _run(["git", "init", "--initial-branch=master", str(repo)])
    for key, value in (
        ("user.name", "Stack PR Bench"),
        ("user.email", "bench@example.com"),
        ("commit.gpgSign", "false"),
        ("advice.detachedHead", "false"),
    ):
        _run(["git", "config", key, value], cwd=repo)
    _run(["git", "remote", "add", "origin", str(remote)], cwd=repo)

    for index in range(fillers):
        (repo / f"filler-{index:05d}.txt").write_text(f"{index}\n")
    (repo / "root.txt").write_text("root\n")
    _run(["git", "add", "-A"], cwd=repo)
    _run(["git", "commit", "--no-gpg-sign", "-m", f"Seed {fillers} files"], cwd=repo)
    _run(["git", "push", "-q", "-u", "origin", "master"], cwd=repo)
    _run(["git", "remote", "set-head", "origin", "master"], cwd=repo)
    _run(["git", "switch", "-q", "-c", "feature"], cwd=repo)

    for index in range(layers):
        (repo / f"layer-{index:02d}.txt").write_text(f"layer {index}\n")
        message = f"Layer {index} subject\n\nLayer {index} body.\n"
        if trailers:
            message += f"\nstack-pr-branch: {layer_branch(index)}\n"
        _run(["git", "add", "-A"], cwd=repo)
        _run(
            ["git", "commit", "--no-gpg-sign", "-F", "-"], cwd=repo, input_text=message
        )

    bin_dir = root / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    # -E keeps the subprocess tracer off the shim's own interpreter.
    gh.write_text(f'#!/bin/sh\nexec "{python}" -E "{shim}" "$@"\n')
    gh.chmod(0o755)

    tip = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    return Fixture(root, repo, remote, bin_dir, state_dir, layers, fillers, tip)
