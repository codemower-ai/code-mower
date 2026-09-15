"""Closed, provider-neutral local observation contract for Code Mower Board.

This module defines a read model, not workflow authority.  It accepts bounded
lifecycle facts from later adapters and never dispatches, renews a lease, or
changes provider state.  Records stay local: this contract does not add or
widen any Board/cloud event field.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from .remote_session import RemoteError, public_projection


SCHEMA = "code_mower.boardObservation.v1"
MAX_BYTES = 262_144
MAX_DEPTH = 16
MAX_SOURCES = 16
MAX_RUNS = 32

STAGES = frozenset(
    {
        "unknown",
        "queued",
        "building",
        "in_review",
        "changes_requested",
        "ready_for_human_review",
        "ready_to_merge",
        "merged",
    }
)
ACTORS = frozenset(
    {"none", "owner", "orchestrator", "builder", "reviewer", "automation", "maintainer"}
)

# Lower rank wins.  This table is the only source of primary-action precedence.
REASON_ROUTES: Mapping[str, tuple[int, str, str]] = {
    "approval_required": (10, "owner", "respond_to_approval"),
    "user_input_required": (20, "owner", "answer_question"),
    "source_unavailable": (30, "orchestrator", "restore_source"),
    "identity_unlinked": (40, "orchestrator", "connect_session"),
    "stale_observation": (50, "orchestrator", "refresh_evidence"),
    "provider_failed": (60, "orchestrator", "inspect_failure"),
    "provider_suspended": (70, "orchestrator", "inspect_provider"),
    "cancelled": (80, "orchestrator", "inspect_cancellation"),
    "changes_requested": (90, "builder", "address_findings"),
    "update_required": (100, "builder", "update_branch"),
    "ci_failed": (110, "builder", "fix_checks"),
    "gate_failed": (120, "builder", "resolve_gate"),
    "review_stale": (130, "orchestrator", "request_review"),
    "review_requested": (140, "reviewer", "review_current_head"),
    "review_in_progress": (150, "reviewer", "finish_review"),
    "ci_pending": (160, "automation", "wait_for_checks"),
    "gate_pending": (170, "automation", "wait_for_gate"),
    "human_review_required": (180, "owner", "review_change"),
    "ready_to_merge": (190, "maintainer", "merge"),
}
REASONS = frozenset(REASON_ROUTES)
ACTIONS = frozenset(route[2] for route in REASON_ROUTES.values()) | {"none"}

_SECRET = re.compile(
    r"(?:ghp_|github_pat_|xox[baprs]-|sk-[A-Za-z0-9]|bearer\s+|eyJ[A-Za-z0-9_-]{8,}\.)", re.I
)


class BoardObservationError(ValueError):
    """Fixed diagnostics intentionally omit observed values and local paths."""


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("board_observation.schema.json").read_text())


def _check(value: Any, rule: Mapping[str, Any], *, depth: int = 0) -> None:
    """Validate the deliberately small JSON-Schema subset used by this contract."""
    if depth > MAX_DEPTH:
        raise BoardObservationError("invalid_contract")
    if value is None and isinstance(rule.get("type"), list) and "null" in rule["type"]:
        return
    if "$ref" in rule:
        name = str(rule["$ref"]).rsplit("/", 1)[-1]
        _check(value, schema()["$defs"][name], depth=depth + 1)
        return
    if "const" in rule and (type(value) is not type(rule["const"]) or value != rule["const"]):
        raise BoardObservationError("invalid_contract")
    if "enum" in rule and value not in rule["enum"]:
        raise BoardObservationError("invalid_contract")
    allowed_types = rule.get("type")
    if isinstance(allowed_types, str):
        allowed_types = [allowed_types]
    if allowed_types:
        expected = {
            "object": dict,
            "array": list,
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "null": type(None),
        }
        if not any(
            type(value) is expected[kind]
            if isinstance(expected[kind], type)
            else type(value) in expected[kind]
            for kind in allowed_types
        ):
            raise BoardObservationError("invalid_contract")
    if type(value) is dict:
        required = set(rule.get("required", ()))
        if not required <= set(value):
            raise BoardObservationError("invalid_contract")
        properties = rule.get("properties", {})
        if rule.get("additionalProperties") is False and set(value) - set(properties):
            raise BoardObservationError("invalid_contract")
        for key, child in value.items():
            child_rule = properties.get(key)
            if child_rule is not None:
                _check(child, child_rule, depth=depth + 1)
    elif type(value) is list:
        if not rule.get("minItems", 0) <= len(value) <= rule.get("maxItems", MAX_RUNS):
            raise BoardObservationError("invalid_contract")
        if rule.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded) != len(set(encoded)):
                raise BoardObservationError("invalid_contract")
        for child in value:
            _check(child, rule["items"], depth=depth + 1)
    elif type(value) is str:
        if not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", MAX_BYTES):
            raise BoardObservationError("invalid_contract")
        if "pattern" in rule and re.fullmatch(rule["pattern"], value) is None:
            raise BoardObservationError("invalid_contract")
    elif type(value) in (int, float):
        if (
            not math.isfinite(value)
            or value < rule.get("minimum", value)
            or value > rule.get("maximum", value)
        ):
            raise BoardObservationError("invalid_contract")


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise BoardObservationError("invalid_timestamp") from None
    if parsed.tzinfo is None:
        raise BoardObservationError("invalid_timestamp")
    return parsed


def _nullable_timestamp(value: str | None) -> datetime | None:
    return None if value is None else _timestamp(value)


def derive_primary(reasons: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Return the deterministic primary route for an already closed reason set."""
    if type(reasons) not in (list, tuple) or any(reason not in REASONS for reason in reasons):
        raise BoardObservationError("invalid_reason")
    if len(reasons) != len(set(reasons)):
        raise BoardObservationError("invalid_reason")
    if not reasons:
        return {"actor": "none", "action": "none"}
    reason = min(reasons, key=lambda item: (REASON_ROUTES[item][0], item))
    _rank, actor, action = REASON_ROUTES[reason]
    return {"actor": actor, "action": action}


