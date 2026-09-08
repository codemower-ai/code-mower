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
  issue's available transitions immediately before the write, and blocked
  unless that transition's live destination status is one of the status ids
  configured for the requested lifecycle category. A category configured
  with a transition id but no such status ids is refused during planning,
  because that gap is knowable from config alone and must not be found only
  after a sibling operation has already written.
- ``comment``: one bounded comment rendered from a closed template table.
  There is no free-form comment passthrough: the transport builds the body
  itself from a template id and a validated GitHub pull request URL.
- ``link``: one GitHub pull request remote link, keyed by a deterministic
  ``globalId`` so Jira upserts instead of duplicating.

Deliberately absent, and rejected by the transport allow-list: delete of any
kind, attachments, arbitrary field updates, ``PUT /issue/{key}`` issue edits,
project or workflow administration, raw issue-body replacement, and writing
any issue property other than this module's own advisory ledger and its
per-comment claim keys.

Replay safety: assignment, transition, and remote link are reconciled from
authoritative live state (current assignee, current status plus available
transitions, and remote-link ``globalId``). Every write gets exactly one
transport attempt. Even an idempotent write cannot be retried under stale
authorization: after an ambiguous timeout, 429, or 5xx, the operator re-runs
the apply so scope and live state are established again first. Reads retain
their bounded retry budget.

Comments have no server-side idempotency key and their bodies are never read
back, so at-most-once comes from Jira's own create-or-update semantics on
issue properties. Each comment intent has its own property key derived from
its fingerprint; ``PUT`` answers 201 when it created that key and 200 when
it replaced an existing one, so only a 201 acquires the right to post. That
is per intent and never evicts, so neither a concurrent apply nor a 33rd
comment on the same issue can produce a second copy. A claim that exists but
was never finalized reports ``unverified`` and asks an owner to reconcile
that one comment by hand; it is never reposted automatically.

Scope is re-established before every *physical write*, not once per apply
and not once per operation. One operation is not one write: a comment
acquires a claim property, posts, and finalizes that claim, and the apply
ends with an advisory ledger PUT, so a per-operation check still leaves
windows where a Jira automation rule can move the issue between two writes
this module has already decided to make.

The invariant therefore lives at the transport boundary rather than in the
handlers. :class:`JiraMutationClient` is armed once, after preflight, with
the validated issue reference, the immutable numeric issue id, and the
configured project id. Immediately before every write attempt that would
leave the process, the client re-reads that issue and requires the live
project id to be present and exactly equal to the configured ``project_id``
-- an empty or unreadable project id is unauthorized, never permission --
and the live issue id to still be the armed one. A write aimed at any other
issue, or attempted on a client that was never armed, is refused before it
is sent. No handler has to remember to ask.

A refused write stops the run: the pending write never happens, the
remaining operations are skipped, and the report carries a closed reason
code. Because the refusal happens before the attempt leaves this process,
it is not counted as a write attempt.

Fingerprints are computed over the immutable numeric issue id, not the
caller's spelling of the issue key, so ``abc-1``, ``ABC-1``, and a key the
issue has since been moved away from all resolve to one replay identity.
Planning performs no Jira call, so a plan built from a key labels its
fingerprints provisional and apply recomputes them from the live id.

GitHub remains the sole pull request, check, review, and merge-gate
authority. Nothing here reads or changes gate state, and a Jira refusal,
conflict, rate limit, or outage cannot weaken a gate decision.

``write_request_count`` is counted at the transport attempt boundary, so it
reports every write attempt an apply made -- including attempts that timed
out, were rejected, or were retried -- rather than only the ones that
answered success. Reads are not counted.

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
import secrets
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

#: Issue property that carries this module's advisory effect ledger.
#:
#: The ledger is a bounded, shared, evicting record of effects that are all
#: reconcilable from live Jira state (assignee, status, remote-link
#: ``globalId``). Losing an entry to eviction therefore costs nothing: the
#: next apply re-reads live state and reaches the same answer. It is never a
#: safety primitive and never gates a write. Comment replay protection used
#: to live here and does not any more -- see ``COMMENT_CLAIM_PREFIX``.
LEDGER_PROPERTY_KEY = "code-mower-mutations-v1"
MAX_LEDGER_ENTRIES = 32

#: Operations the shared ledger records. Comments are deliberately absent:
#: nothing about a comment can be reconciled from live state, so a bounded
#: evicting store must never be the thing that decides whether one was
#: already posted.
LEDGER_OPERATIONS = ("assign", "transition", "link")

#: Closed ledger states. Every recorded effect is one this apply verified
#: against live Jira state, so ``applied`` is the only state it can hold.
LEDGER_STATES = ("applied",)

#: Per-comment claim property prefix. Each distinct comment intent gets its
#: own issue property, keyed by that intent's fingerprint:
#: ``code-mower-comment-v1.<fingerprint>``.
#:
#: Jira answers ``PUT`` of an issue property with 201 when it created the
#: value and 200 when it replaced one, and that create/update distinction is
#: the only at-most-once primitive Jira offers a comment post. Only a 201
#: acquires the right to post. A 200, or a value that was already present,
#: means some other apply holds the claim, and this one never posts.
#:
#: One property per intent is what makes this correct where the shared
#: ledger was not: nothing evicts, so a 33rd comment on an issue cannot push
#: an older comment's protection out and let it repost, and two concurrent
#: applies of the same intent contend on one key instead of both reading an
#: absent ledger and both posting.
COMMENT_CLAIM_PREFIX = "code-mower-comment-v1."
COMMENT_CLAIM_SCHEMA = "code_mower.jiraCommentClaim.v1"

