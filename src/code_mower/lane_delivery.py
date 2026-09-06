"""Provider-neutral delivery and recovery contract for local builder lanes.

The local builder runner (``tools/lanes/run_mac_lane.sh``) used to treat a
provider exit code of ``0`` as a successful unit of work. Dogfooding showed
three gaps behind that assumption:

1. A provider can exit ``0`` without ever producing the commit, push, or PR
   head transition the unit was dispatched for.
2. Interrupting the parent runner can orphan the provider's process group.
3. An orchestrator recovery handoff had no explicit, auditable way to target a
   PR branch owned by another lane, so the only safe workaround was a manual
   commit transplant.

This module holds the provider-neutral pieces of the fix:

* :func:`classify_delivery` decides success from a validated issue/PR/head
  transition, never from the provider exit code alone.
* :func:`supervise_process` and :func:`terminate_process_group` run a provider
  in a dedicated process group and terminate/reap the whole group on timeout,
  interruption, and output overflow.
* :func:`validate_handoff` and :func:`authorize_branch_write` implement the
  explicit recovery handoff and reject implicit cross-lane takeover. A handoff
  only authorizes a branch the named source lane actually owns.
* :func:`scan_auth_material` keeps provider prompts free of instructions that
  would make a provider discover or read auth material; GitHub mutations are
  brokered by the runner instead.
* :func:`build_delivery_outcome_event` records a metadata-only outcome for
  Board/productivity reporting. No prompts, transcripts, stdout/stderr, auth
  output, local paths, or secrets are ever recorded.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from code_mower import __version__


DELIVERY_OUTCOME_SCHEMA = "code_mower.laneDeliveryOutcome.v1"
DEFAULT_DELIVERY_OUTCOME_DIR = Path(".code-mower/lane-delivery")

#: Transitions the runner can observe or validate for itself.
TRANSITION_PR_OPENED = "pr_opened"
TRANSITION_HEAD_ADVANCED = "head_advanced"
TRANSITION_NO_CHANGE = "no_change"
TRANSITION_OWNER_ACTION = "owner_action"
TRANSITION_NONE = "none"

#: No transition could be observed because a snapshot is missing state the
#: runner failed to fetch. Distinct from ``none``, which is an observed
#: not-moved, and never a delivery.
TRANSITION_UNKNOWN = "unknown"

DELIVERING_TRANSITIONS = frozenset(
    {
        TRANSITION_PR_OPENED,
        TRANSITION_HEAD_ADVANCED,
        TRANSITION_NO_CHANGE,
        TRANSITION_OWNER_ACTION,
    }
)

#: Bounded outcomes a unit may declare when it produced no new PR/head state.
DECLARED_OUTCOMES = frozenset({"", TRANSITION_NO_CHANGE, TRANSITION_OWNER_ACTION})

OWNER_ACTION_LABEL = "needs-owner"

#: Supervisor exit codes. 124 matches coreutils `timeout` so existing runner
#: handling for the wall-clock cap keeps working unchanged.
EXIT_TIMEOUT = 124
EXIT_OUTPUT_OVERFLOW = 125
EXIT_INTERRUPTED = 130

#: How a supervised run ended. ``completed`` and ``descendants_held_output``
#: both mean the provider chose its own exit code, so that code is the
#: provider's own verdict on the run.
SUPERVISION_COMPLETED = "completed"
SUPERVISION_DESCENDANTS_HELD_OUTPUT = "descendants_held_output"

#: Reasons where the supervisor, not the provider, ended the run. The exit code
#: is then the supervisor's own, so it says nothing about whether the provider
#: delivered before it was stopped, and classification goes by the observed
#: GitHub transition alone.
SUPERVISION_CAP_REASONS = frozenset({"timeout", "output_overflow", "interrupted"})

SUPERVISION_REASONS = frozenset(
    {SUPERVISION_COMPLETED, SUPERVISION_DESCENDANTS_HELD_OUTPUT}
) | SUPERVISION_CAP_REASONS

DEFAULT_MAX_LOG_BYTES = 32 * 1024 * 1024
DEFAULT_TERM_GRACE_SECONDS = 10.0

#: How long output may keep arriving after the direct provider exits. A
#: background descendant that inherited the provider's stdout holds the pipe
#: open, so waiting for EOF would wait for the full lane timeout.
DEFAULT_DESCENDANT_DRAIN_SECONDS = 2.0

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PR_REF_RE = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[0-9]+)$")
LANE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

#: Prompt text that would push a provider into discovering or reading auth
#: material. Rules are matched by name so a report never echoes the match.
AUTH_MATERIAL_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gh_auth_token_command", re.compile(r"\bgh\s+auth\s+token\b", re.IGNORECASE)),
    ("git_credential_command", re.compile(r"\bgit\s+credential\s+(?:fill|approve|get)\b", re.IGNORECASE)),
    ("credential_helper_output", re.compile(r"credential[._-]?helper", re.IGNORECASE)),
    ("gh_hosts_file", re.compile(r"gh/hosts\.(?:yml|yaml)", re.IGNORECASE)),
    ("netrc_file", re.compile(r"(?:^|[\s./~])\.netrc\b", re.IGNORECASE)),
    ("keychain_lookup", re.compile(r"\bsecurity\s+find-(?:generic|internet)-password\b", re.IGNORECASE)),
    ("token_env_echo", re.compile(r"\b(?:echo|printf|printenv|env)\b[^\n]{0,40}\$\{?(?:GITHUB_TOKEN|GH_TOKEN|DISPATCH_TOKEN)\b", re.IGNORECASE)),
    ("token_env_assignment", re.compile(r"\b(?:GITHUB_TOKEN|GH_TOKEN|DISPATCH_TOKEN|ANTHROPIC_API_KEY|OPENAI_API_KEY)\s*=\s*\S", re.IGNORECASE)),
    ("token_file_read", re.compile(r"\b(?:cat|less|head|tail)\s+[^\n]{0,40}(?:token|credential)", re.IGNORECASE)),
    ("private_key_file", re.compile(r"\bid_(?:rsa|ecdsa|ed25519)\b", re.IGNORECASE)),
)

#: Characters a bearer credential value is built from.
_TOKEN_VALUE_CHARS = r"A-Za-z0-9_./+=~-"

#: A bearer credential is the authentication scheme, then whitespace, then a
#: credential-shaped value: at least ``_MIN_TOKEN_VALUE_CHARS`` characters from
#: the token alphabet, of which at least one is not a letter. Requiring the
#: whitespace is what makes the scheme form match at all; requiring a non-letter
#: keeps ordinary prose that happens to follow the word "bearer" ("bearer of
#: bad news", "bearer authentication") out of the rule. The search window is
#: bounded because metadata values are length-capped before these rules run.
#: A ``ghp_``-style prefix is itself the evidence, so the value after it keeps
#: the original, lower threshold rather than the bearer form's.
_MIN_TOKEN_VALUE_CHARS = 12
_MIN_PREFIXED_TOKEN_CHARS = 8
_MAX_TOKEN_VALUE_CHARS = 255

#: Metadata values must never smuggle transcripts, paths, or secrets into a
#: recorded outcome. Applied to every string leaf of an outcome event.
_UNSAFE_METADATA_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("newline", re.compile(r"[\r\n]")),
    ("absolute_path", re.compile(r"(?:^|\s)(?:/|~/|[A-Za-z]:\\)")),
    ("home_path_segment", re.compile(r"(?:Users|home)/[^/\s]+", re.IGNORECASE)),
    ("secret_assignment", re.compile(r"\b[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)\b\s*[:=]\s*\S", re.IGNORECASE)),
    (
        "bearer_token",
        re.compile(
            rf"\bbearer\s+"
            rf"(?=[{_TOKEN_VALUE_CHARS}]{{{_MIN_TOKEN_VALUE_CHARS}}})"
            rf"[{_TOKEN_VALUE_CHARS}]{{0,{_MAX_TOKEN_VALUE_CHARS}}}[0-9_./+=~-]",
            re.IGNORECASE,
        ),
    ),
    (
        "github_token_prefix",
        re.compile(
            rf"\bgh[pousr]_[{_TOKEN_VALUE_CHARS}]"
            rf"{{{_MIN_PREFIXED_TOKEN_CHARS},{_MAX_TOKEN_VALUE_CHARS}}}",
            re.IGNORECASE,
        ),
    ),
)

_MAX_METADATA_VALUE_CHARS = 200


class LaneDeliveryError(ValueError):
    """Raised when a delivery, handoff, or metadata contract is violated."""


# ---------------------------------------------------------------------------
# State snapshots and delivery classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetState:
    """Issue/PR state observed by the runner around a provider invocation.

    Only the fields the runner can validate for itself are carried. ``pr_number``
    and ``head_sha`` are empty strings when no PR exists yet.

    ``snapshot_complete`` is how the producer says whether every lookup behind
    this snapshot actually succeeded. An empty ``pr_number`` or ``head_sha``
    means "observed absent" only when it is true; a failed GitHub read must set
    it false rather than leave the field empty, because an empty value is
    otherwise indistinguishable from real absence and would fabricate a
    transition. Snapshots loaded from a file must state it explicitly; see
    :func:`_load_state`.
    """

    kind: str
    number: str
    pr_number: str = ""
    head_sha: str = ""
    pr_state: str = ""
    labels: tuple[str, ...] = ()
    runner_comment_id: str = ""
    snapshot_complete: bool = True

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "TargetState":
        kind = _text(payload.get("kind"))
        if kind not in {"issue", "pr"}:
            raise LaneDeliveryError("state kind must be issue or pr")
        number = _text(payload.get("number"))
        if not number.isdigit():
            raise LaneDeliveryError("state number must be a positive integer")
        pr_number = _text(payload.get("pr_number"))
        if pr_number and not pr_number.isdigit():
            raise LaneDeliveryError("state pr_number must be a positive integer")
        head_sha = _text(payload.get("head_sha")).lower()
        if head_sha and not SHA_RE.match(head_sha):
            raise LaneDeliveryError("state head_sha must be a 40-character sha")
        labels = tuple(
            sorted({_text(label) for label in payload.get("labels") or () if _text(label)})
        )
        snapshot_complete = payload.get("snapshot_complete", True)
        if not isinstance(snapshot_complete, bool):
            raise LaneDeliveryError("state snapshot_complete must be a JSON boolean")
        return cls(
            kind=kind,
            number=number,
            pr_number=pr_number,
            head_sha=head_sha,
            pr_state=_text(payload.get("pr_state")).upper(),
            labels=labels,
            runner_comment_id=_text(payload.get("runner_comment_id")),
            snapshot_complete=snapshot_complete,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "number": self.number,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "pr_state": self.pr_state,
            "labels": list(self.labels),
            "runner_comment_id": self.runner_comment_id,
            "snapshot_complete": self.snapshot_complete,
        }


@dataclass(frozen=True)
class DeliveryOutcome:
    delivered: bool
    transition: str
    reason: str
    declared_outcome: str = ""
    provider_exit: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "delivered": self.delivered,
            "transition": self.transition,
            "reason": self.reason,
            "declared_outcome": self.declared_outcome,
        }


def observed_transition(before: TargetState, after: TargetState) -> str:
    """Return the PR/head transition the runner observed for itself.

    Both snapshots must describe the same unit. A transition is the difference
    between two readings of one target; between two different targets the same
    subtraction is meaningless, and it is meaningless in the direction that
    invents delivery — one target's open PR against another's absent one reads
    as ``pr_opened``, and two unrelated heads read as ``head_advanced``. That is
    a caller wiring its own snapshots up wrong, not an observation about either
    unit, so it raises rather than resolving to a transition. Fail-closed does
    not apply: there is no unit here to report as undelivered.

    Returns :data:`TRANSITION_UNKNOWN` when either snapshot is incomplete. A
    failed lookup leaves ``pr_number``/``head_sha`` empty, and comparing an
    empty value against a real one would read as ``pr_opened`` or
    ``head_advanced`` for a target that never moved.
    """

    if before.kind != after.kind or before.number != after.number:
        raise LaneDeliveryError(
            "before and after snapshots must describe the same target: "
            f"before is {before.kind} #{before.number}, "
            f"after is {after.kind} #{after.number}"
        )
    if not before.snapshot_complete or not after.snapshot_complete:
        return TRANSITION_UNKNOWN
    if after.pr_number and not before.pr_number:
        return TRANSITION_PR_OPENED
    if (
        after.pr_number
        and before.pr_number
        and after.pr_number == before.pr_number
        and after.head_sha
        and after.head_sha != before.head_sha
    ):
        return TRANSITION_HEAD_ADVANCED
    return TRANSITION_NONE


def classify_delivery(
    before: TargetState,
    after: TargetState,
    *,
    provider_exit: int,
    declared_outcome: str = "",
    supervision_reason: str = SUPERVISION_COMPLETED,
) -> DeliveryOutcome:
    """Classify a unit of work from state transition, not exit code alone.

    A build or fix round is delivered when the runner observes a new PR or a
    new head on the lane's PR. Otherwise the unit may only pass with a bounded
    declared outcome that the runner can validate from its own GitHub
    operations: ``no_change`` requires a runner-posted comment, and
    ``owner_action`` additionally requires the owner-blocking label.

    ``supervision_reason`` says who ended the run, because the exit code alone
    cannot. When the supervisor stopped the provider — the wall-clock cap,
    output overflow, or interruption — the exit code is the supervisor's, so it
    is not evidence either way and the observed transition decides on its own:
    work that reached GitHub before the cap fired is delivered, and a cap that
    produced nothing is not. A provider that chose its own nonzero exit is a
    failed unit whatever the target looks like, and a run the supervisor
    stopped never gets to declare a bounded outcome, since a half-written
    declaration is exactly what a killed provider can leave behind.

    Classification fails closed on an incomplete snapshot. If a GitHub read
    behind either snapshot failed, nothing here — not the transition, and not
    the comment and label a declared outcome is validated against — can be
    trusted, so the unit is undelivered rather than guessed at.

    Two snapshots that name different targets raise instead. See
    :func:`observed_transition`: there is no single unit to classify, so there
    is no unit to record an undelivered outcome against either.
    """

    declared = _text(declared_outcome)
    if declared not in DECLARED_OUTCOMES:
        raise LaneDeliveryError(
            "declared outcome must be empty, "
            f"{TRANSITION_NO_CHANGE}, or {TRANSITION_OWNER_ACTION}"
        )
    supervision = _text(supervision_reason) or SUPERVISION_COMPLETED
    if supervision not in SUPERVISION_REASONS:
        raise LaneDeliveryError(
            "supervision reason must be one of: "
            + ", ".join(sorted(SUPERVISION_REASONS))
        )
    transition = observed_transition(before, after)

    if not before.snapshot_complete or not after.snapshot_complete:
        return DeliveryOutcome(
            delivered=False,
            transition=TRANSITION_UNKNOWN,
            reason="target_snapshot_unavailable",
            declared_outcome=declared,
            provider_exit=provider_exit,
        )

    if supervision in SUPERVISION_CAP_REASONS:
        if transition in {TRANSITION_PR_OPENED, TRANSITION_HEAD_ADVANCED}:
            return DeliveryOutcome(
                delivered=True,
                transition=transition,
                reason="observed_state_transition",
                declared_outcome=declared,
                provider_exit=provider_exit,
            )
        return DeliveryOutcome(
            delivered=False,
            transition=transition,
            reason="supervision_ended_without_delivery",
            declared_outcome=declared,
            provider_exit=provider_exit,
        )

    if provider_exit != 0:
        return DeliveryOutcome(
            delivered=False,
            transition=transition,
            reason="provider_exit_nonzero",
            declared_outcome=declared,
            provider_exit=provider_exit,
        )

    if transition in {TRANSITION_PR_OPENED, TRANSITION_HEAD_ADVANCED}:
        return DeliveryOutcome(
            delivered=True,
            transition=transition,
            reason="observed_state_transition",
            declared_outcome=declared,
            provider_exit=provider_exit,
        )

    if declared == TRANSITION_NO_CHANGE:
        if not after.runner_comment_id:
            return DeliveryOutcome(
                delivered=False,
                transition=TRANSITION_NONE,
                reason="no_change_missing_runner_comment",
                declared_outcome=declared,
                provider_exit=provider_exit,
            )
        return DeliveryOutcome(
            delivered=True,
            transition=TRANSITION_NO_CHANGE,
            reason="validated_no_change",
            declared_outcome=declared,
            provider_exit=provider_exit,
        )

    if declared == TRANSITION_OWNER_ACTION:
        if OWNER_ACTION_LABEL not in after.labels:
            return DeliveryOutcome(
                delivered=False,
                transition=TRANSITION_NONE,
                reason="owner_action_missing_label",
                declared_outcome=declared,
                provider_exit=provider_exit,
            )
        if not after.runner_comment_id:
            return DeliveryOutcome(
                delivered=False,
                transition=TRANSITION_NONE,
                reason="owner_action_missing_runner_comment",
                declared_outcome=declared,
                provider_exit=provider_exit,
            )
        return DeliveryOutcome(
            delivered=True,
            transition=TRANSITION_OWNER_ACTION,
            reason="validated_owner_action",
            declared_outcome=declared,
            provider_exit=provider_exit,
        )

    return DeliveryOutcome(
        delivered=False,
        transition=TRANSITION_NONE,
        reason="exit_zero_without_delivery",
        declared_outcome=declared,
        provider_exit=provider_exit,
    )


# ---------------------------------------------------------------------------
# Process-group supervision
# ---------------------------------------------------------------------------


#: ``killpg`` reports an empty process group differently per platform. Linux
#: returns ``ESRCH`` and reserves ``EPERM`` for a group whose members exist but
#: cannot be signalled. Darwin's BSD ``killpg(3)`` returns ``EPERM`` for a group
#: with no members at all, so on Darwin ``EPERM`` means drained, not "alive but
#: out of reach". Getting this wrong is what made cleanup burn the whole TERM
#: grace period and then raise ``PermissionError`` from the SIGKILL escalation.
_EMPTY_GROUP_ERRNOS: frozenset[int] = (
    frozenset({errno.ESRCH, errno.EPERM})
    if sys.platform == "darwin"
    else frozenset({errno.ESRCH})
)

#: Errnos that mean a cleanup signal reached nothing. Cleanup races the group it
#: is tearing down, so the last member can exit between the liveness probe and
#: the signal; both platforms' "nothing there" errnos are the outcome cleanup
#: wanted, not a failure to propagate out of the runner.
_UNSIGNALABLE_ERRNOS = frozenset({errno.ESRCH, errno.EPERM})


def _default_is_group_alive(pgid: int) -> bool:
    """Return whether ``pgid`` still has a member this process could signal."""

    try:
        os.killpg(pgid, 0)
    except OSError as exc:
        if exc.errno in _EMPTY_GROUP_ERRNOS:
            return False
        raise
    return True


def terminate_process_group(
    pgid: int,
    *,
    grace_seconds: float = DEFAULT_TERM_GRACE_SECONDS,
    poll_interval: float = 0.1,
    killpg: Callable[[int, int], None] | None = None,
    is_group_alive: Callable[[int], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, ...]:
    """Terminate and reap an entire provider process group.

    Sends ``SIGTERM`` to the group, waits up to ``grace_seconds`` for it to
    drain, then escalates to ``SIGKILL``. Returns the signals actually
    delivered so callers can record a metadata-only cleanup trace. Refuses to
    signal the caller's own process group.

    Cleanup fails closed: a group that vanishes or becomes unsignalable while
    it is being torn down is reported as having received nothing further, never
    by raising out of the runner's cleanup path.
    """

    if pgid <= 1:
        raise LaneDeliveryError("refusing to signal process group id <= 1")
    if pgid == os.getpgrp():
        raise LaneDeliveryError("refusing to signal the runner's own process group")

    send = killpg if killpg is not None else os.killpg
    alive = is_group_alive if is_group_alive is not None else _default_is_group_alive
    sent: list[str] = []

    def _signal(sig: int, name: str) -> bool:
        """Send ``sig`` to the group and report whether it reached anything."""

        try:
            send(pgid, sig)
        except OSError as exc:
            if exc.errno in _UNSIGNALABLE_ERRNOS:
                return False
            raise
        sent.append(name)
        return True

    if not _signal(signal.SIGTERM, "SIGTERM"):
        return tuple(sent)

    deadline = monotonic() + max(0.0, grace_seconds)
    while monotonic() < deadline:
        if not alive(pgid):
            return tuple(sent)
        sleep(poll_interval)

    if alive(pgid):
        _signal(signal.SIGKILL, "SIGKILL")
    return tuple(sent)


@dataclass
class SupervisionResult:
    exit_code: int
    timed_out: bool = False
    overflowed: bool = False
    interrupted: bool = False
    signals_sent: tuple[str, ...] = ()
    output_bytes: int = 0
    descendants_held_output: bool = False

    @property
    def reason(self) -> str:
        if self.timed_out:
            return "timeout"
        if self.overflowed:
            return "output_overflow"
        if self.interrupted:
            return "interrupted"
        if self.descendants_held_output:
            return SUPERVISION_DESCENDANTS_HELD_OUTPUT
        return SUPERVISION_COMPLETED


def _wait_for_group_exit(pgid: int, *, deadline_seconds: float = 5.0) -> bool:
    """Wait, bounded, for a terminated provider group to drain.

    Only the supervisor's direct child is its to reap, and ``supervise_process``
    already waits on that one. Anything the provider spawned is reparented to
    init the moment the provider dies, so this polls group liveness rather than
    calling ``waitpid(-1)``, which would steal the exit status of an unrelated
    child of the runner.
    """

    end = time.monotonic() + deadline_seconds
    while True:
        if not _default_is_group_alive(pgid):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.05)


def supervise_process(
    argv: Sequence[str],
    *,
    log_path: Path,
    timeout_seconds: float,
    max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin_path: Path | None = None,
    term_grace_seconds: float = DEFAULT_TERM_GRACE_SECONDS,
    descendant_drain_seconds: float = DEFAULT_DESCENDANT_DRAIN_SECONDS,
) -> SupervisionResult:
    """Run a provider CLI in its own process group with bounded output.

    The child is started in a new session, so the provider and everything it
    spawns share one process group. Timeout, output overflow, and interruption
    of the runner all terminate and reap that whole group instead of leaving
    inert transports behind.

    The provider's own exit ends the run even when its output pipe stays open.
    A background descendant that inherited stdout holds the write end, so
    waiting for EOF would wait out the entire lane timeout and then report a
    timeout for a provider that finished — a lingering transport is what this
    supervisor exists to clean up, not a reason to stall behind one. Output
    already in flight is drained for ``descendant_drain_seconds`` first, and
    the group is then terminated and reaped as usual.

    The mirror case — a provider that closes its output and keeps working — is
    waited on rather than polled at speed: with no descriptor left to select
    on, the loop waits on the child itself.
    """

    if timeout_seconds <= 0:
        raise LaneDeliveryError("timeout_seconds must be greater than zero")
    if max_log_bytes <= 0:
        raise LaneDeliveryError("max_log_bytes must be greater than zero")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    interrupted = {"value": False}

    def _on_signal(_signum: int, _frame: object) -> None:
        interrupted["value"] = True

    previous_handlers: dict[int, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            previous_handlers[sig] = signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass

    stdin_handle = None
    proc: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    timed_out = False
    overflowed = False
    descendants_held_output = False
    written = 0
    exit_code = 1
    signals_sent: tuple[str, ...] = ()
    try:
        stdin_handle = (
            stdin_path.open("rb") if stdin_path is not None else subprocess.DEVNULL
        )
        proc = subprocess.Popen(  # noqa: S603 - argv is runner-owned
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            stdin=stdin_handle,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        child = proc
        pgid = os.getpgid(child.pid)

        def _group_alive(group_id: int) -> bool:
            # Reap the direct child first. A zombie group leader still counts
            # as a group member on Linux, so probing without reaping would
            # burn the whole TERM grace period on an already-drained group.
            child.poll()
            return _default_is_group_alive(group_id)

        deadline = time.monotonic() + timeout_seconds
        drain_deadline: float | None = None
        selector = selectors.DefaultSelector()
        assert proc.stdout is not None
        selector.register(proc.stdout, selectors.EVENT_READ)
        with log_path.open("wb") as log_handle:
            while True:
                if interrupted["value"]:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # A provider that already exited did not time out, however
                    # close to the cap its leftovers kept the pipe open.
                    if drain_deadline is not None:
                        descendants_held_output = True
                    else:
                        timed_out = True
                    break
                wait = min(remaining, 0.5)
                if drain_deadline is not None:
                    # Draining a pipe an exited provider left behind: poll
                    # briefly so the window is the drain, not the select.
                    wait = min(wait, 0.05)
                if selector.get_map():
                    for _key, _events in selector.select(timeout=wait):
                        chunk = os.read(proc.stdout.fileno(), 65536)
                        if not chunk:
                            selector.unregister(proc.stdout)
                            break
                        if written + len(chunk) > max_log_bytes:
                            log_handle.write(chunk[: max(0, max_log_bytes - written)])
                            written = max_log_bytes
                            overflowed = True
                            break
                        log_handle.write(chunk)
                        written += len(chunk)
                else:
                    # The provider closed its output and is still running, so
                    # there is nothing left to select on. Wait on the process
                    # instead of on an empty selector: how long a selector with
                    # no registered descriptor waits is the backend's business,
                    # and a wait that returns at once would spin this loop at
                    # full CPU for the rest of the provider's run. Waiting on
                    # the child also wakes as soon as it exits.
                    try:
                        proc.wait(timeout=wait)
                    except subprocess.TimeoutExpired:
                        pass
                if overflowed:
                    break
                if proc.poll() is None:
                    continue
                if not selector.get_map():
                    break
                # The provider is gone but its stdout is not: something it
                # spawned inherited the write end. Drain what is already in
                # flight, then stop waiting on a pipe only the leftovers hold.
                now = time.monotonic()
                if drain_deadline is None:
                    drain_deadline = now + max(0.0, descendant_drain_seconds)
                elif now >= drain_deadline:
                    descendants_held_output = True
                    break

        if timed_out or overflowed or interrupted["value"] or descendants_held_output:
            signals_sent = terminate_process_group(
                pgid, grace_seconds=term_grace_seconds, is_group_alive=_group_alive
            )
        try:
            proc.wait(timeout=term_grace_seconds + 5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            signals_sent = signals_sent + terminate_process_group(
                pgid, grace_seconds=1.0, is_group_alive=_group_alive
            )
            proc.wait(timeout=5)
        # Sweep any grandchildren the provider left behind, even on a clean
        # exit: an orphaned transport is exactly the failure this closes.
        if _default_is_group_alive(pgid):
            signals_sent = signals_sent + terminate_process_group(
                pgid, grace_seconds=term_grace_seconds, is_group_alive=_group_alive
            )
        _wait_for_group_exit(pgid)
        exit_code = proc.returncode if proc.returncode is not None else 1
        if exit_code < 0:
            exit_code = 128 - exit_code
    finally:
        # Every terminal path closes the supervisor's own descriptors: the
        # provider pipe, the selector, and any stdin file handle.
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()
        if selector is not None:
            selector.close()
        if stdin_handle not in (None, subprocess.DEVNULL):
            stdin_handle.close()  # type: ignore[union-attr]
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover
                pass

    if timed_out:
        exit_code = EXIT_TIMEOUT
    elif overflowed:
        exit_code = EXIT_OUTPUT_OVERFLOW
    elif interrupted["value"]:
        exit_code = EXIT_INTERRUPTED

    return SupervisionResult(
        exit_code=exit_code,
        timed_out=timed_out,
        overflowed=overflowed,
        interrupted=interrupted["value"],
        signals_sent=signals_sent,
        output_bytes=written,
        descendants_held_output=descendants_held_output,
    )


# ---------------------------------------------------------------------------
# Explicit recovery handoff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Handoff:
    source_lane: str
    destination_lane: str
    target_pr: str
    expected_head: str
    target_branch: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_lane": self.source_lane,
            "destination_lane": self.destination_lane,
            "target_pr": self.target_pr,
            "expected_head": self.expected_head,
            "target_branch": self.target_branch,
        }


def validate_handoff(
    *,
    source_lane: str,
    destination_lane: str,
    target_pr: str,
    expected_head: str,
    running_lane: str,
    repo: str,
    observed_head: str,
    target_branch: str,
    source_branch_prefixes: Iterable[str],
) -> Handoff:
    """Validate an explicit orchestrator recovery handoff.

    Every field is required. The handoff must name a different source lane, be
    addressed to the lane that is actually running, point at a PR in the repo
    under work, and pin the head the orchestrator inspected. A stale
    ``expected_head`` is rejected rather than silently retargeted.

    A handoff also has to prove the branch it hands over is the source lane's
    to give. ``source_branch_prefixes`` is the source lane's configured branch
    prefixes, and ``target_branch`` must carry one of them. Without that check
    a handoff naming any cooperating lane as its source would authorize a write
    to any foreign branch at all -- another builder's, or a bot's -- which is
    the single-writer guarantee this contract exists to keep.
    """

    source = _text(source_lane).lower()
    destination = _text(destination_lane).lower()
    running = _text(running_lane).lower()
    if not LANE_RE.match(source):
        raise LaneDeliveryError("handoff requires a valid --handoff-source-lane")
    if not LANE_RE.match(destination):
        raise LaneDeliveryError("handoff requires a valid --handoff-destination-lane")
    if source == destination:
        raise LaneDeliveryError("handoff source and destination lanes must differ")
    if destination != running:
        raise LaneDeliveryError(
            f"handoff destination lane {destination} does not match running lane {running}"
        )

    match = PR_REF_RE.match(_text(target_pr))
    if not match:
        raise LaneDeliveryError("handoff --handoff-target-pr must be owner/repo#number")
    if not REPO_RE.match(_text(repo)):
        raise LaneDeliveryError("handoff requires --repo as owner/repo")
    if match.group("repo").lower() != _text(repo).lower():
        raise LaneDeliveryError(
            "handoff target PR repository does not match the repository under work"
        )

    expected = _text(expected_head).lower()
    if not SHA_RE.match(expected):
        raise LaneDeliveryError(
            "handoff --handoff-expected-head must be a 40-character sha"
        )
    observed = _text(observed_head).lower()
    if not SHA_RE.match(observed):
        raise LaneDeliveryError("handoff needs an observed 40-character PR head sha")
    if expected != observed:
        raise LaneDeliveryError(
            "handoff expected head does not match the current PR head; "
            "re-issue the handoff against the current head"
        )

    branch = _text(target_branch)
    if not branch:
        raise LaneDeliveryError(
            "handoff requires --target-branch; a handoff authorizes exactly one "
            "branch and has nothing to authorize without it"
        )
    prefixes = tuple(
        text for text in (_text(prefix) for prefix in source_branch_prefixes) if text
    )
    if not prefixes:
        raise LaneDeliveryError(
            f"handoff source lane {source} has no configured branch prefixes; "
            "only a configured builder lane can hand a branch over"
        )
    if not any(branch.startswith(prefix) for prefix in prefixes):
        raise LaneDeliveryError(
            f"handoff target branch {branch} is not owned by source lane {source} "
            f"(expected branch prefix {', '.join(prefixes)}); a lane may only hand "
            "over a branch it owns"
        )

    return Handoff(
        source_lane=source,
        destination_lane=destination,
        target_pr=f"{match.group('repo')}#{match.group('number')}",
        expected_head=expected,
        target_branch=branch,
    )


def authorize_branch_write(
    *,
    lane: str,
    branch: str,
    lane_branch_prefixes: Iterable[str],
    handoff: Handoff | None = None,
) -> str:
    """Return the authority that permits this lane to write ``branch``.

    Normal single-writer enforcement is unchanged: a lane writes branches
    carrying its own prefixes. The only other authority is a validated explicit
    handoff naming that exact branch. Anything else is implicit cross-lane
    takeover and is rejected.
    """

    branch_name = _text(branch)
    if not branch_name:
        raise LaneDeliveryError("branch is required")
    for prefix in lane_branch_prefixes:
        prefix_text = _text(prefix)
        if prefix_text and branch_name.startswith(prefix_text):
            return "lane_prefix"
    if handoff is not None and handoff.target_branch == branch_name:
        return "explicit_handoff"
    raise LaneDeliveryError(
        f"refusing implicit cross-lane takeover: lane {_text(lane)} may not write "
        f"branch {branch_name} without an explicit recovery handoff"
    )


# ---------------------------------------------------------------------------
# Prompt auth-material hygiene
# ---------------------------------------------------------------------------


def scan_auth_material(text: str) -> tuple[str, ...]:
    """Return the names of auth-material rules a prompt matches.

    Only rule names are returned. The matched text is never echoed, so a report
    cannot itself leak a token or a credential path.
    """

    body = text or ""
    return tuple(name for name, pattern in AUTH_MATERIAL_RULES if pattern.search(body))


def assert_prompt_free_of_auth_material(text: str) -> None:
    """Raise if a provider prompt would send a provider hunting for secrets."""

    matches = scan_auth_material(text)
    if matches:
        raise LaneDeliveryError(
            "prompt contains auth-material discovery guidance: " + ", ".join(matches)
        )


# ---------------------------------------------------------------------------
# Metadata-only delivery outcome records
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _assert_safe_metadata(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_safe_metadata(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_safe_metadata(item, path=f"{path}[{index}]")
        return
    if isinstance(value, (bool, int, float)) or value is None:
        return
    if not isinstance(value, str):
        raise LaneDeliveryError(f"{path} must be a JSON scalar")
    if len(value) > _MAX_METADATA_VALUE_CHARS:
        raise LaneDeliveryError(f"{path} exceeds the metadata value length budget")
    for name, pattern in _UNSAFE_METADATA_RULES:
        if pattern.search(value):
            raise LaneDeliveryError(f"{path} looks like {name}; outcomes are metadata only")


def build_delivery_outcome_event(
    *,
    lane: str,
    repo: str,
    kind: str,
    number: str,
    outcome: DeliveryOutcome,
    supervision_reason: str = "completed",
    signals_sent: Iterable[str] = (),
    elapsed_seconds: float | None = None,
    user_interventions: int | None = None,
    handoff: Handoff | None = None,
    created_at: str = "",
) -> dict[str, Any]:
    """Build a metadata-only delivery outcome for Board/productivity reporting.

    The event carries provider exit, delivery transition, handoff shape,
    elapsed time, and intervention count. It never carries prompts,
    transcripts, stdout/stderr, auth output, local paths, or secrets, and
    :func:`_assert_safe_metadata` enforces that on every string leaf.
    """

    lane_text = _text(lane).lower()
    if not LANE_RE.match(lane_text):
        raise LaneDeliveryError("lane must be a short lowercase lane id")
    repo_text = _text(repo)
    if not REPO_RE.match(repo_text):
        raise LaneDeliveryError("repo must be owner/repo")
    if kind not in {"issue", "pr"}:
        raise LaneDeliveryError("kind must be issue or pr")
    number_text = _text(number)
    if not number_text.isdigit():
        raise LaneDeliveryError("number must be a positive integer")
    if elapsed_seconds is not None and elapsed_seconds < 0:
        raise LaneDeliveryError("elapsed_seconds must not be negative")
    if user_interventions is not None and user_interventions < 0:
        raise LaneDeliveryError("user_interventions must not be negative")

    event: dict[str, Any] = {
        "schema": DELIVERY_OUTCOME_SCHEMA,
        "event_id": f"lane-delivery-{uuid.uuid4().hex[:12]}",
        "created_at": _text(created_at) or _utc_now(),
        "code_mower_version": __version__,
        "lane": lane_text,
        "repo": repo_text,
        "target": {"kind": kind, "number": number_text},
        "provider": {
            "exit_code": int(outcome.provider_exit),
            "supervision": _text(supervision_reason) or "completed",
            "signals_sent": [_text(item) for item in signals_sent if _text(item)],
        },
        "delivery": outcome.as_dict(),
        "handoff": handoff.as_dict() if handoff is not None else None,
        "metrics": {},
    }
    if elapsed_seconds is not None:
        event["metrics"]["elapsed_seconds"] = round(float(elapsed_seconds), 3)
    if user_interventions is not None:
        event["metrics"]["user_interventions"] = int(user_interventions)

    _assert_safe_metadata(event, path="event")
    return event


def write_delivery_outcome_event(
    event: Mapping[str, Any], output: Path, *, force: bool = False
) -> Path:
    if output.exists() and not force:
        raise LaneDeliveryError(f"{output.name} already exists; pass --force to overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(dict(event), allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_state(path: str) -> TargetState:
    """Load a snapshot written by the runner.

    ``snapshot_complete`` is required here rather than defaulted. A snapshot
    that reaches the CLI without saying whether its lookups succeeded came from
    a producer that does not know about the fail-closed contract, and silently
    reading it as complete is the failure mode this guard exists to stop.
    """

    if path == "-":
        payload = json.loads(sys.stdin.read() or "{}")
    else:
        payload = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    if not isinstance(payload, Mapping):
        raise LaneDeliveryError("state payload must be a JSON object")
    if "snapshot_complete" not in payload:
        raise LaneDeliveryError("state must state snapshot_complete explicitly")
    return TargetState.from_mapping(payload)


def _add_classify_parser(subparsers: Any) -> None:
    classify = subparsers.add_parser(
        "classify",
        help="Classify delivery from a validated state transition, not exit code.",
    )
    classify.add_argument("--before", required=True, help="Snapshot JSON path or - for stdin.")
    classify.add_argument("--after", required=True, help="Snapshot JSON path or - for stdin.")
    classify.add_argument("--provider-exit", type=int, required=True)
    classify.add_argument("--declared-outcome", default="")
    classify.add_argument("--lane", default="")
    classify.add_argument("--repo", default="")
    classify.add_argument(
        "--supervision",
        default=SUPERVISION_COMPLETED,
        choices=sorted(SUPERVISION_REASONS),
        help="How the supervised run ended; decides whether the exit code is the provider's.",
    )
    classify.add_argument("--signal", action="append", default=[])
    classify.add_argument("--elapsed-seconds", type=float)
    classify.add_argument("--user-interventions", type=int)
    classify.add_argument("--handoff", default="", help="Validated handoff JSON path.")
    classify.add_argument("--output", type=Path, help="Write the outcome event here.")
    classify.add_argument("--force", action="store_true")
    classify.add_argument("--json", action="store_true")


def _add_transition_parser(subparsers: Any) -> None:
    transition = subparsers.add_parser(
        "transition",
        help="Print the PR/head transition observed between two snapshots.",
    )
    transition.add_argument("--before", required=True, help="Snapshot JSON path or - for stdin.")
    transition.add_argument("--after", required=True, help="Snapshot JSON path or - for stdin.")


def _add_handoff_parser(subparsers: Any) -> None:
    handoff = subparsers.add_parser(
        "handoff",
        help="Validate an explicit orchestrator recovery handoff.",
    )
    handoff.add_argument("--lane", required=True, help="Lane actually running.")
    handoff.add_argument("--repo", required=True)
    handoff.add_argument("--source-lane", required=True)
    handoff.add_argument("--destination-lane", required=True)
    handoff.add_argument("--target-pr", required=True, help="owner/repo#number")
    handoff.add_argument("--expected-head", required=True)
    handoff.add_argument("--observed-head", required=True)
    handoff.add_argument("--target-branch", required=True)
    handoff.add_argument(
        "--source-branch-prefix",
        dest="source_branch_prefixes",
        action="append",
        default=[],
        required=True,
        metavar="PREFIX",
        help=(
            "A branch prefix configured for the source lane. Repeatable. The "
            "target branch must carry one of these."
        ),
    )
    handoff.add_argument("--output", type=Path)
    handoff.add_argument("--json", action="store_true")


def _add_scan_prompt_parser(subparsers: Any) -> None:
    scan = subparsers.add_parser(
        "scan-prompt",
        help="Reject provider prompts that would discover or read auth material.",
    )
    scan.add_argument("--prompt-file", required=True, type=Path)
    scan.add_argument("--json", action="store_true")


def _add_supervise_parser(subparsers: Any) -> None:
    supervise = subparsers.add_parser(
        "supervise",
        help="Run a provider CLI in its own process group with bounded output.",
    )
    supervise.add_argument("--log", required=True, type=Path)
    supervise.add_argument("--timeout-seconds", type=float, required=True)
    supervise.add_argument("--max-log-bytes", type=int, default=DEFAULT_MAX_LOG_BYTES)
    supervise.add_argument("--cwd", type=Path)
    supervise.add_argument("--stdin-file", type=Path)
    supervise.add_argument("--status-file", type=Path)
    # The remainder must not be named "command": that is the subparsers dest, and
    # argparse would overwrite the selected subcommand with the provider argv.
    supervise.add_argument(
        "provider_command",
        metavar="command",
        nargs=argparse.REMAINDER,
        help="Provider argv, after --.",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-mower lane-delivery")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_classify_parser(subparsers)
    _add_transition_parser(subparsers)
    _add_handoff_parser(subparsers)
    _add_scan_prompt_parser(subparsers)
    _add_supervise_parser(subparsers)
    args = parser.parse_args(argv)

    try:
        if args.command == "classify":
            return _classify_main(args)
        if args.command == "transition":
            return _transition_main(args)
        if args.command == "handoff":
            return _handoff_main(args)
        if args.command == "scan-prompt":
            return _scan_prompt_main(args)
        if args.command == "supervise":
            return _supervise_main(args)
    except LaneDeliveryError as exc:
        print(f"lane-delivery: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"lane-delivery: {type(exc).__name__}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled lane-delivery command: {args.command}")


def _classify_main(args: argparse.Namespace) -> int:
    before = _load_state(args.before)
    after = _load_state(args.after)
    outcome = classify_delivery(
        before,
        after,
        provider_exit=args.provider_exit,
        declared_outcome=args.declared_outcome,
        supervision_reason=args.supervision,
    )
    handoff = None
    if args.handoff:
        payload = json.loads(Path(args.handoff).read_text(encoding="utf-8") or "{}")
        handoff = Handoff(
            source_lane=_text(payload.get("source_lane")),
            destination_lane=_text(payload.get("destination_lane")),
            target_pr=_text(payload.get("target_pr")),
            expected_head=_text(payload.get("expected_head")),
            target_branch=_text(payload.get("target_branch")),
        )

    event = None
    if args.lane and args.repo:
        event = build_delivery_outcome_event(
            lane=args.lane,
            repo=args.repo,
            kind=after.kind,
            number=after.number,
            outcome=outcome,
            supervision_reason=args.supervision,
            signals_sent=args.signal,
            elapsed_seconds=args.elapsed_seconds,
            user_interventions=args.user_interventions,
            handoff=handoff,
        )
        output = args.output or (
            DEFAULT_DELIVERY_OUTCOME_DIR / f"{event['event_id']}.json"
        )
        write_delivery_outcome_event(event, output, force=args.force)

    if args.json:
        print(json.dumps(event or outcome.as_dict(), indent=2, sort_keys=True))
    else:
        print(
            f"delivery {'ok' if outcome.delivered else 'missing'}: "
            f"transition={outcome.transition} reason={outcome.reason}"
        )
    return 0 if outcome.delivered else 3


def _transition_main(args: argparse.Namespace) -> int:
    """Report the observed transition on its own, before anything acts on it.

    Classification answers "did this unit deliver" at the end of a run, which
    is too late for the one decision that has to be made in the middle of it:
    whether the runner may broker a bounded declared outcome. A provider that
    both wrote ``lane-outcome.json`` and pushed has delivered, and posting the
    declaration's comment or applying ``needs-owner`` on that run would leave
    the owner an owner-blocked pull request alongside a comment saying nothing
    changed. The runner therefore reads the transition first and brokers only
    when it observed none.

    The exit status says nothing about delivery -- ``0`` means the comparison
    was made, whatever it found. Only a pair that cannot be compared at all
    fails, and it fails the way :func:`observed_transition` does.
    """

    before = _load_state(args.before)
    after = _load_state(args.after)
    print(observed_transition(before, after))
    return 0


def _handoff_main(args: argparse.Namespace) -> int:
    handoff = validate_handoff(
        source_lane=args.source_lane,
        destination_lane=args.destination_lane,
        target_pr=args.target_pr,
        expected_head=args.expected_head,
        running_lane=args.lane,
        repo=args.repo,
        observed_head=args.observed_head,
        target_branch=args.target_branch,
        source_branch_prefixes=args.source_branch_prefixes,
    )
    payload = handoff.as_dict()
    _assert_safe_metadata(payload, path="handoff")
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"handoff accepted: {handoff.source_lane} -> {handoff.destination_lane} "
            f"on {handoff.target_pr}"
        )
    return 0


def _scan_prompt_main(args: argparse.Namespace) -> int:
    text = args.prompt_file.read_text(encoding="utf-8", errors="replace")
    matches = scan_auth_material(text)
    if args.json:
        print(json.dumps({"clean": not matches, "rules": list(matches)}, sort_keys=True))
    elif matches:
        print("prompt auth-material rules matched: " + ", ".join(matches), file=sys.stderr)
    else:
        print("prompt clean of auth-material discovery guidance")
    # 1 is a rule match; 2 stays reserved for a usage or execution failure, so a
    # caller can tell "this prompt is dirty" from "the scanner did not run".
    return 0 if not matches else 1


def _supervise_main(args: argparse.Namespace) -> int:
    command = list(args.provider_command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise LaneDeliveryError("supervise requires a command after --")
    result = supervise_process(
        command,
        log_path=args.log,
        timeout_seconds=args.timeout_seconds,
        max_log_bytes=args.max_log_bytes,
        cwd=args.cwd,
        stdin_path=args.stdin_file,
    )
    if args.status_file is not None:
        args.status_file.parent.mkdir(parents=True, exist_ok=True)
        args.status_file.write_text(
            json.dumps(
                {
                    "exit_code": result.exit_code,
                    "reason": result.reason,
                    "signals_sent": list(result.signals_sent),
                    "output_bytes": result.output_bytes,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
