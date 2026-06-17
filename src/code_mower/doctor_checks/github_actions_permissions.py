"""GitHub Actions enablement doctor checks."""

from __future__ import annotations

from .common import DoctorCheck, STATUS_PASS, STATUS_WARN
from .github_api import _github_api_json


def check_actions_permissions(
    *,
    gh_path: str,
    slug: str,
    http_timeout: int,
) -> DoctorCheck:
    actions_payload, actions_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/actions/permissions",
        http_timeout=http_timeout,
    )
    if actions_payload is None:
        return DoctorCheck(
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

    enabled = bool(actions_payload.get("enabled"))
    return DoctorCheck(
        name="github.actions.permissions",
        status=STATUS_PASS if enabled else STATUS_WARN,
        message=(
            f"{slug} GitHub Actions are enabled and inspectable"
            if enabled
            else f"{slug} GitHub Actions appear disabled"
        ),
        detail={
            "repo": slug,
            "enabled": enabled,
            "allowed_actions": str(actions_payload.get("allowed_actions") or ""),
        },
        remediation=(
            None
            if enabled
            else (
                "Enable GitHub Actions for this repository before expecting "
                "Code Mower labelers or audit workflows to run."
            )
        ),
    )
