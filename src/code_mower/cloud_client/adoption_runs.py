"""Metadata-only release-qualification contract for CodeMower.com uploads."""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import uuid
from typing import Any, Mapping

from code_mower import __version__
from code_mower.providers import build_code_mower_tool_provenance

from .bundle import validate_metadata_payload
from .errors import CloudBundleError


ADOPTION_RUN_EVENT_TYPE = "adoption_run"
ADOPTION_RUN_SCHEMA = "code_mower.adoptionRun.v1"
ADOPTION_RESULT_SCHEMA = "code_mower.adoptionResult.v1"
ADOPTION_CONVERTER_SOURCE = "code-mower release qualify"

ADOPTION_CONTEXTS = ("cold_install", "upgrade", "unknown")
ADOPTION_EXECUTION_STATES = ("planned", "executed")
ADOPTION_OUTCOMES = ("pass", "pass_with_warnings", "fail", "incomplete")
ADOPTION_HOST_CLASSES = ("local", "ci", "github_actions", "unknown")
ADOPTION_COVERAGE_VALUES = ("complete", "partial", "unknown")
ADOPTION_STEP_STATUSES = ("pass", "fail", "warn", "unavailable", "planned")

ADOPTION_RUN_DIMENSIONS = (
    "adoption_run_schema",
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
    "outcome",
    "result_timestamp",
    "provenance_coverage",
)

ADOPTION_RUN_COUNT_METRICS = (
    "adoption_run_count",
    "step_count",
    "step_pass_count",
    "step_warn_count",
    "step_fail_count",
    "step_unavailable_count",
    "step_planned_count",
    "warning_count",
    "owner_action_count",
)

ADOPTION_RUN_TIME_METRICS = ("elapsed_seconds",)

