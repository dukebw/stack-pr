from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path


class StackPrError(Exception):
    """A user-actionable stack-pr failure."""


IDENTITY_KEY = "stack-pr-branch"
COMMIT_METADATA_FIELD_COUNT = 6
IDENTITY_VALUE_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/stack/[0-9a-f]{32}"
)
IDENTITY_LINE_RE = re.compile(rf"^{IDENTITY_KEY}: (?P<branch>.+)$", re.MULTILINE)
IDENTITY_KEY_LINE_RE = re.compile(rf"^{IDENTITY_KEY}:[^\n]*$", re.MULTILINE)
LEGACY_LINE_RE = re.compile(r"^stack-info:[^\n]*$", re.MULTILINE)


@dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    tree: str
    message: str
    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    committer_date: str
    branch: str | None = None

    @property
    def subject(self) -> str:
        return self.message.partition("\n")[0]

    @property
    def body(self) -> str:
        _, separator, body = self.message.partition("\n")
        if not separator:
            return ""
        if body.startswith("\n"):
            body = body[1:]
        if body.endswith("\n"):
            body = body[:-1]
        if self.branch is not None:
            trailer = f"{IDENTITY_KEY}: {self.branch}"
            if body == trailer:
                return ""
            suffix = f"\n\n{trailer}"
            if body.endswith(suffix):
                body = body[: -len(suffix)]
        return body

    def with_branch(self, branch: str) -> Commit:
        return replace(self, branch=branch)


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        env=env,
    )


def git(
    repo: Path,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["git", *args],
        cwd=repo,
        input_text=input_text,
        check=check,
        env=env,
    )


def git_output(repo: Path, *args: str) -> str:
    return git(repo, *args).stdout.rstrip("\n")


def repository_root(path: Path) -> Path:
    try:
        return Path(git_output(path, "rev-parse", "--show-toplevel"))
    except subprocess.CalledProcessError as exc:
        raise StackPrError("stack-pr must run inside a Git repository") from exc


def current_branch(repo: Path) -> str:
    result = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode != 0:
        raise StackPrError("export requires a checked-out source branch")
    return result.stdout.strip()


def current_head(repo: Path) -> str:
    return git_output(repo, "rev-parse", "HEAD")


def require_clean_worktree(repo: Path) -> None:
    if git_output(repo, "status", "--porcelain", "--untracked-files=normal"):
        raise StackPrError("export requires a clean working tree")


def fetch_remote(repo: Path, remote: str) -> None:
    try:
        git(repo, "fetch", "--prune", remote)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or str(exc)
        raise StackPrError(f"failed to fetch remote {remote!r}: {detail}") from exc


def infer_target(repo: Path, remote: str) -> str:
    symbolic = git(
        repo,
        "symbolic-ref",
        "--quiet",
        "--short",
        f"refs/remotes/{remote}/HEAD",
        check=False,
    )
    if symbolic.returncode == 0:
        prefix = f"{remote}/"
        value = symbolic.stdout.strip()
        if value.startswith(prefix):
            return value[len(prefix) :]

    remote_head = git(repo, "ls-remote", "--symref", remote, "HEAD", check=False)
    if remote_head.returncode == 0:
        for line in remote_head.stdout.splitlines():
            match = re.match(r"ref: refs/heads/(.+)\s+HEAD$", line)
            if match is not None:
                return match.group(1)
    raise StackPrError(
        f"could not infer the target from {remote!r} HEAD; pass --target"
    )


def require_remote_target(repo: Path, remote: str, target: str) -> None:
    result = git(
        repo,
        "rev-parse",
        "--verify",
        "--quiet",
        f"refs/remotes/{remote}/{target}",
        check=False,
    )
    if result.returncode != 0:
        raise StackPrError(f"remote target {remote}/{target} does not exist locally")


