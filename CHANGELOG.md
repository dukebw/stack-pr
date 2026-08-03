# Top of tree

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
