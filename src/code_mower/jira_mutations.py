#!/usr/bin/env python3
"""Guarded, idempotent Jira Cloud mutation plan/apply surface (issue #799).

Dry-run planning is the default and performs no network call at all. A
network mutation happens only when *both* guards are present:

1. the repository config sets ``tracker.jira_cloud.mutations.writes_enabled:
   true``; and
2. the operator passes ``--apply`` at runtime.

With either guard absent this module returns a plan or an explicit refusal
and issues zero Jira requests. Doctor, tests, and ordinary dry runs
therefore cannot write.

Only four operations exist, and each is allow-listed twice -- once by
``mutations.allowed_operations`` in config, and once by the closed transport
allow-list on :class:`JiraMutationClient`:

- ``assign``: claim the issue for the authenticated account.
- ``transition``: one *configured* transition id, verified against the live
  issue's available transitions immediately before the write.
- ``comment``: one bounded comment rendered from a closed template table.
  There is no free-form comment passthrough: the transport builds the body
  itself from a template id and a validated GitHub pull request URL.
- ``link``: one GitHub pull request remote link, keyed by a deterministic
  ``globalId`` so Jira upserts instead of duplicating.

Deliberately absent, and rejected by the transport allow-list: delete of any
kind, attachments, arbitrary field updates, ``PUT /issue/{key}`` issue edits,
project or workflow administration, raw issue-body replacement, and writing
any issue property other than this module's own idempotency ledger.

Replay safety: assignment, transition, and remote link are reconciled from
authoritative live state (current assignee, current status plus available
transitions, and remote-link ``globalId``). Comments have no server-side
idempotency key and their bodies are never read back, so a bounded issue
property ledger records a stable fingerprint before the comment is posted
and finalizes it afterwards. An interrupted run therefore replays as
"already applied" and never posts a second comment.

GitHub remains the sole pull request, check, review, and merge-gate
authority. Nothing here reads or changes gate state, and a Jira refusal,
conflict, rate limit, or outage cannot weaken a gate decision.

Every report field is bounded metadata: identity, closed reason codes, and
counts. No issue description, comment body from Jira, raw response payload,
exception text, credential, account email, or absolute path is ever
returned, printed, or retained.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import config as code_mower_config
from . import jira_cloud
from .tracker_contract import LIFECYCLE_CATEGORIES


MUTATION_REPORT_SCHEMA = "code_mower.jiraMutationPlan.v1"
LEDGER_SCHEMA = "code_mower.jiraMutationLedger.v1"

#: Issue property that carries this module's replay ledger. It is the only
#: property key the transport may write.
LEDGER_PROPERTY_KEY = "code-mower-mutations-v1"
MAX_LEDGER_ENTRIES = 32

#: Report/apply order. Claim first so a later failure still leaves the issue
#: visibly owned; the pull request link lands before the comment that
#: references it.
MUTATION_ORDER = ("assign", "transition", "link", "comment")

#: Operations this surface will never grow. Reported so the refusal is
#: legible to an operator rather than implied by absence.
NEVER_SUPPORTED_OPERATIONS = (
    "delete",
    "attachment",
    "arbitrary_field_update",
    "issue_body_replacement",
    "project_administration",
    "free_form_comment",
)

#: Closed comment template table. ``{pr_url}`` is the only substitution and
#: must be a validated GitHub pull request URL. No caller-supplied prose can
#: reach a Jira comment.
COMMENT_TEMPLATES: Mapping[str, str] = {
    "claimed": (
        "Code Mower claimed this issue for a supervised builder lane. "
        "Delivery, review, and the merge gate stay on GitHub."
    ),
    "pr_opened": (
        "Code Mower opened GitHub pull request {pr_url} for this issue. "
        "Review and merge decisions remain on GitHub."
    ),
    "pr_merged": ("The GitHub pull request {pr_url} for this issue merged."),
    "pr_blocked": (
        "Code Mower paused this issue: GitHub pull request {pr_url} is not "
        "gate-ready. No Jira state was changed beyond this note."
    ),
}
TEMPLATES_REQUIRING_PR = frozenset({"pr_opened", "pr_merged", "pr_blocked"})

MAX_COMMENT_CHARACTERS = 600
REMOTE_LINK_RELATIONSHIP = "implemented by"
REMOTE_LINK_APPLICATION_NAME = "GitHub"

_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")
_TRANSITION_ID_RE = re.compile(r"^[0-9]{1,32}$")
_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9._-]{1,64})/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})/pull/(?P<number>[1-9][0-9]{0,9})$"
)

#: Closed reason vocabulary. A report never carries free-form failure text.
REASON_CODES = frozenset(
    {
        "ok",
        "writes_disabled",
        "apply_flag_missing",
        "operation_not_allowed",
        "transition_not_configured",
        "transition_unavailable",
        "already_assigned",
        "already_at_target_status",
        "already_commented",
        "already_linked",
        "replay_not_reposted",
        "account_unresolved",
        "issue_out_of_scope",
        "permission_denied",
        "unauthorized",
        "not_found",
        "conflict",
        "rate_limited",
        "unavailable",
        "rejected",
        "cancelled",
        "aborted_after_failure",
    }
)

_ERROR_CODE_OUTCOMES: Mapping[str, tuple[str, str]] = {
    "jira_unauthorized": ("failed", "unauthorized"),
    "jira_forbidden": ("blocked", "permission_denied"),
    "jira_not_found": ("blocked", "not_found"),
    "jira_conflict": ("blocked", "conflict"),
    "jira_rate_limited": ("failed", "rate_limited"),
    "jira_rejected": ("failed", "rejected"),
    "jira_cancelled": ("cancelled", "cancelled"),
    "jira_unavailable": ("failed", "unavailable"),
}

#: Worst-first, so a report status is the most severe operation outcome.
_STATUS_SEVERITY = (
    "cancelled",
    "failed",
    "blocked",
    "refused",
    "skipped",
    "applied",
    "already_applied",
    "planned",
)


class MutationRequestError(ValueError):
    """A malformed config or request, carrying a bounded operator message."""


@dataclass(frozen=True)
class MutationSettings:
    """Validated ``tracker.jira_cloud`` mutation configuration."""

    cloud_id: str
    project_id: str
    site_url: str
    project_key: str
    writes_enabled: bool
    allowed_operations: tuple[str, ...]
    transitions: Mapping[str, str]
    status_category_map: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class MutationRequest:
    """One operator request. Every field is a closed token or empty."""

    issue_ref: str
    claim: bool = False
    transition_category: str = ""
    comment_template: str = ""
    pr_url: str = ""
    link_pr: bool = False


@dataclass
class _Operation:
    operation: str
    status: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    #: True when *this* run opened the ledger's pending entry, which is how a
    #: first post is told apart from an interrupted earlier run's replay.
    opened_this_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "status": self.status,
            "reason": self.reason,
            "fingerprint": self.fingerprint,
            "detail": dict(self.detail),
        }


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def mutation_fingerprint(operation: str, payload: Mapping[str, Any]) -> str:
    """Return a stable, bounded fingerprint for one mutation intent.

    The same request on the same issue always produces the same value across
    processes and restarts, which is what makes the ledger replay-safe.
    """
    canonical = json.dumps(
        {"operation": operation, "payload": dict(payload)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def parse_pull_request_url(pr_url: str) -> dict[str, str]:
    """Validate a public GitHub pull request URL into bounded parts."""
    match = _PR_URL_RE.fullmatch(str(pr_url or "").strip())
    if match is None:
        raise MutationRequestError(
            "--pr-url must be an https://github.com/OWNER/REPO/pull/NUMBER URL"
        )
    owner = match.group("owner")
    repo = match.group("repo")
    number = match.group("number")
    return {
        "owner": owner,
        "repo": repo,
        "number": number,
        "url": f"https://github.com/{owner}/{repo}/pull/{number}",
        "title": f"{owner}/{repo}#{number}",
        "global_id": remote_link_global_id(owner, repo, number),
    }


def remote_link_global_id(owner: str, repo: str, number: str) -> str:
    """Return the deterministic remote-link idempotency key for one PR.

    Jira upserts a remote link when a known ``globalId`` is supplied, so a
    replayed apply updates the same link instead of adding a second one.
    """
    candidate = f"code-mower:github:{owner.lower()}/{repo.lower()}/pull/{number}"
    return jira_cloud.validate_remote_link_global_id(candidate)


def render_comment(template_id: str, pr_url: str = "") -> str:
    """Render one bounded comment from the closed template table."""
    template = COMMENT_TEMPLATES.get(str(template_id or ""))
    if template is None:
        raise MutationRequestError(
            f"--comment must be one of {sorted(COMMENT_TEMPLATES)}"
        )
    if template_id in TEMPLATES_REQUIRING_PR:
        parsed = parse_pull_request_url(pr_url)
        text = template.format(pr_url=parsed["url"])
    else:
        text = template
    if len(text) > MAX_COMMENT_CHARACTERS:  # pragma: no cover - table is bounded
        raise MutationRequestError("rendered comment exceeds the bounded length")
    return text


def comment_document(text: str, fingerprint: str) -> dict[str, Any]:
    """Build the Atlassian Document Format body for one templated comment.

    The trailing marker line makes the replay key visible to a human reader
    without embedding any prose, path, or identifier beyond the fingerprint.
    """
    marker = f"Code Mower idempotency marker: {fingerprint}"
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]},
            {"type": "paragraph", "content": [{"type": "text", "text": marker}]},
        ],
    }


# -- Configuration -------------------------------------------------------


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item)


def resolve_mutation_settings(config: Mapping[str, Any] | None) -> MutationSettings:
    """Validate the tracker block a mutation needs, or raise.

    This is pure configuration reading: no credential resolution and no
    network call happen here.
    """
    tracker = config.get("tracker") if isinstance(config, Mapping) else None
    if not isinstance(tracker, Mapping) or tracker.get("kind") != "jira_cloud":
        raise MutationRequestError(
            "tracker.kind must be jira_cloud to plan a Jira mutation"
        )
    block = tracker.get("jira_cloud")
    if not isinstance(block, Mapping):
        raise MutationRequestError("tracker.jira_cloud block is missing")

    cloud_id = str(block.get("cloud_id") or "")
    project_id = str(block.get("project_id") or "")
    site_url = str(block.get("site_url") or "")
    if not jira_cloud._CLOUD_ID_RE.fullmatch(cloud_id):
        raise MutationRequestError("tracker.jira_cloud.cloud_id is missing or malformed")
    if not jira_cloud._PROJECT_ID_RE.fullmatch(project_id):
        raise MutationRequestError(
            "tracker.jira_cloud.project_id must be the immutable numeric project id"
        )
    try:
        display_site = jira_cloud.display_site_url(site_url)
    except ValueError:
        raise MutationRequestError(
            "tracker.jira_cloud.site_url must be an HTTPS Jira Cloud site URL"
        ) from None

    raw_mutations = block.get("mutations")
    mutations = raw_mutations if isinstance(raw_mutations, Mapping) else {}
    transitions: dict[str, str] = {}
    raw_transitions = mutations.get("transitions")
    if isinstance(raw_transitions, Mapping):
        for category, transition_id in raw_transitions.items():
            if category not in LIFECYCLE_CATEGORIES:
                raise MutationRequestError(
                    "tracker.jira_cloud.mutations.transitions keys must be "
                    f"one of {sorted(LIFECYCLE_CATEGORIES)}"
                )
            if not isinstance(transition_id, str) or not _TRANSITION_ID_RE.fullmatch(
                transition_id
            ):
                raise MutationRequestError(
                    "tracker.jira_cloud.mutations.transitions values must be "
                    "numeric Jira transition ids"
                )
            transitions[category] = transition_id

    status_map: dict[str, tuple[str, ...]] = {}
    raw_status_map = block.get("status_category_map")
    if isinstance(raw_status_map, Mapping):
        for category, ids in raw_status_map.items():
            if category in LIFECYCLE_CATEGORIES:
                status_map[category] = _string_list(ids)

    return MutationSettings(
        cloud_id=cloud_id,
        project_id=project_id,
        site_url=display_site,
        project_key=str(block.get("project_key") or ""),
        writes_enabled=mutations.get("writes_enabled") is True,
        allowed_operations=tuple(
            op for op in MUTATION_ORDER if op in _string_list(mutations.get("allowed_operations"))
        ),
        transitions=transitions,
        status_category_map=status_map,
    )


# -- Planning (no network) ----------------------------------------------


def _plan_operations(
    settings: MutationSettings, request: MutationRequest
) -> list[_Operation]:
    requested: dict[str, _Operation] = {}

    if request.claim:
        requested["assign"] = _Operation(
            operation="assign",
            status="planned",
            reason="ok",
            detail={"assignee": "authenticated_account"},
            fingerprint=mutation_fingerprint(
                "assign", {"issue": request.issue_ref, "assignee": "self"}
            ),
        )

    if request.transition_category:
        category = request.transition_category
        if category not in LIFECYCLE_CATEGORIES:
            raise MutationRequestError(
                f"--transition must be one of {sorted(LIFECYCLE_CATEGORIES)}"
            )
        transition_id = settings.transitions.get(category, "")
        if not transition_id:
            requested["transition"] = _Operation(
                operation="transition",
                status="refused",
                reason="transition_not_configured",
                detail={"lifecycle_category": category},
            )
        else:
            requested["transition"] = _Operation(
                operation="transition",
                status="planned",
                reason="ok",
                detail={
                    "lifecycle_category": category,
                    "transition_id": transition_id,
                    # Configured status ids for this category let apply
                    # recognize an issue that is already at the target.
                    "target_status_ids": list(
                        settings.status_category_map.get(category, ())
                    ),
                },
                fingerprint=mutation_fingerprint(
                    "transition",
                    {"issue": request.issue_ref, "transition_id": transition_id},
                ),
            )

    if request.link_pr:
        parsed = parse_pull_request_url(request.pr_url)
        requested["link"] = _Operation(
            operation="link",
            status="planned",
            reason="ok",
            detail={
                "global_id": parsed["global_id"],
                "title": parsed["title"],
                "url": parsed["url"],
                "relationship": REMOTE_LINK_RELATIONSHIP,
            },
            fingerprint=mutation_fingerprint(
                "link", {"issue": request.issue_ref, "global_id": parsed["global_id"]}
            ),
        )

    if request.comment_template:
        text = render_comment(request.comment_template, request.pr_url)
        comment_detail: dict[str, Any] = {
            "template": request.comment_template,
            "characters": len(text),
        }
        if request.comment_template in TEMPLATES_REQUIRING_PR:
            comment_detail["pr_url"] = parse_pull_request_url(request.pr_url)["url"]
        requested["comment"] = _Operation(
            operation="comment",
            status="planned",
            reason="ok",
            detail=comment_detail,
            fingerprint=mutation_fingerprint(
                "comment",
                {"issue": request.issue_ref, "template": request.comment_template, "text": text},
            ),
        )

    if not requested:
        raise MutationRequestError(
            "request at least one of --claim, --transition, --comment, or --link-pr"
        )

    operations: list[_Operation] = []
    for name in MUTATION_ORDER:
        operation = requested.get(name)
        if operation is None:
            continue
        if name not in settings.allowed_operations and operation.status == "planned":
            operation.status = "refused"
            operation.reason = "operation_not_allowed"
        operations.append(operation)
    return operations


def _report_status(operations: Sequence[_Operation]) -> str:
    statuses = {operation.status for operation in operations}
    for candidate in _STATUS_SEVERITY:
        if candidate in statuses:
            return candidate
    return "planned"  # pragma: no cover - operations are never empty


def _next_action(
    mode: str, status: str, *, writes_enabled: bool, apply_requested: bool
) -> str:
    if status == "refused" and not writes_enabled and apply_requested:
        return (
            "Set tracker.jira_cloud.mutations.writes_enabled: true to allow this "
            "repository to write, then re-run with --apply. No Jira write was attempted."
        )
    if status == "refused":
        return (
            "Add the refused operations to "
            "tracker.jira_cloud.mutations.allowed_operations (and configure a "
            "transition id where required), then re-run."
        )
    if mode == "plan":
        if not writes_enabled:
            return (
                "Dry run only. Writes stay disabled until "
                "tracker.jira_cloud.mutations.writes_enabled is true and --apply is passed."
            )
        return "Dry run only. Re-run with --apply to perform these Jira writes."
    if status in ("applied", "already_applied"):
        return "Jira is synchronized. GitHub remains the only PR, check, and merge-gate authority."
    if status == "blocked":
        return (
            "Re-check Jira permissions, the configured transition id, and the current "
            "issue state, then re-run with --apply. Applied operations are not repeated."
        )
    if status == "cancelled":
        return "The run was cancelled. Re-run with --apply; applied operations are not repeated."
    return (
        "Jira was unavailable, rate limited, or rejected the request. Re-run with "
        "--apply later. GitHub gate decisions are unaffected."
    )


def build_mutation_plan(
    config: Mapping[str, Any] | None,
    request: MutationRequest,
    *,
    apply_requested: bool = False,
) -> dict[str, Any]:
    """Build the bounded plan/refusal report. Performs no network call.

    ``mode`` is ``apply`` only when the repository guard and the runtime
    ``--apply`` guard are both present; otherwise the caller must not touch
    the network.
    """
    settings = resolve_mutation_settings(config)
    try:
        issue_ref = jira_cloud.validate_issue_ref(request.issue_ref)
    except ValueError as exc:
        raise MutationRequestError(f"--issue {exc}") from None
    request = MutationRequest(
        issue_ref=issue_ref,
        claim=request.claim,
        transition_category=request.transition_category,
        comment_template=request.comment_template,
        pr_url=request.pr_url,
        link_pr=request.link_pr,
    )
    operations = _plan_operations(settings, request)

    guard_refusals: list[str] = []
    if not settings.writes_enabled:
        guard_refusals.append("writes_disabled")
    if not apply_requested:
        guard_refusals.append("apply_flag_missing")
    writes_authorized = not guard_refusals

    if apply_requested and not settings.writes_enabled:
        # An explicit apply against a repository that never enabled writes is
        # a refusal, not a silent dry run.
        for operation in operations:
            if operation.status == "planned":
                operation.status = "refused"
                operation.reason = "writes_disabled"

    mode = "apply" if writes_authorized else "plan"
    status = _report_status(operations)
    if mode == "apply" and status == "planned":
        status = "ready"

    browse_url = ""
    if settings.site_url:
        browse_url = f"{settings.site_url.rstrip('/')}/browse/{urllib.parse.quote(issue_ref, safe='')}"

    return {
        "schema": MUTATION_REPORT_SCHEMA,
        "generated_at": _utc_now(),
        "mode": mode,
        "status": status,
        "guards": {
            "writes_enabled": settings.writes_enabled,
            "apply_requested": bool(apply_requested),
            "writes_authorized": writes_authorized,
            "refusals": guard_refusals,
            "allowed_operations": list(settings.allowed_operations),
        },
        "tracker": {
            "kind": "jira_cloud",
            "cloud_id": settings.cloud_id,
            "project_id": settings.project_id,
            "project_key": settings.project_key,
            "issue_ref": issue_ref,
            "browse_url": browse_url,
        },
        "gate_authority": "github",
        "gate_impact": "none",
        "never_supported_operations": list(NEVER_SUPPORTED_OPERATIONS),
        "operations": [operation.as_dict() for operation in operations],
        "write_request_count": 0,
        "next_action": _next_action(
            mode,
            status,
            writes_enabled=settings.writes_enabled,
            apply_requested=bool(apply_requested),
        ),
    }


# -- Transport -----------------------------------------------------------

_WRITE_ALLOW_LIST: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PUT", re.compile(r"^/rest/api/3/issue/[^/]+/assignee$")),
    ("POST", re.compile(r"^/rest/api/3/issue/[^/]+/transitions$")),
    ("POST", re.compile(r"^/rest/api/3/issue/[^/]+/comment$")),
    ("POST", re.compile(r"^/rest/api/3/issue/[^/]+/remotelink$")),
    (
        "PUT",
        re.compile(
            r"^/rest/api/3/issue/[^/]+/properties/" + re.escape(LEDGER_PROPERTY_KEY) + r"$"
        ),
    ),
)


class JiraMutationClient(jira_cloud.JiraReadClient):
    """Read client widened to a closed, guarded Jira write allow-list.

    Constructing this class does not authorize anything: callers reach it
    only after :func:`build_mutation_plan` reports ``mode == "apply"``, which
    requires both the repository write guard and the runtime apply flag.

    The allow-list is the last line of defence. DELETE is rejected for every
    path, issue edits and attachments have no entry, and the only writable
    issue property is this module's ledger. Comment and remote-link bodies
    are built inside this class from a template id and a validated GitHub
    pull request URL, so no caller-supplied prose can reach Jira.
    """

    def _check_request_allowed(self, method: str, path: str) -> None:
        if not path.startswith("/rest/api/3/"):
            raise ValueError("jira_cloud client refuses paths outside /rest/api/3/")
        if method == "GET" or (method == "POST" and path in jira_cloud.READ_ONLY_POST_PATHS):
            return
        if method == "DELETE":
            raise ValueError("jira mutation client never issues a delete request")
        for allowed_method, pattern in _WRITE_ALLOW_LIST:
            if method == allowed_method and pattern.fullmatch(path):
                return
        raise ValueError("jira mutation client refuses a request outside its write allow-list")

    def _auth_headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = super()._auth_headers(extra)
        headers["User-Agent"] = "code-mower-jira-mutations/1.0"
        return headers

    def _issue_path(self, issue_ref: str, suffix: str) -> str:
        quoted = urllib.parse.quote(jira_cloud.validate_issue_ref(issue_ref), safe="")
        return f"/rest/api/3/issue/{quoted}/{suffix}"

    def assign_issue(self, issue_ref: str, account_id: str) -> None:
        """Assign one issue to an Atlassian account id (idempotent PUT)."""
        if not _ACCOUNT_ID_RE.fullmatch(str(account_id or "")):
            raise MutationRequestError("assignee account id is malformed")
        self.request_json(
            "PUT",
            self._issue_path(issue_ref, "assignee"),
            json_body={"accountId": account_id},
            endpoint="assign",
            allow_empty=True,
        )

    def transition_issue(self, issue_ref: str, transition_id: str) -> None:
        """Perform one configured transition; no screen fields are sent."""
        if not _TRANSITION_ID_RE.fullmatch(str(transition_id or "")):
            raise MutationRequestError("transition id must be numeric")
        self.request_json(
            "POST",
            self._issue_path(issue_ref, "transitions"),
            json_body={"transition": {"id": transition_id}},
            endpoint="transition",
            allow_empty=True,
        )

    def add_templated_comment(
        self, issue_ref: str, template_id: str, *, pr_url: str, fingerprint: str
    ) -> str:
        """Post one templated comment and return only its bounded id.

        The body is rendered here from the closed template table, so the
        transport cannot be used to post arbitrary text. The response
        contains the stored comment; only its id is read out of the buffer.
        """
        text = render_comment(template_id, pr_url)
        data = self.request_json(
            "POST",
            self._issue_path(issue_ref, "comment"),
            json_body={"body": comment_document(text, fingerprint)},
            endpoint="comment",
        )
        return jira_cloud._bounded_str(data.get("id"), 32)

    def link_pull_request(self, issue_ref: str, pr_url: str) -> str:
        """Create or update the single PR remote link, keyed by globalId."""
        parsed = parse_pull_request_url(pr_url)
        data = self.request_json(
            "POST",
            self._issue_path(issue_ref, "remotelink"),
            json_body={
                "globalId": parsed["global_id"],
                "relationship": REMOTE_LINK_RELATIONSHIP,
                "application": {"name": REMOTE_LINK_APPLICATION_NAME},
                "object": {"url": parsed["url"], "title": parsed["title"]},
            },
            endpoint="remoteLink",
        )
        return jira_cloud._bounded_str(data.get("id"), 32)

    def set_mutation_ledger(self, issue_ref: str, ledger: Mapping[str, Any]) -> None:
        """Write the bounded replay ledger to this module's own property."""
        payload = _validated_ledger(ledger)
        quoted_key = urllib.parse.quote(
            jira_cloud.validate_property_key(LEDGER_PROPERTY_KEY), safe=""
        )
        self.request_json(
            "PUT",
            self._issue_path(issue_ref, f"properties/{quoted_key}"),
            json_body=payload,
            endpoint="issueProperty",
            allow_empty=True,
        )


