"""GitHub auth, repository, and Actions cost diagnostics."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .common import (
    ACTIONS_BILLING_BLOCK_PATTERNS,
    ACTIONS_COST_SAMPLE_DEFAULT,
    ACTIONS_COST_SAMPLE_MAX,
    ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS,
    ACTIONS_METADATA_WORKFLOW_MARKERS,
    MAX_ACTIONS_FAILED_JOBS_TO_INSPECT,
    MAX_ACTIONS_FAILED_RUNS_TO_INSPECT,
    DoctorCheck,
    STATUS_PASS,
    STATUS_WARN,
    as_sequence,
)
from .runtime import auth_probe_output_detail


def _configured_repositories(config: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    repos: list[Mapping[str, Any]] = []
    for repo in as_sequence(config.get("repositories", [])):
        if isinstance(repo, Mapping) and repo.get("slug"):
            repos.append(repo)
    return tuple(repos)


def _github_api_payload(
    gh_path: str,
    endpoint: str,
    *,
    http_timeout: int,
) -> tuple[Any | None, dict[str, Any]]:
    try:
        completed = subprocess.run(
            [gh_path, "api", endpoint],
            capture_output=True,
            text=True,
            check=False,
            timeout=http_timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, {"error_type": exc.__class__.__name__}
    output = (completed.stdout or completed.stderr or "").strip()
    detail: dict[str, Any] = {
        "endpoint": endpoint,
        "returncode": completed.returncode,
        **auth_probe_output_detail(output),
    }
    if completed.returncode != 0:
        return None, detail
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        detail["parse_error"] = "json"
        return None, detail
    return payload, detail


def _github_api_json(
    gh_path: str,
    endpoint: str,
    *,
    http_timeout: int,
) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    payload, detail = _github_api_payload(
        gh_path,
        endpoint,
        http_timeout=http_timeout,
    )
    if payload is None:
        return None, detail
    if not isinstance(payload, Mapping):
        detail["parse_error"] = "not_object"
        return None, detail
    return payload, detail


def _github_api_list(
    gh_path: str,
    endpoint: str,
    *,
    http_timeout: int,
) -> tuple[list[Any] | None, dict[str, Any]]:
    payload, detail = _github_api_payload(
        gh_path,
        endpoint,
        http_timeout=http_timeout,
    )
    if payload is None:
        return None, detail
    if not isinstance(payload, list):
        detail["parse_error"] = "not_list"
        return None, detail
    return payload, detail


def _selected_saas_or_hosted_lanes(
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
) -> list[str]:
    selected: list[str] = []
    for lane_id, lane in lanes:
        if str(lane.get("driver", "")) in {"saas_event", "hosted_bridge"}:
            selected.append(lane_id)
    return selected


def _annotation_mentions_actions_billing_block(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in ACTIONS_BILLING_BLOCK_PATTERNS)


def _check_run_id_from_actions_job(job: Mapping[str, Any]) -> Any | None:
    check_run_url = str(job.get("check_run_url") or "")
    match = re.search(r"/check-runs/([0-9]+)$", check_run_url)
    if match:
        return match.group(1)
    return None


def _check_recent_actions_billing_blocks(
    *,
    gh_path: str,
    slug: str,
    http_timeout: int,
) -> DoctorCheck:
    runs_payload, runs_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/actions/runs?per_page=10",
        http_timeout=http_timeout,
    )
    if runs_payload is None:
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"could not inspect recent GitHub Actions runs for {slug}",
            detail={"repo": slug, **runs_detail},
            remediation=(
                "Verify gh auth can read Actions run metadata, then rerun "
                "`code-mower doctor --github`."
            ),
        )

    raw_runs = runs_payload.get("workflow_runs")
    if not isinstance(raw_runs, list):
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"recent Actions response for {slug} did not include workflow_runs",
            detail={"repo": slug},
        )

    inspected_runs = 0
    inspected_jobs = 0
    incomplete_inspections: list[dict[str, Any]] = []
    for run in raw_runs:
        if (
            not isinstance(run, Mapping)
            or run.get("conclusion") not in ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS
        ):
            continue
        if inspected_runs >= MAX_ACTIONS_FAILED_RUNS_TO_INSPECT:
            incomplete_inspections.append(
                {
                    "stage": "runs",
                    "reason": "inspection_limit_reached",
                    "limit": MAX_ACTIONS_FAILED_RUNS_TO_INSPECT,
                }
            )
            break
        run_id = run.get("id")
        if run_id is None:
            continue
        inspected_runs += 1
        jobs_payload, _jobs_detail = _github_api_json(
            gh_path,
            f"repos/{slug}/actions/runs/{run_id}/jobs?per_page=20",
            http_timeout=http_timeout,
        )
        if jobs_payload is None:
            incomplete_inspections.append(
                {
                    "run_id": run_id,
                    "workflow": str(run.get("name") or ""),
                    "stage": "jobs",
                }
            )
            continue
        raw_jobs = jobs_payload.get("jobs")
        if not isinstance(raw_jobs, list):
            incomplete_inspections.append(
                {
                    "run_id": run_id,
                    "workflow": str(run.get("name") or ""),
                    "stage": "jobs",
                    "reason": "missing_jobs",
                }
            )
            continue
        if not raw_jobs:
            incomplete_inspections.append(
                {
                    "run_id": run_id,
                    "workflow": str(run.get("name") or ""),
                    "stage": "jobs",
                    "reason": "no_jobs",
                }
            )
            continue
        for job in raw_jobs:
            if (
                not isinstance(job, Mapping)
                or job.get("conclusion") not in ACTIONS_INSPECTABLE_FAILURE_CONCLUSIONS
            ):
                continue
            if inspected_jobs >= MAX_ACTIONS_FAILED_JOBS_TO_INSPECT:
                incomplete_inspections.append(
                    {
                        "run_id": run_id,
                        "workflow": str(run.get("name") or ""),
                        "stage": "annotations",
                        "reason": "inspection_limit_reached",
                        "limit": MAX_ACTIONS_FAILED_JOBS_TO_INSPECT,
                    }
                )
                break
            job_id = job.get("id")
            if job_id is None:
                continue
            check_run_id = _check_run_id_from_actions_job(job)
            if check_run_id is None:
                incomplete_inspections.append(
                    {
                        "run_id": run_id,
                        "workflow": str(run.get("name") or ""),
                        "job": str(job.get("name") or ""),
                        "stage": "annotations",
                        "reason": "missing_check_run_id",
                    }
                )
                continue
            inspected_jobs += 1
            annotations, _annotations_detail = _github_api_list(
                gh_path,
                f"repos/{slug}/check-runs/{check_run_id}/annotations?per_page=20",
                http_timeout=http_timeout,
            )
            if annotations is None:
                incomplete_inspections.append(
                    {
                        "run_id": run_id,
                        "workflow": str(run.get("name") or ""),
                        "job_id": job_id,
                        "job": str(job.get("name") or ""),
                        "stage": "annotations",
                    }
                )
                continue
            for annotation in annotations:
                if not isinstance(annotation, Mapping):
                    continue
                message = str(annotation.get("message") or "")
                if _annotation_mentions_actions_billing_block(message):
                    return DoctorCheck(
                        name="github.actions.recent_failures",
                        status=STATUS_WARN,
                        message=f"{slug} has recent Actions jobs blocked by billing or spending limits",
                        detail={
                            "repo": slug,
                            "billing_block_count": 1,
                            "billing_blocks": [
                                {
                                    "run_id": run_id,
                                    "workflow": str(run.get("name") or ""),
                                    "head_sha": str(run.get("head_sha") or ""),
                                    "job_id": job_id,
                                    "check_run_id": str(check_run_id),
                                    "job": str(job.get("name") or ""),
                                }
                            ],
                            "inspected_failed_runs": inspected_runs,
                            "inspected_failed_jobs": inspected_jobs,
                        },
                        remediation=(
                            "Fix GitHub billing or Actions spending limits, then rerun failed "
                            "workflows before relying on branch protection or deploy checks."
                        ),
                    )

    if incomplete_inspections:
        return DoctorCheck(
            name="github.actions.recent_failures",
            status=STATUS_WARN,
            message=f"{slug} has recent failed Actions runs that doctor could not fully inspect",
            detail={
                "repo": slug,
                "incomplete_inspection_count": len(incomplete_inspections),
                "incomplete_inspections": incomplete_inspections[:5],
                "inspected_failed_runs": inspected_runs,
                "inspected_failed_jobs": inspected_jobs,
            },
            remediation=(
                "Verify gh auth can read workflow jobs and annotations, or inspect "
                "recent failed Actions runs manually before treating doctor as a "
                "green setup signal."
            ),
        )

    return DoctorCheck(
        name="github.actions.recent_failures",
        status=STATUS_PASS,
        message=f"{slug} has no recent Actions billing-block annotations",
        detail={
            "repo": slug,
            "inspected_failed_runs": inspected_runs,
            "inspected_failed_jobs": inspected_jobs,
        },
    )


def _parse_github_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


def _approx_run_seconds(run: Mapping[str, Any]) -> float:
    started = _parse_github_timestamp(
        run.get("run_started_at") or run.get("created_at")
    )
    updated = _parse_github_timestamp(run.get("updated_at"))
    if started is None or updated is None or updated < started:
        return 0.0
    return (updated - started).total_seconds()


def _is_metadata_workflow(name: str, path: str) -> bool:
    haystack = f"{name}\n{path}".lower()
    return any(marker in haystack for marker in ACTIONS_METADATA_WORKFLOW_MARKERS)


def _check_actions_cost_sample(
    *,
    gh_path: str,
    slug: str,
    private: bool,
    http_timeout: int,
    sample_limit: int,
) -> DoctorCheck:
    bounded_limit = max(1, min(sample_limit, ACTIONS_COST_SAMPLE_MAX))
    runs_payload, runs_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/actions/runs?per_page={bounded_limit}",
        http_timeout=http_timeout,
    )
    if runs_payload is None:
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_WARN,
            message=f"could not sample recent GitHub Actions usage for {slug}",
            detail={"repo": slug, **runs_detail},
            remediation=(
                "Verify gh auth can read Actions run metadata. Private repos "
                "should periodically inspect Actions usage metrics after enabling "
                "hosted or informational lanes."
            ),
        )

    raw_runs = runs_payload.get("workflow_runs")
    if not isinstance(raw_runs, list):
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_WARN,
            message=f"Actions usage response for {slug} did not include workflow_runs",
            detail={"repo": slug},
        )

    workflow_counts: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    workflow_seconds: defaultdict[str, float] = defaultdict(float)
    metadata_counts: Counter[str] = Counter()
    metadata_seconds: defaultdict[str, float] = defaultdict(float)
    schedule_runs = 0
    total_seconds = 0.0

    for run in raw_runs:
        if not isinstance(run, Mapping):
            continue
        workflow_name = str(run.get("name") or run.get("display_title") or "unknown")
        workflow_path = str(run.get("path") or "")
        event = str(run.get("event") or "unknown")
        seconds = _approx_run_seconds(run)
        workflow_counts[workflow_name] += 1
        event_counts[event] += 1
        workflow_seconds[workflow_name] += seconds
        total_seconds += seconds
        if event == "schedule":
            schedule_runs += 1
        if _is_metadata_workflow(workflow_name, workflow_path):
            metadata_counts[workflow_name] += 1
            metadata_seconds[workflow_name] += seconds

    sample_size = sum(workflow_counts.values())
    metadata_run_count = sum(metadata_counts.values())
    metadata_share = (metadata_run_count / sample_size) if sample_size else 0.0
    top_workflows = [
        {
            "workflow": workflow,
            "runs": count,
            "approx_minutes": round(workflow_seconds[workflow] / 60, 2),
        }
        for workflow, count in workflow_counts.most_common(10)
    ]
    top_metadata_workflows = [
        {
            "workflow": workflow,
            "runs": count,
            "approx_minutes": round(metadata_seconds[workflow] / 60, 2),
        }
        for workflow, count in metadata_counts.most_common(10)
    ]
    detail = {
        "repo": slug,
        "private": private,
        "sample_limit": bounded_limit,
        "sampled_runs": sample_size,
        "approx_total_minutes": round(total_seconds / 60, 2),
        "metadata_workflow_runs": metadata_run_count,
        "metadata_workflow_share": round(metadata_share, 3),
        "schedule_runs": schedule_runs,
        "top_workflows": top_workflows,
        "top_metadata_workflows": top_metadata_workflows,
        "events": [
            {"event": event, "runs": count}
            for event, count in event_counts.most_common(10)
        ],
    }
    if not sample_size:
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_PASS,
            message=f"{slug} has no recent Actions runs in the sampled window",
            detail=detail,
        )

    noisy_metadata = private and metadata_run_count >= max(5, int(sample_size * 0.2))
    scheduled_private = private and schedule_runs > 0
    if noisy_metadata or scheduled_private:
        reasons: list[str] = []
        if noisy_metadata:
            reasons.append(
                f"{metadata_run_count}/{sample_size} sampled runs look like metadata or reviewer labeler workflows"
            )
        if scheduled_private:
            reasons.append(f"{schedule_runs} sampled runs were scheduled")
        return DoctorCheck(
            name="github.actions.cost_sample",
            status=STATUS_WARN,
            message=f"{slug} private-repo Actions sample suggests avoidable spend: {'; '.join(reasons)}",
            detail=detail,
            remediation=(
                "Prefer label, trusted-comment, or workflow_dispatch triggers for "
                "optional hosted reviewers; add job-level if guards before checkout; "
                "keep informational lanes out of branch protection."
            ),
        )

    return DoctorCheck(
        name="github.actions.cost_sample",
        status=STATUS_PASS,
        message=f"{slug} recent Actions sample has no obvious private-repo cost traps",
        detail=detail,
    )


def check_github_setup(
    *,
    config: Mapping[str, Any],
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
    http_timeout: int,
    actions_cost_sample: int = ACTIONS_COST_SAMPLE_DEFAULT,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    gh_path = shutil.which("gh")
    if not gh_path:
        return [
            DoctorCheck(
                name="github.cli",
                status=STATUS_WARN,
                message="GitHub setup checks require the gh CLI",
                detail={"gh_path": ""},
                remediation=(
                    "Install GitHub CLI, run `gh auth login`, then rerun "
                    "`code-mower doctor --github`."
                ),
            )
        ]

    checks.append(
        DoctorCheck(
            name="github.cli",
            status=STATUS_PASS,
            message="gh found for GitHub setup checks",
            detail={"gh_path": gh_path},
        )
    )

    repos = _configured_repositories(config)
    if not repos:
        checks.append(
            DoctorCheck(
                name="github.repositories",
                status=STATUS_WARN,
                message="config declares no repositories for GitHub setup checks",
                remediation=(
                    "Add repositories[].slug entries to code-mower.yml before "
                    "running GitHub-backed audits."
                ),
            )
        )
        return checks

    selected_saas_or_hosted = _selected_saas_or_hosted_lanes(lanes)
    private_repos: list[str] = []
    unknown_visibility_repos: list[str] = []
    for repo in repos:
        slug = str(repo.get("slug") or "")
        configured_default_branch = str(repo.get("default_branch") or "main")
        repo_payload, repo_detail = _github_api_json(
            gh_path,
            f"repos/{slug}",
            http_timeout=http_timeout,
        )
        if repo_payload is None:
            unknown_visibility_repos.append(slug)
            checks.append(
                DoctorCheck(
                    name="github.repo.metadata",
                    status=STATUS_WARN,
                    message=f"could not read GitHub repository metadata for {slug}",
                    detail={"repo": slug, **repo_detail},
                    remediation=(
                        "Verify gh auth can read this repo. Private repos need "
                        "a token or GitHub App installation with repository access."
                    ),
                )
            )
            continue

        is_private = bool(repo_payload.get("private"))
        default_branch = str(
            repo_payload.get("default_branch") or configured_default_branch or "main"
        )
        if is_private:
            private_repos.append(slug)
        checks.append(
            DoctorCheck(
                name="github.repo.metadata",
                status=STATUS_PASS,
                message=(
                    f"{slug} is reachable "
                    f"({'private' if is_private else 'public'} repository)"
                ),
                detail={
                    "repo": slug,
                    "private": is_private,
                    "visibility": str(repo_payload.get("visibility") or ""),
                    "default_branch": str(repo_payload.get("default_branch") or ""),
                    "archived": bool(repo_payload.get("archived")),
                    "fork": bool(repo_payload.get("fork")),
                },
            )
        )

        permissions = repo_payload.get("permissions")
        if isinstance(permissions, Mapping):
            write_like = any(
                bool(permissions.get(name))
                for name in ("admin", "maintain", "push", "triage")
            )
            checks.append(
                DoctorCheck(
                    name="github.repo.permissions",
                    status=STATUS_PASS if write_like else STATUS_WARN,
                    message=(
                        f"{slug} token has repository write-adjacent permission"
                        if write_like
                        else f"{slug} token appears read-only for repository metadata"
                    ),
                    detail={
                        "repo": slug,
                        "admin": bool(permissions.get("admin")),
                        "maintain": bool(permissions.get("maintain")),
                        "push": bool(permissions.get("push")),
                        "triage": bool(permissions.get("triage")),
                        "pull": bool(permissions.get("pull")),
                    },
                    remediation=(
                        None
                        if write_like
                        else (
                            "Configure a fine-grained PAT or GitHub App token with "
                            "Issues read/write and Pull requests read before expecting "
                            "Code Mower to apply labels or comments."
                        )
                    ),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="github.repo.permissions",
                    status=STATUS_WARN,
                    message=f"{slug} metadata did not include token permissions",
                    detail={"repo": slug},
                    remediation=(
                        "If label writes fail, configure the lane token secrets "
                        "documented by the provider matrix."
                    ),
                )
            )

        actions_payload, actions_detail = _github_api_json(
            gh_path,
            f"repos/{slug}/actions/permissions",
            http_timeout=http_timeout,
        )
        if actions_payload is None:
            checks.append(
                DoctorCheck(
                    name="github.actions.permissions",
                    status=STATUS_WARN,
                    message=f"could not inspect GitHub Actions permissions for {slug}",
                    detail={"repo": slug, **actions_detail},
                    remediation=(
                        "A repo admin should verify Actions are enabled and workflow "
                        "token permissions can write issues/labels or that PAT "
                        "fallback secrets are configured."
                    ),
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    name="github.actions.permissions",
                    status=(
                        STATUS_PASS
                        if bool(actions_payload.get("enabled"))
                        else STATUS_WARN
                    ),
                    message=(
                        f"{slug} GitHub Actions are enabled and inspectable"
                        if bool(actions_payload.get("enabled"))
                        else f"{slug} GitHub Actions appear disabled"
                    ),
                    detail={
                        "repo": slug,
                        "enabled": bool(actions_payload.get("enabled")),
                        "allowed_actions": str(actions_payload.get("allowed_actions") or ""),
                    },
                    remediation=(
                        None
                        if bool(actions_payload.get("enabled"))
                        else (
                            "Enable GitHub Actions for this repository before "
                            "expecting Code Mower labelers or audit workflows to run."
                        )
                    ),
                )
            )

        checks.append(
            _check_recent_actions_billing_blocks(
                gh_path=gh_path,
                slug=slug,
                http_timeout=http_timeout,
            )
        )
        checks.append(
            _check_actions_cost_sample(
                gh_path=gh_path,
                slug=slug,
                private=is_private,
                http_timeout=http_timeout,
                sample_limit=actions_cost_sample,
            )
        )

        encoded_branch = urllib.parse.quote(default_branch, safe="")
        protection_payload, protection_detail = _github_api_json(
            gh_path,
            f"repos/{slug}/branches/{encoded_branch}/protection",
            http_timeout=http_timeout,
        )
        if protection_payload is None:
            checks.append(
                DoctorCheck(
                    name="github.branch_protection",
                    status=STATUS_WARN,
                    message=f"could not confirm branch protection for {slug}@{default_branch}",
                    detail={
                        "repo": slug,
                        "default_branch": default_branch,
                        **protection_detail,
                    },
                    remediation=(
                        "Before enabling autonomous merge, protect the default branch "
                        "and make required checks explicit."
                    ),
                )
            )
        else:
            required_checks = protection_payload.get("required_status_checks")
            contexts: list[str] = []
            if isinstance(required_checks, Mapping):
                raw_contexts = required_checks.get("contexts")
                if isinstance(raw_contexts, list):
                    contexts = [str(item) for item in raw_contexts]
            checks.append(
                DoctorCheck(
                    name="github.branch_protection",
                    status=STATUS_PASS,
                    message=f"{slug}@{default_branch} branch protection is inspectable",
                    detail={
                        "repo": slug,
                        "default_branch": default_branch,
                        "required_status_check_count": len(contexts),
                    },
                )
            )

    if private_repos and selected_saas_or_hosted:
        checks.append(
            DoctorCheck(
                name="github.provider.private_repo",
                status=STATUS_WARN,
                message=(
                    "private repos selected with SaaS/hosted lanes: "
                    + ", ".join(selected_saas_or_hosted)
                ),
                detail={
                    "private_repo_count": len(private_repos),
                    "lanes": selected_saas_or_hosted,
                },
                remediation=(
                    "Install each provider's GitHub App for the selected private "
                    "repositories, confirm plan support, and decide whether sending "
                    "diffs/source to that provider is acceptable."
                ),
            )
        )
    elif unknown_visibility_repos and selected_saas_or_hosted:
        checks.append(
            DoctorCheck(
                name="github.provider.private_repo",
                status=STATUS_WARN,
                message=(
                    "could not determine repository visibility for SaaS/hosted lanes: "
                    + ", ".join(selected_saas_or_hosted)
                ),
                detail={
                    "unknown_repo_count": len(unknown_visibility_repos),
                    "lanes": selected_saas_or_hosted,
                },
                remediation=(
                    "Verify gh auth can read repository metadata before deciding "
                    "whether hosted provider apps need private-repo access."
                ),
            )
        )
    elif selected_saas_or_hosted:
        checks.append(
            DoctorCheck(
                name="github.provider.private_repo",
                status=STATUS_PASS,
                message="selected SaaS/hosted lanes have no private repos in config",
                detail={"lanes": selected_saas_or_hosted},
            )
        )

    return checks
