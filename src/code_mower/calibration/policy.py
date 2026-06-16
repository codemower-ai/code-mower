"""Calibration lane-promotion policy thresholds and report helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .metrics import float_or_zero as _float

MERGE_GATE_USEFUL_RATE = 0.60
SELECTIVE_USEFUL_RATE = 0.50
MERGE_GATE_MIN_FINDINGS = 10
MERGE_GATE_MIN_CLEAN_RUNS = 2


def build_lane_policy_report(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Build lane-promotion policy recommendations from reviewer metrics."""
    if metrics.get("mode") != "reviewer-metrics":
        raise ValueError("lane policy expects a reviewer-metrics report")
    profiles = metrics.get("profiles", {})
    if not isinstance(profiles, Mapping):
        raise ValueError("reviewer-metrics report profiles must be a mapping")

    policies: dict[str, dict[str, Any]] = {}
    for profile_id, stats in sorted(profiles.items()):
        if not isinstance(stats, Mapping):
            continue
        useful_rate = _float(stats.get("useful_rate"))
        useful_findings = int(stats.get("useful_findings") or 0)
        known_findings = int(stats.get("known_disposition_count") or 0)
        clean_pass_runs = int(stats.get("known_clean_pass_runs") or 0)
        false_positive_runs = int(stats.get("blocking_false_positive_runs") or 0)
        known_blocked_missed_runs = int(stats.get("known_blocked_missed_runs") or 0)
        infra_error_runs = int(stats.get("infra_error_runs") or 0)
        audit_input_insufficient_runs = int(
            stats.get("audit_input_insufficient_runs") or 0
        )
        review_classes = [
            str(item)
            for item in stats.get("review_classes", []) or []
            if str(item).strip()
        ]
        useful_review_classes = [
            str(item)
            for item in stats.get("useful_review_classes", []) or []
            if str(item).strip()
        ]
        context_packs = [
            str(item)
            for item in stats.get("context_packs", []) or []
            if str(item).strip()
        ]
        useful_context_packs = [
            str(item)
            for item in stats.get("useful_context_packs", []) or []
            if str(item).strip()
        ]
        narrow_useful_review_classes = [
            review_class
            for review_class in useful_review_classes
            if review_class not in {"", "general"}
        ]
        event_log = stats.get("event_log", {})
        observed_pr_count = (
            int(event_log.get("observed_pr_count") or 0)
            if isinstance(event_log, Mapping)
            else 0
        )
        reasons: list[str] = []
        if known_findings < MERGE_GATE_MIN_FINDINGS:
            reasons.append(
                f"needs at least {MERGE_GATE_MIN_FINDINGS} adjudicated findings"
            )
        if clean_pass_runs < MERGE_GATE_MIN_CLEAN_RUNS:
            reasons.append(
                f"needs at least {MERGE_GATE_MIN_CLEAN_RUNS} known-clean zero-blocker runs"
            )
        if false_positive_runs:
            reasons.append("has known-clean blocking false positives")
        if known_blocked_missed_runs:
            reasons.append("missed known-blocked calibration runs")
        if infra_error_runs:
            reasons.append("has infra/setup failures to stabilize before promotion")
        if audit_input_insufficient_runs:
            reasons.append("needs richer audit input/context before promotion")
        if useful_rate < SELECTIVE_USEFUL_RATE:
            reasons.append("useful-rate below selective-trigger threshold")
        if (
            known_findings >= MERGE_GATE_MIN_FINDINGS
            and clean_pass_runs >= MERGE_GATE_MIN_CLEAN_RUNS
            and false_positive_runs == 0
            and known_blocked_missed_runs == 0
            and infra_error_runs == 0
            and audit_input_insufficient_runs == 0
            and useful_rate >= MERGE_GATE_USEFUL_RATE
        ):
            classification = "merge_gate_candidate"
        elif (
            useful_findings > 0
            and useful_rate >= SELECTIVE_USEFUL_RATE
            and bool(narrow_useful_review_classes)
            and false_positive_runs == 0
            and clean_pass_runs >= MERGE_GATE_MIN_CLEAN_RUNS
            and known_blocked_missed_runs == 0
            and infra_error_runs == 0
            and audit_input_insufficient_runs == 0
        ):
            classification = "selective_trigger_candidate"
        else:
            classification = "informational"

        if (
            classification == "informational"
            and useful_findings > 0
            and useful_rate >= SELECTIVE_USEFUL_RATE
            and not narrow_useful_review_classes
        ):
            reasons.append(
                "needs useful non-general review-class evidence for selective triggers"
            )

        if classification == "merge_gate_candidate":
            recommended_role = "merge_gate_eligible"
            automatic_trigger = "repo_merge_bar_opt_in"
        elif classification == "selective_trigger_candidate":
            recommended_role = "selective_trigger"
            automatic_trigger = "matching_review_class_only"
        else:
            recommended_role = "informational"
            automatic_trigger = "manual_or_calibration_only"

        suggested_trigger_classes = (
            narrow_useful_review_classes
            if classification == "selective_trigger_candidate"
            else []
        )

        policies[str(profile_id)] = {
            "profile_id": str(profile_id),
            "classification": classification,
            "recommended_role": recommended_role,
            "automatic_trigger": automatic_trigger,
            "suggested_trigger_classes": suggested_trigger_classes,
            "review_classes": review_classes,
            "context_packs": context_packs,
            "useful_review_classes": useful_review_classes,
            "useful_context_packs": useful_context_packs,
            "useful_rate": (
                useful_rate if stats.get("useful_rate") is not None else None
            ),
            "useful_findings": useful_findings,
            "known_disposition_count": known_findings,
            "known_clean_pass_runs": clean_pass_runs,
            "blocking_false_positive_runs": false_positive_runs,
            "known_blocked_runs": int(stats.get("known_blocked_runs") or 0),
            "known_blocked_caught_runs": int(
                stats.get("known_blocked_caught_runs") or 0
            ),
            "known_blocked_missed_runs": known_blocked_missed_runs,
            "infra_error_runs": infra_error_runs,
            "audit_input_insufficient_runs": audit_input_insufficient_runs,
            "observed_pr_count": observed_pr_count,
            "reasons": reasons
            or [
                "evidence meets current threshold heuristics; "
                "keep human review in the loop"
            ],
        }

    return {
        "mode": "code-mower-lane-policy",
        "source_mode": metrics.get("mode"),
        "thresholds": {
            "merge_gate_useful_rate": MERGE_GATE_USEFUL_RATE,
            "selective_useful_rate": SELECTIVE_USEFUL_RATE,
            "merge_gate_min_findings": MERGE_GATE_MIN_FINDINGS,
            "merge_gate_min_clean_runs": MERGE_GATE_MIN_CLEAN_RUNS,
        },
        "policies": policies,
        "caveat": (
            "This is a policy recommendation from calibration evidence, not an "
            "automatic repository merge-rule change."
        ),
    }
