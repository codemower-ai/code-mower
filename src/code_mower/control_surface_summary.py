"""Closed metadata-only telemetry for control-surface session lifecycles."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .providers import build_code_mower_tool_provenance

EVENT_TYPE = "control_surface_session_summary"
SUMMARY_SCHEMA = "code_mower.controlSurfaceSessionSummary.v1"
CAPABILITY_VERSION = 1
CAPABILITY_SCHEMA = "code_mower.controlSurfaceSessionSummaryCapability.v1"
SOURCE = "code-mower slack lifecycle"
PRIVACY_CLASSIFICATION = "metadata_only"

ROOT_FIELDS = frozenset(
    {
        "schema",
        "event_type",
        "event_id",
        "created_at",
        "repo_slug",
        "team_id",
        "install_id",
        "source",
        "provider",
        "lens",
        "status",
        "metrics",
        "dimensions",
        "tool",
    }
)
DIMENSION_FIELDS = frozenset(
    {
        "summary_schema",
        "capability_version",
        "control_surface",
        "session",
        "privacy_classification",
        "state",
        "lifecycle_reason",
        "outcome",
        "owner_action",
        "pr_number",
        "head_sha",
        "pr_state",
    }
)
REQUIRED_DIMENSION_FIELDS = frozenset(
    {
        "summary_schema",
        "capability_version",
        "control_surface",
        "session",
        "privacy_classification",
        "state",
        "lifecycle_reason",
        "outcome",
        "owner_action",
    }
)
METRIC_FIELDS = frozenset(
    {
        "summary_count",
        "dispatch_count",
        "message_count",
        "cancel_count",
        "collect_count",
        "elapsed_seconds",
        "usage_acu",
    }
)
REQUIRED_METRIC_FIELDS = frozenset(
    {
        "summary_count",
        "dispatch_count",
        "message_count",
        "cancel_count",
        "collect_count",
    }
)
STATES = frozenset(
    {
        "archived",
        "complete",
        "failed",
        "pending",
        "running",
        "suspended",
        "terminated",
        "uncertain",
        "waiting_for_approval",
        "waiting_for_user",
    }
)
OUTCOME_BY_STATE = {
    "complete": "succeeded",
    "failed": "failed",
    "terminated": "cancelled",
}
LIFECYCLE_POLICY = {
    "archived": {"none": "none", "result_not_ready": "inspect_provider"},
    "complete": {"none": "none", "result_unavailable": "inspect_provider"},
    "failed": {
        "session_failed": "inspect_failure",
        "result_not_ready": "inspect_provider",
    },
    "pending": {"none": "none", "result_not_ready": "inspect_provider"},
    "running": {"none": "none", "result_not_ready": "inspect_provider"},
    "suspended": {
        "session_suspended": "inspect_failure",
        "result_not_ready": "inspect_provider",
    },
    "terminated": {"none": "none", "result_not_ready": "inspect_provider"},
    "uncertain": {
        "provider_unavailable": "inspect_provider",
        "reconcile_dispatch": "inspect_provider",
        "inspect_provider_then_acknowledge": "inspect_provider",
    },
    "waiting_for_approval": {
        "approval_required": "respond_to_approval",
        "result_not_ready": "inspect_provider",
    },
    "waiting_for_user": {
        "user_input_required": "answer_question",
        "result_not_ready": "inspect_provider",
    },
}
TOOL_FIELDS = frozenset(
    {
        "schema",
        "role",
        "tool_name",
        "tool_version",
        "provider",
        "model",
        "model_version_raw",
        "model_source",
        "version_source",
        "integration",
        "lens",
        "runtime_environment",
        "prompt_pack_version",
        "source",
    }
)
TOOL_CONSTANTS = {
    "schema": "code_mower.toolProvenance.v1",
    "role": "reporter",
    "tool_name": "code-mower",
    "provider": "code-mower",
    "model": "",
    "model_version_raw": "",
    "model_source": "not_applicable",
    "version_source": "package_version",
    "integration": SOURCE,
    "lens": "",
    "prompt_pack_version": "",
    "source": SOURCE,
}
TOOL_RUNTIME_ENVIRONMENTS = frozenset({"local", "ci", "github-actions"})
PROVIDERS = frozenset(
    {
        "antigravity",
        "claude",
        "codex",
        "cursor",
        "cursor-bugbot",
        "devin",
        "gitar",
        "greptile",
        "grok-bot",
        "muse",
        "qodo",
    }
)

_CUSTOM_PROVIDER = re.compile(r"custom:[a-z][a-z0-9-]{0,63}")
_EVENT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_REPO_SLUG = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_SESSION = re.compile(r"[0-9a-f]{32}")
_HEAD_SHA = re.compile(r"[0-9a-f]{40}")
_PR_NUMBER = re.compile(r"[1-9][0-9]{0,9}")
_TOOL_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")
_CAPABILITY_FIELDS = frozenset(
    {
        "schema",
        "summary_schema",
        "capability_version",
        "fixture_manifest_sha256",
        "accepting",
    }
)


def _error(detail: str) -> Exception:
    # Keep this module importable without initializing the cloud_client package;
    # cloud_client.events imports this validator while that package is loading.
    from .cloud_client.errors import CloudBundleError

    return CloudBundleError(f"invalid control-surface session summary: {detail}")


def _closed(value: Mapping[str, Any], expected: frozenset[str], name: str) -> None:
    keys = set(value)
    if keys != expected:
        raise _error(f"{name} fields do not match the closed v1 contract")


def _closed_with_optional(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    required: frozenset[str],
    name: str,
) -> None:
    keys = set(value)
    if not required <= keys or not keys <= allowed:
        raise _error(f"{name} fields do not match the closed v1 contract")


def _utc_timestamp(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise _error("created_at must be a UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error("created_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise _error("created_at must be a UTC timestamp")
    return parsed


def _bounded_count(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
        raise _error(f"metrics.{field} must be an integer from 0 through 10000")


def _optional_metric(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _error(f"metrics.{field} must be a finite non-negative number")
    if not math.isfinite(value) or value < 0:
        raise _error(f"metrics.{field} must be a finite non-negative number")


def _validate_tool(value: object) -> None:
    """Validate the closed, metadata-only Code Mower provenance object."""

    if not isinstance(value, Mapping):
        raise _error("tool must be an object")
    _closed(value, TOOL_FIELDS, "tool")
    if any(not isinstance(item, str) for item in value.values()):
        raise _error("tool fields must be strings")
    for field, expected in TOOL_CONSTANTS.items():
        if value.get(field) != expected:
            raise _error(f"tool.{field} does not match the v1 producer contract")
    if value.get("runtime_environment") not in TOOL_RUNTIME_ENVIRONMENTS:
        raise _error("tool.runtime_environment is not a supported coarse category")
    if _TOOL_VERSION.fullmatch(value.get("tool_version", "")) is None:
        raise _error("tool.tool_version must be a bounded package version")


def validate_control_surface_summary(value: Mapping[str, Any]) -> None:
    """Validate the new event's complete closed shape and cross-field invariants."""

    from .cloud_client.bundle import validate_metadata_payload

    if not isinstance(value, Mapping):
        raise _error("event must be an object")
    validate_metadata_payload(value)
    _closed(value, ROOT_FIELDS, "event")
    if value.get("schema") != "code_mower.benchmarkEvent.v1":
        raise _error("unsupported event envelope schema")
    if value.get("event_type") != EVENT_TYPE:
        raise _error("unsupported event type")
    if not isinstance(value.get("event_id"), str) or _EVENT_ID.fullmatch(value["event_id"]) is None:
        raise _error("event_id must be a lowercase UUID")
    _utc_timestamp(value.get("created_at"))
    if not isinstance(value.get("repo_slug"), str) or _REPO_SLUG.fullmatch(value["repo_slug"]) is None:
        raise _error("repo_slug must be canonical OWNER/REPO metadata")
    if value.get("team_id") != "" or value.get("install_id") != "":
        raise _error("tenant identity must come from authenticated ingest")
    if value.get("source") != SOURCE or value.get("lens") != "":
        raise _error("producer provenance does not match the v1 contract")
    _validate_tool(value.get("tool"))
    provider = value.get("provider")
    if not isinstance(provider, str) or (
        provider not in PROVIDERS and _CUSTOM_PROVIDER.fullmatch(provider) is None
    ):
        raise _error("provider must be a bounded categorical provider name")

    dimensions = value.get("dimensions")
    metrics = value.get("metrics")
    if not isinstance(dimensions, Mapping) or not isinstance(metrics, Mapping):
        raise _error("dimensions and metrics must be objects")
    _closed_with_optional(
        dimensions, DIMENSION_FIELDS, REQUIRED_DIMENSION_FIELDS, "dimension"
    )
    _closed_with_optional(metrics, METRIC_FIELDS, REQUIRED_METRIC_FIELDS, "metric")

    if dimensions.get("summary_schema") != SUMMARY_SCHEMA:
        raise _error("unsupported summary schema")
    if type(dimensions.get("capability_version")) is not int or dimensions["capability_version"] != CAPABILITY_VERSION:
        raise _error("unsupported capability version")
    if dimensions.get("control_surface") != "slack":
        raise _error("unsupported control surface")
    if not isinstance(dimensions.get("session"), str) or _SESSION.fullmatch(dimensions["session"]) is None:
        raise _error("session must be a Code Mower opaque correlation key")
    if dimensions.get("privacy_classification") != PRIVACY_CLASSIFICATION:
        raise _error("privacy classification must be metadata_only")

    state = dimensions.get("state")
    if state not in STATES:
        raise _error("unsupported lifecycle state")
    if value.get("status") != state:
        raise _error("envelope status must equal the lifecycle state")
    expected_outcome = OUTCOME_BY_STATE.get(state, "unknown")
    if dimensions.get("outcome") != expected_outcome:
        raise _error("outcome is inconsistent with lifecycle state")
    reason = dimensions.get("lifecycle_reason")
    expected_action = LIFECYCLE_POLICY[state].get(reason)
    if expected_action is None:
        raise _error("lifecycle_reason is inconsistent with lifecycle state")
    if dimensions.get("owner_action") != expected_action:
        raise _error("owner_action is inconsistent with lifecycle state and reason")

    pr_fields = {"pr_number", "head_sha", "pr_state"} & set(dimensions)
    if pr_fields and "pr_number" not in pr_fields:
        raise _error("PR metadata requires pr_number")
    if "pr_number" in dimensions and (
        not isinstance(dimensions["pr_number"], str)
        or _PR_NUMBER.fullmatch(dimensions["pr_number"]) is None
    ):
        raise _error("pr_number must be a canonical positive integer string")
    if "head_sha" in dimensions and (
        not isinstance(dimensions["head_sha"], str)
        or _HEAD_SHA.fullmatch(dimensions["head_sha"]) is None
    ):
        raise _error("head_sha must be a lowercase full commit SHA")
    if "pr_state" in dimensions and dimensions["pr_state"] not in {
        "open",
        "merged",
        "closed_unmerged",
    }:
        raise _error("unsupported pr_state")

    if metrics.get("summary_count") != 1 or type(metrics.get("summary_count")) is not int:
        raise _error("metrics.summary_count must equal 1")
    for field in ("dispatch_count", "message_count", "cancel_count", "collect_count"):
        _bounded_count(metrics.get(field), field)
    for field in ("elapsed_seconds", "usage_acu"):
        if field in metrics:
            _optional_metric(metrics[field], field)
    if "usage_acu" in metrics and provider != "devin":
        raise _error("usage_acu is available only for devin observations")


