"""Local provider CLI command discovery shared by lane runtime and readiness.

Runtime checks and Devin readiness must agree about which executable a lane
would run, and readiness resolves it from an injected environment mapping so its
results are host independent, so the discovery order lives in this neutral
module instead of the doctor check package.
"""

from __future__ import annotations

import os
from typing import Any, Mapping


def candidate_local_cli_commands(
    lane: Mapping[str, Any], *, env: Mapping[str, str] | None = None
) -> list[str]:
    """Return the lane's command candidates in the order runtime would try them."""
    environ = os.environ if env is None else env
    provider_config = lane.get("provider_config", {})
    commands: list[str] = []
    if isinstance(provider_config, Mapping):
        command_env = str(provider_config.get("command_env", ""))
        if command_env and environ.get(command_env):
            commands.append(str(environ[command_env]))
        if provider_config.get("command"):
            commands.append(str(provider_config["command"]))
        alternates = provider_config.get("alternate_commands", [])
        if isinstance(alternates, (list, tuple)):
            for command in alternates:
                if command:
                    commands.append(str(command))
    if not commands:
        commands.append(str(lane.get("provider", "")).replace("_", "-"))
    deduped: list[str] = []
    for command in commands:
        if command and command not in deduped:
            deduped.append(command)
    return deduped
