#!/usr/bin/env python3
"""Rehearse migration from product-local Code Mower tools to the package."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower.migration_rehearsal import (
        FIRST_USER_ARTIFACTS,
        MIRRORED_IMPLEMENTATION_PATTERNS,
        PRIVACY_EXCLUDED_CONTENT,
        CommandResult,
        RehearsalError,
        RunOutput,
        _default_product_rehearsal_local_command,
        _first_user_artifacts,
        _first_user_readiness_scorecard,
        _glob_relative_files,
        _json_payload,
        _load_release_readiness,
        _pip_install_command,
        _resolve_install_package_spec,
        _run,
        _run_rehearsal_step,
        _run_rehearsal_step_to_file,
        render_package_install_rehearsal_text,
        run_package_install_rehearsal,
    )
else:
    try:
        from .migration_rehearsal import (
            FIRST_USER_ARTIFACTS,
            MIRRORED_IMPLEMENTATION_PATTERNS,
            PRIVACY_EXCLUDED_CONTENT,
            CommandResult,
            RehearsalError,
            RunOutput,
            _default_product_rehearsal_local_command,
            _first_user_artifacts,
            _first_user_readiness_scorecard,
            _glob_relative_files,
            _json_payload,
            _load_release_readiness,
            _pip_install_command,
            _resolve_install_package_spec,
            _run,
            _run_rehearsal_step,
            _run_rehearsal_step_to_file,
            render_package_install_rehearsal_text,
            run_package_install_rehearsal,
        )
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from code_mower.migration_rehearsal import (
            FIRST_USER_ARTIFACTS,
            MIRRORED_IMPLEMENTATION_PATTERNS,
            PRIVACY_EXCLUDED_CONTENT,
            CommandResult,
            RehearsalError,
            RunOutput,
            _default_product_rehearsal_local_command,
            _first_user_artifacts,
            _first_user_readiness_scorecard,
            _glob_relative_files,
            _json_payload,
            _load_release_readiness,
            _pip_install_command,
            _resolve_install_package_spec,
            _run,
            _run_rehearsal_step,
            _run_rehearsal_step_to_file,
            render_package_install_rehearsal_text,
            run_package_install_rehearsal,
        )

__all__ = [
    "FIRST_USER_ARTIFACTS",
    "MIRRORED_IMPLEMENTATION_PATTERNS",
    "PRIVACY_EXCLUDED_CONTENT",
    "CommandResult",
    "RehearsalError",
    "RunOutput",
    "_default_product_rehearsal_local_command",
    "_first_user_artifacts",
    "_first_user_readiness_scorecard",
    "_glob_relative_files",
    "_json_payload",
    "_load_release_readiness",
    "_pip_install_command",
    "_resolve_install_package_spec",
    "_run",
    "_run_rehearsal_step",
    "_run_rehearsal_step_to_file",
    "render_package_install_rehearsal_text",
    "run_package_install_rehearsal",
]


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
PRODUCT_SUPPORT_PATTERNS = (
    "tools/run_code_mower_tests.sh",
    "tools/run_code_mower_standalone_rehearsal.sh",
    "tools/run_codex_audit_pr.sh",
    "tools/run_claude_audit_pr.sh",
    "tools/devin_audit_bridge.py",
    "tools/audit_handoff_log.py",
    "tools/request_review.py",
    "tools/safe_gh_comment.py",
    "tools/codex_audit_env.sh",
)
CALIBRATION_EVIDENCE_ADDITIVE_KEYS = frozenset(
    {
        "audit_input_insufficient_count",
        "audit_input_insufficient_runs",
        "result_category",
    }
)
RUNNER_ALIASES = (
    {
        "legacy": "tools/gemini_cli_audit_pr.py",
        "standalone": "code-mower gemini-cli",
        "status": "supported",
        "notes": "Gemini CLI compatibility runner.",
    },
    {
        "legacy": "tools/antigravity_cli_audit_pr.py",
        "standalone": "code-mower antigravity-cli",
        "status": "supported",
        "notes": "Preferred Google CLI lane after Antigravity migration.",
    },
    {
        "legacy": "tools/hermes_cli_audit_pr.py",
        "standalone": "code-mower hermes-cli",
        "status": "supported",
        "notes": "Hermes Agent calibration runner; requires explicit ambient-home opt-in.",
    },
    {
        "legacy": "tools/coderabbit_cli_audit_pr.py",
        "standalone": "code-mower coderabbit-cli",
        "status": "supported",
        "notes": "Manual informational CodeRabbit CLI evidence capture.",
    },
    {
        "legacy": "tools/local_llm_audit_pr.py",
        "standalone": "code-mower local-llm audit",
        "status": "supported",
        "notes": "OpenAI-compatible local model audit runner.",
    },
    {
        "legacy": "tools/trailer_comment_labeler.py",
        "standalone": "code-mower trailer-comment-labeler",
        "status": "supported",
        "notes": "Use for structured audit trailer/comment label state.",
    },
    {
        "legacy": "tools/saas_reviewer_labeler.py",
        "standalone": "code-mower saas-reviewer-labeler",
        "status": "supported",
        "notes": "Use for SaaS reviewer event label state.",
    },
    {
        "legacy": "tools/run_codex_audit_pr.sh",
        "standalone": "",
        "status": "product-wrapper",
        "notes": (
            "No generic standalone Codex authoring runner exists yet. Keep the "
            "product wrapper for model invocation/repost artifacts, and use "
            "`code-mower trailer-comment-labeler --lane codex` for merge-bar "
            "label state."
        ),
    },
    {
        "legacy": "tools/run_claude_audit_pr.sh",
        "standalone": "",
        "status": "product-wrapper",
        "notes": (
            "No generic standalone Claude authoring runner exists yet. Keep the "
            "product wrapper for model invocation/repost artifacts, and use "
            "`code-mower trailer-comment-labeler --lane claude` for merge-bar "
            "label state."
        ),
    },
)


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
        raise ValueError("could not infer local product Code Mower command; pass --local-command")

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
    return [candidate for candidate in candidates if (repo_path / candidate).exists()]


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


def _workflow_local_fallback_references(repo_path: Path) -> list[dict[str, Any]]:
    workflow_root = repo_path / ".github" / "workflows"
    if not workflow_root.is_dir():
        return []
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
            if "CODE_MOWER_USE_LOCAL=1" in line and "tools/code_mower" in line:
                references.append(
                    {
                        "workflow": workflow.relative_to(repo_path).as_posix(),
                        "line": line_number,
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
    product_support_files = _relative_existing_files(
        repo_path,
        PRODUCT_SUPPORT_PATTERNS,
    )
    local_command = _default_local_command(repo_path)
    mirrored_candidates = _glob_relative_files(
        repo_path,
        MIRRORED_IMPLEMENTATION_PATTERNS,
    )
    workflow_mirror_references = _workflow_file_references(
        repo_path,
        mirrored_candidates,
    )
    workflow_local_fallback_references = _workflow_local_fallback_references(
        repo_path,
    )
    mirrors_absent = (
        not mirrored_candidates
        and not workflow_mirror_references
        and not workflow_local_fallback_references
    )
    ready_for_shadow = {
        "standalone_pin_file_present": "tools/code_mower_standalone_pin.env" in support_files,
        "standalone_shadow_wrapper_present": "tools/code_mower_standalone_shadow.sh"
        in support_files,
        "product_local_command_present": local_command is not None,
        "mirrored_files_detected": bool(mirrored_candidates),
    }
    support_ready = (
        ready_for_shadow["standalone_pin_file_present"]
        and ready_for_shadow["standalone_shadow_wrapper_present"]
        and ready_for_shadow["product_local_command_present"]
    )
    shadow_ready = support_ready and shadow_cycles >= required_shadow_cycles
    cycle_ready_for_removal = (
        shadow_ready and standalone_default_cycles >= required_standalone_default_cycles
    )
    removal_ready = (
        cycle_ready_for_removal
        and not workflow_mirror_references
        and not workflow_local_fallback_references
    )
    blockers = []
    if not mirrors_absent and not ready_for_shadow["standalone_pin_file_present"]:
        blockers.append("add tools/code_mower_standalone_pin.env")
    if not mirrors_absent and not ready_for_shadow["standalone_shadow_wrapper_present"]:
        blockers.append("add tools/code_mower_standalone_shadow.sh")
    if not mirrors_absent and not ready_for_shadow["product_local_command_present"]:
        blockers.append("identify the product-local Code Mower command")
    if not mirrors_absent and shadow_cycles < required_shadow_cycles:
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
    if workflow_local_fallback_references:
        blockers.append(
            "remove CODE_MOWER_USE_LOCAL=1 workflow fallback calls before "
            "deleting mirrors; private repos need a public/package install path "
            "or authenticated standalone checkout for Actions"
        )
    if mirrors_absent and support_ready:
        status = "mirrors_removed"
    elif mirrors_absent:
        status = "no_mirrors_detected"
    elif removal_ready:
        status = "ready_to_remove_mirrors"
    elif cycle_ready_for_removal and workflow_local_fallback_references:
        status = "local_fallback_dependency_blocks_mirror_removal"
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
        "product_support_files": product_support_files,
        "product_support_file_count": len(product_support_files),
        "local_command": list(local_command or ()),
        "mirrored_file_count": len(mirrored_candidates),
        "mirrored_files": mirrored_candidates,
        "workflow_mirrored_file_reference_count": len(workflow_mirror_references),
        "workflow_mirrored_file_references": workflow_mirror_references,
        "workflow_local_fallback_reference_count": len(workflow_local_fallback_references),
        "workflow_local_fallback_references": workflow_local_fallback_references,
        "readiness": ready_for_shadow,
        "mirrors_absent": mirrors_absent,
        "blockers": blockers,
        "steps": [
            "Run code-mower migration wrapper-rehearsal against the pinned standalone release and require mismatch_count: 0.",
            "Run at least the required number of clean product release cycles with the pinned standalone wrapper available.",
            "Move workflow calls from mirrored Python files to tools/code_mower standalone wrapper subcommands.",
            "Flip product workflows or wrapper defaults to the pinned standalone command while keeping the product-local mirrors in place.",
            "For private standalone repos, configure authenticated Actions checkout or wait for a public/package install path before removing local mirrors.",
            "Run the normal product merge gates and post-merge deploy checks.",
            "Remove mirrored implementation files in a dedicated PR after the standalone default cycle stays clean.",
        ],
        "notes": [
            "This plan is intentionally conservative: mirrored files are inventory, not deletion approval.",
            "Keep support wrappers such as tools/code_mower and the standalone pin/shadow files during mirror removal.",
            "Product-specific support files may remain after mirrored implementation files are removed.",
            "CODE_MOWER_USE_LOCAL=1 workflow calls are allowed for private-repo safety, but they intentionally depend on repo-local mirror files.",
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
        "Workflow local-fallback references: "
        f"{payload.get('workflow_local_fallback_reference_count', 0)}",
        "",
        "Next steps:",
    ]
    for step in payload["steps"]:
        lines.append(f"- {step}")
    if payload.get("mirrors_absent"):
        lines.append("")
        lines.append(
            "Mirror inventory is empty: no removable mirrored implementation files "
            "or workflow references were detected."
        )
    if payload["blockers"]:
        lines.append("")
        lines.append("Blockers:")
        for blocker in payload["blockers"]:
            lines.append(f"- {blocker}")
    return "\n".join(lines) + "\n"


def render_runner_aliases(*, legacy: str | None = None) -> dict[str, Any]:
    aliases = [dict(row) for row in RUNNER_ALIASES]
    if legacy:
        needle = legacy.strip()
        aliases = [
            row for row in aliases if row["legacy"] == needle or Path(row["legacy"]).name == needle
        ]
    return {
        "mode": "code-mower-runner-aliases",
        "status": "pass",
        "aliases": aliases,
    }


def render_runner_aliases_text(payload: dict[str, Any]) -> str:
    lines = ["Code Mower runner aliases", ""]
    aliases = payload.get("aliases", [])
    if not aliases:
        lines.append("No aliases matched.")
        return "\n".join(lines) + "\n"
    for row in aliases:
        standalone = row.get("standalone") or "(no generic standalone alias)"
        lines.append(f"- {row['legacy']} -> {standalone}")
        lines.append(f"  status: {row['status']}")
        lines.append(f"  notes: {row['notes']}")
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
    aliases = subparsers.add_parser("runner-aliases")
    aliases.add_argument(
        "--legacy",
        default=None,
        help="optional legacy script path or basename to filter, e.g. run_codex_audit_pr.sh",
    )
    aliases.add_argument("--json", action="store_true")
    release = subparsers.add_parser("release-readiness")
    release.add_argument("--repo-path", type=Path, default=Path.cwd())
    release.add_argument("--json", action="store_true")
    package_install = subparsers.add_parser("package-install-rehearsal")
    package_install.add_argument(
        "--package-spec",
        default="code-mower",
        help=(
            "package spec to pip install into a clean venv; use a local path, "
            "git URL, or package index name"
        ),
    )
    package_install.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help="optional product repo to compare against the installed package",
    )
    package_install.add_argument(
        "--local-command",
        default="",
        help=(
            "product-local command prefix for --repo-path, e.g. "
            "'env CODE_MOWER_USE_LOCAL=1 tools/code_mower'"
        ),
    )
    package_install.add_argument(
        "--python",
        type=Path,
        default=None,
        help="Python 3.11+ executable used to create the clean rehearsal venv",
    )
    package_install.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="empty or absent directory for venv, toy repo, and JSON outputs",
    )
    package_install.add_argument(
        "--pip-index-url",
        default="",
        help="optional pip --index-url for package-install rehearsal",
    )
    package_install.add_argument(
        "--pip-extra-index-url",
        action="append",
        default=[],
        help="optional pip --extra-index-url; may be provided multiple times",
    )
    package_install.add_argument("--timeout", type=int, default=180)
    package_install.add_argument("--shadow-cycles", type=int, default=1)
    package_install.add_argument("--standalone-default-cycles", type=int, default=1)
    package_install.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "wrapper-rehearsal":
        try:
            payload = run_wrapper_rehearsal(
                repo_path=args.repo_path,
                local_command=_resolve_command(args.local_command) if args.local_command else None,
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

    if args.command == "runner-aliases":
        payload = render_runner_aliases(legacy=args.legacy)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_runner_aliases_text(payload), end="")
        return 0

    if args.command == "release-readiness":
        release_readiness = _load_release_readiness()
        payload = release_readiness.render_release_readiness(repo_path=args.repo_path)
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(release_readiness.render_release_readiness_text(payload), end="")
        return 0 if payload["status"] == "pass" else 1

    if args.command == "package-install-rehearsal":
        try:
            payload = run_package_install_rehearsal(
                package_spec=args.package_spec,
                repo_path=args.repo_path,
                local_command=_resolve_command(args.local_command) if args.local_command else None,
                python=args.python,
                work_dir=args.work_dir,
                timeout=args.timeout,
                shadow_cycles=args.shadow_cycles,
                standalone_default_cycles=args.standalone_default_cycles,
                pip_index_url=args.pip_index_url,
                pip_extra_index_urls=args.pip_extra_index_url,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError, RehearsalError) as exc:
            payload = {
                "mode": "code-mower-package-install-rehearsal",
                "status": "fail",
                "error": str(exc),
                "steps": getattr(exc, "steps", []),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"package-install rehearsal failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(render_package_install_rehearsal_text(payload), end="")
        return 0

    raise AssertionError(f"unhandled migration command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
