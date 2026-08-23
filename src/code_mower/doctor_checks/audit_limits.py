"""Doctor checks for audit budget and diff limits."""

from __future__ import annotations

import statistics
import subprocess
from typing import Any, Mapping

from code_mower import audit_limits as code_mower_audit_limits

from .github_api import _github_api_list
from .models import STATUS_PASS, STATUS_WARN, DoctorCheck
from .privacy import auth_probe_output_detail

DEFAULT_PR_DIFF_SAMPLE_LIMIT = 20


def check_effective_audit_limits(config: Mapping[str, Any]) -> DoctorCheck:
    settings = code_mower_audit_limits.audit_limits_from_config(config)
    return DoctorCheck(
        name="config.audit_limits",
        status=STATUS_PASS,
        message=(
            "audit limits: "
            f"budget={settings.budget_description}; "
            f"max_diff_bytes={settings.max_diff_bytes}; "
            f"max_diff_hard_limit_bytes={settings.max_diff_hard_limit_bytes}"
        ),
        detail={
            "budget_usd": settings.budget_usd,
            "budget_description": settings.budget_description,
            "max_diff_bytes": settings.max_diff_bytes,
            "max_diff_hard_limit_bytes": settings.max_diff_hard_limit_bytes,
        },
    )


def _pull_request_diff_bytes(
    *,
    gh_path: str,
    slug: str,
    number: int,
    http_timeout: int,
) -> tuple[int | None, dict[str, Any]]:
    command = [
        gh_path,
        "api",
        f"repos/{slug}/pulls/{number}",
        "-H",
        "Accept: application/vnd.github.v3.diff",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=http_timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, {"error_type": exc.__class__.__name__}
    output = completed.stdout or completed.stderr or b""
    detail: dict[str, Any] = {
        "returncode": completed.returncode,
        **auth_probe_output_detail(output.decode("utf-8", errors="replace")),
    }
    if completed.returncode != 0:
        return None, detail
    return len(completed.stdout or b""), detail


def check_recent_pr_diff_median(
    *,
    gh_path: str,
    slug: str,
    hard_limit_bytes: int,
    http_timeout: int,
    sample_limit: int = DEFAULT_PR_DIFF_SAMPLE_LIMIT,
) -> DoctorCheck:
    bounded_limit = max(1, min(sample_limit, 50))
    pulls, pulls_detail = _github_api_list(
        gh_path,
        f"repos/{slug}/pulls?state=all&sort=updated&direction=desc&per_page={bounded_limit}",
        http_timeout=http_timeout,
    )
    if pulls is None:
        return DoctorCheck(
            name="github.audit_diff_median",
            status=STATUS_WARN,
            message=f"could not sample recent PR diff sizes for {slug}",
            detail={"repo": slug, **pulls_detail},
            remediation=(
                "Verify gh auth can read pull requests, then rerun "
                "`code-mower doctor --github`."
            ),
        )

    numbers: list[int] = []
    for item in pulls:
        if not isinstance(item, Mapping):
            continue
        raw_number = item.get("number")
        if isinstance(raw_number, bool):
            continue
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            continue
        if number > 0:
            numbers.append(number)

    diff_bytes: list[int] = []
    failed_numbers: list[int] = []
    for number in numbers:
        size, _detail = _pull_request_diff_bytes(
            gh_path=gh_path,
            slug=slug,
            number=number,
            http_timeout=http_timeout,
        )
        if size is None:
            failed_numbers.append(number)
            continue
        diff_bytes.append(size)

    if not numbers:
        return DoctorCheck(
            name="github.audit_diff_median",
            status=STATUS_PASS,
            message=f"{slug} has no recent PRs to sample for diff limits",
            detail={
                "repo": slug,
                "sampled_prs": 0,
                "hard_limit_bytes": hard_limit_bytes,
            },
        )
    if not diff_bytes:
        return DoctorCheck(
            name="github.audit_diff_median",
            status=STATUS_WARN,
            message=f"could not read any sampled PR diffs for {slug}",
            detail={
                "repo": slug,
                "sampled_prs": 0,
                "attempted_prs": numbers,
                "failed_prs": failed_numbers,
                "hard_limit_bytes": hard_limit_bytes,
            },
            remediation=(
                "Verify gh auth can read pull request diffs, then rerun "
                "`code-mower doctor --github`."
            ),
        )

    median_bytes = int(statistics.median(diff_bytes))
    detail = {
        "repo": slug,
        "sampled_prs": len(diff_bytes),
        "attempted_prs": numbers,
        "failed_prs": failed_numbers,
        "median_diff_bytes": median_bytes,
        "max_sampled_diff_bytes": max(diff_bytes),
        "hard_limit_bytes": hard_limit_bytes,
    }
    if median_bytes > hard_limit_bytes:
        return DoctorCheck(
            name="github.audit_diff_median",
            status=STATUS_WARN,
            message=(
                f"{slug} median sampled PR diff is {median_bytes} bytes, "
                f"above audit hard limit {hard_limit_bytes} bytes"
            ),
            detail=detail,
            remediation=(
                "Raise audit.max_diff_hard_limit_bytes in code-mower.yml or "
                "split unusually large PRs before relying on local audit gates."
            ),
        )
    return DoctorCheck(
        name="github.audit_diff_median",
        status=STATUS_PASS,
        message=(
            f"{slug} median sampled PR diff is {median_bytes} bytes, "
            f"within audit hard limit {hard_limit_bytes} bytes"
        ),
        detail=detail,
    )
