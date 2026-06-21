"""Provider metadata helpers.

This package is the landing zone for provider-specific adapter code as Code
Mower moves away from root-level scripts.  Keep imports here lightweight: the
public CLI remains the stable surface while provider internals settle.
"""

from .provenance import (
    TOOL_PROVENANCE_SCHEMA,
    build_provider_model_env_report,
    build_provider_lane_tool_provenance,
    build_code_mower_tool_provenance,
    model_env_names,
    normalize_tool_provenance,
    preferred_model_env_name,
    runtime_environment,
)
from .local_cli import detect_local_cli_version, safe_version_line

__all__ = [
    "TOOL_PROVENANCE_SCHEMA",
    "build_code_mower_tool_provenance",
    "build_provider_lane_tool_provenance",
    "build_provider_model_env_report",
    "detect_local_cli_version",
    "model_env_names",
    "normalize_tool_provenance",
    "preferred_model_env_name",
    "runtime_environment",
    "safe_version_line",
]
