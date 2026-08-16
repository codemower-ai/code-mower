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
        self.assertEqual(payload["labels"]["builder:grok-bot"], "grok-bot")
        self.assertEqual(payload["authors"]["claude[bot]"], "claude")
        self.assertEqual(payload["trailers"]["CODE_MOWER_BUILDER:claude"], "claude")

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


if __name__ == "__main__":
    unittest.main()
