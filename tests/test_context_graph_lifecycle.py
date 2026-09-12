"""Offline lifecycle tests for the optional local repository graph (issue #913).

Every test here builds a real throwaway Git repository and runs the whole
lifecycle against it with an injected indexer. No graph package is installed,
imported, or required, and nothing leaves this machine: the provider seam is a
callable, so the parts this repository is responsible for -- what gets
materialized, what the manifest binds, how a generation is published, and when
a consumer must refuse one -- are all provable locally.

``NetworkIsolationTests`` is the one place a socket is opened at all. It binds a
listener on loopback in this process and proves a sandboxed child cannot reach
it, which is the only honest way to test a network boundary: an assertion about
proxy variables would have passed on code that had none.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from code_mower import context_graph_command as command
from code_mower import context_graph_lifecycle as lifecycle
from code_mower.context_contract import ContextError


PIN = lifecycle.GraphifyPin(
    distribution="graphifyy",
    version="0.9.58",
    wheel_sha256="a" * 64,
    options=("--no-network",),
)
NOW = datetime(2026, 3, 1, 9, 30, tzinfo=timezone.utc)


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(repository),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    ).stdout


def make_repository(root: Path) -> Path:
    """A small repository with a tracked file, an ignored file and a secret."""
    repository = root / "checkout"
    repository.mkdir()
    git(repository, "init", "-q", "-b", "main")
    (repository / "example_pkg").mkdir()
    (repository / "example_pkg" / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "README.md").write_text("# example\n", encoding="utf-8")
    (repository / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-q", "-m", "initial")
    # Present in the working tree at build time, and tracked by nothing.
    (repository / "scratch").mkdir()
    (repository / "scratch" / "notes.txt").write_text("private working note\n", encoding="utf-8")
    (repository / "untracked-secret.env").write_text("TOKEN=not-a-real-secret\n", encoding="utf-8")
    return repository


def recording_indexer(payload: bytes = b"graph-bytes", *, completeness: str = lifecycle.COMPLETE,
                      seen: list | None = None, indexed_files: int = 0):
    """An indexer that writes a fixed artifact and records what it was shown."""

    def run(request: lifecycle.IndexRequest) -> lifecycle.IndexResult:
        if seen is not None:
            seen.append(request)
        request.output_path.write_bytes(payload)
        return lifecycle.IndexResult(completeness=completeness, indexed_files=indexed_files)

    return run


class TemporaryWorkspace(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name).resolve()
        self.state = self.root / "state"
        self.repository = make_repository(self.root)

    def build(self, **overrides):
        arguments = {
            "pin": PIN,
            "indexer": recording_indexer(),
            "root": self.state,
            "now": NOW,
        }
        arguments.update(overrides)
        return lifecycle.build_graph(self.repository, **arguments)


class PinTests(unittest.TestCase):
    def test_accepts_one_exact_release(self) -> None:
        pin = lifecycle.load_pin(
            {"distribution": "graphifyy", "version": "0.9.58", "wheel_sha256": "b" * 64}
        )
        self.assertEqual(pin.requirement, "graphifyy==0.9.58")
        self.assertEqual(pin.options, ())

    def test_rejects_ranges_and_unpinned_shapes(self) -> None:
        """A range, a marker, or a missing digest lets a build drift silently."""
        for version in (">=0.9", "0.9.*", "latest", "", "0.9.58; python_version>'3'"):
            with self.subTest(version=version):
                with self.assertRaises(ContextError):
                    lifecycle.load_pin(
                        {"distribution": "graphifyy", "version": version, "wheel_sha256": "b" * 64}
                    )

    def test_rejects_missing_or_malformed_artifact_digest(self) -> None:
        for digest in (None, "", "b" * 63, "not-hex" + "b" * 57):
            with self.subTest(digest=digest):
                with self.assertRaises(ContextError):
                    lifecycle.load_pin(
                        {"distribution": "graphifyy", "version": "0.9.58", "wheel_sha256": digest}
                    )

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaises(ContextError):
            lifecycle.load_pin(
                {"distribution": "graphifyy", "version": "0.9.58", "wheel_sha256": "b" * 64,
                 "index_url": "https://example.invalid/simple"}
            )


class CensusAndMaterializationTests(TemporaryWorkspace):
    def test_census_reads_the_commit_not_the_working_tree(self) -> None:
        commit, _ = lifecycle.resolve_revision(self.repository)
        census = lifecycle.read_tracked_census(self.repository, commit)
        self.assertEqual(
            [entry.path for entry in census.entries],
            [".gitignore", "README.md", "example_pkg/config.py"],
        )

    def test_census_digest_changes_when_tracked_content_changes(self) -> None:
        commit, _ = lifecycle.resolve_revision(self.repository)
        before = lifecycle.read_tracked_census(self.repository, commit).digest
        (self.repository / "README.md").write_text("# example changed\n", encoding="utf-8")
        git(self.repository, "commit", "-q", "-am", "change")
        after_commit, _ = lifecycle.resolve_revision(self.repository)
        self.assertNotEqual(before, lifecycle.read_tracked_census(self.repository, after_commit).digest)

    def test_symlinks_and_submodules_are_skipped_rather_than_followed(self) -> None:
        """A tracked symlink can name a target the build was never shown."""
        os.symlink("/etc/passwd", self.repository / "linked.py")
        git(self.repository, "add", "linked.py")
        git(self.repository, "commit", "-q", "-m", "symlink")
        commit, _ = lifecycle.resolve_revision(self.repository)
        census = lifecycle.read_tracked_census(self.repository, commit)
        self.assertNotIn("linked.py", [entry.path for entry in census.entries])
        self.assertIn(("linked.py", "symlink"), census.skipped)

    def test_committed_provider_state_is_skipped_rather_than_indexed(self) -> None:
        """A tracked ``.graphify`` is an old cache, not content to index.

        Materializing it would let the provider resume from a cache built over
        content this build never saw, and the adapter would then collect
        tracked repository bytes as if the provider had just produced them.
        """
        for name in (".graphify", "vendor/.GRAPH"):
            directory = self.repository / name
            directory.mkdir(parents=True)
            (directory / "cache.json").write_text('{"stale": true}\n', encoding="utf-8")
        git(self.repository, "add", ".graphify", "vendor")
        git(self.repository, "commit", "-q", "-m", "committed provider state")
        commit, _ = lifecycle.resolve_revision(self.repository)
        census = lifecycle.read_tracked_census(self.repository, commit)
        self.assertEqual(
            [entry.path for entry in census.entries],
            [".gitignore", "README.md", "example_pkg/config.py"],
        )
        self.assertIn((".graphify/cache.json", "provider state"), census.skipped)
        self.assertIn(("vendor/.GRAPH/cache.json", "provider state"), census.skipped)
        destination = self.root / "materialized-with-state"
        lifecycle.materialize_tracked_files(self.repository, census, destination)
        self.assertFalse((destination / ".graphify").exists())
        self.assertFalse((destination / "vendor").exists())

    def test_committing_provider_state_does_not_move_the_census_digest(self) -> None:
        # The manifest binds the digest of what was indexed. Committed provider
        # state is not indexed, so it does not enter that digest; it is
        # accounted for in ``skipped`` instead.
        commit, _ = lifecycle.resolve_revision(self.repository)
        before = lifecycle.read_tracked_census(self.repository, commit)
        state = self.repository / ".graph"
        state.mkdir()
        (state / "cache.json").write_text('{"stale": true}\n', encoding="utf-8")
        git(self.repository, "add", ".graph")
        git(self.repository, "commit", "-q", "-m", "state")
        after_commit, _ = lifecycle.resolve_revision(self.repository)
        after = lifecycle.read_tracked_census(self.repository, after_commit)
        self.assertEqual(before.digest, after.digest)
        self.assertNotEqual(before.skipped, after.skipped)

    def test_materialization_writes_only_tracked_files(self) -> None:
        commit, _ = lifecycle.resolve_revision(self.repository)
        census = lifecycle.read_tracked_census(self.repository, commit)
        destination = self.root / "materialized"
        written = lifecycle.materialize_tracked_files(self.repository, census, destination)
        present = sorted(
            str(path.relative_to(destination))
            for path in destination.rglob("*")
            if path.is_file()
        )
        self.assertEqual(present, [".gitignore", "README.md", "example_pkg/config.py"])
        self.assertEqual(written, census.total_bytes)
        self.assertFalse((destination / "scratch").exists())
        self.assertFalse((destination / "untracked-secret.env").exists())
        self.assertFalse((destination / ".git").exists())

    def test_materialization_is_private_and_non_executable(self) -> None:
        commit, _ = lifecycle.resolve_revision(self.repository)
        census = lifecycle.read_tracked_census(self.repository, commit)
        destination = self.root / "materialized"
        lifecycle.materialize_tracked_files(self.repository, census, destination)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        for path in destination.rglob("*"):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_materialization_refuses_an_existing_directory(self) -> None:
        commit, _ = lifecycle.resolve_revision(self.repository)
        census = lifecycle.read_tracked_census(self.repository, commit)
        destination = self.root / "materialized"
        destination.mkdir()
        with self.assertRaises(ContextError):
            lifecycle.materialize_tracked_files(self.repository, census, destination)

    def test_escaping_census_paths_are_rejected(self) -> None:
        escaping = ("/etc/passwd", "../outside.py", "a/../../b.py", ".git/config",
                    "vendor/.git/config", "a\\b.py", ".graphify/cache.json",
                    "vendor/.GRAPH/cache.json")
        for index, path in enumerate(escaping):
            with self.subTest(path=path):
                census = lifecycle.TrackedCensus(
                    entries=(lifecycle.TrackedEntry("100644", "0" * 40, path, 1),),
                    skipped=(),
                    digest="c" * 64,
                )
                with self.assertRaises(ContextError):
                    lifecycle.materialize_tracked_files(
                        self.repository, census, self.root / f"escape-{index}"
                    )


class ScrubbedEnvironmentTests(TemporaryWorkspace):
    def test_indexer_never_inherits_ambient_credentials(self) -> None:
        seen: list[lifecycle.IndexRequest] = []
        secrets = {
            "GITHUB_TOKEN": "not-a-real-token",
            "ANTHROPIC_API_KEY": "not-a-real-key",
            "AWS_SECRET_ACCESS_KEY": "not-a-real-key",
            "GRAPHIFY_API_KEY": "not-a-real-key",
        }
        previous = {name: os.environ.get(name) for name in secrets}
        os.environ.update(secrets)
        try:
            self.build(indexer=recording_indexer(seen=seen))
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        environment = seen[0].environment
        for name in secrets:
            self.assertNotIn(name, environment)
        self.assertEqual(environment["no_proxy"], "*")
        self.assertEqual(environment["https_proxy"], "")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")

    def test_provider_home_is_redirected_away_from_the_operator(self) -> None:
        seen: list[lifecycle.IndexRequest] = []
        self.build(indexer=recording_indexer(seen=seen))
        home = Path(seen[0].environment["HOME"])
        self.assertNotEqual(home, Path.home())
        self.assertTrue(str(home).startswith(str(self.state)))

    def test_allowlist_drops_everything_it_does_not_name(self) -> None:
        environment = lifecycle.scrubbed_environment(home=self.root / "h", temporary=self.root / "t")
        allowed = set(lifecycle._ENVIRONMENT_ALLOWLIST) | set(lifecycle._NETWORK_DENY) | {
            "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
        }
        self.assertEqual(set(environment) - allowed, set())


CONNECT_PROBE = """
import socket
import sys