@lru_cache(maxsize=1)
def fixture_manifest_digest() -> str:
    """Return the exact installed fixture-manifest identity advertised by hosted."""

    raw = Path(__file__).with_name(
        "control_surface_session_summary.fixture-manifest.json"
    ).read_bytes()
    return hashlib.sha256(raw).hexdigest()


def capability_accepts_summary(value: object) -> bool:
    """Fail closed unless hosted advertises this exact installed contract."""

    return bool(
        isinstance(value, Mapping)
        and set(value) == _CAPABILITY_FIELDS
        and value.get("schema") == CAPABILITY_SCHEMA
        and value.get("summary_schema") == SUMMARY_SCHEMA
        and type(value.get("capability_version")) is int
        and value["capability_version"] == CAPABILITY_VERSION
        and value.get("fixture_manifest_sha256") == fixture_manifest_digest()
        and value.get("accepting") is True
    )


def capability_from_health(value: object) -> dict[str, Any] | None:
    """Read only the exact capability from the hosted health response."""

    if not isinstance(value, Mapping):
        return None
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return None
    candidate = capabilities.get(EVENT_TYPE)
    return dict(candidate) if capability_accepts_summary(candidate) else None


def opaque_session(logical_session: str) -> str:
    """Derive a stable unlinkable cloud key, never reuse a Slack/provider id."""

    if not isinstance(logical_session, str) or not logical_session or len(logical_session) > 128:
        raise _error("logical session must be bounded local metadata")
    seed = (SUMMARY_SCHEMA + "\0" + logical_session).encode("utf-8")
    return hashlib.sha256(seed).hexdigest()[:32]


