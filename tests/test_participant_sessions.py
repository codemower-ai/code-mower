from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from code_mower import cli, config, init, next_steps, participants, session


STARTER = Path(__file__).resolve().parents[1] / "src/code_mower/templates/code-mower.example.yml"


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class ParticipantTests(unittest.TestCase):
    def setUp(self):
        self.config = config.load_config(STARTER)

    def test_product_aliases_are_stable_and_do_not_merge_distinct_products(self):
        self.assertEqual(participants.parse_participants("Claude Code,codex,Devin CLI,claude"), ("claude", "codex", "devin"))
        self.assertEqual(participants.parse_participants("Grok Bot,Cursor,Cursor Bugbot"), ("grok-bot", "cursor", "cursor-bugbot"))
        for raw in ("", "claude,", "mystery-agent", "claude,,codex"):
            with self.subTest(raw=raw), self.assertRaises(config.ConfigError):
                participants.parse_participants(raw)

    def test_selection_preserves_policy_and_the_source_config(self):
        original = copy.deepcopy(self.config)
        result = participants.config_with_participants(self.config, ("claude", "codex", "devin"))
        self.assertEqual(self.config, original)
        self.assertEqual(result["profiles"]["recommended"]["lanes"], ["claude_audit", "codex", "devin_cli"])
        self.assertFalse(result["lanes"]["devin_cli"]["merge_authority"])
        result["lanes"]["devin_cli"]["merge_authority"] = True
        result["lanes"]["devin_cli"]["informational"] = False
        updated = participants.config_with_participants(result, ("claude", "codex", "devin"))
        self.assertTrue(updated["lanes"]["devin_cli"]["merge_authority"])
        self.assertEqual(config.validate_config(updated), [])

    def test_cursor_selection_does_not_enable_bugbot_or_a_review_gate(self):
        result = participants.config_with_participants(self.config, ("claude", "codex", "cursor", "grok-bot"))
        self.assertEqual(result["profiles"]["recommended"]["lanes"], ["claude_audit", "codex"])
        self.assertEqual(participants.configured_participants(result), ("claude", "codex", "cursor", "grok-bot"))

    def test_builder_only_selection_does_not_invent_a_reviewer(self):
        result = participants.config_with_participants(self.config, ("cursor", "grok-bot"))
        plan = next_steps.build_next_steps({"profiles": result["profiles"], "provider_templates": result["lanes"]})
        self.assertEqual(plan["lanes"], [])
        self.assertEqual(plan["steps"][3]["id"], "choose-reviewers")
        self.assertNotIn("needs-codex-audit", json.dumps(plan))

    def test_review_services_do_not_gain_build_or_orchestration_roles(self):
        plan = session.build_session(repo="team/project", host="codex", selected=("gitar",), config={})
        member = plan["participants"][0]
        self.assertIsNone(member["builder"])
        self.assertFalse(member["can_coordinate"])
        self.assertFalse(member["reviewer"]["merge_authority"])

    def test_generated_config_round_trips_selection_and_devin_workflow(self):
        plan = init.render_init_plan(
            self.config, config_path=str(STARTER), package_mode=True,
            participants=("claude", "codex", "devin"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "generated"
            init.apply_init_plan(plan, output)
            saved = config.load_config(output / "code-mower.yml")
            self.assertEqual(config.validate_config(saved), [])
            self.assertEqual(participants.configured_participants(saved), ("claude", "codex", "devin"))
            self.assertEqual(saved["profiles"]["recommended"]["lanes"], ["claude_audit", "codex", "devin_cli"])
            rerender = init.render_init_plan(saved, config_path=str(output / "code-mower.yml"), package_mode=True)
            self.assertEqual(rerender.data["labels"], plan.data["labels"])
            self.assertIn("devin_cli", (output / ".github/workflows/local-cli-audit.yml").read_text())
            self.assertNotIn("gitar", " ".join(plan.data["labels"]))

    def test_fresh_cli_accepts_with_without_a_config_positional_argument(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["init", "--with", "claude,codex,devin", "--json"]), 0)
            self.assertEqual(json.loads(output.getvalue())["profile"]["lanes"], ["claude_audit", "codex", "devin_cli"])
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_picker_toggles_devin_and_keeps_default_peers(self):
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", side_effect=["bad", "3", ""]), redirect_stderr(io.StringIO()):
            self.assertEqual(participants.pick_participants(participants.DEFAULT_PARTICIPANTS), ("claude", "codex", "devin"))

    def test_picker_can_cancel_and_refuses_noninteractive_input(self):
        with mock.patch("sys.stdin.isatty", return_value=False), self.assertRaisesRegex(config.ConfigError, "--with"):
            participants.pick_participants(participants.DEFAULT_PARTICIPANTS)
        for answer in ("q", EOFError(), KeyboardInterrupt()):
            with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", side_effect=[answer]), redirect_stderr(io.StringIO()), self.assertRaisesRegex(config.ConfigError, "cancelled"):
                participants.pick_participants(participants.DEFAULT_PARTICIPANTS)

    def test_next_steps_uses_saved_selections_and_configuration_path(self):
        plan = init.render_init_plan(self.config, config_path=str(STARTER), participants=("claude", "codex", "devin"))
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "generated"
            init.apply_init_plan(plan, output_dir)
            saved_path = str(output_dir / "code-mower.yml")
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(next_steps.main(["--config", saved_path, "--json"]), 0)
            steps = json.loads(output.getvalue())
            self.assertEqual(steps["lanes"], ["claude_audit", "codex", "devin_cli"])
            self.assertIn(saved_path, steps["steps"][0]["command"])
            self.assertIn("needs-devin-cli-audit", steps["steps"][3]["command"])
            self.assertIn(saved_path, steps["advanced_command"])
            self.assertNotIn("gitar", json.dumps(steps))


class SessionTests(unittest.TestCase):
    def test_each_agent_host_is_the_implicit_orchestrator(self):
        for host in ("claude", "codex", "devin", "cursor", "grok-bot", "antigravity"):
            with self.subTest(host=host):
                plan = session.build_session(repo="team/project", host=host, selected=("claude", "codex", "devin"), config={})
                self.assertEqual(plan["orchestrator"], host)
                self.assertEqual(plan["status"], "prepared")
                self.assertEqual([row["id"] for row in plan["participants"]], ["claude", "codex", "devin"])
                self.assertFalse(plan["participants"][2]["reviewer"]["merge_authority"])

    def test_explicit_orchestrator_is_a_handoff_and_review_services_cannot_coordinate(self):
        plan = session.build_session(repo="team/project", host="codex", selected=("claude", "codex"), config={}, orchestrator="claude")
        self.assertEqual(plan["status"], "handoff_required")
        self.assertEqual(plan["orchestrator"], "claude")
        with self.assertRaises(config.ConfigError):
            session.build_session(repo="team/project", host="gitar", selected=("claude", "codex"), config={})

    def test_missing_host_does_not_guess_or_write_a_session(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp), mock.patch.dict(os.environ, {}, clear=True), redirect_stderr(io.StringIO()):
            self.assertEqual(session.main(["start", "--repo", "team/project"]), 1)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_saved_selection_is_reused_without_launching_providers(self):
        source = config.load_config(STARTER)
        plan = init.render_init_plan(source, config_path=str(STARTER), participants=("claude", "codex", "devin"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            init.apply_init_plan(plan, root)
            with working_directory(root), mock.patch("subprocess.run", side_effect=AssertionError("must not launch a provider")), mock.patch.dict(os.environ, {"CODE_MOWER_HOST": "codex"}):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(cli.main(["session", "start", "--repo", "team/project", "--json"]), 0)
                payload = json.loads(output.getvalue())
                self.assertEqual([member["id"] for member in payload["participants"]], ["claude", "codex", "devin"])
                self.assertEqual(payload["orchestrator"], "codex")
                saved = Path(payload["session_file"])
                self.assertTrue(saved.is_file())
                shown = io.StringIO()
                with redirect_stdout(shown):
                    self.assertEqual(cli.main(["session", "show", str(saved), "--json"]), 0)
                self.assertEqual(json.loads(shown.getvalue()), payload)

    def test_dry_run_does_not_write_session_state(self):
        with tempfile.TemporaryDirectory() as tmp, working_directory(tmp), redirect_stdout(io.StringIO()):
            self.assertEqual(session.main(["start", "--repo", "team/project", "--host", "claude", "--with", "claude,codex,devin", "--dry-run"]), 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
