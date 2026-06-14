"""Cloud bundle schema and safety metadata."""

from __future__ import annotations

from typing import Any


BUNDLE_SCHEMA = "code_mower.cloudBenchmarkBundle.v1"
BUNDLE_MANIFEST_FILENAME = "code-mower-cloud-bundle.json"
MAX_REPORT_UPLOAD_BYTES = 1_000_000
MAX_EVENT_COUNT = 500
SAFE_REPORT_KINDS = {
    "authoring-runs",
    "calibration-runs",
    "lane-policy",
    "reviewer-metrics",
    "spend",
    "value-report",
}
SAFE_EVENT_TYPES = {
    "calibration_run",
    "dogfood_upload",
    "lane_policy_snapshot",
    "reviewer_run",
    "value_report_snapshot",
    "workflow_run",
}
EXCLUDED_CONTENT = (
    "source_code",
    "raw_diffs",
    "raw_model_transcripts",
    "raw_stdout_stderr",
    "auth_probe_output",
    "secrets",
)
EXPECTED_BUNDLE_ENTRIES = {
    "README.md",
    BUNDLE_MANIFEST_FILENAME,
    "reports",
    ".README.md.tmp",
    f".{BUNDLE_MANIFEST_FILENAME}.tmp",
    ".reports.tmp",
}


def is_bundle_manifest(manifest: Any) -> bool:
    return isinstance(manifest, dict) and manifest.get("schema") == BUNDLE_SCHEMA
