"""Optional Devin readiness checks for the local CLI and hosted v3 postures."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from code_mower import config as code_mower_config
from code_mower.provider_capabilities import TRANSPORTS

from .models import DoctorCheck

# `code_mower.devin_readiness` reaches the Devin credential and campaign modules,
# which import this check package; resolve it per call to keep that acyclic.

__all__ = [
    "check_devin_readiness",
    "devin_effective_lane",
    "devin_readiness_selected",
]

DEVIN_REVIEW_LANES = frozenset(
    transport.review_lane
    for transport in TRANSPORTS.values()
    if transport.product == "devin"
)


def devin_readiness_selected(
    config: Mapping[str, Any] | None,
    *,
    lanes: tuple[str, ...] = (),
    profile: str | None = "recommended",
) -> str | None:
    """Return the selected Devin transport without failing an ordinary run."""
    from code_mower.devin_readiness import selected_devin_transport

    try:
        return selected_devin_transport(config, lanes=lanes, profile=profile)
    except code_mower_config.ConfigError:
        return None


def devin_effective_lane(
    effective_lanes: Iterable[tuple[str, Mapping[str, Any]]],
) -> Mapping[str, Any] | None:
    """Return the effective configuration of the selected Devin review lane."""
    for lane_id, effective in effective_lanes:
        if lane_id in DEVIN_REVIEW_LANES:
            return effective
    return None


def check_devin_readiness(
    *,
    config: Mapping[str, Any] | None,
    lanes: tuple[str, ...] = (),
    repo_slug: str = "",
    transport: str | None = None,
    include_unselected: bool = False,
    provider_credential_file: Path | None = None,
    provider_profile: str = "",
    provider_config_dir: Path | None = None,
    config_profile: str | None = "recommended",
    effective_lane: Mapping[str, Any] | None = None,
    adoption_posture: str = "reviewer-gate",
) -> list[DoctorCheck]:
    from code_mower.devin_readiness import devin_readiness

    findings = devin_readiness(
        config,
        lanes=lanes,
        repo_slug=repo_slug,
        transport=transport,
        credential_file=provider_credential_file,
        profile=provider_profile,
        config_profile=config_profile,
        config_dir=provider_config_dir,
        lane_config=effective_lane,
        adoption_posture=adoption_posture,
        include_unselected=include_unselected,
    )
    return [
        DoctorCheck(
            name=finding.name,
            status=finding.status,
            message=finding.message,
            lane=finding.lane,
            detail=dict(finding.detail) or None,
            remediation=finding.remediation or None,
        )
        for finding in findings
    ]
