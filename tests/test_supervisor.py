"""Synthetic private queue/runtime/provider integration. No paid sessions."""
from __future__ import annotations

import concurrent.futures
import copy
import json
import os
import shlex
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from code_mower import builder_lineage, session, session_lease
from code_mower.context_store import ContextStore
from code_mower.devin_sessions import Session
from code_mower.supervisor import AuthorizedTask, HostedBuilder, Supervisor
from code_mower.supervisor_codex import CodexRuntime
from code_mower.supervisor_contract import SupervisorError, decode, public_status, schema, validate
from test_devin_work_orders import CANARY, HEAD, WorkOrderCase

ROOT = Path(__file__).resolve().parents[1]


def decision(request):
    phase, status = request["phase"], request["status"]
    action = {"admit": "accept", "renew": "accept", "handoff": "dispatch"}.get(phase)
    if action is None:
        action = ("review" if status["review"] == "not_requested" else
                  "complete" if status["review"] == "passed" and status["gate"] == "passed" else "wait")
    return dict(schema="code_mower.supervisor_decision.v1", binding=request["binding"],
        generation=request["generation"], scope_digest=request["scope_digest"], decision=action,
        builder_acu=5, reviewer="claude")


class AgentFixture:
    product = "codex"
    generation = "generation_example"

    def __init__(self):
        self.calls = []
        self.after = lambda: None
        self.change = {}

    def decide(self, task, request, *, timeout):
        self.calls.append((copy.deepcopy(request), timeout))
        result = decision(request) | self.change
        self.after()
        return result


class PrivateQueueFixture:
    """Fake #919/#920 resolver; authorization cannot be supplied by the request."""
    def __init__(self, task):
        self.task = task
        self.active = True
        self.actions = []

    def resolve(self, admission, action):
        self.actions.append(action)
        if not self.active:
            raise RuntimeError(CANARY)
        return self.task


class ReviewFixture:
    def __init__(self):
        self.requests = []
        self.available = True
        self.contributors = ("devin",)
        self.review = "pending"
        self.gate = "pending"
        self.writer = None
        self.cancellations = []
        self.after_observe = lambda: None

    def lineage(self, task, target):
        identity = builder_lineage.Identity({"enabled": True,
            "labels": {"builder:" + p: p for p in self.contributors}, "authors": {}})
        chain = builder_lineage.Chain.from_arrivals(target, [])
        return builder_lineage.resolve(chain, identity, "", ["builder:" + p for p in self.contributors])

    def ready(self, reviewer):
        return self.available

    def request(self, task, target, reviewer, *, key):
        self.requests.append((target, reviewer, key))

    def observe(self, task, target, reviewer):
        result = dict(head_sha=target.head_sha, reviewer=reviewer, review=self.review, gate=self.gate,
                      writer=self.writer or ("terminated" if self.review == "passed" else "running"))
        self.after_observe()
        return result

    def cancel(self, task, target, reviewer, *, key):
        self.cancellations.append((target, reviewer, key))
        self.writer = "terminated"
        return self.writer


class SupervisorCase(WorkOrderCase):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        (self.checkout / ".git").mkdir()
        fixture = json.loads((ROOT / "tests/fixtures/slack_contracts.json").read_text())
        self.request, self.policy = fixture["requests"][0], fixture["grant"]
        self.request["text"] = CANARY
        self.admission = dict(schema="code_mower.supervisor.v1",
            binding=dict(tenant="tenant_example", repository="repo_binding_example", work="work_example",
                         run="run_example", operation="operation_example"),
            grant="grant_example", runner="codex_remote", expires_at=int(self.now) + 3600,
            limits=dict(builder_acu=5, runtime_calls=8, runtime_seconds=10, review_requests=1))
        saved = session.build_session(repo=self.order.repository, host="codex",
            selected=("codex", "claude", "devin_api_v3"), config=self.service.role_config)
        saved["id"] = "session_example"
        session_lease.acquire_lease(root=self.checkout, repo=self.order.repository,
            orchestrator="codex", session_id=saved["id"], now=datetime.fromtimestamp(self.now, timezone.utc))
        self.task = AuthorizedTask(copy.deepcopy(self.admission), self.order, self.request, self.policy,
            frozenset({"codex_remote"}), saved, self.checkout, self.service.role_config)
        self.queue = PrivateQueueFixture(self.task)
        self.agent = AgentFixture()
        self.reviews = ReviewFixture()
        self.builder = HostedBuilder(self.service)
        self.supervisor = Supervisor(root=self.root / "supervisor", runner="codex_remote",
            authorization=self.queue, runtime=self.agent, builder=self.builder,
            reviews=self.reviews, clock=lambda: self.now)

    def admitted(self):
        result = self.supervisor.admit(self.admission)
        self.assertEqual(result["status"]["state"], "claimed", result)
        self.assertIsNotNone(result["claim"])
        return result["claim"]

    def started(self):
        claim = self.admitted()
        result = self.supervisor.operate("handoff", claim)
        self.assertEqual(result["status"]["state"], "running", result)
        return claim

    def result(self, claim):
        result = self.supervisor.operate("result", claim)
        self.assertNotIn(CANARY, json.dumps(result))
        return result


