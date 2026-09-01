#!/usr/bin/env python3
"""Concise lane visibility for Code Mower operators."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any


LANE_STATUS_SCHEMA = "code_mower.laneStatus.v1"
WORKFLOW_TERMS = ("code mower", "code-mower", "gate", "audit", "dispatch", "labeler", "clear-stale")
CHECK_TERMS = ("code-mower", "gate", "package", "audit", "label", "dispatch", "gitar", "clear-stale")
PROCESS_PROVIDERS = {
    "antigravity": "antigravity",
    "claude": "claude",
    "codex": "codex",
    "cursor": "cursor",
    "gemini": "gemini",
    "gitar": "gitar",
}
AGENTTRAIL_DEFAULT_PORTS = {5330, 5331}


class LaneStatusUnavailable(RuntimeError):
    """Raised when an optional lane-status input cannot be read safely."""


GitHubJsonRunner = Callable[[Sequence[str]], Any]
CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _run_gh_json(args: Sequence[str]) -> Any:
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise LaneStatusUnavailable(f"gh {args[0] if args else 'command'} failed")
    try:
        return json.loads(completed.stdout or "null")
    except json.JSONDecodeError as exc:
        raise LaneStatusUnavailable("gh returned non-JSON output") from exc


def _run_command(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), check=False, text=True, capture_output=True, timeout=3)


def _stdout(command_runner: CommandRunner, args: Sequence[str]) -> str:
    try:
        completed = command_runner(args)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (completed.stdout or "") if completed.returncode == 0 else ""


def _label_groups(pr: Mapping[str, Any]) -> dict[str, list[str]]:
    labels = pr.get("labels") if isinstance(pr.get("labels"), list) else []
    names = sorted(
        _text(label.get("name"))
        for label in labels
        if isinstance(label, Mapping) and _text(label.get("name"))
    )
    return {
        "builder": [name for name in names if name.startswith("builder:")],
        "dispatched": [name for name in names if "dispatch" in name],
        "needs": [name for name in names if name.startswith("needs-")],
        "done": [name for name in names if name.endswith("-done")],
        "blocked": [name for name in names if name.endswith("-blocked") or "blocked" in name],
    }


def _check_value(check: Mapping[str, Any], keys: Sequence[str], default: str) -> str:
    for key in keys:
        value = _text(check.get(key))
        if value:
            return value
    return default


def _checks(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    checks = [
        {
            "name": _check_value(check, ("name", "context", "workflowName"), "unknown"),
            "state": _check_value(check, ("conclusion", "state", "status"), "unknown").lower(),
        }
        for check in raw
        if isinstance(check, Mapping)
    ]
    major = [check for check in checks if any(term in check["name"].lower() for term in CHECK_TERMS)]
    return (major or checks)[:8]


def _has_state(checks: Sequence[Mapping[str, str]], states: set[str]) -> bool:
    return any(check.get("state", "") in states for check in checks)


def _next_action(
    labels: Mapping[str, Sequence[str]],
    checks: Sequence[Mapping[str, str]],
    merge_state: str,
    is_draft: bool,
) -> str:
    if labels.get("blocked"):
        return "fix BLOCKED audit"
    if is_draft:
        return "finish draft PR"
    if _has_state(checks, {"failure", "failed", "error", "timed_out", "cancelled"}):
        return "fix failing check"
    if merge_state in {"BEHIND", "DIRTY"}:
        return "rebase/behind"
    if labels.get("needs"):
        return "waiting for audits or owner input"
    if _has_state(checks, {"", "pending", "queued", "in_progress", "requested"}):
        return "waiting for checks"
    if merge_state == "CLEAN" and labels.get("done"):
        return "ready for merge or auto-merge"
    return "inspect PR"


def _stale(
    updated_at: str,
    now: datetime,
    minutes: int,
    labels: Mapping[str, Any],
    checks: Sequence[Mapping[str, str]],
) -> bool:
    if not (labels.get("needs") or _has_state(checks, {"pending", "queued", "in_progress", "requested"})):
        return False
    try:
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return updated < now - timedelta(minutes=minutes)


def _author(pr: Mapping[str, Any]) -> str:
    author = pr.get("author")
    return _text(author.get("login")) if isinstance(author, Mapping) else _text(author)


def _summarize_pr(pr: Mapping[str, Any], now: datetime, stale_minutes: int) -> dict[str, Any]:
    labels = _label_groups(pr)
    checks = _checks(pr.get("statusCheckRollup"))
    merge_state = _text(pr.get("mergeStateStatus")) or "UNKNOWN"
    updated_at = _text(pr.get("updatedAt"))
    is_draft = bool(pr.get("isDraft"))
    return {
        "number": int(pr.get("number") or 0),
        "title": _text(pr.get("title")),
        "url": _text(pr.get("url")),
        "branch": _text(pr.get("headRefName")),
        "author": _author(pr),
        "is_draft": is_draft,
        "merge_state": merge_state,
        "updated_at": updated_at,
        "labels": labels,
        "checks": checks,
        "stale": _stale(updated_at, now, stale_minutes, labels, checks),
        "next_action": _next_action(labels, checks, merge_state, is_draft),
    }


def _summarize_run(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": run.get("databaseId"),
        "workflow": _text(run.get("workflowName") or run.get("name")),
        "title": _text(run.get("displayTitle")),
        "status": _text(run.get("status")),
        "conclusion": _text(run.get("conclusion")),
        "event": _text(run.get("event")),
        "branch": _text(run.get("headBranch")),
        "created_at": _text(run.get("createdAt")),
        "updated_at": _text(run.get("updatedAt")),
        "url": _text(run.get("url")),
    }


def _is_relevant_run(run: Mapping[str, Any]) -> bool:
    fields = ("name", "workflowName", "displayTitle", "headBranch")
    haystack = " ".join(_text(run.get(key)) for key in fields).lower()
    return any(term in haystack for term in WORKFLOW_TERMS)


def _gate_alerts(prs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    alerts = []
    for pr in prs:
        if pr["labels"]["blocked"]:
            alerts.append({"pr_number": pr["number"], "kind": "blocked-audit", "message": f"PR #{pr['number']} has a blocked audit label"})
        if pr["stale"]:
            alerts.append({"pr_number": pr["number"], "kind": "stale-gate", "message": f"PR #{pr['number']} has stale pending audit/gate evidence"})
        if any("gate" in check["name"].lower() and check["state"] in {"failure", "failed", "error"} for check in pr["checks"]):
            alerts.append({"pr_number": pr["number"], "kind": "gate-failed", "message": f"PR #{pr['number']} has failing gate status"})
    return alerts


def _remote(
    repo: str,
    gh_json_runner: GitHubJsonRunner,
    now: datetime,
    pr_limit: int,
    workflow_limit: int,
    stale_minutes: int,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        raw_prs = gh_json_runner([
            "pr", "list", "--repo", repo, "--state", "open", "--limit", str(pr_limit),
            "--json", "number,title,url,headRefName,author,isDraft,mergeStateStatus,updatedAt,labels,statusCheckRollup",
        ])
    except LaneStatusUnavailable as exc:
        raw_prs = []
        errors.append(f"pull_requests: {exc}")
    raw_prs = raw_prs if isinstance(raw_prs, list) else []
    prs = [_summarize_pr(pr, now, stale_minutes) for pr in raw_prs if isinstance(pr, Mapping)]

    try:
        raw_runs = gh_json_runner([
            "run", "list", "--repo", repo, "--limit", str(workflow_limit),
            "--json", "databaseId,name,workflowName,displayTitle,status,conclusion,event,headBranch,createdAt,updatedAt,url",
        ])
    except LaneStatusUnavailable as exc:
        raw_runs = []
        errors.append(f"workflow_runs: {exc}")
    raw_runs = raw_runs if isinstance(raw_runs, list) else []
    runs = [_summarize_run(run) for run in raw_runs if isinstance(run, Mapping) and _is_relevant_run(run)][:10]
    alerts = _gate_alerts(prs)
    return {"available": not errors, "errors": errors, "pull_requests": prs, "workflow_runs": runs, "gate_health": {"status": "warn" if alerts else "pass", "alerts": alerts}}


def _port(name: str) -> int | None:
    match = re.search(r":(\d+)(?:\s|$)", name)
    return int(match.group(1)) if match else None


def _listeners(text: str) -> list[dict[str, Any]]:
    current: dict[str, Any] = {}
    found = []
    for line in text.splitlines():
        if not line:
            continue
        key, value = line[0], line[1:]
        if key == "p":
            current = {"pid": value}
        elif key == "c":
            current["process"] = value
        elif key == "n" and current.get("pid") and _port(value) is not None:
            found.append({"pid": int(current["pid"]), "process": _text(current.get("process")), "port": _port(value)})
    return found


def _process_cwd(pid: int, command_runner: CommandRunner) -> str:
    for line in _stdout(command_runner, ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).splitlines():
        if line.startswith("n"):
            return line[1:].strip()
    return ""


def _process_command(pid: int, command_runner: CommandRunner) -> str:
    return _stdout(command_runner, ["ps", "-p", str(pid), "-o", "command="]).strip()


def collect_agenttrail_boards(command_runner: CommandRunner = _run_command) -> dict[str, Any]:
    text = _stdout(command_runner, ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-FnPcn"])
    if not text:
        return {"available": False, "boards": [], "message": "local listener inventory unavailable"}
    boards = []
    for listener in _listeners(text):
        pid = int(listener["pid"])
        command = _process_command(pid, command_runner)
        cwd = _process_cwd(pid, command_runner)
        haystack = f"{listener['process']} {command} {cwd}".lower()
        default_port = listener["port"] in AGENTTRAIL_DEFAULT_PORTS
        node_like = str(listener["process"]).lower() in {"node", "npx"}
        if "agenttrail" in haystack or (default_port and node_like):
            confidence = "high" if "agenttrail" in haystack else "medium"
            boards.append({**listener, "cwd": cwd, "confidence": confidence})
    return {"available": True, "boards": boards, "message": ""}


def _provider(command: str) -> str:
    executable = os.path.basename(command.split()[0]).lower() if command.split() else ""
    if executable in PROCESS_PROVIDERS:
        return PROCESS_PROVIDERS[executable]
    lower = command.lower()
    cli_lanes = {"antigravity-cli": "antigravity", "claude-audit": "claude", "codex-audit": "codex", "gemini-cli": "gemini"}
    for command_name, provider in cli_lanes.items():
        if f"code_mower.cli {command_name}" in lower or f"code-mower {command_name}" in lower:
            return provider
    return ""


def _lane_cwd(cwd: str) -> bool:
    return bool(cwd and cwd != "/" and not cwd.startswith("/Applications/") and "/.codex/plugins/" not in cwd)


def collect_lane_processes(command_runner: CommandRunner = _run_command) -> dict[str, Any]:
    text = _stdout(command_runner, ["ps", "-axo", "pid=,command="])
    if not text:
        return {"available": False, "processes": [], "message": "local process inventory unavailable"}
    processes = []
    for line in text.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        provider = "" if pid == os.getpid() else _provider(command)
        cwd = _process_cwd(pid, command_runner) if provider else ""
        if provider and _lane_cwd(cwd):
            process = os.path.basename(command.split()[0]) if command.split() else provider
            processes.append({"pid": pid, "provider": provider, "process": process, "cwd": cwd})
        if len(processes) >= 12:
            break
    return {"available": True, "processes": processes, "message": ""}


def _global_next(report: Mapping[str, Any]) -> str:
    prs = report["remote"].get("pull_requests", [])
    local_active = bool(report["agenttrail"].get("boards")) or bool(report["local_processes"].get("processes"))
    if not report["remote"].get("available") and local_active:
        return "remote unavailable; inspect local lanes"
    for action in ("fix BLOCKED audit", "fix failing check", "rebase/behind", "waiting for audits or owner input", "waiting for checks", "ready for merge or auto-merge"):
        if any(pr.get("next_action") == action for pr in prs):
            return action
    return "inspect PRs" if prs else ("local lanes visible; connect them to PR evidence" if local_active else "no active lanes")


def collect_status(
    *,
    repo: str,
    gh_json_runner: GitHubJsonRunner = _run_gh_json,
    command_runner: CommandRunner = _run_command,
    now: datetime | None = None,
    pr_limit: int = 50,
    workflow_limit: int = 20,
    stale_minutes: int = 30,
) -> dict[str, Any]:
    observed_at = now or _now()
    report = {
        "schema": LANE_STATUS_SCHEMA,
        "repo": repo,
        "generated_at": observed_at.isoformat().replace("+00:00", "Z"),
        "remote": _remote(repo, gh_json_runner, observed_at, pr_limit, workflow_limit, stale_minutes),
        "agenttrail": collect_agenttrail_boards(command_runner),
        "local_processes": collect_lane_processes(command_runner),
    }
    report["next_action"] = _global_next(report)
    return report


def _label_text(labels: Mapping[str, Sequence[str]]) -> str:
    names = [name for key in ("builder", "dispatched", "needs", "done", "blocked") for name in labels.get(key, ())]
    return ", ".join(names) if names else "none"


def _check_text(checks: Sequence[Mapping[str, str]]) -> str:
    return ", ".join(f"{check['name']}={check['state']}" for check in checks) if checks else "none"


def render_text(report: Mapping[str, Any]) -> str:
    lines = [f"Code Mower lanes status for {report['repo']}", f"Generated: {report['generated_at']}", ""]
    remote = report["remote"]
    errors = "; ".join(remote.get("errors") or ["unavailable"])
    lines.append("GitHub: available" if remote.get("available") else f"GitHub: unavailable ({errors})")
    lines.append("")
    prs = remote.get("pull_requests") or []
    if prs:
        lines.append("Open PRs:")
        for pr in prs:
            stale = " stale" if pr.get("stale") else ""
            lines.append(f"- #{pr['number']} {pr['title']} [{pr['merge_state']}{stale}] {pr['branch']} by {pr['author']} updated {pr['updated_at']}")
            lines.append(f"  labels: {_label_text(pr['labels'])}")
            lines.append(f"  checks: {_check_text(pr['checks'])}")
            lines.append(f"  next: {pr['next_action']}")
    else:
        lines.append("Open PRs: none")
    lines.append("")
    runs = remote.get("workflow_runs") or []
    lines.append("Recent Code Mower workflows:" if runs else "Recent Code Mower workflows: none")
    for run in runs[:5]:
        state = run.get("conclusion") or run.get("status") or "unknown"
        lines.append(f"- {run.get('workflow') or 'workflow'} [{state}] {run.get('branch') or ''} updated {run.get('updated_at') or ''}".rstrip())
    lines.append("")
    alerts = (remote.get("gate_health") or {}).get("alerts") or []
    lines.append("Gate alerts:" if alerts else "Gate alerts: none")
    lines.extend(f"- {alert['message']}" for alert in alerts[:5])
    lines.append("")
    boards = report["agenttrail"].get("boards") or []
    message = report["agenttrail"].get("message") or "none visible"
    lines.append("AgentTrail boards:" if boards else f"AgentTrail boards: none ({message})")
    for board in boards:
        cwd = f" cwd={board['cwd']}" if board.get("cwd") else ""
        lines.append(f"- localhost:{board['port']} pid={board['pid']} process={board['process']} confidence={board['confidence']}{cwd}")
    lines.append("")
    processes = report["local_processes"].get("processes") or []
    message = report["local_processes"].get("message") or "none visible"
    lines.append("Local lane processes:" if processes else f"Local lane processes: none ({message})")
    for process in processes[:8]:
        cwd = f" cwd={process['cwd']}" if process.get("cwd") else ""
        lines.append(f"- {process['provider']} pid={process['pid']} process={process['process']}{cwd}")
    lines.extend(["", f"Next: {report['next_action']}"])
    return "\n".join(lines) + "\n"


def main(
    argv: list[str] | None = None,
    *,
    gh_json_runner: GitHubJsonRunner = _run_gh_json,
    command_runner: CommandRunner = _run_command,
) -> int:
    parser = argparse.ArgumentParser(prog="code-mower lanes")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--repo", required=True)
    status.add_argument("--json", action="store_true")
    status.add_argument("--pr-limit", type=int, default=50)
    status.add_argument("--workflow-limit", type=int, default=20)
    status.add_argument("--stale-minutes", type=int, default=30)
    args = parser.parse_args(list(argv or ()))
    if args.command != "status":  # pragma: no cover - argparse validates choices.
        raise AssertionError(f"unhandled lanes command: {args.command}")
    report = collect_status(
        repo=args.repo,
        gh_json_runner=gh_json_runner,
        command_runner=command_runner,
        pr_limit=args.pr_limit,
        workflow_limit=args.workflow_limit,
        stale_minutes=args.stale_minutes,
    )
    output = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.json else render_text(report)
    print(output, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
