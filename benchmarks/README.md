# Benchmarks

Tier-1 benchmark for `stack-pr`: hermetic, repeatable, and attributing.

It runs the real CLI against a disposable fixture repo with a fake `gh` on
`PATH`, so timings contain stack-pr's own work and nothing else — no network, no
GitHub, no `gh-stack`. That is what makes it usable as an optimisation loop: a
result is comparable between runs on the same machine, and the second run does
not pay for a different network day.

## Running

```bash
.venv/bin/python -m benchmarks.run --preset quick        # seconds, sanity check
.venv/bin/python -m benchmarks.run --preset baseline --json baseline.json
.venv/bin/python -m benchmarks.run --preset scale        # 33.8k-file working tree
```

Override any preset dimension:

```bash
.venv/bin/python -m benchmarks.run \
    --workloads view-clean,export --layers 13 --fillers 1000 --reps 5 \
    --trailers all --delay-ms 370
```

`--keep` leaves fixtures under `$SP_BENCH_ROOT` for inspection.

## Workloads

| workload | command | path |
| --- | --- | --- |
| `view-blocked` | `stack-pr view` | reconcile reports a blocker, so the run stops after one reconcile |
| `view-clean` | `stack-pr view` | full path: two reconciles plus object-database layer replay |
| `export` | `stack-pr export` | plan reconcile, identity insert, replay, applying reconcile |

`export` mutates its clone, so the runner restores the fixture between reps.

## Dimensions

- `--layers` — layer count. Drives per-layer git work and the number of `gh`
  round trips.
- `--fillers` — file count in the working tree. This dimension dominated before
  the replay was moved into the object database (`view` checked out a whole
  worktree; `export` ran `reset --hard` between layers), so it is kept to catch
  a regression back to anything working-tree-bound.
- `--trailers` — whether layer commits already carry `stack-pr-branch`. Without
  them, `export` must rewrite the source branch before publishing.
- `--delay-ms` — milliseconds the shim sleeps per layer per reconcile call,
  emulating the real extension's one GraphQL round trip per layer (~370 ms). Use
  `0` to measure stack-pr's own overhead and `370` to see the realistic mix.

## What it reports

Wall time (median/min/max over `--reps`) and **subprocess count**, which is
deterministic where wall time is not — subprocess count and the per-kind
breakdown are the metrics to optimise against. Every workload also asserts that
it took the path it claims to: expected reconcile count, required subprocesses,
and a list of forbidden ones (no `git worktree`, `cherry-pick` or `reset` during
replay). A change that short-circuits to the cheap path, or that reintroduces a
working-tree checkout, fails the run rather than reporting a flattering number.

## What it deliberately does not measure

- **Network.** `fetch` against a local bare remote is ~0.1 s; against GitHub it
  is seconds and varies by connection. Measure that separately on a real
  checkout.
- **The real `gh-stack` extension.** The shim reproduces the protocol and call
  count, not the extension's internals. Use `--delay-ms` to approximate its
  latency, and the real commands to confirm.
