from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@dataclass
class Repo:
    path: Path
    remote: Path
    root: str

    def git(
        self, *args: str, input_text: str | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.path,
            input=input_text,
            text=True,
            capture_output=True,
            check=check,
        )

    def output(self, *args: str) -> str:
        return self.git(*args).stdout.rstrip("\n")

    def commit_file(self, name: str, content: str, message: str) -> str:
        file_path = self.path / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        self.git("add", name)
        self.git("commit", "--no-gpg-sign", "-F", "-", input_text=message)
        return self.output("rev-parse", "HEAD")

    def commit_message(self, message: str) -> str:
        self.git(
            "commit",
            "--allow-empty",
            "--no-gpg-sign",
            "-F",
            "-",
            input_text=message,
        )
        return self.output("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=master", str(remote)],
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "init", "--initial-branch=master", str(work)],
        text=True,
        capture_output=True,
        check=True,
    )
    result = Repo(work, remote, "")
    result.git("config", "user.name", "Stack PR Test")
    result.git("config", "user.email", "stack-pr@example.com")
    result.git("config", "commit.gpgSign", "false")
    result.git("remote", "add", "origin", str(remote))
    root = result.commit_file("root.txt", "root\n", "Root\n")
    result.root = root
    result.git("push", "-u", "origin", "master")
    result.git("remote", "set-head", "origin", "master")
    result.git("switch", "-c", "feature")
    return result
