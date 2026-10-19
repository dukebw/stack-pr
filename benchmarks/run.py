"""Tier-1 benchmark for stack-pr: hermetic, repeatable, attributing.

Runs the real CLI against a disposable fixture repo with a fake `gh` on PATH, so
timings contain stack-pr's own work and nothing else (no network, no GitHub, no
real extension). Wall time is the headline; subprocess counts and per-command
breakdown are the stable metrics to optimise against, because they do not move
with machine load.

    .venv/bin/python -m benchmarks.run --preset quick
    .venv/bin/python -m benchmarks.run --preset baseline --json baseline.json

Every workload asserts that it exercised the path it claims to: a change that
silently short-circuits to the cheap path fails the run instead of reporting a
flattering number.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from benchmarks.fixture import Fixture, build

TRACER_DIR = Path(__file__).parent / "_tracer"
SHIM = Path(__file__).parent / "gh_shim.py"
BENCH_ROOT = Path(os.environ.get("SP_BENCH_ROOT", "/private/tmp/stack-pr-bench"))
# The second reconcile is the one that must observe materialized layer branches.
RECONCILES_BEFORE_MATERIALIZED = 2


@dataclass(frozen=True)
class Workload:
    name: str
    status: str
    mutates: bool
    required_calls: tuple[str, ...]
    forbidden_calls: tuple[str, ...]
    reconciles: int

    @property
    def command(self) -> tuple[str, ...]:
        return ("view",) if self.name.startswith("view") else ("export",)


WORKLOADS: dict[str, Workload] = {
    "view-blocked": Workload(
        name="view-blocked",
        status="blocked",
        mutates=False,
        required_calls=(),
        forbidden_calls=(),
        reconciles=1,
    ),
    "view-clean": Workload(
        name="view-clean",
        status="create",
        mutates=False,
        required_calls=("git merge-tree", "git commit-tree", "git update-ref"),
        # Replay is object-database only: no checkout, no worktree, no reset.
        forbidden_calls=("git worktree", "git cherry-pick", "git reset"),
        reconciles=2,
    ),
    "export": Workload(
        name="export",
        status="create",
        mutates=True,
        required_calls=("git merge-tree", "git commit-tree", "git update-ref"),
        forbidden_calls=("git worktree", "git cherry-pick", "git reset"),
        reconciles=2,
    ),
}


@dataclass
class Run:
    wall: float
    returncode: int
    stderr: str
    trace: list[dict[str, object]]
    calls: list[dict[str, object]]


def _command_kind(argv: list[str]) -> str:
    if argv[0] == "gh":
        if argv[1:3] == ["stack", "reconcile"]:
            if "--protocol-version" in argv:
                return "gh stack reconcile [protocol-version]"
            return "gh stack reconcile"
        return " ".join(argv[:3])
    if argv[0] == "git":
        sub = argv[1]
        if sub == "-c":
            sub = argv[3]
        return f"git {sub}"
    return argv[0]


def _invoke(
    fixture: Fixture, workload: Workload, *, delay_ms: int, verify: bool
) -> Run:
    fixture.clear_state()
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"GH_DEBUG", "SP_GH_VERIFY", "SP_TRACE", "PYTHONPATH"}
    }
    env.update(
        {
            "PATH": f"{fixture.bin_dir}:{os.environ['PATH']}",
            "PYTHONPATH": str(TRACER_DIR),
            "SP_TRACE": str(fixture.trace),
            "SP_GH_CALL_LOG": str(fixture.call_log),
            "SP_GH_STATUS": workload.status,
            "SP_GH_LAYER_DELAY_MS": str(delay_ms),
            "NO_COLOR": "1",
        }
    )
    if verify:
        env["SP_GH_VERIFY"] = "1"

    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", "stack_pr", *workload.command],
        cwd=fixture.repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    wall = time.perf_counter() - started

    trace = (
        [
            json.loads(line)
            for line in fixture.trace.read_text().splitlines()
            if line.strip()
        ]
        if fixture.trace.exists()
        else []
    )
    calls = (
        [
            json.loads(line)
            for line in fixture.call_log.read_text().splitlines()
            if line.strip()
        ]
        if fixture.call_log.exists()
        else []
    )
    return Run(wall, result.returncode, result.stderr.strip(), trace, calls)


def _check(workload: Workload, run: Run) -> None:
    if run.returncode != 0:
        raise SystemExit(
            f"{workload.name}: stack-pr exited {run.returncode}\n{run.stderr}"
        )
    if len(run.calls) != workload.reconciles:
        raise SystemExit(
            f"{workload.name}: expected {workload.reconciles} reconcile calls, "
            f"saw {len(run.calls)} (cheap path taken?)"
        )
    seen = {" ".join(record["argv"]) for record in run.trace}
    missing = [
        needle
        for needle in workload.required_calls
        if not any(needle in command for command in seen)
    ]
    if missing:
        raise SystemExit(f"{workload.name}: path not exercised, missing {missing}")


def _verify_materialized(fixture: Fixture, workload: Workload, delay_ms: int) -> bool:
    """Prove reconcile #2 observed the materialized layer branches."""
    run = _invoke(fixture, workload, delay_ms=delay_ms, verify=True)
    _check(workload, run)
    if len(run.calls) < RECONCILES_BEFORE_MATERIALIZED:
        return False
    present = run.calls[1].get("branches_present") or {}
    return bool(present) and all(present.values())


