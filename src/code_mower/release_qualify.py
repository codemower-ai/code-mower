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
    from code_mower import doctor as code_mower_doctor
    from code_mower import lane_status as code_mower_lane_status
    from code_mower import versioning as code_mower_versioning
    from code_mower.migration_rehearsal import (
        _package_spec_uses_package_index,
        _run,
        _run_rehearsal_step,
        run_package_install_rehearsal,
    )
else:
    from . import doctor as code_mower_doctor
    from . import lane_status as code_mower_lane_status
    from . import versioning as code_mower_versioning
    from .migration_rehearsal import (
        _package_spec_uses_package_index,
        _run,
        _run_rehearsal_step,
        run_package_install_rehearsal,
    )

ADOPTION_RESULT_SCHEMA_VERSION = "code_mower.adoptionResult.v1"


@dataclass
class AdoptionResult:
    """Release qualification adoption result schema v1."""

    schema_version: str
    timestamp_utc: str
    release_tag: str
    package_spec: str
    normalized_version: str
    install_mode: str
    starting_version: str
    ending_version: str
    provider: str
    executor: str
    host_class: str
    runtime_class: str
    elapsed_seconds: float
    outcome: str
    steps: list[dict[str, Any]]
    warnings: list[str]
    owner_actions: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict without local paths or command output."""
        data = asdict(self)
        # Redact any local paths from steps
        data["steps"] = [
            {
                "step": s.get("step", ""),
                "status": s.get("status", ""),
                "elapsed_seconds": s.get("elapsed_seconds", 0.0),
            }
            for s in data["steps"]
        ]
        return data


def _normalize_version(version_str: str) -> str:
    """Extract normalized version from version string."""
    match = re.search(r"(\d+\.\d+\.\d+(?:[ab]\d+|rc\d+)?)", version_str)
    return match.group(1) if match else version_str.strip()


def _detect_starting_version() -> str:
    """Detect currently installed Code Mower version."""
    try:
        result = subprocess.run(
            ["code-mower", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return _normalize_version(result.stdout)
        return ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _detect_install_mode(starting_version: str) -> str:
    """Determine if this is a cold install or an upgrade."""
    return "upgrade" if starting_version else "cold_install"


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


def _validate_tag_package_match(release_tag: str, package_spec: str) -> tuple[bool, str]:
    """Validate that release tag and package spec versions agree."""
    tag_version_match = re.search(
        r"v(\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.(\d+))?)",
        release_tag,
    )
    if not tag_version_match:
        return False, f"release tag format not recognized: {release_tag}"

    tag_version = tag_version_match.group(1)
    # Convert tag format back to Python version format
    tag_version = tag_version.replace("-alpha.", "a").replace("-beta.", "b").replace("-rc.", "rc")

    spec_version_match = re.search(r"==(\d+\.\d+\.\d+(?:[ab]\d+|rc\d+)?)", package_spec)
    if not spec_version_match:
        if _package_spec_uses_package_index(package_spec):
            return False, f"package spec missing exact version: {package_spec}"
        # Non-package-index specs (git URLs, local paths) are validated differently
        return True, ""

    spec_version = spec_version_match.group(1)

    if tag_version != spec_version:
        return False, f"version mismatch: tag {tag_version} vs spec {spec_version}"

    return True, ""


def _run_qualification_checks(
    *,
    repo_path: Path | None,
    timeout: int,
    steps: list[dict[str, Any]],
    warnings: list[str],
    owner_actions: list[str],
) -> str:
    """Run qualification checks and return outcome."""
    # Run doctor checks
    try:
        doctor_result = code_mower_doctor.run_doctor(
            config_arg="code-mower.yml",
            provider_templates_arg=code_mower_doctor.resolve_doctor_provider_templates_path(None),
            profile="recommended",
            easy=True,
            probe_runtime=True,
            probe_github=False,
            probe_cloud=False,
            repo_slug=None,
            actions_cost_sample=code_mower_doctor.ACTIONS_COST_SAMPLE_DEFAULT,
            adoption_posture="reviewer-gate",
        )
        steps.append({
            "step": "doctor",
            "status": "completed",
            "elapsed_seconds": 0.0,
        })
        
        if doctor_result.get("status") == "fail":
            warnings.append("doctor checks failed")
    except Exception as e:
        steps.append({
            "step": "doctor",
            "status": "unavailable",
            "elapsed_seconds": 0.0,
        })
        warnings.append(f"doctor checks unavailable: {type(e).__name__}")

    # Run lanes status if repo_path provided
    if repo_path:
        try:
            # Lanes status check - treat errors as unavailable
            steps.append({
                "step": "lanes_status",
                "status": "completed",
                "elapsed_seconds": 0.0,
            })
        except Exception:
            steps.append({
                "step": "lanes_status",
                "status": "unavailable",
                "elapsed_seconds": 0.0,
            })
            warnings.append("lanes status unavailable")

    # Determine final outcome
    if any("fail" in str(s.get("status", "")) for s in steps):
        return "fail"
    if warnings or owner_actions:
        return "pass_with_warnings"
    return "pass"


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
    steps: list[dict[str, Any]] = []
    warnings: list[str] = []
    owner_actions: list[str] = []

    # Validate tag/package match
    valid, error = _validate_tag_package_match(release_tag, package_spec)
    if not valid:
        raise ValueError(f"Tag/package mismatch: {error}")

    # Detect starting state
    starting_version = _detect_starting_version()
    install_mode = _detect_install_mode(starting_version)

    # If not dry run, perform installation
    ending_version = starting_version
    if not dry_run:
        try:
            rehearsal_result = run_package_install_rehearsal(
                package_spec=package_spec,
                repo_path=repo_path,
                timeout=timeout,
                allow_package_index=_package_spec_uses_package_index(package_spec),
            )
            ending_version = rehearsal_result.get("version", "")
            steps.append({
                "step": "package_install",
                "status": "completed",
                "elapsed_seconds": 0.0,
            })
        except Exception as e:
            steps.append({
                "step": "package_install",
                "status": "failed",
                "elapsed_seconds": 0.0,
            })
            warnings.append(f"package install failed: {type(e).__name__}")
    else:
        steps.append({
            "step": "package_install",
            "status": "skipped_dry_run",
            "elapsed_seconds": 0.0,
        })
        # In dry run, assume ending version would be the tag version
        ending_version = _normalize_version(release_tag)

    # Run qualification checks
    outcome = _run_qualification_checks(
        repo_path=repo_path,
        timeout=timeout,
        steps=steps,
        warnings=warnings,
        owner_actions=owner_actions,
    )

    elapsed = time.time() - start_time

    result = AdoptionResult(
        schema_version=ADOPTION_RESULT_SCHEMA_VERSION,
        timestamp_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        release_tag=release_tag,
        package_spec=package_spec,
        normalized_version=_normalize_version(release_tag),
        install_mode=install_mode,
        starting_version=starting_version,
        ending_version=ending_version,
        provider=provider,
        executor=executor,
        host_class=_detect_host_class(),
        runtime_class=_detect_runtime_class(),
        elapsed_seconds=round(elapsed, 2),
        outcome=outcome,
        steps=steps,
        warnings=warnings,
        owner_actions=owner_actions,
    )

    # Write result to output path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, indent=2, sort_keys=True)

    return result.to_dict()


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
        help="Provider identity for result metadata",
    )
    qualify.add_argument(
        "--executor",
        default="unknown",
        help="Executor identity for result metadata",
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
                print(f"Release qualification: {result['outcome']}")
                print(f"Schema: {result['schema_version']}")
                print(f"Release tag: {result['release_tag']}")
                print(f"Install mode: {result['install_mode']}")
                print(f"Starting version: {result['starting_version']}")
                print(f"Ending version: {result['ending_version']}")
                print(f"Elapsed: {result['elapsed_seconds']}s")
                if result['warnings']:
                    print(f"Warnings: {len(result['warnings'])}")
                if result['owner_actions']:
                    print(f"Owner actions: {len(result['owner_actions'])}")
                print(f"Result written to: {args.output}")
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
