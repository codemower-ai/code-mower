"""Human-owned GitHub token posture checks."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Mapping, Sequence

from .common import (
    OBSERVER_ADOPTION_POSTURES,
    DoctorCheck,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
)
from .github_api import _github_api_json

DEFAULT_HUMAN_TOKEN_SECRET = "DISPATCH_TOKEN"
DEFAULT_HUMAN_TOKEN_EXPIRES_VAR = "DISPATCH_TOKEN_EXPIRES_AT"
EXPIRY_WARNING_DAYS = 14
NON_EXPIRING_TOKEN_VALUE = "never"
EXPIRY_PLACEHOLDER_VALUES = {"YYYY-MM-DD", "<YYYY-MM-DD>"}


def _owner_surface_value(
    config: Mapping[str, Any],
    key: str,
    default: str,
) -> str:
    surface = config.get("owner_surface")
    if not isinstance(surface, Mapping):
        return default
    value = surface.get(key, default)
    text = str(value).strip() if value is not None else ""
    return text or default


def human_automation_token_config(config: Mapping[str, Any]) -> dict[str, str]:
    return {
        "secret": _owner_surface_value(
            config,
            "dispatch_token_env",
            DEFAULT_HUMAN_TOKEN_SECRET,
        ),
        "expires_var": _owner_surface_value(
            config,
            "dispatch_token_expires_var",
            DEFAULT_HUMAN_TOKEN_EXPIRES_VAR,
        ),
    }


def _lane_token_names(lane: Mapping[str, Any]) -> tuple[str, ...]:
    token_env = lane.get("token_env", [])
    if isinstance(token_env, str):
        return (token_env,)
    if isinstance(token_env, Sequence) and not isinstance(token_env, (bytes, bytearray)):
        return tuple(str(name) for name in token_env if str(name).strip())
    return ()


def human_automation_token_required(
    config: Mapping[str, Any],
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
) -> bool:
    token = human_automation_token_config(config)["secret"]
    if any(token in _lane_token_names(lane) for _lane_id, lane in lanes):
        return True
    identity = config.get("builder_identity")
    has_builder_automation = (
        bool(identity.get("branch_prefixes") or identity.get("fix_round_mentions"))
        if isinstance(identity, Mapping)
        else False
    )
    if has_builder_automation:
        return True
    return any(
        lane.get("type") == "audit"
        and lane.get("driver") in {"local_cli", "hosted_bridge", "saas_event"}
        and lane.get("trigger_policy") in {"label", "comment"}
        for _lane_id, lane in lanes
    )


def _parse_expiry(value: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _is_non_expiring(value: str) -> bool:
    return value.strip().casefold() == NON_EXPIRING_TOKEN_VALUE


def _is_expiry_placeholder(value: str) -> bool:
    return value.strip().upper() in EXPIRY_PLACEHOLDER_VALUES


def _blocking_status_for_posture(adoption_posture: str) -> str:
    return STATUS_WARN if adoption_posture in OBSERVER_ADOPTION_POSTURES else STATUS_FAIL


def _token_readiness_context(adoption_posture: str) -> str:
    if adoption_posture == "hosted-builders":
        return "hosted-builder observer posture"
    if adoption_posture == "orchestrator-only":
        return "orchestrator-only posture"
    return "reviewer-gate posture"


def check_human_automation_token(
    *,
    gh_path: str,
    slug: str,
    config: Mapping[str, Any],
    lanes: Sequence[tuple[str, Mapping[str, Any]]],
    http_timeout: int,
    adoption: bool = False,
    adoption_posture: str = "reviewer-gate",
    now: datetime | None = None,
) -> DoctorCheck:
    token = human_automation_token_config(config)
    secret_name = token["secret"]
    expires_var = token["expires_var"]
    detail = {
        "repo": slug,
        "secret": secret_name,
        "expires_var": expires_var,
        "required": human_automation_token_required(config, lanes),
        "adoption_posture": adoption_posture,
    }
    if not detail["required"]:
        return DoctorCheck(
            name="github.human_automation_token",
            status=STATUS_SKIP,
            message=f"{slug} does not require a shared human automation token",
            detail=detail,
        )

    secret_payload, secret_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/actions/secrets/{secret_name}",
        http_timeout=http_timeout,
    )
    if secret_payload is None:
        status = STATUS_WARN if adoption else _blocking_status_for_posture(adoption_posture)
        owner_action = status == STATUS_WARN
        return DoctorCheck(
            name="github.human_automation_token",
            status=status,
            message=(
                f"{slug} is missing the {secret_name} human automation token secret"
                + (
                    f" for {_token_readiness_context(adoption_posture)}"
                    if status == STATUS_WARN
                    else ""
                )
            ),
            detail={
                **detail,
                "owner_action": owner_action,
                "owner_action_kind": "human_automation_token",
                "secret_check": secret_detail,
            },
            remediation=(
                f"Create one human-owned fine-grained PAT secret with "
                f"`gh secret set {secret_name}`. Grant repository Contents read, "
                "Issues read/write, and Pull requests read/write before relying "
                "on unattended dispatch, labels, or fix-round mentions."
            ),
        )

    variable_payload, variable_detail = _github_api_json(
        gh_path,
        f"repos/{slug}/actions/variables/{expires_var}",
        http_timeout=http_timeout,
    )
    if variable_payload is None:
        return DoctorCheck(
            name="github.human_automation_token",
            status=STATUS_WARN,
            message=f"{slug} is missing the {expires_var} human token expiry metadata",
            detail={
                **detail,
                "owner_action": True,
                "owner_action_kind": "human_automation_token_expiry",
                "created_at": str(secret_payload.get("created_at") or ""),
                "updated_at": str(secret_payload.get("updated_at") or ""),
                "expiry_check": variable_detail,
            },
            remediation=(
                f"Record the PAT expiry date with "
                f"`gh variable set {expires_var} --body YYYY-MM-DD`, or use "
                f"`gh variable set {expires_var} --body never` for a "
                "non-expiring PAT, then rerun `code-mower doctor --github`."
            ),
        )

    expiry_text = str(variable_payload.get("value") or "").strip()
    if _is_non_expiring(expiry_text):
        return DoctorCheck(
            name="github.human_automation_token",
            status=STATUS_PASS,
            message=f"{slug} {secret_name} is recorded as non-expiring",
            detail={
                **detail,
                "created_at": str(secret_payload.get("created_at") or ""),
                "updated_at": str(secret_payload.get("updated_at") or ""),
                "expires_at": NON_EXPIRING_TOKEN_VALUE,
                "non_expiring": True,
            },
        )
    if _is_expiry_placeholder(expiry_text):
        return DoctorCheck(
            name="github.human_automation_token",
            status=STATUS_WARN,
            message=f"{slug} still has placeholder {expires_var} value",
            detail={
                **detail,
                "owner_action": True,
                "owner_action_kind": "human_automation_token_expiry",
                "expires_at": expiry_text,
            },
            remediation=(
                f"Set {expires_var} to the PAT expiry date in YYYY-MM-DD "
                "format, or to `never` for a non-expiring PAT."
            ),
        )
    expiry = _parse_expiry(expiry_text)
    if expiry is None:
        return DoctorCheck(
            name="github.human_automation_token",
            status=STATUS_WARN,
            message=f"{slug} has an invalid {expires_var} value",
            detail={
                **detail,
                "owner_action": True,
                "owner_action_kind": "human_automation_token_expiry",
                "expires_at": expiry_text,
            },
            remediation=(
                f"Set {expires_var} to the PAT expiry date in YYYY-MM-DD "
                "format, or to `never` for a non-expiring PAT."
            ),
        )

    today = (now or datetime.now(UTC)).date()
    days_remaining = (expiry - today).days
    status = STATUS_PASS
    if days_remaining < 0:
        status = STATUS_WARN
    elif days_remaining <= EXPIRY_WARNING_DAYS:
        status = STATUS_WARN

    return DoctorCheck(
        name="github.human_automation_token",
        status=status,
        message=(
            f"{slug} {secret_name} is expired ({-days_remaining} day(s) ago)"
            if days_remaining < 0
            else f"{slug} {secret_name} expires in {days_remaining} day(s)"
        ),
        detail={
            **detail,
            "owner_action": status != STATUS_PASS,
            "owner_action_kind": "human_automation_token_expiry",
            "created_at": str(secret_payload.get("created_at") or ""),
            "updated_at": str(secret_payload.get("updated_at") or ""),
            "expires_at": expiry.isoformat(),
            "days_remaining": days_remaining,
        },
        remediation=(
            f"Rotate {secret_name} and update {expires_var} before relying on "
            "unattended labels, comments, or fix-round mentions."
            if status != STATUS_PASS
            else None
        ),
    )


__all__ = (
    "check_human_automation_token",
    "human_automation_token_config",
    "human_automation_token_required",
)
