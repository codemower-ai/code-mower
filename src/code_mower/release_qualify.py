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
from datetime import datetime, timezone
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

# A normalized (PEP 503) distribution name: lowercase ASCII letters and digits
# with single `-` separators. Package identities are stored in campaign state
# and compared for exact equality, so they are held to this bounded alphabet
# rather than accepted as free-form text.
NORMALIZED_PACKAGE_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# An exact package-index spec, `<name>==<version>`, and the only place a spec is
# taken apart. The name half is the grammar a package index itself accepts --
# letters, digits, `-`, `_` and `.` -- so `zope.interface` and `code.mower` parse
# and are then normalized; the version half is bounded to the characters a
# version can contain, so extras, environment markers, and inexact operators are
# not mistaken for a version.
EXACT_PACKAGE_SPEC_PATTERN = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]*)$"
)
# The one package Code Mower's *own* built-in qualification runner can qualify:
# its install rehearsal installs the distribution into a clean virtualenv and
# then drives the `code-mower` console script to read the installed version
# back. Campaigns are not limited to this package -- each campaign binds the
# exact spec it was created with (see `validate_adoption_result_payload`) and
# lets each provider's own adapter do the qualifying.
BUILTIN_QUALIFICATION_PACKAGE = "code-mower"

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

#: Step ids the built-in `release qualify` runner emits. Provider-authored
#: results may use these or a namespaced extension (below); arbitrary
#: unnamespaced ids are rejected so cross-provider analytics stay comparable.
BUILTIN_QUALIFICATION_STEP_IDS = frozenset(
    {"board", "doctor", "lanes_status", "overhead", "package_install"}
)
#: Explicit provider-extension step-id form: `<namespace>__<name>`, with both
#: halves safe identifiers. The double underscore keeps extensions distinct
#: from every built-in id while staying inside the metadata-only alphabet.
PROVIDER_STEP_EXTENSION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}__[a-z][a-z0-9_]{0,31}$")

#: Trust bounds for an executed result's timestamp_utc. Planned previews carry
#: the wall-clock time of a dry run that did nothing, so only executed results
#: are bounded: not older than this fixed floor, and not newer than now plus a
#: small deterministic clock-skew tolerance.
ADOPTION_RESULT_EARLIEST_TIMESTAMP_UTC = "2020-01-01T00:00:00Z"
ADOPTION_RESULT_FUTURE_SKEW_SECONDS = 300
#: Step timings are measured independently from the overall elapsed time, so
#: their sum must match it within this absolute tolerance: each value is
#: stored rounded to two decimals and per-step timers add overhead, covered
#: by this tolerance in both directions.
ADOPTION_RESULT_STEP_TOTAL_TOLERANCE_SECONDS = 1.0


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


def _normalize_package_name(name: str) -> str:
    """Normalize a distribution name the way PEP 503 does.

    `Code_Mower`, `code.mower`, and `code-mower` are one package to a package
    index, so they are one identity here too -- otherwise a result could be
    refused for a campaign whose spec named the same package with different
    punctuation, or accepted for one that named a different package.
    """
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _parse_exact_package_spec(package_spec: str) -> tuple[str, str]:
    """Parse an exact `name==version` package-index spec once, into both halves.

    Returns the PEP 503-normalized package identity and the exact version the
    spec pins. Every caller that needs either half goes through this one
    function: a second, slightly different spelling of the name grammar
    elsewhere would let a spec parse for one purpose and not the other --
    `zope.interface==5.0.0` yielding an identity while its version came back
    unreadable, and the campaign then being refused for a version mismatch it
    does not have.

    The spec must be an exact package-index spec: paths, URLs, VCS specs, extras,
    and inexact requirements have no single package identity and version that a
    qualification result could be bound to, and are refused. The identity is
    *derived from the spec* rather than assumed to be Code Mower, so a campaign
    created for an exact spec binds its results to that package. Code Mower's own
    built-in runner is separately limited to the package it can actually install
    and verify (see `BUILTIN_QUALIFICATION_PACKAGE`).
    """
    candidate = package_spec.strip()
    if _package_spec_uses_package_index(candidate):
        match = EXACT_PACKAGE_SPEC_PATTERN.match(candidate)
        if match:
            identity = _normalize_package_name(match.group("name"))
            if NORMALIZED_PACKAGE_NAME_PATTERN.match(identity):
                return identity, match.group("version")
    raise ValueError(
        "Only exact package-index specs supported: package spec must be <name>==<version>"
    )


