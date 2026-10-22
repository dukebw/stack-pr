from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from stack_pr.git import (
    Commit,
    ReplayConflictError,
    StackPrError,
    assign_missing_identities,
    check_source_upstream,
    current_branch,
    current_head,
    fetch_remote,
    infer_target,
    insert_missing_identities,
    load_linear_range,
    materialize_layers,
    merge_base,
    require_clean_worktree,
    require_merged_patch_unchanged,
    require_remote_target,
    tracking_refspec,
    upstream_refspec,
)
from stack_pr.reconcile import (
    ASSUME_UNMERGED_VERSION,
    Manifest,
    ManifestLayer,
    ReconcileResult,
    github_login,
    preflight,
    reconcile,
)

Progress = Callable[[str], AbstractContextManager[None]]


@contextmanager
def _quiet_progress(_message: str) -> Iterator[None]:
    yield


def _layer_count(count: int) -> str:
    return f"{count} {'layer' if count == 1 else 'layers'}"


@dataclass(frozen=True)
class Options:
    base: str | None
    head: str
    target: str | None
    remote: str
    draft: bool = False


@dataclass(frozen=True)
class WorkflowResult:
    manifest: Manifest
    reconcile_result: ReconcileResult | None

    @property
    def is_empty(self) -> bool:
        return not self.manifest.layers


# A view's identities for unexported commits are random and never published,
# so they cannot match a PR whatever login prefixes them.
VIEW_LOGIN = "stack-pr"


def _assign_candidates(
    repo: Path, commits: list[Commit], *, login: str | None = None
) -> list[Commit]:
    if all(commit.branch is not None for commit in commits):
        return commits
    return assign_missing_identities(
        commits, login or github_login(repo), (uuid.uuid4().hex for _ in commits)
    )


@dataclass(frozen=True)
class _Range:
    target: str
    target_sha: str
    base: str
    commits: list[Commit]


def _resolve_range(repo: Path, options: Options, target: str) -> _Range:
    target_sha = require_remote_target(repo, options.remote, target)
    base = options.base or merge_base(repo, options.head, options.remote, target)
    return _Range(target, target_sha, base, load_linear_range(repo, base, options.head))


def _refetched_range(repo: Path, options: Options, stale: _Range) -> _Range:
    """Resolve the range again after a fetch, reusing `stale` where it holds.

    The range is base..head and head does not move during a fetch, so it can
    change only if the fetched target moved its merge base.
    """
    target_sha = require_remote_target(repo, options.remote, stale.target)
    if target_sha == stale.target_sha:
        return stale
    base = options.base or merge_base(repo, options.head, options.remote, stale.target)
    if base == stale.base:
        return replace(stale, target_sha=target_sha)
    return _resolve_range(repo, options, stale.target)


@dataclass(frozen=True)
class _Plan:
    target: str
    commits: list[Commit]
    candidates: list[Commit]
    manifest: Manifest
    preliminary: ReconcileResult | None
    protocol: int | None = None


def _plan(
    repo: Path,
    options: Options,
    commit_range: _Range,
    *,
    login: str | None,
    draft: bool,
    preliminary: bool,
) -> _Plan:
    commits = commit_range.commits
    candidates = _assign_candidates(repo, commits, login=login) if commits else []
    manifest = _manifest(
        target=commit_range.target,
        remote=options.remote,
        commits=candidates,
        draft=draft,
    )
    result = (
        reconcile(repo, manifest, dry_run=True) if preliminary and commits else None
    )
    return _Plan(commit_range.target, commits, candidates, manifest, result)


