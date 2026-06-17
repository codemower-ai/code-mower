"""GitHub CLI/token auth doctor probe."""

from __future__ import annotations

import os
import shutil
import subprocess

from .models import STATUS_PASS, STATUS_WARN, DoctorCheck
from .privacy import auth_probe_output_detail


def _github_auth_probe(
    gh_path: str,
    *,
    http_timeout: int,
) -> tuple[int | None, str, str | None]:
    try:
        completed = subprocess.run(
            [gh_path, "auth", "status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=http_timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)
    output = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode, output, None


def _github_auth_probe_check(
    *,
    token_env: list[str],
    gh_path: str,
    http_timeout: int,
) -> DoctorCheck:
    returncode, output, error = _github_auth_probe(
        gh_path,
        http_timeout=http_timeout,
    )
    if error is not None:
        qualifier = "token env is set, but " if token_env else ""
        return DoctorCheck(
            name="runtime.github_auth",
            status=STATUS_WARN,
            message=f"GitHub {qualifier}auth probe failed: {error}",
            detail={"token_env": token_env, "gh_path": gh_path},
            remediation=(
                "Run `gh auth status` locally, refresh the token if needed, "
                "or export a valid GITHUB_TOKEN/GH_TOKEN."
                if token_env
                else "Run `gh auth login` or export GITHUB_TOKEN/GH_TOKEN, "
                "then rerun doctor."
            ),
        )
    if returncode == 0:
        message = (
            "GitHub token env and CLI auth probe are valid"
            if token_env
            else "GitHub CLI auth probe succeeded"
        )
        return DoctorCheck(
            name="runtime.github_auth",
            status=STATUS_PASS,
            message=message,
            detail={
                "token_env": token_env,
                "gh_path": gh_path,
                "returncode": returncode,
                **auth_probe_output_detail(output),
            },
        )
    message = (
        f"GitHub token env is set, but auth probe exited {returncode}"
        if token_env
        else f"GitHub CLI auth probe exited {returncode}"
    )
    return DoctorCheck(
        name="runtime.github_auth",
        status=STATUS_WARN,
        message=message,
        detail={
            "token_env": token_env,
            "gh_path": gh_path,
            "returncode": returncode,
            **auth_probe_output_detail(output),
        },
        remediation=(
            "Run `gh auth status`, refresh CLI auth with `gh auth login`, "
            "or export a valid GITHUB_TOKEN/GH_TOKEN."
            if token_env
            else "Run `gh auth login` or export GITHUB_TOKEN/GH_TOKEN, "
            "then rerun doctor."
        ),
    )


def check_github_auth_surface(
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> DoctorCheck:
    token_env = [name for name in ("GITHUB_TOKEN", "GH_TOKEN") if os.environ.get(name)]
    gh_path = shutil.which("gh")
    if token_env:
        if probe_runtime and gh_path:
            return _github_auth_probe_check(
                token_env=token_env,
                gh_path=gh_path,
                http_timeout=http_timeout,
            )
        return DoctorCheck(
            name="runtime.github_auth",
            status=STATUS_PASS,
            message="GitHub token env is set",
            detail={"token_env": token_env, "gh_path": gh_path or ""},
        )
    if gh_path:
        if probe_runtime:
            return _github_auth_probe_check(
                token_env=[],
                gh_path=gh_path,
                http_timeout=http_timeout,
            )
        return DoctorCheck(
            name="runtime.github_auth",
            status=STATUS_WARN,
            message="GitHub CLI is available, but token env is not set and auth was not probed",
            detail={"token_env": [], "gh_path": gh_path},
            remediation=(
                "Run with --probe-runtime to verify gh auth, or export "
                "GITHUB_TOKEN/GH_TOKEN before enabling GitHub-backed lanes."
            ),
        )
    return DoctorCheck(
        name="runtime.github_auth",
        status=STATUS_WARN,
        message="neither GITHUB_TOKEN/GH_TOKEN nor gh CLI was found",
        detail={"token_env": [], "gh_path": ""},
        remediation=(
            "Install the GitHub CLI and run `gh auth login`, or export "
            "GITHUB_TOKEN/GH_TOKEN."
        ),
    )


__all__ = ("check_github_auth_surface",)
