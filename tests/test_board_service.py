"""Managed Board service lifecycle.

The launchd half of this is simulated through `FakeHost`, which answers the
exact `launchctl`, `ps`, `lsof` and `git` commands the provider runs. That keeps
the macOS lifecycle cases -- stale-service takeover, two repositories, three
services, delayed health, rollback, idempotent restart, an external supervisor
on the port -- runnable on Linux CI as well, so the refusal behavior is
validated everywhere and not only on the one machine that has launchd.
"""

from __future__ import annotations

import json
import plistlib
import shlex
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from unittest import TestCase

from code_mower import board, board_service, lane_status


VERSION = board_service.installed_version()


def _completed(stdout: str = "", *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeHost:
    """One dictionary of local state, answered as launchd, ps, lsof and git."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.uid = 501
        self.loaded: dict[str, int | None] = {}
        self.processes: dict[int, dict[str, object]] = {}
        self.listeners: dict[int, int] = {}
        self.origins: dict[str, str] = {}
        self.identities: dict[int, dict[str, object]] = {}
        self.bootstrap_failures: set[str] = set()
        self.write_failures: set[str] = set()
        self.next_pid = 900
        self.calls: list[list[str]] = []

    # -- simulation -----------------------------------------------------
    def provider(self, **kwargs: object) -> board_service.LaunchdProvider:
        return board_service.LaunchdProvider(
            command_runner=self.run,
            root=self.root,
            uid=self.uid,
            platform="darwin",
            **kwargs,
        )

    def identity_probe(self, host: str, port: int) -> dict[str, object]:
        return dict(self.identities.get(int(port), {"available": False}))

    def _start(self, label: str) -> None:
        path = self.root / f"{label}.plist"
        data = plistlib.loads(path.read_bytes())
        arguments = [str(item) for item in data["ProgramArguments"]]
        binding = board_service.binding_from_arguments(arguments)
        pid = self.next_pid
        self.next_pid += 1
        self.processes[pid] = {
            "command": shlex.join(arguments),
            "cwd": str(data.get("WorkingDirectory") or ""),
            "ppid": 1,
        }
        port = int(binding["port"] or 0)
        self.listeners[port] = pid
        self.identities[port] = {
            "schema": "code_mower.boardIdentity.v1",
            "repo": binding["repo"],
            "board": {
                "version": {
                    "serving_version": VERSION,
                    "installed_version": VERSION,
                    "restart_recommended": False,
                }
            },
        }
        self.loaded[label] = pid

    def _stop(self, label: str) -> None:
        pid = self.loaded.pop(label, None)
        if pid is None:
            return
        self.processes.pop(pid, None)
        for port, holder in list(self.listeners.items()):
            if holder == pid:
                self.listeners.pop(port, None)
                self.identities.pop(port, None)

    def add_foreign_listener(self, port: int, *, command: str, ppid: int) -> int:
        pid = self.next_pid
        self.next_pid += 1
        self.processes[pid] = {"command": command, "cwd": "/tmp/foreign", "ppid": ppid}
        self.listeners[port] = pid
        return pid

    # -- command surface ------------------------------------------------
    def run(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = [str(item) for item in args]
        self.calls.append(argv)
        if argv[:1] == ["launchctl"]:
            return self._launchctl(argv[1:])
        if argv[:1] == ["ps"]:
            return self._ps(argv[1:])
        if argv[:1] == ["lsof"]:
            return self._lsof(argv[1:])
        if argv[:1] == ["git"]:
            return _completed(self.origins.get(argv[2], "") + "\n") if len(argv) > 2 else _completed("", returncode=1)
        return _completed("", returncode=1)

    def _launchctl(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        action = argv[0] if argv else ""
        if action == "version":
            return _completed("launchctl fake\n")
        if action == "print":
            label = argv[1].rsplit("/", 1)[-1]
            if label not in self.loaded:
                return _completed("", returncode=1, stderr="Could not find service\n")
            pid = self.loaded[label]
            body = "\tstate = running\n" + (f"\tpid = {pid}\n" if pid else "")
            return _completed(body)
        if action == "bootstrap":
            label = Path(argv[2]).name[: -len(".plist")]
            if label in self.bootstrap_failures:
                return _completed("", returncode=1, stderr="Bootstrap failed: 5: Input/output error\n")
            self._start(label)
            return _completed("")
        if action == "bootout":
            label = argv[1].rsplit("/", 1)[-1]
            if label not in self.loaded:
                return _completed("", returncode=1, stderr="No such process\n")
            self._stop(label)
            return _completed("")
        if action == "kickstart":
            label = argv[-1].rsplit("/", 1)[-1]
            if label not in self.loaded:
                return _completed("", returncode=1, stderr="No such process\n")
            self._stop(label)
            self._start(label)
            return _completed("")
        return _completed("", returncode=1)

    def _ps(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        pid = int(argv[argv.index("-p") + 1])
        process = self.processes.get(pid)
        if process is None:
            return _completed("", returncode=1)
        if "command=" in argv:
            return _completed(str(process["command"]) + "\n")
        if "ppid=" in argv:
            return _completed(f"  {process['ppid']}\n")
        return _completed("", returncode=1)

    def _lsof(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "-iTCP" in argv:
            if not self.listeners:
                return _completed("", returncode=1)
            lines = []
            for port, pid in sorted(self.listeners.items()):
                lines.extend([f"p{pid}", "ccode-mower", f"n127.0.0.1:{port}"])
            return _completed("\n".join(lines) + "\n")
        if "-d" in argv:
            pid = int(argv[argv.index("-p") + 1])
            process = self.processes.get(pid)
            if process is None:
                return _completed("", returncode=1)
            return _completed(f"p{pid}\nn{process['cwd']}\n")
        return _completed("", returncode=1)


class ServiceHarness(TestCase):
    """Shared fixture: a fake host, a services root, and two checkouts."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # Resolved, because `build_spec` resolves the requested checkout and the
        # whole binding contract is stated in that one canonical spelling. On
        # macOS the temporary root is `/var/...` for `/private/var/...`, so an
        # unresolved fixture would register its origins and assert its rendered
        # argv under a spelling the product never produces -- the checkout would
        # look originless and the ownership guard would be tested vacuously.
        self.tmp = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)
        self.root = self.tmp / "LaunchAgents"
        self.root.mkdir()
        self.host = FakeHost(self.root)
        self.checkout = self.tmp / "code-mower"
        self.checkout.mkdir()
        self.other_checkout = self.tmp / "private-repo"
        self.other_checkout.mkdir()
        self.host.origins[str(self.checkout)] = "git@github.com:codemower-ai/code-mower.git"
        self.host.origins[str(self.other_checkout)] = "git@github.com:codemower-ai/private-repo.git"
        self.slept: list[float] = []

    def sleeper(self, seconds: float) -> None:
        self.slept.append(seconds)

    def spec(
        self,
        *,
        repo: str = "codemower-ai/code-mower",
        repo_path: Path | None = None,
        port: int = 5332,
        record_events: bool = True,
    ) -> board_service.ServiceSpec:
        return board_service.build_spec(
            repo=repo,
            repo_path=repo_path or self.checkout,
            port=port,
            record_events=record_events,
            program=("/usr/local/bin/code-mower",),
            path_env="/usr/local/bin:/usr/bin:/bin",
        )

    def install(self, spec: board_service.ServiceSpec, **kwargs: object) -> dict:
        return board_service.install_service(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
            settle_seconds=0.0,
            refresh_seconds=0.1,
            timeout_seconds=0.0,
            sleeper=self.sleeper,
            **kwargs,
        )

    def restart(self, spec: board_service.ServiceSpec, **kwargs: object) -> dict:
        return board_service.restart_service(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
            settle_seconds=0.0,
            refresh_seconds=0.1,
            timeout_seconds=0.0,
            sleeper=self.sleeper,
            **kwargs,
        )