def _fetch_and_plan(
    repo: Path,
    options: Options,
    *,
    target: str,
    refspecs: list[str],
    login: str | None,
    draft: bool,
    preliminary: Callable[[list[Commit], Callable[[], int]], bool],
) -> _Plan:
    """Fetch the remote while planning against the target fetched last time.

    Fetching changes the commit range only when the remote target gained
    commits from it, so the plan usually stands; it is redone when the range
    differs, or when the stale target could not be used. Only `refspecs` are
    fetched: a full fetch of a repository with many branches costs seconds.

    `preliminary` decides from the commits and the protocol version whether
    the plan includes a dry-run reconcile.
    """
    stale: _Range | None = None
    speculative: _Plan | None = None
    checking: Future[int] | None = None
    protocol: int | None = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        fetching = pool.submit(fetch_remote, repo, options.remote, refspecs)
        try:
            stale = _resolve_range(repo, options, target)
            if stale.commits:
                checking = pool.submit(preflight, repo)
                speculative = _plan(
                    repo,
                    options,
                    stale,
                    login=login,
                    draft=draft,
                    preliminary=preliminary(stale.commits, checking.result),
                )
        except StackPrError:
            # Redone below, so fetch and preflight errors take precedence.
            stale = speculative = None
        fetching.result()
        if checking is not None:
            protocol = checking.result()
    if stale is None:
        fresh = _resolve_range(repo, options, target)
    else:
        fresh = _refetched_range(repo, options, stale)
    if fresh.commits and protocol is None:
        protocol = preflight(repo)
    if speculative is not None and [c.sha for c in speculative.commits] == [
        c.sha for c in fresh.commits
    ]:
        return replace(speculative, protocol=protocol)
    planned = _plan(
        repo,
        options,
        fresh,
        login=login,
        draft=draft,
        preliminary=bool(fresh.commits)
        and preliminary(fresh.commits, lambda: _require(protocol)),
    )
    return replace(planned, protocol=protocol)


def _require(protocol: int | None) -> int:
    if protocol is None:
        raise StackPrError("reconcile protocol version is unknown")
    return protocol


def _has_identity(commits: list[Commit]) -> bool:
    return any(commit.branch is not None for commit in commits)


def _all_identified(commits: list[Commit]) -> bool:
    return all(commit.branch is not None for commit in commits)


def _manifest(
    *, target: str, remote: str, commits: list[Commit], draft: bool
) -> Manifest:
    layers: list[ManifestLayer] = []
    for commit in commits:
        if commit.branch is None:
            raise StackPrError(f"commit {commit.sha[:12]} has no branch identity")
        layers.append(
            ManifestLayer(
                branch=commit.branch,
                title=commit.subject,
                body=commit.body,
                draft=draft,
            )
        )
    return Manifest(base=target, remote=remote, layers=tuple(layers))


def _require_merged_patches_unchanged(
    repo: Path, candidates: list[Commit], plan: ReconcileResult, remote: str
) -> None:
    result_by_branch = {layer.branch: layer for layer in plan.layers}
    for commit in candidates:
        if commit.branch is None:
            raise StackPrError(f"commit {commit.sha[:12]} has no branch identity")
        result_layer = result_by_branch.get(commit.branch)
        if result_layer is not None and result_layer.status == "merged":
            require_merged_patch_unchanged(repo, commit, result_layer.head_sha, remote)


def _plan_from_merged_block(
    repo: Path, manifest: Manifest, blocked: ReconcileResult
) -> ReconcileResult:
    """Turn a result blocked by merged layers into the plan it stands in for.

    Its merged layers and their heads came from GitHub and stand. Any other
    blocker may stem from layers built on a merged one, so then plan afresh.
    """
    plan = blocked.without_merged_layer_blockers()
    if plan.blockers:
        return reconcile(repo, manifest, dry_run=True)
    return plan


def _reconcile_assuming_unmerged(
    repo: Path,
    options: Options,
    target: str,
    candidates: list[Commit],
    manifest: Manifest,
    *,
    dry_run: bool,
) -> tuple[bool, ReconcileResult]:
    """Replay every layer and reconcile once, as if no layer were merged.

    Returns (True, result), or else (False, plan) to replay without merged
    layers: a merged layer blocks the call, and a replay conflict may come
    from one.
    """
    try:
        with materialize_layers(
            repo,
            candidates,
            merged_branches=set(),
            remote=options.remote,
            target=target,
        ):
            result = reconcile(repo, manifest.assuming_unmerged(), dry_run=dry_run)
    except ReplayConflictError:
        return False, reconcile(repo, manifest, dry_run=True)
    if not result.merged_layer_blocked:
        return True, result
    return False, _plan_from_merged_block(repo, manifest, result)


