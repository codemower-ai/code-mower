from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase

from code_mower import migration


class SetupDriftTests(TestCase):
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
