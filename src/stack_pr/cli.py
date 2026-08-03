from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

from stack_pr.git import StackPrError, repository_root
from stack_pr.reconcile import ReconcileResult
from stack_pr.workflow import Options, WorkflowResult, export, view


@contextmanager
def _progress_step(message: str) -> Iterator[None]:
    started_at = monotonic()
    print(f"{message}...", end="", file=sys.stderr, flush=True)
    completed = False
    try:
        yield
        completed = True
    finally:
        status = "done" if completed else "failed"
        print(f" {status} ({monotonic() - started_at:.1f}s)", file=sys.stderr)


def create_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stack-pr",
        description="Map one local commit to one pull request in a native GitHub stack.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-B", "--base", help="local range base")
    common.add_argument("-H", "--head", default="HEAD", help="local range head")
    common.add_argument("-T", "--target", help="native stack trunk")
    common.add_argument("-R", "--remote", default="origin", help="Git remote")
    common.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit native reconcile JSON",
    )

    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("view", parents=[common], help="plan native reconciliation")
    export_parser = commands.add_parser(
        "export", parents=[common], help="export commits to a native stack"
    )
    export_parser.add_argument(
        "--draft",
        action="store_true",
        help="create new pull requests as drafts",
    )
    return parser


def _options(args: argparse.Namespace) -> Options:
    return Options(
        base=args.base,
        head=args.head,
        target=args.target,
        remote=args.remote,
        draft=getattr(args, "draft", False),
    )


def _display_status(status: str, *, exported: bool) -> str:
    if status == "none":
        return "synced"
    if not exported:
        return status
    return {
        "create": "created",
        "reopen": "reopened",
        "remove": "removed",
        "reorder": "reordered",
        "update": "updated",
    }.get(status, status)


def _is_up_to_date(result: ReconcileResult) -> bool:
    settled = {"merged", "none"}
    return (
        not result.blockers
        and all(layer.status in settled for layer in result.layers)
        and all(layer.status == "merged" for layer in result.removed_layers)
    )


def _render_plan(
    result: ReconcileResult, workflow: WorkflowResult, *, exported: bool
) -> None:
    titles = {layer.branch: layer.title for layer in workflow.manifest.layers}
    print(f"{'Result' if exported else 'Plan'} (top to bottom):")
    for layer in reversed(result.layers):
        title = titles.get(layer.branch, layer.branch)
        pr = f" #{layer.number}" if layer.number is not None else ""
        status = _display_status(layer.status, exported=exported)
        print(f"  {status:<9}{pr} {title}")
        if layer.url is not None:
            print(f"             {layer.url}")
        if layer.blocked_reason is not None:
            print(f"             {layer.blocked_reason}")
    for layer in reversed(result.removed_layers):
        pr = f" #{layer.number}" if layer.number is not None else ""
        status = _display_status(layer.status, exported=exported)
        print(f"  {status:<9}{pr} {layer.branch}")
        if layer.url is not None:
            print(f"             {layer.url}")
    if result.stack_url is not None:
        print(f"Native stack: {result.stack_url}")
    if exported or _is_up_to_date(result):
        print("Stack is up to date with local commits.")
    elif result.blockers:
        print("Stack is not up to date; resolve the blockers before exporting.")
    else:
        print("Stack is not up to date; run `stack-pr export` to synchronize it.")


def _print_result(result: WorkflowResult, *, json_output: bool, exported: bool) -> None:
    if result.is_empty:
        if json_output:
            print(
                json.dumps(
                    {
                        "version": 1,
                        "dry_run": not exported,
                        "layers": [],
                        "removed_managed_layers": [],
                        "actions": [],
                        "blockers": [],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            print("No commits selected.")
        return

    reconcile_result = result.reconcile_result
    if reconcile_result is None:
        raise StackPrError("native reconciliation returned no result")
    if json_output:
        print(json.dumps(reconcile_result.raw, sort_keys=True, separators=(",", ":")))
        return

    _render_plan(reconcile_result, result, exported=exported)
    if (
        exported
        and reconcile_result.has_native_stack
        and reconcile_result.top_pr_number is not None
    ):
        print(f"Merge with: gh stack merge {reconcile_result.top_pr_number}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = create_argparser()
    args = parser.parse_args(argv)
    try:
        repo = repository_root(Path.cwd())
        progress = None if args.json_output else _progress_step
        if args.command == "view":
            result = view(repo, _options(args), progress=progress)
            exported = False
        else:
            result = export(repo, _options(args), progress=progress)
            exported = True
        _print_result(result, json_output=args.json_output, exported=exported)
    except StackPrError as exc:
        print(f"stack-pr: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        command = exc.filename or "required command"
        print(f"stack-pr: {command} is not installed", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if isinstance(exc.stderr, str) else ""
        print(f"stack-pr: Git command failed: {detail or exc}", file=sys.stderr)
        return 1
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
