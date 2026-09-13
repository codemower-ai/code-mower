"""Repository branch-name policy: resolution, validation, and its two seams.

A target repository's accepted branch names and a builder's provenance are
distinct contracts. These tests pin the resolver, its configuration surface,
the hosted Devin work-order seam, and the generated local runner seam, plus
the provider-prefix convention that applies when no policy is configured.
"""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from code_mower import branch_policy  # noqa: E402
from code_mower import config as code_mower_config  # noqa: E402
from code_mower import init as code_mower_init  # noqa: E402
from code_mower.audit_labeler_lib import builder_identity_matches  # noqa: E402
from code_mower.devin_work_orders import DevinWorkOrders, WorkOrder  # noqa: E402
from code_mower.remote_session import RemoteError  # noqa: E402
from code_mower.work_orders import WORK_ORDER_SCHEMA  # noqa: E402
from test_init_build_loop import (  # noqa: E402
    _FAKE_GH_DELIVERY_HEADER,
    _LANE_DELIVERY_ENV,
    _builders_plan,
)

CONFIG_PATH = ROOT / "src/code_mower/templates/code-mower.example.yml"
JIRA_TEMPLATE = "fix/{issue_key}-{slug}"
MANIFEST = {"schema": WORK_ORDER_SCHEMA, "repo": "owner/repo",
            "source": {"repo": "owner/repo", "issue_number": "907"},
            "output_path": "x", "context_manifest": "x"}
ORDER_ARGS = dict(repository="owner/repo", issue=907, base="main",
                  author_id=123, author_login="builder[bot]", acu_limit=5)


def _config_with_policy(template: str | None = JIRA_TEMPLATE) -> dict:
    cfg = copy.deepcopy(code_mower_config.load_config(CONFIG_PATH))
    repo = cfg["repositories"][0]
    repo["slug"] = "owner/repo"
    if template is not None:
        repo["delivery_policy"] = {"branch_template": template}
    return cfg


