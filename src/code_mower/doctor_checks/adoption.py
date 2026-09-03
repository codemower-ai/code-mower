"""First-run adoption checks for doctor."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import STATUS_PASS, STATUS_WARN, DoctorCheck


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
