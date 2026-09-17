"""Supervisor-owned v2 input boundary and durable, bounded resume operations.

Only opaque metadata crosses the wire. Input prose is freshly resolved by the
embedding's authenticated private store and is never persisted by this module.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

from . import builder_lineage, supervisor_contract_v2 as contract
from .devin_work_orders import DevinWorkOrders
from .remote_session import RemoteSessions, _key
from .role_eligibility import decide_role, require_role
from .supervisor_contract import SupervisorError, digest


@dataclass(frozen=True, repr=False)
class AuthorizedInput:
    """Resolved private input, not a public request or bearer grant.

    The store binds request to the authenticated actor/current grant, checkpoint
    and original scope. For fixes it also resolves a bounded finding reference
    from the independent review of this exact target. Prose conveys information,
    never approval, new scope, merge authority or a different writer.
    """
    request: dict
    prose: str
    checkpoint: str
    scope_digest: str
    target: builder_lineage.Target | None = None
    finding_ref: str | None = None


def builder_snapshot(engine, order, lifecycle, round_number):
    remote = engine.remote.store.read_only(_key(engine._key(order)))
    if (not remote or not remote.get("binding") or remote["provider"] != engine.remote.provider.name
            or remote["account"] != engine.remote.provider.account
            or remote["repo"].lower() != order.repository.lower()):
        raise SupervisorError("binding_mismatch")
    binding = digest([engine._binding(order), *(remote[k] for k in
                     ("schema", "provider", "account", "repo", "binding", "fingerprint"))])
    checkpoint = digest([binding, round_number, lifecycle["state"], remote["operations"]])
    return binding, checkpoint


class _CheckedConnection:
    """Fence each call through the existing connections, without a provider engine."""
    def __init__(self, connection, guard, before_message=None):
        self.connection, self.guard, self.before_message = connection, guard, before_message

    def __getattr__(self, name):
        value = getattr(self.connection, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            self.guard()
            if name == "message" and self.before_message is not None:
                self.before_message(*args)
                self.guard()
            try:
                return value(*args, **kwargs)
            finally:
                self.guard()
        return call


def guarded_engine(engine, guard, before_message=None):
    remote = RemoteSessions(engine.remote.store.root,
        _CheckedConnection(engine.remote.provider, guard, before_message))
    return DevinWorkOrders(engine.store.root, remote,
        _CheckedConnection(engine.github, guard), config=engine.role_config, runtime=engine.runtime)


def _usage(provider, binding, cap, guard):
    usage = getattr(provider, "usage", None)
    if usage is None:
        raise SupervisorError("usage_unavailable")
    guard()
    spent = usage(binding)
    guard()
    if type(spent) not in (int, float) or not math.isfinite(spent) or spent < 0:
        raise SupervisorError("usage_unavailable")
    if spent >= cap:
        raise SupervisorError("budget_exhausted")


def message_fence(engine, task, action, target, binding, expected_binding, guard, before_resume):
    """Final fence inside RemoteSessions' lock; never re-enter its lifecycle."""
    fresh = guard()
    provider = engine.remote.provider
    saved = engine.remote.store.read_only(_key(engine._key(task.order)))
    identity, _ = builder_snapshot(engine, fresh.order, {"state": "pending"}, 0)
    if identity != expected_binding or saved["binding"] != binding:
        raise SupervisorError("binding_mismatch")
    _usage(provider, binding, fresh.order.acu_limit, guard)
    before_resume()
    snapshot = provider.get(binding)
    guard()
    if snapshot.state == "waiting_for_approval" or (
        snapshot.state == "owner_action" and snapshot.reason == "approval_required"
    ):
        raise SupervisorError("approval_required")
    if action == "clarify":
        if (not (snapshot.state == "waiting_for_user" or (
                snapshot.state == "owner_action" and snapshot.reason == "waiting_for_owner"))
                or snapshot.structured_output is not None):
            raise SupervisorError("wrong_checkpoint")
    else:
        if snapshot.writer_state != "terminated":
            raise SupervisorError("writer_active")
        current = engine.github.read(fresh.order.repository, target.pr_number)
        guard()
        if (current.repository != fresh.order.repository or current.number != target.pr_number
                or current.head_sha != target.head_sha or current.head_branch != target.branch
                or current.state != "open"):
            raise SupervisorError("head_changed")
    guard()


def _input(task, request):
    value = task.checkpoint_input
    if not isinstance(value, AuthorizedInput):
        raise SupervisorError("input_unavailable")
    if (contract.validate("request", value.request) != request
            or type(value.prose) is not str or not value.prose.strip()
            or len(value.prose.encode()) > 32000
            or value.scope_digest != request["claim"]["scope_digest"]
            or not isinstance(value.checkpoint, str)
            or re.fullmatch(r"[a-f0-9]{64}", value.checkpoint) is None):
        raise SupervisorError("binding_mismatch")
    if request["action"] == "clarify":
        if value.target is not None or value.finding_ref is not None:
            raise SupervisorError("wrong_checkpoint")
    elif (not isinstance(value.target, builder_lineage.Target)
          or not isinstance(value.finding_ref, str)
          or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value.finding_ref) is None):
        raise SupervisorError("fix_not_authorized")
    return value


