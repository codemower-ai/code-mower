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
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ) as post:
            result = devin_cli_audit.audit_pr(self._config(allow_dirty=False))

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("needs-devin-cli-audit", result.comment_body)
        self.assertIn("INCOMPLETE", result.comment_body)
        self.assertNotIn("PASS", result.comment_body)
        self.assertTrue(post.called)

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

    def test_dry_run_unknown_renders_bounded_comment(self) -> None:
        # A stale head in dry-run mode must still render a bounded UNKNOWN
        # comment body without posting or crashing on an unbound artifact.
        def _fetch(*args, **kwargs):
            return self._pr_meta(moved=True)

        self._write_fake(json.dumps({"verdict": "pass", "summary": "OK", "findings": []}))
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            side_effect=_fetch,
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ) as post:
            result = devin_cli_audit.audit_pr(self._config(dry_run=True))

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("needs-devin-cli-audit", result.comment_body)
        self.assertIn("INCOMPLETE", result.comment_body)
        self.assertFalse(post.called)
        self.assertIsNone(result.verdict_artifact_path)

    def test_adaptive_diff_expansion_is_trustworthy(self) -> None:
        # target < diff <= hard limit: the complete diff tail must reach the
        # prompt and a PASS remains trustworthy.
        prompt_log = self.tmp / "prompt.log"
        fake = self.tmp / "fake-devin-prompt"
        script = """#!/bin/sh
prev=""
prompt_path=""
for a in "$@"; do
  if [ "$prev" = "--prompt-file" ]; then prompt_path="$a"; fi
  prev="$a"
done
cp "$prompt_path" PROMPT_LOG
echo '{"verdict": "pass", "summary": "Reviewed the complete diff.", "findings": []}'
"""
        fake.write_text(script.replace("PROMPT_LOG", str(prompt_log)), encoding="utf-8")
        fake.chmod(0o755)
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            config = self._config(max_diff_bytes=50, max_diff_hard_limit_bytes=1_000_000)
            config.command = str(fake)
            result = devin_cli_audit.audit_pr(config)

        self.assertEqual(result.verdict, "PASS")
        self.assertIn("devin-cli-audit-done", result.comment_body)
        self.assertIn("expanded above the normal target", result.comment_body)
        prompt_text = prompt_log.read_text(encoding="utf-8")
        # The complete diff tail (the PR change itself) must be present.
        self.assertIn("+b", prompt_text)
        self.assertNotIn("truncated this PR diff", prompt_text)

    def test_provider_write_fails_closed(self) -> None:
        # A fake Devin process that creates a file and emits PASS must never
        # persist a claimed PASS.
        fake = self.tmp / "fake-devin-write"
        fake.write_text(
            """#!/bin/sh
echo pwned > provider-created-file.txt
echo '{"verdict": "pass", "summary": "OK", "findings": []}'
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
        self.assertIn("needs-devin-cli-audit", result.comment_body)
        self.assertNotIn("devin-cli-audit-done", result.comment_body)

    def _write_prompt_observer(self, name: str, *, sleep: int = 0, exit_code: int = 0) -> tuple:
        mode_log = self.tmp / f"{name}.mode"
        path_log = self.tmp / f"{name}.path"
        fake = self.tmp / f"fake-devin-{name}"
        script = """#!/bin/sh
prev=""
prompt_path=""
for a in "$@"; do
  if [ "$prev" = "--prompt-file" ]; then prompt_path="$a"; fi
  prev="$a"
done
stat -f %Lp "$prompt_path" > MODE_LOG 2>/dev/null || stat -c %a "$prompt_path" > MODE_LOG
printf '%s' "$prompt_path" > PATH_LOG
sleep SLEEP
echo '{"verdict": "pass", "summary": "OK", "findings": []}'
exit EXIT_CODE
"""
        script = (
            script.replace("MODE_LOG", str(mode_log))
            .replace("PATH_LOG", str(path_log))
            .replace("SLEEP", str(sleep))
            .replace("EXIT_CODE", str(exit_code))
        )
        fake.write_text(script, encoding="utf-8")
        fake.chmod(0o755)
        return fake, mode_log, path_log

    def _run_with_fake(self, fake: Path, **overrides: object):
        with mock.patch(
            "code_mower.devin_cli_audit_pr.fetch_pull_request",
            return_value=self._pr_meta(),
        ), mock.patch(
            "code_mower.devin_cli_audit_pr.post_pr_comment",
            return_value={"id": 123},
        ):
            config = self._config(**overrides)
            config.command = str(fake)
            return devin_cli_audit.audit_pr(config)

    def test_prompt_file_mode_and_cleanup_on_success(self) -> None:
        fake, mode_log, path_log = self._write_prompt_observer("ok")
        result = self._run_with_fake(fake)
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(mode_log.read_text(encoding="utf-8").strip(), "600")
        self.assertFalse(Path(path_log.read_text(encoding="utf-8")).exists())

    def test_prompt_file_removed_on_nonzero_exit(self) -> None:
        fake, mode_log, path_log = self._write_prompt_observer("fail", exit_code=1)
        result = self._run_with_fake(fake)
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertEqual(mode_log.read_text(encoding="utf-8").strip(), "600")
        self.assertFalse(Path(path_log.read_text(encoding="utf-8")).exists())

    def test_prompt_file_removed_on_timeout(self) -> None:
        fake, mode_log, path_log = self._write_prompt_observer("slow", sleep=3)
        result = self._run_with_fake(fake, timeout=1)
        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertFalse(Path(path_log.read_text(encoding="utf-8")).exists())


if __name__ == "__main__":
    unittest.main()
