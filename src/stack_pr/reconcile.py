from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stack_pr.git import StackPrError, require_git_version

PROTOCOL_VERSION = 1
LAYER_STATUSES = {
    "blocked",
    "create",
    "merged",
    "none",
    "queued",
    "reopen",
    "remove",
    "reorder",
    "unchanged",
    "update",
}


@dataclass(frozen=True)
class ManifestLayer:
    branch: str
    title: str
    body: str
    draft: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "title": self.title,
            "body": self.body,
            "draft": self.draft,
        }


@dataclass(frozen=True)
class Manifest:
    base: str
    remote: str
    layers: tuple[ManifestLayer, ...]
    version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "base": self.base,
            "remote": self.remote,
            "layers": [layer.to_dict() for layer in self.layers],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


@dataclass(frozen=True)
class ResultLayer:
    branch: str
    status: str
    number: int | None
    url: str | None
    head_sha: str | None
    blocked_reason: str | None


@dataclass(frozen=True)
class ReconcileResult:
    raw: dict[str, Any]
    layers: tuple[ResultLayer, ...]
    removed_layers: tuple[ResultLayer, ...]

    @property
    def merged_branches(self) -> set[str]:
        return {layer.branch for layer in self.layers if layer.status == "merged"}

    @property
    def blockers(self) -> list[str]:
        blockers: list[str] = []
        for key in ("blockers", "queued"):
            raw_blockers = self.raw.get(key)
            if isinstance(raw_blockers, list):
                blockers.extend(_blocker_text(blocker) for blocker in raw_blockers)
            elif raw_blockers:
                blockers.append(_blocker_text(raw_blockers))
        if self.raw.get("blocked") is True:
            blockers.append("native reconciliation is blocked")
        elif self.raw.get("blocked"):
            blockers.append(_blocker_text(self.raw["blocked"]))
        for layer in self.layers:
            if layer.status in {"blocked", "queued"}:
                blockers.append(
                    layer.blocked_reason
                    or f"{layer.branch} is {layer.status} and cannot be reconciled"
                )
        return list(dict.fromkeys(blockers))

    @property
    def has_native_stack(self) -> bool:
        stack = self.raw.get("stack")
        return bool(stack) or isinstance(self.raw.get("stack_url"), str)

    @property
    def stack_url(self) -> str | None:
        direct = self.raw.get("stack_url")
        if isinstance(direct, str):
            return direct
        stack = self.raw.get("stack")
        if isinstance(stack, str):
            return stack
        if isinstance(stack, dict):
            url = stack.get("url")
            if isinstance(url, str):
                return url
        return None

    @property
    def top_pr_number(self) -> int | None:
        for layer in reversed(self.layers):
            if (
                layer.status not in {"blocked", "merged", "queued", "remove"}
                and layer.number is not None
            ):
                return layer.number
        return None


def _blocker_text(blocker: object) -> str:
    if isinstance(blocker, dict):
        for key in ("message", "reason", "branch"):
            value = blocker.get(key)
            if isinstance(value, str):
                return value
    return str(blocker)