def _extract_package_identity(package_spec: str) -> str:
    """The normalized package identity of an exact package-index spec.

    A thin projection of :func:`_parse_exact_package_spec` for callers that need
    only the identity, so identity and version never come from separate parses.
    """
    return _parse_exact_package_spec(package_spec)[0]


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


def _validate_step_id(step_id: object, index: int) -> None:
    """Accept only the built-in step taxonomy or a namespaced extension."""
    if isinstance(step_id, str) and (
        step_id in BUILTIN_QUALIFICATION_STEP_IDS
        or PROVIDER_STEP_EXTENSION_PATTERN.match(step_id)
    ):
        return
    raise ValueError(
        f"adoption result step {index} id must be a built-in qualification "
        "step or a namespaced '<namespace>__<name>' provider extension"
    )


def _validate_executed_timestamp_bounds(value: object) -> None:
    """Bound an executed result's timestamp_utc against the trust window.

    Runs after `_validate_timestamp_utc`, so the value already parses; the
    bounds themselves are deterministic (a fixed floor plus now with a fixed
    skew tolerance). Messages name only the bound, never the value.
    """
    text = value if isinstance(value, str) else ""
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    # Runs after _validate_timestamp_utc, so this parse cannot fail.
    parsed = datetime.fromisoformat(normalized)
    earliest = datetime.fromisoformat(
        ADOPTION_RESULT_EARLIEST_TIMESTAMP_UTC.replace("Z", "+00:00")
    )
    if parsed < earliest:
        raise ValueError(
            "adoption result timestamp_utc is older than the trusted executed-result bound"
        )
    now = datetime.now(timezone.utc)
    if (parsed - now).total_seconds() > ADOPTION_RESULT_FUTURE_SKEW_SECONDS:
        raise ValueError(
            "adoption result timestamp_utc is newer than the trusted executed-result bound"
        )


