"""Helpers for linting generated GitHub Actions workflows."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


ACTIONLINT_IGNORES = ("SC2016", "SC2034")
BUILTIN_SELF_HOSTED_LABELS = {
    "self-hosted",
    "linux",
    "windows",
    "macos",
    "x64",
    "arm",
    "arm64",
}
INLINE_RUNS_ON_RE = re.compile(r"^\s*runs-on:\s*\[(?P<labels>[^\]]+)\]\s*(?:#.*)?$")

RunFn = Callable[..., subprocess.CompletedProcess[str]]
WhichFn = Callable[[str], str | None]


class WorkflowLintError(RuntimeError):
    """Raised when generated workflow linting cannot prove workflows valid."""


class WorkflowLintUnavailable(WorkflowLintError):
    """Raised when actionlint is not installed or cannot be resolved."""


@dataclass(frozen=True)
class GeneratedWorkflow:
    path: str
    text: str


@dataclass(frozen=True)
class WorkflowLintResult:
    actionlint_bin: str
    workflow_count: int
    workflows: tuple[str, ...]
    custom_runner_labels: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "actionlint_bin": self.actionlint_bin,
            "workflow_count": self.workflow_count,
            "workflows": list(self.workflows),
            "custom_runner_labels": list(self.custom_runner_labels),
        }


def is_github_workflow_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return normalized.startswith(".github/workflows/") and normalized.endswith(
        (".yml", ".yaml")
    )


def _unquote_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_inline_labels(raw: str) -> tuple[str, ...]:
    labels = []
    for item in raw.split(","):
        label = _unquote_yaml_scalar(item)
        if label:
            labels.append(label)
    return tuple(labels)


def self_hosted_runs_on_labels(text: str) -> tuple[tuple[str, ...], ...]:
    """Return each self-hosted ``runs-on`` label set from a workflow text.

    The generated Code Mower workflows currently use inline arrays. This helper
    also supports the simple block-list shape to keep the doctor check useful
    for hand-edited generated workflows.
    """

    blocks: list[tuple[str, ...]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        inline = INLINE_RUNS_ON_RE.match(line)
        if inline:
            labels = _parse_inline_labels(inline.group("labels"))
            if "self-hosted" in {label.lower() for label in labels}:
                blocks.append(labels)
            index += 1
            continue

        stripped = line.strip()
        if stripped != "runs-on:":
            index += 1
            continue

        indent = len(line) - len(line.lstrip(" "))
        labels: list[str] = []
        index += 1
        while index < len(lines):
            child = lines[index]
            child_indent = len(child) - len(child.lstrip(" "))
            child_stripped = child.strip()
            if child_indent <= indent or not child_stripped:
                break
            if child_stripped.startswith("- "):
                labels.append(_unquote_yaml_scalar(child_stripped[2:]))
            index += 1
        if "self-hosted" in {label.lower() for label in labels}:
            blocks.append(tuple(label for label in labels if label))
    return tuple(blocks)


def custom_self_hosted_runner_labels(workflows: Sequence[GeneratedWorkflow]) -> tuple[str, ...]:
    labels: set[str] = set()
    for workflow in workflows:
        for label_set in self_hosted_runs_on_labels(workflow.text):
            for label in label_set:
                if label.lower() not in BUILTIN_SELF_HOSTED_LABELS:
                    labels.add(label)
    return tuple(sorted(labels))


def actionlint_config_text(labels: Sequence[str]) -> str:
    lines = ["self-hosted-runner:", "  labels:"]
    unique = sorted({label for label in labels if label})
    if not unique:
        unique = ["code-mower-audit"]
    lines.extend(f"    - {label}" for label in unique)
    return "\n".join(lines) + "\n"


def _safe_temp_workflow_path(root: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise WorkflowLintError(f"unsafe workflow path for actionlint: {relative_path}")
    destination = root.joinpath(path)
    try:
        destination.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise WorkflowLintError(
            f"workflow path escapes actionlint temp dir: {relative_path}"
        ) from exc
    return destination


def _resolve_actionlint(
    actionlint_bin: str,
    *,
    which: WhichFn = shutil.which,
) -> str:
    resolved = which(actionlint_bin)
    if resolved:
        return resolved
    path = Path(actionlint_bin).expanduser()
    if path.is_file():
        return str(path.resolve())
    raise WorkflowLintUnavailable(
        f"actionlint executable not found: {actionlint_bin}. "
        "Install actionlint and rerun Code Mower init."
    )


def run_actionlint_on_workflows(
    workflows: Sequence[GeneratedWorkflow],
    *,
    actionlint_bin: str = "actionlint",
    run: RunFn = subprocess.run,
    which: WhichFn = shutil.which,
    env: Mapping[str, str] | None = None,
) -> WorkflowLintResult:
    workflow_items = tuple(workflows)
    if not workflow_items:
        return WorkflowLintResult(
            actionlint_bin=actionlint_bin,
            workflow_count=0,
            workflows=(),
            custom_runner_labels=(),
        )

    resolved = _resolve_actionlint(actionlint_bin, which=which)
    custom_labels = custom_self_hosted_runner_labels(workflow_items)
    with tempfile.TemporaryDirectory(prefix="code-mower-actionlint-") as raw:
        temp_root = Path(raw)
        paths: list[Path] = []
        for workflow in workflow_items:
            destination = _safe_temp_workflow_path(temp_root, workflow.path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(workflow.text, encoding="utf-8")
            paths.append(destination)

        config_path = temp_root / "actionlint-self-hosted.yml"
        config_path.write_text(actionlint_config_text(custom_labels), encoding="utf-8")
        command = [resolved, "-config-file", str(config_path)]
        for code in ACTIONLINT_IGNORES:
            command.extend(("-ignore", code))
        command.extend(str(path) for path in paths)
        completed = run(
            command,
            cwd=temp_root,
            env=dict(env) if env is not None else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    if completed.returncode != 0:
        stdout = (completed.stdout or "")[-4000:]
        stderr = (completed.stderr or "")[-4000:]
        raise WorkflowLintError(
            "actionlint failed for generated workflows: "
            f"returncode={completed.returncode}; stdout={stdout!r}; stderr={stderr!r}"
        )
    return WorkflowLintResult(
        actionlint_bin=resolved,
        workflow_count=len(workflow_items),
        workflows=tuple(workflow.path for workflow in workflow_items),
        custom_runner_labels=custom_labels,
    )
