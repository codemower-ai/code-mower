"""Version-identity and Board-boundary regressions for the v1.4.2 release, #952."""
from pathlib import Path

import unittest

from code_mower import __version__, release_readiness
from code_mower import package as package_module

ROOT = Path(__file__).resolve().parents[1]


class VersionIdentityTests(unittest.TestCase):
    def test_source_version_is_1_4_2(self):
        self.assertEqual(__version__, "1.4.2")

    def test_committed_manifest_version_matches_source(self):
        manifest = package_module.generate_committed_package_manifest(ROOT)
        self.assertEqual(manifest["package"]["version"], __version__)

    def test_release_tag_for_current_version(self):
        self.assertEqual(release_readiness._release_tag_for_version(__version__), "v1.4.2")


class RunbookIdentityTests(unittest.TestCase):
    def test_pypi_release_doc_carries_the_v142_runbook_heading(self):
        doc = (ROOT / "docs/pypi-release.md").read_text(encoding="utf-8")
        self.assertIn(
            f"## v1.4.2 {release_readiness.POST_MERGE_RUNBOOK_HEADING}",
            doc,
        )

    def test_release_notes_and_qualification_docs_exist_for_v142(self):
        release_notes = (ROOT / "docs/v142-release-notes.md").read_text(encoding="utf-8")
        qualification = (ROOT / "docs/v142-qualification.md").read_text(encoding="utf-8")
        self.assertIn("# Code Mower v1.4.2 Release Notes", release_notes)
        self.assertIn("v1.4.2 qualification and evidence matrix", qualification)
        # v1.4.1's own historical documents must remain untouched.
        self.assertTrue((ROOT / "docs/v141-release-notes.md").is_file())
        self.assertTrue((ROOT / "docs/v141-qualification.md").is_file())

    def test_release_history_orders_v142_before_v141_before_v131(self):
        release_history = (ROOT / "docs/release-history.md").read_text(encoding="utf-8")
        self.assertLess(
            release_history.index("[v1.4.2 source candidate notes](v142-release-notes.md)"),
            release_history.index("[v1.4.1 source candidate notes](v141-release-notes.md)"),
        )
        self.assertLess(
            release_history.index("[v1.4.1 source candidate notes](v141-release-notes.md)"),
            release_history.index("[v1.3.1 release notes](v131-release-notes.md)"),
        )


class BoardRestartBoundaryTests(unittest.TestCase):
    def test_qualification_doc_requires_all_three_boards_agree_on_installed_version(self):
        qualification = (ROOT / "docs/v142-qualification.md").read_text(encoding="utf-8")
        self.assertIn("three existing Board", qualification)
        self.assertIn("serving ==", qualification)
        self.assertIn("1.4.2", qualification)

    def test_release_notes_do_not_claim_951_hosted_canary_or_close_951(self):
        release_notes = (ROOT / "docs/v142-release-notes.md").read_text(encoding="utf-8")
        self.assertIn("bounded hosted Devin canary is still pending", release_notes)
        self.assertIn("does not claim the hosted result or close", release_notes)


if __name__ == "__main__":
    unittest.main()