def _measure(
    fixture: Fixture, workload: Workload, *, reps: int, warmup: int, delay_ms: int
) -> dict[str, object]:
    runs: list[Run] = []
    for index in range(warmup + reps):
        fixture.restore()
        run = _invoke(fixture, workload, delay_ms=delay_ms, verify=False)
        _check(workload, run)
        if index >= warmup:
            runs.append(run)

    walls = [run.wall for run in runs]
    counts = [len(run.trace) for run in runs]
    if len(set(counts)) != 1:
        raise SystemExit(
            f"{workload.name}: subprocess count is not stable across reps: {counts}"
        )

    # Attribute the rep closest to the median so a cold-cache outlier in one
    # rep cannot misreport where the time went.
    median = statistics.median(walls)
    representative = min(runs, key=lambda run: abs(run.wall - median))

    spent: defaultdict[str, float] = defaultdict(float)
    tally: Counter[str] = Counter()
    for record in representative.trace:
        kind = _command_kind(record["argv"])
        spent[kind] += float(record["elapsed"])
        tally[kind] += 1

    return {
        "workload": workload.name,
        "median_wall": round(median, 3),
        "min_wall": round(min(walls), 3),
        "max_wall": round(max(walls), 3),
        "spread": round(max(walls) - min(walls), 3),
        "walls": [round(wall, 3) for wall in walls],
        "subprocesses": counts[0],
        "reconciles": len(representative.calls),
        "by_kind": {
            kind: {"count": tally[kind], "seconds": round(spent[kind], 3)}
            for kind in sorted(spent, key=lambda name: -spent[name])
        },
    }


