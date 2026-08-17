from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from code_mower import claude_audit_pr, codex_audit_pr, plan_context


class PlanContextTests(unittest.TestCase):
    def test_renderer_reads_project_docs_and_external_previews_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / ".code-mower" / "project-context"
            project_dir.mkdir(parents=True)
            architecture = project_dir / "architecture.md"
            architecture.write_text(
                "# Architecture\n\nSupported transports: GitHub only.\n",
                encoding="utf-8",
            )
            (project_dir / "project-context-manifest.json").write_text(
                (
                    "{"
                    '"documents": ['
                    '{"path": ".code-mower/project-context/architecture.md", '
                    '"title": "Architecture"}'
                    "]}"
                ),
                encoding="utf-8",
            )

            external_dir = root / ".code-mower" / "context" / "external"
            preview_dir = external_dir / "previews"
            preview_dir.mkdir(parents=True)
            preview = preview_dir / "operating-model.preview.txt"
            preview.write_text("Owner approvals stay outside hosted uploads.\n", encoding="utf-8")
            (external_dir / "external-context-manifest.json").write_text(
                (
                    "{"
                    '"entries": ['
                    '{"filename": "operating-model.md", "bytes": 120, '
                    '"sha256": "abc123", "text_preview_included": true, '
                    '"text_preview_path": ".code-mower/context/external/previews/operating-model.preview.txt"},'
                    '{"filename": "raw-private.md", "bytes": 1000, '
                    '"sha256": "def456", "source_path": "/private/raw-private.md", '
                    '"text_preview_included": false}'
                    "]}"
                ),
                encoding="utf-8",
            )

            rendered = plan_context.render_plan_context(repo_root=root)

            self.assertIn("Contradicts plan of record", rendered.text)
            self.assertIn("Supported transports: GitHub only.", rendered.text)
            self.assertIn("Owner approvals stay outside hosted uploads.", rendered.text)
            self.assertIn("raw-private.md", rendered.text)
            self.assertNotIn("/private/raw-private.md", rendered.text)

    def test_renderer_warns_on_malformed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_dir = root / ".code-mower" / "project-context"
            project_dir.mkdir(parents=True)
            (project_dir / "project-context-manifest.json").write_text("{", encoding="utf-8")

            rendered = plan_context.render_plan_context(repo_root=root)

            self.assertEqual(rendered.included_documents, 0)
            self.assertIn("Plan Context Warnings", rendered.text)
            self.assertIn("unable to read plan context manifest", rendered.text)

    def test_renderer_rejects_context_paths_outside_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = Path(tmp).parent / "outside-plan-secret.txt"
            secret.write_text("do not exfiltrate\n", encoding="utf-8")
            self.addCleanup(lambda: secret.unlink(missing_ok=True))
            project_dir = root / ".code-mower" / "project-context"
            project_dir.mkdir(parents=True)
            (project_dir / "project-context-manifest.json").write_text(
                (
                    "{"
                    '"documents": ['
                    f'{{"path": "{secret}", "title": "Secret"}},'
                    '{"path": "../outside-plan-secret.txt", "title": "Relative Escape"}'
                    "]}"
                ),
                encoding="utf-8",
            )

            rendered = plan_context.render_plan_context(repo_root=root)

            self.assertEqual(rendered.included_documents, 0)
            self.assertIn("plan context path escapes repo root", rendered.text)
            self.assertNotIn("do not exfiltrate", rendered.text)

    def test_codex_review_passes_plan_context_prompt_on_stdin(self) -> None:
        calls = {}

        def fake_run(command, **kwargs):
            calls["command"] = command
            calls["input"] = kwargs.get("input")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(
                    codex_audit_pr,
                    "_resolve_executable_path",
                    return_value="codex",
                ),
                mock.patch.object(
                    codex_audit_pr,
                    "run_subprocess_with_progress",
                    side_effect=fake_run,
                ),
                mock.patch.object(
                    codex_audit_pr,
                    "_read_last_message_file",
                    return_value="review text",
                ),
            ):
                review, _stderr = codex_audit_pr.run_codex_review(
                    codex_audit_pr.AuditConfig(github_token="", repo_paths={}),
                    Path(tmp),
                    "Plan context prompt",
                )

        self.assertEqual(review, "review text")
        self.assertEqual(calls["command"][-1], "-")
        self.assertEqual(calls["input"], "Plan context prompt")

    def test_codex_review_omits_plan_prompt_when_no_context_sections_rendered(self) -> None:
        rendered = plan_context.RenderedPlanContext(
            text=plan_context.PLAN_CONFORMANCE_INSTRUCTIONS,
            included_documents=0,
            included_bytes=0,
        )

        self.assertEqual(codex_audit_pr._codex_review_plan_prompt(rendered), "")

    def test_codex_preflight_requires_review_stdin_prompt_support(self) -> None:
        def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if command == ["codex", "--version"]:
                return subprocess.CompletedProcess(command, 0, stdout="codex 0.140.0\n", stderr="")
            if command == ["codex", "exec", "--help"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="--ignore-user-config\n--output-schema\n--output-last-message\n"
                    "--skip-git-repo-check\n",
                    stderr="",
                )
            if command == ["codex", "exec", "review", "--help"]:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="--base\n--output-last-message\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected command: {command}")

        config = codex_audit_pr.AuditConfig(github_token="", repo_paths={})
        with (
            mock.patch.object(
                codex_audit_pr,
                "_resolve_executable_path",
                return_value="codex",
            ),
            mock.patch.object(codex_audit_pr.subprocess, "run", side_effect=fake_run),
        ):
            with self.assertRaisesRegex(RuntimeError, "stdin prompt"):
                codex_audit_pr.preflight_codex_cli(config)

    def test_claude_prompt_marks_plan_context_as_trusted(self) -> None:
        prompt = claude_audit_pr._review_prompt(
            repo="owner/repo",
            pr_number=1,
            head_sha="a" * 40,
            base_ref="origin/main",
            branch_name="feature",
            title="Update docs",
            diff_stat="README.md | 1 +",
            diff_text="+new docs",
            was_truncated=False,
            plan_context_text="Contradicts plan of record? Check supported transports.",
        )

        self.assertIn("BEGIN TRUSTED PLAN CONTEXT", prompt)
        self.assertIn("Contradicts plan of record?", prompt)


if __name__ == "__main__":
    unittest.main()
