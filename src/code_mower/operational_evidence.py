"""Read-only, local operational acceptance evidence; never grants authority.

A record contains explicit observations supplied by maintained adapters or an
operator. Validation checks their binding, coverage and freshness, not the truth
of a remote claim. The report is not an audit verdict, provider lifecycle state
machine, cloud event, or a replacement for live verification.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

SCHEMA = "code_mower.operationalEvidence.v1"
REPORT_SCHEMA = "code_mower.operationalAcceptance.v1"
MAX_BYTES = 65536
MAX_AGE_SECONDS = 300
CHECKS = (
    "implementation",
    "provider_quiescence",
    "reviewed_head",
    "published_package",
    "usage_settlement",
    "ingestion_storage",
    "aggregate_visibility",
)
# Input source assertions are explicit, closed, and separate from policy authority.
SOURCES = {
    "implementation": {"work_order", "local_delivery"},
    "provider": {"provider_read", "local_supervisor"},
    "review": {"code_mower_audit"},
    "package": {"published_package_inspection"},
    "usage": {"provider_usage", "provider_billing"},
    "ingestion": {"cloud_receipt"},
    "aggregate": {"authenticated_view"},
}
FIELDS = {
    "implementation": {"state", "head"},
    "provider": {"state", "cancellation"},
    "review": {"head", "verdict", "eligible", "ci", "gate"},
    "package": {"head", "release_commit", "artifact_sha256", "contains_head", "published"},
    "usage": {"authorized_acu_cap", "observed_acu", "settled_acu", "settled_usd"},
    "ingestion": {"manifest_sha256", "stored", "accepted_events", "reports"},
    "aggregate": {"manifest_sha256", "state", "visible"},
}
COMMON = {"source", "observed_at", "coverage", "binding"}
ENUMS = {
    ("implementation", "state"): {
        "active",
        "complete",
        "failed",
        "user_cancelled_before_delivery",
        "unknown",
    },
    ("provider", "state"): {"active", "exited", "suspended", "unknown"},
    ("provider", "cancellation"): {"not_requested", "requested", "accepted", "unknown"},
    ("review", "verdict"): {"pass", "blocked", "unknown"},
    ("review", "ci"): {"pass", "blocked", "unknown"},
    ("review", "gate"): {"pass", "blocked", "unknown"},
    ("aggregate", "state"): {"fresh", "stale", "failed", "unknown"},
}
BOOLS = {"eligible", "contains_head", "published", "stored", "visible"}
AMOUNTS = {"authorized_acu_cap", "observed_acu", "settled_acu", "settled_usd"}
COUNTS = {"accepted_events", "reports"}
MESSAGES = {
    "evidence_missing": "Record the missing observation; unavailable evidence is not a pass.",
    "partial_coverage": "Obtain complete source coverage before acceptance.",
    "observation_stale": "Refresh this source observation under the same private binding.",
    "implementation_complete": "Implementation completion is recorded independently of provider exit and review.",
    "implementation_active": "The implementation is still active.",
    "implementation_unknown": "Obtain the bound implementation outcome; none is established.",
    "implementation_failed": "The delivered implementation failed its recorded outcome.",
    "user_cancelled_before_delivery": "The owner cancelled before delivery; no completed repair outcome is asserted.",
    "provider_quiescent": "A recent provider/supervisor observation records an exited or suspended writer.",
    "provider_active": "The writer remains active; do not start a replacement writer.",
    "provider_exit_unknown": "Observe the provider or supervisor; cancellation acknowledgement is not exit.",
    "reviewed_head": "Eligible independent review, CI and the authoritative gate pass at the bound head.",
    "review_incomplete": "Obtain eligible exact-head review, CI and the authoritative gate verdict.",
    "published_package": "Published-package inspection records the reviewed change in the bound release artifact.",
    "package_unverified": "Verify the published artifact; merged-on-main is not package inclusion evidence.",
    "usage_settled": "Provider billing records settled usage; unavailable currencies remain unrecorded.",
    "usage_unsettled": "Usage settlement is unavailable; observed usage and authorized caps are not settled cost.",
    "storage_confirmed": "The receipt confirms stored metadata; aggregate visibility is checked separately.",
    "storage_unconfirmed": "Reconcile the existing receipt before any retry; do not duplicate an uncertain upload.",
    "aggregate_visible": "A recent authenticated view confirms fresh visibility for the accepted manifest.",
    "aggregate_not_visible": "Inspect refresh/visibility for the accepted manifest; do not resubmit the bundle.",
}


class EvidenceError(ValueError):
    """Only closed diagnostics cross the input boundary."""


def _invalid() -> None:
    raise EvidenceError("operational_evidence_invalid")


def _closed(value: Any, keys: set[str]) -> dict:
    if type(value) is not dict or set(value) != keys:
        _invalid()
    return value


def _digest(value: Any, length: int) -> None:
    if type(value) is not str or re.fullmatch(r"[a-f0-9]{" + str(length) + "}", value) is None:
        _invalid()


def _timestamp(value: Any) -> datetime:
    if type(value) is not str or len(value) > 40:
        _invalid()
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            _invalid()
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        _invalid()
    raise AssertionError("unreachable")


def validate_record(record: Any, *, now: datetime | None = None) -> dict:
    """Validate a closed local record. No source data or identifiers are repaired."""
    now = now or datetime.now(timezone.utc)
    _closed(record, {"schema", "binding", "head", "release_commit", "observations"})
    if record["schema"] != SCHEMA:
        _invalid()
    _digest(record["binding"], 64)
    _digest(record["head"], 40)
    if record["release_commit"] is not None:
        _digest(record["release_commit"], 40)
    observations = record["observations"]
    if type(observations) is not dict or observations.keys() - FIELDS.keys():
        _invalid()
    for name, observation in observations.items():
        _closed(observation, COMMON | FIELDS[name])
        if (
            type(observation["source"]) is not str
            or observation["source"] not in SOURCES[name]
            or type(observation["coverage"]) is not str
            or observation["coverage"] not in {"complete", "partial", "unavailable"}
            or observation["binding"] != record["binding"]
        ):
            _invalid()
        observed = _timestamp(observation["observed_at"])
        if (observed - now).total_seconds() > 0:
            _invalid()
        for field in FIELDS[name]:
            value = observation[field]
            enum = ENUMS.get((name, field))
            if enum is not None:
                if type(value) is not str or value not in enum:
                    _invalid()
            elif field in BOOLS:
                if type(value) is not bool:
                    _invalid()
            elif field in COUNTS:
                if type(value) is not int or not 0 <= value <= 1000000:
                    _invalid()
            elif field in AMOUNTS:
                if value is not None and (
                    type(value) not in {int, float}
                    or not 0 <= value <= 1e9
                    or not math.isfinite(value)
                ):
                    _invalid()
            elif field in {"artifact_sha256", "manifest_sha256"}:
                _digest(value, 64)
            elif field == "head":
                _digest(value, 40)
                if value != record["head"]:
                    _invalid()
            elif field == "release_commit":
                _digest(value, 40)
                if value != record["release_commit"]:
                    _invalid()
    aggregate = observations.get("aggregate")
    ingestion = observations.get("ingestion")
    if aggregate and ingestion and aggregate["manifest_sha256"] != ingestion["manifest_sha256"]:
        _invalid()
    usage = observations.get("usage")
    if usage and (usage["settled_acu"] is not None or usage["settled_usd"] is not None):
        if usage["source"] != "provider_billing" or usage["coverage"] != "complete":
            _invalid()
    return record


def _pairs(items: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def read_record(path: Path) -> dict:
    """Bound the read itself, reject non-regular inputs, and hide input diagnostics."""
    try:
        if path.is_symlink():
            _invalid()
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                _invalid()
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            _invalid()
        record = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=lambda _value: _invalid()
        )
        return validate_record(record)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise EvidenceError("operational_evidence_invalid") from None


def acceptance_report(record: dict, *, now: datetime | None = None) -> dict:
    """Evaluate supplied observations; never mutate, dispatch, retry or upload."""
    now = now or datetime.now(timezone.utc)
    validate_record(record, now=now)
    observations = record["observations"]
    checks = []

    def add(
        check: str,
        name: str,
        passed: bool,
        yes: str,
        no: str,
        *,
        live: bool = False,
        cancelled: bool = False,
    ) -> None:
        obs = observations.get(name)
        status, reason = ("pass", yes) if passed else ("warn", no)
        if not obs:
            status, reason = "warn", "evidence_missing"
        elif obs["coverage"] != "complete":
            status, reason = "warn", "partial_coverage"
        elif live and (now - _timestamp(obs["observed_at"])).total_seconds() > MAX_AGE_SECONDS:
            status, reason = "warn", "observation_stale"
        elif cancelled:
            status, reason = "skip", "user_cancelled_before_delivery"
        checks.append(
            {
                "id": check,
                "status": status,
                "reason": reason,
                "message": MESSAGES[reason],
                "source": obs["source"] if obs else None,
                "observed_at": obs["observed_at"] if obs else None,
                "coverage": obs["coverage"] if obs else "unavailable",
            }
        )

    implementation = observations.get("implementation", {})
    state = implementation.get("state", "unknown")
    add(
        "implementation",
        "implementation",
        state == "complete",
        "implementation_complete",
        "implementation_failed"
        if state == "failed"
        else "implementation_active"
        if state == "active"
        else "implementation_unknown",
        cancelled=state == "user_cancelled_before_delivery",
    )
    provider = observations.get("provider", {})
    state = provider.get("state", "unknown")
    add(
        "provider_quiescence",
        "provider",
        state in {"exited", "suspended"},
        "provider_quiescent",
        "provider_active" if state == "active" else "provider_exit_unknown",
        live=True,
    )
    review = observations.get("review", {})
    reviewed = review.get("eligible") is True and all(
        review.get(key) == "pass" for key in ("verdict", "ci", "gate")
    )
    add("reviewed_head", "review", reviewed, "reviewed_head", "review_incomplete")
    package = observations.get("package", {})
    add(
        "published_package",
        "package",
        package.get("published") is True and package.get("contains_head") is True,
        "published_package",
        "package_unverified",
    )
    usage = observations.get("usage", {})
    add(
        "usage_settlement",
        "usage",
        usage.get("settled_acu") is not None or usage.get("settled_usd") is not None,
        "usage_settled",
        "usage_unsettled",
    )
    ingestion = observations.get("ingestion", {})
    stored = (
        ingestion.get("stored") is True
        and ingestion.get("accepted_events", 0) > 0
        and ingestion.get("reports") == 0
        and ingestion.get("coverage") == "complete"
    )
    add("ingestion_storage", "ingestion", stored, "storage_confirmed", "storage_unconfirmed")
    aggregate = observations.get("aggregate", {})
    visible = stored and aggregate.get("state") == "fresh" and aggregate.get("visible") is True
    add(
        "aggregate_visibility",
        "aggregate",
        visible,
        "aggregate_visible",
        "aggregate_not_visible",
        live=True,
    )
    return {
        "schema": REPORT_SCHEMA,
        "mode": "read_only",
        "authority": "none",
        "checks": checks,
        "usage": {key: usage.get(key) for key in sorted(AMOUNTS)},
    }


def render_report(report: dict) -> str:
    lines = ["Operational evidence (recorded observations; no policy authority)"]
    for check in report["checks"]:
        lines.append(f"{check['status'].upper()} {check['id']}: {check['message']}")
    for key, value in report["usage"].items():
        lines.append(f"{key}: {'unavailable' if value is None else value}")
    return "\n".join(lines) + "\n"


def report_file(path: Path, *, json_output: bool = False, required: list[str] | None = None) -> int:
    try:
        report = acceptance_report(read_record(path))
    except EvidenceError:
        error = {
            "schema": REPORT_SCHEMA,
            "mode": "read_only",
            "authority": "none",
            "error": "operational_evidence_invalid",
        }
        print(json.dumps(error) if json_output else error["error"])
        return 2
    print(
        json.dumps(report, indent=2, sort_keys=True) if json_output else render_report(report),
        end="\n" if json_output else "",
    )
    requested = set(required or [])
    return int(
        any(check["id"] in requested and check["status"] != "pass" for check in report["checks"])
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", type=Path, required=True, help="Closed local evidence record")
    parser.add_argument(
        "--require",
        choices=CHECKS,
        action="append",
        default=[],
        help="Require this observation to pass (repeatable); unknown settlement need not block release",
    )
    parser.add_argument("--json", action="store_true")
