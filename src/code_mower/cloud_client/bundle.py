"""Cloud bundle schema and safety metadata."""

from __future__ import annotations

import re
from typing import Any

from .errors import CloudBundleError


BUNDLE_SCHEMA = "code_mower.cloudBenchmarkBundle.v1"
BUNDLE_MANIFEST_FILENAME = "code-mower-cloud-bundle.json"
MAX_REPORT_UPLOAD_BYTES = 1_000_000
MAX_EVENT_COUNT = 500
MAX_METADATA_DEPTH = 32
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
    "provider_catalog_snapshot",
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
UNSAFE_METADATA_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "auth_output",
    "auth_preview",
    "auth_probe_output",
    "authorization",
    "cookie",
    "output_preview",
    "password",
    "private_key",
    "raw_diff",
    "raw_diffs",
    "raw_model_transcript",
    "raw_model_transcripts",
    "raw_output",
    "raw_stderr",
    "raw_stdout",
    "raw_stdout_stderr",
    "secret",
    "session_id",
    "source_code",
    "stderr",
    "stdout",
    "token_prefix",
    "token_value",
    "transcript",
)
UNSAFE_METADATA_VALUE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"gh[pousr]_[A-Za-z0-9_]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"sk-[A-Za-z0-9_-]{20,}",
        r"cmw_(?:live|test)_[A-Za-z0-9_-]{8,}",
        r"AIza[0-9A-Za-z\-_]{20,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}",
        r"(?:ANTHROPIC_API_KEY|CODE_MOWER_CLOUD_TOKEN|GEMINI_API_KEY|GITHUB_TOKEN|OPENAI_API_KEY)=",
    )
)


def is_bundle_manifest(manifest: Any) -> bool:
    return isinstance(manifest, dict) and manifest.get("schema") == BUNDLE_SCHEMA


def _is_unsafe_metadata_key(key: str) -> bool:
    with_word_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key.strip())
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", with_word_boundaries).lower().strip("_")
    if normalized == "token" or normalized.endswith("_token"):
        return True
    return any(fragment in normalized for fragment in UNSAFE_METADATA_KEY_FRAGMENTS)


def _unsafe_metadata_value_reason(value: str) -> str:
    for pattern in UNSAFE_METADATA_VALUE_PATTERNS:
        if pattern.search(value):
            return pattern.pattern
    return ""


def validate_metadata_payload(
    value: Any,
    *,
    path: str = "$",
    depth: int = 0,
    max_depth: int = MAX_METADATA_DEPTH,
) -> None:
    """Reject structured cloud metadata that looks like raw output or secrets."""

    if depth > max_depth:
        raise CloudBundleError(
            "structured cloud metadata is too deeply nested at "
            f"{path!r}; max depth is {max_depth}"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if _is_unsafe_metadata_key(key_text):
                raise CloudBundleError(
                    "structured cloud metadata contains unsafe field "
                    f"{child_path!r}; exclude raw output, auth state, and secrets"
                )
            validate_metadata_payload(
                item,
                path=child_path,
                depth=depth + 1,
                max_depth=max_depth,
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_metadata_payload(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                max_depth=max_depth,
            )
        return
    if isinstance(value, str):
        reason = _unsafe_metadata_value_reason(value)
        if reason:
            raise CloudBundleError(
                "structured cloud metadata contains a secret-like value at "
                f"{path!r}; matched safety pattern {reason!r}"
            )
