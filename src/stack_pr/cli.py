from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

from stack_pr.git import StackPrError, repository_root
from stack_pr.reconcile import ReconcileResult
from stack_pr.workflow import Options, WorkflowResult, export, view

_RESET = "\033[0m"
_BOLD = "1"
_DIM = "2"
_UNDERLINE = "4"
_RED = "31"
_GREEN = "32"
_YELLOW = "33"
_BLUE = "34"
_MAGENTA = "35"
_CYAN = "36"


def _supports_color(stream: object) -> bool:
    if "NO_COLOR" in os.environ or os.environ.get("FORCE_COLOR") == "0":
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return os.environ.get("TERM") != "dumb" and bool(
        getattr(stream, "isatty", lambda: False)()
    )


def _style(text: str, *codes: str, stream: object | None = None) -> str:
    stream = sys.stdout if stream is None else stream
    if not codes or not _supports_color(stream):
        return text
    return f"\033[{';'.join(codes)}m{text}{_RESET}"


def _status_color(status: str) -> str:
    if status in {"create", "created", "merged", "synced"}:
        return _GREEN
    if status in {"update", "updated"}:
        return _CYAN
    if status in {"blocked", "remove", "removed"}:
        return _RED
    if status in {"queued", "reopen", "reopened", "reorder", "reordered"}:
        return _YELLOW
    return _DIM


@contextmanager
def _progress_step(message: str) -> Iterator[None]:
    started_at = monotonic()
    print(
        _style(f"{message}...", _DIM, stream=sys.stderr),
        end="",
        file=sys.stderr,
        flush=True,
    )
    completed = False
    try:
        yield
        completed = True
    finally:
        status = "done" if completed else "failed"
        color = _GREEN if completed else _RED
        elapsed = _style(f"({monotonic() - started_at:.1f}s)", _DIM, stream=sys.stderr)
        print(
            f" {_style(status, color, _BOLD, stream=sys.stderr)} {elapsed}",
            file=sys.stderr,
        )


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
    actions = result.raw.get("actions", [])
    has_pending_action = not isinstance(actions, list) or any(
        not isinstance(action, dict) or action.get("type") != "merged"
        for action in actions
    )
    return (
        not result.blockers
        and not has_pending_action
        and all(layer.status in settled for layer in result.layers)
        and all(layer.status == "merged" for layer in result.removed_layers)
    )


def _render_plan(
    result: ReconcileResult, workflow: WorkflowResult, *, exported: bool
) -> None:
    titles = {layer.branch: layer.title for layer in workflow.manifest.layers}
    heading = f"{'Result' if exported else 'Plan'} (top to bottom):"
    print(_style(heading, _BOLD))
    for layer in reversed(result.layers):
        title = titles.get(layer.branch, layer.branch)
        pr = (
            f" {_style(f'#{layer.number}', _MAGENTA, _BOLD)}"
            if layer.number is not None
            else ""
        )
        status = _display_status(layer.status, exported=exported)
        display_status = _style(f"{status:<9}", _status_color(status), _BOLD)
        print(f"  {display_status}{pr} {title}")
        if layer.url is not None:
            print(f"             {_style(layer.url, _BLUE, _UNDERLINE)}")
        if layer.blocked_reason is not None:
            print(f"             {_style(layer.blocked_reason, _RED)}")
    for layer in reversed(result.removed_layers):
        pr = (
            f" {_style(f'#{layer.number}', _MAGENTA, _BOLD)}"
            if layer.number is not None
            else ""
        )
        status = _display_status(layer.status, exported=exported)
        display_status = _style(f"{status:<9}", _status_color(status), _BOLD)
        print(f"  {display_status}{pr} {layer.branch}")
        if layer.url is not None:
            print(f"             {_style(layer.url, _BLUE, _UNDERLINE)}")
    if result.stack_url is not None:
        print(
            f"{_style('Native stack:', _BOLD)} "
            f"{_style(result.stack_url, _BLUE, _UNDERLINE)}"
        )
    if exported or _is_up_to_date(result):
        print(_style("Stack is up to date with local commits.", _GREEN, _BOLD))
    elif result.blockers:
        print(
            _style(
                "Stack is not up to date; resolve the blockers before exporting:",
                _RED,
                _BOLD,
            )
        )
        # Reasons for desired layers are printed under them above; stack-level
        # blockers and those of removed or foreign branches belong to none.
        shown = {layer.blocked_reason for layer in result.layers}
        for blocker in result.blockers:
            if blocker not in shown:
                print(f"  {_style(blocker, _RED)}")
    else:
        print(
            _style("Stack is not up to date;", _YELLOW, _BOLD),
            "run",
            _style("`stack-pr export`", _CYAN, _BOLD),
            "to synchronize it.",
        )


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
            print(_style("No commits selected.", _YELLOW))
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
        print(
            f"{_style('Merge with:', _BOLD)} "
            f"{_style(f'gh stack merge {reconcile_result.top_pr_number}', _CYAN, _BOLD)}"
        )


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
        print(
            f"{_style('stack-pr:', _RED, _BOLD, stream=sys.stderr)} {exc}",
            file=sys.stderr,
        )
        return 1
    except FileNotFoundError as exc:
        command = exc.filename or "required command"
        print(
            f"{_style('stack-pr:', _RED, _BOLD, stream=sys.stderr)} "
            f"{command} is not installed",
            file=sys.stderr,
        )
        return 1
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() if isinstance(exc.stderr, str) else ""
        print(
            f"{_style('stack-pr:', _RED, _BOLD, stream=sys.stderr)} "
            f"Git command failed: {detail or exc}",
            file=sys.stderr,
        )
        return 1
    else:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
