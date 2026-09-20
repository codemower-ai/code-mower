from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest

from code_mower import config as code_mower_config
from code_mower import init as code_mower_init


ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "src" / "code_mower" / "templates" / "code-mower.example.yml"


class SafeEasyInitTests(unittest.TestCase):
    def _run_in(self, cwd: Path, argv: list[str]) -> tuple[int, str, str]:
        previous = Path.cwd()
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            os.chdir(cwd)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = code_mower_init.main(argv)
        finally:
            os.chdir(previous)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_unconfigured_easy_preview_uses_packaged_starter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, stdout, stderr = self._run_in(Path(tmp), ["--easy", "--json"])

        self.assertEqual(result, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["config_source"]["kind"], "packaged_starter")
        self.assertFalse(payload["config_source"]["root_config_present"])

    def test_configured_easy_preview_uses_root_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            repository_text = STARTER.read_text(encoding="utf-8").replace(
                "owner/example", "acme/configured"
            )
            (repo / "code-mower.yml").write_text(repository_text, encoding="utf-8")

            result, stdout, stderr = self._run_in(repo, ["--easy", "--json"])

        self.assertEqual(result, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["config_source"]["kind"], "explicit_repository_config")
        self.assertEqual(payload["config_source"]["requested_path"], "code-mower.yml")
        self.assertTrue(payload["config_source"]["root_config_present"])
        self.assertEqual(payload["repositories"][0]["slug"], "acme/configured")

    def test_configured_easy_apply_copies_root_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            repository_text = STARTER.read_text(encoding="utf-8").replace(
                "owner/example", "acme/configured"
            )
            (repo / "code-mower.yml").write_text(repository_text, encoding="utf-8")
            output_dir = repo / "generated"

            result, _stdout, stderr = self._run_in(
                repo,
                [
                    "--easy",
                    "--apply",
                    "--output-dir",
                    str(output_dir),
                    "--skip-actionlint",
                    "--skip-github-labels",
                ],
            )

            generated_text = (output_dir / "code-mower.yml").read_text(encoding="utf-8")
            manifest = json.loads(
                (output_dir / code_mower_init.APPLY_MANIFEST_FILENAME).read_text(encoding="utf-8")
            )

        self.assertEqual(result, 0, stderr)
        self.assertEqual(generated_text, repository_text)
        self.assertEqual(manifest["config_source"]["kind"], "explicit_repository_config")

    def test_explicit_packaged_starter_preview_has_exact_upgrade_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "code-mower.yml").write_text(
                STARTER.read_text(encoding="utf-8"), encoding="utf-8"
            )

            result, stdout, stderr = self._run_in(repo, ["--easy", "--packaged-starter", "--json"])

        self.assertEqual(result, 0, stderr)
        hint = json.loads(stdout)["setup_drift_hint"]
        self.assertIn("`code-mower init code-mower.yml --profile recommended --dry-run`", hint)
        self.assertIn(
            "`code-mower init code-mower.yml --profile recommended --apply "
            "--output-dir .code-mower.generated`",
            hint,
        )
        self.assertIn("`code-mower migration setup-drift --repo-path .`", hint)

    def test_explicit_packaged_starter_apply_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            repository_text = STARTER.read_text(encoding="utf-8").replace(
                "owner/example", "acme/configured"
            )
            (repo / "code-mower.yml").write_text(repository_text, encoding="utf-8")
            output_dir = repo / "generated"

            result, _stdout, stderr = self._run_in(
                repo,
                [
                    "--easy",
                    "--packaged-starter",
                    "--apply",
                    "--output-dir",
                    str(output_dir),
                    "--skip-actionlint",
                    "--skip-github-labels",
                ],
            )
            manifest = json.loads(
                (output_dir / code_mower_init.APPLY_MANIFEST_FILENAME).read_text(encoding="utf-8")
            )

        self.assertEqual(result, 0, stderr)
        self.assertEqual(manifest["config_source"]["kind"], "packaged_starter")

    def test_implicit_packaged_apply_beside_root_config_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "code-mower.yml").write_text(
                STARTER.read_text(encoding="utf-8"), encoding="utf-8"
            )

            result, stdout, stderr = self._run_in(
                repo,
                [
                    "--apply",
                    "--output-dir",
                    "generated output",
                    "--skip-actionlint",
                    "--skip-github-labels",
                ],
            )

        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("refusing to apply the packaged starter", stderr)
        self.assertIn(
            "`code-mower init code-mower.yml --profile recommended --apply "
            "--output-dir 'generated output'`",
            stderr,
        )
        self.assertIn(
            "`code-mower init --packaged-starter --profile recommended --apply "
            "--output-dir 'generated output'`",
            stderr,
        )


class GeneratedPathUniquenessTests(unittest.TestCase):
    def test_recommended_plan_has_unique_real_lane_configs(self) -> None:
        config = code_mower_config.load_config(STARTER)
        plan = code_mower_init.render_init_plan(
            config,
            config_path=str(STARTER),
            package_mode=True,
            source_kind="packaged_starter",
        )

        paths = [entry["path"] for entry in plan.data["generated_files"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertFalse(
            any(
                "lane-config" in warning and "collides" in warning
                for warning in plan.data["warnings"]
            )
        )
        for lane in ("claude", "codex"):
            entry = next(
                item
                for item in plan.data["generated_files"]
                if item["path"] == f"tools/lane_configs/{lane}.py"
            )
            self.assertEqual(entry["source"], "lane-config-template")
            self.assertEqual(entry["package_copy_from"], f"lane_configs/{lane}.py")

        with tempfile.TemporaryDirectory() as tmp:
            result = code_mower_init.apply_init_plan(plan, Path(tmp) / "generated")

        lane_placeholders = {
            path for path in result["placeholder_files"] if "tools/lane_configs/" in path
        }
        self.assertEqual(lane_placeholders, set())


if __name__ == "__main__":
    unittest.main()