#: Closed comment-claim states. ``claimed`` is written by the 201 that
#: acquired the claim, immediately before the post. ``posted`` is written
#: after a post this process saw succeed. ``unverified`` records a post whose
#: outcome this process could not determine. Only ``posted`` ever reads back
#: as "the comment is there"; the other two need one owner reconciliation and
#: are never reposted automatically.
COMMENT_CLAIM_STATES = ("claimed", "posted", "unverified")

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
_ISSUE_ID_RE = re.compile(r"^[0-9]{1,32}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{32}$")
_CLAIM_OWNER_RE = re.compile(r"^[0-9a-f]{16,64}$")
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
        "transition_target_mismatch",
        "target_status_not_configured",
        "already_assigned",
        "already_at_target_status",
        "already_commented",
        "already_linked",
        "comment_unverified",
        "comment_claim_held",
        "comment_claim_unconfirmed",
        "issue_identity_unresolved",
        "account_unresolved",
        "issue_out_of_scope",
        "write_guard_unarmed",
        "permission_denied",
        "unauthorized",
        "not_found",
        "conflict",
        "rate_limited",
        "unavailable",
        "rejected",
        "cancelled",
        "aborted_after_failure",
        "aborted_before_apply",
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
#: ``unverified`` outranks ``refused`` because it is the only outcome that
#: leaves Jira in a state this tool cannot describe, and it is the one that
#: needs an owner to look.
_STATUS_SEVERITY = (
    "cancelled",
    "failed",
    "blocked",
    "unverified",
    "refused",
    "skipped",
    "applied",
    "already_applied",
    "planned",
)


class MutationRequestError(ValueError):
    """A malformed config or request, carrying a bounded operator message."""


#: Closed vocabulary for a transport-boundary write refusal. Anything else
#: collapses to ``issue_out_of_scope``, so an unexpected value can only ever
#: make the refusal broader, never narrower.
SCOPE_REFUSAL_REASONS = frozenset(
    {"issue_out_of_scope", "issue_identity_unresolved", "write_guard_unarmed"}
)


