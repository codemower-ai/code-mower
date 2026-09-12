"""Guided delivery and PR review lifecycle for a protected session binding."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any, Mapping

from . import context_review, context_session
from .claude_audit_pr import _decision_authorities_for_repo
from .context_contract import ContextError, ContextRequest
from .context_delivery import (
    SUPPORTED_RECIPIENTS,
    abandon_attachment,
    deliver,
    mark_published,
    read_binding,
    render_evidence,
    reserve_attachment,
)
from .context_packets import load_authorized
from .context_store import ContextStore
from .participants import PARTICIPANTS, participant_id
from .provider_runners import fetch_issue_comments, fetch_pull_request, post_pr_comment
from .provider_runners.github_auth import resolve_github_token_from_env_or_gh
from .provider_runners.github_pr import _gh_request


ATTACH_SCHEMA = "code_mower.contextSessionAttach.v1"


def _workflow_key(record: Mapping[str, Any]) -> str:
    identity = (record["repo"] + "\0" + record["work_item"]).encode()
    return "work-item-" + hashlib.sha256(identity).hexdigest()[:48]


def _github_access(repo_path: Path, base_ref: str) -> tuple[str, tuple[str, ...]]:
    authorities = _decision_authorities_for_repo(repo_path, (), trusted_ref=base_ref)
    token = resolve_github_token_from_env_or_gh()
    if not token:
        raise ContextError("GitHub authorization is required for review input metadata")
    actor = _gh_request("GET", "/user", token=token)
    allowed = {name.lower() for name in authorities}
    if str(actor.get("login", "")).lower() not in allowed:
        raise ContextError(
            "the GitHub actor must be a configured Code Mower control authority on the trusted base"
        )
    return token, tuple(authorities)


def _remote_input(
    repository: str,
    pr: int,
    *,
    token: str,
    authorities: tuple[str, ...],
) -> tuple[str, dict[str, Any] | None]:
    pull = fetch_pull_request(repository, pr, token=token)
    try:
        head = pull["head"]["sha"]
    except (KeyError, TypeError):
        raise ContextError("GitHub did not return a valid pull request head") from None
    current = context_review.latest_input(
        fetch_issue_comments(repository, pr, token=token), authorities=authorities,
    )
    return head, current


def _report(status: str, *, reused: bool, reconciled: bool = False) -> dict[str, Any]:
    return {
        "schema": ATTACH_SCHEMA,
        "status": status,
        "stage": "attached" if status == "attached" else status,
        "dependent_work": "usable" if status == "attached" else "paused",
        "owner_action": status == "attachment_uncertain",
        "reused": reused,
        "reconciled": reconciled,
        "next_action": (
            "Run the independent current-head review, then read authorized feedback."
            if status == "attached"
            else "Rerun attach to reconcile the saved publication intent."
        ),
    }


def _same_bound_session(original: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    for field in ("session_id", "repo", "work_item", "connection", "policy", "packet"):
        if original[field] != current[field]:
            raise ContextError("session context changed before the guided operation")


def _set_uncertain(store: ContextStore, record: Mapping[str, Any]) -> None:
    try:
        context_session.update(
            store,
            record["session_id"],
            expected_generation=record["generation"],
            changes={"attachment_state": "uncertain"},
        )
    except (ContextError, OSError):
        # A crash or competing local mutation leaves the persisted pending
        # intent intact. The next invocation still reconciles that revision.
        pass


def _reserve_for_record(
    association_store: ContextStore,
    packet_store: ContextStore,
    record: Mapping[str, Any],
    *,
    backend: Any,
) -> dict[str, Any]:
    try:
        return reserve_attachment(
            packet_store,
            record["connection"],
            record["packet"],
            record["policy"],
            ContextRequest(
                record["repo"], record["work_item"], record["host"] + ":orchestrator"
            ),
            pr=record["pr"],
            head=record["head"],
            revision=record["revision"],
            backend=backend,
        )
    except ContextError as exc:
        context_session.record_failure(association_store, record, exc)
        raise


def _publish(
    repository: str,
    pr: int,
    head: str,
    metadata: Mapping[str, Any],
    *,
    token: str,
) -> None:
    _gh_request(
        "POST",
        f"/repos/{repository}/statuses/{head}",
        token=token,
        body={
            "context": "code-mower/gate",
            "state": "pending",
            "description": "Context input changed; waiting for current review",
        },
    )
    post_pr_comment(
        repository,
        pr,
        context_review.INPUT_HEADER
        + "\n\nSelected evidence changed. Independent reviews must match this input and "
        "the current code head.\n\n"
        + context_review.marker(metadata),
        token=token,
    )


def attach_session(
    association_store: ContextStore,
    packet_store: ContextStore,
    record: Mapping[str, Any],
    *,
    repo_path: Path,
    pr: int,
    base_ref: str = "origin/main",
    retry_uncertain: bool = False,
    backend: Any = None,
) -> tuple[dict[str, Any], int]:
    """Attach or reconcile one fixed review-input revision for a session."""
    record = context_session.validate(record)
    if type(pr) is not int or pr < 1:
        raise ContextError("guided context attachment requires a pull request")
    if record["connection"] is None or record["packet"] is None:
        raise ContextError("prepare the selected context before attaching it")
    if record["stage"] not in {"prepared", "attached", "reviewed"}:
        raise ContextError("prepare the selected context before attaching it")
    if record["host"] not in {"claude", "codex"}:
        raise ContextError("guided private context currently supports Claude and Codex hosts")

    token, authorities = _github_access(repo_path, base_ref)
    with association_store.locked(_workflow_key(record)):
        current_record = context_session.read(association_store, record["session_id"])
        if current_record is None:
            raise ContextError("this session has no selected work item")
        _same_bound_session(record, current_record)
        record = current_record
        head, current = _remote_input(record["repo"], pr, token=token, authorities=authorities)

        if record["attachment_state"] == "published":
            if record["pr"] != pr:
                raise ContextError("this session is already attached to a different pull request")
            binding = read_binding(packet_store, record["revision"])
            if record["head"] == head:
                if current != binding["metadata"]:
                    raise ContextError(
                        "the trusted current input changed; inspect the pull request before replacing it"
                    )
                try:
                    deliver(
                        packet_store,
                        record["revision"],
                        repository=record["repo"],
                        pr=pr,
                        head=head,
                        recipient=record["host"] + ":orchestrator",
                        current=current,
                        backend=backend,
                    )
                except ContextError as exc:
                    context_session.record_failure(association_store, record, exc)
                    raise
                return _report("attached", reused=True, reconciled=True), 0

        if record["attachment_state"] in {"pending", "uncertain"}:
            if record["pr"] != pr:
                raise ContextError("a saved attachment intent targets a different pull request")
            metadata = _reserve_for_record(
                association_store, packet_store, record, backend=backend,
            )
            if current == metadata:
                mark_published(packet_store, record["connection"], record["revision"])
                record = context_session.update(
                    association_store,
                    record["session_id"],
                    expected_generation=record["generation"],
                    changes={
                        "stage": "attached", "attachment_state": "published",
                        "context_state": "ready",
                    },
                )
                if record["head"] == head:
                    return _report("attached", reused=True, reconciled=True), 0
            elif record["attachment_state"] == "uncertain" and not retry_uncertain:
                return _report("attachment_uncertain", reused=True), 1
            elif record["head"] != head:
                abandon_attachment(
                    packet_store, record["connection"], record["packet"], record["revision"],
                )
                record = context_session.update(
                    association_store,
                    record["session_id"],
                    expected_generation=record["generation"],
                    changes={
                        "stage": "prepared", "pr": None, "head": None, "revision": None,
                        "attachment_state": "none",
                    },
                )
            elif record["attachment_state"] in {"pending", "uncertain"}:
                return _finish_publication(
                    association_store,
                    packet_store,
                    record,
                    metadata,
                    token=token,
                    authorities=authorities,
                )

        revision = uuid.uuid4().hex
        record = context_session.update(
            association_store,
            record["session_id"],
            expected_generation=record["generation"],
            changes={
                "stage": "prepared", "pr": pr, "head": head, "revision": revision,
                "attachment_state": "pending",
            },
        )
        metadata = _reserve_for_record(
            association_store, packet_store, record, backend=backend,
        )
        return _finish_publication(
            association_store,
            packet_store,
            record,
            metadata,
            token=token,
            authorities=authorities,
        )


def _finish_publication(
    association_store: ContextStore,
    packet_store: ContextStore,
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
    *,
    token: str,
    authorities: tuple[str, ...],
) -> tuple[dict[str, Any], int]:
    head, current = _remote_input(record["repo"], record["pr"], token=token, authorities=authorities)
    if head != record["head"]:
        abandon_attachment(
            packet_store, record["connection"], record["packet"], record["revision"],
        )
        context_session.update(
            association_store,
            record["session_id"],
            expected_generation=record["generation"],
            changes={
                "pr": None, "head": None, "revision": None, "attachment_state": "none",
            },
        )
        raise ContextError("pull request head changed before publication; rerun attach")
    if current == metadata:
        mark_published(packet_store, record["connection"], record["revision"])
    else:
        try:
            _publish(record["repo"], record["pr"], record["head"], metadata, token=token)
        except Exception:
            _set_uncertain(association_store, record)
            return _report("attachment_uncertain", reused=True), 1
        mark_published(packet_store, record["connection"], record["revision"])
    context_session.update(
        association_store,
        record["session_id"],
        expected_generation=record["generation"],
        changes={
            "stage": "attached", "attachment_state": "published", "context_state": "ready",
        },
    )
    return _report("attached", reused=False, reconciled=current == metadata), 0


def _builder_recipient(record: Mapping[str, Any]) -> str:
    builder = participant_id(record["builder"])
    recipient = builder + ":builder"
    if recipient not in SUPPORTED_RECIPIENTS:
        raise ContextError(
            "the selected builder cannot consume private context in this release; choose Claude or Codex"
        )
    return recipient


def deliver_session(
    association_store: ContextStore,
    packet_store: ContextStore,
    record: Mapping[str, Any],
    *,
    repo_path: Path,
    base_ref: str = "origin/main",
    backend: Any = None,
) -> str:
    """Render the session's authorized packet for its selected builder."""
    record = context_session.validate(record)
    if record["packet"] is None or record["connection"] is None or record["builder"] is None:
        raise ContextError("prepare the selected context before delivering it")
    recipient = _builder_recipient(record)
    try:
        if record["attachment_state"] != "published":
            packet = load_authorized(
                packet_store,
                record["connection"],
                record["packet"],
                record["policy"],
                ContextRequest(record["repo"], record["work_item"], recipient),
                backend=backend,
            )
            text = render_evidence(packet, record["packet"])
        else:
            token, authorities = _github_access(repo_path, base_ref)
            head, current = _remote_input(
                record["repo"], record["pr"], token=token, authorities=authorities,
            )
            text = deliver(
                packet_store,
                record["revision"],
                repository=record["repo"],
                pr=record["pr"],
                head=head,
                recipient=recipient,
                current=current,
                backend=backend,
            ).text
    except ContextError as exc:
        context_session.record_failure(association_store, record, exc)
        raise
    if record["context_state"] != "ready":
        context_session.update(
            association_store,
            record["session_id"],
            expected_generation=record["generation"],
            changes={"context_state": "ready"},
        )
    return text


