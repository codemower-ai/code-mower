from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from code_mower import cli, session, session_lease


LEASE_FILE = Path(".code-mower/sessions") / session_lease.LEASE_FILE_NAME


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


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
            briefs = sorted(path.name for path in LEASE_FILE.parent.glob("*.json"))
            self.assertEqual(briefs, sorted([f"{first['id']}.json", session_lease.LEASE_FILE_NAME]))
            self.assertEqual(session_lease.read_lease(LEASE_FILE)["session_id"], first["id"])

    def test_a_refusal_for_a_different_repo_names_the_requested_repo(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            start_session(repo="team/project")
            code, out, err = start_session(host="codex", repo="other/repo")
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("already holds the mutating orchestrator lease for team/project", err)
            self.assertIn("requested: other/repo", err)

    def test_concurrent_acquisition_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "sessions"
            contenders = 8
            ready = threading.Barrier(contenders)
            guard = threading.Lock()
            winners: list[str] = []

            def contend(index: int) -> None:
                ready.wait()
                try:
                    record = session_lease.acquire_lease(
                        repo="team/project", orchestrator="claude",
                        session_id=f"session-{index}", state_dir=state_dir,
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
            stored = session_lease.read_lease(session_lease.lease_path(state_dir))
            self.assertEqual(stored["session_id"], winners[0])

    def test_forced_takeover_replaces_a_live_lease_only_when_asked_for(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
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
                LEASE_FILE.parent.mkdir(parents=True)
                LEASE_FILE.write_text(content, encoding="utf-8")
                self.assertIsNone(session_lease.read_lease(LEASE_FILE))
                self.assertEqual(start_session()[0], 0)
                self.assertIsNotNone(session_lease.read_lease(LEASE_FILE))

    def test_the_same_session_re_acquiring_renews_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "sessions"
            first = session_lease.acquire_lease(
                repo="team/project", orchestrator="claude", session_id="session-1",
                state_dir=state_dir, ttl_minutes=60,
            )
            again = session_lease.acquire_lease(
                repo="team/project", orchestrator="claude", session_id="session-1",
                state_dir=state_dir, ttl_minutes=600,
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
            code, out, _ = run_lease("release", "--json")
            self.assertEqual(code, 0)
            payload = json.loads(out)
            self.assertFalse(payload["released"])
            self.assertIsNone(payload["previous"])
            self.assertIn("nothing to release", run_lease("release")[1])

    def test_rendered_inspection_names_the_holder_and_the_expiry(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
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


class LeasePrivacyTests(unittest.TestCase):
    def test_the_lease_holds_only_coordination_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = session_lease.acquire_lease(
                repo="team/project", orchestrator="claude", session_id="session-1",
                state_dir=Path(tmp) / "sessions",
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
            payload = json.loads(start_session()[1])
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(["session", "show", payload["session_file"], "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue()), payload)


if __name__ == "__main__":
    unittest.main()
