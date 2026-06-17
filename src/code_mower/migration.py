#!/usr/bin/env python3
"""Rehearse migration from product-local Code Mower tools to the package."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower.migration_mirror import (
        PRODUCT_SUPPORT_PATTERNS,
        RUNNER_ALIASES,
        _default_local_command,
        _line_requires_workflow_file,
        _relative_existing_files,
        _workflow_file_references,
        _workflow_local_fallback_references,
        render_mirror_removal_plan,
        render_mirror_removal_text,
        render_runner_aliases,
        render_runner_aliases_text,
    )
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
        from .migration_mirror import (
            PRODUCT_SUPPORT_PATTERNS,
            RUNNER_ALIASES,
            _default_local_command,
            _line_requires_workflow_file,
            _relative_existing_files,
            _workflow_file_references,
            _workflow_local_fallback_references,
            render_mirror_removal_plan,
            render_mirror_removal_text,
            render_runner_aliases,
            render_runner_aliases_text,
        )
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
        from code_mower.migration_mirror import (
            PRODUCT_SUPPORT_PATTERNS,
            RUNNER_ALIASES,
            _default_local_command,
            _line_requires_workflow_file,
            _relative_existing_files,
            _workflow_file_references,
            _workflow_local_fallback_references,
            render_mirror_removal_plan,
            render_mirror_removal_text,
            render_runner_aliases,
            render_runner_aliases_text,
        )
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
    "PRODUCT_SUPPORT_PATTERNS",
    "RUNNER_ALIASES",
    "CommandResult",
    "RehearsalError",
    "RunOutput",
    "_default_local_command",
    "_default_product_rehearsal_local_command",
    "_first_user_artifacts",
    "_first_user_readiness_scorecard",
    "_glob_relative_files",
    "_json_payload",
    "_line_requires_workflow_file",
    "_load_release_readiness",
    "_pip_install_command",
    "_relative_existing_files",
    "_resolve_install_package_spec",
    "_run",
    "_run_rehearsal_step",
    "_run_rehearsal_step_to_file",
    "_workflow_file_references",
    "_workflow_local_fallback_references",
    "render_mirror_removal_plan",
    "render_mirror_removal_text",
    "render_package_install_rehearsal_text",
    "render_runner_aliases",
    "render_runner_aliases_text",
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
CALIBRATION_EVIDENCE_ADDITIVE_KEYS = frozenset(
    {
        "audit_input_insufficient_count",
        "audit_input_insufficient_runs",
        "result_category",
    }
)


def _resolve_command(command_text: str) -> tuple[str, ...]:
    parts = tuple(part for part in command_text.split(" ") if part)
    if not parts:
        raise ValueError("command must not be empty")
    return parts


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