class WriteScopeRefused(Exception):
    """One physical write refused by the transport before it was sent.

    Deliberately not a :class:`ValueError` and not a
    :class:`~code_mower.jira_cloud.JiraApiError`: it is neither a malformed
    request nor a transport failure, and it must not be swallowed by the
    handlers that absorb either. It carries one closed reason code and
    nothing else -- no path, no live Jira value, no response.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason if reason in SCOPE_REFUSAL_REASONS else "issue_out_of_scope"
        super().__init__(self.reason)


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


def comment_claim_property_key(fingerprint: str) -> str:
    """Return the dedicated issue-property key for one comment intent."""
    if not _FINGERPRINT_RE.fullmatch(str(fingerprint or "")):
        raise MutationRequestError("comment fingerprint must be 32 lowercase hex characters")
    return jira_cloud.validate_property_key(COMMENT_CLAIM_PREFIX + fingerprint)


def fingerprint_subject(issue_id: str) -> str:
    """Return the issue identity a fingerprint is computed over.

    A Jira issue key is mutable: a project rename or a move rewrites it, and
    an operator may type it in any case. Only the numeric issue id is
    immutable, so it is what a replay key must be built from. Plan time may
    not have it -- resolving one costs a Jira read, and planning performs no
    Jira call at all -- so a plan built from a key carries a provisional
    fingerprint and says so, and apply recomputes from the live id it read
    immediately before writing.
    """
    subject = str(issue_id or "").strip()
    return subject.upper()


def _operation_fingerprint(
    operation: str, subject: str, detail: Mapping[str, Any]
) -> str:
    """Return the replay fingerprint for one operation against one issue.

    Returns ``""`` when the detail does not carry what the operation needs,
    which is how a refused operation stays fingerprint-free.
    """
    if not subject:
        return ""
    if operation == "assign":
        payload: dict[str, Any] = {"issue": subject, "assignee": "self"}
    elif operation == "transition":
        transition_id = str(detail.get("transition_id") or "")
        if not transition_id:
            return ""
        payload = {"issue": subject, "transition_id": transition_id}
    elif operation == "link":
        global_id = str(detail.get("global_id") or "")
        if not global_id:
            return ""
        payload = {"issue": subject, "global_id": global_id}
    elif operation == "comment":
        template = str(detail.get("template") or "")
        if template not in COMMENT_TEMPLATES:
            return ""
        # A comment intent is identified semantically -- the closed template
        # id plus the canonical identity of the pull request it refers to --
        # and never by its rendered prose. Fingerprinting the rendered text
        # would fold two things into the replay key that do not belong in
        # it: how an operator happened to spell the URL, so that
        # github.com/Owner/Repo and github.com/owner/repo would claim two
        # different keys and post the same comment on one pull request
        # twice; and the wording of the template, so that reflowing a
        # sentence in this file would silently unprotect every comment an
        # earlier build already posted.
        pull_request = ""
        if template in TEMPLATES_REQUIRING_PR:
            try:
                parsed = parse_pull_request_url(str(detail.get("pr_url") or ""))
            except MutationRequestError:  # pragma: no cover - plan validated it
                return ""
            pull_request = parsed["global_id"]
        payload = {
            "issue": subject,
            "template": template,
            "pull_request": pull_request,
        }
    else:  # pragma: no cover - handlers are a closed table
        return ""
    return mutation_fingerprint(operation, payload)


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
        )

    if request.transition_category:
        category = request.transition_category
        if category not in LIFECYCLE_CATEGORIES:
            raise MutationRequestError(
                f"--transition must be one of {sorted(LIFECYCLE_CATEGORIES)}"
            )
        transition_id = settings.transitions.get(category, "")
        target_status_ids = tuple(settings.status_category_map.get(category, ()))
        if not transition_id:
            requested["transition"] = _Operation(
                operation="transition",
                status="refused",
                reason="transition_not_configured",
                detail={"lifecycle_category": category},
            )
        elif not target_status_ids:
            # A transition id with no configured target status ids is only
            # half a configuration, and which half is missing is knowable
            # here -- from config alone, before any Jira request. The
            # category's configured status ids are what "the requested
            # lifecycle category" means, so without them there is nothing to
            # verify the edge's destination against and no target to already
            # be at. Refusing at plan time is what keeps a combined
            # ``--claim --transition <category> --apply`` from assigning the
            # issue and only then discovering the gap: whole-plan preflight
            # aborts the run before its first read. Apply keeps the same
            # check as a last line of defence for a hand-supplied plan.
            requested["transition"] = _Operation(
                operation="transition",
                status="refused",
                reason="target_status_not_configured",
                detail={
                    "lifecycle_category": category,
                    "target_status_ids": [],
                },
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
                    "target_status_ids": list(target_status_ids),
                },
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
        )

    if not requested:
        raise MutationRequestError(
            "request at least one of --claim, --transition, --comment, or --link-pr"
        )

    subject = fingerprint_subject(request.issue_ref)
    operations: list[_Operation] = []
    for name in MUTATION_ORDER:
        operation = requested.get(name)
        if operation is None:
            continue
        if name not in settings.allowed_operations and operation.status == "planned":
            operation.status = "refused"
            operation.reason = "operation_not_allowed"
        operation.fingerprint = _operation_fingerprint(name, subject, operation.detail)
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
            "transition id, plus the status_category_map ids that lifecycle "
            "category means, where required), then re-run."
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
    if status == "unverified":
        return (
            "A comment intent on this issue is claimed but unconfirmed -- an "
            "interrupted run, an ambiguous post, or another apply holding the "
            "claim -- so Code Mower will never repost it. Open the issue once, "
            "and add the note by hand only if it is missing. Every other "
            "operation in this report reflects verified live state."
        )
    if status == "blocked":
        return (
            "Re-check Jira permissions, the configured transition id and its target "
            "status ids, and the current issue state, then re-run with --apply. "
            "Applied operations are not repeated."
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
            # Fingerprints are only replay-safe when they are computed over
            # the immutable numeric issue id. Planning performs no Jira call,
            # so a plan given an issue *key* says its fingerprints are
            # provisional; apply resolves the live id and recomputes them.
            "fingerprint_basis": (
                "issue_id"
                if _ISSUE_ID_RE.fullmatch(issue_ref)
                else "issue_ref_provisional"
            ),
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
    (
        "PUT",
        re.compile(
            r"^/rest/api/3/issue/[^/]+/properties/"
            + re.escape(COMMENT_CLAIM_PREFIX)
            + r"[0-9a-f]{32}$"
        ),
    ),
)

#: Every write this surface can issue is scoped to one issue by its path.
#: The armed issue reference is matched against that segment so a write can
#: never land on an issue other than the one preflight authorized.
_ISSUE_WRITE_PATH_RE = re.compile(r"^/rest/api/3/issue/(?P<ref>[^/]+)(?:/.*)?$")


class JiraMutationClient(jira_cloud.JiraReadClient):
    """Read client widened to a closed, guarded Jira write allow-list.

    Constructing this class does not authorize anything: callers reach it
    only after :func:`build_mutation_plan` reports ``mode == "apply"``, which
    requires both the repository write guard and the runtime apply flag, and
    then only after :meth:`arm_for_issue` binds this client to one proven
    issue identity. An unarmed client refuses every write.

    The allow-list is the last line of defence on *what* may be written.
    DELETE is rejected for every path, issue edits and attachments have no
    entry, and the only writable issue property is this module's ledger.
    Comment and remote-link bodies are built inside this class from a
    template id and a validated GitHub pull request URL, so no caller-supplied
    prose can reach Jira.

    The armed write scope is the last line of defence on *where*: it is
    re-proven with a fresh read immediately before every write attempt, so
    an issue that leaves the configured project, or a reference that starts
    resolving to a different issue, costs zero further writes -- whether it
    happens between two operations or between two writes of one operation.
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

    def __post_init__(self) -> None:
        super().__post_init__()
        # Every write attempt this client makes, counted at the transport
        # boundary rather than at the call site, so ambiguous and retried
        # attempts are included. Apply reports the delta across one run.
        self.write_attempts = 0
        # The armed write scope: (issue_ref, issue_id, project_id), or None.
        # A fresh client is unarmed, so it can only read.
        self._write_scope: tuple[str, str, str] | None = None
        # Set while the scope re-read is in flight. The re-read is a GET and
        # therefore unguarded, but the flag keeps the invariant from being
        # able to recurse even if that ever stops being true.
        self._scope_check_active = False

    # -- Armed write scope -----------------------------------------------

    def arm_for_issue(self, *, issue_ref: str, issue_id: str, project_id: str) -> None:
        """Authorize writes against exactly one issue in one project.

        Called once, after preflight has read live state and proved all
        three values. Every later write attempt is re-checked against them,
        so this is the single place a run says which issue it may touch.
        """
        try:
            ref = jira_cloud.validate_issue_ref(issue_ref)
        except ValueError:
            raise MutationRequestError("armed issue reference is malformed") from None
        if not _ISSUE_ID_RE.fullmatch(str(issue_id or "")):
            raise MutationRequestError(
                "armed issue id must be the immutable numeric issue id"
            )
        if not jira_cloud._PROJECT_ID_RE.fullmatch(str(project_id or "")):
            raise MutationRequestError(
                "armed project id must be the immutable numeric project id"
            )
        self._write_scope = (ref, str(issue_id), str(project_id))

    def disarm(self) -> None:
        """Withdraw write authorization; every later write is refused."""
        self._write_scope = None

    def _require_write_scope(self, path: str) -> None:
        """Re-prove the armed scope, or refuse, before one physical write.

        This runs on the attempt path itself rather than in a handler, which
        is the whole point: a handler makes several writes (a comment claims,
        posts, and finalizes) and the apply ends with a ledger PUT, so an
        invariant each handler had to remember would be one handler away from
        being wrong again.

        The read is fresh every time -- nothing here is cached -- because the
        window this closes is exactly the one where Jira changed underneath a
        value this process already read. Reads keep their bounded retry
        budget, so a flaky refresh does not refuse a write on its own; a read
        that ultimately fails raises its closed transport code and the write
        still never leaves.
        """
        scope = self._write_scope
        if scope is None:
            raise WriteScopeRefused("write_guard_unarmed")
        issue_ref, issue_id, project_id = scope
        match = _ISSUE_WRITE_PATH_RE.fullmatch(path)
        if match is None or urllib.parse.unquote(match.group("ref")) != issue_ref:
            # A write aimed anywhere but the armed issue is out of scope by
            # construction, and no amount of reading could bring it in.
            raise WriteScopeRefused("issue_out_of_scope")
        if self._scope_check_active:  # pragma: no cover - the re-read is a GET
            return
        self._scope_check_active = True
        try:
            state = self.get_issue_state(issue_ref)
        finally:
            self._scope_check_active = False
        violation = _issue_scope_violation(state, project_id, issue_id)
        if violation is not None:
            raise WriteScopeRefused(violation[1])

    @staticmethod
    def _is_write_request(method: str, path: str) -> bool:
        """Report whether one request can change Jira."""
        if method == "GET":
            return False
        return not (method == "POST" and path in jira_cloud.READ_ONLY_POST_PATHS)

    def _on_request_attempt(self, method: str, path: str) -> None:
        """Guard, then count, every write attempt leaving this process.

        This fires per HTTP attempt, immediately before the runner is
        invoked, which is the only place that sees *every* physical write --
        including the second and third write of one operation, and the
        advisory ledger PUT that happens after the last one. So it is where
        the armed scope is re-proven: a refusal here means the attempt never
        left, and it is not counted, because nothing at Jira could have
        changed.

        Once the write is authorized it is counted before its outcome is
        known, so a write that timed out, was rate limited, was rejected, or
        was retried is counted every time it was tried. That is the only
        count an operator can trust: an attempt Jira may have committed and
        then failed to acknowledge changed Jira just as much as one that
        returned 201. Reads are not counted -- they cannot change anything.
        """
        if not self._is_write_request(method, path):
            return
        self._require_write_scope(path)
        self.write_attempts += 1

    def _attempts_for(self, method: str, path: str, endpoint: str = "") -> int:
        """Retry reads only; every write requires fresh authorization."""
        if self._is_write_request(method, path):
            return 1
        return super()._attempts_for(method, path, endpoint)

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
        """Write the bounded advisory ledger to this module's own property."""
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

    # -- Comment claim (at-most-once primitive) --------------------------

    def get_comment_claim(
        self, issue_ref: str, fingerprint: str
    ) -> dict[str, Any] | None:
        """Read one comment intent's claim property, or None when unclaimed."""
        return self.get_issue_property(
            issue_ref, comment_claim_property_key(fingerprint)
        )

    def _put_comment_claim(
        self,
        issue_ref: str,
        fingerprint: str,
        payload: Mapping[str, Any],
        *,
        endpoint: str,
    ) -> int:
        quoted_key = urllib.parse.quote(
            comment_claim_property_key(fingerprint), safe=""
        )
        status, _ = self._request_status_parsed(
            "PUT",
            self._issue_path(issue_ref, f"properties/{quoted_key}"),
            json_body=dict(payload),
            endpoint=endpoint,
            allow_empty=True,
        )
        return int(status)

    def acquire_comment_claim(
        self, issue_ref: str, fingerprint: str, *, template: str, owner: str
    ) -> bool:
        """Try to acquire the exclusive right to post one comment.

        Returns True only when Jira answered 201, meaning this request is the
        one that created the property. A 200 means the value was already
        there, so another apply owns the claim and this one must never post.
        """
        payload = _comment_claim_payload(
            fingerprint, template=template, owner=owner, state="claimed"
        )
        return self._put_comment_claim(
            issue_ref, fingerprint, payload, endpoint="commentClaimAcquire"
        ) == 201

    def finalize_comment_claim(
        self, issue_ref: str, fingerprint: str, *, template: str, owner: str, state: str
    ) -> None:
        """Record the outcome of a post on a claim this process already owns.

        Rewriting a key this process holds is a whole-value PUT, but it still
        gets one transport attempt so a retry cannot outlive its scope check.
        """
        payload = _comment_claim_payload(
            fingerprint, template=template, owner=owner, state=state
        )
        self._put_comment_claim(
            issue_ref, fingerprint, payload, endpoint="commentClaimFinalize"
        )


