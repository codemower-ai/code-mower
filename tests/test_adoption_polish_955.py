"""Adoption polish: effective review authority, superseded bridge, concise doctor."""

from __future__ import annotations
from lineage_consumer_fixtures import complete_pr

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower import devin_readiness, init as code_mower_init, migration, review_authority, session
from code_mower.doctor_checks.models import DoctorCheck, DoctorReport
from code_mower.doctor_checks.output import render_doctor_summary, render_doctor_text
from code_mower.yaml_subset import ConfigError


def _review_lane(**overrides):
    lane = {
        "type": "review",
        "driver": "claude_cli",
        "provider": "claude",
        "labels": {"needs": "needs-claude-audit", "done": "claude-audit-done", "blocked": "claude-audit-blocked"},
        "merge_authority": True,
        "informational": False,
    }
    lane.update(overrides)
    return lane


class EffectiveReviewAuthorityTests(unittest.TestCase):
    def test_starter_lane_keeps_maintained_merge_authority(self):
        payload = review_authority.review_authority("claude")
        self.assertTrue(payload["merge_authority"])
        self.assertEqual(payload["label"], "merge-authority lane")
        self.assertEqual(payload["policy_source"], "starter")
        self.assertEqual(payload["reason"], "lane_merge_authority")

    def test_informational_repository_lane_renders_informational(self):
        config = {
            "lanes": {
                "claude_audit": _review_lane(merge_authority=False, informational=True)
            }
        }
        payload = review_authority.review_authority("claude", config=config)
        self.assertFalse(payload["merge_authority"])
        self.assertEqual(payload["label"], "informational only")
        self.assertEqual(payload["policy_source"], "repository")
        self.assertEqual(payload["reason"], "lane_informational")
        self.assertEqual(payload["scope"], "informational")

    def test_qualified_lane_narrowed_by_denied_role_policy(self):
        config = {
            "lanes": {"claude_audit": _review_lane()},
            "role_policy": {"claude": {"reviewer": {"enabled": False}}},
        }
        payload = review_authority.review_authority("claude", config=config)
        self.assertTrue(payload["configured_merge_authority"])
        self.assertFalse(payload["merge_authority"])
        self.assertEqual(payload["reason"], "policy_denied")
        self.assertEqual(payload["label"], "informational only")

    def test_codex_lane_posture_is_read_per_product(self):
        config = {
            "lanes": {
                "codex": _review_lane(
                    driver="codex_cli",
                    provider="codex",
                    merge_authority=False,
                    informational=True,
                )
            }
        }
        self.assertFalse(review_authority.review_authority("codex", config=config)["merge_authority"])
        # An unconfigured lane for another product is unaffected.
        self.assertTrue(review_authority.review_authority("claude", config=config)["merge_authority"])

    def test_operator_override_is_reported_as_the_source(self):
        payload = review_authority.effective_merge_authority("claude", override=False)
        self.assertFalse(payload["merge_authority"])
        self.assertEqual(payload["policy_source"], "operator")
        self.assertEqual(payload["config_source"], "operator_override")
        self.assertEqual(payload["label"], "informational only")

    def test_session_rendering_uses_the_shared_label(self):
        payload = {
            "repo": "o/r",
            "host": "claude",
            "orchestrator": "claude",
            "status": "prepared",
            "participants": [
                {
                    "id": "claude",
                    "name": "Claude Code",
                    "builder": None,
                    "note": "",
                    "reviewer": {
                        "lane": "claude_audit",
                        "merge_authority": False,
                        "informational": True,
                        "policy_source": "repository",
                        "readiness": "unchecked",
                    },
                }
            ],
            "instructions": [],
        }
        text = session.render_session(payload)
        self.assertIn("reviewer: claude_audit (informational lane)", text)
        self.assertNotIn("merge-authority", text)


class HistoricalFixtureTests(unittest.TestCase):
    """Recorded wording stays readable without becoming the configured posture."""

    def test_recorded_header_is_not_a_configured_posture_claim(self):
        recorded = "## Claude audit (merge-authority lane)\n\nHead SHA: `abc`\n"
        self.assertIn(review_authority.MERGE_AUTHORITY_LABEL, recorded)
        config = {
            "lanes": {
                "claude_audit": _review_lane(merge_authority=False, informational=True)
            }
        }
        current = review_authority.review_authority("claude", config=config)
        self.assertEqual(current["label"], review_authority.INFORMATIONAL_LABEL)

    def test_labels_are_stable_strings(self):
        self.assertEqual(review_authority.MERGE_AUTHORITY_LABEL, "merge-authority lane")
        self.assertEqual(review_authority.INFORMATIONAL_LABEL, "informational only")
        self.assertEqual(review_authority.SESSION_INFORMATIONAL_LABEL, "informational lane")


class NonWideningOverrideTests(unittest.TestCase):
    """A positive flag or environment value can never widen computed authority."""

    def _config(self, path: Path, body: str) -> Path:
        path.write_text(body, encoding="utf-8")
        return path

    def setUp(self):
        import tempfile

        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def test_positive_override_cannot_widen_an_informational_lane(self):
        config = self._config(
            self.root / "informational.yml",
            "version: 1\n"
            "lanes:\n"
            "  claude_audit:\n"
            "    type: review\n"
            "    driver: local_cli\n"
            "    provider: claude\n"
            "    merge_authority: false\n"
            "    informational: true\n"
            "    labels:\n"
            "      needs: needs-claude-audit\n"
            "      done: claude-audit-done\n"
            "      blocked: claude-audit-blocked\n",
        )
        payload = review_authority.effective_merge_authority(
            "claude", config_path=config, override=True
        )
        self.assertFalse(payload["merge_authority"])
        self.assertEqual(payload["label"], "informational only")
        self.assertEqual(payload["reason"], "lane_informational")
        self.assertTrue(payload["override_ignored"])
        self.assertEqual(payload["policy_source"], "repository")

    def test_positive_override_cannot_widen_a_denied_role_policy(self):
        config = self._config(
            self.root / "denied.yml",
            "version: 1\n"
            "role_policy:\n"
            "  codex:\n"
            "    reviewer:\n"
            "      enabled: false\n",
        )
        payload = review_authority.effective_merge_authority(
            "codex", config_path=config, override=True
        )
        self.assertFalse(payload["merge_authority"])
        self.assertEqual(payload["reason"], "policy_denied")
        self.assertTrue(payload["override_ignored"])

    def test_positive_override_is_honoured_when_the_configuration_agrees(self):
        payload = review_authority.effective_merge_authority("claude", override=True)
        self.assertTrue(payload["merge_authority"])
        self.assertEqual(payload["policy_source"], "operator")
        self.assertEqual(payload["reason"], "operator_override")
        self.assertNotIn("override_ignored", payload)

    def test_negative_override_still_narrows_a_merge_authority_lane(self):
        payload = review_authority.effective_merge_authority("codex", override=False)
        self.assertFalse(payload["merge_authority"])
        self.assertEqual(payload["scope"], "informational")
        self.assertEqual(payload["policy_source"], "operator")

    def test_override_does_not_skip_an_explicitly_missing_configuration(self):
        for override in (True, False):
            with self.subTest(override=override):
                with self.assertRaises(ConfigError):
                    review_authority.effective_merge_authority(
                        "claude",
                        config_path=self.root / "absent.yml",
                        override=override,
                    )


