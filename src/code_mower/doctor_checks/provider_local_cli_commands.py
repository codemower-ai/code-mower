"""Provider local CLI command discovery helpers."""

from __future__ import annotations

import shutil
from typing import Any, Mapping

from code_mower.local_cli_commands import candidate_local_cli_commands

__all__ = [
    "candidate_local_cli_commands",
    "local_cli_command",
    "resolved_local_cli_command",
]


def local_cli_command(lane: Mapping[str, Any]) -> str:
    candidates = candidate_local_cli_commands(lane)
    if candidates:
        return candidates[0]
    provider = str(lane.get("provider", "")).replace("_", "-")
    return provider or "unknown"


def resolved_local_cli_command(lane: Mapping[str, Any]) -> tuple[str, str] | None:
    for command in candidate_local_cli_commands(lane):
        resolved = shutil.which(command)
        if resolved:
            return command, resolved
    return None