# -- Comment claims and the advisory ledger ------------------------------


def _claim_owner_token() -> str:
    """Return a fresh, meaningless owner token for one claim attempt.

    The token identifies nothing about the machine, operator, repository, or
    run beyond "this attempt": it exists only so a process whose claim write
    failed ambiguously can read the property back and tell its own creation
    apart from another apply's.
    """
    return secrets.token_hex(16)


def _comment_claim_payload(
    fingerprint: str, *, template: str, owner: str, state: str
) -> dict[str, Any]:
    """Build the bounded claim property value. Metadata only.

    Deliberately absent: the rendered comment text, the pull request URL, any
    issue prose, and any local identity. A reader learns which closed
    template was intended, whether it posted, and nothing else.
    """
    if not _FINGERPRINT_RE.fullmatch(str(fingerprint or "")):
        raise MutationRequestError("comment fingerprint must be 32 lowercase hex characters")
    if template not in COMMENT_TEMPLATES:
        raise MutationRequestError("comment template is not in the closed table")
    if state not in COMMENT_CLAIM_STATES:
        raise MutationRequestError("comment claim state is not a closed state")
    if not _CLAIM_OWNER_RE.fullmatch(str(owner or "")):
        raise MutationRequestError("comment claim owner token is malformed")
    return {
        "schema": COMMENT_CLAIM_SCHEMA,
        "operation": "comment",
        "fingerprint": fingerprint,
        "template": template,
        "state": state,
        "owner": owner,
        "at": _utc_now(),
    }


