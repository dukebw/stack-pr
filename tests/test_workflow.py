from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from stack_pr import workflow
from stack_pr.git import StackPrError
from stack_pr.reconcile import Manifest, ReconcileResult, parse_result
from stack_pr.workflow import Options

from .conftest import Repo


def _result(
    manifest: Manifest,
    statuses: list[str],
    *,
    native_stack: bool = True,
    head_shas: dict[str, str] | None = None,
) -> ReconcileResult:
    assert len(manifest.layers) == len(statuses)
    payload: dict[str, object] = {
        "version": 1,
        "layers": [
            {
                "branch": layer.branch,
                "status": statuses[index],
                "number": index + 10,
                "url": f"https://github.test/pull/{index + 10}",
                "pr": {
                    "number": index + 10,
                    "url": f"https://github.test/pull/{index + 10}",
                    "head_sha": (head_shas or {}).get(layer.branch, ""),
                },
            }
            for index, layer in enumerate(manifest.layers)
        ],
    }
    if native_stack:
        payload["stack"] = {"url": "https://github.test/stacks/1"}
    return parse_result(payload)


def _mock_identity_and_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "preflight", lambda repo: None)
    monkeypatch.setattr(workflow, "github_login", lambda repo: "octocat")


def test_range_inference_and_view_purity(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit_file("feature.txt", "feature\n", "Feature subject\n\nFeature body\n")
    (repo.path / "dirty.txt").write_text("leave me alone\n")
    before_head = repo.output("rev-parse", "HEAD")
    before_message = repo.output("show", "-s", "--format=%B", "HEAD")
    before_status = repo.output("status", "--porcelain")
    before_worktrees = repo.output("worktree", "list", "--porcelain")
    _mock_identity_and_preflight(monkeypatch)
    reconcile_calls = 0
    planned_branches: list[str] = []

    def fake_reconcile(
        path: object, manifest: Manifest, *, dry_run: bool
    ) -> ReconcileResult:
        nonlocal reconcile_calls
        assert dry_run
        assert manifest.base == "master"
        assert manifest.remote == "origin"
        reconcile_calls += 1
        planned_branches[:] = [layer.branch for layer in manifest.layers]
        # Unexported commits have no PRs to look up first, so the only
        # reconcile compares the materialized layers.
        assert all(
            repo.git(
                "show-ref", "--verify", f"refs/heads/{branch}", check=False
            ).returncode
            == 0
            for branch in planned_branches
        )
        return _result(manifest, ["create"])

    monkeypatch.setattr(workflow, "reconcile", fake_reconcile)
    result = workflow.view(repo.path, Options(None, "HEAD", None, "origin"))

    assert result.manifest.layers[0].title == "Feature subject"
    assert result.manifest.layers[0].body == "Feature body"
    assert repo.output("rev-parse", "HEAD") == before_head
    assert repo.output("show", "-s", "--format=%B", "HEAD") == before_message
    assert "stack-pr-branch:" not in before_message
    assert repo.output("status", "--porcelain") == before_status
    assert repo.output("worktree", "list", "--porcelain") == before_worktrees
    assert reconcile_calls == 1
    assert all(
        repo.git("show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode
        != 0
        for branch in planned_branches
    )


def test_view_detects_code_only_local_drift(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = "octocat/stack/11111111111111111111111111111111"
    old_head = repo.commit_file(
        "feature.txt",
        "old\n",
        f"Feature subject\n\nFeature body\n\nstack-pr-branch: {branch}\n",
    )
    (repo.path / "feature.txt").write_text("new\n")
    repo.git("add", "feature.txt")
    repo.git("commit", "--amend", "--no-edit", "--no-gpg-sign")
    local_head = repo.output("rev-parse", "HEAD")
    assert local_head != old_head
    _mock_identity_and_preflight(monkeypatch)
    reconcile_calls = 0

    def fake_reconcile(
        path: object, manifest: Manifest, *, dry_run: bool
    ) -> ReconcileResult:
        nonlocal reconcile_calls
        assert dry_run
        reconcile_calls += 1
        if reconcile_calls == 1:
            assert (
                repo.git(
                    "show-ref", "--verify", f"refs/heads/{branch}", check=False
                ).returncode
                != 0
            )
            return _result(manifest, ["none"], head_shas={branch: old_head})

        materialized_head = repo.output("rev-parse", branch)
        assert materialized_head == local_head
        return _result(manifest, ["update"], head_shas={branch: old_head})

    monkeypatch.setattr(workflow, "reconcile", fake_reconcile)

    result = workflow.view(repo.path, Options(None, "HEAD", None, "origin"))

    assert result.reconcile_result is not None
    assert result.reconcile_result.layers[0].status == "update"
    assert reconcile_calls == 2
    assert repo.output("rev-parse", "HEAD") == local_head
    assert repo.output("status", "--porcelain") == ""
    assert (
        repo.git("show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode
        != 0
    )


def test_export_requires_clean_tree_before_native_preflight(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo.path / "dirty.txt").write_text("dirty\n")
    called = False

    def fake_preflight(path: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(workflow, "preflight", fake_preflight)
    with pytest.raises(StackPrError, match="clean working tree"):
        workflow.export(repo.path, Options(repo.root, "HEAD", "master", "origin"))
    assert not called


def test_export_replays_patch_onto_moved_target(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit_file("feature.txt", "feature\n", "Feature\n")
    repo.git("switch", "master")
    repo.commit_file("target.txt", "target\n", "Move target\n")
    repo.git("push", "origin", "master")
    repo.git("switch", "feature")
    _mock_identity_and_preflight(monkeypatch)
    applied_branch = ""
    progress_events: list[str] = []

    @contextmanager
    def progress(message: str) -> Iterator[None]:
        progress_events.append(f"start:{message}")
        yield
        progress_events.append(f"done:{message}")

    def fake_reconcile(
        path: object, manifest: Manifest, *, dry_run: bool
    ) -> ReconcileResult:
        nonlocal applied_branch
        if dry_run:
            return _result(manifest, ["create"])
        applied_branch = manifest.layers[0].branch
        assert repo.output("rev-parse", f"{applied_branch}^") == repo.output(
            "rev-parse", "origin/master"
        )
        assert repo.output("show", f"{applied_branch}:target.txt") == "target"
        assert repo.output("show", f"{applied_branch}:feature.txt") == "feature"
        return _result(manifest, ["create"])

    monkeypatch.setattr(workflow, "reconcile", fake_reconcile)
    before_status = repo.output("status", "--porcelain")
    before_index = repo.output("ls-files", "-s")

    workflow.export(repo.path, Options(None, "HEAD", None, "origin"), progress=progress)

    assert repo.output("branch", "--show-current") == "feature"
    # Replay happens in the object database, so publishing leaves the caller's
    # checkout, index and worktree untouched. HEAD moves only because the
    # source branch is rewritten to carry the trailer.
    assert repo.output("status", "--porcelain") == before_status
    assert repo.output("ls-files", "-s") == before_index
    listing = repo.output("worktree", "list", "--porcelain").splitlines()
    assert sum(1 for line in listing if line.startswith("worktree ")) == 1
    assert (
        repo.git(
            "show-ref", "--verify", f"refs/heads/{applied_branch}", check=False
        ).returncode
        != 0
    )
    assert "stack-pr-branch: octocat/stack/" in repo.output(
        "show", "-s", "--format=%B", "HEAD"
    )
    assert progress_events == [
        "start:Fetching origin and loading commits",
        "done:Fetching origin and loading commits",
        "start:Planning 1 layer against origin/master",
        "done:Planning 1 layer against origin/master",
        "start:Publishing 1 layer",
        "done:Publishing 1 layer",
    ]


def test_export_skips_merged_layer_when_materializing(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = repo.commit_file("one.txt", "one\n", "One\n")
    repo.commit_file("two.txt", "two\n", "Two\n")
    repo.git("switch", "master")
    repo.git("cherry-pick", "--no-gpg-sign", first)
    repo.git("push", "origin", "master")
    repo.git("switch", "feature")
    _mock_identity_and_preflight(monkeypatch)
    branches: list[str] = []

    def fake_reconcile(
        path: object, manifest: Manifest, *, dry_run: bool
    ) -> ReconcileResult:
        nonlocal branches
        branches = [layer.branch for layer in manifest.layers]
        if dry_run:
            return _result(
                manifest,
                ["merged", "update"],
                head_shas={manifest.layers[0].branch: first},
            )
        assert (
            repo.git(
                "show-ref", "--verify", f"refs/heads/{branches[0]}", check=False
            ).returncode
            != 0
        )
        assert repo.output("rev-parse", f"{branches[1]}^") == repo.output(
            "rev-parse", "origin/master"
        )
        assert repo.output("show", f"{branches[1]}:one.txt") == "one"
        assert repo.output("show", f"{branches[1]}:two.txt") == "two"
        return _result(manifest, ["merged", "update"])

    monkeypatch.setattr(workflow, "reconcile", fake_reconcile)
    workflow.export(repo.path, Options(repo.root, "HEAD", "master", "origin"))
    assert all(
        repo.git("show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode
        != 0
        for branch in branches
    )


def test_export_rejects_changed_merged_layer(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit_file("feature.txt", "feature\n", "Feature\n")
    _mock_identity_and_preflight(monkeypatch)

    def fake_reconcile(
        path: object, manifest: Manifest, *, dry_run: bool
    ) -> ReconcileResult:
        assert dry_run
        return _result(
            manifest,
            ["merged"],
            head_shas={manifest.layers[0].branch: repo.root},
        )

    monkeypatch.setattr(workflow, "reconcile", fake_reconcile)

    with pytest.raises(StackPrError, match="changes merged layer"):
        workflow.export(repo.path, Options(repo.root, "HEAD", "master", "origin"))


def test_replay_conflict_restores_checkout_and_temporary_state(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit_file("conflict.txt", "base\n", "Add conflict file\n")
    repo.git("switch", "master")
    repo.commit_file("conflict.txt", "target\n", "Target version\n")
    repo.git("push", "origin", "master")
    repo.git("switch", "feature")
    repo.commit_file("conflict.txt", "feature\n", "Feature version\n")
    _mock_identity_and_preflight(monkeypatch)
    planned_branches: list[str] = []

    def fake_reconcile(
        path: object, manifest: Manifest, *, dry_run: bool
    ) -> ReconcileResult:
        planned_branches[:] = [layer.branch for layer in manifest.layers]
        if not dry_run:
            pytest.fail("apply reconcile must not run after replay conflict")
        return _result(manifest, ["create", "create"])

    monkeypatch.setattr(workflow, "reconcile", fake_reconcile)
    with pytest.raises(StackPrError, match="could not replay commit"):
        workflow.export(repo.path, Options(repo.root, "HEAD", "master", "origin"))

    assert repo.output("branch", "--show-current") == "feature"
    assert repo.output("status", "--porcelain") == ""
    assert not (repo.path / ".git" / "CHERRY_PICK_HEAD").exists()
    assert all(
        repo.git("show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode
        != 0
        for branch in planned_branches
    )


def test_empty_range_is_noop_without_reconcile(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_reconcile(*args: object, **kwargs: object) -> ReconcileResult:
        pytest.fail("empty range must not call native commands")

    monkeypatch.setattr(workflow, "preflight", unexpected_reconcile)
    monkeypatch.setattr(workflow, "github_login", unexpected_reconcile)
    monkeypatch.setattr(workflow, "reconcile", unexpected_reconcile)
    result = workflow.export(repo.path, Options("HEAD", "HEAD", "master", "origin"))
    assert result.is_empty


def test_export_never_pushes_source_branch(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.git("push", "-u", "origin", "feature")
    remote_before = repo.output("ls-remote", "origin", "refs/heads/feature").split()[0]
    repo.commit_file("feature.txt", "feature\n", "Feature\n")
    _mock_identity_and_preflight(monkeypatch)

    def fake_reconcile(
        path: object, manifest: Manifest, *, dry_run: bool
    ) -> ReconcileResult:
        return _result(manifest, ["create"])

    monkeypatch.setattr(workflow, "reconcile", fake_reconcile)
    workflow.export(repo.path, Options(repo.root, "HEAD", "master", "origin"))
    remote_after = repo.output("ls-remote", "origin", "refs/heads/feature").split()[0]
    assert remote_after == remote_before

    first_export_head = repo.output("rev-parse", "HEAD")
    workflow.export(repo.path, Options(repo.root, "HEAD", "master", "origin"))
    assert repo.output("rev-parse", "HEAD") == first_export_head
    assert repo.output("ls-remote", "origin", "refs/heads/feature").split()[0] == (
        remote_before
    )


def test_export_rejects_changes_only_on_source_upstream(repo: Repo) -> None:
    repo.commit_file("feature.txt", "feature\n", "Feature\n")
    repo.git("push", "-u", "origin", "feature")
    repo.commit_file("remote.txt", "remote\n", "Remote change\n")
    repo.git("push", "origin", "feature")
    repo.git("reset", "--hard", "HEAD~1")

    with pytest.raises(StackPrError, match="missing changes from its upstream"):
        workflow.export(repo.path, Options(repo.root, "HEAD", "master", "origin"))


def test_blocked_plan_aborts_before_source_mutation(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.commit_file("feature.txt", "feature\n", "Feature\n")
    before = repo.output("rev-parse", "HEAD")
    _mock_identity_and_preflight(monkeypatch)

    def fake_reconcile(
        path: object, manifest: Manifest, *, dry_run: bool
    ) -> ReconcileResult:
        assert dry_run
        return _result(manifest, ["queued"])

    monkeypatch.setattr(workflow, "reconcile", fake_reconcile)
    with pytest.raises(StackPrError, match="blocked"):
        workflow.export(repo.path, Options(repo.root, "HEAD", "master", "origin"))
    assert repo.output("rev-parse", "HEAD") == before
    assert "stack-pr-branch:" not in repo.output("show", "-s", "--format=%B", "HEAD")
