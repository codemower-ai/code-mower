"""Calibration finding-overlap reports."""

from __future__ import annotations

import hashlib
from itertools import combinations
from typing import Any, Iterable, Mapping


def finding_key(finding: Mapping[str, Any]) -> str:
    raw = "\n".join(
        [
            str(finding.get("repo") or ""),
            str(finding.get("pr_number") or ""),
            str(finding.get("head_sha") or ""),
            str(finding.get("severity") or ""),
            str(finding.get("path") or ""),
            " ".join(str(finding.get("text") or "").lower().split()),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_overlap_report(reports: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    profile_findings: dict[str, set[str]] = {}
    profile_sources: dict[str, set[str]] = {}
    report_count = 0

    for report in reports:
        report_count += 1
        if report.get("mode") != "local-llm-calibration":
            raise ValueError("overlap reports currently expect local-llm-calibration inputs")
        source = ",".join(str(item) for item in report.get("sources", []) or [])
        profiles = report.get("profiles")
        if isinstance(profiles, Mapping):
            for profile_id in profiles:
                profile_id = str(profile_id)
                if not profile_id:
                    continue
                profile_findings.setdefault(profile_id, set())
                if source:
                    profile_sources.setdefault(profile_id, set()).add(source)
        for finding in report.get("findings", []) or []:
            if not isinstance(finding, Mapping):
                continue
            profile_id = str(finding.get("profile_id") or "")
            if not profile_id:
                continue
            profile_findings.setdefault(profile_id, set()).add(finding_key(finding))
            if source:
                profile_sources.setdefault(profile_id, set()).add(source)

    pairs: list[dict[str, Any]] = []
    for left, right in combinations(sorted(profile_findings), 2):
        left_set = profile_findings[left]
        right_set = profile_findings[right]
        union = left_set | right_set
        intersection = left_set & right_set
        jaccard_similarity = len(intersection) / len(union) if union else 1.0
        pairs.append(
            {
                "left": left,
                "right": right,
                "left_findings": len(left_set),
                "right_findings": len(right_set),
                "shared_findings": len(intersection),
                "union_findings": len(union),
                "jaccard_similarity": round(jaccard_similarity, 4),
                "jaccard_distance": round(1.0 - jaccard_similarity, 4),
            }
        )

    return {
        "mode": "code-mower-calibration-overlap",
        "source_report_count": report_count,
        "profiles": {
            profile_id: {
                "finding_count": len(findings),
                "sources": sorted(profile_sources.get(profile_id, set())),
            }
            for profile_id, findings in sorted(profile_findings.items())
        },
        "pairs": pairs,
        "caveat": (
            "Exact text overlap is a conservative proxy. Human adjudication or "
            "semantic clustering is still required before making merge-gate decisions."
        ),
    }


def render_overlap_text(report: Mapping[str, Any]) -> str:
    lines = ["Code Mower calibration overlap", "", "Pairs:"]
    for pair in report.get("pairs", []) or []:
        if isinstance(pair, Mapping):
            lines.append(
                f"- {pair.get('left')} vs {pair.get('right')}: "
                f"shared={pair.get('shared_findings')} "
                f"distance={pair.get('jaccard_distance')}"
            )
    if not report.get("pairs"):
        lines.append("- none")
    lines.extend(["", f"Caveat: {report.get('caveat', '')}"])
    return "\n".join(lines) + "\n"
