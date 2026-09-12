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

import contextlib
import dataclasses
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
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from code_mower import context_graph
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


class FakeChild:
    """Enough of ``subprocess.Popen`` to stand in for the provider process.

    The adapter no longer hands the run to ``subprocess.run``: it opens the
    child itself so the child can lead its own process group and the whole
    group can be stopped if the run overruns. A stand-in is therefore a context
    manager that gets waited on, and a ``CompletedProcess`` no longer describes
    what the launch produces.

    ``pid`` is deliberately never a live process. A stand-in that carried a real
    pid -- this process's own, say -- would have a test signalling a process
    group it is itself a member of.
    """

    def __init__(self, returncode: int = 0, *, overruns: bool = False) -> None:
        self.returncode = returncode
        self.pid = -1
        self.killed = False
        self._overruns = overruns

    def __enter__(self) -> "FakeChild":
        return self

    def __exit__(self, *exception: object) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> int:
        if self._overruns:
            raise subprocess.TimeoutExpired("graphify", timeout or 0)
        return self.returncode

    def poll(self) -> int | None:
        # ``None`` means the leader is still running, which is exactly the
        # state an overrun leaves it in: the wait gave up on it, not the other
        # way round.
        return None if self._overruns else self.returncode

    def kill(self) -> None:
        self.killed = True


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
        # A pin that names no options is still a restricted pin: the adoption
        # conditions are not the operator's to omit by leaving a field out.
        self.assertEqual(pin.options, ("--code-only", "--no-cluster"))

    def test_the_required_extraction_restrictions_are_always_carried(self) -> None:
        """Code-only and no-cluster are conditions of the adopt decision.

        They are prepended into the pin rather than added at the launch site,
        so the manifest records the run that actually happened. A pin that
        already names one keeps exactly one copy of it, and whatever else it
        names is preserved after them.
        """
        pin = lifecycle.load_pin(
            {"distribution": "graphifyy", "version": "0.9.58", "wheel_sha256": "b" * 64,
             "options": ["--no-cluster", "--max-workers", "4"]}
        )
        self.assertEqual(pin.options, ("--code-only", "--no-cluster", "--max-workers", "4"))

    def test_rejects_options_that_undo_the_extraction_restrictions(self) -> None:
        """An option that re-enables clustering or non-code extraction is refused.

        Overriding it by argument order would leave the pin claiming one thing
        and the provider doing another; refusing says so where an operator can
        see it.
        """
        for option in ("--cluster", "--no-code-only", "--code-only=false", "--no-cluster=0"):
            with self.subTest(option=option):
                with self.assertRaises(ContextError):
                    lifecycle.load_pin(
                        {"distribution": "graphifyy", "version": "0.9.58", "wheel_sha256": "b" * 64,
                         "options": [option]}
                    )

    def test_the_options_bound_is_applied_to_what_actually_runs(self) -> None:
        """The bound counts the normalized tuple, not what the file spelled.

        Counting the raw list let a pin pass validation and then normalize into
        one option too many, so its own ``as_metadata`` could no longer be read
        back: a build could publish a generation whose manifest is invalid the
        moment anything reloads it, pruning the last usable generation for one
        nothing can read. The bound now refuses it wherever the pin is made.
        """
        source = {
            "distribution": "graphifyy",
            "version": "0.9.58",
            "wheel_sha256": "b" * 64,
            "options": [f"--flag-{index}" for index in range(lifecycle.MAX_EXTRACT_OPTIONS - 1)],
        }
        with self.assertRaises(ContextError):
            lifecycle.load_pin(source)
        with self.assertRaises(ContextError):
            lifecycle.GraphifyPin(
                distribution="graphifyy",
                version="0.9.58",
                wheel_sha256="b" * 64,
                options=tuple(source["options"]),
            )

    def test_a_pin_at_the_bound_round_trips_through_its_own_metadata(self) -> None:
        """Normalization is a fixed point, so a published manifest can be reread.

        The required flags are dropped wherever they appear and re-prepended
        exactly once, so a pin holding the most options it may hold reloads to
        an equal pin rather than growing by two each time it is written out.
        """
        extra = [f"--flag-{index}" for index in range(lifecycle.MAX_EXTRACT_OPTIONS - 2)]
        pin = lifecycle.load_pin(
            {"distribution": "graphifyy", "version": "0.9.58", "wheel_sha256": "b" * 64,
             "options": extra}
        )
        self.assertEqual(len(pin.options), lifecycle.MAX_EXTRACT_OPTIONS)
        self.assertEqual(lifecycle.load_pin(pin.as_metadata()), pin)
        self.assertEqual(lifecycle.load_pin(pin.as_metadata()).options, pin.options)

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

    def test_committed_code_mower_state_is_skipped_rather_than_indexed(self) -> None:
        """A tracked ``.code-mower`` is this tool's own state, not content.

        ``context_graph`` refuses a packet that cites into ``.code-mower``
        because a graph that reached in there escaped the checkout it was asked
        to index. The lifecycle has to agree at the other end: if those bytes
        are handed to the indexer in the first place, the evidence contract is
        refusing a citation to content the provider has already read.
        """
        for name in (".code-mower", "vendor/.CODE-MOWER"):
            directory = self.repository / name
            directory.mkdir(parents=True)
            (directory / "packet.json").write_text('{"secret": "packet"}\n', encoding="utf-8")
        git(self.repository, "add", ".code-mower", "vendor")
        git(self.repository, "commit", "-q", "-m", "committed code mower state")
        commit, _ = lifecycle.resolve_revision(self.repository)
        census = lifecycle.read_tracked_census(self.repository, commit)
        self.assertEqual(
            [entry.path for entry in census.entries],
            [".gitignore", "README.md", "example_pkg/config.py"],
        )
        self.assertIn((".code-mower/packet.json", "private state"), census.skipped)
        self.assertIn(("vendor/.CODE-MOWER/packet.json", "private state"), census.skipped)
        destination = self.root / "materialized-with-code-mower"
        lifecycle.materialize_tracked_files(self.repository, census, destination)
        self.assertFalse((destination / ".code-mower").exists())
        self.assertFalse((destination / "vendor").exists())

    def test_the_census_excludes_exactly_the_evidence_contract_roots(self) -> None:
        """The two ends of the policy share one set rather than two copies.

        A name added to ``context_graph._EXCLUDED_ROOTS`` must not have to be
        remembered here as well, so this asserts identity of the object and not
        merely equality of its contents.
        """
        self.assertIs(lifecycle._PRIVATE_STATE_ROOTS, context_graph._EXCLUDED_ROOTS)
        for root in context_graph._EXCLUDED_ROOTS:
            with self.subTest(root=root):
                self.assertIsNotNone(lifecycle._private_state_reason(f"vendor/{root}/file.json"))

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

    def test_skipped_paths_are_bounded_like_materialized_ones(self) -> None:
        """The file-count budget bounds what is written, not what is recorded.

        A repository of symlinks, submodules, or committed provider state adds
        nothing to ``entries`` and so passes the tracked-file budget however
        large it gets, while ``skipped`` grows with it. The manifest's
        ``skipped_paths`` is validated against the same bound on every read, so
        an unbounded census would publish a generation, prune its predecessor,
        and then read back ``invalid``. The refusal belongs here, before a build
        has done anything.
        """
        links = [f"link-{index}.py" for index in range(3)]
        for name in links:
            os.symlink("/etc/passwd", self.repository / name)
        # Named, not ``add .``: the workspace deliberately holds an untracked
        # secret, and this test is about the census, not about committing it.
        git(self.repository, "add", *links)
        git(self.repository, "commit", "-q", "-m", "many symlinks")
        commit, _ = lifecycle.resolve_revision(self.repository)
        with mock.patch.object(lifecycle, "MAX_SKIPPED_PATHS", 2):
            with self.assertRaises(ContextError):
                lifecycle.read_tracked_census(self.repository, commit)
        census = lifecycle.read_tracked_census(self.repository, commit)
        self.assertEqual(len(census.skipped), 3)

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
                    "vendor/.GRAPH/cache.json", ".code-mower/packets/one.json",
                    "docs/.CODE-MOWER/evidence.json")
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


