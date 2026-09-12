"""Offline lifecycle tests for the optional local repository graph (issue #913).

Every test here builds a real throwaway Git repository and runs the whole
lifecycle against it with an injected indexer. No graph package is installed,
imported, or required, and nothing reaches the network: the provider seam is a
callable, so the parts this repository is responsible for -- what gets
materialized, what the manifest binds, how a generation is published, and when
a consumer must refuse one -- are all provable locally.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
                    "vendor/.git/config", "a\\b.py")
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


class RemoveTests(TemporaryWorkspace):
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
        report = lifecycle.doctor_report(self.repository, pin=PIN, root=self.state)
        self.assertEqual(report["status"], "pass")

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

    def indexer_script(self) -> Path:
        """A stand-in for a pinned provider CLI, so no package is required."""
        path = self.root / "fake-indexer"
        path.write_text(
            "#!/bin/sh\n"
            'while [ "$#" -gt 0 ]; do\n'
            '  case "$1" in --output) shift; printf graph-bytes > "$1" ;; esac\n'
            "  shift\n"
            "done\n",
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
