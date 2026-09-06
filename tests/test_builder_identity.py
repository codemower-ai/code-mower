import json
import os
import unittest
from unittest import mock

from code_mower import config as code_mower_config
from code_mower import init as code_mower_init
from code_mower.audit_labeler_lib import (
    author_exclusion_reason,
    builder_identity_matches,
)
from code_mower.lane_configs import load_lane_config
from code_mower.trailer_comment_labeler import resolve_label_decision


CURRENT_SHA = "a" * 40


class BuilderIdentityTests(unittest.TestCase):
    def test_identity_mapping_resolves_labels_and_authors(self) -> None:
        mapping = {
            "enabled": True,
            "labels": {"builder:codex": "codex"},
            "authors": {"claude[bot]": "claude"},
            "trailers": {"CODE_MOWER_BUILDER:grok-bot": "grok-bot"},
        }

        matches = builder_identity_matches(
            labels=["builder:codex"],
            author="claude[bot]",
            text="<!-- CODE_MOWER_BUILDER: grok-bot -->",
            config=mapping,
        )

        self.assertEqual(matches, ("codex", "claude"))
        self.assertIn(
            "excluded",
            author_exclusion_reason(
                lane_name="claude",
                labels=[],
                author="claude[bot]",
                text="",
                config=mapping,
            ),
        )

    def test_pr_body_trailer_does_not_create_builder_identity(self) -> None:
        mapping = {
            "enabled": True,
            "labels": {},
            "authors": {},
            "trailers": {"CODE_MOWER_BUILDER:codex": "codex"},
        }

        matches = builder_identity_matches(
            labels=[],
            author="drive-by-user",
            text="<!-- CODE_MOWER_BUILDER: codex -->",
            config=mapping,
        )

        self.assertEqual(matches, ())

    def test_trailer_labeler_skips_author_lane_when_configured(self) -> None:
        body = "\n".join(
            [
                "Codex Audit: PASS",
                f"Head SHA: `{CURRENT_SHA}`",
                "<!-- CODEX_AUDIT_STATE: codex-audit-done -->",
            ]
        )
        event = {
            "action": "created",
            "issue": {
                "number": 123,
                "pull_request": {},
                "labels": [{"name": "builder:codex"}],
                "user": {"login": "builder-user"},
                "body": "",
            },
            "comment": {"user": {"login": "codex-audit-bot"}, "body": body},
        }
        mapping = json.dumps(
            {
                "enabled": True,
                "labels": {"builder:codex": "codex"},
                "authors": {},
                "trailers": {},
            }
        )

        with mock.patch.dict(os.environ, {"CODE_MOWER_AUTHOR_EXCLUSION_JSON": mapping}):
            decision, reason = resolve_label_decision(
                event,
                current_head_sha=CURRENT_SHA,
                config=load_lane_config("codex"),
            )

        self.assertIsNone(decision)
        self.assertIn("codex lane excluded", reason)

    def test_init_embeds_author_exclusion_mapping_for_gate_and_labelers(self) -> None:
        cfg = code_mower_config.load_config(
            code_mower_init._resolve_config_path("code-mower.example.yml")
        )
        plan = code_mower_init.render_init_plan(
            cfg,
            package_mode=True,
            package_command="code-mower",
        )

        gate = next(
            item
            for item in plan.data["generated_files"]
            if item["path"] == ".github/workflows/code-mower-gate.yml"
        )
        payload = json.loads(gate["author_exclusion_json"])

        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["labels"]["builder:codex"], "codex")
        self.assertEqual(payload["labels"]["builder:claude"], "claude")
        self.assertEqual(payload["labels"]["builder:cursor"], "cursor")
        self.assertEqual(payload["labels"]["builder:devin"], "devin")
        self.assertEqual(payload["labels"]["builder:grok-bot"], "cursor")
        self.assertEqual(payload["authors"]["claude[bot]"], "claude")
        self.assertEqual(payload["authors"]["cursor[bot]"], "cursor")
        self.assertEqual(payload["authors"]["devin-ai-integration"], "devin")
        self.assertEqual(payload["authors"]["devin-ai-integration[bot]"], "devin")
        self.assertEqual(payload["authors"]["grok-bot[bot]"], "cursor")
        self.assertEqual(payload["trailers"]["CODE_MOWER_BUILDER:claude"], "claude")
        self.assertEqual(payload["trailers"]["CODE_MOWER_BUILDER:cursor"], "cursor")
        self.assertEqual(payload["trailers"]["CODE_MOWER_BUILDER:devin"], "devin")
        self.assertEqual(payload["trailers"]["CODE_MOWER_BUILDER:grok-bot"], "cursor")

    def test_legacy_grok_bot_identity_maps_to_cursor(self) -> None:
        payload = code_mower_init._author_exclusion_payload(
            {
                "merge_authority_excludes_author": True,
                "builder_identity": {
                    "labels": {"builder:grok-bot": "grok-bot"},
                    "authors": {"grok-bot[bot]": "grok-bot"},
                    "trailers": {"CODE_MOWER_BUILDER:grok-bot": "grok-bot"},
                },
            },
            {},
        )

        self.assertEqual(payload["labels"]["builder:cursor"], "cursor")
        self.assertEqual(payload["labels"]["builder:grok-bot"], "cursor")
        self.assertEqual(payload["authors"]["grok-bot[bot]"], "cursor")
        self.assertEqual(payload["trailers"]["CODE_MOWER_BUILDER:grok-bot"], "cursor")

    def test_author_exclusion_defaults_include_informational_audit_builders(self) -> None:
        payload = code_mower_init._author_exclusion_payload(
            {"merge_authority_excludes_author": True},
            {
                "codex": {
                    "type": "audit",
                    "driver": "local_cli",
                    "merge_authority": True,
                },
                "grok_build": {
                    "type": "audit",
                    "driver": "local_cli",
                    "merge_authority": False,
                    "author_lane": "grok-bot",
                },
            },
        )

        self.assertEqual(payload["labels"]["builder:codex"], "codex")
        self.assertEqual(payload["labels"]["builder:grok-bot"], "cursor")

    def test_config_validation_rejects_malformed_builder_identity(self) -> None:
        cfg = dict(
            code_mower_config.load_config(
                code_mower_init._resolve_config_path("code-mower.example.yml")
            )
        )
        cfg["merge_authority_excludes_author"] = "yes"
        cfg["builder_identity"] = {"unknown": {"builder:codex": "codex"}}

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(
            any(issue.path == "merge_authority_excludes_author" for issue in issues)
        )
        self.assertTrue(any(issue.path == "builder_identity.unknown" for issue in issues))

    def test_config_validation_checks_agent_pr_identity_sections(self) -> None:
        cfg = dict(
            code_mower_config.load_config(
                code_mower_init._resolve_config_path("code-mower.example.yml")
            )
        )
        cfg["builder_identity"] = {
            "branch_prefixes": {"cursor/": "bad lane"},
            "fix_round_mentions": {"bad lane": ""},
        }

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(
            any(issue.path == "builder_identity.branch_prefixes.cursor/" for issue in issues)
        )
        self.assertTrue(
            any(issue.path == "builder_identity.fix_round_mentions.bad lane" for issue in issues)
        )

    def test_config_validation_checks_owner_surface_token_names(self) -> None:
        cfg = dict(
            code_mower_config.load_config(
                code_mower_init._resolve_config_path("code-mower.example.yml")
            )
        )
        cfg["owner_surface"] = {
            "dispatch_token_env": "BAD TOKEN",
            "dispatch_token_expires_var": "1_BAD",
            "local_audit_runner_enabled_var": "BAD ENABLED VAR",
            "builder_wip_cap": "unlimited",
            "lane_runner_trusted_authors": ["owner", ""],
        }

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(any(issue.path == "owner_surface.dispatch_token_env" for issue in issues))
        self.assertTrue(
            any(issue.path == "owner_surface.dispatch_token_expires_var" for issue in issues)
        )
        self.assertTrue(
            any(
                issue.path == "owner_surface.local_audit_runner_enabled_var"
                for issue in issues
            )
        )
        self.assertTrue(any(issue.path == "owner_surface.builder_wip_cap" for issue in issues))
        self.assertTrue(
            any(
                issue.path == "owner_surface.lane_runner_trusted_authors[1]"
                for issue in issues
            )
        )

    def test_config_validation_checks_decision_authorities(self) -> None:
        cfg = dict(
            code_mower_config.load_config(
                code_mower_init._resolve_config_path("code-mower.example.yml")
            )
        )
        cfg["decisions"] = {"authorities": "owner", "unexpected": []}

        issues = code_mower_config.validate_config(cfg)

        self.assertTrue(any(issue.path == "decisions.authorities" for issue in issues))
        self.assertTrue(any(issue.path == "decisions.unexpected" for issue in issues))


if __name__ == "__main__":
    unittest.main()
