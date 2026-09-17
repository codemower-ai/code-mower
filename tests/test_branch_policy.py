"""Repository branch-name policy: resolution, validation, and its two seams.

A target repository's accepted branch names and a builder's provenance are
distinct contracts. These tests pin the resolver, its configuration surface,
the hosted Devin work-order seam, and the generated local runner seam, plus
the provider-prefix convention that applies when no policy is configured.
"""
from __future__ import annotations
from lineage_consumer_fixtures import RUNNER_GH_BOUNDARY, RUNNER_GIT_BOUNDARY, runner_lineage_env, complete_pr

import copy
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runner_fixture_support import enable_fake_codex_sandbox, fake_handoff_source

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


def _config_with_policy(template: str | None = JIRA_TEMPLATE, *, slug: str = "owner/repo") -> dict:
    cfg = copy.deepcopy(code_mower_config.load_config(CONFIG_PATH))
    repo = cfg["repositories"][0]
    repo["slug"] = slug
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

    def test_repository_slugs_are_deduplicated_case_insensitively(self) -> None:
        # Policy lookup is case-insensitive, so `Owner/Repo` would silently
        # shadow (or be shadowed by) `owner/repo`; that is a configuration error.
        cfg = _config_with_policy()
        shadow = copy.deepcopy(cfg["repositories"][0])
        shadow["slug"] = "Owner/Repo"
        shadow["delivery_policy"] = {"branch_template": "{lane}/{issue_number}"}
        cfg["repositories"].append(shadow)
        issues = code_mower_config.validate_config(cfg)
        self.assertEqual([(i.path, i.message) for i in issues],
                         [("repositories[1].slug", "duplicate repository Owner/Repo")])
        with self.assertRaises(branch_policy.BranchPolicyError):
            branch_policy.configured_policies(cfg)
        with self.assertRaises(RemoteError) as raised:
            WorkOrder.repository_policy(cfg, "owner/repo")
        self.assertIn("branch_policy_config", str(raised.exception))
        self.assertEqual(WorkOrder.repository_policy(_config_with_policy(), "OWNER/Repo").template,
                         JIRA_TEMPLATE)


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
        order = self._order("devin/907", branch_policy.default_policy())
        self.assertEqual((order.branch_pattern, order.branch_example), ("", ""))
        fields = DevinWorkOrders._fields(order)
        self.assertNotIn("branch_pattern", fields)
        self.assertNotIn("branch_example", fields)
        self.assertNotIn("branch-name policy", DevinWorkOrders._prompt(order))
        default = branch_policy.policy_for_repository(_config_with_policy(None), "owner/repo")
        self.assertEqual(self._order("devin/907", default).branch_pattern, "")
        by_config = WorkOrder.from_manifest(MANIFEST, "body", branch="devin/907",
                                            config=_config_with_policy(None), **ORDER_ARGS)
        self.assertEqual(by_config.branch_pattern, "")

    def test_dispatcher_cannot_omit_a_configured_repository_policy(self) -> None:
        # Neither omission nor an ambiguous double supply is an accepted way to
        # construct an order: the configured policy is applied from config itself.
        with self.assertRaises(RemoteError) as raised:
            WorkOrder.from_manifest(MANIFEST, "body", branch="devin/907", **ORDER_ARGS)
        self.assertIn("branch_policy_required", str(raised.exception))
        with self.assertRaises(RemoteError):
            WorkOrder.from_manifest(MANIFEST, "body", branch="devin/907", config=_config_with_policy(),
                                    branch_policy=branch_policy.default_policy(), **ORDER_ARGS)
        cfg = _config_with_policy()
        with self.assertRaises(RemoteError) as raised:
            WorkOrder.from_manifest(MANIFEST, "body", branch="devin/907", config=cfg, **ORDER_ARGS)
        self.assertIn("branch_policy_mismatch", str(raised.exception))
        order = WorkOrder.from_manifest(MANIFEST, "body", branch="fix/907-accessible-label",
                                        config=cfg, **ORDER_ARGS)
        self.assertEqual(order.branch_pattern, branch_policy.compile_template(JIRA_TEMPLATE).pattern)
        cfg["repositories"][0]["slug"] = "Owner/Repo"
        with self.assertRaises(RemoteError):
            WorkOrder.from_manifest(MANIFEST, "body", branch="devin/907", config=cfg, **ORDER_ARGS)

    def test_jira_keyed_order_resolves_a_conforming_branch_on_the_first_attempt(self) -> None:
        """The maintained dispatcher path: tracker key -> {issue_key} -> validated order."""
        cfg = _config_with_policy()
        policy = WorkOrder.repository_policy(cfg, "owner/repo")
        branch = WorkOrder.resolve_branch(policy, lane="devin", issue=907, work_item="MB-9506",
                                          slug="NV: accessible label!")
        self.assertEqual(branch, "fix/MB-9506-nv-accessible-label")
        manifest = {**MANIFEST, "source": {"repo": "owner/repo"}}
        order = WorkOrder.from_manifest(manifest, "body", branch=branch, config=cfg,
                                        context_policy="required", context_work_item="MB-9506",
                                        **ORDER_ARGS)
        self.assertEqual((order.branch, order.work_item, order.issue), (branch, "MB-9506", 907))
        fields = json.loads(DevinWorkOrders._prompt(order).split("\n")[1])["policy"]
        self.assertEqual(fields["branch"], branch)
        # Without a tracker key the GitHub issue number is the key; a template
        # that cannot be satisfied fails before any provider action.
        self.assertEqual(WorkOrder.resolve_branch(policy, lane="devin", issue=907, slug="x"),
                         "fix/907-x")
        with self.assertRaises(RemoteError) as raised:
            WorkOrder.resolve_branch(policy, lane="devin", issue=907, work_item="bad key")
        self.assertIn("branch_policy_mismatch", str(raised.exception))


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

    def _run_codex_lane(self, delivered_listing: str, *, template: str = JIRA_TEMPLATE,
                        repo: str = "owner/repo",
                        title_lookup: str = "printf 'NV: Accessible label\\n'",
                        existing_issue_prs: str = "[]",
                        existing_branch: str | None = None,
                        existing_branch_head: str = "c" * 40,
                        existing_branch_prs: str = "[]",
                        explicit_issue_target: bool = False,
                        candidate_issues: str | None = None,
                        gh_call_log: Path | None = None,
                        base_sha: str | None = None,
                        supervise_log: Path | None = None,
                        lineage_store_pr: int | None = None,
                        published_creation_pr: int | None = None,
                        after_snapshot_fails: bool = False,
                        lineage_listing: list[str] | None = None,
                        ) -> tuple[subprocess.CompletedProcess, str, dict]:
        """Run the generated codex runner against a fake provider that opens a PR.

        ``delivered_listing`` is the ``gh pr list`` JSON returned once the provider
        has "delivered"; it is the delivery-snapshot discovery input under test.
        ``title_lookup`` is the fake ``gh issue view --json title -q .title`` body.
        ``existing_branch`` makes the fake ``git ls-remote`` advertise that branch
        as already present on origin and ``existing_branch_prs`` is the ``gh pr
        list --head`` JSON attached to it.

        ``base_sha`` is what the fake ``git rev-parse`` reports for the immutable
        launch base. ``supervise_log`` records the exact ``lane-delivery
        supervise`` argv this runner builds, one argument per line, and strips
        the lineage selection from the invocation that actually runs so the
        fixture stays offline while still inspecting what was passed.
        ``lineage_store_pr`` pre-creates the private lineage record a delivered
        pull request of that number would carry, which is what marks a rerun as
        continuing an existing chain rather than bootstrapping a new one.

        ``published_creation_pr`` makes the supervised creation round behave as
        one that published: it writes the attribution output naming that pull
        request and leaves the private record under the issue.
        ``after_snapshot_fails`` makes every post-delivery ``gh pr view`` fail,
        which is the incomplete after-snapshot that ends the run at the
        undelivered path. ``lineage_listing`` is extended with the private
        lineage directory's entries before the fixture is torn down.
        """
        ls_remote = "exit 0"
        if existing_branch is not None:
            ls_remote = (
                f"case \" $* \" in *' refs/heads/{existing_branch} '*) "
                f"printf '%s\\trefs/heads/%s\\n' '{existing_branch_head}' '{existing_branch}' ;; esac; exit 0"
            )
        runner, _text = self._generate(_config_with_policy(template, slug=repo))
        repo_dir = repo.replace("/", "__")
        header = _FAKE_GH_DELIVERY_HEADER.replace("owner/repo", repo).replace(
            "[{\"number\":77,\"headRefName\":\"codex/issue-12\","
            "\"headRefOid\":\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\","
            "\"headRepository\":{\"nameWithOwner\":\"owner/repo\"},"
            "\"labels\":[{\"name\":\"builder:codex\"}],"
            "\"author\":{\"login\":\"chatgpt-codex-connector[bot]\"},"
            "\"closingIssuesReferences\":[{\"number\":12,"
            "\"repository\":{\"nameWithOwner\":\"owner/repo\"},"
            "\"url\":\"https://github.com/owner/repo/issues/12\"}]}]",
            delivered_listing,
        )
        self.assertNotEqual(header, _FAKE_GH_DELIVERY_HEADER)
        if after_snapshot_fails:
            # Only the read the runner takes *after* the provider delivered:
            # the pre-run snapshot has to stay complete or the unit is refused
            # before a provider ever starts.
            marker = 'if [ "$cmd" = "pr list" ]'
            self.assertIn(marker, header)
            header = header.replace(
                marker,
                'if [ "$cmd" = "pr view" ] && [ -f "$HOME/lane-delivered" ]; then\n'
                "  exit 1\n"
                "el" + marker,
                1,
            )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"
            (work_root / "codex" / repo_dir / ".git" / "hooks").mkdir(parents=True)
            if lineage_store_pr is not None:
                (root / ".local/share/code-mower/lineage" / repo_dir
                 / str(lineage_store_pr)).mkdir(parents=True)
            prompt_log = root / "prompt.md"
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                header
                + """if [ "$cmd" = "pr list" ] && [[ "$args" == *"--label builder:codex"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "issue list" ]; then
  printf '%s\\n' '__CANDIDATE_ISSUES__'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--search"* ]]; then
  printf '%s\\n' '[]'
elif [ "$cmd" = "pr list" ] && [[ "$args" == *"--state all --head "* ]]; then
  printf '%s\\n' '__EXISTING_BRANCH_PRS__'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "issue view" ] && [[ "$args" == *"--json title -q"* ]]; then
  __TITLE_LOOKUP__
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
""".replace("owner/repo", repo).replace("__TITLE_LOOKUP__", title_lookup)
                .replace("__CANDIDATE_ISSUES__", candidate_issues or json.dumps([{
                    "number": 12, "title": "NV: Accessible label",
                    "labels": [{"name": "tier:R"}, {"name": "builder:codex"},
                               {"name": "dispatched:codex"}],
                    "assignees": [], "author": {"login": "owner"},
                }]))
                .replace("__EXISTING_BRANCH_PRS__", existing_branch_prs),
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
if [ "${1:-}" = "-C" ] && [ "${3:-}" = "ls-remote" ]; then
  __LS_REMOTE__