def _stamp(value: dt.datetime) -> str:
    if not isinstance(value, dt.datetime) or value.tzinfo is None:
        raise _error("observed_at must include a UTC offset")
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _owner_action(lifecycle: Mapping[str, Any]) -> str:
    state, reason = lifecycle["state"], lifecycle["reason"]
    try:
        return LIFECYCLE_POLICY[state][reason]
    except KeyError:
        raise _error("lifecycle state and reason are not a supported producer pair") from None


def build_control_surface_summary(
    *,
    logical_session: str,
    repo_slug: str,
    provider: str,
    lifecycle: Mapping[str, Any],
    observed_at: dt.datetime,
    pr_number: int | None = None,
    head_sha: str | None = None,
    pr_state: str | None = None,
    elapsed_seconds: float | None = None,
    usage_acu: float | None = None,
) -> dict[str, Any]:
    """Build one retry-stable allowlisted summary from a public lifecycle."""

    from .remote_session import RemoteError, public_projection

    try:
        projected = public_projection(dict(lifecycle))
    except (RemoteError, TypeError, ValueError):
        raise _error("lifecycle must be the closed public projection") from None
    created_at = _stamp(observed_at)
    state = projected["state"]
    dimensions: dict[str, Any] = {
        "summary_schema": SUMMARY_SCHEMA,
        "capability_version": CAPABILITY_VERSION,
        "control_surface": "slack",
        "session": opaque_session(logical_session),
        "privacy_classification": PRIVACY_CLASSIFICATION,
        "state": state,
        "lifecycle_reason": projected["reason"],
        "outcome": OUTCOME_BY_STATE.get(state, "unknown"),
        "owner_action": _owner_action(projected),
    }
    if pr_number is not None:
        dimensions["pr_number"] = str(pr_number)
    if head_sha is not None:
        dimensions["head_sha"] = head_sha
    if pr_state is not None:
        dimensions["pr_state"] = pr_state
    metrics: dict[str, Any] = {
        "summary_count": 1,
        **{f"{key}_count": projected["counts"][key] for key in ("dispatch", "message", "cancel", "collect")},
    }
    if elapsed_seconds is not None:
        metrics["elapsed_seconds"] = elapsed_seconds
    if usage_acu is not None:
        metrics["usage_acu"] = usage_acu
    event_seed = json.dumps(
        [repo_slug, provider, created_at, dimensions, metrics],
        sort_keys=True,
        separators=(",", ":"),
    )
    event = {
        "schema": "code_mower.benchmarkEvent.v1",
        "event_type": EVENT_TYPE,
        "event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, event_seed)),
        "created_at": created_at,
        "repo_slug": repo_slug,
        "team_id": "",
        "install_id": "",
        "source": SOURCE,
        "provider": provider,
        "lens": "",
        "status": state,
        "metrics": metrics,
        "dimensions": dimensions,
        "tool": build_code_mower_tool_provenance(
            source=SOURCE,
            version=__version__,
            role="reporter",
        ),
    }
    validate_control_surface_summary(event)
    return event


