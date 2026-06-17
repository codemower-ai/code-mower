"""Provider API-model profile checks and probe orchestration."""

from __future__ import annotations

import json
import urllib.error
from typing import Any, Mapping

from .common import (
    DoctorCheck,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
)
from .provider_api_model_openai import fetch_openai_compatible_models
from .provider_api_model_profiles import (
    api_model_key,
    local_llm_lane_profiles,
    runtime_profile,
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
        profiles = local_llm_lane_profiles(lane)
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
        runtime = runtime_profile(lane, profile)
        try:
            models = fetch_openai_compatible_models(
                runtime.api_base,
                api_model_key(lane, profile),
                http_timeout,
            )
        except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
            checks.append(
                DoctorCheck(
                    name="runtime.api_model.probe",
                    status=STATUS_WARN,
                    lane=lane_id,
                    message=f"{runtime.profile_id} probe failed: {exc}",
                    detail={
                        "profile": runtime.profile_id,
                        "api_base": runtime.api_base,
                    },
                    remediation=(
                        "Start the local OpenAI-compatible endpoint, fix api_base "
                        "or api key overrides, then rerun doctor --probe-runtime."
                    ),
                )
            )
            continue
        expected = runtime.model
        status = STATUS_PASS if expected in models else STATUS_WARN
        message = (
            f"{runtime.profile_id} reports expected model {expected}"
            if status == STATUS_PASS
            else f"{runtime.profile_id} did not report expected model {expected}"
        )
        checks.append(
            DoctorCheck(
                name="runtime.api_model.probe",
                status=status,
                lane=lane_id,
                message=message,
                detail={
                    "profile": runtime.profile_id,
                    "api_base": runtime.api_base,
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
