"""Provider API-model profile expansion and runtime overrides."""

from __future__ import annotations

import os
from typing import Any, Mapping

from .common import local_llm_profiles


def profile_from_config(
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


def local_llm_lane_profiles(
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
                return (profile_from_config(selected_profile_id, raw_profile),)
        try:
            return (local_llm_profiles.get_profile(selected_profile_id),)
        except KeyError:
            return ()
    if isinstance(raw_profiles, Mapping):
        return tuple(
            profile_from_config(str(profile_id), profile)
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


def api_model_key(
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


def provider_env_override(
    provider_config: Mapping[str, Any],
    env_key: str,
    fallback: str,
) -> str:
    env_name = str(provider_config.get(env_key, ""))
    if not env_name:
        return fallback
    return os.environ.get(env_name) or fallback


def runtime_profile(
    lane: Mapping[str, Any],
    profile: local_llm_profiles.LocalLlmProfile,
) -> local_llm_profiles.LocalLlmProfile:
    provider_config = lane.get("provider_config", {})
    if not isinstance(provider_config, Mapping):
        return profile
    return local_llm_profiles.LocalLlmProfile(
        profile_id=profile.profile_id,
        description=profile.description,
        api_base=provider_env_override(provider_config, "api_base_env", profile.api_base),
        model=provider_env_override(provider_config, "model_env", profile.model),
        endpoint=profile.endpoint,
        api_key=profile.api_key,
        context_window=profile.context_window,
        max_files=profile.max_files,
        max_file_bytes=profile.max_file_bytes,
        http_timeout=profile.http_timeout,
        informational=profile.informational,
    )
