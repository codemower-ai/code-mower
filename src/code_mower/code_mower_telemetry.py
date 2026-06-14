#!/usr/bin/env python3
"""Summarize Code Mower audit/review telemetry from JSONL event logs."""

from __future__ import annotations

import argparse
import json
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
    lock = event.get("lock")
    if isinstance(lock, Mapping) and lock.get("lane"):
        return str(lock["lane"])
    active = event.get("active")
    if isinstance(active, Mapping) and active.get("lane"):
        return str(active["lane"])
    return "unknown"


def _event_repo(event: Mapping[str, Any]) -> str:
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
    for key in ("finding_count", "findings_count", "findings"):
        value = event.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, list):
            return len(value)
    return 0


def summarize_events(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    event_list = list(events)
    by_event = Counter(str(event.get("event", "unknown")) for event in event_list)
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
        event_name = str(event.get("event", "unknown"))
        if event_name == "started":
            stats["started"] += 1
        if event_name == "finished":
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
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    raise SystemExit(main())