fi
if [ "${1:-}" = "-C" ] && [ "${3:-}" = "rev-parse" ] && [ "${4:-}" = "origin/main^{commit}" ]; then
  __REV_PARSE__
fi
if [ "${1:-}" = "rev-parse" ] && [ "${2:-}" = "--git-path" ]; then
  printf '%s\\n' ".git/${3}"
  exit 0
fi
exit 0
""".replace("owner/repo", repo).replace("__LS_REMOTE__", ls_remote).replace(
                    "__REV_PARSE__",
                    f"printf '%s\\n' '{base_sha}'; exit 0" if base_sha else ":"),
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            fake_codex = bin_dir / "codex"
            fake_codex.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
cat > "$PROMPT_LOG"
: > "$HOME/lane-delivered"
printf 'fake codex completed\\n'
""",
                encoding="utf-8",
            )
            enable_fake_codex_sandbox(fake_codex)
            lane_delivery_env = dict(_LANE_DELIVERY_ENV)
            if supervise_log is not None:
                recorder = bin_dir / "recording-lane-delivery"
                # The runner's own argv is the evidence, so it is written down
                # verbatim before anything consumes it. The lineage selection is
                # then dropped from the invocation that actually runs: starting a
                # creation round would read the real repository, and this fixture
                # is about what the runner passes, not about the round itself.
                #
                # ``published_creation_pr`` stands in for the one effect a real
                # creation round leaves behind before returning: the chain is
                # already minted and published, the attribution output names the
                # pull request the supervisor discovered, and the private record
                # is still filed under the issue the round was launched for.
                recorder.write_text(
                    """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "supervise" ]; then
  printf '%s\\n' "$@" > __SUPERVISE_LOG__
  filtered=()
  skip=""
  provider_argv=0
  lineage_issue=""
  lineage_store=""
  lineage_output=""
  for arg in "$@"; do
    if [ -n "$skip" ]; then
      case "$skip" in
        --lineage-issue) lineage_issue="$arg" ;;
        --lineage-store) lineage_store="$arg" ;;
        --lineage-output) lineage_output="$arg" ;;
      esac
      skip=""
      continue
    fi
    # Everything after the first -- is the provider's own argv and is passed
    # through untouched.
    if [ "$provider_argv" = 1 ]; then
      filtered+=("$arg")
      continue
    fi
    if [ "$arg" = "--" ]; then
      provider_argv=1
      filtered+=("$arg")
      continue
    fi
    case "$arg" in
      --lineage-issue|--lineage-branch|--lineage-before|--lineage-base|--lineage-writer|--lineage-store|--lineage-output)
        skip="$arg"; continue ;;
    esac
    filtered+=("$arg")
  done
  if [ -n "__PUBLISHED_CREATION_PR__" ] && [ -n "$lineage_issue" ] \
    && [ -n "$lineage_store" ] && [ -n "$lineage_output" ]; then
    mkdir -p "$lineage_store"
    printf '%s\\n' '{"dimensions":{"pr_repo":"__REPO__","pr_number":"__PUBLISHED_CREATION_PR__"}}' \
      > "$lineage_output"
  fi
  set -- "${filtered[@]}"
fi
exec __REAL_LANE_DELIVERY__ "$@"
""".replace("__SUPERVISE_LOG__", shlex.quote(str(supervise_log)))
   .replace("__PUBLISHED_CREATION_PR__",
            "" if published_creation_pr is None else str(published_creation_pr))
   .replace("__REPO__", repo)
   .replace("__REAL_LANE_DELIVERY__",
            shlex.quote(_LANE_DELIVERY_ENV["CODE_MOWER_LANE_DELIVERY_CMD"])),
                    encoding="utf-8",
                )
                recorder.chmod(0o755)
                lane_delivery_env["CODE_MOWER_LANE_DELIVERY_CMD"] = str(recorder)
            argv = [str(runner), "--lane", "codex", "--repo", repo, "--max-minutes", "1"]
            if explicit_issue_target:
                argv.extend(["--target", "issue:12"])
            completed = subprocess.run(
                argv,
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "PROMPT_LOG": str(prompt_log),
                    "EXISTING_OPEN_PRS_JSON": existing_issue_prs,
                    "GH_CALL_LOG": str(gh_call_log) if gh_call_log else "",
                    **lane_delivery_env,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            prompt = prompt_log.read_text(encoding="utf-8") if prompt_log.exists() else ""
            guard_path = work_root / "codex" / repo_dir / ".git" / "code-mower-lane-guard.json"
            guard = json.loads(guard_path.read_text(encoding="utf-8")) if guard_path.exists() else {}
            if lineage_listing is not None:
                lineage_root = root / ".local/share/code-mower/lineage" / repo_dir
                lineage_listing.extend(
                    sorted(entry.name for entry in lineage_root.iterdir())
                    if lineage_root.is_dir() else [])
        return completed, prompt, guard

    @staticmethod
    def _pr(number: int, branch: str, *, labels=(), author: str = "owner",
            repo: str = "owner/repo", head: str | None = "c" * 40,
            closing_repo: str = "owner/repo", closing_issue: int = 12) -> dict:
        return {"number": number, "headRefName": branch,
                **({"headRefOid": head} if head is not None else {}),
                "headRepository": {"nameWithOwner": repo},
                "labels": [{"name": name} for name in labels],
                "author": {"login": author},
                "closingIssuesReferences": [{
                    "number": closing_issue,
                    "repository": {"nameWithOwner": closing_repo},
                    "url": f"https://github.com/{closing_repo}/issues/{closing_issue}",
                }]}

    def test_runner_resolves_the_policy_branch_before_the_provider_runs(self) -> None:
        own = self._pr(77, "fix/12-nv-accessible-label", labels=("builder:codex",),
                       author="chatgpt-codex-connector[bot]")
        completed, prompt, guard = self._run_codex_lane(json.dumps([own]))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        policy = branch_policy.compile_template(JIRA_TEMPLATE)
        self.assertIn("fake codex completed", completed.stdout)
        self.assertIn("Branch policy: owner/repo accepts builder branches matching the template "
                      f"{JIRA_TEMPLATE} (pattern {policy.pattern}, for example {policy.example})",
                      prompt)
        self.assertIn("push exactly the branch fix/12-nv-accessible-label", prompt)
        # Write authority is the exact resolved branch, never the policy regex
        # and not the lane's ordinary prefixes either: a policy-bound issue run
        # may push no other name.
        self.assertEqual(guard["allowed_branch"], "fix/12-nv-accessible-label")
        self.assertEqual(guard["allowed_branch_expected_head"], "absent")
        self.assertNotIn("allowed_pattern", guard)
        self.assertEqual(guard["allowed_prefixes"], [])

    # Creation lineage (codemower-ai/code-mower#1020) attests the pull request
    # an issue-targeted round opens. These rows read the argv the runner itself
    # builds for `lane-delivery supervise`, because the contract only holds if
    # the real issue path passes the origin binding; calling the supervisor
    # helper directly would prove nothing about this runner.
    LANE_PREFIX_TEMPLATE = "{lane}/{issue_number}-{slug}"
    CREATED_BRANCH = "codex/12-nv-accessible-label"

    def _supervise_argv(self, **kwargs) -> tuple[subprocess.CompletedProcess, list[str], str, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "supervise-argv"
            completed, prompt, guard = self._run_codex_lane(supervise_log=log, **kwargs)
            argv = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return completed, argv, prompt, guard

    @staticmethod
    def _flag(argv: list[str], flag: str) -> str | None:
        return argv[argv.index(flag) + 1] if flag in argv else None

    def test_issue_runner_launches_a_creation_round_bound_to_the_reserved_branch(self) -> None:
        own = self._pr(77, self.CREATED_BRANCH, labels=("builder:codex",),
                       author="chatgpt-codex-connector[bot]")
        base = "d" * 40
        completed, argv, prompt, guard = self._supervise_argv(
            delivered_listing=json.dumps([own]), template=self.LANE_PREFIX_TEMPLATE,
            base_sha=base)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("supervise", argv)
        # The round is bound to the issue it was launched for, the branch the
        # runner reserved before launch, and the immutable base this checkout
        # sits on -- not to anything the writer chooses afterwards.
        self.assertEqual(self._flag(argv, "--lineage-issue"), "12")
        self.assertEqual(self._flag(argv, "--lineage-branch"), self.CREATED_BRANCH)
        self.assertEqual(self._flag(argv, "--lineage-base"), base)
        self.assertTrue((self._flag(argv, "--lineage-writer") or "").startswith("codex"))
        store = self._flag(argv, "--lineage-store") or ""
        self.assertTrue(store.endswith("/lineage/owner__repo/issue-12"), store)
        self.assertTrue((self._flag(argv, "--lineage-output") or "").endswith(".lineage.json"))
        # An issue-targeted round has no pre-existing pull request to hand over
        # or to snapshot, and the supervisor refuses the combination outright.
        self.assertNotIn("--lineage-before", argv)
        self.assertNotIn("--lineage-handoff", argv)
        self.assertNotIn("--lineage-create", argv)
        # The reserved branch is the one name the writer is told to push and the
        # only one the guard authorizes, so the reservation is not advisory.
        self.assertIn("push exactly the branch " + self.CREATED_BRANCH, prompt)
        self.assertEqual(guard["allowed_branch"], self.CREATED_BRANCH)
        self.assertEqual(guard["allowed_prefixes"], [])

    def test_a_policy_branch_outside_the_lane_prefixes_keeps_the_plain_bootstrap(self) -> None:
        # fix/12-... cannot be reserved by this lane, and the no-PR bootstrap
        # this runner has always performed must survive that, not refuse.
        own = self._pr(77, "fix/12-nv-accessible-label", labels=("builder:codex",),
                       author="chatgpt-codex-connector[bot]")
        completed, argv, _prompt, _guard = self._supervise_argv(
            delivered_listing=json.dumps([own]))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("supervise", argv)
        self.assertFalse([arg for arg in argv if arg.startswith("--lineage-")], argv)
        self.assertIn("is outside this lane's prefixes", completed.stdout)

    def test_an_existing_lane_owned_policy_branch_starts_no_creation_round(self) -> None:
        # Continuing work that already has a branch is not a creation, and the
        # reservation would refuse it; the runner must not ask for one.
        own = self._pr(77, self.CREATED_BRANCH, labels=("builder:codex",),
                       author="chatgpt-codex-connector[bot]")
        completed, argv, _prompt, _guard = self._supervise_argv(
            delivered_listing=json.dumps([own]), template=self.LANE_PREFIX_TEMPLATE,
            existing_branch=self.CREATED_BRANCH, existing_branch_head="c" * 40,
            existing_branch_prs=json.dumps([own]))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("supervise", argv)
        self.assertFalse([arg for arg in argv if arg.startswith("--lineage-")], argv)

    def _continuation_lane(self, **kwargs):
        """An issue rerun whose pull request this lane already created.

        The delivered pull request is open on the reserved branch at ``a``*40,
        closes the issue, and the round moves its head to ``b``*40.
        """
        head = "a" * 40
        open_pr = self._pr(77, self.CREATED_BRANCH, labels=("builder:codex",),
                           author="chatgpt-codex-connector[bot]", head=head)
        moved = self._pr(77, self.CREATED_BRANCH, labels=("builder:codex",),
                         author="chatgpt-codex-connector[bot]", head="b" * 40)
        return self._supervise_argv(
            delivered_listing=json.dumps([moved]), template=self.LANE_PREFIX_TEMPLATE,
            base_sha="d" * 40, existing_issue_prs=json.dumps([open_pr]),
            existing_branch=self.CREATED_BRANCH, existing_branch_head=head,
            existing_branch_prs=json.dumps([open_pr]),
            # The branch comes from the open pull request, never from a fresh
            # slug, so reaching the title lookup would mean this is not the
            # rerun path under test.
            title_lookup="printf 'title lookup must not run\\n' >&2; exit 99",
            explicit_issue_target=True, **kwargs)

    def test_rerunning_an_issue_continues_its_created_pull_requests_chain(self) -> None:
        # The creation round published a chain bound to the head it delivered
        # and moved the private record onto the delivered number. A rerun that
        # advances that pull request without extending the chain leaves the
        # published lineage on a head that no longer exists, which is what
        # answers `lineage_head_pending` for later reviewers and fix rounds.
        completed, argv, _prompt, _guard = self._continuation_lane(lineage_store_pr=77)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("supervise", argv)
        # This is a delivery to an existing pull request, so it takes exactly
        # the continuation path a fix round on that pull request takes.
        self.assertTrue((self._flag(argv, "--lineage-before") or "").endswith(".continuation.json"))
        self.assertEqual(self._flag(argv, "--lineage-base"), "d" * 40)
        self.assertTrue((self._flag(argv, "--lineage-writer") or "").startswith("codex"))
        store = self._flag(argv, "--lineage-store") or ""
        self.assertTrue(store.endswith("/lineage/owner__repo/77"), store)
        self.assertTrue((self._flag(argv, "--lineage-output") or "").endswith(".lineage.json"))
        # Nothing is created here: the branch exists, so there is no
        # reservation to claim, and the writer has not changed, so no handoff.
        self.assertNotIn("--lineage-issue", argv)
        self.assertNotIn("--lineage-branch", argv)
        self.assertNotIn("--lineage-create", argv)
        self.assertNotIn("--lineage-handoff", argv)

    def test_a_pull_request_carrying_no_chain_keeps_the_unsupervised_rerun(self) -> None:
        # Same rerun, no private record: there is no chain to continue, so the
        # long-standing issue rerun is left exactly as it was rather than
        # gaining a new pre-launch refusal surface.
        completed, argv, _prompt, _guard = self._continuation_lane()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("supervise", argv)
        self.assertFalse([arg for arg in argv if arg.startswith("--lineage-")], argv)
        self.assertIn("carries no private lineage record", completed.stdout)

    def _published_creation_lane(self, **kwargs):
        """A creation round that published its chain before returning."""
        own = self._pr(77, self.CREATED_BRANCH, labels=("builder:codex",),
                       author="chatgpt-codex-connector[bot]")
        return self._supervise_argv(
            delivered_listing=json.dumps([own]), template=self.LANE_PREFIX_TEMPLATE,
            base_sha="d" * 40, **kwargs)

    def test_a_published_creation_round_files_its_record_before_classification(self) -> None:
        # The chain is minted and published inside the supervisor. The runner's
        # own after-snapshot then fails, which ends the unit at the undelivered
        # path -- before every post-delivery block. The private record has to be
        # filed on the pull request the supervisor itself discovered by then, or
        # the published chain has nothing for a later fix round or issue rerun
        # to extend and each of them answers `lineage_head_pending` instead.
        listing: list[str] = []
        completed, argv, _prompt, _guard = self._published_creation_lane(
            published_creation_pr=77, after_snapshot_fails=True, lineage_listing=listing)
        self.assertEqual(self._flag(argv, "--lineage-issue"), "12")
        # The run really did end where the finding says it ends.
        self.assertEqual(completed.returncode, 3, completed.stderr)
        self.assertIn("no validated delivery", completed.stderr)
        self.assertEqual(listing, ["77"])

    def test_a_published_creation_round_replaces_the_post_hoc_builder_record(self) -> None:
        # Same publication, classification succeeding this time: the record is
        # filed on the same number, and the weaker post-hoc attribution is not
        # written on top of the exact one this round already published.
        listing: list[str] = []
        completed, argv, _prompt, _guard = self._published_creation_lane(
            published_creation_pr=77, lineage_listing=listing)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self._flag(argv, "--lineage-issue"), "12")
        self.assertEqual(listing, ["77"])

    def test_a_creation_round_that_published_nothing_files_no_record(self) -> None:
        # No published chain, so there is no exact attribution to file and
        # nothing to relocate; the run keeps the post-hoc path it always had.
        listing: list[str] = []
        completed, argv, _prompt, _guard = self._published_creation_lane(lineage_listing=listing)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self._flag(argv, "--lineage-issue"), "12")
        self.assertEqual(listing, [])

    def test_every_runner_copy_passes_the_same_creation_origin_binding(self) -> None:
        for path in (ROOT / "tools/lanes/run_mac_lane.sh",
                     ROOT / "templates/lanes/run_mac_lane.sh",
                     ROOT / "src/code_mower/templates/lanes/run_mac_lane.sh"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(runner=path.name, parent=path.parent.as_posix()):
                self.assertIn('--lineage-issue "$num" --lineage-branch "$creation_branch"', text)
                self.assertIn('--lineage-store "$creation_store"', text)
                # A rerun of a delivered issue continues that chain in every
                # copy, or the published lineage strands wherever this runner
                # was installed from.
                self.assertIn('--lineage-before "$continuation_before"', text)
                self.assertIn('--lineage-store "$continuation_store"', text)
                # A published creation round files its record on the number the
                # supervisor discovered, and does it before the independently
                # fallible delivery classification -- in every copy, or the
                # chain strands wherever this runner was installed from.
                self.assertIn("creation_published=1", text)
                self.assertIn('&& [ -z "$creation_published" ]; then', text)
                self.assertLess(
                    text.index('mv "$creation_store" "$creation_delivered_store"'),
                    text.index('capture_target_state "$after_state"'),
                    "the record must be filed before the after-snapshot is even read")

    def test_runner_refuses_an_existing_policy_branch_it_does_not_own(self) -> None:
        # fix/12-nv-accessible-label is the one name the policy allows for
        # issue 12, for every builder and for humans alike. When it already
        # exists on origin, the pull request attached to it decides ownership;
        # a foreign builder's, a human's, or no attributable pull request at
        # all refuses before the guard is installed or a provider starts. The
        # delivery-snapshot filter must not make such a PR look absent.
        branch = "fix/12-nv-accessible-label"
        cases = {
            "foreign_builder_label": [self._pr(70, branch, labels=("builder:claude",))],
            "foreign_builder_author": [self._pr(70, branch, author="claude[bot]")],
            "nonlocal_builder": [self._pr(70, branch, labels=("builder:cursor",), author="cursor[bot]")],
            "human_pr": [self._pr(70, branch)],
            "conflicting_provenance": [
                self._pr(70, branch, labels=("builder:codex",), author="claude[bot]")
            ],
            "human_pr_beside_own": [
                self._pr(70, branch),
                self._pr(71, branch, labels=("builder:codex",), author="chatgpt-codex-connector[bot]"),
            ],
            "branch_without_pr": [],
        }
        for name, prs in cases.items():
            with self.subTest(case=name):
                completed, prompt, guard = self._run_codex_lane(
                    "[]", existing_branch=branch, existing_branch_prs=json.dumps(prs))
                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                self.assertIn(f"refusing issue #12; policy branch {branch} already exists on owner/repo",
                              completed.stderr)
                if name == "human_pr_beside_own":
                    self.assertIn("with multiple pull requests (70, 71); ownership is ambiguous",
                                  completed.stderr)
                elif prs:
                    self.assertIn("pull request #70 on it is owned by another builder or a human",
                                  completed.stderr)
                else:
                    self.assertIn("no pull request carrying this lane's provenance", completed.stderr)
                self.assertNotIn("fake codex completed", completed.stdout)
                self.assertEqual(prompt, "")
                self.assertEqual(guard, {})

    def test_runner_refuses_an_existing_policy_branch_when_its_pull_requests_cannot_be_read(self) -> None:
        branch = "fix/12-nv-accessible-label"
        completed, prompt, guard = self._run_codex_lane(
            "[]", existing_branch=branch, existing_branch_prs="'; exit 1; echo '")
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("its pull requests could not be read", completed.stderr)
        self.assertEqual(prompt, "")
        self.assertEqual(guard, {})

    def test_runner_continues_on_an_existing_policy_branch_this_lane_owns(self) -> None:
        branch = "fix/12-nv-accessible-label"
        own = self._pr(77, branch, labels=("builder:codex",), author="chatgpt-codex-connector[bot]")
        completed, prompt, guard = self._run_codex_lane(
            json.dumps([own]), existing_branch=branch, existing_branch_prs=json.dumps([own]))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn(f"policy branch {branch} already exists on owner/repo at {'c' * 40} "
                      "with this lane's exact-head provenance",
                      completed.stdout)
        self.assertIn("fake codex completed", completed.stdout)
        self.assertIn(f"push exactly the branch {branch}", prompt)
        self.assertEqual(guard["allowed_branch"], branch)
        self.assertEqual(guard["allowed_branch_expected_head"], "c" * 40)
        self.assertEqual(guard["allowed_prefixes"], [])

    def test_runner_reuses_a_sidebar_linked_pr_after_the_issue_title_changes(self) -> None:
        # The PR has a GitHub closingIssuesReferences relationship and no body
        # field or #12 text. Discovery must use that Development-sidebar link,
        # then retain its branch even though the mutable title now differs.
        old_branch = "fix/12-original-title"
        own = self._pr(77, old_branch, labels=("builder:codex",),
                       author="chatgpt-codex-connector[bot]")
        completed, prompt, guard = self._run_codex_lane(
            json.dumps([own]),
            title_lookup="printf 'title lookup must not run\\n' >&2; exit 99",
            existing_issue_prs=json.dumps([own]),
            existing_branch=old_branch,
            existing_branch_prs=json.dumps([own]),
            explicit_issue_target=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn(f"reusing policy branch {old_branch} from existing pull request #77",
                      completed.stdout)
        self.assertNotIn("title lookup must not run", completed.stderr)
        self.assertIn(f"push exactly the branch {old_branch}", prompt)
        self.assertEqual(guard["allowed_branch"], old_branch)
        self.assertEqual(guard["allowed_branch_expected_head"], "c" * 40)

    def test_runner_ignores_a_same_number_closing_reference_from_another_repo(self) -> None:
        cross_repo = self._pr(
            70, "fix/12-other-repo", labels=("builder:codex",),
            author="chatgpt-codex-connector[bot]", closing_repo="other/repo")
        delivered = self._pr(
            77, "fix/12-nv-accessible-label", labels=("builder:codex",),
            author="chatgpt-codex-connector[bot]")
        completed, prompt, guard = self._run_codex_lane(
            json.dumps([delivered]), existing_issue_prs=json.dumps([cross_repo]))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertNotIn("reusing policy branch fix/12-other-repo", completed.stdout)
        self.assertIn("push exactly the branch fix/12-nv-accessible-label", prompt)
        self.assertEqual(guard["allowed_branch"], "fix/12-nv-accessible-label")
        self.assertEqual(guard["allowed_branch_expected_head"], "absent")

    def test_runner_fails_closed_when_open_pr_enumeration_exceeds_its_bound(self) -> None:
        completed, prompt, guard = self._run_codex_lane(
            "[]", existing_issue_prs=json.dumps([{}] * 1001), explicit_issue_target=True)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("could not completely enumerate existing pull requests",
                      completed.stderr)
        self.assertNotIn("fake codex completed", completed.stdout)
        self.assertEqual(prompt, "")
        self.assertEqual(guard, {})

    def test_issue_selection_reuses_one_open_pr_listing_for_many_candidates(self) -> None:
        existing = [
            self._pr(70 + issue, f"fix/{issue}-existing", labels=("builder:codex",),
                     author="chatgpt-codex-connector[bot]", closing_issue=issue)
            for issue in (12, 13, 14)
        ]
        candidates = json.dumps([
            {
                "number": issue,
                "labels": [{"name": "tier:R"}, {"name": "builder:codex"},
                           {"name": "dispatched:codex"}],
                "assignees": [], "author": {"login": "owner"},
            }
            for issue in (12, 13, 14)
        ])
        with tempfile.TemporaryDirectory() as tmp:
            call_log = Path(tmp) / "gh-calls"
            completed, prompt, guard = self._run_codex_lane(
                "[]", existing_issue_prs=json.dumps(existing),
                candidate_issues=candidates, gh_call_log=call_log)
            calls = call_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("codex: nothing to do", completed.stdout)
        complete_listings = [
            call for call in calls
            if "--limit 1001" in call
            and "--json number,closingIssuesReferences,headRefName,headRefOid" in call
        ]
        self.assertEqual(len(complete_listings), 1, calls)
        self.assertEqual(prompt, "")
        self.assertEqual(guard, {})

    def test_runner_refuses_ambiguous_existing_pull_requests_for_the_issue(self) -> None:
        prs = [
            self._pr(77, "fix/12-original-title", labels=("builder:codex",)),
            self._pr(78, "fix/12-renamed-title", labels=("builder:codex",)),
        ]
        completed, prompt, guard = self._run_codex_lane(
            "[]", existing_issue_prs=json.dumps(prs), explicit_issue_target=True)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("multiple pull requests (77, 78) close it", completed.stderr)
        self.assertEqual(prompt, "")
        self.assertEqual(guard, {})

    def test_runner_refuses_an_existing_issue_pr_with_an_off_policy_branch(self) -> None:
        own = self._pr(77, "codex/12-original-title", labels=("builder:codex",),
                       author="chatgpt-codex-connector[bot]")
        completed, prompt, guard = self._run_codex_lane(
            "[]", existing_issue_prs=json.dumps([own]), explicit_issue_target=True)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("existing pull request #77 branch codex/12-original-title does not match",
                      completed.stderr)
        self.assertEqual(prompt, "")
        self.assertEqual(guard, {})

    def test_runner_refuses_a_foreign_existing_pull_request_for_the_issue(self) -> None:
        foreign = self._pr(77, "fix/12-original-title", labels=("builder:claude",),
                           author="claude[bot]")
        completed, prompt, guard = self._run_codex_lane(
            "[]", existing_issue_prs=json.dumps([foreign]),
            title_lookup="printf 'title lookup must not run\\n' >&2; exit 99",
            explicit_issue_target=True)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("pull request #77 already closes it but is owned by another builder",
                      completed.stderr)
        self.assertNotIn("title lookup must not run", completed.stderr)
        self.assertEqual(prompt, "")
        self.assertEqual(guard, {})

    def test_runner_refuses_a_stale_same_lane_pr_for_an_existing_policy_branch(self) -> None:
        branch = "fix/12-nv-accessible-label"
        stale = self._pr(77, branch, labels=("builder:codex",),
                         author="chatgpt-codex-connector[bot]", head="b" * 40)
        completed, prompt, guard = self._run_codex_lane(
            "[]", existing_branch=branch, existing_branch_head="c" * 40,
            existing_branch_prs=json.dumps([stale]))
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn(f"pull request #77 for policy branch {branch} does not point at current "
                      f"remote head {'c' * 40}", completed.stderr)
        self.assertEqual(prompt, "")
        self.assertEqual(guard, {})

    def test_runner_refuses_multiple_same_lane_prs_for_an_existing_policy_branch(self) -> None:
        branch = "fix/12-nv-accessible-label"
        prs = [
            self._pr(77, branch, labels=("builder:codex",)),
            self._pr(78, branch, labels=("builder:codex",)),
        ]
        completed, prompt, guard = self._run_codex_lane(
            "[]", existing_branch=branch, existing_branch_prs=json.dumps(prs))
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("with multiple pull requests (77, 78); ownership is ambiguous",
                      completed.stderr)
        self.assertEqual(prompt, "")
        self.assertEqual(guard, {})

    def _run_codex_fix_round(self, pr_json: dict, *, template: str | None = JIRA_TEMPLATE,
                             handoff_source: str | None = None, replay: bool = False,
                             ) -> tuple[subprocess.CompletedProcess, dict]:
        """Run the generated codex runner with ``--target pr:21`` against ``pr_json``."""
        runner, _text = self._generate(_config_with_policy(template))
        pr_view = {"headRefOid": "a" * 40, **pr_json}
        full_view = {"title": "Fix", "body": "Body", "url": "https://github.com/owner/repo/pull/21",
                     **pr_view}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            work_root = root / "work"
            (work_root / "codex" / "owner__repo" / ".git" / "hooks").mkdir(parents=True)
            fake_gh = bin_dir / "gh"
            fake_gh.write_text(
                _FAKE_GH_DELIVERY_HEADER
                + """if [ "$cmd" = "pr view" ] && [[ "$args" == *"--json headRefName,headRefOid,headRepository,labels,author"* ]]; then
  printf '%s\\n' '__PR_VIEW__'
elif [ "$cmd" = "pr view" ]; then
  printf '%s\\n' '__FULL_VIEW__'
elif [ "$cmd" = "repo view" ]; then
  printf 'main\\n'
elif [ "$cmd" = "api --paginate" ]; then
  printf '%s\\n' '[[]]'
else
  printf 'unexpected gh invocation: %s\\n' "$*" >&2
  exit 2
fi
""".replace("__PR_VIEW__", json.dumps(pr_view)).replace("__FULL_VIEW__", json.dumps(full_view)),
                encoding="utf-8",
            )
            fake_gh.write_text(fake_gh.read_text().replace("set -euo pipefail\n", "set -euo pipefail\n" + RUNNER_GH_BOUNDARY, 1))
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
            fake_git.write_text(fake_git.read_text().replace("set -euo pipefail\n", "set -euo pipefail\n" + RUNNER_GIT_BOUNDARY, 1))
            fake_git.chmod(0o755)
            fake_codex = bin_dir / "codex"
            fake_codex.write_text("#!/usr/bin/env bash\ncat >/dev/null\nprintf 'fake codex completed\\n'\n",
                                  encoding="utf-8")
            enable_fake_codex_sandbox(fake_codex)
            argv = [str(runner), "--lane", "codex", "--repo", "owner/repo",
                    "--max-minutes", "1", "--target", "pr:21"]
            if handoff_source is not None:
                argv.extend(["--handoff-source-lane", handoff_source,
                             "--handoff-expected-head", pr_view["headRefOid"],
                             "--handoff-source-file", str(fake_handoff_source(root))])
            fixture_env = runner_lineage_env(root, _config_with_policy(template), complete_pr(number=21,
                branch=pr_view['headRefName'], head=pr_view['headRefOid'],
                author=pr_view['author']['login'], labels=[v['name'] for v in pr_view['labels']]))
            # The complete snapshot is from the same exact declared external PR.
            text = fake_gh.read_text().replace('"state":"OPEN","labels":[]',
                '"state":"OPEN","labels":'+json.dumps(pr_view['labels'])+',"headRefName":'+json.dumps(pr_view['headRefName'])+',"author":'+json.dumps(pr_view['author']))
            text = text.replace('a'*40, pr_view['headRefOid'])
            fake_gh.write_text(text)
            completed = subprocess.run(
                argv,
                cwd=ROOT,
                env={
                    **os.environ,
                    "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root),
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    **_LANE_DELIVERY_ENV, **fixture_env,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            if replay:
                repeated = subprocess.run(argv, cwd=ROOT, env={**os.environ, "HOME": str(root),
                    "LANE_WORK_ROOT": str(work_root), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    **_LANE_DELIVERY_ENV, **fixture_env}, text=True, capture_output=True, check=False)
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                completed.stdout += repeated.stdout
                self.assertNotIn("fake codex completed", repeated.stdout)
                from code_mower.remote_session import FakeProvider, RemoteSessions
                state = root.resolve() / "remote-sessions"
                engine = RemoteSessions(state, FakeProvider(state / "fake-provider"))
                self.assertEqual(engine.run("status", "source")["counts"]["cancel"], 1)
            guard_path = work_root / "codex" / "owner__repo" / ".git" / "code-mower-lane-guard.json"
            guard = json.loads(guard_path.read_text(encoding="utf-8")) if guard_path.exists() else {}
        return completed, guard

    def test_fix_round_refuses_an_off_policy_target_even_with_the_lane_prefix(self) -> None:
        # codex/issue-12 carries this lane's prefix, label, and author, yet the
        # repository policy names fix/... as the only acceptable builder branch.
        # The prefix alone must not authorize the write.
        off_policy = self._pr(21, "codex/issue-12", labels=("builder:codex",),
                              author="chatgpt-codex-connector[bot]")
        completed, guard = self._run_codex_fix_round(off_policy)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("refusing target PR #21; head branch codex/issue-12 does not match the "
                      "owner/repo branch policy", completed.stderr)
        self.assertNotIn("fake codex completed", completed.stdout)
        self.assertEqual(guard, {})

    def test_fix_round_guards_exactly_the_policy_compliant_target(self) -> None:
        branch = "fix/12-nv-accessible-label"
        own = self._pr(21, branch, labels=("builder:codex",), author="chatgpt-codex-connector[bot]")
        completed, guard = self._run_codex_fix_round(own)
        self.assertNotIn("refusing", completed.stderr)
        self.assertIn("fake codex completed", completed.stdout)
        self.assertIn("target_pr_branch", guard, completed.stdout + completed.stderr)
        self.assertEqual(guard["target_pr_branch"], branch)
        self.assertEqual(guard["allowed_branch"], branch)
        self.assertEqual(guard["allowed_branch_expected_head"], "c" * 40)
        self.assertEqual(guard["allowed_prefixes"], [])
        # Provenance is still required on a policy-compliant target.
        for name, foreign in {
            "human": self._pr(21, branch),
            "foreign_builder": self._pr(21, branch, labels=("builder:claude",), author="claude[bot]"),
        }.items():
            with self.subTest(case=name):
                completed, guard = self._run_codex_fix_round(foreign)
                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                self.assertIn(f"head branch {branch} is not owned by this lane", completed.stderr)
                self.assertEqual(guard, {})

    def test_policy_compliant_handoff_withholds_destination_lane_prefixes(self) -> None:
        branch = "claude/12-accessible-label"
        source_owned = self._pr(21, branch, labels=("builder:claude",),
                                author="claude[bot]")
        completed, guard = self._run_codex_fix_round(
            source_owned, template="claude/{issue_key}-{slug}", handoff_source="claude")
        self.assertIn("accepted explicit handoff claude -> codex", completed.stdout)
        self.assertNotIn("refusing", completed.stderr)
        self.assertIn("target_pr_branch", guard, completed.stdout + completed.stderr)
        self.assertEqual(guard["target_pr_branch"], branch)
        self.assertEqual(guard["allowed_branch"], branch)
        self.assertEqual(guard["allowed_branch_expected_head"], "c" * 40)
        self.assertEqual(guard["allowed_prefixes"], [])
        self.assertEqual(guard["handoff"]["target_branch"], branch)
        self.assertEqual(guard["handoff"]["expected_head"], "c" * 40)

    def test_generated_devin_to_codex_takeover_and_replay_need_no_prefix_patch(self) -> None:
        source = self._pr(21, "devin/21-fix", labels=("builder:devin",),
                          author="devin-ai-integration[bot]")
        completed, guard = self._run_codex_fix_round(
            source, template=None, handoff_source="devin", replay=True)
        self.assertEqual(completed.stdout.count("accepted explicit handoff devin -> codex"), 1)
        self.assertEqual(completed.stdout.count("fake codex completed"), 1, completed.stdout + completed.stderr)
        self.assertIn("no repeated acceptance or writer launch", completed.stdout)
        self.assertEqual(guard["handoff"]["target_branch"], "devin/21-fix")
        self.assertEqual(guard["handoff"]["expected_head"], "c" * 40)

    def test_handoff_runner_without_ambient_tmpdir_keeps_guard_exit_and_replay_contracts(self) -> None:
        from unittest.mock import patch
        # CPython's temp root remains the approved fixture area; the child shell
        # reproduces Linux CI where TMPDIR is absent from the environment.
        tempfile.gettempdir()
        with patch.dict(os.environ):
            os.environ.pop('TMPDIR', None)
            self.test_policy_compliant_handoff_withholds_destination_lane_prefixes()
            self.test_generated_devin_to_codex_takeover_and_replay_need_no_prefix_patch()

    def test_fix_round_without_a_policy_keeps_the_lane_prefix_target(self) -> None:
        own = self._pr(21, "codex/issue-12", labels=("builder:codex",),
                       author="chatgpt-codex-connector[bot]")
        completed, guard = self._run_codex_fix_round(own, template=None)
        self.assertNotIn("refusing", completed.stderr)
        self.assertIn("fake codex completed", completed.stdout)
        self.assertEqual(guard["target_pr_branch"], "codex/issue-12")
        self.assertEqual(guard["allowed_branch"], "")
        self.assertEqual(guard["allowed_prefixes"], ["codex/"])

    def test_runner_refuses_a_resolved_branch_that_is_not_a_valid_git_ref(self) -> None:
        # {repo_name}/{issue_number} renders .github/12 for a repository named
        # .github: it matches the policy pattern yet no git ref may start a
        # component with a dot. The Python resolver refuses it; the runner must
        # refuse it too, before the guard is installed or a provider starts.
        template = "{repo_name}/{issue_number}"
        policy = branch_policy.compile_template(template)
        self.assertIsNotNone(re.fullmatch(policy.pattern, ".github/12"))
        with self.assertRaises(branch_policy.BranchPolicyError):
            branch_policy.resolve_branch(policy, lane="codex", issue_number=12,
                                         repository="owner/.github")
        completed, prompt, guard = self._run_codex_lane(
            "[]", template=template, repo="owner/.github")
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("resolved branch .github/12 is not a valid git branch name", completed.stderr)
        self.assertNotIn("fake codex completed", completed.stdout)
        self.assertEqual(prompt, "")
        self.assertEqual(guard, {})

    def test_runner_refuses_to_resolve_a_policy_branch_without_the_issue_title(self) -> None:
        # The slug is part of the branch identity. A transient title failure
        # that degraded into an empty slug would resolve fix/12 instead of
        # fix/12-nv-accessible-label, miss the PR an earlier run opened on the
        # real name, and deliver the issue twice. Both a failed and an empty
        # lookup must abort before the guard is installed or a provider starts.
        for name, lookup in (
            ("failed", "printf 'gh: HTTP 502\\n' >&2; exit 1"),
            ("empty", "printf '\\n'"),
        ):
            with self.subTest(lookup=name):
                completed, prompt, guard = self._run_codex_lane("[]", title_lookup=lookup)
                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                self.assertIn("refusing issue #12; could not read the issue title needed to "
                              "resolve the owner/repo policy branch", completed.stderr)
                self.assertNotIn("fix/12", completed.stderr)
                self.assertNotIn("fake codex completed", completed.stdout)
                self.assertEqual(prompt, "")
                self.assertEqual(guard, {})

    def test_runner_ref_validation_matches_the_python_resolver(self) -> None:
        _runner, text = self._generate(_config_with_policy())
        start = text.index("is_valid_ref() {")
        function = text[start:text.index("\n}\n", start) + 3]
        cases = {
            "fix/12-nv-accessible-label": True, "repo/12": True, "a.b/c_d-e": True,
            ".github/12": False, "fix/.hidden": False, "fix/12.": False, "fix/12.lock": False,
            "fix/12..13": False, "fix//12": False, "fix/12/": False, "fix/@{12}": False,
            "-fix/12": False, "fix/12 x": False, "fix/12~": False, "": False,
            "x" * 201: False, "x" * 200: True,
        }
        for branch, expected in cases.items():
            with self.subTest(branch=branch):
                self.assertEqual(branch_policy.is_valid_ref(branch), expected)
                probe = subprocess.run(
                    ["bash", "-c", function + '\nis_valid_ref "$1"', "probe", branch],
                    cwd=ROOT, text=True, capture_output=True, check=False,
                )
                self.assertEqual(probe.returncode == 0, expected, probe.stderr)

    def test_runner_without_policy_keeps_lane_prefix_write_authority(self) -> None:
        _runner, text = self._generate(_config_with_policy(None))
        self.assertIn(
            'allowed_prefixes: (if $mode == "audit" or $policy_branch != "" then [] else (.[$lane] // []) end)',
            text,
        )
        for path in (ROOT / "tools/lanes/run_mac_lane.sh",
                     ROOT / "templates/lanes/run_mac_lane.sh",
                     ROOT / "src/code_mower/templates/lanes/run_mac_lane.sh"):
            with self.subTest(path=path.name):
                self.assertIn('or $policy_branch != "" then []', path.read_text(encoding="utf-8"))

    def test_runner_embeds_provenance_for_every_configured_builder_lane(self) -> None:
        # cursor is a configured builder without a local runner. Its labels and
        # authors still take part in conflict detection; execution eligibility
        # (the lane case) stays limited to local lanes.
        _runner, text = self._generate(_config_with_policy())
        self.assertIn('case "$LANE" in codex|claude)', text)
        self.assertIn(
            """builder_labels_json='{"claude":"builder:claude","codex":"builder:codex","cursor":"builder:cursor","devin":"builder:devin"}'""",
            text,
        )
        provenance = next(row for row in text.splitlines() if row.startswith("provenance_labels_json="))
        self.assertEqual(
            json.loads(provenance[len("provenance_labels_json="):].strip("'")),
            {"builder:claude": "claude", "builder:codex": "codex",
             "builder:cursor": "cursor", "builder:devin": "devin", "builder:grok-bot": "cursor"},
        )
        authors = next(row for row in text.splitlines() if row.startswith("builder_authors_json="))
        self.assertEqual(
            json.loads(authors[len("builder_authors_json="):].strip("'")),
            {"chatgpt-codex-connector[bot]": "codex", "claude[bot]": "claude",
             "cursor[bot]": "cursor", "devin-ai-integration": "devin",
             "devin-ai-integration[bot]": "devin", "grok-bot[bot]": "cursor"},
        )
        # Inactive Devin execution still contributes its canonical source identity.
        self.assertIn("devin-ai-integration", text)

    def test_label_alone_or_author_alone_is_sufficient_lane_provenance(self) -> None:
        for own in (
            self._pr(77, "fix/12-nv-accessible-label", labels=("builder:codex",)),
            self._pr(77, "fix/12-nv-accessible-label", author="ChatGPT-Codex-Connector[bot]"),
        ):
            with self.subTest(pr=own):
                completed, _prompt, _guard = self._run_codex_lane(json.dumps([own]))
                self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_delivery_snapshot_ignores_pull_requests_without_this_lanes_provenance(self) -> None:
        branch = "fix/12-nv-accessible-label"
        cases = {
            "cross_builder_label": self._pr(77, branch, labels=("builder:claude",)),
            "cross_builder_author": self._pr(77, branch, author="claude[bot]"),
            "nonlocal_builder_label": self._pr(77, branch, labels=("builder:cursor",)),
            "nonlocal_builder_author": self._pr(77, branch, author="cursor[bot]"),
            "human": self._pr(77, branch),
            "conflicting_signals": self._pr(77, branch, labels=("builder:codex",), author="claude[bot]"),
            "conflict_with_nonlocal_author": self._pr(77, branch, labels=("builder:codex",), author="cursor[bot]"),
            "conflict_with_nonlocal_alias_label": self._pr(
                77, branch, labels=("builder:codex", "builder:grok-bot"), author="chatgpt-codex-connector[bot]"),
            "fork_head": self._pr(77, branch, labels=("builder:codex",), repo="fork/repo"),
            "other_policy_branch": self._pr(77, "fix/12-something-else", labels=("builder:codex",)),
            "lane_prefix_not_policy_branch": self._pr(77, "codex/issue-12", labels=("builder:codex",)),
        }
        for name, foreign in cases.items():
            with self.subTest(case=name):
                completed, _prompt, _guard = self._run_codex_lane(json.dumps([foreign]))
                self.assertEqual(completed.returncode, 3, completed.stderr)
                self.assertIn("no validated delivery for issue #12", completed.stderr)

    def test_delivery_snapshot_fails_closed_on_multiple_lane_candidates(self) -> None:
        branch = "fix/12-nv-accessible-label"
        listing = [self._pr(77, branch, labels=("builder:codex",)),
                   self._pr(78, branch, labels=("builder:codex",))]
        completed, _prompt, _guard = self._run_codex_lane(json.dumps(listing))
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("multiple pull requests carry the lane provenance", completed.stderr)


if __name__ == "__main__":
    unittest.main()
