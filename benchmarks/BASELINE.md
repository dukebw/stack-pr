# Baseline

Recorded before any optimisation. Every later claim about speed should be
compared against this file plus the harness that produced it.

## Fingerprint

| | |
| --- | --- |
| stack-pr | `d2f56b1` (1.0.4.dev1+gd2f56b1, editable) |
| python | 3.14.7 |
| git | 2.54.0 (Apple Git-157) |
| platform | macOS-27.0.1-arm64, 18 cores |

Machine-specific absolute values. Relative comparisons between arms and against
later runs on the same machine are the point.

## Arms

| arm | files | `gh` latency | workloads | reps |
| --- | --- | --- | --- | --- |
| A | 1 000 | none (`--delay-ms 0`) | all three | 5 |
| B | 1 000 | 370 ms/layer | all three | 5 |
| C | 33 804 | none | `view-clean`, `export` | 3 |
| D | 33 804 | 370 ms/layer | `view-clean`, `export` | 3 |

Arm D is the one that resembles a real checkout: your baseten clone has 33 804
tracked files and talks to a `gh stack` extension that makes one GraphQL round
trip per layer.

## A — 1 000 files, no `gh` latency

Median wall seconds.

| workload | L1 | L5 | L13 | subprocesses @L13 |
| --- | --- | --- | --- | --- |
| `view-blocked` | 0.85 | 1.41 | 2.06 | 51 |
| `view-clean` | 1.85 | 3.54 | 6.21 | 160 |
| `export` | 1.98 | 4.47 | 9.03 | 261 |

Isolates stack-pr's own work. Cost is close to linear in layer count at roughly
0.11 s / 0.38 s / 0.69 s per layer respectively — and each layer adds 3 / 11 / 18
subprocess spawns, so most of that slope is process creation, not git work.

## B — 1 000 files, 370 ms/layer

| workload | L1 | L5 | L13 | `gh` share @L13 |
| --- | --- | --- | --- | --- |
| `view-blocked` | 1.42 | 3.23 | 6.66 | 75 % |
| `view-clean` | 3.01 | 7.00 | 16.23 | 61 % |
| `export` | 2.91 | 8.26 | 19.42 | 52 % |

Adds the extension's latency. `view-clean` runs **two** reconciles, so it pays
2 × 13 × 370 ms ≈ 9.6 s of pure round trips.

## C — 33 804 files, no `gh` latency

| workload | L13 | vs arm A (1 000 files) |
| --- | --- | --- |
| `view-clean` | 21.85 | 6.21 (+15.6) |
| `export` | 13.48 | 9.03 (+4.4) |

`view-clean` degrades far worse, because it checks out a whole disposable
worktree. `export` already has a working tree, so it only pays incremental
`reset --hard`.

## D — 33 804 files, 370 ms/layer (realistic)

| workload | L13 | median | min | max | subprocesses |
| --- | --- | --- | --- | --- | --- |
| `view-clean` | 13 | **35.05** | 32.17 | 35.43 | 160 |
| `export` | 13 | **24.19** | 23.56 | 24.53 | 261 |

### Where `view-clean` (35.05 s) goes

| bucket | seconds | share |
| --- | --- | --- |
| disposable worktree checkout (`git worktree` add + remove) | 13.17 | 38 % |
| `gh stack reconcile` × 2 (13 layers each) | 10.01 | 29 % |
| layer replay in the worktree (`cherry-pick` + `reset`) | 6.07 | 17 % |
| tree/ref bookkeeping (`write-tree`, `commit-tree`, `update-ref`, `show-ref`) | 2.60 | 7 % |
| commit reads (`rev-parse` 30×, `show` 13×, `cat-file` 13×) | 2.27 | 6 % |
| other (`fetch`, `checkout`, `merge-base`, startup) | 0.93 | 3 % |

### Where `export` (24.19 s) goes

| bucket | seconds | share |
| --- | --- | --- |
| `gh stack reconcile` × 2 | 10.07 | 42 % |
| layer replay in the caller's tree (`reset` + `cherry-pick`) | 5.26 | 22 % |
| commit reads (`show` 39×, `rev-parse` 60×, `cat-file` 39×) | 4.87 | 20 % |
| tree/ref bookkeeping | 2.68 | 11 % |
| other | 1.31 | 5 % |

## What this says about where to work

Four costs, in order of addressable size:

1. **The second reconcile (~5 s each).** `view` and `export` both reconcile
   before and after materialising layers. Halving this is entirely local.
2. **The disposable worktree (13.2 s, 38 % of `view`).** A full 33.8k-file
   checkout for a replay that only ever needs an index and a tree. Nothing in
   the replay needs the files on disk.
