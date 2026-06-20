"""Metadata-only provider/tool provenance helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

TOOL_PROVENANCE_SCHEMA = "code_mower.toolProvenance.v1"

TOOL_PROVENANCE_FIELDS = (
    "schema",
    "role",
    "tool_name",
    "tool_version",
    "provider",
    "model",
    "model_version_raw",
    "integration",
    "lens",
    "runtime_environment",
    "prompt_pack_version",
    "source",
)


def _safe_text(value: Any, *, max_length: int = 180) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:max_length]


def runtime_environment() -> str:
    """Return a coarse, privacy-safe runtime environment label."""

    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "github-actions"
    if os.environ.get("CI"):
        return "ci"
    return "local"


def _role_for_event_type(event_type: str) -> str:
    if event_type in {"reviewer_run", "calibration_run", "value_report_snapshot"}:
        return "reviewer"
    if event_type == "workflow_run":
        return "workflow"
    if event_type == "dogfood_upload":
        return "reporter"
    if event_type == "lane_policy_snapshot":
        return "policy"
    return "unknown"


def normalize_tool_provenance(
    value: Any,
    *,
    event: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Normalize tool/model provenance without accepting raw transcripts.

    The returned object intentionally stores only coarse metadata that helps
    compare AI builders/reviewers: tool name/version, provider, model, lens,
    integration, and runtime surface. It must not include prompts, diffs,
    account identifiers, auth output, or session ids.
    """

    source: Mapping[str, Any] = value if isinstance(value, Mapping) else {}
    event = event or {}
    event_type = _safe_text(event.get("event_type"))
    provider = _safe_text(source.get("provider") or event.get("provider"))
    lens = _safe_text(source.get("lens") or event.get("lens"))
    integration = _safe_text(source.get("integration") or event.get("source"))
    tool_name = _safe_text(
        source.get("tool_name")
        or source.get("name")
        or provider
        or integration
        or event.get("source")
    )
    return {
        "schema": TOOL_PROVENANCE_SCHEMA,
        "role": _safe_text(source.get("role") or _role_for_event_type(event_type)),
        "tool_name": tool_name,
        "tool_version": _safe_text(source.get("tool_version") or source.get("version")),
        "provider": provider,
        "model": _safe_text(source.get("model")),
        "model_version_raw": _safe_text(source.get("model_version_raw")),
        "integration": integration,
        "lens": lens,
        "runtime_environment": _safe_text(
            source.get("runtime_environment") or runtime_environment()
        ),
        "prompt_pack_version": _safe_text(source.get("prompt_pack_version")),
        "source": _safe_text(source.get("source") or event.get("source")),
    }


def build_code_mower_tool_provenance(
    *,
    source: str,
    version: str,
    role: str = "reporter",
) -> dict[str, str]:
    """Build provenance for Code Mower-generated operational events."""

    return normalize_tool_provenance(
        {
            "role": role,
            "tool_name": "code-mower",
            "tool_version": version,
            "provider": "code-mower",
            "integration": source,
            "source": source,
        }
    )
