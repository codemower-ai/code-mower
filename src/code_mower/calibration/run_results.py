"""Calibration run-results ingestion and corpus folding."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .corpus import load_json_object
from .evidence import KNOWN_EVIDENCE_DISPOSITIONS
from .run_status import normalize_run_status_category


CALIBRATION_RUN_RESULTS_MODE = "code-mower-calibration-run-results"
CALIBRATION_RUN_RESULTS_SCHEMA = "code_mower.calibrationRunResults.v1"


def load_run_results(paths: Iterable[Path]) -> list[Mapping[str, Any]]:
    reports: list[Mapping[str, Any]] = []
    for path in paths:
        payload = load_json_object(path)
        if payload.get("mode") != CALIBRATION_RUN_RESULTS_MODE:
            raise ValueError(f"{path} is not a Code Mower calibration run-results file")
        reports.append(payload)
    return reports


def csv_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return {str(part).strip() for part in value if str(part).strip()}
    if value is None:
        return set()
    return {str(value).strip()} if str(value).strip() else set()


def normalize_disposition(value: Any) -> str:
    disposition = str(value or "unknown").strip().lower().replace("-", "_")
    if disposition not in KNOWN_EVIDENCE_DISPOSITIONS:
        return "unknown"
    return disposition


def run_matches_disposition_rule(
    run: Mapping[str, Any],
    rule: Mapping[str, Any],
) -> bool:
    reviewers = csv_values(rule.get("reviewer") or rule.get("profile_id") or rule.get("lane"))
    if reviewers:
        reviewer = str(
            run.get("reviewer")
            or run.get("profile_id")
            or run.get("lane")
            or "unknown-reviewer"
        ).strip()
        if reviewer not in reviewers:
            return False
    statuses = {
        normalize_run_status_category(status)
        for status in csv_values(rule.get("status") or rule.get("status_category"))
    }
    if statuses:
        run_status = normalize_run_status_category(run.get("status") or run.get("verdict"))
        if run_status not in statuses:
            return False
    result_categories = csv_values(rule.get("result_category"))
    if result_categories:
        if str(run.get("result_category") or "").strip() not in result_categories:
            return False
    try:
        min_findings = int(rule.get("min_finding_count") or 0)
    except (TypeError, ValueError):
        min_findings = 0
    if min_findings:
        try:
            finding_count = int(run.get("finding_count") or 0)
        except (TypeError, ValueError):
            finding_count = 0
        if finding_count < min_findings:
            return False
    return True


def apply_run_disposition_rules(run: dict[str, Any], item: Mapping[str, Any]) -> None:
    for rule in item.get("reviewer_run_dispositions", []) or []:
        if not isinstance(rule, Mapping):
            continue
        if not run_matches_disposition_rule(run, rule):
            continue
        disposition = normalize_disposition(rule.get("disposition"))
        if disposition != "unknown" and not run.get("disposition"):
            run["disposition"] = disposition
        if rule.get("expected_blocker_caught") is not None:
            run["expected_blocker_caught"] = bool(rule.get("expected_blocker_caught"))
        if rule.get("notes") and not run.get("disposition_notes"):
            run["disposition_notes"] = str(rule.get("notes") or "")
        # Apply the first matching adjudication rule. Additional notes can be
        # modeled as reviewer_evidence if a corpus needs more detail.
        return


def corpus_with_run_results(
    corpus: Mapping[str, Any],
    run_results: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    merged = copy.deepcopy(dict(corpus))
    items = merged.get("corpus", [])
    if not isinstance(items, list):
        raise ValueError("calibration corpus must include a corpus list")
    item_by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("repo") or ""),
            int(item.get("pr_number") or 0),
            str(item.get("head_sha") or ""),
        )
        item_by_key[key] = item

    for result_index, result_report in enumerate(run_results):
        manifest_id = str(
            result_report.get("run_results_id")
            or result_report.get("started_at")
            or result_index
        )
        for run in result_report.get("reviewer_runs", []) or []:
            if not isinstance(run, Mapping):
                continue
            key = (
                str(run.get("repo") or ""),
                int(run.get("pr_number") or 0),
                str(run.get("head_sha") or ""),
            )
            item = item_by_key.get(key)
            if item is None:
                continue
            folded_run = {
                key: value
                for key, value in run.items()
                if key not in {"repo", "pr_number", "head_sha"}
            }
            folded_run.setdefault("calibration_manifest_id", manifest_id)
            apply_run_disposition_rules(folded_run, item)
            item.setdefault("reviewer_runs", []).append(folded_run)
    return merged
