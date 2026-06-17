"""Provider environment and token checks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .common import (
    SUPPORTED_TOKEN_FILE_ENV_NAMES,
    TRUTHY_ENV_VALUES,
    DoctorCheck,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    as_sequence,
    code_mower_secrets,
    token_remediation,
)


def check_token_env(lane_id: str, lane: Mapping[str, Any]) -> list[DoctorCheck]:
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


def check_required_env(lane_id: str, lane: Mapping[str, Any]) -> list[DoctorCheck]:
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

