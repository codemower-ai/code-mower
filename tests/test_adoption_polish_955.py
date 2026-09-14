"""Adoption polish: effective review authority, superseded bridge, concise doctor."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from code_mower import devin_readiness, migration, review_authority, session
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
            "    driver: claude_cli\n"
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
        "    driver: claude_cli\n"
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
        self.assertEqual(source, "packaged_default")

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
        self.assertEqual(command, "`code-mower doctor --easy --devin`")
        self.assertNotIn(self.INSTALLED, command)

    def test_starter_transport_selection_is_portable_and_still_staged(self):
        steps = devin_readiness.select_transport_command(
            "devin_api_v3",
            config_path=self.INSTALLED,
            profile="recommended",
            config_source=self.STARTER,
        )
        self.assertNotIn(self.INSTALLED, steps)
        self.assertIn("code-mower init --easy --set-transport devin=devin_api_v3 --dry-run", steps)
        self.assertIn("--apply --output-dir", steps)
        self.assertIn("code-mower doctor --easy --devin", steps)

    def test_repository_configuration_is_never_replaced_by_the_starter(self):
        repository = "code-mower.yml"
        steps = devin_readiness.select_transport_command(
            "devin_api_v3", config_path=repository, profile="recommended"
        )
        self.assertIn(repository, steps)
        self.assertNotIn("--easy", steps)

    def test_a_non_recommended_starter_profile_keeps_its_explicit_pin(self):
        # `--easy` is an alias for the recommended profile, so it cannot stand in
        # for another one; the command stays honest rather than short.
        command = devin_readiness.doctor_command(
            config_path=self.INSTALLED, profile="advanced", config_source=self.STARTER
        )
        self.assertIn("--profile advanced", command)
        self.assertNotIn("--easy", command)

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
        self.assertIn("packaged starter configuration (--easy)", guidance)
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
        self.assertIn("--easy", rendered)


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
    def setUp(self):
        self.text = (
            Path(__file__).resolve().parents[1] / "docs" / "orchestrator-prompt-pack.md"
        ).read_text(encoding="utf-8")

    def test_optional_devin_section_is_opt_in_and_uses_supported_commands(self):
        self.assertIn("## Optional Devin Setup Prompt", self.text)
        self.assertIn("The default adoption is", self.text)
        self.assertIn("--set-transport devin=devin_api_v3 --dry-run", self.text)
        self.assertIn("code-mower doctor CONFIG --profile PROFILE", self.text)
        self.assertIn(".code-mower.generated", self.text)

    def test_guidance_keeps_staging_and_authority_boundaries(self):
        self.assertIn("selecting a transport grants no review or", self.text)
        self.assertIn("Do not delete or rewrite repository-owned workflow files", self.text)
        self.assertIn("do not start paid sessions", self.text)

    def test_role_and_lease_guidance_is_referenced_not_restated(self):
        self.assertIn("docs/participant-qualification.md", self.text)

    def test_packaged_starter_posture_names_the_portable_selector(self):
        self.assertIn("code-mower doctor --easy --devin", self.text)
        self.assertIn("Never substitute --easy for a repository", self.text)


if __name__ == "__main__":
    unittest.main()
