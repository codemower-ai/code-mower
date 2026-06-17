"""Redacted GitHub API helpers for doctor checks."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Mapping

from .privacy import auth_probe_output_detail


def _github_api_payload(
    gh_path: str,
    endpoint: str,
    *,
    http_timeout: int,
) -> tuple[Any | None, dict[str, Any]]:
    try:
        completed = subprocess.run(
            [gh_path, "api", endpoint],
            capture_output=True,
            text=True,
            check=False,
            timeout=http_timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, {"error_type": exc.__class__.__name__}
    output = (completed.stdout or completed.stderr or "").strip()
    detail: dict[str, Any] = {
        "endpoint": endpoint,
        "returncode": completed.returncode,
        **auth_probe_output_detail(output),
    }
    if completed.returncode != 0:
        return None, detail
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        detail["parse_error"] = "json"
        return None, detail
    return payload, detail


def _github_api_json(
    gh_path: str,
    endpoint: str,
    *,
    http_timeout: int,
) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    payload, detail = _github_api_payload(
        gh_path,
        endpoint,
        http_timeout=http_timeout,
    )
    if payload is None:
        return None, detail
    if not isinstance(payload, Mapping):
        detail["parse_error"] = "not_object"
        return None, detail
    return payload, detail


def _github_api_list(
    gh_path: str,
    endpoint: str,
    *,
    http_timeout: int,
) -> tuple[list[Any] | None, dict[str, Any]]:
    payload, detail = _github_api_payload(
        gh_path,
        endpoint,
        http_timeout=http_timeout,
    )
    if payload is None:
        return None, detail
    if not isinstance(payload, list):
        detail["parse_error"] = "not_list"
        return None, detail
    return payload, detail
