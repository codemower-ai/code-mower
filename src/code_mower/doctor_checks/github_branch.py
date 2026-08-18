"""GitHub branch protection doctor checks."""

from __future__ import annotations

import urllib.parse
from typing import Mapping

from .common import DoctorCheck, STATUS_PASS, STATUS_WARN
from .github_api import _github_api_json

GITHUB_ACTIONS_APP_ID = 15368


def check_branch_protection(
    *,
    gh_path: str,
    slug: str,
    default_branch: str,
    http_timeout: int,
    required_status_context: str | None = None,
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
    check_bindings: list[dict[str, object]] = []
    if isinstance(required_checks, Mapping):
        raw_contexts = required_checks.get("contexts")
        if isinstance(raw_contexts, list):
            contexts.extend(str(item) for item in raw_contexts)
        raw_checks = required_checks.get("checks")
        if isinstance(raw_checks, list):
            for check in raw_checks:
                if isinstance(check, Mapping) and check.get("context"):
                    context = str(check["context"])
                    contexts.append(context)
                    check_bindings.append(
                        {"context": context, "app_id": check.get("app_id")}
                    )
    contexts = list(dict.fromkeys(contexts))
    wrong_gate_bindings = [
        binding
        for binding in check_bindings
        if binding.get("context") == required_status_context
        and binding.get("app_id") == GITHUB_ACTIONS_APP_ID
    ]
    if required_status_context and wrong_gate_bindings:
        return DoctorCheck(
            name="github.branch_protection",
            status=STATUS_WARN,
            message=(
                f"{slug}@{default_branch} requires {required_status_context} "
                "from GitHub Actions instead of Any source"
            ),
            detail={
                "repo": slug,
                "default_branch": default_branch,
                "required_status_context": required_status_context,
                "required_status_contexts": contexts,
                "required_status_check_count": len(contexts),
                "required_status_check_bindings": check_bindings,
            },
            remediation=(
                f"Rebind `{required_status_context}` in branch protection to "
                "Any source, not GitHub Actions. In the API response, the "
                f"`checks[]` entry for `{required_status_context}` should have "
                "`app_id: null`; `app_id: 15368` means GitHub is evaluating "
                "the Actions job check-run instead of the Code Mower commit status."
            ),
        )
    if required_status_context and required_status_context not in contexts:
        return DoctorCheck(
            name="github.branch_protection",
            status=STATUS_WARN,
            message=(
                f"{slug}@{default_branch} does not require {required_status_context}"
            ),
            detail={
                "repo": slug,
                "default_branch": default_branch,
                "required_status_context": required_status_context,
                "required_status_check_count": len(contexts),
                "required_status_contexts": contexts,
            },
            remediation=(
                "Require the generated Code Mower gate status before enabling "
                "unattended merge. Inspect existing checks with "
                f"`gh api repos/{slug}/branches/{encoded_branch}/protection/required_status_checks`, "
                "then PATCH that endpoint with all existing contexts plus "
                f"`{required_status_context}` from Any source."
            ),
        )
    return DoctorCheck(
        name="github.branch_protection",
        status=STATUS_PASS,
        message=(
            f"{slug}@{default_branch} requires {required_status_context}"
            if required_status_context
            else f"{slug}@{default_branch} branch protection is inspectable"
        ),
        detail={
            "repo": slug,
            "default_branch": default_branch,
            "required_status_check_count": len(contexts),
            "required_status_contexts": contexts,
            "required_status_check_bindings": check_bindings,
        },
    )
