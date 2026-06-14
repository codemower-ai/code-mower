#!/usr/bin/env python3
"""Report reviewer accuracy, spend, and value from adjudicated outputs."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import code_mower_telemetry
    else:
        from tools import code_mower_telemetry
elif __package__ == "tools":
    from tools import code_mower_telemetry
else:  # pragma: no cover - exercised after package extraction.
    from . import code_mower_telemetry


POSITIVE_DISPOSITIONS = {"true_positive", "useful"}
NEGATIVE_DISPOSITIONS = {"false_positive", "noise"}
KNOWN_DISPOSITIONS = POSITIVE_DISPOSITIONS | NEGATIVE_DISPOSITIONS | {"unknown"}
SUPPORTED_CALIBRATION_REPORT_MODES = {
    "local-llm-calibration",
    "reviewer-evidence-calibration",
}


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _load_event_summary(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise ValueError(f"event log does not exist or is not a file: {path}")
    return code_mower_telemetry.summarize_events(
        code_mower_telemetry.load_jsonl_events(path)
    )


def _float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _spend_by_profile(spend_payload: Mapping[str, Any] | None) -> dict[str, float]:
    if not spend_payload:
        return {}
    raw_profiles = spend_payload.get("profiles", spend_payload)
    if not isinstance(raw_profiles, Mapping):
        raise ValueError("spend file must be a mapping or contain a profiles mapping")
    spend: dict[str, float] = {}
    for profile_id, value in raw_profiles.items():
        if isinstance(value, Mapping):
            cost = _float(value.get("cost_usd", value.get("usd")))
        else:
            cost = _float(value)
        spend[str(profile_id)] = cost
    return spend


def _rate(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def build_reviewer_metrics(
    calibration_reports: Iterable[Mapping[str, Any]],
    *,
    spend: Mapping[str, Any] | None = None,
    event_summaries: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    spend_map = _spend_by_profile(spend)
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    durations: dict[str, float] = defaultdict(float)
    runs: dict[str, int] = defaultdict(int)
    known_clean_pass_runs: dict[str, int] = defaultdict(int)
    blocking_false_positive_runs: dict[str, int] = defaultdict(int)
    known_blocked_runs: dict[str, int] = defaultdict(int)
    known_blocked_caught_runs: dict[str, int] = defaultdict(int)
    known_blocked_missed_runs: dict[str, int] = defaultdict(int)
    infra_error_runs: dict[str, int] = defaultdict(int)
    audit_input_insufficient_runs: dict[str, int] = defaultdict(int)
    run_statuses: dict[str, Counter[str]] = defaultdict(Counter)
    review_classes: dict[str, set[str]] = defaultdict(set)
    context_packs: dict[str, set[str]] = defaultdict(set)
    useful_review_classes: dict[str, set[str]] = defaultdict(set)
    useful_context_packs: dict[str, set[str]] = defaultdict(set)
    models: dict[str, str] = {}
    source_count = 0
    event_summary_count = 0

    for report in calibration_reports:
        source_count += 1
        if report.get("mode") not in SUPPORTED_CALIBRATION_REPORT_MODES:
            raise ValueError(
                "reviewer metrics expects local-llm-calibration or "
                "reviewer-evidence-calibration reports"
            )
        profiles = report.get("profiles", {})
        if isinstance(profiles, Mapping):
            for profile_id, stats in profiles.items():
                if not isinstance(stats, Mapping):
                    continue
                profile = str(profile_id)
                models[profile] = str(stats.get("model") or models.get(profile, ""))
                runs[profile] += int(stats.get("runs") or 0)
                durations[profile] += _float(stats.get("duration_seconds_total"))
                known_clean_pass_runs[profile] += int(stats.get("known_clean_pass_runs") or 0)
                blocking_false_positive_runs[profile] += int(
                    stats.get("blocking_false_positive_runs") or 0
                )
                known_blocked_runs[profile] += int(stats.get("known_blocked_runs") or 0)
                known_blocked_caught_runs[profile] += int(
                    stats.get("known_blocked_caught_runs") or 0
                )
                known_blocked_missed_runs[profile] += int(
                    stats.get("known_blocked_missed_runs") or 0
                )
                infra_error_runs[profile] += int(stats.get("infra_error_runs") or 0)
                audit_input_insufficient_runs[profile] += int(
                    stats.get("audit_input_insufficient_runs") or 0
                )
                raw_run_statuses = stats.get("run_statuses", {})
                if isinstance(raw_run_statuses, Mapping):
                    for status, count in raw_run_statuses.items():
                        run_statuses[profile][str(status)] += int(count or 0)
                for review_class in stats.get("review_classes", []) or []:
                    text = str(review_class).strip()
                    if text:
                        review_classes[profile].add(text)
                for context_pack in stats.get("context_packs", []) or []:
                    text = str(context_pack).strip()
                    if text:
                        context_packs[profile].add(text)
                for review_class in stats.get("useful_review_classes", []) or []:
                    text = str(review_class).strip()
                    if text:
                        useful_review_classes[profile].add(text)
                for context_pack in stats.get("useful_context_packs", []) or []:
                    text = str(context_pack).strip()
                    if text:
                        useful_context_packs[profile].add(text)
                dispositions = stats.get("dispositions", {})
                if isinstance(dispositions, Mapping):
                    for disposition, count in dispositions.items():
                        normalized = str(disposition).strip().lower()
                        if normalized not in KNOWN_DISPOSITIONS:
                            continue
                        counts[profile][normalized] += int(count or 0)

    lane_events: dict[str, dict[str, Any]] = {}
    for summary in event_summaries:
        event_summary_count += 1
        if summary.get("mode") != "telemetry-summary":
            raise ValueError("event summaries must be telemetry-summary reports")
        lanes = summary.get("lanes", {})
        if not isinstance(lanes, Mapping):
            continue
        for lane_id, lane_stats in lanes.items():
            if not isinstance(lane_stats, Mapping):
                continue
            profile = str(lane_id)
            existing = lane_events.setdefault(
                profile,
                {
                    "events": 0,
                    "finished": 0,
                    "pass": 0,
                    "blocked": 0,
                    "failed": 0,
                    "findings": 0,
                    "observed_pr_count": 0,
                },
            )
            for key in ("events", "finished", "pass", "blocked", "failed", "findings", "observed_pr_count"):
                existing[key] += int(lane_stats.get(key) or 0)

    profiles_out: dict[str, dict[str, Any]] = {}
    for profile_id in sorted(set(counts) | set(runs) | set(spend_map) | set(lane_events)):
        profile_counts = counts[profile_id]
        true_positive = profile_counts["true_positive"]
        false_positive = profile_counts["false_positive"]
        useful = profile_counts["useful"]
        noise = profile_counts["noise"]
        unknown = profile_counts["unknown"]
        known = true_positive + false_positive + useful + noise
        useful_total = true_positive + useful
        negative_total = false_positive + noise
        cost_usd = spend_map.get(profile_id, 0.0)
        run_count = runs.get(profile_id, 0)
        duration_seconds = durations.get(profile_id, 0.0)
        known_blocked_catches = known_blocked_caught_runs.get(profile_id, 0)
        profiles_out[profile_id] = {
            "profile_id": profile_id,
            "model": models.get(profile_id, ""),
            "runs": run_count,
            "duration_seconds_total": round(duration_seconds, 3),
            "cost_usd": round(cost_usd, 4),
            "dispositions": dict(sorted(profile_counts.items())),
            "known_disposition_count": known,
            "unknown_disposition_count": unknown,
            "known_clean_pass_runs": known_clean_pass_runs.get(profile_id, 0),
            "blocking_false_positive_runs": blocking_false_positive_runs.get(profile_id, 0),
            "known_blocked_runs": known_blocked_runs.get(profile_id, 0),
            "known_blocked_caught_runs": known_blocked_caught_runs.get(profile_id, 0),
            "known_blocked_missed_runs": known_blocked_missed_runs.get(profile_id, 0),
            "infra_error_runs": infra_error_runs.get(profile_id, 0),
            "audit_input_insufficient_runs": audit_input_insufficient_runs.get(
                profile_id, 0
            ),
            "run_statuses": dict(sorted(run_statuses.get(profile_id, {}).items())),
            "review_classes": sorted(review_classes.get(profile_id, set())),
            "context_packs": sorted(context_packs.get(profile_id, set())),
            "useful_review_classes": sorted(
                useful_review_classes.get(profile_id, set())
            ),
            "useful_context_packs": sorted(
                useful_context_packs.get(profile_id, set())
            ),
            "useful_findings": useful_total,
            "negative_findings": negative_total,
            "precision": _rate(true_positive, true_positive + false_positive),
            "useful_rate": _rate(useful_total, known),
            "noise_rate": _rate(negative_total, known),
            "useful_findings_per_usd": (
                round(useful_total / cost_usd, 4) if cost_usd > 0 else None
            ),
            "cost_per_run": (
                round(cost_usd / run_count, 4) if cost_usd > 0 and run_count else None
            ),
            "cost_per_useful_finding": (
                round(cost_usd / useful_total, 4) if cost_usd > 0 and useful_total else None
            ),
            "cost_per_known_blocked_catch": (
                round(cost_usd / known_blocked_catches, 4)
                if cost_usd > 0 and known_blocked_catches
                else None
            ),
            "seconds_per_run": (
                round(duration_seconds / run_count, 3) if run_count else None
            ),
            "seconds_per_useful_finding": (
                round(duration_seconds / useful_total, 3)
                if useful_total
                else None
            ),
            "seconds_per_known_blocked_catch": (
                round(duration_seconds / known_blocked_catches, 3)
                if known_blocked_catches
                else None
            ),
            "event_log": lane_events.get(profile_id, {}),
        }

    return {
        "mode": "reviewer-metrics",
        "source_report_count": source_count,
        "event_summary_count": event_summary_count,
        "profiles": profiles_out,
        "recommendations": _recommendations(profiles_out),
    }


def _recommendations(profiles: Mapping[str, Mapping[str, Any]]) -> list[str]:
    recommendations: list[str] = []
    for profile_id, stats in profiles.items():
        if stats.get("known_disposition_count", 0) == 0:
            recommendations.append(
                f"{profile_id}: collect human dispositions before comparing reviewer accuracy."
            )
            continue
        if (stats.get("useful_rate") or 0) < 0.5:
            recommendations.append(
                f"{profile_id}: low useful-rate; keep informational until prompt or context improves."
            )
        if stats.get("known_blocked_missed_runs", 0):
            recommendations.append(
                f"{profile_id}: missed known-blocked calibration runs; keep informational until catch rate improves."
            )
        if stats.get("audit_input_insufficient_runs", 0):
            recommendations.append(
                f"{profile_id}: audit input was insufficient on some runs; add context packs or larger budgets before promotion."
            )
        if stats.get("cost_usd", 0) and not stats.get("useful_findings", 0):
            recommendations.append(
                f"{profile_id}: spend recorded but no useful findings; pause paid runs or narrow triggers."
            )
    if not recommendations:
        recommendations.append(
            "Use these metrics as calibration evidence, not automatic merge authority."
        )
    return recommendations


def render_reviewer_metrics_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower reviewer metrics",
        "",
        "Profiles:",
    ]
    profiles = report.get("profiles", {})
    if isinstance(profiles, Mapping) and profiles:
        for profile_id, stats in profiles.items():
            if not isinstance(stats, Mapping):
                continue
            lines.append(
                f"- {profile_id}: useful={stats.get('useful_findings', 0)} "
                f"negative={stats.get('negative_findings', 0)} "
                f"useful_rate={stats.get('useful_rate')} "
                f"cost=${stats.get('cost_usd', 0)} "
                f"sec_per_run={stats.get('seconds_per_run')} "
                f"useful_per_usd={stats.get('useful_findings_per_usd')} "
                f"cost_per_useful={stats.get('cost_per_useful_finding')}"
            )
            event_log = stats.get("event_log", {})
            if isinstance(event_log, Mapping) and event_log:
                lines.append(
                    f"  events: finished={event_log.get('finished', 0)} "
                    f"pass={event_log.get('pass', 0)} blocked={event_log.get('blocked', 0)} "
                    f"findings={event_log.get('findings', 0)}"
                )
    else:
        lines.append("- none")
    lines.extend(["", "Recommendations:"])
    for recommendation in report.get("recommendations", []) or []:
        lines.append(f"- {recommendation}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("calibration_reports", nargs="*", type=Path)
    parser.add_argument("--spend", type=Path, default=None)
    parser.add_argument(
        "--events",
        nargs="+",
        type=Path,
        default=[],
        help="Optional Code Mower audit event JSONL logs to fold into lane value metrics.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        if not args.calibration_reports and not args.events:
            raise ValueError("pass at least one calibration report or --events log")
        reports = [_load_json(path) for path in args.calibration_reports]
        spend = _load_json(args.spend) if args.spend is not None else None
        event_summaries = [_load_event_summary(path) for path in args.events]
        metrics = build_reviewer_metrics(
            reports,
            spend=spend,
            event_summaries=event_summaries,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(metrics, indent=2, sort_keys=True))
    else:
        print(render_reviewer_metrics_text(metrics), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
