"""Provider local CLI command discovery helpers."""

from __future__ import annotations

import os
import shutil
from typing import Any, Mapping

from .common import as_sequence


def candidate_local_cli_commands(lane: Mapping[str, Any]) -> list[str]:
    provider_config = lane.get("provider_config", {})
    commands: list[str] = []
    if isinstance(provider_config, Mapping):
        command_env = str(provider_config.get("command_env", ""))
        if command_env and os.environ.get(command_env):
            commands.append(str(os.environ[command_env]))
        if provider_config.get("command"):
            commands.append(str(provider_config["command"]))
        for command in as_sequence(provider_config.get("alternate_commands", [])):
            if command:
                commands.append(str(command))
    if not commands:
        provider = str(lane.get("provider", ""))
        commands.append(provider.replace("_", "-"))
    deduped: list[str] = []
    for command in commands:
        if command and command not in deduped:
            deduped.append(command)
    return deduped


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