def merge_base(repo: Path, head: str, remote: str, target: str) -> str:
    try:
        return git_output(repo, "merge-base", head, f"{remote}/{target}")
    except subprocess.CalledProcessError as exc:
        raise StackPrError(
            f"could not find a merge base between {head} and {remote}/{target}"
        ) from exc


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = git(
        repo,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise StackPrError(
            f"could not compare Git revisions {ancestor!r} and {descendant!r}"
        )
    return result.returncode == 0


def _parse_identity(message: str, sha: str) -> str | None:
    if LEGACY_LINE_RE.search(message) is not None:
        raise StackPrError(
            f"commit {sha[:12]} has a legacy stack-info trailer; migration is not supported"
        )

    key_lines = IDENTITY_KEY_LINE_RE.findall(message)
    if len(key_lines) > 1:
        raise StackPrError(f"commit {sha[:12]} has multiple {IDENTITY_KEY} trailers")
    if not key_lines:
        return None

    match = IDENTITY_LINE_RE.search(message)
    if match is None or IDENTITY_VALUE_RE.fullmatch(match.group("branch")) is None:
        raise StackPrError(f"commit {sha[:12]} has an invalid {IDENTITY_KEY} trailer")
    branch = match.group("branch")
    terminal = f"\n\n{IDENTITY_KEY}: {branch}"
    if not message.rstrip("\n").endswith(terminal):
        raise StackPrError(
            f"commit {sha[:12]} must have {IDENTITY_KEY} as its final trailer"
        )
    return branch


def read_commit(repo: Path, sha: str) -> Commit:
    resolved_sha = git_output(repo, "rev-parse", sha)
    raw = git(repo, "cat-file", "commit", resolved_sha).stdout
    header, separator, message = raw.partition("\n\n")
    if not separator:
        raise StackPrError(f"commit {resolved_sha[:12]} has no message")

    tree = ""
    parents: list[str] = []
    for line in header.splitlines():
        if line.startswith("tree "):
            tree = line.removeprefix("tree ")
        elif line.startswith("parent "):
            parents.append(line.removeprefix("parent "))
    if not tree:
        raise StackPrError(f"commit {resolved_sha[:12]} has no tree")

    metadata = git_output(
        repo,
        "show",
        "-s",
        "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI",
        resolved_sha,
    ).split("\0")
    if len(metadata) != COMMIT_METADATA_FIELD_COUNT:
        raise StackPrError(f"could not read metadata for commit {resolved_sha[:12]}")

    return Commit(
        sha=resolved_sha,
        parents=tuple(parents),
        tree=tree,
        message=message,
        author_name=metadata[0],
        author_email=metadata[1],
        author_date=metadata[2],
        committer_name=metadata[3],
        committer_email=metadata[4],
        committer_date=metadata[5],
        branch=_parse_identity(message, resolved_sha),
    )


def load_linear_range(repo: Path, base: str, head: str) -> list[Commit]:
    base_sha = git_output(repo, "rev-parse", base)
    head_sha = git_output(repo, "rev-parse", head)
    if not is_ancestor(repo, base_sha, head_sha):
        raise StackPrError(f"range base {base!r} is not an ancestor of {head!r}")

    shas = git_output(
        repo, "rev-list", "--reverse", "--topo-order", f"{base_sha}..{head_sha}"
    ).splitlines()
    commits = [read_commit(repo, sha) for sha in shas]
    expected_parent = base_sha
    for commit in commits:
        if commit.parents != (expected_parent,):
            raise StackPrError(
                "selected history must be linear and contain no merge commits; "
                f"commit {commit.sha[:12]} has {len(commit.parents)} parents"
            )
        expected_parent = commit.sha
    validate_unique_identities(commits)
    return commits


def validate_unique_identities(commits: Sequence[Commit]) -> None:
    seen: dict[str, str] = {}
    for commit in commits:
        if commit.branch is None:
            continue
        previous = seen.get(commit.branch)
        if previous is not None:
            raise StackPrError(
                f"commits {previous[:12]} and {commit.sha[:12]} share branch identity "
                f"{commit.branch!r}"
            )
        seen[commit.branch] = commit.sha


def assign_missing_identities(
    commits: Sequence[Commit], login: str, branch_ids: Iterator[str]
) -> list[Commit]:
    assigned: list[Commit] = []
    for commit in commits:
        if commit.branch is not None:
            assigned.append(commit)
            continue
        branch_id = next(branch_ids)
        if re.fullmatch(r"[0-9a-f]{32}", branch_id) is None:
            raise StackPrError(
                "generated branch identity is not 32 lowercase hex characters"
            )
        assigned.append(commit.with_branch(f"{login}/stack/{branch_id}"))
    validate_unique_identities(assigned)
    return assigned


def _commit_tree(
    repo: Path,
    source: Commit,
    parent: str,
    message: str,
    *,
    tree: str | None = None,
) -> str:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": source.author_name,
            "GIT_AUTHOR_EMAIL": source.author_email,
            "GIT_AUTHOR_DATE": source.author_date,
            "GIT_COMMITTER_NAME": source.committer_name,
            "GIT_COMMITTER_EMAIL": source.committer_email,
            "GIT_COMMITTER_DATE": source.committer_date,
        }
    )
    return git_output_with_input(
        repo,
        "commit-tree",
        tree or source.tree,
        "-p",
        parent,
        "-F",
        "-",
        input_text=message,
        env=env,
    )


