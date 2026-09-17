"""Offline v2 checkpoint and fix contract; no live or paid provider calls."""
from __future__ import annotations

import concurrent.futures
import copy
import json
import unittest
from dataclasses import replace
from unittest import mock

from code_mower import supervisor_contract as v1, supervisor_contract_v2 as v2
from code_mower.context_store import LockedConnection
from code_mower.supervisor import Supervisor
from code_mower.supervisor_checkpoint import AuthorizedInput
from test_supervisor import CANARY, HEAD, ROOT, PrivateQueueFixture, SupervisorCase


class InputStore(PrivateQueueFixture):
    def resolve(self, admission, action, *, request_key=""):
        task = super().resolve(admission, action)
        if action in {"clarify", "fix"}:
            if task.checkpoint_input.request["request_key"] != request_key:
                raise RuntimeError(CANARY)
        return task


class V2Case(SupervisorCase):
    def setUp(self):
        super().setUp()
        self.admission["schema"] = v2.SCHEMA
        self.admission["limits"].update(clarification_answers=2, fix_requests=1, review_requests=2)
        self.task = replace(self.task, admission=copy.deepcopy(self.admission))
        self.queue = InputStore(self.task)
        self.supervisor.authorization = self.queue
        self.usage = 1.0
        self.provider.usage = lambda binding: self.usage

    def record(self):
        return self.supervisor.store.read_only(self.supervisor._key(self.admission))

    def checkpoint(self, claim, action="clarify", request_key="answer_1"):
        if action == "clarify":
            self.provider.set_state(self.binding(), "owner_action", reason="waiting_for_owner")
            result = self.supervisor.operate("status", claim)
            self.assertEqual(result["status"]["state"], "waiting_for_user", result)
            target = None
        else:
            self.complete()
            result = self.supervisor.operate("result", claim)
            self.assertEqual(result["status"]["state"], "reviewing", result)
            self.reviews.review, self.reviews.writer = "failed", "terminated"
            from code_mower.builder_lineage import Target
            target = Target.from_mapping(result["target"])
        request = dict(schema="code_mower.supervisor_request.v2", claim=copy.deepcopy(claim),
                       action=action, request_key=request_key)
        value = AuthorizedInput(request, CANARY, result["checkpoint"], claim["scope_digest"],
                                target, "findings_1" if target else None)
        self.queue.task = replace(self.task, checkpoint_input=value)
        return value

    def send(self, claim, action="clarify", request_key="answer_1"):
        return self.supervisor.operate(action, claim, request_key=request_key)


