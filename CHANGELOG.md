# Top of tree

* Replay layers with `git merge-tree` in the object database instead of a disposable worktree. `view` and `export` no longer check out, reset, or otherwise touch a working tree, which removes the dominant cost on large repositories.
* Read every commit in the selected range with two git processes instead of three per commit.
* Require git 2.40 or newer.

# Version 1.0.3

* Include pending native Stack operations when deciding whether local commits are synchronized.

# Version 1.0.2

* Show `synced` and an explicit stack-level up-to-date summary instead of the internal `none` action.
* Compare replayed local layer SHAs from a disposable worktree so `view` detects code-only amendments and target movement.
* Render completed export actions in the past tense and confirm the synchronized result.

# Version 1.0.1

* Show timed fetch, planning, and publishing progress during `stack-pr export`.
* Keep `--json` exports quiet so stdout and stderr remain machine-friendly.

# Version 1.0.0

* Replaced custom remote stack management with native GitHub Stack reconciliation
  through the paired `dukebw/gh-stack` extension.
* Reduced the CLI to the commit-oriented `view` and `export` commands.
* Replaced PR URLs and numeric branch allocation metadata with one stable
  `stack-pr-branch` trailer per commit.
* Removed legacy stack migration, landing, abandonment, cross-link generation,
  configuration files, reviewer assignment, per-layer draft masks, and branch
  naming templates.

# Version 0.1.3

* Fix a bug with replacing $USERNAME in the branch name. (#44)

# Version 0.1.2

* Added config files - now defaults for the CL options can be customized with
  local config files (#32).
* Added a feature to customize branch names for stacked PRs (#33).
* Fixed a bug with branches not being deleted when a stack is abandoned (#27).
* Subcommands outputs is suppressed for less spammy look (#26).

# Version 0.1.1
