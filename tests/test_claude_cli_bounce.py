from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import claude_audit_pr, claude_cli_bounce
from code_mower.claude_cli_environment import (
    clean_claude_cli_env,
    render_claude_env_unset_snippet,
)


class ClaudeCliEnvironmentTests(unittest.TestCase):
    def test_clean_env_removes_claude_overrides_and_optional_github_tokens(self) -> None:
        env, removed = clean_claude_cli_env(
            {
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "anthropic",
                "ANTHROPIC_AUTH_TOKEN": "token",
                "CLAUDE_API_KEY": "claude",
                "CLAUDE_CONFIG_DIR": "/tmp/claude",
                "GITHUB_TOKEN": "gh",
                "KEEP_ME": "yes",
            },
            unset_github_tokens=True,
        )

        self.assertEqual(env, {"PATH": "/usr/bin", "KEEP_ME": "yes"})
        self.assertEqual(
            removed,
            (
                "GITHUB_TOKEN",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_API_KEY",
                "CLAUDE_CONFIG_DIR",
            ),
        )

    def test_unset_snippet_is_comment_only_and_shell_sourceable(self) -> None:
        snippet = render_claude_env_unset_snippet(names=("ANTHROPIC_API_KEY",))

        self.assertIn("does not delete Claude credentials", snippet)
        self.assertIn("unset ANTHROPIC_API_KEY", snippet)
        self.assertNotIn("rm ", snippet)

    def test_audit_runner_env_uses_clean_child_env_by_default(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "GITHUB_TOKEN": "gh",
                "GH_TOKEN": "gh2",
                "ANTHROPIC_API_KEY": "anthropic",
                "CLAUDE_API_KEY": "claude",
                "UNRELATED": "ok",
            },
            clear=True,
        ):
            env = claude_audit_pr._claude_env()

        self.assertEqual(env, {"PATH": "/usr/bin", "UNRELATED": "ok"})

    def test_audit_runner_can_preserve_explicit_claude_auth_env(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "GITHUB_TOKEN": "gh",
                "ANTHROPIC_API_KEY": "anthropic",
                "CODE_MOWER_CLAUDE_KEEP_AUTH_ENV": "1",
            },
            clear=True,
        ):
            env = claude_audit_pr._claude_env()

        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "anthropic")


class ClaudeCliBounceTests(unittest.TestCase):
    def test_bounce_reports_clean_env_fix_without_leaking_raw_auth_output(self) -> None:
        inherited = subprocess.CompletedProcess(
            ["claude"],
            0,
            stdout=json.dumps(
                {
                    "is_error": True,
                    "api_error_status": 401,
                    "result": "Invalid authentication credentials",
                }
            ),
            stderr="",
        )
        clean = subprocess.CompletedProcess(
            ["claude"],
            0,
            stdout=json.dumps({"result": "ok"}),
            stderr="",
        )
        with (
            mock.patch("shutil.which", return_value="/usr/bin/claude"),
            mock.patch("subprocess.run", side_effect=[inherited, clean]),
        ):
            report = claude_cli_bounce.bounce_claude_cli(
                base_env={
                    "PATH": "/usr/bin",
                    "ANTHROPIC_API_KEY": "stale",
                },
            )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(
            report["recommendation"],
            "use_clean_claude_env_or_restart_parent_app",
        )
        self.assertEqual(report["probes"][0]["auth_status_code"], "401")
        self.assertEqual(report["probes"][1]["status"], "pass")
        report_json = json.dumps(report)
        self.assertNotIn("Invalid authentication credentials", report_json)
        self.assertNotIn("stale", report_json)

    def test_cli_writes_env_snippet(self) -> None:
        with tempfile.TemporaryDirectory(prefix="code-mower-claude-bounce-test-") as tmp:
            out_file = Path(tmp) / "claude-clean-env.sh"
            with mock.patch.object(
                claude_cli_bounce,
                "bounce_claude_cli",
                return_value={
                    "status": "pass",
                    "command": "claude",
                    "command_path": "/usr/bin/claude",
                    "probes": [],
                    "recommendation": "no_action_needed",
                    "message": "ok",
                },
            ):
                exit_code = claude_cli_bounce.main(
                    ["--write-env", str(out_file), "--json"]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("unset ANTHROPIC_API_KEY", out_file.read_text())


if __name__ == "__main__":
    unittest.main()
