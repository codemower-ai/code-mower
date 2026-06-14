#!/usr/bin/env python3
"""Render calibration reports from local LLM bakeoff summaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


KNOWN_DISPOSITIONS = {
    "true_positive",
    "false_positive",
    "useful",
    "noise",
    "unknown",
}


@dataclass
class ProfileStats:
    profile_id: str
    model: str = ""
    runs: int = 0
    files_reviewed: int = 0
    blocker_file_count: int = 0
    concern_file_count: int = 0
    parse_failure_count: int = 0
    json_repair_used_count: int = 0
    parse_attempts_total: int = 0
    duration_seconds_total: float = 0.0
    verdicts: Counter[str] = field(default_factory=Counter)
    dispositions: Counter[str] = field(default_factory=Counter)

    def record_run(self, run: Mapping[str, Any]) -> None:
        self.runs += 1
        self.model = str(run.get("model") or self.model)
        self.files_reviewed += _int(run.get("files_reviewed"))
        self.blocker_file_count += _int(run.get("blocker_file_count"))
        self.concern_file_count += _int(run.get("concern_file_count"))
        self.parse_failure_count += _int(run.get("parse_failure_count"))
        self.json_repair_used_count += _int(run.get("json_repair_used_count"))
        self.parse_attempts_total += _int(run.get("parse_attempts_total"))
        self.duration_seconds_total += _float(run.get("duration_seconds"))
        verdict = str(run.get("verdict") or "UNKNOWN")
        self.verdicts[verdict] += 1

    def to_dict(self) -> dict[str, Any]:
        average_duration = (
            self.duration_seconds_total / self.runs
            if self.runs
            else 0.0
        )
        parse_failure_rate = (
            self.parse_failure_count / self.files_reviewed
            if self.files_reviewed
            else 0.0
        )
        return {
            "profile_id": self.profile_id,
            "model": self.model,
            "runs": self.runs,
            "files_reviewed": self.files_reviewed,
            "verdicts": dict(sorted(self.verdicts.items())),
            "blocker_file_count": self.blocker_file_count,
            "concern_file_count": self.concern_file_count,
            "parse_failure_count": self.parse_failure_count,
            "json_repair_used_count": self.json_repair_used_count,
            "parse_attempts_total": self.parse_attempts_total,
            "duration_seconds_total": round(self.duration_seconds_total, 3),
            "duration_seconds_average": round(average_duration, 3),
            "parse_failure_rate": round(parse_failure_rate, 4),
            "dispositions": dict(sorted(self.dispositions.items())),
        }


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return 0


def _float(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def load_dispositions(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    payload = _load_json(path)
    dispositions: dict[str, str] = {}
    for finding_id, value in payload.items():
        if isinstance(value, Mapping):
            disposition = str(value.get("disposition", "unknown"))
        else:
            disposition = str(value)
        disposition = disposition.strip().lower()
        if disposition not in KNOWN_DISPOSITIONS:
            raise ValueError(
                f"unknown disposition for finding {finding_id}: {disposition!r}"
            )
        dispositions[str(finding_id)] = disposition
    return dispositions


def _finding_id(
    *,
    repo: str,
    pr_number: Any,
    head_sha: str,
    profile_id: str,
    severity: str,
    path: str,
    index: int,
    text: str,
) -> str:
    raw = "\n".join(
        [
            repo,
            str(pr_number),
            head_sha,
            profile_id,
            severity,
            path,
            str(index),
            text,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def iter_run_findings(
    payload: Mapping[str, Any],
    run: Mapping[str, Any],
) -> Iterable[dict[str, Any]]:
    repo = str(payload.get("repo") or run.get("repo") or "")
    pr_number = payload.get("pr_number", run.get("pr_number", ""))
    head_sha = str(payload.get("head_sha") or run.get("head_sha_end") or "")
    profile_id = str(run.get("profile_id") or "")

    for finding_group in run.get("blocker_findings", []) or []:
        if not isinstance(finding_group, Mapping):
            continue
        path = str(finding_group.get("path") or "")
        for index, text in enumerate(finding_group.get("blockers", []) or []):
            text = str(text)
            yield {
                "id": _finding_id(
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    profile_id=profile_id,
                    severity="BLOCKER",
                    path=path,
                    index=index,
                    text=text,
                ),
                "profile_id": profile_id,
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "severity": "BLOCKER",
                "path": path,
                "text": text,
            }

    for index, text in enumerate(run.get("pr_level_blockers", []) or []):
        text = str(text)
        yield {
            "id": _finding_id(
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                profile_id=profile_id,
                severity="BLOCKER",
                path="__pr__",
                index=index,
                text=text,
            ),
            "profile_id": profile_id,
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "severity": "BLOCKER",
            "path": "__pr__",
            "text": text,
        }

    for finding_group in run.get("concern_findings", []) or []:
        if not isinstance(finding_group, Mapping):
            continue
        path = str(finding_group.get("path") or "")
        for index, text in enumerate(finding_group.get("concerns", []) or []):
            text = str(text)
            yield {
                "id": _finding_id(
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    profile_id=profile_id,
                    severity="CONCERN",
                    path=path,
                    index=index,
                    text=text,
                ),
                "profile_id": profile_id,
                "repo": repo,
                "pr_number": pr_number,
                "head_sha": head_sha,
                "severity": "CONCERN",
                "path": path,
                "text": text,
            }


def build_calibration_report(
    summaries: Iterable[Path],
    *,
    dispositions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    dispositions = dispositions or {}
    profiles: dict[str, ProfileStats] = {}
    findings: list[dict[str, Any]] = []
    sources: list[str] = []

    for summary_path in summaries:
        payload = _load_json(summary_path)
        sources.append(str(summary_path))
        if payload.get("mode") != "local-llm-bakeoff":
            raise ValueError(f"{summary_path} is not a local-llm-bakeoff summary")
        runs = payload.get("runs")
        if not isinstance(runs, list):
            raise ValueError(f"{summary_path} must include a runs list")
        for run in runs:
            if not isinstance(run, Mapping):
                continue
            profile_id = str(run.get("profile_id") or "")
            if not profile_id:
                continue
            stats = profiles.setdefault(profile_id, ProfileStats(profile_id))
            stats.record_run(run)
            for finding in iter_run_findings(payload, run):
                disposition = dispositions.get(finding["id"], "unknown")
                finding["disposition"] = disposition
                stats.dispositions[disposition] += 1
                findings.append(finding)

    data = {
        "mode": "local-llm-calibration",
        "sources": sources,
        "profiles": {
            profile_id: profiles[profile_id].to_dict()
            for profile_id in sorted(profiles)
        },
        "finding_count": len(findings),
        "findings": findings,
        "recommendations": _recommendations(profiles.values()),
    }
    return data


def build_disposition_template(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable human-adjudication template for report findings."""

    template: dict[str, Any] = {}
    findings = report.get("findings", [])
    if not isinstance(findings, list):
        return template
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        finding_id = str(finding.get("id") or "")
        if not finding_id:
            continue
        template[finding_id] = {
            "disposition": str(finding.get("disposition") or "unknown"),
            "profile_id": str(finding.get("profile_id") or ""),
            "repo": str(finding.get("repo") or ""),
            "pr_number": finding.get("pr_number", ""),
            "head_sha": str(finding.get("head_sha") or ""),
            "severity": str(finding.get("severity") or ""),
            "path": str(finding.get("path") or ""),
            "text": str(finding.get("text") or ""),
        }
    return template


