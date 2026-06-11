#!/usr/bin/env python3
"""Rehearse migration from product-local Code Mower tools to the package."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence


DEFAULT_COMMANDS = (
    ("providers", "list"),
    (
        "prompts",
        "validate",
        "--lenses",
        "base-audit,calibration-policy,package-runtime",
        "--json",
    ),
)
CALIBRATION_CANDIDATES = (
    ".code-mower.generated/calibration-corpus.json",
    "tools/calibration_corpus.json",
    "tools/calibration_corpus.example.json",
    "templates/calibration-corpus.json",
)
CALIBRATION_EVIDENCE_ADDITIVE_KEYS = frozenset(
    {
        "audit_input_insufficient_count",
        "audit_input_insufficient_runs",
        "result_category",
    }
)


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout_sha256: str
    stderr_preview: str
    stdout_preview: str

    @classmethod
    def from_completed(
        cls,
        command: Sequence[str],
        completed: subprocess.CompletedProcess[str],
    ) -> "CommandResult":
        return cls(
            command=tuple(command),
            returncode=int(completed.returncode),
            stdout_sha256=hashlib.sha256(
                completed.stdout.encode("utf-8", errors="replace")
            ).hexdigest(),
            stdout_preview=completed.stdout[:800],
            stderr_preview=completed.stderr[:1200],
        )


@dataclass(frozen=True)
class RunOutput:
    public: CommandResult
    stdout: str


def _resolve_command(command_text: str) -> tuple[str, ...]:
    parts = tuple(part for part in command_text.split(" ") if part)
    if not parts:
        raise ValueError("command must not be empty")
    return parts


def _default_local_command(repo_path: Path) -> tuple[str, ...] | None:
    command_candidate = repo_path / "tools" / "code_mower"
    if command_candidate.is_file():
        return (str(command_candidate),)
    candidate = repo_path / "tools" / "code_mower_cli.py"
    if candidate.is_file():
        return (sys.executable, str(candidate))
    return None


def _default_package_command() -> tuple[str, ...]:
    resolved = shutil.which("code-mower")
    return (resolved or "code-mower",)


def _run(command: Sequence[str], *, cwd: Path, timeout: int) -> RunOutput:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    return RunOutput(
        public=CommandResult.from_completed(command, completed),
        stdout=completed.stdout,
    )


def _json_payload(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _prune_additive_calibration_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _prune_additive_calibration_keys(item)
            for key, item in value.items()
            if key not in CALIBRATION_EVIDENCE_ADDITIVE_KEYS
        }
    if isinstance(value, list):
        return [_prune_additive_calibration_keys(item) for item in value]
    return value


def _compatibility_for(
    suffix: Sequence[str],
    local: RunOutput,
    package: RunOutput,
) -> tuple[bool, str]:
    if local.public.returncode != package.public.returncode:
        return False, "returncode_mismatch"
    if local.public.stdout_sha256 == package.public.stdout_sha256:
        return True, "exact_stdout_match"
    if tuple(suffix) == ("providers", "list"):
        local_providers = {line.strip() for line in local.stdout.splitlines() if line.strip()}
        package_providers = {line.strip() for line in package.stdout.splitlines() if line.strip()}
        if local_providers and local_providers <= package_providers:
            return True, "package_provider_superset"
    if suffix[:2] == ("prompts", "validate"):
        local_payload = _json_payload(local.stdout)
        package_payload = _json_payload(package.stdout)
        if isinstance(local_payload, dict) and isinstance(package_payload, dict):
            local_payload.pop("prompt_dir", None)
            package_payload.pop("prompt_dir", None)
            if local_payload == package_payload:
                return True, "prompt_dir_only_difference"
    if suffix[:2] == ("calibration", "evidence"):
        local_payload = _json_payload(local.stdout)
        package_payload = _json_payload(package.stdout)
        if (
            isinstance(local_payload, dict)
            and isinstance(package_payload, dict)
            and _prune_additive_calibration_keys(local_payload)
            == _prune_additive_calibration_keys(package_payload)
        ):
            return True, "calibration_evidence_additive_schema_only"
    return False, "stdout_mismatch"


def _safe_commands(repo_path: Path) -> list[tuple[str, ...]]:
    commands = list(DEFAULT_COMMANDS)
    for candidate in CALIBRATION_CANDIDATES:
        if (repo_path / candidate).is_file():
            commands.append(("calibration", "evidence", candidate, "--json"))
            break
    return commands


def run_wrapper_rehearsal(
    *,
    repo_path: Path,
    local_command: Sequence[str] | None = None,
    package_command: Sequence[str] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    repo_path = repo_path.expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repo path is not a directory: {repo_path}")
    local_command = tuple(local_command) if local_command else _default_local_command(repo_path)
    package_command = tuple(package_command or _default_package_command())
    if not local_command:
        raise ValueError(
            "could not infer local product Code Mower command; pass --local-command"
        )

    comparisons: list[dict[str, Any]] = []
    for suffix in _safe_commands(repo_path):
        local = _run((*local_command, *suffix), cwd=repo_path, timeout=timeout)
        package = _run((*package_command, *suffix), cwd=repo_path, timeout=timeout)
        match, reason = _compatibility_for(suffix, local, package)
        comparisons.append(
            {
                "suffix": list(suffix),
                "match": match,
                "reason": reason,
                "local": asdict(local.public),
                "package": asdict(package.public),
            }
        )

    mismatches = [item for item in comparisons if not item["match"]]
    return {
        "mode": "code-mower-product-wrapper-rehearsal",
        "status": "pass" if not mismatches else "warn",
        "repo_path": str(repo_path),
        "local_command": list(local_command),
        "package_command": list(package_command),
        "comparison_count": len(comparisons),
        "mismatch_count": len(mismatches),
        "comparisons": comparisons,
        "notes": [
            "Only read-only commands are compared.",
            "A pass means this repo is a candidate for CODE_MOWER_USE_STANDALONE shadow mode, not that local tools can be deleted yet.",
        ],
    }


def render_text(payload: dict[str, Any]) -> str:
    lines = [
        "Code Mower product wrapper rehearsal",
        f"Status: {payload['status']}",
        f"Repo: {payload['repo_path']}",
        f"Comparisons: {payload['comparison_count']} ({payload['mismatch_count']} mismatches)",
        "",
    ]
    for item in payload["comparisons"]:
        status = "PASS" if item["match"] else "WARN"
        lines.append(f"- {status} {' '.join(item['suffix'])}")
    return "\n".join(lines) + "\n"


def _relative_existing_files(repo_path: Path, candidates: Sequence[str]) -> list[str]:
    return [
        candidate
        for candidate in candidates
        if (repo_path / candidate).exists()
    ]


def _glob_relative_files(repo_path: Path, patterns: Sequence[str]) -> list[str]:
    found: set[str] = set()
    for pattern in patterns:
        for path in repo_path.glob(pattern):
            if path.is_file():
                found.add(path.relative_to(repo_path).as_posix())
    return sorted(found)


def _workflow_file_references(
    repo_path: Path,
    relative_files: Sequence[str],
) -> list[dict[str, Any]]:
    workflow_root = repo_path / ".github" / "workflows"
    if not workflow_root.is_dir():
        return []
    tracked = set(relative_files)
    references: list[dict[str, Any]] = []
    for workflow in sorted(
        path
        for pattern in ("*.yml", "*.yaml")
        for path in workflow_root.glob(pattern)
        if path.is_file()
    ):
        try:
            lines = workflow.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            lines = workflow.read_text(encoding="utf-8", errors="replace").splitlines()
        for line_number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for relative_file in tracked:
                if relative_file in line and _line_requires_workflow_file(
                    line,
                    relative_file,
                ):
                    references.append(
                        {
                            "workflow": workflow.relative_to(repo_path).as_posix(),
                            "line": line_number,
                            "file": relative_file,
                            "text": stripped[:240],
                        }
                    )
    return references


def _line_requires_workflow_file(line: str, relative_file: str) -> bool:
    escaped = re.escape(relative_file)
    return bool(
        re.search(rf"\bpython3?\b[^\n]*{escaped}", line)
        or re.search(rf"\[\s*!\s*-f\s+{escaped}\s*\]", line)
        or re.search(rf"\btest\s+!\s+-f\s+{escaped}\b", line)
        or re.search(rf"\btest\s+-f\s+{escaped}\b", line)
    )


def render_mirror_removal_plan(
    *,
    repo_path: Path,
    shadow_cycles: int,
    required_shadow_cycles: int,
    standalone_default_cycles: int,
    required_standalone_default_cycles: int,
) -> dict[str, Any]:
    repo_path = repo_path.expanduser().resolve()
    if not repo_path.is_dir():
        raise ValueError(f"repo path is not a directory: {repo_path}")

    support_files = _relative_existing_files(
        repo_path,
        (
            "tools/code_mower",
            "tools/code_mower_standalone_pin.env",
            "tools/code_mower_standalone_shadow.sh",
        ),
    )
    local_command = _default_local_command(repo_path)
    mirrored_candidates = _glob_relative_files(
        repo_path,
        (
            "tools/code_mower_*.py",
            "tools/*_audit_pr.py",
            "tools/*_labeler.py",
            "tools/lane_prompts/*.md",
            "tools/calibration_corpus*.json",
            "tools/reviewer_spend*.json",
            "tools/context_packs*.json",
            "tools/CODE_MOWER*.md",
        ),
    )
    workflow_mirror_references = _workflow_file_references(
        repo_path,
        mirrored_candidates,
    )
    ready_for_shadow = {
        "standalone_pin_file_present": "tools/code_mower_standalone_pin.env"
        in support_files,
        "standalone_shadow_wrapper_present": "tools/code_mower_standalone_shadow.sh"
        in support_files,
        "product_local_command_present": local_command is not None,
        "mirrored_files_detected": bool(mirrored_candidates),
    }
    support_ready = all(ready_for_shadow.values())
    shadow_ready = support_ready and shadow_cycles >= required_shadow_cycles
    cycle_ready_for_removal = (
        shadow_ready
        and standalone_default_cycles >= required_standalone_default_cycles
    )
    removal_ready = cycle_ready_for_removal and not workflow_mirror_references
    blockers = []
    if not ready_for_shadow["standalone_pin_file_present"]:
        blockers.append("add tools/code_mower_standalone_pin.env")
    if not ready_for_shadow["standalone_shadow_wrapper_present"]:
        blockers.append("add tools/code_mower_standalone_shadow.sh")
    if not ready_for_shadow["product_local_command_present"]:
        blockers.append("identify the product-local Code Mower command")
    if shadow_cycles < required_shadow_cycles:
        blockers.append(
            f"complete {required_shadow_cycles - shadow_cycles} more clean shadow cycle(s)"
        )
    if shadow_ready and standalone_default_cycles < required_standalone_default_cycles:
        blockers.append(
            "flip to pinned standalone by default and complete "
            f"{required_standalone_default_cycles - standalone_default_cycles} "
            "clean standalone-default cycle(s)"
        )
    if workflow_mirror_references:
        blockers.append(
            "migrate workflow references from removable mirrored files to "
            "standalone wrapper commands before deleting mirrors"
        )
    if removal_ready:
        status = "ready_to_remove_mirrors"
    elif cycle_ready_for_removal:
        status = "workflow_entrypoint_migration_required"
    elif shadow_ready:
        status = "ready_to_flip_default"
    else:
        status = "shadow_required"
    return {
        "mode": "code-mower-mirror-removal-plan",
        "repo_path": str(repo_path),
        "status": status,
        "shadow_cycles": shadow_cycles,
        "required_shadow_cycles": required_shadow_cycles,
        "standalone_default_cycles": standalone_default_cycles,
        "required_standalone_default_cycles": required_standalone_default_cycles,
        "support_files": support_files,
        "support_file_count": len(support_files),
        "local_command": list(local_command or ()),
        "mirrored_file_count": len(mirrored_candidates),
        "mirrored_files": mirrored_candidates,
        "workflow_mirrored_file_reference_count": len(workflow_mirror_references),
        "workflow_mirrored_file_references": workflow_mirror_references,
        "readiness": ready_for_shadow,
        "blockers": blockers,
        "steps": [
            "Run code-mower migration wrapper-rehearsal against the pinned standalone release and require mismatch_count: 0.",
            "Run at least the required number of clean product release cycles with the pinned standalone wrapper available.",
            "Move workflow calls from mirrored Python files to tools/code_mower standalone wrapper subcommands.",
            "Flip product workflows or wrapper defaults to the pinned standalone command while keeping the product-local mirrors in place.",
            "Run the normal product merge gates and post-merge deploy checks.",
            "Remove mirrored implementation files in a dedicated PR after the standalone default cycle stays clean.",
        ],
        "notes": [
            "This plan is intentionally conservative: mirrored files are inventory, not deletion approval.",
            "Keep support wrappers such as tools/code_mower and the standalone pin/shadow files during mirror removal.",
            "Keep product feature work independent from mirror-removal PRs.",
        ],
    }


def render_mirror_removal_text(payload: dict[str, Any]) -> str:
    lines = [
        "Code Mower mirror-removal plan",
        f"Status: {payload['status']}",
        f"Repo: {payload['repo_path']}",
        f"Shadow cycles: {payload['shadow_cycles']}/{payload['required_shadow_cycles']}",
        "Standalone-default cycles: "
        f"{payload['standalone_default_cycles']}/"
        f"{payload['required_standalone_default_cycles']}",
        f"Mirrored files detected: {payload['mirrored_file_count']}",
        "Workflow mirrored-file references: "
        f"{payload.get('workflow_mirrored_file_reference_count', 0)}",
        "",
        "Next steps:",
    ]
    for step in payload["steps"]:
        lines.append(f"- {step}")
    if payload["blockers"]:
        lines.append("")
        lines.append("Blockers:")
        for blocker in payload["blockers"]:
            lines.append(f"- {blocker}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    wrapper = subparsers.add_parser("wrapper-rehearsal")
    wrapper.add_argument("--repo-path", type=Path, default=Path.cwd())
    wrapper.add_argument(
        "--local-command",
        default="",
        help="product-local command prefix, e.g. 'python tools/code_mower_cli.py'",
    )
    wrapper.add_argument(
        "--package-command",
        default="",
        help="standalone command prefix, e.g. 'code-mower'",
    )
    wrapper.add_argument("--timeout", type=int, default=60)
    wrapper.add_argument("--json", action="store_true")
    mirror = subparsers.add_parser("mirror-removal-plan")
    mirror.add_argument("--repo-path", type=Path, default=Path.cwd())
    mirror.add_argument("--shadow-cycles", type=int, default=0)
    mirror.add_argument("--required-shadow-cycles", type=int, default=1)
    mirror.add_argument("--standalone-default-cycles", type=int, default=0)
    mirror.add_argument("--required-standalone-default-cycles", type=int, default=1)
    mirror.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "wrapper-rehearsal":
        try:
            payload = run_wrapper_rehearsal(
                repo_path=args.repo_path,
                local_command=_resolve_command(args.local_command)
                if args.local_command
                else None,
                package_command=_resolve_command(args.package_command)
                if args.package_command
                else None,
                timeout=args.timeout,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            payload = {
                "mode": "code-mower-product-wrapper-rehearsal",
                "status": "fail",
                "error": str(exc),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"wrapper rehearsal failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_text(payload), end="")
        return 0 if payload["status"] == "pass" else 1

    if args.command == "mirror-removal-plan":
        try:
            payload = render_mirror_removal_plan(
                repo_path=args.repo_path,
                shadow_cycles=args.shadow_cycles,
                required_shadow_cycles=args.required_shadow_cycles,
                standalone_default_cycles=args.standalone_default_cycles,
                required_standalone_default_cycles=args.required_standalone_default_cycles,
            )
        except ValueError as exc:
            payload = {
                "mode": "code-mower-mirror-removal-plan",
                "status": "fail",
                "error": str(exc),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"mirror-removal plan failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_mirror_removal_text(payload), end="")
        return 0

    raise AssertionError(f"unhandled migration command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
