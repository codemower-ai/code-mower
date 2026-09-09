#!/usr/bin/env python3
"""Focused prompt-contract tests for remote failure reasons (issue #819).

The v1.1.0 Cursor qualification returned `unknown` without actionable
classification because the remote prompt's final step shape omitted the
already-supported optional `failure_reason`. These tests pin the
provider-neutral prompt contract: the final-answer shape names
`failure_reason`, a failed `package_install` must carry exactly one closed
reason, every classification rule is taught with bounded examples, `unknown`
stays reserved, and the prompt never requests raw output, logs, or secrets.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from code_mower import campaign_adapters


def _prompt(provider: str = "codex", **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "provider": provider,
        "release_tag": "v1.0.0",
        "package_spec": "code-mower==1.0.0",
        "package_identity": "code-mower",
        "normalized_version": "1.0.0",
        "qualification_context": "cold_install",
        "starting_version": "",
    }
    kwargs.update(overrides)
    return campaign_adapters.build_qualification_prompt(**kwargs)  # type: ignore[arg-type]


def _flowing(prompt: str) -> str:
    return " ".join(prompt.split())


class RemoteFailureReasonPromptTests(unittest.TestCase):
    """Provider-neutral prompt teaches the closed failure-reason contract."""

    def test_final_answer_shape_names_failure_reason(self) -> None:
        """The exact final-answer step shape includes optional failure_reason."""
        prompt = _prompt()
        self.assertIn("optional failure_reason", _flowing(prompt))
        self.assertIn("nowhere else", _flowing(prompt))

    def test_failed_package_install_must_carry_exactly_one_reason(self) -> None:
        """A failed package_install step must carry exactly one closed reason."""
        rule = _flowing(_prompt())
        self.assertIn(
            "A failed package_install step must carry exactly one failure_reason",
            rule,
        )

    def test_closed_taxonomy_names_all_five_reasons(self) -> None:
        """Every closed value is visible outside the certificate branch."""
        rule = _flowing(_prompt())
        for reason in ("network", "package_index", "runtime", "sandbox_permission", "unknown"):
            self.assertIn(reason, rule)

    def test_network_rule_covers_connection_and_tls(self) -> None:
        rule = _flowing(_prompt())
        self.assertIn("network for connection or TLS failures", rule)

    def test_package_index_rule_covers_missing_version_and_404(self) -> None:
        rule = _flowing(_prompt())
        self.assertIn("package_index for a missing version or 404", rule)

    def test_runtime_rule_covers_python_and_dependency_incompatibility(self) -> None:
        rule = _flowing(_prompt())
        self.assertIn("runtime for Python or dependency incompatibility", rule)

    def test_sandbox_permission_rule_covers_permission_disk_and_sandbox(self) -> None:
        rule = _flowing(_prompt())
        self.assertIn("sandbox_permission for permission, disk, or sandbox denials", rule)

    def test_unknown_reserved_for_attempted_but_unclassifiable(self) -> None:
        rule = _flowing(_prompt())
        self.assertIn(
            "Use unknown only for a command that was actually attempted and "
            "cannot be classified from local diagnostics",
            rule,
        )

    def test_prompt_never_requests_raw_output_or_secrets(self) -> None:
        """The prompt asks for the closed reason word, never raw evidence."""
        rule = _flowing(_prompt())
        self.assertIn("Report only the closed reason word", rule)
        self.assertIn("never include raw output, logs, paths", rule)
        for forbidden_request in (
            "include stdout",
            "include stderr",
            "paste the transcript",
            "print the diff",
        ):
            self.assertNotIn(forbidden_request, rule)

    def test_taxonomy_is_provider_neutral(self) -> None:
        """Every provider prompt teaches the taxonomy, not just macOS Claude."""
        for provider in ("codex", "claude", "antigravity", "muse", "devin_cli"):
            with self.subTest(provider=provider):
                rule = _flowing(_prompt(provider, platform_system="Linux"))
                self.assertIn(
                    "A failed package_install step must carry exactly one failure_reason",
                    rule,
                )
                self.assertIn("cannot be classified from local diagnostics", rule)


if __name__ == "__main__":
    unittest.main()