# -- Idempotency ledger --------------------------------------------------


def _validated_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded ledger payload, dropping anything unrecognized.

    The ledger is written to a Jira issue property, so it is held to the same
    metadata-only bar as every other emitted field: closed keys, closed
    states, fingerprints, and timestamps only.
    """
    entries_in = ledger.get("entries")
    entries: dict[str, dict[str, str]] = {}
    if isinstance(entries_in, Mapping):
        for fingerprint, entry in entries_in.items():
            if not isinstance(entry, Mapping) or not isinstance(fingerprint, str):
                continue
            if not re.fullmatch(r"[0-9a-f]{32}", fingerprint):
                continue
            operation = str(entry.get("operation") or "")
            state = str(entry.get("state") or "")
            if operation not in MUTATION_ORDER or state not in ("pending", "applied"):
                continue
            entries[fingerprint] = {
                "operation": operation,
                "state": state,
                "at": jira_cloud._bounded_str(entry.get("at"), 32),
            }
    trimmed = sorted(entries.items(), key=lambda item: item[1]["at"], reverse=True)
    return {
        "schema": LEDGER_SCHEMA,
        "entries": dict(trimmed[:MAX_LEDGER_ENTRIES]),
    }


def _ledger_state(ledger: Mapping[str, Any], fingerprint: str) -> str:
    entries = ledger.get("entries")
    if not isinstance(entries, Mapping):
        return ""
    entry = entries.get(fingerprint)
    if not isinstance(entry, Mapping):
        return ""
    state = str(entry.get("state") or "")
    return state if state in ("pending", "applied") else ""


def _record(ledger: dict[str, Any], operation: _Operation, state: str) -> dict[str, Any]:
    entries = dict(ledger.get("entries") or {})
    entries[operation.fingerprint] = {
        "operation": operation.operation,
        "state": state,
        "at": _utc_now(),
    }
    return _validated_ledger({"entries": entries})


# -- Apply ---------------------------------------------------------------


class _WriteCounter:
    """Counts network mutations so a report can prove zero-write refusals."""

    def __init__(self) -> None:
        self.count = 0

    def bump(self) -> None:
        self.count += 1


def _fail_from_error(operation: _Operation, exc: jira_cloud.JiraApiError) -> None:
    status, reason = _ERROR_CODE_OUTCOMES.get(exc.code, ("failed", "unavailable"))
    operation.status = status
    operation.reason = reason


def apply_mutation_plan(
    plan: Mapping[str, Any], client: JiraMutationClient
) -> dict[str, Any]:
    """Execute an authorized plan against live Jira, idempotently.

    ``plan`` must have ``mode == "apply"``; anything else is returned
    unchanged so a refusal can never be escalated into a write. Live issue
    state and available transitions are re-read immediately before the
    write, so workflow or permission drift blocks instead of guessing.
    """
    report = json.loads(json.dumps(dict(plan)))
    if report.get("mode") != "apply":
        return report

    settings_project = str(report["tracker"]["project_id"])
    issue_ref = str(report["tracker"]["issue_ref"])
    operations = [
        _Operation(
            operation=item["operation"],
            status=item["status"],
            reason=item["reason"],
            detail=dict(item.get("detail") or {}),
            fingerprint=str(item.get("fingerprint") or ""),
        )
        for item in report.get("operations") or []
    ]
    pending = [operation for operation in operations if operation.status == "planned"]
    writes = _WriteCounter()

    def _finish(ledger_status: str) -> dict[str, Any]:
        report["operations"] = [operation.as_dict() for operation in operations]
        report["write_request_count"] = writes.count
        report["ledger_status"] = ledger_status
        report["status"] = _report_status(operations)
        report["next_action"] = _next_action(
            "apply", report["status"], writes_enabled=True, apply_requested=True
        )
        return report

    if not pending:
        return _finish("unchanged")

    def _abort() -> dict[str, Any]:
        for operation in pending:
            if operation.status == "planned":
                operation.status = "skipped"
                operation.reason = "aborted_after_failure"
        return _finish("unchanged")

    # Validate live issue state immediately before any write.
    try:
        state = client.get_issue_state(issue_ref)
        ledger = _validated_ledger(
            client.get_issue_property(issue_ref, LEDGER_PROPERTY_KEY) or {}
        )
    except jira_cloud.JiraApiError as exc:
        for operation in pending:
            _fail_from_error(operation, exc)
        return _finish("unchanged")

    if state.get("project_id") and state["project_id"] != settings_project:
        for operation in pending:
            operation.status = "blocked"
            operation.reason = "issue_out_of_scope"
        return _finish("unchanged")

    report["tracker"]["issue_id"] = state.get("id", "")
    report["tracker"]["issue_key"] = state.get("key", "")

    # A comment cannot be verified after the fact, so its intent is recorded
    # before it is posted. An interrupted run replays as already-applied.
    comment_op = next(
        (operation for operation in pending if operation.operation == "comment"), None
    )
    will_post_comment = comment_op is not None and not _ledger_state(
        ledger, comment_op.fingerprint
    )
    if comment_op is not None and will_post_comment:
        try:
            ledger = _record(ledger, comment_op, "pending")
            client.set_mutation_ledger(issue_ref, ledger)
            writes.bump()
            comment_op.opened_this_run = True
        except jira_cloud.JiraApiError as exc:
            _fail_from_error(comment_op, exc)
            return _abort()

    for operation in pending:
        try:
            handler = _HANDLERS[operation.operation]
            ledger = handler(operation, client, issue_ref, state, ledger, writes)
        except jira_cloud.JiraApiError as exc:
            _fail_from_error(operation, exc)
            return _abort()
        except MutationRequestError:
            operation.status = "blocked"
            operation.reason = "rejected"
            return _abort()
        if operation.status not in ("applied", "already_applied"):
            return _abort()

    ledger_status = "written"
    try:
        client.set_mutation_ledger(issue_ref, ledger)
        writes.bump()
    except jira_cloud.JiraApiError:
        # Applied effects are reconciled from live state on the next run, and
        # a comment left "pending" is never reposted, so a failed ledger write
        # is a reporting gap rather than a duplication risk.
        ledger_status = "write_failed"
    return _finish(ledger_status)


def _apply_assign(
    operation: _Operation,
    client: JiraMutationClient,
    issue_ref: str,
    state: Mapping[str, Any],
    ledger: dict[str, Any],
    writes: _WriteCounter,
) -> dict[str, Any]:
    account_id = client.get_my_account_id()
    if not account_id:
        operation.status = "blocked"
        operation.reason = "account_unresolved"
        return ledger
    if state.get("assignee_account_id") == account_id:
        operation.status = "already_applied"
        operation.reason = "already_assigned"
        return _record(ledger, operation, "applied")
    client.assign_issue(issue_ref, account_id)
    writes.bump()
    operation.status = "applied"
    operation.reason = "ok"
    return _record(ledger, operation, "applied")


def _apply_transition(
    operation: _Operation,
    client: JiraMutationClient,
    issue_ref: str,
    state: Mapping[str, Any],
    ledger: dict[str, Any],
    writes: _WriteCounter,
) -> dict[str, Any]:
    transition_id = str(operation.detail.get("transition_id") or "")
    category = str(operation.detail.get("lifecycle_category") or "")
    current_status = str(state.get("status_id") or "")
    target_ids = set(operation.detail.get("target_status_ids") or ())
    if current_status and current_status in target_ids:
        operation.status = "already_applied"
        operation.reason = "already_at_target_status"
        return _record(ledger, operation, "applied")

    available = client.get_transitions(issue_ref)
    match = next((item for item in available if item.get("id") == transition_id), None)
    if match is None:
        # Either the workflow changed or the account lost Transition Issues.
        # Both are drift; neither may be guessed around.
        operation.status = "blocked"
        operation.reason = "transition_unavailable"
        operation.detail["available_transition_count"] = len(available)
        return ledger
    if current_status and match.get("to_status_id") == current_status:
        operation.status = "already_applied"
        operation.reason = "already_at_target_status"
        return _record(ledger, operation, "applied")

    client.transition_issue(issue_ref, transition_id)
    writes.bump()
    operation.status = "applied"
    operation.reason = "ok"
    operation.detail["lifecycle_category"] = category
    return _record(ledger, operation, "applied")


def _apply_link(
    operation: _Operation,
    client: JiraMutationClient,
    issue_ref: str,
    state: Mapping[str, Any],
    ledger: dict[str, Any],
    writes: _WriteCounter,
) -> dict[str, Any]:
    if client.has_remote_link(issue_ref, str(operation.detail.get("global_id") or "")):
        operation.status = "already_applied"
        operation.reason = "already_linked"
        return _record(ledger, operation, "applied")
    client.link_pull_request(issue_ref, str(operation.detail.get("url") or ""))
    writes.bump()
    operation.status = "applied"
    operation.reason = "ok"
    return _record(ledger, operation, "applied")


def _apply_comment(
    operation: _Operation,
    client: JiraMutationClient,
    issue_ref: str,
    state: Mapping[str, Any],
    ledger: dict[str, Any],
    writes: _WriteCounter,
) -> dict[str, Any]:
    recorded = _ledger_state(ledger, operation.fingerprint)
    if recorded == "applied":
        operation.status = "already_applied"
        operation.reason = "already_commented"
        return ledger
    if recorded == "pending" and not operation.opened_this_run:
        # This run did not open the pending entry, so a previous run already
        # reached the post. Jira has no comment idempotency key and comment
        # bodies are never read back, so replay finalizes without reposting.
        operation.status = "already_applied"
        operation.reason = "replay_not_reposted"
        return _record(ledger, operation, "applied")
    comment_id = client.add_templated_comment(
        issue_ref,
        str(operation.detail.get("template") or ""),
        pr_url=str(operation.detail.get("pr_url") or ""),
        fingerprint=operation.fingerprint,
    )
    writes.bump()
    operation.status = "applied"
    operation.reason = "ok"
    if comment_id:
        operation.detail["comment_id"] = comment_id
    return _record(ledger, operation, "applied")


_HANDLERS: Mapping[str, Callable[..., dict[str, Any]]] = {
    "assign": _apply_assign,
    "transition": _apply_transition,
    "link": _apply_link,
    "comment": _apply_comment,
}


# -- CLI -----------------------------------------------------------------


ClientFactory = Callable[..., JiraMutationClient]


def _default_client_factory(
    *,
    cloud_id: str,
    email: str,
    token: str,
    site_url: str,
    timeout_seconds: float,
) -> JiraMutationClient:
    return JiraMutationClient(
        cloud_id=cloud_id,
        email=email,
        token=token,
        site_url=site_url,
        timeout_seconds=float(timeout_seconds),
    )


def _render_text(report: Mapping[str, Any]) -> str:
    guards = report["guards"]
    tracker = report["tracker"]
    lines = [
        "Code Mower Jira mutation plan",
        f"Mode: {report['mode']}",
        f"Status: {report['status']}",
        f"Writes enabled (config): {str(guards['writes_enabled']).lower()}",
        f"Apply requested (runtime): {str(guards['apply_requested']).lower()}",
        f"Issue: {tracker['issue_ref']} (project {tracker['project_id']})",
        f"Gate authority: {report['gate_authority']} (jira impact: {report['gate_impact']})",
        f"Jira write requests: {report['write_request_count']}",
        "Operations:",
    ]
    for operation in report["operations"]:
        lines.append(
            f"- {operation['operation']}: {operation['status']} ({operation['reason']})"
        )
    lines.append(f"Next action: {report['next_action']}")
    return "\n".join(lines)


def _build_request(args: argparse.Namespace) -> MutationRequest:
    return MutationRequest(
        issue_ref=args.issue,
        claim=bool(args.claim),
        transition_category=args.transition or "",
        comment_template=args.comment or "",
        pr_url=args.pr_url or "",
        link_pr=bool(args.link_pr),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-mower tracker",
        description=(
            "Plan and (only with both guards present) apply bounded Jira Cloud "
            "mutations. Dry run is the default and performs no Jira call."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    mutate = subparsers.add_parser(
        "mutate", help="plan or apply guarded Jira claim/transition/comment/link"
    )
    mutate.add_argument("config", nargs="?", default="code-mower.yml")
    mutate.add_argument("--issue", required=True, help="Jira issue id or key")
    mutate.add_argument("--claim", action="store_true", help="assign to the authenticated account")
    mutate.add_argument(
        "--transition",
        default="",
        choices=("", *LIFECYCLE_CATEGORIES),
        help="apply the configured transition id for this lifecycle category",
    )
    mutate.add_argument(
        "--comment",
        default="",
        choices=("", *sorted(COMMENT_TEMPLATES)),
        help="post one bounded templated comment (no free-form text)",
    )
    mutate.add_argument("--pr-url", default="", help="GitHub pull request URL")
    mutate.add_argument(
        "--link-pr", action="store_true", help="attach one PR remote link for --pr-url"
    )
    mutate.add_argument(
        "--apply",
        action="store_true",
        help="perform the plan; also requires tracker.jira_cloud.mutations.writes_enabled",
    )
    mutate.add_argument("--json", action="store_true")
    mutate.add_argument(
        "--plan-out", default="", help="retain the bounded plan/apply report at this path"
    )
    mutate.add_argument("--provider-credential-file", default="")
    mutate.add_argument("--provider-profile", default="")
    mutate.add_argument("--provider-config-dir", default="")
    mutate.add_argument("--http-timeout", type=float, default=jira_cloud.REQUEST_TIMEOUT_SECONDS)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    client_factory: ClientFactory | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        config = code_mower_config.load_config(Path(args.config))
    except code_mower_config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        settings = resolve_mutation_settings(config)
        request = _build_request(args)
        report = build_mutation_plan(config, request, apply_requested=bool(args.apply))
    except MutationRequestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    has_pending = any(
        operation["status"] == "planned" for operation in report["operations"]
    )
    if report["mode"] == "apply" and has_pending:
        resolution = jira_cloud.resolve_jira_credentials(
            credential_file=Path(args.provider_credential_file)
            if args.provider_credential_file
            else None,
            profile=args.provider_profile,
            config_dir=Path(args.provider_config_dir) if args.provider_config_dir else None,
            env=env,
        )
        if not resolution.has_credentials:
            print(f"error: {resolution.message}", file=sys.stderr)
            print(f"remediation: {resolution.remediation}", file=sys.stderr)
            return 1
        factory = client_factory or _default_client_factory
        try:
            client = factory(
                cloud_id=settings.cloud_id,
                email=resolution.email,
                token=resolution.token,
                site_url=settings.site_url,
                timeout_seconds=float(args.http_timeout),
            )
        except (TypeError, ValueError):
            print("error: Jira tracker identity is malformed", file=sys.stderr)
            return 1
        report = apply_mutation_plan(report, client)

    if args.plan_out:
        try:
            Path(args.plan_out).write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            report["plan_retained"] = True
        except OSError:
            report["plan_retained"] = False
            print("error: unable to retain the plan at the requested path", file=sys.stderr)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_render_text(report))
    return 0 if report["status"] in ("planned", "applied", "already_applied") else 1


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
