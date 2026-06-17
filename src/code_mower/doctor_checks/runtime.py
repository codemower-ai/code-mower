"""Runtime and local toolchain doctor checks."""

from __future__ import annotations

import importlib.util
import shutil
import sys

from .models import STATUS_FAIL, STATUS_PASS, STATUS_WARN, DoctorCheck
from .privacy import auth_probe_output_detail
from .runtime_github_auth import check_github_auth_surface

__all__ = (
    "auth_probe_output_detail",
    "check_github_auth_surface",
    "check_pytest",
    "check_python_runtime",
    "check_ripgrep",
)


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
