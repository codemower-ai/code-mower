"""Provider probe auth-error classification helpers."""

from __future__ import annotations

from typing import Any, Mapping

from .provider_probe_json import json_field


def probe_error_value_is_clean(value: Any) -> bool:
    return value is None or value is False or value == "" or value == 0


def probe_auth_error_detail(
    payload: Mapping[str, Any],
    error_fields: tuple[str, ...],
    auth_status_fields: tuple[str, ...],
    output: str,
) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    for field in auth_status_fields:
        status_value = json_field(payload, field)
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
        not probe_error_value_is_clean(json_field(payload, field))
        for field in error_fields
    )
    if has_dirty_error and any(marker in lowered_output for marker in auth_markers):
        detail["auth_error_detected"] = True
    return detail


__all__ = ("probe_auth_error_detail", "probe_error_value_is_clean")
