from __future__ import annotations

import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from code_mower import antigravity_cli_audit_pr


class AntigravityCliAuditPrTests(unittest.TestCase):
    def test_build_antigravity_audit_argv_structure(self) -> None:
        workspace = Path("/tmp/mock-workspace")
        argv = antigravity_cli_audit_pr.build_antigravity_audit_argv(
            command="agy",
            workspace_dir=workspace,
            prompt_instruction="Read prompt.txt and return JSON.",
            timeout_seconds=300,
            model="gemini-2.5-pro",
        )

        self.assertIsInstance(argv, list)
        self.assertTrue(all(isinstance(item, str) for item in argv))
        self.assertEqual(argv[0], "agy")
        self.assertIn("--sandbox", argv)
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertIn("--add-dir", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(workspace))
        self.assertIn("--print-timeout", argv)
        self.assertEqual(argv[argv.index("--print-timeout") + 1], "300s")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-2.5-pro")
        self.assertIn("--print", argv)
        self.assertEqual(
            argv[argv.index("--print") + 1],
            "Read prompt.txt and return JSON.",
        )
        self.assertLess(
            argv.index("--dangerously-skip-permissions"),
            argv.index("--add-dir"),
        )

    def test_build_antigravity_audit_argv_without_model(self) -> None:
        workspace = Path("/tmp/mock-workspace")
        argv = antigravity_cli_audit_pr.build_antigravity_audit_argv(
            command="agy",
            workspace_dir=workspace,
            prompt_instruction="Review PR.",
            timeout_seconds=60,
        )
        self.assertNotIn("--model", argv)
        self.assertIn("--sandbox", argv)
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_capability_probe_success(self) -> None:
        help_output = """
Usage of agy:
  --add-dir                       Add a directory to the workspace
  --dangerously-skip-permissions  Auto-approve all tool permission requests without prompting
  --print                         Run a single prompt non-interactively and print the response
  --print-timeout                 Timeout for print mode wait
  --sandbox                       Run in a sandbox with terminal restrictions enabled
"""
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["agy", "--help"],
                0,
                stdout=help_output,
                stderr="",
            ),
        ) as mock_run:
            antigravity_cli_audit_pr.verify_antigravity_cli_contract(
                "agy",
                cwd=Path("/tmp"),
                env={},
            )
            mock_run.assert_called_once()
            self.assertEqual(mock_run.call_args[0][0], ["agy", "--help"])

    def test_capability_probe_missing_permission_flag_fails_closed(self) -> None:
        help_output = """
Usage of agy:
  --add-dir                       Add a directory to the workspace
  --print                         Run a single prompt non-interactively and print the response
  --print-timeout                 Timeout for print mode wait
  --sandbox                       Run in a sandbox with terminal restrictions enabled
"""
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["agy", "--help"],
                0,
                stdout=help_output,
                stderr="",
            ),
        ):
            with self.assertRaises(antigravity_cli_audit_pr.AntigravityCliUnsupportedError) as ctx:
                antigravity_cli_audit_pr.verify_antigravity_cli_contract(
                    "agy",
                    cwd=Path("/tmp"),
                    env={},
                )
            error_message = str(ctx.exception)
            self.assertIn("--dangerously-skip-permissions", error_message)
            self.assertIn("Antigravity CLI must support", error_message)

    def test_capability_probe_failed_command_raises_unsupported_error(self) -> None:
        with mock.patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["agy", "--help"],
                1,
                stdout="",
                stderr="command failed",
            ),
        ):
            with self.assertRaises(antigravity_cli_audit_pr.AntigravityCliUnsupportedError) as ctx:
                antigravity_cli_audit_pr.verify_antigravity_cli_contract(
                    "agy",
                    cwd=Path("/tmp"),
                    env={},
                )
            self.assertIn("Antigravity CLI must support", str(ctx.exception))

    def test_run_antigravity_cli_audit_passes_structured_argv(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            if "--help" in args:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="--print --print-timeout --sandbox --add-dir --dangerously-skip-permissions",
                    stderr="",
                )
            verdict_payload = {
                "verdict": "pass",
                "summary": "Clean code.",
                "findings": [],
            }
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(verdict_payload),
                stderr="",
            )

        with (
            mock.patch(
                "code_mower.gemini_cli_audit_pr.fetch_pull_request",
                return_value={"head": {"sha": "abc123"}},
            ),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.fetch_pull_request_diff",
                return_value="diff --git a/file.py b/file.py\n+new line\n",
            ),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.subprocess.run",
                side_effect=fake_run,
            ),
        ):
            payload = antigravity_cli_audit_pr.run_antigravity_cli_audit(
                repo="owner/repo",
                pr_number=42,
                github_token="secret-token",
                command="agy",
                allow_ambient_home=True,
            )

        self.assertEqual(len(calls), 2)
        help_args, audit_args = calls

        self.assertEqual(help_args, ["agy", "--help"])

        self.assertEqual(audit_args[0], "agy")
        self.assertIn("--sandbox", audit_args)
        self.assertIn("--dangerously-skip-permissions", audit_args)
        self.assertIn("--add-dir", audit_args)
        self.assertIn("--print-timeout", audit_args)
        self.assertIn("--print", audit_args)
        self.assertTrue(all(isinstance(arg, str) for arg in audit_args))

        self.assertEqual(payload["verdict"]["verdict"], "pass")
        self.assertEqual(payload["mode"], "antigravity-cli-audit")
        self.assertEqual(payload["returncode"], 0)

    def test_run_antigravity_cli_audit_unsupported_cli_fails_closed(self) -> None:
        def fake_run(args, **kwargs):
            if "--help" in args:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="--print --print-timeout --sandbox --add-dir",
                    stderr="",
                )
            raise AssertionError("Audit subprocess should not be reached when capability probe fails")

        with (
            mock.patch(
                "code_mower.gemini_cli_audit_pr.fetch_pull_request",
                return_value={"head": {"sha": "abc123"}},
            ),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.fetch_pull_request_diff",
                return_value="diff --git a/file.py b/file.py\n+new line\n",
            ),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.subprocess.run",
                side_effect=fake_run,
            ),
        ):
            with self.assertRaises(antigravity_cli_audit_pr.AntigravityCliUnsupportedError) as ctx:
                antigravity_cli_audit_pr.run_antigravity_cli_audit(
                    repo="owner/repo",
                    pr_number=42,
                    github_token="secret-token",
                    command="agy",
                    allow_ambient_home=True,
                )
            self.assertIn("--dangerously-skip-permissions", str(ctx.exception))

    def test_successful_blocked_verdict_parsing(self) -> None:
        blocked_json = {
            "verdict": "blocked",
            "summary": "Security regression identified.",
            "findings": [
                {
                    "severity": "P1",
                    "title": "Unsanitized command input",
                    "file": "audit.py",
                    "line": 55,
                    "detail": "Passing unsanitized shell string to subprocess.",
                }
            ],
        }

        def fake_run(args, **kwargs):
            if "--help" in args:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="--print --print-timeout --sandbox --add-dir --dangerously-skip-permissions",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(blocked_json),
                stderr="",
            )

        with (
            mock.patch(
                "code_mower.gemini_cli_audit_pr.fetch_pull_request",
                return_value={"head": {"sha": "abc123"}},
            ),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.fetch_pull_request_diff",
                return_value="diff --git a/audit.py b/audit.py\n",
            ),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.subprocess.run",
                side_effect=fake_run,
            ),
        ):
            payload = antigravity_cli_audit_pr.run_antigravity_cli_audit(
                repo="owner/repo",
                pr_number=42,
                github_token="secret-token",
                command="agy",
                allow_ambient_home=True,
            )

        self.assertEqual(payload["verdict"]["verdict"], "blocked")
        self.assertEqual(payload["verdict"]["blocker_count"], 1)
        self.assertEqual(payload["verdict"]["findings"][0]["severity"], "P1")
        self.assertEqual(
            payload["verdict"]["findings"][0]["title"],
            "Unsanitized command input",
        )

    def test_main_unsupported_cli_returns_one_and_prints_actionable_error(self) -> None:
        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="--print --print-timeout --sandbox --add-dir",
                stderr="",
            )

        stderr_buf = io.StringIO()
        with (
            mock.patch(
                "code_mower.gemini_cli_audit_pr.resolve_github_token",
                return_value="test-token",
            ),
            mock.patch.dict("os.environ", {"ANTIGRAVITY_CLI_USE_AMBIENT_HOME": "1"}),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.fetch_pull_request",
                return_value={"head": {"sha": "abc123"}},
            ),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.fetch_pull_request_diff",
                return_value="diff --git a/a b/a\n",
            ),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.subprocess.run",
                side_effect=fake_run,
            ),
            redirect_stderr(stderr_buf),
        ):
            exit_code = antigravity_cli_audit_pr.main(
                ["--repo", "owner/repo", "--pr", "42"]
            )

        self.assertEqual(exit_code, 1)
        err_output = stderr_buf.getvalue()
        self.assertIn("error:", err_output)
        self.assertIn("--dangerously-skip-permissions", err_output)

    def test_main_success_verdict_returns_zero(self) -> None:
        def fake_run(args, **kwargs):
            if "--help" in args:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="--print --print-timeout --sandbox --add-dir --dangerously-skip-permissions",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"verdict": "pass", "summary": "Looks good.", "findings": []}),
                stderr="",
            )

        stdout_buf = io.StringIO()
        with (
            mock.patch(
                "code_mower.gemini_cli_audit_pr.resolve_github_token",
                return_value="test-token",
            ),
            mock.patch.dict("os.environ", {"ANTIGRAVITY_CLI_USE_AMBIENT_HOME": "1"}),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.fetch_pull_request",
                return_value={"head": {"sha": "abc123"}},
            ),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.fetch_pull_request_diff",
                return_value="diff --git a/a b/a\n",
            ),
            mock.patch(
                "code_mower.gemini_cli_audit_pr.subprocess.run",
                side_effect=fake_run,
            ),
            redirect_stdout(stdout_buf),
        ):
            exit_code = antigravity_cli_audit_pr.main(
                ["--repo", "owner/repo", "--pr", "42", "--json"]
            )

        self.assertEqual(exit_code, 0)
        output_payload = json.loads(stdout_buf.getvalue())
        self.assertEqual(output_payload["verdict"]["verdict"], "pass")

    def test_ambient_home_opt_in_required(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            antigravity_cli_audit_pr.run_antigravity_cli_audit(
                repo="owner/repo",
                pr_number=42,
                github_token="token",
                allow_ambient_home=False,
            )
        self.assertIn("requires explicit ambient-home opt-in", str(ctx.exception))
        self.assertIn("ANTIGRAVITY_CLI_USE_AMBIENT_HOME=1", str(ctx.exception))

    def test_api_key_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            antigravity_cli_audit_pr.run_antigravity_cli_audit(
                repo="owner/repo",
                pr_number=42,
                github_token="token",
                antigravity_api_key="api-key-123",
                allow_ambient_home=True,
            )
        self.assertIn("does not currently support Gemini API keys", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
