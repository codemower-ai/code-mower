"""Calibration runner internals.

The stable user surface is still the ``code-mower calibration`` CLI. These
modules are intentionally small seams for corpus parsing, evidence handling,
metrics, and policy/reporting code as the runner is split out of the legacy
command adapter.
"""

from .corpus import load_json_object, parse_int
from .evidence import (
    KNOWN_EVIDENCE_DISPOSITIONS,
    NON_BLOCKING_CODERABBIT_SEVERITIES,
    USEFUL_EVIDENCE_DISPOSITIONS,
)
from .arms import DEFAULT_CLI_LANES, DEFAULT_LOCAL_LLM_PROFILES, default_arms
from .identity import head_slug, safe_slug
from .metrics import float_or_zero
from .policy import (
    MERGE_GATE_MIN_CLEAN_RUNS,
    MERGE_GATE_MIN_FINDINGS,
    MERGE_GATE_USEFUL_RATE,
    SELECTIVE_USEFUL_RATE,
    build_lane_policy_report,
)
from .run_status import (
    RUN_STATUS_AUDIT_INPUT_INSUFFICIENT,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CATEGORY_ALIASES,
    RUN_STATUS_INFRA_ERROR,
    RUN_STATUS_PASS,
    RUN_STATUS_UNKNOWN,
    count_normalized_findings,
    normalize_run_status_category,
    status_from_verdict,
)

__all__ = [
    "KNOWN_EVIDENCE_DISPOSITIONS",
    "DEFAULT_CLI_LANES",
    "DEFAULT_LOCAL_LLM_PROFILES",
    "MERGE_GATE_MIN_CLEAN_RUNS",
    "MERGE_GATE_MIN_FINDINGS",
    "MERGE_GATE_USEFUL_RATE",
    "NON_BLOCKING_CODERABBIT_SEVERITIES",
    "RUN_STATUS_AUDIT_INPUT_INSUFFICIENT",
    "RUN_STATUS_BLOCKED",
    "RUN_STATUS_CATEGORY_ALIASES",
    "RUN_STATUS_INFRA_ERROR",
    "RUN_STATUS_PASS",
    "RUN_STATUS_UNKNOWN",
    "SELECTIVE_USEFUL_RATE",
    "USEFUL_EVIDENCE_DISPOSITIONS",
    "build_lane_policy_report",
    "count_normalized_findings",
    "default_arms",
    "float_or_zero",
    "head_slug",
    "load_json_object",
    "normalize_run_status_category",
    "parse_int",
    "safe_slug",
    "status_from_verdict",
]
