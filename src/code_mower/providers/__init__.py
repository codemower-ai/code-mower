"""Provider metadata helpers.

This package is the landing zone for provider-specific adapter code as Code
Mower moves away from root-level scripts.  Keep imports here lightweight: the
public CLI remains the stable surface while provider internals settle.
"""

from .provenance import (
    TOOL_PROVENANCE_SCHEMA,
    build_code_mower_tool_provenance,
    normalize_tool_provenance,
    runtime_environment,
)
from .local_cli import detect_local_cli_version, safe_version_line

__all__ = [
    "TOOL_PROVENANCE_SCHEMA",
    "build_code_mower_tool_provenance",
    "detect_local_cli_version",
    "normalize_tool_provenance",
    "runtime_environment",
    "safe_version_line",
]