def feedback_session(
    association_store: ContextStore,
    packet_store: ContextStore,
    record: Mapping[str, Any],
    *,
    repo_path: Path,
    reviewer: str,
    base_ref: str = "origin/main",
    backend: Any = None,
) -> str:
    """Return one selected reviewer's saved private findings for the builder."""
    record = context_session.validate(record)
    reviewer = participant_id(reviewer)
    if (
        reviewer not in {"claude", "codex"}
        or reviewer not in record["participants"]
        or reviewer == record["builder"]
        or PARTICIPANTS[reviewer].review_lane is None
    ):
        raise ContextError("--reviewer must name a selected independent Claude or Codex reviewer")
    if record["attachment_state"] != "published":
        raise ContextError("attach context and complete the independent review before reading feedback")
    recipient = _builder_recipient(record)
    token, authorities = _github_access(repo_path, base_ref)
    head, current = _remote_input(record["repo"], record["pr"], token=token, authorities=authorities)
    try:
        bound = deliver(
            packet_store,
            record["revision"],
            repository=record["repo"],
            pr=record["pr"],
            head=head,
            recipient=recipient,
            current=current,
            backend=backend,
        )
    except ContextError as exc:
        context_session.record_failure(association_store, record, exc)
        raise
    feedback = bound.binding["feedback"].get(reviewer)
    if feedback is None:
        raise ContextError("no authorized feedback is available for this reviewer")
    if record["stage"] != "reviewed":
        context_session.update(
            association_store,
            record["session_id"],
            expected_generation=record["generation"],
            changes={"stage": "reviewed", "context_state": "ready"},
        )
    return feedback
