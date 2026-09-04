"""Doctor report orchestration."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any, Mapping

from code_mower import config as code_mower_config

from .adoption import (
    PUBLIC_IDENTITY_VARIABLES,
    TRUSTED_AUDIT_AUTHOR_VARIABLES,
    check_adoption_campaign_readiness,
    check_adoption_posture_guidance,
    check_adoption_setup,
    config_with_repository_target,
)
from .audit_limits import check_effective_audit_limits
from .cloud import check_cloud_token_surface
from .common import ACTIONS_COST_SAMPLE_DEFAULT, load_inputs
from .github import check_github_setup
from .github_config import check_repository_posture
from .github_trusted_authors import trusted_author_variable_probe
from .models import STATUS_FAIL, STATUS_PASS, DoctorCheck, DoctorReport
from .providers import check_lane_runtime, effective_lane, provider_template_coverage, selected_lanes
from .registry import DoctorCheckStage, build_doctor_run_plan
from .runtime import (
    check_github_auth_surface,
    check_macos_runner_launchagent,
    check_pytest,
    check_python_runtime,
    check_ripgrep,
)
from .self_hosted_runner import check_self_hosted_runner
from .supervised_pilot import check_supervised_pilot


def _provider_templates_source_root() -> Path:
    source_root = Path(__file__).resolve().parents[3]
    if (source_root / "templates/workflows").is_dir():
        return source_root
    return Path(__file__).resolve().parents[1]


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
        check_macos_runner_launchagent(),
        check_ripgrep(),
    )


def _run_plan_check(
    plan: tuple[DoctorCheckStage, ...],
    *,
    probe_runtime: bool,
    actions_cost_sample: int,
    adoption_posture: str,
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
            "adoption_posture": adoption_posture,
        },
    )


def run_doctor(
    *,
    config_path: Path,
    provider_templates_path: Path,
    profile: str | None,
    repo_slug: str = "",
    repo_source: str = "",
    config_source: str = "",
    adoption: bool = False,
    adoption_posture: str = "reviewer-gate",
    probe_runtime: bool = False,
    github: bool = False,
    cloud: bool = False,
    runner: bool = False,
    http_timeout: int = 5,
    actions_cost_sample: int = ACTIONS_COST_SAMPLE_DEFAULT,
    actionlint_bin: str = "actionlint",
    supervised_pilot: bool = False,
    pilot_mode: str = "manual",
) -> DoctorReport:
    plan = build_doctor_run_plan(
        github=github,
        cloud=cloud,
        runner=runner,
        adoption=adoption or bool(repo_slug),
        supervised_pilot=supervised_pilot,
    )
    enabled_stages = {stage.id for stage in plan}
    config, templates, checks = load_inputs(config_path, provider_templates_path)
    using_packaged_example = config_path.name == "code-mower.example.yml"
    # `doctor --easy` can inspect the packaged example before a repo has written
    # code-mower.yml. In that mode the example should teach the user about stale
    # guards without failing because product workflow files are not installed
    # yet, but it may still verify workflows already present in the checkout.
    repo_root = Path.cwd() if using_packaged_example else config_path.parent
    trusted_author_variables = None
    trusted_author_variable_errors = None
    trusted_author_repo_slug = repo_slug
    if not trusted_author_repo_slug and isinstance(config, Mapping):
        for repository in config.get("repositories") or []:
            if isinstance(repository, Mapping) and repository.get("slug"):
                trusted_author_repo_slug = str(repository.get("slug") or "")
                break
    if github and trusted_author_repo_slug:
        gh_path = shutil.which("gh")
        if gh_path:
            trusted_author_probe = trusted_author_variable_probe(
                gh_path=gh_path,
                slug=trusted_author_repo_slug,
                variables=(
                    *TRUSTED_AUDIT_AUTHOR_VARIABLES,
                    *PUBLIC_IDENTITY_VARIABLES,
                ),
                http_timeout=http_timeout,
            )
            statuses = trusted_author_probe.get("statuses")
            if isinstance(statuses, Mapping):
                trusted_author_variables = {
                    str(name): str(status)
                    for name, status in statuses.items()
                }
            errors = trusted_author_probe.get("read_errors")
            if isinstance(errors, Mapping):
                trusted_author_variable_errors = dict(errors)
    checks.extend(
        check_adoption_setup(
            config=config,
            config_path=config_path,
            adoption=adoption,
            repo_slug=repo_slug,
            repo_source=repo_source,
            config_source=config_source,
            using_packaged_example=using_packaged_example,
            repo_root=Path.cwd() if using_packaged_example else config_path.parent,
            trusted_author_variables=trusted_author_variables,
            trusted_author_variable_errors=trusted_author_variable_errors,
        )
    )
    if config is not None and repo_slug:
        config = config_with_repository_target(config, repo_slug)
    checks.append(
        _run_plan_check(
            plan,
            probe_runtime=probe_runtime,
            actions_cost_sample=actions_cost_sample,
            adoption_posture=adoption_posture,
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
    checks.append(check_repository_posture(config))
    checks.append(check_effective_audit_limits(config))
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
                source_lane=lane,
                repo_root=repo_root,
                missing_workflow_is_warning=using_packaged_example,
                adoption_posture=adoption_posture,
                probe_runtime=probe_runtime,
                http_timeout=http_timeout,
            )
        )
    checks.extend(
        check_adoption_posture_guidance(
            checks,
            adoption=adoption,
            adoption_posture=adoption_posture,
        )
    )

    if adoption:
        checks.extend(
            check_adoption_campaign_readiness(
                config=config,
                repo_root=repo_root,
                repo_slug=repo_slug,
                adoption_posture=adoption_posture,
            )
        )

    if "github" in enabled_stages:
        checks.extend(
            check_github_setup(
                config=config,
                lanes=effective_lanes,
                http_timeout=http_timeout,
                actions_cost_sample=actions_cost_sample,
                adoption=adoption,
                adoption_posture=adoption_posture,
                pilot_mode=pilot_mode,
            )
        )

    if "cloud" in enabled_stages:
        checks.append(check_cloud_token_surface())

    if "runner" in enabled_stages:
        checks.extend(
            check_self_hosted_runner(
                config=config,
                profile=profile,
                config_path=config_path,
                lanes=effective_lanes,
                repo_root=repo_root or config_path.parent,
                provider_templates_root=_provider_templates_source_root(),
                http_timeout=http_timeout,
                actionlint_bin=actionlint_bin,
            )
        )

    if supervised_pilot:
        checks.extend(
            check_supervised_pilot(
                checks,
                repo_slug=repo_slug,
                pilot_mode=pilot_mode,
                adoption_posture=adoption_posture,
            )
        )

    return DoctorReport(
        config_path=str(config_path),
        provider_templates_path=str(provider_templates_path),
        profile=profile,
        checks=tuple(checks),
    )
