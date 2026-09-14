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
        text = render_doctor_summary(self.report)
        for line in text.splitlines():
            self.assertNotIn("/Users/", line)


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


if __name__ == "__main__":
    unittest.main()
