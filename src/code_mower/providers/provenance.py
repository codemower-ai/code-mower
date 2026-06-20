"""Metadata-only provider/tool provenance helpers."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from typing import Any

from .local_cli import detect_local_cli_version

TOOL_PROVENANCE_SCHEMA = "code_mower.toolProvenance.v1"

TOOL_PROVENANCE_FIELDS = (
    "schema",
    "role",
    "tool_name",
    "tool_version",
    "provider",
    "model",
    "model_version_raw",
    "integration",
    "lens",
    "runtime_environment",
    "prompt_pack_version",
    "source",
)


def _safe_text(value: Any, *, max_length: int = 180) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:max_length]


def runtime_environment() -> str:
    """Return a coarse, privacy-safe runtime environment label."""

    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github-actions"
    if os.environ.get("CI"):
        return "ci"
    return "local"


def _role_for_event_type(event_type: str) -> str:
    if event_type in {"reviewer_run", "calibration_run", "value_report_snapshot"}:
        return "reviewer"
    if event_type == "workflow_run":
        return "workflow"
    if event_type == "dogfood_upload":
        return "reporter"
    if event_type == "lane_policy_snapshot":
        return "policy"
    return "unknown"


def normalize_tool_provenance(
    value: Any,
    *,
    event: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Normalize tool/model provenance without accepting raw transcripts.

    The returned object intentionally stores only coarse metadata that helps
    compare AI builders/reviewers: tool name/version, provider, model, lens,
    integration, and runtime surface. It must not include prompts, diffs,
    account identifiers, auth output, or session ids.
    """

    source: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
    event = event or {}
    event_type = _safe_text(event.get("event_type"))
    provider = _safe_text(source.get("provider") or event.get("provider"))
    lens = _safe_text(source.get("lens") or event.get("lens"))
    integration = _safe_text(source.get("integration") or event.get("source"))
    tool_name = _safe_text(
        source.get("tool_name")
        or source.get("name")
        or provider
        or integration
        or event.get("source")
    )
    return {
        "schema": TOOL_PROVENANCE_SCHEMA,
        "role": _safe_text(source.get("role") or _role_for_event_type(event_type)),
        "tool_name": tool_name,
        "tool_version": _safe_text(source.get("tool_version") or source.get("version")),
        "provider": provider,
        "model": _safe_text(source.get("model")),
        "model_version_raw": _safe_text(source.get("model_version_raw")),
        "integration": integration,
        "lens": lens,
        "runtime_environment": _safe_text(
            source.get("runtime_environment") or runtime_environment()
        ),
        "prompt_pack_version": _safe_text(source.get("prompt_pack_version")),
        "source": _safe_text(source.get("source") or event.get("source")),
    }


def build_code_mower_tool_provenance(
    *,
    source: str,
    version: str,
    role: str = "reporter",
) -> dict[str, str]:
    """Build provenance for Code Mower-generated operational events."""

    return normalize_tool_provenance(
        {
            "role": role,
            "tool_name": "code-mower",
            "tool_version": version,
            "provider": "code-mower",
            "integration": source,
            "source": source,
        }
    )


def _lane_value(lane: Any, field: str, default: Any = "") -> Any:
    if isinstance(lane, Mapping):
        return lane.get(field, default)
    return getattr(lane, field, default)


def _provider_config(lane: Any) -> Mapping[str, Any]:
    config = _lane_value(lane, "provider_config", {})
    return config if isinstance(config, Mapping) else {}


def _first_text(values: Any) -> str:
    if isinstance(values, str):
        return values
    if isinstance(values, (list, tuple)):
        for value in values:
            text = _safe_text(value)
            if text:
                return text
    return ""


def _env_value(config: Mapping[str, Any], key: str) -> str:
    env_name = _safe_text(config.get(key))
    return _safe_text(os.environ.get(env_name)) if env_name else ""


def _configured_command(config: Mapping[str, Any], provider: str) -> str:
    override = _env_value(config, "command_env")
    if override:
        return override
    command = _safe_text(config.get("command"))
    if command:
        return command
    alternate = _first_text(config.get("alternate_commands"))
    if alternate:
        return alternate
    return provider


def build_provider_lane_tool_provenance(
    lane_id: str,
    lane: Any,
    *,
    source: str,
    include_version_probe: bool = True,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Return metadata-only tool provenance for a configured provider lane.

    This intentionally reports what Code Mower can know from configuration and
    harmless local version probes. It does not read prompts, diffs, transcripts,
    auth status output, or provider stdout/stderr.
    """

    config = _provider_config(lane)
    provider = _safe_text(_lane_value(lane, "provider")) or lane_id
    driver = _safe_text(_lane_value(lane, "driver")) or "unknown"
    command = _configured_command(config, provider)
    model = _env_value(config, "model_env") or _safe_text(config.get("default_model"))
    provider_from_env = _env_value(config, "provider_env")
    if provider_from_env:
        provider = provider_from_env
    lens = (
        _first_text(config.get("prompt_lenses"))
        or _safe_text(config.get("lens"))
        or "base"
    )
    tool_name = (
        command
        if driver == "local_cli"
        else _safe_text(_lane_value(lane, "adapter")) or provider
    )
    detail: dict[str, Any] = {
        "lane_id": lane_id,
        "driver": driver,
        "command": command if driver == "local_cli" else "",
        "model_known": bool(model),
        "version_known": False,
    }
    tool_version = ""
    if driver == "local_cli" and include_version_probe and command:
        resolved = shutil.which(command)
        detail["command_found"] = bool(resolved)
        if resolved:
            detail["path_basename"] = os.path.basename(resolved)
            version_detail = detect_local_cli_version(resolved)
            tool_version = _safe_text(version_detail.get("tool_version"))
            detail["version_known"] = bool(tool_version)
            detail["tool_version_available"] = bool(
                version_detail.get("tool_version_available")
            )
            if version_detail.get("tool_version_returncode") is not None:
                detail["tool_version_returncode"] = version_detail[
                    "tool_version_returncode"
                ]
    tool = normalize_tool_provenance(
        {
            "role": "reviewer",
            "tool_name": tool_name,
            "tool_version": tool_version,
            "provider": provider,
            "model": model,
            "integration": driver.replace("_", "-"),
            "lens": lens,
            "source": source,
        }
    )
    return tool, detail
