"""Focused tests for the informational Devin CLI reviewer lane (#746)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
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
            result = devin_cli_audit.audit_pr(self._config())

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("needs-devin-cli-audit", result.comment_body)
        self.assertIn("INCOMPLETE", result.comment_body)
        self.assertNotIn("PASS", result.comment_body)
        self.assertTrue(post.called)

    def test_allow_dirty_option_is_rejected(self) -> None:
        # The lane has no dirty-checkout escape hatch; argparse must reject the
        # flag outright rather than silently ignoring it.
        argv = [
            "--repo",
            "owner/repo",
            "--pr",
            "1",
            "--repo-paths",
            f"owner/repo={self.repo}",
            "--dry-run",
            "--allow-dirty",
        ]
        stderr = io.StringIO()
        with mock.patch("sys.stderr", new=stderr):
            with self.assertRaises(SystemExit) as ctx:
                devin_cli_audit.main(argv)

        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("unrecognized arguments: --allow-dirty", stderr.getvalue())

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


def _child_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - still alive, owned elsewhere
        return True
    return True


def _assert_child_reaped(test: unittest.TestCase, pid_file: Path, what: str) -> None:
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 15.0
    while _child_is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    test.assertFalse(_child_is_alive(pid), f"spawned child survived the {what}")


_SPAWN_ORPHAN = "sh -c 'while true; do sleep 1; done' &\n"


class TestGitLimitedDeadline(_DevinCliAuditTestCase):
    """A stalled git subprocess must not hold the lane past its deadline."""

    def _fake_git(self, body: str) -> Path:
        bin_dir = self.tmp / "fake-bin"
        bin_dir.mkdir(exist_ok=True)
        fake_git = bin_dir / "git"
        fake_git.write_text(body, encoding="utf-8")
        fake_git.chmod(0o755)
        return bin_dir

    def _run_with_path(self, bin_dir: Path, **kwargs: object):
        with mock.patch.dict(
            os.environ,
            {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"},
        ):
            return devin_cli_audit._run_git_limited(self.repo, ["diff"], **kwargs)

    def test_stalled_git_times_out_without_leaking_output(self) -> None:
        bin_dir = self._fake_git(
            "#!/bin/sh\n"
            "printf '%s\\n' 'SENTINEL_STDOUT_SHOULD_NOT_LEAK'\n"
            "printf '%s\\n' 'SENTINEL_STDERR_SHOULD_NOT_LEAK' >&2\n"
            "sleep 60\n"
        )
        started = time.monotonic()
        with self.assertRaises(devin_cli_audit.NoTrustworthyVerdictError) as ctx:
            self._run_with_path(bin_dir, max_bytes=1024, timeout=1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 30)
        message = str(ctx.exception)
        self.assertIn("deadline", message)
        self.assertNotIn("SENTINEL_STDOUT_SHOULD_NOT_LEAK", message)
        self.assertNotIn("SENTINEL_STDERR_SHOULD_NOT_LEAK", message)

    def test_silent_stalled_git_times_out(self) -> None:
        bin_dir = self._fake_git("#!/bin/sh\nsleep 60\n")
        started = time.monotonic()
        with self.assertRaises(devin_cli_audit.NoTrustworthyVerdictError):
            self._run_with_path(bin_dir, max_bytes=1024, timeout=1)
        self.assertLess(time.monotonic() - started, 30)

    def test_bounded_output_still_collected_within_deadline(self) -> None:
        bin_dir = self._fake_git(
            "#!/bin/sh\nprintf '%s\\n' 'fake diff output'\n"
        )
        text, observed, truncated = self._run_with_path(
            bin_dir, max_bytes=1024, timeout=30
        )
        self.assertIn("fake diff output", text)
        self.assertGreater(observed, 0)
        self.assertFalse(truncated)

    def test_over_limit_output_is_bounded_and_killed(self) -> None:
        bin_dir = self._fake_git(
            "#!/bin/sh\ni=0\nwhile [ $i -lt 4096 ]; do\n"
            "  printf 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\\n'\n"
            "  i=$((i + 1))\ndone\nsleep 60\n"
        )
        started = time.monotonic()
        text, observed, truncated = self._run_with_path(
            bin_dir, max_bytes=4096, timeout=30
        )
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(truncated)
        self.assertIn("diff truncated by devin-cli-audit wrapper", text)

    def test_timeout_reaps_spawned_git_child(self) -> None:
        pid_file = self.tmp / "git-child.pid"
        bin_dir = self._fake_git(
            "#!/bin/sh\n"
            + _SPAWN_ORPHAN
            + f'echo $! > "{pid_file}"\n'
            + "sleep 60\n"
        )
        with self.assertRaises(devin_cli_audit.NoTrustworthyVerdictError):
            self._run_with_path(bin_dir, max_bytes=1024, timeout=2)

        _assert_child_reaped(self, pid_file, "bounded git timeout")


class TestDevinCliProcessBounds(_DevinCliAuditTestCase):
    """The Devin CLI process is byte-bounded, deadline-bounded, and reaped."""

    SENTINEL = "SENTINEL_RAW_OUTPUT_9c31ab"
    VERDICT = '{"verdict": "pass", "summary": "OK", "findings": []}'

    def _fake(self, name: str, body: str) -> Path:
        fake = self.tmp / f"fake-devin-{name}"
        fake.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        fake.chmod(0o755)
        return fake

    def _flood(self, text: str, *, to_stderr: bool = False) -> str:
        redirect = " >&2" if to_stderr else ""
        return (
            "i=0\n"
            "while [ $i -lt 512 ]; do\n"
            f"  printf '%s\\n' '{text}'{redirect}\n"
            "  i=$((i + 1))\n"
            "done\n"
        )

    def _run(self, fake: Path, **kwargs: object):
        return devin_cli_audit._run_devin_cli(
            command=str(fake),
            prompt="prompt",
            model="",
            cwd=self.repo,
            **kwargs,
        )

    def test_bounded_output_returned_with_independent_stream_bounds(self) -> None:
        # Noisy stderr well past the stdout bound must neither consume the
        # stdout budget nor reject an otherwise small, complete stdout verdict.
        fake = self._fake(
            "independent",
            self._flood(self.SENTINEL, to_stderr=True) + f"echo '{self.VERDICT}'\n",
        )
        stdout, returncode, _duration = self._run(
            fake, timeout=30, max_stdout_bytes=1024, max_stderr_bytes=1024 * 1024
        )

        self.assertEqual(returncode, 0)
        self.assertIn('"verdict": "pass"', stdout)
        self.assertNotIn(self.SENTINEL, stdout)

    def test_overflow_and_timeout_fail_closed_without_raw_output(self) -> None:
        cases = (
            (
                "stdout-overflow",
                self._flood(self.SENTINEL),
                {"timeout": 30, "max_stdout_bytes": 1024, "max_stderr_bytes": 1024},
                "stdout exceeded",
            ),
            (
                "stderr-overflow",
                self._flood(self.SENTINEL, to_stderr=True) + f"echo '{self.VERDICT}'\n",
                {
                    "timeout": 30,
                    "max_stdout_bytes": 1024 * 1024,
                    "max_stderr_bytes": 1024,
                },
                "stderr exceeded",
            ),
            (
                "timeout",
                f"printf '%s\\n' '{self.SENTINEL}'\n"
                f"printf '%s\\n' '{self.SENTINEL}' >&2\n"
                "sleep 60\n",
                {"timeout": 1, "max_stdout_bytes": 1024, "max_stderr_bytes": 1024},
                "timed out",
            ),
        )
        for name, body, kwargs, expected in cases:
            with self.subTest(case=name):
                fake = self._fake(name, body)
                started = time.monotonic()
                failure = devin_cli_audit.NoTrustworthyVerdictError
                with self.assertRaises(failure) as ctx:
                    self._run(fake, **kwargs)

                self.assertLess(time.monotonic() - started, 30)
                message = str(ctx.exception)
                self.assertIn(expected, message)
                self.assertNotIn(self.SENTINEL, message)

    def test_spawned_process_group_is_reaped(self) -> None:
        cases = (
            (
                "timeout",
                "sleep 60\n",
                {"timeout": 2, "max_stdout_bytes": 1024, "max_stderr_bytes": 1024},
            ),
            (
                "stdout-overflow",
                self._flood("x" * 40) + "sleep 60\n",
                {"timeout": 30, "max_stdout_bytes": 1024, "max_stderr_bytes": 1024},
            ),
        )
        for name, tail, kwargs in cases:
            with self.subTest(case=name):
                pid_file = self.tmp / f"devin-{name}-child.pid"
                fake = self._fake(
                    f"spawner-{name}",
                    _SPAWN_ORPHAN + f'echo $! > "{pid_file}"\n' + tail,
                )
                with self.assertRaises(devin_cli_audit.NoTrustworthyVerdictError):
                    self._run(fake, **kwargs)

                _assert_child_reaped(self, pid_file, f"Devin CLI {name}")

    def test_overflow_does_not_leak_raw_output_to_comment_or_artifact(self) -> None:
        fake = self._fake("audit-overflow", self._flood(self.SENTINEL))
        with mock.patch.object(devin_cli_audit, "MAX_DEVIN_STDOUT_BYTES", 512):
            result = self._run_with_fake(fake)

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("needs-devin-cli-audit", result.comment_body)
        self.assertIn("exceeded", result.comment_body)
        self.assertNotIn(self.SENTINEL, result.comment_body)
        self.assertIsNotNone(result.verdict_artifact_path)
        payload = json.loads(result.verdict_artifact_path.read_text(encoding="utf-8"))
        self.assertNotIn(self.SENTINEL, json.dumps(payload))


class TestChangedFileNameCollection(_DevinCliAuditTestCase):
    """Changed-file names are bounded, fail closed, and survive odd paths."""

    WEIRD_NAME = 'odd dir/weird\nname "quoted" ünicode.py'

    def _commit_weird_named_file(self) -> str:
        target = self.repo / self.WEIRD_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("weird\n", encoding="utf-8")
        self._run_git(["add", "--", self.WEIRD_NAME])
        self._run_git(["commit", "-m", "weird name"])
        self.head_sha = self._run_git_text(["rev-parse", "HEAD"])
        return self.WEIRD_NAME

    def _resolve_diff(self, **overrides: object):
        return devin_cli_audit._resolve_diff(
            self.repo,
            1,
            "main",
            self.head_sha,
            int(overrides.get("max_diff_bytes", devin_cli_audit.DEFAULT_MAX_DIFF_BYTES)),
            int(
                overrides.get(
                    "max_diff_hard_limit_bytes",
                    devin_cli_audit.DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES,
                )
            ),
        )

    def test_names_are_collected_nul_delimited_through_the_bounded_primitive(self) -> None:
        name = self._commit_weird_named_file()
        real_run_git_limited = devin_cli_audit._run_git_limited
        limited_calls: list[tuple[list[str], dict]] = []

        def _spy(cwd, args, **kwargs):
            limited_calls.append((list(args), dict(kwargs)))
            return real_run_git_limited(cwd, args, **kwargs)

        with mock.patch.object(
            devin_cli_audit, "_run_git_limited", side_effect=_spy
        ), mock.patch.object(
            devin_cli_audit, "run_git", wraps=devin_cli_audit.run_git
        ) as run_git_spy:
            _diff, changed_files = self._resolve_diff()

        # A newline in a path is exactly what line-splitting gets wrong: the
        # weird name must arrive as one entry, not two fragments.
        self.assertEqual(sorted(changed_files), sorted(("file.py", name)))

        name_only_calls = [
            (args, kwargs) for args, kwargs in limited_calls if "--name-only" in args
        ]
        self.assertEqual(len(name_only_calls), 1)
        args, kwargs = name_only_calls[0]
        self.assertIn("-z", args)
        self.assertEqual(
            kwargs["max_bytes"], devin_cli_audit.MAX_CHANGED_FILE_NAMES_BYTES
        )
        # Unbounded run_git must never be the path that collects the names.
        for call in run_git_spy.call_args_list:
            self.assertNotIn("--name-only", list(call.args[1]))

    def test_name_list_overflow_fails_closed_without_leaking_names(self) -> None:
        self._commit_weird_named_file()
        with mock.patch.object(devin_cli_audit, "MAX_CHANGED_FILE_NAMES_BYTES", 4):
            with self.assertRaises(devin_cli_audit.NoTrustworthyVerdictError) as ctx:
                self._resolve_diff()

        message = str(ctx.exception)
        self.assertIn("changed-file list", message)
        self.assertIn("no trustworthy verdict available", message)
        self.assertNotIn("file.py", message)
        self.assertNotIn("odd dir", message)

    def test_name_list_overflow_yields_unknown_verdict(self) -> None:
        self._commit_weird_named_file()
        self._write_fake(json.dumps({"verdict": "pass", "summary": "OK", "findings": []}))
        with mock.patch.object(devin_cli_audit, "MAX_CHANGED_FILE_NAMES_BYTES", 4):
            result = self._run_with_fake(self.command)

        self.assertEqual(result.verdict, "UNKNOWN")
        self.assertIn("needs-devin-cli-audit", result.comment_body)
        self.assertNotIn("devin-cli-audit-done", result.comment_body)
        payload = json.loads(result.verdict_artifact_path.read_text(encoding="utf-8"))
        self.assertNotIn("odd dir", json.dumps(payload))


class TestDisposableHeadCheckout(_DevinCliAuditTestCase):
    """Devin reviews a throwaway exact-head clone, never the audit checkout."""

    def setUp(self) -> None:
        super().setUp()
        self.observed: list[Path] = []

    def _spy_disposable_checkout(self):
        real = devin_cli_audit._disposable_head_checkout
        observed = self.observed

        @contextlib.contextmanager
        def _spy(repo_path, head_sha):
            with real(repo_path, head_sha) as review_path:
                observed.append(Path(review_path))
                yield review_path

        return mock.patch.object(devin_cli_audit, "_disposable_head_checkout", _spy)

    def _assert_discarded(self) -> None:
        self.assertEqual(len(self.observed), 1)
        review_path = self.observed[0]
        self.assertFalse(review_path.exists())
        self.assertFalse(review_path.parent.exists())

    def _assert_no_path_leak(self, result) -> None:
        review_path = self.observed[0]
        for text in (str(review_path), str(review_path.parent)):
            self.assertNotIn(text, result.comment_body)
        if result.verdict_artifact_path is not None:
            payload = result.verdict_artifact_path.read_text(encoding="utf-8")
            self.assertNotIn(str(review_path), payload)
            self.assertNotIn(str(review_path.parent), payload)

    def _fake(self, name: str, body: str) -> Path:
        fake = self.tmp / f"fake-devin-{name}"
        fake.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        fake.chmod(0o755)
        return fake

    def test_devin_cwd_is_a_disposable_exact_head_clone_without_origin(self) -> None:
        logs = {name: self.tmp / f"{name}.log" for name in ("cwd", "head", "remote", "config", "alternates")}
        fake = self._fake(
            "inspect",
            f"pwd > {logs['cwd']}\n"
            f"git rev-parse HEAD > {logs['head']}\n"
            f"git remote > {logs['remote']}\n"
            f"git config --local --get-regexp 'remote|credential' > {logs['config']} 2>/dev/null\n"
            f"ls .git/objects/info/alternates > {logs['alternates']} 2>&1\n"
            "echo '{\"verdict\": \"pass\", \"summary\": \"OK\", \"findings\": []}'\n",
        )
        with self._spy_disposable_checkout():
            result = self._run_with_fake(fake)

        self.assertEqual(result.verdict, "PASS")
        review_path = self.observed[0]
        observed_cwd = Path(logs["cwd"].read_text(encoding="utf-8").strip())
        self.assertEqual(observed_cwd.resolve(), review_path.resolve())
        self.assertNotEqual(observed_cwd.resolve(), self.repo.resolve())
        self.assertEqual(logs["head"].read_text(encoding="utf-8").strip(), self.head_sha)
        self.assertEqual(logs["remote"].read_text(encoding="utf-8").strip(), "")
        self.assertEqual(logs["config"].read_text(encoding="utf-8").strip(), "")
        # No alternates file: the copy borrows no objects from the audit checkout.
        self.assertNotIn("alternates\n", logs["alternates"].read_text(encoding="utf-8"))

        self._assert_discarded()
        self._assert_no_path_leak(result)
        # The trusted checkout is untouched.
        self.assertEqual(self._run_git_text(["rev-parse", "HEAD"]), self.head_sha)
        self.assertEqual(self._run_git_text(["status", "--porcelain"]), "")

    def test_disposable_checkout_is_discarded_on_every_exit_path(self) -> None:
        verdict = '{"verdict": "pass", "summary": "OK", "findings": []}'
        cases = (
            ("success", f"echo '{verdict}'", {}, "PASS"),
            ("provider-failure", f"echo '{verdict}'\nexit 1", {}, "UNKNOWN"),
            ("timeout", f"sleep 5\necho '{verdict}'", {"timeout": 1}, "UNKNOWN"),
        )
        for name, body, overrides, expected in cases:
            with self.subTest(case=name):
                self.observed.clear()
                fake = self._fake(name, body)
                with self._spy_disposable_checkout():
                    result = self._run_with_fake(fake, **overrides)

                self.assertEqual(result.verdict, expected)
                self._assert_discarded()
                self._assert_no_path_leak(result)

        with self.subTest(case="exception"):
            self.observed.clear()
            fake = self._fake("boom", f"echo '{verdict}'")
            with self._spy_disposable_checkout(), mock.patch.object(
                devin_cli_audit,
                "_run_devin_cli",
                side_effect=RuntimeError("unexpected failure"),
            ):
                with self.assertRaises(RuntimeError):
                    self._run_with_fake(fake)

            self._assert_discarded()

    def test_provider_writes_to_ignored_files_and_git_metadata_do_not_persist(self) -> None:
        (self.repo / ".gitignore").write_text("scratch/\n", encoding="utf-8")
        self._run_git(["add", ".gitignore"])
        self._run_git(["commit", "-m", "ignore scratch"])
        self.head_sha = self._run_git_text(["rev-parse", "HEAD"])

        fake = self._fake(
            "ignored-writes",
            "mkdir -p scratch\n"
            "echo pwned > scratch/ignored.txt\n"
            "echo pwned > .git/PWNED\n"
            "git config --local devin.pwned true\n"
            "echo '{\"verdict\": \"pass\", \"summary\": \"OK\", \"findings\": []}'\n",
        )
        with self._spy_disposable_checkout():
            result = self._run_with_fake(fake)

        # Ordinary status never reports these writes, which is exactly why the
        # provider must not run in the reusable audit checkout.
        self.assertEqual(result.verdict, "PASS")
        self._assert_discarded()
        self._assert_no_path_leak(result)
        self.assertFalse((self.repo / "scratch").exists())
        self.assertFalse((self.repo / ".git" / "PWNED").exists())
        self.assertEqual(
            subprocess.run(
                ["git", "config", "--local", "--get", "devin.pwned"],
                cwd=self.repo,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "",
        )
        self.assertEqual(self._run_git_text(["status", "--porcelain"]), "")
        self.assertEqual(self._run_git_text(["rev-parse", "HEAD"]), self.head_sha)


if __name__ == "__main__":
    unittest.main()
