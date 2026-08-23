from __future__ import annotations

import copy
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            self.assertIn("[omitted: issue title author is not trusted]", runner_text)
            self.assertIn("[omitted: PR title author is not trusted]", runner_text)
            self.assertIn("has_open_pr_for_issue()", runner_text)
            self.assertIn("closingIssuesReferences", runner_text)

    def test_mac_lane_runner_does_not_treat_issue_prefix_pr_as_open_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"
            work = work_root / "codex" / "repo"
            work.joinpath(".git").mkdir(parents=True)

            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
cmd="${1:-} ${2:-}"
args=" $* "
if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:codex"* ]]; then
  printf '\\n'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[{"number":12,"title":"Issue 12","labels":[{"name":"tier:R"},{"name":"builder:codex"},{"name":"dispatched:codex"}],"assignees":[],"author":{"login":"owner"}}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  if [[ "$args" == *"--json number"* ]]; then
    printf '1\\n'
  else
    printf '%s\\n' '[{"number":99,"body":"Closes #123","closingIssuesReferences":[{"number":123}]}]'
  fi
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ]; then
  if [[ "$args" == *"--json comments"* ]]; then
    printf '%s\\n' '{"comments":[]}'
  else
    printf '%s\\n' '{"title":"Issue 12","body":"Body","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"owner"}}'
  fi
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)

            fake_git = bin_dir / "git"
            fake_git.write_text(
                "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)

            fake_codex = bin_dir / "codex"
            fake_codex.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
cat >/dev/null
printf 'fake codex completed\\n'
""",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)

            completed = subprocess.run(
                [
                    str(ROOT / "tools/lanes/run_mac_lane.sh"),
                    "--lane",
                    "codex",
                    "--repo",
                    "owner/repo",
                    "--max-minutes",
                    "1",
                ],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "LANE_CODEX_EXTRA_FLAGS": "--fake-extra",
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                },
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertIn("codex: selected build issue #12", completed.stdout)
        self.assertIn("fake codex completed", completed.stdout)

    def test_build_loop_owner_surface_parameters_render_into_templates(self) -> None:
        cfg = copy.deepcopy(code_mower_config.load_config(CONFIG_PATH))
        cfg["owner_surface"]["owner_login"] = "jeffhuber"
        cfg["owner_surface"]["needs_owner_label"] = "needs-jeff"
        cfg["owner_surface"]["owner_decision_label"] = "decision-jeff"
        cfg["owner_surface"]["owner_sitting_label"] = "sitting-jeff"
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
        self.assertIn('"needs-jeff","decision-jeff","sitting-jeff"', dispatch)
        self.assertIn('runs-on: ["self-hosted", "macOS", "bridge-pro-lane"]', mac_runner)
        self.assertIn("vars.BRIDGE_PRO_LANE_ENABLED == 'true'", mac_runner)
        self.assertIn("`needs-jeff`", readme)
        self.assertIn("`decision-jeff`", readme)
        self.assertIn("`sitting-jeff`", readme)
        self.assertIn("default `2`", readme)
        self.assertIn(
            "LANE_TRUSTED_AUTHORS:-jeffhuber,github-actions[bot]",
            runner,
        )
        self.assertIn(
            """owner_labels_json='["needs-jeff","decision-jeff","sitting-jeff"]'""",
            runner,
        )
        self.assertIn("def owner_blocking_label($name)", runner)
        self.assertIn("owner_blocking_label(.name)|not", runner)

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

    def test_label_target_prefers_output_checkout_remote(self) -> None:
        cfg = code_mower_config.load_config(CONFIG_PATH)

        with mock.patch.object(
            code_mower_init,
            "_detect_github_repo_slug",
            return_value="target/repo",
        ):
            target = code_mower_init._target_repo_slug_for_labels(
                cfg,
                Path("/tmp/generated"),
            )

        self.assertEqual(target, "target/repo")

    def test_label_target_remote_parser_accepts_github_remotes(self) -> None:
        self.assertEqual(
            code_mower_init._github_repo_slug_from_remote(
                "git@github.com:target/repo.git\n",
            ),
            "target/repo",
        )
        self.assertEqual(
            code_mower_init._github_repo_slug_from_remote(
                "https://github.com/target/repo.git",
            ),
            "target/repo",
        )
        self.assertEqual(
            code_mower_init._github_repo_slug_from_remote("ssh://example.com/repo.git"),
            "",
        )

    def test_label_target_falls_back_to_primary_config_repo(self) -> None:
        cfg = code_mower_config.load_config(CONFIG_PATH)

        with mock.patch.object(
            code_mower_init,
            "_detect_github_repo_slug",
            return_value="",
        ):
            target = code_mower_init._target_repo_slug_for_labels(
                cfg,
                Path("/tmp/generated"),
            )

        self.assertEqual(target, "owner/example")

    def test_init_apply_ensures_github_labels_in_target_repo(self) -> None:
        captured: dict[str, object] = {}

        def fake_ensure(labels, *, repo=None, gh_bin="gh", color="ededed"):
            captured["repo"] = repo
            captured["labels"] = sorted(set(labels))
            return {
                "status": "passed",
                "repo": repo or "",
                "requested": sorted(set(labels)),
                "created": [],
                "existing": sorted(set(labels)),
                "failed": [],
            }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            code_mower_init,
            "_detect_github_repo_slug",
            return_value="target/repo",
        ), mock.patch.object(
            code_mower_init,
            "ensure_github_labels",
            side_effect=fake_ensure,
        ):
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                result = code_mower_init.main(
                    [
                        str(CONFIG_PATH),
                        "--builders",
                        "codex",
                        "--apply",
                        "--output-dir",
                        str(Path(tmp) / "target"),
                        "--skip-actionlint",
                        "--json",
                    ]
                )

        self.assertEqual(result, 0)
        self.assertEqual(captured["repo"], "target/repo")
        self.assertIn("builder:codex", captured["labels"])
        self.assertIn("dispatched:codex", captured["labels"])
        self.assertIn('"repo": "target/repo"', stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
