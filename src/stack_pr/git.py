from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import IO, NoReturn


class StackPrError(Exception):
    """A user-actionable stack-pr failure."""


# `merge-tree --write-tree --merge-base` is the replay primitive; it landed in 2.40.
MINIMUM_GIT_VERSION = (2, 40)
GIT_VERSION_RE = re.compile(r"(\d+)\.(\d+)")

IDENTITY_KEY = "stack-pr-branch"
# Scratch ref that `fast-import` writes commit chains to.
REPLAY_REF = "refs/stack-pr/replay"
# `cat-file --batch` header: "<sha> <type> <size>".
BATCH_HEADER_FIELD_COUNT = 3
METADATA_FIELD_COUNT = 9
METADATA_FORMAT = "%H%x00%T%x00%P%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI"
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


def require_git_version(repo: Path) -> None:
    raw = git_output(repo, "version")
    match = GIT_VERSION_RE.search(raw)
    if match is None:
        raise StackPrError(f"could not determine the git version from {raw!r}")
    installed = (int(match.group(1)), int(match.group(2)))
    if installed < MINIMUM_GIT_VERSION:
        raise StackPrError(
            f"git {installed[0]}.{installed[1]} is too old; stack-pr needs "
            f"git {MINIMUM_GIT_VERSION[0]}.{MINIMUM_GIT_VERSION[1]} or newer for "
            "`git merge-tree --write-tree --merge-base`. Upgrade git and retry."
        )


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


def require_remote_target(repo: Path, remote: str, target: str) -> str:
    """Return the SHA of the remote target's tracking ref, which must exist."""
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
    return result.stdout.strip()


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


def _commit_messages(repo: Path, shas: Sequence[str]) -> list[str]:
    """Read raw commit bodies in one process, preserving message bytes exactly."""
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repo,
        input=("\n".join(shas) + "\n").encode(),
        capture_output=True,
        check=True,
    )
    payload = result.stdout
    messages: list[str] = []
    offset = 0
    for _ in shas:
        newline = payload.index(b"\n", offset)
        header = payload[offset:newline].decode()
        fields = header.split()
        if len(fields) != BATCH_HEADER_FIELD_COUNT or fields[1] != "commit":
            raise StackPrError(f"could not read commit object {header!r}")
        size = int(fields[2])
        start = newline + 1
        raw = payload[start : start + size]
        offset = start + size + 1
        _, separator, message = raw.partition(b"\n\n")
        if not separator:
            raise StackPrError(f"commit {fields[0][:12]} has no message")
        messages.append(message.decode())
    return messages


def read_commits(repo: Path, shas: Sequence[str]) -> list[Commit]:
    """Read many commits in two processes, whatever the count."""
    if not shas:
        return []
    rows = git(
        repo,
        "log",
        "--no-walk=unsorted",
        "-z",
        f"--format={METADATA_FORMAT}",
        *shas,
    ).stdout.split("\0")
    if rows and rows[-1] == "":
        rows.pop()
    if len(rows) != len(shas) * METADATA_FIELD_COUNT:
        raise StackPrError(
            f"expected metadata for {len(shas)} commits, "
            f"read {len(rows) // METADATA_FIELD_COUNT}"
        )

    messages = _commit_messages(
        repo, [rows[index * METADATA_FIELD_COUNT] for index in range(len(shas))]
    )
    commits: list[Commit] = []
    for index, message in enumerate(messages):
        fields = rows[index * METADATA_FIELD_COUNT : (index + 1) * METADATA_FIELD_COUNT]
        sha, tree, parents_text, *metadata = fields
        if not tree:
            raise StackPrError(f"commit {sha[:12]} has no tree")
        commits.append(
            Commit(
                sha=sha,
                parents=tuple(parents_text.split()) if parents_text else (),
                tree=tree,
                message=message,
                author_name=metadata[0],
                author_email=metadata[1],
                author_date=metadata[2],
                committer_name=metadata[3],
                committer_email=metadata[4],
                committer_date=metadata[5],
                branch=_parse_identity(message, sha),
            )
        )
    return commits


def read_commit(repo: Path, sha: str) -> Commit:
    return read_commits(repo, [sha])[0]


