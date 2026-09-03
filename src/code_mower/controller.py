#!/usr/bin/env python3
"""Supervised-pilot controller decisions for Code Mower."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from . import config as code_mower_config
from . import lane_status
from .cloud_client import (
    EVENT_SCHEMA,
    CloudBundleError,
    normalize_event,
    utc_now,
    validate_cloud_event,
)
from .providers import build_code_mower_tool_provenance


CONTROLLER_REPORT_SCHEMA = "code_mower.supervisedControllerReport.v1"
CONTROLLER_DECISION_SCHEMA = "code_mower.supervisedControllerDecision.v1"
SUPERVISED_PILOT_SCHEMA = "code_mower.supervisedPilot.v1"
CONTROL_MODES = ("dry_run", "no_merge", "manual", "promoted")
OWNER_LABEL_DEFAULT = "needs-owner"
READY_LABEL_DEFAULT = "tier:R"
DISPATCHED_PREFIX = "dispatched:"
SUCCESS_STATES = {"pass", "passed", "success", "successful", "completed"}
FAILURE_STATES = {"failure", "failed", "error", "timed_out", "cancelled"}
PENDING_STATES = lane_status.PENDING_STATES
ACTION_PRIORITY = {
    "fix BLOCKED audit": 0,
    "fix failing check": 1,
    "rebase/behind": 2,
    "requeue stale audit": 3,
    "rerun stale gate": 4,
    "waiting for audits or owner input": 5,
    "waiting for checks": 6,
    "ready for merge or auto-merge": 7,
    "inspect PR": 8,
}


@dataclass(frozen=True)
class ControllerOptions:
    repo: str
    mode: str = "dry_run"
    gate_required: bool = False
    auto_merge_enabled: bool = False
    merge_token_ready: bool = False
    issue_limit: int = 50


def _text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int | float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _optional_id(value: Any) -> int | None:
    number = _int(value)
    return number if number > 0 else None


def _labels(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else []
    names = []
    for item in raw:
        if isinstance(item, Mapping):
            name = _text(item.get("name"))
        else:
            name = _text(item)
        if name:
            names.append(name)
    return sorted(set(names))


def _owner_surface(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("owner_surface")
    return value if isinstance(value, Mapping) else {}


def _ready_label(config: Mapping[str, Any]) -> str:
    return _text(_owner_surface(config).get("ready_label")) or READY_LABEL_DEFAULT


def _owner_label(config: Mapping[str, Any]) -> str:
    return _text(_owner_surface(config).get("needs_owner_label")) or OWNER_LABEL_DEFAULT


def _wip_cap(config: Mapping[str, Any]) -> int:
    value = _text(_owner_surface(config).get("builder_wip_cap")) or "5"
    return max(_int(value, 5), 1)


def _builder_identity_labels(config: Mapping[str, Any]) -> Mapping[str, str]:
    identity = config.get("builder_identity")
    if not isinstance(identity, Mapping):
        return {}
    labels = identity.get("labels")
    if not isinstance(labels, Mapping):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


def _builder_lane_from_labels(names: Sequence[str], config: Mapping[str, Any]) -> str:
    configured = _builder_identity_labels(config)
    for name in names:
        if name in configured:
            return configured[name]
    for name in names:
        if name.startswith("builder:"):
            return name.removeprefix("builder:")
    return ""


def _trailer_lane(lane_id: str, lane: Mapping[str, Any]) -> str:
    return _text(lane.get("author_lane") or lane.get("trailer_lane") or lane.get("provider") or lane_id)


def _merge_reviewers(config: Mapping[str, Any]) -> list[dict[str, str]]:
    lanes = config.get("lanes")
    if not isinstance(lanes, Mapping):
        return []
    reviewers = []
    for lane_id, raw_lane in lanes.items():
        lane = raw_lane if isinstance(raw_lane, Mapping) else {}
        if not lane.get("merge_authority") or lane.get("informational"):
            continue
        labels = lane.get("labels")
        label_map = labels if isinstance(labels, Mapping) else {}
        reviewers.append(
            {
                "lane_id": str(lane_id),
                "author_lane": _trailer_lane(str(lane_id), lane),
                "done_label": _text(label_map.get("done")),
                "blocked_label": _text(label_map.get("blocked")),
                "needs_label": _text(label_map.get("needs")),
            }
        )
    return [reviewer for reviewer in reviewers if reviewer["done_label"]]


def _author_never_gates(config: Mapping[str, Any]) -> bool:
    return bool(config.get("merge_authority_excludes_author", True))


def _issue_summary(issue: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    names = _labels(issue.get("labels"))
    return {
        "number": _int(issue.get("number")),
        "url": _text(issue.get("url")),
        "author": _text((issue.get("author") or {}).get("login") if isinstance(issue.get("author"), Mapping) else issue.get("author")),
        "updated_at": _text(issue.get("updatedAt") or issue.get("updated_at")),
        "labels": names,
        "builder_lane": _builder_lane_from_labels(names, config),
        "assigned": bool(issue.get("assignees")),
        "dispatched": any(name.startswith(DISPATCHED_PREFIX) for name in names),
        "owner_action": _owner_label(config) in names,
    }


def _collect_ready_issues(
    *,
    repo: str,
    config: Mapping[str, Any],
    gh_json_runner: lane_status.GitHubJsonRunner,
    issue_limit: int,
) -> dict[str, Any]:
    if not repo:
        return {"available": False, "errors": ["repo is required"], "issues": []}
    ready_label = _ready_label(config)
    try:
        raw = gh_json_runner(
            [
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                str(issue_limit),
                "--label",
                ready_label,
                "--json",
                "number,url,author,labels,assignees,updatedAt",
            ]
        )
    except lane_status.LaneStatusUnavailable as exc:
        return {"available": False, "errors": [f"ready_issues: {exc}"], "issues": []}
    issues = raw if isinstance(raw, list) else []
    return {
        "available": True,
        "errors": [],
        "issues": [
            _issue_summary(issue, config)
            for issue in issues
            if isinstance(issue, Mapping)
        ],
    }


def _check_state(check: Mapping[str, Any]) -> str:
    return _text(check.get("state")).lower()


def _gate_state(pr: Mapping[str, Any]) -> str:
    for check in pr.get("checks") or []:
        if isinstance(check, Mapping) and _text(check.get("name")).lower() == "code-mower/gate":
            return _check_state(check)
    return "missing"


def _has_failure(pr: Mapping[str, Any]) -> bool:
    return any(
        _check_state(check) in FAILURE_STATES
        for check in pr.get("checks") or []
        if isinstance(check, Mapping)
    )


def _has_pending(pr: Mapping[str, Any]) -> bool:
    return any(
        _check_state(check) in PENDING_STATES
        for check in pr.get("checks") or []
        if isinstance(check, Mapping)
    )


def _reviewer_outcomes(
    pr: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], bool, bool, str]:
    labels = pr.get("labels") if isinstance(pr.get("labels"), Mapping) else {}
    done = set(labels.get("done") or [])
    blocked = set(labels.get("blocked") or [])
    builder_lane = _builder_lane_from_labels(labels.get("builder") or [], config)
    excluded_author_lane = ""
    outcomes = []
    for reviewer in _merge_reviewers(config):
        excluded = (
            _author_never_gates(config)
            and builder_lane
            and reviewer["author_lane"] == builder_lane
        )
        if excluded:
            excluded_author_lane = reviewer["author_lane"]
            continue
        verdict = "PASS" if reviewer["done_label"] in done else "MISSING"
        if reviewer["blocked_label"] and reviewer["blocked_label"] in blocked:
            verdict = "BLOCKED"
        outcomes.append(
            {
                "lane_id": reviewer["author_lane"],
                "config_lane_id": reviewer["lane_id"],
                "verdict": verdict,
                "promoted": True,
            }
        )
    passed = bool(outcomes) and all(outcome["verdict"] == "PASS" for outcome in outcomes)
    return outcomes, bool(excluded_author_lane), passed, builder_lane


def _pr_priority(pr: Mapping[str, Any]) -> tuple[int, int]:
    return (ACTION_PRIORITY.get(_text(pr.get("next_action")), 99), _int(pr.get("number")))


def _active_lane_counts(prs: Sequence[Mapping[str, Any]], config: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pr in prs:
        labels = pr.get("labels") if isinstance(pr.get("labels"), Mapping) else {}
        lane = _builder_lane_from_labels(labels.get("builder") or [], config)
        if lane:
            counts[lane] = counts.get(lane, 0) + 1
    return counts


def _ready_issue_decision(
    *,
    issues: Sequence[Mapping[str, Any]],
    prs: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> dict[str, Any] | None:
    active_counts = _active_lane_counts(prs, config)
    cap = _wip_cap(config)
    for issue in issues:
        if issue.get("owner_action") or issue.get("assigned") or issue.get("dispatched"):
            continue
        lane = _text(issue.get("builder_lane"))
        if not lane:
            continue
        if active_counts.get(lane, 0) >= cap:
            continue
        return {
            "decision_state": "dispatch_builder",
            "next_action": "dispatch builder lane",
            "next_detail": f"issue #{issue['number']} is ready for {lane}; dry-run controller will not mutate labels",
            "issue_number": issue["number"],
            "issue_url": issue.get("url", ""),
            "lane_id": lane,
            "stop_condition": "",
            "owner_action_kind": "",
            "merge_method": "",
            "would_mutate": False,
        }
    return None


def _pr_decision(
    *,
    pr: Mapping[str, Any],
    config: Mapping[str, Any],
    options: ControllerOptions,
) -> dict[str, Any]:
    labels = pr.get("labels") if isinstance(pr.get("labels"), Mapping) else {}
    needs = set(labels.get("needs") or [])
    owner_label = _owner_label(config)
    configured_reviewers = _merge_reviewers(config)
    gate_state = _gate_state(pr)
    reviewer_outcomes, author_lane_excluded, reviewers_passed, builder_lane = _reviewer_outcomes(pr, config)
    base = {
        "pr_number": pr.get("number"),
        "pr_url": pr.get("url", ""),
        "branch": pr.get("branch", ""),
        "author": pr.get("author", ""),
        "head_sha_prefix": _text(pr.get("head_sha"))[:12],
        "lane_id": builder_lane,
        "gate_status": gate_state,
        "reviewer_outcomes": reviewer_outcomes,
        "author_lane_excluded": author_lane_excluded,
        "promoted_reviewers_passed": reviewers_passed,
        "would_mutate": False,
    }
    if labels.get("blocked"):
        return {
            **base,
            "decision_state": "blocked_audit",
            "next_action": "fix blocked audit",
            "next_detail": _text(pr.get("next_detail")) or f"PR #{pr['number']} has a blocked audit label",
            "stop_condition": "blocked_audit",
            "owner_action_kind": "",
            "merge_method": "",
        }
    if owner_label in needs:
        return {
            **base,
            "decision_state": "owner_action",
            "next_action": "owner action required",
            "next_detail": f"PR #{pr['number']} has {owner_label}",
            "stop_condition": "owner_label",
            "owner_action_kind": "needs_owner_label",
            "merge_method": "",
        }
    if pr.get("stale"):
        return {
            **base,
            "decision_state": "stale_evidence",
            "next_action": _text(pr.get("next_action")) or "requeue stale evidence",
            "next_detail": _text(pr.get("next_detail")),
            "stop_condition": "stale_evidence",
            "owner_action_kind": "",
            "merge_method": "",
        }
    if pr.get("is_draft"):
        return {
            **base,
            "decision_state": "draft_pr",
            "next_action": "finish draft PR",
            "next_detail": f"PR #{pr['number']} is still draft",
            "stop_condition": "draft_pr",
            "owner_action_kind": "",
            "merge_method": "",
        }
    if _has_failure(pr):
        return {
            **base,
            "decision_state": "failing_check",
            "next_action": "fix failing check",
            "next_detail": f"PR #{pr['number']} has a failing check",
            "stop_condition": "failing_check",
            "owner_action_kind": "",
            "merge_method": "",
        }
    if _text(pr.get("merge_state")) in {"BEHIND", "DIRTY"}:
        return {
            **base,
            "decision_state": "not_mergeable",
            "next_action": "rebase/behind",
            "next_detail": f"PR #{pr['number']} merge state is {pr['merge_state']}",
            "stop_condition": "branch_not_mergeable",
            "owner_action_kind": "",
            "merge_method": "",
        }
    if not configured_reviewers:
        return {
            **base,
            "decision_state": "owner_action",
            "next_action": "configure peer reviewer lanes",
            "next_detail": f"PR #{pr['number']} cannot be cleared because no merge-authority reviewer lanes are configured",
            "stop_condition": "reviewer_lanes_missing",
            "owner_action_kind": "reviewer_lanes_missing",
            "merge_method": "",
        }
    if author_lane_excluded and not reviewer_outcomes:
        return {
            **base,
            "decision_state": "owner_action",
            "next_action": "configure peer reviewer lanes",
            "next_detail": (
                f"PR #{pr['number']} cannot be cleared because every configured "
                "merge-authority reviewer lane is excluded as the author lane"
            ),
            "stop_condition": "reviewer_lanes_missing",
            "owner_action_kind": "reviewer_lanes_missing",
            "merge_method": "",
        }
    if needs or _has_pending(pr):
        return {
            **base,
            "decision_state": "waiting_for_evidence",
            "next_action": _text(pr.get("next_action")) or "waiting for audits or checks",
            "next_detail": _text(pr.get("next_detail")),
            "stop_condition": "evidence_pending",
            "owner_action_kind": "",
            "merge_method": "",
        }
    if not reviewers_passed:
        return {
            **base,
            "decision_state": "waiting_for_evidence",
            "next_action": "waiting for peer reviewer pass",
            "next_detail": f"PR #{pr['number']} is missing peer reviewer PASS evidence",
            "stop_condition": "peer_reviewer_missing",
            "owner_action_kind": "",
            "merge_method": "",
        }
    if options.mode == "promoted":
        if not options.gate_required:
            return {
                **base,
                "decision_state": "owner_action",
                "next_action": "require code-mower/gate in branch protection",
                "next_detail": "promoted mode will not merge until branch protection requires code-mower/gate",
                "stop_condition": "required_gate_not_verified",
                "owner_action_kind": "required_gate_missing",
                "merge_method": "",
            }
        if gate_state not in SUCCESS_STATES:
            return {
                **base,
                "decision_state": "owner_action",
                "next_action": "rerun or require code-mower/gate",
                "next_detail": f"code-mower/gate is {gate_state}",
                "stop_condition": "required_gate_not_green",
                "owner_action_kind": "required_gate_not_green",
                "merge_method": "",
            }
        if not options.auto_merge_enabled:
            return {
                **base,
                "decision_state": "owner_action",
                "next_action": "enable repository auto-merge",
                "next_detail": "promoted mode requires repository auto-merge before Code Mower can request it",
                "stop_condition": "auto_merge_disabled",
                "owner_action_kind": "repo_auto_merge",
                "merge_method": "",
            }
        if not options.merge_token_ready:
            return {
                **base,
                "decision_state": "owner_action",
                "next_action": "configure merge-capable credential",
                "next_detail": "promoted mode requires a credential allowed to enable pull request auto-merge",
                "stop_condition": "merge_credential_missing",
                "owner_action_kind": "merge_credential_missing",
                "merge_method": "",
            }
        return {
            **base,
            "decision_state": "ready_to_merge",
            "next_action": "enable pull request auto merge",
            "next_detail": f"PR #{pr['number']} has promoted reviewer and gate evidence",
            "stop_condition": "",
            "owner_action_kind": "",
            "merge_method": "squash",
        }
    return {
        **base,
        "decision_state": "ready_to_merge",
        "next_action": "manual merge decision" if options.mode != "dry_run" else "manual merge dry run",
        "next_detail": f"PR #{pr['number']} is clean with current evidence; controller will not merge in {options.mode} mode",
        "stop_condition": "",
        "owner_action_kind": "",
        "merge_method": "squash",
    }


def _select_pr(prs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return sorted(prs, key=_pr_priority)[0] if prs else None


def _queue_metrics(
    *,
    prs: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    decision: Mapping[str, Any],
) -> dict[str, int]:
    active_lanes = _active_lane_counts(prs, config)
    return {
        "active_lane_count": len(active_lanes),
        "open_pr_count": len(prs),
        "ready_issue_count": len(issues),
        "ready_pr_count": sum(1 for pr in prs if pr.get("next_action") == "ready for merge or auto-merge"),
        "blocked_pr_count": sum(1 for pr in prs if (pr.get("labels") or {}).get("blocked")),
        "owner_action_count": 1 if decision.get("decision_state") == "owner_action" else 0,
        "stale_evidence_count": sum(1 for pr in prs if pr.get("stale")),
    }


def evaluate_controller_report(
    *,
    status_report: Mapping[str, Any],
    ready_issues: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    options: ControllerOptions,
) -> dict[str, Any]:
    remote = status_report.get("remote") if isinstance(status_report.get("remote"), Mapping) else {}
    prs = remote.get("pull_requests") if isinstance(remote.get("pull_requests"), list) else []
    issue_payload = ready_issues if isinstance(ready_issues, Mapping) else {"available": False, "errors": [], "issues": []}
    issues = issue_payload.get("issues") if isinstance(issue_payload.get("issues"), list) else []
    selected_pr = _select_pr(prs)
    if not remote.get("available"):
        decision = {
            "decision_state": "owner_action",
            "next_action": "remote unavailable; fix GitHub access",
            "next_detail": "; ".join(str(error) for error in remote.get("errors") or []),
            "stop_condition": "github_unavailable",
            "owner_action_kind": "github_access",
            "would_mutate": False,
        }
    elif selected_pr is not None:
        decision = _pr_decision(pr=selected_pr, config=config, options=options)
    elif dispatch_decision := _ready_issue_decision(issues=issues, prs=prs, config=config):
        decision = dispatch_decision
    else:
        decision = {
            "decision_state": "no_work",
            "next_action": "no active lanes",
            "next_detail": "no open PRs or ready undispatched issues matched the controller policy",
            "stop_condition": "",
            "owner_action_kind": "",
            "would_mutate": False,
        }
    metrics = _queue_metrics(prs=prs, issues=issues, config=config, decision=decision)
    report = {
        "schema": CONTROLLER_REPORT_SCHEMA,
        "repo": options.repo,
        "generated_at": _text(status_report.get("generated_at")) or utc_now(),
        "mode": options.mode,
        "remote_available": bool(remote.get("available")),
        "ready_issues_available": bool(issue_payload.get("available")),
        "decision": {
            "schema": CONTROLLER_DECISION_SCHEMA,
            **decision,
        },
        "queue": {
            "active_lanes": _active_lane_counts(prs, config),
            "metrics": metrics,
            "ready_issue_errors": list(issue_payload.get("errors") or []),
        },
    }
    return report


def _event_type_for_decision(decision: Mapping[str, Any]) -> str:
    state = _text(decision.get("decision_state"))
    if state == "ready_to_merge":
        return "merge_decision"
    if state == "owner_action":
        return "owner_intervention"
    if state == "no_work":
        return "queue_state_snapshot"
    return "controller_decision"


def _event_status_for_decision(decision: Mapping[str, Any]) -> str:
    state = _text(decision.get("decision_state"))
    if state == "ready_to_merge":
        return "ready"
    if state == "owner_action":
        return "owner_action"
    if state in {"blocked_audit", "stale_evidence", "failing_check", "not_mergeable"}:
        return "blocked"
    if state == "no_work":
        return "observed"
    return "observed"


def build_controller_event(
    *,
    report: Mapping[str, Any],
    team_id: str = "",
    install_id: str = "",
    source: str = "code-mower-controller",
) -> dict[str, Any]:
    decision = report.get("decision") if isinstance(report.get("decision"), Mapping) else {}
    queue = report.get("queue") if isinstance(report.get("queue"), Mapping) else {}
    metrics = queue.get("metrics") if isinstance(queue.get("metrics"), Mapping) else {}
    event_type = _event_type_for_decision(decision)
    dimensions: dict[str, Any] = {
        "supervised_pilot_schema": SUPERVISED_PILOT_SCHEMA,
        "controller_mode": _text(report.get("mode")) or "dry_run",
        "decision_state": _text(decision.get("decision_state")),
        "next_action": _text(decision.get("next_action")),
        "next_detail": _text(decision.get("next_detail"))[:180],
        "repo_slug": _text(report.get("repo")),
        "remote_available": bool(report.get("remote_available")),
        "ready_issues_available": bool(report.get("ready_issues_available")),
        "stop_condition": _text(decision.get("stop_condition")),
        "owner_action_kind": _text(decision.get("owner_action_kind")),
        "lane_id": _text(decision.get("lane_id")),
        "pr_number": _optional_id(decision.get("pr_number")),
        "issue_number": _optional_id(decision.get("issue_number")),
        "branch": _text(decision.get("branch")),
        "author_login": _text(decision.get("author")),
        "head_sha_prefix": _text(decision.get("head_sha_prefix"))[:12],
        "gate_status": _text(decision.get("gate_status")),
        "reviewer_outcomes": decision.get("reviewer_outcomes") or [],
        "author_lane_excluded": bool(decision.get("author_lane_excluded")),
        "promoted_reviewers_passed": bool(decision.get("promoted_reviewers_passed")),
        "merge_method": _text(decision.get("merge_method")),
        "would_mutate": bool(decision.get("would_mutate")),
    }
    dimensions = {
        key: value
        for key, value in dimensions.items()
        if value is not None and value != "" and value != []
    }
    event = {
        "schema": EVENT_SCHEMA,
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "created_at": _text(report.get("generated_at")) or utc_now(),
        "repo_slug": _text(report.get("repo")),
        "team_id": team_id,
        "install_id": install_id,
        "source": source,
        "provider": "code-mower",
        "lens": "supervised-pilot" if event_type != "merge_decision" else "merge-policy",
        "status": _event_status_for_decision(decision),
        "tool": build_code_mower_tool_provenance(
            source=source,
            version=__version__,
            role="controller",
        ),
        "metrics": dict(metrics),
        "dimensions": dimensions,
    }
    return validate_cloud_event(normalize_event(event, event_type))


def collect_controller_report(
    *,
    config_path: Path,
    options: ControllerOptions,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> dict[str, Any]:
    config = code_mower_config.load_config(config_path)
    issues = code_mower_config.validate_config(config)
    if issues:
        issue_text = "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        raise code_mower_config.ConfigError(f"invalid Code Mower config: {issue_text}")
    status_report = lane_status.collect_status(
        repo=options.repo,
        gh_json_runner=gh_json_runner,
        command_runner=command_runner,
    )
    ready_issues = _collect_ready_issues(
        repo=options.repo,
        config=config,
        gh_json_runner=gh_json_runner,
        issue_limit=options.issue_limit,
    )
    return evaluate_controller_report(
        status_report=status_report,
        ready_issues=ready_issues,
        config=config,
        options=options,
    )


def render_text(report: Mapping[str, Any]) -> str:
    decision = report.get("decision") if isinstance(report.get("decision"), Mapping) else {}
    queue = report.get("queue") if isinstance(report.get("queue"), Mapping) else {}
    metrics = queue.get("metrics") if isinstance(queue.get("metrics"), Mapping) else {}
    lines = [
        f"Code Mower controller for {report.get('repo')}",
        f"Generated: {report.get('generated_at')}",
        f"Mode: {report.get('mode')}",
        "",
        f"Decision: {decision.get('decision_state')}",
        f"Next: {decision.get('next_action')}",
    ]
    if decision.get("next_detail"):
        lines.append(f"Detail: {decision.get('next_detail')}")
    if decision.get("pr_number"):
        lines.append(f"PR: #{decision.get('pr_number')} {decision.get('branch') or ''}".rstrip())
    if decision.get("issue_number"):
        lines.append(f"Issue: #{decision.get('issue_number')}")
    if decision.get("lane_id"):
        lines.append(f"Lane: {decision.get('lane_id')}")
    if decision.get("stop_condition"):
        lines.append(f"Stop: {decision.get('stop_condition')}")
    if decision.get("owner_action_kind"):
        lines.append(f"Owner action: {decision.get('owner_action_kind')}")
    lines.extend(
        [
            "",
            "Queue:",
            f"- open PRs: {metrics.get('open_pr_count', 0)}",
            f"- ready issues: {metrics.get('ready_issue_count', 0)}",
            f"- active lanes: {metrics.get('active_lane_count', 0)}",
            f"- blocked PRs: {metrics.get('blocked_pr_count', 0)}",
            f"- stale evidence: {metrics.get('stale_evidence_count', 0)}",
            "",
            "Mutation: none unless a future explicit apply command uses this decision.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_event(path: Path, event: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(
    argv: list[str] | None = None,
    *,
    gh_json_runner: lane_status.GitHubJsonRunner = lane_status.run_gh_json,
    command_runner: lane_status.CommandRunner = lane_status.run_command,
) -> int:
    parser = argparse.ArgumentParser(prog="code-mower controller")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--repo", required=True)
    run.add_argument("--config", type=Path, default=Path("code-mower.yml"))
    run.add_argument("--mode", choices=CONTROL_MODES, default="dry_run")
    run.add_argument("--gate-required", action="store_true")
    run.add_argument("--auto-merge-enabled", action="store_true")
    run.add_argument("--merge-token-ready", action="store_true")
    run.add_argument("--issue-limit", type=int, default=50)
    run.add_argument("--event-file", type=Path, default=None)
    run.add_argument("--team-id", default="")
    run.add_argument("--install-id", default="")
    run.add_argument("--source", default="code-mower-controller")
    run.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv or ()))
    if args.command != "run":  # pragma: no cover - argparse validates commands.
        raise AssertionError(f"unhandled controller command: {args.command}")
    try:
        options = ControllerOptions(
            repo=args.repo,
            mode=args.mode,
            gate_required=args.gate_required,
            auto_merge_enabled=args.auto_merge_enabled,
            merge_token_ready=args.merge_token_ready,
            issue_limit=args.issue_limit,
        )
        report = collect_controller_report(
            config_path=args.config,
            options=options,
            gh_json_runner=gh_json_runner,
            command_runner=command_runner,
        )
        event = build_controller_event(
            report=report,
            team_id=args.team_id,
            install_id=args.install_id,
            source=args.source,
        )
        if args.event_file is not None:
            _write_event(args.event_file, event)
        if args.json:
            print(json.dumps({**report, "event": event}, indent=2, sort_keys=True))
        else:
            print(render_text(report), end="")
            if args.event_file is not None:
                print(f"Event: {args.event_file}")
        return 0
    except (
        CloudBundleError,
        code_mower_config.ConfigError,
        lane_status.LaneStatusUnavailable,
        OSError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
