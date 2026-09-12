from __future__ import annotations

import copy
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from code_mower import cli, context_session, session_lease
from code_mower.context_contract import ContextError
from code_mower.context_store import ContextStore


ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "src/code_mower/templates/code-mower.example.yml"


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def private_store(root: Path) -> ContextStore:
    return ContextStore(root, vault=mock.Mock())


def session_value(session_id: str = "a" * 32) -> dict:
    return {
        "schema": "code_mower.session.v1",
        "id": session_id,
        "repo": "owner/repo",
        "host": "codex",
        "orchestrator": "codex",
        "participants": [{"id": "claude"}, {"id": "codex"}],
        "lease": {"state": "held", "mutating": True},
    }


POLICY = {
    "schema": "code_mower.contextPolicy.v1",
    "connection": "example-context",
    "policy_version": "v1",
    "required": False,
}


@unittest.skipUnless(os.name == "posix", "private storage needs POSIX")
class ContextSessionContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = private_store(self.root / "private")
        self.session = session_value()

    def test_create_read_and_status_redact_every_private_reference(self):
        record = context_session.create(
            self.store, self.session, work_item="SECRET-123", policy=POLICY,
        )
        self.assertEqual(context_session.read(self.store, self.session["id"]), record)
        state = context_session.status(record, lease_live=True)
        self.assertEqual(state["stage"], "selected")
        self.assertEqual(state["schema"], "code_mower.contextSessionStatus.v1")
        encoded = json.dumps(state)
        for private in ("SECRET-123", "example-context", "owner/repo", self.session["id"], str(self.root)):
            self.assertNotIn(private, encoded)
        directory_mode = stat.S_IMODE((self.root / "private").stat().st_mode)
        private_file = self.root / "private" / ("session-" + self.session["id"] + ".json")
        file_mode = stat.S_IMODE(private_file.stat().st_mode)
        self.assertEqual(directory_mode, 0o700)
        self.assertEqual(file_mode, 0o600)

    def test_create_is_idempotent_only_for_the_same_binding(self):
        first = context_session.create(self.store, self.session, work_item="ITEM-1", policy=POLICY)
        again = context_session.create(self.store, self.session, work_item="ITEM-1", policy=POLICY)
        self.assertEqual(first, again)
        with self.assertRaisesRegex(ContextError, "different work item"):
            context_session.create(self.store, self.session, work_item="ITEM-2", policy=POLICY)

    def test_generation_update_detects_concurrency_and_cannot_change_identity(self):
        context_session.create(self.store, self.session, work_item="ITEM-1", policy=POLICY)
        changed = context_session.update(
            self.store, self.session["id"], expected_generation=0,
            changes={"stage": "prepared", "packet": "b" * 32, "work_order": "work-order.md"},
        )
        self.assertEqual(changed["generation"], 1)
        with self.assertRaisesRegex(ContextError, "changed concurrently"):
            context_session.update(self.store, self.session["id"], expected_generation=0, changes={})
        with self.assertRaisesRegex(ContextError, "identity cannot be changed"):
            context_session.update(
                self.store, self.session["id"], expected_generation=1,
                changes={"work_item": "ITEM-2"},
            )

    def test_no_provider_and_inactive_lease_have_bounded_status(self):
        record = context_session.create(self.store, self.session, work_item="ITEM-1", policy=None)
        self.assertEqual(context_session.status(record, lease_live=True)["stage"], "not_configured")
        inactive = context_session.status(record, lease_live=False)
        self.assertEqual(inactive["stage"], "lease_inactive")
        self.assertTrue(inactive["owner_action"])
        self.assertEqual(context_session.status(None, lease_live=True)["stage"], "not_selected")

    def test_resolution_rejects_conflicting_trusted_values(self):
        self.assertEqual(context_session.resolve_bound("repository", None, "owner/repo"), "owner/repo")
        with self.assertRaisesRegex(ContextError, "conflicts with the saved session"):
            context_session.resolve_bound("repository", "owner/repo", "owner/other")

    def test_malformed_or_cross_bound_records_fail_closed(self):
        record = context_session.create(self.store, self.session, work_item="ITEM-1", policy=POLICY)
        mutations = (
            {"repo": "invalid"},
            {"connection": "other"},
            {"participants": ["codex", "codex"]},
            {"generation": -1},
            {"stage": "mystery"},
            {"revision": "c" * 32},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ContextError):
                context_session.validate({**copy.deepcopy(record), **mutation})