def load_linear_range(repo: Path, base: str, head: str) -> list[Commit]:
    base_sha = git_output(repo, "rev-parse", base)
    head_sha = git_output(repo, "rev-parse", head)
    if not is_ancestor(repo, base_sha, head_sha):
        raise StackPrError(f"range base {base!r} is not an ancestor of {head!r}")

    shas = git_output(
        repo, "rev-list", "--reverse", "--topo-order", f"{base_sha}..{head_sha}"
    ).splitlines()
    commits = read_commits(repo, shas)
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


def _raw_date(iso_date: str) -> str:
    """Convert a strict ISO 8601 date (`%aI`) to git's raw `<epoch> <+hhmm>`."""
    moment = datetime.fromisoformat(iso_date)
    offset = moment.utcoffset()
    if offset is None:
        raise StackPrError(f"commit date {iso_date!r} has no timezone")
    minutes = int(offset.total_seconds()) // 60
    sign = "-" if minutes < 0 else "+"
    hours, minutes = divmod(abs(minutes), 60)
    return f"{int(moment.timestamp())} {sign}{hours:02d}{minutes:02d}"


def _write_commit_chain(
    repo: Path, parent: str, commits: Sequence[tuple[Commit, str, str]]
) -> list[str]:
    """Write `(source, message, tree)` commits as one parent chain, in one process.

    Identities and dates come from each source commit, so a commit whose tree,
    parent and message are unchanged gets its original SHA back. The chain is
    written to REPLAY_REF, which callers delete when publishing the result.
    """
    stream = bytearray()
    for index, (source, message, tree) in enumerate(commits, 1):
        data = message.encode()
        stream += (
            f"commit {REPLAY_REF}\nmark :{index}\n"
            f"author {source.author_name} <{source.author_email}> "
            f"{_raw_date(source.author_date)}\n"
            f"committer {source.committer_name} <{source.committer_email}> "
            f"{_raw_date(source.committer_date)}\n"
            f"data {len(data)}\n"
        ).encode()
        stream += data
        previous = parent if index == 1 else f":{index - 1}"
        stream += f'\nfrom {previous}\nM 040000 {tree} ""\n\n'.encode()
    for index in range(1, len(commits) + 1):
        stream += f"get-mark :{index}\n".encode()
    stream += b"done\n"
    result = subprocess.run(
        ["git", "fast-import", "--quiet", "--done", "--force"],
        cwd=repo,
        input=bytes(stream),
        capture_output=True,
        check=False,
    )
    shas = result.stdout.decode().split()
    if result.returncode != 0 or len(shas) != len(commits):
        raise StackPrError(
            f"git fast-import failed to write commits: {result.stderr.decode().strip()}"
        )
    return shas


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
    chain: list[tuple[Commit, str, str]] = []
    for commit in commits_to_rewrite:
        branch = assignments.get(commit.sha)
        message = (
            _message_with_identity(commit.message, branch)
            if branch is not None
            else commit.message
        )
        chain.append((commit, message, commit.tree))
    new_tip_shas = _write_commit_chain(repo, parent, chain)
    rewritten = {
        commit.sha: new_sha for commit, new_sha in zip(commits_to_rewrite, new_tip_shas)
    }

    try:
        git(
            repo,
            "update-ref",
            "--stdin",
            input_text=(
                f"update refs/heads/{source_branch} {new_tip_shas[-1]} {source_tip}\n"
                f"delete {REPLAY_REF}\n"
            ),
        )
    except subprocess.CalledProcessError as exc:
        raise StackPrError(
            "source branch changed while identities were inserted"
        ) from exc

    new_shas = [
        rewritten.get(original.sha, original.sha) for original in original_commits
    ]
    updated: list[Commit] = []
    for commit, assigned in zip(read_commits(repo, new_shas), assigned_commits):
        if assigned.branch is None:
            raise StackPrError(f"commit {commit.sha[:12]} has no assigned identity")
        updated.append(commit.with_branch(assigned.branch))
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


