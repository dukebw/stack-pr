from __future__ import annotations

import re

import pytest

from stack_pr.git import (
    StackPrError,
    assign_missing_identities,
    insert_missing_identities,
    load_linear_range,
)

from .conftest import Repo


def test_identity_insertion_preserves_author_message_and_existing_identity(
    repo: Repo,
) -> None:
    repo.commit_file("one.txt", "one\n", "First subject\n\nFirst body\n")
    existing = "octocat/stack/11111111111111111111111111111111"
    repo.commit_file(
        "two.txt",
        "two\n",
        f"Second subject\n\nSecond body\n\nstack-pr-branch: {existing}\n",
    )
    before = load_linear_range(repo.path, repo.root, "HEAD")
    source_tip = repo.output("rev-parse", "HEAD")
    assigned = assign_missing_identities(
        before,
        "octocat",
        iter(["22222222222222222222222222222222"]),
    )

    after = insert_missing_identities(
        repo.path,
        before,
        assigned,
        source_branch="feature",
        source_tip=source_tip,
    )

    assert repo.output("branch", "--show-current") == "feature"
    assert repo.output("status", "--porcelain") == ""
    assert after[0].subject == "First subject"
    assert after[0].body == "First body"
    assert after[0].author_name == before[0].author_name
    assert after[0].author_email == before[0].author_email
    assert after[0].author_date == before[0].author_date
    assert after[0].branch == "octocat/stack/22222222222222222222222222222222"
    assert after[1].message == before[1].message
    assert after[1].branch == existing


def test_legacy_trailer_is_rejected(repo: Repo) -> None:
    repo.commit_message(
        "Legacy\n\nstack-info: PR: https://example.test/1, branch: old/stack/1\n"
    )
    with pytest.raises(StackPrError, match="legacy stack-info"):
        load_linear_range(repo.path, repo.root, "HEAD")


def test_multiple_identity_trailers_are_rejected(repo: Repo) -> None:
    branch_one = "octocat/stack/11111111111111111111111111111111"
    branch_two = "octocat/stack/22222222222222222222222222222222"
    repo.commit_message(
        f"Multiple\n\nstack-pr-branch: {branch_one}\nstack-pr-branch: {branch_two}\n"
    )
    with pytest.raises(StackPrError, match="multiple stack-pr-branch"):
        load_linear_range(repo.path, repo.root, "HEAD")


def test_duplicate_identities_across_commits_are_rejected(repo: Repo) -> None:
    branch = "octocat/stack/11111111111111111111111111111111"
    repo.commit_message(f"One\n\nstack-pr-branch: {branch}\n")
    repo.commit_message(f"Two\n\nstack-pr-branch: {branch}\n")
    with pytest.raises(StackPrError, match="share branch identity"):
        load_linear_range(repo.path, repo.root, "HEAD")


def test_generated_identity_is_fixed_opaque_name(repo: Repo) -> None:
    repo.commit_message("One\n")
    commits = load_linear_range(repo.path, repo.root, "HEAD")
    assigned = assign_missing_identities(
        commits,
        "octocat",
        iter(["abcdefabcdefabcdefabcdefabcdefab"]),
    )
    assert assigned[0].branch == "octocat/stack/abcdefabcdefabcdefabcdefabcdefab"
    assert re.fullmatch(r"octocat/stack/[0-9a-f]{32}", assigned[0].branch or "")
