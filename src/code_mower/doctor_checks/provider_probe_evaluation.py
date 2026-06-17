"""Provider auth smoke-probe evaluation."""

from __future__ import annotations

from typing import Any, Mapping

from .common import STATUS_PASS, STATUS_WARN, as_sequence
from .provider_probe_auth import probe_auth_error_detail, probe_error_value_is_clean
from .provider_probe_json import json_field, parse_probe_json


def evaluate_json_probe(
    provider_config: Mapping[str, Any],
    output: str,
    *,
    returncode: int,
) -> tuple[str, str, dict[str, Any]]:
    payload, parse_detail = parse_probe_json(output)
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
        value = json_field(payload, field)
        if not probe_error_value_is_clean(value):
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
        probe_auth_error_detail(
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
        value = json_field(payload, field)
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


__all__ = ("evaluate_json_probe",)
