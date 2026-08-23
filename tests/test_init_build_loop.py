from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from code_mower import config as code_mower_config
from code_mower import init as code_mower_init


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "src/code_mower/templates/code-mower.example.yml"


def _builders_plan(config: dict | None = None):
    cfg = config or code_mower_config.load_config(CONFIG_PATH)
    return code_mower_init.render_init_plan(
        cfg,
        package_mode=True,
        package_command="code-mower",
        builders=code_mower_init._parse_builder_lanes("codex,claude,cursor"),
    )


class InitBuildLoopTests(unittest.TestCase):
    def test_builders_alias_cursor_to_existing_hosted_builder_identity(self) -> None:
        plan = _builders_plan()

        self.assertEqual(plan.data["builder_loop"]["builders"], ["codex", "claude", "grok-bot"])
        self.assertIn("builder:codex", plan.data["labels"])
        self.assertIn("builder:claude", plan.data["labels"])
        self.assertIn("builder:grok-bot", plan.data["labels"])
        self.assertIn("dispatched:codex", plan.data["labels"])
        self.assertIn("dispatched:grok-bot", plan.data["labels"])
        self.assertIn("tier:R", plan.data["labels"])
        self.assertIn("DISPATCH_TOKEN", plan.data["required_secrets"])
        self.assertIn("DISPATCH_TOKEN_EXPIRES_AT", plan.data["required_variables"])
        self.assertIn("LANE_MAC_RUNNER_ENABLED", plan.data["required_variables"])

        lanes = {entry["lane"]: entry for entry in plan.data["builder_loop"]["lanes"]}
        self.assertEqual(lanes["codex"]["audit_labels_display"], "needs-claude-audit")
        self.assertEqual(lanes["claude"]["audit_labels_display"], "needs-codex-audit")
        self.assertEqual(lanes["grok-bot"]["mention"], "@cursor")
        self.assertEqual(lanes["grok-bot"]["doc_target"], "docs/lanes/grok.md")
        self.assertIn("Builder loop:", plan.text)

    def test_init_apply_renders_build_loop_workflows_script_and_lane_docs(self) -> None:
        plan = _builders_plan()
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "generated"
            code_mower_init.apply_init_plan(plan, output_dir)

            expected = (
                ".github/workflows/dispatch-lanes.yml",
                ".github/workflows/lane-mac-runner.yml",
                "tools/lanes/run_mac_lane.sh",
                "docs/lanes/README.md",
                "docs/lanes/codex.md",
                "docs/lanes/claude.md",
                "docs/lanes/grok.md",
            )
            for rel_path in expected:
                path = output_dir / rel_path
                self.assertTrue(path.is_file(), rel_path)
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("__BUILD_LOOP", text)
                self.assertNotIn("__LANE_MAC", text)
                self.assertNotIn("{% raw %}", text)

            for rel_path in (
                ".github/workflows/dispatch-lanes.yml",
                ".github/workflows/lane-mac-runner.yml",
            ):
                with (output_dir / rel_path).open(encoding="utf-8") as handle:
                    yaml.safe_load(handle)

            runner = output_dir / "tools/lanes/run_mac_lane.sh"
            self.assertTrue(runner.stat().st_mode & 0o111)
            runner_text = runner.read_text(encoding="utf-8")
            self.assertIn("case \"$LANE\" in codex|claude)", runner_text)
            self.assertIn('repo_owner="${REPO%%/*}"', runner_text)

    def test_build_loop_owner_surface_parameters_render_into_templates(self) -> None:
        cfg = copy.deepcopy(code_mower_config.load_config(CONFIG_PATH))
        cfg["owner_surface"]["owner_login"] = "jeffhuber"
        cfg["owner_surface"]["needs_owner_label"] = "needs-jeff"
        cfg["owner_surface"]["builder_wip_cap"] = "2"
        cfg["owner_surface"]["lane_runner_labels"] = [
            "self-hosted",
            "macOS",
            "bridge-pro-lane",
        ]
        cfg["owner_surface"]["lane_runner_enabled_var"] = "BRIDGE_PRO_LANE_ENABLED"
        plan = _builders_plan(cfg)

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "generated"
            code_mower_init.apply_init_plan(plan, output_dir)
            dispatch = output_dir.joinpath(
                ".github/workflows/dispatch-lanes.yml"
            ).read_text(encoding="utf-8")
            mac_runner = output_dir.joinpath(
                ".github/workflows/lane-mac-runner.yml"
            ).read_text(encoding="utf-8")
            readme = output_dir.joinpath("docs/lanes/README.md").read_text(
                encoding="utf-8"
            )
            runner = output_dir.joinpath("tools/lanes/run_mac_lane.sh").read_text(
                encoding="utf-8"
            )

        self.assertIn('CODE_MOWER_MAX_WIP: ${{ github.event.inputs.max_wip || vars.CODE_MOWER_MAX_WIP || \'2\' }}', dispatch)
        self.assertIn('GH_TOKEN: ${{ secrets.DISPATCH_TOKEN || \'\' }}', dispatch)
        self.assertIn("DISPATCH_TOKEN secret is missing or empty", dispatch)
        self.assertIn('default_wip = int("2")', dispatch)
        self.assertNotIn("github.token", dispatch)
        self.assertIn('"needs-jeff"', dispatch)
        self.assertIn('runs-on: ["self-hosted", "macOS", "bridge-pro-lane"]', mac_runner)
        self.assertIn("vars.BRIDGE_PRO_LANE_ENABLED == 'true'", mac_runner)
        self.assertIn("`needs-jeff`", readme)
        self.assertIn("default `2`", readme)
        self.assertIn(
            "LANE_TRUSTED_AUTHORS:-jeffhuber,github-actions[bot]",
            runner,
        )

    def test_build_loop_rejects_invalid_builder_wip_cap(self) -> None:
        for value in ("unlimited", "5.0", "0"):
            with self.subTest(value=value):
                cfg = copy.deepcopy(code_mower_config.load_config(CONFIG_PATH))
                cfg["owner_surface"]["builder_wip_cap"] = value

                with self.assertRaisesRegex(
                    code_mower_config.ConfigError,
                    "owner_surface.builder_wip_cap",
                ):
                    _builders_plan(cfg)


if __name__ == "__main__":
    unittest.main()
