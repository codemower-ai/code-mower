from __future__ import annotations

import json
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import TestCase, mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import observe


class ObserveAgentTrailTests(TestCase):
    def test_dry_run_pins_agenttrail_without_init_or_browser_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = str(Path(tmp).resolve())
            out = StringIO()
            with redirect_stdout(out):
                exit_code = observe.main(
                    [
                        "agenttrail",
                        "--repo",
                        tmp,
                        "--dry-run",
                        "--json",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["agenttrail_version"], observe.AGENTTRAIL_VERSION)
        self.assertEqual(
            payload["command"],
            ["npx", "-y", f"agenttrail@{observe.AGENTTRAIL_VERSION}", repo, "--no-open"],
        )
        self.assertFalse(payload["calls_init"])
        self.assertNotIn("init", payload["command"])

    def test_launch_blocks_if_agenttrail_changes_repo_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            process = mock.Mock()
            process.pid = 1234
            process.poll.return_value = None
            process.wait.return_value = 0

            with (
                mock.patch.object(observe, "_check_runtime", return_value=None),
                mock.patch.object(observe, "_git_status", side_effect=["", "?? PLAN.md\n"]),
                mock.patch.object(observe, "_start_background", return_value=process) as start,
                mock.patch.object(observe.os, "getpgid", return_value=5678),
                mock.patch.object(observe.os, "killpg") as killpg,
                mock.patch.object(observe.time, "sleep"),
            ):
                config = observe.AgentTrailConfig(
                    repo=Path(tmp),
                    version=observe.AGENTTRAIL_VERSION,
                    npx_command="npx",
                    node_command="node",
                    port=None,
                    dry_run=False,
                    allow_repo_changes=False,
                    claude_hooks=False,
                    settle_seconds=0,
                )
                exit_code, payload = observe.run_agenttrail(config)

        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "blocked")
        self.assertTrue(payload["repo_changed"])
        self.assertTrue(payload["terminated"])
        start.assert_called_once()
        killpg.assert_called_once_with(5678, observe.signal.SIGTERM)
        process.wait.assert_called_once()
        process.terminate.assert_not_called()

    def test_claude_hooks_output_is_suppressed_for_json_callers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            process = mock.Mock()
            process.pid = 1234
            process.poll.return_value = None

            with (
                mock.patch.object(observe, "_check_runtime", return_value=None),
                mock.patch.object(observe, "_git_status", side_effect=["", ""]),
                mock.patch.object(observe, "_start_background", return_value=process),
                mock.patch.object(observe.subprocess, "run") as run,
            ):
                config = observe.AgentTrailConfig(
                    repo=Path(tmp),
                    version=observe.AGENTTRAIL_VERSION,
                    npx_command="npx",
                    node_command="node",
                    port=None,
                    dry_run=False,
                    allow_repo_changes=False,
                    claude_hooks=True,
                    settle_seconds=0,
                )
                exit_code, payload = observe.run_agenttrail(config)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "started")
        run.assert_called_once()
        _, kwargs = run.call_args
        self.assertIs(kwargs["stdin"], observe.subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], observe.subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], observe.subprocess.DEVNULL)
