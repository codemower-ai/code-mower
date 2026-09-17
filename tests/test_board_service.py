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
from contextlib import AbstractContextManager
from importlib import metadata
from pathlib import Path
from unittest import TestCase, mock

from code_mower import board, board_service, lane_status


# A Board answers about its versions through `board.board_version_payload()`, so
# the fake host answers with exactly that payload rather than a hand-written one
# that always populates `installed_version`. A source checkout reports an empty
# installed version there, and the serving gate has to accept the real shape.
CODE_MOWER_VERSION = board.CODE_MOWER_VERSION

# A stand-in for an operator's private checkout, spelled without this platform's
# home prefix so the repository privacy scan stays clean. What the redaction
# tests need from it is only that it is an absolute local path.
_PRIVATE_CHECKOUT = "/opt/operator/private-checkout"


def _without_distribution_metadata() -> AbstractContextManager[object]:
    """Run as a source checkout: no `code-mower` distribution is installed.

    Both sides read the same `importlib.metadata`, so one patch puts the Board
    payload and the serving gate in the same mode -- which is the point of the
    contract being tested.
    """

    def _missing(name: str) -> str:
        raise metadata.PackageNotFoundError(name)

    return mock.patch.object(metadata, "version", _missing)


def _with_distribution_metadata(version: str) -> AbstractContextManager[object]:
    """Run as an installed distribution reporting `version`."""

    return mock.patch.object(metadata, "version", lambda name: version)


