"""Runtime and local toolchain doctor checks."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys

from .models import STATUS_FAIL, STATUS_PASS, STATUS_WARN, DoctorCheck
from .privacy import auth_probe_output_detail


def check_python_runtime() -> DoctorCheck:
    version = ".".join(str(part) for part in sys.version_info[:3])
    status = STATUS_PASS if sys.version_info >= (3, 11) else STATUS_FAIL
    return DoctorCheck(
        name="runtime.python",
        status=status,
        message=(
            f"Python {version} satisfies Code Mower's >=3.11 requirement"
            if status == STATUS_PASS
            else f"Python {version} is too old; Code Mower requires >=3.11"
        ),
        detail={
            "executable": sys.executable,
            "version": version,
            "required": ">=3.11",
        },
        remediation=(
            None
            if status == STATUS_PASS
            else "Run Code Mower with Python >=3.11, then rerun doctor."
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
            try:
                completed = subprocess.run(
                    [gh_path, "auth", "status"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=http_timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return DoctorCheck(
                    name="runtime.github_auth",
                    status=STATUS_WARN,
                    message=f"GitHub token env is set, but auth probe failed: {exc}",
                    detail={"token_env": token_env, "gh_path": gh_path},
                    remediation=(
                        "Run `gh auth status` locally, refresh the token if needed, "
                        "or export a valid GITHUB_TOKEN/GH_TOKEN."
                    ),
                )
            output = (completed.stdout or completed.stderr or "").strip()
            if completed.returncode == 0:
                return DoctorCheck(
                    name="runtime.github_auth",
                    status=STATUS_PASS,
                    message="GitHub token env and CLI auth probe are valid",
                    detail={
                        "token_env": token_env,
                        "gh_path": gh_path,
                        "returncode": completed.returncode,
                        **auth_probe_output_detail(output),
                    },
                )
            return DoctorCheck(
                name="runtime.github_auth",
                status=STATUS_WARN,
                message=f"GitHub token env is set, but auth probe exited {completed.returncode}",
                detail={
                    "token_env": token_env,
                    "gh_path": gh_path,
                    "returncode": completed.returncode,
                    **auth_probe_output_detail(output),
                },
                remediation=(
                    "Run `gh auth status`, refresh CLI auth with `gh auth login`, "
                    "or export a valid GITHUB_TOKEN/GH_TOKEN."
                ),
            )
        return DoctorCheck(
            name="runtime.github_auth",
            status=STATUS_PASS,
            message="GitHub token env is set",
            detail={"token_env": token_env, "gh_path": gh_path or ""},
        )
    if gh_path:
        if probe_runtime:
            try:
                completed = subprocess.run(
                    [gh_path, "auth", "status"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=http_timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return DoctorCheck(
                    name="runtime.github_auth",
                    status=STATUS_WARN,
                    message=f"GitHub CLI auth probe failed: {exc}",
                    detail={"token_env": [], "gh_path": gh_path},
                    remediation=(
                        "Run `gh auth login` or export GITHUB_TOKEN/GH_TOKEN, "
                        "then rerun doctor."
                    ),
                )
            output = (completed.stdout or completed.stderr or "").strip()
            if completed.returncode == 0:
                return DoctorCheck(
                    name="runtime.github_auth",
                    status=STATUS_PASS,
                    message="GitHub CLI auth probe succeeded",
                    detail={
                        "token_env": [],
                        "gh_path": gh_path,
                        "returncode": completed.returncode,
                        **auth_probe_output_detail(output),
                    },
                )
            return DoctorCheck(
                name="runtime.github_auth",
                status=STATUS_WARN,
                message=f"GitHub CLI auth probe exited {completed.returncode}",
                detail={
                    "token_env": [],
                    "gh_path": gh_path,
                    "returncode": completed.returncode,
                    **auth_probe_output_detail(output),
                },
                remediation=(
                    "Run `gh auth login` or export GITHUB_TOKEN/GH_TOKEN, "
                    "then rerun doctor."
                ),
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


def check_ripgrep() -> DoctorCheck:
    path = shutil.which("rg")
    if path:
        return DoctorCheck(
            name="runtime.ripgrep",
            status=STATUS_PASS,
            message="rg found",
            detail={"command": "rg", "path": path},
        )
    return DoctorCheck(
        name="runtime.ripgrep",
        status=STATUS_WARN,
        message="rg was not found; reviewer CLIs may fall back to slower grep tools",
        detail={"command": "rg"},
        remediation=(
            "Install ripgrep, for example `brew install ripgrep` on macOS or "
            "`apt-get install ripgrep` on Ubuntu, and ensure rg is on PATH."
        ),
    )


def check_pytest() -> DoctorCheck:
    spec = importlib.util.find_spec("pytest")
    if spec is not None:
        return DoctorCheck(
            name="runtime.pytest",
            status=STATUS_PASS,
            message="pytest import is available for product-side test wrappers",
            detail={"module": "pytest"},
        )
    return DoctorCheck(
        name="runtime.pytest",
        status=STATUS_WARN,
        message=(
            "pytest is not installed in this Python environment; standalone "
            "easy-mode does not require it, but product-side Code Mower test "
            "wrappers often do"
        ),
        detail={"module": "pytest"},
        remediation=(
            "Install pytest in the product repository virtualenv before running "
            "product-side wrapper tests, for example `python -m pip install pytest`."
        ),
    )
