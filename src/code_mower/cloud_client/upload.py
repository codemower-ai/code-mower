"""Upload payload construction and network posting for CodeMower.com."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .bundle import (
    MAX_REPORT_UPLOAD_BYTES,
    validate_metadata_payload,
)
from .endpoints import validate_upload_endpoint
from .errors import CloudBundleError
from .manifest import load_bundle_manifest, report_path_from_manifest


UPLOAD_SCHEMA = "code_mower.cloudUpload.v1"


def _validate_upload_endpoint(endpoint: str) -> None:
    try:
        validate_upload_endpoint(endpoint)
    except ValueError as exc:
        raise CloudBundleError(str(exc)) from exc


def _included_report_payloads(
    manifest: dict[str, Any],
    bundle_dir: Path,
    *,
    include_reports: bool,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for entry in manifest.get("included_reports", []):
        if not isinstance(entry, dict):
            raise CloudBundleError("bundle manifest has a non-object included_reports entry")
        target = str(entry.get("target", ""))
        report_payload = {
            "kind": entry.get("kind", ""),
            "target": target,
            "bytes": entry.get("bytes", 0),
            "source_basename": entry.get("source_basename", ""),
        }
        if include_reports:
            path = report_path_from_manifest(bundle_dir, target)
            size = path.stat().st_size
            if size > MAX_REPORT_UPLOAD_BYTES:
                raise CloudBundleError(
                    f"refusing to upload {target}: {size} bytes exceeds "
                    f"{MAX_REPORT_UPLOAD_BYTES} byte limit"
                )
            try:
                report_payload["text"] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise CloudBundleError(f"report is not UTF-8 text: {target}") from exc
            except OSError as exc:
                raise CloudBundleError(f"unable to read report {target}: {exc}") from exc
        payloads.append(report_payload)
    return payloads


def build_upload_payload(
    *,
    bundle_dir: Path,
    include_reports: bool = False,
) -> dict[str, Any]:
    bundle_dir = bundle_dir.expanduser()
    if not bundle_dir.is_dir():
        raise CloudBundleError(f"bundle directory does not exist: {bundle_dir}")
    manifest = load_bundle_manifest(bundle_dir)
    validate_metadata_payload(manifest)
    events = manifest.get("events", [])
    return {
        "schema": UPLOAD_SCHEMA,
        "bundle_schema": manifest.get("schema"),
        "privacy_mode": manifest.get("privacy_mode", ""),
        "upload_mode": "reports_included" if include_reports else "metadata_only",
        "repo_slug": manifest.get("repo_slug", ""),
        "team_id": manifest.get("team_id", ""),
        "install_id": manifest.get("install_id", ""),
        "excluded_content": manifest.get("excluded_content", []),
        "reports": _included_report_payloads(
            manifest,
            bundle_dir,
            include_reports=include_reports,
        ),
        "events": events,
        "notes": [
            "This upload payload is built from an explicit local bundle.",
            "Report contents are included only when --include-reports is set.",
        ],
    }


def post_upload_payload(
    *,
    payload: dict[str, Any],
    endpoint: str,
    token: str = "",
    timeout: float = 20.0,
) -> dict[str, Any]:
    _validate_upload_endpoint(endpoint)
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "code-mower-cloud-upload",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
            status = response.getcode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CloudBundleError(f"upload failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise CloudBundleError(f"upload failed: {exc.reason}") from exc
    try:
        parsed = json.loads(response_body) if response_body else {}
    except json.JSONDecodeError as exc:
        raise CloudBundleError(f"upload response was not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CloudBundleError("upload response JSON must be an object")
    return {
        "mode": "cloud-upload",
        "endpoint": endpoint,
        "status": status,
        "response": parsed,
    }
