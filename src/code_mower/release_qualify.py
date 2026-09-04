#!/usr/bin/env python3
"""Release qualification runner with stable adoption-result schema."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower import board as code_mower_board
    from code_mower import doctor_checks
    from code_mower import lane_status
    from code_mower.migration_rehearsal import (
        _package_spec_uses_package_index,
        run_package_install_rehearsal,
    )
    from code_mower.package import DEFAULT_PROVIDER_TEMPLATES
else:
    from . import board as code_mower_board
    from . import doctor_checks
    from . import lane_status
    from .migration_rehearsal import (
        _package_spec_uses_package_index,
        run_package_install_rehearsal,
    )
    from .package import DEFAULT_PROVIDER_TEMPLATES

SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[ab]\d+|rc\d+)?$")
VALID_CONTEXTS = {"cold_install", "upgrade", "unknown"}


@dataclass
class StepResult:
    """Step execution result."""

    id: str
    status: str
    elapsed_seconds: float
    warning_count: int
    owner_action_count: int


@dataclass
class AdoptionResult:
    """Release qualification adoption result schema v1."""

    schema: str
    timestamp_utc: str
    release_tag: str
    package_identity: str
    normalized_version: str
    qualification_context: str
    starting_version: str
    ending_version: str
    provider: str
    executor: str
    host_class: str
    runtime_class: str
    elapsed_seconds: float
    outcome: str
    steps: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        return asdict(self)


def _normalize_version(version_str: str) -> str:
    """Extract normalized version from version string."""
    match = re.search(r"(\d+\.\d+\.\d+(?:[ab]\d+|rc\d+)?)", version_str)
    return match.group(1) if match else ""


def _detect_host_class() -> str:
    """Detect coarse host class."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github_actions"
    if os.environ.get("CI") == "true":
        return "ci"
    return "local"


def _detect_runtime_class() -> str:
    """Detect coarse runtime class."""
    return f"python_{sys.version_info.major}.{sys.version_info.minor}"


def _validate_safe_identifier(value: str, name: str) -> None:
    """Validate identifier is safe for metadata."""
    if not SAFE_IDENTIFIER_PATTERN.match(value):
        raise ValueError(f"{name} must be safe identifier")


def _validate_starting_version(value: str) -> None:
    """Validate starting version is empty or normalized version."""
    if value and not VERSION_PATTERN.match(value):
        raise ValueError("starting_version must be empty or normalized version")


def _validate_qualification_context(value: str) -> None:
    """Validate qualification context is in closed set."""
    if value not in VALID_CONTEXTS:
        raise ValueError(f"qualification_context must be one of: {', '.join(sorted(VALID_CONTEXTS))}")


def _extract_package_identity(package_spec: str) -> str:
    """Extract sanitized package identity from spec."""
    if _package_spec_uses_package_index(package_spec):
        match = re.match(r"^([\w-]+)==", package_spec)
        if match:
            name = match.group(1)
            if name != "code-mower":
                raise ValueError("Only code-mower package is supported")
            return name
    raise ValueError("Only code-mower package is supported")


def _validate_tag_format(release_tag: str) -> tuple[bool, str, str]:
    """Validate release tag and extract normalized version."""
    match = re.fullmatch(
        r"v(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?",
        release_tag,
    )
    if not match:
        return False, "", "release tag must match v<major>.<minor>.<patch>[-<stage>.<num>]"
    base = match.group(1)
    stage = match.group(2)
    num = match.group(3)
    if stage and num:
        stage_map = {"alpha": "a", "beta": "b", "rc": "rc"}
        normalized = f"{base}{stage_map[stage]}{num}"
    else:
        normalized = base
    return True, normalized, ""


def _aggregate_outcome(steps: list[StepResult]) -> str:
    """Aggregate step statuses to overall outcome."""
    has_fail = any(s.status == "fail" for s in steps)
    has_warn = any(s.status == "warn" for s in steps)
    has_unavailable = any(s.status == "unavailable" for s in steps)
    has_planned = any(s.status == "planned" for s in steps)

    if has_fail or has_planned:
        return "fail"
    if has_warn or has_unavailable:
        return "pass_with_warnings"
    return "pass"


