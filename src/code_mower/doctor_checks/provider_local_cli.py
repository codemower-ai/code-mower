"""Provider local CLI discovery and auth smoke probes."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .common import (
    SUPPORTED_TOKEN_FILE_ENV_NAMES,
    DoctorCheck,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    as_sequence,
    code_mower_secrets,
    local_cli_remediation,
)
from .provider_probe import evaluate_json_probe, local_cli_probe_remediation
from .runtime import auth_probe_output_detail


def _candidate_local_cli_commands(lane: Mapping[str, Any]) -> list[str]:
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


def _local_cli_command(lane: Mapping[str, Any]) -> str:
    candidates = _candidate_local_cli_commands(lane)
    if candidates:
        return candidates[0]
    provider = str(lane.get("provider", "")).replace("_", "-")
    return provider or "unknown"


def check_local_cli(lane_id: str, lane: Mapping[str, Any]) -> DoctorCheck:
    provider_config = lane.get("provider_config", {})
    commands = _candidate_local_cli_commands(lane)
    detail: dict[str, Any] = {"commands": commands}
    if isinstance(provider_config, Mapping) and provider_config.get("command"):
        detail["command"] = str(provider_config["command"])
    if isinstance(provider_config, Mapping) and provider_config.get("command_env"):
        detail["command_env"] = str(provider_config["command_env"])
    if isinstance(provider_config, Mapping) and provider_config.get("protocol"):
        detail["protocol"] = str(provider_config["protocol"])
    for command in commands:
        resolved = shutil.which(command)
        if resolved:
            detail.update({"command": command, "path": resolved})
            return DoctorCheck(
                name="runtime.local_cli",
                status=STATUS_PASS,
                lane=lane_id,
                message=f"{command} found",
                detail=detail,
            )
    return DoctorCheck(
        name="runtime.local_cli",
        status=STATUS_WARN,
        lane=lane_id,
        message=f"none of the candidate commands were found: {', '.join(commands)}",
        detail=detail,
        remediation=local_cli_remediation(
            commands,
            str(detail.get("command_env", "")),
        ),
    )


def _resolved_local_cli_command(lane: Mapping[str, Any]) -> tuple[str, str] | None:
    for command in _candidate_local_cli_commands(lane):
        resolved = shutil.which(command)
        if resolved:
            return command, resolved
    return None


def _local_cli_probe_args(lane: Mapping[str, Any], command: str) -> tuple[str, ...]:
    provider_config = lane.get("provider_config", {})
    if isinstance(provider_config, Mapping):
        raw_probe = provider_config.get("doctor_probe_args")
        if isinstance(raw_probe, (list, tuple)) and raw_probe:
            return tuple(str(part) for part in raw_probe)
    provider = str(lane.get("provider") or "")
    if provider in {"gemini", "antigravity", "coderabbit", "claude", "codex"}:
        return ("--version",)
    return ("--help",)


def _local_cli_probe_timeout(lane: Mapping[str, Any], default_timeout: int) -> int:
    provider_config = lane.get("provider_config", {})
    if not isinstance(provider_config, Mapping):
        return default_timeout
    raw_timeout = provider_config.get("doctor_probe_timeout_seconds")
    if raw_timeout is None:
        return default_timeout
    try:
        return max(1, int(raw_timeout))
    except (TypeError, ValueError):
        return default_timeout


def _local_cli_probe_env(lane: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    child_env = os.environ.copy()
    forwarded: list[str] = []

    token_names: list[str] = []
    for item in as_sequence(lane.get("token_env", [])):
        token_names.append(str(item))
    for group in as_sequence(lane.get("token_env_any", [])):
        for item in as_sequence(group):
            token_names.append(str(item))

    for name in token_names:
        if name not in SUPPORTED_TOKEN_FILE_ENV_NAMES or child_env.get(name):
            continue
        path_text = child_env.get(f"{name}_FILE", "").strip()
        if not path_text:
            continue
        try:
            result = code_mower_secrets.read_secret_file(
                Path(path_text),
                supported_env_names=SUPPORTED_TOKEN_FILE_ENV_NAMES,
            )
        except OSError:
            continue
        if not result.ok:
            continue
        child_env[name] = result.value
        forwarded.append(f"{name}_FILE")

    return child_env, {"token_file_env_forwarded": sorted(set(forwarded))}


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
    resolved_pair = _resolved_local_cli_command(lane)
    if resolved_pair is None:
        command = _local_cli_command(lane)
        return DoctorCheck(
            name="runtime.local_cli.probe",
            status=STATUS_WARN,
            lane=lane_id,
            message=f"{command} was not found, so runtime probe could not run",
            detail={"command": command},
            remediation=local_cli_remediation([command]),
        )
    command, resolved = resolved_pair
    probe_args = _local_cli_probe_args(lane, command)
    timeout_seconds = _local_cli_probe_timeout(lane, http_timeout)
    child_env, env_detail = _local_cli_probe_env(lane)
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
        return DoctorCheck(
            name="runtime.local_cli.probe",
            status=STATUS_WARN,
            lane=lane_id,
            message=f"{command} probe failed: {exc}",
            detail={
                "command": command,
                "path": resolved,
                "args": list(probe_args),
                "timeout_seconds": timeout_seconds,
                **env_detail,
            },
            remediation=local_cli_probe_remediation(command, probe_args, lane),
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
            f"{command} {json_message}"
            if json_message
            else (
                f"{command} probe succeeded"
                if status == STATUS_PASS
                else f"{command} probe exited {completed.returncode}"
            )
        ),
        detail={
            "command": command,
            "path": resolved,
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
                command,
                probe_args,
                lane,
                auth_error_detected=bool(json_detail.get("auth_error_detected")),
            )
        ),
    )
