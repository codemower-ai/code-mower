"""GitHub branch protection doctor checks."""

from __future__ import annotations

import urllib.parse
from typing import Mapping

from .common import DoctorCheck, STATUS_PASS, STATUS_WARN
from .github_api import _github_api_json


def check_branch_protection(
    *,
    gh_path: str,
    slug: str,
    default_branch: str,
    http_timeout: int,
) -> DoctorCheck:
    encoded_branch = urllib.parse.quote(default_branch, safe="")
    protection_payload, protection_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/branches/{encoded_branch}/protection",
        http_timeout=http_timeout,
    )
    if protection_payload is None:
        return DoctorCheck(
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

    required_checks = protection_payload.get("required_status_checks")
    contexts: list[str] = []
    if isinstance(required_checks, Mapping):
        raw_contexts = required_checks.get("contexts")
        if isinstance(raw_contexts, list):
            contexts = [str(item) for item in raw_contexts]
    return DoctorCheck(
        name="github.branch_protection",
        status=STATUS_PASS,
        message=f"{slug}@{default_branch} branch protection is inspectable",
        detail={
            "repo": slug,
            "default_branch": default_branch,
            "required_status_check_count": len(contexts),
        },
    )
