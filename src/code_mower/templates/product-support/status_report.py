#!/usr/bin/env python3
"""Generate a Code Mower owner-status digest from GitHub metadata.

Reads labels, titles, PR state, assignees, timestamps, and optional local Code
Mower spend/value files. It does not read source, diffs, transcripts, or issue
bodies.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any


REPO = os.environ.get("REPO", os.environ.get("GITHUB_REPOSITORY", ""))
NEEDS_OWNER = os.environ.get("NEEDS_OWNER_LABEL", "needs-owner")
READY = os.environ.get("READY_LABEL", "tier:R")
PHASES = [
    label.strip()
    for label in os.environ.get(
        "PHASE_LABELS",
        "phase:0,phase:1,phase:2,phase:3,phase:4,phase:5",
    ).split(",")
    if label.strip()
]
SPEND = Path(os.environ.get("REVIEWER_SPEND_PATH", ".code-mower/reviewer-spend.json"))
VALUE = Path(os.environ.get("REVIEWER_VALUE_REPORT_PATH", ".code-mower/reviewer-value-report.md"))


def gh_json(*args: str) -> list[dict[str, Any]]:
    repo_args = ("-R", REPO) if REPO else ()
    out = subprocess.run(
        ["gh", *args[:2], *repo_args, *args[2:]],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    data = json.loads(out) if out else []
    return data if isinstance(data, list) else []


def issues(state: str) -> list[dict[str, Any]]:
    return gh_json(
        "issue",
        "list",
        "--state",
        state,
        "--limit",
        "500",
        "--json",
        "number,title,labels,milestone,closedAt,assignees",
    )


def prs(state: str) -> list[dict[str, Any]]:
    return gh_json(
        "pr",
        "list",
        "--state",
        state,
        "--limit",
        "200",
        "--json",
        "number,title,labels,mergedAt,createdAt,author,isDraft,headRefName",
    )


def labels(item: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(label.get("name") or "")
        for label in item.get("labels", [])
        if isinstance(label, dict)
    )


def has(item: dict[str, Any], label: str) -> bool:
    return label in labels(item)


def since(value: Any, cutoff: dt.datetime) -> bool:
    if not value:
        return False
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")) >= cutoff
    except ValueError:
        return False


def num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def lane(pr: dict[str, Any]) -> str:
    for label in labels(pr):
        if label.startswith("builder:"):
            return label.split(":", 1)[1]
    author = pr.get("author") if isinstance(pr.get("author"), dict) else {}
    return str(author.get("login") or "unknown")


def audit_state(pr: dict[str, Any]) -> str:
    states: dict[str, str] = {}
    for label in labels(pr):
        if label.startswith("needs-") and label.endswith("-audit"):
            states.setdefault(label[len("needs-") : -len("-audit")], "pending")
        elif label.endswith("-audit-blocked"):
            states[label[: -len("-audit-blocked")]] = "blocked"
        elif label.endswith("-audit-done"):
            states[label[: -len("-audit-done")]] = "done"
    return ", ".join(f"{k}:{v}" for k, v in sorted(states.items())) or "no audit labels"


def row(kind: str, item: dict[str, Any]) -> str:
    return f"- {kind} #{item['number']} {item['title']}"


def spend_rows() -> list[str]:
    try:
        data = json.loads(SPEND.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["- none recorded"]

    if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
        rows = ["| Lane | Cost USD |", "|---|---:|"]
        for name, profile in sorted(data["profiles"].items()):
            cost = num(profile.get("cost_usd") if isinstance(profile, dict) else 0)
            rows.append(f"| {name} | {cost:.4f} |")
        return rows

    records = data.get("runs") if isinstance(data, dict) else data
    if not isinstance(records, list):
        return ["- unrecognized spend schema"]

    totals: dict[str, dict[str, Any]] = {}
    for record in (item for item in records if isinstance(item, dict)):
        name = str(record.get("lane") or record.get("profile") or "unknown")
        total = totals.setdefault(
            name,
            {"runs": 0, "wall": 0.0, "tokens": 0, "cost": 0.0, "verdicts": set()},
        )
        tokens = record.get("tokens")
        if isinstance(tokens, dict):
            token_count = num(tokens.get("total")) or num(tokens.get("total_tokens"))
            token_count = token_count or num(tokens.get("input")) + num(tokens.get("output"))
        else:
            token_count = num(tokens) or num(record.get("total_tokens"))
        total["runs"] = int(total["runs"]) + 1
        total["wall"] = num(total["wall"]) + num(record.get("wall_seconds"))
        total["tokens"] = int(total["tokens"]) + int(token_count)
        total["cost"] = num(total["cost"]) + num(record.get("cost_usd") or record.get("cost"))
        if record.get("verdict"):
            total["verdicts"].add(str(record["verdict"]))

    if not totals:
        return ["- none recorded"]
    rows = [
        "| Lane | Runs | Wall seconds | Tokens | Cost USD | Verdicts |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for name, total in sorted(totals.items()):
        verdicts = ", ".join(sorted(total["verdicts"])) if total["verdicts"] else "-"
        rows.append(
            f"| {name} | {total['runs']} | {num(total['wall']):.0f} | "
            f"{total['tokens']} | {num(total['cost']):.4f} | {verdicts} |"
        )
    return rows


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    week_ago = now - dt.timedelta(days=7)
    open_i, closed_i = issues("open"), issues("closed")
    open_p, merged_p = prs("open"), prs("merged")

    lines = [f"# Code Mower status - {now:%Y-%m-%d}", "", "## Phase Progress", ""]
    if PHASES:
        lines += ["| Phase | Open | Closed | Total | Closed % |", "|---|---:|---:|---:|---:|"]
        for phase in PHASES:
            o = sum(1 for issue in open_i if has(issue, phase))
            c = sum(1 for issue in closed_i if has(issue, phase))
            pct = f"{100 * c / (o + c):.0f}%" if o + c else "-"
            lines.append(f"| {phase} | {o} | {c} | {o + c} | {pct} |")
    else:
        lines.append("- no phase labels configured")

    owner = [row("issue", i) for i in open_i if has(i, NEEDS_OWNER)]
    owner += [row("PR", pr) for pr in open_p if has(pr, NEEDS_OWNER)]
    lines += ["", f"## Needs Owner ({NEEDS_OWNER})", "", *(owner or ["- none"])]

    lines += ["", "## Open PRs", ""]
    for pr in open_p:
        draft = " (draft)" if pr.get("isDraft") else ""
        lines.append(f"- #{pr['number']} {pr['title']} - {lane(pr)}{draft} - {audit_state(pr)}")
    if not open_p:
        lines.append("- none")

    recent = [pr for pr in merged_p if since(pr.get("mergedAt"), week_ago)]
    lines += ["", "## Merged In The Last 7 Days", ""]
    lines += [f"- #{pr['number']} {pr['title']} - {lane(pr)}" for pr in recent] or ["- none"]

    recent_closed = [issue for issue in closed_i if since(issue.get("closedAt"), week_ago)]
    lines += ["", "## Issues Closed In The Last 7 Days", ""]
    lines += [row("issue", issue) for issue in recent_closed] or ["- none"]

    ready = [
        issue
        for issue in open_i
        if has(issue, READY)
        and not has(issue, NEEDS_OWNER)
        and not issue.get("assignees")
        and not any(label.startswith("blocked-by:") for label in labels(issue))
    ]
    lines += ["", f"## Ready Queue ({READY}, Unblocked)", ""]
    lines += [row("issue", issue) for issue in ready[:10]] or ["- none"]

    lines += ["", "## Reviewer Spend", "", *spend_rows()]
    lines += ["", "## Reviewer Value Report", ""]
    lines.append(f"- see `{VALUE}`" if VALUE.exists() else "- none recorded")
    lines += ["", "---", "_Generated by tools/status_report.py from metadata only._"]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
