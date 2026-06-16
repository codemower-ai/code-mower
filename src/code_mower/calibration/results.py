"""Calibration reviewer result normalization."""

from __future__ import annotations

from typing import Any, Mapping

from .evidence import NON_BLOCKING_CODERABBIT_SEVERITIES
from .run_status import (
    RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_INFRA_ERROR,
    RUN_STATUS_PASS,
    count_normalized_findings,
    normalize_run_status_category,
    status_from_verdict,
)
from .truth import expected_finding_matches, truth_for_item


AUDIT_INPUT_INSUFFICIENT_PATTERNS = (
    "audit input incomplete",
    "audit input is incomplete",
    "audit input was incomplete",
    "diff is incomplete",
    "diff was incomplete",
    "diff was truncated",
    "diff truncation",
    "incomplete diff",
    "incomplete review context",
    "insufficient audit input",
    "review context is incomplete",
    "truncated diff",
)


def _finding_is_audit_input_insufficient(finding: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(finding.get(key) or "").strip().lower()
        for key in ("summary", "text", "message", "body", "title", "detail")
    )
    return any(pattern in text for pattern in AUDIT_INPUT_INSUFFICIENT_PATTERNS)


def audit_input_insufficient_result(findings: Any) -> bool:
    if not isinstance(findings, list):
        return False
    blockers = [
        finding
        for finding in findings
        if isinstance(finding, Mapping)
        and str(finding.get("severity") or "").strip().upper() in {"P0", "P1", "P2"}
    ]
    return bool(blockers) and all(
        _finding_is_audit_input_insufficient(finding) for finding in blockers
    )


