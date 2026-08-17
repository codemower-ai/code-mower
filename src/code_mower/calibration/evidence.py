"""Calibration evidence classification constants."""

KNOWN_EVIDENCE_DISPOSITIONS = {
    "true_positive",
    "useful",
    "false_positive",
    "noise",
    "unknown",
}
USEFUL_EVIDENCE_DISPOSITIONS = {"true_positive", "useful"}
NEGATIVE_EVIDENCE_DISPOSITIONS = {"false_positive", "noise"}
NON_BLOCKING_CODERABBIT_SEVERITIES = {
    "info",
    "informational",
    "low",
    "minor",
    "nit",
    "notice",
    "style",
    "suggestion",
}