class BoardServiceContractTest(ServiceHarness):
    """Platform-neutral: parsing, rendering, redaction, and refusals."""

    def test_build_spec_rejects_requests_that_can_never_be_valid(self) -> None:
        with self.assertRaises(board_service.ServiceRequestError):
            self.spec(repo="not-a-slug")
        with self.assertRaises(board_service.ServiceRequestError):
            self.spec(port=0)
        with self.assertRaises(board_service.ServiceRequestError):
            self.spec(repo_path=self.tmp / "missing")

    def test_definition_renders_deterministically_and_binds_the_exact_request(self) -> None:
        first = board_service.render_definition(self.spec())
        second = board_service.render_definition(self.spec())

        self.assertEqual(first, second)
        self.assertEqual(board_service.definition_digest(first), board_service.definition_digest(second))
        data = plistlib.loads(first.encode("utf-8"))
        self.assertEqual(data["Label"], "ai.codemower.board.5332")
        self.assertTrue(data["KeepAlive"])
        self.assertEqual(
            data["ProgramArguments"],
            [
                "/usr/local/bin/code-mower",
                "board",
                "serve",
                "--repo",
                "codemower-ai/code-mower",
                "--repo-path",
                str(self.checkout),
                "--host",
                "127.0.0.1",
                "--port",
                "5332",
                "--record-events",
            ],
        )
        self.assertEqual(data["WorkingDirectory"], str(self.checkout))
        # One canonical spelling: what is rendered is what `build_spec` resolved,
        # so the argv, the working directory and every later binding comparison
        # are stated in the same path.
        canonical = str(self.checkout.resolve())
        self.assertEqual(data["WorkingDirectory"], canonical)
        rendered_binding = board_service.binding_from_arguments(data["ProgramArguments"])
        self.assertEqual(rendered_binding["repo_path"], canonical)

    def test_a_different_binding_renders_a_different_digest(self) -> None:
        one = board_service.definition_digest(board_service.render_definition(self.spec()))
        two = board_service.definition_digest(
            board_service.render_definition(self.spec(repo_path=self.other_checkout, repo="codemower-ai/private-repo"))
        )

        self.assertNotEqual(one, two)

    def test_definition_payload_redacts_local_paths_by_default(self) -> None:
        redacted = board_service.definition_payload(self.spec())
        shown = board_service.definition_payload(self.spec(), show_local_paths=True)

        self.assertNotIn(str(self.checkout), json.dumps(redacted))
        self.assertEqual(redacted["repo_path"], lane_status.LOCAL_PATH_REDACTION)
        self.assertTrue(redacted["arguments_redacted"])
        self.assertIn(lane_status.LOCAL_PATH_REDACTION, redacted["arguments"])
        self.assertIn("codemower-ai/code-mower", redacted["arguments"])
        self.assertNotIn("definition", redacted)
        self.assertIn(str(self.checkout), shown["definition"])

    def test_binding_is_recovered_from_either_argument_spelling(self) -> None:
        spaced = board_service.binding_from_arguments(
            ["code-mower", "board", "serve", "--repo", "a/b", "--port", "5333", "--repo-path", "/tmp/x"]
        )
        joined = board_service.binding_from_arguments(
            ["code-mower", "board", "serve", "--repo=a/b", "--port=5333", "--repo-path=/tmp/x"]
        )

        self.assertEqual(spaced, joined)
        self.assertEqual(spaced["repo"], "a/b")
        self.assertEqual(spaced["port"], 5333)
        self.assertEqual(spaced["repo_path"], "/tmp/x")

    def test_a_platform_without_launchd_refuses_instead_of_pretending(self) -> None:
        provider = board_service.select_provider(platform="linux", command_runner=self.host.run)
        payload = board_service.install_service(
            self.spec(), provider=provider, command_runner=self.host.run, sleeper=self.sleeper
        )

        self.assertIsInstance(provider, board_service.UnsupportedProvider)
        self.assertEqual(payload["status"], "unsupported_platform")
        self.assertEqual(board_service.managed_services(platform="linux", command_runner=self.host.run), [])

    def test_a_host_that_cannot_run_launchctl_is_unavailable(self) -> None:
        provider = board_service.LaunchdProvider(
            command_runner=lambda _args: _completed("", returncode=127),
            root=self.root,
            uid=501,
            platform="darwin",
        )

        available, why = provider.available()

        self.assertFalse(available)
        self.assertIn("launchctl", why)

    def test_a_path_from_another_repository_is_refused_before_anything_is_applied(self) -> None:
        payload = self.install(self.spec(repo="codemower-ai/private-repo", repo_path=self.checkout))

        self.assertEqual(payload["status"], "ownership_mismatch")
        self.assertEqual(payload["ownership"], "mismatch")
        self.assertEqual(list(self.root.glob("*.plist")), [])
        self.assertEqual(self.host.loaded, {})

    def test_a_path_with_no_readable_origin_is_refused_before_anything_is_applied(self) -> None:
        # Ownership is proven from the checkout's origin. A path whose origin
        # cannot be read is unprovable, not implicitly ours, so it refuses on the
        # same terms as a path that names another repository.
        unproven = self.tmp / "unproven"
        unproven.mkdir()

        payload = self.install(self.spec(repo_path=unproven))

        self.assertEqual(payload["status"], "ownership_mismatch")
        self.assertEqual(payload["ownership"], "unverified")
        self.assertEqual(list(self.root.glob("*.plist")), [])
        self.assertEqual(self.host.loaded, {})
        self.assertNotIn(str(unproven), json.dumps(payload))

    def test_an_unprovable_path_is_refused_on_restart_too(self) -> None:
        unproven = self.tmp / "unproven-restart"
        unproven.mkdir()

        payload = self.restart(self.spec(repo_path=unproven))

        self.assertEqual(payload["status"], "ownership_mismatch")
        self.assertEqual(payload["ownership"], "unverified")
        self.assertEqual(list(self.root.glob("*.plist")), [])
        self.assertEqual(self.host.loaded, {})


