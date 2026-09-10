"""Local lease display must remain diagnostic, read-only, and independent."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from unittest import TestCase, skipUnless
from unittest.mock import patch

from code_mower import board, session_lease


class BoardLeaseTests(TestCase):
    def test_required_states_on_live_and_pending_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text("gitdir: unused\n")
            child = root / "child"
            child.mkdir()
            config = board.BoardConfig(repo="owner/repo", repo_path=str(child))
            path = session_lease.lease_path(root)
            record = dict(
                schema=session_lease.LEASE_SCHEMA,
                repo="owner/repo",
                orchestrator="codex",
                session_id="local-session",
                acquired_at="2026-01-01T00:00:00Z",
                renewed_at="2026-01-01T00:00:00Z",
                expires_at="2099-01-01T03:00:00+03:00",
            )
            for state, content in [
                ("absent", None),
                ("active", json.dumps(record)),
                ("expired", json.dumps({**record, "expires_at": "2000-01-01T00:00:00Z"})),
                ("malformed", "{"),
                ("malformed", json.dumps({**record, "expires_at": "bad"})),
                ("malformed", json.dumps({"schema": "wrong"})),
                ("malformed", b"\xff"),
            ]:
                with self.subTest(state=state, content=content):
                    if content is not None:
                        path.parent.mkdir(exist_ok=True)
                        path.write_bytes(
                            content if isinstance(content, bytes) else content.encode()
                        )
                    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
                    with (
                        patch.object(
                            board.lane_status, "collect_status", return_value={"repo": "owner/repo"}
                        ),
                        patch.object(board, "supervised_pilot_payload", return_value={}),
                        patch.object(board.productivity_report, "board_payload", return_value={}),
                    ):
                        for payload in (
                            board.status_payload(config),
                            board._pending_status_payload(config),
                        ):
                            lease = payload["orchestrator_lease"]
                            self.assertEqual(lease["state"], state)
                            self.assertIn("owner_queue", payload)
                            self.assertNotIn(
                                "orchestrator_lease", board._recordable_payload(payload)
                            )
                            self.assertNotIn("session_id", lease)
                            self.assertNotIn("lease_file", lease)
                            if state == "active":
                                self.assertEqual(lease["provider"], "codex")
                                self.assertEqual(lease["expires_at"], "2099-01-01T00:00:00Z")
                    self.assertEqual(
                        before, sorted(str(p.relative_to(root)) for p in root.rglob("*"))
                    )
                    if content is not None:
                        self.assertEqual(
                            path.read_bytes(),
                            content if isinstance(content, bytes) else content.encode(),
                        )
                    else:
                        self.assertFalse(path.parent.exists())

    def test_unavailable_and_absent_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(session_lease.observe_lease(start=tmp)["state"], "unavailable")
            (Path(tmp) / ".git").mkdir()
            with patch.object(Path, "read_text", side_effect=PermissionError("private path")):
                payload = board._pending_status_payload(
                    board.BoardConfig(repo="owner/repo", repo_path=tmp)
                )
            self.assertEqual(
                payload["orchestrator_lease"],
                {"state": "unavailable", "provider": None, "expires_at": None},
            )
            self.assertIn("owner_queue", payload)

    def test_expiry_boundary_and_legacy_reader_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            now = datetime(2026, 1, 1, tzinfo=UTC)
            session_lease.acquire_lease(
                repo="owner/repo",
                orchestrator="codex",
                session_id="local",
                root=root,
                now=now,
                ttl_minutes=1,
            )
            self.assertEqual(session_lease.observe_lease(start=root, now=now)["state"], "active")
            self.assertEqual(
                session_lease.observe_lease(start=root, now=now.replace(minute=1))["state"],
                "expired",
            )
            path = session_lease.lease_path(root)
            record = session_lease.read_lease(path)
            record["expires_at"] = "bad"
            path.write_text(json.dumps(record))
            self.assertEqual(session_lease.lease_state(session_lease.read_lease(path)), "expired")
            self.assertEqual(session_lease.observe_lease(start=root)["state"], "malformed")

    @skipUnless(shutil.which("node"), "Node.js required for shipped renderer test")
    def test_renderer_local_time_utc_hover_and_escaping(self):
        html = board.render_board_html(board.BoardConfig(repo="owner/repo"))
        helpers = html[html.index("    const text =") : html.index("    function labels(")]
        renderer = re.search(r"    function renderLease\(lease\) \{.*?\n    \}", html, re.S).group()
        script = helpers + renderer + "\nconsole.log(renderLease(JSON.parse(process.argv[1])));"
        for state in ("active", "expired", "absent", "malformed", "unavailable"):
            result = subprocess.run(
                [
                    shutil.which("node"),
                    "-e",
                    script,
                    json.dumps(
                        {
                            "state": state,
                            "provider": "<script>alert(1)</script>",
                            "expires_at": "2026-01-01T12:00:00Z",
                        }
                    ),
                ],
                env={**os.environ, "TZ": "America/New_York"},
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn(state, result)
            self.assertIn("7:00:00", result)
            self.assertIn('title="UTC 2026-01-01T12:00:00Z"', result)
            self.assertIn("&lt;script&gt;", result)
            self.assertNotIn("<script>", result)
