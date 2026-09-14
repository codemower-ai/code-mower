"""Takeover cancellation, idempotency, and bounded Git capabilities (#962)."""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from code_mower import init as initialization
from code_mower import config, devin_sessions, lane_delivery, lane_handoff, lane_runtime
from code_mower.remote_session import FakeProvider, RemoteError, RemoteSessions

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def handoff(head=SHA):
    return lane_delivery.validate_handoff(source_lane="devin", destination_lane="codex",
        target_pr="owner/repo#42", expected_head=head, running_lane="codex", repo="owner/repo",
        observed_head=head, target_branch="devin/42", source_branch_prefixes=["devin/"])


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL).strip()


def checkout(root):
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "builder@example.com")
    git(root, "config", "user.name", "Builder")
    git(root, "checkout", "-qb", "devin/42")
    (root / "file").write_text("initial\n")
    git(root, "add", "file")
    git(root, "commit", "-qm", "initial")
    return root


class WriterStateTests(unittest.TestCase):
    def test_raw_provider_state_remains_independent_of_result_and_archival(self):
        for status, detail, archived, expected in (
            ("running", "finished", False, "running"),
            ("running", "finished", True, "running"),
            ("exit", "finished", False, "terminated"),
            ("suspended", "finished", False, "suspended"),
            ("error", "finished", False, "unknown"),
        ):
            with self.subTest(status=status, archived=archived):
                session = devin_sessions.normalize_session({"session_id": "test-session",
                    "status": status, "status_detail": detail, "is_archived": archived,
                    "structured_output": {"ready": True}})
                self.assertEqual(session.writer_state, expected)

    def test_remote_logical_completion_is_cancelled_once_and_retired(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            provider = FakeProvider(root / "provider")
            engine = RemoteSessions(root / "sessions", provider)
            engine.run("dispatch", "work", prose="Implement bounded work", repo="owner/repo", apply=True)
            original_get = provider.get
            def ready_while_running(binding):
                value = original_get(binding)
                return devin_sessions.Session(binding, value.state, structured_output={"ready": True},
                                              writer_state=value.writer_state)
            with mock.patch.object(provider, "get", side_effect=ready_while_running), \
                    mock.patch.object(provider, "cancel", wraps=provider.cancel) as cancel:
                self.assertEqual(engine.run("status", "work")["state"], "complete")
                self.assertEqual(engine.writer_state("work", repo="owner/repo"), "running")
                self.assertEqual(engine.retire_writer("work", repo="owner/repo", request="handoff"), "terminated")
                engine.retire_writer("work", repo="owner/repo", request="handoff")
                self.assertEqual(cancel.call_count, 1)
            with self.assertRaisesRegex(RemoteError, "writer_retired"):
                engine.run("message", "work", request="resume", prose="resume work", apply=True)
            with self.assertRaisesRegex(RemoteError, "binding_mismatch"):
                engine.writer_state("work", repo="owner/other")

    def test_failed_cancellation_is_uncertain_and_never_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            provider = FakeProvider(root / "provider")
            engine = RemoteSessions(root / "sessions", provider)
            engine.run("dispatch", "work", prose="Implement bounded work", repo="owner/repo", apply=True)
            with mock.patch.object(provider, "cancel", side_effect=RuntimeError("private diagnostic")) as cancel:
                for _ in range(2):
                    with self.assertRaises(RemoteError):
                        engine.retire_writer("work", repo="owner/repo", request="handoff")
                self.assertEqual(cancel.call_count, 1)
            self.assertEqual(engine.writer_state("work", repo="owner/repo"), "unknown")


class TakeoverIntentTests(unittest.TestCase):
    def test_source_binding_is_private_bounded_and_outside_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source.json"
            for raw in ('[]', '{"transport":"local_process"}', ' ' * 16385):
                source.write_text(raw)
                source.chmod(0o600)
                with self.assertRaises(ValueError):
                    lane_handoff.read_source(source)
            source.write_text(json.dumps({"transport": "local_process", "writer": "one",
                                          "state_dir": str(root / "writers")}))
            source.chmod(0o644)
            with self.assertRaises(lane_delivery.LaneDeliveryError):
                lane_handoff.read_source(source)
            source.chmod(0o600)
            self.assertEqual(lane_handoff.read_source(source)["writer"], "one")
            link = root / "linked.json"
            link.symlink_to(source)
            with self.assertRaises(OSError):
                lane_handoff.read_source(link)
            (root / ".git").mkdir()
            with self.assertRaises(lane_delivery.LaneDeliveryError):
                lane_handoff.read_source(source)

    def test_control_failure_cleans_owned_process_and_restores_handlers(self):
        import signal
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            writer = mock.Mock()
            writer.stop_requested.return_value = False
            writer.started.side_effect = RuntimeError("private control failure")
            before = signal.getsignal(signal.SIGTERM)
            with self.assertRaises(RuntimeError):
                lane_delivery.supervise_process([sys.executable, "-c", "import time; time.sleep(20)"],
                    log_path=root / "log", timeout_seconds=30, writer=writer)
            pid, _pgid = writer.started.call_args.args
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            writer.finish.assert_called_once_with(quiescent=True)
            self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_parallel_replay_accepts_and_launches_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            stop = mock.Mock(return_value="terminated")
            source = {"transport": "remote_session", "provider": "fake", "session": "work", "state_dir": str(root)}
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: lane_handoff.prepare(handoff(), source, root,
                    stop=stop, head=lambda _: SHA), range(2)))
            self.assertEqual(sum(result["notify"] for result in results), 1)
            self.assertEqual(sum(result["launch_allowed"] for result in results), 1)
            self.assertEqual(stop.call_count, 1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                launches = list(pool.map(lambda _: lane_handoff.reserve_launch(handoff(), root,
                    stop=stop, head=lambda _: SHA), range(2)))
            self.assertEqual(sum(launches), 1)

    def test_cancel_failure_or_source_head_movement_emits_one_owner_action(self):
        for stop, head in ((mock.Mock(side_effect=RuntimeError("private")), SHA),
                           (mock.Mock(return_value="terminated"), "b" * 40)):
            with self.subTest(head=head), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                first = lane_handoff.prepare(handoff(), {}, root, stop=stop, head=lambda _, value=head: value)
                second = lane_handoff.prepare(handoff(), {}, root, stop=stop, head=lambda _, value=head: value)
                self.assertTrue(first["owner_action"])
                self.assertTrue(first["notify"])
                self.assertFalse(second["notify"])
                self.assertFalse(first["launch_allowed"])
                self.assertEqual(stop.call_count, 1)
                with self.assertRaises(lane_delivery.LaneDeliveryError):
                    lane_handoff.reserve_launch(handoff(), root, head=lambda _: SHA)

    def test_moved_head_and_changed_destination_fail_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            stop = mock.Mock(return_value="terminated")
            lane_handoff.prepare(handoff(), {}, root, stop=stop, head=lambda _: SHA)
            with self.assertRaises(lane_delivery.LaneDeliveryError):
                lane_handoff.reserve_launch(handoff(), root, stop=stop, head=lambda _: "b" * 40)
            other = lane_delivery.Handoff("devin", "claude", "owner/repo#42", SHA, "devin/42")
            with self.assertRaises(lane_delivery.LaneDeliveryError):
                lane_handoff.reserve_launch(other, root, stop=stop, head=lambda _: SHA)

    def test_local_supervisor_stops_owned_writer_and_proves_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work = checkout(root / "checkout")
            store = root / "writers"
            args = [sys.executable, "-m", "code_mower.lane_delivery", "supervise",
                "--log", str(root / "provider.log"), "--timeout-seconds", "30", "--cwd", str(work),
                "--writer", "source", "--writer-state-dir", str(store), "--writer-repo", "owner/repo",
                "--writer-lane", "devin", "--", sys.executable, "-c", "import time; time.sleep(25)"]
            proc = subprocess.Popen(args, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                writer = lane_handoff.LocalWriter(store, "source")
                deadline = time.monotonic() + 5
                while True:
                    with writer.store.locked(writer.key) as locked:
                        state = locked.read()
                    if state and state.get("pid"):
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.03)
                self.assertEqual(writer.stop(handoff(git(work, "rev-parse", "HEAD"))), "terminated")
                self.assertEqual(proc.wait(timeout=5), lane_delivery.EXIT_INTERRUPTED)
                with self.assertRaisesRegex(lane_delivery.LaneDeliveryError, "already registered"):
                    writer.register(repo="owner/repo", lane="devin", checkout=work)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=20)

    def test_unreachable_local_supervisor_and_unpublished_commit_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work = checkout(root / "checkout")
            original = handoff(git(work, "rev-parse", "HEAD"))
            writer = lane_handoff.LocalWriter(root / "writers", "source")
            writer.register(repo="owner/repo", lane="devin", checkout=work)
            with self.assertRaisesRegex(lane_delivery.LaneDeliveryError, "quiescence"):
                writer.stop(original, timeout=0.01)
            writer.finish(quiescent=True)
            (work / "file").write_text("advanced\n")
            git(work, "commit", "-qam", "unpublished")
            with self.assertRaisesRegex(lane_delivery.LaneDeliveryError, "moved"):
                writer.stop(original)


