"""Provider auth smoke-probe parsing and remediation helpers."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .common import STATUS_PASS, STATUS_WARN, as_sequence


def parse_probe_json(output: str) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
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


def json_field(payload: Mapping[str, Any], dotted_field: str) -> Any:
    value: Any = payload
    for part in dotted_field.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


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
