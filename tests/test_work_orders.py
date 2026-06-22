from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from code_mower import builder_experiment
from code_mower import work_orders


class WorkOrderTests(unittest.TestCase):
    def test_project_context_init_writes_editable_local_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "project-context"
            report = work_orders.create_project_context(
                output_dir=output_dir,
                project_name="Demo Repo",
            )

            self.assertEqual(report["schema"], work_orders.PROJECT_CONTEXT_SCHEMA)
            self.assertTrue(Path(report["manifest_path"]).exists())
            self.assertTrue(
                (output_dir / "architecture.md")
                .read_text(encoding="utf-8")
                .startswith("# Architecture\n")
            )
            self.assertIn(
                "Code Mower treats it as planning input",
                (output_dir / "quality-bar.md").read_text(encoding="utf-8"),
            )

            second_report = work_orders.create_project_context(
                output_dir=output_dir,
                project_name="Demo Repo",
            )
            self.assertEqual(second_report["created"], [])
            self.assertTrue(second_report["skipped"])

    def test_external_context_manifest_is_metadata_only_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "product-context.md"
            source.write_text("# Product context\n\nKeep this local.\n", encoding="utf-8")
            output_dir = Path(tmp) / "external"

            manifest = work_orders.add_external_context([source], output_dir=output_dir)

            self.assertEqual(manifest["schema"], work_orders.EXTERNAL_CONTEXT_SCHEMA)
            self.assertEqual(manifest["entry_count"], 1)
            entry = manifest["entries"][0]
            self.assertEqual(entry["filename"], "product-context.md")
            self.assertIs(entry["text_preview_included"], False)
            self.assertNotIn("text_preview_path", entry)
            self.assertTrue(Path(manifest["manifest_path"]).exists())

    def test_external_context_can_write_bounded_preview_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "requirements.txt"
            source.write_text("first line\nsecond line\n", encoding="utf-8")

            manifest = work_orders.add_external_context(
                [source],
                output_dir=Path(tmp) / "external",
                include_preview=True,
                max_preview_chars=5,
            )

            entry = manifest["entries"][0]
            self.assertIs(entry["text_preview_included"], True)
            self.assertIs(entry["text_preview_truncated"], True)
            preview_path = Path(entry["text_preview_path"])
            self.assertEqual(preview_path.read_text(encoding="utf-8"), "first\n")

    def test_external_context_rejects_negative_preview_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "requirements.txt"
            source.write_text("private context\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "max-preview-chars"):
                work_orders.add_external_context(
                    [source],
                    output_dir=Path(tmp) / "external",
                    include_preview=True,
                    max_preview_chars=-1,
                )

    def test_issue_plan_work_order_critique_and_builder_seed_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            issue_body = "Add a privacy-preserving setup command for project doctrine."
            plan = work_orders.build_issue_plan(
                title="Project context setup",
                body=issue_body,
                issue_url="https://github.com/owner/repo/issues/1",
                repo="owner/repo",
                issue_number="1",
            )
            issue_plan_path = root / "issue-plan.md"
            work_orders.write_issue_plan(plan, issue_plan_path)

            work_order = work_orders.draft_work_order(
                title="Project context setup",
                source_text=issue_plan_path.read_text(encoding="utf-8"),
                repo="owner/repo",
                role_lenses=["architect", "qa"],
                review_lanes=["codex-audit"],
                output=root / "work-order.md",
            )
            work_order_path = Path(work_order["output_path"])
            self.assertIn("## Role/Lens Passes", work_order_path.read_text(encoding="utf-8"))

            critique = work_orders.create_critique_plan(
                work_order_path,
                reviewers=["codex", "claude"],
                output_dir=root / "critique",
            )
            self.assertEqual(len(critique["prompts"]), 2)
            self.assertTrue(Path(critique["manifest_path"]).exists())

            seed = work_orders.seed_builder_experiment(
                work_order_path,
                repo="owner/repo",
                builders=["codex-desktop", "claude-code"],
                output=root / "builder-experiment.json",
                task_class="planning",
                context_packs=["project-context"],
                prompt_lenses=["generic-programming"],
            )
            self.assertEqual(seed["builder_count"], 2)
            spec = json.loads(Path(seed["output_path"]).read_text(encoding="utf-8"))
            normalized = builder_experiment.normalize_spec(spec)
            self.assertEqual(normalized["tasks"][0]["repo"], "owner/repo")
            self.assertEqual(normalized["builders"][0]["builder_id"], "codex-desktop")

    def test_plan_from_issue_prints_markdown_when_stdout_is_selected(self) -> None:
        out = StringIO()
        with redirect_stdout(out):
            exit_code = work_orders.plan_main(
                [
                    "from-issue",
                    "--title",
                    "Planning workflow",
                    "--body",
                    "Add a planning workflow.",
                ]
            )

        self.assertEqual(exit_code, 0)
        text = out.getvalue()
        self.assertTrue(text.startswith("# Issue Plan: Planning workflow"))
        self.assertIn("Add a planning workflow.", text)

    def test_plan_from_issue_reports_missing_body_file_as_cli_error(self) -> None:
        err = StringIO()
        with redirect_stderr(err):
            exit_code = work_orders.plan_main(
                [
                    "from-issue",
                    "--title",
                    "Planning workflow",
                    "--body-file",
                    "/tmp/does-not-exist-code-mower-plan.md",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("error: could not read", err.getvalue())

    def test_issue_plan_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "issue-plan.md"
            output.write_text("locally edited\n", encoding="utf-8")
            plan = work_orders.build_issue_plan(
                title="Planning workflow",
                body="Add a planning workflow.",
            )

            with self.assertRaisesRegex(ValueError, "pass --force"):
                work_orders.write_issue_plan(plan, output)
            self.assertEqual(output.read_text(encoding="utf-8"), "locally edited\n")

            work_orders.write_issue_plan(plan, output, force=True)
            self.assertTrue(
                output.read_text(encoding="utf-8").startswith("# Issue Plan: Planning workflow")
            )

    def test_work_order_draft_rejects_json_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "Markdown path"):
                work_orders.draft_work_order(
                    title="Planning workflow",
                    source_text="Add a planning workflow.",
                    output=Path(tmp) / "work-order.json",
                )

    def test_work_order_draft_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "work-order.md"
            manifest = Path(tmp) / "work-order.json"
            output.write_text("locally edited\n", encoding="utf-8")
            manifest.write_text("{\"edited\": true}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "pass --force"):
                work_orders.draft_work_order(
                    title="Planning workflow",
                    source_text="Add a planning workflow.",
                    output=output,
                )

            self.assertEqual(output.read_text(encoding="utf-8"), "locally edited\n")
            self.assertEqual(manifest.read_text(encoding="utf-8"), "{\"edited\": true}\n")

            work_orders.draft_work_order(
                title="Planning workflow",
                source_text="Add a planning workflow.",
                output=output,
                force=True,
            )
            self.assertTrue(
                output.read_text(encoding="utf-8").startswith("# Work Order: Planning workflow")
            )


if __name__ == "__main__":
    unittest.main()
