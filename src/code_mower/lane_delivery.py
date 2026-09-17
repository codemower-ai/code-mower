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
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Sequence

if TYPE_CHECKING:
    from .builder_lineage_producer import Observation

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
    writer: Any = None,
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
        if writer is not None and writer.stop_requested():
            writer.finish(quiescent=True)
            return SupervisionResult(exit_code=EXIT_INTERRUPTED, interrupted=True)
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
        if writer is not None:
            writer.started(child.pid, pgid)

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
                if writer is not None and writer.stop_requested():
                    interrupted["value"] = True
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
        try:
            if proc is not None:
                # Control I/O failures must also clean descendants after their
                # leader exits. Only this supervisor's known group is touched.
                proc.poll()
                if _default_is_group_alive(proc.pid):
                    terminate_process_group(proc.pid, grace_seconds=term_grace_seconds)
                if proc.poll() is None:
                    proc.wait(timeout=term_grace_seconds + 5)
                if writer is not None:
                    writer.finish(quiescent=_wait_for_group_exit(proc.pid))
        finally:
            # Control-store failures do not skip descriptor/signal cleanup.
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
    source_ownership: Observation | None = None,
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
    if source_ownership is not None:
        from .builder_lineage import Chain, ContractError, Lineage, Target
        from .builder_lineage_producer import Observation
        try:
            target = Target(repo, int(match.group("number")), branch, observed)
            if not isinstance(source_ownership, Observation):
                raise ValueError
            chain, decision = source_ownership.chain, source_ownership.decision
            if not isinstance(chain, Chain) or not isinstance(decision, Lineage):
                raise ValueError
            if not chain.episodes or chain.target != target or decision.target != target:
                raise ValueError
            validated = Chain.from_arrivals(target, chain.episodes)
            contributors = tuple(sorted({lane for episode in validated.episodes
                for lane in (episode.source_lane, episode.destination_lane)}))
            final = validated.episodes[-1]
            if (decision.status != "ready" or decision.reason != "verified_lineage"
                    or final.resulting_head != observed or final.destination_lane != source
                    or decision.current_writer != source or decision.contributors != contributors):
                raise ValueError
        except (ContractError, ValueError, TypeError, AttributeError):
            raise LaneDeliveryError("handoff requires verified exact current source ownership") from None
    else:
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
        metavar="PREFIX",
        help=(
            "A branch prefix configured for the source lane. Repeatable. The "
            "target branch must carry one of these."
        ),
    )
    handoff.add_argument("--output", type=Path)
    handoff.add_argument("--json", action="store_true")
    handoff.add_argument("--lineage-store", type=Path)
    handoff.add_argument("--source-file", type=Path, help="Private bound source transport; required for takeover")
    handoff.add_argument("--state-dir", type=Path, help="Private handoff intent store")
    handoff.add_argument("--reserve-launch", action="store_true", help="Claim the verified destination launch once")


def _add_scan_prompt_parser(subparsers: Any) -> None:
    scan = subparsers.add_parser(
        "scan-prompt",
        help="Reject provider prompts that would discover or read auth material.",
    )
    scan.add_argument("--prompt-file", required=True, type=Path)
    scan.add_argument("--json", action="store_true")


def _add_writer_id_parser(subparsers: Any) -> None:
    writer_id = subparsers.add_parser(
        "writer-id",
        help="Derive the canonical lineage writer identity and supervised round ID.",
    )
    writer_id.add_argument("--lane", required=True)
    writer_id.add_argument("--repo", required=True)
    writer_id.add_argument("--run", default=None,
                           help="Caller-owned per-run suffix for the supervised round ID")