@unittest.skipUnless(os.name == "posix", "private storage needs POSIX")
class ContextSessionCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.private = self.root / "private"
        self.sessions = self.repo / ".code-mower" / "sessions"

    def run_cli(self, args: list[str]):
        out, err = io.StringIO(), io.StringIO()
        with working_directory(self.repo), redirect_stdout(out), redirect_stderr(err):
            result = cli.main(args)
        return result, out.getvalue(), err.getvalue()

    def start(self, *extra: str):
        return self.run_cli([
            "session", "start", "--repo", "owner/repo", "--host", "codex",
            "--with", "claude,codex", "--state-dir", str(self.sessions),
            "--context-state-dir", str(self.private), "--work-item", "SECRET-123",
            "--json", *extra,
        ])

    def test_start_stores_only_a_redacted_marker_and_status_resumes_privately(self):
        code, output, error = self.start()
        self.assertEqual((code, error), (0, ""))
        saved = json.loads(output)
        self.assertEqual(saved["work_item"], {"selected": True})
        self.assertNotIn("SECRET-123", output)
        session_file = Path(saved["session_file"])
        self.assertNotIn("SECRET-123", session_file.read_text())
        code, output, error = self.run_cli([
            "session", "context", "status", str(session_file), "--repo-path", str(self.repo),
            "--context-state-dir", str(self.private), "--json",
        ])
        self.assertEqual((code, error), (0, ""))
        status = json.loads(output)
        self.assertEqual(status["stage"], "not_configured")
        self.assertNotIn("SECRET-123", output)

    def test_work_item_refuses_read_only_session_and_rolls_back_failed_brief(self):
        code, output, error = self.start("--no-lease")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("requires a mutating session lease", error)
        self.assertFalse(self.private.exists())
        self.assertIsNone(session_lease.read_lease(self.repo / ".code-mower" / session_lease.LEASE_FILE_NAME))

        original_open = Path.open
        def fail_session_write(path, *args, **kwargs):
            if path.parent == self.sessions and path.suffix == ".json":
                raise OSError("no write")
            return original_open(path, *args, **kwargs)
        with mock.patch("pathlib.Path.open", autospec=True, side_effect=fail_session_write):
            code, output, error = self.start()
        self.assertEqual((code, output), (1, ""))
        self.assertIn("no write", error)
        self.assertIsNone(session_lease.read_lease(self.repo / ".code-mower" / session_lease.LEASE_FILE_NAME))
        self.assertEqual(list(self.private.glob("*.json")), [])

    def test_dry_run_marks_selection_without_private_state(self):
        code, output, error = self.start("--dry-run")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["work_item"], {"selected": True})
        self.assertFalse(self.private.exists())

    def test_changed_trusted_connection_is_rejected_before_status(self):
        config_path = self.repo / "code-mower.yml"
        config_text = STARTER.read_text(encoding="utf-8") + (
            "\ncontext:\n"
            "  schema: code_mower.contextPolicy.v1\n"
            "  connection: example-context\n"
            "  policy_version: v1\n"
            "  required: false\n"
        )
        config_path.write_text(config_text, encoding="utf-8")
        code, output, error = self.start("--config", str(config_path))
        self.assertEqual((code, error), (0, ""))
        saved = json.loads(output)
        config_path.write_text(
            config_text.replace("connection: example-context", "connection: different-context"),
            encoding="utf-8",
        )
        code, output, error = self.run_cli([
            "session", "context", "status", saved["session_file"],
            "--repo-path", str(self.repo), "--config", str(config_path),
            "--context-state-dir", str(self.private), "--json",
        ])
        self.assertEqual((code, output), (1, ""))
        self.assertIn("policy changed after session start", error)


if __name__ == "__main__":
    unittest.main()