class RuntimeTests(unittest.TestCase):
    def test_shared_git_directory_and_runtime_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work = checkout(root / "checkout")
            (work / ".git" / "commondir").write_text(str(root / "elsewhere"))
            with self.assertRaises(lane_delivery.LaneDeliveryError):
                lane_runtime.prepare(work)
            (work / ".git" / "commondir").unlink()
            (work / ".code-mower").symlink_to(root, target_is_directory=True)
            with self.assertRaises(lane_delivery.LaneDeliveryError):
                lane_runtime.prepare(work)

    def test_preflight_rejects_a_profile_that_only_denies_outside_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work = checkout(root / "checkout")
            prepared = lane_runtime.prepare(work, sys.executable)
            before = (work / ".git/config").read_bytes()
            # Offline faulty-platform adapter: confinement works, child read-only
            # rules do not. The real preflight script must reject it before spend.
            program = """import os, sys
from pathlib import Path
original = os.open
def open_with_broken_children(path, flags, mode=0o777):
    if Path(path).name == 'must-not-write':
        raise PermissionError('simulated outside denial')
    return original(path, flags, mode)
os.open = open_with_broken_children
exec(sys.argv[-1])
"""
            cli = root / "fake-codex"
            cli.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -c "
                           + shlex.quote(program) + ' "$@"\n')
            cli.chmod(0o755)
            with self.assertRaisesRegex(lane_delivery.LaneDeliveryError, "protected guard"):
                lane_runtime.preflight(work, str(cli), prepared["codex_config"], sys.executable)
            self.assertEqual((work / ".git/config").read_bytes(), before)
            self.assertFalse(list((work / ".git/hooks").glob("code-mower-capability-*")))
            self.assertFalse((work / ".git/code-mower-lane-guard.json").exists())

    def test_python_shims_use_exact_selected_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work = checkout(root / "checkout")
            prepared = lane_runtime.prepare(work, sys.executable)
            for name in ("python", "python3"):
                result = subprocess.check_output([str(Path(prepared["bin_dir"]) / name),
                    "-c", "import sys; print(sys.executable)"], text=True).strip()
                self.assertEqual(Path(result), Path(sys.executable))

    def test_checked_in_runner_is_generated_from_canonical_configuration(self):
        plan = initialization.render_init_plan(config.load_config(ROOT / "code-mower.yml"), package_mode=True)
        entry = next(row for row in plan.data["generated_files"]
                     if row["path"] == initialization.LANE_MAC_RUNNER_SCRIPT_PATH)
        template = (ROOT / initialization.LANE_MAC_RUNNER_SCRIPT_TEMPLATE).read_text()
        self.assertEqual((ROOT / "tools/lanes/run_mac_lane.sh").read_text(),
                         initialization._render_workflow_template(template, entry))
        prefixes = json.loads(entry["lane_mac_runner_branch_prefixes_json"])
        self.assertIn("devin/", prefixes["devin"])
        self.assertEqual(json.loads(entry["lane_mac_runner_builder_labels_json"])["devin"], "builder:devin")

    def test_runner_rejects_incomplete_or_non_pr_handoff_before_remote_work(self):
        for target, flags, diagnostic in (
            ("issue:12", ["--handoff-source-lane", "devin", "--handoff-expected-head", SHA,
                          "--handoff-source-file", "private-binding.json"], "explicit --target pr:"),
            ("pr:12", ["--handoff-source-lane", "devin", "--handoff-expected-head", SHA],
             "required together"),
            ("pr:12", ["--handoff-source-file", "private-binding.json"], "required together"),
        ):
            with self.subTest(target=target, flags=flags):
                result = subprocess.run(["bash", str(ROOT / "tools/lanes/run_mac_lane.sh"),
                    "--lane", "codex", "--repo", "owner/repo", "--target", target, *flags],
                    env={**os.environ, "LANE_PYTHON": sys.executable}, text=True, capture_output=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(diagnostic, result.stderr)
                self.assertNotIn("unbound variable", result.stderr)

    def test_runner_reports_unavailable_configured_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(["bash", str(ROOT / "tools/lanes/run_mac_lane.sh"),
                "--lane", "codex", "--repo", "owner/repo", "--target", "issue:12"],
                env={**os.environ, "LANE_PYTHON": str(Path(tmp) / "missing-python")},
                text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("configured LANE_PYTHON executable is unavailable", result.stderr)

    def test_canonical_execution_selection_is_explicit_and_validated(self):
        cfg = config.load_config(ROOT / "code-mower.yml")
        cfg["owner_surface"]["lane_runner_builders"] = ["devin"]
        plan = initialization.render_init_plan(cfg, package_mode=True)
        entry = next(row for row in plan.data["generated_files"]
                     if row["path"] == initialization.LANE_MAC_RUNNER_SCRIPT_PATH)
        self.assertEqual(entry["lane_mac_runner_allowed_case"], "devin")
        cfg["owner_surface"]["lane_runner_builders"] = ["devin", "devin"]
        with self.assertRaises(config.ConfigError):
            initialization.render_init_plan(cfg, package_mode=True)
        self.assertTrue(any(issue.path == "owner_surface.lane_runner_builders"
                            for issue in config.validate_config(cfg)))

    @unittest.skipUnless(os.environ.get("CODE_MOWER_TEST_CODEX_SANDBOX") == "1", "explicit installed-Codex sandbox rehearsal")
    def test_real_codex_sandbox_git_and_checkout_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            work = checkout(root / "checkout")
            prepared = lane_runtime.prepare(work, sys.executable)
            codex = shutil.which("codex")
            self.assertIsNotNone(codex)
            lane_runtime.preflight(work, codex, prepared["codex_config"], sys.executable)
            # A local bare remote inside the disposable checkout permits a real
            # push without network traffic or any external repository mutation.
            bare = work / ".code-mower" / "remote.git"
            git(work, "init", "--bare", "-q", str(bare))
            git(work, "remote", "add", "origin", str(bare))
            script = (ROOT / "tools/lanes/run_mac_lane.sh").read_text()
            hook_text = script.split("<<'HOOK'\n", 1)[1].split("\nHOOK\n", 1)[0]
            hook = work / ".git/hooks/pre-push"
            hook.write_text(hook_text + "\n")
            hook.chmod(0o755)
            (work / ".git/code-mower-lane-guard.json").write_text(json.dumps({
                "lane": "codex", "mode": "fix", "allowed_prefixes": ["codex/"], "handoff": None}))
            args = [codex, "sandbox", "-P", lane_runtime.PROFILE, "-C", str(work)]
            for setting in prepared["codex_config"]:
                args.extend(["-c", setting])
            commands = "git fetch origin && git checkout -b codex/test && printf change >> file && git add file && git commit -qm change && git push origin HEAD:refs/heads/codex/test"
            result = subprocess.run([*args, "bash", "-c", commands], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            denied = subprocess.run([*args, "git", "push", "origin", "HEAD:refs/heads/foreign/test"], capture_output=True)
            self.assertNotEqual(denied.returncode, 0)
            denied = subprocess.run([*args, sys.executable, "-c", "from pathlib import Path; Path('.git/hooks/pre-push').write_text('changed')"], capture_output=True)
            self.assertNotEqual(denied.returncode, 0)