class ResolverTests(unittest.TestCase):
    def test_jira_bug_resolves_to_a_conforming_branch_on_the_first_attempt(self) -> None:
        policy = branch_policy.compile_template(JIRA_TEMPLATE)
        branch = branch_policy.resolve_branch(
            policy, lane="muse", issue_key="MB-9506", slug="NV: accessible label!")
        self.assertEqual(branch, "fix/MB-9506-nv-accessible-label")
        self.assertEqual(policy.example, "fix/ABC-123-short-description")
        self.assertIsNotNone(re.fullmatch(policy.pattern, branch))
        self.assertIsNotNone(re.fullmatch(policy.pattern, policy.example))

    def test_default_policy_is_the_provider_prefix_convention(self) -> None:
        policy = branch_policy.policy_for_repository(_config_with_policy(None), "owner/repo")
        self.assertFalse(policy.configured)
        self.assertEqual(
            branch_policy.resolve_branch(policy, lane="codex", issue_number=907, slug="Fix It"),
            "codex/907-fix-it")
        branch_policy.validate_branch(policy, "devin/907")
        self.assertEqual(branch_policy.configured_policies(_config_with_policy(None)), {})

    def test_configured_policy_is_found_case_insensitively(self) -> None:
        policy = branch_policy.policy_for_repository(_config_with_policy(), "Owner/Repo")
        self.assertTrue(policy.configured)
        self.assertEqual(policy.template, JIRA_TEMPLATE)
        self.assertEqual(
            branch_policy.configured_policies(_config_with_policy()),
            {"owner/repo": policy.describe()})

    def test_issue_key_falls_back_to_the_issue_number_deterministically(self) -> None:
        policy = branch_policy.compile_template(JIRA_TEMPLATE)
        self.assertEqual(
            branch_policy.resolve_branch(policy, lane="codex", issue_number=907, slug=""),
            "fix/907")
        self.assertEqual(
            branch_policy.resolve_branch(policy, lane="codex", issue_number=907, slug="!!!"),
            "fix/907")
        with self.assertRaisesRegex(branch_policy.BranchPolicyError, "neither"):
            branch_policy.resolve_branch(policy, lane="codex")
        numbered = branch_policy.compile_template("{work_type}/{issue_number}-{slug}")
        with self.assertRaises(branch_policy.BranchPolicyError):
            branch_policy.resolve_branch(numbered, lane="codex", issue_key="MB-9506")
        self.assertEqual(
            branch_policy.resolve_branch(numbered, lane="codex", issue_number=12, slug="a b"),
            "fix/12-a-b")

    def test_only_documented_variables_are_accepted(self) -> None:
        for bad in ("fix/{branch}", "fix/{issue_key", "fix/{slug}", "", "  fix/{issue_key}",
                    "fix/{issue_key}.lock", 7, "{issue_key}/..x"):
            with self.subTest(template=bad), self.assertRaises(branch_policy.BranchPolicyError):
                branch_policy.compile_template(bad)
        template = "{lane}/{work_type}/{repo_name}/{issue_key}-{slug}"
        policy = branch_policy.compile_template(template)
        self.assertEqual(
            branch_policy.resolve_branch(policy, lane="Muse", issue_key="MB-1", slug="x",
                                         repository="owner/Repo.js"),
            "muse/fix/Repo.js/MB-1-x")

    def test_nonconforming_branch_is_rejected_with_an_actionable_message(self) -> None:
        policy = branch_policy.compile_template(JIRA_TEMPLATE)
        with self.assertRaises(branch_policy.BranchPolicyError) as raised:
            branch_policy.validate_branch(policy, "muse/MB-9506-nv-accessible-label")
        message = str(raised.exception)
        self.assertIn("delivery_policy.branch_template", message)
        self.assertIn(policy.pattern, message)
        self.assertIn("fix/ABC-123-short-description", message)
        for bad in ("fix/MB-9506/", "fix/MB..9506", "fix/", "fix/MB-9506-Upper_Case/x", "", None):
            with self.subTest(branch=bad), self.assertRaises(branch_policy.BranchPolicyError):
                branch_policy.validate_branch(policy, bad)

    def test_pattern_is_portable_to_the_runner_jq_engine(self) -> None:
        # The generated runner evaluates the same pattern with jq's regex engine.
        policy = branch_policy.compile_template(JIRA_TEMPLATE)
        self.assertNotIn("\\d", policy.pattern)
        self.assertNotIn("(?P", policy.pattern)


class ProvenanceTests(unittest.TestCase):
    def test_policy_branch_keeps_provenance_with_label_and_author(self) -> None:
        # The VinoVoss/Muse fixture: the branch starts with fix/ and cannot encode
        # the provider; builder:muse and the authenticated author still can.
        cfg = _config_with_policy()
        cfg["builder_identity"]["labels"]["builder:muse"] = "muse"
        cfg["builder_identity"]["authors"]["muse-bot[bot]"] = "muse"
        cfg["builder_identity"]["branch_prefixes"]["muse/"] = "muse"
        self.assertEqual(code_mower_config.validate_config(cfg), [])
        identity = cfg["builder_identity"]
        identity["enabled"] = True
        policy = branch_policy.policy_for_repository(cfg, "owner/repo")
        branch = branch_policy.resolve_branch(
            policy, lane="muse", issue_key="MB-9506", slug="nv accessible label")
        self.assertTrue(branch.startswith("fix/"))
        # The branch prefix no longer names a lane; label and author still do.
        self.assertIsNone(next(
            (lane for prefix, lane in identity["branch_prefixes"].items()
             if branch.startswith(prefix)), None))
        self.assertEqual(
            builder_identity_matches(labels=["builder:muse"], author="muse-bot[bot]", text="",
                                     config=identity),
            ("muse",))
        self.assertEqual(
            builder_identity_matches(labels=[], author="muse-bot[bot]", text="", config=identity),
            ("muse",))
        # The provider-prefixed convention still infers the lane from the branch.
        self.assertEqual(identity["branch_prefixes"].get("muse/"), "muse")


