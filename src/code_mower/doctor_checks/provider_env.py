"""Provider environment and token checks."""

from __future__ import annotations

from typing import Any, Mapping

from .common import (
    DoctorCheck,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    token_remediation,
)
from .provider_env_required import provider_required_env_status
from .provider_env_tokens import provider_token_status


def check_token_env(lane_id: str, lane: Mapping[str, Any]) -> list[DoctorCheck]:
    token_status = provider_token_status(lane)
    checks: list[DoctorCheck] = []
    if not token_status.declares_tokens:
        checks.append(
            DoctorCheck(
                name="env.tokens",
                status=STATUS_SKIP,
                lane=lane_id,
                message="lane declares no token env vars",
            )
        )
        return checks

    missing = list(token_status.missing)
    missing_any = [list(group) for group in token_status.missing_any]
    token_file_env = list(token_status.token_file_env)
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
                    "token_file_env": token_file_env,
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
                    "token_env": list(token_status.token_env),
                    "token_env_any": [list(group) for group in token_status.token_env_any],
                    "token_file_env": token_file_env,
                },
            )
        )
    return checks


def check_required_env(lane_id: str, lane: Mapping[str, Any]) -> list[DoctorCheck]:
    env_status = provider_required_env_status(lane)
    if not env_status.declares_required_env:
        return []
    required = list(env_status.required)
    required_truthy = list(env_status.required_truthy)
    missing = list(env_status.missing)
    missing_truthy = list(env_status.missing_truthy)
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