def _fingerprint() -> dict[str, object]:
    def _out(*args: str) -> str:
        return subprocess.run(
            args, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {
        "python": platform.python_version(),
        "git": _out("git", "--version"),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "stack_pr_sha": _out(
            "git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"
        ),
    }


def _pick(
    args: argparse.Namespace,
    preset: dict[str, object],
    name: str,
    cast: Callable[[str], object],
) -> list[object]:
    raw = getattr(args, name)
    if raw is not None:
        return [cast(part) for part in raw.split(",")]
    value = preset[name]
    items = value if isinstance(value, list) else [value]
    return [cast(item) for item in items]


def _run_cell(
    args: argparse.Namespace,
    workload: Workload,
    *,
    layers: int,
    fillers: int,
    reps: int,
) -> dict[str, object]:
    root = BENCH_ROOT / f"{args.trailers}-L{layers}-F{fillers}"
    fixture = build(
        root,
        layers=layers,
        fillers=fillers,
        trailers=args.trailers == "all",
        python=Path(sys.executable),
        shim=SHIM,
    )
    measured = _measure(
        fixture, workload, reps=reps, warmup=args.warmup, delay_ms=args.delay_ms
    )
    measured.update(
        {
            "layers": layers,
            "fillers": fillers,
            "trailers": args.trailers,
            "delay_ms": args.delay_ms,
            "materialized": _verify_materialized(fixture, workload, args.delay_ms),
        }
    )
    print(
        f"{workload.name:<14}{layers:>7}{fillers:>7}"
        f"{measured['median_wall']:>9.3f}{measured['min_wall']:>8.3f}"
        f"{measured['max_wall']:>8.3f}{measured['subprocesses']:>7}"
        f"{measured['reconciles']:>7}"
    )
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return measured


def _print_header() -> None:
    header = (
        f"{'workload':<14}{'layers':>7}{'files':>7}{'median':>9}{'min':>8}"
        f"{'max':>8}{'procs':>7}{'reconc':>7}"
    )
    print(header)
    print("-" * len(header))


def _print_attribution(results: list[dict[str, object]]) -> None:
    print()
    print("Attribution (last rep, seconds by command kind):")
    for result in results:
        total = float(result["median_wall"])
        print(
            f"  {result['workload']} L{result['layers']} "
            f"F{result['fillers']}   median {total:.3f}s"
        )
        accounted = 0.0
        for kind, stats in result["by_kind"].items():
            accounted += stats["seconds"]
            share = 100 * stats["seconds"] / total if total else 0
            print(
                f"    {kind:<42}{stats['count']:>4}x{stats['seconds']:>8.2f}s"
                f"{share:>6.0f}%"
            )
        unaccounted = total - accounted
        share = 100 * unaccounted / total if total else 0
        print(
            f"    {'(python startup + process overhead)':<42}{'':>4} "
            f"{unaccounted:>7.2f}s{share:>6.0f}%"
        )


PRESETS: dict[str, dict[str, object]] = {
    "quick": {
        "layers": [1, 5],
        "fillers": [500],
        "workloads": ["view-clean"],
        "reps": 2,
    },
    "baseline": {
        "layers": [1, 5, 13],
        "fillers": [1000],
        "workloads": ["view-blocked", "view-clean", "export"],
        "reps": 5,
    },
    "scale": {
        "layers": [13],
        "fillers": [33804],
        "workloads": ["view-clean", "export"],
        "reps": 3,
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.run")
    parser.add_argument("--preset", choices=sorted(PRESETS), default="baseline")
    parser.add_argument("--layers", default=None, help="comma-separated layer counts")
    parser.add_argument("--fillers", default=None, help="comma-separated filler counts")
    parser.add_argument(
        "--workloads", default=None, help="comma-separated workload names"
    )
    parser.add_argument("--reps", type=int, default=None)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument(
        "--trailers",
        choices=("none", "all"),
        default="none",
        help="whether layer commits already carry stack-pr-branch",
    )
    parser.add_argument(
        "--delay-ms", type=int, default=0, help="shim latency per layer"
    )
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument("--keep", action="store_true", help="keep fixture directories")
    args = parser.parse_args(argv)

    preset = PRESETS[args.preset]
    layers_list = _pick(args, preset, "layers", int)
    fillers_list = _pick(args, preset, "fillers", int)
    workloads_list = _pick(args, preset, "workloads", str)
    reps = args.reps if args.reps is not None else int(preset["reps"])

    unknown = [name for name in workloads_list if name not in WORKLOADS]
    if unknown:
        raise SystemExit(f"unknown workloads: {unknown}")

    print(_fingerprint())
    print(
        f"reps={reps} warmup={args.warmup} trailers={args.trailers} delay_ms={args.delay_ms}"
    )
    print()
    _print_header()

    results = [
        _run_cell(
            args,
            WORKLOADS[workload_name],
            layers=layers,
            fillers=fillers,
            reps=reps,
        )
        for workload_name in workloads_list
        for layers in layers_list
        for fillers in fillers_list
    ]
    _print_attribution(results)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(
                {"fingerprint": _fingerprint(), "results": results},
                indent=2,
            )
            + "\n"
        )
        print(f"\nwrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
