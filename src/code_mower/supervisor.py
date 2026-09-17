"""Qualified Code Mower supervisor adapter, independent of Slack/hosted queues.

One private durable reservation per tenant/repository/work prevents a new run,
expired claim, or restarted runtime from becoming a replacement writer. Recovery
uses the existing owner-authorized lane_handoff facilities, never a retry here.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import builder_lineage, session_lease, slack_contract
from .context_store import ContextStore
from .devin_work_orders import DevinWorkOrders, PacketContext, WorkOrder
from .remote_session import public_projection
from .role_eligibility import decide_role, require_role
from .session import build_session
from .supervisor_contract import SCHEMA, SupervisorError, digest, validate as validate_v1
from . import supervisor_contract_v2 as v2
from .supervisor_checkpoint import AuthorizedInput, _usage, builder_snapshot, guarded_engine, message_fence


def validate(kind, value):
    # Version selection is explicit in every versioned record. v1 never accepts v2.
    validator = v2.validate if isinstance(value, dict) and str(value.get("schema", "")).endswith(".v2") else validate_v1
    return validator(kind, value)


def _version(value):
    return "v2" if value.get("schema") == v2.SCHEMA or str(value.get("schema", "")).endswith(".v2") else "v1"


@dataclass(frozen=True, repr=False)
class AuthorizedTask:
    """Fresh private resolution, never constructed from a bridge JSON body alone.

    The resolver authenticates the caller, checks the requested action and current
    tenant/grant membership, and binds the stored verified Slack receipt to the
    exact approved WorkOrder, run and session. It owns revocation and expiry.
    No campaign authentication or credential discovery is performed here.
    """
    admission: dict
    order: WorkOrder
    slack_request: dict
    slack_policy: dict
    registered_runners: frozenset[str]
    session: dict
    checkout: Path
    config: dict
    context: PacketContext | None = None
    checkpoint_input: AuthorizedInput | None = None


class Authorization(Protocol):
    def resolve(self, admission: dict, action: str, *, request_key: str = "") -> AuthorizedTask:
        """Independently reauthorize this caller/action; raise on revocation.

        Bind tenant/repository/work/run/operation and grant to private stored
        state. Admission/handoff require start authority, status requires status,
        renew requires continued start/supervisor authority, cancel requires a
        newly authenticated cancellation, result requires the
        authenticated orchestrator collection route. A claim is not a grant.
        Return no stale cached positive decision. Bound dependency timeouts.
        """
        ...


class Runtime(Protocol):
    product: str
    generation: str

    def decide(self, task: AuthorizedTask, request: dict, *, timeout: int) -> dict:
        """Reach the configured agent and obtain its schema-bound decision.

        No builder calls or mutations here. Generation is fresh on every runtime
        connection/restart, never restored from a caller or a saved claim.
        """
        ...


@dataclass(frozen=True, repr=False)
class Observation:
    lifecycle: dict
    writer: str
    target: builder_lineage.Target | None = None
    merge: str = "unknown"
    binding: str | None = None
    checkpoint: str | None = None
    round_number: int = 0


class Builder(Protocol):
    product: str
    transport: str
    identity: str

    def dispatch(self, task: AuthorizedTask, **checks) -> Observation: ...
    def observe(self, task: AuthorizedTask, *, collect: bool, **checks) -> Observation: ...
    def cancel(self, task: AuthorizedTask, *, request: str, **checks) -> Observation: ...
    def binding(self, task: AuthorizedTask) -> str: ...
    def check_usage(self, task: AuthorizedTask, *, guard) -> None: ...
    def resume(self, task: AuthorizedTask, action: str, *, request: str, prose: str,
               target: builder_lineage.Target | None, **checks) -> Observation: ...


class Reviews(Protocol):
    """Existing independent audit routing, not a new source of review authority.

    Every read is freshly bound to the exact Target. lineage must use verified
    contributor history (#963/#975), not a builder-supplied list. ready checks
    the selected existing lane runtime. request uses the existing broker and a
    durable idempotency key; no fallback, grant, merge or tracker write is added.
    observe returns only {head_sha, reviewer, review, gate, writer}; gate comes from the
    authoritative code-mower/gate, not from the reviewer or builder's prose.
    writer is an independent review-runtime exit observation. cancel must stop
    that exact audit request through its existing lifecycle and return its
    observed writer state; accepting a cancellation is not proof of termination.
    """
    def lineage(self, task: AuthorizedTask, target: builder_lineage.Target) -> builder_lineage.Lineage: ...
    def ready(self, reviewer: str) -> bool: ...
    def request(self, task: AuthorizedTask, target: builder_lineage.Target,
                reviewer: str, *, key: str) -> None: ...
    def observe(self, task: AuthorizedTask, target: builder_lineage.Target, reviewer: str) -> dict: ...
    def cancel(self, task: AuthorizedTask, target: builder_lineage.Target,
               reviewer: str, *, key: str) -> str: ...


class HostedBuilder:
    """Maintained builder connection: exact WorkOrder/round + RemoteSessions.

    The embedding supplies the already authenticated engine; no ambient provider
    selection or credentials. Keep its private roots stable and tenant isolated.
    """
    product = "devin"
    transport = "devin_api_v3"

    def __init__(self, engine: DevinWorkOrders):
        self.engine = engine

    @property
    def identity(self):
        engine = self.engine
        return digest([str(engine.store.root), str(engine.remote.store.root),
                       engine.remote.provider.name, engine.remote.provider.account])

    def _run(self, task, command, *, guard=None, expected_round=0, expected_binding=None,
             before_message=None, **kwargs):
        # Re-evaluate the current repository policy at every new execution.
        engine = DevinWorkOrders(self.engine.store.root, self.engine.remote, self.engine.github,
                                 config=task.config, runtime=self.engine.runtime)
        if expected_binding is not None:
            binding, _ = builder_snapshot(engine, task.order, {"state": "pending"}, expected_round)
            if binding != expected_binding:
                raise SupervisorError("binding_mismatch")
        if guard is not None:
            engine = guarded_engine(engine, guard, before_message)
        value = engine.run(command, task.order, apply=True, **kwargs)
        if "session" not in value:
            raise SupervisorError("builder_unavailable")
        if value["round"] != expected_round:
            # Only the supervisor may advance the exact saved round.
            raise SupervisorError("binding_mismatch")
        target, merge = None, "unknown"
        if value.get("verified_pr"):
            pr = value["verified_pr"]
            target = builder_lineage.Target(pr["repository"], pr["pr_number"],
                                            task.order.branch, pr["head_sha"])
            current = engine.github.read(task.order.repository, target.pr_number)
            if (current.head_sha != target.head_sha or current.head_branch != target.branch
                    or current.repository.lower() != target.repo):
                raise SupervisorError("head_changed")
            merge = current.state
        writer = engine.remote.writer_state(engine._key(task.order), repo=task.order.repository)
        binding, checkpoint = builder_snapshot(engine, task.order, value["session"], value["round"])
        return Observation(value["session"], writer, target, merge, binding, checkpoint, value["round"])

    def dispatch(self, task, **checks):
        return self._run(task, "dispatch", context=task.context, **checks)

    def observe(self, task, *, collect, **checks):
        return self._run(task, "collect" if collect else "status", **checks)

    def cancel(self, task, *, request, **checks):
        return self._run(task, "cancel", request=request, **checks)

    def binding(self, task):
        return builder_snapshot(self.engine, task.order, {"state": "pending"}, 0)[0]

    def check_usage(self, task, *, guard):
        guard()
        # Use the durable private binding without opening a second lifecycle.
        from .remote_session import _key
        saved = self.engine.remote.store.read_only(_key(self.engine._key(task.order)))
        _usage(self.engine.remote.provider, saved["binding"], task.order.acu_limit, guard)

    def resume(self, task, action, *, request, prose, target, guard, expected_binding,
               before_resume, **checks):
        def before_message(binding, _prose):
            message_fence(self.engine, task, action, target, binding, expected_binding, guard, before_resume)
        return self._run(task, action, request=request, prose=prose,
                         reviewed_head=target.head_sha if target else "", context=task.context,
                         guard=guard, expected_binding=expected_binding,
                         before_message=before_message, **checks)


def _status(*, version="v1", **changes):
    return validate("status", dict(
        schema="code_mower.supervisor_status." + version, state="waiting", reason="none",
        next_action="none", implementation="pending", writer="not_started",
        review="not_requested", review_writer="not_started", gate="unknown", merge="unknown",
        lifecycle=public_projection({"state": "pending"}),
        tracker_write=False, merge_authority=False,
    ) | changes)


def failure(reason: str, *, state="rejected", version="v1") -> dict:
    action = {"supervisor_unavailable": "configure_supervisor",
              "supervisor_unqualified": "configure_supervisor",
              "not_registered": "configure_supervisor",
              "claim_expired": "new_authorization", "claim_revoked": "new_authorization"}.get(reason, "owner_action")
    return validate("result", dict(schema="code_mower.supervisor_result." + version, claim=None,
                                   status=_status(version=version, state=state, reason=reason, next_action=action,
                                                  writer="unknown", review_writer="unknown"), target=None,
                                   **({"checkpoint": None} if version == "v2" else {})))


class Supervisor:
    def __init__(self, *, root: Path, runner: str, authorization: Authorization,
                 runtime: Runtime, builder: Builder, reviews: Reviews, clock=time.time):
        self.store, self.runner = ContextStore(root), runner
        self.authorization, self.runtime = authorization, runtime
        self.builder, self.reviews, self.clock = builder, reviews, clock

    @staticmethod
    def _key(admission):
        binding = admission["binding"]
        # Run and operation deliberately excluded: a restart/new delivery cannot
        # reserve a second writer for the same work under a different run ID.
        return "s" + digest([binding[k] for k in ("tenant", "repository", "work")])[:62]

    def _authorize(self, admission, action, *, request_key=""):
        if admission["runner"] != self.runner:
            raise SupervisorError("binding_mismatch")
        now = self.clock()
        if not isinstance(now, (int, float)) or not 0 < now < admission["expires_at"]:
            raise SupervisorError("claim_expired")
        try:
            task = self.authorization.resolve(validate("admission", admission), action,
                **({"request_key": request_key} if request_key else {}))
        except Exception:
            raise SupervisorError("claim_revoked") from None
        # Resolution is an external boundary too; do not carry its starting
        # timestamp through a slow authorization call.
        now = self.clock()
        if not isinstance(now, (int, float)) or not 0 < now < admission["expires_at"]:
            raise SupervisorError("claim_expired")
        if not isinstance(task, AuthorizedTask) or task.admission != admission:
            raise SupervisorError("binding_mismatch")
        if self.runner not in task.registered_runners:
            raise SupervisorError("not_registered")
        # Initial maintained supervisor runtime is Codex. Selecting hosted Devin
        # as a builder transport never promotes it to orchestrator.
        if self.runtime.product != "codex":
            raise SupervisorError("supervisor_unqualified")
        try:
            intent = slack_contract.normalize(task.slack_request, task.slack_policy, verified=True,
                                             registered_runners=task.registered_runners)
            identity = task.slack_request["identity"]
            if (intent["operation"] != "dispatch" or identity["runner"] != self.runner
                    or identity["repository"] != admission["binding"]["repository"]):
                raise SupervisorError("binding_mismatch")
            saved = task.session
            if (saved["schema"] != "code_mower.session.v1" or saved["host"] != self.runtime.product
                    or saved["orchestrator"] != self.runtime.product
                    or saved["repo"] != task.order.repository or saved["id"] != identity["session"]):
                raise SupervisorError("binding_mismatch")
            selected = tuple(m.get("execution", {}).get("transport", m["id"])
                             for m in saved["participants"])
            fresh = build_session(repo=task.order.repository, host=self.runtime.product,
                                  selected=selected, config=task.config)
            if not any(m["id"] == self.builder.product and m["builder"]
                       and m.get("execution", {}).get("transport", "agent_handoff") == self.builder.transport
                       for m in fresh["participants"]):
                raise SupervisorError("policy_denied")
            require_role(decide_role(self.runtime.product, "orchestrator", config=task.config))
        except SupervisorError:
            raise
        except Exception:
            raise SupervisorError("supervisor_unqualified") from None
        if not session_lease.verify_live_lease(
            repo=task.order.repository, session_id=saved["id"], orchestrator=self.runtime.product,
            root=task.checkout, now=datetime.fromtimestamp(now, timezone.utc),
        ).get("mutating"):
            raise SupervisorError("lease_unavailable")
        return task

    def _fingerprint(self, task):
        lease = session_lease.observe_lease_record(root=task.checkout)["record"]
        if lease is None:
            raise SupervisorError("lease_unavailable")
        return digest([task.admission, asdict(task.order), task.slack_request,
                       task.session["id"], lease["acquired_at"], str(task.checkout.resolve()), self.builder.identity])

    def _check(self, record, claim, action, *, request_key=""):
        if record is None or record.get("schema") not in {SCHEMA, v2.SCHEMA} or record["claim"] != claim:
            raise SupervisorError("binding_mismatch")
        if record["revoked"]:
            raise SupervisorError("claim_revoked")
        if claim["generation"] != self.runtime.generation:
            raise SupervisorError("supervisor_restarted")
        if self.clock() >= claim["expires_at"]:
            raise SupervisorError("claim_expired")
        task = self._authorize(record["admission"], action, request_key=request_key)
        if claim["generation"] != self.runtime.generation:
            raise SupervisorError("supervisor_restarted")
        if self.clock() >= claim["expires_at"]:
            raise SupervisorError("claim_expired")
        if record["fingerprint"] != self._fingerprint(task):
            raise SupervisorError("binding_mismatch")
        return replace(task, order=replace(task.order, acu_limit=record["plan"]["builder_acu"]))

    def _decide(self, locked, record, task, phase):
        limits = record["admission"]["limits"]
        if record["calls"] >= limits["runtime_calls"]:
            raise SupervisorError("budget_exhausted")
        record["calls"] += 1
        record["pending"] = "runtime"
        locked.write(record)  # Even a lost agent response consumes its allowance.
        request = dict(binding=record["admission"]["binding"], generation=self.runtime.generation,
                       scope_digest=record["scope_digest"], phase=phase,
                       status=record["status"], target=record["target"], limits=limits,
                       plan=record["plan"])
        try:
            decision = validate("decision", self.runtime.decide(task, request, timeout=limits["runtime_seconds"]))
        except Exception:
            raise SupervisorError("supervisor_unavailable") from None
        if (decision["binding"] != request["binding"] or decision["generation"] != request["generation"]
                or decision["generation"] != self.runtime.generation
                or decision["scope_digest"] != request["scope_digest"]
                or decision["builder_acu"] > min(limits["builder_acu"], task.order.acu_limit)
                or (record["plan"] is not None and any(decision[k] != record["plan"][k]
                    for k in ("builder_acu", "reviewer")))):
            raise SupervisorError("binding_mismatch")
        require_role(decide_role(self.runtime.product, "orchestrator", config=task.config,
                                runtime="ready"), execution=True)
        record["pending"] = None
        locked.write(record)
        return decision

    @staticmethod
    def _result(record):
        return validate("result", dict(schema="code_mower.supervisor_result." + _version(record),
            claim=record["claim"], status=record["status"], target=record["target"],
            **({"checkpoint": record.get("checkpoint")} if _version(record) == "v2" else {})))

    def _receipt(self, record):
        # A duplicate admission/handoff is a saved receipt, not a fresh result
        # observation. Only operate('result') may report exact-head completion.
        status = record["status"] | dict(implementation="pending", gate="unknown", merge="unknown",
            review="pending" if record["review_target"] else "not_requested")
        if status["state"] == "complete":
            status.update(state="reviewing", next_action="result")
        return self._result(record | dict(status=status, target=None))

    def admit(self, admission: dict) -> dict:
        version = _version(admission) if isinstance(admission, dict) else "v1"
        try:
            admission = validate("admission", admission)
            task = self._authorize(admission, "admit")
            with self.store.locked(self._key(admission)) as locked:
                record = locked.read()
                if record is not None:
                    if record["fingerprint"] != self._fingerprint(task):
                        raise SupervisorError("claim_conflict")
                    if record["claim"] is not None:
                        self._check(record, record["claim"], "admit")
                    # A duplicate never calls the agent or creates new work.
                    return self._receipt(record)
                record = dict(schema=admission["schema"], admission=admission, fingerprint=self._fingerprint(task),
                              scope_digest=digest(asdict(task.order)), claim=None, plan=None,
                              pending=None, calls=0, started=False, revoked=False, cancel=False,
                              review_target=None, status=_status(version=version), target=None)
                if version == "v2":
                    record.update(round=0, builder_binding=None, checkpoint=None, requests={},
                                  clarification_answers=0, fix_requests=0, review_requests=0)
                locked.write(record)
                try:
                    plan = self._decide(locked, record, task, "admit")
                    task = self._authorize(admission, "admit")
                    if record["fingerprint"] != self._fingerprint(task):
                        raise SupervisorError("binding_mismatch")
                    if plan["decision"] != "accept":
                        raise SupervisorError("policy_denied")
                    record["plan"] = plan
                    record["claim"] = validate("claim", dict(schema="code_mower.supervisor_claim." + version,
                        binding=admission["binding"], grant=admission["grant"], runner=self.runner,
                        session=task.session["id"], generation=plan["generation"], token=uuid.uuid4().hex,
                        scope_digest=record["scope_digest"], expires_at=min(admission["expires_at"], int(self.clock()) + 900)))
                    record["status"] = _status(version=version, state="claimed", next_action="status")
                except Exception as exc:
                    reason = exc.args[0] if isinstance(exc, SupervisorError) else "supervisor_unavailable"
                    record["status"] = failure(reason, state="waiting", version=version)["status"]
                locked.write(record)
                return self._result(record)
        except SupervisorError as exc:
            return failure(exc.args[0], version=version)
        except Exception:
            return failure("supervisor_unavailable", state="waiting", version=version)

    def _builder_call(self, record, claim, action, method, task, **kwargs):
        if _version(record) == "v2":
            def guard():
                self._check(record, claim, action)
            kwargs.update(guard=guard, expected_round=record["round"],
                          expected_binding=record["builder_binding"])
        return getattr(self.builder, method)(task, **kwargs)

    def _observed(self, record, observed, *, collect):
        if not isinstance(observed, Observation):
            raise SupervisorError("invalid_contract")
        if _version(record) == "v2":
            if (not observed.binding or not observed.checkpoint
                    or record["builder_binding"] not in {None, observed.binding}
                    or observed.round_number != record["round"]):
                raise SupervisorError("binding_mismatch")
            record.update(builder_binding=observed.binding, checkpoint=observed.checkpoint)
        changes = dict(lifecycle=validate("lifecycle", observed.lifecycle), writer=observed.writer)
        if collect:
            record["target"] = asdict(observed.target) if observed.target is not None else None
            changes.update(implementation="verified" if observed.target else "pending", merge=observed.merge)
        # Completion, exit, gate and merge are independent observations.
        if _version(record) == "v2" and not record["cancel"]:
            state = observed.lifecycle["state"]
            if state in {"waiting_for_user", "waiting_for_approval"}:
                changes.update(state=state,
                    reason="user_input_required" if state == "waiting_for_user" else "approval_required",
                    next_action="clarify" if state == "waiting_for_user" else "owner_action")
        record["status"] = validate("status", record["status"] | changes)

    def _review_allowed(self, task, target, reviewer, *, guard=lambda: None):
        guard()
        selected = {m["id"] for m in task.session["participants"] if m.get("reviewer")}
        if reviewer not in selected or self.reviews.ready(reviewer) is not True:
            raise SupervisorError("review_unavailable")
        guard()
        require_role(decide_role(reviewer, "reviewer", config=task.config,
                                runtime="ready"), execution=True)
        lineage = self.reviews.lineage(task, target)
        guard()
        if (lineage.target != target or self.builder.product not in lineage.contributors
                or not builder_lineage.admit(lineage, reviewer)):
            raise SupervisorError("review_unavailable")

    def operate(self, action: str, claim: dict, *, request_key: str | None = None) -> dict:
        """Authenticated re-entry. No operation retries an ambiguous mutation."""
        version = _version(claim) if isinstance(claim, dict) else "v1"
        if version == "v2" and action in {"clarify", "fix"}:
            from .supervisor_checkpoint import operate
            return operate(self, action, claim, request_key=request_key)
        try:
            if request_key is not None:
                raise SupervisorError("invalid_contract")
            if action not in {"handoff", "renew", "status", "result", "cancel"}:
                raise SupervisorError("invalid_contract")
            claim = validate("claim", claim)
            with self.store.locked(self._key(claim)) as locked:
                record = locked.read()
                task = self._check(record, claim, action)
                # An unanswered decision cannot have delegated work: the runtime
                # port has no side effects. Explicit cancellation may abandon it
                # without replaying it or refunding its charged call allowance.
                if record["pending"] and not (action == "cancel" and record["pending"] == "runtime"):
                    return self._uncertain(locked, record, "recovery_required")
                if action == "renew":
                    expiry = min(record["admission"]["expires_at"], int(self.clock()) + 900)
                    if expiry <= claim["expires_at"] or record["cancel"]:
                        return self._receipt(record)
                    decision = self._decide(locked, record, task, "renew")
                    self._check(record, claim, action)
                    if decision["decision"] != "accept":
                        raise SupervisorError("policy_denied")
                    # Only a still-live exact claim can be renewed. Its run,
                    # plan, invocation count and delegation reservations remain
                    # unchanged; old claim copies fail equality on re-entry.
                    record["claim"]["expires_at"] = expiry
                    locked.write(record)
                    return self._receipt(record)
                if action == "handoff":
                    if record["started"] or record["cancel"]:
                        return self._receipt(record)
                    decision = self._decide(locked, record, task, "handoff")
                    task = self._check(record, claim, action)
                    if decision["decision"] != "dispatch":
                        raise SupervisorError("policy_denied")
                    require_role(decide_role(self.builder.product, "builder", config=task.config,
                        transport=self.builder.transport, runtime="ready", bounded=True), execution=True)
                    record["pending"] = "dispatch"
                    record["status"] = _status(version=version, state="uncertain", writer="unknown",
                                                reason="handoff_uncertain", next_action="owner_action")
                    locked.write(record)
                    observed = self._builder_call(record, claim, action, "dispatch", task)
                    self._check(record, claim, action)
                    record.update(started=True, pending=None)
                    record["status"] = _status(version=version, state="running", next_action="status")
                    self._observed(record, observed, collect=False)
                elif action == "cancel":
                    if record["cancel"]:
                        return self._result(record)
                    record.update(cancel=True, pending="cancel")
                    record["status"] = record["status"] | dict(state="uncertain", reason="cancel_uncertain", next_action="owner_action")
                    locked.write(record)
                    if record["started"]:
                        self._observed(record, self._builder_call(record, claim, action, "cancel", task, request=claim["token"]), collect=False)
                        self._check(record, claim, action)
                    if record["review_target"] is not None:
                        target = builder_lineage.Target.from_mapping(record["review_target"])
                        writer = self.reviews.cancel(task, target, record["plan"]["reviewer"], key=claim["token"])
                        if writer not in {"running", "suspended", "terminated", "unknown"}:
                            raise SupervisorError("invalid_contract")
                        record["status"]["review_writer"] = writer
                        self._check(record, claim, action)
                    record["pending"] = None
                    self._cancel_status(record)
                elif record["started"]:
                    # Never carry a completion verdict through a refresh failure.
                    record["target"] = None
                    record["status"] = record["status"] | dict(state="running", reason="none", next_action="status",
                        implementation="pending", review="pending" if record["review_target"] else "not_requested",
                        gate="unknown", merge="unknown")
                    locked.write(record)
                    self._observed(record, self._builder_call(record, claim, action, "observe", task, collect=action == "result"), collect=action == "result")
                    self._check(record, claim, action)
                    if record["cancel"]:
                        if record["review_target"] is not None:
                            target = builder_lineage.Target.from_mapping(record["review_target"])
                            review = self._review_observation(task, target, record["plan"]["reviewer"])
                            record["status"]["review_writer"] = review["writer"]
                            self._check(record, claim, action)
                        self._cancel_status(record)
                    elif action == "result":
                        self._collect(locked, record, task, claim)
                locked.write(record)
                return self._result(record)
        except SupervisorError as exc:
            return failure(exc.args[0], version=version)
        except Exception:
            return failure("recovery_required", state="uncertain", version=version)

    @staticmethod
    def _cancel_status(record):
        exited = (record["status"]["writer"] in {"not_started", "terminated"}
                  and record["status"]["review_writer"] in {"not_started", "terminated"})
        if record["status"]["review_writer"] == "terminated":
            record["status"]["review"] = "cancelled"
        record["status"].update(state="cancelled" if exited else "running",
            reason="none" if exited else "cancel_pending", next_action="none" if exited else "status")

    def _uncertain(self, locked, record, reason):
        record["status"].update(state="uncertain", reason=reason, next_action="owner_action")
        locked.write(record)
        return self._result(record)

    def _collect(self, locked, record, task, claim):
        status = record["status"]
        if record["target"] is None:
            if status["state"] in {"waiting_for_user", "waiting_for_approval"}:
                return
            status.update(reason="result_not_ready", next_action="result")
            return
        if status["writer"] != "terminated":
            status.update(reason="writer_active", next_action="result")
            return
        target = builder_lineage.Target.from_mapping(record["target"])
        if target.repo != task.order.repository.lower() or target.branch != task.order.branch:
            raise SupervisorError("binding_mismatch")
        reviewer = record["plan"]["reviewer"]
        self._review_allowed(task, target, reviewer)
        if record["review_target"] is not None:
            if record["review_target"] != record["target"]:
                status.update(review="unavailable", gate="unknown")
                raise SupervisorError("head_changed")
            review = self._review_observation(task, target, reviewer)
            status.update(review=review["review"], gate=review["gate"], review_writer=review["writer"])
        decision = self._decide(locked, record, task, "result")
        task = self._check(record, claim, "result")
        if decision["decision"] == "review" and record["review_target"] is None:
            latest = self._builder_call(record, claim, "result", "observe", task, collect=True)
            if latest.target != target or latest.writer != "terminated":
                raise SupervisorError("head_changed")
            self._check(record, claim, "result")
            self._review_allowed(task, target, reviewer)
            if _version(record) == "v2":
                if record["review_requests"] >= record["admission"]["limits"]["review_requests"]:
                    raise SupervisorError("budget_exhausted")
                record["review_requests"] += 1
            record.update(pending="review", review_target=record["target"])
            locked.write(record)
            self.reviews.request(task, target, reviewer, key=claim["token"] + ("-" + str(record["round"]) if _version(record) == "v2" else ""))
            self._check(record, claim, "result")
            record["pending"] = None
            status.update(state="reviewing", review="pending", review_writer="unknown", reason="none", next_action="result")
        elif (decision["decision"] == "complete" and status["review"] == "passed"
              and status["gate"] == "passed" and status["review_writer"] == "terminated"):
            # Exact-head collection repeated after the runtime's decision prevents
            # its latency from turning a stale audit into completion.
            latest = self._builder_call(record, claim, "result", "observe", task, collect=True)
            if latest.target != target or latest.writer != "terminated":
                raise SupervisorError("head_changed")
            self._check(record, claim, "result")
            self._review_allowed(task, target, reviewer)
            review = self._review_observation(task, target, reviewer)
            if review["review"] != "passed" or review["gate"] != "passed" or review["writer"] != "terminated":
                raise SupervisorError("review_unavailable")
            self._check(record, claim, "result")
            self._observed(record, latest, collect=True)
            record["status"].update(state="complete", reason="none", next_action="none")
        elif decision["decision"] in {"wait", "owner_action"}:
            status.update(state="reviewing", reason="review_failed" if status["review"] == "failed" else "gate_pending",
                          next_action="owner_action" if decision["decision"] == "owner_action" else "result")
            if (_version(record) == "v2" and status["review"] == "failed"
                    and status["review_writer"] == "terminated"
                    and record["fix_requests"] < record["admission"]["limits"]["fix_requests"]
                    and record["review_requests"] < record["admission"]["limits"]["review_requests"]):
                status["next_action"] = "fix"
        else:
            raise SupervisorError("policy_denied")

    def _review_observation(self, task, target, reviewer):
        review = self.reviews.observe(task, target, reviewer)
        if (type(review) is not dict or set(review) != {"head_sha", "reviewer", "review", "gate", "writer"}
                or review["head_sha"] != target.head_sha or review["reviewer"] != reviewer
                or review["review"] not in {"pending", "passed", "failed", "unavailable", "cancelled"}
                or review["gate"] not in {"pending", "passed", "failed", "unknown"}
                or review["writer"] not in {"running", "suspended", "terminated", "unknown"}):
            raise SupervisorError("review_unavailable")
        return review

    def revoke(self, claim: dict) -> None:
        """Trusted authorization-store notification; no provider side effects."""
        claim = validate("claim", claim)
        with self.store.locked(self._key(claim)) as locked:
            record = locked.read()
            if record is None or record["claim"] != claim:
                raise SupervisorError("binding_mismatch")
            record["revoked"] = True
            locked.write(record)
