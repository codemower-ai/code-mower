"""Provider-neutral remote facts into the existing local Board correlation seam.

No provider, GitHub or filesystem I/O lives here. Execution adapters capture
metadata with their read-only observe methods; Board consumes the returned
LocalObservationInput through its existing hook. Round numbers and opaque
generation keys fence evidence internally and never expose provider references.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .board_local_observation import (
    LocalEvidenceObservation, LocalObservationError, LocalObservationInput,
    LocalRunObservation, LocalWorkObservation, WorkBinding, _fresh, _opaque,
    _parse_timestamp, _same_work, _utc, _validate_binding, run_from_remote_lifecycle,
)
from .remote_session import RemoteObservation, RemoteWorkObservation


@dataclass(frozen=True)
class RemoteRun:
    binding: WorkBinding
    round_number: int
    observation: RemoteObservation
    role: str = "builder"
    reported_stage: str | None = None


@dataclass(frozen=True)
class RemoteEvidence:
    """Already verified metadata captured by an exact-round evidence producer."""

    round_number: int
    observation: LocalEvidenceObservation


def remote_work_input(
    work: LocalWorkObservation, *, round_number: int, runs: Sequence[RemoteRun],
    current_pr: LocalEvidenceObservation | None = None,
    evidence: Sequence[RemoteEvidence] = (), controller: Mapping[str, Any] | None = None,
    now: datetime | None = None, stale_after_seconds: int = 300,
) -> LocalObservationInput:
    """Adapt one exact round without deriving review/merge authority from status.

    ``current_pr`` must be a separate GitHub observation of the current PR/head.
    Controller verdicts, label-derived PASS and provider completion do not count
    as review evidence. Old-round evidence is dropped even if the head repeats.
    """
    instant = _utc(now or datetime.now(timezone.utc))
    _validate_binding(work.binding)
    if type(round_number) is not int or not 0 <= round_number <= 100:
        raise LocalObservationError("identity_mismatch")
    if type(stale_after_seconds) is not int or stale_after_seconds < 1:
        raise LocalObservationError("invalid_input")
    adapted_runs = []
    for run in runs:
        if not isinstance(run, RemoteRun) or run.round_number != round_number:
            raise LocalObservationError("identity_mismatch")
        _validate_binding(run.binding)
        if not _same_work(run.binding, work.binding):
            raise LocalObservationError("identity_mismatch")
        observation = run.observation
        if (not isinstance(observation, RemoteObservation)
                or not observation.generation or _utc(observation.checked_at) > instant
                or (observation.observed_at is not None
                    and _utc(observation.observed_at) > _utc(observation.checked_at))):
            raise LocalObservationError("run_unavailable")
        run_id = _opaque(f"{work.binding}\0{round_number}\0{observation.generation}", "run")
        if observation.lifecycle is None or observation.observed_at is None:
            if observation.available:
                raise LocalObservationError("run_unavailable")
            adapted = LocalRunObservation(
                run_id, work.binding, observation.provider, run.role, "dispatched", "observed",
                None, source_kind="remote_session", source_available=False,
                checked_at=observation.checked_at,
            )
        else:
            adapted = run_from_remote_lifecycle(
                id=run_id, binding=work.binding, provider=observation.provider, role=run.role,
                observed_at=observation.observed_at, heartbeat_at=observation.observed_at,
                lifecycle=observation.lifecycle, reported_stage=run.reported_stage,
            )
            adapted = replace(adapted, source_available=observation.available,
                              retain_remote_observation=True, checked_at=observation.checked_at)
        adapted_runs.append(adapted)

    adapted_evidence = []
    pr_current = False
    if current_pr is not None:
        if (current_pr.kind != "merge" or current_pr.source_kind != "github"
                or not _same_work(current_pr.binding, work.binding)):
            raise LocalObservationError("identity_mismatch")
        pr_current = (current_pr.source_available and work.binding.pr_number is not None
                      and _fresh(current_pr.observed_at, instant, stale_after_seconds))
        adapted_evidence.append(current_pr if pr_current else replace(
            current_pr, state="unknown", source_available=False,
        ))
    for item in evidence:
        if not isinstance(item, RemoteEvidence) or item.round_number != round_number:
            continue
        observed = item.observation
        _validate_binding(observed.binding)
        if not _same_work(observed.binding, work.binding, include_head=False):
            raise LocalObservationError("identity_mismatch")
        if observed.kind in {"review", "ci", "gate"}:
            exact = _same_work(observed.binding, work.binding)
            fresh = _fresh(observed.observed_at, instant, stale_after_seconds)
            if observed.kind == "review" and (not exact or not fresh):
                observed = replace(observed, state="stale")
            elif not exact or not fresh or not pr_current:
                observed = replace(observed, state="unknown", source_available=False)
            if not pr_current:
                observed = replace(observed, state="unknown", source_available=False)
        elif observed.kind != "review_request":
            # A controller or audit artifact cannot vouch for merge/assignment.
            raise LocalObservationError("evidence_unavailable")
        adapted_evidence.append(observed)

    if controller is not None:
        from .controller import CONTROLLER_REPORT_SCHEMA

        decision = controller.get("decision")
        if (controller.get("schema") != CONTROLLER_REPORT_SCHEMA
                or controller.get("repo") != work.binding.repository
                or not isinstance(decision, Mapping)
                or not ((work.binding.pr_number is not None
                         and decision.get("pr_number") == work.binding.pr_number)
                        or work.reference == f"issue-{decision.get('issue_number')}")):
            raise LocalObservationError("identity_mismatch")
        # A proposed controller action is intent only. Its done-label verdicts,
        # head prefix, prose and merge recommendation confer no authority.
        assigned = decision.get("lane_id")
        if assigned and work.assigned_provider is None:
            generated = _parse_timestamp(controller.get("generated_at"))
            if generated is None or generated > instant:
                raise LocalObservationError("invalid_timestamp")
            work = replace(work, assigned_provider=assigned,
                           observed_at=min(work.observed_at, generated))
    return LocalObservationInput(work=replace(
        work, runs=tuple(adapted_runs), evidence=tuple(adapted_evidence),
    ))


def hosted_work_input(
    work: LocalWorkObservation, observation: RemoteWorkObservation, *, expected_round: int,
    evidence: Sequence[RemoteEvidence] = (), controller: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> LocalObservationInput:
    """Hosted work orders use exactly the same adapter as any other provider."""
    if (observation.repository != work.binding.repository
            or work.reference != f"issue-{observation.issue}"
            or observation.round_number != expected_round):
        raise LocalObservationError("identity_mismatch")
    binding = work.binding
    if observation.github_available and observation.pr_number is not None:
        if binding.pr_number not in (None, observation.pr_number):
            raise LocalObservationError("identity_mismatch")
        binding = replace(binding, pr_number=observation.pr_number, head_sha=observation.head_sha)
    work = replace(work, binding=binding)
    current_pr = LocalEvidenceObservation(
        "merge", observation.pr_state if observation.pr_state in {"open", "merged"} else "unknown",
        binding, observation.session.checked_at, source_kind="github",
        source_available=observation.github_available,
    )
    snapshot = remote_work_input(
        work, round_number=expected_round,
        runs=(RemoteRun(binding, expected_round, observation.session),), current_pr=current_pr,
        evidence=evidence, controller=controller, now=now,
    )
    if observation.implementation_verified and observation.github_available:
        # This is a separate implementation observation, not a claim that the
        # remote writer stopped. A resumable provider can still be running.
        implementation = LocalRunObservation(
            _opaque(f"{binding}\0{expected_round}\0{observation.generation}", "implementation"),
            binding, observation.session.provider, "builder", "implementation_complete", "observed",
            observation.session.checked_at, source_kind="github",
            checked_at=observation.session.checked_at,
        )
        snapshot = replace(snapshot, work=replace(snapshot.work, runs=(*snapshot.work.runs, implementation)))
    return snapshot