def operate(supervisor, action, claim, *, request_key):
    from .supervisor import _status, failure

    try:
        request = contract.validate("request", dict(schema="code_mower.supervisor_request.v2",
            action=action, claim=claim, request_key=request_key))
        # Detach from the caller before external authorization runs.
        claim = request["claim"]
        with supervisor.store.locked(supervisor._key(claim)) as locked:
            record = locked.read()
            task = supervisor._check(record, claim, action, request_key=request_key)
            value = _input(task, request)
            fingerprint = digest(asdict(value))

            def guard():
                fresh = supervisor._check(record, claim, action, request_key=request_key)
                if digest(asdict(_input(fresh, request))) != fingerprint:
                    raise SupervisorError("request_conflict")
                if supervisor.builder.binding(fresh) != record["builder_binding"]:
                    raise SupervisorError("binding_mismatch")
                if action == "fix":
                    reviewer = record["plan"]["reviewer"]
                    if reviewer not in {m["id"] for m in fresh.session["participants"] if m.get("reviewer")}:
                        raise SupervisorError("review_unavailable")
                    require_role(decide_role(reviewer, "reviewer", config=fresh.config,
                                             runtime="ready"), execution=True)
                return fresh

            guard()
            previous = record["requests"].get(request_key)
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise SupervisorError("request_conflict")
                if previous["outcome"] is not None:
                    return contract.validate("result", previous["outcome"])
                return supervisor._uncertain(locked, record, "mutation_uncertain")
            if record["pending"]:
                return supervisor._uncertain(locked, record, "recovery_required")
            if not record["started"] or record["cancel"]:
                raise SupervisorError("wrong_checkpoint")
            counter = "clarification_answers" if action == "clarify" else "fix_requests"
            if record[counter] >= record["admission"]["limits"][counter]:
                raise SupervisorError("budget_exhausted")
            if value.checkpoint != record["checkpoint"]:
                raise SupervisorError("wrong_checkpoint")
            if action == "clarify" and (record["target"] is not None or record["review_target"] is not None):
                raise SupervisorError("wrong_checkpoint")
            if action == "fix":
                if (record["review_target"] != asdict(value.target)
                        or record["target"] != asdict(value.target)):
                    raise SupervisorError("head_changed")
                if record["review_requests"] >= record["admission"]["limits"]["review_requests"]:
                    raise SupervisorError("budget_exhausted")

            def observed_checkpoint():
                fresh = guard()
                observed = supervisor.builder.observe(fresh, collect=action == "fix", guard=guard,
                    expected_round=record["round"], expected_binding=record["builder_binding"])
                guard()
                if observed.lifecycle["state"] == "waiting_for_approval":
                    raise SupervisorError("approval_required")
                if (observed.binding != record["builder_binding"]
                        or observed.checkpoint != value.checkpoint):
                    raise SupervisorError("wrong_checkpoint")
                if action == "clarify":
                    if observed.lifecycle["state"] != "waiting_for_user":
                        raise SupervisorError("wrong_checkpoint")
                else:
                    if (observed.target != value.target or observed.merge != "open"
                            or observed.writer != "terminated"):
                        raise SupervisorError("head_changed")
                    supervisor._review_allowed(fresh, value.target, record["plan"]["reviewer"], guard=guard)
                    guard()
                    review = supervisor._review_observation(fresh, value.target, record["plan"]["reviewer"])
                    guard()
                    if review["review"] != "failed" or review["writer"] != "terminated":
                        raise SupervisorError("fix_not_authorized")
                return observed

            observed_checkpoint()
            supervisor.builder.check_usage(task, guard=guard)
            # A usage read can take time. Repeat the observed checkpoint before
            # reserving a mutation; the final provider-call fence repeats it too.
            observed_checkpoint()
            # All preflight failures above are read-only with respect to sends.
            # This fsync'd intent invalidates completion and consumes the allowance
            # before even entering the work-order message lifecycle.
            record[counter] += 1
            record["requests"][request_key] = dict(fingerprint=fingerprint, outcome=None)
            record.update(pending=action, target=None)
            record["status"] = _status(version="v2", state="uncertain", reason="mutation_uncertain",
                next_action="owner_action", writer="unknown")
            locked.write(record)

            def before_resume():
                fresh = guard()
                if action == "fix":
                    supervisor._review_allowed(fresh, value.target, record["plan"]["reviewer"], guard=guard)
                    review = supervisor._review_observation(fresh, value.target, record["plan"]["reviewer"])
                    guard()
                    if review["review"] != "failed" or review["writer"] != "terminated":
                        raise SupervisorError("fix_not_authorized")
                return fresh

            prose = ("Information for the existing checkpoint only. This is not approval or new authority. "
                     "Preserve safe mode, the original scope, sole writer and cumulative cap; stop for approval.\n"
                     if action == "clarify" else
                     "Fix only the authorized findings within the original work scope. Preserve safe mode, "
                     "the sole writer and cumulative cap. Do not review, approve or merge this PR.\n") + value.prose
            try:
                fresh = guard()
                observed = supervisor.builder.resume(fresh, action, request=request_key, prose=prose,
                    target=value.target, guard=guard,
                    expected_round=record["round"] + 1, expected_binding=record["builder_binding"],
                    before_resume=before_resume)
                guard()
                if observed.lifecycle["state"] == "uncertain":
                    return supervisor._uncertain(locked, record, "mutation_uncertain")
                completed = dict(record, round=record["round"] + 1, pending=None, review_target=None)
                completed["status"] = _status(version="v2", state="running", next_action="status")
                supervisor._observed(completed, observed, collect=False)
                outcome = supervisor._result(completed)
                completed["requests"] = record["requests"] | {
                    request_key: dict(fingerprint=fingerprint, outcome=outcome)}
                locked.write(completed)
                return outcome
            except Exception:
                # A local error cannot prove the external mutation was not sent.
                # Keep the intent pending and never automatically reimburse it.
                return supervisor._uncertain(locked, record, "mutation_uncertain")
    except SupervisorError as exc:
        return failure(exc.args[0], version="v2")
    except Exception:
        return failure("recovery_required", state="uncertain", version="v2")
