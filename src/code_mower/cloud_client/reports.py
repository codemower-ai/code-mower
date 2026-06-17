"""Bundle report payload construction helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .bundle import MAX_REPORT_UPLOAD_BYTES
from .errors import CloudBundleError
from .manifest import report_path_from_manifest


def included_report_payloads(
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