def ordered_reasons(reasons: list[str] | tuple[str, ...]) -> list[str]:
    """Canonicalize a set of closed reasons for producer implementations."""
    if type(reasons) not in (list, tuple) or any(reason not in REASONS for reason in reasons):
        raise BoardObservationError("invalid_reason")
    if len(reasons) != len(set(reasons)):
        raise BoardObservationError("invalid_reason")
    return sorted(reasons, key=lambda item: (REASON_ROUTES[item][0], item))


def _validate_source(source: Mapping[str, Any], *, created_at: datetime) -> None:
    checked = _timestamp(source["checked_at"])
    observed = _nullable_timestamp(source["observed_at"])
    event = _nullable_timestamp(source["event_at"])
    heartbeat = _nullable_timestamp(source["heartbeat_at"])
    if checked > created_at or any(
        item is not None and item > checked for item in (observed, event, heartbeat)
    ):
        raise BoardObservationError("invalid_timestamp")
    if event is not None and observed is not None and event > observed:
        raise BoardObservationError("invalid_timestamp")
    if observed is None and any(item is not None for item in (event, heartbeat)):
        raise BoardObservationError("invalid_timestamp")
    if source["freshness"] in {"fresh", "stale"} and observed is None:
        raise BoardObservationError("invalid_freshness")
    if (source["freshness"] == "unavailable") != (source["coverage"] == "unavailable"):
        raise BoardObservationError("invalid_freshness")


def _validate_measurements(measurements: Mapping[str, Any]) -> None:
    for measurement in measurements.values():
        unavailable = measurement["coverage"] == "unavailable"
        if unavailable:
            if measurement != {
                "value": None,
                "coverage": "unavailable",
                "observed": 0,
                "total": None,
            }:
                raise BoardObservationError("invalid_measurement")
            continue
        if measurement["value"] is None or measurement["observed"] < 1:
            raise BoardObservationError("invalid_measurement")
        total = measurement["total"]
        if total is None or total < measurement["observed"]:
            raise BoardObservationError("invalid_measurement")
        if measurement["coverage"] == "complete" and measurement["observed"] != total:
            raise BoardObservationError("invalid_measurement")
        if measurement["coverage"] == "partial" and measurement["observed"] >= total:
            raise BoardObservationError("invalid_measurement")


