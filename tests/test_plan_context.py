from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
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

    def test_codex_review_writes_trusted_context_file_for_base_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            worktree = root / "worktree"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Code Mower"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "code-mower@example.com"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-qm", "base"],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            exclude_path = repo / ".git" / "info" / "exclude"
            exclude_before = exclude_path.read_text(encoding="utf-8")
            trusted_context = (
                "# Plan-Conformance Lens\n\n"
                "Supported transports: GitHub only.\n\n"
                "Trusted Code Mower decision registry:\nADR-007\n"
            )
            review_cwds: list[Path] = []

            def fake_run_progress(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                review_cwd = Path(str(kwargs["cwd"]))
                review_cwds.append(review_cwd)
                self.assertEqual(review_cwd, worktree)
                worktree_context_file = (
                    review_cwd / ".code-mower" / "codex-audit" / "trusted-audit-context.md"
                )
                agents_file = review_cwd / "AGENTS.md"
                agents_text = agents_file.read_text()
                self.assertFalse(worktree_context_file.exists())
                marker = "read `"
                context_start = agents_text.index(marker) + len(marker)
                context_end = agents_text.index("`", context_start)
                context_file = Path(agents_text[context_start:context_end])
                self.assertTrue(context_file.is_absolute())
                self.assertNotIn(str(review_cwd), str(context_file))
                self.assertIn("Supported transports: GitHub only.", context_file.read_text())
                self.assertIn("decision registry", agents_text)
                self.assertNotIn("-", command)
                self.assertNotIn("input", kwargs)
                return subprocess.CompletedProcess(command, 0, stdout="review text", stderr="")

            with (
                mock.patch.object(
                    codex_audit_pr,
                    "_resolve_executable_path",
                    return_value="codex",
                ),
                mock.patch.object(
                    codex_audit_pr,
                    "run_subprocess_with_progress",
                    side_effect=fake_run_progress,
                ),
            ):
                review_text, _stderr = codex_audit_pr.run_codex_review(
                    codex_audit_pr.AuditConfig(github_token="", repo_paths={}),
                    worktree,
                    trusted_context,
                )

            self.assertEqual(review_text, "review text")
            self.assertEqual(len(review_cwds), 1)
            self.assertEqual(exclude_path.read_text(encoding="utf-8"), exclude_before)

    def test_codex_review_skips_trusted_context_for_symlinked_agents_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            outside = root / "outside.txt"
            outside.write_text("do not overwrite\n", encoding="utf-8")
            (worktree / "AGENTS.md").symlink_to(outside)

            def fake_run_progress(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                review_cwd = Path(str(kwargs["cwd"]))
                self.assertTrue((review_cwd / "AGENTS.md").is_symlink())
                self.assertFalse(
                    (
                        review_cwd
                        / ".code-mower"
                        / "codex-audit"
                        / "trusted-audit-context.md"
                    ).exists()
                )
                return subprocess.CompletedProcess(command, 0, stdout="review text", stderr="")

            with (
                mock.patch.object(
                    codex_audit_pr,
                    "_resolve_executable_path",
                    return_value="codex",
                ),
                mock.patch.object(
                    codex_audit_pr,
                    "run_subprocess_with_progress",
                    side_effect=fake_run_progress,
                ),
            ):
                _review_text, stderr = codex_audit_pr.run_codex_review(
                    codex_audit_pr.AuditConfig(github_token="", repo_paths={}),
                    worktree,
                    "trusted context\n",
                )

            self.assertEqual(outside.read_text(encoding="utf-8"), "do not overwrite\n")
            self.assertIn("trusted audit context: skipped", stderr)
            self.assertIn("AGENTS.md is a symlink", stderr)

    def test_codex_review_skips_trusted_context_for_symlinked_context_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "worktree"
            worktree.mkdir()
            context_dir = worktree / ".code-mower" / "codex-audit"
            context_dir.mkdir(parents=True)
            outside = root / "outside-context.txt"
            outside.write_text("do not overwrite\n", encoding="utf-8")
            (context_dir / "trusted-audit-context.md").symlink_to(outside)

            def fake_run_progress(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                review_cwd = Path(str(kwargs["cwd"]))
                self.assertFalse((review_cwd / "AGENTS.md").exists())
                return subprocess.CompletedProcess(command, 0, stdout="review text", stderr="")

            with (
                mock.patch.object(
                    codex_audit_pr,
                    "_resolve_executable_path",
                    return_value="codex",
                ),
                mock.patch.object(
                    codex_audit_pr,
                    "run_subprocess_with_progress",
                    side_effect=fake_run_progress,
                ),
            ):
                _review_text, stderr = codex_audit_pr.run_codex_review(
                    codex_audit_pr.AuditConfig(github_token="", repo_paths={}),
                    worktree,
                    "trusted context\n",
                )

            self.assertEqual(outside.read_text(encoding="utf-8"), "do not overwrite\n")
            self.assertIn("trusted audit context: skipped", stderr)
            self.assertIn("trusted-audit-context.md", stderr)

    def test_codex_review_merges_generated_agents_with_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            old_generated = (
                "<!-- CODE_MOWER_CODEX_AUDIT_CONTEXT_BEGIN -->\n"
                "old generated text\n"
                "<!-- CODE_MOWER_CODEX_AUDIT_CONTEXT_END -->\n"
            )
            (worktree / "AGENTS.md").write_text(
                old_generated + "\nKeep repo instructions.\n",
                encoding="utf-8",
            )

            def fake_run_progress(
                command: list[str],
                **kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                agents_text = (Path(str(kwargs["cwd"])) / "AGENTS.md").read_text(
                    encoding="utf-8"
                )
                self.assertIn("Code Mower Audit Context", agents_text)
                self.assertNotIn("old generated text", agents_text)
                self.assertIn("Keep repo instructions.", agents_text)
                self.assertLess(
                    agents_text.index("Code Mower Audit Context"),
                    agents_text.index("Keep repo instructions."),
                )
                return subprocess.CompletedProcess(command, 0, stdout="review text", stderr="")

            with (
                mock.patch.object(
                    codex_audit_pr,
                    "_resolve_executable_path",
                    return_value="codex",
                ),
                mock.patch.object(
                    codex_audit_pr,
                    "run_subprocess_with_progress",
                    side_effect=fake_run_progress,
                ),
            ):
                review_text, stderr = codex_audit_pr.run_codex_review(
                    codex_audit_pr.AuditConfig(github_token="", repo_paths={}),
                    worktree,
                    "trusted context\n",
                )

        self.assertEqual(review_text, "review text")
        self.assertEqual(stderr, "")

    def test_codex_review_omits_plan_prompt_when_no_context_sections_rendered(self) -> None:
        rendered = plan_context.RenderedPlanContext(
            text=plan_context.PLAN_CONFORMANCE_INSTRUCTIONS,
            included_documents=0,
            included_bytes=0,
        )

        self.assertEqual(codex_audit_pr._codex_review_context_prompt(rendered), "")

    def test_renderer_reads_default_context_from_trusted_git_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            project_dir = root / ".code-mower" / "project-context"
            project_dir.mkdir(parents=True)
            architecture = project_dir / "architecture.md"
            architecture.write_text("committed plan\n", encoding="utf-8")
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
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Code Mower",
                    "-c",
                    "user.email=code-mower@example.com",
                    "commit",
                    "-m",
                    "add plan context",
                ],
                cwd=root,
                check=True,
                capture_output=True,
            )
            architecture.write_text("mutable working tree plan\n", encoding="utf-8")

            rendered = plan_context.render_plan_context(
                repo_root=root,
                trusted_git_ref="HEAD",
            )

            self.assertIn("committed plan", rendered.text)
            self.assertNotIn("mutable working tree plan", rendered.text)

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