_TAG_PATTERN = re.compile(r"^v(\d+\.\d+\.\d+)(?:-(alpha|beta|rc)\.(\d+))?$")
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[ab]\d+|rc\d+)?$")
_SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: Step taxonomy in lockstep with release_qualify: the built-in qualification
#: steps plus an explicit `<namespace>__<name>` provider-extension form.
#: Arbitrary unnamespaced ids are rejected. Kept local on purpose (see
#: _expected_outcome): cloud_client must not import the qualification runner.
BUILTIN_QUALIFICATION_STEP_IDS = frozenset(
    {"board", "doctor", "lanes_status", "package_install"}
)
_PROVIDER_STEP_EXTENSION_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]{0,31}__[a-z][a-z0-9_]{0,31}$"
)
ADOPTION_RESULT_EARLIEST_TIMESTAMP_UTC = "2020-01-01T00:00:00Z"
ADOPTION_RESULT_FUTURE_SKEW_SECONDS = 300
ADOPTION_RESULT_STEP_TOTAL_TOLERANCE_SECONDS = 1.0
_RUNTIME_CLASS_PATTERN = re.compile(r"^python_\d+\.\d+$")
_PATH_LIKE_PATTERN = re.compile(
    r"/(home|Users|tmp|var|etc|root|code-mower)/|^[A-Za-z]:\\|~/|\.\.[/\\]"
)


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CloudBundleError(f"adoption_run {field} must be a non-empty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise CloudBundleError(f"adoption_run {field} must be a string")
    return value.strip()


def _single_line(value: str, field: str) -> str:
    if "\n" in value or "\r" in value:
        raise CloudBundleError(
            f"adoption_run {field} must be single-line metadata, not prose or output"
        )
    if _PATH_LIKE_PATTERN.search(value):
        raise CloudBundleError(
            f"adoption_run {field} must not contain local paths"
        )
    return value


def _timestamp(value: object, field: str) -> dt.datetime:
    text = _required_text(value, field)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CloudBundleError(
            f"adoption_run {field} must be an ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CloudBundleError(f"adoption_run {field} must include a UTC offset")
    return parsed


def _count(metrics: Mapping[str, Any], field: str, *, required: bool = False) -> int | None:
    value = metrics.get(field)
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CloudBundleError(
            f"adoption_run metric {field!r} must be a non-negative integer"
        )
    return value


def _normalized_from_tag(release_tag: str) -> str:
    match = _TAG_PATTERN.match(release_tag)
    if not match:
        raise CloudBundleError(
            "adoption_run dimension 'release_tag' must match "
            "v<major>.<minor>.<patch>[-<stage>.<num>]"
        )
    base, stage, num = match.group(1), match.group(2), match.group(3)
    if stage and num:
        stage_map = {"alpha": "a", "beta": "b", "rc": "rc"}
        return f"{base}{stage_map[stage]}{num}"
    return base


def _version_key(value: str) -> tuple[int, int, int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", value)
    if not match:
        raise CloudBundleError(
            f"adoption_run version {value!r} must be a normalized version"
        )
    stage_rank = {"a": 0, "b": 1, "rc": 2, None: 3}[match.group(4)]
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        stage_rank,
        int(match.group(5) or 0),
    )


def _check_version(value: str, field: str) -> str:
    text = _optional_text(value, field)
    if text and not _VERSION_PATTERN.match(text):
        raise CloudBundleError(
            f"adoption_run dimension {field!r} must be empty or a normalized version"
        )
    return text


def validate_adoption_run_payload(event: Mapping[str, Any]) -> None:
    """Validate one closed, metadata-only adoption_run event."""

    dimensions = event.get("dimensions")
    metrics = event.get("metrics")
    if not isinstance(dimensions, Mapping):
        raise CloudBundleError("adoption_run dimensions must be an object")
    if not isinstance(metrics, Mapping):
        raise CloudBundleError("adoption_run metrics must be an object")
    if dimensions.get("adoption_run_schema") != ADOPTION_RUN_SCHEMA:
        raise CloudBundleError(
            "adoption_run dimensions.adoption_run_schema must be "
            f"{ADOPTION_RUN_SCHEMA!r}"
        )
    unknown_dimensions = [
        str(key) for key in dimensions if key not in ADOPTION_RUN_DIMENSIONS
    ]
    if unknown_dimensions:
        raise CloudBundleError(
            f"unsupported adoption_run dimension {unknown_dimensions[0]!r}"
        )
    for key, value in dimensions.items():
        if not isinstance(value, str):
            raise CloudBundleError(
                f"adoption_run dimension {key!r} must be a string"
            )
        _single_line(value, f"dimension {key!r}")

    release_tag = _required_text(dimensions.get("release_tag"), "dimension 'release_tag'")
    normalized_version = _required_text(
        dimensions.get("normalized_version"), "dimension 'normalized_version'"
    )
    if not _VERSION_PATTERN.match(normalized_version):
        raise CloudBundleError(
            "adoption_run dimension 'normalized_version' must be a normalized version"
        )
    if _normalized_from_tag(release_tag) != normalized_version:
        raise CloudBundleError(
            "adoption_run release_tag and normalized_version disagree"
        )

    package_identity = _required_text(
        dimensions.get("package_identity"), "dimension 'package_identity'"
    )
    if package_identity != "code-mower":
        raise CloudBundleError(
            "adoption_run dimension 'package_identity' must be 'code-mower'"
        )

    context = _required_text(
        dimensions.get("qualification_context"), "dimension 'qualification_context'"
    )
    if context not in ADOPTION_CONTEXTS:
        raise CloudBundleError(
            f"unsupported adoption_run qualification_context {context!r}"
        )
    starting_version = _check_version(
        dimensions.get("starting_version"), "'starting_version'"
    )
    _check_version(dimensions.get("ending_version"), "'ending_version'")
    if context == "upgrade":
        if not starting_version:
            raise CloudBundleError(
                "adoption_run upgrade context requires starting_version"
            )
        if _version_key(starting_version) >= _version_key(normalized_version):
            raise CloudBundleError(
                "adoption_run starting_version must be lower than normalized_version"
            )
    elif starting_version:
        raise CloudBundleError(
            "adoption_run starting_version is only valid for upgrade context"
        )

    for field in ("provider", "executor"):
        text = _required_text(dimensions.get(field), f"dimension {field!r}")
        if not _SAFE_IDENTIFIER_PATTERN.match(text):
            raise CloudBundleError(
                f"adoption_run dimension {field!r} must be a safe identifier"
            )

    host_class = _required_text(dimensions.get("host_class"), "dimension 'host_class'")
    if host_class not in ADOPTION_HOST_CLASSES:
        raise CloudBundleError(
            f"unsupported adoption_run host_class {host_class!r}"
        )
    runtime_class = _required_text(
        dimensions.get("runtime_class"), "dimension 'runtime_class'"
    )
    if runtime_class != "unknown" and not _RUNTIME_CLASS_PATTERN.match(runtime_class):
        raise CloudBundleError(
            "adoption_run dimension 'runtime_class' must be 'unknown' or "
            "'python_<major>.<minor>'"
        )

    execution_state = _required_text(
        dimensions.get("execution_state"), "dimension 'execution_state'"
    )
    if execution_state not in ADOPTION_EXECUTION_STATES:
        raise CloudBundleError(
            f"unsupported adoption_run execution_state {execution_state!r}"
        )
    outcome = _required_text(dimensions.get("outcome"), "dimension 'outcome'")
    if outcome not in ADOPTION_OUTCOMES:
        raise CloudBundleError(f"unsupported adoption_run outcome {outcome!r}")
    if execution_state == "executed" and outcome == "incomplete":
        raise CloudBundleError(
            "adoption_run executed runs must not report outcome 'incomplete'"
        )
    if execution_state == "planned" and outcome not in {"incomplete", "fail"}:
        raise CloudBundleError(
            "adoption_run planned runs must report outcome 'incomplete' or 'fail'"
        )

    _timestamp(dimensions.get("result_timestamp"), "dimension 'result_timestamp'")

    coverage = _required_text(
        dimensions.get("provenance_coverage"), "dimension 'provenance_coverage'"
    )
    if coverage not in ADOPTION_COVERAGE_VALUES:
        raise CloudBundleError(
            f"unsupported adoption_run provenance_coverage {coverage!r}"
        )
    if coverage == "complete" and (
        dimensions.get("provider") in ("", "unknown")
        or dimensions.get("executor") in ("", "unknown")
        or dimensions.get("host_class") == "unknown"
        or dimensions.get("runtime_class") == "unknown"
    ):
        raise CloudBundleError(
            "adoption_run complete provenance_coverage requires known "
            "provider, executor, host_class, and runtime_class"
        )

    allowed_metrics = set(ADOPTION_RUN_COUNT_METRICS + ADOPTION_RUN_TIME_METRICS)
    unknown_metrics = [str(key) for key in metrics if key not in allowed_metrics]
    if unknown_metrics:
        raise CloudBundleError(
            f"unsupported adoption_run metric {unknown_metrics[0]!r}; "
            "missing model, token, and cost data stays omitted, never zero-filled"
        )
    if _count(metrics, "adoption_run_count", required=True) != 1:
        raise CloudBundleError("adoption_run metric 'adoption_run_count' must equal 1")
    step_count = _count(metrics, "step_count", required=True)
    breakdown = {
        field: _count(metrics, field, required=True)
        for field in (
            "step_pass_count",
            "step_warn_count",
            "step_fail_count",
            "step_unavailable_count",
            "step_planned_count",
        )
    }
    if step_count is not None and sum(breakdown.values()) != step_count:
        raise CloudBundleError(
            "adoption_run step status counts must sum to step_count"
        )
    expected = _expected_outcome(
        execution_state=execution_state,
        fail_count=int(breakdown["step_fail_count"] or 0),
        warn_count=int(breakdown["step_warn_count"] or 0),
        unavailable_count=int(breakdown["step_unavailable_count"] or 0),
        planned_count=int(breakdown["step_planned_count"] or 0),
    )
    if outcome != expected:
        raise CloudBundleError(
            f"adoption_run outcome {outcome!r} disagrees with step statuses "
            f"for {execution_state} run; expected {expected!r}"
        )
    _count(metrics, "warning_count", required=True)
    _count(metrics, "owner_action_count", required=True)

    elapsed = metrics.get("elapsed_seconds")
    if elapsed is None:
        raise CloudBundleError("adoption_run metric 'elapsed_seconds' is required")
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, int | float)
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise CloudBundleError(
            "adoption_run metric 'elapsed_seconds' must be finite and non-negative"
        )


def _expected_outcome(
    *,
    execution_state: str,
    fail_count: int,
    warn_count: int,
    unavailable_count: int,
    planned_count: int,
) -> str:
    """Mirror release_qualify._aggregate_outcome without importing it.

    Kept local on purpose: cloud_client must not import the qualification
    runner (heavy, cycle-prone). Semantics must stay in lockstep with
    release_qualify._aggregate_outcome: any fail wins; planned runs never
    report pass; executed runs with unexecuted steps fail; warn/unavailable
    degrade to pass_with_warnings; otherwise pass.
    """

    if execution_state not in ADOPTION_EXECUTION_STATES:
        return "fail"
    if fail_count > 0:
        return "fail"
    if execution_state == "planned":
        return "incomplete"
    if planned_count > 0:
        return "fail"
    if warn_count > 0 or unavailable_count > 0:
        return "pass_with_warnings"
    return "pass"


def _validate_executed_timestamp_bounds(value: object) -> None:
    """Bound an executed result's timestamp_utc against the trust window.

    Lockstep with release_qualify: a fixed floor plus now with a fixed skew
    tolerance. Messages name only the bound, never the value.
    """

    text = value if isinstance(value, str) else ""
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CloudBundleError(
            "adoption result timestamp_utc must be an ISO 8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise CloudBundleError(
            "adoption result timestamp_utc must include a UTC offset"
        )
    earliest = dt.datetime.fromisoformat(
        ADOPTION_RESULT_EARLIEST_TIMESTAMP_UTC.replace("Z", "+00:00")
    )
    if parsed < earliest:
        raise CloudBundleError(
            "adoption result timestamp_utc is older than the trusted "
            "executed-result bound"
        )
    now = dt.datetime.now(dt.timezone.utc)
    if (parsed - now).total_seconds() > ADOPTION_RESULT_FUTURE_SKEW_SECONDS:
        raise CloudBundleError(
            "adoption result timestamp_utc is newer than the trusted "
            "executed-result bound"
        )


def _adoption_event_id(result: Mapping[str, Any]) -> str:
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"adoption-run:{canonical}"))


