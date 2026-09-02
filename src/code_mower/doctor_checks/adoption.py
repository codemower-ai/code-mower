"""First-run adoption checks for doctor."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .models import STATUS_PASS, STATUS_WARN, DoctorCheck


OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
OWNER_LOGIN_PLACEHOLDER = "TODO_OWNER_LOGIN"


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


def check_adoption_setup(
    *,
    config: Mapping[str, Any] | None,
    config_path: Path,
    adoption: bool,
    repo_slug: str,
    repo_source: str,
    using_packaged_example: bool,
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
    if using_packaged_example:
        checks.append(
            DoctorCheck(
                name="doctor.adoption.config_source",
                status=STATUS_WARN,
                message="using packaged starter config for adoption checks",
                detail={
                    "config_path": str(config_path),
                    "configured_repositories": repositories,
                    "effective_repository": repo_slug,
                },
                remediation=(
                    "Run `code-mower init --easy --apply`, review the generated "
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
                message="using repository Code Mower config",
                detail={
                    "config_path": str(config_path),
                    "configured_repositories": repositories,
                    "effective_repository": repo_slug,
                },
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
    if not owner_login or owner_login.lower() == OWNER_LOGIN_PLACEHOLDER.lower():
        checks.append(
            DoctorCheck(
                name="doctor.adoption.owner_login",
                status=STATUS_WARN,
                message="owner_surface.owner_login is not configured",
                detail={"owner_login": owner_login},
                remediation=(
                    "Set owner_surface.owner_login to the decision owner's "
                    "GitHub login before trusting manual decisions or owner "
                    "notifications."
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

    checks.append(
        DoctorCheck(
            name="doctor.adoption.trusted_authors",
            status=STATUS_WARN,
            message="human-run audit comments must be listed as trusted lane authors",
            detail={
                "variables": [
                    "CLAUDE_AUDIT_BOT_AUTHORS",
                    "CODEX_BOT_AUTHORS",
                ]
            },
            remediation=(
                "For manual pilot audits, set repository variables such as "
                "CLAUDE_AUDIT_BOT_AUTHORS and CODEX_BOT_AUTHORS to the "
                "GitHub logins that may post trusted audit verdicts."
            ),
        )
    )
    return tuple(checks)