def preflight(repo: Path) -> None:
    require_git_version(repo)
    result = subprocess.run(
        ["gh", "stack", "reconcile", "--protocol-version"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != str(PROTOCOL_VERSION):
        raise StackPrError(
            "installed gh stack does not support reconcile protocol v1; "
            "install the paired extension with `gh extension remove stack` then "
            "`gh extension install dukebw/gh-stack`"
        )


def github_login(repo: Path) -> str:
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    login = result.stdout.strip()
    if result.returncode != 0 or not login:
        detail = result.stderr.strip() or "GitHub login was empty"
        raise StackPrError(
            f"could not determine the authenticated GitHub login: {detail}"
        )
    return login


def reconcile(repo: Path, manifest: Manifest, *, dry_run: bool) -> ReconcileResult:
    command = ["gh", "stack", "reconcile", "--input", "-", "--json"]
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(
        command,
        cwd=repo,
        input=manifest.to_json(),
        text=True,
        capture_output=True,
        check=False,
    )
    parsed: ReconcileResult | None = None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        if result.returncode == 0:
            raise StackPrError("gh stack reconcile returned invalid JSON") from exc
    else:
        parsed = parse_result(payload)

    if result.returncode != 0:
        if dry_run and parsed is not None and parsed.blockers:
            return parsed
        blockers = "; ".join(parsed.blockers) if parsed is not None else ""
        detail = (
            blockers
            or result.stderr.strip()
            or result.stdout.strip()
            or "unknown error"
        )
        raise StackPrError(f"gh stack reconcile failed: {detail}")
    if parsed is None:
        raise StackPrError("gh stack reconcile returned no JSON result")
    return parsed


def _layer_status(raw_layer: dict[str, Any], pr: object) -> str:
    native_status = raw_layer.get("status")
    action = raw_layer.get("action")
    if raw_layer.get("merged") is True or (
        isinstance(pr, dict) and pr.get("merged") is True
    ):
        return "merged"
    if raw_layer.get("queued") is True or (
        isinstance(pr, dict) and pr.get("queued") is True
    ):
        return "queued"
    if raw_layer.get("blocked") is True or native_status == "blocked":
        return "blocked"
    if native_status == "merged":
        return "merged"
    if native_status == "queued":
        return "queued"
    status = action if isinstance(action, str) else native_status
    if not isinstance(status, str) or status not in LAYER_STATUSES:
        raise StackPrError(f"unsupported status {status!r}")
    return status


def _layer_blocker_reason(
    payload: dict[str, Any], raw_layer: dict[str, Any], branch: str
) -> str | None:
    reason = raw_layer.get("reason", raw_layer.get("blocked_reason"))
    if isinstance(reason, str):
        return reason
    raw_blockers = payload.get("blockers")
    if not isinstance(raw_blockers, list):
        return None
    for blocker in raw_blockers:
        message = blocker.get("message") if isinstance(blocker, dict) else None
        if (
            isinstance(blocker, dict)
            and blocker.get("branch") == branch
            and isinstance(message, str)
        ):
            return message
    return None


def _parse_layers(
    payload: dict[str, Any], raw_layers: list[object], field_name: str
) -> tuple[ResultLayer, ...]:
    layers: list[ResultLayer] = []
    for index, raw_layer in enumerate(raw_layers):
        if not isinstance(raw_layer, dict):
            raise StackPrError(f"reconcile {field_name} layer {index} is not an object")
        branch = raw_layer.get("branch")
        if not isinstance(branch, str) or not branch:
            raise StackPrError(f"reconcile {field_name} layer {index} has no branch")
        pr = raw_layer.get(
            "pr", raw_layer.get("pull_request", raw_layer.get("pullRequest"))
        )
        try:
            status = _layer_status(raw_layer, pr)
        except StackPrError as exc:
            raise StackPrError(
                f"reconcile {field_name} layer {branch!r} has {exc}"
            ) from exc

        number_value = raw_layer.get(
            "number", raw_layer.get("pr_number", raw_layer.get("prNumber"))
        )
        url_value = raw_layer.get(
            "url", raw_layer.get("pr_url", raw_layer.get("prUrl"))
        )
        if isinstance(pr, dict):
            number_value = pr.get("number", number_value)
            url_value = pr.get("url", url_value)
        number = number_value if isinstance(number_value, int) else None
        url = url_value if isinstance(url_value, str) else None
        head_sha_value = pr.get("head_sha") if isinstance(pr, dict) else None
        head_sha = head_sha_value if isinstance(head_sha_value, str) else None
        reason = _layer_blocker_reason(payload, raw_layer, branch)
        layers.append(ResultLayer(branch, status, number, url, head_sha, reason))
    return tuple(layers)


def parse_result(payload: object) -> ReconcileResult:
    if not isinstance(payload, dict):
        raise StackPrError("gh stack reconcile returned a non-object response")
    if payload.get("version") != PROTOCOL_VERSION:
        raise StackPrError(
            "gh stack reconcile returned an unsupported protocol version"
        )
    raw_layers = payload.get("layers")
    if not isinstance(raw_layers, list):
        raise StackPrError("gh stack reconcile response has no layers array")
    raw_removed = payload.get("removed_managed_layers", [])
    if not isinstance(raw_removed, list):
        raise StackPrError(
            "gh stack reconcile response has an invalid removed_managed_layers array"
        )

    return ReconcileResult(
        payload,
        _parse_layers(payload, raw_layers, "desired"),
        _parse_layers(payload, raw_removed, "removed"),
    )
