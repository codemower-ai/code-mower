"""Local CLI provider metadata probes."""

from __future__ import annotations

import subprocess
from typing import Any


def safe_version_line(output: str) -> str:
    """Return a compact, display-safe first version line."""

    line = next((item.strip() for item in output.splitlines() if item.strip()), "")
    if not line:
        return ""
    return " ".join(line.split())[:180]


def detect_local_cli_version(resolved: str) -> dict[str, Any]:
    """Run a short version probe for display-only tool provenance."""

    try:
        completed = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"tool_version_available": False, "tool_version_error": str(exc)[:180]}
    output = (completed.stdout or completed.stderr or "").strip()
    version = safe_version_line(output)
    if not version:
        return {
            "tool_version_available": False,
            "tool_version_returncode": completed.returncode,
        }
    return {
        "tool_version_available": completed.returncode == 0,
        "tool_version": version,
        "tool_version_returncode": completed.returncode,
    }