def view(
    repo: Path, options: Options, *, progress: Progress | None = None
) -> WorkflowResult:
    step = progress or _quiet_progress
    with step(f"Fetching {options.remote} and loading commits"):
        target = options.target or infer_target(repo, options.remote)
        planned = _fetch_and_plan(
            repo,
            options,
            target=target,
            refspecs=[tracking_refspec(options.remote, target)],
            login=VIEW_LOGIN,
            draft=False,
            # Unexported commits have no PRs, so their plan is the comparison.
            # Otherwise a v2 extension compares in one call after replaying.
            preliminary=lambda commits, protocol: (
                not _has_identity(commits) or protocol() < ASSUME_UNMERGED_VERSION
            ),
        )
    commits, candidates, manifest = (
        planned.commits,
        planned.candidates,
        planned.manifest,
    )
    if not commits:
        return WorkflowResult(manifest, None)

    count = _layer_count(len(commits))
    with step(f"Comparing {count} with {options.remote}/{target}"):
        preliminary = planned.preliminary
        if preliminary is None:
            done, preliminary = _reconcile_assuming_unmerged(
                repo, options, target, candidates, manifest, dry_run=True
            )
            if done:
                return WorkflowResult(manifest, preliminary)
        if preliminary.blockers:
            return WorkflowResult(manifest, preliminary)
        _require_merged_patches_unchanged(repo, candidates, preliminary, options.remote)

        with materialize_layers(
            repo,
            candidates,
            merged_branches=preliminary.merged_branches,
            remote=options.remote,
            target=target,
        ):
            # Commits without an identity were never exported, so none has a
            # PR to compare with the replayed layers and the plan already is
            # the comparison; replaying only checks the stack still applies.
            if not _has_identity(commits):
                return WorkflowResult(manifest, preliminary)
            compared = reconcile(repo, manifest, dry_run=True)
    return WorkflowResult(manifest, compared)


def export(
    repo: Path, options: Options, *, progress: Progress | None = None
) -> WorkflowResult:
    step = progress or _quiet_progress
    require_clean_worktree(repo)
    source_branch = current_branch(repo)
    source_tip = current_head(repo)

    with step(f"Fetching {options.remote} and planning"):
        target = options.target or infer_target(repo, options.remote)
        refspecs = [tracking_refspec(options.remote, target)]
        upstream = upstream_refspec(repo, source_branch, options.remote)
        if upstream is not None:
            refspecs.append(upstream)
        planned = _fetch_and_plan(
            repo,
            options,
            target=target,
            refspecs=refspecs,
            login=None,
            draft=options.draft,
            # Commits without an identity are rewritten to carry one, so a
            # plan must clear the stack first. Otherwise a v2 extension plans
            # and applies in one call.
            preliminary=lambda commits, protocol: (
                not _all_identified(commits) or protocol() < ASSUME_UNMERGED_VERSION
            ),
        )
        check_source_upstream(repo, source_branch, options.remote)
    commits, candidates, manifest = (
        planned.commits,
        planned.candidates,
        planned.manifest,
    )
    if not commits:
        return WorkflowResult(manifest, None)

    count = _layer_count(len(commits))
    plan = planned.preliminary
    with step(f"Publishing {count}"):
        if plan is None:
            done, plan = _reconcile_assuming_unmerged(
                repo, options, target, candidates, manifest, dry_run=False
            )
            if done:
                return WorkflowResult(manifest, plan)
        if plan.blockers:
            raise StackPrError(
                "native reconciliation is blocked: " + "; ".join(plan.blockers)
            )
        _require_merged_patches_unchanged(repo, candidates, plan, options.remote)
        identified = insert_missing_identities(
            repo,
            commits,
            candidates,
            source_branch=source_branch,
            source_tip=source_tip,
        )
        with materialize_layers(
            repo,
            identified,
            merged_branches=plan.merged_branches,
            remote=options.remote,
            target=target,
        ):
            applied = reconcile(repo, manifest, dry_run=False)
    return WorkflowResult(manifest, applied)
