"""Provider local CLI probe argument, timeout, and environment helpers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .common import (
    SUPPORTED_TOKEN_FILE_ENV_NAMES,
    as_sequence,
    code_mower_secrets,
)


def local_cli_probe_args(lane: Mapping[str, Any], command: str) -> tuple[str, ...]:
    provider_config = lane.get("provider_config", {})
    if isinstance(provider_config, Mapping):
        raw_probe = provider_config.get("doctor_probe_args")
        if isinstance(raw_probe, (list, tuple)) and raw_probe:
            return tuple(str(part) for part in raw_probe)
    provider = str(lane.get("provider") or "")
    if provider in {"gemini", "antigravity", "coderabbit", "claude", "codex"}:
        return ("--version",)
    return ("--help",)


def local_cli_probe_timeout(lane: Mapping[str, Any], default_timeout: int) -> int:
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


def local_cli_probe_env(lane: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
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
