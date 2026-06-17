"""Hosted-provider GitHub repository compatibility checks."""

from __future__ import annotations

from collections.abc import Sequence

from .common import DoctorCheck, STATUS_PASS, STATUS_WARN


def check_private_repo_provider_surface(
    *,
    private_repos: Sequence[str],
    unknown_visibility_repos: Sequence[str],
    selected_saas_or_hosted: Sequence[str],
) -> DoctorCheck | None:
    if private_repos and selected_saas_or_hosted:
        return DoctorCheck(
            name="github.provider.private_repo",
            status=STATUS_WARN,
            message=(
                "private repos selected with SaaS/hosted lanes: "
                + ", ".join(selected_saas_or_hosted)
            ),
            detail={
                "private_repo_count": len(private_repos),
                "lanes": list(selected_saas_or_hosted),
            },
            remediation=(
                "Install each provider's GitHub App for the selected private "
                "repositories, confirm plan support, and decide whether sending "
                "diffs/source to that provider is acceptable."
            ),
        )

    if unknown_visibility_repos and selected_saas_or_hosted:
        return DoctorCheck(
            name="github.provider.private_repo",
            status=STATUS_WARN,
            message=(
                "could not determine repository visibility for SaaS/hosted lanes: "
                + ", ".join(selected_saas_or_hosted)
            ),
            detail={
                "unknown_repo_count": len(unknown_visibility_repos),
                "lanes": list(selected_saas_or_hosted),
            },
            remediation=(
                "Verify gh auth can read repository metadata before deciding "
                "whether hosted provider apps need private-repo access."
            ),
        )

    if selected_saas_or_hosted:
        return DoctorCheck(
            name="github.provider.private_repo",
            status=STATUS_PASS,
            message="selected SaaS/hosted lanes have no private repos in config",
            detail={"lanes": list(selected_saas_or_hosted)},
        )

    return None