class RepositoryConfigSelectionTests(unittest.TestCase):
    def test_explicit_missing_config_is_an_error_not_a_starter_fallback(self):
        with self.assertRaises(ConfigError):
            review_authority.resolve_repository_config(config_path="no-such-config.yml")

    def test_checkout_without_config_falls_back_to_maintained_defaults(self):
        config, source = review_authority.resolve_repository_config(
            repo_root=Path(__file__).resolve().parent / "does-not-exist"
        )
        self.assertIsNone(config)
        self.assertEqual(source, "packaged_default")


class TrustedBaseAuthorityTests(unittest.TestCase):
    """Implicit discovery reads active policy, not the change under review."""

    LANE = (
        "version: 1\n"
        "lanes:\n"
        "  claude_audit:\n"
        "    type: review\n"
        "    driver: local_cli\n"
        "    provider: claude\n"
        "    merge_authority: {authority}\n"
        "    informational: {informational}\n"
        "    labels:\n"
        "      needs: needs-claude-audit\n"
        "      done: claude-audit-done\n"
        "      blocked: claude-audit-blocked\n"
    )

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        )

    def setUp(self):
        import tempfile

        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self._git("init", "--initial-branch", "main")
        self._git("config", "user.email", "lane@example.invalid")
        self._git("config", "user.name", "Lane")
        self._git("config", "commit.gpgsign", "false")

    def _commit(self, body: str, message: str) -> None:
        (self.root / "code-mower.yml").write_text(body, encoding="utf-8")
        self._git("add", "code-mower.yml")
        self._git("commit", "-m", message)

    def test_a_pr_promoting_its_own_lane_reports_the_base_policy(self):
        self._commit(
            self.LANE.format(authority="false", informational="true"), "base policy"
        )
        # The checkout is the PR head, which proposes merge authority.
        (self.root / "code-mower.yml").write_text(
            self.LANE.format(authority="true", informational="false"), encoding="utf-8"
        )
        payload = review_authority.effective_merge_authority(
            "claude", repo_root=self.root, base_ref="main"
        )
        self.assertFalse(payload["merge_authority"])
        self.assertEqual(payload["config_source"], "trusted_base_config")
        self.assertEqual(payload["reason"], "lane_informational")

    def test_a_pr_demoting_its_own_lane_also_reports_the_base_policy(self):
        self._commit(
            self.LANE.format(authority="true", informational="false"), "base policy"
        )
        (self.root / "code-mower.yml").write_text(
            self.LANE.format(authority="false", informational="true"), encoding="utf-8"
        )
        payload = review_authority.effective_merge_authority(
            "claude", repo_root=self.root, base_ref="main"
        )
        self.assertTrue(payload["merge_authority"])
        self.assertEqual(payload["config_source"], "trusted_base_config")

    def test_a_base_without_a_configuration_keeps_the_maintained_default(self):
        (self.root / "README.md").write_text("x\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "no config")
        (self.root / "code-mower.yml").write_text(
            self.LANE.format(authority="false", informational="true"), encoding="utf-8"
        )
        config, source = review_authority.resolve_repository_config(
            repo_root=self.root, base_ref="main"
        )
        self.assertIsNone(config)
        self.assertEqual(source, "packaged_default")

    def test_unavailable_discovery_never_falls_back_to_the_head_checkout(self):
        self._commit(
            self.LANE.format(authority="false", informational="true"), "base policy"
        )
        (self.root / "code-mower.yml").write_text(
            self.LANE.format(authority="true", informational="false"), encoding="utf-8"
        )
        config, source = review_authority.resolve_repository_config(
            repo_root=self.root, base_ref="refs/heads/no-such-base"
        )
        self.assertIsNone(config)
        self.assertEqual(source, review_authority.TRUSTED_BASE_UNAVAILABLE)

    def test_a_missing_base_ref_never_grants_the_maintained_default(self):
        # A base that could not be read proves nothing. Reporting the starter's
        # defaults would claim merge authority no trusted policy supports.
        self._commit(
            self.LANE.format(authority="true", informational="false"), "base policy"
        )
        payload = review_authority.effective_merge_authority(
            "codex", repo_root=self.root, base_ref="refs/heads/no-such-base"
        )
        self.assertFalse(payload["merge_authority"])
        self.assertFalse(payload["configured_merge_authority"])
        self.assertEqual(
            payload["config_source"], review_authority.TRUSTED_BASE_UNAVAILABLE
        )
        self.assertEqual(payload["reason"], review_authority.TRUSTED_BASE_UNAVAILABLE)
        self.assertEqual(payload["label"], review_authority.INFORMATIONAL_LABEL)
        self.assertTrue(payload["action"])

    def test_malformed_tracked_configuration_is_unavailable_not_default(self):
        # Tracked, but not a configuration mapping at all.
        self._commit("- not\n- a mapping\n", "malformed base policy")
        config, source = review_authority.resolve_repository_config(
            repo_root=self.root, base_ref="main"
        )
        self.assertIsNone(config)
        self.assertEqual(source, review_authority.TRUSTED_BASE_UNAVAILABLE)

    def test_a_failed_git_lookup_is_unavailable_not_default(self):
        self._commit(
            self.LANE.format(authority="true", informational="false"), "base policy"
        )
        broken = self.root / "broken"
        broken.mkdir()
        (broken / ".git").write_text("not a git dir\n", encoding="utf-8")
        config, source = review_authority.resolve_repository_config(
            repo_root=broken, base_ref="main"
        )
        self.assertIsNone(config)
        self.assertEqual(source, review_authority.TRUSTED_BASE_UNAVAILABLE)

    def test_a_positive_override_cannot_widen_an_unavailable_base(self):
        self._commit(
            self.LANE.format(authority="true", informational="false"), "base policy"
        )
        payload = review_authority.effective_merge_authority(
            "claude",
            repo_root=self.root,
            base_ref="refs/heads/no-such-base",
            override=True,
        )
        self.assertFalse(payload["merge_authority"])
        self.assertTrue(payload["override_ignored"])

    def test_verified_absence_is_distinguished_from_unavailability(self):
        # Proving the base tracks no configuration is evidence; it selects the
        # maintained defaults, and nothing else in this class does.
        (self.root / "README.md").write_text("x\n", encoding="utf-8")
        self._git("add", "README.md")
        self._git("commit", "-m", "no config")
        _, absent = review_authority.resolve_repository_config(
            repo_root=self.root, base_ref="main"
        )
        _, unavailable = review_authority.resolve_repository_config(
            repo_root=self.root, base_ref="refs/heads/no-such-base"
        )
        self.assertEqual(absent, "packaged_default")
        self.assertEqual(unavailable, review_authority.TRUSTED_BASE_UNAVAILABLE)

    def test_an_explicit_selection_still_wins_over_the_trusted_base(self):
        self._commit(
            self.LANE.format(authority="true", informational="false"), "base policy"
        )
        selected = self.root / "selected.yml"
        selected.write_text(
            self.LANE.format(authority="false", informational="true"), encoding="utf-8"
        )
        payload = review_authority.effective_merge_authority(
            "claude", config_path=selected, repo_root=self.root, base_ref="main"
        )
        self.assertFalse(payload["merge_authority"])
        self.assertEqual(payload["config_source"], "explicit_repository_config")


class FetchedBaseAuthorityTests(unittest.TestCase):
    """Both wrappers render the posture of the base revision they fetched.

    The local base ref can be stale or absent when a wrapper starts. Resolving
    then reports the policy the audit's own fetch is about to replace, while the
    review compares against the refreshed revision -- so a repository demotion
    would keep merge-authority wording, and a base that is merely not fetched yet
    would report as unavailable. These drive the real entry points with offline
    fakes so the ordering, not just the resolver, is covered.
    """

    LANES = (
        "version: 1\n"
        "project:\n  name: fixture\n  state_dir: .code-mower\n"
        "repositories:\n  - slug: owner/repo\n    default_branch: main\n"
        "lanes:\n"
        "  claude_audit:\n"
        "    type: review\n"
        "    driver: local_cli\n"
        "    provider: claude\n"
        "    merge_authority: {authority}\n"
        "    informational: {informational}\n"
        "    labels:\n"
        "      needs: needs-claude-audit\n"
        "      done: claude-audit-done\n"
        "      blocked: claude-audit-blocked\n"
        "  codex:\n"
        "    type: audit\n"
        "    driver: local_cli\n"
        "    provider: codex\n"
        "    merge_authority: {authority}\n"
        "    informational: {informational}\n"
        "    labels:\n"
        "      needs: needs-codex-audit\n"
        "      done: codex-audit-done\n"
        "      blocked: codex-audit-blocked\n"
    )
    AUTHORITATIVE = LANES.format(authority="true", informational="false")
    DEMOTED = LANES.format(authority="false", informational="true")

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    def setUp(self):
        import shutil
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self._git("init", "--initial-branch", "main")
        self._git("config", "user.email", "lane@example.invalid")
        self._git("config", "user.name", "Lane")
        self._git("config", "commit.gpgsign", "false")

    def _commit(self, body: str, message: str) -> str:
        (self.repo / "code-mower.yml").write_text(body, encoding="utf-8")
        self._git("add", "code-mower.yml")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _stale_demotion(self) -> None:
        """Leave `origin/main` on the authoritative policy the remote demoted."""
        self._commit(self.AUTHORITATIVE, "authoritative policy")
        self._git("update-ref", "refs/remotes/origin/main", "main")
        self._commit(self.DEMOTED, "demote the review lane")
        # The PR head under review keeps declaring merge authority, so reading
        # the checkout instead of the fetched base would also be wrong.
        (self.repo / "code-mower.yml").write_text(self.AUTHORITATIVE, encoding="utf-8")

    def _fetch_effect(self):
        """Advance `origin/main` the way the wrapper's real fetch would."""

        def effect(*args, **kwargs):
            self._git("update-ref", "refs/remotes/origin/main", "main")
            return self._git("rev-parse", "main")

        return effect

    def _request(self, product: str, override=None):
        return review_authority.AuthorityRequest(product=product, override=override)

    def _run_codex(self, config):
        from code_mower import codex_audit_pr as cap

        worktree = self.tmp / "worktree"
        worktree.mkdir(exist_ok=True)
        head = "d" * 40
        pr_payload = {"head": {"sha": head, "ref": "human/fix"}, "title": "Fix"}
        pr_payload = complete_pr(pr_payload)
        parsed = cap.CodexVerdict(verdict="PASS", prose="Summary:\n\nNone.")
        diagnostics = cap.ReviewContextDiagnostics(
            base_ref=config.base_ref,
            head_sha=head,
            changed_file_count=1,
            diff_bytes=128,
            requested_max_bytes=config.max_diff_bytes,
            hard_limit_bytes=(
                config.max_diff_hard_limit_bytes
                or cap.DEFAULT_MAX_DIFF_HARD_LIMIT_BYTES
            ),
            included_diff_bytes=128,
            effective_budget_usd=config.max_budget_usd or cap.DEFAULT_MAX_BUDGET_USD,
        )
        with (
            mock.patch.object(cap, "fetch_issue_comments", return_value=[]),
            mock.patch.dict(
                "os.environ",
                {
                    "PYTEST_CURRENT_TEST": "",
                    "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(self.tmp / "verdicts"),
                    "GITHUB_RUN_ID": "",
                },
            ),
            mock.patch.object(cap, "fetch_pull_request", side_effect=[pr_payload] * 2),
            mock.patch.object(cap, "preflight_codex_cli", return_value="codex-test"),
            mock.patch.object(cap, "_discover_venv", return_value=None),
            mock.patch.object(cap, "_fetch_pr_head"),
            mock.patch.object(cap, "_fetch_base_ref", side_effect=self._fetch_effect()),
            mock.patch.object(
                cap, "_build_review_context_diagnostics", return_value=diagnostics
            ),
            mock.patch.object(cap, "_create_temp_worktree", return_value=worktree),
            mock.patch.object(cap, "_remove_worktree"),
            mock.patch.object(cap, "run_codex_review", return_value=("review", "")),
            mock.patch.object(
                cap,
                "run_codex_verdict_structuring",
                return_value=(parsed, '{"structured_output":"pass"}', ""),
            ),
            mock.patch.object(cap, "post_pr_comment", return_value={"html_url": "u"}),
        ):
            return cap.audit_pr(config, "owner/repo", 42)

    def _codex_config(self, **kwargs):
        from code_mower import codex_audit_pr as cap

        return cap.AuditConfig(
            "token",
            {"owner/repo": self.repo},
            include_plan_context=False,
            include_decision_context=False,
            **{"merge_authority": False, **kwargs},
        )

    def test_codex_drops_authority_when_the_fetch_brings_a_demotion(self):
        # `merge_authority=True` stands in for a posture resolved against the
        # stale local ref: the fetched base demoted the lane, so the comment the
        # audit renders must not keep that wording.
        self._stale_demotion()
        result = self._run_codex(
            self._codex_config(
                merge_authority=True, authority_request=self._request("codex")
            )
        )
        self.assertIn(review_authority.INFORMATIONAL_LABEL, result.comment_body)
        self.assertNotIn(review_authority.MERGE_AUTHORITY_LABEL, result.comment_body)

    def test_codex_reports_a_base_that_only_the_fetch_made_available(self):
        # Nothing named `origin/main` locally yet: resolving before the fetch
        # would report the base unavailable for a repository that has a policy.
        self._commit(self.AUTHORITATIVE, "authoritative policy")
        result = self._run_codex(self._codex_config(authority_request=self._request("codex")))
        self.assertIn(review_authority.MERGE_AUTHORITY_LABEL, result.comment_body)

    def test_codex_without_a_request_keeps_the_authority_it_was_given(self):
        # Direct callers and recorded fixtures decided authority themselves.
        self._stale_demotion()
        result = self._run_codex(self._codex_config(merge_authority=True))
        self.assertIn(review_authority.MERGE_AUTHORITY_LABEL, result.comment_body)

    def test_codex_positive_override_cannot_widen_the_fetched_demotion(self):
        self._stale_demotion()
        result = self._run_codex(
            self._codex_config(
                merge_authority=True,
                authority_request=self._request("codex", override=True),
            )
        )
        self.assertIn(review_authority.INFORMATIONAL_LABEL, result.comment_body)

    def _run_claude(self, config):
        from code_mower import claude_audit_pr as cap

        head = "d" * 40
        pr_payload = {"head": {"sha": head, "ref": "human/fix"}, "title": "Fix"}
        pr_payload = complete_pr(pr_payload)
        parsed = cap.ClaudeVerdict(verdict="PASS", prose="Summary:\n\nNone.")
        advance = self._fetch_effect()

        def build_diff_context(*args, **kwargs):
            # Stands in for the real builder, which fetches the base and pins the
            # revision it diffed against onto the context it returns.
            return cap.DiffContext(
                "stat", "diff", ("src/app.py",), False, 1000, 1000, 40, 40,
                fetched_base_ref=advance(),
            )

        with (
            mock.patch.object(cap, "fetch_issue_comments", return_value=[]),
            mock.patch.dict(
                "os.environ",
                {
                    "PYTEST_CURRENT_TEST": "",
                    "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(self.tmp / "verdicts"),
                    "GITHUB_RUN_ID": "",
                },
            ),
            mock.patch.object(cap, "fetch_pull_request", side_effect=[pr_payload] * 2),
            mock.patch.object(cap, "_build_diff_context", side_effect=build_diff_context),
            mock.patch.object(cap.code_mower_prompts, "load_review_prompt", return_value=""),
            mock.patch.object(
                cap,
                "run_claude_audit",
                return_value=(parsed, '{"structured_output":"pass"}', ""),
            ),
            mock.patch.object(cap, "post_pr_comment", return_value={"html_url": "u"}),
        ):
            return cap.audit_pr(config, "owner/repo", 42)

    def _claude_config(self, **kwargs):
        from code_mower import claude_audit_pr as cap

        return cap.ClaudeAuditConfig(
            "token",
            {"owner/repo": self.repo},
            include_plan_context=False,
            include_decision_context=False,
            **{"merge_authority": False, **kwargs},
        )

    def test_claude_drops_authority_when_the_diff_base_carries_a_demotion(self):
        self._stale_demotion()
        result = self._run_claude(
            self._claude_config(
                merge_authority=True, authority_request=self._request("claude")
            )
        )
        self.assertIn(review_authority.INFORMATIONAL_LABEL, result.comment_body)
        self.assertNotIn(review_authority.MERGE_AUTHORITY_LABEL, result.comment_body)

    def test_claude_reports_a_base_that_only_the_fetch_made_available(self):
        self._commit(self.AUTHORITATIVE, "authoritative policy")
        result = self._run_claude(
            self._claude_config(authority_request=self._request("claude"))
        )
        self.assertIn(review_authority.MERGE_AUTHORITY_LABEL, result.comment_body)

    def test_claude_without_a_request_keeps_the_authority_it_was_given(self):
        self._stale_demotion()
        result = self._run_claude(self._claude_config(merge_authority=True))
        self.assertIn(review_authority.MERGE_AUTHORITY_LABEL, result.comment_body)

    def test_the_posture_and_the_review_consume_the_same_fetched_revision(self):
        # The rendered posture must match the policy at the exact revision the
        # diff was taken from, not the checkout and not the pre-fetch ref.
        self._stale_demotion()
        fetched = self._git("rev-parse", "main")
        at_fetched = review_authority.effective_merge_authority(
            "claude", repo_root=self.repo, base_ref=fetched
        )
        self.assertFalse(at_fetched["merge_authority"])
        at_head = review_authority.effective_merge_authority(
            "claude", config_path=self.repo / "code-mower.yml"
        )
        self.assertTrue(at_head["merge_authority"])
        result = self._run_claude(
            self._claude_config(
                merge_authority=True, authority_request=self._request("claude")
            )
        )
        self.assertIn(at_fetched["label"], result.comment_body)
        self.assertNotIn(at_head["label"], result.comment_body)

    def test_a_historical_diff_context_records_no_fetched_revision(self):
        from code_mower import claude_audit_pr as cap

        context = cap.DiffContext("stat", "diff", (), False, 1, 1, 1, 1)
        self.assertEqual(context.fetched_base_ref, "")
        self.assertEqual(tuple(context), ("stat", "diff", False))


    # Every consumer below the fetch reads the revision the audit fetched.
    #
    # Resolving the posture against the fetched revision is not enough on its
    # own: `base_ref` stays a mutable name, so the trusted-ref lookups, the
    # review context and the review itself could still resolve it again later
    # and read a different commit. The tests below advance `origin/main` *after*
    # the fetch -- the way an upstream merge landing mid-review would -- and
    # prove the rendered posture and the downstream review and context all stay
    # on the one fetched snapshot.

    def _advance_the_tracking_ref(self) -> str:
        """Land a re-promotion on `origin/main` after the audit fetched it."""

        self._commit(self.AUTHORITATIVE, "re-promote the review lane upstream")
        self._git("update-ref", "refs/remotes/origin/main", "main")
        return self._git("rev-parse", "main")

    def test_codex_review_and_context_stay_on_the_fetched_revision(self):
        from code_mower import codex_audit_pr as cap

        self._stale_demotion()
        observed: dict[str, str] = {}
        worktree = self.tmp / "worktree"
        worktree.mkdir(exist_ok=True)
        head = "d" * 40
        pr_payload = {"head": {"sha": head, "ref": "human/fix"}, "title": "Fix"}
        pr_payload = complete_pr(pr_payload)
        parsed = cap.CodexVerdict(verdict="PASS", prose="Summary:\n\nNone.")

        def prepare(**kwargs):
            observed["context"] = kwargs["base_ref"]
            # Upstream moves on while this review runs. Anything that resolves
            # the name again from here reads the re-promotion, not the fetch.
            observed["moved_to"] = self._advance_the_tracking_ref()
            return None

        def diagnostics(local_repo, **kwargs):
            observed["diagnostics"] = kwargs["base_ref"]
            return cap.ReviewContextDiagnostics(
                base_ref=kwargs["base_ref"],
                head_sha=head,
                changed_file_count=1,
                diff_bytes=128,
                included_diff_bytes=128,
            )

        def review(config, *args, **kwargs):
            observed["review"] = config.base_ref
            return ("review", "")

        config = self._codex_config(
            merge_authority=True, authority_request=self._request("codex")
        )
        with (
            mock.patch.object(cap, "fetch_issue_comments", return_value=[]),
            mock.patch.dict(
                "os.environ",
                {
                    "PYTEST_CURRENT_TEST": "",
                    "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(self.tmp / "verdicts"),
                    "GITHUB_RUN_ID": "",
                },
            ),
            mock.patch.object(cap, "fetch_pull_request", side_effect=[pr_payload] * 2),
            mock.patch.object(cap, "preflight_codex_cli", return_value="codex-test"),
            mock.patch.object(cap, "_discover_venv", return_value=None),
            mock.patch.object(cap, "_fetch_pr_head"),
            mock.patch.object(cap, "_fetch_base_ref", side_effect=self._fetch_effect()),
            mock.patch.object(cap.context_audit, "prepare", side_effect=prepare),
            mock.patch.object(
                cap, "_build_review_context_diagnostics", side_effect=diagnostics
            ),
            mock.patch.object(cap, "_create_temp_worktree", return_value=worktree),
            mock.patch.object(cap, "_remove_worktree"),
            mock.patch.object(cap, "run_codex_review", side_effect=review),
            mock.patch.object(
                cap,
                "run_codex_verdict_structuring",
                return_value=(parsed, '{"structured_output":"pass"}', ""),
            ),
            mock.patch.object(cap, "post_pr_comment", return_value={"html_url": "u"}),
        ):
            result = cap.audit_pr(config, "owner/repo", 42)

        fetched = self._git("rev-parse", "refs/remotes/origin/main~1")
        self.assertNotEqual(observed["moved_to"], fetched)
        for consumer in ("context", "diagnostics", "review"):
            self.assertEqual(observed[consumer], fetched, consumer)
        # The named ref would have read the re-promotion instead.
        self.assertNotIn("origin/main", observed.values())
        self.assertIn(review_authority.INFORMATIONAL_LABEL, result.comment_body)

    def test_claude_review_and_context_stay_on_the_fetched_revision(self):
        from code_mower import claude_audit_pr as cap

        self._stale_demotion()
        observed: dict[str, str] = {}
        head = "d" * 40
        pr_payload = {"head": {"sha": head, "ref": "human/fix"}, "title": "Fix"}
        pr_payload = complete_pr(pr_payload)
        parsed = cap.ClaudeVerdict(verdict="PASS", prose="Summary:\n\nNone.")
        advance = self._fetch_effect()

        def build_diff_context(*args, **kwargs):
            return cap.DiffContext(
                "stat", "diff", ("src/app.py",), False, 1000, 1000, 40, 40,
                fetched_base_ref=advance(),
            )

        def prepare(**kwargs):
            observed["context"] = kwargs["base_ref"]
            observed["moved_to"] = self._advance_the_tracking_ref()
            return None

        def load_review_prompt(*args, **kwargs):
            observed["doctrine"] = kwargs["trusted_git_ref"]
            return ""

        def audit(config, prompt):
            observed["review"] = config.base_ref
            observed["prompt_names"] = config.base_ref in prompt
            return (parsed, '{"structured_output":"pass"}', "")

        config = self._claude_config(
            merge_authority=True, authority_request=self._request("claude")
        )
        with (
            mock.patch.object(cap, "fetch_issue_comments", return_value=[]),
            mock.patch.dict(
                "os.environ",
                {
                    "PYTEST_CURRENT_TEST": "",
                    "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(self.tmp / "verdicts"),
                    "GITHUB_RUN_ID": "",
                },
            ),
            mock.patch.object(cap, "fetch_pull_request", side_effect=[pr_payload] * 2),
            mock.patch.object(cap, "_build_diff_context", side_effect=build_diff_context),
            mock.patch.object(cap.context_audit, "prepare", side_effect=prepare),
            mock.patch.object(
                cap.code_mower_prompts,
                "load_review_prompt",
                side_effect=load_review_prompt,
            ),
            mock.patch.object(cap, "run_claude_audit", side_effect=audit),
            mock.patch.object(cap, "post_pr_comment", return_value={"html_url": "u"}),
        ):
            result = cap.audit_pr(config, "owner/repo", 42)

        fetched = self._git("rev-parse", "refs/remotes/origin/main~1")
        self.assertNotEqual(observed["moved_to"], fetched)
        for consumer in ("context", "doctrine", "review"):
            self.assertEqual(observed[consumer], fetched, consumer)
        self.assertTrue(observed["prompt_names"])
        self.assertIn(review_authority.INFORMATIONAL_LABEL, result.comment_body)

    def test_a_force_push_race_still_renders_the_fetched_base(self):
        from code_mower import claude_audit_pr as cap

        self._stale_demotion()
        fetched = self._git("rev-parse", "main")
        head = "d" * 40
        pr_payload = {"head": {"sha": head, "ref": "human/fix"}, "title": "Fix"}
        pr_payload = complete_pr(pr_payload)
        advance = self._fetch_effect()

        def build_diff_context(*args, **kwargs):
            # The base is fetched before the head mismatch is detected, so the
            # stale notice knows which revision the audit had already taken.
            raise cap._FetchedHeadMismatchWithBase(head, "e" * 40, advance())

        config = self._claude_config(
            merge_authority=True, authority_request=self._request("claude")
        )
        with (
            mock.patch.object(cap, "fetch_issue_comments", return_value=[]),
            mock.patch.dict(
                "os.environ",
                {
                    "PYTEST_CURRENT_TEST": "",
                    "CODE_MOWER_VERDICT_ARTIFACT_DIR": str(self.tmp / "verdicts"),
                    "GITHUB_RUN_ID": "",
                },
            ),
            mock.patch.object(cap, "fetch_pull_request", side_effect=[pr_payload] * 2),
            mock.patch.object(cap, "_build_diff_context", side_effect=build_diff_context),
            mock.patch.object(cap, "post_pr_comment", return_value={"html_url": "u"}),
        ):
            result = cap.audit_pr(config, "owner/repo", 42)

        self.assertEqual(result.verdict, "STALE")
        self.assertIn(review_authority.INFORMATIONAL_LABEL, result.comment_body)
        self.assertNotIn(review_authority.MERGE_AUTHORITY_LABEL, result.comment_body)
        self.assertTrue(fetched)

    def test_the_mismatch_stays_catchable_as_the_shared_exception(self):
        from code_mower import claude_audit_pr as cap
        from code_mower.provider_runners import FetchedHeadMismatch

        error = cap._FetchedHeadMismatchWithBase("a" * 40, "b" * 40, "c" * 40)
        self.assertIsInstance(error, FetchedHeadMismatch)
        self.assertEqual(error.expected_sha, "a" * 40)
        self.assertEqual(error.actual_sha, "b" * 40)
        self.assertEqual(error.fetched_base_ref, "c" * 40)


class PortableStarterCommandTests(unittest.TestCase):
    """The packaged starter has no repository path a rendered command can pin."""

    STARTER = devin_readiness.PACKAGED_STARTER_SOURCE
    # Stands in for the installation-specific path the starter resolves to at
    # runtime; the literal prefix is assembled the way privacy_scan.py writes its
    # own patterns.
    INSTALLED = "/" + "opt/venv/lib/code_mower/templates/code-mower.example.yml"

    def test_starter_doctor_command_uses_the_supported_selector(self):
        command = devin_readiness.doctor_command(
            config_path=self.INSTALLED,
            profile="recommended",
            config_source=self.STARTER,
            devin=True,
        )
        self.assertEqual(
            command,
            "`code-mower doctor --packaged-starter --profile recommended --devin`",
        )
        self.assertNotIn(self.INSTALLED, command)

    def test_starter_transport_selection_is_portable_and_still_staged(self):
        steps = devin_readiness.select_transport_command(
            "devin_api_v3",
            config_path=self.INSTALLED,
            profile="recommended",
            config_source=self.STARTER,
        )
        self.assertNotIn(self.INSTALLED, steps)
        self.assertIn(
            "code-mower init --packaged-starter --profile recommended "
            "--set-transport devin=devin_api_v3 --dry-run",
            steps,
        )
        self.assertIn("--apply --output-dir", steps)

    def test_starter_verification_inspects_the_installed_configuration(self):
        # Preview and staging read the package resource, but an install writes the
        # repository's own configuration and leaves the starter unchanged, so the
        # final check must select the installed file at the same profile.
        steps = devin_readiness.select_transport_command(
            "devin_api_v3",
            config_path=self.INSTALLED,
            profile="advanced",
            config_source=self.STARTER,
        )
        preview, _, verification = steps.partition("install them through")
        self.assertIn(
            "code-mower init --packaged-starter --profile advanced "
            "--set-transport devin=devin_api_v3 --dry-run",
            preview,
        )
        self.assertIn("--packaged-starter --profile advanced --set-transport", preview)
        self.assertIn(
            "`code-mower doctor code-mower.yml --profile advanced --devin`",
            verification,
        )
        self.assertNotIn("--packaged-starter", verification)
        self.assertNotIn(self.INSTALLED, steps)

    def test_repository_verification_keeps_the_configuration_it_installs_over(self):
        # A repository finding installs over its own file, so nothing redirects.
        steps = devin_readiness.select_transport_command(
            "devin_api_v3", config_path="ops/mower.yml", profile="recommended"
        )
        _, _, verification = steps.partition("install them through")
        self.assertIn(
            "`code-mower doctor ops/mower.yml --profile recommended --devin`",
            verification,
        )
        self.assertNotIn("code-mower.yml", verification)

    def test_the_installed_configuration_path_matches_what_init_writes(self):
        self.assertEqual(
            devin_readiness.INSTALLED_CONFIG_PATH, code_mower_init.ADOPTION_CONFIG_PATH
        )

    def test_repository_configuration_is_never_replaced_by_the_starter(self):
        repository = "code-mower.yml"
        steps = devin_readiness.select_transport_command(
            "devin_api_v3", config_path=repository, profile="recommended"
        )
        self.assertIn(repository, steps)
        self.assertNotIn("--packaged-starter", steps)
        self.assertNotIn("--easy", steps)

    def test_a_non_recommended_starter_profile_keeps_its_selected_profile(self):
        # The selector names the package resource and chooses no profile, so a
        # starter finding under any profile stays pinned to the one it describes.
        command = devin_readiness.doctor_command(
            config_path=self.INSTALLED, profile="advanced", config_source=self.STARTER
        )
        self.assertIn("--packaged-starter", command)
        self.assertIn("--profile advanced", command)
        self.assertNotIn(self.INSTALLED, command)
        self.assertNotIn("--easy", command)

    def test_a_starter_profile_containing_spaces_stays_quoted(self):
        command = devin_readiness.doctor_command(
            config_path=self.INSTALLED, profile="my profile", config_source=self.STARTER
        )
        self.assertIn("--packaged-starter --profile 'my profile'", command)

    def test_paths_and_profiles_containing_spaces_stay_quoted(self):
        spaced = "/" + "srv/Code Mower/code-mower.yml"
        command = devin_readiness.doctor_command(
            config_path=spaced, profile="my profile", devin=True
        )
        self.assertIn("'/" + "srv/Code Mower/code-mower.yml'", command)
        self.assertIn("--profile 'my profile'", command)

    def test_custom_lane_guidance_names_the_starter_without_a_path(self):
        guidance = devin_readiness.custom_lane_guidance(
            "devin_api_v3",
            config_path=self.INSTALLED,
            profile="recommended",
            config_source=self.STARTER,
            lanes=("house_devin",),
        )
        self.assertNotIn(self.INSTALLED, guidance)
        self.assertIn("packaged starter configuration (--packaged-starter)", guidance)
        self.assertIn("`house_devin`", guidance)

    def test_readiness_findings_carry_the_starter_source_into_remediation(self):
        findings = devin_readiness.devin_readiness(
            None,
            transport="devin_api_v3",
            config_profile="recommended",
            config_path=self.INSTALLED,
            config_source=self.STARTER,
            env={},
        )
        rendered = "\n".join(
            f"{finding.remediation}\n{json.dumps(finding.detail, default=str)}"
            for finding in findings
        )
        self.assertNotIn(self.INSTALLED, rendered)
        self.assertIn("--packaged-starter", rendered)


class PackagedStarterSelectorTests(unittest.TestCase):
    """`--packaged-starter` selects the maintained resource, not a cwd-local file.

    The rendered command is only portable if the selector it names resolves the
    same configuration from any directory, so these assert the CLI-level
    selection against decoy files rather than comparing command strings.
    """

    def _decoy_dir(self) -> Path:
        import tempfile

        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        # Both cwd-local files that `--easy` would prefer: doctor picks up
        # code-mower.yml, init picks up code-mower.example.yml.
        for name in ("code-mower.yml", "code-mower.example.yml"):
            (root / name).write_text("lanes: {}\n", encoding="utf-8")
        return root

    def test_the_packaged_starter_resolver_ignores_cwd_local_decoys(self):
        from code_mower import package as code_mower_package

        root = self._decoy_dir()
        cwd = Path.cwd()
        os.chdir(root)
        self.addCleanup(os.chdir, cwd)
        resolved = code_mower_package.packaged_starter_config_path()
        self.assertTrue(resolved.is_file())
        self.assertEqual(resolved.name, "code-mower.example.yml")
        for decoy in ("code-mower.yml", "code-mower.example.yml"):
            self.assertNotEqual(resolved.resolve(), (root / decoy).resolve())

    def test_doctor_and_init_select_the_same_config_under_decoys(self):
        from code_mower import doctor as code_mower_doctor
        from code_mower import init as code_mower_init
        from code_mower import package as code_mower_package

        root = self._decoy_dir()
        cwd = Path.cwd()
        os.chdir(root)
        self.addCleanup(os.chdir, cwd)
        expected = code_mower_package.packaged_starter_config_path().resolve()

        selected: list[Path] = []
        with mock.patch.object(
            code_mower_doctor, "run_doctor", side_effect=RuntimeError("stop")
        ) as run_doctor:
            with self.assertRaises(RuntimeError):
                code_mower_doctor.main(["--packaged-starter", "--profile", "advanced"])
        selected.append(Path(run_doctor.call_args.kwargs["config_path"]).resolve())
        self.assertEqual(run_doctor.call_args.kwargs["config_source"], "packaged_starter")
        # The selector chooses the resource, never the profile.
        self.assertEqual(run_doctor.call_args.kwargs["profile"], "advanced")

        with mock.patch.object(
            code_mower_init, "load_config", side_effect=RuntimeError("stop")
        ) as load_config:
            with self.assertRaises(RuntimeError):
                code_mower_init.main(
                    ["--packaged-starter", "--profile", "advanced", "--dry-run"]
                )
        selected.append(Path(load_config.call_args.args[0]).resolve())

        self.assertEqual(selected, [expected, expected])

    def test_a_contradictory_explicit_config_is_rejected_not_ignored(self):
        from code_mower import doctor as code_mower_doctor
        from code_mower import init as code_mower_init

        root = self._decoy_dir()
        cwd = Path.cwd()
        os.chdir(root)
        self.addCleanup(os.chdir, cwd)
        selections = (
            (code_mower_doctor.main, ["code-mower.yml", "--packaged-starter"]),
            (
                code_mower_init.main,
                ["code-mower.yml", "--packaged-starter", "--dry-run"],
            ),
        )
        for main, argv in selections:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                status = main(argv)
            self.assertEqual(status, 1)
            self.assertIn("--packaged-starter", err.getvalue())

    def test_the_cli_routes_the_selector_without_injecting_a_default_config(self):
        from code_mower import cli as code_mower_cli

        with mock.patch.object(
            code_mower_cli.code_mower_init, "main", return_value=0
        ) as init_main:
            code_mower_cli._init_main(["--packaged-starter", "--dry-run"])
        self.assertEqual(init_main.call_args.args[0], ["--packaged-starter", "--dry-run"])


class SupersededDevinBridgeTests(unittest.TestCase):
    def _repo(self, *paths: str) -> Path:
        import tempfile

        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        for path in paths:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# legacy\n", encoding="utf-8")
        return root

    def test_no_devin_repository_reports_nothing(self):
        summary = migration._superseded_devin_bridge_summary(self._repo(), files=[])
        self.assertEqual(summary["status"], "skip")
        self.assertEqual(summary["reason"], "no_superseded_bridge_files")
        self.assertEqual(summary["paths"], [])

    def test_bridge_and_labeler_pair_reports_bounded_migration(self):
        root = self._repo(
            ".github/workflows/devin-audit-bridge.yml",
            ".github/workflows/devin-audit-labeler.yml",
        )
        files = [
            {"path": ".github/workflows/devin-audit-bridge.yml", "tracked": True},
            {"path": ".github/workflows/devin-audit-labeler.yml", "tracked": True},
        ]
        summary = migration._superseded_devin_bridge_summary(root, files=files)
        self.assertEqual(summary["status"], "warn")
        self.assertEqual(summary["reason"], "superseded_bridge_pair")
        self.assertEqual(summary["transport"], "devin_api_v3")
        self.assertEqual(
            summary["paths"],
            [
                ".github/workflows/devin-audit-bridge.yml",
                ".github/workflows/devin-audit-labeler.yml",
            ],
        )
        action = summary["next_action"]
        self.assertIn("superseded", action)
        self.assertIn("devin_api_v3", action)
        self.assertIn("--set-transport", action)
        self.assertIn("--dry-run", action)
        self.assertIn("never deletes or rewrites", action)
        # Bounded: only the observed files are named.
        self.assertNotIn("tools/devin_audit_bridge.py", action)

    def test_single_legacy_file_is_still_reported(self):
        root = self._repo("tools/devin_audit_bridge.py")
        summary = migration._superseded_devin_bridge_summary(root, files=[])
        self.assertEqual(summary["status"], "warn")
        self.assertEqual(summary["reason"], "superseded_bridge_files")
        self.assertEqual(summary["paths"], ["tools/devin_audit_bridge.py"])

    def test_detection_never_removes_the_files(self):
        root = self._repo(".github/workflows/devin-audit-bridge.yml")
        migration._superseded_devin_bridge_summary(root, files=[])
        self.assertTrue((root / ".github/workflows/devin-audit-bridge.yml").is_file())

    def test_legacy_paths_are_setup_drift_candidates(self):
        for path in migration.SUPERSEDED_DEVIN_BRIDGE_PATHS:
            self.assertTrue(migration._is_setup_candidate_path(path), path)

    def test_reported_option_matches_the_supported_selection_flag(self):
        self.assertEqual(
            migration.DEVIN_TRANSPORT_OPTION, devin_readiness.TRANSPORT_OPTION
        )

    def test_next_action_includes_the_superseded_migration(self):
        superseded = {"status": "warn", "next_action": "migrate the superseded bridge"}
        action = migration._setup_drift_next_action(
            changed_count=0,
            standalone_pin={"status": "skip"},
            builder_hint={"status": "skip"},
            repo_path_hint={"status": "pass"},
            superseded_bridge=superseded,
        )
        self.assertEqual(action, "migrate the superseded bridge")

    def test_text_rendering_surfaces_the_superseded_transport(self):
        payload = {
            "status": "warn",
            "repo_path": "/tmp/repo",
            "profile": "recommended",
            "counts": {},
            "next_action": "migrate",
            "superseded_bridge": {
                "status": "warn",
                "reason": "superseded_bridge_pair",
                "transport": "devin_api_v3",
                "paths": [".github/workflows/devin-audit-bridge.yml"],
                "next_action": "preview the transport selection",
            },
        }
        text = migration.render_setup_drift_text(payload)
        self.assertIn("Superseded transport: WARN superseded_bridge_pair", text)
        self.assertIn("superseded_by=devin_api_v3", text)
        self.assertIn("Superseded transport next: preview the transport selection", text)

    def test_text_rendering_omits_the_section_when_absent(self):
        payload = {
            "status": "pass",
            "repo_path": "/tmp/repo",
            "profile": "recommended",
            "counts": {},
            "next_action": "ok",
            "superseded_bridge": {"status": "skip", "reason": "no_superseded_bridge_files"},
        }
        self.assertNotIn("Superseded transport", migration.render_setup_drift_text(payload))


def _report(checks):
    return DoctorReport(
        config_path="code-mower.yml",
        provider_templates_path="providers.yml",
        profile="recommended",
        checks=tuple(checks),
    )


class ConciseDoctorViewTests(unittest.TestCase):
    def setUp(self):
        self.report = _report(
            [
                DoctorCheck(
                    name="doctor.adoption.posture_hint",
                    status="warn",
                    message="hosted-builders posture",
                    remediation="ignore local CLI warnings",
                ),
                DoctorCheck(name="github.token", status="fail", message="token missing"),
                DoctorCheck(
                    name="provider.devin.optional", status="warn", message="devin not selected"
                ),
                DoctorCheck(
                    name="provider.graphify.optional", status="warn", message="graphify absent"
                ),
                DoctorCheck(name="config.lanes", status="pass", message="ok"),
            ]
        )

    def test_summary_leads_with_failures_and_keeps_counts(self):
        text = render_doctor_summary(self.report)
        self.assertIn("Code Mower doctor (concise)", text)
        self.assertIn("Adoption posture: WARN doctor.adoption.posture_hint", text)
        self.assertIn("Active failures and owner actions", text)
        self.assertIn("FAIL github.token", text)
        self.assertIn("Remaining detail by group", text)
        self.assertIn("--json", text)
        # Optional-provider warning detail is counted, not listed line by line.
        self.assertNotIn("devin not selected", text)

    def test_full_text_view_keeps_every_check(self):
        text = render_doctor_text(self.report)
        self.assertIn("devin not selected", text)
        self.assertIn("graphify absent", text)

    def test_summary_reports_a_clean_run(self):
        text = render_doctor_summary(_report([DoctorCheck(name="config.lanes", status="pass", message="ok")]))
        self.assertIn("No active failures or owner actions.", text)

    def test_summary_handles_an_empty_report(self):
        self.assertIn("No checks ran.", render_doctor_summary(_report([])))

    def test_json_detail_is_unchanged_by_the_concise_flag(self):
        payload = self.report.as_dict()
        self.assertEqual(len(payload["checks"]), 5)

    def test_summary_keeps_local_detail_out_of_nothing_it_did_not_receive(self):
        # Privacy: the summary renders only fields the report already carried.
        # The home prefix is assembled the way scripts/privacy_scan.py writes its
        # own patterns, so the assertion does not become a tracked literal.
        home_prefix = "/" + "Users/"
        text = render_doctor_summary(self.report)
        for line in text.splitlines():
            self.assertNotIn(home_prefix, line)


class ConciseDoctorCliTests(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "code_mower.doctor", *args],
            cwd=Path(__file__).resolve().parents[1],
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "HOME": str(Path.home()),
            },
            capture_output=True,
            text=True,
        )

    def test_concise_and_advanced_are_mutually_exclusive(self):
        result = self._run("--concise", "--advanced")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_json_output_is_json_even_with_concise(self):
        result = self._run("src/code_mower/templates/code-mower.example.yml", "--concise", "--json")
        self.assertIn(result.returncode, (0, 1))
        json.loads(result.stdout)