class AdmissionTests(SupervisorCase):
    def test_live_acceptance_precedes_builder_create_and_duplicate_claim_is_atomic(self):
        with mock.patch.object(self.provider, "create", wraps=self.provider.create) as create:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                values = list(pool.map(lambda _: self.supervisor.admit(self.admission), range(2)))
            self.assertEqual(values[0], values[1])
            self.assertEqual(len(self.agent.calls), 1)
            self.assertEqual(create.call_count, 0)
            claim = values[0]["claim"]
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: self.supervisor.operate("handoff", claim), range(2)))
            self.assertEqual(create.call_count, 1)
            self.assertEqual(len(self.agent.calls), 2)
            self.assertIn("handoff", self.queue.actions)

    def test_registration_configuration_and_role_are_not_readiness(self):
        for condition, reason in (("unregistered", "not_registered"), ("unqualified", "supervisor_unqualified"),
                                  ("unreachable", "supervisor_unavailable"), ("no_lease", "lease_unavailable")):
            with self.subTest(condition=condition):
                original = self.queue.task
                with mock.patch.object(self.provider, "create", wraps=self.provider.create) as create:
                    if condition == "unregistered":
                        self.queue.task = replace(original, registered_runners=frozenset())
                    elif condition == "unqualified":
                        self.agent.product = "devin"
                    elif condition == "unreachable":
                        self.agent.after = mock.Mock(side_effect=RuntimeError(CANARY))
                    else:
                        session_lease.release_lease(root=self.checkout, session_id="session_example")
                    result = self.supervisor.admit(self.admission)
                    self.assertEqual(result["status"]["reason"], reason, result)
                    self.assertIsNone(result["claim"])
                    self.assertEqual(create.call_count, 0)
                    self.assertNotIn(CANARY, json.dumps(result))
                self.queue.task = original
                self.agent.product = "codex"

    def test_narrowing_role_policy_and_explicit_transport_selection(self):
        for change in ({"role_policy": {"codex": {"orchestrator": {"enabled": False}}}},
                       {"role_policy": {"codex": {"orchestrator": {"qualification": "unmaintained"}}}}):
            with self.subTest(change=change):
                self.queue.task = replace(self.task, config=self.task.config | change)
                result = self.supervisor.admit(self.admission)
                self.assertEqual(result["status"]["reason"], "supervisor_unqualified")
        self.queue.task = self.task
        saved = copy.deepcopy(self.task.session)
        for member in saved["participants"]:
            if member["id"] == "devin":
                member["execution"]["transport"] = "devin_cli"
        self.queue.task = replace(self.task, session=saved)
        self.assertEqual(self.supervisor.admit(self.admission)["status"]["reason"], "policy_denied")

    def test_all_exact_bindings_fail_closed_at_handoff_and_reentry(self):
        claim = self.started()
        for field in claim["binding"]:
            bad = copy.deepcopy(claim)
            bad["binding"][field] = "different"
            for action in ("handoff", "status", "result", "cancel"):
                with self.subTest(field=field, action=action):
                    self.assertEqual(self.supervisor.operate(action, bad)["status"]["reason"], "binding_mismatch")
        for field in ("grant", "runner", "session", "generation", "token", "scope_digest", "expires_at"):
            bad = copy.deepcopy(claim)
            bad[field] = claim[field] + 1 if field == "expires_at" else "f" * 64 if field == "scope_digest" else "different"
            self.assertEqual(self.supervisor.operate("handoff", bad)["status"]["reason"], "binding_mismatch")

    def test_restart_expiry_revocation_and_new_run_never_replace_writer(self):
        claim = self.started()
        with mock.patch.object(self.provider, "create", wraps=self.provider.create) as create:
            self.agent.generation = "restarted"
            self.assertEqual(self.supervisor.operate("handoff", claim)["status"]["reason"], "supervisor_restarted")
            self.agent.generation = claim["generation"]
            self.now = claim["expires_at"]
            self.assertEqual(self.supervisor.operate("status", claim)["status"]["reason"], "claim_expired")
            self.now -= 1
            self.queue.active = False
            self.assertEqual(self.supervisor.operate("result", claim)["status"]["reason"], "claim_revoked")
            self.queue.active = True
            self.supervisor.revoke(claim)
            self.assertEqual(self.supervisor.operate("cancel", claim)["status"]["reason"], "claim_revoked")
            new = copy.deepcopy(self.admission)
            new["binding"]["run"] = "new_run"
            self.queue.task = replace(self.task, admission=new)
            self.assertEqual(self.supervisor.admit(new)["status"]["reason"], "claim_conflict")
            self.assertEqual(create.call_count, 0)

    def test_lease_replaced_or_expired_cannot_be_reused(self):
        claim = self.admitted()
        session_lease.release_lease(root=self.checkout, session_id="session_example")
        self.assertEqual(self.supervisor.operate("handoff", claim)["status"]["reason"], "lease_unavailable")
        session_lease.acquire_lease(root=self.checkout, repo=self.order.repository, orchestrator="codex",
            session_id="session_example", now=datetime.fromtimestamp(self.now + 1, timezone.utc))
        self.assertEqual(self.supervisor.operate("handoff", claim)["status"]["reason"], "binding_mismatch")

    def test_live_renewal_rechecks_reachability_without_new_writer_or_budget(self):
        claim = self.started()
        calls = len(self.agent.calls)
        self.now = claim["expires_at"] - 30
        with mock.patch.object(self.provider, "create", wraps=self.provider.create) as create:
            result = self.supervisor.operate("renew", claim)
            renewed = result["claim"]
            self.assertGreater(renewed["expires_at"], claim["expires_at"])
            self.assertLessEqual(renewed["expires_at"], self.admission["expires_at"])
            self.assertEqual(self.supervisor.operate("status", claim)["status"]["reason"], "binding_mismatch")
            self.assertEqual(self.supervisor.operate("handoff", renewed)["status"]["state"], "running")
            self.assertEqual(create.call_count, 0)
        self.assertEqual(len(self.agent.calls), calls + 1)
        self.now = renewed["expires_at"]
        self.assertEqual(self.supervisor.operate("renew", renewed)["status"]["reason"], "claim_expired")

    def test_authorization_rechecked_after_agent_before_create(self):
        claim = self.admitted()
        self.agent.after = lambda: setattr(self.queue, "active", False)
        with mock.patch.object(self.provider, "create", wraps=self.provider.create) as create:
            self.assertEqual(self.supervisor.operate("handoff", claim)["status"]["reason"], "claim_revoked")
            self.assertEqual(create.call_count, 0)

    def test_private_scope_and_provider_binding_cannot_change_after_admission(self):
        claim = self.admitted()
        self.queue.task = replace(self.task, order=replace(self.order, body="changed private scope"))
        self.assertEqual(self.supervisor.operate("handoff", claim)["status"]["reason"], "binding_mismatch")
        self.queue.task = self.task
        self.provider.account = "another-account"
        self.assertEqual(self.supervisor.operate("handoff", claim)["status"]["reason"], "binding_mismatch")

    def test_unknown_and_overspending_runtime_decisions_fail_before_create(self):
        for change in ({"builder_acu": 6}, {"generation": "wrong"}, {"decision": "launch_anything"},
                       {"merge_authority": True}, {"scope_digest": "f" * 64}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                self.supervisor.store = ContextStore(Path(tmp).resolve() / "supervisor")
                self.agent.change = change
                result = self.supervisor.admit(self.admission)
                self.assertIsNone(result["claim"])
                self.assertIn(result["status"]["state"], {"waiting", "rejected"})
        self.assertFalse((self.root / "provider").exists())


class LifecycleTests(SupervisorCase):
    def test_ambiguous_handoff_and_disconnect_do_not_retry_create(self):
        claim = self.admitted()
        original = self.provider.create
        def ambiguous(*args):
            original(*args)
            raise RuntimeError(CANARY)
        with mock.patch.object(self.provider, "create", side_effect=ambiguous) as create:
            first = self.supervisor.operate("handoff", claim)
            second = self.supervisor.operate("handoff", claim)
            status = self.supervisor.operate("status", claim)
            self.assertEqual(first["status"]["state"], "uncertain")
            self.assertEqual(second["status"]["reason"], "recovery_required")
            self.assertEqual(status["status"]["state"], "uncertain")
            self.assertEqual(create.call_count, 1)

    def test_process_crash_after_reservation_is_not_readiness(self):
        with mock.patch.object(self.agent, "decide", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.supervisor.admit(self.admission)
        # Duplicate receipt after process restart cannot forget the pending call.
        result = self.supervisor.admit(self.admission)
        self.assertIsNone(result["claim"])
        self.assertNotEqual(result["status"]["state"], "claimed")
        self.assertEqual(len(self.agent.calls), 0)

    def test_builder_completion_provider_exit_review_and_merge_are_separate(self):
        claim = self.started()
        self.complete()
        original = self.provider.get
        def still_running(binding):
            snapshot = original(binding)
            return Session(binding, snapshot.state, snapshot.reason, snapshot.structured_output, "running")
        with mock.patch.object(self.provider, "get", side_effect=still_running):
            result = self.result(claim)
            self.assertEqual(result["status"]["implementation"], "verified", result)
            self.assertEqual(result["status"]["writer"], "running")
            self.assertEqual(result["status"]["review"], "not_requested")
            self.assertEqual(self.reviews.requests, [])
        result = self.result(claim)
        self.assertEqual(result["status"]["state"], "reviewing", result)
        self.assertEqual(len(self.reviews.requests), 1)
        self.reviews.review = self.reviews.gate = "passed"
        result = self.result(claim)
        self.assertEqual(result["status"]["state"], "complete", result)
        self.assertEqual(result["target"]["head_sha"], HEAD)
        self.assertEqual(result["status"]["merge"], "open")
        self.assertFalse(result["status"]["merge_authority"])
        self.assertFalse(result["status"]["tracker_write"])

    def test_exit_without_verified_pr_does_not_complete(self):
        claim = self.started()
        self.provider.set_state(self.binding(), "terminated")
        result = self.result(claim)
        self.assertEqual(result["status"]["writer"], "terminated")
        self.assertEqual(result["status"]["implementation"], "pending")
        self.assertEqual(self.reviews.requests, [])

    def test_missing_or_contributing_reviewer_cannot_satisfy_review(self):
        claim = self.started()
        self.complete()
        self.reviews.available = False
        self.assertEqual(self.result(claim)["status"]["reason"], "review_unavailable")
        self.reviews.available = True
        self.reviews.contributors = ("devin", "claude")
        self.assertEqual(self.result(claim)["status"]["reason"], "review_unavailable")
        self.assertEqual(self.reviews.requests, [])

    def test_review_request_is_not_repeated_after_ambiguous_delivery(self):
        claim = self.started()
        self.complete()
        with mock.patch.object(self.reviews, "request", side_effect=RuntimeError(CANARY)) as request:
            self.assertEqual(self.result(claim)["status"]["state"], "uncertain")
            self.assertEqual(self.result(claim)["status"]["reason"], "recovery_required")
            self.assertEqual(request.call_count, 1)

    def test_changed_head_during_completion_decision_invalidates_review(self):
        claim = self.started()
        self.complete()
        self.result(claim)
        self.reviews.review = self.reviews.gate = "passed"
        def change_head():
            self.github.pr = replace(self.github.pr, head_sha="b" * 40)
        self.agent.after = change_head
        result = self.result(claim)
        self.assertNotEqual(result["status"]["state"], "complete")
        self.assertEqual(len(self.reviews.requests), 1)

    def test_changed_head_during_review_routing_decision_prevents_audit_dispatch(self):
        claim = self.started()
        self.complete()
        self.agent.after = lambda: setattr(self.github, "pr", replace(self.github.pr, head_sha="b" * 40))
        self.assertNotEqual(self.result(claim)["status"]["state"], "reviewing")
        self.assertEqual(self.reviews.requests, [])

    def test_revocation_during_final_review_observation_invalidates_completion(self):
        claim = self.started()
        self.complete()
        self.result(claim)
        self.reviews.review = self.reviews.gate = "passed"
        self.agent.after = lambda: setattr(self.reviews, "after_observe", lambda: setattr(self.queue, "active", False))
        self.assertEqual(self.result(claim)["status"]["reason"], "claim_revoked")

    def test_gate_change_during_completion_decision_does_not_complete(self):
        claim = self.started()
        self.complete()
        self.result(claim)
        self.reviews.review = self.reviews.gate = "passed"
        self.agent.after = lambda: setattr(self.reviews, "gate", "failed")
        self.assertNotEqual(self.result(claim)["status"]["state"], "complete")

    def test_cancellation_before_and_after_handoff_is_idempotent_and_no_new_writer(self):
        claim = self.admitted()
        self.assertEqual(self.supervisor.operate("cancel", claim)["status"]["state"], "cancelled")
        self.supervisor.operate("handoff", claim)
        self.assertFalse((self.root / "provider").exists())
        # A separate synthetic authorized work reservation exercises active cancel.
        self.supervisor.store = ContextStore(self.root / "another-supervisor")
        claim = self.started()
        with mock.patch.object(self.provider, "cancel", wraps=self.provider.cancel) as cancel:
            self.assertEqual(self.supervisor.operate("cancel", claim)["status"]["state"], "cancelled")
            self.supervisor.operate("cancel", claim)
            self.supervisor.operate("handoff", claim)
            self.assertEqual(cancel.call_count, 1)

    def test_cancel_acceptance_is_not_exit_and_ambiguous_cancel_is_not_retried(self):
        claim = self.started()
        with mock.patch.object(self.provider, "cancel", return_value=Session("ignored", "running")) as cancel:
            result = self.supervisor.operate("cancel", claim)
            self.assertEqual(result["status"]["reason"], "cancel_pending")
            self.assertNotEqual(result["status"]["state"], "cancelled")
            self.supervisor.operate("cancel", claim)
            self.assertEqual(cancel.call_count, 1)
        self.provider.set_state(self.binding(), "terminated")
        self.assertEqual(self.supervisor.operate("status", claim)["status"]["state"], "cancelled")

    def test_ambiguous_cancel_requires_recovery_even_if_provider_exited(self):
        claim = self.started()
        def ambiguous(binding):
            self.provider.set_state(binding, "terminated")
            raise RuntimeError(CANARY)
        with mock.patch.object(self.provider, "cancel", side_effect=ambiguous) as cancel:
            self.supervisor.operate("cancel", claim)
            result = self.supervisor.operate("cancel", claim)
            self.assertEqual(result["status"]["state"], "uncertain")
            self.assertEqual(cancel.call_count, 1)

    def test_runtime_calls_are_bounded_and_cancel_remains_possible_at_cap(self):
        self.admission["limits"]["runtime_calls"] = 2
        self.queue.task = replace(self.task, admission=copy.deepcopy(self.admission))
        claim = self.started()
        self.complete()
        self.assertEqual(self.result(claim)["status"]["reason"], "budget_exhausted")
        self.assertEqual(self.supervisor.operate("cancel", claim)["status"]["state"], "cancelled")
        self.assertEqual(len(self.agent.calls), 2)

    def test_disconnected_decision_can_be_cancelled_without_retrying_the_agent(self):
        claim = self.started()
        self.complete()
        with mock.patch.object(self.agent, "decide", side_effect=RuntimeError(CANARY)) as agent:
            self.assertEqual(self.result(claim)["status"]["reason"], "supervisor_unavailable")
            self.assertEqual(self.supervisor.operate("cancel", claim)["status"]["state"], "cancelled")
            self.assertEqual(agent.call_count, 1)

    def test_cancellation_stops_the_exact_delegated_review_once(self):
        claim = self.started()
        self.complete()
        self.result(claim)
        result = self.supervisor.operate("cancel", claim)
        self.assertEqual(result["status"]["state"], "cancelled")
        self.assertEqual(result["status"]["review_writer"], "terminated")
        self.supervisor.operate("cancel", claim)
        self.assertEqual(self.reviews.cancellations, self.reviews.requests)

    def test_cancellation_waits_for_review_exit_and_never_repeats_ambiguous_cancel(self):
        claim = self.started()
        self.complete()
        self.result(claim)
        with mock.patch.object(self.reviews, "cancel", return_value="running") as cancel:
            self.assertEqual(self.supervisor.operate("cancel", claim)["status"]["reason"], "cancel_pending")
            self.supervisor.operate("cancel", claim)
            self.assertEqual(cancel.call_count, 1)
        self.reviews.writer = "terminated"
        self.assertEqual(self.supervisor.operate("status", claim)["status"]["state"], "cancelled")

    def test_passing_review_with_live_review_runtime_is_not_completion(self):
        claim = self.started()
        self.complete()
        self.result(claim)
        self.reviews.review = self.reviews.gate = "passed"
        self.reviews.writer = "running"
        self.assertNotEqual(self.result(claim)["status"]["state"], "complete")

    def test_duplicate_receipts_cannot_republish_stale_completion(self):
        claim = self.started()
        self.complete()
        self.result(claim)
        self.reviews.review = self.reviews.gate = "passed"
        self.assertEqual(self.result(claim)["status"]["state"], "complete")
        self.github.pr = replace(self.github.pr, head_sha="b" * 40)
        for result in (self.supervisor.admit(self.admission), self.supervisor.operate("handoff", claim)):
            self.assertNotEqual(result["status"]["state"], "complete")
            self.assertIsNone(result["target"])


class CodexRuntimeTests(SupervisorCase):
    def runtime(self, code):
        executable = self.root / "codex"
        executable.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -c " + shlex.quote(code) + ' "$@"\n')
        executable.chmod(0o700)
        return CodexRuntime(executable=executable, private_root=self.root / "runtime", model="synthetic",
                            environment={"PATH": os.defpath})

    def test_real_local_process_path_drives_synthetic_builder_and_review_interface(self):
        # This executable is an offline CLI fixture, not a claim of live hosted
        # Codex/Devin qualification or completion. It executes the production argv,
        # stdin, schema, process supervisor, private files and cleanup path.
        code = '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
assert args[0] == 'exec' and args[-1] == '-'
assert args[args.index('--sandbox') + 1] == 'read-only'
assert '--ignore-user-config' in args and '--ephemeral' in args
assert 'cli_auth_credentials_store="keyring"' in args
assert args[args.index('--enable') + 1] == 'secret_auth_storage'
assert 'shell_tool' in args and 'apps' in args and 'plugins' in args
payload = json.loads(sys.stdin.read().split('\\n', 1)[1])
r = payload['request']
s = r['status']
action = {'admit': 'accept', 'renew': 'accept', 'handoff': 'dispatch'}.get(r['phase'])
if action is None:
    action = 'review' if s['review'] == 'not_requested' else 'complete'
reply = dict(schema='code_mower.supervisor_decision.v1', binding=r['binding'],
    generation=r['generation'], scope_digest=r['scope_digest'], decision=action,
    builder_acu=5, reviewer='claude')
schema = json.loads(Path(args[args.index('--output-schema') + 1]).read_text())
assert set(reply) == set(schema['required'])
assert set(schema['$defs']) == {'binding'}
assert schema['properties']['schema']['type'] == 'string'
assert schema['properties']['decision']['type'] == 'string'
assert schema['properties']['reviewer']['type'] == 'string'
for name in ('input', 'schema', 'result', 'log'):
    assert os.stat(name).st_mode & 0o077 == 0
Path(args[args.index('--output-last-message') + 1]).write_text(json.dumps(reply))
'''
        self.supervisor.runtime = self.runtime(code)
        claim = self.started()
        self.complete()
        self.assertEqual(self.result(claim)["status"]["state"], "reviewing")
        self.reviews.review = self.reviews.gate = "passed"
        self.assertEqual(self.result(claim)["status"]["state"], "complete")
        self.assertEqual(len(self.reviews.requests), 1)
        self.assertEqual(list((self.root / "runtime").glob("decision-*")), [])

    def test_explicit_file_credential_store_survives_ignored_user_config(self):
        code = '''import sys
args = sys.argv[1:]
assert '--ignore-user-config' in args
assert 'cli_auth_credentials_store="file"' in args
assert 'secret_auth_storage' not in args
raise SystemExit(1)
'''
        runtime = self.runtime(code)
        runtime.credential_store = "file"
        with self.assertRaisesRegex(SupervisorError, "supervisor_unavailable"):
            runtime.decide(self.task, {}, timeout=1)

    def test_unknown_credential_store_is_refused(self):
        executable = self.root / "codex"
        executable.write_text("#!/bin/sh\nexit 1\n")
        executable.chmod(0o700)
        with self.assertRaisesRegex(SupervisorError, "supervisor_unavailable"):
            CodexRuntime(executable=executable, private_root=self.root / "runtime",
                         model="synthetic", credential_store="auto")

    def test_timeout_bad_response_and_process_error_fail_closed_and_clean_up(self):
        for code in ("import time; time.sleep(5)", "print('private output')", "raise SystemExit(1)"):
            with self.subTest(code=code):
                runtime = self.runtime(code)
                with self.assertRaisesRegex(SupervisorError, "supervisor_unavailable"):
                    runtime.decide(self.task, {}, timeout=1)
                self.assertEqual(list((self.root / "runtime").glob("decision-*")), [])

    def test_runtime_state_must_be_private_and_outside_git(self):
        runtime = self.runtime("raise SystemExit(1)")
        runtime.root = self.checkout / "runtime"
        with self.assertRaises(SupervisorError):
            runtime.decide(self.task, {}, timeout=1)
        self.assertFalse(runtime.root.exists())


class ContractTests(unittest.TestCase):
    def test_materialized_package_contains_runtime_contract_and_consumer_fixtures(self):
        from code_mower.package_manifest import PACKAGE_FILES
        targets = {target for _, target, _ in PACKAGE_FILES}
        for target in ("supervisor.py", "supervisor_codex.py", "supervisor_contract.py",
                       "supervisor_contract.schema.json", "supervisor_contract.fixtures.json"):
            self.assertIn("src/code_mower/" + target, targets)
        self.assertIn("docs/supervisor-contract.md", targets)

    def test_lifecycle_schema_matches_existing_contract_exactly(self):
        remote = json.loads((ROOT / "src/code_mower/remote_session.schema.json").read_text())
        self.assertEqual(schema()["$defs"]["lifecycle"], remote["$defs"]["metadata"])

    def test_portable_packaged_fixtures_and_closed_projection(self):
        fixtures = json.loads((ROOT / "src/code_mower/supervisor_contract.fixtures.json").read_text())
        for kind, value in fixtures["valid"].items():
            with self.subTest(kind=kind):
                self.assertEqual(validate(kind, value), value)
                self.assertEqual(decode(kind, json.dumps(value).encode()), value)
                for changed in (value | {"unexpected": CANARY}, value | {"schema": "unknown.v99"}):
                    with self.assertRaises(SupervisorError):
                        validate(kind, changed)
        public = public_status(fixtures["valid"]["result"])
        for private in ("tenant_example", "repo_binding_example", "run_example", "claim_example", HEAD):
            self.assertNotIn(private, json.dumps(public))
        for raw in (b'{"schema":1,"schema":2}', b'[]', b'{"x":NaN}', b' ' * 65537,
                    b'[' * 2000 + b']' * 2000):
            with self.assertRaises(SupervisorError):
                decode("admission", raw)

    def test_completion_and_cancellation_invariants_are_semantic(self):
        fixtures = json.loads((ROOT / "src/code_mower/supervisor_contract.fixtures.json").read_text())
        status = fixtures["valid"]["result"]["status"]
        with self.assertRaises(SupervisorError):
            validate("status", status | dict(state="complete"))
        with self.assertRaises(SupervisorError):
            validate("status", status | dict(state="cancelled", writer="running"))


if __name__ == "__main__":
    unittest.main()
