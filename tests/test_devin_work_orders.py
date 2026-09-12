"""Offline trusted-work-order delivery and hostile builder evidence fixtures."""
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower.devin_sessions import DevinClient
from code_mower.devin_work_orders import (
    COMPLETION_SCHEMA, Candidates, DevinWorkOrders, PullRequest, WorkOrder,
)
from code_mower.remote_session import FakeProvider, RemoteError, RemoteSessions, _key
from code_mower.work_orders import WORK_ORDER_SCHEMA

CANARY = "PRIVATE_PROSE_SOURCE_DIFF_CREDENTIAL_RESULT"
HEAD = "a" * 40


class GitHubFixture:
    def __init__(self):
        self.pr = PullRequest("owner/repo", 42, (("owner/repo", 907),), 123,
                              "builder[bot]", "owner/repo", "devin/907", HEAD, "main")
        self.page = None
        self.reads = []
        self.calls = []

    def candidates(self, repository, branch, *, limit):
        self.calls.append((repository, branch, limit))
        return self.page or Candidates((self.pr,), True)

    def read(self, repository, number):
        self.calls.append((repository, number))
        return self.reads.pop(0) if self.reads else self.pr


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.provider = FakeProvider(self.root / "provider")
        self.remote = RemoteSessions(self.root / "remote", self.provider)
        self.github = GitHubFixture()
        self.service = DevinWorkOrders(self.root / "builder", self.remote, self.github)
        self.order = WorkOrder.from_manifest(
            {"schema": WORK_ORDER_SCHEMA, "repo": "owner/repo",
             "source": {"repo": "owner/repo", "issue_number": "907"},
             "output_path": CANARY, "context_manifest": CANARY}, CANARY,
            repository="owner/repo", issue=907, branch="devin/907", base="main",
            author_id=123, author_login="builder[bot]", acu_limit=5)
        self.key = self.service._key(self.order)

    def run_order(self, command, **kwargs):
        return self.service.run(command, self.order, apply=True, **kwargs)

    def binding(self):
        with self.remote.store.locked(_key(self.key)) as locked:
            return locked.read()["binding"]

    def claim(self, **kwargs):
        return {"schema": COMPLETION_SCHEMA, "round": 0, "repository": "owner/repo",
                "issue": 907, "pr_number": 42, "head_sha": HEAD, **kwargs}

    def complete(self, **kwargs):
        self.provider.set_state(self.binding(), "complete", result=self.claim(**kwargs))

    def test_preview_and_manifest_binding(self):
        self.assertTrue(self.service.run("dispatch", self.order)["apply_required"])
        self.assertFalse((self.root / "builder").exists())
        self.assertFalse((self.root / "remote").exists())
        self.assertNotIn(CANARY, repr(self.order))
        with self.assertRaisesRegex(RemoteError, "work_order_binding"):
            WorkOrder.from_manifest({}, CANARY, repository="owner/repo", issue=907,
                                    branch="devin/907", base="main", author_id=123,
                                    author_login="builder[bot]")
        for changes in ({"branch": "main"}, {"branch": "bad/../ref"}, {"acu_limit": True},
                        {"issue": True}, {"author_login": CANARY + "\n"}):
            with self.subTest(changes=changes), self.assertRaises(RemoteError):
                replace(self.order, **changes)

    def test_delivery_progress_clarification_completion_fix_cancel_privacy(self):
        with patch.object(self.provider, "create", wraps=self.provider.create) as create:
            self.assertEqual(self.run_order("dispatch")["session"]["state"], "running")
            prompt, repo, cap, _ = create.call_args.args
            self.assertIn(CANARY, prompt)
            self.assertIn(COMPLETION_SCHEMA, prompt)
            self.assertEqual((repo, cap), ("owner/repo", 5))
            self.run_order("dispatch")
            self.assertEqual(create.call_count, 1)
        outputs = []
        for reason, state in (("waiting_for_owner", "waiting_for_user"),
                              ("approval_required", "waiting_for_approval")):
            self.provider.set_state(self.binding(), "owner_action", reason=reason)
            outputs.append(self.run_order("status"))
            self.assertEqual(outputs[-1]["session"]["state"], state)
        outputs.append(self.run_order("clarify", request="clarification", prose=CANARY))
        self.complete(round=1)
        with patch.object(self.remote, "observed_acu", return_value=1.25):
            evidence = self.run_order("collect")
        outputs.append(evidence)
        self.assertEqual(evidence["verified_pr"]["head_sha"], HEAD)
        self.assertEqual(evidence["observed_acu"], 1.25)
        self.assertEqual(len(self.github.calls), 3)
        self.assertFalse(evidence["merge_authority"])
        outputs.append(self.run_order("fix", request="fix-1", prose=CANARY, reviewed_head=HEAD))
        self.assertIsNone(self.remote.private_result(self.key))
        self.assertIsNone(outputs[-1]["verified_pr"])
        self.assertEqual(self.run_order("fix", request="fix-1", prose=CANARY,
                                        reviewed_head=HEAD)["round"], 2)
        self.github.pr = replace(self.github.pr, head_sha="b" * 40)
        self.complete(round=2, head_sha="b" * 40)
        outputs.append(self.run_order("collect"))
        self.assertEqual(outputs[-1]["verified_pr"]["head_sha"], "b" * 40)
        outputs.append(self.run_order("cancel", request="cancel-1"))
        self.assertEqual(outputs[-1]["session"]["state"], "terminated")
        self.assertEqual(self.run_order("cancel", request="cancel-1")["session"]["counts"]["cancel"], 1)
        serialized = json.dumps(outputs)
        for private in (CANARY, str(self.root), "structured_output", "linked_issues", "builder[bot]"):
            self.assertNotIn(private, serialized)
        for path in self.root.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_hostile_github_claims_fail_closed(self):
        mutations = [
            {"repository": "other/repo"}, {"linked_issues": (("owner/repo", 908),)},
            {"linked_issues": (("other/repo", 907),)}, {"linked_issues": ()},
            {"linked_issues": (("owner/repo", 907), ("owner/repo", 908))},
            {"author_id": 456}, {"author_login": "imposter"}, {"head_branch": "other"},
            {"head_repository": "fork/repo"}, {"head_sha": "b" * 40},
            {"base_branch": "other"}, {"number": 43}, {"state": "closed"},
        ]
        self.run_order("dispatch")
        self.complete()
        original = self.github.pr
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.github.pr = replace(original, **mutation)
                with self.assertRaisesRegex(RemoteError, "pull_request_binding"):
                    self.run_order("collect")
        self.github.pr = original
        for page in (Candidates((), True), Candidates((original, original), True),
                     Candidates((original,), False)):
            self.github.page = page
            with self.assertRaisesRegex(RemoteError, "ambiguous"):
                self.run_order("collect")
        self.github.page = None
        self.github.reads = [original, replace(original, head_sha="b" * 40)]
        with self.assertRaisesRegex(RemoteError, "pull_request_binding"):
            self.run_order("collect")
        self.assertIsNone(self.run_order("status")["verified_pr"])

    def test_completion_validation_and_private_adapter_failures(self):
        self.run_order("dispatch")
        self.complete()
        for claim in (None, {"raw": CANARY}, self.claim(issue=True), self.claim(round=1),
                      self.claim(repository="other/repo"), self.claim(head_sha=CANARY),
                      self.claim(extra=CANARY), self.claim(pr_number=True)):
            with patch.object(self.remote, "private_result", return_value=claim):
                with self.assertRaisesRegex(RemoteError, "invalid_completion"):
                    self.run_order("collect")
        with patch.object(self.github, "read", side_effect=RuntimeError(CANARY)):
            with self.assertRaisesRegex(RemoteError, "^github_unavailable$"):
                self.run_order("collect")
        with patch.object(self.remote, "observed_acu", return_value=float("nan")):
            with self.assertRaisesRegex(RemoteError, "invalid_usage"):
                self.run_order("collect")

    def test_binding_and_exact_head_fix_rules(self):
        self.run_order("dispatch")
        for mutation in ({"body": "changed"}, {"branch": "devin/other"}, {"acu_limit": 6},
                         {"author_id": 456}):
            with self.assertRaisesRegex(RemoteError, "work_order_binding"):
                self.service.run("dispatch", replace(self.order, **mutation), apply=True)
        with self.assertRaisesRegex(RemoteError, "stale_review_head"):
            self.run_order("fix", request="fix", prose=CANARY, reviewed_head=HEAD)
        self.complete()
        self.run_order("collect")
        with self.assertRaisesRegex(RemoteError, "fix_requires"):
            self.run_order("clarify", request="bypass", prose=CANARY)
        self.github.pr = replace(self.github.pr, head_sha="b" * 40)
        with self.assertRaisesRegex(RemoteError, "pull_request_binding"):
            self.run_order("fix", request="fix", prose=CANARY, reviewed_head=HEAD)
        self.assertIsNone(self.run_order("status")["verified_pr"])

    def test_lost_create_reconciles_without_paid_duplicate_and_schema(self):
        calls, tags, matches = [], [], []
        def runner(method, url, body, headers):
            calls.append((method, url))
            if method == "POST":
                self.assertEqual(body["repos"], ["owner/repo"])
                self.assertEqual(body["max_acu_limit"], 5)
                self.assertTrue(body["structured_output_required"])
                self.assertFalse(body["structured_output_schema"]["additionalProperties"])
                tags[:] = body["tags"]
                raise TimeoutError(CANARY)
            if "?" in url:
                return {"items": [{"session_id": sid, "status": "running", "tags": tags}
                                  for sid in matches], "has_next_page": False}
            return {"session_id": "devin-one", "status": "running"}
        client = DevinClient("org-example", "test-key", api_runner=runner)
        self.service = DevinWorkOrders.hosted(self.root / "hosted", client, self.github)
        with self.assertRaisesRegex(RemoteError, "reconcile_dispatch"):
            self.run_order("dispatch")
        self.assertEqual(self.run_order("dispatch")["session"]["state"], "uncertain")
        matches[:] = ["devin-one", "devin-two"]
        self.assertEqual(self.run_order("status")["session"]["next_action"], "inspect_provider")
        matches[:] = ["devin-one"]
        self.service = DevinWorkOrders.hosted(self.root / "hosted", client, self.github)
        self.assertEqual(self.run_order("status")["session"]["state"], "running")
        self.assertEqual(sum(m == "POST" for m, _ in calls), 1)
        self.assertRegex(tags[-1], r"^cm-[0-9a-f]{32}$")

    def test_branch_single_writer_and_lost_message_acknowledgement(self):
        self.run_order("dispatch")
        with self.assertRaisesRegex(RemoteError, "branch_writer_conflict"):
            self.service.run("dispatch", replace(self.order, issue=908), apply=True)
        original = self.provider.message
        def lost(binding, prose):
            original(binding, prose)
            raise TimeoutError(CANARY)
        with patch.object(self.provider, "message", side_effect=lost) as message:
            with self.assertRaisesRegex(RemoteError, "provider_unavailable"):
                self.run_order("clarify", request="m1", prose=CANARY)
            with self.assertRaisesRegex(RemoteError, "inspect_provider"):
                self.run_order("clarify", request="m2", prose=CANARY)
            with self.assertRaisesRegex(RemoteError, "inspect_provider"):
                self.run_order("clarify", request="m1", prose=CANARY)
            result = self.run_order("clarify", request="m1", prose=CANARY,
                                    acknowledge_delivered=True)
            self.assertEqual(result["round"], 1)
            self.assertEqual(message.call_count, 1)
        self.complete()  # Provider replays a completion from before the message.
        with self.assertRaisesRegex(RemoteError, "invalid_completion"):
            self.run_order("collect")

    def test_not_ready_and_missing_results_have_no_evidence(self):
        self.run_order("dispatch")
        self.assertIsNone(self.run_order("collect")["verified_pr"])
        self.provider.set_state(self.binding(), "complete")
        result = self.run_order("collect")
        self.assertIsNone(result["verified_pr"])
        self.assertEqual(result["session"]["reason"], "result_unavailable")
        self.assertFalse(self.github.calls)

    def test_hosted_completion_usage_fix_and_cancellation(self):
        status = {"status": "running"}
        calls = []
        def runner(method, url, body, headers):
            calls.append((method, url))
            if "/consumption/" in url:
                return {"total_acus": 3.25, "private": CANARY}
            if method == "DELETE":
                status.clear()
                status["status"] = "exit"
                return {"session_id": "devin-one", **status}
            if url.endswith("/messages"):
                status.clear()
                status["status"] = "running"
            return {"session_id": "devin-one", **status}
        client = DevinClient("org-example", "test-key", api_runner=runner)
        self.service = DevinWorkOrders.hosted(self.root / "hosted", client, self.github)
        self.run_order("dispatch")
        status.update(status="exit", status_detail="finished", structured_output=self.claim())
        result = self.run_order("collect")
        self.assertEqual(result["observed_acu"], 3.25)
        self.assertEqual(result["transport"], "devin_api_v3")
        self.assertEqual(result["verified_pr"]["pr_number"], 42)
        self.run_order("fix", request="fix-1", prose=CANARY, reviewed_head=HEAD)
        self.github.pr = replace(self.github.pr, head_sha="b" * 40)
        status.update(status="exit", status_detail="finished",
                      structured_output=self.claim(round=1, head_sha="b" * 40))
        self.assertEqual(self.run_order("collect")["verified_pr"]["head_sha"], "b" * 40)
        self.assertEqual(self.run_order("cancel", request="c1")["session"]["state"], "terminated")
        self.assertEqual(sum(method == "POST" and url.endswith("/sessions")
                             for method, url in calls), 1)

    def test_usage_api_is_bounded_and_not_completion_content(self):
        calls = []
        def runner(method, url, body, headers):
            calls.append((method, url))
            return {"total_acus": 2.5, "private": CANARY}
        client = DevinClient("org-example", "test-key", api_runner=runner)
        self.assertEqual(client.session_acu("devin-one"), 2.5)
        self.assertTrue(calls[0][1].endswith("/consumption/daily/sessions/devin-one"))


if __name__ == "__main__":
    unittest.main()
