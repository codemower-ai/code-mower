"""Calibration runner internals.

The stable user surface is still the ``code-mower calibration`` CLI. These
modules are intentionally small seams for corpus parsing, evidence handling,
metrics, pilot planning, result normalization, truth matching, and
policy/reporting code as the runner is split out of the legacy command adapter.
"""

from .corpus import load_corpus, load_json_object, parse_int
from .evidence import (
    KNOWN_EVIDENCE_DISPOSITIONS,
    NON_BLOCKING_CODERABBIT_SEVERITIES,
    USEFUL_EVIDENCE_DISPOSITIONS,
)
from .evidence_report import build_reviewer_evidence_report, render_evidence_text
from .arms import DEFAULT_CLI_LANES, DEFAULT_LOCAL_LLM_PROFILES, default_arms
from .identity import head_slug, safe_slug
from .metrics import float_or_zero
from .overlap import build_overlap_report, finding_key, render_overlap_text
from .policy import (
    MERGE_GATE_MIN_CLEAN_RUNS,
    MERGE_GATE_MIN_FINDINGS,
    MERGE_GATE_USEFUL_RATE,
    SELECTIVE_USEFUL_RATE,
    build_lane_policy_report,
    render_policy_text,
)
from .planning import build_pilot_plan, render_plan_text
from .results import (
    AUDIT_INPUT_INSUFFICIENT_PATTERNS,
    audit_input_insufficient_result,
    coderabbit_blocking_findings,
    infra_run_record,
    local_llm_findings,
    run_records_from_summary,
)
from .run_results import (
    CALIBRATION_RUN_RESULTS_MODE,
    CALIBRATION_RUN_RESULTS_SCHEMA,
    apply_run_disposition_rules,
    corpus_with_run_results,
    csv_values,
    load_run_results,
    normalize_disposition,
    run_matches_disposition_rule,
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
from .truth import (
    TRUTH_EXPECTATION_ALIASES,
    TRUTH_EXPECTATION_KNOWN_BLOCKED,
    TRUTH_EXPECTATION_KNOWN_CLEAN,
    TRUTH_EXPECTATION_UNKNOWN,
    expected_finding_matches,
    normalize_truth,
    normalize_truth_expectation,
    truth_for_item,
)
from .value_report import build_value_report, render_value_report_text

__all__ = [
    "KNOWN_EVIDENCE_DISPOSITIONS",
    "AUDIT_INPUT_INSUFFICIENT_PATTERNS",
    "CALIBRATION_RUN_RESULTS_MODE",
    "CALIBRATION_RUN_RESULTS_SCHEMA",
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
    "TRUTH_EXPECTATION_ALIASES",
    "TRUTH_EXPECTATION_KNOWN_BLOCKED",
    "TRUTH_EXPECTATION_KNOWN_CLEAN",
    "TRUTH_EXPECTATION_UNKNOWN",
    "USEFUL_EVIDENCE_DISPOSITIONS",
    "build_lane_policy_report",
    "build_reviewer_evidence_report",
    "build_value_report",
    "build_pilot_plan",
    "count_normalized_findings",
    "audit_input_insufficient_result",
    "coderabbit_blocking_findings",
    "apply_run_disposition_rules",
    "corpus_with_run_results",
    "csv_values",
    "default_arms",
    "expected_finding_matches",
    "finding_key",
    "float_or_zero",
    "head_slug",
    "infra_run_record",
    "load_corpus",
    "load_json_object",
    "load_run_results",
    "local_llm_findings",
    "normalize_disposition",
    "normalize_run_status_category",
    "normalize_truth",
    "normalize_truth_expectation",
    "parse_int",
    "build_overlap_report",
    "render_overlap_text",
    "render_evidence_text",
    "render_plan_text",
    "render_policy_text",
    "render_value_report_text",
    "run_records_from_summary",
    "run_matches_disposition_rule",
    "safe_slug",
    "status_from_verdict",
    "truth_for_item",
]