try:
    socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5).close()
except OSError:
    sys.exit(1)
sys.exit(0)
"""


class NetworkIsolationTests(unittest.TestCase):
    """The provider's network boundary, against a socket that is really there."""

    def setUp(self) -> None:
        self.listener = socket.socket()
        self.addCleanup(self.listener.close)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]

    def connect(self, prefix: tuple[str, ...]) -> int:
        return subprocess.run(
            [*prefix, sys.executable, "-c", CONNECT_PROBE, str(self.port)],
            check=False,
            capture_output=True,
            timeout=120,
        ).returncode

    def test_an_unsandboxed_child_reaches_the_listening_socket(self) -> None:
        # The control. Without it, a sandboxed child that failed to start for
        # some unrelated reason would read as proof of isolation.
        self.assertEqual(self.connect(()), 0)

    def test_a_sandboxed_child_cannot_reach_the_listening_socket(self) -> None:
        sandbox = lifecycle.network_sandbox_command()
        if sandbox is None:
            self.skipTest("this host offers no OS sandbox that denies a child the network")
        self.assertEqual(self.connect(sandbox), 1)

    def launcher(self, exit_code: int) -> str:
        """A stand-in launcher, so the classifier is pinned on every host.

        A host that offers no real mechanism -- a Linux host with unprivileged
        user namespaces restricted, say -- would otherwise leave both halves of
        the accept/reject decision untested.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "launcher"
        path.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    def test_a_child_that_reports_a_denial_is_accepted(self) -> None:
        self.assertTrue(lifecycle._sandbox_denies_network((self.launcher(lifecycle._PROBE_DENIED),)))

    def test_a_child_that_reached_the_network_stack_is_rejected(self) -> None:
        self.assertFalse(lifecycle._sandbox_denies_network((self.launcher(3),)))

    def test_a_launcher_that_cannot_start_is_rejected(self) -> None:
        self.assertFalse(lifecycle._sandbox_denies_network(("/nonexistent/launcher",)))

    def test_a_candidate_that_does_not_deny_the_network_is_rejected(self) -> None:
        # ``env`` runs its argument unchanged: a prefix that contains nothing
        # must not be mistaken for a boundary just because it launches.
        passthrough = ("/usr/bin/env",)
        if not os.access(passthrough[0], os.X_OK):  # pragma: no cover - platform
            self.skipTest("no pass-through launcher to test against")
        self.assertFalse(lifecycle._sandbox_denies_network(passthrough))


#: What a provider that finished leaves behind: a completion claim and counts
#: that admit nothing outstanding.
FINISHED_REPORT = {"complete": True, "code_files": 3, "requeued": 0}


class ProviderLaunchTests(TemporaryWorkspace):
    """What ``subprocess_indexer`` actually hands the operating system."""

    def request(self) -> lifecycle.IndexRequest:
        # A fresh directory per launch, because that is what a build hands the
        # provider: ``materialize_tracked_files`` refuses a destination that
        # already exists, and so the provider never sees state from a prior run.
        source = Path(tempfile.mkdtemp(dir=self.root))
        return lifecycle.IndexRequest(
            source_root=source,
            output_path=self.root / "graph.bin",
            environment={"PATH": os.environ.get("PATH", "")},
            pin=PIN,
            commit="a" * 40,
            tree="b" * 40,
        )

    def run_indexer(
        self,
        executable: str,
        *,
        sandbox=("/sandbox", "--deny"),
        report: object = FINISHED_REPORT,
        state_directory: str = ".graphify",
    ) -> tuple[list[str], lifecycle.IndexResult]:
        """Launch the adapter with the provider's side of the contract faked.

        ``extract`` writes its state beside the sources it was run over, so the
        stand-in has to leave that state behind for the adapter to collect --
        an exit status alone is not a finished build.
        """
        request = self.request()
        recorded: list[list[str]] = []
        # What the adapter asked the operating system for, kept beside the argv
        # because the stream arrangement is as much of the contract as it is.
        self.launch_options: list[dict[str, object]] = []

        def fake_run(argv, **kwargs):
            recorded.append(list(argv))
            self.launch_options.append(dict(kwargs))
            if state_directory:
                written = request.source_root / state_directory
                written.mkdir(exist_ok=True)
                (written / "graph.bin").write_bytes(b"graph-bytes")
                if report is not None:
                    (written / "manifest.json").write_text(json.dumps(report), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch.object(lifecycle, "network_sandbox_command", lambda: sandbox):
            indexer = lifecycle.subprocess_indexer(executable)
        # Patched only around the launch, so the Git calls a build makes are
        # never intercepted by this stand-in.
        with mock.patch.object(subprocess, "run", fake_run):
            result = indexer(request)
        return recorded[0], result

    def launched_argv(self, executable: str, *, sandbox=("/sandbox", "--deny")) -> list[str]:
        return self.run_indexer(executable, sandbox=sandbox)[0]

    def test_the_provider_is_launched_inside_the_sandbox(self) -> None:
        argv = self.launched_argv("graphify")
        self.assertEqual(argv[:3], ["/sandbox", "--deny", "graphify"])

    def test_the_provider_is_given_no_stream_this_process_has_to_hold(self) -> None:
        """A talkative indexer must not be able to fill this process's memory.

        Nothing reads the provider's stdout or stderr -- completeness comes
        from the report it writes, not from what it printed -- so buffering
        them would only accumulate whatever it chose to log, for up to the
        timeout, under neither the tracked-content budget nor the artifact
        one. Inheriting them instead is not the alternative: diagnostics can
        echo indexed source, and this process may be writing JSON to stdout.
        """
        self.launched_argv("graphify")
        options = self.launch_options[0]
        self.assertNotIn("capture_output", options)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)

    def test_the_provider_is_invoked_through_its_documented_extract_interface(self) -> None:
        # The interface the adopt decision evaluated, recorded in
        # docs/graphify-evaluation.md as ``extract`` plus options. An
        # ``index --source ... --output ...`` shape would be a different CLI.
        argv = self.launched_argv("graphify")
        self.assertEqual(argv[3:], ["extract", *PIN.options])
        self.assertNotIn("--source", argv)
        self.assertNotIn("--output", argv)

    def test_the_collected_artifact_holds_the_state_the_provider_wrote(self) -> None:
        _, result = self.run_indexer("graphify")
        self.assertEqual(result.completeness, lifecycle.COMPLETE)
        self.assertEqual(result.indexed_files, 3)
        names = self.archived_names((self.root / "graph.bin").read_bytes())
        self.assertIn("graph.bin", names)

    def archived_names(self, artifact: bytes) -> list[str]:
        with tarfile.open(fileobj=io.BytesIO(artifact), mode="r") as archive:
            return archive.getnames()

    def test_collecting_the_same_state_twice_produces_the_same_bytes(self) -> None:
        # The manifest binds a digest of the artifact, so two builds of one
        # commit have to agree on the bytes down to the archive metadata.
        self.run_indexer("graphify")
        first = (self.root / "graph.bin").read_bytes()
        (self.root / "graph.bin").unlink()
        self.run_indexer("graphify")
        self.assertEqual(first, (self.root / "graph.bin").read_bytes())

    def test_a_provider_that_wrote_no_state_publishes_nothing(self) -> None:
        with self.assertRaises(ContextError):
            self.run_indexer("graphify", state_directory="")

    def test_a_successful_run_with_requeued_entries_is_partial(self) -> None:
        # The defect the clean-room run recorded: a repeat that exits zero in
        # 1.63 s having requeued 54 entries has not built a complete graph.
        _, result = self.run_indexer("graphify", report={"complete": True, "files": 429, "requeued": 54})
        self.assertEqual(result.completeness, lifecycle.PARTIAL)
        self.assertIn("54 requeued", " ".join(result.notes))

    def test_a_provider_that_denies_completion_is_partial(self) -> None:
        _, result = self.run_indexer("graphify", report={"complete": False, "files": 10})
        self.assertEqual(result.completeness, lifecycle.PARTIAL)

    def test_a_run_that_left_no_report_is_partial_rather_than_complete(self) -> None:
        # Exit status zero is not completion evidence. Absent evidence resolves
        # to the state ``graph_status`` refuses, not the one it accepts.
        _, result = self.run_indexer("graphify", report=None)
        self.assertEqual(result.completeness, lifecycle.PARTIAL)

    def test_an_unparseable_report_is_partial_rather_than_complete(self) -> None:
        request = self.request()

        def fake_run(argv, **kwargs):
            written = request.source_root / ".graph"
            written.mkdir(exist_ok=True)
            (written / "graph.bin").write_bytes(b"graph-bytes")
            (written / "manifest.json").write_bytes(b"{not json")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch.object(lifecycle, "network_sandbox_command", lambda: ("/sandbox",)):
            indexer = lifecycle.subprocess_indexer("graphify")
        with mock.patch.object(subprocess, "run", fake_run):
            result = indexer(request)
        self.assertEqual(result.completeness, lifecycle.PARTIAL)

    def test_a_report_without_affirmative_completion_evidence_is_partial(self) -> None:
        """A document that parses is not a document that claims completion.

        ``{}`` and a report in some schema this adapter does not understand
        both say nothing about whether the extraction finished, and nothing is
        not a claim. Treating them as complete would hand ``graph_status`` a
        usable generation built from an unknown run.
        """
        silent = (
            {},
            {"schema": "unexpected"},
            {"code_files": 3},
            {"complete": True},
            {"status": "running", "code_files": 3},
            {"status": "partial", "complete": True, "code_files": 3},
        )
        for report in silent:
            with self.subTest(report=report):
                _, result = self.run_indexer("graphify", report=report)
                self.assertEqual(result.completeness, lifecycle.PARTIAL)
                self.assertTrue(result.notes)

    def test_a_report_that_claims_completion_and_counts_its_work_is_complete(self) -> None:
        claimed = (
            {"complete": True, "code_files": 3},
            {"status": "success", "indexed_files": 3},
            {"completed": True, "entries": 0, "requeued": 0},
        )
        for report in claimed:
            with self.subTest(report=report):
                _, result = self.run_indexer("graphify", report=report)
                self.assertEqual(result.completeness, lifecycle.COMPLETE)

    def test_an_oversized_report_is_refused_without_being_read_whole(self) -> None:
        """Provider output is unbounded input; the read is bounded at the stream.

        Slicing after ``read_bytes()`` would have allocated the whole document
        first, so the assertion is not only that the build stays ``partial``:
        nothing in the collection path may read a provider file whole.
        """
        request = self.request()
        oversized = b'{"complete": true, "code_files": 3, "pad": "' + b"x" * lifecycle.MAX_MANIFEST_BYTES + b'"}'

        def fake_run(argv, **kwargs):
            written = request.source_root / ".graphify"
            written.mkdir(exist_ok=True)
            (written / "graph.bin").write_bytes(b"graph-bytes")
            (written / "manifest.json").write_bytes(oversized)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        def refuse_whole_file_read(self: Path) -> bytes:
            raise AssertionError(f"{self} was read whole")

        with mock.patch.object(lifecycle, "network_sandbox_command", lambda: ("/sandbox",)):
            indexer = lifecycle.subprocess_indexer("graphify")
        with mock.patch.object(subprocess, "run", fake_run):
            with mock.patch.object(Path, "read_bytes", refuse_whole_file_read):
                result = indexer(request)
        self.assertEqual(result.completeness, lifecycle.PARTIAL)

    def test_extraction_refuses_to_run_over_pre_existing_provider_state(self) -> None:
        """State that predates the run is a cache, and would be collected as output.

        The census already keeps committed provider state out of the copy, so
        this is the second check rather than the only one -- the source root is
        an argument, and everything after the run treats what it finds there as
        freshly produced.
        """
        request = self.request()
        (request.source_root / ".graphify").mkdir()
        (request.source_root / ".graphify" / "cache.json").write_text("{}", encoding="utf-8")
        launched: list[list[str]] = []

        def fake_run(argv, **kwargs):
            launched.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with mock.patch.object(lifecycle, "network_sandbox_command", lambda: ("/sandbox",)):
            indexer = lifecycle.subprocess_indexer("graphify")
        with mock.patch.object(subprocess, "run", fake_run):
            with self.assertRaises(ContextError):
                indexer(request)
        self.assertEqual(launched, [])
        self.assertFalse((self.root / "graph.bin").exists())

    def test_a_host_without_a_sandbox_refuses_to_launch_a_provider(self) -> None:
        with mock.patch.object(lifecycle, "network_sandbox_command", lambda: None):
            with self.assertRaises(ContextError):
                lifecycle.subprocess_indexer("graphify")

    def test_a_relative_provider_path_binds_to_the_invocation_directory(self) -> None:
        # The child runs in the materialized copy, so a relative path left
        # unresolved would be looked up there instead of where it is installed.
        installed = self.root / "venv" / "bin"
        installed.mkdir(parents=True)
        provider = installed / "graphify"
        provider.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        provider.chmod(0o700)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        argv = self.launched_argv(os.path.join("venv", "bin", "graphify"))
        self.assertEqual(argv[2], str(provider))

    def test_a_bare_command_name_keeps_its_path_lookup(self) -> None:
        self.assertEqual(lifecycle._resolved_executable("graphify"), "graphify")

    def test_an_unnamed_provider_is_refused(self) -> None:
        with self.assertRaises(ContextError):
            lifecycle._resolved_executable("")


class BuildAndPublishTests(TemporaryWorkspace):
    def test_manifest_binds_every_required_fact(self) -> None:
        manifest = self.build()
        commit, tree = lifecycle.resolve_revision(self.repository)
        census = lifecycle.read_tracked_census(self.repository, commit)
        self.assertEqual(manifest.commit, commit)
        self.assertEqual(manifest.tree, tree)
        self.assertEqual(manifest.provider, PIN.as_metadata())
        self.assertEqual(manifest.built_at, NOW.isoformat())
        self.assertEqual(manifest.tracked_files, census.file_count)
        self.assertEqual(manifest.tracked_bytes, census.total_bytes)
        self.assertEqual(manifest.census_digest, census.digest)
        self.assertEqual(manifest.graph_digest, hashlib.sha256(b"graph-bytes").hexdigest())
        self.assertEqual(manifest.graph_bytes, len(b"graph-bytes"))
        self.assertEqual(manifest.completeness, lifecycle.COMPLETE)

    def test_manifest_round_trips_through_validation(self) -> None:
        manifest = self.build()
        self.assertEqual(lifecycle.load_manifest(manifest.to_json()), manifest)

    def test_shareable_summary_carries_no_content_or_local_path(self) -> None:
        summary = self.build().shareable_summary()
        rendered = json.dumps(summary)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn("VALUE = 1", rendered)
        self.assertNotIn("scratch", rendered)

    def test_state_is_private_to_the_operator(self) -> None:
        self.build()
        state = lifecycle.GraphStateRoot(self.repository, root=self.state)
        self.assertEqual(stat.S_IMODE(state.path.stat().st_mode), 0o700)
        generation = state.current_generation()
        self.assertEqual(stat.S_IMODE((state.generations_path / generation).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(state.artifact_path(generation).stat().st_mode), 0o600)

    def test_two_worktrees_of_one_repository_keep_separate_state(self) -> None:
        other = self.root / "other"
        other.mkdir()
        self.assertNotEqual(lifecycle.workspace_id(self.repository), lifecycle.workspace_id(other))

    def test_refresh_publishes_a_new_immutable_generation(self) -> None:
        first = self.build(keep_previous=True)
        (self.repository / "README.md").write_text("# changed\n", encoding="utf-8")
        git(self.repository, "commit", "-q", "-am", "change")
        second = self.build(indexer=recording_indexer(b"second-graph"), keep_previous=True)
        self.assertNotEqual(first.generation, second.generation)
        self.assertNotEqual(first.commit, second.commit)
        state = lifecycle.GraphStateRoot(self.repository, root=self.state)
        self.assertEqual(state.current_generation(), second.generation)
        # The superseded generation is untouched, not rewritten in place.
        self.assertEqual(state.read_manifest(first.generation), first)
        self.assertEqual(state.artifact_path(first.generation).read_bytes(), b"graph-bytes")

    def test_pruning_keeps_only_the_published_generation(self) -> None:
        self.build()
        (self.repository / "README.md").write_text("# changed\n", encoding="utf-8")
        git(self.repository, "commit", "-q", "-am", "change")
        second = self.build()
        self.assertEqual(list(lifecycle.iter_generations(self.repository, root=self.state)), [second.generation])

    def test_pruning_happens_before_the_build_lock_is_released(self) -> None:
        """Pruning after unlocking can delete a concurrent builder's generation.

        A builder that released the lock, paused, and only then pruned would
        remove whatever a second builder published in the meantime -- or that
        builder's staging directory -- leaving ``current`` naming a directory
        that no longer exists, with both builds reporting success.
        """
        order: list[str] = []
        prune, release = lifecycle.GraphStateRoot.prune, lifecycle._BuildLock.__exit__

        def record_prune(state, *, keep):
            order.append("prune")
            return prune(state, keep=keep)

        def record_release(lock, *exception):
            order.append("unlock")
            return release(lock, *exception)

        with mock.patch.object(lifecycle.GraphStateRoot, "prune", record_prune), \
                mock.patch.object(lifecycle._BuildLock, "__exit__", record_release):
            self.build()
        self.assertEqual(order, ["prune", "unlock"])

    def test_a_failed_build_publishes_nothing(self) -> None:
        first = self.build()

        def failing(request: lifecycle.IndexRequest) -> lifecycle.IndexResult:
            raise ContextError("provider failed")

        with self.assertRaises(ContextError):
            self.build(indexer=failing)
        state = lifecycle.GraphStateRoot(self.repository, root=self.state)
        self.assertEqual(state.current_generation(), first.generation)
        self.assertEqual(list(lifecycle.iter_generations(self.repository, root=self.state)), [first.generation])

    def test_a_provider_that_writes_nothing_fails_closed(self) -> None:
        def silent(request: lifecycle.IndexRequest) -> lifecycle.IndexResult:
            return lifecycle.IndexResult()

        with self.assertRaises(ContextError):
            self.build(indexer=silent)
        self.assertEqual(list(lifecycle.iter_generations(self.repository, root=self.state)), [])

    def test_an_oversized_artifact_is_refused_before_publication(self) -> None:
        original = lifecycle.MAX_ARTIFACT_BYTES
        lifecycle.MAX_ARTIFACT_BYTES = 4
        try:
            with self.assertRaises(ContextError):
                self.build(indexer=recording_indexer(b"too-large-for-the-budget"))
        finally:
            lifecycle.MAX_ARTIFACT_BYTES = original
        self.assertEqual(list(lifecycle.iter_generations(self.repository, root=self.state)), [])

    def test_state_is_refused_inside_a_git_repository(self) -> None:
        with self.assertRaises(ContextError):
            self.build(root=self.repository / ".code-mower-state")


class StatusFailsClosedTests(TemporaryWorkspace):
    def test_a_fresh_build_is_current(self) -> None:
        manifest = self.build()
        status = lifecycle.graph_status(self.repository, root=self.state)
        self.assertEqual(status.state, "current")
        self.assertTrue(status.usable)
        self.assertEqual(status.manifest, manifest)

    def test_no_state_is_absent_and_unusable(self) -> None:
        status = lifecycle.graph_status(self.repository, root=self.state)
        self.assertEqual(status.state, "absent")
        self.assertFalse(status.usable)

    def test_a_new_commit_makes_the_graph_stale(self) -> None:
        self.build()
        (self.repository / "README.md").write_text("# changed\n", encoding="utf-8")
        git(self.repository, "commit", "-q", "-am", "change")
        status = lifecycle.graph_status(self.repository, root=self.state)
        self.assertEqual(status.state, "stale")
        self.assertFalse(status.usable)

    def test_a_generation_pruned_mid_read_is_read_again(self) -> None:
        # Readers take no lock, so a refresh can publish and prune between the
        # pointer read and the validation of what it named. The failure that
        # produces is about a directory a healthy build superseded, not about
        # the graph the operator has.
        first = self.build()
        second = self.build(indexer=recording_indexer(b"second-graph"))
        raced = lifecycle.GenerationStatus(
            state="invalid",
            generation=first.generation,
            detail="local graph generation is missing its manifest",
        )
        real, attempts = lifecycle._status_once, []

        def once(state, repository, **keywords):
            attempts.append(1)
            return raced if len(attempts) == 1 else real(state, repository, **keywords)

        with mock.patch.object(lifecycle, "_status_once", once):
            status = lifecycle.graph_status(self.repository, root=self.state)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(status.usable)
        self.assertEqual(status.generation, second.generation)

    def test_a_verdict_about_the_published_generation_is_not_retried(self) -> None:
        # The retry exists for a moved pointer only. A genuinely corrupt
        # current generation is reported on the first read, not polled.
        manifest = self.build()
        artifact = lifecycle.GraphStateRoot(self.repository, root=self.state).artifact_path(manifest.generation)
        artifact.write_bytes(b"tampered!!!")
        real, attempts = lifecycle._status_once, []

        def once(state, repository, **keywords):
            attempts.append(1)
            return real(state, repository, **keywords)

        with mock.patch.object(lifecycle, "_status_once", once):
            status = lifecycle.graph_status(self.repository, root=self.state)
        self.assertEqual(status.state, "corrupt")
        self.assertEqual(len(attempts), 1)

    def test_a_tampered_artifact_is_corrupt(self) -> None:
        manifest = self.build()
        artifact = lifecycle.GraphStateRoot(self.repository, root=self.state).artifact_path(manifest.generation)
        artifact.write_bytes(b"tampered!!!")  # same length, different content
        status = lifecycle.graph_status(self.repository, root=self.state)
        self.assertEqual(status.state, "corrupt")
        self.assertFalse(status.usable)

    def test_a_truncated_artifact_is_corrupt(self) -> None:
        manifest = self.build()
        artifact = lifecycle.GraphStateRoot(self.repository, root=self.state).artifact_path(manifest.generation)
        artifact.write_bytes(b"short")
        self.assertEqual(lifecycle.graph_status(self.repository, root=self.state).state, "corrupt")

    def test_an_oversized_artifact_is_refused_on_read(self) -> None:
        manifest = self.build()
        state = lifecycle.GraphStateRoot(self.repository, root=self.state)
        artifact = state.artifact_path(manifest.generation)
        original = lifecycle.MAX_ARTIFACT_BYTES
        lifecycle.MAX_ARTIFACT_BYTES = 4
        try:
            self.assertIn(
                lifecycle.graph_status(self.repository, root=self.state).state,
                ("corrupt", "oversized", "invalid"),
            )
        finally:
            lifecycle.MAX_ARTIFACT_BYTES = original
        self.assertTrue(artifact.exists())

    def test_a_partial_build_is_unusable_by_default(self) -> None:
        self.build(indexer=recording_indexer(completeness=lifecycle.PARTIAL))
        self.assertEqual(lifecycle.graph_status(self.repository, root=self.state).state, "partial")
        allowed = lifecycle.graph_status(self.repository, root=self.state, require_complete=False)
        self.assertEqual(allowed.state, "current")

    def test_a_corrupt_manifest_is_invalid(self) -> None:
        manifest = self.build()
        path = (
            lifecycle.GraphStateRoot(self.repository, root=self.state).generations_path
            / manifest.generation
            / lifecycle.MANIFEST_NAME
        )
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(lifecycle.graph_status(self.repository, root=self.state).state, "invalid")

    def test_a_manifest_missing_its_revision_binding_is_invalid(self) -> None:
        manifest = self.build()
        payload = manifest.to_json()
        payload.pop("tree")
        path = (
            lifecycle.GraphStateRoot(self.repository, root=self.state).generations_path
            / manifest.generation
            / lifecycle.MANIFEST_NAME
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(lifecycle.graph_status(self.repository, root=self.state).state, "invalid")

    def test_a_manifest_relabelled_to_another_generation_is_invalid(self) -> None:
        manifest = self.build()
        payload = manifest.to_json()
        payload["generation"] = "f" * 32
        path = (
            lifecycle.GraphStateRoot(self.repository, root=self.state).generations_path
            / manifest.generation
            / lifecycle.MANIFEST_NAME
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(lifecycle.graph_status(self.repository, root=self.state).state, "invalid")

    def test_a_corrupt_pointer_is_invalid(self) -> None:
        self.build()
        (lifecycle.GraphStateRoot(self.repository, root=self.state).path / lifecycle.CURRENT_NAME).write_text(
            "../../elsewhere\n", encoding="utf-8"
        )
        self.assertEqual(lifecycle.graph_status(self.repository, root=self.state).state, "invalid")

    def test_group_readable_state_is_invalid(self) -> None:
        """State loosened after the fact fails closed rather than being used."""
        self.build()
        path = lifecycle.GraphStateRoot(self.repository, root=self.state).path
        path.chmod(0o750)
        try:
            self.assertEqual(lifecycle.graph_status(self.repository, root=self.state).state, "invalid")
        finally:
            path.chmod(0o700)

    def test_a_group_readable_artifact_is_invalid(self) -> None:
        manifest = self.build()
        artifact = lifecycle.GraphStateRoot(self.repository, root=self.state).artifact_path(manifest.generation)
        artifact.chmod(0o640)
        try:
            self.assertEqual(lifecycle.graph_status(self.repository, root=self.state).state, "invalid")
        finally:
            artifact.chmod(0o600)


class GitBoundaryTests(TemporaryWorkspace):
    """The other half of the offline boundary: Git's own reads.

    The provider runs inside a sandbox, but the census reader and the blob
    materializer are Git children of this process, outside it. In a partial
    clone their reads can fetch missing objects from a remote, so the boundary
    has to cover them too.
    """

    def commit(self) -> str:
        return lifecycle.resolve_revision(self.repository)[0]

    def test_git_children_get_no_lazy_fetch_and_no_transport(self) -> None:
        environment = lifecycle.git_environment()
        self.assertEqual(environment["GIT_NO_LAZY_FETCH"], "1")
        # Set but empty: git reads the variable as the complete list of
        # permitted transports, and an empty list permits none.
        self.assertEqual(environment["GIT_ALLOW_PROTOCOL"], "")
        self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(environment["GIT_CONFIG_SYSTEM"], os.devnull)

    def test_git_children_inherit_no_ambient_secret(self) -> None:
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "not-a-real-secret"}):
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", lifecycle.git_environment())

    def test_the_transport_denial_outranks_repository_local_configuration(self) -> None:
        # Local configuration belongs to the untrusted checkout and is always
        # read, so the denial has to travel on the command line, which is the
        # only level above it.
        self.assertIn("protocol.allow=never", lifecycle._GIT_SAFETY_OPTIONS)

    def test_a_full_clone_is_read_without_complaint(self) -> None:
        lifecycle.refuse_lazy_object_fetch(self.repository)
        self.assertTrue(lifecycle.read_tracked_census(self.repository, self.commit()).entries)

    def test_a_partial_clone_is_refused_before_its_tree_is_read(self) -> None:
        census = lifecycle.read_tracked_census(self.repository, self.commit())
        git(self.repository, "config", "--local", "remote.origin.promisor", "true")
        with self.assertRaises(ContextError):
            lifecycle.read_tracked_census(self.repository, self.commit())
        with self.assertRaises(ContextError):
            lifecycle.materialize_tracked_files(self.repository, census, self.root / "fresh")
        self.assertFalse((self.root / "fresh").exists())

    def test_a_partial_clone_build_publishes_nothing(self) -> None:
        git(self.repository, "config", "--local", "remote.origin.partialclonefilter", "blob:none")
        with self.assertRaises(ContextError):
            self.build()
        self.assertEqual(lifecycle.graph_status(self.repository, root=self.state).state, "absent")

    def test_the_partial_clone_extension_is_refused_too(self) -> None:
        # The other shape it takes: a repository-format extension, with the
        # version bump that makes git accept one.
        git(self.repository, "config", "--local", "core.repositoryformatversion", "1")
        git(self.repository, "config", "--local", "extensions.partialclone", "origin")
        with self.assertRaises(ContextError):
            lifecycle.refuse_lazy_object_fetch(self.repository)


class RemoveTests(TemporaryWorkspace):
    def test_remove_takes_the_build_lock(self) -> None:
        # A removal running beside a build deletes its sources, its output and
        # its generations; the builder then either fails or recreates state
        # that ``remove`` has already reported as gone.
        self.build()
        taken: list[str] = []
        lock = lifecycle.GraphStateRoot.lock

        def record_lock(state):
            taken.append("lock")
            return lock(state)

        with mock.patch.object(lifecycle.GraphStateRoot, "lock", record_lock):
            self.assertTrue(lifecycle.remove_graph(self.repository, root=self.state))
        self.assertEqual(taken, ["lock"])

    def test_the_lock_survives_the_removal_it_serializes(self) -> None:
        # The inode a waiting builder is blocked on must still be there when
        # the remover lets go of it. A lock file inside the deleted tree would
        # be unlinked mid-removal and the next builder would lock a new one.
        self.build()
        state = lifecycle.GraphStateRoot(self.repository, root=self.state)
        self.assertFalse(state.lock_path.is_relative_to(state.path))
        before = state.lock_path.stat().st_ino
        self.assertTrue(lifecycle.remove_graph(self.repository, root=self.state))
        self.assertTrue(state.lock_path.exists())
        self.assertEqual(state.lock_path.stat().st_ino, before)

    def test_the_retained_lock_carries_nothing_and_stays_private(self) -> None:
        self.build()
        state = lifecycle.GraphStateRoot(self.repository, root=self.state)
        lifecycle.remove_graph(self.repository, root=self.state)
        self.assertEqual(state.lock_path.read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(state.lock_path.stat().st_mode), 0o600)

    def test_removing_nothing_creates_nothing(self) -> None:
        # A removal on an installation that never opted in must not bring a
        # private state tree into existence just to report that it is empty.
        state = lifecycle.GraphStateRoot(self.repository, root=self.state)
        self.assertFalse(lifecycle.remove_graph(self.repository, root=self.state))
        self.assertFalse(state.path.exists())
        self.assertFalse(state.lock_path.exists())

    def test_remove_deletes_every_generation(self) -> None:
        self.build()
        state = lifecycle.GraphStateRoot(self.repository, root=self.state)
        self.assertTrue(lifecycle.remove_graph(self.repository, root=self.state))
        self.assertFalse(state.path.exists())
        self.assertEqual(lifecycle.graph_status(self.repository, root=self.state).state, "absent")

    def test_remove_is_idempotent(self) -> None:
        self.assertFalse(lifecycle.remove_graph(self.repository, root=self.state))

    def test_remove_refuses_state_that_is_not_private(self) -> None:
        self.build()
        path = lifecycle.GraphStateRoot(self.repository, root=self.state).path
        path.chmod(0o755)
        try:
            with self.assertRaises(ContextError):
                lifecycle.remove_graph(self.repository, root=self.state)
            self.assertTrue(path.exists())
        finally:
            path.chmod(0o700)


class DoctorTests(TemporaryWorkspace):
    def test_an_unconfigured_installation_skips_rather_than_fails(self) -> None:
        report = lifecycle.doctor_report(self.repository, pin=None, root=self.state)
        self.assertEqual(report["status"], "skip")
        self.assertEqual({check["status"] for check in report["checks"]}, {"skip"})

    def test_a_healthy_build_passes(self) -> None:
        self.build()
        # Isolation is a property of the host, not of this build; a host that
        # offers a sandbox is the healthy case being described here.
        with mock.patch.object(lifecycle, "network_sandbox_command", lambda: ("/sandbox",)):
            report = lifecycle.doctor_report(self.repository, pin=PIN, root=self.state)
        self.assertEqual(report["status"], "pass")

    def test_a_host_that_cannot_contain_a_provider_fails_doctor(self) -> None:
        self.build()
        with mock.patch.object(lifecycle, "network_sandbox_command", lambda: None):
            report = lifecycle.doctor_report(self.repository, pin=PIN, root=self.state)
        self.assertEqual(report["status"], "fail")
        isolation = [check for check in report["checks"] if check["check"] == "context-graph-isolation"]
        self.assertEqual([check["status"] for check in isolation], ["fail"])

    def test_isolation_is_not_asked_about_when_nothing_is_pinned(self) -> None:
        report = lifecycle.doctor_report(self.repository, pin=None, root=self.state)
        isolation = [check for check in report["checks"] if check["check"] == "context-graph-isolation"]
        self.assertEqual([check["status"] for check in isolation], ["skip"])

    def test_a_stale_graph_fails_doctor(self) -> None:
        self.build()
        (self.repository / "README.md").write_text("# changed\n", encoding="utf-8")
        git(self.repository, "commit", "-q", "-am", "change")
        report = lifecycle.doctor_report(self.repository, pin=PIN, root=self.state)
        self.assertEqual(report["status"], "fail")

    def test_doctor_output_carries_no_indexed_content(self) -> None:
        self.build()
        rendered = json.dumps(lifecycle.doctor_report(self.repository, pin=PIN, root=self.state))
        self.assertNotIn("VALUE = 1", rendered)
        self.assertNotIn("not-a-real-secret", rendered)


class CommandTests(TemporaryWorkspace):
    def pin_file(self) -> Path:
        path = self.root / "pin.json"
        path.write_text(json.dumps(PIN.as_metadata()), encoding="utf-8")
        return path

    def indexer_script(self, *, complete: bool = True) -> Path:
        """A stand-in for a pinned provider CLI, so no package is required.

        It answers to ``extract`` and writes its state into the directory it
        was run in, which is the contract ``docs/graphify-evaluation.md``
        records for the evaluated release.
        """
        path = self.root / "fake-indexer"
        path.write_text(
            "#!/bin/sh\n"
            '[ "$1" = "extract" ] || exit 64\n'
            "mkdir -p .graphify\n"
            "printf graph-bytes > .graphify/graph.bin\n"
            'printf \'{"complete": %s, "code_files": 1, "requeued": 0}\' '
            f"'{'true' if complete else 'false'}' > .graphify/manifest.json\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        return path

    def run_command(self, *arguments: str) -> tuple[int, str]:
        from contextlib import redirect_stdout
        from io import StringIO

        buffer = StringIO()
        with redirect_stdout(buffer):
            code = command.main(list(arguments))
        return code, buffer.getvalue()

    def base(self) -> list[str]:
        return ["--repo-path", str(self.repository), "--state-dir", str(self.state), "--json"]

    def test_build_status_refresh_remove_round_trip(self) -> None:
        # The only test that launches a provider for real, so it is also the
        # only one that needs the host to offer the sandbox a build requires.
        if lifecycle.network_sandbox_command() is None:
            self.skipTest("this host offers no OS sandbox that denies a child the network")
        pin, indexer = str(self.pin_file()), str(self.indexer_script())
        code, output = self.run_command("build", *self.base(), "--pin-file", pin, "--indexer", indexer)
        self.assertEqual(code, 0, output)
        published = json.loads(output)
        self.assertEqual(published["status"], "published")

        code, output = self.run_command("status", *self.base())
        self.assertEqual(code, 0, output)
        self.assertTrue(json.loads(output)["usable"])

        # A second ``build`` refuses; ``refresh`` is the explicit rebuild verb.
        code, _ = self.run_command("build", *self.base(), "--pin-file", pin, "--indexer", indexer)
        self.assertEqual(code, 1)
        code, output = self.run_command("refresh", *self.base(), "--pin-file", pin, "--indexer", indexer)
        self.assertEqual(code, 0, output)
        self.assertNotEqual(json.loads(output)["generation"], published["generation"])

        code, output = self.run_command("remove", *self.base())
        self.assertEqual(code, 0, output)
        self.assertTrue(json.loads(output)["removed"])
        code, output = self.run_command("status", *self.base())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["state"], "absent")

    def test_a_provider_that_admits_an_incomplete_run_is_not_usable(self) -> None:
        # End to end through the real launcher: the provider exits zero and
        # writes state, and the build is still refused because its own report
        # denies completion. Exit status is not completion evidence.
        if lifecycle.network_sandbox_command() is None:
            self.skipTest("this host offers no OS sandbox that denies a child the network")
        code, output = self.run_command(
            "build", *self.base(),
            "--pin-file", str(self.pin_file()),
            "--indexer", str(self.indexer_script(complete=False)),
        )
        self.assertEqual(code, 0, output)
        code, output = self.run_command("status", *self.base())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["state"], "partial")

    def test_status_reports_stale_with_a_nonzero_exit(self) -> None:
        self.build()
        (self.repository / "README.md").write_text("# changed\n", encoding="utf-8")
        git(self.repository, "commit", "-q", "-am", "change")
        code, output = self.run_command("status", *self.base())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["state"], "stale")

    def test_build_without_a_pin_is_refused(self) -> None:
        code, _ = self.run_command("build", *self.base(), "--indexer", str(self.indexer_script()))
        self.assertEqual(code, 1)
        self.assertEqual(list(lifecycle.iter_generations(self.repository, root=self.state)), [])

    def test_remove_hides_the_private_path_unless_asked(self) -> None:
        self.build()
        _, output = self.run_command("remove", *self.base())
        self.assertNotIn(str(self.state), output)

    def test_doctor_reports_an_unconfigured_installation(self) -> None:
        code, output = self.run_command("doctor", *self.base())
        self.assertEqual(code, 0, output)
        self.assertEqual(json.loads(output)["status"], "skip")

    def test_command_is_registered_on_the_cli(self) -> None:
        from code_mower import cli

        self.assertIs(cli.COMMAND_HANDLERS["context-graph"], command.main)
        self.assertIn("context-graph", cli.COMMAND_DESCRIPTIONS)


if __name__ == "__main__":  # pragma: no cover - direct invocation
    unittest.main()