def git_output_with_input(
    repo: Path,
    *args: str,
    input_text: str,
    env: Mapping[str, str] | None = None,
) -> str:
    return git(repo, *args, input_text=input_text, env=env).stdout.rstrip("\n")


def _message_with_identity(message: str, branch: str) -> str:
    if not message.endswith("\n"):
        message += "\n"
    if not message.endswith("\n\n"):
        message += "\n"
    return f"{message}{IDENTITY_KEY}: {branch}\n"


def insert_missing_identities(
    repo: Path,
    original_commits: Sequence[Commit],
    assigned_commits: Sequence[Commit],
    *,
    source_branch: str,
    source_tip: str,
) -> list[Commit]:
    if len(original_commits) != len(assigned_commits):
        raise StackPrError("identity assignment count does not match selected commits")
    assignments = {
        original.sha: assigned.branch
        for original, assigned in zip(original_commits, assigned_commits)
        if original.branch is None
    }
    if not assignments:
        return list(assigned_commits)
    if not original_commits:
        return []
    selected_head = original_commits[-1].sha
    if not is_ancestor(repo, selected_head, source_tip):
        raise StackPrError(
            "selected head must be an ancestor of the checked-out branch"
        )

    first_missing_index = next(
        index
        for index, commit in enumerate(original_commits)
        if commit.sha in assignments
    )
    first_missing = original_commits[first_missing_index]
    parent = first_missing.parents[0]
    commits_to_rewrite = load_linear_range(repo, parent, source_tip)
    rewritten: dict[str, str] = {}

    for commit in commits_to_rewrite:
        branch = assignments.get(commit.sha)
        message = (
            _message_with_identity(commit.message, branch)
            if branch is not None
            else commit.message
        )
        new_sha = _commit_tree(repo, commit, parent, message)
        rewritten[commit.sha] = new_sha
        parent = new_sha

    try:
        git(
            repo,
            "update-ref",
            f"refs/heads/{source_branch}",
            parent,
            source_tip,
        )
    except subprocess.CalledProcessError as exc:
        raise StackPrError(
            "source branch changed while identities were inserted"
        ) from exc

    updated: list[Commit] = []
    for original, assigned in zip(original_commits, assigned_commits):
        new_sha = rewritten.get(original.sha, original.sha)
        if assigned.branch is None:
            raise StackPrError(f"commit {original.sha[:12]} has no assigned identity")
        updated.append(read_commit(repo, new_sha).with_branch(assigned.branch))
    validate_unique_identities(updated)
    return updated


def check_source_upstream(repo: Path, source_branch: str, fetched_remote: str) -> None:
    upstream = git(
        repo,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        check=False,
    )
    if upstream.returncode != 0:
        return

    upstream_remote = git(
        repo, "config", "--get", f"branch.{source_branch}.remote", check=False
    ).stdout.strip()
    if upstream_remote and upstream_remote not in (".", fetched_remote):
        fetch_remote(repo, upstream_remote)

    upstream_only = git_output(repo, "cherry", "HEAD", "@{upstream}").splitlines()
    if not any(line.startswith("+") for line in upstream_only):
        return
    raise StackPrError(
        f"checked-out source branch {source_branch!r} is missing changes from its "
        f"upstream {upstream.stdout.strip()!r}"
    )