def validate_adoption_result_payload(
    result: object,
    *,
    expected_package_identity: str = "",
) -> None:
    """Strictly validate one closed-schema local adoptionResult payload.

    Rejects any undeclared top-level or step field so raw output, local
    paths, prompts, or secrets accidentally attached by an adapter, a
    manual upload, or a GitHub comment can never enter campaign state.

    ``expected_package_identity`` binds the result to one package. Callers that
    hold a campaign's exact package spec pass the identity derived from *that
    spec* (see :func:`_extract_package_identity`), so a result for some other
    distribution can never satisfy the campaign -- and a campaign for a package
    other than Code Mower is not refused just for being one. Left empty, only
    the structural check applies: the field must still be a normalized package
    name, so an unbound caller can never store free-form text as an identity.
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
    package_identity = result.get("package_identity")
    if not isinstance(package_identity, str) or not NORMALIZED_PACKAGE_NAME_PATTERN.match(
        package_identity
    ):
        raise ValueError("adoption result package_identity must be a normalized package name")
    if expected_package_identity and package_identity != expected_package_identity:
        raise ValueError(
            f"adoption result package_identity {package_identity!r} does not match the "
            f"campaign package {expected_package_identity!r}"
        )

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

    if execution_state == "executed":
        _validate_executed_timestamp_bounds(result.get("timestamp_utc"))

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

        _validate_step_id(step.get("id"), index)
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
        if status == "pass" and step["owner_action_count"] > 0:
            raise ValueError(
                f"adoption result step {index} reports status 'pass' with a "
                "nonzero owner_action_count"
            )
        parsed_steps.append(
            StepResult(
                id=str(step["id"]),
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

    step_total = sum(step.elapsed_seconds for step in parsed_steps)
    if (
        abs(step_total - float(result["elapsed_seconds"]))
        > ADOPTION_RESULT_STEP_TOTAL_TOLERANCE_SECONDS
    ):
        raise ValueError(
            "adoption result step elapsed_seconds differ from total elapsed_seconds "
            "beyond tolerance"
        )
    if outcome == "pass" and any(
        step.owner_action_count > 0 for step in parsed_steps
    ):
        raise ValueError(
            "adoption result outcome 'pass' requires zero owner_action_count"
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

    # One parse of the spec yields both the identity this run binds its result to
    # and the version it pins, so the two can never be judged by different
    # grammars.
    package_identity, spec_version = _parse_exact_package_spec(package_spec)
    if spec_version != normalized_version:
        raise ValueError(f"Version mismatch: tag {normalized_version} vs spec version")

    if package_identity != BUILTIN_QUALIFICATION_PACKAGE:
        # Not a general narrowing of package identity -- campaigns bind whatever
        # exact spec they were created with. This runner specifically installs
        # the distribution into a clean virtualenv and reads the installed
        # version back through the `code-mower` console script, so it can only
        # honestly qualify that one package. Refuse rather than emit a result
        # whose package_install step is guaranteed to fail for the wrong reason.
        raise ValueError(
            f"built-in release qualification only supports the "
            f"{BUILTIN_QUALIFICATION_PACKAGE} package"
        )

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

    elapsed = round(time.time() - start_time, 2)
    measured_step_seconds = round(sum(step.elapsed_seconds for step in steps), 2)
    steps.append(
        StepResult(
            id="overhead",
            status="planned" if dry_run else "pass",
            elapsed_seconds=max(0.0, round(elapsed - measured_step_seconds, 2)),
            warning_count=0,
            owner_action_count=0,
        )
    )
    outcome = _aggregate_outcome(steps, execution_state=execution_state)

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
        elapsed_seconds=elapsed,
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
        description=(
            "Run or manage a multi-provider release qualification campaign. "
            "Mutating invocations (create, resume, dispatch, --record-result, "
            "--retry-provider) are serialized: each takes an exclusive advisory "
            "lock on the campaign directory and holds it while it loads state, "
            "claims a provider attempt, invokes an adapter or posts a hosted "
            "dispatch, and writes the result back. Two such commands started at "
            "the same time therefore run one after the other, and the second one "
            "sees the first one's recorded attempts -- so concurrency can never "
            "duplicate a local adapter run or a paid/hosted dispatch. The lock is "
            "released by the operating system if a command crashes or is killed, "
            "so a dead run never blocks the next one. Reads are lock-free: "
            "`--status` and Board reads take no lock, need no writable campaign "
            "directory, and stay available during a long applied run. Campaign "
            "files are published with an atomic rename, so a read never observes "
            "a half-written campaign. Status is strictly read-only: `--status` "
            "or the `status` action combined with a mutating intent "
            "(--retry-provider, --record-result, --apply, --resume, or another "
            "action) is rejected with a bounded error before any lock, mutation, "
            "poll, or dispatch, rather than silently dropping the mutation. An "
            "explicit action combined with a flag naming a different one (such "
            "as `create --resume`) is refused the same way, before any lookup, "
            "directory creation, lock, or dispatch. Option scope is enforced: "
            "`--interval` is accepted only for `watch`, and `--timeout` only for "
            "`watch` and `upload`."
        ),
    )
    campaign.add_argument(
        "action",
        nargs="?",
        default=None,
        choices=["create", "status", "resume", "dispatch", "upload", "watch"],
        help=(
            "Optional campaign action. create: start a new campaign (fails if one "
            "already exists for the identifier). status: inspect only. "
            "resume/dispatch: advance an existing campaign (fails if none exists). "
            "watch: poll a stored campaign at a positive interval and bounded timeout "
            "(--interval, --timeout). "
            "upload: convert every completed provider's qualification result into "
            "metadata-only cloud adoption_run events (--timeout for bounded network post); "
            "preview by default, network upload only with --yes. "
            "Omitting the action creates a new campaign, or advances the existing "
            "one when the identifier already names a campaign. An action may be "
            "spelled with the equivalent legacy flag (status with --status, "
            "resume/dispatch with --resume), but never with a flag naming a "
            "different action: `create --resume` is rejected, not resolved."
        ),
    )
    campaign.add_argument(
        "--release-tag",
        default="",
        help=(
            "Exact release tag (e.g., v1.0.0). Used alone it selects the campaign "
            "whose stored release tag matches exactly -- never a campaign stored "
            "under that text as a custom --campaign-id. If several campaigns "
            "share the tag the request is rejected as ambiguous; name one with "
            "--campaign-id"
        ),
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
        default="",
        help=(
            "Qualification context (cold_install/upgrade/unknown). Defaults to "
            "cold_install when creating a campaign. When it is supplied against "
            "an existing campaign it must match that campaign's stored context, "
            "including an explicit cold_install; omit it to advance a campaign "
            "under whatever context it was created with."
        ),
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
            "fails; without one it reports the most recently updated campaign. "
            "Read-only: it cannot be combined with --retry-provider, "
            "--record-result, --apply, --resume, or a non-status action"
        ),
    )
    campaign.add_argument(
        "--campaign-id",
        default="",
        help=(
            "Optional campaign identifier (default: campaign-<release-tag>). "
            "Must use only lowercase ASCII letters, digits, '.', '_', and '-', "
            "start with a letter or digit, and be at most 64 characters. The id "
            "is used verbatim as the stored file's name, so ids map one-to-one "
            "onto campaigns; an id outside this alphabet is rejected with a "
            "bounded error rather than rewritten to fit."
        ),
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
            "repeat a provider whose applied attempt was already made. It "
            "mutates, so it cannot be combined with --status or the status "
            "action."
        ),
    )
    campaign.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Perform the upload network post for the `upload` action; without it "
            "upload prints the same event set as an inspectable preview and "
            "nothing leaves this machine. Applies only to `upload`"
        ),
    )
    campaign.add_argument(
        "--endpoint",
        default="",
        help="Cloud upload endpoint for `upload` (defaults to CODE_MOWER_CLOUD_ENDPOINT)",
    )
    campaign.add_argument(
        "--token-env",
        default="",
        help="Environment variable holding the Code Mower Cloud token for `upload`",
    )
    campaign.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="Token env file to use for `upload` when the token env is not set",
    )
    campaign.add_argument(
        "--token-dir",
        type=Path,
        default=None,
        help="Directory with Code Mower Cloud token profiles to use for `upload`",
    )
    campaign.add_argument(
        "--install-id",
        default="",
        help="Stored token profile name and install identity to use for `upload`",
    )
    campaign.add_argument(
        "--team-id",
        default="",
        help="Team identity to attribute uploaded adoption_run events to",
    )
    campaign.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Polling interval in seconds; valid only for the 'watch' action (defaults to 10.0)",
    )
    campaign.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Timeout in seconds; valid only for the 'watch' action (bounded "
            "duration, defaults to 600.0) and the 'upload' action (request "
            "timeout, defaults to 20.0)"
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
        # Same guard shape as `qualify` above: the campaign implementation
        # already answers every anticipated failure with its own bounded,
        # path-free message and a non-zero exit, and those returns pass straight
        # through here. This only catches what nothing else did, so an
        # unexpected implementation exception ends as one bounded line instead
        # of a raw traceback. The generic arm reports the exception *type*
        # rather than its text, because an arbitrary exception's message may
        # carry a local path and the campaign surface stays metadata-only.
        try:
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
                yes=args.yes,
                endpoint=args.endpoint,
                token_env=args.token_env,
                token_file=args.token_file,
                token_dir=args.token_dir,
                install_id=args.install_id,
                team_id=args.team_id,
                timeout=args.timeout,
                interval=args.interval,
                emit_json=args.json,
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"error: campaign failed: {type(e).__name__}", file=sys.stderr)
            return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
