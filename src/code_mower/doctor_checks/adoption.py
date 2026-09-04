"""First-run adoption checks for doctor."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


from .cloud import DEFAULT_CLOUD_TOKEN_DIR, DEFAULT_CLOUD_TOKEN_ENV
from .models import STATUS_PASS, STATUS_SKIP, STATUS_WARN, DoctorCheck


OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
OWNER_LOGIN_PLACEHOLDER = "TODO_OWNER_LOGIN"
TRUSTED_AUDIT_AUTHOR_VARIABLES = (
    "CLAUDE_AUDIT_BOT_AUTHORS",
    "CODEX_BOT_AUTHORS",
)
PUBLIC_IDENTITY_VARIABLES = (
    "CODE_MOWER_OWNER_LOGIN",
    "CODE_MOWER_DECISION_AUTHORITIES",
    "CODE_MOWER_TRUSTED_AUTHORS_JSON",
)
GENERATED_SETUP_MARKERS = {
    "generated_config": (".code-mower.generated/code-mower.yml",),
    "installed_gate_workflow": (".github/workflows/code-mower-gate.yml",),
    "installed_dispatch_workflow": (".github/workflows/dispatch-lanes.yml",),
    "installed_agent_pr_labeler": (
        ".github/workflows/code-mower-agent-pr-labeler.yml",
    ),
}


def normalize_repo_slug(value: str, *, option: str = "--repo") -> str:
    """Return a validated GitHub owner/repo slug."""

    slug = value.strip().strip("/")
    if not OWNER_REPO_RE.fullmatch(slug):
        raise ValueError(f"{option} expects an OWNER/REPO slug")
    return slug


def repo_slug_from_remote(remote_url: str) -> str:
    """Extract owner/repo from common GitHub remote URL forms."""

    remote = remote_url.strip()
    if remote.startswith("git@github.com:"):
        remote = remote.removeprefix("git@github.com:")
    elif remote.startswith("https://github.com/"):
        remote = remote.removeprefix("https://github.com/")
    elif remote.startswith("http://github.com/"):
        remote = remote.removeprefix("http://github.com/")
    elif remote.startswith("ssh://git@github.com/"):
        remote = remote.removeprefix("ssh://git@github.com/")
    else:
        return ""
    remote = remote.removesuffix(".git").strip("/")
    parts = remote.split("/")
    if len(parts) < 2:
        return ""
    slug = f"{parts[0]}/{parts[1]}"
    return slug if OWNER_REPO_RE.fullmatch(slug) else ""


def detect_repo_slug(repo_path: Path, *, git_bin: str = "git") -> str:
    """Detect the current checkout's GitHub owner/repo slug."""

    try:
        completed = subprocess.run(
            [git_bin, "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return ""
    if completed.returncode != 0:
        return ""
    return repo_slug_from_remote(completed.stdout)


def config_with_repository_target(
    config: Mapping[str, Any],
    repo_slug: str,
) -> Mapping[str, Any]:
    """Return a config copy whose GitHub checks target repo_slug."""

    repositories = [
        repo for repo in config.get("repositories") or [] if isinstance(repo, Mapping)
    ]
    source_repo = next(
        (repo for repo in repositories if str(repo.get("slug") or "") == repo_slug),
        repositories[0] if repositories else {},
    )
    default_branch = str(source_repo.get("default_branch") or "main")
    local_path_env = str(source_repo.get("local_path_env") or "")
    target: dict[str, str] = {"slug": repo_slug, "default_branch": default_branch}
    if local_path_env:
        target["local_path_env"] = local_path_env
    return {**dict(config), "repositories": [target]}


def _configured_owner_login(config: Mapping[str, Any] | None) -> str:
    if not isinstance(config, Mapping):
        return ""
    owner_surface = config.get("owner_surface")
    if not isinstance(owner_surface, Mapping):
        return ""
    return str(owner_surface.get("owner_login") or "").strip()


def _configured_repositories(config: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(config, Mapping):
        return []
    slugs: list[str] = []
    for repo in config.get("repositories") or []:
        if isinstance(repo, Mapping) and repo.get("slug"):
            slugs.append(str(repo.get("slug")))
    return slugs


def _generated_setup_marker_types(repo_root: Path | None) -> list[str]:
    if repo_root is None:
        return []
    marker_types: list[str] = []
    for marker_type, relative_paths in GENERATED_SETUP_MARKERS.items():
        if any((repo_root / relative_path).exists() for relative_path in relative_paths):
            marker_types.append(marker_type)
    return marker_types


def _config_source_state(
    *,
    source: str,
    using_packaged_example: bool,
    generated_marker_types: Sequence[str],
) -> str:
    if not using_packaged_example:
        return source or "repository_config"
    if source == "source_tree_starter":
        return "source_tree_starter"
    if generated_marker_types:
        return "generated_without_root_config"
    return "cold_configless"


def check_adoption_posture_guidance(
    checks: Sequence[DoctorCheck],
    *,
    adoption: bool,
    adoption_posture: str,
) -> tuple[DoctorCheck, ...]:
    """Return first-run guidance when default posture may not match this host."""

    if not adoption or adoption_posture != "reviewer-gate":
        return ()
    local_provider_gap_names = {"runtime.local_cli", "runtime.local_cli.probe"}
    gap_lanes = sorted(
        {
            str(check.lane)
            for check in checks
            if check.lane
            and (
                check.name in local_provider_gap_names
                or check.name.startswith("runtime.local_audit")
            )
            and check.status == STATUS_WARN
        }
    )
    if not gap_lanes:
        return ()
    return (
        DoctorCheck(
            name="doctor.adoption.posture_hint",
            status=STATUS_WARN,
            message=(
                "default reviewer-gate posture checks local reviewer CLIs on this host"
            ),
            detail={
                "adoption_posture": adoption_posture,
                "local_provider_gap_lanes": gap_lanes,
                "next_steps": [
                    "rerun_orchestrator_only_if_this_host_only_coordinates",
                    "rerun_hosted_builders_if_this_host_observes_hosted_lanes",
                    "finish_local_cli_setup_if_this_host_runs_reviewers",
                ],
                "commands": [
                    "code-mower doctor --adoption --orchestrator-only --repo OWNER/REPO",
                    "code-mower doctor --adoption --hosted-builders --repo OWNER/REPO",
                ],
            },
            remediation=(
                "If this host only coordinates work, rerun "
                "`code-mower doctor --adoption --orchestrator-only --repo OWNER/REPO`. "
                "If it observes hosted builder lanes, rerun "
                "`code-mower doctor --adoption --hosted-builders --repo OWNER/REPO`. "
                "Only keep the default reviewer-gate posture on machines that "
                "run local reviewer CLIs."
            ),
        ),
    )


def check_adoption_setup(
    *,
    config: Mapping[str, Any] | None,
    config_path: Path,
    adoption: bool,
    repo_slug: str,
    repo_source: str,
    using_packaged_example: bool,
    config_source: str = "",
    repo_root: Path | None = None,
    trusted_author_variables: Mapping[str, str] | None = None,
    trusted_author_variable_errors: Mapping[str, Any] | None = None,
) -> tuple[DoctorCheck, ...]:
    """Return first-run adoption posture checks."""

    if not adoption and not repo_slug and not using_packaged_example:
        return ()

    checks: list[DoctorCheck] = []
    if repo_slug:
        checks.append(
            DoctorCheck(
                name="doctor.repo",
                status=STATUS_PASS,
                message=f"GitHub target repository: {repo_slug}",
                detail={"repo": repo_slug, "source": repo_source or "explicit"},
            )
        )
    elif not adoption:
        pass
    else:
        checks.append(
            DoctorCheck(
                name="doctor.repo",
                status=STATUS_WARN,
                message="adoption checks could not infer the GitHub repository",
                detail={"repo": "", "source": ""},
                remediation=(
                    "Run from a GitHub checkout or pass "
                    "`code-mower doctor --adoption --repo OWNER/REPO`."
                ),
            )
        )

    repositories = _configured_repositories(config)
    source = config_source or (
        "packaged_starter" if using_packaged_example else "repository_config"
    )
    generated_marker_types = _generated_setup_marker_types(repo_root)
    source_state = _config_source_state(
        source=source,
        using_packaged_example=using_packaged_example,
        generated_marker_types=generated_marker_types,
    )
    source_messages = {
        "explicit_config": "using explicit Code Mower config",
        "cold_configless": (
            "using packaged starter config because no repository code-mower.yml was found"
        ),
        "generated_without_root_config": (
            "using packaged starter config because generated Code Mower setup exists "
            "but repository code-mower.yml is not installed"
        ),
        "packaged_starter": "using packaged starter config for adoption checks",
        "repository_config": "using repository Code Mower config",
        "source_tree_starter": "using source-tree starter config for adoption checks",
    }
    next_steps = (
        [
            "review_generated_setup",
            "install_root_config",
            "rerun_adoption_doctor",
        ]
        if source_state == "generated_without_root_config"
        else [
            "run_init_easy_apply",
            "edit_generated_config",
            "rerun_adoption_doctor",
        ]
        if source_state == "cold_configless"
        else []
    )
    detail = {
        "config_path": str(config_path),
        "config_source": source,
        "config_source_state": source_state,
        "configured_repositories": repositories,
        "effective_repository": repo_slug,
        "repository_config_present": not using_packaged_example,
        "generated_setup_detected": bool(generated_marker_types),
        "generated_setup_marker_types": generated_marker_types,
    }
    if next_steps:
        detail["next_steps"] = next_steps
    if using_packaged_example:
        checks.append(
            DoctorCheck(
                name="doctor.adoption.config_source",
                status=STATUS_WARN,
                message=source_messages.get(source_state, source_messages["packaged_starter"]),
                detail=detail,
                remediation=(
                    "Review the generated setup, commit an edited code-mower.yml "
                    "at the repository root, then rerun doctor."
                    if source_state == "generated_without_root_config"
                    else "Run `code-mower init --easy --apply`, review the generated "
                    "setup, and commit an edited code-mower.yml before relying "
                    "on recurring workflows."
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="doctor.adoption.config_source",
                status=STATUS_PASS,
                message=source_messages.get(source, source_messages["repository_config"]),
                detail=detail,
            )
        )

    if (
        repo_slug
        and repo_source == "git_remote"
        and repositories
        and repo_slug not in repositories
        and not using_packaged_example
    ):
        checks.append(
            DoctorCheck(
                name="doctor.adoption.repo_mismatch",
                status=STATUS_WARN,
                message="remote origin does not match code-mower.yml repositories",
                detail={
                    "inferred_repository": repo_slug,
                    "configured_repositories": repositories,
                },
                remediation=(
                    "Pass `--repo OWNER/REPO` to choose an explicit target, or "
                    "update code-mower.yml or remote.origin.url so doctor and "
                    "the committed config agree."
                ),
            )
        )

    if not adoption and not repo_slug:
        return tuple(checks)

    owner_login = _configured_owner_login(config)
    owner_login_variable = (
        str((trusted_author_variables or {}).get("CODE_MOWER_OWNER_LOGIN") or "")
        == "present"
    )
    if (
        not owner_login
        or owner_login.lower() == OWNER_LOGIN_PLACEHOLDER.lower()
    ) and owner_login_variable:
        checks.append(
            DoctorCheck(
                name="doctor.adoption.owner_login",
                status=STATUS_PASS,
                message="owner decision login configured by repository variable",
                detail={
                    "owner_login": "",
                    "source": "CODE_MOWER_OWNER_LOGIN",
                    "value_hidden": True,
                },
            )
        )
    elif not owner_login or owner_login.lower() == OWNER_LOGIN_PLACEHOLDER.lower():
        checks.append(
            DoctorCheck(
                name="doctor.adoption.owner_login",
                status=STATUS_WARN,
                message="owner_surface.owner_login is not configured",
                detail={"owner_login": owner_login},
                remediation=(
                    "Set owner_surface.owner_login to the decision owner's "
                    "GitHub login, or set the CODE_MOWER_OWNER_LOGIN repository "
                    "variable when personal identity should not be tracked."
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="doctor.adoption.owner_login",
                status=STATUS_PASS,
                message=f"owner decision login configured: {owner_login}",
                detail={"owner_login": owner_login},
            )
        )

    trusted_detail: dict[str, Any] = {
        "variables": list(TRUSTED_AUDIT_AUTHOR_VARIABLES)
    }
    if trusted_author_variables is not None:
        variable_status = {
            name: str(trusted_author_variables.get(name) or "not_confirmed")
            for name in TRUSTED_AUDIT_AUTHOR_VARIABLES
        }
        trusted_detail["variable_status"] = variable_status
        if trusted_author_variable_errors:
            trusted_detail["variable_read_errors"] = {
                name: trusted_author_variable_errors[name]
                for name in sorted(trusted_author_variable_errors)
                if name in TRUSTED_AUDIT_AUTHOR_VARIABLES
            }
        all_present = all(
            variable_status[name] == "present" for name in TRUSTED_AUDIT_AUTHOR_VARIABLES
        )
        if all_present:
            checks.append(
                DoctorCheck(
                    name="doctor.adoption.trusted_authors",
                    status=STATUS_PASS,
                    message="trusted audit-comment author variables are configured",
                    detail=trusted_detail,
                )
            )
            return tuple(checks)
        if any(
            variable_status[name] == "not_confirmed"
            for name in TRUSTED_AUDIT_AUTHOR_VARIABLES
        ):
            checks.append(
                DoctorCheck(
                    name="doctor.adoption.trusted_authors",
                    status=STATUS_WARN,
                    message=(
                        "trusted audit-comment author variables were not fully confirmed"
                    ),
                    detail=trusted_detail,
                    remediation=(
                        "Ensure `gh auth status` can read repository Actions "
                        "variables for this repo, then set CLAUDE_AUDIT_BOT_AUTHORS "
                        "and CODEX_BOT_AUTHORS to the GitHub logins that may post "
                        "trusted audit verdicts."
                    ),
                )
            )
            return tuple(checks)

    checks.append(
        DoctorCheck(
            name="doctor.adoption.trusted_authors",
            status=STATUS_WARN,
            message="human-run audit comments must be listed as trusted lane authors",
            detail=trusted_detail,
            remediation=(
                "For manual pilot audits, set repository variables such as "
                "CLAUDE_AUDIT_BOT_AUTHORS and CODEX_BOT_AUTHORS to the "
                "GitHub logins that may post trusted audit verdicts."
            ),
        )
    )
    return tuple(checks)

DEFAULT_CAMPAIGN_PROVIDERS = (
    "claude",
    "codex",
    "antigravity",
    "muse",
    "cursor_bugbot",
    "devin",
)


def _is_provider_enabled(
    lane: Any,
    config: Mapping[str, Any] | None,
    repo_root: Path,
) -> bool:
    """Determine if a provider lane is actively enabled or optional."""
    from code_mower.release_campaigns import (
        _campaign_adapter_override_lane_keys,
        _load_campaign_adapter_overrides,
    )

    if isinstance(config, Mapping) and isinstance(config.get("lanes"), Mapping):
        lanes_cfg = config["lanes"]
        for key in _campaign_adapter_override_lane_keys(lane):
            lane_entry = lanes_cfg.get(key)
            if isinstance(lane_entry, Mapping):
                if "enabled" in lane_entry:
                    return bool(lane_entry["enabled"])
                if "enabled_by_default" in lane_entry:
                    return bool(lane_entry["enabled_by_default"])
                provider_cfg = lane_entry.get("provider_config")
                if isinstance(provider_cfg, Mapping) and "campaign_adapter_argv" in provider_cfg:
                    return True

    overrides, _, _ = _load_campaign_adapter_overrides(lane, repo_root)
    if overrides.get("campaign_adapter_argv"):
        return True

    return bool(getattr(lane, "enabled_by_default", False))


def _resolve_adapter_config_for_lane(
    lane: Any,
    repo_root: Path,
    config: Mapping[str, Any] | None = None,
) -> tuple[Any, Any, str, str]:
    """Resolve effective campaign adapter argv and timeout for a lane."""
    from code_mower.release_campaigns import (
        _campaign_adapter_override_lane_keys,
        _resolve_campaign_adapter_config,
        _safe_error,
    )

    if isinstance(config, Mapping) and "lanes" in config:
        lanes_cfg = config["lanes"]
        if lanes_cfg is not None and not isinstance(lanes_cfg, Mapping):
            return None, None, _safe_error("adapter_configuration_invalid"), "lanes must be a mapping"
        if isinstance(lanes_cfg, Mapping):
            configured_keys = [
                key
                for key in _campaign_adapter_override_lane_keys(lane)
                if lanes_cfg.get(key) is not None
            ]
            if len(configured_keys) > 1:
                return (
                    None,
                    None,
                    _safe_error("adapter_configuration_invalid"),
                    (
                        f"code-mower.yml configures the same provider lane under "
                        f"{len(configured_keys)} names ({', '.join(configured_keys)}); "
                        "keep exactly one"
                    ),
                )
            if configured_keys:
                lane_cfg = lanes_cfg[configured_keys[0]]
                if not isinstance(lane_cfg, Mapping):
                    return None, None, _safe_error("adapter_configuration_invalid"), ""
                provider_cfg = lane_cfg.get("provider_config")
                if provider_cfg is not None and not isinstance(provider_cfg, Mapping):
                    return None, None, _safe_error("adapter_configuration_invalid"), ""
                overrides: dict[str, Any] = {}
                if isinstance(provider_cfg, Mapping):
                    for key in (
                        "campaign_adapter_argv",
                        "campaign_adapter_timeout_seconds",
                        "campaign_adapter_enabled",
                    ):
                        if key in provider_cfg:
                            overrides[key] = provider_cfg[key]
                enabled = overrides.get(
                    "campaign_adapter_enabled",
                    lane.provider_config.get("campaign_adapter_enabled", True),
                )
                if not isinstance(enabled, bool):
                    return None, None, _safe_error("adapter_configuration_invalid"), ""
                if enabled is False:
                    return None, None, "", ""
                argv_template = overrides.get(
                    "campaign_adapter_argv",
                    lane.provider_config.get("campaign_adapter_argv"),
                )
                timeout_value = overrides.get(
                    "campaign_adapter_timeout_seconds",
                    lane.provider_config.get("campaign_adapter_timeout_seconds"),
                )
                return argv_template, timeout_value, "", ""

    return _resolve_campaign_adapter_config(lane, repo_root)


def check_adoption_campaign_readiness(
    *,
    config: Mapping[str, Any] | None,
    repo_root: Path | None = None,
    repo_slug: str = "",
    adoption_posture: str = "reviewer-gate",
    env: Mapping[str, str] | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    command_runner: Any = None,
    token_dir: Path | None = None,
    providers: Sequence[str] = DEFAULT_CAMPAIGN_PROVIDERS,
) -> tuple[DoctorCheck, ...]:
    """Validate release campaign readiness across configured providers and storage."""
    from code_mower import lane_status
    from code_mower.cloud import resolve_cloud_token
    from code_mower.release_campaigns import (
        _check_credentials,
        _find_command,
        _safe_error,
        _validate_adapter_argv_template,
        _validate_adapter_timeout,
        resolve_provider_lane,
    )

    root = repo_root or Path.cwd()
    current_env = os.environ if env is None else env
    runner = lane_status._run_command if command_runner is None else command_runner
    checks: list[DoctorCheck] = []

    for prov in providers:
        try:
            canonical, lane = resolve_provider_lane(prov)
        except ValueError:
            continue

        is_enabled = _is_provider_enabled(lane, config=config, repo_root=root)

        if lane.driver == "local_cli":
            if adoption_posture in {"hosted-builders", "orchestrator-only"}:
                checks.append(
                    DoctorCheck(
                        name="doctor.campaign.adapter",
                        status=STATUS_SKIP,
                        lane=canonical,
                        message=f"skipped {canonical} campaign adapter check in {adoption_posture} posture",
                        detail={
                            "provider": canonical,
                            "lane": lane.lane_id,
                            "driver": lane.driver,
                            "adoption_posture": adoption_posture,
                            "skipped": True,
                        },
                    )
                )
                continue

            argv_template, timeout_value, config_error, config_detail = _resolve_adapter_config_for_lane(
                lane,
                repo_root=root,
                config=config,
            )
            if not config_error and argv_template is not None:
                try:
                    _validate_adapter_argv_template(argv_template)
                    if timeout_value is not None:
                        _validate_adapter_timeout(timeout_value)
                except ValueError as exc:
                    config_error = _safe_error("adapter_configuration_invalid")
                    config_detail = str(exc)

            cmd = _find_command(lane, which_fn=which_fn)
            command_name = lane.provider_config.get("command") or lane.provider

            if config_error:
                detail = {
                    "provider": canonical,
                    "lane": lane.lane_id,
                    "driver": lane.driver,
                    "error": config_error,
                    "adapter_configured": False,
                    "command": cmd or command_name,
                    "command_found": bool(cmd),
                    "enabled": is_enabled,
                    "actionable": is_enabled,
                    "optional": not is_enabled,
                }
                if is_enabled:
                    detail["owner_action"] = True
                checks.append(
                    DoctorCheck(
                        name="doctor.campaign.adapter",
                        status=STATUS_WARN,
                        lane=canonical,
                        message=f"{canonical} campaign adapter configuration is invalid",
                        detail=detail,
                        remediation=(
                            config_detail
                            or f"Fix invalid {canonical} campaign adapter configuration in code-mower.yml."
                        ),
                    )
                )
            elif not cmd:
                detail = {
                    "provider": canonical,
                    "lane": lane.lane_id,
                    "driver": lane.driver,
                    "command": command_name,
                    "command_found": False,
                    "adapter_configured": bool(argv_template),
                    "enabled": is_enabled,
                    "actionable": is_enabled,
                    "optional": not is_enabled,
                }
                if is_enabled:
                    detail["owner_action"] = True
                checks.append(
                    DoctorCheck(
                        name="doctor.campaign.adapter",
                        status=STATUS_WARN,
                        lane=canonical,
                        message=f"{canonical} command {command_name!r} not found on PATH",
                        detail=detail,
                        remediation=f"Install {command_name} CLI on PATH or specify the executable in code-mower.yml.",
                    )
                )
            elif not argv_template:
                detail = {
                    "provider": canonical,
                    "lane": lane.lane_id,
                    "driver": lane.driver,
                    "command": cmd,
                    "command_found": True,
                    "adapter_configured": False,
                    "enabled": is_enabled,
                    "actionable": is_enabled,
                    "optional": not is_enabled,
                }
                if is_enabled:
                    detail["owner_action"] = True
                checks.append(
                    DoctorCheck(
                        name="doctor.campaign.adapter",
                        status=STATUS_WARN,
                        lane=canonical,
                        message=f"{canonical} campaign adapter not configured",
                        detail=detail,
                        remediation=(
                            f"Configure campaign_adapter_argv for {canonical} in code-mower.yml."
                            if is_enabled
                            else f"Configure campaign_adapter_argv for {canonical} in code-mower.yml to include this optional provider in campaigns."
                        ),
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        name="doctor.campaign.adapter",
                        status=STATUS_PASS,
                        lane=canonical,
                        message=f"{canonical} campaign adapter and command ready",
                        detail={
                            "provider": canonical,
                            "lane": lane.lane_id,
                            "driver": lane.driver,
                            "command": cmd,
                            "command_found": True,
                            "adapter_configured": True,
                            "enabled": is_enabled,
                        },
                    )
                )

        elif lane.driver in {"hosted_bridge", "saas_event"}:
            has_credentials, missing_var = _check_credentials(lane, env=current_env)
            has_repo = bool(repo_slug)

            if not has_credentials:
                detail = {
                    "provider": canonical,
                    "lane": lane.lane_id,
                    "driver": lane.driver,
                    "missing_variable": missing_var,
                    "repo_slug": repo_slug,
                    "enabled": is_enabled,
                    "actionable": is_enabled,
                    "optional": not is_enabled,
                }
                if is_enabled:
                    detail["owner_action"] = True
                checks.append(
                    DoctorCheck(
                        name="doctor.campaign.credentials",
                        status=STATUS_WARN,
                        lane=canonical,
                        message=f"{canonical} hosted credentials missing ({missing_var})",
                        detail=detail,
                        remediation=f"Set {missing_var} in environment for {canonical} campaign dispatch.",
                    )
                )
            elif not has_repo:
                detail = {
                    "provider": canonical,
                    "lane": lane.lane_id,
                    "driver": lane.driver,
                    "has_credentials": True,
                    "repo_slug": "",
                    "enabled": is_enabled,
                    "actionable": is_enabled,
                    "optional": not is_enabled,
                }
                if is_enabled:
                    detail["owner_action"] = True
                checks.append(
                    DoctorCheck(
                        name="doctor.campaign.credentials",
                        status=STATUS_WARN,
                        lane=canonical,
                        message=f"{canonical} hosted dispatch requires a repository slug",
                        detail=detail,
                        remediation="Run from a GitHub checkout or pass `code-mower doctor --adoption --repo OWNER/REPO`.",
                    )
                )
            else:
                checks.append(
                    DoctorCheck(
                        name="doctor.campaign.credentials",
                        status=STATUS_PASS,
                        lane=canonical,
                        message=f"{canonical} hosted credentials and repository target ready",
                        detail={
                            "provider": canonical,
                            "lane": lane.lane_id,
                            "driver": lane.driver,
                            "repo_slug": repo_slug,
                            "has_credentials": True,
                            "enabled": is_enabled,
                        },
                    )
                )

    # 3. Campaign Storage Writable Check
    storage_rel = ".code-mower/campaigns"
    target_dir = root / ".code-mower" / "campaigns"
    writable = False
    try:
        for required_path in (target_dir.parent, target_dir):
            if required_path.is_symlink() and not required_path.exists():
                raise OSError("broken campaign storage symlink")
        probe_dir = target_dir
        while not probe_dir.exists() and probe_dir.parent != probe_dir:
            probe_dir = probe_dir.parent
        if probe_dir.is_dir():
            with tempfile.NamedTemporaryFile(
                prefix=".code-mower-doctor-",
                dir=probe_dir,
            ):
                writable = True
    except OSError:
        writable = False

    if writable:
        checks.append(
            DoctorCheck(
                name="doctor.campaign.storage",
                status=STATUS_PASS,
                message=f"campaign storage directory is writable ({storage_rel})",
                detail={"storage_dir": storage_rel, "writable": True},
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="doctor.campaign.storage",
                status=STATUS_WARN,
                message=f"campaign storage directory is not writable ({storage_rel})",
                detail={
                    "storage_dir": storage_rel,
                    "writable": False,
                    "actionable": True,
                    "owner_action": True,
                },
                remediation=f"Ensure write permissions for {storage_rel}.",
            )
        )

    # 4. Cloud Upload Readiness Check
    cloud_token_env = DEFAULT_CLOUD_TOKEN_ENV
    cloud_resolution = resolve_cloud_token(
        token_env=cloud_token_env,
        token_dir=(token_dir or DEFAULT_CLOUD_TOKEN_DIR).expanduser(),
        env=current_env,
    )
    if cloud_resolution.has_token:
        checks.append(
            DoctorCheck(
                name="doctor.campaign.cloud_upload",
                status=STATUS_PASS,
                message="Code Mower Cloud upload token configured",
                detail={
                    "token_env": cloud_token_env,
                    "configured": True,
                    "source": cloud_resolution.source,
                },
            )
        )
    elif cloud_resolution.status == "ambiguous":
        checks.append(
            DoctorCheck(
                name="doctor.campaign.cloud_upload",
                status=STATUS_WARN,
                message="multiple Code Mower Cloud token profiles found; none selected",
                detail={
                    "token_env": cloud_token_env,
                    "configured": False,
                    "status": cloud_resolution.status,
                    "candidate_files": list(cloud_resolution.token_files),
                    "optional": True,
                    "actionable": False,
                },
                remediation=(
                    "Select a current profile with `code-mower cloud setup --token-stdin`, "
                    "or pass --token-file when uploading."
                ),
            )
        )
    elif cloud_resolution.status == "malformed":
        candidate_files = list(cloud_resolution.token_files)
        if cloud_resolution.token_file is not None:
            candidate_files.append(cloud_resolution.token_file.name)
        checks.append(
            DoctorCheck(
                name="doctor.campaign.cloud_upload",
                status=STATUS_WARN,
                message="stored Code Mower Cloud token profile is malformed",
                detail={
                    "token_env": cloud_token_env,
                    "configured": False,
                    "status": cloud_resolution.status,
                    "candidate_files": sorted(set(candidate_files)),
                    "optional": True,
                    "actionable": False,
                },
                remediation=(
                    "Run `code-mower cloud setup --token-stdin` again, or pass "
                    "--token-file with a sourceable token env file when uploading."
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="doctor.campaign.cloud_upload",
                status=STATUS_WARN,
                message="optional Code Mower Cloud upload token is not configured",
                detail={
                    "token_env": cloud_token_env,
                    "configured": False,
                    "optional": True,
                    "actionable": False,
                },
                remediation=(
                    "Cloud upload is optional. Set CODE_MOWER_CLOUD_TOKEN or run "
                    "`code-mower cloud setup --token-stdin` to enable qualification uploads."
                ),
            )
        )

    # 5. Board Visibility Check
    board_info = lane_status.collect_local_boards(runner)
    raw_boards = board_info.get("boards") or []
    redacted_boards = [
        {
            "port": b.get("port"),
            "process": b.get("process"),
            "confidence": b.get("confidence"),
        }
        for b in raw_boards
        if isinstance(b, Mapping)
    ]

    if redacted_boards:
        ports_str = ", ".join(str(b["port"]) for b in redacted_boards if b.get("port"))
        checks.append(
            DoctorCheck(
                name="doctor.campaign.board_visibility",
                status=STATUS_PASS,
                message=(
                    f"Code Mower Board visible on port {ports_str}"
                    if ports_str
                    else f"Code Mower Board visible ({len(redacted_boards)} running)"
                ),
                detail={
                    "available": True,
                    "board_count": len(redacted_boards),
                    "boards": redacted_boards,
                },
            )
        )
    else:
        available = bool(board_info.get("available", False))
        checks.append(
            DoctorCheck(
                name="doctor.campaign.board_visibility",
                status=STATUS_WARN,
                message=(
                    "Code Mower Board is not running locally"
                    if available
                    else "local Board listener inventory unavailable"
                ),
                detail={
                    "available": available,
                    "board_count": 0,
                    "boards": [],
                    "optional": True,
                    "actionable": False,
                },
                remediation=(
                    "Start Code Mower Board with `code-mower board serve --repo OWNER/REPO` "
                    "to monitor campaign progress."
                    if available
                    else "Verify local listener tools (lsof or ss) are available to inspect running Boards."
                ),
            )
        )

    provider_checks = [
        check
        for check in checks
        if check.name in {"doctor.campaign.adapter", "doctor.campaign.credentials"}
    ]
    ready_providers = sorted(
        {check.lane for check in provider_checks if check.status == STATUS_PASS and check.lane}
    )
    actionable_providers = sorted(
        {
            check.lane
            for check in provider_checks
            if check.status == STATUS_WARN
            and isinstance(check.detail, Mapping)
            and check.detail.get("actionable")
            and check.lane
        }
    )
    optional_providers = sorted(
        {
            check.lane
            for check in provider_checks
            if check.status == STATUS_WARN
            and isinstance(check.detail, Mapping)
            and check.detail.get("optional")
            and check.lane
        }
    )
    preview_command = (
        "code-mower release campaign --release-tag RELEASE_TAG "
        "--package-spec PACKAGE==VERSION --repo-slug OWNER/REPO"
    )
    storage_ready = any(
        check.name == "doctor.campaign.storage" and check.status == STATUS_PASS
        for check in checks
    )
    if actionable_providers or not storage_ready:
        readiness_status = STATUS_WARN
        readiness_message = "release campaign needs configuration before dispatch"
        readiness_remediation = (
            "Resolve the actionable campaign checks above, then preview with "
            f"`{preview_command}`."
        )
    elif ready_providers:
        readiness_status = STATUS_PASS
        readiness_message = (
            f"release campaign preview is ready for {len(ready_providers)} provider(s)"
        )
        readiness_remediation = f"Preview with `{preview_command}`."
    else:
        readiness_status = STATUS_WARN
        readiness_message = "no release campaign provider is ready in this posture"
        readiness_remediation = (
            "Configure at least one campaign provider, then preview with "
            f"`{preview_command}`."
        )
    checks.append(
        DoctorCheck(
            name="doctor.campaign.readiness",
            status=readiness_status,
            message=readiness_message,
            detail={
                "ready_providers": ready_providers,
                "actionable_providers": actionable_providers,
                "optional_providers": optional_providers,
                "preview_command": preview_command,
            },
            remediation=readiness_remediation,
        )
    )

    return tuple(checks)
