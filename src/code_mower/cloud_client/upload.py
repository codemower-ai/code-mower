"""Upload payload construction and network posting for CodeMower.com."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .bundle import (
    BUNDLE_SCHEMA,
    EXCLUDED_CONTENT,
    MAX_EVENT_COUNT,
    validate_metadata_payload,
)
from .endpoints import validate_upload_endpoint
from .errors import CloudBundleError
from .events import normalize_event, validate_cloud_event
from .export import build_provenance_summary
from .manifest import load_bundle_manifest
from .reports import included_report_payloads


UPLOAD_SCHEMA = "code_mower.cloudUpload.v1"


def _validate_upload_endpoint(endpoint: str) -> None:
    try:
        validate_upload_endpoint(endpoint)
    except ValueError as exc:
        raise CloudBundleError(str(exc)) from exc


def build_upload_payload_from_manifest(
    manifest: dict[str, Any],
    *,
    reports: list[dict[str, Any]] | None = None,
    include_reports: bool = False,
) -> dict[str, Any]:
    """Assemble one upload payload from an already-loaded bundle manifest.

    The single place the upload payload shape is written down, so a caller that
    holds a manifest in memory (no bundle directory on disk) posts exactly the
    same payload as one built from an exported bundle, and the metadata-only
    validation both go through is the same call.
    """

    validate_metadata_payload(manifest)
    return {
        "schema": UPLOAD_SCHEMA,
        "bundle_schema": manifest.get("schema"),
        "privacy_mode": manifest.get("privacy_mode", ""),
        "upload_mode": "reports_included" if include_reports else "metadata_only",
        "repo_slug": manifest.get("repo_slug", ""),
        "team_id": manifest.get("team_id", ""),
        "install_id": manifest.get("install_id", ""),
        "provenance": manifest.get("provenance", {}),
        "excluded_content": manifest.get("excluded_content", []),
        "reports": list(reports or []),
        "events": manifest.get("events", []),
        "notes": [
            "This upload payload is built from an explicit local bundle.",
            "Report contents are included only when --include-reports is set.",
        ],
    }


def build_upload_payload(
    *,
    bundle_dir: Path,
    include_reports: bool = False,
) -> dict[str, Any]:
    bundle_dir = bundle_dir.expanduser()
    if not bundle_dir.is_dir():
        raise CloudBundleError(f"bundle directory does not exist: {bundle_dir}")
    manifest = load_bundle_manifest(bundle_dir)
    return build_upload_payload_from_manifest(
        manifest,
        reports=included_report_payloads(
            manifest,
            bundle_dir,
            include_reports=include_reports,
        ),
        include_reports=include_reports,
    )


def build_event_upload_payload(
    *,
    events: list[dict[str, Any]],
    repo_slug: str = "",
    team_id: str = "",
    install_id: str = "",
) -> dict[str, Any]:
    """Build a metadata-only upload payload from events already held in memory.

    For callers that publish structured events they just derived from local
    state -- release campaign evidence, for one -- rather than from an exported
    bundle directory. Reports are structurally impossible here: the payload
    carries no report entries at all, so no report text can cross the boundary.
    Events go through the same ``normalize_event``/``validate_cloud_event``
    boundary and the same metadata-only manifest validation as an exported
    bundle's, and the same ``MAX_EVENT_COUNT`` bound applies.
    """

    normalized = [
        validate_cloud_event(
            normalize_event(dict(event), str(event.get("event_type") or ""))
        )
        for event in events
    ]
    if len(normalized) > MAX_EVENT_COUNT:
        raise CloudBundleError(
            f"too many events: {len(normalized)}; max {MAX_EVENT_COUNT}"
        )
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "privacy_mode": "metadata_only",
        "repo_slug": repo_slug,
        "team_id": team_id,
        "install_id": install_id,
        "provenance": build_provenance_summary(normalized),
        "excluded_content": list(EXCLUDED_CONTENT),
        "events": normalized,
    }
    return build_upload_payload_from_manifest(manifest)


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
