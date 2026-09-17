"""Pure, read-only local producer for the Board observation contract.

The producer deliberately owns no lifecycle state.  Callers pass immutable,
already observed facts from the maintained session, runner, delivery, review,
CI, and gate paths.  This module binds those facts to the exact current
session and worktree, derives the closed Board projection, and validates it
with :mod:`code_mower.board_observation`.

``observe_local_work`` is the narrow integration boundary for Board.  It does
not scan directories, follow paths supplied by evidence, acquire a lock,
renew a lease, inspect a PID, contact a provider, or mutate workflow state.
Malformed or racing input therefore fails closed as ``None``.  A process
observation can only produce a deduplicated ``unlinked`` record; it can never
be promoted to session work from provider, author, PID, or command text.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import builder_lineage, context_session, session_current, session_lease
from .board_observation import SCHEMA, derive_primary, ordered_reasons, validate
from .context_contract import ContextError
from .lane_delivery import DELIVERY_OUTCOME_SCHEMA
from .provider_runners import validate_audit_verdict_artifact_payload
from .remote_session import RemoteError, public_projection
from .review_authority import review_authority


_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,127}\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_HEAD = re.compile(r"[a-f0-9]{40}\Z")
_WORKTREE = re.compile(r"sha256:[a-f0-9]{64}\Z")
_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/#-]{0,127}\Z")
_ISSUE = re.compile(r"(?:issue[-:/#]?|#)([1-9][0-9]*)\Z", re.IGNORECASE)
_ALLOWED_SOURCE_KINDS = frozenset(
    {
        "work_queue",
        "run_registry",
        "local_runner",
        "remote_session",
        "github",
        "review",
        "ci",
        "gate",
        "measurement",
    }
)
_HEAD_EVIDENCE = frozenset({"review", "ci", "gate"})
_EVIDENCE_KINDS = frozenset(
    {"assignment", "review_request", "review", "ci", "gate_publisher", "gate", "merge"}
)
_LIVE_PHASES = frozenset(
    {"observed_running", "provider_progress", "waiting_for_user", "waiting_for_approval"}
)
_UNAVAILABLE_MEASUREMENT = {
    "value": None,
    "coverage": "unavailable",
    "observed": 0,
    "total": None,
}


class LocalObservationError(ValueError):
    """A fixed producer refusal that never contains observed private values."""


@dataclass(frozen=True)
class WorkBinding:
    """Exact identity shared by one work record and all correlated evidence."""

    session_id: str
    work_id: str
    repository: str
    worktree_id: str
    pr_number: int | None = None
    head_sha: str | None = None


@dataclass(frozen=True)
class LocalRunObservation:
    """One maintained runner/provider observation with an exact work binding."""

    id: str
    binding: WorkBinding
    provider: str
    role: str
    phase: str
    basis: str
    observed_at: datetime | None
    source_kind: str = "local_runner"
    event_at: datetime | None = None
    heartbeat_at: datetime | None = None
    reported_stage: str | None = None
    lifecycle: Mapping[str, Any] | None = None
    source_available: bool = True
    retain_remote_observation: bool = False
    checked_at: datetime | None = None


@dataclass(frozen=True)
class LocalEvidenceObservation:
    """Review/check/merge evidence explicitly bound to one PR and head."""

    kind: str
    state: str
    binding: WorkBinding
    observed_at: datetime
    source_kind: str = "github"
    coverage: str = "full"
    event_at: datetime | None = None
    source_available: bool = True


@dataclass(frozen=True)
class LocalPolicyObservation:
    """Explicit GitHub/trusted policy facts, independent of an audit verdict."""

    binding: WorkBinding
    observed_at: datetime
    reasons: tuple[str, ...] = ()
    source_available: bool = True


@dataclass(frozen=True)
class LocalWorkObservation:
    """One local work item and all facts a maintained adapter has observed."""

    binding: WorkBinding
    reference: str
    observed_at: datetime
    runs: tuple[LocalRunObservation, ...] = ()
    evidence: tuple[LocalEvidenceObservation, ...] = ()
    assigned_provider: str | None = None
    assigned_role: str = "builder"
    policy: LocalPolicyObservation | None = None


@dataclass(frozen=True)
class LocalProcessObservation:
    """A launcher-group observation that intentionally carries no work binding.

    ``group_id`` is a stable opaque launcher/run identity supplied by the
    maintained process observer.  It is not a PID.  Descendants with the same
    group identity collapse to one unlinked row.
    """

    group_id: str
    provider: str
    observed_at: datetime
    role: str = "unknown"


@dataclass(frozen=True)
class LocalObservationInput:
    """Immutable producer input captured by the later Board integration hook."""

    work: LocalWorkObservation | None = None
    processes: tuple[LocalProcessObservation, ...] = ()
    work_queue_complete: bool = False
    run_registry_complete: bool = False


CurrentSessionResolver = Callable[..., dict[str, Any]]


def worktree_identity(start: str | Path) -> str:
    """Return an opaque identity for one canonical Git working-copy root."""
    try:
        root = session_lease.find_working_copy_root(start)
        canonical = str(root.resolve(strict=True))
    except (OSError, RuntimeError, session_lease.SessionLeaseError):
        raise LocalObservationError("worktree_unavailable") from None
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise LocalObservationError("invalid_timestamp")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _safe_identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise LocalObservationError("identity_mismatch")
    return value


def _validate_binding(binding: WorkBinding) -> None:
    if (
        not isinstance(binding, WorkBinding)
        or session_current._SESSION_ID.fullmatch(binding.session_id) is None
        or _IDENTIFIER.fullmatch(binding.work_id) is None
        or _REPOSITORY.fullmatch(binding.repository) is None
        or _WORKTREE.fullmatch(binding.worktree_id) is None
        or (binding.pr_number is None) != (binding.head_sha is None)
        or (
            binding.pr_number is not None
            and (type(binding.pr_number) is not int or not 1 <= binding.pr_number <= 2_147_483_647)
        )
        or (binding.head_sha is not None and _HEAD.fullmatch(binding.head_sha) is None)
    ):
        raise LocalObservationError("identity_mismatch")


def _same_work(left: WorkBinding, right: WorkBinding, *, include_head: bool = True) -> bool:
    base = (
        left.session_id,
        left.work_id,
        left.repository,
        left.worktree_id,
        left.pr_number,
    ) == (
        right.session_id,
        right.work_id,
        right.repository,
        right.worktree_id,
        right.pr_number,
    )
    return base and (not include_head or left.head_sha == right.head_sha)


def _opaque(seed: str, prefix: str) -> str:
    return prefix + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _public_reference(value: str) -> str:
    """Keep recognized tracker identity; hash every other work-item string."""
    match = _ISSUE.fullmatch(value.strip()) if isinstance(value, str) else None
    if match:
        return "issue-" + match.group(1)
    if isinstance(value, str) and _REFERENCE.fullmatch(value) and len(value) <= 32:
        # Short Jira-style keys and already opaque work ids are safe references.
        if re.fullmatch(r"[A-Z][A-Z0-9]+-[1-9][0-9]*", value) or value.startswith("work-"):
            return value
    return _opaque(str(value), "work-")


def work_from_context_session(
    record: Mapping[str, Any],
    *,
    worktree_id: str,
    observed_at: datetime,
    runs: Sequence[LocalRunObservation] = (),
    evidence: Sequence[LocalEvidenceObservation] = (),
) -> LocalWorkObservation:
    """Adapt the existing private context-session contract without reading it.

    The caller remains responsible for the descriptor-safe read that produced
    ``record``.  This helper only validates the existing contract and discards
    private prose.  The output reference is a tracker key or an opaque digest.
    """
    try:
        current = context_session.validate(record)
    except (ContextError, TypeError, ValueError):
        raise LocalObservationError("work_unavailable") from None
    session_id = current["session_id"]
    work_item = current["work_item"]
    work_id = _opaque(f"{session_id}\0{current['repo']}\0{work_item}", "work")
    binding = WorkBinding(
        session_id=session_id,
        work_id=work_id,
        repository=current["repo"],
        worktree_id=worktree_id,
        pr_number=current["pr"],
        head_sha=current["head"],
    )
    return LocalWorkObservation(
        binding=binding,
        reference=_public_reference(work_item),
        observed_at=observed_at,
        runs=tuple(runs),
        evidence=tuple(evidence),
        assigned_provider=current["builder"],
    )


def run_from_remote_lifecycle(
    *,
    id: str,
    binding: WorkBinding,
    provider: str,
    role: str,
    observed_at: datetime,
    lifecycle: Mapping[str, Any],
    event_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
    reported_stage: str | None = None,
) -> LocalRunObservation:
    """Map only the existing public remote lifecycle projection to a run phase."""
    try:
        projected = public_projection(dict(lifecycle))
    except (RemoteError, TypeError, ValueError):
        raise LocalObservationError("run_unavailable") from None
    state = projected["state"]
    phases = {
        "pending": "dispatched",
        "uncertain": "dispatched",
        "running": "provider_progress" if reported_stage else "observed_running",
        "waiting_for_user": "waiting_for_user",
        "waiting_for_approval": "waiting_for_approval",
        "complete": "implementation_complete",
        "archived": "implementation_complete",
        "failed": "failed",
        "suspended": "failed",
        "terminated": "cancelled",
    }
    phase = phases[state]
    if state == "archived" and projected["reason"] != "none":
        phase = "cancelled"
    basis = "provider_reported" if phase not in {"dispatched", "observed_running"} else "observed"
    return LocalRunObservation(
        id=id,
        binding=binding,
        provider=provider,
        role=role,
        phase=phase,
        basis=basis,
        observed_at=observed_at,
        source_kind="remote_session",
        event_at=event_at,
        heartbeat_at=heartbeat_at,
        reported_stage=reported_stage,
        lifecycle=projected,
    )


def run_from_delivery_outcome(
    event: Mapping[str, Any],
    *,
    binding: WorkBinding,
    target_kind: str,
    target_number: int,
    observed_at: datetime,
) -> LocalRunObservation:
    """Project one already-classified maintained lane-delivery outcome.

    Delivery classification stays in :mod:`code_mower.lane_delivery`; this
    adapter only verifies its repository/target binding and chooses the closed
    Board phase.  Provider exit or supervision alone never becomes liveness.
    """
    try:
        if not isinstance(event, Mapping) or event.get("schema") != DELIVERY_OUTCOME_SCHEMA:
            raise LocalObservationError("run_unavailable")
        target = event.get("target")
        delivery = event.get("delivery")
        provider = event.get("provider")
        lane = event.get("lane")
        if (
            event.get("repo") != binding.repository
            or target_kind not in {"issue", "pr"}
            or not isinstance(target, Mapping)
            or target.get("kind") != target_kind
            or target.get("number") != str(target_number)
            or not isinstance(delivery, Mapping)
            or type(delivery.get("delivered")) is not bool
            or not isinstance(delivery.get("transition"), str)
            or not isinstance(delivery.get("reason"), str)
            or not isinstance(provider, Mapping)
            or type(provider.get("exit_code")) is not int
        ):
            raise LocalObservationError("identity_mismatch")
        lane_id = _safe_identifier(lane)
        delivered = delivery["delivered"]
        phase = "implementation_complete" if delivered else "failed"
        if not delivered and provider.get("supervision") == "interrupted":
            phase = "cancelled"
        return LocalRunObservation(
            id=_opaque(str(event.get("event_id") or ""), "run"),
            binding=binding,
            provider=lane_id,
            role="builder",
            phase=phase,
            basis="observed",
            observed_at=observed_at,
            source_kind="local_runner",
            event_at=_parse_timestamp(event.get("created_at")),
        )
    except (LocalObservationError, TypeError, ValueError):
        raise LocalObservationError("run_unavailable") from None


def review_from_audit_artifact(
    artifact: Mapping[str, Any],
    *,
    binding: WorkBinding,
    observed_at: datetime,
    lineage: builder_lineage.Lineage | None = None,
    policy: Mapping[str, Any] | None = None,
) -> LocalEvidenceObservation:
    """Admit an independent, authoritative exact-head audit without private prose.

    The caller supplies the resolved exact-head lineage and trusted base policy,
    never a provider's self-reported qualification or a mutable done label.
    Missing/identity-only lineage cannot confer authority.
    """
    try:
        payload = validate_audit_verdict_artifact_payload(dict(artifact))
        if (
            payload.get("repo") != binding.repository
            or payload.get("pr_number") != binding.pr_number
            or payload.get("head_sha_start") != binding.head_sha
            or payload.get("head_sha_end") != binding.head_sha
            or payload.get("quarantined") is True
            or not isinstance(lineage, builder_lineage.Lineage)
            or lineage.target is None
            or (lineage.target.repo, lineage.target.pr_number, lineage.target.head_sha)
            != (binding.repository, binding.pr_number, binding.head_sha)
            or not builder_lineage.admit(lineage, payload["lane_id"])
            or not review_authority(payload["lane_id"], config=policy)["merge_authority"]
        ):
            raise LocalObservationError("identity_mismatch")
        verdict = str(payload.get("verdict") or "").lower()
        state = {
            "pass": "pass",
            "passed": "pass",
            "done": "pass",
            "completed": "pass",
            "success": "pass",
            "succeeded": "pass",
            "blocked": "blocked",
            "fail": "blocked",
            "failed": "blocked",
            "failure": "blocked",
            "stale": "stale",
        }.get(verdict, "unknown")
        return LocalEvidenceObservation(
            kind="review",
            state=state,
            binding=binding,
            observed_at=observed_at,
            source_kind="review",
            coverage="full" if state != "unknown" else "unavailable",
            event_at=_parse_timestamp(payload.get("created_at")),
        )
    except (LocalObservationError, TypeError, ValueError):
        raise LocalObservationError("evidence_unavailable") from None


def _parse_timestamp(value: object) -> datetime | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise LocalObservationError("invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LocalObservationError("invalid_timestamp") from None
    return _utc(parsed)


def _source(
    *,
    id: str,
    kind: str,
    now: datetime,
    observed_at: datetime | None,
    event_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
    available: bool = True,
    complete: bool = True,
    fresh: bool = True,
) -> dict[str, Any]:
    return {
        "id": id,
        "kind": kind,
        "freshness": "fresh" if available and fresh else "stale" if available else "unavailable",
        "coverage": "complete" if available and complete else "partial" if available else "unavailable",
        "event_at": _stamp(event_at) if event_at is not None else None,
        "observed_at": _stamp(observed_at) if observed_at is not None else None,
        "checked_at": _stamp(now),
        "heartbeat_at": _stamp(heartbeat_at) if heartbeat_at is not None else None,
    }


def _default_evidence() -> dict[str, dict[str, Any]]:
    def basic(state: str = "unknown") -> dict[str, Any]:
        return {"state": state, "source_id": None}

    def headed() -> dict[str, Any]:
        return {
            "state": "unknown",
            "source_id": None,
            "head_sha": None,
            "coverage": "unavailable",
        }
    return {
        "lease": basic(),
        "assignment": basic(),
        "review_request": basic(),
        "review": headed(),
        "ci": headed(),
        "gate_publisher": basic(),
        "gate": headed(),
        "merge": basic(),
    }


def _measurements() -> dict[str, dict[str, Any]]:
    return {
        name: dict(_UNAVAILABLE_MEASUREMENT)
        for name in (
            "elapsed_seconds",
            "cost_usd",
            "quality_score",
            "productivity_count",
            "provenance_count",
        )
    }


def _unlinked(
    *,
    processes: Sequence[LocalProcessObservation],
    repository: str,
    now: datetime,
    stale_after_seconds: int,
) -> dict[str, Any] | None:
    if not processes:
        return None
    groups: dict[str, LocalProcessObservation] = {}
    for process in processes:
        if not isinstance(process, LocalProcessObservation):
            return None
        _safe_identifier(process.group_id)
        _safe_identifier(process.provider)
        if process.role not in {"unknown", "orchestrator", "builder", "reviewer"}:
            return None
        observed = _utc(process.observed_at)
        if observed > now:
            return None
        previous = groups.get(process.group_id)
        if previous is not None and (
            previous.provider != process.provider or previous.role != process.role
        ):
            return None
        if previous is None or previous.observed_at < process.observed_at:
            groups[process.group_id] = process
    observed = max(_utc(item.observed_at) for item in groups.values())
    source = _source(
        id="processobs",
        kind="local_process",
        now=now,
        observed_at=observed,
        fresh=_fresh(observed, now, stale_after_seconds),
    )
    payload = {
        "schema": SCHEMA,
        "kind": "unlinked",
        "created_at": _stamp(now),
        "scope": {"session_id": None, "repository": repository, "worktree_id": None},
        "display": {"authorized": False, "session_label": None},
        "sources": [source],
        "work": None,
        "unlinked": [
            {
                "id": item.group_id,
                "provider": item.provider,
                "role": item.role,
                "source_id": "processobs",
                "observed_at": _stamp(_utc(item.observed_at)),
            }
            for item in sorted(groups.values(), key=lambda row: row.group_id)
        ],
    }
    return validate(payload)


def _fresh(observed_at: datetime, now: datetime, stale_after_seconds: int) -> bool:
    delta = (now - _utc(observed_at)).total_seconds()
    return 0 <= delta <= stale_after_seconds


def _active_scope(
    current: Mapping[str, Any], *, repository: str, worktree_id: str
) -> tuple[str, Mapping[str, Any]]:
    if current.get("state") != session_current.STATE_ACTIVE or current.get("current") is not True:
        raise LocalObservationError("session_unavailable")
    session = current.get("session")
    if not isinstance(session, Mapping):
        raise LocalObservationError("session_unavailable")
    session_id = session.get("id")
    if (
        not isinstance(session_id, str)
        or session_current._SESSION_ID.fullmatch(session_id) is None
        or session.get("repo") != repository
        or current.get("lease", {}).get("state") != "active"
        or not _WORKTREE.fullmatch(worktree_id)
    ):
        raise LocalObservationError("identity_mismatch")
    return session_id, session


def _no_work(
    *, session_id: str, repository: str, worktree_id: str, now: datetime
) -> dict[str, Any]:
    sources = [
        _source(id="sessionobs", kind="session", now=now, observed_at=now),
        _source(id="queueobs", kind="work_queue", now=now, observed_at=now),
        _source(id="registryobs", kind="run_registry", now=now, observed_at=now),
    ]
    return validate(
        {
            "schema": SCHEMA,
            "kind": "no_work",
            "created_at": _stamp(now),
            "scope": {
                "session_id": session_id,
                "repository": repository,
                "worktree_id": worktree_id,
            },
            "display": {"authorized": False, "session_label": None},
            "sources": sources,
            "work": None,
            "unlinked": [],
        }
    )


def _derive_state(
    runs: Sequence[dict[str, Any]], evidence: Mapping[str, Mapping[str, Any]], reasons: set[str]
) -> str:
    # A review/merge fact does not erase an independently observed provider wait
    # or failure (a completed implementation can still have an active writer).
    phases = {run["phase"] for run in runs}
    if any(run["phase"] == "failed" and (run.get("lifecycle") or {}).get("state") != "suspended"
           for run in runs):
        reasons.add("provider_failed")
    if any((run.get("lifecycle") or {}).get("state") == "suspended" for run in runs):
        reasons.add("provider_suspended")
    if phases & {"cancelled"}:
        reasons.add("cancelled")
    if phases & {"waiting_for_user"}:
        reasons.add("user_input_required")
    if phases & {"waiting_for_approval"}:
        reasons.add("approval_required")
    if evidence["merge"]["state"] == "merged":
        return "merged"
    if evidence["review"]["state"] == "blocked":
        reasons.add("changes_requested")
    if evidence["ci"]["state"] == "failed":
        reasons.add("ci_failed")
    if evidence["gate"]["state"] == "failed":
        reasons.add("gate_failed")
    if evidence["review"]["state"] == "running":
        reasons.add("review_in_progress")
    if evidence["review"]["state"] == "stale":
        reasons.add("review_stale")
    if evidence["ci"]["state"] == "pending":
        reasons.add("ci_pending")
    if evidence["gate"]["state"] == "pending":
        reasons.add("gate_pending")
    if evidence["review"]["state"] == "blocked":
        return "changes_requested"
    if evidence["review"]["state"] == "running":
        return "in_review"
    if (
        evidence["review"]["state"] == "pass"
        and evidence["ci"]["state"] == "pass"
        and evidence["ci"]["coverage"] == "full"
        and evidence["gate"]["state"] == "pass"
        and evidence["merge"]["state"] == "ready"
        and not reasons
        and not phases & (_LIVE_PHASES | {"assigned", "dispatched"})
    ):
        reasons.add("ready_to_merge")
        return "ready_to_merge"
    if "human_review_required" in reasons:
        return "ready_for_human_review"
    if phases & {"observed_running", "provider_progress", "waiting_for_user", "waiting_for_approval"}:
        return "building"
    if phases & {"assigned", "dispatched"}:
        return "queued"
    if phases & {"implementation_complete"}:
        return "in_review"
    return "unknown"


def _produce_work(
    *,
    work: LocalWorkObservation,
    session_id: str,
    repository: str,
    worktree_id: str,
    now: datetime,
    stale_after_seconds: int,
) -> dict[str, Any]:
    _validate_binding(work.binding)
    if (
        work.binding.session_id != session_id
        or work.binding.repository != repository
        or work.binding.worktree_id != worktree_id
        or _REFERENCE.fullmatch(work.reference) is None
        or _utc(work.observed_at) > now
    ):
        raise LocalObservationError("identity_mismatch")

    sources = [
        _source(id="sessionobs", kind="session", now=now, observed_at=now),
        _source(
            id="queueobs",
            kind="work_queue",
            now=now,
            observed_at=work.observed_at,
            fresh=_fresh(work.observed_at, now, stale_after_seconds),
        ),
        _source(
            id="registryobs",
            kind="run_registry",
            now=now,
            observed_at=work.observed_at,
            fresh=_fresh(work.observed_at, now, stale_after_seconds),
        ),
        _source(id="leaseobs", kind="lease", now=now, observed_at=now),
    ]
    evidence = _default_evidence()
    evidence["lease"] = {"state": "held", "source_id": "leaseobs"}
    reasons: set[str] = set()
    if not _fresh(work.observed_at, now, stale_after_seconds):
        reasons.add("stale_observation")
    if work.policy is not None:
        policy = work.policy
        if not isinstance(policy, LocalPolicyObservation):
            raise LocalObservationError("evidence_unavailable")
        _validate_binding(policy.binding)
        if (not _same_work(work.binding, policy.binding)
                or _utc(policy.observed_at) > now
                or len(set(policy.reasons)) != len(policy.reasons)
                or set(policy.reasons) - {
                    "update_required", "human_review_required", "approval_required",
                    "user_input_required",
                }):
            raise LocalObservationError("evidence_unavailable")
        fresh = _fresh(policy.observed_at, now, stale_after_seconds)
        sources.append(_source(id="policyobs", kind="github", now=now,
                               observed_at=policy.observed_at,
                               available=policy.source_available, fresh=fresh))
        if not policy.source_available:
            reasons.add("source_unavailable")
        elif not fresh:
            reasons.add("stale_observation")
        else:
            reasons.update(policy.reasons)

    rendered_runs: list[dict[str, Any]] = []
    seen_runs: set[str] = set()
    for index, run in enumerate(work.runs):
        if not isinstance(run, LocalRunObservation):
            raise LocalObservationError("run_unavailable")
        _validate_binding(run.binding)
        if not _same_work(work.binding, run.binding):
            raise LocalObservationError("identity_mismatch")
        if run.id in seen_runs:
            raise LocalObservationError("identity_mismatch")
        seen_runs.add(run.id)
        _safe_identifier(run.id)
        _safe_identifier(run.provider)
        if run.source_kind not in _ALLOWED_SOURCE_KINDS:
            raise LocalObservationError("run_unavailable")
        observed = _utc(run.observed_at) if run.observed_at is not None else None
        checked = _utc(run.checked_at) if run.checked_at is not None else now
        if checked > now or (observed is not None and observed > checked):
            raise LocalObservationError("invalid_timestamp")
        event = _utc(run.event_at) if run.event_at is not None else None
        heartbeat = _utc(run.heartbeat_at) if run.heartbeat_at is not None else None
        if observed is None:
            if run.source_available or event is not None or heartbeat is not None:
                raise LocalObservationError("invalid_timestamp")
            sources.append(_source(id=f"runobs{index}", kind=run.source_kind, now=checked,
                                   observed_at=None, available=False))
            reasons.add("source_unavailable")
            continue
        if observed > now or (event is not None and event > observed) or (
            heartbeat is not None and heartbeat > observed
        ):
            raise LocalObservationError("invalid_timestamp")
        fresh = _fresh(observed, now, stale_after_seconds)
        source_id = f"runobs{index}"
        sources.append(
            _source(
                id=source_id,
                kind=run.source_kind,
                now=checked,
                observed_at=observed,
                event_at=event,
                heartbeat_at=heartbeat,
                available=run.source_available,
                fresh=fresh,
            )
        )
        if not run.source_available:
            reasons.add("source_unavailable")
            if not run.retain_remote_observation:
                continue
        if run.phase in _LIVE_PHASES and (not fresh or heartbeat is None):
            reasons.add("stale_observation")
            if not run.retain_remote_observation or heartbeat is None:
                continue
        if not fresh:
            reasons.add("stale_observation")
        lifecycle = None
        if run.lifecycle is not None:
            try:
                lifecycle = public_projection(dict(run.lifecycle))
            except (RemoteError, TypeError, ValueError):
                raise LocalObservationError("run_unavailable") from None
        if run.retain_remote_observation and (run.source_kind != "remote_session" or lifecycle is None):
            raise LocalObservationError("run_unavailable")
        rendered_runs.append(
            {
                "id": run.id,
                "binding": {
                    "session_id": run.binding.session_id,
                    "work_id": run.binding.work_id,
                    "repository": run.binding.repository,
                    "worktree_id": run.binding.worktree_id,
                },
                "provider": run.provider,
                "role": run.role,
                "phase": run.phase,
                "basis": run.basis,
                "reported_stage": run.reported_stage,
                "source_id": source_id,
                "event_at": _stamp(event) if event is not None else None,
                "observed_at": _stamp(observed),
                "heartbeat_at": _stamp(heartbeat) if heartbeat is not None else None,
                "lifecycle": lifecycle,
            }
        )

    if work.assigned_provider is not None:
        provider = _safe_identifier(work.assigned_provider)
        evidence["assignment"] = {"state": "assigned", "source_id": "queueobs"}
        if not rendered_runs:
            rendered_runs.append(
                {
                    "id": _opaque(work.binding.work_id + "\0" + provider, "run"),
                    "binding": {
                        "session_id": session_id,
                        "work_id": work.binding.work_id,
                        "repository": repository,
                        "worktree_id": worktree_id,
                    },
                    "provider": provider,
                    "role": work.assigned_role,
                    "phase": "assigned",
                    "basis": "configured",
                    "reported_stage": None,
                    "source_id": "queueobs",
                    "event_at": None,
                    "observed_at": _stamp(_utc(work.observed_at)),
                    "heartbeat_at": None,
                    "lifecycle": None,
                }
            )
    else:
        evidence["assignment"] = {"state": "unassigned", "source_id": "queueobs"}

    seen_evidence: set[str] = set()
    for index, item in enumerate(work.evidence):
        if not isinstance(item, LocalEvidenceObservation) or item.kind not in _EVIDENCE_KINDS:
            raise LocalObservationError("evidence_unavailable")
        if item.kind in seen_evidence:
            raise LocalObservationError("evidence_unavailable")
        seen_evidence.add(item.kind)
        _validate_binding(item.binding)
        if not _same_work(work.binding, item.binding, include_head=False):
            raise LocalObservationError("identity_mismatch")
        if item.kind not in _HEAD_EVIDENCE and not _same_work(work.binding, item.binding):
            raise LocalObservationError("identity_mismatch")
        observed = _utc(item.observed_at)
        event = _utc(item.event_at) if item.event_at is not None else None
        if observed > now or (event is not None and event > observed):
            raise LocalObservationError("invalid_timestamp")
        if item.source_kind not in _ALLOWED_SOURCE_KINDS:
            raise LocalObservationError("evidence_unavailable")
        source_id = f"evidenceobs{index}"
        sources.append(
            _source(
                id=source_id,
                kind=item.source_kind,
                now=now,
                observed_at=observed,
                event_at=event,
                available=item.source_available,
                fresh=_fresh(observed, now, stale_after_seconds),
            )
        )
        if not item.source_available:
            reasons.add("source_unavailable")
            continue
        fresh = _fresh(observed, now, stale_after_seconds)
        if not fresh:
            reasons.add("stale_observation")
        if item.kind in _HEAD_EVIDENCE:
            state = item.state
            exact_head = item.binding.head_sha == work.binding.head_sha
            if state not in {"unknown", "not_started"} and not exact_head:
                state = "stale"
                reasons.add("review_stale" if item.kind == "review" else "update_required")
            elif state not in {"unknown", "not_started"} and not fresh:
                state = "stale"
                reasons.add("review_stale" if item.kind == "review" else "stale_observation")
            evidence[item.kind] = {
                "state": state,
                "source_id": source_id if state not in {"unknown", "not_started"} else None,
                "head_sha": item.binding.head_sha if state not in {"unknown", "not_started"} else None,
                "coverage": item.coverage if state not in {"unknown", "not_started"} else "unavailable",
            }
        else:
            evidence[item.kind] = {
                "state": item.state,
                "source_id": source_id
                if item.state not in {"unknown", "none", "not_started"}
                else None,
            }

    stage = _derive_state(rendered_runs, evidence, reasons)
    if work.binding.pr_number is not None and evidence["review_request"]["state"] == "requested":
        if evidence["review"]["state"] in {"unknown", "not_started"}:
            reasons.add("review_requested")
    canonical_reasons = ordered_reasons(list(reasons))
    payload = {
        "schema": SCHEMA,
        "kind": "work",
        "created_at": _stamp(now),
        "scope": {
            "session_id": session_id,
            "repository": repository,
            "worktree_id": worktree_id,
        },
        "display": {"authorized": False, "session_label": None},
        "sources": sources,
        "work": {
            "id": work.binding.work_id,
            "reference": work.reference,
            "stage": stage,
            "reasons": canonical_reasons,
            "primary": derive_primary(canonical_reasons),
            "pull_request": {
                "number": work.binding.pr_number,
                "head_sha": work.binding.head_sha,
            },
            "runs": rendered_runs,
            "evidence": evidence,
            "measurements": _measurements(),
        },
        "unlinked": [],
    }
    return validate(payload)


def observe_local_work(
    *,
    repository: str,
    start: str | Path,
    snapshot: LocalObservationInput | None = None,
    state_dir: str | Path | None = None,
    now: datetime | None = None,
    stale_after_seconds: int = 300,
    current_session_resolver: CurrentSessionResolver = session_current.resolve_current_session,
) -> dict[str, Any] | None:
    """Return one validated local Board observation, or fail closed as ``None``.

    The later Board hook should capture maintained facts into
    :class:`LocalObservationInput` and call this function.  The injected
    resolver exists for deterministic tests; production uses the exact
    read-only resolver from #935.
    """
    try:
        if _REPOSITORY.fullmatch(repository) is None:
            raise LocalObservationError("identity_mismatch")
        snapshot = snapshot or LocalObservationInput()
        if not isinstance(snapshot, LocalObservationInput):
            raise LocalObservationError("invalid_input")
        instant = _utc(now or datetime.now(timezone.utc))
        if type(stale_after_seconds) is not int or stale_after_seconds < 1:
            raise LocalObservationError("invalid_input")
        worktree_id = worktree_identity(start)
        current = current_session_resolver(start=start, state_dir=state_dir, now=instant)
        try:
            session_id, _session = _active_scope(
                current, repository=repository, worktree_id=worktree_id
            )
        except LocalObservationError:
            return _unlinked(
                processes=snapshot.processes,
                repository=repository,
                now=instant,
                stale_after_seconds=stale_after_seconds,
            )
        if snapshot.work is None:
            if snapshot.processes:
                return _unlinked(
                    processes=snapshot.processes,
                    repository=repository,
                    now=instant,
                    stale_after_seconds=stale_after_seconds,
                )
            if snapshot.work_queue_complete and snapshot.run_registry_complete:
                return _no_work(
                    session_id=session_id,
                    repository=repository,
                    worktree_id=worktree_id,
                    now=instant,
                )
            return None
        return _produce_work(
            work=snapshot.work,
            session_id=session_id,
            repository=repository,
            worktree_id=worktree_id,
            now=instant,
            stale_after_seconds=stale_after_seconds,
        )
    except (LocalObservationError, OSError, RuntimeError, TypeError, ValueError):
        return None
