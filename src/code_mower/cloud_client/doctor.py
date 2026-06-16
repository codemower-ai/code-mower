"""Cloud upload readiness diagnostics."""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any

from .bundle import BUNDLE_MANIFEST_FILENAME
from .endpoints import (
    dashboard_url_for_endpoint,
    health_url_for_endpoint,
    is_local_http_endpoint,
    probe_cloud_service,
    validate_upload_endpoint,
)
from .errors import CloudBundleError
from .upload import build_upload_payload


def _validate_upload_endpoint(endpoint: str) -> None:
    try:
        validate_upload_endpoint(endpoint)
    except ValueError as exc:
        raise CloudBundleError(str(exc)) from exc


def run_cloud_doctor(
    *,
    bundle_dir: Path,
    endpoint: str,
    token_env: str,
    require_token: bool = True,
    probe_service: bool = False,
    timeout: float = 5.0,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    endpoint_is_valid = False

    try:
        _validate_upload_endpoint(endpoint)
        endpoint_is_valid = True
        checks.append(
            {
                "name": "endpoint",
                "status": "pass",
                "message": f"upload endpoint is allowed: {endpoint}",
            }
        )
    except CloudBundleError as exc:
        checks.append(
            {
                "name": "endpoint",
                "status": "fail",
                "message": str(exc),
            }
        )

    if probe_service and endpoint_is_valid:
        checks.append(probe_cloud_service(endpoint, timeout=timeout))
    elif endpoint_is_valid:
        checks.append(
            {
                "name": "service",
                "status": "skip",
                "message": "service health probe skipped; pass --probe-service to check it",
                "detail": {"health_url": health_url_for_endpoint(endpoint)},
            }
        )

    token_present = bool(os.environ.get(token_env, ""))
    if token_present:
        checks.append(
            {
                "name": "token",
                "status": "pass",
                "message": f"{token_env} is set",
            }
        )
    elif is_local_http_endpoint(endpoint):
        checks.append(
            {
                "name": "token",
                "status": "warn",
                "message": (
                    f"{token_env} is not set; local configless ingest may still work"
                ),
            }
        )
    elif not require_token:
        checks.append(
            {
                "name": "token",
                "status": "warn",
                "message": f"{token_env} is not set; upload will remain disabled",
                "remediation": (
                    "Run `code-mower cloud setup --token-stdin` or export "
                    f"{token_env} before using --yes."
                ),
            }
        )
    else:
        checks.append(
            {
                "name": "token",
                "status": "fail",
                "message": f"{token_env} is not set",
                "remediation": (
                    "Set CODE_MOWER_CLOUD_TOKEN to a team ingest token before "
                    "running cloud upload --yes."
                ),
            }
        )

    manifest_path = bundle_dir / BUNDLE_MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            payload = build_upload_payload(bundle_dir=bundle_dir, include_reports=False)
            checks.append(
                {
                    "name": "bundle",
                    "status": "pass",
                    "message": (
                        f"bundle is readable with {len(payload['reports'])} report summaries "
                        f"and {len(payload['events'])} structured events"
                    ),
                }
            )
        except CloudBundleError as exc:
            checks.append(
                {
                    "name": "bundle",
                    "status": "fail",
                    "message": str(exc),
                }
            )
    else:
        checks.append(
            {
                "name": "bundle",
                "status": "warn",
                "message": (
                    f"bundle manifest not found at {manifest_path}; run cloud export first"
                ),
            }
        )

    failures = sum(1 for check in checks if check["status"] == "fail")
    warnings = sum(1 for check in checks if check["status"] == "warn")
    return {
        "mode": "cloud-doctor",
        "status": "fail" if failures else "pass",
        "endpoint": endpoint,
        "dashboard_url": dashboard_url_for_endpoint(endpoint),
        "health_url": health_url_for_endpoint(endpoint),
        "token_env": token_env,
        "bundle_dir": str(bundle_dir),
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
        "next_steps": [
            {
                "id": "open-dashboard",
                "label": "Open the Code Mower Cloud dashboard",
                "url": dashboard_url_for_endpoint(endpoint),
            },
            {
                "id": "setup-token",
                "label": "Store a dashboard-issued token locally",
                "command": (
                    "code-mower cloud setup --token-stdin "
                    "--team-id YOUR_TEAM_SLUG --install-id YOUR_INSTALL_ID"
                ),
            },
            {
                "id": "dry-run-upload",
                "label": "Preview the metadata upload without network transfer",
                "command": f"code-mower cloud upload {shlex.quote(str(bundle_dir))} --dry-run --json",
            },
            {
                "id": "upload",
                "label": "Upload metadata after inspecting the dry run",
                "command": f"code-mower cloud upload {shlex.quote(str(bundle_dir))} --yes --json",
            },
        ],
    }


def render_cloud_doctor_text(report: dict[str, Any]) -> str:
    lines = [
        "Code Mower cloud doctor",
        f"Status: {report['status']}",
        f"Endpoint: {report['endpoint']}",
        f"Dashboard: {report.get('dashboard_url', '')}",
        f"Token env: {report['token_env']}",
        f"Bundle: {report['bundle_dir']}",
        "",
    ]
    for check in report["checks"]:
        lines.append(
            f"- {check['status'].upper()} {check['name']}: {check['message']}"
        )
        if check.get("remediation"):
            lines.append(f"  remediation: {check['remediation']}")
    return "\n".join(lines) + "\n"
