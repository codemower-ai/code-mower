"""Reviewer evidence report building for calibration corpora."""

from __future__ import annotations

from typing import Any, Mapping

from .evidence import USEFUL_EVIDENCE_DISPOSITIONS
from .run_results import normalize_disposition
from .run_status import (
    RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_INFRA_ERROR,
    RUN_STATUS_PASS,
    normalize_run_status_category,
)
from .truth import truth_for_item


def _reviewer_id(record: Mapping[str, Any]) -> str:
    reviewer = str(
        record.get("reviewer")
        or record.get("profile_id")
        or record.get("lane")
        or "unknown-reviewer"
    ).strip()
    return reviewer or "unknown-reviewer"


def build_reviewer_evidence_report(corpus: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize adjudicated reviewer evidence embedded in a calibration corpus."""

    profile_counts: dict[str, dict[str, int]] = {}
    profile_runs: dict[str, set[tuple[Any, ...]]] = {}
    profile_durations: dict[str, float] = {}
    profile_clean_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_blocking_false_positive_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_known_blocked_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_known_blocked_caught_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_known_blocked_missed_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_audit_input_insufficient_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_infra_error_run_keys: dict[str, set[tuple[Any, ...]]] = {}
    profile_run_statuses: dict[str, dict[str, int]] = {}
    profile_review_classes: dict[str, set[str]] = {}
    profile_context_packs: dict[str, set[str]] = {}
    profile_useful_review_classes: dict[str, set[str]] = {}
    profile_useful_context_packs: dict[str, set[str]] = {}
    models: dict[str, str] = {}
    findings: list[dict[str, Any]] = []
    run_dispositions: list[dict[str, Any]] = []
    run_records: list[dict[str, Any]] = []

    for item in corpus.get("corpus", []) or []:
        if not isinstance(item, Mapping):
            continue
        repo = str(item.get("repo") or "")
        pr_number = int(item.get("pr_number") or 0)
        head_sha = str(item.get("head_sha") or "")
        review_class = str(item.get("review_class") or "general")
        context_packs = [
            str(pack)
            for pack in item.get("context_packs", []) or []
            if str(pack).strip()
        ]
        truth = truth_for_item(item)
        run_key = (repo, pr_number, head_sha)

        for index, evidence in enumerate(item.get("reviewer_evidence", []) or []):
            if not isinstance(evidence, Mapping):
                continue
            reviewer = _reviewer_id(evidence)
            disposition = normalize_disposition(evidence.get("disposition"))
            profile_review_classes.setdefault(reviewer, set()).add(review_class)
            profile_context_packs.setdefault(reviewer, set()).update(context_packs)
            if disposition in USEFUL_EVIDENCE_DISPOSITIONS:
                profile_useful_review_classes.setdefault(reviewer, set()).add(review_class)
                profile_useful_context_packs.setdefault(reviewer, set()).update(context_packs)
            profile_counts.setdefault(reviewer, {})
            profile_counts[reviewer][disposition] = (
                profile_counts[reviewer].get(disposition, 0) + 1
            )
            profile_runs.setdefault(reviewer, set()).add(run_key)
            if evidence.get("duration_seconds") is not None:
                try:
                    profile_durations[reviewer] = profile_durations.get(
                        reviewer, 0.0
                    ) + float(evidence.get("duration_seconds"))
                except (TypeError, ValueError):
                    pass
            if evidence.get("model"):
                models[reviewer] = str(evidence.get("model"))
            findings.append(
                {
                    "profile_id": reviewer,
                    "repo": repo,
                    "pr_number": pr_number,
                    "head_sha": head_sha,
                    "difficulty": str(item.get("difficulty") or "unknown"),
                    "review_class": review_class,
                    "context_packs": context_packs,
                    "source": str(item.get("source") or ""),
                    "evidence_index": index,
                    "disposition": disposition,
                    "path": str(evidence.get("path") or ""),
                    "severity": str(evidence.get("severity") or ""),
                    "text": str(evidence.get("summary") or evidence.get("text") or ""),
                }
            )

        for run_index, run in enumerate(item.get("reviewer_runs", []) or []):
            if not isinstance(run, Mapping):
                continue
            reviewer = _reviewer_id(run)
            profile_review_classes.setdefault(reviewer, set()).add(review_class)
            profile_context_packs.setdefault(reviewer, set()).update(context_packs)
            run_identity_parts = [
                str(run.get("calibration_manifest_id") or "").strip(),
                str(
                    run.get("calibration_run_id")
                    or run.get("run_id")
                    or run.get("replicate")
                    or ""
                ).strip(),
            ]
            run_identity = "::".join(part for part in run_identity_parts if part)
            reviewer_run_key: tuple[Any, ...] = (
                (*run_key, run_identity) if run_identity else run_key
            )
            profile_runs.setdefault(reviewer, set()).add(reviewer_run_key)
            if run.get("duration_seconds") is not None:
                try:
                    profile_durations[reviewer] = profile_durations.get(
                        reviewer, 0.0
                    ) + float(run.get("duration_seconds"))
                except (TypeError, ValueError):
                    pass
            if run.get("model"):
                models[reviewer] = str(run.get("model"))
            run_disposition = normalize_disposition(
                run.get("disposition")
                or (
                    run.get("adjudication", {}).get("disposition")
                    if isinstance(run.get("adjudication"), Mapping)
                    else None
                )
            )
            status = str(run.get("status") or run.get("verdict") or "unknown").strip().lower()
            status_category = normalize_run_status_category(status)
            profile_run_statuses.setdefault(reviewer, {})
            profile_run_statuses[reviewer][status] = (
                profile_run_statuses[reviewer].get(status, 0) + 1
            )
            known_clean = bool(run.get("known_clean") or truth.get("known_clean"))
            known_blocked = bool(run.get("known_blocked") or truth.get("known_blocked"))
            try:
                finding_count = int(run.get("finding_count") or 0)
            except (TypeError, ValueError):
                finding_count = 0
            try:
                expected_finding_matches = int(run.get("expected_finding_matches") or 0)
            except (TypeError, ValueError):
                expected_finding_matches = 0
            if (
                known_clean
                and finding_count == 0
                and status_category == RUN_STATUS_PASS
            ):
                profile_clean_run_keys.setdefault(reviewer, set()).add(reviewer_run_key)
            if known_clean and status_category == RUN_STATUS_BLOCKED:
                profile_blocking_false_positive_run_keys.setdefault(
                    reviewer, set()
                ).add(reviewer_run_key)
            expected_blocker_caught = bool(
                run.get("expected_blocker_caught")
                or run.get("caught_expected_blocker")
                or (
                    run_disposition in USEFUL_EVIDENCE_DISPOSITIONS
                    and status_category == RUN_STATUS_BLOCKED
                )
                or expected_finding_matches > 0
            )
            evidence_disposition = run_disposition
            evidence_notes = str(
                run.get("disposition_notes")
                or (
                    run.get("adjudication", {}).get("notes")
                    if isinstance(run.get("adjudication"), Mapping)
                    else ""
                )
                or ""
            )
            if evidence_disposition == "unknown" and expected_finding_matches > 0:
                evidence_disposition = "true_positive"
                evidence_notes = evidence_notes or "Matched an expected calibration finding."
            if (
                evidence_disposition == "unknown"
                and known_clean
                and status_category == RUN_STATUS_BLOCKED
            ):
                evidence_disposition = "false_positive"
                evidence_notes = evidence_notes or "Blocked a known-clean calibration control."
            if evidence_disposition != "unknown":
                profile_counts.setdefault(reviewer, {})
                profile_counts[reviewer][evidence_disposition] = (
                    profile_counts[reviewer].get(evidence_disposition, 0) + 1
                )
                if evidence_disposition in USEFUL_EVIDENCE_DISPOSITIONS:
                    profile_useful_review_classes.setdefault(reviewer, set()).add(
                        review_class
                    )
                    profile_useful_context_packs.setdefault(reviewer, set()).update(
                        context_packs
                    )
                run_dispositions.append(
                    {
                        "profile_id": reviewer,
                        "repo": repo,
                        "pr_number": pr_number,
                        "head_sha": head_sha,
                        "difficulty": str(item.get("difficulty") or "unknown"),
                        "review_class": review_class,
                        "context_packs": context_packs,
                        "source": str(item.get("source") or ""),
                        "run_index": run_index,
                        "disposition": evidence_disposition,
                        "inferred": run_disposition == "unknown",
                        "notes": evidence_notes,
                    }
                )
            if known_blocked:
                profile_known_blocked_run_keys.setdefault(reviewer, set()).add(
                    reviewer_run_key
                )
                if expected_blocker_caught:
                    profile_known_blocked_caught_run_keys.setdefault(
                        reviewer, set()
                    ).add(reviewer_run_key)
                elif status_category in {RUN_STATUS_PASS, RUN_STATUS_BLOCKED}:
                    profile_known_blocked_missed_run_keys.setdefault(
                        reviewer, set()
                    ).add(reviewer_run_key)
            if status_category == RUN_STATUS_INFRA_ERROR:
                profile_infra_error_run_keys.setdefault(reviewer, set()).add(
                    reviewer_run_key
                )
            if status_category == RUN_STATUS_AUDIT_INPUT_INSUFFICIENT:
                profile_audit_input_insufficient_run_keys.setdefault(
                    reviewer, set()
                ).add(reviewer_run_key)
            run_records.append(
                {
                    "profile_id": reviewer,
                    "repo": repo,
                    "pr_number": pr_number,
                    "head_sha": head_sha,
                    "difficulty": str(item.get("difficulty") or "unknown"),
                    "review_class": review_class,
                    "context_packs": context_packs,
                    "source": str(item.get("source") or ""),
                    "run_index": run_index,
                    "status": status,
                    "status_category": status_category,
                    "known_clean": known_clean,
                    "known_blocked": known_blocked,
                    "finding_count": finding_count,
                    "expected_finding_matches": expected_finding_matches,
                    "expected_blocker_caught": expected_blocker_caught,
                    "disposition": evidence_disposition,
                    "duration_seconds": run.get("duration_seconds"),
                    "parse_status": str(run.get("parse_status") or ""),
                    "result_category": str(run.get("result_category") or ""),
                    "audit_input_insufficient_count": int(
                        run.get("audit_input_insufficient_count") or 0
                    ),
                    "artifact": str(run.get("artifact") or ""),
                    "calibration_run_id": run_identity,
                }
            )

    profiles = {
        reviewer: {
            "model": models.get(reviewer, ""),
            "runs": len(profile_runs.get(reviewer, set())),
            "duration_seconds_total": round(profile_durations.get(reviewer, 0.0), 3),
            "dispositions": dict(sorted(counts.items())),
            "finding_count": sum(counts.values()),
            "known_clean_pass_runs": len(profile_clean_run_keys.get(reviewer, set())),
            "blocking_false_positive_runs": len(
                profile_blocking_false_positive_run_keys.get(reviewer, set())
            ),
            "known_blocked_runs": len(profile_known_blocked_run_keys.get(reviewer, set())),
            "known_blocked_caught_runs": len(
                profile_known_blocked_caught_run_keys.get(reviewer, set())
            ),
            "known_blocked_missed_runs": len(
                profile_known_blocked_missed_run_keys.get(reviewer, set())
            ),
            "infra_error_runs": len(profile_infra_error_run_keys.get(reviewer, set())),
            "audit_input_insufficient_runs": len(
                profile_audit_input_insufficient_run_keys.get(reviewer, set())
            ),
            "run_statuses": dict(sorted(profile_run_statuses.get(reviewer, {}).items())),
            "review_classes": sorted(profile_review_classes.get(reviewer, set())),
            "context_packs": sorted(profile_context_packs.get(reviewer, set())),
            "useful_review_classes": sorted(
                profile_useful_review_classes.get(reviewer, set())
            ),
            "useful_context_packs": sorted(
                profile_useful_context_packs.get(reviewer, set())
            ),
        }
        for reviewer in sorted(set(profile_counts) | set(profile_runs))
        for counts in [profile_counts.get(reviewer, {})]
    }
    return {
        "mode": "reviewer-evidence-calibration",
        "corpus_name": corpus.get("name", ""),
        "description": corpus.get("description", ""),
        "source_item_count": len(corpus.get("corpus", []) or []),
        "evidence_count": len(findings) + len(run_dispositions),
        "finding_evidence_count": len(findings),
        "run_disposition_count": len(run_dispositions),
        "sources": [str(corpus.get("name", "calibration-corpus"))],
        "profiles": profiles,
        "findings": findings,
        "run_dispositions": run_dispositions,
        "reviewer_runs": run_records,
        "caveat": (
            "This report summarizes historical adjudicated evidence embedded in "
            "the corpus. Use it to bootstrap calibration, then confirm with fresh "
            "blind runs before promoting lanes."
        ),
    }


def render_evidence_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower reviewer evidence",
        f"Corpus: {report.get('corpus_name', '')}",
        f"Adjudicated evidence: {report.get('evidence_count', 0)}",
        f"Finding evidence: {report.get('finding_evidence_count', 0)}",
        f"Run dispositions: {report.get('run_disposition_count', 0)}",
        "",
        "Profiles:",
    ]
    profiles = report.get("profiles", {})
    if isinstance(profiles, Mapping) and profiles:
        for profile_id, stats in profiles.items():
            if not isinstance(stats, Mapping):
                continue
            dispositions = stats.get("dispositions", {})
            lines.append(
                f"- {profile_id}: runs={stats.get('runs', 0)} "
                f"findings={stats.get('finding_count', 0)} "
                f"clean_passes={stats.get('known_clean_pass_runs', 0)} "
                f"dispositions={dispositions}"
            )
    else:
        lines.append("- none")
    lines.extend(["", f"Caveat: {report.get('caveat', '')}"])
    return "\n".join(lines) + "\n"
