#!/usr/bin/env python3
"""Mirror-removal planning helpers for Code Mower migration gates."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Sequence

from .migration_rehearsal import (
    MIRRORED_IMPLEMENTATION_PATTERNS,
    _glob_relative_files,
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


def _default_local_command(repo_path: Path) -> tuple[str, ...] | None:
    command_candidate = repo_path / "tools" / "code_mower"
    if command_candidate.is_file():
        return (str(command_candidate),)
    candidate = repo_path / "tools" / "code_mower_cli.py"
    if candidate.is_file():
        return (sys.executable, str(candidate))
    return None


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
