"""High-level CodeMower.com upload operations."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .. import code_mower_telemetry
from .. import reviewer_spend
from .bundle import MAX_EVENT_COUNT
from .doctor import run_cloud_doctor
from .dogfood import build_dogfood_dry_run_preview, build_dogfood_plan, default_dogfood_reports
from .endpoints import is_local_http_endpoint
from .errors import CloudBundleError
from .events import (
    build_dogfood_event,
    build_provider_catalog_snapshot_events,
    build_workflow_run_event,
    run_gh_run_list,
)
from .export import build_cloud_bundle
from .git_metadata import detect_repo_slug
from .setup import DEFAULT_INSTALL_ID_ENV, DEFAULT_TEAM_ID_ENV
from .upload import build_upload_payload, post_upload_payload


CATCH_UP_TRUST_GUIDANCE = {
    "use_for": "historical activity context and dashboard coverage backfill",
    "do_not_use_for": "reviewer or lens accuracy, lane promotion, or merge-gate policy",
    "next_step": (
        "run current dogfood uploads plus reviewer-runs or calibration evidence "
        "before making provider/lens decisions"
    ),
}


def build_catch_up_summary(
    *,
    repo_slug: str,
    runs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    requested_limit: int,
    include_git_ref: bool,
) -> dict[str, Any]:
    """Summarize imported GitHub Actions history without exposing ref details."""

    def increment(target: dict[str, int], raw: object) -> None:
        value = str(raw or "").strip() or "unknown"
        target[value] = target.get(value, 0) + 1

    workflow_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    conclusion_counts: dict[str, int] = {}
    created_values: list[str] = []
    updated_values: list[str] = []

    for run in runs:
        increment(workflow_counts, run.get("name"))
        increment(status_counts, run.get("status"))
        increment(conclusion_counts, run.get("conclusion"))
        created_at = str(run.get("createdAt") or "").strip()
        updated_at = str(run.get("updatedAt") or "").strip()
        if created_at:
            created_values.append(created_at)
        if updated_at:
            updated_values.append(updated_at)

    return {
        "repo_slug": repo_slug,
        "requested_limit": requested_limit,
        "run_count": len(runs),
        "event_count": len(events),
        "provenance": "imported_history",
        "source_category": "history",
        "history_only": True,
        "calibration_evidence": False,
        "trust_guidance": CATCH_UP_TRUST_GUIDANCE,
        "git_ref_included": include_git_ref,
        "workflow_counts": dict(sorted(workflow_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "conclusion_counts": dict(sorted(conclusion_counts.items())),
        "oldest_run_at": min(created_values) if created_values else "",
        "newest_run_at": max(created_values) if created_values else "",
        "last_updated_at": max(updated_values) if updated_values else "",
    }


def _reviewer_spend_events(
    *,
    repo_path: Path,
    spend_path: Path | None,
    repo_slug: str,
    team_id: str,
    install_id: str,
    source: str,
) -> list[dict[str, Any]]:
    resolved_spend_path = spend_path or repo_path / reviewer_spend.DEFAULT_SPEND_PATH
    if not resolved_spend_path.is_file():
        return []
    return reviewer_spend.spend_runs_to_events(
        reviewer_spend.load_spend_file(resolved_spend_path),
        repo_slug=repo_slug,
        team_id=team_id,
        install_id=install_id,
        source=source,
    )


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _event_match_values(event: Mapping[str, Any]) -> tuple[str, str, str, str]:
    dimensions = _mapping(event.get("dimensions"))
    lane = str(
        dimensions.get("audit_comment_lane_id")
        or dimensions.get("lane_id")
        or dimensions.get("lane")
        or event.get("lens")
        or event.get("provider")
        or ""
    ).strip()
    return (
        str(event.get("repo_slug") or "").strip(),
        str(dimensions.get("pr_number") or "").strip(),
        lane,
        str(dimensions.get("head_sha") or "").strip(),
    )


def _merge_spend_metrics(
    event: dict[str, Any],
    spend_event: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(event)
    metrics = dict(_mapping(event.get("metrics")))
    for key, value in _mapping(spend_event.get("metrics")).items():
        metrics.setdefault(key, value)
    dimensions = dict(_mapping(event.get("dimensions")))
    spend_dimensions = _mapping(spend_event.get("dimensions"))
    if spend_dimensions.get("spend_run_id"):
        dimensions["spend_run_id"] = spend_dimensions["spend_run_id"]
    if spend_event.get("source"):
        dimensions["spend_source"] = spend_event["source"]
    merged["metrics"] = metrics
    merged["dimensions"] = dimensions
    if not _mapping(event.get("tool")) and isinstance(spend_event.get("tool"), Mapping):
        merged["tool"] = dict(spend_event["tool"])
    return merged


def _merge_reviewer_spend_events(
    events: list[dict[str, Any]],
    spend_events: list[dict[str, Any]],
    *,
    include_unmatched_spend: bool = False,
) -> list[dict[str, Any]]:
    if not spend_events:
        return events
    merged = list(events)
    exact_matches: dict[tuple[str, str, str, str], int] = {}
    fallback_matches: dict[tuple[str, str, str], int | None] = {}
    for index, event in enumerate(merged):
        repo_slug, pr_number, lane, head_sha = _event_match_values(event)
        if not repo_slug or not pr_number or not lane:
            continue
        if head_sha:
            exact_matches[(repo_slug, pr_number, lane, head_sha)] = index
        fallback_key = (repo_slug, pr_number, lane)
        fallback_matches[fallback_key] = (
            None if fallback_key in fallback_matches else index
        )

    unmatched: list[dict[str, Any]] = []
    merged_targets: set[int] = set()
    for spend_event in spend_events:
        repo_slug, pr_number, lane, head_sha = _event_match_values(spend_event)
        target_index = None
        if head_sha:
            target_index = exact_matches.get((repo_slug, pr_number, lane, head_sha))
        if target_index is None:
            target_index = fallback_matches.get((repo_slug, pr_number, lane))
        if target_index is None or target_index in merged_targets:
            unmatched.append(spend_event)
            continue
        merged_targets.add(target_index)
        merged[target_index] = _merge_spend_metrics(merged[target_index], spend_event)
    if not include_unmatched_spend:
        return merged
    remaining = max(0, MAX_EVENT_COUNT - len(merged))
    return [*merged, *unmatched[:remaining]]


def dogfood_upload(
    *,
    repo_path: Path,
    output_dir: Path,
    reports: list[tuple[Path, str]],
    events: list[dict[str, Any]],
    spend_path: Path | None,
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
    spend_events = _reviewer_spend_events(
        repo_path=repo_path,
        spend_path=spend_path,
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        source="code-mower cloud dogfood spend",
    )
    dogfood_plan = build_dogfood_plan(
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        source=source,
        reports=resolved_reports,
        events=[*events, *spend_events],
    )
    provider_catalog_events = build_provider_catalog_snapshot_events(
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        source=source,
        include_version_probe=True,
    )
    all_events = [
        build_dogfood_event(
            repo_path=repo_path,
            plan=dogfood_plan,
        ),
        *provider_catalog_events,
        *spend_events,
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
    catch_up_summary = build_catch_up_summary(
        repo_slug=detected_repo_slug,
        runs=runs,
        events=events,
        requested_limit=limit,
        include_git_ref=include_git_ref,
    )
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
            "catch_up": catch_up_summary,
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
            "catch_up": catch_up_summary,
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
        "catch_up": catch_up_summary,
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
    offset: int = 0,
    endpoint: str,
    token_env: str,
    yes: bool,
    timeout: float,
    include_git_ref: bool,
    spend_path: Path | None = None,
    include_unmatched_spend: bool = False,
) -> dict[str, Any]:
    if limit < 1 or limit > MAX_EVENT_COUNT:
        raise CloudBundleError(f"--limit must be between 1 and {MAX_EVENT_COUNT}")
    if offset < 0:
        raise CloudBundleError("--offset must be non-negative")
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
            offset=offset,
            include_git_ref=include_git_ref,
        )
    except ValueError as exc:
        raise CloudBundleError(str(exc)) from exc
    spend_events = _reviewer_spend_events(
        repo_path=repo_path,
        spend_path=spend_path,
        repo_slug=detected_repo_slug,
        team_id=resolved_team_id,
        install_id=resolved_install_id,
        source="code-mower cloud reviewer-runs spend",
    )
    events = _merge_reviewer_spend_events(
        events,
        spend_events,
        include_unmatched_spend=include_unmatched_spend,
    )
    if not events:
        return {
            "mode": "cloud-reviewer-runs",
            "status": "no_events",
            "repo_slug": detected_repo_slug,
            "event_count": 0,
            "verdicts": str(verdicts.expanduser()),
            "git_ref_included": include_git_ref,
            "offset": offset,
            "include_unmatched_spend": include_unmatched_spend,
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
            "offset": offset,
            "include_unmatched_spend": include_unmatched_spend,
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
            "offset": offset,
            "include_unmatched_spend": include_unmatched_spend,
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
        "offset": offset,
        "include_unmatched_spend": include_unmatched_spend,
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


def build_repo_sync_data_class_summary(
    repos: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Summarize synced data by dashboard provenance class."""

    summary: dict[str, dict[str, Any]] = {
        "current_dogfood": {
            "steps": 0,
            "events": 0,
            "description": "current repo metadata and provider inventory",
        },
        "imported_history": {
            "steps": 0,
            "events": 0,
            "description": "sanitized GitHub Actions history",
            "trust_guidance": CATCH_UP_TRUST_GUIDANCE,
        },
        "reviewer_evidence": {
            "steps": 0,
            "events": 0,
            "description": "metadata-only reviewer verdict artifacts",
        },
    }
    for repo in repos:
        steps = repo.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            mode = str(step.get("mode") or "")
            if mode == "cloud-dogfood":
                target = summary["current_dogfood"]
                target["steps"] += 1
                export = step.get("export")
                if isinstance(export, dict):
                    target["events"] += int(export.get("event_count") or 0)
            elif mode == "cloud-catch-up":
                target = summary["imported_history"]
                target["steps"] += 1
                catch_up = step.get("catch_up")
                if isinstance(catch_up, dict):
                    target["events"] += int(catch_up.get("event_count") or 0)
                else:
                    target["events"] += int(step.get("run_count") or 0)
            elif mode == "cloud-reviewer-runs":
                target = summary["reviewer_evidence"]
                target["steps"] += 1
                target["events"] += int(step.get("event_count") or 0)
    return summary


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
                        spend_path=None,
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
                        spend_path=None,
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
        "data_class_summary": build_repo_sync_data_class_summary(repos),
        "repos": repos,
    }