def _run_doctor_check(config_path: Path, repo_slug: str, config_source: str) -> StepResult:
    """Run doctor check and return step result."""
    start = time.time()
    try:
        from code_mower.package import resolve_provider_templates_path
        provider_templates_path = resolve_provider_templates_path(DEFAULT_PROVIDER_TEMPLATES)
        report = doctor_checks.run_doctor(
            config_path=config_path,
            provider_templates_path=provider_templates_path,
            profile="recommended",
            repo_slug=repo_slug,
            config_source=config_source,
            adoption=True,
            adoption_posture="reviewer-gate",
            probe_runtime=True,
            github=True,
            cloud=True,
        )
        status = report.status
        if status not in {"pass", "fail", "warn", "unavailable"}:
            status = "fail"
        warnings = report.warnings
        actions = report.owner_actions
    except (OSError, ValueError):
        status = "unavailable"
        warnings = 0
        actions = 0

    return StepResult(
        id="doctor",
        status=status,
        elapsed_seconds=round(time.time() - start, 2),
        warning_count=warnings,
        owner_action_count=actions,
    )


def _run_lanes_check(repo_slug: str) -> StepResult:
    """Run lanes status check."""
    start = time.time()
    try:
        result = lane_status.collect_status(repo=repo_slug)
        remote = result.get("remote", {}) if isinstance(result.get("remote"), dict) else {}
        if not remote.get("available"):
            status = "warn"
            warnings = 1
        else:
            status = "pass"
            warnings = 0
    except (OSError, ValueError, lane_status.LaneStatusUnavailable):
        status = "unavailable"
        warnings = 0

    return StepResult(
        id="lanes_status",
        status=status,
        elapsed_seconds=round(time.time() - start, 2),
        warning_count=warnings,
        owner_action_count=0,
    )


def _run_board_check(repo_slug: str, repo_path: Path) -> StepResult:
    """Run board diagnostics."""
    start = time.time()
    try:
        config = code_mower_board.BoardConfig(
            repo=repo_slug,
            repo_path=str(repo_path),
        )
        payload = code_mower_board.doctor_payload(config)
        status = payload.get("status", "fail")
        checks = payload.get("checks", []) if isinstance(payload.get("checks"), list) else []
        warnings = sum(1 for c in checks if isinstance(c, dict) and c.get("status") == "warn")
        actions = sum(1 for c in checks if isinstance(c, dict) and c.get("owner_action"))
    except (OSError, ValueError):
        status = "unavailable"
        warnings = 0
        actions = 0

    return StepResult(
        id="board",
        status=status,
        elapsed_seconds=round(time.time() - start, 2),
        warning_count=warnings,
        owner_action_count=actions,
    )


def _infer_repo_slug(repo_path: Path | None) -> str:
    """Infer safe repo slug from git remote."""
    if not repo_path:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(\.git)?$", url)
            if match:
                return match.group(1)
    except Exception:
        pass
    return ""


def _resolve_config_path(repo_path: Path | None) -> Path:
    """Resolve repository config path."""
    if repo_path:
        return repo_path / "code-mower.yml"
    return Path("code-mower.yml")