class PromptPackDevinGuidanceTests(unittest.TestCase):
    """The #955 Devin guidance, now split by #1015 into pointer and detail.

    The detailed prompt moved to `docs/devin-setup-prompt.md`; the universal
    pack keeps the opt-in pointer and the authority/lease guardrails. Every
    guarantee below still has to hold across the pair.
    """

    def setUp(self):
        docs = Path(__file__).resolve().parents[1] / "docs"
        self.text = (docs / "orchestrator-prompt-pack.md").read_text(encoding="utf-8")
        self.companion = (docs / "devin-setup-prompt.md").read_text(encoding="utf-8")

    def test_optional_devin_section_is_opt_in_and_uses_supported_commands(self):
        self.assertIn("## Optional Devin Setup", self.text)
        self.assertIn("The default adoption is", self.text)
        self.assertIn("devin-setup-prompt.md", self.text)
        self.assertIn("# Optional Devin Setup Prompt", self.companion)
        self.assertIn("The default adoption", self.companion)
        self.assertIn("--set-transport devin=devin_api_v3 --dry-run", self.companion)
        self.assertIn("code-mower doctor CONFIG --profile PROFILE", self.companion)
        self.assertIn(".code-mower.generated", self.companion)

    def test_guidance_keeps_staging_and_authority_boundaries(self):
        self.assertIn("grants no review or merge authority", self.text)
        self.assertIn("Do not delete or rewrite repository-owned workflow files", self.text)
        self.assertIn("do not start paid sessions", self.text)
        self.assertIn("Do not delete or rewrite repository-owned workflow files", self.companion)
        self.assertIn("do not start paid sessions", self.companion)

    def test_role_and_lease_guidance_is_referenced_not_restated(self):
        self.assertIn("docs/participant-qualification.md", self.text)
        self.assertIn("participant-qualification.md", self.companion)

    def test_packaged_starter_posture_names_the_portable_selector(self):
        self.assertIn(
            "code-mower doctor --packaged-starter --profile PROFILE --devin", self.companion
        )
        self.assertIn(
            "Never substitute the starter for a repository configuration", self.companion
        )

    def test_the_prompt_pack_does_not_call_easy_a_packaged_starter_selector(self):
        # `--easy` resolves against cwd-local files, so neither document may offer
        # it as the way to name the maintained package resource.
        self.assertNotIn("code-mower doctor --easy --devin", self.text)
        self.assertNotIn("code-mower doctor --easy --devin", self.companion)
        self.assertIn("--easy does not", self.companion)


if __name__ == "__main__":
    unittest.main()
