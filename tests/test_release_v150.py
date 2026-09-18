"""Candidate identity, artifact tampering and pre-tag qualification regressions."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from code_mower import __version__, release_readiness

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_candidate", ROOT / "scripts/release_candidate.py")
candidate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(candidate)
SHA = "a" * 40


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dist = Path(self.temp.name)
        with zipfile.ZipFile(self.dist / candidate.NAMES[0], "w") as archive:
            archive.writestr("code_mower-1.5.0.dist-info/METADATA",
                             "Name: code-mower\nVersion: 1.5.0\nRequires-Dist: PyYAML>=6.0\nRequires-Dist: packaging>=23.2\n")
            for module in candidate.MODULES:
                archive.writestr("code_mower/" + module, b"synthetic")
            for doc in candidate.DOCS:
                archive.writestr("code_mower-1.5.0.data/data/share/code-mower/docs/" + doc, b"synthetic")
        with tarfile.open(self.dist / candidate.NAMES[1], "w:gz") as archive:
            for path in (["src/code_mower/" + m for m in candidate.MODULES] +
                         ["docs/" + d for d in candidate.DOCS]):
                info = tarfile.TarInfo("code_mower-1.5.0/" + path)
                info.size = 9
                archive.addfile(info, io.BytesIO(b"synthetic"))
        self.manifest = {"schema": candidate.SCHEMA, "version": "1.5.0", "source_sha": SHA,
                         "kind": "candidate", "release_pr": 42,
                         "artifacts": {name: candidate.digest(self.dist / name) for name in candidate.NAMES},
                         "inventory": candidate.inspect(self.dist)}
        self.write_manifest()

    def write_manifest(self):
        (self.dist / "candidate.json").write_text(json.dumps(self.manifest))

    def test_exact_pair_passes_but_wrong_source_and_rehearsal_cannot_publish(self):
        candidate.verify(self.dist, SHA, candidate=True)
        with self.assertRaisesRegex(ValueError, "SHA mismatch"):
            candidate.verify(self.dist, "b" * 40, candidate=True)
        self.manifest["kind"] = "rehearsal"
        self.write_manifest()
        candidate.verify(self.dist, SHA)
        with self.assertRaisesRegex(ValueError, "pre-merge"):
            candidate.verify(self.dist, SHA, candidate=True)

    def test_digest_change_or_extra_distribution_refused(self):
        with (self.dist / candidate.NAMES[0]).open("ab") as stream:
            stream.write(b"tampered")
        with self.assertRaisesRegex(ValueError, "digest"):
            candidate.verify(self.dist, SHA)
        (self.dist / "unexpected.whl").write_bytes(b"extra")
        with self.assertRaisesRegex(ValueError, "unexpected distribution"):
            candidate.verify(self.dist, SHA)

    def test_inventories_must_match_even_if_digests_are_updated(self):
        with zipfile.ZipFile(self.dist / candidate.NAMES[0], "a") as archive:
            archive.writestr("code_mower/extra.py", b"extra")
        self.manifest["artifacts"][candidate.NAMES[0]] = candidate.digest(self.dist / candidate.NAMES[0])
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "inventory"):
            candidate.verify(self.dist, SHA)

    def test_private_inventory_refused(self):
        with zipfile.ZipFile(self.dist / candidate.NAMES[0], "a") as archive:
            archive.writestr("code_mower/.code-mower/private.json", b"synthetic")
        with self.assertRaisesRegex(ValueError, "private archive"):
            candidate.inspect(self.dist)

    def test_dirty_or_wrong_checkout_does_not_build(self):
        for outputs in (("b" * 40,), (SHA, " M README.md")):
            with self.subTest(outputs=outputs), patch.object(candidate, "run", side_effect=outputs), \
                    patch.object(candidate.subprocess, "run") as build:
                with self.assertRaises(ValueError):
                    candidate.build(ROOT, self.dist / "new", SHA, None)
                build.assert_not_called()


class ReleaseContractTests(unittest.TestCase):
    def test_identity_and_readiness(self):
        self.assertEqual(__version__, "1.5.0")
        self.assertEqual(release_readiness.render_release_readiness(ROOT)["status"], "pass")

    def test_removing_private_acceptance_or_moving_tag_first_blocks(self):
        text = (ROOT / "docs/v150-release-runbook.md").read_text()
        for bad in (text.replace("## 3. Private acceptance", "## Removed acceptance"),
                    text.replace('git tag -a v1.5.0 "$RELEASE_SHA"', "tag removed"),
                    text.replace("aggregate campaign ACU", "unspecified budget")):
            with self.subTest(text=bad[:10]), patch.object(release_readiness, "_read_text_if_exists", return_value=bad):
                order, assertions = release_readiness._candidate_runbook_checks(ROOT)
                self.assertTrue(order or assertions)

    def test_historical_v14_records_are_unchanged(self):
        # Recorded from the parent release-prep baseline. No Git history needed in sdist tests.
        expected = json.loads((ROOT / "tests/fixtures/release_identity/v14-evidence-sha256.json").read_text())
        for name, digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest)