def coderabbit_blocking_findings(findings: Any) -> list[Mapping[str, Any]]:
    if not isinstance(findings, list):
        return []
    blocking: list[Mapping[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        severity = str(finding.get("severity") or "").strip().lower()
        if severity in NON_BLOCKING_CODERABBIT_SEVERITIES:
            continue
        blocking.append(finding)
    return blocking


def local_llm_findings(run: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for finding_group in run.get("blocker_findings", []) or []:
        if not isinstance(finding_group, Mapping):
            continue
        path = str(finding_group.get("path") or "")
        for text in finding_group.get("blockers", []) or []:
            findings.append({"path": path, "text": str(text)})
    for text in run.get("pr_level_blockers", []) or []:
        findings.append({"path": "__pr__", "text": str(text)})
    for finding_group in run.get("concern_findings", []) or []:
        if not isinstance(finding_group, Mapping):
            continue
        path = str(finding_group.get("path") or "")
        for text in finding_group.get("concerns", []) or []:
            findings.append({"path": path, "text": str(text)})
    return findings


def observed_head_field(observed_head_sha: str, head_sha: str) -> dict[str, str]:
    if observed_head_sha and observed_head_sha != head_sha:
        return {"observed_head_sha": observed_head_sha}
    return {}


def run_records_from_summary(
    *,
    summary: Mapping[str, Any],
    item: Mapping[str, Any],
    command_result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    artifact = str(command_result.get("summary_path") or "")
    returncode = command_result.get("returncode")
    try:
        returncode_int = int(returncode) if returncode is not None else None
    except (TypeError, ValueError):
        returncode_int = None
    mode = str(summary.get("mode") or "")
    repo = str(summary.get("repo") or item.get("repo") or "")
    pr_number = int(summary.get("pr_number") or item.get("pr_number") or 0)
    head_sha = str(item.get("head_sha") or "")
    observed_head_sha = str(summary.get("head_sha") or "")
    truth = truth_for_item(item)
    known_clean = bool(truth.get("known_clean"))
    known_blocked = bool(truth.get("known_blocked"))
    expected_findings = item.get("expected_findings", [])
    calibration_run_id = str(command_result.get("run_id") or "")
    replicate = command_result.get("replicate")

    def expected_matches(status: str, finding_count: int, findings: Any = None) -> int:
        if not known_blocked or status != RUN_STATUS_BLOCKED or finding_count <= 0:
            return 0
        if expected_findings:
            return expected_finding_matches(expected_findings, findings)
        return 1

    if mode in {"antigravity-cli-audit", "gemini-cli-audit", "hermes-cli-audit"}:
        default_reviewer = (
            "antigravity-cli"
            if mode == "antigravity-cli-audit"
            else "hermes-cli"
            if mode == "hermes-cli-audit"
            else "gemini-cli"
        )
        reviewer_id = str(command_result.get("reviewer_id") or default_reviewer)
        verdict = summary.get("verdict", {})
        if not isinstance(verdict, Mapping):
            verdict = {}
        findings = verdict.get("findings", [])
        parse_failed = bool(verdict.get("parse_failed"))
        parse_status = "parse_failed" if parse_failed else "json"
        result_category = normalize_run_status_category(verdict.get("result_category"))
        audit_input_insufficient = (
            result_category == RUN_STATUS_AUDIT_INPUT_INSUFFICIENT
            or audit_input_insufficient_result(findings)
        )
        if parse_failed:
            status = RUN_STATUS_INFRA_ERROR
        elif audit_input_insufficient:
            status = RUN_STATUS_AUDIT_INPUT_INSUFFICIENT
        else:
            status = status_from_verdict(verdict.get("verdict"), returncode=returncode_int)
        finding_count = count_normalized_findings(findings)
        return [
            {
                "reviewer": reviewer_id,
                "status": status,
                "finding_count": finding_count,
                "expected_finding_matches": expected_matches(status, finding_count, findings),
                "result_category": status if audit_input_insufficient else "",
                "audit_input_insufficient_count": 1 if audit_input_insufficient else 0,
                "known_clean": known_clean,
                "known_blocked": known_blocked,
                "duration_seconds": summary.get("duration_seconds"),
                "parse_status": parse_status,
                "artifact": artifact,
                "model": summary.get("model") or "",
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                **observed_head_field(observed_head_sha, head_sha),
                "calibration_run_id": calibration_run_id,
                "replicate": replicate,
            }
        ]
    if mode == "coderabbit-cli-audit":
        reviewer_id = str(command_result.get("reviewer_id") or "coderabbit-cli")
        try:
            raw_finding_count = int(summary.get("finding_count") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid coderabbit finding_count") from exc
        findings = summary.get("findings", [])
        if isinstance(findings, list) and findings:
            parsed_finding_count = count_normalized_findings(findings)
            blocking_findings = coderabbit_blocking_findings(findings)
            if raw_finding_count > parsed_finding_count:
                finding_count = raw_finding_count
            else:
                finding_count = len(blocking_findings)
        else:
            parsed_finding_count = 0
            blocking_findings = []
            finding_count = raw_finding_count
        parse_status = str(summary.get("parse_status") or "").strip().lower()
        head_check = summary.get("head_check", {})
        head_check_status = (
            str(head_check.get("status") or "")
            if isinstance(head_check, Mapping)
            else ""
        )
        if parse_status in {"raw", "parse_failed"}:
            status = RUN_STATUS_INFRA_ERROR
        elif head_check_status and head_check_status != "pass":
            status = RUN_STATUS_INFRA_ERROR
        elif returncode_int not in {None, 0} and not finding_count:
            status = RUN_STATUS_INFRA_ERROR
        else:
            status = RUN_STATUS_BLOCKED if finding_count else RUN_STATUS_PASS
        record = {
            "reviewer": reviewer_id,
            "status": status,
            "finding_count": finding_count,
            "expected_finding_matches": expected_matches(
                status,
                finding_count,
                blocking_findings,
            ),
            "known_clean": known_clean,
            "known_blocked": known_blocked,
            "duration_seconds": summary.get("duration_seconds"),
            "parse_status": parse_status,
            "artifact": artifact,
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            **observed_head_field(observed_head_sha, head_sha),
            "calibration_run_id": calibration_run_id,
            "replicate": replicate,
        }
        if raw_finding_count != finding_count:
            record["raw_finding_count"] = raw_finding_count
        if parsed_finding_count != raw_finding_count:
            record["parsed_finding_count"] = parsed_finding_count
        if raw_finding_count > parsed_finding_count:
            record["unparsed_finding_count"] = raw_finding_count - parsed_finding_count
        non_blocking_finding_count = parsed_finding_count - len(blocking_findings)
        if non_blocking_finding_count:
            record["non_blocking_finding_count"] = non_blocking_finding_count
        return [record]
    if mode == "local-llm-bakeoff":
        records: list[dict[str, Any]] = []
        for run in summary.get("runs", []) or []:
            if not isinstance(run, Mapping):
                continue
            finding_count = (
                int(run.get("blocker_file_count") or 0)
                + int(run.get("concern_file_count") or 0)
                + len(run.get("pr_level_blockers", []) or [])
            )
            findings = local_llm_findings(run)
            status = status_from_verdict(run.get("verdict"), returncode=returncode_int)
            records.append(
                {
                    "reviewer": str(run.get("profile_id") or "local-llm"),
                    "status": status,
                    "finding_count": finding_count,
                    "expected_finding_matches": expected_matches(
                        status,
                        finding_count,
                        findings,
                    ),
                    "known_clean": known_clean,
                    "known_blocked": known_blocked,
                    "duration_seconds": run.get("duration_seconds"),
                    "parse_status": (
                        "parse_failed"
                        if int(run.get("parse_failure_count") or 0)
                        else "json"
                    ),
                    "artifact": artifact,
                    "model": str(run.get("model") or ""),
                    "repo": str(run.get("repo") or repo),
                    "pr_number": int(run.get("pr_number") or pr_number),
                    "head_sha": head_sha,
                    **observed_head_field(
                        str(run.get("head_sha_end") or observed_head_sha),
                        head_sha,
                    ),
                    "calibration_run_id": calibration_run_id,
                    "replicate": replicate,
                }
            )
        return records
    return []


def infra_run_record(
    *,
    lane_id: str,
    item: Mapping[str, Any],
    status: str,
    duration_seconds: float,
    artifact: str,
) -> dict[str, Any]:
    reviewer = lane_id
    if reviewer == "local-llm":
        reviewer = "local-llm"
    truth = truth_for_item(item)
    return {
        "reviewer": reviewer,
        "status": status,
        "finding_count": 0,
        "expected_finding_matches": 0,
        "known_clean": bool(truth.get("known_clean")),
        "known_blocked": bool(truth.get("known_blocked")),
        "duration_seconds": round(duration_seconds, 3),
        "parse_status": status,
        "artifact": artifact,
        "repo": str(item.get("repo") or ""),
        "pr_number": int(item.get("pr_number") or 0),
        "head_sha": str(item.get("head_sha") or ""),
        "calibration_run_id": str(item.get("calibration_run_id") or ""),
        "replicate": item.get("replicate"),
    }
