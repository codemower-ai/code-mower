#!/usr/bin/env python3
"""Summarize Code Mower audit/review telemetry from JSONL event logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    module_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(module_dir.parent))
    if module_dir.name == "code_mower":  # pragma: no cover - extracted direct CLI.
        from code_mower import audit_handoff_log
    else:
        from tools import audit_handoff_log
elif __package__ == "tools":
    from tools import audit_handoff_log
else:  # pragma: no cover - exercised after package extraction.
    from . import audit_handoff_log


PASS_VERDICTS = {"completed", "done", "pass", "passed", "success", "succeeded"}
BLOCKED_VERDICTS = {"block", "blocked", "fail", "failed", "failure"}
VERDICT_ARTIFACT_SCHEMA = "code_mower.auditVerdictArtifact.v1"
BENCHMARK_EVENT_SCHEMA = "code_mower.benchmarkEvent.v1"
VERDICT_ARTIFACT_DIR_ENV = "CODE_MOWER_VERDICT_ARTIFACT_DIR"
DEFAULT_VERDICT_ARTIFACT_DIR = Path.home() / ".cache" / "code-mower-audits" / "verdicts"
SEVERITY_RE = re.compile(r"\b(P[0-3])=(\d+)\b")


def default_verdict_artifact_dir() -> Path:
    configured = os.environ.get(VERDICT_ARTIFACT_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_VERDICT_ARTIFACT_DIR


def _normalize_repo_slug(value: str) -> str:
    text = value.strip().strip("/")
    parts = [part for part in text.split("/") if part]
    if len(parts) != 2:
        return text
    return f"{parts[0]}/{parts[1]}"


def _repo_slug_from_artifact_path(path: Path) -> str:
    for parent in [path.parent, *path.parents]:
        name = parent.name
        if "__" in name:
            owner, repo = name.split("__", 1)
            if owner and repo:
                return f"{owner}/{repo}"
    return ""


def _artifact_pr_from_path(path: Path) -> int | None:
    for parent in [path.parent, *path.parents]:
        match = re.fullmatch(r"pr-(\d+)", parent.name)
        if match:
            return int(match.group(1))
    return None


def _lane_provider(lane_id: str) -> str:
    if lane_id.endswith("-audit"):
        return lane_id.removesuffix("-audit")
    return lane_id or "unknown"


def _severity_counts(comment_body: str) -> dict[str, int]:
    counts = {"p0": 0, "p1": 0, "p2": 0, "p3": 0}
    for severity, value in SEVERITY_RE.findall(comment_body):
        counts[severity.lower()] += int(value)
    return counts


def _coerce_severity_counts(value: Any) -> dict[str, int] | None:
    if not isinstance(value, Mapping):
        return None
    counts = {"p0": 0, "p1": 0, "p2": 0, "p3": 0}
    found = False
    for key in counts:
        raw = value.get(key) if key in value else value.get(key.upper())
        if raw is None:
            continue
        try:
            counts[key] = int(raw)
        except (TypeError, ValueError):
            return None
        found = True
    return counts if found else None


def _artifact_severity_counts(payload: Mapping[str, Any]) -> tuple[dict[str, int], str]:
    structured_counts = _coerce_severity_counts(payload.get("severity_counts"))
    if structured_counts is not None:
        return structured_counts, "structured"
    finding_count = payload.get("finding_count")
    if isinstance(finding_count, int):
        return {"p0": 0, "p1": 0, "p2": 0, "p3": 0}, "finding_count_only"
    comment_counts = _severity_counts(str(payload.get("comment_body") or ""))
    if any(comment_counts.values()):
        return comment_counts, "severity_summary"
    return comment_counts, "unavailable"


def _normal_artifact_verdict(value: Any) -> str:
    verdict = str(value or "").strip().lower()
    if verdict in PASS_VERDICTS:
        return "pass"
    if verdict in BLOCKED_VERDICTS:
        return "blocked"
    return verdict or "unknown"


def _verdict_event_id(payload: Mapping[str, Any], path: Path) -> str:
    # Keep event IDs stable without encoding private git refs. Git refs are only
    # exported when the operator explicitly passes --include-git-ref.
    seed = "|".join(
        [
            str(payload.get("repo") or _repo_slug_from_artifact_path(path)),
            str(payload.get("pr_number") or _artifact_pr_from_path(path) or ""),
            str(payload.get("lane_id") or ""),
            str(payload.get("verdict") or ""),
            str(payload.get("created_at") or ""),
            path.name,
        ]
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return f"verdict-artifact-{digest}"


def reviewer_run_event_from_verdict_artifact(
    payload: Mapping[str, Any],
    *,
    path: Path,
    include_git_ref: bool = False,
) -> dict[str, Any]:
    if payload.get("schema") != VERDICT_ARTIFACT_SCHEMA:
        raise ValueError(
            f"unsupported verdict artifact schema {payload.get('schema')!r}; "
            f"expected {VERDICT_ARTIFACT_SCHEMA!r}"
        )
    lane_id = str(payload.get("lane_id") or "unknown")
    repo_slug = _normalize_repo_slug(
        str(payload.get("repo") or _repo_slug_from_artifact_path(path))
    )
    pr_number = payload.get("pr_number")
    if pr_number is None:
        pr_number = _artifact_pr_from_path(path)
    try:
        pr_number_value: int | None = int(pr_number) if pr_number is not None else None
    except (TypeError, ValueError):
        pr_number_value = None
    severities, severity_source = _artifact_severity_counts(payload)
    finding_count_raw = payload.get("finding_count")
    finding_count = (
        int(finding_count_raw)
        if isinstance(finding_count_raw, int)
        else sum(severities.values())
    )
    status = _normal_artifact_verdict(payload.get("verdict"))
    dimensions: dict[str, Any] = {
        "lane_id": lane_id,
        "pr_number": pr_number_value,
        "artifact_source": "local-verdict-cache",
        "git_ref_included": include_git_ref,
        "severity_count_source": severity_source,
    }
    if include_git_ref:
        head = str(payload.get("head_sha_end") or payload.get("head_sha_start") or "")
        dimensions["head_sha"] = head
        dimensions["head_sha_short"] = head[:12]
    return {
        "schema": BENCHMARK_EVENT_SCHEMA,
        "event_type": "reviewer_run",
        "event_id": _verdict_event_id(payload, path),
        "created_at": str(payload.get("created_at") or ""),
        "repo_slug": repo_slug,
        "source": "verdict-artifact-export",
        "provider": _lane_provider(lane_id),
        "lens": "base",
        "status": status,
        "metrics": {
            "finding_count": finding_count,
            "p0_count": severities["p0"],
            "p1_count": severities["p1"],
            "p2_count": severities["p2"],
            "p3_count": severities["p3"],
        },
        "dimensions": dimensions,
    }


def _iter_verdict_artifact_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    return sorted(root.glob("**/*.json"))


def export_reviewer_run_events_from_verdicts(
    root: Path,
    *,
    repo: str = "",
    limit: int = 0,
    include_git_ref: bool = False,
) -> list[dict[str, Any]]:
    repo_filter = _normalize_repo_slug(repo) if repo else ""
    events: list[dict[str, Any]] = []
    for path in _iter_verdict_artifact_paths(root.expanduser()):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: invalid verdict artifact: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path}: verdict artifact must be a JSON object")
        if payload.get("schema") != VERDICT_ARTIFACT_SCHEMA:
            continue
        artifact_repo = _normalize_repo_slug(
            str(payload.get("repo") or _repo_slug_from_artifact_path(path))
        )
        if repo_filter and artifact_repo != repo_filter:
            continue
        events.append(
            reviewer_run_event_from_verdict_artifact(
                payload,
                path=path,
                include_git_ref=include_git_ref,
            )
        )
    events.sort(
        key=lambda event: (str(event.get("created_at") or ""), str(event.get("event_id") or "")),
        reverse=True,
    )
    return events[:limit] if limit else events


def load_jsonl_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not path.exists():
        return events
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_no}: event must be a JSON object")
        events.append(payload)
    return events


def _event_lane(event: Mapping[str, Any]) -> str:
    lane = event.get("lane")
    if lane:
        return str(lane)
    dimensions = event.get("dimensions")
    if isinstance(dimensions, Mapping) and dimensions.get("lane_id"):
        return str(dimensions["lane_id"])
    provider = event.get("provider")
    if provider:
        lens = str(event.get("lens") or "base")
        return f"{provider}:{lens}" if lens else str(provider)
    lock = event.get("lock")
    if isinstance(lock, Mapping) and lock.get("lane"):
        return str(lock["lane"])
    active = event.get("active")
    if isinstance(active, Mapping) and active.get("lane"):
        return str(active["lane"])
    return "unknown"


def _event_repo(event: Mapping[str, Any]) -> str:
    repo_slug = event.get("repo_slug")
    if repo_slug:
        return str(repo_slug)
    repo = event.get("repo")
    if repo:
        return str(repo)
    lock = event.get("lock")
    if isinstance(lock, Mapping) and lock.get("repo"):
        return str(lock["repo"])
    return "unknown"


def _event_pr(event: Mapping[str, Any]) -> str:
    pr = event.get("pr")
    if pr is not None:
        return str(pr)
    dimensions = event.get("dimensions")
    if isinstance(dimensions, Mapping) and dimensions.get("pr_number") is not None:
        return str(dimensions["pr_number"])
    lock = event.get("lock")
    if isinstance(lock, Mapping) and lock.get("pr") is not None:
        return str(lock["pr"])
    return "unknown"


def _event_head(event: Mapping[str, Any]) -> str:
    head = event.get("head")
    if head:
        return str(head)
    lock = event.get("lock")
    if isinstance(lock, Mapping) and lock.get("head"):
        return str(lock["head"])
    return "unknown"


def _normal_verdict(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in PASS_VERDICTS:
        return "pass"
    if text in BLOCKED_VERDICTS:
        return "blocked"
    return text or "unknown"


def _event_outcome(event: Mapping[str, Any]) -> str:
    if event.get("verdict") is not None:
        return _normal_verdict(event.get("verdict"))
    status = str(event.get("status") or "").strip().lower()
    if status in PASS_VERDICTS or status in BLOCKED_VERDICTS:
        return _normal_verdict(status)
    return "unknown"


def _finding_count(event: Mapping[str, Any]) -> int:
    metrics = event.get("metrics")
    if isinstance(metrics, Mapping):
        for key in ("finding_count", "findings_count", "findings"):
            value = metrics.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, list):
                return len(value)
    for key in ("finding_count", "findings_count", "findings"):
        value = event.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
    return 0


def summarize_events(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    event_list = list(events)
    by_event = Counter(
        str(event.get("event") or event.get("event_type") or "unknown")
        for event in event_list
    )
    by_lane = Counter(_event_lane(event) for event in event_list)
    by_verdict = Counter(_event_outcome(event) for event in event_list)
    by_verdict.pop("unknown", None)

    lane_stats: dict[str, dict[str, Any]] = {}
    lane_heads: dict[str, set[str]] = defaultdict(set)
    lane_repos: dict[str, set[str]] = defaultdict(set)
    lane_prs: dict[str, set[str]] = defaultdict(set)
    lane_repo_prs: dict[str, set[str]] = defaultdict(set)
    authoring_tools = Counter()

    for event in event_list:
        lane = _event_lane(event)
        repo = _event_repo(event)
        pr = _event_pr(event)
        stats = lane_stats.setdefault(
            lane,
            {
                "events": 0,
                "started": 0,
                "finished": 0,
                "pass": 0,
                "blocked": 0,
                "failed": 0,
                "findings": 0,
            },
        )
        stats["events"] += 1
        event_name = str(event.get("event") or event.get("event_type") or "unknown")
        if event_name == "started":
            stats["started"] += 1
        if event_name in {"finished", "reviewer_run", "calibration_run"}:
            stats["finished"] += 1
        verdict = _event_outcome(event)
        if verdict in {"pass", "blocked"}:
            stats[verdict] += 1
        if str(event.get("status", "")).lower() == "failed":
            stats["failed"] += 1
        stats["findings"] += _finding_count(event)
        lane_heads[lane].add(_event_head(event))
        lane_repos[lane].add(repo)
        lane_prs[lane].add(pr)
        if repo != "unknown" or pr != "unknown":
            lane_repo_prs[lane].add(f"{repo}#{pr}")

        authoring_tool = (
            event.get("authoring_tool")
            or event.get("authoringTool")
            or event.get("tool")
        )
        if authoring_tool:
            authoring_tools[str(authoring_tool)] += 1

    for lane, stats in lane_stats.items():
        heads = sorted(item for item in lane_heads[lane] if item != "unknown")
        repos = sorted(item for item in lane_repos[lane] if item != "unknown")
        prs = sorted(item for item in lane_prs[lane] if item != "unknown")
        repo_prs = sorted(item for item in lane_repo_prs[lane] if item != "unknown#unknown")
        stats["heads"] = heads
        stats["repositories"] = repos
        stats["pull_requests"] = prs
        stats["repo_pull_requests"] = repo_prs
        stats["observed_pr_count"] = len(repo_prs)
        finished = stats["finished"]
        stats["pass_rate"] = round(stats["pass"] / finished, 4) if finished else None
        stats["blocked_rate"] = (
            round(stats["blocked"] / finished, 4) if finished else None
        )

    return {
        "mode": "telemetry-summary",
        "event_count": len(event_list),
        "events_by_type": dict(sorted(by_event.items())),
        "events_by_lane": dict(sorted(by_lane.items())),
        "verdicts": dict(sorted(by_verdict.items())),
        "lanes": dict(sorted(lane_stats.items())),
        "authoring_intelligence": {
            "observed_authoring_tools": dict(sorted(authoring_tools.items())),
            "caveat": (
                "Observational telemetry can compare findings density and "
                "head-to-head catch differential, but it is not causal proof "
                "of which AI writes better code."
            ),
        },
    }


def render_summary_text(summary: Mapping[str, Any]) -> str:
    lines = [
        "Code Mower telemetry summary",
        f"Events: {summary.get('event_count', 0)}",
        "",
        "Lanes:",
    ]
    lanes = summary.get("lanes", {})
    if isinstance(lanes, Mapping) and lanes:
        for lane, stats in lanes.items():
            if not isinstance(stats, Mapping):
                continue
            lines.append(
                f"- {lane}: finished={stats.get('finished', 0)} "
                f"pass={stats.get('pass', 0)} blocked={stats.get('blocked', 0)} "
                f"findings={stats.get('findings', 0)}"
            )
    else:
        lines.append("- none")
    caveat = summary.get("authoring_intelligence", {})
    if isinstance(caveat, Mapping):
        lines.extend(["", f"Caveat: {caveat.get('caveat', '')}"])
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    summarize = subparsers.add_parser("summarize")
    summarize.add_argument(
        "events",
        nargs="?",
        type=Path,
        default=audit_handoff_log.events_path(),
    )
    summarize.add_argument("--json", action="store_true")
    export_verdicts = subparsers.add_parser(
        "export-verdict-events",
        help="Export saved audit verdict artifacts as metadata-only reviewer_run JSONL events.",
    )
    export_verdicts.add_argument(
        "verdicts",
        nargs="?",
        type=Path,
        default=default_verdict_artifact_dir(),
        help="Verdict artifact file or directory.",
    )
    export_verdicts.add_argument("--repo", default="", help="Optional owner/repo filter.")
    export_verdicts.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum events to export; 0 means no limit.",
    )
    export_verdicts.add_argument(
        "--include-git-ref",
        action="store_true",
        help="Include head SHA metadata. Off by default for privacy.",
    )
    export_verdicts.add_argument(
        "--output",
        type=Path,
        help="Write JSONL events to this path instead of stdout.",
    )
    args = parser.parse_args(argv)

    if args.command == "summarize":
        try:
            summary = summarize_events(load_jsonl_events(args.events))
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        if args.json:
            print(json.dumps(summary, indent=2, sort_keys=True))
        else:
            print(render_summary_text(summary), end="")
        return 0

    if args.command == "export-verdict-events":
        try:
            events = export_reviewer_run_events_from_verdicts(
                args.verdicts,
                repo=args.repo,
                limit=args.limit,
                include_git_ref=args.include_git_ref,
            )
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        text = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
        if not events:
            print(
                "warning: no verdict artifacts matched; no reviewer_run events exported",
                file=sys.stderr,
            )
            return 0
        if args.output:
            try:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(text, encoding="utf-8")
            except OSError as exc:
                print(f"error: failed to write {args.output}: {exc}", file=sys.stderr)
                return 1
        else:
            print(text, end="")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