def _validate_lifecycle(run: Mapping[str, Any]) -> None:
    lifecycle = run["lifecycle"]
    if lifecycle is None:
        return
    try:
        projected = public_projection(dict(lifecycle))
    except RemoteError:
        raise BoardObservationError("invalid_lifecycle") from None
    if projected != lifecycle:
        raise BoardObservationError("invalid_lifecycle")
    allowed_phases = {
        "pending": {"dispatched"},
        "running": {"observed_running", "provider_progress"},
        "waiting_for_user": {"waiting_for_user"},
        "waiting_for_approval": {"waiting_for_approval"},
        "complete": {"implementation_complete"},
        "failed": {"failed"},
        "suspended": {"failed"},
        "terminated": {"cancelled"},
        "archived": {"implementation_complete", "cancelled"},
        "uncertain": {"dispatched"},
    }
    if run["phase"] not in allowed_phases[lifecycle["state"]]:
        raise BoardObservationError("invalid_lifecycle")


def _validate_evidence(work: Mapping[str, Any], sources: Mapping[str, Mapping[str, Any]]) -> None:
    head = work["pull_request"]["head_sha"]
    number = work["pull_request"]["number"]
    if (number is None) != (head is None):
        raise BoardObservationError("identity_mismatch")
    evidence = work["evidence"]
    allowed_states = {
        "lease": {"held", "absent", "expired", "unverifiable", "unknown"},
        "assignment": {"assigned", "unassigned", "unknown"},
        "review_request": {"requested", "not_requested", "unknown"},
        "review": {"not_started", "running", "pass", "blocked", "stale", "unknown"},
        "ci": {"not_started", "pending", "pass", "failed", "unknown"},
        "gate_publisher": {"not_started", "pending", "pass", "failed", "unknown"},
        "gate": {"not_started", "pending", "pass", "failed", "unknown"},
        "merge": {
            "none",
            "draft",
            "open",
            "blocked",
            "ready",
            "merged",
            "closed_unmerged",
            "unknown",
        },
    }
    for name, item in evidence.items():
        source_id = item["source_id"]
        state = item["state"]
        if state not in allowed_states[name]:
            raise BoardObservationError("invalid_evidence")
        if source_id is not None and source_id not in sources:
            raise BoardObservationError("identity_mismatch")
        if state not in {"unknown", "not_started", "none"} and source_id is None:
            raise BoardObservationError("identity_mismatch")
        if name in {"review", "ci", "gate"}:
            evidence_head = item["head_sha"]
            if state in {"unknown", "not_started"} and (
                source_id is not None
                or evidence_head is not None
                or item["coverage"] != "unavailable"
            ):
                raise BoardObservationError("invalid_evidence")
            if state in {"running", "pass", "blocked", "pending", "failed", "stale"}:
                if number is None or evidence_head is None:
                    raise BoardObservationError("identity_mismatch")
            if state != "stale" and evidence_head is not None and evidence_head != head:
                raise BoardObservationError("identity_mismatch")
            if (
                name in {"review", "gate"}
                and state not in {"unknown", "not_started"}
                and item["coverage"] != "full"
            ):
                raise BoardObservationError("invalid_evidence")
    if evidence["review"]["state"] == "running":
        source = sources[evidence["review"]["source_id"]]
        if source["freshness"] != "fresh":
            raise BoardObservationError("stale_live_claim")


def _safe_display(display: Mapping[str, Any]) -> None:
    label = display["session_label"]
    if display["authorized"] is False and label is not None:
        raise BoardObservationError("unauthorized_display")
    if label is None:
        return
    if any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in label):
        raise BoardObservationError("invalid_contract")
    if (
        label.startswith(("/", "~", "\\"))
        or re.match(r"[A-Za-z]:[\\/]", label)
        or _SECRET.search(label)
    ):
        raise BoardObservationError("privacy_violation")