class ListingStreamTests(unittest.TestCase):
    """The census consumes its listing as it arrives, bounded, and stops the reader.

    Capturing the whole listing first put a repository's worth of metadata in
    this process before any census bound could be checked, so a tree far past
    every budget exhausted memory instead of being refused at the budget. The
    reader is a stand-in here because a repository large enough to prove the
    bound for real would be the thing the bound exists to avoid.
    """

    class Reader:
        """Enough of ``Popen`` for the record stream: a pipe and an exit status."""

        def __init__(self, payload: bytes, returncode: int = 0) -> None:
            self.stdout = io.BytesIO(payload)
            self.returncode = returncode
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def poll(self) -> int | None:
            return self.returncode if self.killed else None

        def kill(self) -> None:
            self.killed = True

    def records(self, payload: bytes, returncode: int = 0) -> list[str]:
        return list(lifecycle._read_records(self.Reader(payload, returncode)))

    def test_records_are_split_on_the_delimiter(self) -> None:
        self.assertEqual(self.records(b"one\0two\0"), ["one", "two"])

    def test_a_run_of_bytes_past_the_record_bound_is_refused(self) -> None:
        # No delimiter, so nothing can be classified and nothing can be
        # released: this is exactly the shape that grows without limit.
        with self.assertRaises(ContextError):
            self.records(b"x" * (lifecycle.MAX_CENSUS_RECORD_BYTES + 1) + b"\0")

    def test_a_stream_that_ends_mid_record_is_refused(self) -> None:
        with self.assertRaises(ContextError):
            self.records(b"one\0two")

    def test_a_reader_that_failed_is_refused_even_after_a_clean_stream(self) -> None:
        with self.assertRaises(ContextError):
            self.records(b"one\0", returncode=1)

    def test_a_consumer_that_stops_early_stops_the_reader_with_it(self) -> None:
        """A refused census must not leave Git enumerating the rest of the tree."""
        reader = self.Reader(b"one\0two\0")
        with mock.patch.object(subprocess, "Popen", lambda *arguments, **keywords: reader):
            with self.assertRaises(ContextError):
                with lifecycle._git_records(Path("/nonexistent"), "ls-tree") as records:
                    next(records)
                    raise ContextError("the consumer reached its budget")
        self.assertTrue(reader.stdout.closed)
        self.assertTrue(reader.killed)


CONNECT_PROBE = """
import socket
import sys

try:
    socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5).close()
except OSError:
    sys.exit(1)
sys.exit(0)
"""


READ_PROBE = """
import sys

try:
    with open(sys.argv[1], "rb") as handle:
        handle.read(1)
except OSError:
    sys.exit(1)
sys.exit(0)
"""

#: Set by the CI job that installs a real isolation mechanism. There, a host
#: without one is a broken job rather than a host that cannot be asked, so the
#: skip becomes a failure: coverage that silently skips is coverage nobody has.
REQUIRE_CONTAINMENT = "CODE_MOWER_REQUIRE_CONTAINMENT"


def containment_evidence() -> str:
    """Why this host offered no mechanism, in the terms the probe decided in.

    A required job that fails with "none" tells an operator nothing they can
    act on: a launcher can be absent, present but untrusted, or present and
    trusted and unable to start a child at all. So the failure carries the
    control's verdict, each candidate's verdict, and the launcher's own stderr
    -- which is where a refused namespace or an impossible bind says so.
    """
    lines: list[str] = []
    readable = lifecycle._interpreter_read_paths()
    with tempfile.TemporaryDirectory() as scratch:
        lines.append(f"control (no prefix): {lifecycle._classify_probe((), cwd=scratch)}")
        for name, path in lifecycle._SANDBOX_CANDIDATES:
            launcher = lifecycle._trusted_launcher(path)
            if launcher is None:
                lines.append(f"{name} at {path}: exists={os.path.exists(path)}, not trusted")
                continue
            prefix = lifecycle._prefix_for(
                lifecycle.Containment(name=name, launcher=launcher),
                writable=(scratch,),
                readable=readable,
            )
            started = subprocess.run(
                [*prefix, sys.executable, "-c", "print('started')"],
                check=False,
                capture_output=True,
                timeout=120,
                cwd=scratch,
            )
            lines.append(
                f"{name} at {path}: verdict={lifecycle._classify_probe(prefix, cwd=scratch)}"
                f" start={started.returncode} stdout={started.stdout[:200]!r}"
                f" stderr={started.stderr[:400]!r}"
            )
    return "\n".join(lines)