def _run_upgrade_rehearsal(
    *,
    starting_spec: str,
    target_spec: str,
    timeout: int,
) -> dict[str, Any]:
    """Perform two-stage upgrade rehearsal in a single environment.

    Returns dict with 'version' key containing the final installed version,
    or raises on failure.
    """
    import tempfile
    import shutil

    work_dir = Path(tempfile.mkdtemp(prefix="code-mower-upgrade-"))
    try:
        venv_path = work_dir / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_path)],
            check=True,
            timeout=timeout,
            capture_output=True,
        )

        pip_exe = venv_path / "bin" / "pip"
        if not pip_exe.exists():
            pip_exe = venv_path / "Scripts" / "pip.exe"

        subprocess.run(
            [str(pip_exe), "install", starting_spec],
            check=True,
            timeout=timeout,
            capture_output=True,
        )

        verify_result = subprocess.run(
            [str(venv_path / "bin" / "code-mower"), "--version"],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
        starting_version = verify_result.stdout.strip()

        subprocess.run(
            [str(pip_exe), "install", "--upgrade", target_spec],
            check=True,
            timeout=timeout,
            capture_output=True,
        )

        final_result = subprocess.run(
            [str(venv_path / "bin" / "code-mower"), "--version"],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
        final_version = final_result.stdout.strip()

        return {"version": final_version, "starting_version": starting_version}
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def run_release_qualification(
    *,
    release_tag: str,
    package_spec: str,
    output_path: Path,
    repo_path: Path | None = None,
    repo_slug: str = "",
    dry_run: bool = True,
    qualification_context: str = "",
    starting_version: str = "",
    provider: str = "local_cli",
    executor: str = "unknown",
    timeout: int = 180,
) -> dict[str, Any]:
    """Run release qualification and emit adoption result."""
    start_time = time.time()
    steps: list[StepResult] = []

    _validate_safe_identifier(provider, "provider")
    _validate_safe_identifier(executor, "executor")
    _validate_starting_version(starting_version)

    valid, normalized_version, error = _validate_tag_format(release_tag)
    if not valid:
        raise ValueError(error)

    if not _package_spec_uses_package_index(package_spec):
        raise ValueError("Only exact package-index specs supported")

    spec_match = re.match(r"^[\w-]+==(.+)$", package_spec)
    if not spec_match or not VERSION_PATTERN.match(spec_match.group(1)):
        raise ValueError("Package spec must be exact index spec")
    if spec_match.group(1) != normalized_version:
        raise ValueError(f"Version mismatch: tag {normalized_version} vs spec version")

    package_identity = _extract_package_identity(package_spec)

    if not qualification_context:
        qualification_context = "unknown"
    _validate_qualification_context(qualification_context)

    if qualification_context == "upgrade" and not starting_version:
        raise ValueError("starting_version required for upgrade context")

    ending_version = ""
    config_path = _resolve_config_path(repo_path)
    config_source = f"file:{config_path}" if config_path.is_file() else "default"

    if repo_path is None:
        repo_path = Path.cwd()

    if not repo_slug and repo_path:
        repo_slug = _infer_repo_slug(repo_path)

    doctor_step = _run_doctor_check(config_path, repo_slug, config_source)
    steps.append(doctor_step)

    if repo_slug:
        lanes_step = _run_lanes_check(repo_slug)
        steps.append(lanes_step)

        if repo_path:
            board_step = _run_board_check(repo_slug, repo_path)
            steps.append(board_step)

    if dry_run:
        steps.append(
            StepResult(
                id="package_install",
                status="planned",
                elapsed_seconds=0.0,
                warning_count=0,
                owner_action_count=0,
            )
        )
    else:
        rehearsal_start = time.time()
        try:
            if qualification_context == "upgrade":
                starting_spec = f"{package_identity}=={starting_version}"

                upgrade_result = _run_upgrade_rehearsal(
                    starting_spec=starting_spec,
                    target_spec=package_spec,
                    timeout=timeout,
                )
                rehearsal_version_raw = upgrade_result.get("version", "")
                rehearsal_version = _normalize_version(rehearsal_version_raw)
                ending_version = rehearsal_version

                starting_installed_raw = upgrade_result.get("starting_version", "")
                starting_installed = _normalize_version(starting_installed_raw)
                if starting_installed != starting_version:
                    raise ValueError(f"Failed to install starting version {starting_version}")

                if rehearsal_version != normalized_version:
                    rehearsal_status = "fail"
                else:
                    rehearsal_status = "pass"
            else:
                rehearsal_result = run_package_install_rehearsal(
                    package_spec=package_spec,
                    repo_path=repo_path,
                    timeout=timeout,
                    allow_package_index=True,
                )
                rehearsal_version_raw = rehearsal_result.get("version", "")
                rehearsal_version = _normalize_version(rehearsal_version_raw)
                ending_version = rehearsal_version
                if rehearsal_version != normalized_version:
                    rehearsal_status = "fail"
                else:
                    rehearsal_status = "pass"
        except Exception:
            rehearsal_status = "fail"

        steps.append(
            StepResult(
                id="package_install",
                status=rehearsal_status,
                elapsed_seconds=round(time.time() - rehearsal_start, 2),
                warning_count=0,
                owner_action_count=0,
            )
        )

    outcome = _aggregate_outcome(steps)
    elapsed = time.time() - start_time

    result = AdoptionResult(
        schema="code_mower.adoptionResult.v1",
        timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        release_tag=release_tag,
        package_identity=package_identity,
        normalized_version=normalized_version,
        qualification_context=qualification_context,
        starting_version=starting_version,
        ending_version=ending_version,
        provider=provider,
        executor=executor,
        host_class=_detect_host_class(),
        runtime_class=_detect_runtime_class(),
        elapsed_seconds=round(elapsed, 2),
        outcome=outcome,
        steps=[
            {
                "id": s.id,
                "status": s.status,
                "elapsed_seconds": s.elapsed_seconds,
                "warning_count": s.warning_count,
                "owner_action_count": s.owner_action_count,
            }
            for s in steps
        ],
    )

    result_dict = result.to_dict()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(result_dict, f, indent=2, sort_keys=True)

    return result_dict


def main(argv: Sequence[str] | None = None) -> int:
    """Release qualification command entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    qualify = subparsers.add_parser(
        "qualify",
        help="Run release qualification for one provider/environment",
    )
    qualify.add_argument(
        "--release-tag",
        required=True,
        help="Exact release tag (e.g., v1.0.0)",
    )
    qualify.add_argument(
        "--package-spec",
        required=True,
        help="Exact package-index spec (e.g., code-mower==1.0.0)",
    )
    qualify.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for adoption result JSON",
    )
    qualify.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help="Repository path (defaults to current directory)",
    )
    qualify.add_argument(
        "--repo-slug",
        default="",
        help="Optional repository slug (OWNER/REPO)",
    )
    qualify.add_argument(
        "--execute",
        action="store_true",
        help="Execute qualification (default is dry-run)",
    )
    qualify.add_argument(
        "--qualification-context",
        default="",
        help="Qualification context (safe identifier)",
    )
    qualify.add_argument(
        "--starting-version",
        default="",
        help="Starting version (empty or normalized)",
    )
    qualify.add_argument(
        "--provider",
        default="local_cli",
        help="Provider identity (safe identifier)",
    )
    qualify.add_argument(
        "--executor",
        default="unknown",
        help="Executor identity (safe identifier)",
    )
    qualify.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Timeout in seconds",
    )
    qualify.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "qualify":
        try:
            result = run_release_qualification(
                release_tag=args.release_tag,
                package_spec=args.package_spec,
                output_path=args.output,
                repo_path=args.repo_path,
                repo_slug=args.repo_slug,
                dry_run=not args.execute,
                qualification_context=args.qualification_context,
                starting_version=args.starting_version,
                provider=args.provider,
                executor=args.executor,
                timeout=args.timeout,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"Qualification: {result['outcome']}")
                print(f"Schema: {result['schema']}")
                print(f"Package: {result['package_identity']} {result['normalized_version']}")
                print(f"Context: {result['qualification_context']}")
                print(f"Steps: {len(result['steps'])}")
            return 0 if result["outcome"] in ("pass", "pass_with_warnings") else 1
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"error: qualification failed: {e}", file=sys.stderr)
            return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
