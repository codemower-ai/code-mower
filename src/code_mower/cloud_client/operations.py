"""High-level CodeMower.com upload operations."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import code_mower_telemetry
from .bundle import MAX_EVENT_COUNT
from .doctor import run_cloud_doctor
from .dogfood import build_dogfood_dry_run_preview, build_dogfood_plan, default_dogfood_reports
from .endpoints import is_local_http_endpoint
from .errors import CloudBundleError
from .events import (
    build_dogfood_event,
    build_workflow_run_event,
    run_gh_run_list,
)
from .export import build_cloud_bundle
from .git_metadata import detect_repo_slug
from .setup import DEFAULT_INSTALL_ID_ENV, DEFAULT_TEAM_ID_ENV
from .upload import build_upload_payload, post_upload_payload


def dogfood_upload(
    *,
    repo_path: Path,
    output_dir: Path,
    reports: list[tuple[Path, str]],
    events: list[dict[str, Any]],
    repo_slug: str,
    team_id: str,
    install_id: str,
    source: str,
    endpoint: str,
    token_env: str,
    include_reports: bool,
    yes: bool,
    timeout: float,
) -> dict[str, Any]:
    repo_path = repo_path.expanduser().resolve()
    detected_repo_slug = repo_slug or detect_repo_slug(repo_path)
    if not detected_repo_slug:
        raise CloudBundleError(
            "unable to detect repo slug; pass --repo-slug OWNER/REPO"
        )
    resolved_team_id = team_id or os.environ.get(DEFAULT_TEAM_ID_ENV, "")
    resolved_install_id = install_id or os.environ.get(DEFAULT_INSTALL_ID_ENV, "")
    resolved_reports = reports or default_dogfood_reports(repo_path)
    dogfood_plan = build_dogfood_plan(
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        source=source,
        reports=resolved_reports,
        events=events,
    )
    all_events = [
        build_dogfood_event(
            repo_path=repo_path,
            plan=dogfood_plan,
        ),
        *events,
    ]
    export_result = build_cloud_bundle(
        reports=resolved_reports,
        events=all_events,
        output_dir=output_dir,
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        anonymous=False,
    )
    doctor_result = run_cloud_doctor(
        bundle_dir=output_dir,
        endpoint=endpoint,
        token_env=token_env,
        require_token=yes,
    )
    if doctor_result["failures"]:
        return {
            "mode": "cloud-dogfood",
            "status": "doctor_failed",
            "export": export_result,
            "doctor": doctor_result,
        }
    payload = build_upload_payload(
        bundle_dir=output_dir,
        include_reports=include_reports,
    )
    if not yes:
        return {
            "mode": "cloud-dogfood",
            "status": "dry_run",
            "export": export_result,
            "doctor": doctor_result,
            "upload": build_dogfood_dry_run_preview(endpoint=endpoint, payload=payload),
        }
    token = os.environ.get(token_env, "")
    if not token and not is_local_http_endpoint(endpoint):
        raise CloudBundleError(
            f"{token_env} is not set; refusing non-local upload without a token"
        )
    return {
        "mode": "cloud-dogfood",
        "status": "uploaded",
        "export": export_result,
        "doctor": doctor_result,
        "upload": post_upload_payload(
            payload=payload,
            endpoint=endpoint,
            token=token,
            timeout=timeout,
        ),
    }


def catch_up_upload(
    *,
    repo_path: Path,
    output_dir: Path,
    repo_slug: str,
    team_id: str,
    install_id: str,
    source: str,
    limit: int,
    endpoint: str,
    token_env: str,
    yes: bool,
    timeout: float,
    include_git_ref: bool,
) -> dict[str, Any]:
    if limit < 1 or limit > MAX_EVENT_COUNT:
        raise CloudBundleError(f"--limit must be between 1 and {MAX_EVENT_COUNT}")
    repo_path = repo_path.expanduser().resolve()
    detected_repo_slug = repo_slug or detect_repo_slug(repo_path)
    if not detected_repo_slug:
        raise CloudBundleError(
            "unable to detect repo slug; pass --repo-slug OWNER/REPO"
        )
    resolved_team_id = team_id or os.environ.get(DEFAULT_TEAM_ID_ENV, "")
    resolved_install_id = install_id or os.environ.get(DEFAULT_INSTALL_ID_ENV, "")
    runs = run_gh_run_list(
        repo_slug=detected_repo_slug,
        limit=limit,
        repo_path=repo_path,
    )
    events = [
        build_workflow_run_event(
            repo_slug=detected_repo_slug,
            team_id=resolved_team_id,
            install_id=resolved_install_id,
            source=source,
            run=run,
            include_git_ref=include_git_ref,
        )
        for run in runs
    ]
    export_result = build_cloud_bundle(
        reports=[],
        events=events,
        output_dir=output_dir,
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        anonymous=False,
    )
    doctor_result = run_cloud_doctor(
        bundle_dir=output_dir,
        endpoint=endpoint,
        token_env=token_env,
        require_token=yes,
    )
    if doctor_result["failures"]:
        return {
            "mode": "cloud-catch-up",
            "status": "doctor_failed",
            "repo_slug": detected_repo_slug,
            "run_count": len(runs),
            "export": export_result,
            "doctor": doctor_result,
        }
    payload = build_upload_payload(bundle_dir=output_dir, include_reports=False)
    if not yes:
        return {
            "mode": "cloud-catch-up",
            "status": "dry_run",
            "repo_slug": detected_repo_slug,
            "run_count": len(runs),
            "export": export_result,
            "doctor": doctor_result,
            "upload": build_dogfood_dry_run_preview(endpoint=endpoint, payload=payload),
        }
    token = os.environ.get(token_env, "")
    if not token and not is_local_http_endpoint(endpoint):
        raise CloudBundleError(
            f"{token_env} is not set; refusing non-local upload without a token"
        )
    return {
        "mode": "cloud-catch-up",
        "status": "uploaded",
        "repo_slug": detected_repo_slug,
        "run_count": len(runs),
        "export": export_result,
        "doctor": doctor_result,
        "upload": post_upload_payload(
            payload=payload,
            endpoint=endpoint,
            token=token,
            timeout=timeout,
        ),
    }


def reviewer_runs_upload(
    *,
    repo_path: Path,
    verdicts: Path,
    output_dir: Path,
    repo_slug: str,
    team_id: str,
    install_id: str,
    limit: int,
    endpoint: str,
    token_env: str,
    yes: bool,
    timeout: float,
    include_git_ref: bool,
) -> dict[str, Any]:
    if limit < 1 or limit > MAX_EVENT_COUNT:
        raise CloudBundleError(f"--limit must be between 1 and {MAX_EVENT_COUNT}")
    repo_path = repo_path.expanduser().resolve()
    detected_repo_slug = repo_slug or detect_repo_slug(repo_path)
    if not detected_repo_slug:
        raise CloudBundleError(
            "unable to detect repo slug; pass --repo-slug OWNER/REPO"
        )
    resolved_team_id = team_id or os.environ.get(DEFAULT_TEAM_ID_ENV, "")
    resolved_install_id = install_id or os.environ.get(DEFAULT_INSTALL_ID_ENV, "")
    try:
        events = code_mower_telemetry.export_reviewer_run_events_from_verdicts(
            verdicts,
            repo=detected_repo_slug,
            limit=limit,
            include_git_ref=include_git_ref,
        )
    except ValueError as exc:
        raise CloudBundleError(str(exc)) from exc
    if not events:
        return {
            "mode": "cloud-reviewer-runs",
            "status": "no_events",
            "repo_slug": detected_repo_slug,
            "event_count": 0,
            "verdicts": str(verdicts.expanduser()),
            "git_ref_included": include_git_ref,
        }
    export_result = build_cloud_bundle(
        reports=[],
        events=events,
        output_dir=output_dir,
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        anonymous=False,
    )
    doctor_result = run_cloud_doctor(
        bundle_dir=output_dir,
        endpoint=endpoint,
        token_env=token_env,
        require_token=yes,
    )
    if doctor_result["failures"]:
        return {
            "mode": "cloud-reviewer-runs",
            "status": "doctor_failed",
            "repo_slug": detected_repo_slug,
            "event_count": len(events),
            "export": export_result,
            "doctor": doctor_result,
        }
    payload = build_upload_payload(bundle_dir=output_dir, include_reports=False)
    if not yes:
        return {
            "mode": "cloud-reviewer-runs",
            "status": "dry_run",
            "repo_slug": detected_repo_slug,
            "event_count": len(events),
            "export": export_result,
            "doctor": doctor_result,
            "upload": build_dogfood_dry_run_preview(endpoint=endpoint, payload=payload),
        }
    token = os.environ.get(token_env, "")
    if not token and not is_local_http_endpoint(endpoint):
        raise CloudBundleError(
            f"{token_env} is not set; refusing non-local upload without a token"
        )
    return {
        "mode": "cloud-reviewer-runs",
        "status": "uploaded",
        "repo_slug": detected_repo_slug,
        "event_count": len(events),
        "export": export_result,
        "doctor": doctor_result,
        "upload": post_upload_payload(
            payload=payload,
            endpoint=endpoint,
            token=token,
            timeout=timeout,
        ),
    }


def parse_repo_sync_spec(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        return "", Path(spec)
    repo_slug, repo_path = spec.split("=", 1)
    repo_slug = repo_slug.strip()
    repo_path = repo_path.strip()
    if not repo_slug or not repo_path:
        raise CloudBundleError(
            "--repo entries must be PATH or OWNER/REPO=PATH for repo-sync"
        )
    return repo_slug, Path(repo_path)


def repo_sync_output_name(repo_slug: str, repo_path: Path, index: int) -> str:
    raw = repo_slug.replace("/", "__") if repo_slug else repo_path.name
    cleaned = "".join(
        ch.lower() if ch.isalnum() else "-" for ch in raw.strip()
    ).strip("-")
    return f"{cleaned or 'repo'}-{index + 1}"


def repo_sync_upload(
    *,
    repo_specs: list[str],
    output_dir: Path,
    modes: list[str],
    team_id: str,
    install_id: str,
    source_prefix: str,
    limit: int,
    endpoint: str,
    token_env: str,
    include_reports: bool,
    include_git_ref: bool,
    yes: bool,
    timeout: float,
) -> dict[str, Any]:
    selected_modes = modes or ["dogfood", "reviewer-runs"]
    repos: list[dict[str, Any]] = []
    error_count = 0
    step_statuses: list[str] = []

    for index, spec in enumerate(repo_specs):
        repo_slug, repo_path = parse_repo_sync_spec(spec)
        repo_path = repo_path.expanduser().resolve()
        repo_output_dir = output_dir / repo_sync_output_name(repo_slug, repo_path, index)
        repo_result: dict[str, Any] = {
            "repo_spec": spec,
            "repo_slug": repo_slug,
            "repo_path": str(repo_path),
            "steps": [],
        }

        for mode in selected_modes:
            try:
                if mode == "dogfood":
                    step_result = dogfood_upload(
                        repo_path=repo_path,
                        output_dir=repo_output_dir / "dogfood",
                        reports=[],
                        events=[],
                        repo_slug=repo_slug,
                        team_id=team_id,
                        install_id=install_id,
                        source=f"{source_prefix}-dogfood",
                        endpoint=endpoint,
                        token_env=token_env,
                        include_reports=include_reports,
                        yes=yes,
                        timeout=timeout,
                    )
                elif mode == "catch-up":
                    step_result = catch_up_upload(
                        repo_path=repo_path,
                        output_dir=repo_output_dir / "catch-up",
                        repo_slug=repo_slug,
                        team_id=team_id,
                        install_id=install_id,
                        source=f"{source_prefix}-catch-up",
                        limit=limit,
                        endpoint=endpoint,
                        token_env=token_env,
                        yes=yes,
                        timeout=timeout,
                        include_git_ref=include_git_ref,
                    )
                elif mode == "reviewer-runs":
                    step_result = reviewer_runs_upload(
                        repo_path=repo_path,
                        verdicts=code_mower_telemetry.default_verdict_artifact_dir(),
                        output_dir=repo_output_dir / "reviewer-runs",
                        repo_slug=repo_slug,
                        team_id=team_id,
                        install_id=install_id,
                        limit=limit,
                        endpoint=endpoint,
                        token_env=token_env,
                        yes=yes,
                        timeout=timeout,
                        include_git_ref=include_git_ref,
                    )
                else:  # pragma: no cover - argparse constrains modes.
                    raise CloudBundleError(f"unsupported repo-sync mode: {mode}")
            except CloudBundleError as exc:
                step_result = {
                    "mode": f"cloud-{mode}",
                    "status": "error",
                    "error": str(exc),
                }
            repo_result["repo_slug"] = repo_result["repo_slug"] or str(
                step_result.get("repo_slug") or ""
            )
            repo_result["steps"].append(step_result)
            step_status = str(step_result.get("status") or "")
            step_statuses.append(step_status)
            if step_result.get("status") in {"error", "doctor_failed"}:
                error_count += 1

        repos.append(repo_result)

    status = "dry_run" if not yes else "uploaded"
    if error_count:
        status = "partial"
    elif yes and not any(step_status == "uploaded" for step_status in step_statuses):
        status = "no_events"
    return {
        "mode": "cloud-repo-sync",
        "status": status,
        "repo_count": len(repos),
        "step_count": sum(len(repo["steps"]) for repo in repos),
        "error_count": error_count,
        "modes": selected_modes,
        "repos": repos,
    }