def require_containment(test: unittest.TestCase) -> lifecycle.Containment:
    mechanism = lifecycle.containment_mechanism()
    if mechanism is None:
        if os.environ.get(REQUIRE_CONTAINMENT) == "1":
            test.fail(
                "this job requires a verified isolation mechanism and this host offers none\n"
                + containment_evidence()
            )
        test.skipTest("this host offers no OS sandbox that contains a child process")
    return mechanism


@contextlib.contextmanager
def stand_in_containment(prefix: tuple[str, ...]):
    """A verified mechanism whose argv is a fixed stand-in.

    The real prefix is a function of the host, so a test that wants to read the
    argv a launch was given -- rather than to prove containment -- pins it.
    ``_provider_read_paths`` goes with it: the exposure a build computes names
    an install that only a host with the pinned provider on it actually has.
    """
    with mock.patch.object(
        lifecycle, "containment_mechanism", lambda: lifecycle.Containment("stand-in", "/sandbox")
    ):
        with mock.patch.object(lifecycle, "containment_prefix", lambda **keywords: prefix):
            with mock.patch.object(lifecycle, "_provider_read_paths", lambda command: ()):
                yield


@contextlib.contextmanager
def no_containment():
    """A host that offers no mechanism at all."""
    with mock.patch.object(lifecycle, "containment_mechanism", lambda: None):
        yield