class BoardServiceLifecycleTest(ServiceHarness):
    """macOS lifecycle, simulated end to end."""

    def test_install_starts_a_supervised_service_and_validates_the_binding(self) -> None:
        payload = self.install(self.spec())

        self.assertEqual(payload["status"], "installed")
        self.assertEqual(payload["delayed_health"]["state"], "pass")
        binding = payload["delayed_health"]["binding"]
        self.assertEqual(binding["failing_checks"], [])
        covered = {check["id"] for check in binding["checks"]}
        self.assertEqual(covered, set(board_service.BINDING_CHECK_IDS))
        self.assertTrue((self.root / "ai.codemower.board.5332.plist").exists())
        self.assertIn("ai.codemower.board.5332", self.host.loaded)

    def test_three_managed_boards_over_two_repositories_all_validate(self) -> None:
        self.install(self.spec())
        self.install(self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5342))
        self.install(self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5344))

        status = board_service.service_status(
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )

        self.assertEqual(status["status"], "ok")
        self.assertEqual(status["failing"], [])
        self.assertEqual([row["port"] for row in status["services"]], [5332, 5342, 5344])
        self.assertEqual(
            {row["repo"] for row in status["services"]},
            {"codemower-ai/code-mower", "codemower-ai/private-repo"},
        )
        # Every service is supervised, so each survives the invoking shell.
        for row in status["services"]:
            self.assertEqual(self.host.processes[row["pid"]]["ppid"], 1)

    def test_status_redacts_local_paths_by_default(self) -> None:
        self.install(self.spec())

        redacted = board_service.service_status(
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )
        shown = board_service.service_status(
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
            show_local_paths=True,
        )

        self.assertNotIn(str(self.checkout), json.dumps(redacted))
        self.assertNotIn(str(self.checkout), board_service.render_status_text(redacted))
        self.assertIn(str(self.checkout), json.dumps(shown))

    def test_restart_is_idempotent_when_the_definition_has_not_changed(self) -> None:
        self.install(self.spec())
        first_pid = self.host.loaded["ai.codemower.board.5332"]

        payload = self.restart(self.spec())

        self.assertEqual(payload["status"], "restarted")
        self.assertEqual(payload["delayed_health"]["state"], "pass")
        self.assertNotEqual(self.host.loaded["ai.codemower.board.5332"], first_pid)
        self.assertIn(
            ["launchctl", "kickstart", "-k", "gui/501/ai.codemower.board.5332"],
            self.host.calls,
        )
        # Restarting again changes nothing about the binding.
        again = self.restart(self.spec())
        self.assertEqual(again["status"], "restarted")
        self.assertEqual(again["digest"], payload["digest"])

    def test_installing_an_already_installed_definition_reports_unchanged(self) -> None:
        self.install(self.spec())

        payload = self.install(self.spec())

        self.assertEqual(payload["status"], "unchanged")
        self.assertEqual(payload["delayed_health"]["state"], "pass")

    def test_a_stale_managed_binding_is_never_taken_over_silently(self) -> None:
        self.install(self.spec(repo_path=self.checkout))
        stale_digest = self.host.provider().read_service("ai.codemower.board.5332").digest

        # The same port, now asked to serve a different repository checkout.
        drifted = self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5332)
        refused = self.restart(drifted)

        self.assertEqual(refused["status"], "stale_arguments")
        self.assertEqual(self.host.provider().read_service("ai.codemower.board.5332").digest, stale_digest)
        self.assertEqual(self.host.identities[5332]["repo"], "codemower-ai/code-mower")

    def test_an_explicit_replace_takes_over_a_stale_binding_atomically(self) -> None:
        self.install(self.spec())
        drifted = self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5332)

        payload = self.restart(drifted, replace=True)

        self.assertEqual(payload["status"], "restarted")
        self.assertEqual(payload["delayed_health"]["state"], "pass")
        self.assertEqual(self.host.identities[5332]["repo"], "codemower-ai/private-repo")
        service = self.host.provider().read_service("ai.codemower.board.5332")
        self.assertEqual(service.repo, "codemower-ai/private-repo")
        self.assertEqual(service.digest, board_service.definition_digest(board_service.render_definition(drifted)))
        # Nothing is left staged next to the definition.
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), ["ai.codemower.board.5332.plist"])

    def test_a_failed_apply_restores_the_previous_definition(self) -> None:
        self.install(self.spec())
        before = (self.root / "ai.codemower.board.5332.plist").read_text(encoding="utf-8")
        self.host.bootstrap_failures.add("ai.codemower.board.5332")
        drifted = self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5332)

        payload = self.restart(drifted, replace=True)

        self.assertEqual(payload["status"], "rollback_failed")
        self.assertIn("rollback also failed", payload["message"])
        self.assertEqual((self.root / "ai.codemower.board.5332.plist").read_text(encoding="utf-8"), before)

    def test_a_failed_first_install_leaves_no_definition_behind(self) -> None:
        self.host.bootstrap_failures.add("ai.codemower.board.5332")

        payload = self.install(self.spec())

        self.assertEqual(payload["status"], "apply_failed")
        self.assertFalse((self.root / "ai.codemower.board.5332.plist").exists())

    def test_delayed_health_refreshes_until_the_binding_settles(self) -> None:
        clock = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        spec = self.spec()
        board_service.install_service(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
            settle_seconds=0.0,
            refresh_seconds=0.1,
            timeout_seconds=0.0,
            sleeper=self.sleeper,
        )
        # The listener answers, then goes away before the window closes.
        self.host.identities[5332] = {"available": False}

        health = board_service.delayed_health(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
            settle_seconds=4.0,
            refresh_seconds=1.0,
            timeout_seconds=6.0,
            sleeper=self.sleeper,
            clock=lambda: next(clock),
        )

        self.assertEqual(health["state"], "fail")
        self.assertGreater(health["attempts"], 1)
        self.assertEqual(health["settle_seconds"], 4.0)
        self.assertIn(4.0, self.slept)
        self.assertIn("binding.repo", health["binding"]["failing_checks"])

    def test_a_service_that_comes_back_on_stale_arguments_fails_the_gate(self) -> None:
        spec = self.spec()
        self.install(spec)
        # launchd restarted the job, but the process it brought back carries an
        # older argument list than the definition it was applied from.
        pid = self.host.loaded["ai.codemower.board.5332"]
        self.host.processes[pid]["command"] = "/usr/local/bin/code-mower board serve --repo codemower-ai/code-mower --port 5332"

        binding = board_service.validate_binding(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )

        self.assertEqual(binding["status"], "fail")
        self.assertIn("process.arguments", binding["failing_checks"])

    def test_a_process_bound_to_another_repository_path_fails_the_gate(self) -> None:
        spec = self.spec()
        self.install(spec)
        pid = self.host.loaded["ai.codemower.board.5332"]
        self.host.processes[pid]["cwd"] = str(self.other_checkout)

        binding = board_service.validate_binding(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )

        self.assertIn("process.repo_path", binding["failing_checks"])
        self.assertNotIn(str(self.other_checkout), json.dumps(binding))

    def test_a_stale_serving_version_fails_the_gate(self) -> None:
        spec = self.spec()
        self.install(spec)
        self.host.identities[5332]["board"] = {
            "version": {
                "serving_version": "0.0.1",
                "installed_version": VERSION,
                "restart_recommended": True,
            }
        }

        binding = board_service.validate_binding(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )

        self.assertIn("binding.serving_version", binding["failing_checks"])

    def test_another_supervisor_on_the_port_stops_the_apply(self) -> None:
        self.host.add_foreign_listener(5332, command="/usr/local/bin/code-mower board serve --repo other/other", ppid=1)

        payload = self.install(self.spec())

        self.assertEqual(payload["status"], "external_supervisor")
        self.assertEqual(list(self.root.glob("*.plist")), [])

    def test_an_unrelated_local_process_on_the_port_stops_the_apply(self) -> None:
        self.host.add_foreign_listener(5332, command="/usr/local/bin/code-mower board serve --repo other/other", ppid=4242)

        payload = self.install(self.spec())

        self.assertEqual(payload["status"], "port_conflict")
        self.assertEqual(list(self.root.glob("*.plist")), [])

    def test_a_port_owned_by_another_managed_label_is_never_replaced(self) -> None:
        self.install(self.spec())
        # A second label claiming the same port: ownership is the label, and a
        # label that is not ours may not be replaced.
        spec = self.spec(port=5332)
        other = board_service.ServiceSpec(**{**spec.__dict__, "label": "ai.codemower.board.9999"})

        ownership = board_service.port_ownership(
            5332, provider=self.host.provider(), label=other.label, command_runner=self.host.run
        )

        self.assertEqual(ownership["state"], "managed_other")
        self.assertEqual(ownership["label"], "ai.codemower.board.5332")

    def test_remove_unloads_the_service_and_confirms_the_port_was_released(self) -> None:
        self.install(self.spec())

        payload = board_service.remove_service(
            provider=self.host.provider(),
            repo="codemower-ai/code-mower",
            command_runner=self.host.run,
            settle_seconds=1.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(payload["status"], "removed")
        self.assertTrue(payload["definition_deleted"])
        self.assertFalse((self.root / "ai.codemower.board.5332.plist").exists())
        self.assertEqual(self.host.listeners, {})
        self.assertIn(1.0, self.slept)

    def test_remove_reports_a_port_that_is_still_held(self) -> None:
        self.install(self.spec())

        def sticky_sleeper(_seconds: float) -> None:
            # Something else grabs the port during the settle window.
            self.host.add_foreign_listener(5332, command="python -m http.server 5332", ppid=4242)

        payload = board_service.remove_service(
            provider=self.host.provider(),
            port=5332,
            command_runner=self.host.run,
            settle_seconds=1.0,
            sleeper=sticky_sleeper,
        )

        self.assertEqual(payload["status"], "remove_incomplete")
        self.assertIn("still held", payload["message"])

    def test_an_ambiguous_repository_selection_resolves_nothing(self) -> None:
        self.install(self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5342))
        self.install(self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5344))

        resolved = board_service.resolve_service(provider=self.host.provider(), repo="codemower-ai/private-repo")
        removal = board_service.remove_service(
            provider=self.host.provider(),
            repo="codemower-ai/private-repo",
            command_runner=self.host.run,
            settle_seconds=0.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(resolved["status"], "ambiguous_repository")
        self.assertIsNone(resolved["service"])
        self.assertEqual(removal["status"], "ambiguous_repository")
        self.assertEqual(sorted(removal["matches"]), ["ai.codemower.board.5342", "ai.codemower.board.5344"])
        self.assertEqual(len(self.host.loaded), 2)

    def test_removing_a_service_that_is_not_installed_changes_nothing(self) -> None:
        payload = board_service.remove_service(
            provider=self.host.provider(),
            port=5332,
            command_runner=self.host.run,
            settle_seconds=0.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(payload["status"], "not_installed")


class BoardStopSelectorTest(ServiceHarness):
    """`board stop` selector resolution and the keepalive guard."""

    def _stop(self, **kwargs: object) -> dict:
        stopped: list[tuple[int, int]] = []
        payload = board.stop_board(
            command_runner=self.host.run,
            killer=lambda pid, sig: stopped.append((pid, sig)),
            service_probe=lambda: self.host.provider().list_services(),
            **kwargs,
        )
        payload["_signalled"] = stopped
        return payload

    def _transient_board(self, port: int, repo: str, cwd: Path) -> int:
        return self.host.add_foreign_listener(
            port,
            command=f"/usr/local/bin/code-mower board serve --repo {repo} --repo-path {cwd} --port {port}",
            ppid=4242,
        )

    def test_repo_selector_resolves_one_transient_board(self) -> None:
        pid = self._transient_board(5332, "codemower-ai/code-mower", self.checkout)

        payload = self._stop(repo="codemower-ai/code-mower", yes=True)

        self.assertEqual(payload["status"], "stopped")
        self.assertEqual([entry[0] for entry in payload["_signalled"]], [pid])
        self.assertNotIn(str(self.checkout), json.dumps({k: v for k, v in payload.items() if k != "_signalled"}))

    def test_repo_selector_refuses_an_ambiguous_target_without_stopping_anything(self) -> None:
        self._transient_board(5342, "codemower-ai/private-repo", self.other_checkout)
        self._transient_board(5344, "codemower-ai/private-repo", self.other_checkout)

        payload = self._stop(repo="codemower-ai/private-repo", yes=True)

        self.assertEqual(payload["status"], "ambiguous_selector")
        self.assertEqual(payload["_signalled"], [])
        self.assertEqual(payload["stopped"], [])
        self.assertIn("add --port or --pid", payload["message"])

    def test_disagreeing_selectors_stop_nothing(self) -> None:
        self._transient_board(5332, "codemower-ai/code-mower", self.checkout)
        self._transient_board(5342, "codemower-ai/private-repo", self.other_checkout)

        payload = self._stop(repo="codemower-ai/code-mower", port=5342, yes=True)

        self.assertEqual(payload["status"], "selector_mismatch")
        self.assertEqual(payload["_signalled"], [])

    def test_agreeing_selectors_resolve_the_same_binding(self) -> None:
        pid = self._transient_board(5332, "codemower-ai/code-mower", self.checkout)

        payload = self._stop(repo="codemower-ai/code-mower", port=5332, pid=pid, yes=True)

        self.assertEqual(payload["status"], "stopped")
        self.assertEqual([entry[0] for entry in payload["_signalled"]], [pid])

    def test_an_unknown_repository_is_not_found_rather_than_a_broad_scan(self) -> None:
        self._transient_board(5332, "codemower-ai/code-mower", self.checkout)

        payload = self._stop(repo="someone/else", yes=True)

        self.assertEqual(payload["status"], "not_found")
        self.assertEqual(payload["_signalled"], [])

    def test_a_malformed_repository_selector_is_rejected(self) -> None:
        payload = self._stop(repo="not-a-slug", yes=True)

        self.assertEqual(payload["status"], "invalid_selector")
        self.assertEqual(payload["_signalled"], [])

    def test_stop_refuses_a_port_a_keepalive_service_would_reclaim(self) -> None:
        self.install(self.spec())

        payload = self._stop(port=5332, yes=True)

        self.assertEqual(payload["status"], "managed_service")
        self.assertEqual(payload["_signalled"], [])
        self.assertEqual(payload["managed_service"]["label"], "ai.codemower.board.5332")
        self.assertIn("board service remove --port 5332", payload["message"])
        self.assertIn("ai.codemower.board.5332", self.host.loaded)

    def test_stop_refuses_a_managed_board_selected_by_repository(self) -> None:
        self.install(self.spec())

        payload = self._stop(repo="codemower-ai/code-mower", yes=True)

        self.assertEqual(payload["status"], "managed_service")
        self.assertEqual(payload["_signalled"], [])

    def test_inventory_names_which_boards_are_managed(self) -> None:
        self.install(self.spec())
        self._transient_board(5342, "codemower-ai/private-repo", self.other_checkout)

        payload = board.board_inventory_payload(
            command_runner=self.host.run,
            status_probe=None,
            service_probe=lambda: self.host.provider().list_services(),
        )
        rows = {row["port"]: row for row in payload["boards"]}

        self.assertTrue(rows[5332]["managed"])
        self.assertEqual(rows[5332]["service_label"], "ai.codemower.board.5332")
        self.assertFalse(rows[5342]["managed"])
        rendered = board.render_inventory_text(payload)
        self.assertIn("service=ai.codemower.board.5332", rendered)
        self.assertIn("service=none (transient)", rendered)

    def test_stop_exit_codes_separate_refusals_from_selector_errors(self) -> None:
        self.assertEqual(board._stop_exit_code("stopped"), 0)
        self.assertEqual(board._stop_exit_code("ambiguous_selector"), 2)
        self.assertEqual(board._stop_exit_code("selector_mismatch"), 2)
        self.assertEqual(board._stop_exit_code("managed_service"), 1)
        self.assertEqual(board._service_exit_code("installed"), 0)
        self.assertEqual(board._service_exit_code("ambiguous_repository"), 2)
        self.assertEqual(board._service_exit_code("stale_arguments"), 1)