class ConfigValidationTests(unittest.TestCase):
    def test_delivery_policy_is_validated_separately_from_builder_identity(self) -> None:
        self.assertEqual(code_mower_config.validate_config(_config_with_policy()), [])
        cfg = _config_with_policy("fix/{unknown}")
        issues = code_mower_config.validate_config(cfg)
        self.assertEqual([i.path for i in issues], ["repositories[0].delivery_policy.branch_template"])
        self.assertIn("{unknown}", issues[0].message)
        cfg = _config_with_policy()
        cfg["repositories"][0]["delivery_policy"] = {"branch_prefix": "fix/"}
        paths = sorted(i.path for i in code_mower_config.validate_config(cfg))
        self.assertEqual(paths, ["repositories[0].delivery_policy.branch_prefix",
                                 "repositories[0].delivery_policy.branch_template"])


class HostedWorkOrderTests(unittest.TestCase):
    def _order(self, branch: str, policy=None) -> WorkOrder:
        return WorkOrder.from_manifest(MANIFEST, "body", branch=branch, branch_policy=policy,
                                       **ORDER_ARGS)

    def test_conforming_branch_carries_pattern_and_example_into_the_prompt(self) -> None:
        policy = branch_policy.policy_for_repository(_config_with_policy(), "owner/repo")
        order = self._order("fix/907-accessible-label", policy)
        self.assertEqual((order.branch_pattern, order.branch_example),
                         (policy.pattern, policy.example))
        prompt = DevinWorkOrders._prompt(order)
        self.assertIn("branch-name policy", prompt)
        self.assertIn(policy.example, prompt)
        fields = json.loads(prompt.split("\n")[1])["policy"]
        self.assertEqual(fields["branch"], "fix/907-accessible-label")
        self.assertEqual(fields["branch_pattern"], policy.pattern)
        self.assertEqual(fields["branch_example"], policy.example)
        # The branch says nothing about the builder; the authenticated author does.
        self.assertEqual((fields["author_id"], fields["author_login"]), (123, "builder[bot]"))

    def test_nonconforming_branch_is_rejected_before_any_provider_action(self) -> None:
        policy = branch_policy.policy_for_repository(_config_with_policy(), "owner/repo")
        with self.assertRaises(RemoteError) as raised:
            self._order("devin/907", policy)
        self.assertIn("branch_policy_mismatch", str(raised.exception))
        self.assertIn(policy.example, str(raised.exception))
        with self.assertRaises(RemoteError):
            WorkOrder("owner/repo", 907, "devin/907", "main", 123, "builder[bot]", 5, "body",
                      branch_pattern=policy.pattern, branch_example=policy.example)

    def test_unconfigured_repositories_keep_provider_prefixed_orders_unchanged(self) -> None:
        order = self._order("devin/907")
        self.assertEqual((order.branch_pattern, order.branch_example), ("", ""))
        fields = DevinWorkOrders._fields(order)
        self.assertNotIn("branch_pattern", fields)
        self.assertNotIn("branch_example", fields)
        self.assertNotIn("branch-name policy", DevinWorkOrders._prompt(order))
        default = branch_policy.policy_for_repository(_config_with_policy(None), "owner/repo")
        self.assertEqual(self._order("devin/907", default).branch_pattern, "")


