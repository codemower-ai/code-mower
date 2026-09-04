#!/usr/bin/env python3
"""Release qualification runner with stable adoption-result schema."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
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
RUNTIME_CLASS_PATTERN = re.compile(r"^python_\d+\.\d+$")
VALID_CONTEXTS = {"cold_install", "upgrade", "unknown"}
VALID_STEP_STATUSES = {"pass", "fail", "warn", "unavailable", "planned"}
VALID_EXECUTION_STATES = {"planned", "executed"}
VALID_HOST_CLASSES = {"local", "ci", "github_actions", "unknown"}
VALID_OUTCOMES = {"pass", "pass_with_warnings", "fail", "incomplete"}

ADOPTION_RESULT_SCHEMA = "code_mower.adoptionResult.v1"
ADOPTION_RESULT_FIELDS = frozenset(
    {
        "schema",
        "timestamp_utc",
        "release_tag",
        "package_identity",
        "normalized_version",
        "qualification_context",
        "starting_version",
        "ending_version",
        "provider",
        "executor",
        "host_class",
        "runtime_class",
        "execution_state",
        "elapsed_seconds",
        "outcome",
        "steps",
    }
)
ADOPTION_RESULT_STEP_FIELDS = frozenset(
    {"id", "status", "elapsed_seconds", "warning_count", "owner_action_count"}
)
MAX_ADOPTION_RESULT_STEPS = 32
# ISO 8601 date/time with a mandatory UTC/offset designator ("Z" or
# +HH:MM/-HH:MM). Deliberately rejects bare local timestamps, paths,
# multiline values, and any other free-form string.
TIMESTAMP_UTC_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})"
)


@dataclass
class StepResult:
    """Step execution result."""

    id: str
    status: str
    elapsed_seconds: float
    warning_count: int
    owner_action_count: int

    def __post_init__(self) -> None:
        if self.status not in VALID_STEP_STATUSES:
            self.status = "fail"


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
    execution_state: str
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


def _version_key(value: str) -> tuple[int, int, int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", value)
    if not match:
        raise ValueError("version must be normalized")
    stage_rank = {"a": 0, "b": 1, "rc": 2, None: 3}[match.group(4)]
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        stage_rank,
        int(match.group(5) or 0),
    )


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


def _aggregate_outcome(steps: list[StepResult], *, execution_state: str = "executed") -> str:
    """Aggregate step statuses to overall outcome."""
    if execution_state not in VALID_EXECUTION_STATES:
        return "fail"
    has_fail = any(s.status == "fail" for s in steps)
    has_warn = any(s.status == "warn" for s in steps)
    has_unavailable = any(s.status == "unavailable" for s in steps)
    has_planned = any(s.status == "planned" for s in steps)

    if has_fail:
        return "fail"
    if execution_state == "planned":
        return "incomplete"
    if has_planned:
        return "fail"
    if has_warn or has_unavailable:
        return "pass_with_warnings"
    return "pass"


def _finite_non_negative(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"adoption result {field} must be finite and non-negative")


def _validate_timestamp_utc(value: object) -> None:
    """Validate timestamp_utc is a real ISO 8601 timestamp with a UTC/offset designator.

    A bare regex match is not enough: month/day/hour ranges are checked too,
    so a plausible-looking-but-invalid string (e.g. month 13) is rejected the
    same as a path, a multiline value, or free-form text.
    """
    if not isinstance(value, str) or not TIMESTAMP_UTC_PATTERN.fullmatch(value):
        raise ValueError(
            "adoption result timestamp_utc must be an ISO 8601 timestamp with a UTC/offset designator"
        )
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("adoption result timestamp_utc must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("adoption result timestamp_utc must include a UTC/offset designator")


def validate_adoption_result_payload(result: object) -> None:
    """Strictly validate one closed-schema local adoptionResult payload.

    Rejects any undeclared top-level or step field so raw output, local
    paths, prompts, or secrets accidentally attached by an adapter, a
    manual upload, or a GitHub comment can never enter campaign state.
    """
    if not isinstance(result, dict):
        raise ValueError("adoption result must be a JSON object")

    unknown = sorted(set(result) - ADOPTION_RESULT_FIELDS)
    if unknown:
        raise ValueError(f"adoption result has unsupported field(s): {unknown}")
    missing = sorted(ADOPTION_RESULT_FIELDS - set(result))
    if missing:
        raise ValueError(f"adoption result missing required field(s): {missing}")

    if result.get("schema") != ADOPTION_RESULT_SCHEMA:
        raise ValueError(f"unsupported adoption result schema {result.get('schema')!r}")

    _validate_timestamp_utc(result.get("timestamp_utc"))

    valid_tag, normalized_version, tag_error = _validate_tag_format(str(result.get("release_tag") or ""))
    if not valid_tag:
        raise ValueError(tag_error)
    if result.get("normalized_version") != normalized_version:
        raise ValueError("adoption result release_tag and normalized_version disagree")
    if result.get("package_identity") != "code-mower":
        raise ValueError("adoption result package_identity must be 'code-mower'")

    context = result.get("qualification_context")
    _validate_qualification_context(str(context or ""))
    starting_version = str(result.get("starting_version") or "")
    _validate_starting_version(starting_version)
    ending_version = str(result.get("ending_version") or "")
    if ending_version and not VERSION_PATTERN.match(ending_version):
        raise ValueError("adoption result ending_version must be empty or a normalized version")
    if context == "upgrade":
        if not starting_version:
            raise ValueError("adoption result upgrade context requires starting_version")
        if _version_key(starting_version) >= _version_key(normalized_version):
            raise ValueError("adoption result starting_version must be lower than normalized_version")
    elif starting_version:
        raise ValueError("adoption result starting_version is only valid for upgrade context")

    for field_name in ("provider", "executor"):
        _validate_safe_identifier(str(result.get(field_name) or ""), field_name)

    if result.get("host_class") not in VALID_HOST_CLASSES:
        raise ValueError(f"unsupported adoption result host_class {result.get('host_class')!r}")
    runtime_class = str(result.get("runtime_class") or "")
    if runtime_class != "unknown" and not RUNTIME_CLASS_PATTERN.match(runtime_class):
        raise ValueError(
            "adoption result runtime_class must be 'unknown' or 'python_<major>.<minor>'"
        )

    execution_state = result.get("execution_state")
    if execution_state not in VALID_EXECUTION_STATES:
        raise ValueError(f"unsupported adoption result execution_state {execution_state!r}")
    outcome = result.get("outcome")
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"unsupported adoption result outcome {outcome!r}")

    if execution_state == "executed" and outcome in {"pass", "pass_with_warnings"}:
        if ending_version != normalized_version:
            raise ValueError(
                "adoption result ending_version must equal normalized_version for an "
                "executed pass/pass_with_warnings result"
            )

    _finite_non_negative(result.get("elapsed_seconds"), "elapsed_seconds")

    steps = result.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("adoption result steps must be a non-empty list")
    if len(steps) > MAX_ADOPTION_RESULT_STEPS:
        raise ValueError(f"adoption result has too many steps; max {MAX_ADOPTION_RESULT_STEPS}")

    parsed_steps: list[StepResult] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"adoption result step {index} must be an object")
        unknown_step = sorted(set(step) - ADOPTION_RESULT_STEP_FIELDS)
        if unknown_step:
            raise ValueError(f"adoption result step {index} has unsupported field(s): {unknown_step}")
        missing_step = sorted(ADOPTION_RESULT_STEP_FIELDS - set(step))
        if missing_step:
            raise ValueError(f"adoption result step {index} missing field(s): {missing_step}")

        step_id = step.get("id")
        if not isinstance(step_id, str) or not SAFE_IDENTIFIER_PATTERN.match(step_id):
            raise ValueError(f"adoption result step {index} id must be a safe identifier")
        status = step.get("status")
        if status not in VALID_STEP_STATUSES:
            raise ValueError(f"adoption result step {index} has unsupported status {status!r}")
        _finite_non_negative(step.get("elapsed_seconds"), f"step {index} elapsed_seconds")
        for count_field in ("warning_count", "owner_action_count"):
            value = step.get(count_field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"adoption result step {index} {count_field} must be a non-negative integer"
                )
        parsed_steps.append(
            StepResult(
                id=step_id,
                status=status,
                elapsed_seconds=float(step["elapsed_seconds"]),
                warning_count=int(step["warning_count"]),
                owner_action_count=int(step["owner_action_count"]),
            )
        )

    expected_outcome = _aggregate_outcome(parsed_steps, execution_state=execution_state)
    if outcome != expected_outcome:
        raise ValueError(
            f"adoption result outcome {outcome!r} disagrees with step statuses; "
            f"expected {expected_outcome!r}"
        )


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
        raise ValueError("starting_version is required for upgrade qualification")
    if qualification_context != "upgrade" and starting_version:
        raise ValueError("starting_version is only valid for upgrade qualification")
    if (
        qualification_context == "upgrade"
        and _version_key(starting_version) >= _version_key(normalized_version)
    ):
        raise ValueError("starting_version must be lower than the target version")

    ending_version = ""
    execution_state = "planned" if dry_run else "executed"
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
            rehearsal_result = run_package_install_rehearsal(
                package_spec=package_spec,
                preinstall_package_spec=(
                    f"code-mower=={starting_version}"
                    if qualification_context == "upgrade"
                    else ""
                ),
                repo_path=repo_path,
                timeout=timeout,
                allow_package_index=True,
            )
            rehearsal_version_raw = rehearsal_result.get("version", "")
            rehearsal_version = _normalize_version(rehearsal_version_raw)
            preinstall_version = _normalize_version(
                rehearsal_result.get("preinstall_version", "")
            )
            ending_version = rehearsal_version
            if (
                rehearsal_version != normalized_version
                or (
                    qualification_context == "upgrade"
                    and preinstall_version != starting_version
                )
            ):
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

    outcome = _aggregate_outcome(steps, execution_state=execution_state)
    elapsed = time.time() - start_time

    result = AdoptionResult(
        schema=ADOPTION_RESULT_SCHEMA,
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
        execution_state=execution_state,
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

    campaign = subparsers.add_parser(
        "campaign",
        help="Run or manage multi-provider release qualification campaign",
    )
    campaign.add_argument(
        "action",
        nargs="?",
        default=None,
        choices=["create", "status", "resume", "dispatch"],
        help=(
            "Optional campaign action. create: start a new campaign (fails if one "
            "already exists for the identifier). status: inspect only. "
            "resume/dispatch: advance an existing campaign (fails if none exists). "
            "Omitting the action creates a new campaign, or advances the existing "
            "one when the identifier already names a campaign."
        ),
    )
    campaign.add_argument(
        "--release-tag",
        default="",
        help="Exact release tag (e.g., v1.0.0)",
    )
    campaign.add_argument(
        "--package-spec",
        default="",
        help="Exact package-index spec (e.g., code-mower==1.0.0)",
    )
    campaign.add_argument(
        "--providers",
        default="",
        help="Comma-separated provider list (default: claude,codex,antigravity,muse,cursor_bugbot,devin)",
    )
    campaign.add_argument(
        "--qualification-context",
        default="cold_install",
        help="Qualification context (cold_install/upgrade/unknown)",
    )
    campaign.add_argument(
        "--starting-version",
        default="",
        help="Starting version (required for upgrade qualification)",
    )
    campaign.add_argument(
        "--repo-path",
        type=Path,
        default=None,
        help="Repository path (defaults to current directory)",
    )
    campaign.add_argument(
        "--repo-slug",
        default="",
        help=(
            "Repository slug (OWNER/REPO) used for hosted dispatch and polling. "
            "May be supplied later to fill a campaign created without one; it can "
            "never change a slug the campaign already carries."
        ),
    )
    campaign.add_argument(
        "--issue",
        default="",
        help="Optional GitHub issue number for remote or comment dispatch",
    )
    campaign.add_argument(
        "--apply",
        action="store_true",
        help="Apply mutations, remote dispatch, or paid work (default is dry-run)",
    )
    campaign.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing campaign (poll running or dispatch pending)",
    )
    campaign.add_argument(
        "--status",
        action="store_true",
        help=(
            "Inspect status of an existing campaign without dispatching. With an "
            "explicit --campaign-id/--release-tag this reports that campaign or "
            "fails; without one it reports the most recently updated campaign"
        ),
    )
    campaign.add_argument(
        "--campaign-id",
        default="",
        help="Optional campaign identifier",
    )
    campaign.add_argument(
        "--campaigns-dir",
        type=Path,
        default=None,
        help="Directory to store campaign files (defaults to .code-mower/campaigns)",
    )
    campaign.add_argument(
        "--record-result",
        type=Path,
        default=None,
        help="Path to an adoption result JSON to record for a provider",
    )
    campaign.add_argument(
        "--record-provider",
        default="",
        help="Provider identity when manually recording an adoption result",
    )
    campaign.add_argument(
        "--retry-provider",
        default="",
        help=(
            "Explicitly retry one provider's applied dispatch/adapter attempt "
            "(must already be a campaign member). This is the only way to "
            "repeat a provider whose applied attempt was already made."
        ),
    )
    campaign.add_argument("--json", action="store_true")

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
            return 0 if result["outcome"] != "fail" else 1
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"error: qualification failed: {e}", file=sys.stderr)
            return 1

    if args.command == "campaign":
        try:
            from . import release_campaigns
        except ImportError:
            import release_campaigns  # type: ignore
        providers_list = (
            [p.strip() for p in args.providers.split(",") if p.strip()]
            if args.providers
            else ()
        )
        return release_campaigns.campaign_command(
            action=args.action,
            release_tag=args.release_tag,
            package_spec=args.package_spec,
            providers=providers_list,
            qualification_context=args.qualification_context,
            starting_version=args.starting_version,
            repo_path=args.repo_path,
            repo_slug=args.repo_slug,
            issue=args.issue,
            apply=args.apply,
            resume=args.resume,
            status=args.status,
            campaign_id=args.campaign_id,
            campaigns_dir=args.campaigns_dir,
            record_result=args.record_result,
            record_provider=args.record_provider,
            retry_provider=args.retry_provider,
            emit_json=args.json,
        )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
