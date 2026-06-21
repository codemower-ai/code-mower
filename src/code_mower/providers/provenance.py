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
    "model_source",
    "version_source",
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
        "model_source": _safe_text(source.get("model_source")),
        "version_source": _safe_text(source.get("version_source")),
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
            "model_source": "not_applicable",
            "version_source": "package_version",
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


def _text_values(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        text = _safe_text(values)
        return (text,) if text else ()
    if isinstance(values, (list, tuple)):
        result: list[str] = []
        for value in values:
            text = _safe_text(value)
            if text and text not in result:
                result.append(text)
        return tuple(result)
    return ()


def _env_value(config: Mapping[str, Any], key: str) -> str:
    env_name = _safe_text(config.get(key))
    return _safe_text(os.environ.get(env_name)) if env_name else ""


def _env_value_any(config: Mapping[str, Any], key: str, any_key: str) -> str:
    env_names = []
    primary = _safe_text(config.get(key))
    if primary:
        env_names.append(primary)
    for env_name in _text_values(config.get(any_key)):
        if env_name not in env_names:
            env_names.append(env_name)
    for env_name in env_names:
        value = _safe_text(os.environ.get(env_name))
        if value:
            return value
    return ""


def model_env_names(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Return ordered model env vars for a provider config.

    The primary provider env var is listed first, followed by aliases from
    ``model_env_any``. Duplicate names are removed while preserving order.
    """

    env_names: list[str] = []
    primary = _safe_text(config.get("model_env"))
    if primary:
        env_names.append(primary)
    for env_name in _text_values(config.get("model_env_any")):
        if env_name not in env_names:
            env_names.append(env_name)
    return tuple(env_names)


def preferred_model_env_name(config: Mapping[str, Any]) -> str:
    """Choose the env var Code Mower should recommend setting.

    Prefer the Code Mower-specific alias when present so users can record
    benchmark model identity without changing the behavior of the provider CLI
    itself.
    """

    names = model_env_names(config)
    for name in names:
        if name.startswith("CODE_MOWER_"):
            return name
    return names[0] if names else ""


def build_provider_model_env_report(
    lanes: Mapping[str, Any],
    *,
    providers: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a privacy-safe model-provenance setup report for provider lanes."""

    requested = {provider.strip() for provider in providers if provider.strip()}
    rows: list[dict[str, Any]] = []
    matched: set[str] = set()
    for lane_id, lane in sorted(lanes.items()):
        provider = _safe_text(_lane_value(lane, "provider")) or lane_id
        if requested and lane_id not in requested and provider not in requested:
            continue
        if lane_id in requested:
            matched.add(lane_id)
        if provider in requested:
            matched.add(provider)
        config = _provider_config(lane)
        driver = _safe_text(_lane_value(lane, "driver")) or "unknown"
        tool, provenance_detail = build_provider_lane_tool_provenance(
            lane_id,
            lane,
            source="code-mower providers provenance-env",
        )
        model_source = _safe_text(provenance_detail.get("model_source"))
        env_names = model_env_names(config)
        preferred_env = preferred_model_env_name(config)
        env_status = [
            {"name": name, "is_set": bool(os.environ.get(name, "").strip())}
            for name in env_names
        ]
        if model_source == "missing" and preferred_env:
            action = "set_model_env"
            export_command = f'export {preferred_env}="TODO_MODEL_NAME"'
        elif model_source == "missing":
            action = "not_configurable"
            export_command = ""
        else:
            action = "none"
            export_command = ""
        rows.append(
            {
                "lane_id": lane_id,
                "provider": provider,
                "driver": driver,
                "model_known": bool(provenance_detail.get("model_known")),
                "model_source": model_source,
                "tool_name": tool.get("tool_name", ""),
                "tool_version": tool.get("tool_version", ""),
                "version_known": bool(provenance_detail.get("version_known")),
                "version_source": _safe_text(
                    provenance_detail.get("version_source")
                ),
                "command": _safe_text(provenance_detail.get("command")),
                "command_found": bool(provenance_detail.get("command_found")),
                "env_names": list(env_names),
                "preferred_env": preferred_env,
                "env_status": env_status,
                "action": action,
                "export_command": export_command,
            }
        )
    unknown = sorted(requested - matched)
    missing = [row for row in rows if row["action"] == "set_model_env"]
    missing_version_probe = [
        row
        for row in rows
        if row["driver"] == "local_cli" and row["version_source"] == "missing"
    ]
    return {
        "mode": "code-mower-provider-model-env-report",
        "providers": rows,
        "provider_count": len(rows),
        "missing_model_env_count": len(missing),
        "missing_version_probe_count": len(missing_version_probe),
        "unknown_providers": unknown,
        "status": "fail" if unknown else "pass",
    }


def _profile_model(config: Mapping[str, Any]) -> tuple[str, str]:
    profile_env = _safe_text(config.get("profile_env"))
    profile_id = _safe_text(os.environ.get(profile_env)) if profile_env else ""
    profiles = config.get("profiles")
    if not profile_id or not isinstance(profiles, Mapping):
        return "", ""
    profile = profiles.get(profile_id)
    if not isinstance(profile, Mapping):
        return "", profile_id
    return _safe_text(profile.get("model")), profile_id


_VENDOR_HIDDEN_MODEL_DRIVERS = frozenset({"hosted_bridge", "manual", "saas_event"})


def _configured_model(config: Mapping[str, Any], driver: str) -> tuple[str, str]:
    env_model = _env_value_any(config, "model_env", "model_env_any")
    if env_model:
        return env_model, "env"
    profile_model, profile_id = _profile_model(config)
    if profile_model:
        return profile_model, f"profile:{profile_id}"
    default_model = _safe_text(config.get("default_model"))
    if default_model:
        return default_model, "default"
    if driver in _VENDOR_HIDDEN_MODEL_DRIVERS:
        return "", "vendor_hidden"
    return "", "missing"


def _configured_command_candidates(
    config: Mapping[str, Any],
    provider: str,
) -> tuple[str, ...]:
    override = _env_value(config, "command_env")
    if override:
        return (override,)
    candidates: list[str] = []
    command = _safe_text(config.get("command"))
    if command:
        candidates.append(command)
    for alternate in _text_values(config.get("alternate_commands")):
        if alternate not in candidates:
            candidates.append(alternate)
    if not candidates:
        candidates.append(provider)
    return tuple(candidates)


def _select_available_command(candidates: tuple[str, ...]) -> tuple[str, str]:
    for command in candidates:
        resolved = shutil.which(command)
        if resolved:
            return command, resolved
    return (candidates[0] if candidates else "", "")


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
    command_candidates = _configured_command_candidates(config, provider)
    command, resolved_command = _select_available_command(command_candidates)
    model, model_source = _configured_model(config, driver)
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
    hidden_vendor_version = driver in _VENDOR_HIDDEN_MODEL_DRIVERS
    detail: dict[str, Any] = {
        "lane_id": lane_id,
        "driver": driver,
        "command": command if driver == "local_cli" else "",
        "command_candidates": list(command_candidates) if driver == "local_cli" else [],
        "model_known": bool(model),
        "model_source": model_source,
        "version_known": False,
        "version_source": "vendor_hidden"
        if hidden_vendor_version
        else ("not_applicable" if driver == "api_model" else "missing"),
    }
    tool_version = ""
    if driver == "local_cli" and include_version_probe and command:
        detail["command_found"] = bool(resolved_command)
        if resolved_command:
            detail["path_basename"] = os.path.basename(resolved_command)
            version_detail = detect_local_cli_version(resolved_command)
            tool_version = _safe_text(version_detail.get("tool_version"))
            detail["version_known"] = bool(tool_version)
            detail["version_source"] = "cli_version_probe" if tool_version else "missing"
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
            "model_source": model_source,
            "version_source": detail["version_source"],
            "integration": driver.replace("_", "-"),
            "lens": lens,
            "source": source,
        }
    )
    return tool, detail