def _completed(stdout: str = "", *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _joined_arguments(arguments: Sequence[str]) -> list[str]:
    """Restate a rendered argv in the `--flag=value` spelling.

    `binding_from_arguments` accepts both, so a definition may arrive in either
    -- this builds the one this lane never renders, to test against it.
    """

    flags = {"--repo", "--repo-path", "--host", "--port"}
    joined: list[str] = []
    index = 0
    values = [str(item) for item in arguments]
    while index < len(values):
        if values[index] in flags and index + 1 < len(values):
            joined.append(f"{values[index]}={values[index + 1]}")
            index += 2
        else:
            joined.append(values[index])
            index += 1
    return joined


class FakeHost:
    """One dictionary of local state, answered as launchd, ps, lsof and git."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.uid = 501
        self.loaded: dict[str, int | None] = {}
        self.job_arguments: dict[str, list[str]] = {}
        self.processes: dict[int, dict[str, object]] = {}
        self.listeners: dict[int, int] = {}
        # A port can genuinely be held by more than one process; the primary
        # mapping holds one per port, this holds the rest.
        self.extra_listeners: list[tuple[int, int]] = []
        # Two processes can hold the same port on different addresses without
        # either failing to bind, so the address a listener answers on is part
        # of the inventory rather than a constant.
        self.listener_addresses: dict[int, str] = {}
        # A host where the listener inventory cannot be taken at all: `lsof`
        # cannot run and there is no `ss` to fall back to. Distinct from a host
        # where nothing is listening, which `lsof` reports by exiting 1.
        self.listener_inventory_available = True
        self.origins: dict[str, str] = {}
        self.identities: dict[int, dict[str, object]] = {}
        self.bootstrap_failures: set[str] = set()
        # A `launchctl bootout` that fails leaves the job exactly where it was:
        # still loaded, still supervised, still holding its port.
        self.bootout_failures: set[str] = set()
        self.write_failures: set[str] = set()
        # A host where `launchctl` itself cannot be reached: the capability
        # probe fails, and so does every other question asked of launchd. The
        # definitions on disk and the processes they started are untouched.
        self.launchctl_reachable = True
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
            "argv": list(arguments),
            "cwd": str(data.get("WorkingDirectory") or ""),
            "ppid": 1,
        }
        self.job_arguments[label] = list(arguments)
        port = int(binding["port"] or 0)
        self.listeners[port] = pid
        self.identities[port] = {
            "schema": "code_mower.boardIdentity.v1",
            "repo": binding["repo"],
            "board": {"version": board.board_version_payload()},
        }
        self.loaded[label] = pid

    def _stop(self, label: str) -> None:
        pid = self.loaded.pop(label, None)
        self.job_arguments.pop(label, None)
        if pid is None:
            return
        self.processes.pop(pid, None)
        for port, holder in list(self.listeners.items()):
            if holder == pid:
                self.listeners.pop(port, None)
                self.identities.pop(port, None)

    def add_foreign_listener(
        self, port: int, *, command: str, ppid: int, address: str = "127.0.0.1"
    ) -> int:
        pid = self.next_pid
        self.next_pid += 1
        self.processes[pid] = {"argv": shlex.split(command), "cwd": "/tmp/foreign", "ppid": ppid}
        self.listeners[port] = pid
        self.listener_addresses[pid] = address
        return pid

    def relaunch_on(self, label: str, arguments: Sequence[str]) -> int:
        """Bring a job back with an argument list of the caller's choosing.

        launchd is the authority on the argv of a job it supervises, so a test
        that wants the running process to disagree with its definition has to
        say so here rather than by rewriting a `ps` line.
        """

        self._stop(label)
        pid = self.next_pid
        self.next_pid += 1
        argv = [str(item) for item in arguments]
        binding = board_service.binding_from_arguments(argv)
        self.processes[pid] = {"argv": argv, "cwd": str(binding["repo_path"] or ""), "ppid": 1}
        self.job_arguments[label] = argv
        self.loaded[label] = pid
        port = int(binding["port"] or 0)
        if port:
            self.listeners[port] = pid
            self.identities[port] = {
                "schema": "code_mower.boardIdentity.v1",
                "repo": binding["repo"],
                "board": {"version": board.board_version_payload()},
            }
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
        # This host has launchctl, ps, lsof and git, and nothing else. A binary
        # that is not installed raises rather than exiting non-zero, which is
        # how `ss` behaves on macOS -- and the only way a caller can tell "the
        # tool answered, nothing matched" from "there was no tool to ask".
        raise FileNotFoundError(f"{argv[0] if argv else ''}: command not found")

    def _launchctl(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        action = argv[0] if argv else ""
        if not self.launchctl_reachable:
            return _completed("", returncode=1, stderr="launchctl: Could not connect to launchd\n")
        if action == "version":
            return _completed("launchctl fake\n")
        if action == "print":
            label = argv[1].rsplit("/", 1)[-1]
            if label not in self.loaded:
                return _completed("", returncode=1, stderr="Could not find service\n")
            pid = self.loaded[label]
            body = "\tstate = running\n" + (f"\tpid = {pid}\n" if pid else "")
            # launchd prints the job's argv one argument per line, which is the
            # only local source that keeps argument boundaries intact.
            arguments = self.job_arguments.get(label)
            if arguments is not None:
                lines = "".join(f"\t\t{item}\n" for item in arguments)
                body += f"\targuments = {{\n{lines}\t}}\n"
            return _completed(body)
        if action == "bootstrap":
            label = Path(argv[2]).name[: -len(".plist")]
            if label in self.bootstrap_failures:
                # Real `launchctl` names the definition file it could not load,
                # which is how a local path reaches an operation message at all.
                return _completed(
                    "",
                    returncode=1,
                    stderr=f"Bootstrap failed: 5: Input/output error\nPath: {argv[2]}\n",
                )
            # launchd will not bootstrap a label its domain already holds, so a
            # rollback that writes a definition back without first unloading the
            # job it replaced cannot look like it succeeded here.
            if label in self.loaded:
                return _completed(
                    "", returncode=1, stderr="Bootstrap failed: 37: Operation already in progress\n"
                )
            self._start(label)
            return _completed("")
        if action == "bootout":
            label = argv[1].rsplit("/", 1)[-1]
            if label in self.bootout_failures:
                return _completed("", returncode=1, stderr="Boot-out failed: 5: Input/output error\n")
            if label not in self.loaded:
                return _completed("", returncode=1, stderr="No such process\n")
            self._stop(label)
            return _completed("")
        if action == "kickstart":
            label = argv[-1].rsplit("/", 1)[-1]
            if label not in self.loaded:
                return _completed("", returncode=1, stderr="No such process\n")
            # `kickstart -k` restarts the job launchd already holds: it re-execs
            # the argument list launchd registered when the job was bootstrapped
            # and never rereads the definition on disk. A fake that reread the
            # plist here would heal a stale runtime binding that the real
            # launchd would bring straight back, and hide the fact that only a
            # bootout and a bootstrap apply a changed definition.
            registered = list(self.job_arguments.get(label) or [])
            if not registered:
                self._stop(label)
                self._start(label)
                return _completed("")
            self.relaunch_on(label, registered)
            return _completed("")
        return _completed("", returncode=1)

    def _process_name(self, pid: int) -> str:
        argv = [str(item) for item in (self.processes.get(pid) or {}).get("argv") or []]
        return Path(argv[0]).name if argv else "unknown"

    def _ps(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        pid = int(argv[argv.index("-p") + 1])
        process = self.processes.get(pid)
        if process is None:
            return _completed("", returncode=1)
        if "command=" in argv:
            # Exactly what the real `ps` renders: one space-joined line with no
            # quoting, so an argument containing a space is indistinguishable
            # from two arguments. Nothing may reconstruct an argv from this.
            return _completed(" ".join(str(item) for item in process["argv"]) + "\n")
        if "ppid=" in argv:
            return _completed(f"  {process['ppid']}\n")
        return _completed("", returncode=1)

    def _lsof(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "-iTCP" in argv:
            if not self.listener_inventory_available:
                # `lsof` could not answer at all. Not the same as exiting 1 with
                # nothing to report, which is the answer "no process holds it".
                raise FileNotFoundError("lsof: command not found")
            if not self.listeners and not self.extra_listeners:
                # The real `lsof` exits 1 when no file matched, which is how it
                # says the host has no listeners.
                return _completed("", returncode=1)
            lines = []
            held = sorted([*self.listeners.items(), *self.extra_listeners])
            for port, pid in held:
                # The real `lsof` names the executable that holds the port, which
                # is how a listener gets classified. Reporting every listener as
                # `code-mower` would make a Node server on 5332 look Board-shaped
                # and hide exactly the misclassification this inventory must not
                # make.
                address = self.listener_addresses.get(pid, "127.0.0.1")
                lines.extend([f"p{pid}", f"c{self._process_name(pid)}", f"n{address}:{port}"])
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

    def test_both_argument_spellings_are_redacted_the_same_way(self) -> None:
        # The parser accepts either spelling, so the redactor has to hide the
        # path in either. Testing only the standalone form left the joined one
        # fully visible under an `arguments_redacted: true` payload.
        redacted = board_service.redact_arguments(
            [
                "code-mower",
                "board",
                "serve",
                "--repo=a/b",
                "--port=5333",
                f"--repo-path={_PRIVATE_CHECKOUT}",
                "--repo-path",
                _PRIVATE_CHECKOUT,
                "--record-events",
            ],
            show_local_paths=False,
        )

        self.assertNotIn(_PRIVATE_CHECKOUT, " ".join(redacted))
        # The option name is not private, and it is what makes a redacted argv
        # readable; only its value is replaced.
        self.assertEqual(redacted[5], f"--repo-path={lane_status.LOCAL_PATH_REDACTION}")
        self.assertEqual(redacted[7], lane_status.LOCAL_PATH_REDACTION)
        # Nothing that is not a path is touched.
        self.assertEqual(redacted[3], "--repo=a/b")
        self.assertEqual(redacted[4], "--port=5333")
        self.assertEqual(redacted[8], "--record-events")

    def test_a_home_relative_equals_form_path_is_redacted_too(self) -> None:
        redacted = board_service.redact_arguments(
            ["--repo-path=~/private-checkout", "--log-dir=~/logs"], show_local_paths=False
        )

        self.assertEqual(
            redacted,
            [
                f"--repo-path={lane_status.LOCAL_PATH_REDACTION}",
                f"--log-dir={lane_status.LOCAL_PATH_REDACTION}",
            ],
        )

    def test_show_local_paths_returns_equals_form_arguments_verbatim(self) -> None:
        arguments = ["--repo=a/b", f"--repo-path={_PRIVATE_CHECKOUT}"]

        self.assertEqual(
            board_service.redact_arguments(arguments, show_local_paths=True), arguments
        )

    def test_a_launchctl_diagnostic_keeps_its_reason_and_loses_its_paths(self) -> None:
        # `launchctl` names the definition file it could not load. Redacting the
        # whole string would throw away the only part an operator can act on, so
        # the reason stays and the path-shaped runs go.
        diagnostic = (
            "launchctl bootstrap failed: Load failed: 5: Input/output error while reading "
            f"{_PRIVATE_CHECKOUT}/Library/LaunchAgents/ai.codemower.board.5332.plist"
        )

        redacted = board_service.redact_diagnostic(diagnostic, show_local_paths=False)

        self.assertNotIn(_PRIVATE_CHECKOUT, redacted)
        self.assertIn(lane_status.LOCAL_PATH_REDACTION, redacted)
        # The failure is still readable, including the slash inside "Input/output":
        # a slash that continues a word does not begin a path.
        self.assertIn("Load failed: 5: Input/output error", redacted)
        self.assertIn("launchctl bootstrap failed", redacted)

    def test_a_home_relative_diagnostic_path_is_redacted_too(self) -> None:
        redacted = board_service.redact_diagnostic(
            "launchctl bootout failed: ~/Library/LaunchAgents/ai.codemower.board.5332.plist: "
            "No such process",
            show_local_paths=False,
        )

        self.assertNotIn("~/Library", redacted)
        self.assertIn("No such process", redacted)

    def test_a_repository_slug_is_not_mistaken_for_a_path(self) -> None:
        # `owner/repo` and `http://host/path` both carry slashes and neither is
        # a local path; over-redacting them would make diagnostics unreadable.
        text = "the definition serves codemower-ai/code-mower via http://127.0.0.1:5332/health"

        self.assertEqual(board_service.redact_diagnostic(text, show_local_paths=False), text)

    def test_show_local_paths_returns_a_diagnostic_verbatim(self) -> None:
        diagnostic = f"launchctl bootstrap failed: {_PRIVATE_CHECKOUT}/x.plist"

        self.assertEqual(
            board_service.redact_diagnostic(diagnostic, show_local_paths=True), diagnostic
        )

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

    def test_status_redacts_a_definition_written_in_equals_form(self) -> None:
        # A definition this lane rendered always uses the standalone spelling,
        # but `binding_from_arguments` accepts the joined one, so a definition
        # installed by hand or by an older build is a service status has to
        # describe -- without publishing the checkout it names.
        spec = self.spec()
        self.install(spec)
        joined = _joined_arguments(spec.arguments)
        path = self.root / f"{spec.label}.plist"
        data = plistlib.loads(path.read_bytes())
        data["ProgramArguments"] = joined
        path.write_bytes(plistlib.dumps(data))
        # launchd is the authority on a running job's argv, so the live
        # arguments the gate reports carry the joined spelling as well.
        self.host.relaunch_on(spec.label, joined)

        redacted = board_service.service_status(
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )

        self.assertNotIn(str(self.checkout), json.dumps(redacted))
        self.assertNotIn(str(self.checkout), board_service.render_status_text(redacted))
        row = redacted["services"][0]
        self.assertTrue(row["arguments_redacted"])
        self.assertIn(f"--repo-path={lane_status.LOCAL_PATH_REDACTION}", row["arguments"])
        self.assertIn("--repo=codemower-ai/code-mower", row["arguments"])

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

    def test_replace_reloads_a_definition_launchd_holds_on_stale_arguments(self) -> None:
        # The plist on disk is exactly the rendered one, but launchd registered
        # this job from an earlier version of it. `kickstart -k` re-execs the
        # registered argv and never rereads the file, so the shortcut would
        # bring the same stale binding back and fail the gate on every restart.
        # `--replace` has to reload the definition: bootout, then bootstrap.
        spec = self.spec()
        self.install(spec)
        stale = [item for item in spec.arguments if item != "--record-events"]
        self.host.relaunch_on(spec.label, stale)
        definition = (self.root / f"{spec.label}.plist").read_text(encoding="utf-8")
        self.host.calls.clear()

        payload = self.restart(spec, replace=True)

        self.assertEqual(payload["status"], "restarted")
        self.assertEqual(payload["delayed_health"]["state"], "pass")
        self.assertEqual(self.host.job_arguments[spec.label], list(spec.arguments))
        self.assertIn(["launchctl", "bootout", f"gui/501/{spec.label}"], self.host.calls)
        self.assertIn(
            ["launchctl", "bootstrap", "gui/501", str(self.root / f"{spec.label}.plist")],
            self.host.calls,
        )
        self.assertNotIn(["launchctl", "kickstart", "-k", f"gui/501/{spec.label}"], self.host.calls)
        # The definition was never rewritten: it already said the right thing.
        self.assertEqual((self.root / f"{spec.label}.plist").read_text(encoding="utf-8"), definition)
        self.assertEqual(sorted(item.name for item in self.root.iterdir()), [f"{spec.label}.plist"])

    def test_a_job_running_on_stale_arguments_is_refused_without_replace(self) -> None:
        spec = self.spec()
        self.install(spec)
        stale = [item for item in spec.arguments if item != "--record-events"]
        pid = self.host.relaunch_on(spec.label, stale)
        self.host.calls.clear()

        payload = self.restart(spec)

        self.assertEqual(payload["status"], "stale_arguments")
        self.assertIn("--replace", payload["message"])
        # Nothing was touched: not the job, not its registered arguments.
        self.assertEqual(self.host.loaded[spec.label], pid)
        self.assertEqual(self.host.job_arguments[spec.label], stale)
        self.assertNotIn(["launchctl", "kickstart", "-k", f"gui/501/{spec.label}"], self.host.calls)
        self.assertNotIn(["launchctl", "bootout", f"gui/501/{spec.label}"], self.host.calls)

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

    def test_a_failed_first_install_that_cannot_be_cleaned_up_is_a_failed_rollback(self) -> None:
        # Nothing was installed before, so rolling back means leaving nothing
        # behind. A definition that survives its failed apply starts the service
        # again at the next login, so reporting `apply_failed` -- and rendering
        # "Rollback: ok" -- would describe a host other than the one we left.
        self.host.bootstrap_failures.add("ai.codemower.board.5332")

        class KeepsDefinition(board_service.LaunchdProvider):
            def delete_definition(self, label: str) -> bool:
                return False

        payload = board_service.install_service(
            self.spec(),
            provider=KeepsDefinition(
                command_runner=self.host.run, root=self.root, uid=self.host.uid, platform="darwin"
            ),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
            settle_seconds=0.0,
            refresh_seconds=0.1,
            timeout_seconds=0.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(payload["status"], "rollback_failed")
        self.assertFalse(payload["rollback"]["ok"])
        self.assertFalse(payload["rollback"]["deleted"])
        self.assertIn("next login", payload["rollback"]["detail"])
        self.assertTrue((self.root / "ai.codemower.board.5332.plist").exists())
        self.assertIn("Rollback: failed", board_service.render_operation_text(payload))

    def test_a_failed_apply_reports_why_without_publishing_the_definition_path(self) -> None:
        # The failure reason is the actionable part and stays; the definition
        # path launchd names in it is local and goes. Without this the operation
        # message printed the checkout location in both text and JSON while
        # `repo_path` in the same payload said local paths were hidden.
        self.host.bootstrap_failures.add("ai.codemower.board.5332")

        payload = self.install(self.spec())

        self.assertEqual(payload["status"], "apply_failed")
        self.assertNotIn(str(self.root), payload["message"])
        self.assertNotIn(str(self.root), json.dumps(payload))
        self.assertNotIn(str(self.root), board_service.render_operation_text(payload))
        self.assertIn("Input/output error", payload["message"])
        self.assertIn(lane_status.LOCAL_PATH_REDACTION, payload["message"])

    def test_a_failed_rollback_detail_hides_the_definition_path_too(self) -> None:
        # `rollback.detail` is built from the same `launchctl` output and is
        # printed beside the message in both renderings, so it is sanitized by
        # the same chokepoint rather than separately.
        self.install(self.spec())
        self.host.bootstrap_failures.add("ai.codemower.board.5332")
        drifted = self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5332)

        payload = self.restart(drifted, replace=True)

        self.assertEqual(payload["status"], "rollback_failed")
        self.assertNotIn(str(self.root), json.dumps(payload))
        self.assertNotIn(str(self.other_checkout), json.dumps(payload))
        self.assertIn("rollback also failed", payload["message"])

    def test_show_local_paths_still_reveals_a_failure_diagnostic(self) -> None:
        self.host.bootstrap_failures.add("ai.codemower.board.5332")

        payload = self.install(self.spec(), show_local_paths=True)

        self.assertEqual(payload["status"], "apply_failed")
        self.assertIn(str(self.root), payload["message"])

    def test_a_definition_that_cannot_be_decoded_is_unreadable_not_a_traceback(self) -> None:
        spec = self.spec()
        self.install(spec)
        path = self.root / "ai.codemower.board.5332.plist"
        # Invalid UTF-8. Decoding raises `UnicodeDecodeError`, which is a
        # `ValueError` and so escapes an `OSError` handler entirely: enumeration
        # and everything built on it must report the definition as unreadable
        # rather than ending in a traceback.
        path.write_bytes(b"\xff\xfe not a plist \x00")

        service = self.host.provider().read_service(spec.label)
        services = self.host.provider().list_services()
        refused = self.restart(self.spec())

        self.assertFalse(service.readable)
        self.assertEqual(service.port, 5332)
        # Still installed, still supervised: runtime state survives a definition
        # that cannot be parsed, which is what ownership is decided on.
        self.assertTrue(service.loaded)
        self.assertEqual([item.label for item in services], [spec.label])
        self.assertEqual(refused["status"], "stale_arguments")
        self.assertEqual(path.read_bytes(), b"\xff\xfe not a plist \x00")

    def test_a_binary_definition_is_refused_rather_than_crashing_enumeration(self) -> None:
        spec = self.spec()
        self.install(spec)
        path = self.root / "ai.codemower.board.5332.plist"
        # A binary plist parses, but it is not UTF-8 text -- and the rollback
        # that protects a replacement restores the previous definition by
        # writing its text back, so a definition with no text has no
        # recoverable backup. It is refused like any other definition that
        # cannot be read, and `--replace` is the only takeover.
        path.write_bytes(plistlib.dumps(board_service.launchd_definition(spec), fmt=plistlib.FMT_BINARY))

        service = self.host.provider().read_service(spec.label)
        services = self.host.provider().list_services()
        refused = self.restart(spec)

        self.assertFalse(service.readable)
        self.assertEqual(service.port, 5332)
        self.assertEqual([item.label for item in services], [spec.label])
        self.assertEqual(refused["status"], "stale_arguments")
        self.assertFalse(refused["installed_readable"])

        taken_over = self.restart(spec, replace=True)

        self.assertEqual(taken_over["status"], "restarted")
        self.assertTrue(self.host.provider().read_service(spec.label).readable)

    def test_a_definition_that_names_another_label_never_removes_that_other_service(self) -> None:
        self.install(self.spec())
        other = self.spec(port=5333)
        self.install(other)
        path = self.root / "ai.codemower.board.5332.plist"
        # A definition selected by one filename but declaring another Label. The
        # filename is what selects it; the embedded Label is what launchd
        # registers the job as. Returning the embedded one would aim this
        # removal's bootout and delete at the *other* installed Board.
        hijacked = board_service.launchd_definition(self.spec())
        hijacked["Label"] = other.label
        path.write_bytes(plistlib.dumps(hijacked, sort_keys=True))

        service = self.host.provider().read_service("ai.codemower.board.5332")

        self.assertEqual(service.label, "ai.codemower.board.5332")
        self.assertFalse(service.readable)
        self.assertEqual(service.port, 5332)
        # Still installed and still supervised under the label that selected it.
        self.assertTrue(service.loaded)

        payload = board_service.remove_service(
            provider=self.host.provider(),
            port=5332,
            command_runner=self.host.run,
            settle_seconds=0.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(payload["label"], "ai.codemower.board.5332")
        self.assertFalse(path.exists())
        # The Board that was never selected is exactly where it was.
        self.assertTrue((self.root / "ai.codemower.board.5333.plist").exists())
        self.assertTrue(self.host.provider().read_service(other.label).readable)
        self.assertTrue(self.host.provider().read_service(other.label).loaded)

    def test_a_definition_whose_arguments_are_not_a_list_is_unreadable(self) -> None:
        spec = self.spec()
        self.install(spec)
        path = self.root / "ai.codemower.board.5332.plist"
        # Each of these is a syntactically valid plist. An integer or a boolean
        # raises `TypeError` on iteration, which is neither an `OSError` nor a
        # plist parse error and so escapes both handlers; a string is worse than
        # a crash, iterating into one argument per character and reading as a
        # plausible argv. None of them is an argument vector.
        for value in (5332, True, "code-mower board serve --port 5332"):
            with self.subTest(value=value):
                malformed = board_service.launchd_definition(spec)
                malformed["ProgramArguments"] = value
                path.write_bytes(plistlib.dumps(malformed, sort_keys=True))

                service = self.host.provider().read_service(spec.label)
                services = self.host.provider().list_services()
                refused = self.restart(spec)

                self.assertFalse(service.readable)
                self.assertEqual(service.arguments, ())
                self.assertEqual(service.port, 5332)
                self.assertTrue(service.loaded)
                self.assertEqual([item.label for item in services], [spec.label])
                self.assertEqual(refused["status"], "stale_arguments")

    def test_a_module_entry_point_carries_the_path_it_must_import_from(self) -> None:
        # The generated launchd environment keeps only what the definition
        # names, and the working directory is the served repository, so a `-m`
        # program started from a source checkout has no way to reach
        # `code_mower.cli` -- the keepalive job would fail and respawn forever.
        spec = board_service.build_spec(
            repo="codemower-ai/code-mower",
            repo_path=self.checkout,
            port=5332,
            program=("/usr/bin/python3", "-m", "code_mower.cli"),
            path_env="/usr/bin:/bin",
        )
        environment = board_service.launchd_definition(spec)["EnvironmentVariables"]

        self.assertEqual(environment["PYTHONPATH"], board_service.module_search_path())
        # Canonical, and resolved from the package itself rather than inherited
        # from whatever the installing shell happened to have.
        self.assertTrue(
            (Path(board_service.module_search_path()) / "code_mower" / "__init__.py").is_file()
        )
        # A console script carries its own interpreter and package location, so
        # it needs nothing from the environment and renders the bytes it always
        # did.
        console = board_service.launchd_definition(self.spec())["EnvironmentVariables"]
        self.assertNotIn("PYTHONPATH", console)

    def test_a_service_may_not_bind_a_host_board_serve_would_refuse(self) -> None:
        # `board serve` refuses a non-loopback host, so a service that names one
        # describes a Board that can never come up: launchd would restart the
        # failing process forever, and a replacement would stop the working
        # service first and leave the invalid definition installed once the
        # health window expired. The refusal has to happen before any mutation.
        for host in ("0.0.0.0", "::", "192.168.1.10", "example.com"):
            with self.subTest(host=host):
                with self.assertRaises(board_service.ServiceRequestError) as caught:
                    board_service.build_spec(
                        repo="codemower-ai/code-mower", repo_path=self.checkout, port=5332, host=host
                    )
                self.assertIn("loopback", str(caught.exception))
        self.assertEqual(list(self.root.iterdir()), [])
        for host in ("127.0.0.1", "127.0.0.53", "localhost", "::1"):
            with self.subTest(host=host):
                spec = board_service.build_spec(
                    repo="codemower-ai/code-mower", repo_path=self.checkout, port=5332, host=host
                )
                self.assertEqual(spec.host, host)
        # One rule: `board serve` and `board service` agree by construction.
        self.assertTrue(board._is_loopback("127.0.0.1"))
        self.assertFalse(board._is_loopback("0.0.0.0"))

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
        self.host.relaunch_on(
            "ai.codemower.board.5332",
            ["/usr/local/bin/code-mower", "board", "serve", "--repo", "codemower-ai/code-mower", "--port", "5332"],
        )

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

    def test_a_port_shared_with_another_process_fails_the_gate(self) -> None:
        # Two processes can hold one port on different addresses without either
        # failing to bind. The serving gate has to apply the bar `port_ownership`
        # applies before an apply -- every listener is this process -- or status
        # reports a validated binding that the next restart refuses outright.
        spec = self.spec()
        self.install(spec)
        managed_pid = self.host.loaded["ai.codemower.board.5332"]
        foreign_pid = self.host.add_foreign_listener(
            5332, command="/usr/local/bin/node /srv/dashboard/server.js", ppid=4242, address="[::1]"
        )
        self.host.listeners[5332] = managed_pid
        self.host.extra_listeners.append((5332, foreign_pid))

        binding = board_service.validate_binding(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )
        ownership = board_service.port_ownership(
            5332, provider=self.host.provider(), label=spec.label, command_runner=self.host.run
        )

        self.assertIn("binding.port", binding["failing_checks"])
        # The gate and the apply guard agree; the whole point of the finding.
        self.assertEqual(ownership["state"], "foreign")
        port_check = next(item for item in binding["checks"] if item["id"] == "binding.port")
        self.assertEqual(port_check["listener_count"], 2)
        self.assertIn("shared", port_check["message"])

    def test_a_port_that_cannot_be_inventoried_fails_the_gate(self) -> None:
        spec = self.spec()
        self.install(spec)
        self.host.listener_inventory_available = False

        binding = board_service.validate_binding(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )

        self.assertIn("binding.port", binding["failing_checks"])
        port_check = next(item for item in binding["checks"] if item["id"] == "binding.port")
        self.assertFalse(port_check["listener_inventory_available"])

    def test_a_stale_serving_version_fails_the_gate(self) -> None:
        spec = self.spec()
        self.install(spec)
        self.host.identities[5332]["board"] = {
            "version": board.board_version_payload() | {"serving_version": "0.0.1", "restart_recommended": True}
        }

        binding = board_service.validate_binding(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )

        self.assertIn("binding.serving_version", binding["failing_checks"])

    def test_a_source_checkout_board_passes_the_version_checks(self) -> None:
        # The supported module entry point runs from a checkout with no
        # distribution metadata. `board_version_payload()` reports an empty
        # installed version there, and the gate has to accept its own side
        # having none either rather than demanding the imported version.
        with _without_distribution_metadata():
            spec = self.spec()
            payload = self.install(spec)
            binding = board_service.validate_binding(
                spec,
                provider=self.host.provider(),
                command_runner=self.host.run,
                identity_probe=self.host.identity_probe,
            )

        self.assertEqual(payload["status"], "installed")
        self.assertEqual(binding["failing_checks"], [])
        installed_check = next(item for item in binding["checks"] if item["id"] == "binding.installed_version")
        self.assertTrue(installed_check["source_checkout"])
        self.assertEqual(installed_check["installed_version"], "")
        serving_check = next(item for item in binding["checks"] if item["id"] == "binding.serving_version")
        self.assertEqual(serving_check["serving_version"], CODE_MOWER_VERSION)

    def test_an_installed_distribution_board_passes_the_version_checks(self) -> None:
        with _with_distribution_metadata(CODE_MOWER_VERSION):
            spec = self.spec()
            payload = self.install(spec)
            binding = board_service.validate_binding(
                spec,
                provider=self.host.provider(),
                command_runner=self.host.run,
                identity_probe=self.host.identity_probe,
            )

        self.assertEqual(payload["status"], "installed")
        self.assertEqual(binding["failing_checks"], [])
        installed_check = next(item for item in binding["checks"] if item["id"] == "binding.installed_version")
        self.assertFalse(installed_check["source_checkout"])
        self.assertEqual(installed_check["installed_version"], CODE_MOWER_VERSION)

    def test_an_installed_distribution_that_disagrees_fails_the_gate(self) -> None:
        # Exact comparison is still what installed-package mode owes: a Board
        # serving some other installation of Code Mower is not this binding.
        with _with_distribution_metadata(CODE_MOWER_VERSION):
            spec = self.spec()
            self.install(spec)
            self.host.identities[5332]["board"] = {
                "version": board.board_version_payload() | {"installed_version": "0.0.1", "restart_recommended": True}
            }
            binding = board_service.validate_binding(
                spec,
                provider=self.host.provider(),
                command_runner=self.host.run,
                identity_probe=self.host.identity_probe,
            )

        self.assertIn("binding.installed_version", binding["failing_checks"])
        self.assertIn("binding.serving_version", binding["failing_checks"])

    def test_a_source_checkout_refuses_a_board_that_claims_an_installation(self) -> None:
        # Accepting an empty installed version is the source-checkout contract,
        # not a blanket pass: a Board that names a distribution this checkout
        # does not have is serving other code.
        with _without_distribution_metadata():
            spec = self.spec()
            self.install(spec)
            self.host.identities[5332]["board"] = {
                "version": board.board_version_payload() | {"installed_version": "9.9.9"}
            }
            binding = board_service.validate_binding(
                spec,
                provider=self.host.provider(),
                command_runner=self.host.run,
                identity_probe=self.host.identity_probe,
            )

        self.assertIn("binding.installed_version", binding["failing_checks"])

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

    def test_install_creates_the_log_directories_launchd_must_open(self) -> None:
        # A fresh checkout has no `.code-mower/board/logs`. launchd will not
        # start a job whose StandardOutPath cannot be opened, so the apply has
        # to create both parents itself.
        logs = self.checkout / ".code-mower" / "board" / "logs"
        self.assertFalse(logs.exists())

        payload = self.install(self.spec())

        self.assertEqual(payload["status"], "installed")
        self.assertTrue(logs.is_dir())
        data = plistlib.loads((self.root / "ai.codemower.board.5332.plist").read_bytes())
        self.assertTrue(Path(data["StandardOutPath"]).parent.is_dir())
        self.assertTrue(Path(data["StandardErrorPath"]).parent.is_dir())

    def test_an_unusable_log_directory_is_reported_before_the_running_board_is_stopped(self) -> None:
        self.install(self.spec())
        serving_pid = self.host.loaded["ai.codemower.board.5332"]
        blocked = self.tmp / "blocked"
        blocked.mkdir()
        # A regular file where the log directory's parent has to go.
        (blocked / ".code-mower").write_text("not a directory", encoding="utf-8")
        self.host.origins[str(blocked)] = "git@github.com:codemower-ai/code-mower.git"

        payload = self.restart(self.spec(repo_path=blocked), replace=True)

        self.assertEqual(payload["status"], "apply_failed")
        self.assertIn("log directory", payload["message"])
        # The Board that was serving is still the Board that is serving.
        self.assertEqual(self.host.loaded["ai.codemower.board.5332"], serving_pid)
        self.assertEqual(self.host.identities[5332]["repo"], "codemower-ai/code-mower")

    def test_a_replacement_is_refused_while_the_old_job_is_still_loaded(self) -> None:
        # The definition on disk is the only description of the job launchd
        # holds, and the replacement swaps it atomically. Writing it over a job
        # that could not be unloaded loses the original contents: the bootstrap
        # then fails because the label is occupied, and rollback preserves the
        # replacement rather than an original that is by then gone.
        spec = self.spec()
        self.install(spec)
        original = (self.root / f"{spec.label}.plist").read_text(encoding="utf-8")
        serving_pid = self.host.loaded[spec.label]
        self.host.bootout_failures.add(spec.label)

        payload = self.restart(self.spec(record_events=False), replace=True)

        self.assertEqual(payload["status"], "unload_failed")
        self.assertIn("left exactly as it was", payload["message"])
        # Nothing on the host moved: same definition, same job, same listener.
        self.assertEqual((self.root / f"{spec.label}.plist").read_text(encoding="utf-8"), original)
        self.assertEqual(self.host.loaded[spec.label], serving_pid)
        self.assertEqual(self.host.listeners[5332], serving_pid)
        self.assertEqual(self.host.job_arguments[spec.label], list(spec.arguments))
        # And the service is still described by its own definition, so status,
        # remove and the board stop guard all still reach it.
        installed = self.host.provider().read_service(spec.label)
        self.assertEqual(installed.digest, board_service.definition_digest(board_service.render_definition(spec)))
        self.assertTrue(installed.loaded)

    def test_a_replacement_proceeds_when_the_old_job_is_confirmed_absent(self) -> None:
        # `bootout` failing because there is no such job is not the same fact as
        # failing while launchd still holds it. A definition whose job was
        # already booted out is replaced normally.
        spec = self.spec()
        self.install(spec)
        self.host.provider().bootout(spec.label)
        changed = self.spec(record_events=False)

        payload = self.restart(changed, replace=True)

        self.assertEqual(payload["status"], "restarted")
        self.assertEqual(self.host.job_arguments[spec.label], list(changed.arguments))

    def test_a_console_script_reported_with_its_interpreter_is_still_healthy(self) -> None:
        spec = self.spec()
        self.install(spec)
        definition_argv = list(self.host.job_arguments["ai.codemower.board.5332"])
        # What a `#!`-headed console script is actually exec'd as: the
        # interpreter, then the script, then the script's own arguments.
        self.host.relaunch_on(
            "ai.codemower.board.5332", ["/usr/local/bin/python3.12", *definition_argv]
        )

        binding = board_service.validate_binding(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )

        self.assertEqual(binding["failing_checks"], [])
        check = next(item for item in binding["checks"] if item["id"] == "process.arguments")
        self.assertTrue(check["interpreter_prefix_normalized"])

    def test_an_interpreter_running_another_script_still_fails_the_gate(self) -> None:
        # Folding away the interpreter prefix narrows a false failure; it must
        # not turn a genuinely different program into a match.
        spec = self.spec()
        self.install(spec)
        definition_argv = list(self.host.job_arguments["ai.codemower.board.5332"])
        self.host.relaunch_on(
            "ai.codemower.board.5332",
            ["/usr/local/bin/python3.12", "/usr/local/bin/some-other-tool", *definition_argv[1:]],
        )

        binding = board_service.validate_binding(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )

        self.assertIn("process.arguments", binding["failing_checks"])

    def test_a_listener_that_is_not_a_board_still_stops_the_apply(self) -> None:
        # A Node server is not a Board, but it holds 5332 just as firmly. The
        # Board-shaped inventory would call this port free.
        self.host.add_foreign_listener(5332, command="/usr/local/bin/node /srv/dashboard/server.js", ppid=4242)

        payload = self.install(self.spec())

        self.assertEqual(payload["status"], "port_conflict")
        self.assertEqual(list(self.root.glob("*.plist")), [])
        self.assertEqual(self.host.loaded, {})

    def test_a_listener_on_a_nondefault_port_still_stops_the_apply(self) -> None:
        self.host.add_foreign_listener(5999, command="/usr/bin/python3 -m http.server 5999", ppid=4242)

        payload = self.install(self.spec(port=5999))

        self.assertEqual(payload["status"], "port_conflict")
        self.assertEqual(list(self.root.glob("*.plist")), [])

    def test_a_failed_replacement_write_reloads_the_service_it_stopped(self) -> None:
        self.install(self.spec())
        before = (self.root / "ai.codemower.board.5332.plist").read_text(encoding="utf-8")
        drifted = self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5332)

        payload = self._restart_with(self._refusing_writes(), drifted, replace=True)

        self.assertEqual(payload["status"], "apply_failed")
        self.assertTrue(payload["rollback"]["ok"])
        self.assertTrue(payload["rollback"]["restored"])
        # The atomic swap never happened, so the definition is untouched -- and
        # the Board it describes is serving again rather than left stopped.
        self.assertEqual((self.root / "ai.codemower.board.5332.plist").read_text(encoding="utf-8"), before)
        self.assertIn("ai.codemower.board.5332", self.host.loaded)
        self.assertEqual(self.host.identities[5332]["repo"], "codemower-ai/code-mower")

    def test_a_failed_write_that_cannot_be_reloaded_says_so(self) -> None:
        self.install(self.spec())
        self.host.bootstrap_failures.add("ai.codemower.board.5332")
        drifted = self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5332)

        payload = self._restart_with(self._refusing_writes(), drifted, replace=True)

        self.assertEqual(payload["status"], "rollback_failed")
        self.assertIn("rollback also failed", payload["message"])
        self.assertFalse(payload["rollback"]["restored"])

    def test_a_partially_registered_replacement_is_unloaded_before_restoring(self) -> None:
        # A bootstrap can register the job and *then* give up waiting for it.
        # Restoring the previous definition on top of a replacement launchd
        # still holds would leave launchd supervising the replacement while the
        # definition on disk describes the service it replaced, and the
        # restoring bootstrap would fail because the label is already loaded.
        self.install(self.spec())
        before = (self.root / "ai.codemower.board.5332.plist").read_text(encoding="utf-8")
        drifted = self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5332)

        class RegistersThenGivesUp(board_service.LaunchdProvider):
            gave_up = False

            def bootstrap(self, label: str) -> tuple[bool, str]:
                ok, detail = super().bootstrap(label)
                if not self.gave_up:
                    self.gave_up = True
                    return False, "timed out waiting for the job to answer"
                return ok, detail

        payload = self._restart_with(
            RegistersThenGivesUp(
                command_runner=self.host.run, root=self.root, uid=self.host.uid, platform="darwin"
            ),
            drifted,
            replace=True,
        )

        self.assertEqual(payload["status"], "apply_failed")
        self.assertTrue(payload["rollback"]["ok"])
        self.assertTrue(payload["rollback"]["restored"])
        # On disk and in launchd, what is left is the previous service -- not a
        # replacement running against a definition that no longer describes it.
        self.assertEqual((self.root / "ai.codemower.board.5332.plist").read_text(encoding="utf-8"), before)
        self.assertIn("ai.codemower.board.5332", self.host.loaded)
        self.assertEqual(self.host.identities[5332]["repo"], "codemower-ai/code-mower")

    def test_a_replacement_that_cannot_be_unloaded_leaves_the_definition_alone(self) -> None:
        # The other half: if the replacement cannot be confirmed unloaded, the
        # previous definition is not written under it. Overwriting the
        # definition of a job launchd still supervises would leave neither the
        # replacement nor the previous service described by what is on disk.
        self.install(self.spec())
        drifted = self.spec(repo="codemower-ai/private-repo", repo_path=self.other_checkout, port=5332)
        replacement_text = board_service.render_definition(drifted)

        class WillNotUnload(board_service.LaunchdProvider):
            booted_out = False

            def bootstrap(self, label: str) -> tuple[bool, str]:
                super().bootstrap(label)
                return False, "timed out waiting for the job to answer"

            def bootout(self, label: str) -> tuple[bool, str]:
                if self.booted_out:
                    return False, "Bootout failed: 125: Unknown error"
                # The apply's own bootout of the previous service works; the
                # rollback's attempt on the replacement is what fails.
                self.booted_out = True
                return super().bootout(label)

        payload = self._restart_with(
            WillNotUnload(
                command_runner=self.host.run, root=self.root, uid=self.host.uid, platform="darwin"
            ),
            drifted,
            replace=True,
        )

        self.assertEqual(payload["status"], "rollback_failed")
        self.assertFalse(payload["rollback"]["ok"])
        self.assertFalse(payload["rollback"]["restored"])
        self.assertIn("could not be unloaded", payload["rollback"]["detail"])
        self.assertEqual(
            (self.root / "ai.codemower.board.5332.plist").read_text(encoding="utf-8"),
            replacement_text,
        )
        self.assertIn("Rollback: failed", board_service.render_operation_text(payload))

    def test_a_rollback_that_cannot_unload_the_job_keeps_its_definition(self) -> None:
        # launchd registered the job and then bootstrap gave up waiting for it,
        # so the apply failed with a job still supervised -- and the rollback's
        # bootout failed too. The definition is the only handle `board service
        # status`, `remove` and the `board stop` keepalive guard have on that
        # job, because all three discover services by scanning definitions.
        # Deleting it would strand a running, self-restarting Board outside the
        # inventory entirely, so it is kept until the job is confirmed gone.
        class StrandsTheJob(board_service.LaunchdProvider):
            def bootstrap(self, label: str) -> tuple[bool, str]:
                super().bootstrap(label)
                return False, "timed out waiting for the job to answer"

            def bootout(self, label: str) -> tuple[bool, str]:
                return False, "Bootout failed: 125: Unknown error"

        payload = board_service.install_service(
            self.spec(),
            provider=StrandsTheJob(
                command_runner=self.host.run, root=self.root, uid=self.host.uid, platform="darwin"
            ),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
            settle_seconds=0.0,
            refresh_seconds=0.1,
            timeout_seconds=0.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(payload["status"], "rollback_failed")
        self.assertFalse(payload["rollback"]["ok"])
        self.assertFalse(payload["rollback"]["deleted"])
        self.assertIn("still-loaded job stays discoverable", payload["rollback"]["detail"])
        self.assertIn("ai.codemower.board.5332", self.host.loaded)
        self.assertTrue((self.root / "ai.codemower.board.5332.plist").exists())
        # The point of keeping it: the stranded job is still manageable.
        self.assertEqual(
            [item.label for item in self.host.provider().list_services()],
            ["ai.codemower.board.5332"],
        )
        self.assertIn("Rollback: failed", board_service.render_operation_text(payload))

    def test_an_identity_probe_brackets_an_ipv6_address(self) -> None:
        # `http://::1:5332/api/identity` names something other than the service
        # on `::1`: the colons of the address run into the port. Board binds
        # IPv6 and `build_spec` accepts `--host ::1`, so an unbracketed probe
        # would fail against a perfectly healthy service and exhaust the whole
        # health window before install or restart reported failure.
        seen: list[str] = []

        class Answer:
            def __enter__(self) -> "Answer":
                return self

            def __exit__(self, *_exc: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"repo": "codemower-ai/code-mower"}'

        def urlopen(url: str, timeout: float = 0.0) -> Answer:
            seen.append(url)
            return Answer()

        with mock.patch.object(board_service.urllib.request, "urlopen", urlopen):
            ipv6 = board_service.probe_identity("::1", 5332)
            board_service.probe_identity("127.0.0.1", 5332)

        self.assertEqual(seen, ["http://[::1]:5332/api/identity", "http://127.0.0.1:5332/api/identity"])
        self.assertEqual(ipv6["repo"], "codemower-ai/code-mower")

    def test_an_unreadable_definition_is_not_permission_to_take_over(self) -> None:
        self.install(self.spec())
        path = self.root / "ai.codemower.board.5332.plist"
        path.write_text("this is not a plist", encoding="utf-8")

        refused_restart = self.restart(self.spec())
        refused_install = self.install(self.spec())

        self.assertEqual(refused_restart["status"], "stale_arguments")
        self.assertFalse(refused_restart["installed_readable"])
        self.assertEqual(refused_install["status"], "stale_arguments")
        # Nothing overwritten -- the contents could not be read, so a rollback
        # could not have restored them -- and nothing unloaded.
        self.assertEqual(path.read_text(encoding="utf-8"), "this is not a plist")
        self.assertIn("ai.codemower.board.5332", self.host.loaded)

    def test_an_explicit_replace_takes_over_an_unreadable_definition(self) -> None:
        self.install(self.spec())
        (self.root / "ai.codemower.board.5332.plist").write_text("this is not a plist", encoding="utf-8")

        payload = self.restart(self.spec(), replace=True)

        self.assertEqual(payload["status"], "restarted")
        service = self.host.provider().read_service("ai.codemower.board.5332")
        self.assertTrue(service.readable)
        self.assertEqual(service.digest, payload["digest"])

    def test_restart_loads_a_valid_definition_whose_job_was_unloaded(self) -> None:
        spec = self.spec()
        self.install(spec)
        # A logout, or a manual `launchctl bootout`: the job is gone, but the
        # definition it was applied from is still exactly right. `kickstart`
        # cannot load an unregistered job, so restart has to bootstrap it.
        self.host.provider().bootout(spec.label)
        self.assertEqual(self.host.loaded, {})

        payload = self.restart(spec)

        self.assertEqual(payload["status"], "restarted")
        self.assertEqual(payload["delayed_health"]["state"], "pass")
        self.assertIn("ai.codemower.board.5332", self.host.loaded)
        self.assertIn(
            ["launchctl", "bootstrap", "gui/501", str(self.root / "ai.codemower.board.5332.plist")],
            self.host.calls,
        )

    def test_removal_that_cannot_delete_the_definition_is_not_reported_as_removed(self) -> None:
        self.install(self.spec())

        class KeepsDefinition(board_service.LaunchdProvider):
            def delete_definition(self, label: str) -> bool:
                return False

        payload = board_service.remove_service(
            provider=KeepsDefinition(
                command_runner=self.host.run, root=self.root, uid=self.host.uid, platform="darwin"
            ),
            port=5332,
            command_runner=self.host.run,
            settle_seconds=0.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(payload["status"], "remove_incomplete")
        self.assertTrue(payload["definition_present"])
        self.assertFalse(payload["definition_deleted"])
        self.assertIn("next login", payload["message"])
        self.assertTrue((self.root / "ai.codemower.board.5332.plist").exists())

    def test_a_checkout_path_containing_a_space_still_validates(self) -> None:
        # `ps -o command=` renders an argv as one unquoted line, so a path with
        # a space in it cannot be split back into the arguments it came from --
        # splitting would turn "My Checkout" into two arguments and fail a
        # perfectly healthy service. launchd reports the boundaries.
        spaced = self.tmp / "My Checkout"
        spaced.mkdir()
        self.host.origins[str(spaced)] = "git@github.com:codemower-ai/code-mower.git"
        spec = self.spec(repo_path=spaced)

        payload = self.install(spec)

        self.assertEqual(payload["status"], "installed")
        self.assertEqual(payload["delayed_health"]["binding"]["failing_checks"], [])
        # One argument, not the two a `ps` line would have been split into.
        self.assertIn(str(spaced), self.host.job_arguments["ai.codemower.board.5332"])
        self.assertNotIn(str(spaced), json.dumps(payload))

    def test_arguments_launchd_does_not_report_fail_the_gate(self) -> None:
        # No argument list is not the same fact as a matching one; a gate that
        # cannot see the argv must not pass it.
        spec = self.spec()
        self.install(spec)
        self.host.job_arguments.pop("ai.codemower.board.5332")

        binding = board_service.validate_binding(
            spec,
            provider=self.host.provider(),
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
        )

        self.assertIn("process.arguments", binding["failing_checks"])
        check = next(item for item in binding["checks"] if item["id"] == "process.arguments")
        self.assertFalse(check["arguments_reported"])

    def test_a_stopped_service_does_not_lend_its_port_to_an_unrelated_process(self) -> None:
        # The definition stays installed when the job stops, and an unrelated
        # process is free to take the port it vacated. Ownership is the
        # supervised pid, not a definition that merely names the port.
        spec = self.spec()
        self.install(spec)
        self.host.provider().bootout(spec.label)
        self.host.add_foreign_listener(5332, command="/usr/local/bin/node /srv/dashboard/server.js", ppid=4242)

        ownership = board_service.port_ownership(
            5332, provider=self.host.provider(), label=spec.label, command_runner=self.host.run
        )
        payload = self.restart(spec)

        self.assertEqual(ownership["state"], "foreign")
        self.assertEqual(payload["status"], "port_conflict")
        self.assertEqual(self.host.loaded, {})

    def test_a_second_listener_on_the_port_is_never_hidden_by_the_first(self) -> None:
        # One owned listener does not make the port ours: every listener has to
        # clear the bar, or the keepalive job fights whatever else is bound.
        spec = self.spec()
        self.install(spec)
        managed_pid = self.host.loaded["ai.codemower.board.5332"]
        foreign_pid = self.host.add_foreign_listener(
            5332, command="/usr/local/bin/node /srv/dashboard/server.js", ppid=4242
        )
        self.host.listeners[5332] = managed_pid
        self.host.extra_listeners.append((5332, foreign_pid))

        ownership = board_service.port_ownership(
            5332, provider=self.host.provider(), label=spec.label, command_runner=self.host.run
        )

        self.assertEqual(ownership["state"], "foreign")
        self.assertEqual(ownership["pid"], foreign_pid)

    def test_an_inventory_that_cannot_be_taken_is_not_an_empty_one(self) -> None:
        # `lsof` exiting 1 is an answer -- nothing holds the port -- and `lsof`
        # that cannot run at all, with no `ss` to fall back to, is not. The two
        # arrive as the same empty list, so the inventory has to carry which
        # one it is or "free" gets inferred from "we could not look".
        empty = board_service.port_listener_inventory(5332, self.host.run)
        self.host.listener_inventory_available = False
        unknown = board_service.port_listener_inventory(5332, self.host.run)

        self.assertEqual((empty["available"], empty["listeners"]), (True, []))
        self.assertEqual((unknown["available"], unknown["listeners"]), (False, []))

    def test_a_port_that_cannot_be_checked_is_never_called_free(self) -> None:
        # Nothing is installed and nothing is listening, so the only thing
        # standing between this install and a keepalive job bootstrapped into a
        # conflict it retries forever is refusing to guess at occupancy.
        self.host.listener_inventory_available = False

        ownership = board_service.port_ownership(
            5332, provider=self.host.provider(), label="ai.codemower.board.5332", command_runner=self.host.run
        )
        payload = self.install(self.spec())

        self.assertEqual(ownership["state"], "unknown")
        self.assertEqual(payload["status"], "listener_inventory_unavailable")
        # Fails closed: no definition written, no job bootstrapped.
        self.assertEqual(self.host.loaded, {})
        self.assertFalse((self.root / "ai.codemower.board.5332.plist").exists())

    def test_a_restart_refuses_while_port_occupancy_is_unknown(self) -> None:
        spec = self.spec()
        self.install(spec)
        installed = (self.root / "ai.codemower.board.5332.plist").read_bytes()
        loaded = dict(self.host.loaded)
        self.host.listener_inventory_available = False

        payload = self.restart(spec, replace=True)

        self.assertEqual(payload["status"], "listener_inventory_unavailable")
        self.assertEqual(self.host.loaded, loaded)
        self.assertEqual((self.root / "ai.codemower.board.5332.plist").read_bytes(), installed)

    def test_removal_does_not_claim_a_released_port_it_could_not_check(self) -> None:
        # The definition really was deleted and that is reported. "Released its
        # port" is a second claim resting on an inventory that was never taken.
        self.install(self.spec())
        self.host.listener_inventory_available = False

        payload = board_service.remove_service(
            provider=self.host.provider(),
            port=5332,
            command_runner=self.host.run,
            settle_seconds=0.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(payload["status"], "remove_incomplete")
        self.assertTrue(payload["definition_deleted"])
        self.assertFalse(payload["definition_present"])
        self.assertIn("could not be checked", payload["message"])

    def test_an_unreadable_definition_still_owns_the_port_it_is_serving(self) -> None:
        # Ownership is the supervised pid, and a definition that cannot be
        # parsed is still a job launchd supervises under our label. Reading the
        # runtime state only for parseable definitions would make our own
        # running Board look like somebody else's supervised process.
        spec = self.spec()
        self.install(spec)
        (self.root / "ai.codemower.board.5332.plist").write_text("this is not a plist", encoding="utf-8")

        service = self.host.provider().read_service(spec.label)
        ownership = board_service.port_ownership(
            5332, provider=self.host.provider(), label=spec.label, command_runner=self.host.run
        )

        self.assertFalse(service.readable)
        self.assertTrue(service.loaded)
        self.assertEqual(ownership["state"], "managed_self")

    def test_removal_that_cannot_unload_the_job_keeps_the_definition(self) -> None:
        # Deleting the definition of a job launchd still holds would strand a
        # running, self-restarting service: discovery scans definition files, so
        # status, remove and the `board stop` keepalive guard would all lose it.
        self.install(self.spec())

        class RefusesBootout(board_service.LaunchdProvider):
            def bootout(self, label: str) -> tuple[bool, str]:
                return False, "launchctl bootout failed: Operation not permitted"

        payload = board_service.remove_service(
            provider=RefusesBootout(
                command_runner=self.host.run, root=self.root, uid=self.host.uid, platform="darwin"
            ),
            port=5332,
            command_runner=self.host.run,
            settle_seconds=0.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(payload["status"], "remove_incomplete")
        self.assertTrue(payload["definition_present"])
        self.assertFalse(payload["definition_deleted"])
        self.assertTrue((self.root / "ai.codemower.board.5332.plist").exists())
        # Still discoverable, and still running.
        self.assertIn("ai.codemower.board.5332", self.host.loaded)
        resolved = board_service.resolve_service(provider=self.host.provider(), port=5332)
        self.assertEqual(resolved["status"], "ok")

    def test_removal_whose_job_is_already_gone_still_deletes_the_definition(self) -> None:
        # The unload reported a failure, but launchd does not hold the job:
        # deleting the definition is safe, and saying `removed` is not honest.
        self.install(self.spec())
        self.host.provider().bootout("ai.codemower.board.5332")

        class NoisyBootout(board_service.LaunchdProvider):
            def bootout(self, label: str) -> tuple[bool, str]:
                return False, "launchctl bootout failed: exit 3"

        payload = board_service.remove_service(
            provider=NoisyBootout(
                command_runner=self.host.run, root=self.root, uid=self.host.uid, platform="darwin"
            ),
            port=5332,
            command_runner=self.host.run,
            settle_seconds=0.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(payload["status"], "remove_incomplete")
        self.assertTrue(payload["definition_deleted"])
        self.assertFalse((self.root / "ai.codemower.board.5332.plist").exists())

    def test_a_runtime_query_that_fails_is_never_read_as_a_missing_job(self) -> None:
        self.install(self.spec())

        def unreachable_launchd(argv: list[str]) -> subprocess.CompletedProcess[str]:
            # Not "could not find service": launchd said nothing about whether
            # it holds this job. Treating that as absence is what lets a failed
            # bootout be followed by a delete, stranding a keepalive service
            # with nothing left to manage it by.
            if argv[:2] == ["launchctl", "print"]:
                return _completed("", returncode=1, stderr="Could not connect to launchd\n")
            return self.host.run(argv)

        class RefusesBootout(board_service.LaunchdProvider):
            def bootout(self, label: str) -> tuple[bool, str]:
                return False, "launchctl bootout failed: Operation not permitted"

        provider = RefusesBootout(
            command_runner=unreachable_launchd, root=self.root, uid=self.host.uid, platform="darwin"
        )
        payload = board_service.remove_service(
            provider=provider,
            port=5332,
            command_runner=self.host.run,
            settle_seconds=0.0,
            sleeper=self.sleeper,
        )

        self.assertEqual(
            provider.runtime_state("ai.codemower.board.5332"), (board_service.JOB_UNKNOWN, None)
        )
        # The contrast the finding is about: a job launchd positively reports as
        # missing is absent, and only that releases the definition.
        self.assertEqual(
            self.host.provider().runtime_state("ai.codemower.board.9999"),
            (board_service.JOB_ABSENT, None),
        )
        self.assertEqual(payload["status"], "remove_incomplete")
        self.assertFalse(payload["definition_deleted"])
        self.assertTrue(payload["definition_present"])
        self.assertTrue((self.root / "ai.codemower.board.5332.plist").exists())

    def test_remove_reports_a_port_reclaimed_by_a_process_that_is_not_a_board(self) -> None:
        self.install(self.spec())

        def sticky_sleeper(_seconds: float) -> None:
            self.host.add_foreign_listener(5332, command="/usr/local/bin/node /srv/dashboard/server.js", ppid=4242)

        payload = board_service.remove_service(
            provider=self.host.provider(),
            port=5332,
            command_runner=self.host.run,
            settle_seconds=1.0,
            sleeper=sticky_sleeper,
        )

        self.assertEqual(payload["status"], "remove_incomplete")
        self.assertIn("still held", payload["message"])

    def _refusing_writes(self) -> board_service.LaunchdProvider:
        class RefusingWrites(board_service.LaunchdProvider):
            def write_definition(self, label: str, text: str) -> Path:
                raise PermissionError("read-only file system")

        return RefusingWrites(
            command_runner=self.host.run, root=self.root, uid=self.host.uid, platform="darwin"
        )

    def _restart_with(self, provider: object, spec: board_service.ServiceSpec, **kwargs: object) -> dict:
        return board_service.restart_service(
            spec,
            provider=provider,
            command_runner=self.host.run,
            identity_probe=self.host.identity_probe,
            settle_seconds=0.0,
            refresh_seconds=0.1,
            timeout_seconds=0.0,
            sleeper=self.sleeper,
            **kwargs,
        )

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

    def test_a_transient_board_on_a_stopped_services_port_is_not_managed(self) -> None:
        spec = self.spec()
        self.install(spec)
        # The job is booted out, but its definition stays installed -- and a
        # transient Board is then free to take the port it vacated. An installed
        # definition names a port; it does not prove who holds it. Calling this
        # listener managed would label it in `board list` with a service that is
        # not running it, and make `board stop --yes` refuse to stop a Board
        # nothing would restart.
        self.host.provider().bootout(spec.label)
        pid = self._transient_board(5332, "codemower-ai/code-mower", self.checkout)

        inventory = board.board_inventory_payload(
            command_runner=self.host.run,
            status_probe=None,
            service_probe=lambda: self.host.provider().list_services(),
        )
        rows = {row["port"]: row for row in inventory["boards"]}
        payload = self._stop(port=5332, yes=True)

        self.assertFalse(rows[5332]["managed"])
        self.assertEqual(rows[5332]["service_label"], "")
        self.assertIn("service=none (transient)", board.render_inventory_text(inventory))
        self.assertEqual(payload["status"], "stopped")
        self.assertEqual([entry[0] for entry in payload["_signalled"]], [pid])

    def test_a_repository_named_with_leading_punctuation_is_a_valid_selector(self) -> None:
        # `owner/.github` is a real GitHub repository name and Board serves it.
        # Imposing the *owner* naming rule on the repository component locked
        # those repositories out of the service lifecycle and this selector
        # while serving them worked.
        dotted = self.tmp / "dot-github"
        dotted.mkdir()
        self.host.origins[str(dotted)] = "git@github.com:codemower-ai/.github.git"
        installed = self.install(self.spec(repo="codemower-ai/.github", repo_path=dotted, port=5342))
        pid = self._transient_board(5344, "codemower-ai/.github", dotted)

        by_repo = self._stop(repo="codemower-ai/.github", port=5344, yes=True)
        traversal = self._stop(repo="codemower-ai/..", yes=True)

        self.assertEqual(installed["status"], "installed")
        self.assertEqual(installed["repo"], "codemower-ai/.github")
        self.assertEqual(by_repo["status"], "stopped")
        self.assertEqual([entry[0] for entry in by_repo["_signalled"]], [pid])
        # A component with no alphanumeric in it is path traversal, not a
        # repository, and this slug becomes a path component downstream.
        self.assertEqual(traversal["status"], "invalid_selector")
        self.assertEqual(traversal["_signalled"], [])

    def test_stop_refuses_a_port_whose_supervision_launchd_will_not_confirm(self) -> None:
        # `launchctl print` failing for a reason launchd does not characterise
        # as a missing job says nothing about whether it still supervises this
        # service. Reading that as "not loaded" would make the listener look
        # transient, so `board stop --yes` would signal a keepalive-managed
        # Board and report the port released while launchd restarted it.
        spec = self.spec()
        self.install(spec)

        def unreachable_launchd(argv: list[str]) -> subprocess.CompletedProcess[str]:
            if argv[:2] == ["launchctl", "print"]:
                return _completed("", returncode=1, stderr="Could not connect to launchd\n")
            return self.host.run(argv)

        provider = board_service.LaunchdProvider(
            command_runner=unreachable_launchd, root=self.root, uid=self.host.uid, platform="darwin"
        )
        service = provider.read_service(spec.label)
        stopped: list[tuple[int, int]] = []
        payload = board.stop_board(
            port=5332,
            yes=True,
            command_runner=self.host.run,
            killer=lambda pid, sig: stopped.append((pid, sig)),
            service_probe=lambda: provider.list_services(),
        )
        inventory = board.board_inventory_payload(
            command_runner=self.host.run,
            status_probe=None,
            service_probe=lambda: provider.list_services(),
        )

        self.assertEqual(service.load_state, board_service.JOB_UNKNOWN)
        self.assertFalse(service.loaded)
        self.assertEqual(payload["status"], "managed_service")
        self.assertEqual(stopped, [])
        self.assertEqual(payload["managed_service"]["supervision"], "unknown")
        self.assertIn("could not be asked", payload["message"])
        self.assertIn("ai.codemower.board.5332", self.host.loaded)
        # And the inventory says the same thing rather than asserting it is
        # supervised or calling it transient.
        rows = {row["port"]: row for row in inventory["boards"]}
        self.assertTrue(rows[5332]["managed"])
        self.assertEqual(rows[5332]["service_supervision"], "unknown")
        self.assertIn("supervision unconfirmed", board.render_inventory_text(inventory))

    def test_installed_services_survive_a_launchctl_that_cannot_be_probed(self) -> None:
        # `launchctl version` failing says nothing about what launchd holds: the
        # definitions are still installed and their jobs may still be running.
        # Answering "no managed services" would make every listener look
        # transient, so `board stop --yes` would signal a keepalive-managed
        # Board and report a port released that launchd reclaims immediately.
        spec = self.spec()
        self.install(spec)
        self.host.launchctl_reachable = False
        provider = self.host.provider()

        available, _why = provider.available()
        services = board_service.managed_services(provider=provider, command_runner=self.host.run)
        stopped: list[tuple[int, int]] = []
        payload = board.stop_board(
            port=5332,
            yes=True,
            command_runner=self.host.run,
            killer=lambda pid, sig: stopped.append((pid, sig)),
            service_probe=lambda: board_service.managed_services(
                provider=provider, command_runner=self.host.run
            ),
        )
        inventory = board.board_inventory_payload(
            command_runner=self.host.run,
            status_probe=None,
            service_probe=lambda: board_service.managed_services(
                provider=provider, command_runner=self.host.run
            ),
        )

        self.assertFalse(available)
        self.assertEqual([item.label for item in services], [spec.label])
        # Discovered, but claiming nothing about what launchd is supervising.
        self.assertEqual(services[0].load_state, board_service.JOB_UNKNOWN)
        self.assertFalse(services[0].loaded)
        self.assertIsNone(services[0].pid)
        self.assertEqual(payload["status"], "managed_service")
        self.assertEqual(payload["managed_service"]["supervision"], "unknown")
        self.assertEqual(stopped, [])
        self.assertIn(spec.label, self.host.loaded)
        rows = {row["port"]: row for row in inventory["boards"]}
        self.assertTrue(rows[5332]["managed"])
        self.assertEqual(rows[5332]["service_supervision"], "unknown")

    def test_a_platform_without_launchd_still_reports_no_managed_services(self) -> None:
        # The other half of the distinction: an unsupported platform has no
        # managed-service implementation at all, so there is nothing installed
        # to enumerate and a transient Board stays stoppable.
        pid = self._transient_board(5332, "codemower-ai/code-mower", self.checkout)
        provider = board_service.select_provider(platform="linux", command_runner=self.host.run)

        services = board_service.managed_services(provider=provider, command_runner=self.host.run)
        payload = self._stop(port=5332, yes=True)

        self.assertEqual(services, [])
        self.assertEqual(payload["status"], "stopped")
        self.assertEqual([entry[0] for entry in payload["_signalled"]], [pid])

    def test_a_supervised_listener_is_managed_even_on_a_port_its_definition_does_not_name(
        self,
    ) -> None:
        # An installed definition can name a port that is not the one launchd is
        # currently serving: the plist was edited, or the job was bootstrapped
        # from an earlier version of it. Finding the service only under its
        # on-disk port misses the listener it is actually supervising, so
        # `board list` calls it transient and `board stop --yes` signals a
        # process launchd restarts within moments.
        spec = self.spec()
        self.install(spec)
        supervised_pid = self.host.relaunch_on(spec.label, self.spec(port=5342).arguments)

        inventory = board.board_inventory_payload(
            command_runner=self.host.run,
            status_probe=None,
            service_probe=lambda: self.host.provider().list_services(),
        )
        rows = {row["port"]: row for row in inventory["boards"]}
        payload = self._stop(port=5342, yes=True)

        # launchd named this exact pid as the job it is running; no port on disk
        # can contradict that.
        self.assertEqual(self.host.loaded[spec.label], supervised_pid)
        self.assertTrue(rows[5342]["managed"])
        self.assertEqual(rows[5342]["service_label"], spec.label)
        self.assertEqual(rows[5342]["service_supervision"], "confirmed")
        self.assertEqual(payload["status"], "managed_service")
        self.assertEqual(payload["_signalled"], [])
        self.assertEqual(payload["managed_service"]["label"], spec.label)
        # The refusal names the port actually being served, and still points at
        # the selector `board service` resolves this definition by.
        self.assertIn("port 5342 is served by", payload["message"])
        self.assertIn("installed definition names port 5332", payload["message"])
        self.assertIn("board service remove --port 5332", payload["message"])
        self.assertIn(spec.label, self.host.loaded)

    def test_stop_exit_codes_separate_refusals_from_selector_errors(self) -> None:
        self.assertEqual(board._stop_exit_code("stopped"), 0)
        self.assertEqual(board._stop_exit_code("ambiguous_selector"), 2)
        self.assertEqual(board._stop_exit_code("selector_mismatch"), 2)
        self.assertEqual(board._stop_exit_code("managed_service"), 1)
        self.assertEqual(board._service_exit_code("installed"), 0)
        self.assertEqual(board._service_exit_code("ambiguous_repository"), 2)
        self.assertEqual(board._service_exit_code("stale_arguments"), 1)