class NetworkIsolationTests(unittest.TestCase):
    """The provider's network boundary, against a socket that is really there."""

    def setUp(self) -> None:
        self.listener = socket.socket()
        self.addCleanup(self.listener.close)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]

    def connect(self, prefix: tuple[str, ...], *, cwd: Path | None = None) -> int:
        # ``cwd`` names a directory inside the exposure, because that is where a
        # build's provider starts: the boundary does not expose this process's
        # own working directory, and a child asked to start in a directory its
        # sandbox does not have never runs at all.
        return subprocess.run(
            [*prefix, sys.executable, "-c", CONNECT_PROBE, str(self.port)],
            check=False,
            capture_output=True,
            timeout=120,
            cwd=None if cwd is None else str(cwd),
        ).returncode

    def test_an_unsandboxed_child_reaches_the_listening_socket(self) -> None:
        # The control. Without it, a sandboxed child that failed to start for
        # some unrelated reason would read as proof of isolation.
        self.assertEqual(self.connect(()), 0)

    def real_prefix(self, scratch: Path) -> tuple[str, ...]:
        """The argv this host would really confine a build with."""
        require_containment(self)
        return lifecycle.containment_prefix(
            writable=(scratch,), readable=lifecycle._interpreter_read_paths()
        )

    def test_a_sandboxed_child_cannot_reach_the_listening_socket(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            prefix = self.real_prefix(Path(scratch))
            self.assertEqual(self.connect(prefix, cwd=Path(scratch)), 1)

    def test_the_selected_mechanism_really_contains_a_child_on_this_host(self) -> None:
        """The integration control: the real mechanism, not a stand-in for one.

        The synthetic launchers below pin the classification *algorithm* on
        every host, including hosts with no sandbox at all. They cannot show
        that ``sandbox-exec`` or ``bwrap`` as this module spells them actually
        confines anything. Where one of them is available, this runs it for
        real: an unconfined child must reach a listener that is really there
        and read a secret planted outside its exposure, and a child under the
        selected prefix must do neither.
        """
        with tempfile.TemporaryDirectory() as scratch:
            prefix = self.real_prefix(Path(scratch))
            self.assertEqual(lifecycle._classify_probe((), cwd=scratch), lifecycle._REACHED)
            self.assertEqual(lifecycle._classify_probe(prefix, cwd=scratch), lifecycle._CONTAINED)

    def test_the_selected_mechanism_hides_a_file_outside_the_exposure(self) -> None:
        """The filesystem half, named separately from the classifier that uses it.

        The reproduction this replaces read an external ignored ``.env`` through
        the selected sandbox: the macOS profile denied the network and allowed
        the whole host filesystem, and ``--dev-bind / /`` did the same on Linux.
        A working directory is not a boundary. What is exposed is exposed; a
        secret beside it is not there at all.
        """
        with tempfile.TemporaryDirectory() as scratch:
            prefix = self.real_prefix(Path(scratch))
            exposed = Path(scratch) / "inside"
            exposed.write_text("visible", encoding="utf-8")
            hidden = self.root_outside() / ".env"
            hidden.write_text("SECRET=planted", encoding="utf-8")
            self.assertEqual(self.read_through(prefix, exposed, cwd=Path(scratch)), 0)
            self.assertEqual(self.read_through(prefix, hidden, cwd=Path(scratch)), 1)

    def root_outside(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def read_through(self, prefix: tuple[str, ...], path: Path, *, cwd: Path | None = None) -> int:
        return subprocess.run(
            [*prefix, sys.executable, "-c", READ_PROBE, str(path)],
            check=False,
            capture_output=True,
            timeout=120,
            cwd=None if cwd is None else str(cwd),
        ).returncode

    def test_a_mechanism_that_denies_only_the_network_is_not_a_boundary(self) -> None:
        """Half a boundary classifies as none.

        A launcher that gives its child an empty network namespace and leaves
        the host filesystem in place is exactly what this module used to select.
        The child reports it, and the classification refuses it rather than
        recording the half it liked.
        """
        launcher = self.network_only_launcher(self.closed_port())
        self.assertEqual(lifecycle._classify_probe((launcher,)), lifecycle._UNUSABLE)

    def network_only_launcher(self, port: int) -> str:
        """Redirects the probe at a closed port but leaves the secret readable."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "network-only-launcher"
        path.write_text(f'#!/bin/sh\nexec "$1" "$2" "$3" {port} "$5" "$6"\n', encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    def test_a_launcher_on_PATH_cannot_shadow_a_trusted_one(self) -> None:
        """Every candidate is an absolute path, so ``PATH`` decides nothing.

        A launcher resolved through the inherited ``PATH`` can be shadowed by a
        program that computes the probe's evidence and reports containment
        without establishing any, and the whole verdict is then forged. The
        shadow is planted with the names this module looks for and must not be
        selected -- nor even consulted.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for name in ("bwrap", "unshare", "sandbox-exec"):
            shadow = Path(directory.name) / name
            shadow.write_text("#!/bin/sh\nexit 40\n", encoding="utf-8")
            shadow.chmod(0o700)
        for _, candidate in lifecycle._SANDBOX_CANDIDATES:
            self.assertTrue(os.path.isabs(candidate), candidate)
            self.assertFalse(candidate.startswith(directory.name))
        with mock.patch.dict(os.environ, {"PATH": directory.name}):
            with mock.patch.object(
                lifecycle, "_classify_probe", lambda prefix, **keywords: lifecycle._REACHED
            ):
                # Every candidate classifies as "reached", so nothing can be
                # selected; the point is that the shadow was never a candidate.
                self.assertIsNone(lifecycle._probe_containment())

    def test_a_launcher_anybody_could_replace_is_not_trusted(self) -> None:
        """Ownership and ancestry, not just the file's own mode.

        An executable in a directory somebody else may write can be replaced
        between the probe that trusted it and the build that runs it. A
        system launcher is the positive control: root-owned, in root-owned
        directories nobody else may write.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        launcher = Path(directory.name) / "launcher"
        launcher.write_text("#!/bin/sh\nexit 40\n", encoding="utf-8")
        launcher.chmod(0o700)
        os.chmod(directory.name, 0o777)
        self.assertIsNone(lifecycle._trusted_launcher(str(launcher)))
        self.assertIsNone(lifecycle._trusted_launcher("/nonexistent/launcher"))
        if not os.path.isfile("/usr/bin/env"):  # pragma: no cover - platform
            self.skipTest("no system launcher to use as the positive control")
        self.assertEqual(lifecycle._trusted_launcher("/usr/bin/env"), "/usr/bin/env")

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

    def closed_port(self) -> int:
        """A loopback port that refuses connections, as an empty namespace does.

        Bound and never listened on, rather than bound and released: a released
        ephemeral port can be handed straight back to the listener this test is
        trying to prove unreachable. Holding it means the refusal is the one
        this test arranged.
        """
        held = socket.socket()
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", 0))
        return held.getsockname()[1]

    def redirecting_launcher(self, port: int) -> str:
        """A stand-in for a launcher that gives its child its own loopback.

        This is the shape ``bwrap --unshare-net`` has: the child really runs,
        loopback really comes up inside the new namespace, and the connection is
        *refused* because the host's listener is not in there with it. The
        launcher runs the probe it was handed against a port nothing is on,
        which is what the child would have seen, and points it at a path that
        does not exist in place of the planted secret, which is what a child
        with no view of the host filesystem sees. ``$6`` is the nonce,
        forwarded so the child can still show it ran: a launcher that swallowed
        it would be a launcher that did not run the probe.
        """
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "namespace-launcher"
        path.write_text(
            f'#!/bin/sh\nexec "$1" "$2" "$3" {port} /nonexistent/secret "$6"\n',
            encoding="utf-8",
        )
        path.chmod(0o700)
        return str(path)

    def test_a_launcher_that_never_runs_the_child_is_rejected(self) -> None:
        """An exit code is not evidence, and the denial code least of all.

        A launcher that exits with the probe's own "could not connect" code
        without executing anything confines nothing, and this is the shape a
        broken or hostile launcher has. Nothing reaches the listener either --
        nothing ran -- so a classifier reading the exit code alone would accept
        it as a boundary and every build would then run its provider
        unconfined. The child's per-run evidence is what separates the two.
        """
        self.assertFalse(lifecycle._prefix_confines((self.launcher(lifecycle._PROBE_BASE),)))

    def test_a_child_that_reached_the_network_stack_is_rejected(self) -> None:
        self.assertFalse(lifecycle._prefix_confines((self.launcher(lifecycle._PROBE_BASE + lifecycle._PROBE_REACHED_LISTENER),)))

    def test_evidence_from_another_run_does_not_prove_this_one(self) -> None:
        """A replayed transcript is not a child that ran.

        The evidence is the digest of a nonce generated for one run, so a
        launcher that printed a previous run's evidence -- or one that parroted
        its own argv -- says nothing about this run.
        """
        stale = hashlib.sha256(b"an-earlier-nonce").hexdigest()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "replaying-launcher"
        path.write_text(
            f"#!/bin/sh\necho {stale}\necho \"$@\"\nexit {lifecycle._PROBE_BASE}\n",
            encoding="utf-8",
        )
        path.chmod(0o700)
        self.assertFalse(lifecycle._prefix_confines((str(path),)))

    def test_a_launcher_that_cannot_start_is_rejected(self) -> None:
        self.assertFalse(lifecycle._prefix_confines(("/nonexistent/launcher",)))

    def test_a_candidate_that_does_not_deny_the_network_is_rejected(self) -> None:
        # ``env`` runs its argument unchanged: a prefix that contains nothing
        # must not be mistaken for a boundary just because it launches.
        passthrough = ("/usr/bin/env",)
        if not os.access(passthrough[0], os.X_OK):  # pragma: no cover - platform
            self.skipTest("no pass-through launcher to test against")
        self.assertFalse(lifecycle._prefix_confines(passthrough))

    def test_a_refused_connection_inside_a_namespace_is_containment(self) -> None:
        """The bubblewrap case: refused by an empty namespace, not by the host.

        Classifying on the child's errno cannot tell that apart from a refusal
        by an unused host port, so a probe that only accepted ``EPERM``-shaped
        denials rejected working bubblewrap isolation and left such hosts unable
        to build at all. The verdict is taken at the listener instead: nothing
        arrived, so the child was contained.
        """
        self.assertTrue(lifecycle._prefix_confines((self.redirecting_launcher(self.closed_port()),)))

    def test_the_probe_proves_its_own_apparatus_before_trusting_a_refusal(self) -> None:
        """No candidate passes if an unsandboxed child cannot reach the listener.

        "Could not connect" only means containment when connecting was possible
        in the first place. If the control fails -- no probe interpreter,
        loopback unavailable -- every candidate would look like a boundary, so
        the whole probe refuses and builds refuse with it.
        """
        calls: list[tuple[str, ...]] = []

        def classify(prefix, **keywords):
            # The probe child starts inside the exposure, so every call names a
            # working directory; what this test reads is the prefix.
            calls.append(tuple(prefix))
            return lifecycle._CONTAINED

        with mock.patch.object(lifecycle, "_classify_probe", classify):
            self.assertIsNone(lifecycle._probe_containment())
        # The control ran, and nothing was probed after it failed.
        self.assertEqual(calls, [()])


#: What a provider that finished leaves behind: a completion claim and counts
#: that admit nothing outstanding.
FINISHED_REPORT = {"complete": True, "code_files": 3, "requeued": 0}


class ProviderLaunchTests(TemporaryWorkspace):
    """What ``subprocess_indexer`` actually hands the operating system."""

    def setUp(self) -> None:
        super().setUp()
        # Every artifact path this fixture has handed out, newest last, so a
        # test that launches more than once can name the run it means.
        self.artifacts: list[Path] = []

    def request(self, pin: lifecycle.GraphifyPin = PIN) -> lifecycle.IndexRequest:
        # A fresh directory per launch, because that is what a build hands the
        # provider: ``materialize_tracked_files`` refuses a destination that
        # already exists, and so the provider never sees state from a prior run.
        source = Path(tempfile.mkdtemp(dir=self.root))
        # The artifact is fresh for the same reason. A build writes it into the
        # generation it is about to publish, and the writer opens it O_EXCL --
        # a path shared between two launches would collide on the second rather
        # than exercise anything, so each request names its own.
        artifact = Path(tempfile.mkdtemp(dir=self.root)) / "graph.bin"
        self.artifacts.append(artifact)
        return lifecycle.IndexRequest(
            source_root=source,
            output_path=artifact,
            environment={"PATH": os.environ.get("PATH", "")},
            pin=pin,
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
        pin: lifecycle.GraphifyPin = PIN,
    ) -> tuple[list[str], lifecycle.IndexResult]:
        """Launch the adapter with the provider's side of the contract faked.

        ``extract`` writes its state beside the sources it was run over, so the
        stand-in has to leave that state behind for the adapter to collect --
        an exit status alone is not a finished build.
        """
        request = self.request(pin)
        recorded: list[list[str]] = []
        # What the adapter asked the operating system for, kept beside the argv
        # because the stream arrangement is as much of the contract as it is.
        self.launch_options: list[dict[str, object]] = []

        def fake_popen(argv, **kwargs):
            recorded.append(list(argv))
            self.launch_options.append(dict(kwargs))
            if state_directory:
                written = request.source_root / state_directory
                written.mkdir(exist_ok=True)
                (written / "graph.bin").write_bytes(b"graph-bytes")
                if report is not None:
                    (written / "manifest.json").write_text(json.dumps(report), encoding="utf-8")
            return FakeChild()

        # The stand-in spans the launch as well as the construction: the
        # exposure is built per run, from what *that* run exposes, so the
        # prefix is computed inside ``indexer(request)`` and a host with no
        # real mechanism would otherwise refuse there. ``Popen`` is patched
        # only around the launch, so the Git calls a build makes are never
        # intercepted by this stand-in.
        with stand_in_containment(sandbox):
            indexer = lifecycle.subprocess_indexer(executable)
            with mock.patch.object(subprocess, "Popen", fake_popen):
                result = indexer(request)
        return recorded[0], result

    def launched_argv(self, executable: str, *, sandbox=("/sandbox", "--deny")) -> list[str]:
        return self.run_indexer(executable, sandbox=sandbox)[0]

    def test_the_provider_is_launched_inside_the_sandbox(self) -> None:
        argv = self.launched_argv("graphify")
        self.assertEqual(argv[:3], ["/sandbox", "--deny", "graphify"])

    def test_the_exposure_is_built_from_what_this_run_writes(self) -> None:
        """The boundary names this request's copy and this request's scratch.

        A prefix computed once, at construction, could not name either: both
        are made per build. The launch would then confine the provider to
        somewhere other than the tree it was asked to index, and the
        redirected ``HOME`` and ``TMPDIR`` the environment points at would be
        absent from the child's filesystem view -- a provider that cannot
        start. So the exposure is read back from the call itself.
        """
        scratch = Path(tempfile.mkdtemp(dir=self.root))
        request = dataclasses.replace(self.request(), writable=(scratch,))
        recorded: list[dict[str, object]] = []

        def record_prefix(**keywords: object) -> tuple[str, ...]:
            recorded.append(dict(keywords))
            return ("/sandbox",)

        def fake_popen(argv, **kwargs):
            written = request.source_root / ".graphify"
            written.mkdir(exist_ok=True)
            (written / "graph.bin").write_bytes(b"graph-bytes")
            (written / "manifest.json").write_text(json.dumps(FINISHED_REPORT), encoding="utf-8")
            return FakeChild()

        with stand_in_containment(("/sandbox",)):
            indexer = lifecycle.subprocess_indexer("graphify")
            with mock.patch.object(lifecycle, "containment_prefix", record_prefix):
                with mock.patch.object(subprocess, "Popen", fake_popen):
                    indexer(request)
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["writable"], (request.source_root, scratch))

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

    def test_the_provider_leads_its_own_process_group(self) -> None:
        """A group, so a timed-out run can be stopped as a whole.

        The signal that ends an overrun has to reach workers the provider
        started -- under a launcher such as ``sandbox-exec`` the direct child is
        the launcher, not the indexer -- and a group is the only handle on them
        this process has. Safe only because no stream is inherited.
        """
        self.launched_argv("graphify")
        self.assertIs(self.launch_options[0]["start_new_session"], True)

    def test_the_provider_is_invoked_through_its_documented_extract_interface(self) -> None:
        # The interface the adopt decision evaluated, recorded in
        # docs/graphify-evaluation.md as ``extract`` plus options. An
        # ``index --source ... --output ...`` shape would be a different CLI.
        argv = self.launched_argv("graphify")
        self.assertEqual(argv[3:], ["extract", *PIN.options])
        self.assertNotIn("--source", argv)
        self.assertNotIn("--output", argv)

    def test_extraction_is_restricted_to_code_and_never_clusters(self) -> None:
        """The adoption conditions are enforced at the launch, not assumed.

        A pin is free to name no options at all, and one that did would
        otherwise have launched the provider into clustering and whatever
        extraction it does by default -- both of which are separate decisions
        nobody has taken.
        """
        bare = lifecycle.GraphifyPin(distribution="graphifyy", version="0.9.58", wheel_sha256="a" * 64)
        argv, _ = self.run_indexer("graphify", pin=bare)
        self.assertEqual(argv[3:], ["extract", "--code-only", "--no-cluster"])

    def test_the_launch_restricts_extraction_even_if_the_pin_did_not(self) -> None:
        # The pin normalizes its own options, so this reaches past the
        # constructor to prove the guarantee does not rest on it alone.
        stripped = lifecycle.GraphifyPin(distribution="graphifyy", version="0.9.58", wheel_sha256="a" * 64)
        object.__setattr__(stripped, "options", ())
        argv, _ = self.run_indexer("graphify", pin=stripped)
        self.assertEqual(argv[3:], ["extract", "--code-only", "--no-cluster"])

    def test_the_collected_artifact_holds_the_state_the_provider_wrote(self) -> None:
        _, result = self.run_indexer("graphify")
        self.assertEqual(result.completeness, lifecycle.COMPLETE)
        self.assertEqual(result.indexed_files, 3)
        names = self.archived_names(self.artifacts[-1].read_bytes())
        self.assertIn("graph.bin", names)

    def archived_names(self, artifact: bytes) -> list[str]:
        with tarfile.open(fileobj=io.BytesIO(artifact), mode="r") as archive:
            return archive.getnames()

    def test_collecting_the_same_state_twice_produces_the_same_bytes(self) -> None:
        # The manifest binds a digest of the artifact, so two builds of one
        # commit have to agree on the bytes down to the archive metadata.
        self.run_indexer("graphify")
        self.run_indexer("graphify")
        first, second = (artifact.read_bytes() for artifact in self.artifacts)
        self.assertEqual(first, second)

    def test_packing_is_bounded_by_the_archive_and_not_by_the_file_sizes(self) -> None:
        """Empty files are not free: their headers are bytes this process holds.

        The budget is enforced on the serialized archive as it is written, so
        state whose contents sum to nothing at all is still refused once the
        archive it turns into would exceed what this process may allocate.
        """
        state = self.root / "packed"
        state.mkdir()
        for index in range(16):
            (state / f"node-{index:02d}.bin").write_bytes(b"")
        self.assertEqual(sum(path.stat().st_size for path in state.iterdir()), 0)
        with mock.patch.object(lifecycle, "MAX_ARTIFACT_BYTES", 2048):
            with self.assertRaises(ContextError):
                lifecycle._pack_state(state)

    def test_packing_refuses_more_entries_than_its_budget_allows(self) -> None:
        # Bounded while the names are collected, before anything is archived.
        state = self.root / "entries"
        state.mkdir()
        for index in range(4):
            (state / f"node-{index}.bin").write_bytes(b"x")
        with mock.patch.object(lifecycle, "MAX_ARTIFACT_ENTRIES", 3):
            with self.assertRaises(ContextError):
                lifecycle._pack_state(state)
        self.assertEqual(len(self.archived_names(lifecycle._pack_state(state))), 4)

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

        def fake_popen(argv, **kwargs):
            written = request.source_root / ".graph"
            written.mkdir(exist_ok=True)
            (written / "graph.bin").write_bytes(b"graph-bytes")
            (written / "manifest.json").write_bytes(b"{not json")
            return FakeChild()

        with stand_in_containment(("/sandbox",)):
            indexer = lifecycle.subprocess_indexer("graphify")
            with mock.patch.object(subprocess, "Popen", fake_popen):
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

        def fake_popen(argv, **kwargs):
            written = request.source_root / ".graphify"
            written.mkdir(exist_ok=True)
            (written / "graph.bin").write_bytes(b"graph-bytes")
            (written / "manifest.json").write_bytes(oversized)
            return FakeChild()

        def refuse_whole_file_read(self: Path) -> bytes:
            raise AssertionError(f"{self} was read whole")

        with stand_in_containment(("/sandbox",)):
            indexer = lifecycle.subprocess_indexer("graphify")
            with mock.patch.object(subprocess, "Popen", fake_popen):
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

        def fake_popen(argv, **kwargs):
            launched.append(list(argv))
            return FakeChild()

        with stand_in_containment(("/sandbox",)):
            indexer = lifecycle.subprocess_indexer("graphify")
            with mock.patch.object(subprocess, "Popen", fake_popen):
                with self.assertRaises(ContextError):
                    indexer(request)
        self.assertEqual(launched, [])
        self.assertFalse(request.output_path.exists())

    def test_an_overrun_is_stopped_before_it_is_reported_as_one(self) -> None:
        """The error arrives after the group is stopped, not before.

        ``build_graph`` deletes the scratch directory as soon as the adapter
        raises. If the report came first, a worker that outlived the timeout
        would still be writing into a directory being removed underneath it.
        """
        request = self.request()
        order: list[str] = []

        def fake_popen(argv, **kwargs):
            return FakeChild(overruns=True)

        def record_termination(child, group) -> None:
            order.append("stopped")

        with stand_in_containment(("/sandbox",)):
            indexer = lifecycle.subprocess_indexer("graphify")
            with mock.patch.object(subprocess, "Popen", fake_popen):
                with mock.patch.object(lifecycle, "_terminate_process_group", record_termination):
                    with self.assertRaises(ContextError) as raised:
                        indexer(request)
                    order.append("reported")
        self.assertEqual(order, ["stopped", "reported"])
        self.assertIn("time budget", str(raised.exception))
        self.assertFalse(request.output_path.exists())

    def test_a_host_without_a_sandbox_refuses_to_launch_a_provider(self) -> None:
        with no_containment():
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


@unittest.skipUnless(hasattr(os, "killpg"), "process groups are a POSIX facility")
class ExtractionOverrunTests(unittest.TestCase):
    """What a timed-out provider leaves running, proved against real processes.

    An assertion about which signal was sent would have passed on code that
    signalled only the direct child, which is the whole defect: an indexer that
    forks workers -- and a launcher such as ``sandbox-exec``, where the direct
    child is the launcher rather than the indexer -- outlives that signal and
    keeps writing into a scratch directory the build is about to delete.
    """

    #: Stands in for a provider that starts a worker and then overruns. The
    #: worker's pid is written where the test can read it, because the point is
    #: what happens to a process this module never had a handle on.
    PROVIDER = """
import subprocess
import sys
import time

worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
with open(sys.argv[1], "w") as handle:
    handle.write(str(worker.pid))
time.sleep(300)
"""

    #: Stands in for a provider that starts a worker and then exits on its own,
    #: leaving the worker running. The leader's exit is ordinary -- it even
    #: reports a status -- and says nothing about whether the work has stopped.
    ABANDONS_WORKER = """
import subprocess
import sys

worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
with open(sys.argv[1], "w") as handle:
    handle.write(str(worker.pid))
raise SystemExit(5)
"""

    def reaped(self, pid: int, *, within: float = 15.0) -> bool:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(0.05)
        return False

    def test_a_group_that_cannot_be_established_as_empty_fails_the_run(self) -> None:
        """Sending SIGKILL is not the group being gone.

        The caller's next act is to pack or delete the state these processes
        are writing, so returning on the strength of a signal that was sent --
        rather than on a group that was observed empty -- hands the rest of the
        build a race it cannot see. A group that outlasts the kill fails the
        run instead.
        """
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(lifecycle, "_group_is_empty", lambda group: False):
                with mock.patch.object(lifecycle, "_await_group_exit", lambda group, timeout: None):
                    with self.assertRaises(ContextError) as raised:
                        lifecycle._run_contained(
                            [sys.executable, "-c", "pass"],
                            environment={"PATH": os.environ.get("PATH", "")},
                            cwd=directory,
                            timeout=30.0,
                        )
        self.assertIn("could not be stopped", str(raised.exception))

    def test_a_timed_out_run_takes_the_workers_it_started_with_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorded = Path(directory) / "worker.pid"
            with self.assertRaises(subprocess.TimeoutExpired):
                lifecycle._run_contained(
                    [sys.executable, "-c", self.PROVIDER, str(recorded)],
                    environment={"PATH": os.environ.get("PATH", "")},
                    cwd=directory,
                    timeout=3.0,
                )
            worker = int(recorded.read_text(encoding="utf-8"))
            self.assertNotEqual(worker, os.getpid())
            self.assertTrue(self.reaped(worker), "a worker outlived the run that started it")

    def test_a_cancelled_run_takes_the_workers_it_started_with_it(self) -> None:
        """Ctrl-C is not the timeout, and the provider never sees the signal.

        The child leads its own session, so the terminal's SIGINT reaches this
        process and not the provider. Cleaning up only on ``TimeoutExpired``
        left ``KeyboardInterrupt`` to unwind past a running provider and its
        workers, while ``build_graph`` deleted the scratch directory they were
        writing into. Every exit from the wait stops the group now, not just
        the one the timeout takes.
        """
        original = subprocess.Popen.wait
        cancelled: list[bool] = []

        def wait(child, timeout=None):
            if cancelled:
                return original(child, timeout=timeout)
            cancelled.append(True)
            # Interrupt once the provider has a worker to abandon; an interrupt
            # delivered before that would prove nothing about descendants.
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                try:
                    if recorded.read_text(encoding="utf-8").strip():
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            recorded = Path(directory) / "worker.pid"
            with mock.patch.object(subprocess.Popen, "wait", wait):
                with self.assertRaises(KeyboardInterrupt):
                    lifecycle._run_contained(
                        [sys.executable, "-c", self.PROVIDER, str(recorded)],
                        environment={"PATH": os.environ.get("PATH", "")},
                        cwd=directory,
                        timeout=300.0,
                    )
            worker = int(recorded.read_text(encoding="utf-8"))
            self.assertNotEqual(worker, os.getpid())
            self.assertTrue(self.reaped(worker), "a worker outlived the run that was cancelled")

    def test_a_provider_that_exits_leaves_no_worker_behind_it(self) -> None:
        """A normal exit is not evidence that the work stopped.

        Cleaning up only on the timeout and the interrupt left the ordinary
        ending -- the leader returning a status while a worker it started is
        still running -- to be trusted. ``subprocess_indexer`` would then pack
        an artifact being concurrently modified, and ``build_graph`` would
        delete a scratch directory still being written into. The status still
        comes back; it now means what it appears to mean.
        """
        with tempfile.TemporaryDirectory() as directory:
            recorded = Path(directory) / "worker.pid"
            returncode = lifecycle._run_contained(
                [sys.executable, "-c", self.ABANDONS_WORKER, str(recorded)],
                environment={"PATH": os.environ.get("PATH", "")},
                cwd=directory,
                timeout=60.0,
            )
            self.assertEqual(returncode, 5)
            worker = int(recorded.read_text(encoding="utf-8"))
            self.assertNotEqual(worker, os.getpid())
            self.assertTrue(
                self.reaped(worker),
                "a worker outlived the provider that started it and returned",
            )

    def test_a_run_that_finishes_in_time_reports_its_own_status(self) -> None:
        # The containment is not a behaviour change for an ordinary run: the
        # exit status still comes back, and nothing is signalled.
        with tempfile.TemporaryDirectory() as directory:
            returncode = lifecycle._run_contained(
                [sys.executable, "-c", "raise SystemExit(3)"],
                environment={"PATH": os.environ.get("PATH", "")},
                cwd=directory,
                timeout=60.0,
            )
        self.assertEqual(returncode, 3)

    def test_a_run_that_leaves_nothing_behind_is_not_charged_the_grace_period(self) -> None:
        # Checking the group on every exit has to be free in the case that is
        # every real build: an empty group is a signal-0 probe, not a wait, so
        # a provider that exited with no descendants is finished the moment its
        # status is read.
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            lifecycle._run_contained(
                [sys.executable, "-c", "raise SystemExit(0)"],
                environment={"PATH": os.environ.get("PATH", "")},
                cwd=directory,
                timeout=60.0,
            )
            elapsed = time.monotonic() - started
        self.assertLess(
            elapsed,
            lifecycle._TERMINATION_GRACE_SECONDS,
            "a clean run waited out a grace period it had nothing to wait for",
        )


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

    def test_state_is_refused_behind_a_symlink_into_a_repository(self) -> None:
        """A lexical ancestor walk does not see the repository through a link.

        ``--state-dir /outside/link/state`` names no repository in its own
        spelling, but ``/outside/link`` can point at a directory inside one, and
        the ``O_NOFOLLOW`` opens only cover the final component of each
        directory this lane creates. Resolved, the path is inside the checkout
        and the artifacts would land in it.
        """
        inside = self.repository / "subdir"
        inside.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        os.symlink(inside, outside / "link")
        with self.assertRaises(ContextError):
            self.build(root=outside / "link" / "graph-state")
        self.assertFalse((inside / "graph-state").exists())

    def test_an_ordinary_symlinked_ancestor_is_resolved_not_refused(self) -> None:
        # Resolving, not rejecting: private roots legitimately sit behind
        # links -- macOS reaches ``/tmp`` through a link into its ``private``
        # directory -- so a symlinked ancestor outside any repository must
        # still build.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.root / "linked-state")
        manifest = self.build(root=self.root / "linked-state" / "graph")
        self.assertEqual(manifest.completeness, lifecycle.COMPLETE)

    def test_an_ancestor_retargeted_after_the_check_does_not_move_the_state(self) -> None:
        """Check and use travel the same path, so retargeting between them does nothing.

        Checking a resolved snapshot and then writing through the original
        spelling is two different paths: ``/outside/link`` can resolve outside
        every repository when it is checked and point into one by the time the
        first directory is created. The state root is canonicalized once, at
        construction, and every later open goes through that canonical path, so
        a link swapped afterwards is no longer on the route.
        """
        safe = self.root / "safe"
        safe.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        link = outside / "link"
        os.symlink(safe, link)
        state = lifecycle.GraphStateRoot(self.repository, root=link / "graph-state")
        inside = self.repository / "subdir"
        inside.mkdir()
        link.unlink()
        os.symlink(inside, link)
        state.ensure()
        self.assertTrue(state.path.is_relative_to(safe))
        self.assertTrue((safe / "graph-state" / "graph").is_dir())
        self.assertFalse((inside / "graph-state").exists())

    def test_a_component_that_becomes_a_symlink_before_creation_is_refused(self) -> None:
        """The half a canonical snapshot cannot cover: a component that is not there yet.

        Resolving the root at construction settles what its *existing*
        ancestors mean. It cannot settle what a component nobody has created
        yet will mean, and a recursive ``mkdir`` would follow whatever appears
        there. Here the nested root is absent at construction and an ancestor
        of it is created as a link into the repository before ``ensure``: the
        creation must refuse rather than write state inside the repository.
        """
        outside = self.root / "outside"
        outside.mkdir()
        absent = outside / "deep" / "state"
        state = lifecycle.GraphStateRoot(self.repository, root=absent)
        inside = self.repository / "subdir"
        inside.mkdir()
        os.symlink(inside, outside / "deep")
        with self.assertRaises(ContextError):
            state.ensure()
        self.assertFalse((inside / "state").exists())

    def test_a_manifest_no_reader_could_load_publishes_nothing(self) -> None:
        """Publication validates the whole manifest before it touches state.

        A manifest that this process can write but ``load_manifest`` refuses
        would otherwise become ``current``, prune the generation that worked,
        and read back ``invalid`` on the next status -- a build reporting
        success while destroying the only usable graph.
        """
        first = self.build()
        state = lifecycle.GraphStateRoot(self.repository, root=self.state)
        unreadable = dataclasses.replace(
            first,
            generation=uuid.uuid4().hex,
            skipped_paths=lifecycle.MAX_SKIPPED_PATHS + 1,
        )
        with self.assertRaises(ContextError):
            state.publish(unreadable, b"graph-bytes")
        self.assertEqual(state.current_generation(), first.generation)
        self.assertEqual(list(lifecycle.iter_generations(self.repository, root=self.state)), [first.generation])
        self.assertEqual(state.read_manifest(first.generation), first)


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

    def test_git_children_ignore_replacement_objects(self) -> None:
        self.assertEqual(lifecycle.git_environment()["GIT_NO_REPLACE_OBJECTS"], "1")

    def test_a_replacement_object_cannot_substitute_committed_content(self) -> None:
        """``refs/replace`` changes what a read returns, not what a commit names.

        Every ordinary Git read honours a replacement, so a census and a
        materialization would bind bytes the recorded commit and tree do not
        contain -- and deleting the replacement afterwards would leave
        ``graph_status`` still reporting ``current``, because it compares object
        names and nothing else. The unguarded read is asserted first so this
        cannot pass by the replacement quietly not applying.
        """
        tracked = "example_pkg/config.py"
        committed = (self.repository / tracked).read_text(encoding="utf-8")
        blob = git(self.repository, "rev-parse", f"HEAD:{tracked}").strip()
        decoy = self.root / "decoy.py"
        decoy.write_text("VALUE = 'substituted'\n", encoding="utf-8")
        substitute = git(self.repository, "hash-object", "-w", str(decoy)).strip()
        git(self.repository, "replace", blob, substitute)
        self.assertEqual(git(self.repository, "cat-file", "blob", blob), decoy.read_text(encoding="utf-8"))

        census = lifecycle.read_tracked_census(self.repository, self.commit())
        entry = next(item for item in census.entries if item.path == tracked)
        # ``ls-tree --long`` reports the replacement's size while still naming
        # the original blob, so the census is bound too, not only the content.
        self.assertEqual(entry.size, len(committed.encode("utf-8")))
        destination = self.root / "materialized"
        lifecycle.materialize_tracked_files(self.repository, census, destination)
        self.assertEqual((destination / tracked).read_text(encoding="utf-8"), committed)

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
        with stand_in_containment(("/sandbox",)):
            report = lifecycle.doctor_report(self.repository, pin=PIN, root=self.state)
        self.assertEqual(report["status"], "pass")

    def test_a_host_that_cannot_contain_a_provider_fails_doctor(self) -> None:
        self.build()
        with no_containment():
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
        require_containment(self)
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
        require_containment(self)
        code, output = self.run_command(
            "build", *self.base(),
            "--pin-file", str(self.pin_file()),
            "--indexer", str(self.indexer_script(complete=False)),
        )
        # The build says so itself. Reporting ``current`` here and ``partial``
        # one command later would describe an unusable generation as usable for
        # exactly as long as it took to ask again.
        self.assertEqual(code, 1, output)
        published = json.loads(output)
        self.assertFalse(published["usable"])
        self.assertEqual(published["completeness"], lifecycle.PARTIAL)
        code, output = self.run_command("status", *self.base())
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["state"], "partial")

    def test_an_incomplete_build_is_printed_as_partial(self) -> None:
        require_containment(self)
        arguments = [argument for argument in self.base() if argument != "--json"]
        code, output = self.run_command(
            "build", *arguments,
            "--pin-file", str(self.pin_file()),
            "--indexer", str(self.indexer_script(complete=False)),
        )
        self.assertEqual(code, 1, output)
        self.assertIn("Local graph: partial", output)
        self.assertNotIn("Local graph: current", output)

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
