"""Focused tests for the informational Devin CLI reviewer lane (#746)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import code_mower.devin_cli_audit_pr as devin_cli_audit
from code_mower.provider_runners.workspace import ProviderWorkspaceError


class _DevinCliAuditTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="code-mower-devin-cli-audit-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.artifact_dir = self.tmp / "verdicts"
        self.artifact_dir.mkdir(parents=True)
        os.environ["CODE_MOWER_VERDICT_ARTIFACT_DIR"] = str(self.artifact_dir)

        # Build a small repo: main at commit A, detached HEAD at commit B.
        self._run_git(["init"])
        self._run_git(["config", "user.email", "devin@example.com"])
        self._run_git(["config", "user.name", "Devin CLI Test"])
        self._run_git(["checkout", "--orphan", "main"])
        (self.repo / "file.py").write_text("a\n", encoding="utf-8")
        self._run_git(["add", "file.py"])
        self._run_git(["commit", "-m", "base"])
        self._run_git(["checkout", "--detach"])
        (self.repo / "file.py").write_text("b\n", encoding="utf-8")
        self._run_git(["add", "file.py"])
        self._run_git(["commit", "-m", "pr"])
        self.head_sha = self._run_git_text(["rev-parse", "HEAD"])

        self.command = self.tmp / "fake-devin"

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_git(self, args: list[str]) -> None:
        subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

    def _run_git_text(self, args: list[str]) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _write_fake(self, output: str, *, sleep: int = 0) -> None:
        body = (
            "#!/bin/sh\n"
            f"sleep {sleep}\n"
            f"printf '%s\\n' {json.dumps(output)}\n"
        )
        self.command.write_text(body, encoding="utf-8")
        self.command.chmod(0o755)

    def _pr_meta(self, *, author: str = "someone", moved: bool = False) -> dict:
        return {
            "title": "Test PR",
            "body": "Test body",
            "user": {"login": author},
            "head": {
                "sha": "different" if moved else self.head_sha,
                "repo": {"full_name": "owner/repo"},
            },
        }

    def _config(self, **overrides: object) -> devin_cli_audit.AuditConfig:
        return devin_cli_audit.AuditConfig(
            github_token="token",
            repo="owner/repo",
            pr_number=1,
            repo_paths={"owner/repo": self.repo},
            command=str(self.command),
            base_ref="main",
                    dry_run=False,
            **overrides,
        )


class TestDevinCliAuditPass(_DevinCliAuditTestCase):
    def test_known_clean_pass(self) -> None:
        self._write_fake(
            json.dumps(
                {
                    "verdict": "pass",
                    "summary": "No issues found.",
                    "findings": [],
                }
            )
        )
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ) as post:
            result = devin_cli_audit.audit_pr(self._config())

        self.assertEqual(result.verdict, "PASS")
        self.assertIn("Devin CLI Audit Result — PASS", result.comment_body)
        self.assertIn("<!-- DEVIN_CLI_AUDIT_STATE: devin-cli-audit-done -->", result.comment_body)
        self.assertTrue(post.called)
        self.assertIn("Head SHA:", result.comment_body)

    def test_known_blocked(self) -> None:
        self._write_fake(
            json.dumps(
                {
                    "verdict": "blocked",
                    "summary": "One blocker.",
                    "findings": [
                        {
                            "severity": "P1",
                            "title": "Bad logic",
                            "file": "file.py",
                            "line": 1,
                            "detail": "This is wrong.",
                        }
                    ],
                }
            )
        )
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            result = devin_cli_audit.audit_pr(self._config())

        self.assertEqual(result.verdict, "BLOCKED")
        self.assertIn("Devin CLI Audit Result — BLOCKED", result.comment_body)
        self.assertIn("<!-- DEVIN_CLI_AUDIT_STATE: devin-cli-audit-blocked -->", result.comment_body)

    def test_malformed_output_fails_closed(self) -> None:
        self._write_fake("not json")
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            result = devin_cli_audit.audit_pr(self._config())

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("<!-- DEVIN_CLI_AUDIT_STATE: needs-devin-cli-audit -->", result.comment_body)
        self.assertIn("INCOMPLETE", result.comment_body)

    def test_author_exclusion(self) -> None:
        self._write_fake(json.dumps({"verdict": "pass", "summary": "OK", "findings": []}))
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(author="devin-cli-audit-bot"),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            result = devin_cli_audit.audit_pr(self._config())

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("needs-devin-cli-audit", result.comment_body)

    def test_dirty_checkout_fails_closed(self) -> None:
        (self.repo / "uncommitted").write_text("x", encoding="utf-8")
        self._write_fake(json.dumps({"verdict": "pass", "summary": "OK", "findings": []}))
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ):
            with self.assertRaises(ProviderWorkspaceError):
                devin_cli_audit.audit_pr(self._config(allow_dirty=False))

    def test_timeout_fails_closed(self) -> None:
        self._write_fake(
            json.dumps({"verdict": "pass", "summary": "OK", "findings": []}),
            sleep=3,
        )
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            result = devin_cli_audit.audit_pr(self._config(timeout=1))

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("needs-devin-cli-audit", result.comment_body)

    def test_stale_head_after_run(self) -> None:
        self._write_fake(json.dumps({"verdict": "pass", "summary": "OK", "findings": []}))

        def _fetch(*args, **kwargs):
            # First call is the initial fetch; second is the stale check.
            _fetch.calls += 1
            if _fetch.calls == 1:
                return self._pr_meta()
            return self._pr_meta(moved=True)

        _fetch.calls = 0
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            side_effect=_fetch,
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            result = devin_cli_audit.audit_pr(self._config())

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("needs-devin-cli-audit", result.comment_body)

    def test_artifact_does_not_leak_raw_output_or_prompt(self) -> None:
        self._write_fake(
            json.dumps(
                {
                    "verdict": "pass",
                    "summary": "No issues.",
                    "findings": [],
                }
            )
        )
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            result = devin_cli_audit.audit_pr(self._config())

        self.assertIsNotNone(result.verdict_artifact_path)
        payload = json.loads(result.verdict_artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["verdict"], "pass")
        self.assertNotIn("stdout", payload)
        self.assertNotIn("stderr", payload)
        self.assertNotIn("prompt", payload)
        self.assertNotIn("token", payload)

class TestDevinCliAuditPostureAndLimits(_DevinCliAuditTestCase):
    def test_permission_mode_argv_uses_auto(self) -> None:
        argv_log = self.tmp / "devin-argv.log"
        fake = self.tmp / "fake-devin-argv"
        script = """#!/bin/sh
