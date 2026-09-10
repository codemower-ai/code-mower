from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest import mock

from code_mower import cli, file_locks, session, session_lease


LEASE_FILE = Path(".code-mower") / session_lease.LEASE_FILE_NAME


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _init_git_repo(path: Path) -> None:
    """Mark ``path`` as an ordinary Git checkout root: the CLI's lease commands
    auto-detect their working copy this way, so mutating tests need it."""
    (Path(path) / ".git").mkdir()


def _init_git_worktree(path: Path, *, gitdir: Path) -> None:
    """Mark ``path`` as a Git worktree pointing at ``gitdir``, the way ``git
    worktree add`` leaves a ``.git`` file instead of a ``.git`` directory."""
    (Path(path) / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")


def start_session(*args, host="claude", repo="team/project", as_json=True):
    """Run `code-mower session start` and capture its exit code and streams."""
    out, err = io.StringIO(), io.StringIO()
    argv = ["session", "start", "--repo", repo, "--host", host, "--with", "claude,codex", *args]
    if as_json:
        argv.append("--json")
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def run_lease(*args):
    """Run `code-mower session lease ...` and capture its exit code and streams."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["session", "lease", *args])
    return code, out.getvalue(), err.getvalue()


def expire_lease(*, path=LEASE_FILE, seconds=1):
    """Rewrite the stored lease so it is already past its expiry."""
    record = session_lease.read_lease(path)
    stale = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    path.write_text(json.dumps({**record, "expires_at": stale.isoformat()}), encoding="utf-8")
    return record


class SessionStartupLeaseTests(unittest.TestCase):
    def test_startup_takes_a_metadata_only_lease_recorded_in_the_brief(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            code, out, _ = start_session()
            self.assertEqual(code, 0)
            payload = json.loads(out)
            record = json.loads(LEASE_FILE.read_text(encoding="utf-8"))
            self.assertEqual(set(record), set(session_lease.LEASE_FIELDS))
            self.assertEqual(record["schema"], "code_mower.session_lease.v1")
            self.assertEqual(record["repo"], "team/project")
            self.assertEqual(record["orchestrator"], "claude")
            self.assertEqual(record["session_id"], payload["id"])
            stamps = {field: datetime.fromisoformat(record[field]) for field in
                      ("acquired_at", "renewed_at", "expires_at")}
            for field, stamp in stamps.items():
                with self.subTest(field=field):
                    self.assertEqual(stamp.utcoffset(), timedelta(0))
            self.assertEqual(stamps["acquired_at"], stamps["renewed_at"])
            self.assertGreater(stamps["expires_at"], stamps["renewed_at"])
            self.assertTrue(payload["lease"]["mutating"])
            self.assertEqual(payload["lease"]["state"], "held")
            self.assertEqual(payload["lease"]["session_id"], payload["id"])
            self.assertIn(f"Lease: held by Claude Code until {record['expires_at']}",
                          session.render_session(payload))

    def test_a_second_live_session_is_refused_with_owner_actions_and_writes_no_brief(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            first = json.loads(start_session()[1])
            code, out, err = start_session(host="codex")
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("already holds the mutating orchestrator lease for team/project", err)
            self.assertIn(f"holder: claude (session {first['id']})", err)
            self.assertIn("code-mower session lease show", err)
            self.assertIn("code-mower session lease release --session-id", err)
            self.assertIn("code-mower session lease release --force", err)
            self.assertIn("--dry-run", err)
            # the lease lives at the working-copy root, separate from the
            # briefs directory that --state-dir controls
            self.assertEqual(
                sorted(path.name for path in LEASE_FILE.parent.glob("*.json")),
                [session_lease.LEASE_FILE_NAME],
            )
            self.assertEqual(
                sorted(path.name for path in Path(session.DEFAULT_STATE_DIR).glob("*.json")),
                [f"{first['id']}.json"],
            )
            self.assertEqual(session_lease.read_lease(LEASE_FILE)["session_id"], first["id"])

    def test_a_refusal_for_a_different_repo_names_the_requested_repo(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            start_session(repo="team/project")
            code, out, err = start_session(host="codex", repo="other/repo")
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("already holds the mutating orchestrator lease for team/project", err)
            self.assertIn("requested: other/repo", err)

    def test_concurrent_acquisition_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contenders = 8
            ready = threading.Barrier(contenders)
            guard = threading.Lock()
            winners: list[str] = []

            def contend(index: int) -> None:
                ready.wait()
                try:
                    record = session_lease.acquire_lease(
                        repo="team/project", orchestrator="claude",
                        session_id=f"session-{index}", root=root,
                    )
                except session_lease.SessionLeaseError:
                    return
                with guard:
                    winners.append(record["session_id"])

            threads = [threading.Thread(target=contend, args=(index,)) for index in range(contenders)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(winners), 1)
            stored = session_lease.read_lease(session_lease.lease_path(root))
            self.assertEqual(stored["session_id"], winners[0])

    def test_forced_takeover_replaces_a_live_lease_only_when_asked_for(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            first = json.loads(start_session()[1])
            self.assertEqual(start_session(host="codex")[0], 1)
            code, out, _ = start_session("--force-lease", host="codex")
            self.assertEqual(code, 0)
            taken = json.loads(out)
            record = session_lease.read_lease(LEASE_FILE)
            self.assertEqual(record["orchestrator"], "codex")
            self.assertEqual(record["session_id"], taken["id"])
            self.assertNotEqual(record["session_id"], first["id"])
            self.assertEqual(record["acquired_at"], record["renewed_at"])

    def test_an_expired_lease_is_recovered_without_an_owner_decision(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            first = json.loads(start_session()[1])
            expired = expire_lease()
            self.assertEqual(session_lease.lease_state(session_lease.read_lease(LEASE_FILE)), "expired")
            code, out, _ = start_session(host="codex")
            self.assertEqual(code, 0)
            record = session_lease.read_lease(LEASE_FILE)
            self.assertEqual(record["session_id"], json.loads(out)["id"])
            self.assertNotEqual(record["session_id"], first["id"])
            self.assertGreater(datetime.fromisoformat(record["acquired_at"]),
                               datetime.fromisoformat(expired["acquired_at"]))

    def test_a_lease_file_this_version_cannot_honor_never_wedges_the_repository(self):
        unusable = (
            "",
            "not json at all",
            json.dumps(["a", "list"]),
            json.dumps({"schema": "code_mower.session_lease.v99", "repo": "team/project"}),
            json.dumps({"schema": session_lease.LEASE_SCHEMA, "repo": "team/project"}),
            json.dumps({field: "" for field in session_lease.LEASE_FIELDS}),
        )
        for content in unusable:
            with self.subTest(content=content[:24]), tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
                _init_git_repo(tmp)
                LEASE_FILE.parent.mkdir(parents=True)
                LEASE_FILE.write_text(content, encoding="utf-8")
                self.assertIsNone(session_lease.read_lease(LEASE_FILE))
                self.assertEqual(start_session()[0], 0)
                self.assertIsNotNone(session_lease.read_lease(LEASE_FILE))

    def test_invalid_utf8_lease_file_is_treated_as_malformed_and_recovered(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            LEASE_FILE.parent.mkdir(parents=True)
            LEASE_FILE.write_bytes(b"\xff\xfe\x00not valid utf-8")
            self.assertIsNone(session_lease.read_lease(LEASE_FILE))
            code, out, _ = start_session()
            self.assertEqual(code, 0)
            record = session_lease.read_lease(LEASE_FILE)
            self.assertIsNotNone(record)
            self.assertEqual(record["session_id"], json.loads(out)["id"])

        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            LEASE_FILE.parent.mkdir(parents=True)
            LEASE_FILE.write_bytes(b"\xff\xfe\x00not valid utf-8")
            code, out, _ = run_lease("release", "--force", "--json")
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["released"])
            self.assertFalse(LEASE_FILE.exists())

    def test_the_same_session_re_acquiring_renews_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = session_lease.acquire_lease(
                repo="team/project", orchestrator="claude", session_id="session-1",
                root=root, ttl_minutes=60,
            )
            again = session_lease.acquire_lease(
                repo="team/project", orchestrator="claude", session_id="session-1",
                root=root, ttl_minutes=600,
            )
            self.assertEqual(again["acquired_at"], first["acquired_at"])
            self.assertGreater(datetime.fromisoformat(again["expires_at"]),
                               datetime.fromisoformat(first["expires_at"]))


class ReadOnlySessionTests(unittest.TestCase):
    def test_dry_run_generation_needs_no_lease_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            code, out, _ = start_session("--dry-run")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertEqual(payload["lease"], {"state": "absent", "mutating": False})
            self.assertIn(session.READ_ONLY_LEASE_INSTRUCTION, payload["instructions"])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_read_only_generation_still_works_while_another_session_holds_the_lease(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            held = json.loads(start_session()[1])
            self.assertEqual(start_session("--dry-run", host="codex")[0], 0)
            code, out, _ = start_session("--no-lease", host="codex")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertFalse(payload["lease"]["mutating"])
            self.assertTrue(Path(payload["session_file"]).is_file())
            self.assertIn("Lease: none (read-only brief", session.render_session(payload))
            self.assertEqual(session_lease.read_lease(LEASE_FILE)["session_id"], held["id"])

    def test_a_read_only_brief_cannot_quietly_force_a_takeover(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            code, _, err = start_session("--no-lease", "--force-lease")
            self.assertEqual(code, 1)
            self.assertIn("--force-lease", err)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class LeaseCommandTests(unittest.TestCase):
    def test_inspect_renew_and_release_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            code, out, _ = run_lease("show", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["state"], "absent")
            self.assertIsNone(json.loads(out)["lease"])

            started = json.loads(start_session()[1])
            code, out, _ = run_lease("show", "--json")
            self.assertEqual(code, 0)
            shown = json.loads(out)
            self.assertEqual(shown["state"], "held")
            self.assertEqual(shown["lease"]["session_id"], started["id"])
            self.assertGreater(shown["expires_in_seconds"], 0)
            self.assertEqual(session_lease.read_lease(LEASE_FILE), shown["lease"])

            code, out, _ = run_lease(
                "renew", "--session-id", started["id"], "--lease-ttl-minutes", "30", "--json",
            )
            self.assertEqual(code, 0)
            renewed = json.loads(out)["lease"]
            self.assertEqual(renewed["acquired_at"], shown["lease"]["acquired_at"])
            self.assertGreaterEqual(datetime.fromisoformat(renewed["renewed_at"]),
                                    datetime.fromisoformat(shown["lease"]["renewed_at"]))
            self.assertLess(datetime.fromisoformat(renewed["expires_at"]),
                            datetime.fromisoformat(shown["lease"]["expires_at"]))

            code, out, _ = run_lease("release", "--session-id", started["id"], "--json")
            self.assertEqual(code, 0)
            released = json.loads(out)
            self.assertTrue(released["released"])
            self.assertEqual(released["previous"]["session_id"], started["id"])
            self.assertFalse(LEASE_FILE.exists())
            self.assertEqual(json.loads(run_lease("show", "--json")[1])["state"], "absent")

    def test_renew_and_release_refuse_a_live_lease_owned_by_another_session(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            started = json.loads(start_session()[1])
            code, _, err = run_lease("renew", "--session-id", "not-the-owner")
            self.assertEqual(code, 1)
            self.assertIn("already holds the mutating orchestrator lease", err)

            code, _, err = run_lease("release")
            self.assertEqual(code, 1)
            self.assertIn("code-mower session lease release --force", err)
            self.assertEqual(session_lease.read_lease(LEASE_FILE)["session_id"], started["id"])

            code, out, _ = run_lease("release", "--force", "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["previous"]["session_id"], started["id"])
            self.assertFalse(LEASE_FILE.exists())

    def test_an_expired_lease_is_not_renewed_but_is_free_to_release(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            started = json.loads(start_session()[1])
            expire_lease()
            code, _, err = run_lease("renew", "--session-id", started["id"])
            self.assertEqual(code, 1)
            self.assertIn("expired", err)
            self.assertIn("code-mower session start", err)

            code, out, _ = run_lease("release", "--json")
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["released"])
            self.assertEqual(json.loads(out)["previous_state"], "expired")

    def test_releasing_an_absent_lease_succeeds_and_reports_nothing_to_release(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            code, out, _ = run_lease("release", "--json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertFalse(payload["released"])
            self.assertIsNone(payload["previous"])
            self.assertIn("nothing to release", run_lease("release")[1])

    def test_rendered_inspection_names_the_holder_and_the_expiry(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            started = json.loads(start_session()[1])
            text = run_lease("show")[1]
            self.assertIn("Session lease: team/project", text)
            self.assertIn("State: held", text)
            self.assertIn("Orchestrator: claude", text)
            self.assertIn(started["id"], text)
            self.assertIn("left)", text)

            expire_lease()
            expired_text = run_lease("show")[1]
            self.assertIn("State: expired", expired_text)
            self.assertIn("the next code-mower session start takes it over", expired_text)

    def test_a_nonpositive_lease_ttl_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            code, _, err = start_session("--lease-ttl-minutes", "0")
            self.assertEqual(code, 1)
            self.assertIn("positive number of minutes", err)
            self.assertFalse(LEASE_FILE.exists())


class LeaseClockOrderingTests(unittest.TestCase):
    """Lock contention must never backdate a lease to a moment before it won the lock."""

    @staticmethod
    @contextmanager
    def _lock_held_for(lock_path: Path, *, seconds: float) -> Iterator[None]:
        holder_ready = threading.Event()
        release_holder = threading.Event()

        def hold_lock() -> None:
            with file_locks.exclusive_file_lock(lock_path):
                holder_ready.set()
                release_holder.wait()

        def delayed_release() -> None:
            time.sleep(seconds)
            release_holder.set()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        holder_ready.wait()
        releaser = threading.Thread(target=delayed_release)
        releaser.start()
        try:
            yield
        finally:
            holder.join()
            releaser.join()

    def test_acquire_samples_the_clock_after_lock_contention_not_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = session_lease.lease_path(root)
            path.parent.mkdir(parents=True)
            lock_path = path.with_name(f"{path.name}.lock")

            with self._lock_held_for(lock_path, seconds=0.2):
                call_started = datetime.now(timezone.utc)
                record = session_lease.acquire_lease(
                    repo="team/project", orchestrator="claude",
                    session_id="session-1", root=root,
                )

            acquired_at = datetime.fromisoformat(record["acquired_at"])
            self.assertGreaterEqual(acquired_at, call_started + timedelta(seconds=0.15))
            self.assertGreater(datetime.fromisoformat(record["expires_at"]), acquired_at)

    def test_renew_samples_the_clock_after_lock_contention_not_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_lease.acquire_lease(
                repo="team/project", orchestrator="claude",
                session_id="session-1", root=root, ttl_minutes=60,
            )
            path = session_lease.lease_path(root)
            lock_path = path.with_name(f"{path.name}.lock")

            with self._lock_held_for(lock_path, seconds=0.2):
                call_started = datetime.now(timezone.utc)
                payload = session_lease.renew_lease(
                    root=root, session_id="session-1", ttl_minutes=60,
                )

            renewed_at = datetime.fromisoformat(payload["lease"]["renewed_at"])
            self.assertGreaterEqual(renewed_at, call_started + timedelta(seconds=0.15))

    @staticmethod
    def _write_raw_lease(path: Path, *, expires_at: datetime) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        record = {
            "schema": session_lease.LEASE_SCHEMA,
            "repo": "team/project",
            "orchestrator": "claude",
            "session_id": "session-1",
            "acquired_at": now.isoformat(),
            "renewed_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        path.write_text(json.dumps(record), encoding="utf-8")

    def test_verify_live_lease_samples_the_clock_after_lock_contention_not_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = session_lease.lease_path(root)
            # Expires well before the lock releases, so a read that samples
            # the clock before contention (rather than after winning the
            # lock) would wrongly call this lease still held.
            self._write_raw_lease(path, expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.1))
            lock_path = path.with_name(f"{path.name}.lock")

            with self._lock_held_for(lock_path, seconds=0.2):
                result = session_lease.verify_live_lease(
                    repo="team/project", session_id="session-1", orchestrator="claude", root=root,
                )

            self.assertEqual(result["state"], session_lease.STATE_EXPIRED)
            self.assertFalse(result["mutating"])

    def test_verify_live_lease_honors_an_explicit_now_instead_of_the_real_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = session_lease.lease_path(root)
            injected_now = datetime.now(timezone.utc)
            # The real clock will have moved past expiry by the time the lock
            # is won; an explicitly injected `now` must still be honored
            # verbatim rather than resampled inside the lock.
            self._write_raw_lease(path, expires_at=injected_now + timedelta(seconds=0.1))
            lock_path = path.with_name(f"{path.name}.lock")

            with self._lock_held_for(lock_path, seconds=0.2):
                result = session_lease.verify_live_lease(
                    repo="team/project", session_id="session-1", orchestrator="claude",
                    now=injected_now, root=root,
                )

            self.assertEqual(result["state"], session_lease.STATE_HELD)
            self.assertTrue(result["mutating"])


class LeasePrivacyTests(unittest.TestCase):
    def test_the_lease_holds_only_coordination_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = session_lease.acquire_lease(
                repo="team/project", orchestrator="claude", session_id="session-1",
                root=Path(tmp),
            )
            self.assertEqual(set(record), set(session_lease.LEASE_FIELDS))
            self.assertEqual(len(session_lease.LEASE_FIELDS), 7)

    def test_no_export_or_upload_path_can_reach_the_lease(self):
        from code_mower.cloud_client import dogfood

        uploaded = [relative for relative, _ in dogfood.DEFAULT_DOGFOOD_REPORTS]
        self.assertEqual([path for path in uploaded if "session" in path], [])

        source_root = Path(session_lease.__file__).resolve().parents[1]
        readers = sorted(
            path.relative_to(source_root).as_posix()
            for path in source_root.rglob("*.py")
            if session_lease.LEASE_FILE_NAME in path.read_text(encoding="utf-8", errors="ignore")
        )
        self.assertEqual(readers, ["code_mower/session_lease.py"])

        lease_source = Path(session_lease.__file__).read_text(encoding="utf-8")
        for transport in ("import urllib", "import http", "import requests", "import socket",
                          "import subprocess", "urlopen"):
            with self.subTest(transport=transport):
                self.assertNotIn(transport, lease_source)


class BriefCompatibilityTests(unittest.TestCase):
    def test_briefs_written_before_leases_still_render_and_show(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            payload = session.build_session(
                repo="team/project", host="claude", selected=("claude", "codex"), config={},
            )
            self.assertNotIn("lease", payload)
            self.assertNotIn("Lease:", session.render_session(payload))
            legacy = Path("legacy-session.json")
            legacy.write_text(json.dumps(payload), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["session", "show", str(legacy), "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue()), payload)

    def test_a_saved_leased_brief_round_trips_through_session_show(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            payload = json.loads(start_session()[1])
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["session", "show", payload["session_file"], "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue()), payload)

    def test_show_renders_read_only_when_the_lease_lock_cannot_be_written(self):
        # session show re-verifies a saved brief's lease against the live lock
        # file, but the checkout itself may be read-only for this caller
        # (inspected from a mount, or by a user who doesn't own the lock).
        # That must degrade to a non-mutating render with reacquire guidance,
        # never crash and never claim the brief still carries authority.
        if os.geteuid() == 0:
            self.skipTest("root bypasses file write permissions")
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            payload = json.loads(start_session()[1])
            self.assertTrue(payload["lease"]["mutating"])
            lock_path = LEASE_FILE.with_name(f"{LEASE_FILE.name}.lock")
            original_mode = lock_path.stat().st_mode
            lock_path.chmod(0o400)
            try:
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(["session", "show", payload["session_file"], "--json"])
            finally:
                lock_path.chmod(original_mode)

            self.assertEqual(code, 0)
            shown = json.loads(out.getvalue())
            self.assertEqual(shown["lease"], {"state": session_lease.STATE_UNVERIFIABLE, "mutating": False})
            self.assertIn(session.STALE_LEASE_INSTRUCTION, shown["instructions"])
            # The underlying lease itself is untouched -- this was a read.
            record = session_lease.read_lease(LEASE_FILE)
            self.assertEqual(record["session_id"], payload["id"])


class WriteFailureCleanupTests(unittest.TestCase):
    def test_cleanup_after_a_write_failure_preserves_the_original_error_and_the_new_holder(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            original_release = session_lease.release_lease

            def race_then_release(*, session_id=None, force=False, now=None, root=None):
                # Simulate another session force-taking the lease in the narrow
                # window between this call's acquisition and its failed write.
                session_lease.acquire_lease(
                    repo="team/project", orchestrator="codex",
                    session_id="rival-session", root=root, force=True,
                )
                return original_release(session_id=session_id, force=force, now=now, root=root)

            with mock.patch.object(session_lease, "release_lease", side_effect=race_then_release), \
                 mock.patch.object(session.json, "dump", side_effect=OSError("disk full")):
                code, out, err = start_session()

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("disk full", err)
            self.assertNotIn("already holds the mutating orchestrator lease", err)
            record = session_lease.read_lease(LEASE_FILE)
            self.assertIsNotNone(record)
            self.assertEqual(record["session_id"], "rival-session")

    def test_a_destination_directory_that_cannot_be_created_leaves_no_orphaned_lease(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            # An existing file where --state-dir points makes mkdir fail with
            # FileExistsError, before the brief would ever be written.
            state_dir = Path(tmp) / "state-as-file"
            state_dir.write_text("not a directory", encoding="utf-8")

            code, out, err = start_session("--state-dir", str(state_dir))

            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertNotEqual(err, "")
            self.assertFalse(LEASE_FILE.exists())
            self.assertIsNone(session_lease.read_lease(LEASE_FILE))


if __name__ == "__main__":
    unittest.main()