class CheckpointTests(V2Case):
    def test_clarification_resumes_same_writer_once_with_saved_outcome(self):
        claim = self.started()
        self.checkpoint(claim)
        before = self.record()
        with mock.patch.object(self.provider, "message", wraps=self.provider.message) as message, \
             mock.patch.object(self.provider, "create", wraps=self.provider.create) as create:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.send(claim), range(2)))
            self.assertEqual(results[0]["status"]["state"], "running", results)
            self.assertEqual(results[0], results[1])
            self.assertEqual(message.call_count, 1)
            self.assertEqual(create.call_count, 0)
            self.assertIn("Preserve safe mode", message.call_args.args[1])
        after = self.record()
        for key in ("admission", "plan", "scope_digest", "builder_binding", "calls", "fix_requests"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(after["round"], 1)
        self.assertEqual(after["clarification_answers"], 1)
        self.complete(round=1)
        reviewed = self.supervisor.operate("result", claim)
        self.assertEqual(reviewed["status"]["state"], "reviewing", reviewed)
        self.assertEqual(self.send(claim), results[0])
        self.assertNotIn(CANARY, json.dumps(after))

    def test_fix_requires_exact_independent_failed_review_and_preserves_cap(self):
        claim = self.started()
        self.checkpoint(claim, "fix", "fix_1")
        before = self.record()
        result = self.send(claim, "fix", "fix_1")
        self.assertEqual(result["status"]["state"], "running", result)
        self.assertEqual(self.record()["fix_requests"], 1)
        self.assertEqual(self.record()["plan"], before["plan"])
        self.assertEqual(self.record()["review_requests"], 1)
        self.assertIsNone(result["target"])
        self.github.pr = replace(self.github.pr, head_sha="b" * 40)
        self.complete(round=1, head_sha="b" * 40)
        reviewed = self.supervisor.operate("result", claim)
        self.assertEqual(reviewed["status"]["state"], "reviewing", reviewed)
        self.assertEqual(len(self.reviews.requests), 2)
        self.assertNotEqual(self.reviews.requests[0][2], self.reviews.requests[1][2])
        self.reviews.review, self.reviews.gate = "passed", "passed"
        done = self.supervisor.operate("result", claim)
        self.assertEqual(done["status"]["state"], "complete", done)
        self.assertFalse(done["status"]["merge_authority"])
        budget = self.agent.calls[-1][0]["checkpoint_budget"]
        self.assertEqual(budget, dict(round=1, remaining=dict(
            clarification_answers=2, fix_requests=0, review_requests=0)))

    def test_cancel_uses_exact_round_review_key_before_and_after_fix(self):
        claim = self.started()
        self.checkpoint(claim, "fix", "fix_1")
        self.send(claim, "fix", "fix_1")
        self.github.pr = replace(self.github.pr, head_sha="b" * 40)
        self.complete(round=1, head_sha="b" * 40)
        self.supervisor.operate("result", claim)
        result = self.supervisor.operate("cancel", claim)
        self.assertEqual(result["status"]["state"], "cancelled", result)
        self.assertEqual(self.reviews.cancellations[-1], self.reviews.requests[-1])

    def test_waiting_for_approval_never_accepts_clarification(self):
        claim = self.started()
        self.checkpoint(claim)
        self.provider.set_state(self.binding(), "owner_action", reason="approval_required")
        with mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            result = self.send(claim)
            self.assertEqual(result["status"]["reason"], "approval_required", result)
            self.assertEqual(message.call_count, 0)
        status = self.supervisor.operate("result", claim)
        self.assertEqual(status["status"]["state"], "waiting_for_approval", status)
        self.assertEqual(status["status"]["next_action"], "owner_action")
        self.assertEqual(self.record()["clarification_answers"], 0)

    def test_running_and_stale_checkpoint_fail_without_send(self):
        claim = self.started()
        value = self.checkpoint(claim)
        with mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.provider.set_state(self.binding(), "running")
            self.assertEqual(self.send(claim)["status"]["reason"], "wrong_checkpoint")
            self.provider.set_state(self.binding(), "waiting_for_user")
            self.queue.task = replace(self.queue.task, checkpoint_input=replace(value, checkpoint="f" * 64))
            self.assertEqual(self.send(claim)["status"]["reason"], "wrong_checkpoint")
            self.assertEqual(message.call_count, 0)

    def test_changed_input_key_conflict_is_not_a_second_message(self):
        claim = self.started()
        value = self.checkpoint(claim)
        self.assertEqual(self.send(claim)["status"]["state"], "running")
        self.queue.task = replace(self.queue.task, checkpoint_input=replace(value, prose="changed private answer"))
        with mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.assertEqual(self.send(claim)["status"]["reason"], "request_conflict")
            self.assertEqual(message.call_count, 0)

    def test_pending_is_fsynced_before_message_and_crash_cannot_resend(self):
        claim = self.started()
        self.checkpoint(claim)
        real_message = self.provider.message

        def crash(binding, prose):
            record = self.record()
            self.assertEqual(record["pending"], "clarify")
            self.assertEqual(record["clarification_answers"], 1)
            self.assertIsNone(record["requests"]["answer_1"]["outcome"])
            self.assertGreater(fsync.call_count, 0)
            real_message(binding, prose)
            raise SystemExit("synthetic process crash")

        with mock.patch("code_mower.context_store.os.fsync", wraps=__import__("os").fsync) as fsync, \
             mock.patch.object(self.provider, "message", side_effect=crash) as message:
            with self.assertRaises(SystemExit):
                self.send(claim)
            self.supervisor = Supervisor(root=self.root / "supervisor", runner="codex_remote",
                authorization=self.queue, runtime=self.agent, builder=self.builder,
                reviews=self.reviews, clock=lambda: self.now)
            result = self.send(claim)
            self.assertEqual(result["status"]["reason"], "mutation_uncertain", result)
            self.assertEqual(message.call_count, 1)
            self.assertEqual(self.supervisor.operate("handoff", claim)["status"]["state"], "uncertain")

    def test_ambiguous_send_and_lost_outcome_never_resend(self):
        claim = self.started()
        self.checkpoint(claim)
        with mock.patch.object(self.provider, "message", side_effect=RuntimeError(CANARY)) as message:
            for _ in range(2):
                result = self.send(claim)
                self.assertEqual(result["status"]["state"], "uncertain", result)
                self.assertNotIn(CANARY, json.dumps(result))
            self.assertEqual(message.call_count, 1)

    def test_crash_before_provider_call_leaves_one_pending_intent(self):
        claim = self.started()
        self.checkpoint(claim)
        real_write = LockedConnection.write

        def crash(locked, record):
            real_write(locked, record)
            if record.get("pending") == "clarify":
                raise SystemExit("synthetic crash after fsync")
        with mock.patch.object(LockedConnection, "write", crash), \
             mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            with self.assertRaises(SystemExit):
                self.send(claim)
            self.assertEqual(message.call_count, 0)
        self.assertEqual(self.send(claim)["status"]["reason"], "mutation_uncertain")

    def test_revocation_and_runtime_restart_deny_duplicates(self):
        claim = self.started()
        self.checkpoint(claim)
        self.send(claim)
        self.queue.active = False
        self.assertEqual(self.send(claim)["status"]["reason"], "claim_revoked")
        self.queue.active = True
        self.agent.generation = "restarted"
        self.assertEqual(self.send(claim)["status"]["reason"], "supervisor_restarted")

    def test_revocation_during_send_is_uncertain_and_not_repeated(self):
        claim = self.started()
        self.checkpoint(claim)
        real_message = self.provider.message

        def revoke(*args):
            value = real_message(*args)
            self.queue.active = False
            return value
        with mock.patch.object(self.provider, "message", side_effect=revoke) as message:
            self.assertEqual(self.send(claim)["status"]["state"], "uncertain")
            self.queue.active = True
            self.assertEqual(self.send(claim)["status"]["reason"], "mutation_uncertain")
            self.assertEqual(message.call_count, 1)

    def test_changed_claim_grant_provider_account_and_scope_fail_closed(self):
        claim = self.started()
        value = self.checkpoint(claim)
        with mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            for field in ("token", "session", "generation", "grant"):
                bad = copy.deepcopy(claim)
                bad[field] = "different"
                self.assertEqual(self.send(bad)["status"]["reason"], "binding_mismatch")
            original = self.queue.task
            new_admission = copy.deepcopy(self.admission)
            new_admission["grant"] = "new_revision"
            self.queue.task = replace(original, admission=new_admission)
            self.assertEqual(self.send(claim)["status"]["reason"], "binding_mismatch")
            self.queue.task = replace(original, checkpoint_input=replace(value, scope_digest="b" * 64))
            self.assertEqual(self.send(claim)["status"]["reason"], "binding_mismatch")
            self.queue.task = original
            self.provider.account = "different_account"
            self.assertEqual(self.send(claim)["status"]["reason"], "binding_mismatch")
            self.assertEqual(message.call_count, 0)

    def test_changed_head_contributor_and_review_denied_before_fix(self):
        claim = self.started()
        self.checkpoint(claim, "fix", "fix_1")
        with mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.reviews.contributors = ("devin", "claude")
            self.assertEqual(self.send(claim, "fix", "fix_1")["status"]["reason"], "review_unavailable")
            self.reviews.contributors = ("devin",)
            self.reviews.review = "passed"
            self.assertEqual(self.send(claim, "fix", "fix_1")["status"]["reason"], "fix_not_authorized")
            self.reviews.review = "failed"
            self.github.pr = replace(self.github.pr, head_sha="b" * 40)
            self.assertNotEqual(self.send(claim, "fix", "fix_1")["status"]["state"], "running")
            self.assertEqual(message.call_count, 0)

    def test_cap_exhaustion_and_missing_usage_cannot_send(self):
        claim = self.started()
        self.checkpoint(claim)
        self.usage = 5
        with mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.assertEqual(self.send(claim)["status"]["reason"], "budget_exhausted")
            self.assertEqual(message.call_count, 0)
            self.assertEqual(self.record()["clarification_answers"], 0)
            self.usage = 0
            self.assertEqual(self.send(claim)["status"]["state"], "running")
            self.assertEqual(message.call_count, 1)

    def test_zero_fix_allowance_cannot_be_added_after_handoff(self):
        self.admission["limits"]["fix_requests"] = 0
        self.task = replace(self.task, admission=copy.deepcopy(self.admission))
        self.queue.task = self.task
        claim = self.started()
        self.checkpoint(claim, "fix", "fix_1")
        self.assertEqual(self.send(claim, "fix", "fix_1")["status"]["reason"], "budget_exhausted")

    def test_private_projection_and_closed_wire(self):
        claim = self.started()
        value = self.checkpoint(claim)
        result = self.send(claim)
        public = json.dumps(v2.public_status(result))
        for secret in (CANARY, self.binding(), str(self.root), claim["token"], HEAD,
                       value.checkpoint, claim["scope_digest"], claim["binding"]["tenant"]):
            self.assertNotIn(secret, public)
        for key in ("prose", "body", "finding_ref", "approval", "acu_limit"):
            with self.assertRaises(v1.SupervisorError):
                v2.validate("request", value.request | {key: CANARY})
        for key in ("", "x" * 65, "raw prose", None, 123):
            self.assertEqual(self.send(claim, request_key=key)["status"]["reason"], "invalid_contract")


class CompatibilityTests(V2Case):
    def test_all_frozen_v1_fixtures_and_operation_enum_are_unchanged(self):
        fixtures = json.loads((ROOT / "src/code_mower/supervisor_contract.fixtures.json").read_text())
        for kind, value in fixtures["valid"].items():
            self.assertEqual(v1.validate(kind, value), value)
        for action in ("clarify", "fix"):
            with self.assertRaises(v1.SupervisorError):
                v1.validate("request", fixtures["valid"]["request"] | {"action": action})
        self.assertEqual(set(v1.schema()["$defs"]["request"]["properties"]["action"]["enum"]),
                         {"handoff", "renew", "status", "result", "cancel"})

    def test_v1_claim_cannot_acquire_v2_authority_or_replace_its_writer(self):
        self.admission["schema"] = v1.SCHEMA
        self.admission["limits"].pop("clarification_answers")
        self.admission["limits"].pop("fix_requests")
        self.admission["limits"]["review_requests"] = 1
        self.task = replace(self.task, admission=copy.deepcopy(self.admission))
        self.queue.task = self.task
        claim = self.started()
        self.assertEqual(self.send(claim)["status"]["reason"], "invalid_contract")
        changed = copy.deepcopy(self.admission)
        changed["schema"] = v2.SCHEMA
        changed["limits"].update(clarification_answers=1, fix_requests=1)
        self.queue.task = replace(self.task, admission=changed)
        self.assertEqual(self.supervisor.admit(changed)["status"]["reason"], "claim_conflict")


class FenceTests(V2Case):
    def test_verified_target_never_overrides_waiting_checkpoint(self):
        claim = self.started()
        self.complete()
        observe = self.builder.observe

        for state, reason in (("waiting_for_user", "user_input_required"),
                              ("waiting_for_approval", "approval_required")):
            with self.subTest(state=state):
                def waiting(*args, state=state, reason=reason, **kwargs):
                    observed = observe(*args, **kwargs)
                    return replace(observed, lifecycle=observed.lifecycle | dict(state=state, reason=reason))

                calls = len(self.agent.calls)
                with mock.patch.object(self.builder, "observe", side_effect=waiting):
                    result = self.supervisor.operate("result", claim)
                self.assertEqual(result["status"]["state"], state, result)
                self.assertIsNotNone(result["target"])
                self.assertEqual(len(self.agent.calls), calls)
                self.assertEqual(self.reviews.requests, [])

    def test_renewed_live_claim_can_retrieve_same_saved_answer(self):
        claim = self.started()
        value = self.checkpoint(claim)
        first = self.send(claim)
        self.now += 60
        renewed = self.supervisor.operate("renew", claim)["claim"]
        self.assertGreater(renewed["expires_at"], claim["expires_at"])
        self.queue.task = replace(self.queue.task, checkpoint_input=replace(value,
            request=value.request | {"claim": renewed}))
        with mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.assertEqual(self.send(renewed), first)
            self.assertEqual(self.send(claim)["status"]["reason"], "binding_mismatch")
            self.assertEqual(message.call_count, 0)

    def test_authorization_latency_cannot_outlive_claim_or_generation(self):
        claim = self.started()
        self.checkpoint(claim)
        resolve = self.queue.resolve

        def restart(*args, **kwargs):
            value = resolve(*args, **kwargs)
            self.agent.generation = "new_generation"
            return value

        with mock.patch.object(self.queue, "resolve", side_effect=restart):
            self.assertEqual(self.send(claim)["status"]["reason"], "supervisor_restarted")
        self.agent.generation = claim["generation"]

        def expire(*args, **kwargs):
            value = resolve(*args, **kwargs)
            self.now = claim["expires_at"]
            return value

        with mock.patch.object(self.queue, "resolve", side_effect=expire):
            self.assertEqual(self.send(claim)["status"]["reason"], "claim_expired")
        self.assertIsNone(self.record()["pending"])

    def test_refuses_missing_and_invalid_usage_without_charging(self):
        claim = self.started()
        self.checkpoint(claim)
        with mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            for usage in (None, True, -1, float("nan"), float("inf"), "1"):
                self.usage = usage
                self.assertEqual(self.send(claim)["status"]["reason"], "usage_unavailable")
            del self.provider.usage
            self.assertEqual(self.send(claim)["status"]["reason"], "usage_unavailable")
            self.assertEqual(message.call_count, 0)
            self.assertEqual(self.record()["clarification_answers"], 0)

    def test_live_lease_expiry_and_replacement_deny_answer(self):
        from code_mower import session_lease
        from datetime import datetime, timezone
        claim = self.started()
        self.checkpoint(claim)
        self.now = claim["expires_at"]
        self.assertEqual(self.send(claim)["status"]["reason"], "claim_expired")
        self.now -= 1
        session_lease.release_lease(root=self.checkout, session_id=claim["session"])
        self.assertEqual(self.send(claim)["status"]["reason"], "lease_unavailable")
        session_lease.acquire_lease(root=self.checkout, repo=self.order.repository, orchestrator="codex",
            session_id=claim["session"], now=datetime.fromtimestamp(self.now, timezone.utc))
        self.assertEqual(self.send(claim)["status"]["reason"], "binding_mismatch")

    def test_last_pre_send_checkpoint_change_is_never_sent(self):
        claim = self.started()
        self.checkpoint(claim)
        real_run = self.builder.resume

        def change(*args, **kwargs):
            self.provider.set_state(self.binding(), "owner_action", reason="approval_required")
            return real_run(*args, **kwargs)
        with mock.patch.object(self.builder, "resume", side_effect=change), \
             mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.assertEqual(self.send(claim)["status"]["reason"], "mutation_uncertain")
            self.assertEqual(message.call_count, 0)
        self.assertEqual(self.record()["clarification_answers"], 1)
        self.assertEqual(self.send(claim)["status"]["reason"], "mutation_uncertain")

    def test_head_and_allowance_rechecked_at_last_pre_send_fence(self):
        claim = self.started()
        self.checkpoint(claim, "fix", "fix_1")
        real_resume = self.builder.resume

        def change(*args, **kwargs):
            # Work-order verification and remote reservation have already begun
            # when this usage read moves the head. The final fence must refuse.
            def usage(binding):
                self.github.pr = replace(self.github.pr, head_sha="b" * 40)
                return 1
            self.provider.usage = usage
            return real_resume(*args, **kwargs)
        with mock.patch.object(self.builder, "resume", side_effect=change), \
             mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.assertEqual(self.send(claim, "fix", "fix_1")["status"]["reason"], "mutation_uncertain")
            self.assertEqual(message.call_count, 0)

    def test_revocation_after_read_stops_before_intent(self):
        claim = self.started()
        self.checkpoint(claim)
        real_get = self.provider.get

        def revoke(binding):
            value = real_get(binding)
            self.queue.active = False
            return value
        with mock.patch.object(self.provider, "get", side_effect=revoke), \
             mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.assertNotEqual(self.send(claim)["status"]["state"], "running")
            self.assertEqual(message.call_count, 0)
        self.assertIsNone(self.record()["pending"])
        self.assertEqual(self.record()["clarification_answers"], 0)

    def test_saved_outcome_write_failure_cannot_clear_pending(self):
        claim = self.started()
        self.checkpoint(claim)
        original = LockedConnection.write

        def fail_once(locked, record):
            if (record.get("schema") == v2.SCHEMA
                    and record.get("requests", {}).get("answer_1", {}).get("outcome") is not None):
                raise OSError("synthetic disk error")
            return original(locked, record)
        with mock.patch.object(LockedConnection, "write", fail_once), \
             mock.patch.object(self.provider, "message", wraps=self.provider.message) as message:
            self.assertEqual(self.send(claim)["status"]["reason"], "mutation_uncertain")
            self.assertEqual(self.send(claim)["status"]["reason"], "mutation_uncertain")
            self.assertEqual(message.call_count, 1)
        self.assertEqual(self.record()["pending"], "clarify")
        self.assertIsNone(self.record()["requests"]["answer_1"]["outcome"])

    def test_new_key_cannot_recover_pending_send(self):
        claim = self.started()
        value = self.checkpoint(claim)
        with mock.patch.object(self.provider, "message", side_effect=RuntimeError(CANARY)) as message:
            self.send(claim)
            self.queue.task = replace(self.queue.task, checkpoint_input=replace(value,
                request=value.request | {"request_key": "answer_2"}))
            self.assertEqual(self.send(claim, request_key="answer_2")["status"]["reason"], "recovery_required")
            self.assertEqual(message.call_count, 1)
        self.assertEqual(self.record()["clarification_answers"], 1)

    def test_second_answer_consumes_only_answer_allowance(self):
        claim = self.started()
        self.checkpoint(claim)
        self.send(claim)
        self.checkpoint(claim, request_key="answer_2")
        self.assertEqual(self.send(claim, request_key="answer_2")["status"]["state"], "running")
        self.checkpoint(claim, request_key="answer_3")
        self.assertEqual(self.send(claim, request_key="answer_3")["status"]["reason"], "budget_exhausted")
        self.assertEqual(self.record()["round"], 2)
        self.assertEqual(self.record()["fix_requests"], 0)

    def test_fix_requires_room_for_next_independent_audit(self):
        self.admission["limits"]["review_requests"] = 1
        self.task = replace(self.task, admission=copy.deepcopy(self.admission))
        self.queue.task = self.task
        claim = self.started()
        self.checkpoint(claim, "fix", "fix_1")
        self.assertEqual(self.send(claim, "fix", "fix_1")["status"]["reason"], "budget_exhausted")
        self.assertEqual(self.record()["fix_requests"], 0)

    def test_running_reviewer_or_changed_finding_cannot_authorize_fix(self):
        claim = self.started()
        value = self.checkpoint(claim, "fix", "fix_1")
        self.reviews.writer = "running"
        self.assertEqual(self.send(claim, "fix", "fix_1")["status"]["reason"], "fix_not_authorized")
        self.reviews.writer = "terminated"
        self.queue.task = replace(self.queue.task, checkpoint_input=replace(value, finding_ref=None))
        self.assertEqual(self.send(claim, "fix", "fix_1")["status"]["reason"], "fix_not_authorized")

    def test_fixed_head_offers_fix_in_closed_projection(self):
        claim = self.started()
        self.checkpoint(claim, "fix", "fix_1")
        result = self.supervisor.operate("result", claim)
        self.assertEqual(result["status"]["next_action"], "fix", result)
        self.assertEqual(result["status"]["reason"], "review_failed")

    def test_replaced_provider_binding_denies_saved_receipt(self):
        from code_mower.remote_session import _key
        claim = self.started()
        self.checkpoint(claim)
        self.send(claim)
        with self.remote.store.locked(_key(self.key)) as locked:
            saved = locked.read()
            saved["binding"] = "different_provider_session"
            locked.write(saved)
        self.assertEqual(self.send(claim)["status"]["reason"], "binding_mismatch")


class PackagedV2Tests(unittest.TestCase):
    def test_packaged_fixtures_both_versions_and_closed_decode(self):
        fixtures = json.loads((ROOT / "src/code_mower/supervisor_contract_v2.fixtures.json").read_text())
        self.assertFalse(fixtures["live_hosted_evidence"])
        for kind, value in fixtures["valid"].items():
            self.assertEqual(v2.validate(kind, value), value)
            self.assertEqual(v2.decode(kind, json.dumps(value).encode()), value)
        for value in fixtures["requests"]:
            self.assertEqual(v2.validate("request", value), value)
            with self.assertRaises(v1.SupervisorError):
                v1.validate("request", value)
        for value in fixtures["statuses"]:
            self.assertEqual(v2.validate("status", value), value)
        for raw in (b'{"schema":1,"schema":2}', b'[]', b'{"x":NaN}', b' ' * 65537):
            with self.assertRaises(v1.SupervisorError):
                v2.decode("admission", raw)
        self.assertEqual(v2.schema()["$defs"]["lifecycle"], v1.schema()["$defs"]["lifecycle"])
        self.assertEqual(v2.schema()["$defs"]["decision"], v1.schema()["$defs"]["decision"])

    def test_materialized_inventory_includes_new_contract_files(self):
        from code_mower.package_manifest import PACKAGE_FILES
        targets = {target for _, target, _ in PACKAGE_FILES}
        for name in ("supervisor_checkpoint.py", "supervisor_contract_v2.py",
                     "supervisor_contract_v2.schema.json", "supervisor_contract_v2.fixtures.json"):
            self.assertIn("src/code_mower/" + name, targets)
        committed = json.loads((ROOT / "code-mower-package-manifest.json").read_text())
        inventory = {item["target"] for item in committed["files_written"]}
        self.assertTrue(all("src/code_mower/" + name in inventory for name in
            ("supervisor_contract_v2.schema.json", "supervisor_contract_v2.fixtures.json")))
