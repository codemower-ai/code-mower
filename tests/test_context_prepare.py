"""Guided context preparation, idempotence, and recovery."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from code_mower import context_prepare, context_session, session
from code_mower.context_connections import connect
from code_mower.context_contract import ContextError
from code_mower.context_store import ContextStore
from test_context_connections import MemoryVault
from test_context_packets import RetrievalBackend
from test_coworker_retrieval import POLICY


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


def session_value(session_id: str = "a" * 32, *, host: str = "codex") -> dict:
    return {
        "schema": "code_mower.session.v1",
        "id": session_id,
        "repo": "owner/repo",
        "host": host,
        "orchestrator": host,
        "participants": [{"id": "claude"}, {"id": "codex"}],
        "lease": {"state": "held", "mutating": True},
    }


@unittest.skipUnless(os.name == "posix", "private context needs POSIX protections")
class ContextPrepareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.private = self.root / "private"
        self.associations = context_session.association_store(self.private)
        self.packet_store = ContextStore(self.private, vault=MemoryVault())
        self.backend = RetrievalBackend()
        connect(
            self.packet_store,
            "example",
            {
                "principal": "one@example.invalid",
                "workspace": "example",
                "repositories": ["owner/repo"],
                "recipients": [
                    "codex:orchestrator", "claude:orchestrator",
                    "codex:builder", "claude:builder",
                    "codex:reviewer", "claude:reviewer",
                ],
            },
            backend=self.backend,
        )

    def create_record(self, session_id: str = "a" * 32, *, host: str = "codex", required: bool = True):
        return context_session.create(
            self.associations,
            session_value(session_id, host=host),
            work_item="SECRET-123",
            policy={**POLICY, "required": required},
        )

    def prepare(self, record, **kwargs):
        return context_prepare.prepare(
            self.associations,
            record,
            repo_root=self.repo,
            context_root=self.private,
            packet_store=self.packet_store,
            backend=self.backend,
            **kwargs,
        )

    def test_first_run_and_retry_use_one_search_and_redact_the_work_order(self):
        record = self.create_record()
        first, code = self.prepare(record)
        self.assertEqual((code, first["status"], first["reused"]), (0, "prepared", False))
        saved = context_session.read(self.associations, record["session_id"])
        self.assertEqual(saved["stage"], "prepared")
        self.assertEqual(self.backend.searches, 1)
        text = (self.repo / saved["work_order"]).read_text(encoding="utf-8")
        self.assertIn(saved["packet"], text)
        self.assertIn("- Builder: `codex`", text)
        self.assertIn("- claude_audit", text)
        for private in ("SECRET-123", "one@example.invalid", "example", "gitar", "- codex\n"):
            self.assertNotIn(private, text)

        again, code = self.prepare(saved)
        self.assertEqual((code, again["status"], again["reused"]), (0, "prepared", True))
        self.assertEqual(self.backend.searches, 1)
        rendered = json.dumps(again)
        for private in ("SECRET-123", "one@example.invalid", "example", saved["packet"]):
            self.assertNotIn(private, rendered)

    def test_failed_or_interrupted_search_requires_explicit_refresh(self):
        record = self.create_record()
        self.backend.fail_search = True
        unavailable, code = self.prepare(record)
        self.assertEqual((code, unavailable["status"]), (1, "required_unavailable"))
        reserved = context_session.read(self.associations, record["session_id"])
        self.assertEqual((reserved["stage"], reserved["packet"]), ("preparing", None))
        self.backend.fail_search = False
        with self.assertRaisesRegex(ContextError, "--refresh"):
            self.prepare(reserved)
        completed, code = self.prepare(reserved, refresh=True)
        self.assertEqual((code, completed["status"]), (0, "prepared"))
        self.assertEqual(self.backend.searches, 2)

    def test_optional_failure_keeps_ordinary_work_usable(self):
        record = self.create_record(required=False)
        self.backend.fail_search = True
        unavailable, code = self.prepare(record)
        self.assertEqual((code, unavailable["status"]), (0, "optional_unavailable"))
        self.assertEqual(unavailable["dependent_work"], "usable")
        self.assertNotIn("SECRET-123", json.dumps(unavailable))

    def test_no_provider_keeps_the_ordinary_workflow_usable(self):
        record = context_session.create(
            self.associations,
            session_value(),
            work_item="SECRET-123",
            policy=None,
        )
        report, code = self.prepare(record)
        self.assertEqual((code, report["status"]), (0, "not_configured"))
        self.assertEqual(report["dependent_work"], "usable")
        self.assertEqual(self.backend.searches, 0)

    def test_changed_private_query_requires_refresh(self):
        record = self.create_record()
        _first, code = self.prepare(record, query="first bounded query")
        self.assertEqual(code, 0)
        saved = context_session.read(self.associations, record["session_id"])
        first_packet = saved["packet"]
        self.assertNotIn("first bounded query", json.dumps(saved))
        self.assertNotIn(
            "first bounded query",
            (self.repo / saved["work_order"]).read_text(encoding="utf-8"),
        )
        with self.assertRaisesRegex(ContextError, "input changed"):
            self.prepare(saved, query="second bounded query")
        second, code = self.prepare(saved, query="second bounded query", refresh=True)
        self.assertEqual((code, second["status"]), (0, "prepared"))
        self.assertEqual(self.backend.searches, 2)
        refreshed = context_session.read(self.associations, record["session_id"])
        self.assertNotEqual(first_packet, refreshed["packet"])
        self.assertNotIn("second bounded query", json.dumps(refreshed))

    def test_work_order_interruption_resumes_without_another_search(self):
        record = self.create_record()
        with mock.patch.object(
            context_prepare.work_orders,
            "draft_work_order",
            side_effect=OSError("private local detail"),
        ):
            with self.assertRaisesRegex(ContextError, "did not complete"):
                self.prepare(record)
        resumable = context_session.read(self.associations, record["session_id"])
        self.assertEqual(resumable["stage"], "preparing")
        self.assertIsNotNone(resumable["packet"])
        status = context_session.status(resumable, lease_live=True)
        self.assertFalse(status["owner_action"])
        self.assertEqual(status["dependent_work"], "paused")
        self.assertIn("reused", status["next_action"])
        report, code = self.prepare(resumable)
        self.assertEqual((code, report["status"]), (0, "prepared"))
        self.assertEqual(self.backend.searches, 1)

    def test_claude_and_codex_hosts_get_symmetric_independent_lanes(self):
        expectations = (("codex", "a" * 32, "claude_audit"), ("claude", "b" * 32, "codex"))
        for host, session_id, expected_lane in expectations:
            with self.subTest(host=host):
                record = self.create_record(session_id, host=host)
                report, code = self.prepare(record)
                self.assertEqual((code, report["status"]), (0, "prepared"))
                saved = context_session.read(self.associations, session_id)
                text = (self.repo / saved["work_order"]).read_text(encoding="utf-8")
                self.assertIn("- " + expected_lane, text)
                self.assertNotIn("- gitar", text)

    def test_explicit_selected_builder_controls_the_independent_lane(self):
        record = self.create_record(host="codex")
        report, code = self.prepare(record, builder="claude")
        self.assertEqual((code, report["status"]), (0, "prepared"))
        saved = context_session.read(self.associations, record["session_id"])
        self.assertEqual(saved["builder"], "claude")
        text = (self.repo / saved["work_order"]).read_text(encoding="utf-8")
        self.assertIn("- Builder: `claude`", text)
        self.assertIn("- codex", text)
        self.assertNotIn("- claude_audit", text)
        with self.assertRaisesRegex(ContextError, "selected builder"):
            self.prepare(saved, builder="devin")

    def test_builder_handoff_redrafts_without_repeating_the_search(self):
        record = self.create_record(host="codex")
        _report, code = self.prepare(record)
        self.assertEqual(code, 0)
        saved = context_session.read(self.associations, record["session_id"])
        report, code = self.prepare(saved, builder="claude")
        self.assertEqual((code, report["status"]), (0, "prepared"))
        changed = context_session.read(self.associations, record["session_id"])
        self.assertEqual(changed["builder"], "claude")
        self.assertEqual(self.backend.searches, 1)
        text = (self.repo / changed["work_order"]).read_text(encoding="utf-8")
        self.assertIn("- Builder: `claude`", text)
        self.assertIn("- codex", text)
        self.assertNotIn("- claude_audit", text)

    def test_cli_prepare_never_renders_private_identifiers(self):
        config_path = self.repo / "code-mower.yml"
        config_path.write_text(
            STARTER.read_text(encoding="utf-8")
            + "\ncontext:\n"
            + "  schema: code_mower.contextPolicy.v1\n"
            + "  connection: example\n"
            + "  policy_version: v1\n"
            + "  required: true\n",
            encoding="utf-8",
        )
        sessions = self.repo / ".code-mower" / "sessions"
        with working_directory(self.repo), redirect_stdout(io.StringIO()) as start_out:
            code = session.main([
                "start", "--repo", "owner/repo", "--host", "codex",
                "--with", "claude,codex", "--config", str(config_path),
                "--state-dir", str(sessions), "--context-state-dir", str(self.private),
                "--work-item", "SECRET-123", "--json",
            ])
        self.assertEqual(code, 0)
        session_file = json.loads(start_out.getvalue())["session_file"]
        with (
            working_directory(self.repo),
            redirect_stdout(io.StringIO()) as out,
            redirect_stderr(io.StringIO()) as err,
            mock.patch.object(context_prepare, "ContextStore", return_value=self.packet_store),
            mock.patch.object(context_prepare.context_packets, "_backend", return_value=self.backend),
        ):
            code = session.main([
                "context", "prepare", session_file, "--repo-path", str(self.repo),
                "--config", str(config_path), "--context-state-dir", str(self.private),
                "--json",
            ])
        self.assertEqual((code, err.getvalue()), (0, ""))
        rendered = out.getvalue()
        for private in ("SECRET-123", "one@example.invalid", "example", "source_example"):
            self.assertNotIn(private, rendered)


if __name__ == "__main__":
    unittest.main()
