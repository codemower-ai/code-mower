"""Provider catalog coverage and runtime check orchestration."""

from __future__ import annotations

from typing import Any, Mapping

from .common import (
    DoctorCheck,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    as_mapping,
    as_sequence,
    code_mower_config,
)
from .provider_api_model import check_api_model
from .provider_env import check_required_env, check_token_env
from .provider_local_cli import (
    check_local_cli,
    check_local_cli_probe,
    evaluate_json_probe,
    local_cli_probe_remediation,
)

__all__ = [
    "check_lane_runtime",
    "effective_lane",
    "evaluate_json_probe",
    "local_cli_probe_remediation",
    "provider_template_coverage",
    "selected_lanes",
]


def selected_lanes(
    config: Mapping[str, Any],
    profile: str | None,
) -> tuple[str, ...]:
    lanes = as_mapping(config.get("lanes"), "lanes")
    if not profile:
        return tuple(str(lane_id) for lane_id in sorted(lanes))
    profiles = as_mapping(config.get("profiles", {}), "profiles")
    profile_config = profiles.get(profile)
    if not isinstance(profile_config, Mapping):
        known = ", ".join(sorted(str(item) for item in profiles))
        raise code_mower_config.ConfigError(
            f"unknown profile {profile!r}; known profiles: {known}"
        )
    return tuple(str(lane_id) for lane_id in as_sequence(profile_config.get("lanes", [])))


def provider_template_coverage(
    lanes: tuple[str, ...],
    templates: Mapping[str, Any],
) -> DoctorCheck:
    provider_templates = as_mapping(
        templates.get("provider_templates"),
        "provider_templates",
    )
    missing = sorted(set(lanes) - set(str(item) for item in provider_templates))
    if missing:
        return DoctorCheck(
            name="provider_templates.coverage",
            status=STATUS_FAIL,
            message=f"provider templates missing selected lanes: {', '.join(missing)}",
            detail={"missing_lanes": missing},
            remediation=(
                "Add these lane ids to the provider catalog or remove them from "
                "the selected profile before running audits."
            ),
        )
    return DoctorCheck(
        name="provider_templates.coverage",
        status=STATUS_PASS,
        message="provider templates cover selected lanes",
    )


def _merge_mapping_defaults(
    defaults: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(defaults)
    merged.update(overrides)
    return merged


def effective_lane(
    lane_id: str,
    lane: Mapping[str, Any],
    provider_templates: Mapping[str, Any],
) -> Mapping[str, Any]:
    template = provider_templates.get(lane_id, {})
    if not isinstance(template, Mapping):
        template = {}
    merged = _merge_mapping_defaults(template, lane)
    for key in ("labels", "provider_config", "review_hygiene"):
        template_value = template.get(key, {})
        lane_value = lane.get(key, {})
        if isinstance(template_value, Mapping) and isinstance(lane_value, Mapping):
            merged[key] = _merge_mapping_defaults(template_value, lane_value)
    return merged


def check_lane_runtime(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> list[DoctorCheck]:
    checks = check_token_env(lane_id, lane)
    checks.extend(check_required_env(lane_id, lane))
    driver = str(lane.get("driver", ""))
    if driver == "local_cli":
        checks.append(check_local_cli(lane_id, lane))
        checks.append(
            check_local_cli_probe(
                lane_id,
                lane,
                probe_runtime=probe_runtime,
                http_timeout=http_timeout,
            )
        )
    elif driver == "api_model":
        checks.extend(
            check_api_model(
                lane_id,
                lane,
                probe_runtime=probe_runtime,
                http_timeout=http_timeout,
            )
        )
    elif driver in {"manual", "hosted_bridge", "saas_event"}:
        checks.append(
            DoctorCheck(
                name="runtime.probe",
                status=STATUS_SKIP,
                lane=lane_id,
                message=f"{driver} lanes do not have a local runtime probe yet",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="runtime.probe",
                status=STATUS_WARN,
                lane=lane_id,
                message=f"unknown driver {driver!r}; no runtime probe available",
                remediation=(
                    "Use a supported driver: local_cli, api_model, hosted_bridge, "
                    "saas_event, or manual."
                ),
            )
        )
    return checks
