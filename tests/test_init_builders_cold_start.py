from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path


from code_mower import cli
from code_mower import init as code_mower_init


ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "src/code_mower/templates/code-mower.example.yml"
DOC_SOURCES = (
    ROOT / "README.md",
    *sorted((ROOT / "docs").glob("*.md")),
    ROOT / "src/code_mower/package_content.py",
)
INIT_BUILDERS_COMMAND_RE = re.compile(r"`?(code-mower init --builders [^`\n\"]+?)`?(?=[`\"\n])")


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        rc = cli.main(argv)
    return rc, stdout.getvalue(), stderr.getvalue()


class InitBuildersColdStartTests(unittest.TestCase):
    def test_fresh_directory_renders_starter_preview_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, working_directory(Path(tmp)):
            rc, stdout, stderr = _run_cli(["init", "--builders", "codex,claude,cursor"])
            self.assertEqual(rc, 0, stderr)
            self.assertIn("Code Mower init dry-run", stdout)
            self.assertIn("Config source: packaged starter (code-mower.example.yml)", stdout)
            self.assertIn("builder:cursor", stdout)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_fresh_directory_accepts_equals_form_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, working_directory(Path(tmp)):
            rc, stdout, stderr = _run_cli(["init", "--builders=codex,claude", "--json"])
            self.assertEqual(rc, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["config_source"]["kind"], "packaged_starter")
            self.assertEqual(payload["builder_loop"]["builders"], ["codex", "claude"])
            self.assertEqual(payload["profile"]["lanes"], ["codex", "claude_audit"])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_fresh_directory_honors_selected_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, working_directory(Path(tmp)):
            rc, stdout, stderr = _run_cli(
                ["init", "--builders", "codex", "--profile", "public_oss", "--json"]
            )
            self.assertEqual(rc, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["profile"]["id"], "public_oss")
            self.assertEqual(payload["builder_loop"]["builders"], ["codex"])

    def test_fresh_directory_apply_writes_only_under_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, working_directory(Path(tmp)):
            rc, stdout, stderr = _run_cli(
                [
                    "init", "--builders", "codex,claude", "--apply",
                    "--output-dir", "generated", "--skip-actionlint", "--skip-github-labels",
                ]
            )
            self.assertEqual(rc, 0, stderr)
            self.assertIn("Code Mower init apply wrote", stdout)
            self.assertIn("Builder loop next steps:", stdout)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["generated"])
            self.assertTrue((Path(tmp) / "generated" / "code-mower.yml").is_file())

    def test_configured_repository_uses_tracked_config_not_starter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, working_directory(Path(tmp)):
            tracked = STARTER.read_text() + (
                "\n  team_default:\n"
                "    description: Tracked repository profile.\n"
                "    lanes:\n"
                "      - codex\n"
            )
            (Path(tmp) / "code-mower.yml").write_text(tracked)

            rc, stdout, stderr = _run_cli(["init", "--builders", "codex", "--json"])
            self.assertEqual(rc, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["config_source"]["kind"], "explicit_repository_config")
            self.assertEqual(payload["config_source"]["requested_path"], "code-mower.yml")

            rc, stdout, stderr = _run_cli(
                ["init", "--builders", "codex", "--profile", "team_default", "--json"]
            )
            self.assertEqual(rc, 0, stderr)
            self.assertEqual(json.loads(stdout)["profile"]["id"], "team_default")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["code-mower.yml"])

    def test_configured_repository_reports_unknown_profile_instead_of_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, working_directory(Path(tmp)):
            shutil.copy(STARTER, Path(tmp) / "code-mower.yml")
            rc, _, stderr = _run_cli(["init", "--builders", "codex", "--profile", "missing"])
            self.assertEqual(rc, 1)
            self.assertIn("unknown profile 'missing'", stderr)
            self.assertIn("Init loaded config 'code-mower.yml'", stderr)

    def test_invalid_lane_error_is_not_a_config_error(self) -> None:
        for raw in ("", " , ", "bad lane!"):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp, working_directory(Path(tmp)):
                rc, _, stderr = _run_cli(["init", "--builders", raw])
                self.assertEqual(rc, 1)
                self.assertIn("error:", stderr)
                self.assertNotIn("Init loaded config", stderr)
                self.assertNotIn("code-mower.yml", stderr)
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_explicit_missing_config_reports_missing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, working_directory(Path(tmp)):
            rc, _, stderr = _run_cli(["init", "missing.yml", "--builders", "codex"])
            self.assertEqual(rc, 1)
            self.assertIn("unable to read missing.yml", stderr)
            self.assertIn("Init loaded config 'missing.yml'", stderr)
            self.assertNotIn("builder lane", stderr)

    def test_help_documents_starter_fallback(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout), self.assertRaises(SystemExit):
            code_mower_init.main(["--help"])
        self.assertIn("packaged starter config", stdout.getvalue())

    def test_documented_builders_commands_preview_from_a_fresh_directory(self) -> None:
        commands: dict[str, Path] = {}
        for source in DOC_SOURCES:
            for match in INIT_BUILDERS_COMMAND_RE.finditer(source.read_text()):
                commands.setdefault(match.group(1).strip(), source)
        self.assertTrue(commands, "expected documented `code-mower init --builders` commands")

        for command, source in commands.items():
            argv = command.split()[1:]
            argv = [arg for arg in argv if arg != "--apply"]
            if "--dry-run" not in argv:
                argv.append("--dry-run")
            with self.subTest(command=command, source=source.relative_to(ROOT)):
                with tempfile.TemporaryDirectory() as tmp, working_directory(Path(tmp)):
                    rc, stdout, stderr = _run_cli(argv)
                    self.assertEqual(rc, 0, f"{command}\n{stderr}")
                    self.assertIn("Code Mower init dry-run", stdout)
                    self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
