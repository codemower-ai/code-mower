"""Code Mower audit gate-health checks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .audit_labeler_lib import GitHubToken, github_actions_comment_attested

MARKER = "CODE_MOWER_GATE_HEALTH_ALERT"
NON_TERMINAL_CHECK_STATUSES = {"queued", "requested", "waiting", "pending", "in_progress"}
LOCAL_AUDIT_FAILURE_CONCLUSIONS = {"action_required", "cancelled", "failure", "timed_out"}
COMMENT_ALERT_LIMIT = 20
LOCAL_AUDIT_WORKFLOW_PATH_SUFFIXES = (
    "/local-cli-audit.yml",
    "local-cli-audit.yml",
)
LOCAL_AUDIT_WORKFLOW_NAMES = {
    "Code Mower Local CLI Audits",
}


@dataclass(frozen=True)
class Alert:
    key: str
    title: str
    body: str
    pr_number: int | None = None
    gate_stalled: bool = False


def parse_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def label_names(pr: dict[str, Any]) -> set[str]:
    return {str(item.get("name") or "") for item in pr.get("labels") or []}


def latest_label_time(timeline: Sequence[dict[str, Any]], label: str) -> datetime | None:
    seen: datetime | None = None
    for item in timeline:
        label_name = str((item.get("label") or {}).get("name") or "")
        if item.get("event") == "labeled" and label_name == label:
            created = parse_time(str(item.get("created_at") or ""))
            seen = created if seen is None or created > seen else seen
    return seen


def has_head_bound_terminal(
    *,
    repo: str,
    number: int,
    comments: Sequence[dict[str, Any]],
    lane: dict[str, Any],
    head_sha: str,
    tokens: Sequence[GitHubToken] = (),
) -> bool:
    head_marker = f"Head SHA: `{head_sha}`"
    labels = (str(lane.get("done") or ""), str(lane.get("blocked") or ""))
    suffixes = {": " + label + " -->" for label in labels if label}
    for comment in reversed(comments):
        author = str(((comment.get("user") or {}).get("login")) or "")
        body = str(comment.get("body") or "")
        if not trusted_comment_author(
            repo=repo,
            lane=lane,
            author=author,
            body=body,
            comment_id=comment.get("id"),
            number=number,
            head_sha=head_sha,
            tokens=tokens,
        ):
            continue
        if head_marker in body and any(suffix in body for suffix in suffixes):
            return True
    return False


def trusted_authors(lane: dict[str, Any]) -> set[str]:
    raw = str(lane.get("bot_authors") or "")
    env_raw = os.environ.get(str(lane.get("authors_env") or ""), "")
    merged = raw + "," + env_raw
    return {item.strip().lower() for item in merged.split(",") if item.strip()}


def trusted_github_actions_workflows(lane: dict[str, Any]) -> set[str]:
    raw = str(lane.get("github_actions_workflows") or "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def trusted_comment_author(
    *,
    repo: str,
    lane: dict[str, Any],
    author: str,
    body: str,
    comment_id: object,
    number: int,
    head_sha: str,
    tokens: Sequence[GitHubToken],
) -> bool:
    authors = trusted_authors(lane)
    if not authors:
        return False
    author_login = author.strip().lower()
    if author_login not in authors:
        return False
    if author_login != "github-actions[bot]":
        return True
    return github_actions_comment_attested(
        repo=repo,
        body=body,
        comment_id=comment_id,
        issue_number=int(number),
        head_sha=head_sha,
        workflow_paths=trusted_github_actions_workflows(lane),
        tokens=tokens,
    )


def _check_run_sort_key(run: dict[str, Any]) -> tuple[str, int, str, str]:
    created = str(
        run.get("created_at")
        or run.get("started_at")
        or run.get("completed_at")
        or ""
    )
    try:
        numeric_id = int(run.get("id") or run.get("run_id") or 0)
    except (TypeError, ValueError):
        numeric_id = 0
    return (
        created,
        numeric_id,
        str(run.get("started_at") or ""),
        str(run.get("completed_at") or ""),
    )


def _check_run_non_terminal(run: dict[str, Any]) -> bool:
    return run.get("status") in NON_TERMINAL_CHECK_STATUSES or (
        run.get("conclusion") is None and run.get("status") != "completed"
    )


def _check_run_workflow_path(run: dict[str, Any]) -> str:
    workflow = run.get("workflow")
    return str(
        run.get("workflow_path")
        or run.get("workflowPath")
        or (workflow.get("path") if isinstance(workflow, dict) else "")
        or ""
    )


def _check_run_workflow_name(run: dict[str, Any]) -> str:
    workflow = run.get("workflow")
    return str(
        run.get("workflow_name")
        or run.get("workflowName")
        or (workflow.get("name") if isinstance(workflow, dict) else "")
        or ""
    )


def is_local_audit_check_run(run: dict[str, Any]) -> bool:
    if str(run.get("name") or "") != "audit":
        return False
    workflow_path = _check_run_workflow_path(run)
    if any(workflow_path.endswith(suffix) for suffix in LOCAL_AUDIT_WORKFLOW_PATH_SUFFIXES):
        return True
    return _check_run_workflow_name(run) in LOCAL_AUDIT_WORKFLOW_NAMES


def latest_local_audit_failure(check_runs: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    audits = [run for run in check_runs if is_local_audit_check_run(run)]
    if not audits:
        return None
    audits.sort(key=_check_run_sort_key, reverse=True)
    latest = audits[0]
    if _check_run_non_terminal(latest):
        return None
    return latest if latest.get("conclusion") in LOCAL_AUDIT_FAILURE_CONCLUSIONS else None


def recent_alert_keys(
    comments: Sequence[dict[str, Any]],
    now: datetime,
    hours: int,
) -> set[str]:
    cutoff = now - timedelta(hours=hours)
    keys: set[str] = set()
    prefix = f"<!-- {MARKER} key="
    for comment in comments:
        body = str(comment.get("body") or "")
        created_raw = str(comment.get("created_at") or comment.get("createdAt") or "")
        if prefix not in body or parse_time(created_raw) < cutoff:
            continue
        pos = 0
        while True:
            start = body.find(prefix, pos)
            if start == -1:
                break
            start += len(prefix)
            end = body.find("-->", start)
            if end != -1:
                keys.add(body[start:end].strip())
            pos = start + 1
    return keys


def runner_alert(runners: Sequence[dict[str, Any]], runner_label: str) -> Alert | None:
    if not runner_label:
        return None
    relevant = [
        runner
        for runner in runners
        if runner_label
        in {str(label.get("name") or "") for label in runner.get("labels") or []}
    ]
    offline = [runner for runner in relevant if runner.get("status") != "online"]
    if not relevant:
        return Alert(
            "runner-missing",
            "Code Mower audit runner missing",
            f"No self-hosted runner has label `{runner_label}`.",
        )
    if offline and not any(runner.get("status") == "online" for runner in relevant):
        names = ", ".join(str(runner.get("name") or runner.get("id")) for runner in offline)
        return Alert(
            "runner-offline",
            "Code Mower audit runner offline",
            f"No `{runner_label}` runner is online. Offline: {names}.",
        )
    return None


def evaluate(
    *,
    now: datetime,
    lanes: Sequence[dict[str, Any]],
    prs: Sequence[dict[str, Any]],
    timelines: dict[int, Sequence[dict[str, Any]]],
    comments: dict[int, Sequence[dict[str, Any]]],
    check_runs: dict[int | str, Sequence[dict[str, Any]]],
    head_times: dict[str, datetime],
    runners: Sequence[dict[str, Any]],
    status_comments: Sequence[dict[str, Any]],
    stale_minutes: int,
    dedupe_hours: int,
    runner_label: str,
    repo: str = "",
    tokens: Sequence[GitHubToken] = (),
) -> list[Alert]:
    recent = recent_alert_keys(status_comments, now, dedupe_hours)
    alerts = [
        alert
        for alert in [runner_alert(runners, runner_label)]
        if alert and alert.key not in recent
    ]
    cutoff = now - timedelta(minutes=stale_minutes)
    lanes_by_needs = {
        str(lane.get("needs") or ""): lane
        for lane in lanes
        if str(lane.get("needs") or "")
    }
    for pr in prs:
        number = int(pr["number"])
        head_sha = str(pr.get("headRefOid") or pr.get("head_sha") or "")
        labels = label_names(pr)
        for needs_label, lane in lanes_by_needs.items():
            if needs_label not in labels:
                continue
            pr_timeline = timelines.get(number)
            pr_comments = comments.get(number)
            if pr_timeline is None or pr_comments is None:
                continue
            labeled_at = latest_label_time(pr_timeline, needs_label)
            active_since = max(
                (item for item in (labeled_at, head_times.get(head_sha)) if item),
                default=None,
            )
            has_terminal = has_head_bound_terminal(
                repo=repo,
                number=number,
                comments=pr_comments,
                lane=lane,
                head_sha=head_sha,
                tokens=tokens,
            )
            if active_since and active_since <= cutoff and not has_terminal:
                lane_id = str(lane.get("id") or needs_label)
                key = f"pr-{number}-{lane_id}-{head_sha[:12]}-stale"
                if key not in recent:
                    alerts.append(
                        Alert(
                            key,
                            f"PR #{number} audit stalled",
                            (
                                f"`{needs_label}` has been active for this head since "
                                f"{active_since.isoformat()} with no head-bound "
                                f"{lane_id} verdict for `{head_sha[:12]}`."
                            ),
                            number,
                            True,
                        )
                    )
        failed = latest_local_audit_failure(
            check_runs.get(number, check_runs.get(head_sha, ()))
        )
        if failed:
            key = f"pr-{number}-{head_sha[:12]}-local-audit-failed"
            if key not in recent:
                alerts.append(
                    Alert(
                        key,
                        f"PR #{number} local audit failed",
                        (
                            "Latest Code Mower Local CLI `audit` check concluded "
                            f"`{failed.get('conclusion')}` for `{head_sha[:12]}`."
                        ),
                        number,
                    )
                )
    return alerts


def gh_json(args: Sequence[str], env: dict[str, str] | None = None) -> Any:
    return json.loads(subprocess.check_output(["gh", *args], env=env, text=True))


def gh_api_list(
    repo: str,
    path: str,
    key: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    pages = gh_json(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repo}/{path}",
            "-H",
            "Accept: application/vnd.github+json",
        ],
        env=env,
    )
    items: list[dict[str, Any]] = []
    for page in pages:
        raw_items = page.get(key, []) if key else page
        items.extend(raw_items if isinstance(raw_items, list) else [])
    return items


def _actions_run_id(check_run: dict[str, Any]) -> str:
    for key in ("details_url", "html_url"):
        value = str(check_run.get(key) or "")
        marker = "/actions/runs/"
        if marker not in value:
            continue
        suffix = value.split(marker, 1)[1]
        run_id = suffix.split("/", 1)[0]
        if run_id.isdigit():
            return run_id
    return ""


def enrich_check_runs_with_workflows(
    repo: str,
    check_runs: Sequence[dict[str, Any]],
    failures: list[str],
) -> list[dict[str, Any]]:
    workflow_runs: dict[str, dict[str, Any]] = {}
    enriched: list[dict[str, Any]] = []
    for check_run in check_runs:
        item = dict(check_run)
        run_id = _actions_run_id(item)
        if run_id:
            try:
                if run_id not in workflow_runs:
                    workflow_runs[run_id] = gh_json(
                        [
                            "api",
                            f"repos/{repo}/actions/runs/{run_id}",
                            "-H",
                            "Accept: application/vnd.github+json",
                        ]
                    )
            except (subprocess.CalledProcessError, ValueError) as exc:
                failures.append(f"workflow-run:{run_id}")
                print(f"warning: failed to fetch workflow run {run_id}: {exc}", flush=True)
            workflow_run = workflow_runs.get(run_id) or {}
            item.setdefault("workflow_path", str(workflow_run.get("path") or ""))
            item.setdefault("workflow_name", str(workflow_run.get("name") or ""))
            item.setdefault("run_id", run_id)
        enriched.append(item)
    return enriched


def fetch_per_pr(
    repo: str,
    prs: Sequence[dict[str, Any]],
    kind: str,
    failures: list[str],
) -> dict[Any, list[dict[str, Any]]]:
    out: dict[Any, list[dict[str, Any]]] = {}
    for pr in prs:
        number = int(pr["number"])
        sha = str(pr["headRefOid"])
        try:
            path, key, out_key = {
                "timeline": (f"issues/{number}/timeline?per_page=100", None, number),
                "comments": (f"issues/{number}/comments?per_page=100", None, number),
                "checks": (f"commits/{sha}/check-runs?per_page=100", "check_runs", number),
            }[kind]
            items = gh_api_list(repo, path, key)
            out[out_key] = (
                enrich_check_runs_with_workflows(repo, items, failures)
                if kind == "checks"
                else items
            )
        except (subprocess.CalledProcessError, ValueError) as exc:
            failures.append(f"{kind}:pr-{number}")
            print(f"warning: failed to fetch {kind} for PR #{number}: {exc}", flush=True)
    return out


def fetch_head_times(
    repo: str,
    prs: Sequence[dict[str, Any]],
    failures: list[str],
) -> dict[str, datetime]:
    out: dict[str, datetime] = {}
    for pr in prs:
        sha = str(pr["headRefOid"])
        try:
            data = gh_json(
                ["api", f"repos/{repo}/commits/{sha}", "-H", "Accept: application/vnd.github+json"]
            )
            out[sha] = parse_time(str(data["commit"]["committer"]["date"]))
        except (KeyError, subprocess.CalledProcessError, ValueError) as exc:
            failures.append(f"head-time:{sha[:12]}")
            print(f"warning: failed to fetch head time for {sha[:12]}: {exc}", flush=True)
    return out


def run_gh(args: Sequence[str], payload: str | None = None, quiet: bool = False) -> bool:
    result = subprocess.run(["gh", *args], input=payload, text=True, capture_output=True)
    if result.returncode and not quiet:
        print((result.stderr or result.stdout or f"gh {' '.join(args)} failed").strip())
    return result.returncode == 0


def post_comment(repo: str, issue: int, body: str) -> bool:
    return run_gh(
        ["api", "-X", "POST", f"repos/{repo}/issues/{issue}/comments", "--input", "-"],
        json.dumps({"body": body}),
    )


def add_gate_stalled(repo: str, pr_number: int) -> bool:
    if not run_gh(["api", f"repos/{repo}/labels/gate-stalled"], quiet=True) and not run_gh(
        [
            "label",
            "create",
            "gate-stalled",
            "--repo",
            repo,
            "--color",
            "BFD4F2",
            "--description",
            "Code Mower gate-health alarm detected a stale audit gate",
        ],
        quiet=True,
    ):
        print("warning: failed to create or verify gate-stalled label", flush=True)
        return False
    return run_gh(
        ["api", "-X", "POST", f"repos/{repo}/issues/{pr_number}/labels", "--input", "-"],
        json.dumps({"labels": ["gate-stalled"]}),
    )


def alert_chunks(alerts: Sequence[Alert], limit: int = COMMENT_ALERT_LIMIT) -> list[Sequence[Alert]]:
    return [alerts[start : start + limit] for start in range(0, len(alerts), limit)]


def format_alert_comment_chunk(
    chunk: Sequence[Alert],
    total: int,
    start: int,
    owner_login: str,
) -> str:
    end = start + len(chunk)
    mention = f"@{owner_login} " if owner_login and owner_login != "TODO_OWNER_LOGIN" else ""
    lines = [
        f"{mention}Code Mower gate-health alert",
        "",
        f"{total} alert(s) detected; showing {start + 1}-{end}:",
    ]
    lines.extend(f"- {alert.title}: {alert.body}" for alert in chunk)
    lines.append("")
    lines.extend(f"<!-- {MARKER} key={alert.key} -->" for alert in chunk)
    return "\n".join(lines)


def format_alert_comments(
    alerts: Sequence[Alert],
    *,
    owner_login: str = "",
    limit: int = COMMENT_ALERT_LIMIT,
) -> list[str]:
    bodies: list[str] = []
    for start, chunk in enumerate(alert_chunks(alerts, limit)):
        bodies.append(
            format_alert_comment_chunk(chunk, len(alerts), start * limit, owner_login)
        )
    return bodies


def _load_lanes(raw: str) -> list[dict[str, Any]]:
    lanes = json.loads(raw)
    if not isinstance(lanes, list):
        raise ValueError("lanes must be a JSON array")
    return [lane for lane in lanes if isinstance(lane, dict)]


def _status_issue_number(raw: str) -> int | None:
    value = raw.strip()
    if not value or value == "TODO_STATUS_ISSUE":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("--status-issue must be a numeric issue number") from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower gate-health")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--status-issue", default=os.environ.get("STATUS_ISSUE", ""))
    parser.add_argument(
        "--lanes-json",
        default=os.environ.get("CODE_MOWER_GATE_HEALTH_LANES_JSON", "[]"),
    )
    parser.add_argument(
        "--stale-minutes",
        type=int,
        default=int(os.environ.get("CODE_MOWER_GATE_HEALTH_MAX_WAIT_MINUTES", "30")),
    )
    parser.add_argument(
        "--dedupe-hours",
        type=int,
        default=int(os.environ.get("CODE_MOWER_GATE_HEALTH_DEDUPE_HOURS", "6")),
    )
    parser.add_argument(
        "--runner-label",
        default=os.environ.get("CODE_MOWER_LOCAL_AUDIT_RUNNER_LABEL", ""),
    )
    parser.add_argument("--runner-token-env", default="CODE_MOWER_GATE_HEALTH_RUNNER_TOKEN")
    parser.add_argument(
        "--runner-check",
        choices=("auto", "disabled", "required"),
        default="auto",
    )
    parser.add_argument("--owner-login", default=os.environ.get("OWNER_LOGIN", ""))
    parser.add_argument("--alert-runner-api-unavailable", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(argv or ()))

    if not args.repo:
        print("error: --repo is required")
        return 1
    try:
        status_issue = _status_issue_number(args.status_issue)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    now = datetime.now(timezone.utc)
    fetch_failures: list[str] = []
    lanes = _load_lanes(args.lanes_json)
    needs_labels = {str(lane.get("needs") or "") for lane in lanes if str(lane.get("needs") or "")}
    prs = gh_json(
        [
            "pr",
            "list",
            "--repo",
            args.repo,
            "--state",
            "open",
            "--limit",
            "500",
            "--json",
            "number,title,headRefOid,labels",
        ]
    )
    gate_prs = [pr for pr in prs if label_names(pr) & needs_labels]
    timelines = fetch_per_pr(args.repo, gate_prs, "timeline", fetch_failures)
    comments = fetch_per_pr(args.repo, gate_prs, "comments", fetch_failures)
    eval_prs = [
        pr
        for pr in gate_prs
        if int(pr["number"]) in timelines and int(pr["number"]) in comments
    ]
    check_runs = fetch_per_pr(args.repo, prs, "checks", fetch_failures)
    head_times = fetch_head_times(args.repo, eval_prs, fetch_failures)
    dedupe_since = (
        (now - timedelta(hours=args.dedupe_hours))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    status_comments = (
        gh_api_list(
            args.repo,
            f"issues/{status_issue}/comments?per_page=100&since={dedupe_since}",
        )
        if status_issue is not None
        else []
    )
    recent = recent_alert_keys(status_comments, now, args.dedupe_hours)
    runner_token = os.environ.get(args.runner_token_env, "")
    runner_check = "disabled" if args.runner_check == "auto" and not runner_token else args.runner_check
    runner_state = runner_check
    runner_inventory_available = runner_check != "disabled"
    extra_alerts: list[Alert] = []
    if (
        runner_check == "disabled"
        and args.alert_runner_api_unavailable
        and "runner-check-disabled" not in recent
    ):
        extra_alerts.append(
            Alert(
                "runner-check-disabled",
                "Code Mower audit runner check disabled",
                f"`{args.runner_token_env}` is not configured, so self-hosted runner health was not checked.",
            )
        )
    try:
        runner_env = {**os.environ, "GH_TOKEN": runner_token} if runner_token else None
        runners = (
            gh_api_list(args.repo, "actions/runners?per_page=100", "runners", env=runner_env)
            if runner_check != "disabled"
            else []
        )
    except subprocess.CalledProcessError as exc:
        if runner_check == "required":
            print(f"error: required runner check failed: {exc}", flush=True)
            print(
                json.dumps(
                    {
                        "ok": False,
                        "alerts": [],
                        "failed_alerts": [],
                        "fetch_failures": ["runners"],
                        "runner_check": "unavailable",
                    },
                    sort_keys=True,
                )
            )
            return 1
        runner_state = "unavailable"
        runner_inventory_available = False
        fetch_failures.append("runners")
        runners = []
        if args.alert_runner_api_unavailable and "runner-api-unavailable" not in recent:
            extra_alerts.append(
                Alert(
                    "runner-api-unavailable",
                    "Code Mower audit runner status unavailable",
                    f"Could not read self-hosted runners for `{args.runner_label}`: {exc}.",
                )
            )
    alerts = extra_alerts + evaluate(
        now=now,
        lanes=lanes,
        prs=prs,
        timelines=timelines,
        comments=comments,
        check_runs=check_runs,
        head_times=head_times,
        runners=runners,
        status_comments=status_comments,
        stale_minutes=args.stale_minutes,
        dedupe_hours=args.dedupe_hours,
        runner_label=args.runner_label if runner_inventory_available else "",
        repo=args.repo,
        tokens=(GitHubToken("GH_TOKEN", os.environ.get("GH_TOKEN", "")),),
    )
    failures: list[str] = []
    announced_alerts = list(alerts) if args.dry_run else []
    if alerts and not args.dry_run:
        if status_issue is None:
            print(
                "warning: --status-issue is not configured; skipped gate-health status comments",
                flush=True,
            )
            announced_alerts.extend(alerts)
        else:
            for start, chunk in enumerate(alert_chunks(alerts)):
                body = format_alert_comment_chunk(
                    chunk,
                    len(alerts),
                    start * COMMENT_ALERT_LIMIT,
                    args.owner_login,
                )
                if not post_comment(args.repo, status_issue, body):
                    failures.extend(f"status-comment:{alert.key}" for alert in chunk)
                    continue
                announced_alerts.extend(chunk)
    for alert in alerts:
        if args.dry_run or alert not in announced_alerts:
            continue
        if alert.gate_stalled and alert.pr_number is not None and not add_gate_stalled(
            args.repo,
            alert.pr_number,
        ):
            failures.append(f"{alert.key}:gate-stalled")
    print(
        json.dumps(
            {
                "ok": not failures and not fetch_failures,
                "alerts": [alert.key for alert in alerts],
                "failed_alerts": failures,
                "fetch_failures": fetch_failures,
                "runner_check": runner_state,
            },
            sort_keys=True,
        )
    )
    return 1 if failures or fetch_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
