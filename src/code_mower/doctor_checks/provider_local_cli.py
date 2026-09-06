"""Provider local CLI discovery and auth smoke probes."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Mapping

from code_mower.providers import detect_local_cli_version

from .common import (
    DoctorCheck,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    local_cli_remediation,
)
from .provider_local_cli_commands import (
    candidate_local_cli_commands,
    local_cli_command,
    resolved_local_cli_command,
)
from .provider_local_cli_probe_config import (
    local_cli_probe_args,
    local_cli_probe_env,
    local_cli_probe_timeout,
)
from .provider_probe import evaluate_json_probe, local_cli_probe_remediation
from .privacy import auth_probe_output_detail


def _local_cli_reported_value(lane: Mapping[str, Any], value: str) -> str:
    """Return a command/path value safe to report for this lane.

    Lanes that opt in via ``local_cli_path_basename_only`` get
    ``os.path.basename`` applied to every reported value, so a configured
    absolute-path command override (for example ``CODE_MOWER_DEVIN_CLI_COMMAND``)
    can never leak a local directory into doctor output. All other lanes report
    the value unchanged. Execution always uses the raw resolved path; only the
    reported form is reduced.
    """
    provider_config = lane.get("provider_config", {})
    if isinstance(provider_config, Mapping) and bool(
        provider_config.get("local_cli_path_basename_only")
    ):
        return os.path.basename(value)
    return value


def _local_cli_reported_commands(lane: Mapping[str, Any], commands: list[str]) -> list[str]:
    reported: list[str] = []
    for command in commands:
        value = _local_cli_reported_value(lane, command)
        if value and value not in reported:
            reported.append(value)
    return reported


def check_local_cli(lane_id: str, lane: Mapping[str, Any]) -> DoctorCheck:
    provider_config = lane.get("provider_config", {})
    commands = candidate_local_cli_commands(lane)
    reported_commands = _local_cli_reported_commands(lane, commands)
    detail: dict[str, Any] = {"commands": reported_commands}
    if isinstance(provider_config, Mapping) and provider_config.get("command"):
        detail["command"] = _local_cli_reported_value(
            lane, str(provider_config["command"])
        )
    if isinstance(provider_config, Mapping) and provider_config.get("command_env"):
        detail["command_env"] = str(provider_config["command_env"])
    if isinstance(provider_config, Mapping) and provider_config.get("protocol"):
        detail["protocol"] = str(provider_config["protocol"])
    for command in commands:
        resolved = shutil.which(command)
        if resolved:
            version_detail = detect_local_cli_version(resolved)
            reported_command = _local_cli_reported_value(lane, command)
            detail.update(
                {
                    "command": reported_command,
                    "path": _local_cli_reported_value(lane, resolved),
                    **version_detail,
                }
            )
            version = version_detail.get("tool_version")
            return DoctorCheck(
                name="runtime.local_cli",
                status=STATUS_PASS,
                lane=lane_id,
                message=f"{reported_command} found"
                + (f" ({version})" if version else ""),
                detail=detail,
            )
    return DoctorCheck(
        name="runtime.local_cli",
        status=STATUS_WARN,
        lane=lane_id,
        message=f"none of the candidate commands were found: {', '.join(reported_commands)}",
        detail=detail,
        remediation=local_cli_remediation(
            reported_commands,
            str(detail.get("command_env", "")),
        ),
    )


def check_local_cli_probe(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> DoctorCheck:
    if not probe_runtime:
        return DoctorCheck(
            name="runtime.local_cli.probe",
            status=STATUS_SKIP,
            lane=lane_id,
            message="local CLI probing skipped; pass --probe-runtime to run a harmless version/help command",
        )
    resolved_pair = resolved_local_cli_command(lane)
    if resolved_pair is None:
        command = local_cli_command(lane)
        reported_command = _local_cli_reported_value(lane, command)
        return DoctorCheck(
            name="runtime.local_cli.probe",
            status=STATUS_WARN,
            lane=lane_id,
            message=f"{reported_command} was not found, so runtime probe could not run",
            detail={"command": reported_command},
            remediation=local_cli_remediation([reported_command]),
        )
    command, resolved = resolved_pair
    reported_command = _local_cli_reported_value(lane, command)
    reported_path = _local_cli_reported_value(lane, resolved)
    probe_args = local_cli_probe_args(lane, command)
    timeout_seconds = local_cli_probe_timeout(lane, http_timeout)
    child_env, env_detail = local_cli_probe_env(lane)
    try:
        completed = subprocess.run(
            [resolved, *probe_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        exc_text = str(exc)
        if reported_path != resolved:
            exc_text = exc_text.replace(resolved, reported_path)
        if reported_command != command:
            exc_text = exc_text.replace(command, reported_command)
        return DoctorCheck(
            name="runtime.local_cli.probe",
            status=STATUS_WARN,
            lane=lane_id,
            message=f"{reported_command} probe failed: {exc_text}",
            detail={
                "command": reported_command,
                "path": reported_path,
                "args": list(probe_args),
                "timeout_seconds": timeout_seconds,
                **env_detail,
            },
            remediation=local_cli_probe_remediation(reported_command, probe_args, lane),
        )
    output = (completed.stdout or completed.stderr or "").strip()
    provider_config = lane.get("provider_config", {})
    json_detail: dict[str, Any] = {}
    json_message = ""
    if isinstance(provider_config, Mapping) and provider_config.get("doctor_probe_expect_json"):
        status, json_message, json_detail = evaluate_json_probe(
            provider_config,
            output,
            returncode=completed.returncode,
        )
    else:
        status = STATUS_PASS if completed.returncode == 0 else STATUS_WARN
    return DoctorCheck(
        name="runtime.local_cli.probe",
        status=status,
        lane=lane_id,
        message=(
            f"{reported_command} {json_message}"
            if json_message
            else (
                f"{reported_command} probe succeeded"
                if status == STATUS_PASS
                else f"{reported_command} probe exited {completed.returncode}"
            )
        ),
        detail={
            "command": reported_command,
            "path": reported_path,
            "args": list(probe_args),
            "timeout_seconds": timeout_seconds,
            "returncode": completed.returncode,
            **env_detail,
            **json_detail,
            **auth_probe_output_detail(output),
        },
        remediation=(
            None
            if status == STATUS_PASS
            else local_cli_probe_remediation(
                reported_command,
                probe_args,
                lane,
                auth_error_detected=bool(json_detail.get("auth_error_detected")),
            )
        ),
    )