3. **Per-commit fan-out (2.3 s / 4.9 s).** Reading one commit costs three
   spawns (`rev-parse`, `cat-file`, `show`); some of those are outright
   redundant, e.g. `read_commit` re-running `rev-parse` on a SHA that
   `rev-list` already returned in full, and `materialize_layers` calling
   `current_head` right after `reset --hard <sha>` it already holds.
4. **Per-layer `reset` + `cherry-pick` (5.3 s in `export`).** Runs in the
   caller's own working tree, so it is both the slow part and the part that
   leaves the checkout detached and being rewritten for the duration.

The remaining ~5 s of `gh stack reconcile` is `gh-stack` making one GraphQL
round trip per layer, sequentially. That is a different repository; the only
lever here is not calling it twice.

## Reproducing

```bash
.venv/bin/python -m benchmarks.run --preset baseline --json arm-a.json
.venv/bin/python -m benchmarks.run --preset baseline --delay-ms 370 --json arm-b.json
.venv/bin/python -m benchmarks.run --preset scale --json arm-c.json
.venv/bin/python -m benchmarks.run --workloads view-clean,export \
    --layers 13 --fillers 33804 --delay-ms 370 --reps 3 --json arm-d.json
```

## Result after optimisation

Two changes, measured step by step against the arms above.

1. **Read commits in bulk.** Reading a range cost three processes per commit
   (`rev-parse`, `cat-file`, `show`); it now costs two processes in total
   (`git log --no-walk -z` for metadata, `git cat-file --batch` for raw bodies).
2. **Replay in the object database.** `materialize_layers` used to check out a
   disposable worktree and run `cherry-pick` + `write-tree` + `reset --hard`
   per layer. It now uses `git merge-tree --write-tree --merge-base` plus
   `commit-tree`, and applies all layer refs in one transactional
   `update-ref --stdin`. No checkout, index, or working tree is touched.

### Arm D (L13, 33 804 files, 370 ms/layer)

| workload | baseline | after (1) | after (2) | total | subprocesses |
| --- | --- | --- | --- | --- | --- |
| `view-clean` | 35.05 | 32.82 | **13.04** | **2.7×** | 160 → 46 |
| `export` | 24.19 | 22.05 | **13.83** | **1.75×** | 261 → 73 |

### Arm A (L13, 1 000 files, no `gh` latency)

| workload | baseline | after | total | subprocesses |
| --- | --- | --- | --- | --- |
| `view-blocked` | 2.06 | **0.76** | 2.7× | 51 → 15 |
| `view-clean` | 6.21 | **2.01** | 3.1× | 160 → 46 |
| `export` | 9.03 | **3.05** | 3.0× | 261 → 73 |

`view-blocked` is now flat in layer count: 15 processes at L1, L5 and L13.

### What remains

`gh stack reconcile` is now **77 %** of `view-clean` and **72 %** of `export` —
9.98 s, essentially the whole remaining runtime. The rest:

| | `view-clean` (13.04 s) | `export` (13.83 s) |
| --- | --- | --- |
| `gh stack reconcile` × 2 | 9.98 (77 %) | 9.98 (72 %) |
| `git merge-tree` × 13 | 1.47 (11 %) | 1.38 (10 %) |
| `git commit-tree` | 0.49 (4 %) | 0.89 (6 %) |
| everything else | 1.10 (8 %) | 1.58 (11 %) |

Two things are worth stating plainly:

- **The extension's cost is per layer.** Reconciliation issues one
  `FindPRForBranchAllStates` GraphQL query per layer, sequentially: 13 queries
  at ~370 ms. A single aliased query would remove ~4.4 s *per call*, ~8.9 s
  across the two. That is a `dukebw/gh-stack` change, not a stack-pr one.
- **The second reconcile cannot be dropped here.** In the first call the local
  branches do not exist yet, so `localSHA` is empty and the extension reports
  the GitHub PR head; the drift check in `reconcile.go:428`
  (`localSHA != pr.HeadRefOID`) can only fire once the layers are materialized.
  Collapsing the two calls would mean reimplementing that comparison and the
  surrounding status/action derivation in stack-pr, making it a second source
  of truth for stack semantics.

`git merge-tree` at ~113 ms per layer over a 33.8k-file tree is the genuine
cost of replaying a patch, and is not obviously reducible.

### Compatibility decision

`merge-tree --merge-base` is git 2.40+. `preflight` now gates on it and fails
with an actionable message, rather than falling back to the worktree replay.
macOS command-line-tools git is 2.39.5, so those users must upgrade; the
fallback was rejected to avoid maintaining two replay implementations that the
benchmark would not keep honest.
