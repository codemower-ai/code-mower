"""Endpoint normalization and health checks for CodeMower.com uploads."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_HEALTH_PATH = "/api/health"
DEFAULT_DASHBOARD_PATH = "/dashboard"


def is_local_http_endpoint(endpoint: str) -> bool:
    parsed_endpoint = urllib.parse.urlparse(endpoint)
    return (
        parsed_endpoint.scheme == "http"
        and parsed_endpoint.hostname in {"localhost", "127.0.0.1"}
    )


def validate_upload_endpoint(endpoint: str) -> None:
    parsed_endpoint = urllib.parse.urlparse(endpoint)
    if parsed_endpoint.scheme != "https" and not is_local_http_endpoint(endpoint):
        raise ValueError("upload endpoint must be https:// or a local development endpoint")
    if not parsed_endpoint.netloc:
        raise ValueError(f"upload endpoint is missing a host: {endpoint!r}")


def origin_for_endpoint(endpoint: str) -> str:
    parsed_endpoint = urllib.parse.urlparse(endpoint)
    if not parsed_endpoint.scheme or not parsed_endpoint.netloc:
        return ""
    return urllib.parse.urlunparse(
        (parsed_endpoint.scheme, parsed_endpoint.netloc, "", "", "", "")
    )


def url_for_endpoint_path(endpoint: str, path: str) -> str:
    origin = origin_for_endpoint(endpoint)
    if not origin:
        return ""
    return urllib.parse.urljoin(origin + "/", path.lstrip("/"))


def dashboard_url_for_endpoint(endpoint: str) -> str:
    return url_for_endpoint_path(endpoint, DEFAULT_DASHBOARD_PATH)


def health_url_for_endpoint(endpoint: str) -> str:
    return url_for_endpoint_path(endpoint, DEFAULT_HEALTH_PATH)


def probe_cloud_service(endpoint: str, *, timeout: float) -> dict[str, Any]:
    health_url = health_url_for_endpoint(endpoint)
    if not health_url:
        return {
            "name": "service",
            "status": "fail",
            "message": "unable to derive health URL from upload endpoint",
        }
    request = urllib.request.Request(
        health_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "code-mower-cloud-doctor",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            status_code = response.getcode()
    except urllib.error.HTTPError as exc:
        return {
            "name": "service",
            "status": "fail",
            "message": f"health check failed with HTTP {exc.code}",
            "detail": {"health_url": health_url, "status_code": exc.code},
        }
    except urllib.error.URLError as exc:
        return {
            "name": "service",
            "status": "fail",
            "message": f"health check failed: {exc.reason}",
            "detail": {"health_url": health_url},
        }
    parsed: dict[str, Any] = {}
    if response_body.strip():
        try:
            maybe_parsed = json.loads(response_body)
            if isinstance(maybe_parsed, dict):
                parsed = maybe_parsed
        except json.JSONDecodeError:
            parsed = {}
    if 200 <= status_code < 300:
        detail: dict[str, Any] = {"health_url": health_url, "status_code": status_code}
        for key in ("app", "supabaseConfigured"):
            if key in parsed and isinstance(parsed[key], str | bool | int | float):
                detail[key] = parsed[key]
        return {
            "name": "service",
            "status": "pass",
            "message": f"health endpoint is reachable: {health_url}",
            "detail": detail,
        }
    return {
        "name": "service",
        "status": "fail",
        "message": f"health check returned HTTP {status_code}",
        "detail": {"health_url": health_url, "status_code": status_code},
    }
