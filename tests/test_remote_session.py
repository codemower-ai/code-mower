"""Offline lifecycle, transport parity, process death, and privacy regression tests."""
import contextlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower.context_contract import ContextError
from code_mower.devin_sessions import DevinClient
from code_mower.remote_session import (
    DevinProvider, FakeProvider, RemoteError, RemoteSessions, _key, public_projection,
)
from code_mower.session import main


class CrashFake(FakeProvider):
    def create(self, prompt, repo, limit, checkpoint):
        super().create(prompt, repo, limit, checkpoint)
        os._exit(23)


class CancelCrashFake(FakeProvider):
    def cancel(self, binding):
        super().cancel(binding)
        os._exit(25)


class MessageCrashFake(FakeProvider):
    def message(self, binding, prose):
        super().message(binding, prose)
        os._exit(24)


def worker(root, crash=False, message=False, cancel=False):
    root = Path(root)
    cls = CancelCrashFake if cancel else MessageCrashFake if message else CrashFake if crash else FakeProvider
    service = RemoteSessions(root, cls(root / "fake-provider"))
    if cancel:
        service.run("cancel", "work", request="c1", apply=True)
    elif message:
        service.run("message", "work", request="m1", prose="private", apply=True)
    else:
        service.run("dispatch", "work", prose="private", repo="owner/repo", apply=True)


def ordinary_mutation(root, command):
    root = Path(root)
    service = RemoteSessions(root, FakeProvider(root / "fake-provider"))
    service.run(command, "work", request="same-key", prose="private", apply=True)


class RemoteSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "state"
        self.provider = FakeProvider(self.root / "fake-provider")
        self.service = RemoteSessions(self.root, self.provider)

    def dispatch(self, **kwargs):
        return self.service.run("dispatch", "work", prose="private", repo="owner/repo",
                                apply=True, **kwargs)

    def record(self):
        with self.service.store.locked(_key("work")) as locked:
            return locked.read()

    def test_lifecycle_idempotency_and_private_results(self):
        self.assertEqual(self.dispatch()["counts"]["dispatch"], 1)
        self.dispatch()
        self.service.run("message", "work", request="m1", prose="secret prose", apply=True)
        out = self.service.run("message", "work", request="m1", prose="secret prose", apply=True)
        self.assertEqual(out["counts"]["message"], 1)
        with self.assertRaisesRegex(RemoteError, "request_conflict"):
            self.service.run("message", "work", request="m1", prose="changed", apply=True)
        binding = self.record()["binding"]
        self.provider.set_state(binding, "complete", result={"private": "source and diff"})
        out = self.service.run("collect", "work", apply=True)
        self.assertNotIn("source", json.dumps(out))
        self.assertEqual(self.service.private_result("work"), {"private": "source and diff"})
        self.assertEqual(self.service.run("collect", "work", apply=True)["counts"]["collect"], 1)
        self.service.run("cancel", "work", request="c1", apply=True)
        self.assertEqual(self.service.run("cancel", "work", request="c1", apply=True)["counts"]["cancel"], 1)
        for path in self.root.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_results_do_not_collide(self):
        self.dispatch()
        self.provider.set_state(self.record()["binding"], "complete", result={"one": 1})
        self.service.run("collect", "work", apply=True)
        self.service.run("dispatch", "other", prose="another", repo="owner/repo", apply=True)
        self.assertIsNone(self.service.private_result("other"))
        self.assertEqual(self.service.private_result("work"), {"one": 1})

    def test_waiting_states(self):
        self.dispatch()
        for reason, state in (("approval_required", "waiting_for_approval"),
                              ("waiting_for_owner", "waiting_for_user")):
            self.provider.set_state(self.record()["binding"], "owner_action", reason=reason)
            self.assertEqual(self.service.run("status", "work")["state"], state)

    def test_structured_result_completes_waiting_session_but_not_approval(self):
        self.dispatch()
        binding = self.record()["binding"]
        result = {"schema": "code_mower.builderCompletion.v1", "round": 10}
        self.provider.set_state(
            binding, "owner_action", reason="approval_required", result=result,
        )
        self.assertEqual(self.service.run("collect", "work", apply=True)["state"],
                         "waiting_for_approval")
        self.assertIsNone(self.service.private_result("work"))

        self.provider.set_state(binding, "owner_action", reason="waiting_for_owner", result=result)
        self.assertEqual(self.service.run("collect", "work", apply=True)["state"], "complete")
        self.assertEqual(self.service.private_result("work"), result)

        self.provider.set_state(binding, "terminated", result=result)
        self.assertEqual(self.service.run("status", "work")["state"], "terminated")
        self.assertIsNone(self.service.private_result("work"))

    def test_preview_has_no_io(self):
        self.assertEqual(self.service.run("dispatch", "work", prose="secret", repo="owner/repo")["mode"], "dry_run")
        self.assertFalse(self.root.exists())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(["dispatch", "work", "--repo", "owner/repo", "--provider", "devin",
                         "--input-file", "missing-secret-file", "--remote-state-dir", str(self.root)])
        self.assertEqual(code, 0)
        self.assertNotIn("secret", out.getvalue())
        self.assertFalse(self.root.exists())

    def test_process_crash_reconciliation_and_concurrency(self):
        ctx = multiprocessing.get_context("spawn")
        process = ctx.Process(target=worker, args=(str(self.root), True))
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 23)
        self.assertIsNone(self.record()["binding"])
        self.assertEqual(self.service.run("status", "work")["counts"]["dispatch"], 1)
        processes = [ctx.Process(target=worker, args=(str(self.root),)) for _ in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(len(list((self.root / "fake-provider").glob("*.json"))), 1)

    def test_concurrent_fresh_dispatch_and_duplicate_mutations(self):
        ctx = multiprocessing.get_context("spawn")
        groups = [[ctx.Process(target=worker, args=(str(self.root),)) for _ in range(4)]]
        for command in ("message", "cancel"):
            groups.append([ctx.Process(target=ordinary_mutation, args=(str(self.root), command))
                           for _ in range(4)])
        for processes in groups:
            for process in processes:
                process.start()
            for process in processes:
                process.join(15)
            self.assertEqual([process.exitcode for process in processes], [0] * len(processes))
        self.assertEqual(len(list((self.root / "fake-provider").glob("*.json"))), 1)
        self.assertEqual(self.record()["counts"],
                         {"dispatch": 1, "message": 1, "cancel": 1, "collect": 0})

    def test_message_crash_requires_acknowledgement(self):
        self.dispatch()
        process = multiprocessing.get_context("spawn").Process(
            target=worker, args=(str(self.root), False, True))
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 24)
        self.assertEqual(self.service.run("status", "work")["state"], "uncertain")
        for request in ("m1", "new-key"):
            with self.assertRaisesRegex(RemoteError, "inspect_provider_then_acknowledge"):
                self.service.run("message", "work", request=request, prose="private", apply=True)
        out = self.service.run("message", "work", request="m1", apply=True,
                               acknowledge_delivered=True)
        self.assertEqual(out["counts"]["message"], 1)

    def test_uncertain_create_never_repeated_even_without_checkpoint(self):
        with patch.object(self.provider, "create", side_effect=RuntimeError("private prompt")) as create:
            with self.assertRaisesRegex(RemoteError, "reconcile_dispatch"):
                self.dispatch()
            self.assertEqual(self.dispatch()["next_action"], "inspect_provider")
            self.assertEqual(create.call_count, 1)

    def test_closed_projection_and_adapter_errors(self):
        out = public_projection({"state": "running", "messages": ["secret"], "repo": "private"})
        self.assertNotIn("secret", json.dumps(out))
        with self.assertRaises(RemoteError):
            public_projection({"state": "secret"})
        self.dispatch()
        with patch.object(self.provider, "get", side_effect=RemoteError("secret credentials")):
            with self.assertRaises(RemoteError) as error:
                self.service.run("status", "work")
            self.assertNotIn("secret", str(error.exception))

    def test_unsafe_state_rejected(self):
        self.root.mkdir(mode=0o755)
        with self.assertRaises(ContextError):
            self.dispatch()
        self.root.chmod(0o700)
        (self.root / ".git").mkdir()
        with self.assertRaises(ContextError):
            self.dispatch()

    def test_cancel_crash_does_not_replay(self):
        self.dispatch()
        process = multiprocessing.get_context("spawn").Process(
            target=worker, args=(str(self.root), False, False, True))
        process.start()
        process.join(15)
        self.assertEqual(process.exitcode, 25)
        with patch.object(self.provider, "cancel") as cancel:
            with self.assertRaisesRegex(RemoteError, "inspect_provider_then_acknowledge"):
                self.service.run("cancel", "work", request="c1", apply=True)
            out = self.service.run("cancel", "work", request="c1", apply=True,
                                   acknowledge_delivered=True)
            self.assertEqual(out["state"], "terminated")
            cancel.assert_not_called()

    def test_devin_lost_create_response_recovers_only_unique_match(self):
        tag = ""
        matches = []
        calls = []
        def runner(method, url, body, headers):
            nonlocal tag
            calls.append(method)
            if method == "POST":
                tag = body["tags"][-1]
                raise TimeoutError("private output")
            if "?" in url:
                return {"items": [{"session_id": sid, "status": "running", "tags": [tag]}
                                  for sid in matches], "has_next_page": False}
            return {"session_id": "remote-1", "status": "running"}
        provider = DevinProvider(DevinClient("org-example", "test-key", api_runner=runner))
        self.service = RemoteSessions(self.root, provider)
        with self.assertRaisesRegex(RemoteError, "reconcile_dispatch"):
            self.dispatch()
        self.assertEqual(self.dispatch()["next_action"], "inspect_provider")
        matches[:] = ["remote-1", "remote-2"]
        self.assertEqual(self.service.run("status", "work")["next_action"], "inspect_provider")
        matches[:] = ["remote-1"]
        restarted = RemoteSessions(self.root, provider)
        self.assertEqual(restarted.run("status", "work")["state"], "running")
        self.assertEqual(calls.count("POST"), 1)

    def test_cli_metadata_and_package_schema(self):
        input_file = Path(self.tmp.name) / "input"
        input_file.write_text("PROSE_CANARY private prompt and source")
        common = ["work", "--remote-state-dir", str(self.root)]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["dispatch", *common, "--repo", "owner/repo",
                                   "--input-file", str(input_file), "--apply"]), 0)
            self.assertEqual(main(["status", *common]), 0)
        self.assertNotIn("PROSE_CANARY", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        schema = json.loads((Path(__file__).resolve().parents[1] / "src" / "code_mower" /
                             "remote_session.schema.json").read_text())
        schema = schema["$defs"]["metadata"]
        for line in output.getvalue().splitlines():
            value = json.loads(line)
            self.assertEqual(set(value), set(schema["required"]))
            self.assertIn(value["state"], schema["properties"]["state"]["enum"])
        with patch.object(self.provider, "account", "different-account"):
            with self.assertRaisesRegex(RemoteError, "binding_mismatch"):
                self.service.run("status", "work")

    def test_devin_adapter_parity_and_checkpoint(self):
        calls = []
        status = {"status": "running", "status_detail": "waiting_for_approval"}
        def runner(method, url, body, headers):
            calls.append((method, url))
            if method == "POST" and not url.endswith("messages"):
                saved = json.loads((self.root / (_key("work") + ".json")).read_text())
                self.assertTrue(saved["checkpoint"]["tag"] in body["tags"])
                self.assertIsNone(saved["binding"])
            return {"session_id": "remote-1", **status, "structured_output": {"secret": "result"}}
        self.service = RemoteSessions(self.root, DevinProvider(DevinClient("org-example", "test-key", api_runner=runner)))
        self.assertEqual(self.dispatch()["state"], "waiting_for_approval")
        self.dispatch()
        self.assertEqual(sum(method == "POST" for method, _ in calls), 1)
        self.assertEqual(self.service.run("collect", "work", apply=True)["reason"], "result_not_ready")
        status.update(status_detail="waiting_for_user")
        self.service.run("collect", "work", apply=True)
        self.assertEqual(self.service.private_result("work"), {"secret": "result"})
        self.service.run("cancel", "work", request="c1", apply=True)
        self.assertTrue(any(method == "DELETE" for method, _ in calls))


if __name__ == "__main__":
    unittest.main()
