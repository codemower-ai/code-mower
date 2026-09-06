from __future__ import annotations

import copy
import io
import json
import shlex
import tempfile
import unittest
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from code_mower import cli, config, init, next_steps, package
from code_mower.provider_registry import REFERENCE_PROVIDERS


ROOT = Path(__file__).resolve().parents[1]
STARTER = ROOT / "src/code_mower/templates/code-mower.example.yml"
CATALOG = ROOT / "src/code_mower/templates/providers.yml"


class NextStepsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.templates = package.load_provider_templates(CATALOG)

    def test_initial_profiles_agree_across_starter_and_catalogs(self) -> None:
        for path in (STARTER, CATALOG, ROOT / "templates/providers.yml"):
            payload = yaml.safe_load(path.read_text())
            for profile in ("recommended", "public_oss"):
                with self.subTest(path=path, profile=profile):
                    self.assertEqual(payload["profiles"][profile]["lanes"], ["codex", "claude_audit"])
            self.assertIn("gitar", payload["profiles"]["saas_research"]["lanes"])
        self.assertFalse(REFERENCE_PROVIDERS["gitar"].enabled_by_default)

    def test_easy_init_does_not_generate_gitar_workflows_labels_or_secrets(self) -> None:
        plan = init.render_init_plan(config.load_config(STARTER), package_mode=True)
        self.assertEqual(plan.data["profile"]["lanes"], ["codex", "claude_audit"])
        self.assertEqual(plan.data["informational_lanes"], [])
        for key in ("labels", "workflows", "required_secrets", "required_variables"):
            self.assertNotIn("gitar", json.dumps(plan.data[key]).lower())

    def test_default_steps_only_request_selected_peers_and_show_local_status(self) -> None:
        plan = next_steps.build_next_steps(self.templates, repo="team/project", pr="42")
        self.assertEqual([step["id"] for step in plan["steps"]], [
            "render-easy-config", "write-reviewable-config", "doctor-easy",
            "first-audit", "lanes-status", "productivity-report",
        ])
        first_audit = next(step for step in plan["steps"] if step["id"] == "first-audit")
        self.assertEqual(shlex.split(first_audit["command"]), [
            "gh", "pr", "edit", "42", "--repo", "team/project",
            "--add-label", "needs-codex-audit", "--add-label", "needs-claude-audit",
        ])
        self.assertIn("labels alone", first_audit["why"])
        for step in plan["steps"]:
            for unrelated in ("antigravity", "gitar", "migration", "cloud", "calibration run"):
                self.assertNotIn(unrelated, step["command"])
        self.assertIn("--repo team/project --pr 42 --advanced", plan["advanced_command"])

    def test_advanced_default_keeps_maintenance_without_inventing_calibration_commands(self) -> None:
        plan = next_steps.build_next_steps(self.templates, advanced=True)
        steps = {step["id"]: step for step in plan["steps"]}
        self.assertIn("wrapper-rehearsal", steps)
        self.assertIn("package-install-rehearsal", steps)
        self.assertIn("cloud-upload-dry-run", steps)
        self.assertNotIn("calibration-run", steps)
        self.assertNotIn("--runs", steps["value-report"]["command"])
        self.assertIn("draft-calibration-corpus.json", steps["value-report"]["command"])
        self.assertNotIn("antigravity", json.dumps(plan))

    def test_calibration_never_adds_a_provider_outside_the_explicit_profile(self) -> None:
        templates = copy.deepcopy(self.templates)
        templates["profiles"]["one_research_peer"] = {"lanes": ["muse_cli"]}
        plan = next_steps.build_next_steps(templates, profile="one_research_peer", advanced=True)
        commands = [step["command"] for step in plan["steps"] if step["id"] == "calibration-run"]
        self.assertEqual(len(commands), 1)
        argv = shlex.split(commands[0])
        self.assertEqual(argv[argv.index("--lanes") + 1], "muse-cli")
        self.assertNotIn("antigravity", json.dumps(plan))

    def test_non_executable_profile_does_not_fall_back_to_other_calibration_providers(self) -> None:
        plan = next_steps.build_next_steps(self.templates, profile="saas_research", advanced=True)
        self.assertNotIn("calibration-run", [step["id"] for step in plan["steps"]])

    def test_cli_advanced_guidance_is_explicit_in_json_and_text(self) -> None:
        for advanced in (False, True):
            argv = ["--provider-templates", str(CATALOG), "--repo", "team/project", "--pr", "42"]
            if advanced:
                argv.append("--advanced")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(next_steps.main([*argv, "--json"]), 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["advanced"], advanced)
            text = next_steps.render_next_steps_text(plan)
            self.assertEqual("migration wrapper-rehearsal" in text, advanced)
            self.assertIn("needs-codex-audit", text)
            self.assertIn("needs-claude-audit", text)

    def test_cli_auto_detects_saved_repository_profile(self) -> None:
        saved = STARTER.read_text().replace(
            "      - codex\n      - claude_audit", "      - claude_audit", 1,
        )
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            Path("code-mower.yml").write_text(saved)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(next_steps.main(["--json"]), 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["lanes"], ["claude_audit"])
            self.assertIn("init code-mower.yml --profile recommended", plan["steps"][0]["command"])
            self.assertNotIn("needs-codex-audit", json.dumps(plan))

    def test_explicit_config_wins_over_current_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            Path("code-mower.yml").write_text("profiles: {}\nlanes: {}\n")
            path = Path("selected peers.yml")
            path.write_text(STARTER.read_text())
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(next_steps.main(["--config", str(path), "--json"]), 0)
            plan = json.loads(output.getvalue())
            self.assertEqual(plan["lanes"], ["codex", "claude_audit"])
            self.assertEqual(shlex.split(plan["steps"][0]["command"])[2], str(path))

    def test_invalid_saved_profile_is_reported_without_substituting_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, chdir(tmp):
            Path("code-mower.yml").write_text("lanes: {}\n")
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                self.assertEqual(next_steps.main(["--json"]), 1)
            self.assertIn("profiles must be a mapping", error.getvalue())
            self.assertEqual(output.getvalue(), "")

    def test_empty_profile_suggests_an_available_non_mutating_command(self) -> None:
        templates = copy.deepcopy(self.templates)
        templates["profiles"]["recommended"]["lanes"] = []
        plan = next_steps.build_next_steps(templates)
        step = plan["steps"][3]
        self.assertEqual(step["id"], "choose-reviewers")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(shlex.split(step["command"])[1:]), 0)
        self.assertNotIn("--interactive", step["command"])


if __name__ == "__main__":
    unittest.main()
