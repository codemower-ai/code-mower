"""Bundle manifest loading and path validation helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bundle import BUNDLE_MANIFEST_FILENAME, is_bundle_manifest
from .errors import CloudBundleError


def load_bundle_manifest(bundle_dir: Path) -> dict[str, Any]:
    manifest_path = bundle_dir / BUNDLE_MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise CloudBundleError(f"bundle manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CloudBundleError(f"unable to read bundle manifest {manifest_path}: {exc}") from exc
    if not is_bundle_manifest(manifest):
        raise CloudBundleError(f"unsupported bundle manifest schema in {manifest_path}")
    return manifest


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
