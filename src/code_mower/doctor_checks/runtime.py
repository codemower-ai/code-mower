"""Runtime and local toolchain doctor checks."""

from __future__ import annotations

import importlib.util
import plistlib
import shutil
import sys
from pathlib import Path

from .models import STATUS_FAIL, STATUS_PASS, STATUS_SKIP, STATUS_WARN, DoctorCheck
from .privacy import auth_probe_output_detail
from .runtime_github_auth import check_github_auth_surface

__all__ = (
    "auth_probe_output_detail",
    "check_github_auth_surface",
    "check_macos_runner_launchagent",
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


def check_macos_runner_launchagent(
    *,
    home: Path | None = None,
    platform: str = sys.platform,
) -> DoctorCheck:
    if platform != "darwin":
        return DoctorCheck(
            name="runtime.macos_runner_launchagent",
            status=STATUS_SKIP,
            message="macOS runner LaunchAgent check skipped on this platform",
        )

    try:
        home_dir = home or Path.home()
    except RuntimeError as exc:
        return DoctorCheck(
            name="runtime.macos_runner_launchagent",
            status=STATUS_WARN,
            message="could not resolve home directory for macOS runner LaunchAgent check",
            detail={"error": str(exc)},
            remediation=(
                "Set HOME for the runner account or pass an explicit home path, "
                "then rerun doctor to inspect actions.runner.*.plist."
            ),
        )

    launch_agents = home_dir / "Library" / "LaunchAgents"
    plists = sorted(launch_agents.glob("actions.runner.*.plist"))
    if not plists:
        return DoctorCheck(
            name="runtime.macos_runner_launchagent",
            status=STATUS_SKIP,
            message="no GitHub Actions runner LaunchAgent plist found",
            detail={"glob": str(launch_agents / "actions.runner.*.plist")},
        )

    bad: list[str] = []
    unreadable: list[str] = []
    for plist_path in plists:
        try:
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException):
            unreadable.append(str(plist_path))
            continue
        if isinstance(payload, dict) and payload.get("SessionCreate") is True:
            bad.append(str(plist_path))

    remediation = (
        "Remove the `SessionCreate` key from the runner LaunchAgent plist, then "
        "unload and reload the LaunchAgent or fully recycle the listener process. "
        "Afterward, run `claude -p \"Reply with exactly: ok\" --output-format json` "
        "from a runner job to verify login-keychain access."
    )
    if bad:
        return DoctorCheck(
            name="runtime.macos_runner_launchagent",
            status=STATUS_WARN,
            message=(
                "GitHub Actions runner LaunchAgent has SessionCreate=true; "
                "Claude Code may not see the login keychain"
            ),
            detail={"session_create_plists": bad, "unreadable_plists": unreadable},
            remediation=remediation,
        )
    if unreadable:
        return DoctorCheck(
            name="runtime.macos_runner_launchagent",
            status=STATUS_WARN,
            message="could not inspect every GitHub Actions runner LaunchAgent plist",
            detail={"unreadable_plists": unreadable},
            remediation="Fix permissions on the unreadable plist(s), then rerun doctor.",
        )
    return DoctorCheck(
        name="runtime.macos_runner_launchagent",
        status=STATUS_PASS,
        message="GitHub Actions runner LaunchAgent plists do not set SessionCreate=true",
        detail={"plists": [str(path) for path in plists]},
    )
