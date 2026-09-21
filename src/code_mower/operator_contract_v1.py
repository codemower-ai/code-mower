"""Normative semantic checks for the closed Operator v1 contract.

JSON Schema validates each record's closed shape.  These helpers validate the
cross-field, wall-clock, and before/after rules that JSON Schema cannot express
without implementation-specific extensions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


_ACTION_IMMUTABLE_FIELDS = (
    "tenant_id",
    "repository_id",
    "work_id",
    "work_generation",
    "action_id",
    "operation",
    "idempotency_key",
    "request_digest",
    "lease_epoch",
    "fence_token",
    "target_head_sha",
    "created_at",
)


def qualification_semantic_errors(
    record: Mapping[str, Any],
    *,
    max_evidence_age_seconds: int,
    now: int,
) -> tuple[str, ...]:
    """Return fail-closed semantic errors for a qualification record.

    Callers must first validate the record against
    ``operator_policy_v1.schema.json``. ``now`` is an explicit durable-store
    observation time, which keeps replay and tests deterministic.
    """

    errors: list[str] = []
    status = record.get("status")
    decision = record.get("decision")
    reason = record.get("reason")
    evidence = record.get("evidence")

    if status == "qualified":
        if decision != "allowed" or reason != "evidence_current":
            errors.append("qualified evidence must be allowed and current")
        if not isinstance(evidence, Mapping):
            errors.append("qualified evidence must be present")
        else:
            observed_at = evidence.get("observed_at")
            expires_at = evidence.get("expires_at")
            if not isinstance(observed_at, int) or not isinstance(expires_at, int):
                errors.append("qualification evidence timestamps must be integers")
            else:
                if observed_at >= expires_at:
                    errors.append("qualification evidence must expire after observation")
                if observed_at > now:
                    errors.append("qualification evidence cannot be observed in the future")
                if now >= expires_at:
                    errors.append("qualification evidence is expired")
                if now - observed_at > max_evidence_age_seconds:
                    errors.append("qualification evidence exceeds the policy age limit")
    elif status == "pending":
        if decision != "denied":
            errors.append("pending qualification must be denied")
        if reason not in {"evidence_missing", "human_merge_required"}:
            errors.append("pending qualification has a contradictory reason")
        if evidence is not None:
            errors.append("pending qualification cannot claim completed evidence")
    elif status == "failed":
        if decision != "denied":
            errors.append("failed qualification must be denied")
        if reason not in {"evidence_failed", "capability_missing"}:
            errors.append("failed qualification has a contradictory reason")
    elif status == "stale":
        if decision != "denied" or reason != "evidence_stale":
            errors.append("stale qualification must be denied as stale")
        if not isinstance(evidence, Mapping):
            errors.append("stale qualification must retain its evidence")
        else:
            observed_at = evidence.get("observed_at")
            expires_at = evidence.get("expires_at")
            if not isinstance(observed_at, int) or not isinstance(expires_at, int):
                errors.append("qualification evidence timestamps must be integers")
            elif observed_at >= expires_at:
                errors.append("qualification evidence must expire after observation")
            elif observed_at > now:
                errors.append("qualification evidence cannot be observed in the future")
            elif now < expires_at and now - observed_at <= max_evidence_age_seconds:
                errors.append("stale qualification evidence is still current")

    required = record.get("required_capabilities")
    declared = record.get("declared_capabilities")
    if isinstance(required, list) and isinstance(declared, list):
        missing = sorted(set(required) - set(declared))
        if missing and status == "qualified":
            errors.append("qualified provider is missing required capabilities")
    return tuple(errors)


def action_intent_semantic_errors(
    record: Mapping[str, Any],
    *,
    current_work_generation: int,
) -> tuple[str, ...]:
    """Check the durable work-generation binding for an action intent."""

    if record.get("work_generation") != current_work_generation:
        return ("action intent belongs to a stale work generation",)
    return ()


def _records_by_schema(records: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    by_schema: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        schema = record.get("schema")
        if isinstance(schema, str):
            by_schema.setdefault(schema, []).append(record)
    return by_schema


def _action_pair(
    before: dict[str, list[Mapping[str, Any]]],
    after: dict[str, list[Mapping[str, Any]]],
    errors: list[str],
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    schema = "code_mower.operatorActionIntent.v1"
    if len(before.get(schema, [])) != 1 or len(after.get(schema, [])) != 1:
        errors.append("scenario must contain one action intent before and after")
        return None
    old = before[schema][0]
    new = after[schema][0]
    for field in _ACTION_IMMUTABLE_FIELDS:
        if old.get(field) != new.get(field):
            errors.append(f"action intent changed immutable field {field}")
    return old, new


def _work_pair(
    before: dict[str, list[Mapping[str, Any]]],
    after: dict[str, list[Mapping[str, Any]]],
    errors: list[str],
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    schema = "code_mower.operatorWorkItem.v1"
    if len(before.get(schema, [])) != 1 or len(after.get(schema, [])) != 1:
        errors.append("scenario must contain one work item before and after")
        return None
    return before[schema][0], after[schema][0]


def recovery_transition_errors(
    event: str,
    before_records: Sequence[Mapping[str, Any]],
    after_records: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """Validate one executable Operator recovery transition.

    Records must already pass their JSON Schema and semantic checks.  The
    result validates the event's before/after invariant, including counters
    and immutable dispatch identity.
    """

    errors: list[str] = []
    before = _records_by_schema(before_records)
    after = _records_by_schema(after_records)

    if event in {
        "restart_after_dispatch",
        "duplicate_delivery",
        "stale_head",
        "provider_timeout",
        "partial_success",
    }:
        pair = _action_pair(before, after, errors)
        if pair is None:
            return tuple(errors)
        old, new = pair
        old_attempts = old.get("budget", {}).get("attempt_count")
        new_attempts = new.get("budget", {}).get("attempt_count")

        if event == "restart_after_dispatch":
            if old.get("certainty") != "unknown" or new.get("certainty") != "unknown":
                errors.append("restart must preserve an unknown dispatched outcome")
            if new.get("next_action") != "reconcile":
                errors.append("restart must reconcile before another dispatch")
            if old_attempts != new_attempts:
                errors.append("restart cannot reserve another mutation attempt")
        elif event == "duplicate_delivery":
            if old != new:
                errors.append("duplicate delivery must return the recorded intent unchanged")
        elif event == "stale_head":
            if new.get("head_status") != "stale":
                errors.append("stale-head recovery must record the stale target")
            if new.get("state") != "abandoned" or new.get("next_action") != "abandon":
                errors.append("a pre-dispatch stale head must abandon the intent")
            if new_attempts != 0:
                errors.append("a pre-dispatch stale head cannot consume an attempt")
        elif event == "provider_timeout":
            if new.get("certainty") != "unknown" or new.get("next_action") != "reconcile":
                errors.append("provider timeout must become unknown and reconcile")
            if old_attempts != 0 or new_attempts != 1:
                errors.append("provider timeout must record exactly one dispatched attempt")
        elif event == "partial_success":
            if new.get("certainty") != "unknown" or new.get("next_action") != "reconcile":
                errors.append("partial success must remain unknown pending reconciliation")
            if old_attempts != new_attempts:
                errors.append("partial success cannot repeat the mutation set")

    elif event == "lease_takeover":
        pair = _action_pair(before, after, errors)
        lease_schema = "code_mower.operatorLease.v1"
        if len(before.get(lease_schema, [])) != 1 or len(after.get(lease_schema, [])) != 1:
            errors.append("takeover must contain one lease before and after")
            return tuple(errors)
        old_lease = before[lease_schema][0]
        new_lease = after[lease_schema][0]
        if new_lease.get("epoch") != old_lease.get("epoch", 0) + 1:
            errors.append("takeover must increment the lease epoch")
        if new_lease.get("fence_token") == old_lease.get("fence_token"):
            errors.append("takeover must issue a new fence token")
        if pair is not None:
            old, new = pair
            authority = new.get("reconciliation_authority")
            if old.get("certainty") != "unknown" or new.get("certainty") != "unknown":
                errors.append("takeover must preserve an unknown dispatched outcome")
            if new.get("fence_status") != "stale" or new.get("next_action") != "reconcile":
                errors.append("takeover must reconcile the stale-fenced dispatch")
            if not isinstance(authority, Mapping):
                errors.append("takeover reconciliation requires current authority")
            elif (
                authority.get("lease_epoch") != new_lease.get("epoch")
                or authority.get("fence_token") != new_lease.get("fence_token")
            ):
                errors.append("reconciliation authority must match the takeover lease")
            if old.get("budget", {}).get("attempt_count") != new.get("budget", {}).get("attempt_count"):
                errors.append("takeover cannot blindly reserve another attempt")

    elif event in {"budget_exhaustion", "owner_escalation_redelivery", "owner_stop"}:
        pair = _work_pair(before, after, errors)
        if pair is None:
            return tuple(errors)
        old_work, new_work = pair
        if old_work.get("work_id") != new_work.get("work_id"):
            errors.append("work transition changed work identity")
        if old_work.get("generation") != new_work.get("generation"):
            errors.append("recovery transition changed work generation")

        if event == "budget_exhaustion":
            if new_work.get("state") != "awaiting_owner" or new_work.get("reason") != "budget_exhausted":
                errors.append("budget exhaustion must stop awaiting owner")
            if new_work.get("owner_escalation_count") != old_work.get("owner_escalation_count", 0) + 1:
                errors.append("budget exhaustion must reserve one bounded escalation")
            if not isinstance(new_work.get("owner_escalation_key"), str):
                errors.append("budget exhaustion must persist an escalation dedupe key")
        elif event == "owner_escalation_redelivery":
            if old_work != new_work:
                errors.append("owner escalation redelivery must reuse the durable notification record")
        else:
            action_pair = _action_pair(before, after, errors)
            if new_work.get("state") != "cancelled" or new_work.get("reason") != "owner_cancelled":
                errors.append("owner stop must cancel the work item")
            if action_pair is not None:
                old_action, new_action = action_pair
                if new_action.get("state") != "abandoned" or new_action.get("next_action") != "abandon":
                    errors.append("owner stop must abandon a prepared action")
                if old_action.get("budget", {}).get("attempt_count") != new_action.get("budget", {}).get(
                    "attempt_count"
                ):
                    errors.append("owner stop cannot start another mutation")
    else:
        errors.append(f"unknown recovery event: {event}")
    return tuple(errors)
