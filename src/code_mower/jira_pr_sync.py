#!/usr/bin/env python3
"""Synchronize one Jira work item with one GitHub PR and gate state (issue #802).

One configured Jira issue is linked to one GitHub pull request through the
guarded mutation plan/apply surface in ``jira_mutations`` (issue #799). This
module adds no new write primitive: every Jira effect is a
``MutationRequest`` built here and executed there, so the repository write
guard, the runtime ``--apply`` guard, the closed transition-id mapping, the
templated-comment table, and the replay protection all stay exactly as #799
defined them. GitHub remains the sole PR, check, review, and merge-gate
authority: this module never reads gate state and never changes it, and a
Jira refusal, conflict, rate limit, or outage only ever lands in this sync
report, never in a gate decision.

Authoritative identity comes from exactly two bounded sources: the PR branch
name and the leading token of the PR title. Free-form issue bodies,
descriptions, comments, source, diffs, and transcripts are never searched.
Zero markers means missing identity, two different markers means ambiguous
identity, and a marker whose project prefix disagrees with the configured
``project_key`` (or with an explicitly passed ``--issue``) means mismatched
identity. All three fail closed to an owner action with zero Jira calls.

Every milestone verifies the single PR remote link (Jira upserts on the
deterministic ``globalId``, so replays update instead of duplicating).
Bounded templated comments go out only on meaningful state transitions
(opened, blocked, merged). Lifecycle moves use only explicitly configured
``mutations.transitions`` ids, and a milestone whose category has no
configured id simply requests no transition rather than refusing the whole
plan. Transitions are never inferred from display names.

Replay safety is inherited: link upserts, comment per-intent claims, and
transition already-at-target reconciliation make duplicate webhooks, polls,
retries, and restarts converge to ``already_applied``. ``reconcile`` folds
duplicate missed events before replaying, so recovery after an outage is
idempotent too.

Every report field is bounded metadata: issue key, milestone, PR
owner/repo/number/URL, transition category, template id, closed reason
codes, and counts. No source, raw diff, transcript, issue body, description
or comment text, raw stdout/stderr, auth output, secret, email, branch
string, title string, or local path is ever returned, printed, or retained.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

from . import jira_mutations


SYNC_REPORT_SCHEMA = "code_mower.jiraPrSync.v1"

#: GitHub/Code Mower lifecycle milestones this sync understands. Each is a
#: closed event name supplied by the caller (webhook, poll, or manual
#: replay), never inferred from prose.
PR_MILESTONES = (
    "opened",
    "updated",
    "blocked",
    "green",
    "merged",
    "closed_unmerged",
)

#: Milestone policy: which lifecycle category to move toward and which
#: closed comment template to post. A transition category configured with
#: no transition id is skipped, never refused: the link (and any comment)
#: still applies. Milestones without a comment stay silent no matter how
#: often they replay.
_MILESTONE_POLICY: Mapping[str, Mapping[str, str]] = {
    "opened": {"transition_category": "in_progress", "comment_template": "pr_opened"},
    "updated": {"transition_category": "", "comment_template": ""},
    "blocked": {"transition_category": "blocked", "comment_template": "pr_blocked"},
    "green": {"transition_category": "", "comment_template": ""},
    "merged": {"transition_category": "done", "comment_template": "pr_merged"},
    "closed_unmerged": {"transition_category": "", "comment_template": ""},
}

_KEY_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]*-[0-9]+(?![A-Za-z0-9_])")
_TITLE_LEADING_RE = re.compile(r"^([A-Z][A-Z0-9_]*-[0-9]+)\b")
_MAX_KEY_LENGTH = 64
_MAX_INPUT_LENGTH = 256

#: Closed reason vocabulary for identity resolution and sync reports.
SYNC_REASONS = frozenset(
    {
        "ok",
        "missing_jira_identity",
        "ambiguous_jira_identity",
        "jira_identity_mismatch",
        "invalid_pr_url",
        "invalid_milestone",
        "invalid_request",
    }
)


def _bounded_input(value: Any) -> str:
    text = str(value or "")
    return text if len(text) <= _MAX_INPUT_LENGTH else ""


def _markers_in_branch(branch: str) -> tuple[str, ...]:
    """Return every distinct well-formed key token found in a branch name."""
    text = _bounded_input(branch)
    if not text:
        return ()
    found: list[str] = []
    for match in _KEY_RE.finditer(text):
        key = match.group(0)
        if len(key) <= _MAX_KEY_LENGTH and key not in found:
            found.append(key)
    return tuple(found)


def _marker_in_title(pr_title: str) -> str:
    """Return the leading key token of a PR title, or "".

    Only the leading token counts: the title is otherwise free-form prose
    and is never searched.
    """
    text = _bounded_input(pr_title).strip()
    if not text:
        return ""
    match = _TITLE_LEADING_RE.match(text)
    if match is None or len(match.group(1)) > _MAX_KEY_LENGTH:
        return ""
    return match.group(1)


def parse_jira_marker(*, branch: str = "", pr_title: str = "") -> dict[str, str]:
    """Resolve the one authoritative Jira key from bounded PR metadata.

    Returns ``{"status": "ok", "issue_key": key}`` when exactly one
    distinct marker is present, ``{"status": "missing", ...}`` when neither
    source carries one, and ``{"status": "ambiguous", ...}`` when the
    sources disagree or either source carries more than one distinct key.
    Carries no input text back to the caller.
    """
    branch_keys = _markers_in_branch(branch)
    title_key = _marker_in_title(pr_title)
    distinct = list(branch_keys)
    if title_key and title_key not in distinct:
        distinct.append(title_key)
    if not distinct:
        return {"status": "missing", "reason": "missing_jira_identity"}
    if len(distinct) > 1 or len(branch_keys) > 1:
        return {"status": "ambiguous", "reason": "ambiguous_jira_identity"}
    return {"status": "ok", "issue_key": distinct[0], "reason": "ok"}


def milestone_policy(milestone: str) -> dict[str, str]:
    """Return the transition category and comment template for one milestone."""
    policy = _MILESTONE_POLICY.get(str(milestone or ""))
    if policy is None:
        raise jira_mutations.MutationRequestError(
            f"--milestone must be one of {sorted(PR_MILESTONES)}"
        )
    return {"transition_category": policy["transition_category"],
            "comment_template": policy["comment_template"]}


def _configured_project_key(config: Mapping[str, Any] | None) -> str:
    tracker = config.get("tracker") if isinstance(config, Mapping) else None
    block = tracker.get("jira_cloud") if isinstance(tracker, Mapping) else None
    key = block.get("project_key") if isinstance(block, Mapping) else ""
    return str(key or "")


def resolve_sync_identity(
    config: Mapping[str, Any] | None,
    *,
    branch: str = "",
    pr_title: str = "",
    issue_ref: str = "",
) -> dict[str, str]:
    """Resolve and cross-check the sync target, failing closed on mismatch.

    An explicitly passed issue reference must equal the marker parsed from
    PR metadata, and a parsed marker must share the configured project-key
    prefix when one is configured. Anything else is an owner action, never
    a guess.
    """
    parsed = parse_jira_marker(branch=branch, pr_title=pr_title)
    explicit = str(issue_ref or "").strip().upper()
    if explicit and (len(explicit) > _MAX_KEY_LENGTH or not _KEY_RE.fullmatch(explicit)):
        return {"status": "mismatch", "reason": "jira_identity_mismatch"}
    if parsed["status"] != "ok":
        reason = str(parsed.get("reason") or "missing_jira_identity")
        return {"status": "missing" if reason == "missing_jira_identity" else "ambiguous",
                "reason": reason}
    key = str(parsed["issue_key"])
    if explicit and explicit != key.upper():
        return {"status": "mismatch", "reason": "jira_identity_mismatch"}
    project_key = _configured_project_key(config)
    if project_key and key.split("-", 1)[0] != project_key.upper():
        return {"status": "mismatch", "reason": "jira_identity_mismatch"}
    return {"status": "ok", "issue_key": key, "reason": "ok"}


def _owner_action_report(
    *,
    milestone: str,
    pr: Mapping[str, Any],
    reason: str,
    next_action: str,
) -> dict[str, Any]:
    return {
        "schema": SYNC_REPORT_SCHEMA,
        "status": "blocked",
        "reason": reason,
        "milestone": milestone if milestone in PR_MILESTONES else "",
        "issue_key": "",
        "pr": dict(pr),
        "transition_category": "",
        "comment_template": "",
        "gate_authority": "github",
        "gate_impact": "none",
        "mutation_plan": None,
        "write_request_count": 0,
        "next_action": next_action,
    }


def build_sync_plan(
    config: Mapping[str, Any] | None,
    *,
    milestone: str,
    pr_url: str,
    branch: str = "",
    pr_title: str = "",
    issue_ref: str = "",
    apply_requested: bool = False,
) -> dict[str, Any]:
    """Build the bounded sync report for one milestone. No network call.

    Identity failures return a fail-closed owner-action report with zero
    Jira calls. Otherwise the Jira effects are planned through
    :func:`jira_mutations.build_mutation_plan`, so both write guards and
    the closed transition/comment/link contract apply unchanged.
    """
    try:
        policy = milestone_policy(milestone)
    except jira_mutations.MutationRequestError:
        return _owner_action_report(
            milestone=str(milestone or ""),
            pr={},
            reason="invalid_milestone",
            next_action=(
                "Re-run with --milestone one of "
                f"{sorted(PR_MILESTONES)}. No Jira write was attempted."
            ),
        )
    try:
        pr = jira_mutations.parse_pull_request_url(pr_url)
    except jira_mutations.MutationRequestError:
        return _owner_action_report(
            milestone=milestone,
            pr={},
            reason="invalid_pr_url",
            next_action=(
                "Re-run with --pr-url as an "
                "https://github.com/OWNER/REPO/pull/NUMBER URL. "
                "No Jira write was attempted."
            ),
        )
    pr_meta = {"owner": pr["owner"], "repo": pr["repo"],
               "number": pr["number"], "url": pr["url"]}

    identity = resolve_sync_identity(
        config, branch=branch, pr_title=pr_title, issue_ref=issue_ref
    )
    if identity["status"] != "ok":
        reason = str(identity.get("reason") or "missing_jira_identity")
        if identity["status"] == "ambiguous":
            action = (
                "Owner action required: more than one Jira marker was found "
                "in the branch or PR title. Keep one matching marker and "
                "re-run. No Jira write was attempted."
            )
        elif identity["status"] == "mismatch":
            action = (
                "Owner action required: the Jira marker disagrees with "
                "--issue or the configured project key. Confirm the owning "
                "issue with the PR author, correct the metadata or config, "
                "and re-run. No Jira write was attempted."
            )
        else:
            action = (
                "Owner action required: no Jira marker was found in the "
                "branch name or the leading PR title token. Add one issue "
                "key there and re-run. "
                "No Jira write was attempted."
            )
        return _owner_action_report(
            milestone=milestone, pr=pr_meta, reason=reason, next_action=action
        )

    settings = jira_mutations.resolve_mutation_settings(config)
    transition_category = policy["transition_category"]
    if transition_category and transition_category not in settings.transitions:
        # A milestone whose category has no configured transition id
        # requests no transition: the link (and any comment) still applies.
        transition_category = ""
    request = jira_mutations.MutationRequest(
        issue_ref=str(identity["issue_key"]),
        claim=False,
        transition_category=transition_category,
        comment_template=policy["comment_template"],
        pr_url=pr["url"],
        link_pr=True,
    )
    plan = jira_mutations.build_mutation_plan(
        config, request, apply_requested=bool(apply_requested)
    )
    stale_categories = {
        "opened": ("blocked", "done"),
        "blocked": ("done",),
    }.get(milestone, ())
    stale_status_ids = sorted(
        {
            status_id
            for category in stale_categories
            for status_id in settings.status_category_map.get(category, ())
        }
    )
    if stale_status_ids:
        for operation in plan.get("operations") or []:
            if operation.get("operation") in {"transition", "comment"}:
                operation.setdefault("detail", {})["skip_if_status_ids"] = stale_status_ids
    return {
        "schema": SYNC_REPORT_SCHEMA,
        "status": plan["status"],
        "reason": "ok",
        "milestone": milestone,
        "issue_key": str(identity["issue_key"]),
        "pr": pr_meta,
        "transition_category": transition_category,
        "comment_template": policy["comment_template"],
        "gate_authority": "github",
        "gate_impact": "none",
        "mutation_plan": plan,
        "write_request_count": int(plan.get("write_request_count") or 0),
        "next_action": str(plan.get("next_action") or ""),
    }


def apply_sync_plan(
    sync_report: Mapping[str, Any],
    client: jira_mutations.JiraMutationClient,
) -> dict[str, Any]:
    """Execute an authorized sync plan against live Jira, idempotently.

    A fail-closed owner-action report is returned unchanged so identity
    failures can never escalate into writes. Otherwise the embedded plan
    runs through :func:`jira_mutations.apply_mutation_plan` and inherits
    its per-operation reconciliation: replays converge to
    ``already_applied`` instead of duplicating links, comments, or
    transitions. Jira failures stay in this report; gate state is neither
    read nor changed.
    """
    report = dict(sync_report)
    plan = report.get("mutation_plan")
    if not isinstance(plan, Mapping):
        return report
    applied = jira_mutations.apply_mutation_plan(plan, client)
    report["mutation_plan"] = applied
    report["status"] = str(applied.get("status") or report.get("status"))
    report["write_request_count"] = int(applied.get("write_request_count") or 0)
    report["next_action"] = str(applied.get("next_action") or report.get("next_action"))
    return report


def _pr_number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 1 else None


def discover_links(
    prs: Sequence[Mapping[str, Any]],
    *,
    cloud_id: str,
    project_id: str,
    key_to_issue_id: Mapping[str, str],
) -> dict[str, Any]:
    """Join PR branch/title markers to explicit local link references.

    Returns ``{"links": {(cloud_id, project_id, issue_id): pr_number},
    "rejected": [{"pr_number": ..., "reason": ...}]}``. Only the branch name
    and the leading PR title token are read; bodies, descriptions, comments,
    diffs, and transcripts are never consulted. Ambiguous, missing, or
    unresolvable markers are rejected with a closed reason instead of
    guessed, and when two PRs name one issue the lowest PR number wins.
    """
    links: dict[tuple[str, str, str], int] = {}
    rejected: list[dict[str, str]] = []
    candidates: list[tuple[str, int]] = []
    for pr in prs:
        if not isinstance(pr, Mapping):
            continue
        number = _pr_number(pr.get("number"))
        if number is None:
            continue
        parsed = parse_jira_marker(
            branch=str(pr.get("branch") or pr.get("headRefName") or ""),
            pr_title=str(pr.get("title") or ""),
        )
        if parsed["status"] != "ok":
            rejected.append({"pr_number": str(number),
                             "reason": str(parsed.get("reason") or "missing_jira_identity")})
            continue
        key = str(parsed["issue_key"])
        issue_id = key_to_issue_id.get(key, "")
        if not issue_id:
            rejected.append({"pr_number": str(number), "reason": "jira_identity_mismatch"})
            continue
        candidates.append((issue_id, number))
    candidates.sort(key=lambda item: (item[0], item[1]))
    seen: set[str] = set()
    for issue_id, number in candidates:
        reference = (cloud_id, project_id, issue_id)
        if issue_id in seen:
            rejected.append({"pr_number": str(number), "reason": "already_linked_elsewhere"})
            continue
        seen.add(issue_id)
        links[reference] = number
    return {"links": links, "rejected": rejected}


def reconcile_missed_events(
    events: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any] | None,
    *,
    apply_requested: bool = False,
    apply_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Replay missed sync events idempotently after an outage.

    Duplicate ``(issue marker, PR URL, milestone)`` events collapse to one
    replay; the rest report ``duplicate_skipped`` without a Jira call. Each
    surviving event builds (and, when both guards are present and ``apply_fn``
    is supplied, applies) through the same guarded surface as the live path,
    so recovery converges instead of duplicating. Reports carry bounded
    metadata only.
    """
    received = 0
    planned_events: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        received += 1
        planned_events.append(
            build_sync_plan(
                config,
                milestone=str(event.get("milestone") or ""),
                pr_url=str(event.get("pr_url") or ""),
                branch=str(event.get("branch") or ""),
                pr_title=str(event.get("pr_title") or ""),
                issue_ref=str(event.get("issue_ref") or event.get("issue_key") or ""),
                apply_requested=bool(apply_requested),
            )
        )

    pr_markers: dict[str, set[str]] = {}
    for report in planned_events:
        pr = report.get("pr") if isinstance(report.get("pr"), Mapping) else {}
        pr_url = str(pr.get("url") or "").lower()
        issue_key = str(report.get("issue_key") or "")
        if pr_url and issue_key:
            pr_markers.setdefault(pr_url, set()).add(issue_key)
    conflicted_prs = {
        pr_url for pr_url, markers in pr_markers.items() if len(markers) > 1
    }

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for report in planned_events:
        milestone = str(report.get("milestone") or "")
        issue_key = str(report.get("issue_key") or "")
        pr = report.get("pr") if isinstance(report.get("pr"), Mapping) else {}
        pr_url = str(pr.get("url") or "").lower()
        if pr_url in conflicted_prs:
            results.append({
                "status": "blocked",
                "reason": "ambiguous_jira_identity",
                "milestone": milestone,
                "issue_key": "",
                "write_request_count": 0,
            })
            continue
        if report.get("status") == "blocked":
            results.append({
                "status": "blocked",
                "reason": str(report.get("reason") or "invalid_request"),
                "milestone": milestone,
                "issue_key": issue_key,
                "write_request_count": 0,
            })
            continue
        key = (issue_key, pr_url, milestone)
        if key in seen:
            results.append(
                {
                    "status": "duplicate_skipped",
                    "reason": "ok",
                    "milestone": milestone,
                    "issue_key": issue_key,
                    "write_request_count": 0,
                }
            )
            continue
        seen.add(key)
        if (
            apply_requested
            and apply_fn is not None
            and isinstance(report.get("mutation_plan"), Mapping)
            and report["mutation_plan"].get("mode") == "apply"
            and any(
                operation.get("status") == "planned"
                for operation in report["mutation_plan"].get("operations") or []
            )
        ):
            report = apply_fn(report)
        results.append({
            "status": str(report.get("status")),
            "reason": str(report.get("reason") or "ok"),
            "milestone": milestone,
            "issue_key": str(report.get("issue_key") or ""),
            "write_request_count": int(report.get("write_request_count") or 0),
        })
    replayed = sum(
        1 for item in results
        if item["status"] not in {"duplicate_skipped", "blocked"}
    )
    blocked = sum(1 for item in results if item["status"] == "blocked")
    return {
        "schema": "code_mower.jiraPrSyncReconcile.v1",
        "events_received": received,
        "events_replayed": replayed,
        "events_blocked": blocked,
        "gate_authority": "github",
        "gate_impact": "none",
        "results": results,
    }
