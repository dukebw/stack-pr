from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from stack_pr.git import (
    Commit,
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
)
from stack_pr.reconcile import (
    Manifest,
    ManifestLayer,
    ReconcileResult,
    github_login,
    preflight,
    reconcile,
)


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


def _assign_candidates(repo: Path, commits: list[Commit]) -> list[Commit]:
    login = github_login(repo)
    return assign_missing_identities(
        commits, login, (uuid.uuid4().hex for _ in commits)
    )


def _resolve_range(
    repo: Path, options: Options, *, fetch: bool
) -> tuple[str, str, list[Commit]]:
    target = options.target or infer_target(repo, options.remote)
    if fetch:
        fetch_remote(repo, options.remote)
    require_remote_target(repo, options.remote, target)
    base = options.base or merge_base(repo, options.head, options.remote, target)
    return target, base, load_linear_range(repo, base, options.head)


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


def view(repo: Path, options: Options) -> WorkflowResult:
    target, _, commits = _resolve_range(repo, options, fetch=False)
    if not commits:
        return WorkflowResult(Manifest(target, options.remote, ()), None)

    preflight(repo)
    candidates = _assign_candidates(repo, commits)
    manifest = _manifest(
        target=target,
        remote=options.remote,
        commits=candidates,
        draft=False,
    )
    return WorkflowResult(manifest, reconcile(repo, manifest, dry_run=True))


def export(repo: Path, options: Options) -> WorkflowResult:
    require_clean_worktree(repo)
    source_branch = current_branch(repo)
    source_tip = current_head(repo)

    target, _, commits = _resolve_range(repo, options, fetch=True)
    check_source_upstream(repo, source_branch, options.remote)
    if not commits:
        return WorkflowResult(Manifest(target, options.remote, ()), None)

    preflight(repo)
    candidates = _assign_candidates(repo, commits)
    manifest = _manifest(
        target=target,
        remote=options.remote,
        commits=candidates,
        draft=options.draft,
    )
    plan = reconcile(repo, manifest, dry_run=True)
    if plan.blockers:
        raise StackPrError(
            "native reconciliation is blocked: " + "; ".join(plan.blockers)
        )
    result_by_branch = {layer.branch: layer for layer in plan.layers}
    for commit in candidates:
        if commit.branch is None:
            raise StackPrError(f"commit {commit.sha[:12]} has no branch identity")
        result_layer = result_by_branch.get(commit.branch)
        if result_layer is not None and result_layer.status == "merged":
            require_merged_patch_unchanged(
                repo, commit, result_layer.head_sha, options.remote
            )

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
        source_branch=source_branch,
    ):
        applied = reconcile(repo, manifest, dry_run=False)
    return WorkflowResult(manifest, applied)
