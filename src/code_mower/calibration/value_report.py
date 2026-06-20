"""Reviewer value report building and Markdown rendering."""

from __future__ import annotations

import html
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


def render_value_report_html(report: Mapping[str, Any]) -> str:
    """Render a self-contained local HTML reviewer value report."""

    metrics = report.get("metrics", {})
    policy = report.get("policy", {})
    profiles = metrics.get("profiles", {}) if isinstance(metrics, Mapping) else {}
    policies = policy.get("policies", {}) if isinstance(policy, Mapping) else {}
    recommendations = (
        metrics.get("recommendations", []) if isinstance(metrics, Mapping) else []
    )

    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""))

    rows: list[str] = []
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
            rows.append(
                "<tr>"
                f"<td><code>{esc(profile_id)}</code></td>"
                f"<td>{esc(stats.get('runs', 0))}</td>"
                f"<td>{esc(stats.get('useful_findings', 0))}</td>"
                f"<td>{esc(stats.get('negative_findings', 0))}</td>"
                f"<td>{esc(stats.get('useful_rate', ''))}</td>"
                f"<td>{esc(caught_missed)}</td>"
                f"<td>{esc(stats.get('cost_usd', 0))}</td>"
                f"<td>{esc(stats.get('seconds_per_run', ''))}</td>"
                f"<td>{esc(profile_policy.get('recommended_role', ''))}</td>"
                "</tr>"
            )
    else:
        rows.append("<tr><td colspan='9'>No reviewer rows yet.</td></tr>")

    recommendation_items = (
        "\n".join(f"<li>{esc(item)}</li>" for item in recommendations)
        if isinstance(recommendations, list) and recommendations
        else "<li>Collect more adjudicated reviewer evidence before changing merge policy.</li>"
    )
    return (
        "<!doctype html>\n"
        "<html lang='en'>\n"
        "<head>\n"
        "  <meta charset='utf-8'>\n"
        "  <meta name='viewport' content='width=device-width, initial-scale=1'>\n"
        f"  <title>Code Mower Reviewer Value Report - {esc(report.get('corpus_name', ''))}</title>\n"
        "  <style>\n"
        "    :root { color-scheme: light; --ink:#101522; --muted:#5b6475; --line:#d8e0ec; --panel:#fff; --bg:#f6f8fb; --accent:#2f66d0; }\n"
        "    body { margin:0; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:var(--bg); color:var(--ink); }\n"
        "    main { max-width:1120px; margin:0 auto; padding:40px 20px 56px; }\n"
        "    .hero, .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; }\n"
        "    .hero { padding:28px; }\n"
        "    h1 { margin:0; font-size:34px; letter-spacing:0; }\n"
        "    p { color:var(--muted); line-height:1.6; }\n"
        "    .stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; margin-top:20px; }\n"
        "    .stat { border:1px solid var(--line); border-radius:8px; padding:14px; background:#fbfcff; }\n"
        "    .stat strong { display:block; font-size:24px; margin-top:6px; }\n"
        "    .card { margin-top:20px; padding:22px; overflow:auto; }\n"
        "    table { width:100%; border-collapse:collapse; font-size:14px; min-width:860px; }\n"
        "    th { text-align:left; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.08em; }\n"
        "    th, td { padding:12px; border-bottom:1px solid var(--line); vertical-align:top; }\n"
        "    code { background:#eef3fb; padding:2px 5px; border-radius:5px; }\n"
        "    ul { padding-left:20px; }\n"
        "  </style>\n"
        "</head>\n"
        "<body><main>\n"
        "  <section class='hero'>\n"
        "    <p style='margin:0 0 8px;color:var(--accent);font-weight:700;text-transform:uppercase;font-size:12px;letter-spacing:.08em'>Code Mower</p>\n"
        "    <h1>Reviewer Value Report</h1>\n"
        f"    <p>Corpus <code>{esc(report.get('corpus_name', ''))}</code>. This local report compares reviewer/lens usefulness, false positives, spend, latency, and policy recommendations from metadata-only calibration evidence.</p>\n"
        "    <div class='stats'>\n"
        f"      <div class='stat'>Items<strong>{esc(report.get('source_item_count', 0))}</strong></div>\n"
        f"      <div class='stat'>Evidence<strong>{esc(report.get('evidence_count', 0))}</strong></div>\n"
        f"      <div class='stat'>Reviewer runs<strong>{esc(report.get('reviewer_run_count', 0))}</strong></div>\n"
        f"      <div class='stat'>Run dispositions<strong>{esc(report.get('run_disposition_count', 0))}</strong></div>\n"
        "    </div>\n"
        "  </section>\n"
        "  <section class='card'>\n"
        "    <h2>Reviewer / lens signal</h2>\n"
        "    <table><thead><tr><th>Reviewer</th><th>Runs</th><th>Useful</th><th>Negative</th><th>Useful rate</th><th>Blocked caught/missed</th><th>Cost</th><th>Sec/run</th><th>Role</th></tr></thead>\n"
        f"    <tbody>{''.join(rows)}</tbody></table>\n"
        "  </section>\n"
        "  <section class='card'>\n"
        "    <h2>Recommendations</h2>\n"
        f"    <ul>{recommendation_items}</ul>\n"
        "  </section>\n"
        "</main></body></html>\n"
    )
