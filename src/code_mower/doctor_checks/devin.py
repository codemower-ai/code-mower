"""Optional Devin readiness checks for the local CLI and hosted v3 postures."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from code_mower import config as code_mower_config
from code_mower.provider_capabilities import TRANSPORTS, devin_lane_transport_name

from .models import DoctorCheck

# `code_mower.devin_readiness` reaches the Devin credential and campaign modules,
# which import this check package; resolve it per call to keep that acyclic.

__all__ = [
    "check_devin_readiness",
    "devin_effective_lane",
    "devin_effective_lanes",
    "devin_readiness_selected",
    "devin_selection_ambiguity",
]


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


def devin_selection_ambiguity(
    config: Mapping[str, Any] | None,
    *,
    lanes: tuple[str, ...] = (),
    profile: str | None = "recommended",
) -> str | None:
    """Return why the selected Devin transport is ambiguous, if it is.

    An unresolvable selection is a readiness defect of a repository that did
    select Devin, so planning needs it: dropping the stage would report a clean
    run for a configuration whose Devin posture cannot be determined.
    """
    from code_mower.devin_readiness import selected_devin_transport

    try:
        selected_devin_transport(config, lanes=lanes, profile=profile)
    except code_mower_config.ConfigError as error:
        return str(error)
    return None


def devin_effective_lanes(
    effective_lanes: Iterable[tuple[str, Mapping[str, Any]]],
    transport: str | None = None,
) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """Return the selected Devin lanes as their IDs and effective configs.

    A lane is matched by what it declares, so a valid custom-named lane supplies
    its own command configuration exactly as a canonical lane does, and its own
    ID so findings name the lane the repository actually has. A profile may
    validly select several Devin lanes on one transport, each with its own
    command: all of them are returned, because rejecting the configuration or
    reporting one lane's executable under another lane's identity would both be
    wrong. Lanes on the other transport are excluded once the transport is
    resolved.

    Raises:
        ConfigError: when the active lanes span both transports and none was
            selected, because the posture itself is then unknown.
    """
    wanted = (
        transport
        if transport in TRANSPORTS and TRANSPORTS[transport].product == "devin"
        else None
    )
    matched: list[tuple[str, Mapping[str, Any]]] = []
    declared_transports: set[str] = set()
    for lane_id, effective in effective_lanes:
        declared = devin_lane_transport_name(lane_id, effective)
        if declared is not None and wanted in (None, declared):
            matched.append((lane_id, effective))
            declared_transports.add(declared)
    if len(declared_transports) > 1:
        raise code_mower_config.ConfigError(
            "active Devin lanes select both transports ("
            + ", ".join(lane_id for lane_id, _ in matched)
            + "); scope the check to one Code Mower --profile or name the transport "
            "with session_defaults.transports.devin"
        )
    return tuple(matched)


def devin_effective_lane(
    effective_lanes: Iterable[tuple[str, Mapping[str, Any]]],
    transport: str | None = None,
) -> tuple[str, Mapping[str, Any]] | None:
    """Return the one selected Devin review lane, for single-lane callers.

    Raises:
        ConfigError: when several lanes are selected, because a single answer
            would report one lane's configuration under another lane's identity.
    """
    matched = devin_effective_lanes(effective_lanes, transport)
    if len(matched) > 1:
        raise code_mower_config.ConfigError(
            "several active Devin lanes are selected ("
            + ", ".join(lane_id for lane_id, _ in matched)
            + "); a single-lane caller must scope the Code Mower --profile being "
            "inspected to one Devin lane"
        )
    return matched[0] if matched else None


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
    config_path: str = "",
    effective_lanes: Iterable[tuple[str, Mapping[str, Any]]] = (),
    adoption_posture: str = "reviewer-gate",
) -> list[DoctorCheck]:
    from code_mower.devin_readiness import (
        SCHEMA,
        devin_readiness,
        selected_devin_transport,
    )

    try:
        # The transport is resolved before the lane so an ambiguous selection is
        # reported as itself, and the selected lane is resolved here so an
        # unidentifiable lane fails closed through the same bounded finding
        # instead of reporting another lane's identity.
        selected = transport or selected_devin_transport(
            config, lanes=lanes, profile=config_profile
        )
        selected_lanes = devin_effective_lanes(effective_lanes, selected)
        findings = devin_readiness(
            config,
            lanes=lanes,
            repo_slug=repo_slug,
            transport=selected,
            credential_file=provider_credential_file,
            profile=provider_profile,
            config_profile=config_profile,
            config_dir=provider_config_dir,
            config_path=config_path,
            lane_configs=selected_lanes,
            adoption_posture=adoption_posture,
            include_unselected=include_unselected,
        )
    except code_mower_config.ConfigError as error:
        # An unresolved selection is reported as a bounded failing finding: the
        # rest of the readiness answers depend on knowing the posture.
        return [
            DoctorCheck(
                name="provider.devin.selection",
                status="fail",
                message=f"Devin transport selection is ambiguous: {error}",
                detail={"schema": SCHEMA, "selection": "ambiguous"},
                remediation=str(error),
            )
        ]
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