def _writer_id_main(args: argparse.Namespace) -> int:
    payload = {"lane": args.lane, "repo": args.repo,
               "writer": lineage_writer_id(args.lane, args.repo)}
    if args.run is not None:
        payload["round_id"] = lineage_writer_id(args.lane, args.repo, run=args.run)
    print(json.dumps(payload, sort_keys=True))
    return 0


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
    supervise.add_argument("--writer", help="Stable private alias for this supervisor invocation")
    supervise.add_argument("--writer-state-dir", type=Path)
    supervise.add_argument("--writer-repo")
    supervise.add_argument("--writer-lane")
    supervise.add_argument("--lineage-before", type=Path)
    supervise.add_argument("--lineage-issue", type=int,
                           help="Supervise an issue-targeted round that creates the pull request itself")
    supervise.add_argument("--lineage-branch",
                           help="The single unused branch an issue-targeted round may create")
    supervise.add_argument("--lineage-base")
    supervise.add_argument("--lineage-store", type=Path)
    supervise.add_argument("--lineage-create", action="store_true")
    supervise.add_argument("--lineage-writer")
    supervise.add_argument("--lineage-handoff", type=Path)
    supervise.add_argument("--lineage-handoff-root", type=Path)
    supervise.add_argument("--lineage-output", type=Path)
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
    _add_writer_id_parser(subparsers)
    admit = subparsers.add_parser("admit-builder", help="Check role admission against the trusted fresh-base checkout")
    admit.add_argument("--checkout", type=Path, required=True)
    admit.add_argument("--lane", required=True)
    admit.add_argument("--runtime-readiness", choices=("ready", "unchecked", "unavailable"), default="unchecked")
    runtime = subparsers.add_parser("runtime", help="Prepare bounded dedicated-checkout builder capabilities")
    runtime.add_argument("--checkout", type=Path, required=True)
    runtime.add_argument("--python", default="")
    runtime.add_argument("--codex", default="", help="Also verify the installed Codex sandbox without a model call")
    owner = subparsers.add_parser("lineage-owner", help="Resolve current ownership from trusted exact evidence")
    owner.add_argument("--repo", required=True)
    owner.add_argument("--pr", required=True, type=int)
    owner.add_argument("--lineage-store", type=Path)
    record = subparsers.add_parser("lineage-record", help="Attribute an exact delivered PR using trusted policy and history")
    record.add_argument("--repo", required=True)
    record.add_argument("--pr", required=True, type=int)
    record.add_argument("--base", required=True)
    record.add_argument("--lane", required=True, choices=("codex", "claude", "devin"))
    record.add_argument("--output", required=True, type=Path)
    subparsers.add_parser("lineage-capabilities", help="Refuse unsupported installed lineage APIs")
    args = parser.parse_args(argv)
    if args.command == "handoff" and not args.source_branch_prefixes and args.lineage_store is None:
        parser.error("handoff requires --source-branch-prefix unless exact source ownership is selected with --lineage-store")

    try:
        if args.command == "lineage-record":
            return _lineage_record_main(args)
        if args.command == "lineage-owner":
            return _lineage_owner_main(args)
        if args.command == "lineage-capabilities":
            from .provider_runners.lineage import require_capabilities
            require_capabilities()
            return 0
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
        if args.command == "writer-id":
            return _writer_id_main(args)
        if args.command == "admit-builder":
            return _admit_builder_main(args)
        if args.command == "runtime":
            from . import lane_runtime
            payload = lane_runtime.prepare(args.checkout, args.python)
            if args.codex:
                lane_runtime.preflight(args.checkout, args.codex, payload["codex_config"], payload["python"])
            print(json.dumps(payload))
            return 0
    except LaneDeliveryError as exc:
        print(f"lane-delivery: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"lane-delivery: {type(exc).__name__}", file=sys.stderr)
        return 2
    raise AssertionError(f"unhandled lane-delivery command: {args.command}")


def _admit_builder_main(args: argparse.Namespace) -> int:
    # The maintained runner has reset this dedicated checkout to the trusted
    # default branch, before any provider launch. Missing config means the
    # maintained defaults; unreadable or non-regular config never means defaults.
    from .config import load_config
    from .participants import configured_transports
    from .role_eligibility import decide_role, require_builder, require_role
    from .yaml_subset import ConfigError

    if not args.checkout.is_dir():
        raise LaneDeliveryError("builder role admission requires an existing trusted checkout")
    path = args.checkout / "code-mower.yml"
    try:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            configuration = {}
        else:
            if not stat.S_ISREG(mode):
                raise ConfigError("builder role admission requires regular trusted repository configuration")
            try:
                configuration = load_config(path)
            except (ConfigError, OSError, UnicodeError):
                raise ConfigError("builder role admission requires valid trusted repository configuration") from None
        if args.lane == "devin":
            if configured_transports(configuration).get("devin", "devin_cli") != "devin_cli":
                raise ConfigError("the local Devin runner cannot substitute for a selected hosted transport")
            decision = require_builder(config=configuration, transport="devin_cli",
                                       runtime=args.runtime_readiness)
        else:
            decision = decide_role(args.lane, "builder", config=configuration,
                                   runtime=args.runtime_readiness, bounded=True)
            require_role(decision, execution=True)
    except (ConfigError, OSError, UnicodeError) as exc:
        if isinstance(exc, ConfigError):
            raise LaneDeliveryError(str(exc)) from None
        raise LaneDeliveryError("trusted builder configuration is unavailable; inspect local permissions") from None
    print(json.dumps(decision, sort_keys=True))
    return 0


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
    from . import lane_handoff
    ownership = None
    if args.lineage_store:
        from .builder_lineage_producer import Observation, ProducerStore, GitHub
        from .audit_labeler_lib import lineage_decision, lineage_snapshot
        from .provider_runners.lineage import remote_policy
        io = GitHub()
        raw = io._json(f"repos/{args.repo}/pulls/{args.target_pr.split('#')[1]}")
        target, author, labels = lineage_snapshot(args.repo, int(args.target_pr.split('#')[1]), raw)
        _, identity, authority = remote_policy(io, target, raw['base']['sha'])
        selected = ProducerStore(args.lineage_store).read(target)
        chain, decision = lineage_decision(target, identity, authority, io.history(target),
            author=author, labels=labels, private=selected['episodes'])
        ownership = Observation(chain, decision)
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
        source_ownership=ownership,
    )
    if args.source_file is None:
        raise LaneDeliveryError("handoff requires a private source binding; writer quiescence is unverified")
    source = lane_handoff.read_source(args.source_file)
    root = args.state_dir or lane_handoff.default_root()
    if args.reserve_launch:
        payload = {"launch_allowed": lane_handoff.reserve_launch(handoff, root),
                   "handoff": handoff.as_dict()}
    else:
        payload = lane_handoff.prepare(handoff, source, root)
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
            "handoff " + ("accepted" if payload.get("accepted") else "not launched")
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
    writer = None
    finish_lineage = None
    if getattr(args, "lineage_issue", None) is not None:
        writer, finish_lineage = _start_creation_round(args)
    elif args.lineage_before:
        writer, finish_lineage = _start_lineage_round(args)
    elif args.writer:
        from .lane_handoff import LocalWriter
        if not (args.writer_state_dir and args.writer_repo and args.writer_lane and args.cwd):
            raise LaneDeliveryError("writer supervision requires private state, repo, lane, and checkout")
        writer = LocalWriter(args.writer_state_dir, args.writer)
        writer.register(repo=args.writer_repo, lane=args.writer_lane, checkout=args.cwd)
    result = supervise_process(
        command,
        log_path=args.log,
        timeout_seconds=args.timeout_seconds,
        max_log_bytes=args.max_log_bytes,
        cwd=args.cwd,
        stdin_path=args.stdin_file,
        writer=writer,
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
    if finish_lineage is not None:
        finish_lineage(result)
    return result.exit_code




def lineage_target_state(repo, payload):
    """Explicit exact producer snapshot; legacy classify normalization is unchanged."""
    from .builder_lineage import Target
    from .builder_lineage_producer import ProducerRefusal, Snapshot
    if (not isinstance(payload, dict) or payload.get("snapshot_complete") is not True
            or payload.get("kind") != "pr" or payload.get("pr_state") != "OPEN"
            or not isinstance(payload.get("labels"), list)):
        raise ProducerRefusal("Complete exact PR snapshot required.")
    number = payload.get("pr_number")
    if not isinstance(number, str) or not number.isdigit() or payload.get("number") != number:
        raise ProducerRefusal("Exact PR snapshot number required.")
    target = Target(repo, int(number), payload.get("branch"), payload.get("head_sha"))
    return Snapshot(target, payload.get("author"), tuple(payload["labels"]))


def _lineage_checkout(checkout, target):
    from .builder_lineage_producer import ProducerRefusal
    path = Path(checkout)
    if path != path.resolve() or not (path / ".git").exists():
        raise ProducerRefusal("Known delivery checkout unavailable.")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True,
                                       timeout=10, stderr=subprocess.DEVNULL).strip()
    if git("rev-parse", "HEAD") != target.head_sha or git("branch", "--show-current") != target.branch:
        raise ProducerRefusal("Observed delivery checkout head or exact branch differs.")


