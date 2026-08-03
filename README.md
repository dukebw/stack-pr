# stack-pr

`stack-pr` turns a linear series of local commits into a native GitHub stack.
Each non-merge commit is one pull request layer. Remote stack state, pull
requests, branch pushes, reordering, removal, and merge state are delegated to
`gh stack reconcile`.

## Requirements

- Git
- GitHub CLI authenticated for the repository
- The paired `dukebw/gh-stack` extension, which provides reconciliation protocol v1

Replace the official extension explicitly:

```bash
gh extension remove stack
gh extension install dukebw/gh-stack
```

`stack-pr` deliberately has no legacy fallback and makes no direct pull request
or Stacks API calls.

## Installation

```bash
pipx install git+https://github.com/dukebw/stack-pr.git
```

Use `pipx install .` from a local checkout while developing the paired forks.

## Workflow

Create one commit per reviewable layer, inspect the native reconciliation plan,
then export it:

```bash
stack-pr view
stack-pr export
```

`view` is read-only. `export` requires a clean working tree, fetches the remote,
adds stable branch identities to commits that do not have one, replays active
patches onto the latest target, and invokes the native reconciler. The source
branch is never pushed.

Adding a branch identity rewrites that commit and its descendants. As with the
original stack-pr metadata, this invalidates existing commit signatures.

The only commit metadata owned by stack-pr is this final trailer:

```text
stack-pr-branch: octocat/stack/0123456789abcdef0123456789abcdef
```

Legacy `stack-info:` trailers are rejected and are not migrated.

## Commands

```text
stack-pr view   [-B BASE] [-H HEAD] [-T TARGET] [-R REMOTE] [--json]
stack-pr export [-B BASE] [-H HEAD] [-T TARGET] [-R REMOTE] [--json] [--draft]
```

- `--base` selects the local range base.
- `--head` selects the local range head and defaults to `HEAD`.
- `--target` selects the native stack trunk.
- `--remote` defaults to `origin`.
- `--json` emits only the versioned native reconcile response on stdout.
- `--draft` applies only to pull requests created by that export.

When omitted, the target comes from the remote HEAD and the local range base is
the merge base of the selected head and `REMOTE/TARGET`.

After export, use the native command shown by stack-pr to merge the stack:

```bash
gh stack merge TOP_PR_NUMBER
```

## Development

Run the development tools with the `dev` extra:

```bash
uv run --extra dev ruff format --check .
uv run --extra dev ruff check .
uv run --extra dev mypy src tests
uv run --extra dev pytest
```