def _require_commit_object(repo: Path, sha: str, remote: str) -> None:
    exists = git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False)
    if exists.returncode == 0:
        return
    fetched = git(repo, "fetch", "--no-tags", remote, sha, check=False)
    if fetched.returncode != 0:
        raise StackPrError(
            f"could not fetch merged layer commit {sha[:12]} from {remote}"
        )


def commit_patch_id(repo: Path, sha: str) -> str:
    patch = git(
        repo,
        "show",
        "--pretty=format:",
        "--binary",
        "--no-ext-diff",
        sha,
    ).stdout
    result = git(repo, "patch-id", "--stable", input_text=patch)
    patch_id = result.stdout.partition(" ")[0].strip()
    if not patch_id:
        raise StackPrError(f"could not compute patch identity for commit {sha[:12]}")
    return patch_id


def require_merged_patch_unchanged(
    repo: Path, commit: Commit, merged_head_sha: str | None, remote: str
) -> None:
    if not merged_head_sha:
        raise StackPrError(
            f"native reconciliation returned no head SHA for merged layer "
            f"{commit.branch!r}"
        )
    _require_commit_object(repo, merged_head_sha, remote)
    if commit_patch_id(repo, commit.sha) != commit_patch_id(repo, merged_head_sha):
        raise StackPrError(
            f"commit {commit.sha[:12]} changes merged layer {commit.branch!r}; "
            f"remove its {IDENTITY_KEY} trailer to export the change as a new PR"
        )


def _local_branch_exists(repo: Path, branch: str) -> bool:
    return (
        git(
            repo,
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            check=False,
        ).returncode
        == 0
    )


@contextmanager
def materialize_layers(
    repo: Path,
    commits: Sequence[Commit],
    *,
    merged_branches: set[str],
    remote: str,
    target: str,
    source_branch: str,
) -> Iterator[None]:
    active = [commit for commit in commits if commit.branch not in merged_branches]
    for commit in active:
        if commit.branch is None:
            raise StackPrError(f"commit {commit.sha[:12]} has no branch identity")
        if _local_branch_exists(repo, commit.branch):
            raise StackPrError(
                f"temporary layer branch {commit.branch!r} already exists locally"
            )

    created: list[str] = []
    detached = False
    try:
        git(repo, "checkout", "--detach", f"{remote}/{target}")
        detached = True
        for commit in active:
            branch = commit.branch
            if branch is None:
                raise StackPrError(f"commit {commit.sha[:12]} has no branch identity")
            cherry_pick = git(
                repo, "cherry-pick", "--no-commit", commit.sha, check=False
            )
            if cherry_pick.returncode != 0:
                detail = cherry_pick.stderr.strip() or "patch did not apply"
                raise StackPrError(
                    f"could not replay commit {commit.sha[:12]} onto {remote}/{target}: "
                    f"{detail}"
                )
            tree = git_output(repo, "write-tree")
            parent = current_head(repo)
            materialized_sha = _commit_tree(
                repo, commit, parent, commit.message, tree=tree
            )
            git(repo, "reset", "--hard", materialized_sha)
            git(repo, "update-ref", f"refs/heads/{branch}", materialized_sha)
            created.append(branch)

        git(repo, "checkout", source_branch)
        detached = False
        yield
    finally:
        git(repo, "cherry-pick", "--abort", check=False)
        if detached or current_branch_if_attached(repo) != source_branch:
            git(repo, "reset", "--hard", check=False)
            git(repo, "checkout", source_branch, check=False)
        for branch in created:
            git(repo, "update-ref", "-d", f"refs/heads/{branch}", check=False)


def current_branch_if_attached(repo: Path) -> str | None:
    result = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None
