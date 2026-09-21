"""The committed labeler identity value, executed through the strict contract.

Issue #1004: the trusted Claude/Codex labeler workflows on the default branch
carried a legacy ``trailers`` field that ``builder_lineage.Identity`` refuses,
so every exact-head verdict failed closed before it could move a label. These
rows read the committed workflow values themselves rather than a synthesized
policy, so a stale regenerated artifact is caught in CI instead of on a PR.
"""
from __future__ import annotations

import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from code_mower import builder_lineage as lineage_core
from code_mower import config as code_mower_config
from code_mower import init as code_mower_init
from code_mower import trailer_comment_labeler as trailer

ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/repo"
HEAD = "b" * 40
BASE = "a" * 40
AUTHORITIES = ("lineage-publisher[bot]",)
AUTHOR_EXCLUSION_ENV = "CODE_MOWER_AUTHOR_EXCLUSION_JSON"
ENV_LINE_RE = re.compile(
    rf"^\s*{AUTHOR_EXCLUSION_ENV}:\s*(\"(?:[^\"\\]|\\.)*\")\s*$",
    re.MULTILINE,
)
IDENTITY_FIELDS = frozenset(
    {"enabled", "labels", "authors", "branch_prefixes", "require_verified_lineage"}
)
# Every trusted consumer of the value: the gate plus both trailer labelers.
TRUSTED_WORKFLOWS = (
    ".github/workflows/code-mower-gate.yml",
    ".github/workflows/claude-audit-labeler.yml",
    ".github/workflows/codex-audit-labeler.yml",
)
LABELER_TEMPLATES = (
    "src/code_mower/templates/workflows/trailer-comment-labeler.yml.j2",
)


def committed_identity_text(relative_path: str) -> str:
    """The exact JSON text the workflow exports, unescaped from its YAML scalar."""
    text = (ROOT / relative_path).read_text(encoding="utf-8")
    matches = ENV_LINE_RE.findall(text)
    if len(matches) != 1:
        raise AssertionError(
            f"{relative_path} must export exactly one literal "
            f"{AUTHOR_EXCLUSION_ENV}; found {len(matches)}"
        )
    return json.loads(matches[0])


class CommittedLabelerIdentity(unittest.TestCase):
    def test_committed_identity_parses_under_the_strict_contract(self) -> None:
        for relative_path in TRUSTED_WORKFLOWS:
            with self.subTest(workflow=relative_path):
                raw = committed_identity_text(relative_path)
                self.assertLessEqual(set(json.loads(raw)), IDENTITY_FIELDS)
                identity = lineage_core.Identity.from_text(raw)
                self.assertTrue(identity.enabled)
                self.assertTrue(identity.require_verified_lineage)
                self.assertTrue(identity.branch_prefixes)
                self.assertIn(("builder:claude", "claude"), identity.labels)
                self.assertIn(("builder:codex", "codex"), identity.labels)
                self.assertIn(("claude[bot]", "claude"), identity.authors)

    def test_committed_identity_matches_canonical_generation(self) -> None:
        config = code_mower_config.load_config(ROOT / "code-mower.yml")
        expected = code_mower_init._author_exclusion_json(config, {})

        for relative_path in TRUSTED_WORKFLOWS:
            with self.subTest(workflow=relative_path):
                self.assertEqual(committed_identity_text(relative_path), expected)

    def test_labeler_templates_keep_the_generated_placeholder(self) -> None:
        for relative_path in LABELER_TEMPLATES:
            with self.subTest(template=relative_path):
                text = (ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn(
                    f"{AUTHOR_EXCLUSION_ENV}: __AUTHOR_EXCLUSION_JSON__",
                    text,
                )
                self.assertNotIn("CODE_MOWER_BUILDER:", text)


class ExactHeadLabelTransition(unittest.TestCase):
    """Row: an eligible current-head PASS moves needs-* to *-audit-done."""

    def _run_claude_labeler(self, identity_text: str, *, history_error: Exception | None = None):
        labels = ("builder:codex", "needs-claude-audit")
        pull_request = {
            "number": 42,
            "state": "open",
            "head": {"ref": "codex/topic", "sha": HEAD},
            "base": {"repo": {"full_name": REPO}, "sha": BASE},
            "user": {"login": "human"},
            "labels": [{"name": name} for name in labels],
        }
        comment = {
            "id": 1,
            "created_at": "2026-09-16T00:00:00Z",
            "user": {"login": "claude-audit-bot"},
            "body": (
                "Claude Audit - PASS\n"
                f"Head SHA: `{HEAD}`\n"
                "<!-- CLAUDE_AUDIT_STATE: claude-audit-done -->"
            ),
        }
        event = {
            "action": "created",
            "issue": {
                "number": 42,
                "pull_request": {},
                "user": {"login": "human"},
                "body": "",
                "labels": [{"name": name} for name in labels],
            },
            "comment": comment,
        }
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            env = {
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_REPOSITORY": REPO,
                "GITHUB_TOKEN": "fixture",
                AUTHOR_EXCLUSION_ENV: identity_text,
                "CODE_MOWER_DECISION_AUTHORITIES": ",".join(AUTHORITIES),
            }
            stdout = io.StringIO()
            history = (
                {"side_effect": history_error}
                if history_error is not None
                else {"return_value": [comment]}
            )
            with (
                patch.dict(os.environ, env, clear=True),
                redirect_stdout(stdout),
                patch.object(trailer, "fetch_pull_request", return_value=pull_request),
                patch.object(trailer, "fetch_issue_comments", **history),
                patch.object(trailer, "apply_label_decision") as apply,
            ):
                self.assertEqual(trailer.main(["--lane", "claude"]), 0)
            return apply, stdout.getvalue()

    def test_committed_identity_admits_the_current_head_pass(self) -> None:
        apply, output = self._run_claude_labeler(
            committed_identity_text(".github/workflows/claude-audit-labeler.yml")
        )

        apply.assert_called_once()
        decision = apply.call_args.args[1]
        self.assertEqual(decision.add_label, "claude-audit-done", output)
        self.assertEqual(
            set(decision.remove_labels),
            {"needs-claude-audit", "claude-audit-blocked"},
        )
        self.assertEqual(decision.reviewed_sha, HEAD)

    def test_legacy_trailers_identity_leaves_labels_unchanged(self) -> None:
        legacy = json.loads(
            committed_identity_text(".github/workflows/claude-audit-labeler.yml")
        )
        legacy["trailers"] = {"CODE_MOWER_BUILDER:claude": "claude"}

        apply, output = self._run_claude_labeler(json.dumps(legacy, sort_keys=True))

        apply.assert_not_called()
        self.assertIn("label state unchanged", output)

    def test_incomplete_history_leaves_labels_unchanged(self) -> None:
        apply, output = self._run_claude_labeler(
            committed_identity_text(".github/workflows/claude-audit-labeler.yml"),
            history_error=RuntimeError("comment history pagination cap exceeded"),
        )

        apply.assert_not_called()
        self.assertIn("label state unchanged", output)


if __name__ == "__main__":  # pragma: no cover - direct execution
    unittest.main()
