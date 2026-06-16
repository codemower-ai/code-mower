"""Calibration truth normalization and expected-finding matching."""

from __future__ import annotations

import re
from typing import Any, Mapping


MATCH_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
MATCH_STOPWORDS = {
    "about",
    "after",
    "before",
    "catch",
    "caught",
    "instead",
    "issue",
    "only",
    "should",
    "specific",
    "that",
    "this",
    "until",
    "with",
}
TRUTH_EXPECTATION_UNKNOWN = "unknown"
TRUTH_EXPECTATION_KNOWN_CLEAN = "known_clean"
TRUTH_EXPECTATION_KNOWN_BLOCKED = "known_blocked"
TRUTH_EXPECTATION_ALIASES = {
    "blocked": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "bug": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "catch": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "known-blocked": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "known_blocked": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "seeded-bug": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "seeded_bug": TRUTH_EXPECTATION_KNOWN_BLOCKED,
    "clean": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "known-clean": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "known_clean": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "no-blocker": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "no_blocker": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "pass": TRUTH_EXPECTATION_KNOWN_CLEAN,
    "unknown": TRUTH_EXPECTATION_UNKNOWN,
}


def known_clean_source(source: str) -> bool:
    return source.startswith("known-clean")


def known_blocked_source(source: str) -> bool:
    return source.startswith("known-blocked") or source.startswith("seeded-bug")


def normalize_truth_expectation(value: Any) -> str:
    expectation = str(value or "").strip().lower().replace("-", "_")
    if not expectation:
        return TRUTH_EXPECTATION_UNKNOWN
    return TRUTH_EXPECTATION_ALIASES.get(expectation, TRUTH_EXPECTATION_UNKNOWN)


def truth_from_source(source: str) -> str:
    if known_clean_source(source):
        return TRUTH_EXPECTATION_KNOWN_CLEAN
    if known_blocked_source(source):
        return TRUTH_EXPECTATION_KNOWN_BLOCKED
    return TRUTH_EXPECTATION_UNKNOWN


def normalize_truth(item: Mapping[str, Any], *, source: str | None = None) -> dict[str, Any]:
    """Return the first-class calibration truth block for a corpus item.

    Older corpora encoded ground truth in ``source`` prefixes and per-run
    ``known_clean`` / ``known_blocked`` booleans. Keep those working, but prefer
    an explicit ``truth.expectation`` field for new corpora so value reports do
    not depend on naming conventions.
    """

    raw_truth = item.get("truth")
    truth_mapping = raw_truth if isinstance(raw_truth, Mapping) else {}
    expectation = normalize_truth_expectation(
        truth_mapping.get("expectation")
        or truth_mapping.get("expected_outcome")
        or truth_mapping.get("outcome")
        or truth_mapping.get("status")
    )
    if expectation == TRUTH_EXPECTATION_UNKNOWN:
        if bool(item.get("known_clean")):
            expectation = TRUTH_EXPECTATION_KNOWN_CLEAN
        elif bool(item.get("known_blocked")):
            expectation = TRUTH_EXPECTATION_KNOWN_BLOCKED
        else:
            expectation = truth_from_source(
                str(source if source is not None else item.get("source") or "")
            )
    expected_findings = list(
        truth_mapping.get("expected_findings")
        or item.get("expected_findings")
        or []
    )
    expected_themes = [
        str(theme)
        for theme in truth_mapping.get("expected_themes", []) or []
        if str(theme).strip()
    ]
    return {
        "expectation": expectation,
        "known_clean": expectation == TRUTH_EXPECTATION_KNOWN_CLEAN,
        "known_blocked": expectation == TRUTH_EXPECTATION_KNOWN_BLOCKED,
        "expected_findings": expected_findings,
        "expected_themes": expected_themes,
        "notes": str(truth_mapping.get("notes") or ""),
    }


def truth_for_item(item: Mapping[str, Any]) -> dict[str, Any]:
    truth = item.get("truth")
    if isinstance(truth, Mapping):
        return normalize_truth(
            {**dict(item), "truth": truth},
            source=str(item.get("source") or ""),
        )
    return normalize_truth(item, source=str(item.get("source") or ""))


def _finding_path(finding: Mapping[str, Any]) -> str:
    return str(finding.get("path") or finding.get("file") or finding.get("filename") or "")


def _finding_text(finding: Mapping[str, Any]) -> str:
    parts = [
        str(finding.get(key) or "").strip()
        for key in ("summary", "text", "message", "body", "title", "detail")
    ]
    return " ".join(part for part in parts if part)


def _match_tokens(value: str) -> set[str]:
    return {
        token
        for token in (match.group(0).lower() for match in MATCH_TOKEN_RE.finditer(value))
        if len(token) > 2 and token not in MATCH_STOPWORDS
    }


def _path_matches(expected_path: str, finding_path: str) -> bool:
    if not expected_path:
        return True
    if not finding_path:
        return False
    expected = expected_path.strip().lower()
    found = finding_path.strip().lower()
    return found == expected or found.endswith(f"/{expected}") or expected.endswith(f"/{found}")


def _text_matches(expected_summary: str, finding_text: str) -> bool:
    if not expected_summary:
        return True
    if not finding_text:
        return False
    expected_tokens = _match_tokens(expected_summary)
    if not expected_tokens:
        return expected_summary.lower() in finding_text.lower()
    overlap = expected_tokens & _match_tokens(finding_text)
    return len(overlap) >= min(2, len(expected_tokens))


def expected_finding_matches(expected_findings: Any, findings: Any) -> int:
    if not isinstance(expected_findings, list) or not expected_findings:
        return 0
    if not isinstance(findings, list) or not findings:
        return 0
    matches = 0
    for expected in expected_findings:
        if not isinstance(expected, Mapping):
            continue
        expected_path = str(expected.get("path") or expected.get("file") or "")
        expected_summary = str(expected.get("summary") or expected.get("text") or "")
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            if _path_matches(expected_path, _finding_path(finding)) and _text_matches(
                expected_summary,
                _finding_text(finding),
            ):
                matches += 1
                break
    return matches