class _NulFields:
    """Read NUL-terminated fields from a pipe as they arrive."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._buffer = b""

    def next(self) -> bytes | None:
        while b"\0" not in self._buffer:
            chunk = self._stream.read1(65536)  # type: ignore[attr-defined]
            if not chunk:
                return None
            self._buffer += chunk
        field, _, self._buffer = self._buffer.partition(b"\0")
        return field


def _replay_trees(
    repo: Path, commits: Sequence[Commit], parent: str, target_label: str
) -> list[str]:
    """Replay each commit's change onto the previous result, in one process.

    `merge-tree --merge-base` runs the same three-way merge as `cherry-pick`
    without reading or writing the index or working tree. With `--stdin` it
    answers one merge per input line, so each layer can build on the last.
    """
    process = subprocess.Popen(
        ["git", "merge-tree", "--write-tree", "--stdin"],
        cwd=repo,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None:
        raise StackPrError("git merge-tree did not open its pipes")
    fields = _NulFields(process.stdout)
    trees: list[str] = []
    ours = parent
    try:
        for commit in commits:
            if not commit.parents:
                raise StackPrError(
                    f"commit {commit.sha[:12]} has no parent to replay from"
                )
            process.stdin.write(
                f"{commit.parents[0]} -- {ours} {commit.sha}\n".encode()
            )
            process.stdin.flush()
            status, tree = fields.next(), fields.next()
            if status == b"0":
                _raise_replay_conflict(repo, commit, ours, target_label)
            # A clean merge's record ends with an empty field.
            if status != b"1" or tree is None or fields.next() != b"":
                raise StackPrError(
                    f"git merge-tree failed replaying commit {commit.sha[:12]}"
                )
            ours = tree.decode()
            trees.append(ours)
    finally:
        process.stdin.close()
        process.stdout.close()
        stderr = process.stderr.read().decode() if process.stderr else ""
        if process.wait() != 0 and len(trees) == len(commits):
            raise StackPrError(f"git merge-tree failed: {stderr.strip()}")
    return trees


def _raise_replay_conflict(
    repo: Path, commit: Commit, parent: str, target_label: str
) -> NoReturn:
    """Re-run one conflicted replay on its own to report its conflict details."""
    result = git(
        repo,
        "merge-tree",
        "--write-tree",
        f"--merge-base={commit.parents[0]}",
        parent,
        commit.sha,
        check=False,
    )
    lines = result.stdout.splitlines()
    detail = (
        "\n".join(line for line in lines[1:] if line.strip())
        or result.stderr.strip()
        or "patch did not apply"
    )
    raise StackPrError(
        f"could not replay commit {commit.sha[:12]} onto {target_label}: {detail}"
    )


def _local_branch_names(repo: Path) -> set[str]:
    listed = git_output(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
    )
    return set(listed.splitlines())


def _update_refs(repo: Path, updates: Sequence[tuple[str, str | None]]) -> None:
    if not updates:
        return
    lines = [
        f"delete {ref}" if value is None else f"update {ref} {value}"
        for ref, value in updates
    ]
    git(repo, "update-ref", "--stdin", input_text="\n".join(lines) + "\n")


@contextmanager
def materialize_layers(
    repo: Path,
    commits: Sequence[Commit],
    *,
    merged_branches: set[str],
    remote: str,
    target: str,
) -> Iterator[None]:
    """Publish active layers as local branches replayed onto the target.

    Layer trees come from `merge-tree` and commits from `fast-import`, so the
    caller's checkout, index and working tree are never touched.
    """
    active = [commit for commit in commits if commit.branch not in merged_branches]
    existing = _local_branch_names(repo)
    for commit in active:
        if commit.branch is None:
            raise StackPrError(f"commit {commit.sha[:12]} has no branch identity")
        if commit.branch in existing:
            raise StackPrError(
                f"temporary layer branch {commit.branch!r} already exists locally"
            )

    target_label = f"{remote}/{target}"
    parent = git_output(repo, "rev-parse", target_label)
    created: list[tuple[str, str | None]] = []
    try:
        if active:
            trees = _replay_trees(repo, active, parent, target_label)
            shas = _write_commit_chain(
                repo,
                parent,
                [(commit, commit.message, tree) for commit, tree in zip(active, trees)],
            )
            replayed = [
                (f"refs/heads/{commit.branch}", sha)
                for commit, sha in zip(active, shas)
            ]
            _update_refs(repo, [*replayed, (REPLAY_REF, None)])
            created = [(ref, None) for ref, _value in replayed]
        yield
    finally:
        _update_refs(repo, created)
