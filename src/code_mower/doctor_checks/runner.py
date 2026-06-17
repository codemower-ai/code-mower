"""Doctor report orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from code_mower import config as code_mower_config

from .cloud import check_cloud_token_surface
from .common import ACTIONS_COST_SAMPLE_DEFAULT, load_inputs
from .github import check_github_setup
from .models import STATUS_FAIL, STATUS_PASS, DoctorCheck, DoctorReport
from .providers import check_lane_runtime, effective_lane, provider_template_coverage, selected_lanes
from .registry import DoctorCheckStage, build_doctor_run_plan
from .runtime import (
    check_github_auth_surface,
    check_pytest,
    check_python_runtime,
    check_ripgrep,
)


def _global_runtime_checks(
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> tuple[DoctorCheck, ...]:
    return (
        check_python_runtime(),
        check_pytest(),
        check_github_auth_surface(
            probe_runtime=probe_runtime,
            http_timeout=http_timeout,
        ),
        check_ripgrep(),
    )


def _run_plan_check(
    plan: tuple[DoctorCheckStage, ...],
    *,
    probe_runtime: bool,
    actions_cost_sample: int,
) -> DoctorCheck:
    return DoctorCheck(
        name="doctor.plan",
        status=STATUS_PASS,
        message="doctor run plan: " + ", ".join(stage.id for stage in plan),
        detail={
            "stages": [
                {
                    "id": stage.id,
                    "group": stage.group_id,
                    "optional": stage.optional,
                }
                for stage in plan
            ],
            "probe_runtime": probe_runtime,
            "actions_cost_sample": actions_cost_sample,
        },
    )


def run_doctor(
    *,
    config_path: Path,
    provider_templates_path: Path,
    profile: str | None,
    probe_runtime: bool = False,
    github: bool = False,
    cloud: bool = False,
    http_timeout: int = 5,
    actions_cost_sample: int = ACTIONS_COST_SAMPLE_DEFAULT,
) -> DoctorReport:
    plan = build_doctor_run_plan(github=github, cloud=cloud)
    enabled_stages = {stage.id for stage in plan}
    config, templates, checks = load_inputs(config_path, provider_templates_path)
    checks.append(
        _run_plan_check(
            plan,
            probe_runtime=probe_runtime,
            actions_cost_sample=actions_cost_sample,
        )
    )
    if config is None or templates is None:
        return DoctorReport(
            config_path=str(config_path),
            provider_templates_path=str(provider_templates_path),
            profile=profile,
            checks=tuple(checks),
        )

    try:
        lanes = selected_lanes(config, profile)
    except code_mower_config.ConfigError as exc:
        checks.append(
            DoctorCheck(
                name="profile.select",
                status=STATUS_FAIL,
                message=str(exc),
                remediation=(
                    "Choose an existing profile from code-mower.yml or run "
                    "`code-mower init --easy` to inspect the recommended profile."
                ),
            )
        )
        return DoctorReport(
            config_path=str(config_path),
            provider_templates_path=str(provider_templates_path),
            profile=profile,
            checks=tuple(checks),
        )

    checks.append(
        DoctorCheck(
            name="profile.select",
            status=STATUS_PASS,
            message=(
                f"selected profile {profile}: {', '.join(lanes)}"
                if profile
                else f"selected all lanes: {', '.join(lanes)}"
            ),
            detail={"lanes": list(lanes)},
        )
    )
    checks.append(provider_template_coverage(lanes, templates))
    checks.extend(
        _global_runtime_checks(
            probe_runtime=probe_runtime,
            http_timeout=http_timeout,
        )
    )

    lane_configs = config.get("lanes")
    if not isinstance(lane_configs, Mapping):
        raise code_mower_config.ConfigError("lanes must be a mapping")
    provider_templates = templates.get("provider_templates")
    if not isinstance(provider_templates, Mapping):
        raise code_mower_config.ConfigError("provider_templates must be a mapping")

    effective_lanes: list[tuple[str, Mapping[str, Any]]] = []
    for lane_id in lanes:
        lane = lane_configs.get(lane_id)
        if not isinstance(lane, Mapping):
            checks.append(
                DoctorCheck(
                    name="lane.load",
                    status=STATUS_FAIL,
                    lane=lane_id,
                    message="selected lane is missing from config",
                    remediation=(
                        "Add the lane to code-mower.yml or remove it from the "
                        "selected profile."
                    ),
                )
            )
            continue
        effective = effective_lane(lane_id, lane, provider_templates)
        effective_lanes.append((lane_id, effective))
        checks.extend(
            check_lane_runtime(
                lane_id,
                effective,
                probe_runtime=probe_runtime,
                http_timeout=http_timeout,
            )
        )

    if "github" in enabled_stages:
        checks.extend(
            check_github_setup(
                config=config,
                lanes=effective_lanes,
                http_timeout=http_timeout,
                actions_cost_sample=actions_cost_sample,
            )
        )

    if "cloud" in enabled_stages:
        checks.append(check_cloud_token_surface())

    return DoctorReport(
        config_path=str(config_path),
        provider_templates_path=str(provider_templates_path),
        profile=profile,
        checks=tuple(checks),
    )
