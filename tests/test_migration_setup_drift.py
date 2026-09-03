from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from code_mower import init as code_mower_init
from code_mower import migration


class SetupDriftTests(TestCase):
    def test_setup_drift_renders_config_presence_for_target_repo(self) -> None:
        source_config = (
            Path(__file__).parents[1] / "src" / "code_mower" / "templates" / "code-mower.example.yml"
        )
        observed: dict[str, Path] = {}
        original_render = code_mower_init.render_init_plan

        def capture_render(*args: object, **kwargs: object) -> object:
            observed["repo_root"] = Path(str(kwargs.get("repo_root")))
            return original_render(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "target"
            repo_path.mkdir()
            (repo_path / "code-mower.yml").write_text(
                source_config.read_text(encoding="utf-8"), encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
            elsewhere = Path(tmp) / "elsewhere"
            elsewhere.mkdir()
            with patch.object(code_mower_init, "render_init_plan", side_effect=capture_render):
                with patch("pathlib.Path.cwd", return_value=elsewhere):
                    migration.render_setup_drift_report(repo_path=repo_path)

        self.assertEqual(observed["repo_root"], repo_path.resolve())

    def test_setup_drift_keeps_explicit_starter_named_config_explicit(self) -> None:
        source_config = (
            Path(__file__).parents[1] / "src" / "code_mower" / "templates" / "code-mower.example.yml"
        )
        observed: dict[str, object] = {}
        original_render = code_mower_init.render_init_plan

        def capture_render(*args: object, **kwargs: object) -> object:
            observed["source_kind"] = kwargs.get("source_kind")
            return original_render(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            explicit_config = repo_path / "code-mower.example.yml"
            explicit_config.write_text(source_config.read_text(encoding="utf-8"), encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
            with patch.object(code_mower_init, "render_init_plan", side_effect=capture_render):
                migration.render_setup_drift_report(
                    repo_path=repo_path,
                    config=str(explicit_config),
                )

        self.assertEqual(observed["source_kind"], "explicit_repository_config")

    def test_classify_setup_drift_covers_expected_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            (repo_path / ".github" / "workflows").mkdir(parents=True)
            (repo_path / "same.txt").write_text("same\n", encoding="utf-8")
            (repo_path / "differs.txt").write_text("old\n", encoding="utf-8")
            (repo_path / ".github" / "workflows" / "old-code-mower.yml").write_text(
                "legacy\n",
                encoding="utf-8",
            )

            files = migration._classify_setup_drift(
                repo_path=repo_path,
                generated_files={
                    ".github/workflows/new-code-mower.yml": "new\n",
                    "differs.txt": "new\n",
                    "missing.yml": None,
                    "same.txt": "same\n",
                },
                tracked_files={
                    ".github/workflows/old-code-mower.yml",
                    "differs.txt",
                    "same.txt",
                },
            )

        states = {item["path"]: item["classification"] for item in files}
        self.assertEqual(states["same.txt"], "same")
        self.assertEqual(states["differs.txt"], "differs")
        self.assertEqual(states[".github/workflows/new-code-mower.yml"], "new")
        self.assertEqual(states[".github/workflows/old-code-mower.yml"], "repo-only")
        self.assertEqual(states["missing.yml"], "missing-from-output")

    def test_setup_drift_report_records_explicit_config_source(self) -> None:
        source_config = (
            Path(__file__).parents[1] / "src" / "code_mower" / "templates" / "code-mower.example.yml"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "target"
            repo_path.mkdir()
            (repo_path / "code-mower.yml").write_text(
                source_config.read_text(encoding="utf-8"), encoding="utf-8"
            )
            subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
            payload = migration.render_setup_drift_report(repo_path=repo_path)

        self.assertEqual(payload["config_source"], "explicit_repository_config")
        text = migration.render_setup_drift_text(payload)
        self.assertIn("Config source: explicit repository config (", text)
        self.assertIn("code-mower.yml", text)

    def test_setup_drift_report_records_packaged_starter_config_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "target"
            repo_path.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)
            payload = migration.render_setup_drift_report(repo_path=repo_path)

        self.assertEqual(payload["config_source"], "packaged_starter")
        text = migration.render_setup_drift_text(payload)
        self.assertIn("Config source: packaged starter (code-mower.example.yml)", text)

    def test_setup_drift_text_omits_config_source_when_unclassified(self) -> None:
        payload = {
            "status": "pass",
            "repo_path": "/tmp/repo",
            "profile": "recommended",
            "counts": {
                "same": 0,
                "differs": 0,
                "new": 0,
                "repo-only": 0,
                "missing-from-output": 0,
            },
            "next_action": "generated setup matches tracked Code Mower files",
            "files": [],
        }

        self.assertNotIn("Config source:", migration.render_setup_drift_text(payload))

    def test_render_setup_drift_text_omits_file_contents(self) -> None:
        payload = {
            "status": "warn",
            "repo_path": "/tmp/repo",
            "profile": "recommended",
            "counts": {
                "same": 0,
                "differs": 1,
                "new": 0,
                "repo-only": 0,
                "missing-from-output": 0,
            },
            "next_action": "review drift",
            "files": [
                {
                    "path": "code-mower.yml",
                    "classification": "differs",
                    "repo_bytes": 24,
                    "generated_bytes": 28,
                }
            ],
        }

        text = migration.render_setup_drift_text(payload)

        self.assertIn("DIFFERS code-mower.yml", text)
        self.assertIn("repo=24b", text)
        self.assertNotIn("secret", text)

    def test_git_tracked_files_reports_empty_git_repo_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo_path, check=True)

            tracked, available = migration._git_tracked_files(repo_path)

        self.assertTrue(available)
        self.assertEqual(tracked, set())

    def test_repo_path_hint_warns_when_everything_looks_new(self) -> None:
        hint = migration._setup_drift_repo_path_hint(
            tracked_available=True,
            counts={
                "same": 0,
                "differs": 0,
                "new": 7,
                "repo-only": 0,
                "missing-from-output": 0,
            },
        )

        self.assertEqual(hint["status"], "warn")
        self.assertEqual(hint["reason"], "repo_path_may_be_incomplete")
        self.assertIn("full repository checkout", hint["next_action"])

    def test_repo_path_hint_warns_when_git_tracking_is_unavailable(self) -> None:
        hint = migration._setup_drift_repo_path_hint(
            tracked_available=False,
            counts={
                "same": 0,
                "differs": 0,
                "new": 0,
                "repo-only": 0,
                "missing-from-output": 0,
            },
        )

        self.assertEqual(hint["status"], "warn")
        self.assertEqual(hint["reason"], "tracked_source_unavailable")
        self.assertIn("real git checkout", hint["next_action"])

    def test_builder_hint_distinguishes_reviewer_only_repos(self) -> None:
        hint = migration._setup_drift_builder_hint(
            files=[
                {
                    "path": ".github/workflows/codex-audit-labeler.yml",
                    "classification": "same",
                }
            ],
            builders_supplied=False,
        )

        self.assertEqual(hint["status"], "skip")
        self.assertEqual(hint["reason"], "reviewer_lanes_without_builder_dispatch")
        self.assertIn("no builder-dispatch files", hint["next_action"])

    def test_setup_drift_text_prints_repo_path_and_reviewer_hints(self) -> None:
        payload = {
            "status": "warn",
            "repo_path": "/tmp/repo",
            "profile": "recommended",
            "counts": {
                "same": 0,
                "differs": 0,
                "new": 7,
                "repo-only": 0,
                "missing-from-output": 0,
            },
            "next_action": "rerun against the full repository checkout",
            "repo_path_hint": {
                "status": "warn",
                "reason": "repo_path_may_be_incomplete",
                "next_action": "rerun against the full repository checkout",
            },
            "builder_hint": {
                "status": "skip",
                "reason": "reviewer_lanes_without_builder_dispatch",
                "paths": [".github/workflows/codex-audit-labeler.yml"],
                "next_action": "pass --builders only if this repo uses builder lanes",
            },
            "files": [],
        }

        text = migration.render_setup_drift_text(payload)

        self.assertIn("Repo path hint: WARN repo_path_may_be_incomplete", text)
        self.assertIn("Builder hint: SKIP reviewer_lanes_without_builder_dispatch", text)
        self.assertIn(".github/workflows/codex-audit-labeler.yml", text)
