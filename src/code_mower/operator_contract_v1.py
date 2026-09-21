"""Normative semantic checks for the closed Operator v1 contract.

JSON Schema validates each record's closed shape.  These helpers validate the
cross-field, wall-clock, and before/after rules that JSON Schema cannot express
without implementation-specific extensions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
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
    "lease_id",
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
        elif reason == "evidence_failed" and not isinstance(evidence, Mapping):
            errors.append("failed evidence must retain the evidence record")
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
        if status == "failed" and reason == "capability_missing" and not missing:
            errors.append("capability-missing status has no missing capability")
    return tuple(errors)


def action_intent_semantic_errors(
    record: Mapping[str, Any],
    *,
    current_work_generation: int,
    current_lease: Mapping[str, Any] | None = None,
    current_head_sha: str | None = None,
    now: int | None = None,
) -> tuple[str, ...]:
    """Check an intent against independently read current durable authority."""

    errors: list[str] = []
    if record.get("work_generation") != current_work_generation:
        errors.append("action intent belongs to a stale work generation")

    authority = record.get("reconciliation_authority")
    next_action = record.get("next_action")
    certainty = record.get("certainty")
    requires_result_commit = certainty in {"confirmed_success", "confirmed_failure"}
    requires_authority = next_action in {"dispatch", "retry", "reconcile"} or requires_result_commit
    requires_exact_head = next_action in {"dispatch", "retry"} or requires_result_commit

    if requires_authority:
        if not isinstance(current_lease, Mapping):
            errors.append("current durable lease is required for this action")
        else:
            if current_lease.get("schema") != "code_mower.operatorLease.v1":
                errors.append("current durable lease has the wrong schema")
            errors.extend(lease_semantic_errors(current_lease, now=now, require_live=True))
            if current_lease.get("tenant_id") != record.get("tenant_id"):
                errors.append("current lease tenant does not match the action intent")
            if current_lease.get("repository_id") != record.get("repository_id"):
                errors.append("current lease repository does not match the action intent")
            if current_lease.get("lease_id") != record.get("lease_id"):
                errors.append("current lease identity does not match the action intent")
            expected_epoch = record.get("lease_epoch")
            expected_token = record.get("fence_token")
            if isinstance(authority, Mapping):
                expected_epoch = authority.get("lease_epoch")
                expected_token = authority.get("fence_token")
            if (
                current_lease.get("epoch") != expected_epoch
                or current_lease.get("fence_token") != expected_token
            ):
                errors.append("action authority does not match the current lease fence")

    if next_action in {"dispatch", "retry"}:
        if record.get("fence_status") != "current":
            errors.append("dispatch or retry requires a current dispatch fence")
        if record.get("head_status") != "current":
            errors.append("dispatch or retry requires a current target head")
    if requires_exact_head:
        if current_head_sha is None:
            errors.append("current durable head is required for this action")
        elif current_head_sha != record.get("target_head_sha"):
            errors.append("action target does not match the current durable head")
    return tuple(errors)


def lease_semantic_errors(
    record: Mapping[str, Any],
    *,
    now: int | None = None,
    require_live: bool = False,
) -> tuple[str, ...]:
    """Validate lease chronology and, when requested, live mutation authority."""

    errors: list[str] = []
    if record.get("schema") != "code_mower.operatorLease.v1":
        errors.append("lease record has the wrong schema")
    acquired_at = record.get("acquired_at")
    renew_by = record.get("renew_by")
    expires_at = record.get("expires_at")
    if all(isinstance(value, int) for value in (acquired_at, renew_by, expires_at)):
        if not acquired_at <= renew_by < expires_at:
            errors.append("lease chronology must satisfy acquired_at <= renew_by < expires_at")
        if require_live:
            if now is None:
                errors.append("current time is required to establish a live lease")
            elif not acquired_at <= now < renew_by:
                errors.append("lease is not live for starting or committing mutation work")
    else:
        errors.append("lease chronology requires integer timestamps")
    if require_live and record.get("state") != "active":
        errors.append("mutation authority requires an active lease")
    return tuple(errors)


def _decimal(value: object, label: str, errors: list[str]) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        errors.append(f"{label} is not a decimal amount")
        return None
    if not parsed.is_finite():
        errors.append(f"{label} is not a finite decimal amount")
        return None
    return parsed


def policy_binding_errors(
    policy: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    current_work_generation: int | None = None,
    current_lease: Mapping[str, Any] | None = None,
    current_head_sha: str | None = None,
    now: int | None = None,
) -> tuple[str, ...]:
    """Bind one state record to the owner policy and current durable authority."""

    errors: list[str] = []
    if record.get("tenant_id") != policy.get("tenant_id"):
        errors.append("record tenant is outside the owner policy")
    allowlist = policy.get("repository_allowlist")
    if not isinstance(allowlist, list) or record.get("repository_id") not in allowlist:
        errors.append("record repository is outside the owner policy allowlist")

    budgets = policy.get("budgets")
    if not isinstance(budgets, Mapping):
        return tuple((*errors, "owner policy budgets are unavailable"))
    schema = record.get("schema")
    if schema == "code_mower.operatorActionIntent.v1":
        mutations = policy.get("authority", {}).get("mutations", [])
        if record.get("operation") not in mutations:
            errors.append("action operation is not authorized by owner policy")
        budget = record.get("budget")
        if not isinstance(budget, Mapping):
            errors.append("action budget is unavailable")
        else:
            ceiling_fields = (
                ("attempt_count", "max_attempts_per_action"),
                ("reconciliation_count", "max_reconciliations_per_action"),
                ("elapsed_seconds", "max_action_seconds"),
            )
            for field, ceiling in ceiling_fields:
                value = budget.get(field)
                limit = budgets.get(ceiling)
                if isinstance(value, int) and isinstance(limit, int) and value > limit:
                    errors.append(f"action {field} exceeds owner policy {ceiling}")
            spend = _decimal(budget.get("spend_usd"), "action spend", errors)
            spend_limit = _decimal(budgets.get("max_spend_usd"), "policy spend ceiling", errors)
            if spend is not None and spend_limit is not None and spend > spend_limit:
                errors.append("action spend exceeds owner policy max_spend_usd")
        if current_work_generation is None:
            errors.append("current work generation is required for an action intent")
        else:
            errors.extend(
                action_intent_semantic_errors(
                    record,
                    current_work_generation=current_work_generation,
                    current_lease=current_lease,
                    current_head_sha=current_head_sha,
                    now=now,
                )
            )
        if isinstance(current_lease, Mapping):
            errors.extend(policy_binding_errors(policy, current_lease, now=now))
    elif schema == "code_mower.operatorWorkItem.v1":
        count = record.get("owner_escalation_count")
        limit = budgets.get("max_owner_escalations")
        if isinstance(count, int) and isinstance(limit, int) and count > limit:
            errors.append("owner escalation count exceeds owner policy")
        created_at = record.get("created_at")
        updated_at = record.get("updated_at")
        work_limit = budgets.get("max_work_seconds")
        if isinstance(created_at, int) and isinstance(updated_at, int) and updated_at < created_at:
            errors.append("work updated_at precedes created_at")
        elapsed = record.get("elapsed_seconds")
        if isinstance(elapsed, int) and isinstance(work_limit, int) and elapsed > work_limit:
            errors.append("work elapsed time exceeds owner policy max_work_seconds")
        spend = _decimal(record.get("spend_usd"), "work spend", errors)
        spend_limit = _decimal(budgets.get("max_spend_usd"), "policy spend ceiling", errors)
        if spend is not None and spend_limit is not None and spend > spend_limit:
            errors.append("work spend exceeds owner policy max_spend_usd")
    elif schema == "code_mower.operatorLease.v1":
        errors.extend(lease_semantic_errors(record, now=now))
        renew_by = record.get("renew_by")
        expires_at = record.get("expires_at")
        ttl = budgets.get("lease_ttl_seconds")
        renewal = budgets.get("lease_renewal_seconds")
        if all(isinstance(value, int) for value in (renew_by, expires_at, ttl, renewal)):
            if expires_at - renew_by != ttl - renewal:
                errors.append("lease deadlines do not match owner policy cadence")
    return tuple(errors)


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
    old_budget = old.get("budget")
    new_budget = new.get("budget")
    if isinstance(old_budget, Mapping) and isinstance(new_budget, Mapping):
        for field in ("attempt_count", "reconciliation_count", "elapsed_seconds"):
            old_value = old_budget.get(field)
            new_value = new_budget.get(field)
            if isinstance(old_value, int) and isinstance(new_value, int) and new_value < old_value:
                errors.append(f"action cumulative {field} moved backwards")
        old_spend = _decimal(old_budget.get("spend_usd"), "old action spend", errors)
        new_spend = _decimal(new_budget.get("spend_usd"), "new action spend", errors)
        if old_spend is not None and new_spend is not None and new_spend < old_spend:
            errors.append("action cumulative spend moved backwards")
    old_updated = old.get("updated_at")
    new_updated = new.get("updated_at")
    if isinstance(old_updated, int) and isinstance(new_updated, int) and new_updated < old_updated:
        errors.append("action updated_at moved backwards")
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
    old = before[schema][0]
    new = after[schema][0]
    for field in ("tenant_id", "repository_id", "work_id", "generation", "created_at"):
        if old.get(field) != new.get(field):
            errors.append(f"work item changed immutable field {field}")
    old_count = old.get("owner_escalation_count")
    new_count = new.get("owner_escalation_count")
    if isinstance(old_count, int) and isinstance(new_count, int) and new_count < old_count:
        errors.append("owner escalation count moved backwards")
    old_elapsed = old.get("elapsed_seconds")
    new_elapsed = new.get("elapsed_seconds")
    if isinstance(old_elapsed, int) and isinstance(new_elapsed, int) and new_elapsed < old_elapsed:
        errors.append("work cumulative elapsed_seconds moved backwards")
    old_spend = _decimal(old.get("spend_usd"), "old work spend", errors)
    new_spend = _decimal(new.get("spend_usd"), "new work spend", errors)
    if old_spend is not None and new_spend is not None and new_spend < old_spend:
        errors.append("work cumulative spend moved backwards")
    old_updated = old.get("updated_at")
    new_updated = new.get("updated_at")
    if isinstance(old_updated, int) and isinstance(new_updated, int) and new_updated < old_updated:
        errors.append("work updated_at moved backwards")
    return old, new


def _lease_pair(
    before: dict[str, list[Mapping[str, Any]]],
    after: dict[str, list[Mapping[str, Any]]],
    errors: list[str],
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | None:
    schema = "code_mower.operatorLease.v1"
    if len(before.get(schema, [])) != 1 or len(after.get(schema, [])) != 1:
        errors.append("takeover must contain one lease before and after")
        return None
    old = before[schema][0]
    new = after[schema][0]
    errors.extend(lease_semantic_errors(old))
    errors.extend(lease_semantic_errors(new))
    for field in ("tenant_id", "repository_id", "lease_id"):
        if old.get(field) != new.get(field):
            errors.append(f"takeover changed lease scope field {field}")
    if old.get("holder_id") == new.get("holder_id"):
        errors.append("takeover must use a new holder identity")
    if new.get("state") != "active":
        errors.append("takeover must establish an active lease")
    old_acquired = old.get("acquired_at")
    new_acquired = new.get("acquired_at")
    if isinstance(old_acquired, int) and isinstance(new_acquired, int) and new_acquired < old_acquired:
        errors.append("takeover lease acquisition moved backwards")
    return old, new


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
        lease_pair = _lease_pair(before, after, errors)
        if lease_pair is None:
            return tuple(errors)
        old_lease, new_lease = lease_pair
        if new_lease.get("epoch") != old_lease.get("epoch", 0) + 1:
            errors.append("takeover must increment the lease epoch")
        if new_lease.get("fence_token") == old_lease.get("fence_token"):
            errors.append("takeover must issue a new fence token")
        if pair is not None:
            old, new = pair
            authority = new.get("reconciliation_authority")
            for action, lease, label in ((old, old_lease, "dispatch"), (new, new_lease, "recovery")):
                if action.get("tenant_id") != lease.get("tenant_id"):
                    errors.append(f"{label} intent tenant does not match its lease")
                if action.get("repository_id") != lease.get("repository_id"):
                    errors.append(f"{label} intent repository does not match its lease")
            if (
                old.get("lease_epoch") != old_lease.get("epoch")
                or old.get("fence_token") != old_lease.get("fence_token")
            ):
                errors.append("dispatched intent does not match the original lease fence")
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
