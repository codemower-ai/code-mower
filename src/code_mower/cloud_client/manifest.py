"""Bundle manifest loading and path validation helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .bundle import BUNDLE_MANIFEST_FILENAME, is_bundle_manifest
from .errors import CloudBundleError


UPLOAD_IDENTITY_SCHEMA = "code_mower.cloudUploadIdentity.v1"


def read_bundle_manifest(bundle_dir: Path) -> tuple[dict[str, Any], bytes]:
    """Return one bundle manifest and the exact bytes it was parsed from.

    Callers that have to prove which bytes they previewed or submitted need the
    manifest and its digest to come from a single read: a second read can see a
    replaced file of the same shape.
    """

    manifest_path = bundle_dir / BUNDLE_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise CloudBundleError(f"bundle manifest not found: {manifest_path}")
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CloudBundleError(f"unable to read bundle manifest {manifest_path}: {exc}") from exc
    if not is_bundle_manifest(manifest):
        raise CloudBundleError(f"unsupported bundle manifest schema in {manifest_path}")
    return manifest, raw


def load_bundle_manifest(bundle_dir: Path) -> dict[str, Any]:
    return read_bundle_manifest(bundle_dir)[0]


def bundle_manifest_identity(
    manifest: dict[str, Any],
    manifest_bytes: bytes,
) -> dict[str, Any]:
    """Describe the exact manifest bytes and events a producer is acting on.

    Malformed or repeated event rows fail closed rather than being filtered or
    collapsed, so this identity always names every event the payload carries.
    """

    events = manifest.get("events")
    if not isinstance(events, list):
        raise CloudBundleError("bundle manifest events must be a list")
    event_ids: list[str] = []
    event_type_counts: dict[str, int] = {}
    for row in events:
        if not isinstance(row, dict):
            raise CloudBundleError("bundle manifest event rows must be objects")
        event_id = str(row.get("event_id") or "").strip()
        event_type = str(row.get("event_type") or "").strip()
        if not event_id or not event_type:
            raise CloudBundleError("bundle manifest event identity is missing")
        if event_id in event_ids:
            raise CloudBundleError(f"bundle manifest repeats event {event_id}")
        event_ids.append(event_id)
        event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
    return {
        "schema": UPLOAD_IDENTITY_SCHEMA,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "event_count": len(event_ids),
        "event_ids": event_ids,
        "event_type_counts": dict(sorted(event_type_counts.items())),
    }


def report_path_from_manifest(bundle_dir: Path, target: str) -> Path:
    if not target or target.startswith("/") or ".." in Path(target).parts:
        raise CloudBundleError(f"unsafe report target in bundle manifest: {target!r}")
    path = bundle_dir / target
    try:
        resolved = path.resolve()
        bundle_resolved = bundle_dir.resolve()
    except OSError as exc:
        raise CloudBundleError(f"unable to resolve bundle report path {path}: {exc}") from exc
    if not resolved.is_relative_to(bundle_resolved):
        raise CloudBundleError(f"report target escapes bundle directory: {target!r}")
    if not resolved.is_file():
        raise CloudBundleError(f"bundle report file is missing: {target!r}")
    return resolved