#: The alphabet :class:`LineageRound` accepts for a stable writer identity and
#: for a supervised round ID. It is deliberately narrower than a repository
#: slug, so identities are derived rather than pasted.
LINEAGE_ID_PATTERN = r"[A-Za-z0-9_-]{1,100}"


def lineage_writer_id(lane: Any, repo: Any, *, run: Any = None) -> str:
    """Canonical lineage identity for ``lane`` writing to ``repo``.

    ``LineageRound`` accepts only ``LINEAGE_ID_PATTERN`` for both the stable
    writer identity and the supervised round ID, while a repository name may
    legally contain ``.``. The runner used to paste ``<lane>-<owner>__<name>``
    into both, so an otherwise eligible dotted or very long slug was refused
    before the provider ever launched.

    Every historical identifier ``LineageRound`` already accepted is preserved
    exactly, including a name containing ``--``: persisted private lineage
    records hold that stable writer, and :func:`lineage_continuation` refuses
    unless the derived writer still equals it, so rewriting one would strand the
    repository after its next round launched. Only what the historical form
    could not represent is encoded: a slug outside the accepted alphabet or the
    100-character cap, an owner/name boundary that does not decode back to this
    exact slug, or an owner that would make the identity read as an encoded one.

    Encoding is ``<lane>--<readable>-<24 hex of sha256(lane + "\\n" + slug)>``.
    For one lane the two namespaces are disjoint: an encoded identity always
    begins ``<lane>--`` and a preserved one never does, because an owner
    starting with ``-`` is encoded instead. Within the encoded namespace
    ``readable`` carries no ``-``, so the digest of the exact lane and slug
    always decodes out, keeping slugs that sanitize or truncate alike apart.
    Identities are only ever compared inside one lane: a private lineage record
    is keyed by the exact target, and both :func:`lineage_continuation` and
    ``ProducerStore.record`` compare the observed transport as well as the
    writer, so the lane is never carried by this string alone.

    ``run`` appends a caller-owned, already-accepted suffix (the runner passes
    its timestamp and PID) so one derivation serves both identities.
    """
    if not isinstance(lane, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", lane):
        raise LaneDeliveryError("Canonical lineage identity requires an exact lane name")
    if not isinstance(repo, str):
        raise LaneDeliveryError("Canonical lineage identity requires an OWNER/REPO slug")
    owner, separator, name = repo.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise LaneDeliveryError("Canonical lineage identity requires an OWNER/REPO slug")
    if run is not None and (not isinstance(run, str)
                            or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", run)):
        raise LaneDeliveryError("Canonical lineage identity requires an accepted run suffix")
    suffix = "" if run is None else f"-{run}"
    key = f"{owner}__{name}"
    preserved = f"{lane}-{key}{suffix}"
    # Preserve the historical identifier whenever it is accepted as-is, decodes
    # back to this exact slug, and cannot be read as an encoded identity. An
    # owner carrying "__" would otherwise let two repositories share one
    # preserved identity; an owner starting with "-" would put a preserved
    # identity inside the encoded "<lane>--" namespace.
    if (re.fullmatch(LINEAGE_ID_PATTERN, preserved)
            and not preserved.startswith(f"{lane}--")
            and key.partition("__")[::2] == (owner, name)):
        return preserved
    digest = hashlib.sha256(f"{lane}\n{repo}".encode("utf-8")).hexdigest()[:24]
    room = 100 - len(f"{lane}--{digest}{suffix}") - 1
    if room < 1:
        raise LaneDeliveryError("Canonical lineage identity does not fit the accepted alphabet")
    # Hyphens are sanitized away too, so the separator before the digest is the
    # first "-" after the marker and the exact digest always decodes out.
    readable = re.sub(r"[^A-Za-z0-9_]", "_", key)[:room]
    derived = f"{lane}--{readable}-{digest}{suffix}"
    if not re.fullmatch(LINEAGE_ID_PATTERN, derived):  # pragma: no cover - defensive
        raise LaneDeliveryError("Canonical lineage identity does not fit the accepted alphabet")
    return derived


class LineageRound:
    """Explicit supervisor observer, inactive until a broker deliberately uses it.

    A round is registered before launch and passed as supervise_process(writer=).
    Existing LocalWriter owns stop/reap observations. Stable writer identity is
    distinct from the unique, non-reusable supervised round ID.
    """
    def __init__(self, root, round_id, writer, before, transport, checkout, *,
                 config, runtime_observation):
        from .builder_lineage import Target
        from .builder_lineage_producer import ProducerRefusal, require_producer
        from .lane_handoff import LocalWriter
        require_producer(transport, config, runtime_observation)
        if not isinstance(before, Target) or any(
                not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", v)
                for v in (round_id, writer)):
            raise ProducerRefusal("Exact named supervised round required.")
        _lineage_checkout(checkout, before)
        self.control = LocalWriter(root, round_id)
        self.control.register(repo=before.repo, lane=transport.lane, checkout=Path(checkout))
        self.round_id, self.writer, self.before, self.transport = round_id, writer, before, transport
        with self.control.store.locked(self.control.key) as locked:
            record = locked.read()
            record["lineage_round"] = dict(round_id=round_id, writer=writer,
                target={"repo": before.repo, "pr_number": before.pr_number,
                        "branch": before.branch, "head_sha": before.head_sha},
                transport=transport.__dict__)
            locked.write(record)

    def stop_requested(self):
        return self.control.stop_requested()

    def started(self, pid, pgid):
        self.control.started(pid, pgid)

    def finish(self, *, quiescent):
        self.control.finish(quiescent=quiescent)

    def observed(self, after):
        from .builder_lineage_producer import ProducerRefusal
        if (after.repo, after.pr_number, after.branch) != (
                self.before.repo, self.before.pr_number, self.before.branch):
            raise ProducerRefusal("Delivery round target differs.")
        with self.control.store.locked(self.control.key) as locked:
            record = locked.read()
        expected = dict(round_id=self.round_id, writer=self.writer,
            target={"repo": self.before.repo, "pr_number": self.before.pr_number,
                    "branch": self.before.branch, "head_sha": self.before.head_sha},
            transport=self.transport.__dict__)
        if (not isinstance(record, dict) or record.get("schema") != "code_mower.localWriter.v1"
                or record.get("lineage_round") != expected
                or record.get("repo") != after.repo or record.get("lane") != self.transport.lane
                or record.get("finished") is not True or record.get("quiescent") is not True
                or any(type(record.get(k)) is not int or record[k] <= 0 for k in ("pid", "pgid"))):
            raise ProducerRefusal("Independent stopped/reaped named writer evidence required.")
        _lineage_checkout(record["checkout"], after)
        return self


def lineage_continuation(round_observer, after, previous):
    """A stopped supervised round by the same actual writer, with chained heads."""
    from .builder_lineage import Episode
    from .builder_lineage_producer import ProducerRefusal, _delivery
    if not isinstance(round_observer, LineageRound):
        raise ProducerRefusal("A supervised round observer is required.")
    observed = round_observer.observed(after)
    if (not isinstance(previous, dict) or previous.get("writer") != observed.writer
            or previous.get("transport") != observed.transport.__dict__
            or previous.get("round_id") == observed.round_id):
        raise ProducerRefusal("Continuation requires the same writer and a fresh supervised round.")
    episode = Episode.from_mapping(previous["episodes"][-1])
    if (episode.repo, episode.pr_number, episode.branch, episode.resulting_head, episode.destination_lane) != (
            after.repo, after.pr_number, after.branch, observed.before.head_sha, observed.transport.lane):
        raise ProducerRefusal("Continuation does not chain the exact prior delivery.")
    return _delivery(Episode(sequence=episode.sequence + 1, repo=after.repo,
        pr_number=after.pr_number, branch=after.branch, source_lane=observed.transport.lane,
        destination_lane=observed.transport.lane, expected_head=observed.before.head_sha,
        resulting_head=after.head_sha, writer_state="same_writer", kind="continuation"),
        observed.writer, observed.round_id, observed.transport)


@dataclass(frozen=True)
class CreationOrigin:
    """The immutable issue-targeted binding one creation round is launched with.

    The issue number never reaches published metadata. It names the unit of work
    whose single created pull request may be attributed, so two rounds launched
    against different issues can never share one supervised creation record.

    ``branch`` is the single branch this round is allowed to create, reserved
    before the writer is launched. ``pull_frontier`` is the highest pull request
    number observed in the repository at the same moment. Together with the
    targeted issue they fix both what this round may claim and the number below
    which nothing can have been created by it, so neither a pre-existing pull
    request nor another writer's concurrent one can be claimed as created.
    """
    repo: str
    issue_number: int
    base_sha: str
    branch: str
    pull_frontier: int

    def __post_init__(self) -> None:
        from .builder_lineage_producer import ProducerRefusal
        for name in ("repo", "base_sha"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ProducerRefusal("Exact creation origin text required.")
            object.__setattr__(self, name, value.strip().lower())
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*", self.repo):
            raise ProducerRefusal("Exact OWNER/REPO creation origin required.")
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise ProducerRefusal("Exact positive issue number required.")
        if type(self.pull_frontier) is not int or self.pull_frontier < 0:
            raise ProducerRefusal("Observed pre-launch pull request frontier required.")
        if not re.fullmatch(r"[0-9a-f]{40}", self.base_sha):
            raise ProducerRefusal("Immutable 40-hex starting base required.")
        # A branch name is compared exactly everywhere it is used, and is read
        # back out of a checkout and a ref listing, so nothing that could make
        # two spellings look alike is accepted. The rule is the repository's one
        # branch contract, bound rather than restated: ``branch_policy`` resolves
        # the branch the runner reserves and the pre-push guard authorizes, and
        # lineage ``Target`` validates the same name again when the episode is
        # minted. A stricter spelling here would refuse — after branch
        # resolution and guard setup, and before the writer ever launched — a
        # name both of those accept.
        from .branch_policy import is_valid_ref
        if not is_valid_ref(self.branch):
            raise ProducerRefusal("Exact reserved creation branch required.")

    @property
    def creation_floor(self) -> int:
        """The exclusive lower bound on a pull request number this round created.

        Both inputs are observed before launch: the targeted issue already held
        its number, and every pull request that existed held one at or below the
        frontier. GitHub's monotone per-repository numbering therefore places
        anything created during the round strictly above both.
        """
        return max(self.pull_frontier, self.issue_number)

    @classmethod
    def for_launch(cls, io, repo, issue_number, base_sha, branch):
        """Bind a round to the branch and frontier read before its launch.

        Both reads happen before the round is registered, and therefore before
        the supervised writer exists at all: the frontier dates every pull
        request the repository already had, and the reservation proves this
        branch had neither a ref nor a pull request of its own.
        """
        validated = cls(repo=repo, issue_number=issue_number, base_sha=base_sha,
                        branch=branch, pull_frontier=0)
        frontier = io.pull_frontier(validated.repo)
        reserve_creation_branch(io, validated.repo, validated.branch)
        return cls(repo=validated.repo, issue_number=issue_number, base_sha=base_sha,
                   branch=validated.branch, pull_frontier=frontier)


def reserve_creation_branch(io, repo, branch):
    """Refuse unless one creation round may exclusively claim ``branch``.

    A pull request number above the pre-launch frontier proves only that the
    pull request was opened after the round started, not that this round opened
    it: another writer's concurrent pull request is numbered above the frontier
    too, and a supervised writer that checked out its branch would otherwise
    satisfy every other check. The branch is what separates them, so it is
    claimed before launch and only when nothing else holds it.

    A branch with no ref cannot already carry a pull request opened from it, and
    an empty pull request list covers the case where the ref was opened from and
    then deleted. Afterwards, the created pull request must sit on this exact
    branch, so it can only be one this round's writer pushed and opened.
    """
    from .builder_lineage_producer import ProducerRefusal
    if io.pulls_for_branch(repo, branch):
        raise ProducerRefusal("Reserved creation branch already has a pull request.")
    if io.branch_ref(repo, branch) is not None:
        raise ProducerRefusal("Reserved creation branch already exists in the repository.")


def _creation_repository(checkout):
    from .builder_lineage_producer import ProducerRefusal
    path = Path(checkout)
    if path != path.resolve() or not (path / ".git").exists():
        raise ProducerRefusal("Known creation checkout unavailable.")
    return path


def _uncommitted_work(status):
    """The porcelain entries a creation round refuses to absorb into its base.

    Runner-owned private state is dropped rather than refused, and it has to be
    dropped here rather than left to the repository: preparing this round's own
    runtime writes ``.code-mower/runtime/bin/python*`` into the checkout before
    the writer exists, no part of the runner installs a git exclusion for it,
    and a repository that does not ignore ``.code-mower/`` would otherwise fail
    every creation round on the runner's own files.

    The roots dropped are exactly the ones the evidence contract already calls
    private state, bound rather than copied, so a name added there does not have
    to be remembered here. They are matched at the top level only: a nested
    directory that happens to share one of those names is ordinary content. Only
    *untracked* entries under them are dropped, too — a repository that
    genuinely tracks a file under one of those roots has committable content
    there, and a staged or modified one still refuses.

    ``status`` is unstripped NUL-separated ``git status --porcelain -z`` output,
    which leaves paths unquoted; a rename or copy entry carries its source in the
    following field. It is read unstripped because an entry's first column is a
    significant space: stripping would turn a modified worktree file into an
    unreadable code.
    """
    from .context_graph import _names_private_state
    fields, entries, index = status.split("\0"), [], 0
    while index < len(fields):
        entry, index = fields[index], index + 1
        if not entry.strip():
            continue
        code, path = entry[:2], entry[3:]
        if code[:1] in ("R", "C"):
            index += 1
        if code == "??" and _names_private_state(path.split("/")[:1]):
            continue
        entries.append(entry)
    return entries


def _creation_checkout(checkout, origin):
    """A creation round may only start from the exact immutable base it declares.

    The declared base is only the round's whole starting point if the checkout
    holds nothing else. Work another lane left staged, modified or untracked is
    invisible to a HEAD comparison, and the supervised writer could commit it
    and open a pull request whose creation episode names only this lane —
    dropping the contributor that actually wrote it from every later
    reviewer-exclusion check. A dirty checkout is therefore refused before the
    round is registered rather than attributed afterwards.

    Ignored paths, and the runner's own untracked private state, are excluded by
    :func:`_uncommitted_work`: neither is committable content this round could
    absorb, and the runner writes the latter into the checkout itself.
    """
    from .builder_lineage_producer import ProducerRefusal
    path = _creation_repository(checkout)
    def git(*args, strip=True):
        text = subprocess.check_output(["git", "-C", str(path), *args], text=True,
                                       timeout=10, stderr=subprocess.DEVNULL)
        return text.strip() if strip else text
    if git("rev-parse", "HEAD") != origin.base_sha:
        raise ProducerRefusal("Creation checkout head differs from the immutable base.")
    if _uncommitted_work(git("status", "--porcelain", "-z", "--untracked-files=all", strip=False)):
        raise ProducerRefusal("Creation checkout carries work beyond the immutable base.")
    return path


def observed_creation_branch(checkout, origin):
    """Read the exact branch and head the stopped writer left in its own checkout.

    The branch is still observed rather than declared: the writer has to have
    ended on the branch reserved for this round before launch, and any other
    branch it checked out — including one another writer had already published —
    is refused here instead of being discovered as this round's creation. The
    immutable base must be a real ancestor of the head it left: a branch that
    never grew out of the launch base is not this round's creation, and an
    unmoved head created nothing at all.
    """
    from .builder_lineage_producer import ProducerRefusal
    path = _creation_repository(checkout)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True,
                                       timeout=10, stderr=subprocess.DEVNULL).strip()
    branch, head = git("branch", "--show-current"), git("rev-parse", "HEAD")
    if not branch or head == origin.base_sha:
        raise ProducerRefusal("Stopped writer left no created branch above the base.")
    if branch != origin.branch:
        raise ProducerRefusal("Stopped writer left a branch this round never reserved.")
    try:
        subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor",
                        origin.base_sha, head], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.SubprocessError:
        raise ProducerRefusal("Created head does not descend from the immutable base.") from None
    return branch, head


def discover_created_pull(io, origin, branch, head_sha):
    """Bind the observed created branch to exactly one readable pull request.

    Ambiguity fails closed in both directions: no pull request for the observed
    branch, or more than one ever opened from it, leaves this round unable to
    name what it created.

    A branch that already had a pull request before launch is not this round's
    creation either, even when its head descends from the launch base: a writer
    that checked out someone else's work would otherwise mint a creation episode
    naming only this lane and drop every earlier contributor from the chain. The
    pre-launch frontier in ``origin`` is what makes that case decidable, so a
    pull request numbered at or below it is refused rather than attributed.

    The frontier alone cannot separate this round's creation from another
    writer's concurrent one, so the pull request must also sit on the branch
    this round reserved before launch, and that branch must now point at the
    exact head the stopped writer left in its own checkout. A branch that had no
    ref at reservation and carries this writer's head afterwards was pushed
    during this round, and the single pull request on it was opened from it.
    """
    from .builder_lineage import Target
    from .builder_lineage_producer import ProducerRefusal
    if branch != origin.branch:
        raise ProducerRefusal("Observed branch differs from the branch reserved before launch.")
    raw = io.pulls_for_branch(origin.repo, branch)
    if len(raw) != 1:
        raise ProducerRefusal("Ambiguous or absent created pull request for the observed branch.")
    entry = raw[0]
    if (not isinstance(entry, dict) or entry.get("state") != "open"
            or not isinstance(entry.get("base"), dict)
            or not isinstance(entry["base"].get("repo"), dict)
            or not isinstance(entry.get("head"), dict)):
        raise ProducerRefusal("Complete open created pull request metadata required.")
    created = Target(entry["base"]["repo"].get("full_name"), entry.get("number"),
                     entry["head"].get("ref"), entry["head"].get("sha"))
    if (created.repo, created.branch, created.head_sha) != (origin.repo, branch, head_sha):
        raise ProducerRefusal("Created pull request differs from the observed branch head.")
    if created.pr_number <= origin.creation_floor:
        raise ProducerRefusal("Pull request existed before the supervised creation round.")
    if io.branch_ref(origin.repo, branch) != head_sha:
        raise ProducerRefusal("Reserved creation branch does not carry the observed created head.")
    return created


class LineageCreationRound:
    """An issue-targeted supervised round; there is no pull request to bind yet.

    Registration happens before launch against the immutable base the checkout
    actually sits on, which must carry nothing else, and against the one branch
    reserved for this round while nothing else held it. Stop/reap observation,
    quiescence, the stable named writer and the unique non-reusable round ID
    remain the existing LocalWriter evidence that LineageRound already depends
    on.
    """
    def __init__(self, root, round_id, writer, origin, transport, checkout, *,
                 config, runtime_observation):
        from .builder_lineage_producer import ProducerRefusal, require_producer
        from .lane_handoff import LocalWriter
        require_producer(transport, config, runtime_observation)
        if not isinstance(origin, CreationOrigin) or any(
                not isinstance(v, str) or not re.fullmatch(LINEAGE_ID_PATTERN, v)
                for v in (round_id, writer)):
            raise ProducerRefusal("Exact named supervised creation round required.")
        self.checkout = _creation_checkout(checkout, origin)
        self.control = LocalWriter(root, round_id)
        self.control.register(repo=origin.repo, lane=transport.lane, checkout=self.checkout)
        self.round_id, self.writer, self.origin, self.transport = round_id, writer, origin, transport
        with self.control.store.locked(self.control.key) as locked:
            record = locked.read()
            record["lineage_creation"] = self._binding()
            locked.write(record)

    def _binding(self):
        return dict(round_id=self.round_id, writer=self.writer,
                    origin=dict(repo=self.origin.repo, issue_number=self.origin.issue_number,
                                base_sha=self.origin.base_sha, branch=self.origin.branch,
                                pull_frontier=self.origin.pull_frontier),
                    transport=self.transport.__dict__)

    def stop_requested(self):
        return self.control.stop_requested()

    def started(self, pid, pgid):
        self.control.started(pid, pgid)

    def finish(self, *, quiescent):
        self.control.finish(quiescent=quiescent)

    def _created(self, created):
        """The exact discovered creation this round may be bound to, if any."""
        from .builder_lineage import Target
        from .builder_lineage_producer import ProducerRefusal
        if not isinstance(created, Target) or (created.repo, created.branch) != (
                self.origin.repo, self.origin.branch):
            raise ProducerRefusal("Created pull request is outside the supervised creation origin.")
        if created.pr_number <= self.origin.creation_floor:
            raise ProducerRefusal("Pull request existed before the supervised creation round.")
        return dict(repo=created.repo, pr_number=created.pr_number,
                    branch=created.branch, head_sha=created.head_sha)

    def bind_created(self, created):
        """Persist the one independently discovered pull request this round created.

        Registration binds the round to an issue and a base; neither names a
        pull request, because none exists yet. Discovery is what finds it, so
        the discovered target is written back into the stopped writer's own
        record before any receipt can be minted. One finished round therefore
        attributes exactly one creation: an identical replay is accepted
        unchanged, and a second, different pull request is refused instead of
        acquiring the same verified single-lane attribution.
        """
        from .builder_lineage_producer import ProducerRefusal
        bound = self._created(created)
        self.observed()
        with self.control.store.locked(self.control.key) as locked:
            record = locked.read()
            existing = record.get("lineage_created")
            if existing is None:
                record["lineage_created"] = bound
                locked.write(record)
            elif existing != bound:
                raise ProducerRefusal("This supervised round already created a different pull request.")
        self.observed(created)
        return created

    def observed(self, created=None):
        """The stopped writer's own evidence, optionally for one bound creation.

        Called with ``created``, the round additionally requires the persisted
        binding to name that exact pull request and the writer's checkout to
        still sit on that exact branch head, the way ``LineageRound.observed``
        requires of a delivery. A head that is absent from the checkout, or a
        target this round never discovered, has no supervised creation evidence.
        """
        from .builder_lineage_producer import ProducerRefusal
        with self.control.store.locked(self.control.key) as locked:
            record = locked.read()
        if (not isinstance(record, dict) or record.get("schema") != "code_mower.localWriter.v1"
                or record.get("lineage_creation") != self._binding()
                or record.get("repo") != self.origin.repo
                or record.get("lane") != self.transport.lane
                or record.get("checkout") != str(self.checkout)
                or record.get("finished") is not True or record.get("quiescent") is not True
                or any(type(record.get(k)) is not int or record[k] <= 0 for k in ("pid", "pgid"))):
            raise ProducerRefusal("Independent stopped/reaped named writer evidence required.")
        if created is not None:
            if record.get("lineage_created") != self._created(created):
                raise ProducerRefusal("Creation differs from the pull request this round discovered.")
            _lineage_checkout(record["checkout"], created)
        return self


def lineage_creation(round_observer, created, base_sha):
    """One stopped issue-targeted round that opened exactly this pull request.

    The round must already be bound to ``created`` by
    :meth:`LineageCreationRound.bind_created`, which is what makes the target a
    discovered observation rather than a caller's claim. Repository, immutable
    base and the pre-launch frontier are then all re-checked here, so no caller
    can hand this contract a pull request the round did not create.
    """
    from .builder_lineage import Episode
    from .builder_lineage_producer import ProducerRefusal, _delivery
    if not isinstance(round_observer, LineageCreationRound):
        raise ProducerRefusal("A supervised creation round observer is required.")
    observed = round_observer.observed(created)
    if (created.repo, base_sha) != (observed.origin.repo, observed.origin.base_sha):
        raise ProducerRefusal("Created pull request is outside the supervised creation origin.")
    return _delivery(Episode(sequence=1, repo=created.repo, pr_number=created.pr_number,
        branch=created.branch, source_lane=observed.transport.lane,
        destination_lane=observed.transport.lane, expected_head=base_sha,
        resulting_head=created.head_sha, writer_state="terminated", kind="creation"),
        observed.writer, observed.round_id, observed.transport)


def _start_creation_round(args, *, io=None, runtime_observation=None):
    """One launcher lifetime owns an issue-targeted round and its created PR.

    Nothing is published before the writer is independently observed to have
    stopped, the created branch is read back from its own checkout, and exactly
    one readable pull request is bound to that exact branch head.

    The branch is the launcher's own pre-launch reservation, not something the
    writer chooses afterwards: the caller names one branch inside this lane's
    configured prefixes, it is claimed only while nothing else holds it, and the
    supervised writer is required to have ended on exactly it.
    """
    from .builder_lineage_producer import GitHub, ProducerStore, Transport, exact_snapshot, publish
    from .provider_runners.lineage import require_capabilities, trusted_policy
    from . import lane_runtime
    require_capabilities()
    if args.lineage_output is None:
        raise LaneDeliveryError("Attribution output required before launch")
    if not (args.lineage_store and args.writer_repo and args.writer_lane and args.cwd
            and args.writer and args.lineage_writer and args.writer_state_dir
            and args.lineage_branch):
        raise LaneDeliveryError("Creation lineage requires the supervised writer bindings and a private store")
    if args.lineage_before or args.lineage_handoff:
        raise LaneDeliveryError("Creation lineage has no pre-existing pull request target")
    config, identity, authorities = trusted_policy(args.cwd, args.lineage_base)
    transport = Transport(args.writer_lane, "devin_cli" if args.writer_lane == "devin" else args.writer_lane,
                          args.writer_lane + "_cli", "local_cli")
    prefixes = [prefix for prefix, lane in identity.branch_prefixes if lane == transport.lane]
    if not prefixes:
        raise LaneDeliveryError("Creation requires a configured branch prefix for this lane")
    if not any(args.lineage_branch.lower().startswith(prefix) for prefix in prefixes):
        raise LaneDeliveryError("Reserved creation branch is outside this lane's configured prefixes")
    io = io if io is not None else GitHub()
    # Read before registration, so the frontier this round is bound to is always
    # older than anything the supervised writer can open, and the branch it may
    # create is claimed while it is still provably unused.
    origin = CreationOrigin.for_launch(io, args.writer_repo, args.lineage_issue, args.lineage_base,
                                       args.lineage_branch)
    if runtime_observation is None:
        def runtime_observation():
            lane_runtime.prepare(args.cwd, sys.executable)
            return "ready"
    store = ProducerStore(args.lineage_store)
    observer = LineageCreationRound(args.writer_state_dir, args.writer, args.lineage_writer,
        origin, transport, args.cwd, config=config, runtime_observation=runtime_observation)

    def finish(result):
        from .builder_runs import record_lineage_builder
        observer.observed()
        if result.reason != "completed" or result.exit_code != 0:
            raise LaneDeliveryError("No completed supervised delivery")
        branch, head_sha = observed_creation_branch(observer.checkout, origin)
        if not any(branch.lower().startswith(prefix) for prefix in prefixes):
            raise LaneDeliveryError("Created branch is outside this lane's configured prefixes")
        # Bind the discovered pull request into the stopped writer's own record
        # before anything is minted, so this round can attribute only this one.
        created = observer.bind_created(discover_created_pull(io, origin, branch, head_sha))
        snapshot = exact_snapshot(io, created)
        delivery = lineage_creation(observer, created, origin.base_sha)
        store.record(delivery, created, identity, authorities, io.history(created),
                     author=snapshot.author, labels=snapshot.labels, config=config,
                     runtime_observation=runtime_observation, create=True)
        publication = publish(io, created, identity, authorities, store.read(created)["episodes"])
        record_lineage_builder(publication.observation, transport, args.lineage_output,
            created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    return observer, finish


def _start_lineage_round(args, *, io=None, runtime_observation=None):
    """One launcher lifetime owns registration, stopped delivery and publication."""
    from .audit_labeler_lib import lineage_decision
    from .builder_lineage_producer import GitHub, Observation, ProducerStore, Transport, exact_snapshot, publish
    from .provider_runners.lineage import require_capabilities, trusted_policy
    from . import lane_handoff, lane_runtime
    require_capabilities()
    if args.lineage_output is None:
        raise LaneDeliveryError("Attribution output required before launch")
    # A branch reservation only means anything for a round that creates the pull
    # request; a delivery to an existing one already has its branch.
    if getattr(args, "lineage_branch", None):
        raise LaneDeliveryError("An existing pull request target has no branch to reserve")
    config, identity, authorities = trusted_policy(args.cwd, args.lineage_base)
    before = lineage_target_state(args.writer_repo, json.loads(args.lineage_before.read_text()))
    io = io if io is not None else GitHub()
    if exact_snapshot(io, before.target) != before:
        raise LaneDeliveryError("Lineage pre-launch snapshot differs")
    transport = Transport(args.writer_lane, "devin_cli" if args.writer_lane == "devin" else args.writer_lane,
                          args.writer_lane + "_cli", "local_cli")
    if runtime_observation is None:
        def runtime_observation():
            lane_runtime.prepare(args.cwd, sys.executable)
            return "ready"
    handoff = Handoff(**json.loads(args.lineage_handoff.read_text())) if args.lineage_handoff else None
    store = ProducerStore(args.lineage_store) if args.lineage_store else None
    # Public-only is explicitly selected for ordinary first writes/first takeover.
    # A selected continuation or re-handoff must read the existing private record.
    previous = store.read(before.target) if store is not None and not args.lineage_create else None
    history = io.history(before.target)
    chain, decision = lineage_decision(before.target, identity, authorities, history,
        author=before.author, labels=before.labels, private=previous["episodes"] if previous is not None else ())
    ownership = Observation(chain, decision) if previous is not None else None
    if decision.status != "ready":
        raise LaneDeliveryError("Lineage pre-launch " + decision.reason)
    if handoff:
        prefixes = [prefix for prefix, lane in identity.branch_prefixes if lane == handoff.source_lane]
        validate_handoff(**handoff.as_dict(), running_lane=transport.lane, repo=before.target.repo,
            observed_head=before.target.head_sha, source_branch_prefixes=prefixes, source_ownership=ownership)
        if not args.lineage_handoff_root or store is None:
            raise LaneDeliveryError("Selected handoff and producer stores required")
    elif decision.current_writer != transport.lane:
        raise LaneDeliveryError("Observed current writer differs from actual transport")
    observer = LineageRound(args.writer_state_dir, args.writer, args.lineage_writer,
        before.target, transport, args.cwd, config=config, runtime_observation=runtime_observation)

    def finish(result):
        from .builder_lineage import Target
        from .builder_runs import record_lineage_builder
        raw = io._json(f"repos/{before.target.repo}/pulls/{before.target.pr_number}")
        after_target = Target(raw['base']['repo']['full_name'], raw['number'], raw['head']['ref'], raw['head']['sha'])
        after = exact_snapshot(io, after_target)
        observer.observed(after.target)
        if result.reason != "completed" or result.exit_code != 0:
            raise LaneDeliveryError("No completed supervised delivery")
        if after.target == before.target:
            return
        if handoff:
            delivery = lane_handoff.lineage_handoff(handoff, args.lineage_handoff_root, observer,
                after.target, source_branch_prefixes=prefixes, sequence=len(chain.episodes) + 1,
                source_ownership=ownership)
        elif previous is not None:
            delivery = lineage_continuation(observer, after.target, previous)
        else:
            delivery = None
        if delivery is not None:
            store.record(delivery, after.target, identity, authorities, io.history(after.target),
                author=after.author, labels=after.labels, config=config,
                runtime_observation=runtime_observation, create=args.lineage_create)
            publication = publish(io, after.target, identity, authorities, store.read(after.target)['episodes'])
            observation = publication.observation
        else:
            final_chain, final_decision = lineage_decision(after.target, identity, authorities, io.history(after.target),
                author=after.author, labels=after.labels)
            observation = Observation(final_chain, final_decision)
        if args.lineage_output is None:
            raise LaneDeliveryError("Attribution output required")
        record_lineage_builder(observation, transport, args.lineage_output,
            created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    return observer, finish


def _lineage_owner_main(args):
    from .builder_lineage_producer import GitHub, ProducerStore
    from .audit_labeler_lib import lineage_decision, lineage_snapshot, lineage_projection
    from .provider_runners.lineage import remote_policy
    io = GitHub()
    raw = io._json(f"repos/{args.repo}/pulls/{args.pr}")
    target, author, labels = lineage_snapshot(args.repo, args.pr, raw)
    _, identity, authority = remote_policy(io, target, raw['base']['sha'])
    private = ProducerStore(args.lineage_store).read(target)['episodes'] if args.lineage_store else ()
    _, decision = lineage_decision(target, identity, authority, io.history(target),
        author=author, labels=labels, private=private)
    print(json.dumps(lineage_projection(decision)))
    return 0 if decision.status == 'ready' else 2


def _lineage_record_main(args):
    from .builder_lineage_producer import GitHub, Transport, staged_record
    from .audit_labeler_lib import lineage_snapshot
    from .provider_runners.lineage import remote_policy, require_capabilities
    require_capabilities()
    io = GitHub()
    raw = io._json(f"repos/{args.repo}/pulls/{args.pr}")
    target, _, _ = lineage_snapshot(args.repo, args.pr, raw)
    config, identity, authorities = remote_policy(io, target, args.base)
    transport = Transport(args.lane, "devin_cli" if args.lane == "devin" else args.lane,
                          args.lane + "_cli", "local_cli")
    environ = {
        "LINEAGE_TARGET_JSON": json.dumps(dict(repo=target.repo, pr_number=target.pr_number,
            branch=target.branch, head_sha=target.head_sha)),
        "LINEAGE_POLICY_JSON": json.dumps(dict(base_sha=args.base, identity=identity.to_mapping(), roles=config)),
        "LINEAGE_AUTHORITY_JSON": json.dumps(sorted(authorities.accounts)),
        "LINEAGE_TRANSPORT_JSON": json.dumps(transport.__dict__),
        "LINEAGE_OUTPUT": str(args.output),
    }
    staged_record(environ, io=io)
    return 0


if __name__ == "__main__":  # pragma: no cover
    from code_mower.lane_delivery import main as entrypoint
    raise SystemExit(entrypoint())
