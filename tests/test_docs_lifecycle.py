"""Documentation inventory, ownership, and immutable-history tests."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

import yaml

from code_mower import __version__
from code_mower import docs_lifecycle
from code_mower import release_readiness


ROOT = Path(__file__).resolve().parents[1]


class DocumentationLifecycleTests(unittest.TestCase):
    def _write(self, root: Path, relative: str, text: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _manifest(self, root: Path, rows: list[dict[str, str]]) -> None:
        self._write(
            root,
            docs_lifecycle.MANIFEST_PATH,
            yaml.safe_dump({"schema": docs_lifecycle.SCHEMA, "documents": rows}, sort_keys=False),
        )

    def test_repository_manifest_is_complete_and_current(self) -> None:
        report = docs_lifecycle.validate_manifest(ROOT)
        self.assertEqual(report["status"], "pass", report["problems"])
        self.assertEqual(report["document_count"], report["declared_count"])
        self.assertGreater(report["status_counts"]["canonical"], 0)
        self.assertGreater(report["status_counts"]["frozen"], 0)
        self.assertEqual(
            list(docs_lifecycle.JOURNEY_SUBJECTS),
            [
                "installation",
                "quickstart",
                "upgrade",
                "board-operations",
                "troubleshooting",
            ],
        )
        index = (ROOT / docs_lifecycle.INDEX_PATH).read_text(encoding="utf-8")
        journey_links = [
            "(install.md)",
            "(quickstart.md)",
            "(upgrade-existing-repo.md)",
            "(board-service-lifecycle.md)",
            "(troubleshooting.md)",
        ]
        offsets = [index.index(link) for link in journey_links]
        self.assertEqual(offsets, sorted(offsets))

        readiness = release_readiness.render_release_readiness(ROOT)
        lifecycle_check = next(
            check for check in readiness["checks"] if check["id"] == "documentation-lifecycle"
        )
        self.assertEqual(lifecycle_check["status"], "pass")

    def test_unclassified_duplicate_subject_and_changed_frozen_bytes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "docs/README.md", "placeholder")
            self._write(root, "docs/install.md", "# Install\n")
            self._write(root, "docs/other.md", "# Other\n")
            digest = hashlib.sha256(b"# Other\n").hexdigest()
            rows = [
                {"path": "docs/README.md", "status": "canonical", "subject": "index"},
                {"path": "docs/install.md", "status": "canonical", "subject": "install"},
                {"path": "docs/other.md", "status": "canonical", "subject": "install"},
            ]
            self._manifest(root, rows)
            docs_lifecycle.write_index(root)
            report = docs_lifecycle.validate_manifest(root)
            self.assertTrue(any("duplicates 'install'" in item for item in report["problems"]))

            rows[-1] = {"path": "docs/other.md", "status": "frozen", "sha256": digest}
            self._manifest(root, rows)
            docs_lifecycle.write_index(root)
            self._write(root, "docs/other.md", "changed\n")
            report = docs_lifecycle.validate_manifest(root)
            self.assertTrue(any("immutable content differs" in item for item in report["problems"]))

            self._write(root, "docs/new.md", "# New\n")
            report = docs_lifecycle.validate_manifest(root)
            self.assertTrue(any("unclassified Markdown" in item for item in report["problems"]))

    def test_index_is_deterministic_and_staleness_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "docs/README.md", "placeholder")
            self._write(root, "docs/install.md", "# Install Code Mower\n")
            self._manifest(
                root,
                [
                    {"path": "docs/README.md", "status": "canonical", "subject": "index"},
                    {"path": "docs/install.md", "status": "canonical", "subject": "install"},
                ],
            )
            docs_lifecycle.write_index(root)
            first = (root / docs_lifecycle.INDEX_PATH).read_text(encoding="utf-8")
            docs_lifecycle.write_index(root)
            self.assertEqual((root / docs_lifecycle.INDEX_PATH).read_text(encoding="utf-8"), first)
            self.assertIn("[Install Code Mower](install.md)", first)
            self._write(root, "docs/README.md", first + "manual edit\n")
            report = docs_lifecycle.validate_manifest(root)
            self.assertTrue(any("generated index is stale" in item for item in report["problems"]))

    def test_supporting_document_cannot_repeat_current_install_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "docs/README.md", "placeholder")
            self._write(
                root,
                "docs/detail.md",
                f"# Detail\n\nInstall `code-mower=={__version__}`.\n",
            )
            self._manifest(
                root,
                [
                    {
                        "path": "docs/README.md",
                        "status": "canonical",
                        "subject": "index",
                    },
                    {"path": "docs/detail.md", "status": "supporting"},
                ],
            )
            docs_lifecycle.write_index(root)
            report = docs_lifecycle.validate_manifest(root)
            self.assertTrue(
                any("supporting documents must link" in item for item in report["problems"])
            )