class GeneratedRunnerTests(unittest.TestCase):
    """The generated local runner resolves and guards the policy branch."""

    def _generate(self, cfg: dict) -> tuple[Path, str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        output_dir = Path(tmp.name) / "generated"
        code_mower_init.apply_init_plan(_builders_plan(cfg), output_dir)
        runner = output_dir / "tools/lanes/run_mac_lane.sh"
        return runner, runner.read_text(encoding="utf-8")

    def test_runner_without_policy_matches_the_repository_copy_defaults(self) -> None:
        _runner, text = self._generate(_config_with_policy(None))
        self.assertIn("branch_policy_json='{}'", text)
        repo_copy = (ROOT / "tools/lanes/run_mac_lane.sh").read_text(encoding="utf-8")
        self.assertIn("branch_policy_json='{}'", repo_copy)

    def test_runner_embeds_configured_policy_per_repository(self) -> None:
        _runner, text = self._generate(_config_with_policy())
        line = next(row for row in text.splitlines() if row.startswith("branch_policy_json="))
        embedded = json.loads(line[len("branch_policy_json="):].strip("'"))
        expected = branch_policy.compile_template(JIRA_TEMPLATE).describe()
        self.assertEqual(embedded, {"owner/repo": expected})

    def test_runner_resolves_the_policy_branch_before_the_provider_runs(self) -> None:
        runner, _text = self._generate(_config_with_policy())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"
            (work_root / "codex" / "owner__repo" / ".git" / "hooks").mkdir(parents=True)
            prompt_log = root / "prompt.md"
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:codex"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '[{"number":12,"title":"NV: Accessible label","labels":[{"name":"tier:R"},{"name":"builder:codex"},{"name":"dispatched:codex"}],"assignees":[],"author":{"login":"owner"}}]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ] && [[ "$args" == *"--json title -q"* ]]; then
  printf 'NV: Accessible label\\n'
elif [ "$cmd" = "issue view" ] && [[ "$args" == *"--json title,body,labels,url,author"* ]]; then
  printf '%s\\n' '{"title":"NV: Accessible label","body":"Body","labels":[{"name":"tier:R"}],"url":"https://github.com/owner/repo/issues/12","author":{"login":"owner"}}'
elif [ "$cmd" = "issue view" ] && [[ "$args" == *"--json comments"* ]]; then
  printf '%s\\n' '{"comments":[]}'
elif [ "$cmd" = "issue view" ] && [[ "$args" == *"--json author,comments"* ]]; then
  printf '%s\\n' '{"author":{"login":"owner"},"comments":[]}'
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
                """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-C" ] && [ "${3:-}" = "config" ]; then
  printf '%s\\n' 'https://github.com/owner/repo.git'
  exit 0
fi
if [ "${1:-}" = "rev-parse" ] && [ "${2:-}" = "--git-path" ]; then
  printf '%s\\n' ".git/${3}"
  exit 0
fi
exit 0
""",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            fake_codex = bin_dir / "codex"
            fake_codex.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
cat > "$PROMPT_LOG"
cp "$(git rev-parse --git-path code-mower-lane-guard.json)" "$GUARD_LOG" 2>/dev/null || true
: > "$HOME/lane-delivered"
printf 'fake codex completed\\n'
""",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            guard_log = root / "guard.json"
            completed = subprocess.run(
                [str(runner), "--lane", "codex", "--repo", "owner/repo", "--max-minutes", "1"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "PROMPT_LOG": str(prompt_log),
                    "GUARD_LOG": str(guard_log),
                    **_LANE_DELIVERY_ENV,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            prompt = prompt_log.read_text(encoding="utf-8")
            guard = json.loads(
                (work_root / "codex" / "owner__repo" / ".git" / "code-mower-lane-guard.json")
                .read_text(encoding="utf-8"))

        policy = branch_policy.compile_template(JIRA_TEMPLATE)
        self.assertIn("fake codex completed", completed.stdout)
        self.assertIn("Branch policy: owner/repo accepts builder branches matching the template "
                      f"{JIRA_TEMPLATE} (pattern {policy.pattern}, for example {policy.example})",
                      prompt)
        self.assertIn("push exactly the branch fix/12-nv-accessible-label", prompt)
        self.assertEqual(guard["allowed_pattern"], policy.pattern)
        self.assertEqual(guard["allowed_prefixes"], ["codex/"])


if __name__ == "__main__":
    unittest.main()