for a in "$@"; do
  echo "$a"
done > ARGV_LOG
echo '{"verdict": "pass", "summary": "OK", "findings": []}'
"""
        fake.write_text(script.replace("ARGV_LOG", str(argv_log)), encoding="utf-8")
        fake.chmod(0o755)
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            config = self._config()
            config.command = str(fake)
            devin_cli_audit.audit_pr(config)

        self.assertTrue(argv_log.exists())
        argv = argv_log.read_text(encoding="utf-8").splitlines()
        self.assertIn("--permission-mode", argv)
        permission_index = argv.index("--permission-mode")
        self.assertEqual(argv[permission_index + 1], "auto")
        self.assertNotIn("autonomous", argv)
        self.assertIn("--sandbox", argv)
        self.assertIn("--print", argv)
        self.assertIn("--prompt-file", argv)
        self.assertIn("--respect-workspace-trust", argv)

    def test_nonzero_exit_does_not_leak_raw_output(self) -> None:
        sentinel = "SECRET_TOKEN_d7f2a9c1"
        fake = self.tmp / "fake-devin-leak"
        fake.write_text(
            """#!/bin/sh
echo 'SECRET_TOKEN_d7f2a9c1'
echo 'SECRET_TOKEN_d7f2a9c1' >&2
exit 1
""",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            config = self._config()
            config.command = str(fake)
            result = devin_cli_audit.audit_pr(config)

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertNotIn(sentinel, result.comment_body)
        self.assertIn("no trustworthy verdict available", result.comment_body)
        self.assertIsNotNone(result.verdict_artifact_path)
        payload = json.loads(result.verdict_artifact_path.read_text(encoding="utf-8"))
        self.assertNotIn(sentinel, payload["comment_body"])
        self.assertNotIn("stdout", payload)
        self.assertNotIn("stderr", payload)
        self.assertNotIn("prompt", payload)

    def test_diff_hard_limit_truncates_to_unknown(self) -> None:
        self._write_fake(
            json.dumps({"verdict": "pass", "summary": "OK", "findings": []})
        )
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            result = devin_cli_audit.audit_pr(
                self._config(max_diff_bytes=50, max_diff_hard_limit_bytes=50)
            )

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("hard limit", result.comment_body.lower())
        self.assertNotIn("PASS", result.comment_body)



if __name__ == "__main__":
    unittest.main()