def adoption_result_to_event(
    result: Mapping[str, Any],
    *,
    repo_slug: str = "",
    team_id: str = "",
    install_id: str = "",
    source: str = ADOPTION_CONVERTER_SOURCE,
) -> dict[str, Any]:
    """Convert a local adoptionResult payload into a closed adoption_run event.

    Only release/package identity, provider/executor, qualification and
    execution state, coarse host/runtime class, outcome, elapsed time, bounded
    step/count summaries, warning and owner-action counts, and provenance
    coverage cross the boundary. Missing model, token, cost, and optional
    measurements stay omitted, never zero-filled. Report text is never
    included.
    """

    if not isinstance(result, Mapping):
        raise CloudBundleError("adoption result must be a JSON object")
    validate_metadata_payload(result)
    if result.get("schema") != ADOPTION_RESULT_SCHEMA:
        raise CloudBundleError(
            f"unsupported adoption result schema {result.get('schema')!r}; "
            f"expected {ADOPTION_RESULT_SCHEMA}"
        )

    steps = result.get("steps")
    if not isinstance(steps, list):
        raise CloudBundleError("adoption result steps must be a list")
    if len(steps) > 32:
        raise CloudBundleError("adoption result has too many steps; max 32")
    status_counts = {
        "step_pass_count": 0,
        "step_warn_count": 0,
        "step_fail_count": 0,
        "step_unavailable_count": 0,
        "step_planned_count": 0,
    }
    status_to_metric = {
        "pass": "step_pass_count",
        "warn": "step_warn_count",
        "fail": "step_fail_count",
        "unavailable": "step_unavailable_count",
        "planned": "step_planned_count",
    }
    warning_count = 0
    owner_action_count = 0
    step_total = 0.0
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise CloudBundleError(
                f"adoption result step {index} must be an object"
            )
        step_id = step.get("id")
        if not isinstance(step_id, str) or (
            step_id not in BUILTIN_QUALIFICATION_STEP_IDS
            and not _PROVIDER_STEP_EXTENSION_PATTERN.match(step_id)
        ):
            raise CloudBundleError(
                f"adoption result step {index} id must be a built-in "
                "qualification step or a namespaced provider extension"
            )
        status = step.get("status")
        if status not in ADOPTION_STEP_STATUSES:
            raise CloudBundleError(
                f"adoption result step {index} has unsupported status {status!r}"
            )
        status_counts[status_to_metric[status]] += 1
        for field in ("warning_count", "owner_action_count"):
            value = step.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CloudBundleError(
                    f"adoption result step {index} field {field!r} must be "
                    "a non-negative integer"
                )
        if status == "pass" and int(step.get("owner_action_count") or 0) > 0:
            raise CloudBundleError(
                f"adoption result step {index} reports status 'pass' with a "
                "nonzero owner_action_count"
            )
        warning_count += int(step.get("warning_count") or 0)
        owner_action_count += int(step.get("owner_action_count") or 0)
        elapsed_step = step.get("elapsed_seconds")
        if (
            elapsed_step is None
            or isinstance(elapsed_step, bool)
            or not isinstance(elapsed_step, int | float)
            or not math.isfinite(elapsed_step)
            or elapsed_step < 0
        ):
            raise CloudBundleError(
                f"adoption result step {index} elapsed_seconds must be "
                "finite and non-negative"
            )
        step_total += float(elapsed_step)

    elapsed = result.get("elapsed_seconds")
    if (
        elapsed is None
        or isinstance(elapsed, bool)
        or not isinstance(elapsed, int | float)
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise CloudBundleError(
            "adoption result elapsed_seconds must be finite and non-negative"
        )
    if step_total - float(elapsed) > ADOPTION_RESULT_STEP_TOTAL_TOLERANCE_SECONDS:
        raise CloudBundleError(
            "adoption result step elapsed_seconds exceed total elapsed_seconds "
            "beyond tolerance"
        )

    execution_state = str(result.get("execution_state") or "")
    outcome = str(result.get("outcome") or "")
    if execution_state not in ADOPTION_EXECUTION_STATES:
        raise CloudBundleError(
            f"unsupported adoption_run execution_state {execution_state!r}"
        )
    if outcome not in ADOPTION_OUTCOMES:
        raise CloudBundleError(f"unsupported adoption_run outcome {outcome!r}")
    expected_outcome = _expected_outcome(
        execution_state=execution_state,
        fail_count=status_counts["step_fail_count"],
        warn_count=status_counts["step_warn_count"],
        unavailable_count=status_counts["step_unavailable_count"],
        planned_count=status_counts["step_planned_count"],
    )
    if outcome != expected_outcome:
        raise CloudBundleError(
            f"adoption result outcome {outcome!r} disagrees with step statuses "
            f"for {execution_state} run; expected {expected_outcome!r}"
        )
    if outcome == "pass" and owner_action_count > 0:
        raise CloudBundleError(
            "adoption result outcome 'pass' requires zero owner_action_count"
        )
    if execution_state == "executed":
        _validate_executed_timestamp_bounds(result.get("timestamp_utc"))

    provider = str(result.get("provider") or "")
    executor = str(result.get("executor") or "")
    host_class = str(result.get("host_class") or "")
    runtime_class = str(result.get("runtime_class") or "")
    known_identity = all(
        part and part != "unknown"
        for part in (provider, executor, host_class, runtime_class)
    )

    event = {
        "schema": "code_mower.benchmarkEvent.v1",
        "event_id": _adoption_event_id(dict(result)),
        "event_type": ADOPTION_RUN_EVENT_TYPE,
        "created_at": str(result.get("timestamp_utc") or ""),
        "repo_slug": repo_slug,
        "team_id": team_id,
        "install_id": install_id,
        "source": source,
        "provider": provider,
        "lens": "",
        "status": str(result.get("outcome") or "observed"),
        "tool": build_code_mower_tool_provenance(
            source=source,
            version=__version__,
            role="reporter",
        ),
        "metrics": {
            "adoption_run_count": 1,
            "elapsed_seconds": elapsed,
            "step_count": len(steps),
            **status_counts,
            "warning_count": warning_count,
            "owner_action_count": owner_action_count,
        },
        "dimensions": {
            "adoption_run_schema": ADOPTION_RUN_SCHEMA,
            "release_tag": str(result.get("release_tag") or ""),
            "package_identity": str(result.get("package_identity") or ""),
            "normalized_version": str(result.get("normalized_version") or ""),
            "qualification_context": str(result.get("qualification_context") or ""),
            "starting_version": str(result.get("starting_version") or ""),
            "ending_version": str(result.get("ending_version") or ""),
            "provider": provider,
            "executor": executor,
            "host_class": host_class,
            "runtime_class": runtime_class,
            "execution_state": str(result.get("execution_state") or ""),
            "outcome": str(result.get("outcome") or ""),
            "result_timestamp": str(result.get("timestamp_utc") or ""),
            "provenance_coverage": "complete" if known_identity else "partial",
        },
    }
    validate_metadata_payload(event)
    validate_adoption_run_payload(event)
    return event


def adoption_event_from_result_dict(
    value: Mapping[str, Any],
    event_type: str,
) -> dict[str, Any] | None:
    """Convert an adoptionResult file entry when loading adoption_run events."""

    if event_type != ADOPTION_RUN_EVENT_TYPE:
        return None
    if value.get("schema") != ADOPTION_RESULT_SCHEMA:
        return None
    return adoption_result_to_event(value)