def _claim_state(claim: Mapping[str, Any] | None) -> str:
    """Return a claim's closed state, or ``"unknown"`` for anything else.

    An unreadable or hand-edited value is treated as a held claim in an
    unknown state, never as an absent one: guessing "absent" here is the one
    mistake that reposts a comment.
    """
    if not isinstance(claim, Mapping):
        return ""
    state = str(claim.get("state") or "")
    return state if state in COMMENT_CLAIM_STATES else "unknown"


def _validated_ledger(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Return a bounded ledger payload, dropping anything unrecognized.

    The ledger is written to a Jira issue property, so it is held to the same
    metadata-only bar as every other emitted field: closed keys, closed
    states, fingerprints, and timestamps only.

    Comment entries are dropped, including any written by an earlier build of
    this module. Comment replay protection lives in a dedicated per-intent
    claim property now, and leaving comment rows in a shared 32-entry store
    that evicts by age would only invite them to be trusted again.
    """
    entries_in = ledger.get("entries")
    entries: dict[str, dict[str, str]] = {}
    if isinstance(entries_in, Mapping):
        for fingerprint, entry in entries_in.items():
            if not isinstance(entry, Mapping) or not isinstance(fingerprint, str):
                continue
            if not _FINGERPRINT_RE.fullmatch(fingerprint):
                continue
            operation = str(entry.get("operation") or "")
            state = str(entry.get("state") or "")
            if operation not in LEDGER_OPERATIONS or state not in LEDGER_STATES:
                continue
            entries[fingerprint] = {
                "operation": operation,
                "state": state,
                "at": jira_cloud._bounded_str(entry.get("at"), 32),
            }
    # Newest first, with the fingerprint breaking ties, so two effects
    # recorded in the same second evict in a defined order rather than
    # whichever one the mapping happened to yield first.
    trimmed = sorted(
        entries.items(), key=lambda item: (item[1]["at"], item[0]), reverse=True
    )
    return {
        "schema": LEDGER_SCHEMA,
        "entries": dict(trimmed[:MAX_LEDGER_ENTRIES]),
    }


def _ledger_key(ledger: Mapping[str, Any]) -> str:
    """Return a comparable form of the ledger, ignoring timestamps."""
    entries = ledger.get("entries")
    rows = entries.items() if isinstance(entries, Mapping) else ()
    return json.dumps(
        sorted(
            (fingerprint, str(entry.get("state") or ""))
            for fingerprint, entry in rows
            if isinstance(entry, Mapping)
        ),
        separators=(",", ":"),
    )


def _record(ledger: dict[str, Any], operation: _Operation, state: str) -> dict[str, Any]:
    """Note one live-reconcilable effect in the advisory ledger."""
    if operation.operation not in LEDGER_OPERATIONS or not operation.fingerprint:
        return ledger
    entries = dict(ledger.get("entries") or {})
    entries[operation.fingerprint] = {
        "operation": operation.operation,
        "state": state,
        "at": _utc_now(),
    }
    return _validated_ledger({"entries": entries})


# -- Apply ---------------------------------------------------------------


#: Operation outcomes the run may continue past. ``unverified`` is included
#: so the ledger entry recording that unknown state is still persisted; the
#: report status still carries it to the operator.
_CONTINUING_STATUSES = ("applied", "already_applied", "unverified")

#: Operation outcomes that condemn the whole apply before it starts. Any one
#: of these on any requested operation means the plan was never authorized
#: in full, so no sibling operation may be written.
_PREFLIGHT_ABORT_STATUSES = ("refused", "blocked", "cancelled", "failed")


def _fail_from_error(operation: _Operation, exc: jira_cloud.JiraApiError) -> None:
    status, reason = _ERROR_CODE_OUTCOMES.get(exc.code, ("failed", "unavailable"))
    operation.status = status
    operation.reason = reason


def _issue_scope_violation(
    state: Mapping[str, Any], project_id: str, expected_issue_id: str = ""
) -> tuple[str, str] | None:
    """Say why this live issue may not be written, or ``None`` when it may.

    Authorization here is proven, never assumed. An absent, empty, or
    unreadable live project id is *not* evidence that the issue sits in the
    configured project, so it is refused rather than waved through: a field
    that did not come back, or an issue Jira moved to a project this
    repository never configured, must both cost zero writes. The configured
    project id must itself be present and match exactly.

    ``expected_issue_id`` is supplied once identity is established, so a
    refresh that resolves the same reference to a different issue -- a key
    reused after a move -- stops the run instead of writing to a stranger.
    """
    live_project = str(state.get("project_id") or "")
    if not project_id or live_project != project_id:
        return ("blocked", "issue_out_of_scope")
    live_id = str(state.get("id") or "")
    if not _ISSUE_ID_RE.fullmatch(live_id):
        # Without the immutable id there is no replay-safe fingerprint, and
        # guessing one from the caller's spelling of the key is exactly the
        # mistake that lets a renamed or differently-cased issue repost.
        return ("blocked", "issue_identity_unresolved")
    if expected_issue_id and live_id != expected_issue_id:
        return ("blocked", "issue_identity_unresolved")
    return None


def apply_mutation_plan(
    plan: Mapping[str, Any], client: JiraMutationClient
) -> dict[str, Any]:
    """Execute an authorized plan against live Jira, idempotently.

    ``plan`` must have ``mode == "apply"``; anything else is returned
    unchanged so a refusal can never be escalated into a write. A plan whose
    requested operations are not all still applicable fails closed as a
    whole, before any Jira call. Live issue state and available transitions
    are re-read immediately before *each* operation, not once per apply, so
    workflow or permission drift -- including drift an earlier operation in
    this same plan provoked -- blocks instead of guessing.
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
    # Counted at the transport boundary, so the report covers every write
    # attempt this apply made -- including ones that timed out, were
    # rejected, or were retried -- not only the ones that answered success.
    writes_before = client.write_attempts

    def _finish(ledger_status: str) -> dict[str, Any]:
        report["operations"] = [operation.as_dict() for operation in operations]
        report["write_request_count"] = client.write_attempts - writes_before
        report["ledger_status"] = ledger_status
        report["status"] = _report_status(operations)
        report["next_action"] = _next_action(
            "apply", report["status"], writes_enabled=True, apply_requested=True
        )
        return report

    # A plan is authorized as a whole or not at all. One operation the plan
    # already refused -- an unconfigured transition, an operation outside
    # allowed_operations -- or one carried over as blocked, cancelled, or
    # failed condemns the whole request, so the allowed remainder must not
    # be executed as a partial apply the operator never asked for. Fail
    # closed here, before the first Jira read, so a refused request costs
    # zero HTTP calls.
    if any(operation.status in _PREFLIGHT_ABORT_STATUSES for operation in operations):
        for operation in pending:
            operation.status = "skipped"
            operation.reason = "aborted_before_apply"
        return _finish("unchanged")

    if not pending:
        return _finish("unchanged")

    def _abort(ledger_status: str = "unchanged") -> dict[str, Any]:
        for operation in pending:
            if operation.status == "planned":
                operation.status = "skipped"
                operation.reason = "aborted_after_failure"
        return _finish(ledger_status)

    # Whole-plan preflight: resolve the immutable identity, prove the issue is
    # in scope, and read the ledger once -- all before the first write, so a
    # request this apply may not perform costs zero write attempts.
    try:
        state = client.get_issue_state(issue_ref)
        ledger = _validated_ledger(
            client.get_issue_property(issue_ref, LEDGER_PROPERTY_KEY) or {}
        )
    except jira_cloud.JiraApiError as exc:
        for operation in pending:
            _fail_from_error(operation, exc)
        return _finish("unchanged")

    ledger_before = _ledger_key(ledger)

    violation = _issue_scope_violation(state, settings_project)
    if violation is not None:
        for operation in pending:
            operation.status, operation.reason = violation
        return _finish("unchanged")

    issue_id = str(state.get("id") or "")
    report["tracker"]["issue_id"] = issue_id
    report["tracker"]["issue_key"] = state.get("key", "")
    report["tracker"]["fingerprint_basis"] = "issue_id"

    # Recompute every fingerprint over the immutable live issue id. The plan
    # may have been built from a key the operator typed -- "abc-1", "ABC-1",
    # or a key this issue has since been moved away from -- and all of those
    # must resolve to the one replay identity this issue actually has.
    for operation in pending:
        refreshed = _operation_fingerprint(
            operation.operation, fingerprint_subject(issue_id), operation.detail
        )
        if refreshed:
            operation.fingerprint = refreshed

    # Arm the transport. Until this call the client can only read, and from
    # here every physical write it makes -- each of an operation's several
    # writes, and the advisory ledger PUT after the last operation -- re-reads
    # this issue and refuses unless the immutable id and the configured
    # project id both still match. The invariant lives there, at the attempt
    # boundary, precisely so no handler has to remember to ask.
    try:
        client.arm_for_issue(
            issue_ref=issue_ref, issue_id=issue_id, project_id=settings_project
        )
    except MutationRequestError:
        # Preflight matched a project id this transport will not accept as an
        # immutable numeric id, so nothing here is authorized to write.
        for operation in pending:
            operation.status = "blocked"
            operation.reason = "issue_out_of_scope"
        return _finish("unchanged")
    try:
        return _apply_pending(
            client, issue_ref, settings_project, issue_id,
            pending, ledger, ledger_before, _finish, _abort,
        )
    finally:
        # One armed scope belongs to one apply. A client handed to a second
        # run must be armed again from that run's own preflight.
        client.disarm()


def _apply_pending(
    client: JiraMutationClient,
    issue_ref: str,
    settings_project: str,
    issue_id: str,
    pending: Sequence[_Operation],
    ledger: dict[str, Any],
    ledger_before: str,
    _finish: Callable[[str], dict[str, Any]],
    _abort: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Run the armed operations, then persist the advisory ledger.

    Each handler owns its own idempotency: assign, transition, and link
    reconcile from authoritative live state, and comment acquires its own
    per-intent claim property immediately before it posts. Nothing records a
    comment intent before the comment step runs, so an assignment,
    transition, or link that fails first cannot leave a claim behind for a
    comment that was never attempted.
    """
    for operation in pending:
        # One plan's operations are not one atomic Jira change, and Jira does
        # not hold still between them: a project automation rule can fire on
        # the assignment this loop just made and move the issue's status, or
        # move the issue into another project entirely. So authoritative state
        # is re-read and re-validated immediately before every operation, and
        # no handler ever decides from a snapshot an earlier write may have
        # invalidated. A refresh that fails, leaves the configured project, or
        # resolves to a different issue stops the run here -- the remaining
        # operations are skipped without another mutation.
        try:
            state = client.get_issue_state(issue_ref)
        except jira_cloud.JiraApiError as exc:
            _fail_from_error(operation, exc)
            return _abort()
        violation = _issue_scope_violation(state, settings_project, issue_id)
        if violation is not None:
            operation.status, operation.reason = violation
            return _abort()
        try:
            handler = _HANDLERS[operation.operation]
            ledger = handler(operation, client, issue_ref, state, ledger)
        except WriteScopeRefused as refusal:
            # The transport refused a write because the issue left the armed
            # scope after this operation's own state read -- possibly between
            # two writes this operation was already making. Nothing was sent.
            # A handler that had already recorded a truthful outcome, such as
            # a comment holding a claim it did acquire, keeps it; anything
            # still merely planned becomes the refusal itself.
            if operation.status == "planned":
                operation.status = "blocked"
                operation.reason = refusal.reason
            operation.detail["scope_refusal"] = refusal.reason
            # The advisory ledger PUT is a write too, and it is exactly as
            # out of scope as the one just refused, so it is not attempted.
            return _abort(f"refused_{refusal.reason}")
        except jira_cloud.JiraApiError as exc:
            _fail_from_error(operation, exc)
            return _abort()
        except MutationRequestError:
            operation.status = "blocked"
            operation.reason = "rejected"
            return _abort()
        if operation.status not in _CONTINUING_STATUSES:
            return _abort()

    if _ledger_key(ledger) == ledger_before:
        # Nothing live-reconcilable changed, so there is nothing to record.
        # Rewriting an identical property would only spend a write attempt.
        return _finish("unchanged")

    ledger_status = "written"
    try:
        client.set_mutation_ledger(issue_ref, ledger)
    except WriteScopeRefused as refusal:
        # The ledger PUT is a physical write like any other and sits outside
        # every per-operation check, so it is guarded by the same transport
        # invariant. An issue that left scope after the last operation does
        # not get one more write on the way out.
        ledger_status = f"refused_{refusal.reason}"
    except jira_cloud.JiraApiError:
        # Every effect the ledger records is reconciled from live state on
        # the next run, and comment replay protection does not live here at
        # all, so a failed ledger write is a reporting gap and never a
        # duplication risk.
        ledger_status = "write_failed"
    return _finish(ledger_status)


def _apply_assign(
    operation: _Operation,
    client: JiraMutationClient,
    issue_ref: str,
    state: Mapping[str, Any],
    ledger: dict[str, Any],
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
    operation.status = "applied"
    operation.reason = "ok"
    return _record(ledger, operation, "applied")


def _apply_transition(
    operation: _Operation,
    client: JiraMutationClient,
    issue_ref: str,
    state: Mapping[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    transition_id = str(operation.detail.get("transition_id") or "")
    category = str(operation.detail.get("lifecycle_category") or "")
    current_status = str(state.get("status_id") or "")
    target_ids = set(operation.detail.get("target_status_ids") or ())

    # The configured target status ids are what "the requested lifecycle
    # category" means here, so nothing -- not a write, and not an
    # already-at-target answer -- may be decided before them. Without them
    # there is no target to be at. Planning already refuses this case, so a
    # plan built by this module never arrives here; the check stays as the
    # last line of defence for a plan supplied directly to this function.
    if not target_ids:
        operation.status = "blocked"
        operation.reason = "target_status_not_configured"
        return ledger
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

    # A configured transition id is only a workflow edge, and a workflow can
    # be re-pointed underneath it. Verify where this edge actually lands
    # against the status ids configured for the requested lifecycle category
    # before writing, so a re-pointed transition blocks rather than moving the
    # issue somewhere the requested category never meant.
    #
    # A self-transition -- an edge whose destination is the status the issue
    # already holds -- is checked by exactly this rule and nothing else.
    # Control only reaches here when the current status is outside the
    # configured target ids, so a destination equal to it is outside them
    # too: it is a non-target destination that happens to be where the issue
    # already sits, and it blocks. Answering "already at target status"
    # ahead of this check would have called a status the category never
    # names a target, and reported a re-pointed workflow as success.
    destination = str(match.get("to_status_id") or "")
    if destination not in target_ids:
        operation.status = "blocked"
        operation.reason = "transition_target_mismatch"
        operation.detail["destination_status_id"] = destination
        return ledger

    client.transition_issue(issue_ref, transition_id)
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
) -> dict[str, Any]:
    if client.has_remote_link(issue_ref, str(operation.detail.get("global_id") or "")):
        operation.status = "already_applied"
        operation.reason = "already_linked"
        return _record(ledger, operation, "applied")
    client.link_pull_request(issue_ref, str(operation.detail.get("url") or ""))
    operation.status = "applied"
    operation.reason = "ok"
    return _record(ledger, operation, "applied")


def _apply_comment(
    operation: _Operation,
    client: JiraMutationClient,
    issue_ref: str,
    state: Mapping[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """Post one templated comment at most once, ever.

    A comment is the only effect here with no server-side idempotency key and
    nothing in live Jira to reconcile against: this module never reads
    comment bodies back, so "is it already there?" cannot be answered by
    looking. The answer instead comes from a dedicated issue property named
    after this comment intent's fingerprint. Creating that property is the
    claim, and Jira's 201-created versus 200-updated answer is what makes the
    claim exclusive between two applies racing on the same intent.
    """
    fingerprint = operation.fingerprint
    template = str(operation.detail.get("template") or "")
    pr_url = str(operation.detail.get("pr_url") or "")
    if not _FINGERPRINT_RE.fullmatch(fingerprint) or template not in COMMENT_TEMPLATES:
        operation.status = "blocked"
        operation.reason = "rejected"
        return ledger

    claim = client.get_comment_claim(issue_ref, fingerprint)
    if claim is not None:
        claimed_state = _claim_state(claim)
        operation.detail["claim_state"] = claimed_state
        if claimed_state == "posted":
            operation.status = "already_applied"
            operation.reason = "already_commented"
        else:
            # The claim exists but no run ever recorded a completed post. It
            # may have committed, been lost in flight, or never left. This
            # tool cannot tell, so it says so and never reposts.
            operation.status = "unverified"
            operation.reason = "comment_unverified"
        return ledger

    owner = _claim_owner_token()
    try:
        acquired = client.acquire_comment_claim(
            issue_ref, fingerprint, template=template, owner=owner
        )
    except jira_cloud.JiraApiError as exc:
        # The acquire is attempted exactly once and its answer was lost, so
        # the one fact that authorizes a post -- Jira answering 201 Created
        # rather than 200 OK -- is gone for good. A readback cannot recover
        # it. Finding this attempt's own owner token stored proves only that
        # the PUT landed, and a PUT that landed as a 200 overwrote a claim
        # another apply already held, possibly for a comment that apply had
        # already posted. So the readback classifies the outcome and never
        # unlocks the post.
        outcome = _recover_comment_claim(client, issue_ref, fingerprint, owner)
        if outcome == "unknown":
            # Nothing readable is claimed. This run failed with the transport
            # reason and a later run may claim the intent cleanly.
            _fail_from_error(operation, exc)
            operation.detail["claim_state"] = "unknown"
            return ledger
        operation.status = "unverified"
        if outcome == "self":
            # This attempt's token is stored, but whether it created the
            # claim or replaced someone else's is unknowable. The claim
            # stays held, so the comment is never posted by any later run
            # either, and one owner reconciles it.
            operation.reason = "comment_claim_unconfirmed"
            operation.detail["claim_state"] = "claimed_unconfirmed"
        else:
            operation.reason = "comment_claim_held"
            operation.detail["claim_state"] = "held_by_another_apply"
        return ledger

    if not acquired:
        # Another apply created the property first. It owns this comment,
        # including the duty to report whether it landed.
        operation.status = "unverified"
        operation.reason = "comment_claim_held"
        operation.detail["claim_state"] = "held_by_another_apply"
        return ledger

    # Recorded before the post, and before the finalization that follows it,
    # because both are guarded writes that can be refused. Whatever happens
    # from here, "this process holds the claim" is the fact that stands
    # unless something later proves a stronger one.
    operation.detail["claim_state"] = "claimed"
    try:
        comment_id = client.add_templated_comment(
            issue_ref, template, pr_url=pr_url, fingerprint=fingerprint
        )
    except WriteScopeRefused:
        # The claim landed and then the issue left scope, so the post never
        # left this process. The claim is deliberately not released: it is
        # what keeps any later run from posting this intent, and releasing it
        # here would trade a comment that was never posted for the chance of
        # one posted twice. Nothing is replayed either. The truthful report
        # is a held, unconfirmed claim, which is exactly what a later run
        # will read back off the property.
        operation.status = "unverified"
        operation.reason = "comment_unverified"
        raise
    except jira_cloud.JiraApiError as exc:
        # The post is attempted exactly once, so this may be a request Jira
        # rejected outright or one it committed and failed to acknowledge.
        # Both end here as unverified: the claim stays held, the closed
        # transport code says why, and one owner reconciles this comment by
        # hand rather than a rerun risking a second copy.
        operation.status = "unverified"
        operation.reason = "comment_unverified"
        operation.detail["post_error"] = _ERROR_CODE_OUTCOMES.get(
            exc.code, ("failed", "unavailable")
        )[1]
        operation.detail["claim_state"] = _finalize_claim_quietly(
            client, issue_ref, fingerprint, template=template, owner=owner,
            state="unverified",
        )
        return ledger

    operation.status = "applied"
    operation.reason = "ok"
    if comment_id:
        operation.detail["comment_id"] = comment_id
    operation.detail["claim_state"] = _finalize_claim_quietly(
        client, issue_ref, fingerprint, template=template, owner=owner, state="posted"
    )
    return ledger


def _recover_comment_claim(
    client: JiraMutationClient, issue_ref: str, fingerprint: str, owner: str
) -> str:
    """Describe, never authorize, the outcome of an ambiguous claim acquire.

    This reads the claim back once and reports who holds it. It cannot
    report who *created* it: the claim property is a create-or-update PUT
    and only Jira's 201-versus-200 answer separates the two, which is
    exactly what an ambiguous acquire lost. A stored owner token matching
    this attempt is therefore equally consistent with "this write created
    the claim" and with "this write overwrote a claim another apply already
    held, for a comment that apply may already have posted". No caller may
    turn any answer here into a comment post.

    Returns ``"self"`` when this attempt's token is stored, ``"other"`` when
    another apply's is, and ``"unknown"`` when no readable claim is there --
    which covers both an absent claim and an unreadable one, because the
    property read reports them identically and neither may be guessed apart.
    """
    try:
        claim = client.get_comment_claim(issue_ref, fingerprint)
    except jira_cloud.JiraApiError:
        return "unknown"
    if not isinstance(claim, Mapping):
        return "unknown"
    return "self" if str(claim.get("owner") or "") == owner else "other"


def _finalize_claim_quietly(
    client: JiraMutationClient,
    issue_ref: str,
    fingerprint: str,
    *,
    template: str,
    owner: str,
    state: str,
) -> str:
    """Record a post outcome on a held claim; report what was persisted.

    A failure here loses only the record, never the protection: the claim
    property still exists, so the comment is still never reposted. The next
    run just reads the weaker ``claimed`` state and reports ``unverified``.

    A :class:`WriteScopeRefused` is deliberately *not* absorbed. It means the
    issue left scope after the post, which is a fact about the whole run and
    not about this one property, so it propagates and stops the apply --
    notably before the advisory ledger PUT that would otherwise follow.
    """
    try:
        client.finalize_comment_claim(
            issue_ref, fingerprint, template=template, owner=owner, state=state
        )
    except jira_cloud.JiraApiError:
        return "claimed"
    return state


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
        f"Jira write attempts: {report['write_request_count']}",
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