def write_disposition_template(
    path: Path,
    report: Mapping[str, Any],
    *,
    force: bool = False,
) -> None:
    if path.exists() and not force:
        raise ValueError(f"refusing to overwrite existing disposition file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_disposition_template(report)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _recommendations(stats: Iterable[ProfileStats]) -> list[str]:
    recommendations: list[str] = []
    for profile in sorted(stats, key=lambda item: item.profile_id):
        if profile.files_reviewed and profile.parse_failure_count:
            rate = profile.parse_failure_count / profile.files_reviewed
            if rate >= 0.2:
                recommendations.append(
                    f"{profile.profile_id}: high parse-failure rate ({rate:.0%}); keep informational and tighten prompt/output repair before promotion."
                )
        false_blockers = profile.dispositions.get("false_positive", 0) + profile.dispositions.get("noise", 0)
        true_or_useful = profile.dispositions.get("true_positive", 0) + profile.dispositions.get("useful", 0)
        if false_blockers and false_blockers > true_or_useful:
            recommendations.append(
                f"{profile.profile_id}: more adjudicated noise than useful findings; do not use as merge authority yet."
            )
    if not recommendations:
        recommendations.append(
            "Keep local LLM lanes informational until multiple PRs have human dispositions for blocker and concern findings."
        )
    return recommendations


def render_calibration_text(report: Mapping[str, Any]) -> str:
    lines = [
        "Local LLM calibration report",
        "",
        "Profiles:",
    ]
    profiles = report.get("profiles", {})
    if isinstance(profiles, Mapping):
        for profile_id, stats in profiles.items():
            if not isinstance(stats, Mapping):
                continue
            verdicts = ", ".join(
                f"{key}={value}"
                for key, value in sorted((stats.get("verdicts") or {}).items())
            ) or "none"
            lines.extend(
                [
                    f"- {profile_id} ({stats.get('model', '')})",
                    f"  runs: {stats.get('runs', 0)}, files: {stats.get('files_reviewed', 0)}, verdicts: {verdicts}",
                    f"  blockers: {stats.get('blocker_file_count', 0)}, concerns: {stats.get('concern_file_count', 0)}",
                    f"  parse failures: {stats.get('parse_failure_count', 0)}, json repair used: {stats.get('json_repair_used_count', 0)}",
                    f"  avg runtime: {stats.get('duration_seconds_average', 0)}s",
                ]
            )
            dispositions = stats.get("dispositions") or {}
            if dispositions:
                lines.append(
                    "  dispositions: "
                    + ", ".join(f"{key}={value}" for key, value in sorted(dispositions.items()))
                )

    lines.extend(["", "Recommendations:"])
    for recommendation in report.get("recommendations", []) or []:
        lines.append(f"- {recommendation}")

    finding_count = report.get("finding_count", 0)
    lines.extend(["", f"Findings indexed for adjudication: {finding_count}"])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summaries", nargs="+", type=Path)
    parser.add_argument(
        "--dispositions",
        type=Path,
        default=None,
        help="Optional JSON mapping of finding id to true_positive/false_positive/useful/noise.",
    )
    parser.add_argument(
        "--write-disposition-template",
        type=Path,
        default=None,
        help=(
            "Write a JSON adjudication template keyed by finding id. Fill in "
            "disposition values, then pass it back via --dispositions."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow --write-disposition-template to overwrite an existing file.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = build_calibration_report(
            args.summaries,
            dispositions=load_dispositions(args.dispositions),
        )
        if args.write_disposition_template is not None:
            write_disposition_template(
                args.write_disposition_template,
                report,
                force=args.force,
            )
            report["disposition_template_path"] = str(args.write_disposition_template)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_calibration_text(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
