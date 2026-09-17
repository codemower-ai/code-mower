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
from .supervisor_contract import SCHEMA, SupervisorError, digest, validate


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


class Authorization(Protocol):
    def resolve(self, admission: dict, action: str) -> AuthorizedTask:
        """Independently reauthorize this caller/action; raise on revocation.

        Bind tenant/repository/work/run/operation and grant to private stored
        state. Admission/handoff require start authority, status requires status,
        cancel requires a newly authenticated cancellation, result requires the
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


class Builder(Protocol):
    product: str
    transport: str
    identity: str

    def dispatch(self, task: AuthorizedTask) -> Observation: ...
    def observe(self, task: AuthorizedTask, *, collect: bool) -> Observation: ...
    def cancel(self, task: AuthorizedTask, *, request: str) -> Observation: ...


class Reviews(Protocol):
    """Existing independent audit routing, not a new source of review authority.

    Every read is freshly bound to the exact Target. lineage must use verified
    contributor history (#963/#975), not a builder-supplied list. ready checks
    the selected existing lane runtime. request uses the existing broker and a
    durable idempotency key; no fallback, grant, merge or tracker write is added.
    observe returns only {head_sha, reviewer, review, gate}; gate comes from the
    authoritative code-mower/gate, not from the reviewer or builder's prose.
    """
    def lineage(self, task: AuthorizedTask, target: builder_lineage.Target) -> builder_lineage.Lineage: ...
    def ready(self, reviewer: str) -> bool: ...
    def request(self, task: AuthorizedTask, target: builder_lineage.Target,
                reviewer: str, *, key: str) -> None: ...
    def observe(self, task: AuthorizedTask, target: builder_lineage.Target, reviewer: str) -> dict: ...


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

    def _run(self, task, command, **kwargs):
        # Re-evaluate the current repository policy at every new execution.
        engine = DevinWorkOrders(self.engine.store.root, self.engine.remote, self.engine.github,
                                 config=task.config, runtime=self.engine.runtime)
        value = engine.run(command, task.order, apply=True, **kwargs)
        if "session" not in value:
            raise SupervisorError("builder_unavailable")
        if value["round"] != 0:
            # v1 authorizes one bounded round, never an implicit fix/recovery.
            raise SupervisorError("binding_mismatch")
        target, merge = None, "unknown"
        if value.get("verified_pr"):
            pr = value["verified_pr"]
            target = builder_lineage.Target(pr["repository"], pr["pr_number"],
                                            task.order.branch, pr["head_sha"])
            current = engine.github.read(target.repo, target.pr_number)
            if (current.head_sha != target.head_sha or current.head_branch != target.branch
                    or current.repository != target.repo):
                raise SupervisorError("head_changed")
            merge = current.state
        writer = engine.remote.writer_state(engine._key(task.order), repo=task.order.repository)
        return Observation(value["session"], writer, target, merge)

    def dispatch(self, task):
        return self._run(task, "dispatch", context=task.context)

    def observe(self, task, *, collect):
        return self._run(task, "collect" if collect else "status")

    def cancel(self, task, *, request):
        return self._run(task, "cancel", request=request)


def _status(**changes):
    return validate("status", dict(
        schema="code_mower.supervisor_status.v1", state="waiting", reason="none",
        next_action="none", implementation="pending", writer="not_started",
        review="not_requested", gate="unknown", merge="unknown",
        lifecycle=public_projection({"state": "pending"}),
        tracker_write=False, merge_authority=False,
    ) | changes)


def failure(reason: str, *, state="rejected") -> dict:
    action = {"supervisor_unavailable": "configure_supervisor",
              "supervisor_unqualified": "configure_supervisor",
              "not_registered": "configure_supervisor",
              "claim_expired": "new_authorization", "claim_revoked": "new_authorization"}.get(reason, "owner_action")
    return validate("result", dict(schema="code_mower.supervisor_result.v1", claim=None,
                                   status=_status(state=state, reason=reason, next_action=action), target=None))


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

    def _authorize(self, admission, action):
        if admission["runner"] != self.runner:
            raise SupervisorError("binding_mismatch")
        now = self.clock()
        if not isinstance(now, (int, float)) or not 0 < now < admission["expires_at"]:
            raise SupervisorError("claim_expired")
        try:
            task = self.authorization.resolve(validate("admission", admission), action)
        except Exception:
            raise SupervisorError("claim_revoked") from None
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

    def _check(self, record, claim, action):
        if record is None or record.get("schema") != SCHEMA or record["claim"] != claim:
            raise SupervisorError("binding_mismatch")
        if record["revoked"]:
            raise SupervisorError("claim_revoked")
        if claim["generation"] != self.runtime.generation:
            raise SupervisorError("supervisor_restarted")
        if self.clock() >= claim["expires_at"]:
            raise SupervisorError("claim_expired")
        task = self._authorize(record["admission"], action)
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
        return validate("result", dict(schema="code_mower.supervisor_result.v1",
            claim=record["claim"], status=record["status"], target=record["target"]))

    def _receipt(self, record):
        # A duplicate admission/handoff is a saved receipt, not a fresh result
        # observation. Only operate('result') may report exact-head completion.
        status = record["status"] | dict(implementation="pending", gate="unknown",
            review="pending" if record["review_target"] else "not_requested")
        if status["state"] == "complete":
            status.update(state="reviewing", next_action="result")
        return self._result(record | dict(status=status, target=None))

    def admit(self, admission: dict) -> dict:
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
                record = dict(schema=SCHEMA, admission=admission, fingerprint=self._fingerprint(task),
                              scope_digest=digest(asdict(task.order)), claim=None, plan=None,
                              pending=None, calls=0, started=False, revoked=False, cancel=False,
                              review_target=None, status=_status(), target=None)
                locked.write(record)
                try:
                    plan = self._decide(locked, record, task, "admit")
                    task = self._authorize(admission, "admit")
                    if record["fingerprint"] != self._fingerprint(task):
                        raise SupervisorError("binding_mismatch")
                    if plan["decision"] != "accept":
                        raise SupervisorError("policy_denied")
                    record["plan"] = plan
                    record["claim"] = validate("claim", dict(schema="code_mower.supervisor_claim.v1",
                        binding=admission["binding"], grant=admission["grant"], runner=self.runner,
                        session=task.session["id"], generation=plan["generation"], token=uuid.uuid4().hex,
                        scope_digest=record["scope_digest"], expires_at=min(admission["expires_at"], int(self.clock()) + 900)))
                    record["status"] = _status(state="claimed", next_action="status")
                except Exception as exc:
                    reason = exc.args[0] if isinstance(exc, SupervisorError) else "supervisor_unavailable"
                    record["status"] = failure(reason, state="waiting")["status"]
                locked.write(record)
                return self._result(record)
        except SupervisorError as exc:
            return failure(exc.args[0])
        except Exception:
            return failure("supervisor_unavailable", state="waiting")

    def _observed(self, record, observed, *, collect):
        if not isinstance(observed, Observation):
            raise SupervisorError("invalid_contract")
        changes = dict(lifecycle=validate("lifecycle", observed.lifecycle), writer=observed.writer)
        if collect:
            record["target"] = asdict(observed.target) if observed.target is not None else None
            changes.update(implementation="verified" if observed.target else "pending", merge=observed.merge)
        # Completion, exit, gate and merge are independent observations.
        record["status"] = validate("status", record["status"] | changes)

    def _review_allowed(self, task, target, reviewer):
        selected = {m["id"] for m in task.session["participants"] if m.get("reviewer")}
        if reviewer not in selected or not self.reviews.ready(reviewer):
            raise SupervisorError("review_unavailable")
        require_role(decide_role(reviewer, "reviewer", config=task.config,
                                runtime="ready"), execution=True)
        lineage = self.reviews.lineage(task, target)
        if (lineage.target != target or self.builder.product not in lineage.contributors
                or not builder_lineage.admit(lineage, reviewer)):
            raise SupervisorError("review_unavailable")

    def operate(self, action: str, claim: dict) -> dict:
        """Authenticated re-entry. No operation retries an ambiguous mutation."""
        try:
            if action not in {"handoff", "status", "result", "cancel"}:
                raise SupervisorError("invalid_contract")
            claim = validate("claim", claim)
            with self.store.locked(self._key(claim)) as locked:
                record = locked.read()
                task = self._check(record, claim, action)
                if record["pending"]:
                    return self._uncertain(locked, record, "recovery_required")
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
                    record["status"] = _status(state="uncertain", writer="unknown",
                                                reason="handoff_uncertain", next_action="owner_action")
                    locked.write(record)
                    observed = self.builder.dispatch(task)
                    self._check(record, claim, action)
                    record.update(started=True, pending=None)
                    record["status"] = _status(state="running", next_action="status")
                    self._observed(record, observed, collect=False)
                elif action == "cancel":
                    if record["cancel"]:
                        return self._result(record)
                    record.update(cancel=True, pending="cancel")
                    record["status"] = record["status"] | dict(state="uncertain", reason="cancel_uncertain", next_action="owner_action")
                    locked.write(record)
                    if record["started"]:
                        self._observed(record, self.builder.cancel(task, request=claim["token"]), collect=False)
                        self._check(record, claim, action)
                    record["pending"] = None
                    self._cancel_status(record)
                elif record["started"]:
                    # Never carry a completion verdict through a refresh failure.
                    record["target"] = None
                    record["status"] = record["status"] | dict(state="running", reason="none", next_action="status",
                        implementation="pending", review="pending" if record["review_target"] else "not_requested", gate="unknown")
                    locked.write(record)
                    self._observed(record, self.builder.observe(task, collect=action == "result"), collect=action == "result")
                    self._check(record, claim, action)
                    if record["cancel"]:
                        self._cancel_status(record)
                    elif action == "result":
                        self._collect(locked, record, task, claim)
                locked.write(record)
                return self._result(record)
        except SupervisorError as exc:
            return failure(exc.args[0])
        except Exception:
            return failure("recovery_required", state="uncertain")

    @staticmethod
    def _cancel_status(record):
        exited = record["status"]["writer"] in {"not_started", "terminated"}
        record["status"].update(state="cancelled" if exited else "running",
            reason="none" if exited else "cancel_pending", next_action="none" if exited else "status")

    def _uncertain(self, locked, record, reason):
        record["status"].update(state="uncertain", reason=reason, next_action="owner_action")
        locked.write(record)
        return self._result(record)

    def _collect(self, locked, record, task, claim):
        status = record["status"]
        if record["target"] is None:
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
            status.update(review=review["review"], gate=review["gate"])
        decision = self._decide(locked, record, task, "result")
        task = self._check(record, claim, "result")
        if decision["decision"] == "review" and record["review_target"] is None:
            self._review_allowed(task, target, reviewer)
            record.update(pending="review", review_target=record["target"])
            locked.write(record)
            self.reviews.request(task, target, reviewer, key=claim["token"])
            self._check(record, claim, "result")
            record["pending"] = None
            status.update(state="reviewing", review="pending", reason="none", next_action="result")
        elif decision["decision"] == "complete" and status["review"] == "passed" and status["gate"] == "passed":
            # Exact-head collection repeated after the runtime's decision prevents
            # its latency from turning a stale audit into completion.
            latest = self.builder.observe(task, collect=True)
            if latest.target != target or latest.writer != "terminated":
                raise SupervisorError("head_changed")
            self._check(record, claim, "result")
            self._review_allowed(task, target, reviewer)
            review = self._review_observation(task, target, reviewer)
            if review["review"] != "passed" or review["gate"] != "passed":
                raise SupervisorError("review_unavailable")
            self._observed(record, latest, collect=True)
            record["status"].update(state="complete", reason="none", next_action="none")
        elif decision["decision"] in {"wait", "owner_action"}:
            status.update(state="reviewing", reason="review_failed" if status["review"] == "failed" else "gate_pending",
                          next_action="owner_action" if decision["decision"] == "owner_action" else "result")
        else:
            raise SupervisorError("policy_denied")

    def _review_observation(self, task, target, reviewer):
        review = self.reviews.observe(task, target, reviewer)
        if (type(review) is not dict or set(review) != {"head_sha", "reviewer", "review", "gate"}
                or review["head_sha"] != target.head_sha or review["reviewer"] != reviewer
                or review["review"] not in {"pending", "passed", "failed", "unavailable"}
                or review["gate"] not in {"pending", "passed", "failed", "unknown"}):
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
