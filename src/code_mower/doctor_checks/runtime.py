"""Base runtime checks shared across Code Mower doctor profiles."""

from __future__ import annotations

import importlib.util
import shutil
import sys

from .common import DoctorCheck, STATUS_FAIL, STATUS_PASS, STATUS_WARN
from .github import _check_github_auth_surface


def _check_python_runtime() -> DoctorCheck:
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


def _check_ripgrep() -> DoctorCheck:
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


def _check_pytest() -> DoctorCheck:
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


def _global_runtime_checks(
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> list[DoctorCheck]:
    return [
        _check_python_runtime(),
        _check_pytest(),
        _check_github_auth_surface(
            probe_runtime=probe_runtime,
            http_timeout=http_timeout,
        ),
        _check_ripgrep(),
    ]

