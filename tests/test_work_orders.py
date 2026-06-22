from __future__ import annotations

import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from code_mower import builder_experiment
from code_mower import work_orders


def test_project_context_init_writes_editable_local_docs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp) / "project-context"
        report = work_orders.create_project_context(
            output_dir=output_dir,
            project_name="Demo Repo",
        )

        assert report["schema"] == work_orders.PROJECT_CONTEXT_SCHEMA
        assert Path(report["manifest_path"]).exists()
        assert (output_dir / "architecture.md").read_text(encoding="utf-8").startswith(
            "# Architecture\n"
        )
        assert "Code Mower treats it as planning input" in (
            output_dir / "quality-bar.md"
        ).read_text(encoding="utf-8")

        second_report = work_orders.create_project_context(
            output_dir=output_dir,
            project_name="Demo Repo",
        )
        assert second_report["created"] == []
        assert second_report["skipped"]


def test_external_context_manifest_is_metadata_only_by_default() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "product-context.md"
        source.write_text("# Product context\n\nKeep this local.\n", encoding="utf-8")
        output_dir = Path(tmp) / "external"

        manifest = work_orders.add_external_context([source], output_dir=output_dir)

        assert manifest["schema"] == work_orders.EXTERNAL_CONTEXT_SCHEMA
        assert manifest["entry_count"] == 1
        entry = manifest["entries"][0]
        assert entry["filename"] == "product-context.md"
        assert entry["text_preview_included"] is False
        assert "text_preview_path" not in entry
        assert Path(manifest["manifest_path"]).exists()


def test_external_context_can_write_bounded_preview_when_requested() -> None:
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
        assert entry["text_preview_included"] is True
        preview_path = Path(entry["text_preview_path"])
        assert preview_path.read_text(encoding="utf-8") == "first\n"


def test_external_context_rejects_negative_preview_limit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "requirements.txt"
        source.write_text("private context\n", encoding="utf-8")

        try:
            work_orders.add_external_context(
                [source],
                output_dir=Path(tmp) / "external",
                include_preview=True,
                max_preview_chars=-1,
            )
        except ValueError as exc:
            assert "max-preview-chars" in str(exc)
        else:  # pragma: no cover - defensive assertion path.
            raise AssertionError("negative preview limit should fail")


def test_issue_plan_work_order_critique_and_builder_seed_round_trip() -> None:
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
        assert "## Role/Lens Passes" in work_order_path.read_text(encoding="utf-8")

        critique = work_orders.create_critique_plan(
            work_order_path,
            reviewers=["codex", "claude"],
            output_dir=root / "critique",
        )
        assert len(critique["prompts"]) == 2
        assert Path(critique["manifest_path"]).exists()

        seed = work_orders.seed_builder_experiment(
            work_order_path,
            repo="owner/repo",
            builders=["codex-desktop", "claude-code"],
            output=root / "builder-experiment.json",
            task_class="planning",
            context_packs=["project-context"],
            prompt_lenses=["generic-programming"],
        )
        assert seed["builder_count"] == 2
        spec = json.loads(Path(seed["output_path"]).read_text(encoding="utf-8"))
        normalized = builder_experiment.normalize_spec(spec)
        assert normalized["tasks"][0]["repo"] == "owner/repo"
        assert normalized["builders"][0]["builder_id"] == "codex-desktop"


def test_plan_from_issue_prints_markdown_when_stdout_is_selected() -> None:
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

    assert exit_code == 0
    text = out.getvalue()
    assert text.startswith("# Issue Plan: Planning workflow")
    assert "Add a planning workflow." in text


def test_plan_from_issue_reports_missing_body_file_as_cli_error() -> None:
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

    assert exit_code == 1
    assert "error: could not read" in err.getvalue()


def test_issue_plan_refuses_to_overwrite_without_force() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "issue-plan.md"
        output.write_text("locally edited\n", encoding="utf-8")
        plan = work_orders.build_issue_plan(
            title="Planning workflow",
            body="Add a planning workflow.",
        )

        try:
            work_orders.write_issue_plan(plan, output)
        except ValueError as exc:
            assert "pass --force" in str(exc)
        else:  # pragma: no cover - defensive assertion path.
            raise AssertionError("existing issue plan should require force")
        assert output.read_text(encoding="utf-8") == "locally edited\n"

        work_orders.write_issue_plan(plan, output, force=True)
        assert output.read_text(encoding="utf-8").startswith("# Issue Plan: Planning workflow")


def test_work_order_draft_rejects_json_output_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            work_orders.draft_work_order(
                title="Planning workflow",
                source_text="Add a planning workflow.",
                output=Path(tmp) / "work-order.json",
            )
        except ValueError as exc:
            assert "Markdown path" in str(exc)
        else:  # pragma: no cover - defensive assertion path.
            raise AssertionError("JSON work-order output should fail")


def test_work_order_draft_refuses_to_overwrite_without_force() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "work-order.md"
        manifest = Path(tmp) / "work-order.json"
        output.write_text("locally edited\n", encoding="utf-8")
        manifest.write_text("{\"edited\": true}\n", encoding="utf-8")

        try:
            work_orders.draft_work_order(
                title="Planning workflow",
                source_text="Add a planning workflow.",
                output=output,
            )
        except ValueError as exc:
            assert "pass --force" in str(exc)
        else:  # pragma: no cover - defensive assertion path.
            raise AssertionError("existing work order should require force")

        assert output.read_text(encoding="utf-8") == "locally edited\n"
        assert manifest.read_text(encoding="utf-8") == "{\"edited\": true}\n"

        work_orders.draft_work_order(
            title="Planning workflow",
            source_text="Add a planning workflow.",
            output=output,
            force=True,
        )
        assert output.read_text(encoding="utf-8").startswith("# Work Order: Planning workflow")