def validate(value: object) -> dict[str, Any]:
    """Validate a decoded observation and return a detached, ASCII-safe copy."""
    _check(value, schema())
    if not isinstance(value, dict):  # defensive; the packaged schema already establishes this
        raise BoardObservationError("invalid_contract")
    created_at = _timestamp(value["created_at"])
    _safe_display(value["display"])
    sources = {source["id"]: source for source in value["sources"]}
    if len(sources) != len(value["sources"]):
        raise BoardObservationError("identity_mismatch")
    for source in sources.values():
        _validate_source(source, created_at=created_at)

    kind, scope, work, unlinked = value["kind"], value["scope"], value["work"], value["unlinked"]
    if kind == "unlinked":
        if (
            scope["session_id"] is not None
            or scope["worktree_id"] is not None
            or work is not None
            or not unlinked
        ):
            raise BoardObservationError("identity_mismatch")
        for run in unlinked:
            if run["source_id"] not in sources:
                raise BoardObservationError("identity_mismatch")
            if _timestamp(run["observed_at"]) > created_at:
                raise BoardObservationError("invalid_timestamp")
        return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))

    if scope["session_id"] is None or scope["worktree_id"] is None or unlinked:
        raise BoardObservationError("identity_mismatch")
    if kind == "no_work":
        if work is not None:
            raise BoardObservationError("identity_mismatch")
        required = {"session", "work_queue", "run_registry"}
        covered = {
            source["kind"]
            for source in sources.values()
            if source["freshness"] == "fresh" and source["coverage"] == "complete"
        }
        if not required <= covered:
            raise BoardObservationError("insufficient_coverage")
        return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))

    if work is None:
        raise BoardObservationError("identity_mismatch")
    reasons = work["reasons"]
    if reasons != ordered_reasons(reasons) or work["primary"] != derive_primary(reasons):
        raise BoardObservationError("invalid_route")
    _validate_measurements(work["measurements"])
    _validate_evidence(work, sources)
    for run in work["runs"]:
        binding = run["binding"]
        if (
            binding["session_id"] != scope["session_id"]
            or binding["work_id"] != work["id"]
            or binding["repository"] != scope["repository"]
            or binding["worktree_id"] != scope["worktree_id"]
            or run["source_id"] not in sources
        ):
            raise BoardObservationError("identity_mismatch")
        source = sources[run["source_id"]]
        if run["phase"] == "assigned" and run["basis"] not in {"configured", "requested"}:
            raise BoardObservationError("invalid_evidence")
        if run["phase"] in {"dispatched", "observed_running"} and run["basis"] != "observed":
            raise BoardObservationError("invalid_evidence")
        if (
            run["phase"] in {"provider_progress", "waiting_for_user", "waiting_for_approval"}
            and run["basis"] != "provider_reported"
        ):
            raise BoardObservationError("invalid_evidence")
        event = _nullable_timestamp(run["event_at"])
        observed = _timestamp(run["observed_at"])
        heartbeat = _nullable_timestamp(run["heartbeat_at"])
        if observed > created_at or any(
            item is not None and item > observed for item in (event, heartbeat)
        ):
            raise BoardObservationError("invalid_timestamp")
        if observed > _timestamp(source["checked_at"]):
            raise BoardObservationError("invalid_timestamp")
        if run["phase"] in {
            "observed_running",
            "provider_progress",
            "waiting_for_user",
            "waiting_for_approval",
        }:
            if source["freshness"] != "fresh" or heartbeat is None:
                raise BoardObservationError("stale_live_claim")
        if run["phase"] == "provider_progress":
            if run["basis"] != "provider_reported" or run["reported_stage"] is None:
                raise BoardObservationError("invalid_lifecycle")
        elif run["reported_stage"] is not None:
            raise BoardObservationError("invalid_lifecycle")
        _validate_lifecycle(run)
    run_ids = [run["id"] for run in work["runs"]]
    if len(run_ids) != len(set(run_ids)):
        raise BoardObservationError("identity_mismatch")
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


def decode(raw: bytes) -> dict[str, Any]:
    """Decode bounded JSON while rejecting duplicate keys and non-finite numbers."""
    if type(raw) is not bytes or len(raw) > MAX_BYTES:
        raise BoardObservationError("invalid_contract")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise BoardObservationError("invalid_contract")
            result[key] = item
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        return validate(value)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise BoardObservationError("invalid_contract") from None
