"""Reviewer value report building and Markdown rendering."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .. import reviewer_metrics
from .evidence_report import build_reviewer_evidence_report
from .policy import build_lane_policy_report
from .run_results import corpus_with_run_results


def build_value_report(
    corpus: Mapping[str, Any],
    *,
    spend: Mapping[str, Any] | None = None,
    event_summaries: Iterable[Mapping[str, Any]] = (),
    run_results: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    corpus = corpus_with_run_results(corpus, run_results)
    evidence = build_reviewer_evidence_report(corpus)
    metrics = reviewer_metrics.build_reviewer_metrics(
        [evidence],
        spend=spend,
        event_summaries=event_summaries,
    )
    policy = build_lane_policy_report(metrics)
    return {
        "mode": "code-mower-reviewer-value-report",
        "corpus_name": corpus.get("name", ""),
        "description": corpus.get("description", ""),
        "source_item_count": evidence["source_item_count"],
        "evidence_count": evidence["evidence_count"],
        "finding_evidence_count": evidence.get("finding_evidence_count", 0),
        "run_disposition_count": evidence.get("run_disposition_count", 0),
        "reviewer_run_count": len(evidence.get("reviewer_runs", [])),
        "evidence": evidence,
        "metrics": metrics,
        "policy": policy,
    }


def render_value_report_text(report: Mapping[str, Any]) -> str:
    metrics = report.get("metrics", {})
    policy = report.get("policy", {})
    profiles = metrics.get("profiles", {}) if isinstance(metrics, Mapping) else {}
    policies = policy.get("policies", {}) if isinstance(policy, Mapping) else {}
    lines = [
        "# Code Mower Reviewer Value Report",
        "",
        f"Corpus: `{report.get('corpus_name', '')}`",
        f"Items: {report.get('source_item_count', 0)}",
        f"Adjudicated evidence: {report.get('evidence_count', 0)}",
        f"Finding evidence: {report.get('finding_evidence_count', 0)}",
        f"Run dispositions: {report.get('run_disposition_count', 0)}",
        f"Reviewer runs: {report.get('reviewer_run_count', 0)}",
        "",
        "| Reviewer | Runs | Useful | Negative | Useful rate | Known-clean pass | Known-blocked caught/missed | Infra errors | Input gaps | Cost | Sec/run | Cost/useful | Policy | Recommended role |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    if isinstance(profiles, Mapping) and profiles:
        for profile_id, stats in sorted(profiles.items()):
            if not isinstance(stats, Mapping):
                continue
            profile_policy = policies.get(profile_id, {}) if isinstance(policies, Mapping) else {}
            if not isinstance(profile_policy, Mapping):
                profile_policy = {}
            caught_missed = (
                f"{stats.get('known_blocked_caught_runs', 0)}/"
                f"{stats.get('known_blocked_missed_runs', 0)}"
            )
            useful_rate = stats.get("useful_rate")
            useful_rate_text = "" if useful_rate is None else str(useful_rate)
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"`{profile_id}`",
                        str(stats.get("runs", 0)),
                        str(stats.get("useful_findings", 0)),
                        str(stats.get("negative_findings", 0)),
                        useful_rate_text,
                        str(stats.get("known_clean_pass_runs", 0)),
                        caught_missed,
                        str(stats.get("infra_error_runs", 0)),
                        str(stats.get("audit_input_insufficient_runs", 0)),
                        str(stats.get("cost_usd", 0)),
                        (
                            ""
                            if stats.get("seconds_per_run") is None
                            else str(stats.get("seconds_per_run"))
                        ),
                        (
                            ""
                            if stats.get("cost_per_useful_finding") is None
                            else str(stats.get("cost_per_useful_finding"))
                        ),
                        f"`{profile_policy.get('classification', '')}`",
                        f"`{profile_policy.get('recommended_role', '')}`",
                    ]
                )
                + " |"
            )
    else:
        lines.append("| none | 0 | 0 | 0 |  | 0 | 0/0 | 0 | 0 | 0 |  |  |  |  |")

    recommendations = metrics.get("recommendations", []) if isinstance(metrics, Mapping) else []
    lines.extend(["", "## Recommendations"])
    if isinstance(recommendations, list) and recommendations:
        lines.extend(f"- {item}" for item in recommendations)
    else:
        lines.append("- Collect more adjudicated reviewer evidence before changing merge policy.")

    lines.extend(["", "## Policy Reasons"])
    if isinstance(policies, Mapping) and policies:
        for profile_id, profile_policy in sorted(policies.items()):
            if not isinstance(profile_policy, Mapping):
                continue
            reasons = profile_policy.get("reasons", [])
            reason_text = (
                "; ".join(str(reason) for reason in reasons)
                if isinstance(reasons, list)
                else ""
            )
            trigger_classes = profile_policy.get("suggested_trigger_classes", [])
            trigger_text = (
                f"; suggested classes: {', '.join(str(item) for item in trigger_classes)}"
                if isinstance(trigger_classes, list) and trigger_classes
                else ""
            )
            lines.append(
                f"- `{profile_id}`: `{profile_policy.get('classification', '')}`"
                f" / `{profile_policy.get('recommended_role', '')}`"
                f" / `{profile_policy.get('automatic_trigger', '')}`"
                + (f" - {reason_text}{trigger_text}" if reason_text or trigger_text else "")
            )
    else:
        lines.append("- No policy rows available.")
    caveat = policy.get("caveat") if isinstance(policy, Mapping) else None
    if caveat:
        lines.extend(["", f"_Caveat: {caveat}_"])
    return "\n".join(lines) + "\n"
