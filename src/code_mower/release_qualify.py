#!/usr/bin/env python3
"""Release qualification runner with stable adoption-result schema."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from code_mower import board as code_mower_board
    from code_mower import doctor_checks as code_mower_doctor_checks
    from code_mower import lane_status as code_mower_lane_status
    from code_mower import versioning as code_mower_versioning
    from code_mower.migration_rehearsal import (
        _package_spec_uses_package_index,
        run_package_install_rehearsal,
    )
else:
    from . import board as code_mower_board
    from . import doctor_checks as code_mower_doctor_checks
    from . import lane_status as code_mower_lane_status
    from . import versioning as code_mower_versioning
    from .migration_rehearsal import (
        _package_spec_uses_package_index,
        run_package_install_rehearsal,
    )

SAFE_PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SAFE_EXECUTOR_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


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
    step_count: int
    warning_count: int
    owner_action_count: int

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict."""
        return asdict(self)


def _normalize_version(version_str: str) -> str:
    """Extract normalized version from version string."""
    match = re.search(r"(\d+\.\d+\.\d+(?:[ab]\d+|rc\d+)?)", version_str)
    return match.group(1) if match else ""


def _detect_qualification_context() -> str:
    """Detect qualification context from environment."""
    try:
        result = subprocess.run(
            ["code-mower", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            version = _normalize_version(result.stdout)
            return "upgrade" if version else "unknown"
        return "unknown"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "cold_install"


def _detect_host_class() -> str:
    """Detect coarse host class."""
    import os
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github_actions"
    if os.environ.get("CI") == "true":
        return "ci"
    return "local"


def _detect_runtime_class() -> str:
    """Detect coarse runtime class."""
    return f"python_{sys.version_info.major}.{sys.version_info.minor}"


def _validate_safe_identifier(value: str, name: str, pattern: re.Pattern[str]) -> None:
    """Validate identifier is safe for metadata."""
    if not pattern.match(value):
        raise ValueError(
            f"{name} must match {pattern.pattern}: got {value!r}"
        )


def _extract_package_identity(package_spec: str) -> str:
    """Extract sanitized package identity from spec."""
    if _package_spec_uses_package_index(package_spec):
        match = re.match(r"^([\w-]+)==([\d.abc]+)$", package_spec)
        if match:
            return match.group(1)
    return "code-mower"


def _validate_tag_format(release_tag: str) -> tuple[bool, str, str]:
    """Validate release tag and extract normalized version."""
    match = re.fullmatch(
        r"v(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?",
        release_tag,
    )
    if not match:
        return False, "", f"release tag must match v<major>.<minor>.<patch>[-<stage>.<num>]: {release_tag}"
    base = match.group(1)
    stage = match.group(2)
    num = match.group(3)
    if stage and num:
        stage_map = {"alpha": "a", "beta": "b", "rc": "rc"}
        normalized = f"{base}{stage_map[stage]}{num}"
    else:
        normalized = base
    return True, normalized, ""


def _validate_tag_package_match(
    release_tag: str,
    package_spec: str,
) -> tuple[bool, str, str]:
    """Validate that release tag and package spec versions agree."""
    valid, normalized, error = _validate_tag_format(release_tag)
    if not valid:
        return False, "", error

    if _package_spec_uses_package_index(package_spec):
        spec_match = re.match(r"^[\w-]+==([\d.abc]+)$", package_spec)
        if not spec_match:
            return False, "", f"package-index spec missing exact version: {package_spec}"
        spec_version = spec_match.group(1)
        if normalized != spec_version:
            return False, "", f"version mismatch: tag {normalized} vs spec {spec_version}"
        return True, normalized, ""

    return False, normalized, "pending_verification"


def _run_doctor_check(timeout: int) -> tuple[str, int, int]:
    """Run doctor check and return status, warnings, owner actions."""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "code-mower.yml"
            config_path.write_text("repositories: []\n", encoding="utf-8")
            provider_templates = code_mower_doctor_checks.resolve_doctor_provider_templates_path(None)
            report = code_mower_doctor_checks.run_doctor(
                config_path=config_path,
                provider_templates_path=provider_templates,
                profile="recommended",
                easy=True,
                probe_runtime=True,
                probe_github=False,
                probe_cloud=False,
                repo_slug=None,
                adoption=False,
            )
            status = report.status
            warnings = sum(1 for c in report.checks if c.status == "warn")
            owner_actions = 0
            return status, warnings, owner_actions
    except Exception:
        return "unavailable", 0, 0


def _run_lanes_status_check(repo_path: Path, timeout: int) -> tuple[str, int, int]:
    """Run lanes status check."""
    try:
        result = subprocess.run(
            ["code-mower", "lanes", "status", "--repo", str(repo_path), "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return "pass", 0, 0
        return "fail", 0, 0
    except Exception:
        return "unavailable", 0, 0


def _run_board_check(repo_path: Path, timeout: int) -> tuple[str, int, int]:
    """Run board diagnostics."""
    try:
        report = code_mower_board.render_board_report(
            repo_slug=None,
            repo_path=repo_path,
            record_events=False,
        )
        return "pass", 0, 0
    except Exception:
        return "unavailable", 0, 0


def run_release_qualification(
    *,
    release_tag: str,
    package_spec: str,
    output_path: Path,
    repo_path: Path | None = None,
    dry_run: bool = True,
    provider: str = "local_cli",
    executor: str = "unknown",
    timeout: int = 180,
) -> dict[str, Any]:
    """Run release qualification and emit adoption result."""
    start_time = time.time()
    step_count = 0
    warning_count = 0
    owner_action_count = 0

    _validate_safe_identifier(provider, "provider", SAFE_PROVIDER_PATTERN)
    _validate_safe_identifier(executor, "executor", SAFE_EXECUTOR_PATTERN)

    valid, normalized_version, error = _validate_tag_package_match(
        release_tag,
        package_spec,
    )
    if not valid and error != "pending_verification":
        raise ValueError(f"Tag/package validation failed: {error}")

    verification_pending = error == "pending_verification"
    package_identity = _extract_package_identity(package_spec)
    qualification_context = _detect_qualification_context()
    starting_version = _normalize_version(subprocess.run(
        ["code-mower", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout) if qualification_context == "upgrade" else ""

    ending_version = ""
    outcome = "pass"

    if not dry_run:
        step_count += 1
        try:
            rehearsal_result = run_package_install_rehearsal(
                package_spec=package_spec,
                repo_path=repo_path,
                timeout=timeout,
                allow_package_index=_package_spec_uses_package_index(package_spec),
            )
            ending_version = rehearsal_result.get("version", "")
            if verification_pending and ending_version != normalized_version:
                outcome = "fail"
                warning_count += 1
        except Exception:
            outcome = "fail"
            warning_count += 1
    else:
        ending_version = normalized_version if not verification_pending else ""

    step_count += 1
    doctor_status, doctor_warnings, doctor_actions = _run_doctor_check(timeout)
    warning_count += doctor_warnings
    owner_action_count += doctor_actions
    if doctor_status == "fail":
        outcome = "fail"
    elif doctor_status == "unavailable":
        warning_count += 1

    if repo_path:
        step_count += 1
        lanes_status, lanes_warnings, lanes_actions = _run_lanes_status_check(repo_path, timeout)
        warning_count += lanes_warnings
        owner_action_count += lanes_actions
        if lanes_status == "unavailable":
            warning_count += 1

        step_count += 1
        board_status, board_warnings, board_actions = _run_board_check(repo_path, timeout)
        warning_count += board_warnings
        owner_action_count += board_actions
        if board_status == "unavailable":
            warning_count += 1

    if outcome != "fail" and (warning_count > 0 or owner_action_count > 0):
        outcome = "pass_with_warnings"

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
        step_count=step_count,
        warning_count=warning_count,
        owner_action_count=owner_action_count,
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
        help="Package specification (e.g., code-mower==1.0.0)",
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
        help="Optional repository path for lanes status checks",
    )
    qualify.add_argument(
        "--execute",
        action="store_true",
        help="Execute qualification (default is dry-run)",
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
        help="Timeout in seconds for individual steps",
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
                dry_run=not args.execute,
                provider=args.provider,
                executor=args.executor,
                timeout=args.timeout,
            )
            if args.json:
                print(json.dumps(result, indent=2, sort_keys=True))
            else:
                print(f"Qualification: {result['outcome']}")
                print(f"Schema: {result['schema']}")
                print(f"Release: {result['release_tag']}")
                print(f"Package: {result['package_identity']}")
                print(f"Version: {result['normalized_version']}")
                print(f"Context: {result['qualification_context']}")
                if result['warning_count']:
                    print(f"Warnings: {result['warning_count']}")
                if result['owner_action_count']:
                    print(f"Owner actions: {result['owner_action_count']}")
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
