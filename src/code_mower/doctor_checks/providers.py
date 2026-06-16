"""Provider catalog coverage, env checks, and runtime probes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from .common import (
    SUPPORTED_TOKEN_FILE_ENV_NAMES,
    TRUTHY_ENV_VALUES,
    DoctorCheck,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    as_mapping,
    as_sequence,
    code_mower_config,
    code_mower_secrets,
    local_cli_remediation,
    local_llm_profiles,
    token_remediation,
)
from .runtime import auth_probe_output_detail


def selected_lanes(
    config: Mapping[str, Any],
    profile: str | None,
) -> tuple[str, ...]:
    lanes = as_mapping(config.get("lanes"), "lanes")
    if not profile:
        return tuple(str(lane_id) for lane_id in sorted(lanes))
    profiles = as_mapping(config.get("profiles", {}), "profiles")
    profile_config = profiles.get(profile)
    if not isinstance(profile_config, Mapping):
        known = ", ".join(sorted(str(item) for item in profiles))
        raise code_mower_config.ConfigError(
            f"unknown profile {profile!r}; known profiles: {known}"
        )
    return tuple(str(lane_id) for lane_id in as_sequence(profile_config.get("lanes", [])))


def provider_template_coverage(
    lanes: tuple[str, ...],
    templates: Mapping[str, Any],
) -> DoctorCheck:
    provider_templates = as_mapping(
        templates.get("provider_templates"),
        "provider_templates",
    )
    missing = sorted(set(lanes) - set(str(item) for item in provider_templates))
    if missing:
        return DoctorCheck(
            name="provider_templates.coverage",
            status=STATUS_FAIL,
            message=f"provider templates missing selected lanes: {', '.join(missing)}",
            detail={"missing_lanes": missing},
            remediation=(
                "Add these lane ids to the provider catalog or remove them from "
                "the selected profile before running audits."
            ),
        )
    return DoctorCheck(
        name="provider_templates.coverage",
        status=STATUS_PASS,
        message="provider templates cover selected lanes",
    )


def _merge_mapping_defaults(
    defaults: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(defaults)
    merged.update(overrides)
    return merged


def effective_lane(
    lane_id: str,
    lane: Mapping[str, Any],
    provider_templates: Mapping[str, Any],
) -> Mapping[str, Any]:
    template = provider_templates.get(lane_id, {})
    if not isinstance(template, Mapping):
        template = {}
    merged = _merge_mapping_defaults(template, lane)
    for key in ("labels", "provider_config", "review_hygiene"):
        template_value = template.get(key, {})
        lane_value = lane.get(key, {})
        if isinstance(template_value, Mapping) and isinstance(lane_value, Mapping):
            merged[key] = _merge_mapping_defaults(template_value, lane_value)
    return merged


def _check_token_env(lane_id: str, lane: Mapping[str, Any]) -> list[DoctorCheck]:
    token_env = list(as_sequence(lane.get("token_env", [])))
    token_env_any = [
        [str(item) for item in as_sequence(group)]
        for group in as_sequence(lane.get("token_env_any", []))
    ]
    review_hygiene = lane.get("review_hygiene", {})
    if not token_env and isinstance(review_hygiene, Mapping) and review_hygiene.get("token_env"):
        token_env = [review_hygiene["token_env"]]
    checks: list[DoctorCheck] = []
    if not token_env and not token_env_any:
        checks.append(
            DoctorCheck(
                name="env.tokens",
                status=STATUS_SKIP,
                lane=lane_id,
                message="lane declares no token env vars",
            )
        )
        return checks

    def token_file_value(name: str, path_text: str) -> str:
        if name not in SUPPORTED_TOKEN_FILE_ENV_NAMES:
            return ""
        try:
            result = code_mower_secrets.read_secret_file(
                Path(path_text),
                supported_env_names=SUPPORTED_TOKEN_FILE_ENV_NAMES,
            )
        except OSError:
            return ""
        return result.value

    def token_is_present(name: str) -> bool:
        if os.environ.get(name):
            return True
        path_text = os.environ.get(f"{name}_FILE", "").strip()
        if not path_text:
            return False
        return bool(token_file_value(name, path_text))

    token_file_env = [
        f"{name}_FILE"
        for name in [str(item) for item in token_env]
        if name in SUPPORTED_TOKEN_FILE_ENV_NAMES and os.environ.get(f"{name}_FILE")
    ]
    token_file_env.extend(
        f"{name}_FILE"
        for group in token_env_any
        for name in group
        if name in SUPPORTED_TOKEN_FILE_ENV_NAMES and os.environ.get(f"{name}_FILE")
    )
    missing = [str(name) for name in token_env if not token_is_present(str(name))]
    missing_any = [
        group
        for group in token_env_any
        if group and not any(token_is_present(name) for name in group)
    ]
    if missing or missing_any:
        messages = []
        if missing:
            messages.append(f"missing token env vars: {', '.join(missing)}")
        for group in missing_any:
            messages.append(f"set one of: {', '.join(group)}")
        checks.append(
            DoctorCheck(
                name="env.tokens",
                status=STATUS_WARN,
                lane=lane_id,
                message="; ".join(messages),
                detail={
                    "missing": missing,
                    "missing_any": missing_any,
                    "token_file_env": sorted(set(token_file_env)),
                },
                remediation=token_remediation(missing, missing_any),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="env.tokens",
                status=STATUS_PASS,
                lane=lane_id,
                message="token env vars are set",
                detail={
                    "token_env": [str(name) for name in token_env],
                    "token_env_any": token_env_any,
                    "token_file_env": sorted(set(token_file_env)),
                },
            )
        )
    return checks


def _check_required_env(lane_id: str, lane: Mapping[str, Any]) -> list[DoctorCheck]:
    provider_config = lane.get("provider_config", {})
    if not isinstance(provider_config, Mapping):
        return []
    required = [
        str(name)
        for name in as_sequence(provider_config.get("required_env", []))
        if str(name).strip()
    ]
    required_truthy = [
        str(name)
        for name in as_sequence(provider_config.get("required_env_truthy", []))
        if str(name).strip()
    ]
    if not required and not required_truthy:
        return []
    missing = [name for name in required if not os.environ.get(name)]
    missing_truthy = [
        name
        for name in required_truthy
        if os.environ.get(name, "").strip().lower() not in TRUTHY_ENV_VALUES
    ]
    if missing or missing_truthy:
        return [
            DoctorCheck(
                name="env.required",
                status=STATUS_WARN,
                lane=lane_id,
                message=(
                    "missing required env vars: "
                    + ", ".join([*missing, *missing_truthy])
                ),
                detail={
                    "missing": missing,
                    "missing_truthy": missing_truthy,
                    "required_env": required,
                    "required_env_truthy": required_truthy,
                },
                remediation=(
                    "Set the required env vars only when you accept the lane's "
                    "documented runtime trust model."
                ),
            )
        ]
    return [
        DoctorCheck(
            name="env.required",
            status=STATUS_PASS,
            lane=lane_id,
            message="required env vars are set",
            detail={
                "required_env": required,
                "required_env_truthy": required_truthy,
            },
        )
    ]


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


def _check_local_cli(lane_id: str, lane: Mapping[str, Any]) -> DoctorCheck:
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


def _parse_probe_json(output: str) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    text = output.strip()
    if not text:
        return None, {"json_parsed": False, "json_error": "empty_output"}

    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])

    last_error = "json"
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = str(exc)
            continue
        if isinstance(payload, Mapping):
            return payload, {
                "json_parsed": True,
                "json_extracted": candidate != text,
            }
        return None, {"json_parsed": False, "json_error": "not_object"}
    return None, {"json_parsed": False, "json_error": last_error}


def _json_field(payload: Mapping[str, Any], dotted_field: str) -> Any:
    value: Any = payload
    for part in dotted_field.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _probe_error_value_is_clean(value: Any) -> bool:
    return value is None or value is False or value == "" or value == 0


def _probe_auth_error_detail(
    payload: Mapping[str, Any],
    error_fields: tuple[str, ...],
    auth_status_fields: tuple[str, ...],
    output: str,
) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    for field in auth_status_fields:
        status_value = _json_field(payload, field)
        if status_value is None:
            continue
        status_text = str(status_value).strip()
        if status_text in {"401", "403"}:
            detail["auth_status_code"] = status_text
            detail["auth_status_field"] = field
            detail["auth_error_detected"] = True
            break

    lowered_output = output.lower()
    auth_markers = (
        "invalid authentication",
        "authentication credentials",
        "unauthorized",
        "forbidden",
        "not authenticated",
        "auth token",
        "api key",
        "oauth",
    )
    has_dirty_error = any(
        not _probe_error_value_is_clean(_json_field(payload, field))
        for field in error_fields
    )
    if has_dirty_error and any(marker in lowered_output for marker in auth_markers):
        detail["auth_error_detected"] = True
    return detail


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


def evaluate_json_probe(
    provider_config: Mapping[str, Any],
    output: str,
    *,
    returncode: int,
) -> tuple[str, str, dict[str, Any]]:
    payload, parse_detail = _parse_probe_json(output)
    detail: dict[str, Any] = dict(parse_detail)
    if payload is None:
        if returncode != 0:
            return (
                STATUS_WARN,
                f"probe exited {returncode} without parseable JSON",
                detail,
            )
        return (
            STATUS_WARN,
            "probe output was not parseable JSON",
            detail,
        )

    error_fields = tuple(
        str(field)
        for field in as_sequence(provider_config.get("doctor_probe_error_fields", []))
        if str(field).strip()
    )
    dirty_errors = []
    for field in error_fields:
        value = _json_field(payload, field)
        if not _probe_error_value_is_clean(value):
            dirty_errors.append(field)
    detail["error_fields"] = list(error_fields)
    detail["dirty_error_fields"] = dirty_errors
    auth_status_fields = tuple(
        str(field)
        for field in as_sequence(
            provider_config.get(
                "doctor_probe_auth_status_fields",
                ("api_error_status",),
            )
        )
        if str(field).strip()
    )
    detail["auth_status_fields"] = list(auth_status_fields)
    detail.update(
        _probe_auth_error_detail(
            payload,
            error_fields,
            auth_status_fields,
            output,
        )
    )

    field = str(provider_config.get("doctor_probe_expect_json_field", "")).strip()
    expected = provider_config.get("doctor_probe_expect_json_value")
    response_matches = True
    if field:
        value = _json_field(payload, field)
        response_matches = expected is None or str(value).strip() == str(expected).strip()
        detail.update(
            {
                "response_field": field,
                "response_present": value is not None,
                "response_length": len(str(value)) if value is not None else 0,
                "response_matches_expected": response_matches,
            }
        )

    if returncode != 0:
        if detail.get("auth_error_detected"):
            return (
                STATUS_WARN,
                "probe reported provider authentication failure",
                detail,
            )
        return STATUS_WARN, f"probe exited {returncode}", detail
    if dirty_errors:
        if detail.get("auth_error_detected"):
            return (
                STATUS_WARN,
                "probe reported provider authentication failure",
                detail,
            )
        return (
            STATUS_WARN,
            "probe reported provider/API error fields",
            detail,
        )
    if not response_matches:
        return (
            STATUS_WARN,
            "probe response did not match expected sentinel",
            detail,
        )
    return STATUS_PASS, "auth smoke probe succeeded", detail


def local_cli_probe_remediation(
    command: str,
    probe_args: tuple[str, ...],
    lane: Mapping[str, Any],
    *,
    auth_error_detected: bool = False,
) -> str:
    provider = str(lane.get("provider") or "").strip()
    if auth_error_detected and provider == "claude":
        return (
            "Run `claude auth status`, then run "
            "`claude -p \"Reply with exactly: ok\" --output-format json`. "
            "If status says logged in but the prompt returns 401, refresh Claude "
            "Code OAuth by removing stale local Claude credentials and running "
            "`claude` to log in again. For automation, prefer a provider/API-key "
            "credential path over interactive Claude.ai OAuth."
        )
    if auth_error_detected:
        return (
            f"Run `{command} {' '.join(probe_args)}` manually, refresh provider "
            "auth credentials or API keys, then rerun doctor --probe-runtime."
        )
    return (
        f"Run `{command} {' '.join(probe_args)}` manually, fix CLI "
        "installation or auth, then rerun doctor --probe-runtime."
    )


def _check_local_cli_probe(
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


def _profile_from_config(
    profile_id: str, raw: Mapping[str, Any]
) -> local_llm_profiles.LocalLlmProfile:
    canonical = local_llm_profiles.LOCAL_LLM_PROFILES.get(profile_id)

    def profile_int(field: str, default: int) -> int:
        value = raw.get(field, default)
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"API model profile {profile_id!r} field {field!r} must be an integer"
            ) from exc

    return local_llm_profiles.LocalLlmProfile(
        profile_id=profile_id,
        description=str(
            raw.get(
                "description",
                canonical.description if canonical else f"{profile_id} local LLM profile",
            )
        ),
        api_base=str(raw.get("api_base", canonical.api_base if canonical else "")),
        model=str(raw.get("model", canonical.model if canonical else "")),
        endpoint=str(raw.get("endpoint", canonical.endpoint if canonical else "")),
        api_key=str(raw.get("api_key", canonical.api_key if canonical else "EMPTY")),
        context_window=profile_int(
            "context_window", canonical.context_window if canonical else 128_000
        ),
        max_files=profile_int("max_files", canonical.max_files if canonical else 25),
        max_file_bytes=profile_int(
            "max_file_bytes", canonical.max_file_bytes if canonical else 60_000
        ),
        http_timeout=profile_int("http_timeout", canonical.http_timeout if canonical else 900),
        informational=bool(
            raw.get("informational", canonical.informational if canonical else True)
        ),
    )


def _local_llm_lane_profiles(
    lane: Mapping[str, Any],
) -> tuple[local_llm_profiles.LocalLlmProfile, ...]:
    provider_config = lane.get("provider_config", {})
    if not isinstance(provider_config, Mapping):
        return ()
    raw_profiles = provider_config.get("profiles", {})
    profile_env = str(provider_config.get("profile_env", ""))
    selected_profile_id = os.environ.get(profile_env) if profile_env else None
    if selected_profile_id:
        if isinstance(raw_profiles, Mapping):
            raw_profile = raw_profiles.get(selected_profile_id)
            if isinstance(raw_profile, Mapping):
                return (_profile_from_config(selected_profile_id, raw_profile),)
        try:
            return (local_llm_profiles.get_profile(selected_profile_id),)
        except KeyError:
            return ()
    if isinstance(raw_profiles, Mapping):
        return tuple(
            _profile_from_config(str(profile_id), profile)
            for profile_id, profile in sorted(raw_profiles.items())
            if isinstance(profile, Mapping)
        )
    if isinstance(raw_profiles, (list, tuple)):
        profiles: list[local_llm_profiles.LocalLlmProfile] = []
        for profile_id in raw_profiles:
            try:
                profiles.append(local_llm_profiles.get_profile(str(profile_id)))
            except KeyError:
                continue
        return tuple(profiles)
    return ()


def fetch_openai_compatible_models(
    api_base: str,
    api_key: str,
    timeout: int,
) -> list[str]:
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    data = payload.get("data", []) if isinstance(payload, Mapping) else []
    return [
        str(entry.get("id"))
        for entry in data
        if isinstance(entry, Mapping) and entry.get("id")
    ]


def _api_model_key(
    lane: Mapping[str, Any], profile: local_llm_profiles.LocalLlmProfile
) -> str:
    provider_config = lane.get("provider_config", {})
    api_key_env = ""
    if isinstance(provider_config, Mapping):
        api_key_env = str(provider_config.get("api_key_env", ""))
    token_env = lane.get("token_env", [])
    if not api_key_env and isinstance(token_env, list):
        for item in token_env:
            name = str(item)
            if name.endswith("_API_KEY"):
                api_key_env = name
                break
    if api_key_env:
        value = os.environ.get(api_key_env)
        if value:
            return value
    return profile.api_key


def _provider_env_override(
    provider_config: Mapping[str, Any],
    env_key: str,
    fallback: str,
) -> str:
    env_name = str(provider_config.get(env_key, ""))
    if not env_name:
        return fallback
    return os.environ.get(env_name) or fallback


def _runtime_profile(
    lane: Mapping[str, Any],
    profile: local_llm_profiles.LocalLlmProfile,
) -> local_llm_profiles.LocalLlmProfile:
    provider_config = lane.get("provider_config", {})
    if not isinstance(provider_config, Mapping):
        return profile
    return local_llm_profiles.LocalLlmProfile(
        profile_id=profile.profile_id,
        description=profile.description,
        api_base=_provider_env_override(provider_config, "api_base_env", profile.api_base),
        model=_provider_env_override(provider_config, "model_env", profile.model),
        endpoint=profile.endpoint,
        api_key=profile.api_key,
        context_window=profile.context_window,
        max_files=profile.max_files,
        max_file_bytes=profile.max_file_bytes,
        http_timeout=profile.http_timeout,
        informational=profile.informational,
    )


def _check_api_model(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    try:
        profiles = _local_llm_lane_profiles(lane)
    except (TypeError, ValueError) as exc:
        checks.append(
            DoctorCheck(
                name="runtime.api_model.profiles",
                status=STATUS_FAIL,
                lane=lane_id,
                message=str(exc),
                remediation=(
                    "Fix provider_config.profiles in code-mower.yml or use a "
                    "known packaged local LLM profile id."
                ),
            )
        )
        return checks
    if not profiles:
        checks.append(
            DoctorCheck(
                name="runtime.api_model.profiles",
                status=STATUS_WARN,
                lane=lane_id,
                message="no API model profiles configured",
                remediation=(
                    "Add provider_config.profiles to this lane or select a "
                    "packaged local LLM profile."
                ),
            )
        )
        return checks

    checks.append(
        DoctorCheck(
            name="runtime.api_model.profiles",
            status=STATUS_PASS,
            lane=lane_id,
            message=f"{len(profiles)} API model profile(s) configured",
            detail={"profiles": [profile.profile_id for profile in profiles]},
        )
    )
    if not probe_runtime:
        checks.append(
            DoctorCheck(
                name="runtime.api_model.probe",
                status=STATUS_SKIP,
                lane=lane_id,
                message="runtime probing skipped; pass --probe-runtime to query model endpoints",
            )
        )
        return checks

    for profile in profiles:
        runtime_profile = _runtime_profile(lane, profile)
        try:
            models = fetch_openai_compatible_models(
                runtime_profile.api_base,
                _api_model_key(lane, profile),
                http_timeout,
            )
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            checks.append(
                DoctorCheck(
                    name="runtime.api_model.probe",
                    status=STATUS_WARN,
                    lane=lane_id,
                    message=f"{runtime_profile.profile_id} probe failed: {exc}",
                    detail={
                        "profile": runtime_profile.profile_id,
                        "api_base": runtime_profile.api_base,
                    },
                    remediation=(
                        "Start the local OpenAI-compatible endpoint, fix api_base "
                        "or api key overrides, then rerun doctor --probe-runtime."
                    ),
                )
            )
            continue
        expected = runtime_profile.model
        status = STATUS_PASS if expected in models else STATUS_WARN
        message = (
            f"{runtime_profile.profile_id} reports expected model {expected}"
            if status == STATUS_PASS
            else f"{runtime_profile.profile_id} did not report expected model {expected}"
        )
        checks.append(
            DoctorCheck(
                name="runtime.api_model.probe",
                status=status,
                lane=lane_id,
                message=message,
                detail={
                    "profile": runtime_profile.profile_id,
                    "api_base": runtime_profile.api_base,
                    "expected_model": expected,
                    "models": models,
                },
                remediation=(
                    None
                    if status == STATUS_PASS
                    else (
                        "Update the configured local LLM model or start an endpoint "
                        "that serves the expected model."
                    )
                ),
            )
        )
    return checks


def check_lane_runtime(
    lane_id: str,
    lane: Mapping[str, Any],
    *,
    probe_runtime: bool,
    http_timeout: int,
) -> list[DoctorCheck]:
    checks = _check_token_env(lane_id, lane)
    checks.extend(_check_required_env(lane_id, lane))
    driver = str(lane.get("driver", ""))
    if driver == "local_cli":
        checks.append(_check_local_cli(lane_id, lane))
        checks.append(
            _check_local_cli_probe(
                lane_id,
                lane,
                probe_runtime=probe_runtime,
                http_timeout=http_timeout,
            )
        )
    elif driver == "api_model":
        checks.extend(
            _check_api_model(
                lane_id,
                lane,
                probe_runtime=probe_runtime,
                http_timeout=http_timeout,
            )
        )
    elif driver in {"manual", "hosted_bridge", "saas_event"}:
        checks.append(
            DoctorCheck(
                name="runtime.probe",
                status=STATUS_SKIP,
                lane=lane_id,
                message=f"{driver} lanes do not have a local runtime probe yet",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="runtime.probe",
                status=STATUS_WARN,
                lane=lane_id,
                message=f"unknown driver {driver!r}; no runtime probe available",
                remediation=(
                    "Use a supported driver: local_cli, api_model, hosted_bridge, "
                    "saas_event, or manual."
                ),
            )
        )
    return checks
