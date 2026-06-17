"""First-user readiness scorecards for package-install rehearsals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

FIRST_USER_ARTIFACTS = (
    ("calibration_plan", ".code-mower/calibration-plan.json"),
    ("draft_calibration_corpus", ".code-mower/draft-calibration-corpus.json"),
    ("draft_reviewer_value_report", ".code-mower/draft-reviewer-value-report.md"),
    ("calibration_evidence", "calibration-evidence.json"),
    ("reviewer_metrics", "reviewer-metrics.json"),
    ("lane_policy", "lane-policy.json"),
    ("reviewer_value_report", "reviewer-value-report.md"),
    ("cloud_export", "cloud-export.json"),
    ("cloud_upload_dry_run", "cloud-upload-dry-run.json"),
    ("cloud_dogfood_dry_run", "cloud-dogfood-dry-run.json"),
)
PRIVACY_EXCLUDED_CONTENT = frozenset(
    {
        "source_code",
        "raw_diffs",
        "raw_model_transcripts",
        "raw_stdout_stderr",
        "auth_probe_output",
        "secrets",
    }
)


def first_user_artifacts(toy_repo: Path) -> dict[str, str]:
    return {key: str(toy_repo / relative_path) for key, relative_path in FIRST_USER_ARTIFACTS}


def _read_json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _readiness_check(
    *,
    check_id: str,
    title: str,
    status: str,
    evidence: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check: dict[str, Any] = {
        "id": check_id,
        "title": title,
        "status": status,
        "evidence": evidence,
    }
    if detail:
        check["detail"] = detail
    return check


def _artifact_exists_check(
    *,
    check_id: str,
    title: str,
    path: Path,
    min_bytes: int = 1,
) -> dict[str, Any]:
    exists = path.is_file()
    size = path.stat().st_size if exists else 0
    return _readiness_check(
        check_id=check_id,
        title=title,
        status="pass" if exists and size >= min_bytes else "fail",
        evidence=str(path),
        detail={"exists": exists, "bytes": size},
    )


def _step_succeeded(steps: Sequence[dict[str, Any]], *needles: str) -> bool:
    for step in steps:
        if step.get("returncode") != 0:
            continue
        command = " ".join(str(part) for part in step.get("command", ()))
        if all(needle in command for needle in needles):
            return True
    return False


def _cloud_dry_run_check(
    *,
    check_id: str,
    title: str,
    path: Path,
    upload_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = upload_payload if upload_payload is not None else _read_json_file(path)
    upload = payload.get("upload") if isinstance(payload, dict) else None
    if not isinstance(upload, dict):
        upload = payload if isinstance(payload, dict) else {}
    excluded = set(upload.get("excluded_content") or ())
    missing_exclusions = sorted(PRIVACY_EXCLUDED_CONTENT - excluded)
    detail = {
        "mode": upload.get("mode"),
        "privacy_mode": upload.get("privacy_mode"),
        "requires_yes": upload.get("requires_yes"),
        "would_upload": upload.get("would_upload"),
        "missing_exclusions": missing_exclusions,
    }
    passes = (
        path.is_file()
        and upload.get("mode") == "cloud-upload-dry-run"
        and upload.get("privacy_mode") == "metadata_and_reports"
        and upload.get("requires_yes") is True
        and upload.get("would_upload") is False
        and not missing_exclusions
    )
    return _readiness_check(
        check_id=check_id,
        title=title,
        status="pass" if passes else "fail",
        evidence=str(path),
        detail=detail,
    )


def first_user_readiness_scorecard(
    *,
    toy_repo: Path,
    outputs: Path,
    version: str,
    steps: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    artifacts = first_user_artifacts(toy_repo)
    generated_dir = toy_repo / ".code-mower.generated"
    cloud_upload_path = Path(artifacts["cloud_upload_dry_run"])
    dogfood_path = Path(artifacts["cloud_dogfood_dry_run"])
    cloud_export_payload = _read_json_file(Path(artifacts["cloud_export"]))
    dogfood_payload = _read_json_file(dogfood_path)
    dogfood_upload = dogfood_payload.get("upload") if isinstance(dogfood_payload, dict) else None

    checks = [
        _readiness_check(
            check_id="package-installed",
            title="Package installs and exposes the CLI",
            status="pass" if version.startswith("code-mower ") else "fail",
            evidence=version,
        ),
        _readiness_check(
            check_id="easy-init-generated",
            title="Easy-mode setup writes reviewable generated files",
            status=(
                "pass"
                if (
                    (generated_dir / "code-mower-init-plan.json").is_file()
                    and (generated_dir / "smoke-tests.sh").is_file()
                    and (generated_dir / "tools" / "code_mower").is_file()
                )
                else "fail"
            ),
            evidence=str(generated_dir),
        ),
        _readiness_check(
            check_id="doctor-ran",
            title="First-run doctor completes",
            status="pass" if _step_succeeded(steps, "doctor", "--easy") else "fail",
            evidence="code-mower doctor --easy --json",
        ),
        _artifact_exists_check(
            check_id="draft-calibration-corpus",
            title="Auto-discovery creates a reviewable draft corpus",
            path=Path(artifacts["draft_calibration_corpus"]),
        ),
        _artifact_exists_check(
            check_id="draft-value-report",
            title="Draft corpus can produce a reviewer value report",
            path=Path(artifacts["draft_reviewer_value_report"]),
        ),
        _artifact_exists_check(
            check_id="starter-value-report",
            title="Starter corpus can produce the first value report",
            path=Path(artifacts["reviewer_value_report"]),
        ),
        _readiness_check(
            check_id="cloud-export-metadata-bundle",
            title="Cloud export creates a metadata/report bundle",
            status=(
                "pass"
                if (
                    isinstance(cloud_export_payload, dict)
                    and cloud_export_payload.get("mode") == "cloud-export"
                    and len(cloud_export_payload.get("included_reports") or ()) >= 3
                    and cloud_export_payload.get("upload_ready") is False
                )
                else "fail"
            ),
            evidence=artifacts["cloud_export"],
        ),
        _cloud_dry_run_check(
            check_id="cloud-upload-dry-run-privacy",
            title="Cloud upload stays dry-run and excludes private content",
            path=cloud_upload_path,
        ),
        _readiness_check(
            check_id="cloud-dogfood-dry-run",
            title="CodeMower.com dogfood path stays dry-run by default",
            status=(
                "pass"
                if (
                    isinstance(dogfood_payload, dict) and dogfood_payload.get("status") == "dry_run"
                )
                else "fail"
            ),
            evidence=str(dogfood_path),
        ),
        _cloud_dry_run_check(
            check_id="cloud-dogfood-upload-privacy",
            title="Dogfood upload preview excludes private content",
            path=dogfood_path,
            upload_payload=dogfood_upload,
        ),
    ]
    passed = sum(1 for check in checks if check["status"] == "pass")
    failed = sum(1 for check in checks if check["status"] == "fail")
    warnings = sum(1 for check in checks if check["status"] == "warn")
    scorecard = {
        "mode": "code-mower-first-user-readiness",
        "status": "pass" if failed == 0 else "fail",
        "passed": passed,
        "failed": failed,
        "warnings": warnings,
        "total": len(checks),
        "checks": checks,
        "artifact": str(outputs / "first-user-readiness.json"),
    }
    return scorecard
