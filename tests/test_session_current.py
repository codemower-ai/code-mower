from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from code_mower import cli, lane_status, session, session_current, session_lease


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
    (Path(path) / ".git").mkdir()


def start_session(*args, host="claude", repo="team/project"):
    out, err = io.StringIO(), io.StringIO()
    argv = ["session", "start", "--repo", repo, "--host", host, "--with", "claude,codex", *args, "--json"]
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    assert code == 0, err.getvalue()
    return json.loads(out.getvalue())


def show_current(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["session", "show", "--current", *args])
    return code, out.getvalue(), err.getvalue()


def _tree_snapshot(root: Path) -> dict[str, tuple]:
    """Every path under ``root`` with the bytes/timestamps a read must not change."""
    snapshot = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        snapshot[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, content)
    return snapshot


class ResolveCurrentSessionTests(unittest.TestCase):
    def test_a_live_default_state_session_is_found_from_root_and_any_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            saved = start_session()
            nested = Path("src") / "deep" / "er"
            nested.mkdir(parents=True)
            for start in (Path(tmp), nested):
                with self.subTest(start=str(start)):
                    result = session_current.resolve_current_session(start=start)
                    self.assertEqual(result["schema"], session_current.CURRENT_SESSION_SCHEMA)
                    self.assertEqual(result["state"], "active")
                    self.assertTrue(result["current"])
                    self.assertIsNone(result["guidance"])
                    self.assertEqual(result["lease"]["state"], "active")
                    self.assertEqual(result["lease"]["provider"], "claude")
                    self.assertEqual(set(result["lease"]), {"state", "provider", "expires_at"})
                    self.assertEqual(result["session"]["id"], saved["id"])
                    self.assertTrue(result["session"]["lease"]["mutating"])
                    self.assertEqual(result["session"]["lease"]["session_id"], saved["id"])

    def test_a_session_started_from_a_subdirectory_saves_under_the_root_and_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            _init_git_repo(tmp)
            nested = Path(tmp) / "pkg" / "inner"
            nested.mkdir(parents=True)
            with working_directory(nested):
                saved = start_session()
                self.assertEqual(
                    Path(saved["session_file"]),
                    (Path(tmp) / session_current.DEFAULT_STATE_DIR / f"{saved['id']}.json").resolve(),
                )
                self.assertFalse((nested / ".code-mower").exists())
                code, out, _ = show_current("--json")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out)["id"], saved["id"])
            with working_directory(tmp):
                explicit = start_session("--state-dir", "briefs", "--force-lease")
                self.assertEqual(Path(explicit["session_file"]), (Path(tmp) / "briefs" / f"{explicit['id']}.json").resolve())

    def test_a_brief_missing_rendered_fields_is_invalid_not_current(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            saved = start_session()
            path = Path(session_current.DEFAULT_STATE_DIR) / f"{saved['id']}.json"
            for missing in ("status", "instructions"):
                with self.subTest(missing=missing):
                    brief = json.loads(path.read_text(encoding="utf-8"))
                    del brief[missing]
                    path.write_text(json.dumps(brief), encoding="utf-8")
                    result = session_current.resolve_current_session()
                    self.assertEqual(result["state"], "brief_invalid")
                    self.assertFalse(result["current"])
                    code, out, err = show_current()
                    self.assertEqual((code, out), (1, ""))
                    self.assertIn("unavailable or invalid", err)
            brief = json.loads(path.read_text(encoding="utf-8"))
            brief["participants"] = [{"name": "Claude"}]
            path.write_text(json.dumps(brief), encoding="utf-8")
            self.assertEqual(session_current.resolve_current_session()["state"], "brief_invalid")

    def test_cli_shows_the_current_brief_from_a_subdirectory_with_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            saved = start_session()
            Path("nested").mkdir()
            with working_directory("nested"):
                code, out, err = show_current("--json")
                self.assertEqual((code, err), (0, ""))
                shown = json.loads(out)
                self.assertEqual(shown["id"], saved["id"])
                self.assertEqual(shown["lease"]["session_id"], saved["id"])
                code, out, err = show_current()
                self.assertEqual((code, err), (0, ""))
                self.assertIn("Code Mower session: team/project", out)
                self.assertIn("Lease: held by Claude Code", out)

    def test_current_is_strictly_read_only(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            start_session()
            # Remove the lock the startup acquisition left so a read that
            # takes the lock would be caught recreating it.
            LEASE_FILE.with_name(f"{LEASE_FILE.name}.lock").unlink()
            before = _tree_snapshot(Path(tmp))
            code, _, _ = show_current("--json")
            self.assertEqual(code, 0)
            code, _, _ = show_current()
            self.assertEqual(code, 0)
            session_current.resolve_current_session(state_dir="elsewhere")
            self.assertEqual(_tree_snapshot(Path(tmp)), before)

    def test_no_lease_and_missing_working_copy_return_nonzero_bounded_guidance(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            code, out, err = show_current()
            self.assertEqual((code, out), (1, ""))
            self.assertIn("no Git working copy", err)
            self.assertFalse(Path(".code-mower").exists())
            _init_git_repo(tmp)
            code, out, err = show_current("--json")
            self.assertEqual(code, 1)
            self.assertIn("no mutating orchestrator lease is held", err)
            diagnostic = json.loads(out)
            self.assertEqual(diagnostic["state"], "lease_absent")
            self.assertFalse(diagnostic["current"])
            self.assertIsNone(diagnostic["session"])
            self.assertEqual(diagnostic["lease"], {"state": "absent", "provider": None, "expires_at": None})
            self.assertIn("code-mower session show SESSION_FILE", diagnostic["guidance"])
            self.assertFalse(Path(".code-mower").exists())

    def test_expired_and_malformed_leases_are_diagnosed_without_a_brief(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            start_session()
            record = session_lease.read_lease(LEASE_FILE)
            stale = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            LEASE_FILE.write_text(json.dumps({**record, "expires_at": stale}), encoding="utf-8")
            result = session_current.resolve_current_session()
            self.assertEqual(result["state"], "lease_expired")
            self.assertEqual(result["lease"]["state"], "expired")
            self.assertEqual(result["lease"]["provider"], "claude")
            self.assertIsNone(result["session"])
            self.assertIn("session lease show", result["guidance"])
            LEASE_FILE.write_text("{not json", encoding="utf-8")
            result = session_current.resolve_current_session()
            self.assertEqual(result["state"], "lease_malformed")
            self.assertEqual(result["lease"], {"state": "malformed", "provider": None, "expires_at": None})
            code, out, err = show_current()
            self.assertEqual((code, out), (1, ""))
            self.assertIn("not a lease this version can read", err)

    def test_missing_brief_reports_the_safe_lease_and_asks_for_the_file(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            saved = start_session()
            Path(saved["session_file"]).unlink()
            result = session_current.resolve_current_session()
            self.assertEqual(result["state"], "brief_missing")
            self.assertEqual(result["lease"]["state"], "active")
            self.assertEqual(result["lease"]["provider"], "claude")
            self.assertNotIn(saved["id"], json.dumps(result))
            self.assertIn("--state-dir", result["guidance"])
            code, out, err = show_current()
            self.assertEqual((code, out), (1, ""))
            self.assertIn("saved brief was not found", err)
            self.assertNotIn(saved["id"], err)

    def test_a_custom_state_dir_is_confined_to_the_exact_lease_session_id(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            saved = start_session("--state-dir", "briefs/here")
            # Default location has no brief: --current alone must not find it.
            self.assertEqual(session_current.resolve_current_session()["state"], "brief_missing")
            # A newer, unrelated brief in the same directory is never selected by mtime.
            decoy = Path("briefs/here") / f"{'f' * 32}.json"
            decoy.write_text(json.dumps({**saved, "id": "f" * 32}), encoding="utf-8")
            os.utime(decoy, ns=(2**62, 2**62))
            code, out, err = show_current("--state-dir", "briefs/here", "--json")
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out)["id"], saved["id"])
            # A sibling directory is not scanned.
            result = session_current.resolve_current_session(state_dir="briefs")
            self.assertEqual(result["state"], "brief_missing")
            # And the wrong repository, session, or orchestrator is rejected.
            brief_path = Path(saved["session_file"])
            for field, value in (("repo", "other/repo"), ("orchestrator", "codex"), ("id", "a" * 32)):
                with self.subTest(field=field):
                    brief_path.write_text(json.dumps({**saved, field: value}), encoding="utf-8")
                    result = session_current.resolve_current_session(state_dir="briefs/here")
                    self.assertEqual(result["state"], "brief_mismatch")
                    self.assertIsNone(result["session"])
            brief_path.write_text("{}", encoding="utf-8")
            self.assertEqual(
                session_current.resolve_current_session(state_dir="briefs/here")["state"], "brief_invalid",
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are required")
    def test_symlinked_briefs_and_state_dirs_are_refused_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            saved = start_session()
            brief_path = Path(saved["session_file"])
            real = Path("real.json")
            real.write_bytes(brief_path.read_bytes())
            brief_path.unlink()
            try:
                brief_path.symlink_to(real.resolve())
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not permitted here")
            result = session_current.resolve_current_session()
            self.assertEqual(result["state"], "brief_refused")
            self.assertIsNone(result["session"])
            Path("linked-dir").symlink_to(Path(session.DEFAULT_STATE_DIR).resolve(), target_is_directory=True)
            result = session_current.resolve_current_session(state_dir="linked-dir")
            self.assertEqual(result["state"], "brief_refused")
            code, out, err = show_current()
            self.assertEqual((code, out), (1, ""))
            self.assertIn("refusing to follow it", err)

    def test_a_lease_replaced_or_expired_during_the_read_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            saved = start_session()
            original = session_lease.observe_lease_record

            def replace_after_first_read(*, root, now=None):
                observed = original(root=root, now=now)
                if observed["record"] is not None and observed["record"]["session_id"] == saved["id"]:
                    session_lease.release_lease(session_id=saved["id"])
                    session_lease.acquire_lease(repo="team/project", orchestrator="codex", session_id="b" * 32)
                return observed

            with mock.patch.object(session_lease, "observe_lease_record", replace_after_first_read):
                result = session_current.resolve_current_session()
            self.assertEqual(result["state"], "lease_changed")
            self.assertIsNone(result["session"])
            self.assertEqual(result["lease"]["provider"], "codex")

            session_lease.release_lease(force=True)
            saved = start_session()
            record = session_lease.read_lease(LEASE_FILE)
            expires = datetime.fromisoformat(record["expires_at"])
            clocks = iter([expires - timedelta(seconds=1), expires + timedelta(seconds=1)])
            original_state = session_lease.lease_state

            def ticking_state(record, *, now=None):
                return original_state(record, now=next(clocks))

            with mock.patch.object(session_lease, "lease_state", ticking_state):
                result = session_current.resolve_current_session()
            self.assertEqual(result["state"], "lease_expired")
            self.assertIsNone(result["session"])

    def test_direct_show_stays_compatible_and_argument_combinations_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            _init_git_repo(tmp)
            saved = start_session()
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(cli.main(["session", "show", saved["session_file"], "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue()), saved)
            for argv, message in (
                (["session", "show"], "pass a SESSION_FILE, or --current"),
                (["session", "show", saved["session_file"], "--current"], "not both"),
                (["session", "show", saved["session_file"], "--state-dir", "x"], "only with --current"),
            ):
                with self.subTest(argv=argv):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        self.assertEqual(cli.main(argv), 1)
                    self.assertEqual(out.getvalue(), "")
                    self.assertIn(message, err.getvalue())
            out = io.StringIO()
            with redirect_stdout(out), self.assertRaises(SystemExit):
                cli.main(["session", "show", "--help"])
            self.assertIn("--current", out.getvalue())
            self.assertIn("session lease show", out.getvalue())


class LanesStatusLeaseTests(unittest.TestCase):
    @staticmethod
    def _gh(args):
        if args[:2] in (["pr", "list"], ["run", "list"]):
            return []
        raise lane_status.LaneStatusUnavailable("unexpected gh call")

    @staticmethod
    def _completed(stdout: str = ""):
        import subprocess
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    def _status(self, *, repo: str, checkout: Path, as_json: bool):
        out = StringIO()
        with working_directory(checkout), redirect_stdout(out):
            code = lane_status.main(
                ["status", "--repo", repo, *(["--json"] if as_json else [])],
                gh_json_runner=self._gh,
                command_runner=lambda _args: self._completed(""),
            )
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_lanes_status_reports_the_checkout_lease_bound_to_the_requested_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _init_git_repo(root)
            payload = json.loads(self._status(repo="team/project", checkout=root, as_json=True))
            self.assertEqual(payload["orchestrator_lease"], {"state": "absent", "provider": None, "expires_at": None})
            self.assertIn("Orchestrator lease: absent", self._status(repo="team/project", checkout=root, as_json=False))

            with working_directory(root):
                saved = start_session()
            nested = root / "nested"
            nested.mkdir()
            payload = json.loads(self._status(repo="team/project", checkout=nested, as_json=True))
            lease = payload["orchestrator_lease"]
            self.assertEqual(set(lease), {"state", "provider", "expires_at"})
            self.assertEqual((lease["state"], lease["provider"]), ("active", "claude"))
            self.assertTrue(lease["expires_at"].endswith("Z"))
            self.assertNotIn(saved["id"], json.dumps(payload))
            self.assertNotIn(tmp, json.dumps(payload))
            text = self._status(repo="team/project", checkout=nested, as_json=False)
            self.assertIn("Orchestrator lease: active provider=claude expires=", text)
            self.assertIn("code-mower session show --current", text)
            self.assertNotIn(saved["id"], text)

            payload = json.loads(self._status(repo="other/repo", checkout=root, as_json=True))
            self.assertEqual(
                payload["orchestrator_lease"], {"state": "other_repository", "provider": None, "expires_at": None},
            )
            self.assertIn("Orchestrator lease: other_repository", self._status(repo="other/repo", checkout=root, as_json=False))

            (root / LEASE_FILE).write_text("nope", encoding="utf-8")
            payload = json.loads(self._status(repo="team/project", checkout=root, as_json=True))
            self.assertEqual(payload["orchestrator_lease"]["state"], "malformed")
            self.assertIn("Orchestrator lease: malformed", self._status(repo="team/project", checkout=root, as_json=False))

    def test_lanes_status_outside_a_checkout_is_unavailable_and_still_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = json.loads(self._status(repo="team/project", checkout=Path(tmp), as_json=True))
            self.assertEqual(payload["orchestrator_lease"], {"state": "unavailable", "provider": None, "expires_at": None})
            self.assertFalse((Path(tmp) / ".code-mower").exists())


if __name__ == "__main__":
    unittest.main()
