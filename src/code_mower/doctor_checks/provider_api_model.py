"""Provider API-model profile checks and OpenAI-compatible probes."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping

from .common import (
    DoctorCheck,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    local_llm_profiles,
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


def check_api_model(
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
