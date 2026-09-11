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

    def test_narrowing_selection_reports_removal_of_promoted_and_custom_reviewers(self):
        for lane_id in ("greptile", "custom_security"):
            with self.subTest(lane=lane_id):
                source = copy.deepcopy(self.config)
                source["lanes"][lane_id] = copy.deepcopy(source["lanes"]["greptile"])
                source["lanes"][lane_id]["merge_authority"] = True
                source["lanes"][lane_id]["informational"] = False
                if lane_id == "custom_security":
                    source["lanes"][lane_id]["labels"] = {
                        "needs": "needs-custom-security-audit", "done": "custom-security-audit-done",
                        "blocked": "custom-security-audit-blocked",
                    }
                source["profiles"]["recommended"]["lanes"].append(lane_id)
                plan = init.render_init_plan(source, participants=("claude", "codex", "devin"))
                self.assertEqual(plan.data["participant_selection"]["review_lanes_removed"], [lane_id])
                self.assertEqual(plan.data["participant_selection"]["merge_authority_lanes_removed"], [lane_id])
                self.assertIn(f"removes merge-authority reviewer {lane_id}", plan.text)
                self.assertIn("gate requirements will no longer include it", plan.text)
                self.assertNotIn(lane_id, plan.data["profile"]["lanes"])
                saved = next(item for item in plan.data["generated_files"] if item["path"] == "code-mower.yml")["config_data"]
                self.assertTrue(saved["lanes"][lane_id]["merge_authority"])
                self.assertIn(lane_id, source["profiles"]["recommended"]["lanes"])

    def test_picker_addition_keeps_an_existing_promoted_reviewer_selected(self):
        source = participants.config_with_participants(self.config, ("claude", "codex"))
        source["profiles"]["recommended"]["lanes"].append("greptile")
        source["lanes"]["greptile"]["merge_authority"] = True
        source["lanes"]["greptile"]["informational"] = False
        initial = participants.picker_initial_participants(source, profile="recommended")
        self.assertIn("greptile", initial)
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", side_effect=["3", ""]), redirect_stderr(io.StringIO()):
            selected = participants.pick_participants(initial)
        plan = init.render_init_plan(source, participants=selected)
        self.assertIn("devin_cli", plan.data["profile"]["lanes"])
        self.assertIn("greptile", plan.data["profile"]["lanes"])
        self.assertEqual(plan.data["participant_selection"]["review_lanes_removed"], [])

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
            (root / ".git").mkdir()
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


JIRA_TRACKER_CONFIG = {
    "tracker": {
        "kind": "jira_cloud",
        "jira_cloud": {
            "site_url": "https://example.atlassian.net",
            "cloud_id": "11111111-2222-3333-4444-555555555555",
            "project_id": "10001",
            "project_key": "ABC",
        },
    },
}


class JiraTrackerSessionTests(unittest.TestCase):
    def test_jira_tracker_section_is_absent_for_github_and_unconfigured_trackers(self):
        for tracker_config in ({}, {"tracker": {"kind": "github"}}):
            with self.subTest(tracker_config=tracker_config):
                plan = session.build_session(
                    repo="team/project", host="claude", selected=("claude", "codex"),
                    config=tracker_config,
                )
                self.assertNotIn("tracker", plan)

    def test_jira_tracker_section_names_the_project_without_body_text_or_credentials(self):
        plan = session.build_session(
            repo="team/project", host="claude", selected=("claude", "codex"),
            config=JIRA_TRACKER_CONFIG,
        )
        tracker = plan["tracker"]
        self.assertEqual(tracker["kind"], "jira_cloud")
        self.assertEqual(tracker["project"], "ABC")
        self.assertEqual(tracker["mutation_commands"], ["code-mower tracker mutate", "code-mower tracker pr-sync"])
        blob = json.dumps(plan)
        for forbidden in ("11111111-2222-3333-4444-555555555555", "example.atlassian.net", "JIRA_API_TOKEN"):
            self.assertNotIn(forbidden, blob)

    def test_jira_tracker_falls_back_to_project_id_when_no_key_is_configured(self):
        config_without_key = copy.deepcopy(JIRA_TRACKER_CONFIG)
        del config_without_key["tracker"]["jira_cloud"]["project_key"]
        plan = session.build_session(
            repo="team/project", host="codex", selected=("claude", "codex"),
            config=config_without_key,
        )
        self.assertEqual(plan["tracker"]["project"], "10001")

    def test_claude_and_codex_receive_materially_identical_jira_contract_instructions(self):
        claude_plan = session.build_session(
            repo="team/project", host="claude", selected=("claude", "codex"),
            config=JIRA_TRACKER_CONFIG,
        )
        codex_plan = session.build_session(
            repo="team/project", host="codex", selected=("claude", "codex"),
            config=JIRA_TRACKER_CONFIG,
        )
        self.assertEqual(claude_plan["tracker"], codex_plan["tracker"])
        instructions = claude_plan["tracker"]["instructions"]
        joined = " ".join(instructions)
        self.assertIn("authoritative for queue reads and all", joined)
        self.assertIn("Rovo MCP", joined)
        self.assertIn("tracker mutate", joined)
        self.assertIn("tracker pr-sync", joined)
        self.assertIn("implementation starts", joined)
        self.assertIn("ready_for_review", joined)

    def test_cursor_receives_identical_jira_contract_as_codex_and_claude(self):
        cursor_plan = session.build_session(
            repo="team/project", host="cursor", selected=("claude", "codex", "cursor"),
            config=JIRA_TRACKER_CONFIG,
        )
        claude_plan = session.build_session(
            repo="team/project", host="claude", selected=("claude", "codex", "cursor"),
            config=JIRA_TRACKER_CONFIG,
        )
        codex_plan = session.build_session(
            repo="team/project", host="codex", selected=("claude", "codex", "cursor"),
            config=JIRA_TRACKER_CONFIG,
        )
        self.assertEqual(cursor_plan["tracker"], claude_plan["tracker"])
        self.assertEqual(cursor_plan["tracker"], codex_plan["tracker"])
        instructions = cursor_plan["tracker"]["instructions"]
        joined = " ".join(instructions)
        self.assertIn("authoritative for queue reads and all", joined)
        self.assertIn("Rovo MCP", joined)
        self.assertIn("tracker mutate", joined)
        self.assertIn("tracker pr-sync", joined)

    def test_rendered_brief_includes_the_jira_contract_and_project(self):
        plan = session.build_session(
            repo="team/project", host="claude", selected=("claude", "codex"),
            config=JIRA_TRACKER_CONFIG,
        )
        text = session.render_session(plan)
        self.assertIn("Tracker: jira_cloud (project ABC)", text)
        self.assertIn("code-mower tracker mutate", text)

        github_plan = session.build_session(
            repo="team/project", host="claude", selected=("claude", "codex"),
            config={},
        )
        self.assertNotIn("Tracker:", session.render_session(github_plan))


if __name__ == "__main__":
    unittest.main()