def gated_control_surface_summary(
    capability: object, **summary: Any
) -> dict[str, Any] | None:
    """Return no cloud event until exact hosted acceptance is advertised."""

    if not capability_accepts_summary(capability):
        return None
    return build_control_surface_summary(**summary)


def _transition_identity(value: Mapping[str, Any]) -> tuple[Any, ...]:
    dimensions = value["dimensions"]
    metrics = value["metrics"]
    return (
        value["repo_slug"],
        value["provider"],
        *(
            dimensions.get(key)
            for key in (
                "session",
                "state",
                "lifecycle_reason",
                "outcome",
                "owner_action",
                "pr_number",
                "head_sha",
                "pr_state",
            )
        ),
        *(
            metrics.get(f"{key}_count")
            for key in ("dispatch", "message", "cancel", "collect")
        ),
        (
            metrics.get("elapsed_seconds")
            if dimensions["state"] in {"complete", "failed", "terminated", "archived"}
            else None
        ),
        (
            metrics.get("usage_acu")
            if dimensions["state"] in {"complete", "failed", "terminated", "archived"}
            else None
        ),
    )


def gated_control_surface_transition(
    capability: object,
    previous: Mapping[str, Any] | None = None,
    **summary: Any,
) -> dict[str, Any] | None:
    """Emit only a meaningful lifecycle/count/PR change after capability acceptance."""

    current = gated_control_surface_summary(capability, **summary)
    if current is None:
        return None
    if previous is None:
        return current
    try:
        validate_control_surface_summary(previous)
    except ValueError:
        return current
    return None if _transition_identity(previous) == _transition_identity(current) else current


def slack_board_run(
    *,
    logical_session: str,
    binding: Any,
    provider: str,
    observed_at: dt.datetime,
    lifecycle: Mapping[str, Any],
) -> Any:
    """Adapt a Slack-requested lifecycle into the existing local Board seam."""

    from .board_local_observation import run_from_remote_lifecycle

    return run_from_remote_lifecycle(
        id="slack-" + opaque_session(logical_session)[:20],
        binding=binding,
        provider=provider,
        role="builder",
        observed_at=observed_at,
        lifecycle=lifecycle,
    )
