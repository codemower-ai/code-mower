"""The final-tag text contract, including the immutable v1.4.1 contradiction."""

from __future__ import annotations

import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from code_mower import release_identity, release_readiness, versioning


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/release_identity"


class ReleaseIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        (self.repo / "src/code_mower").mkdir(parents=True)
        self.write_release("1.5.0")

    def write(self, path, text):
        (self.repo / path).write_text(text, encoding="utf-8")

    def write_release(self, version):
        self.write("pyproject.toml", '[project]\nname = "code-mower"\n'
                   f'version = "{version}"\nreadme = "README.md"\n')
        self.write("src/code_mower/__init__.py", f'__version__ = "{version}"\n')
        self.write("README.md", "# Code Mower\n\n" + versioning.public_baseline_sentence(version)
                   + "\n\n## TestPyPI qualification\n\nRehearse the candidate before publication.\n")
        self.write("CHANGELOG.md", f"# Changelog\n\n## Unreleased\n\nPublication pending #2000.\n\n"
                   f"## {version} — release\n\n- The release includes the new identity check.\n\n"
                   "## 1.4.1 — source candidate (publication pending #915)\n\nHistorical prose.\n")

    def check(self, tag="v1.5.0"):
        return release_identity.check_release_identity(self.repo, tag)

    def test_final_tag_accepts_final_text_before_publication(self):
        self.assertEqual(self.check(), [])

    def test_current_checkout_obeys_the_contract(self):
        from code_mower import __version__
        self.assertEqual(release_identity.check_release_identity(
            ROOT, versioning.release_tag_for_version(__version__)), [])

    def test_published_v141_contradiction_is_rejected_independently_on_each_surface(self):
        for surface, fixture in (("README.md", "v141-readme.md"),
                                 ("CHANGELOG.md", "v141-changelog.md")):
            with self.subTest(surface=surface):
                self.write_release("1.4.1")
                # Avoid a duplicate historical heading in this fixture.
                self.write("CHANGELOG.md", "## 1.4.1 — release\n\n- Changes.\n")
                self.write(surface, (FIXTURES / fixture).read_text(encoding="utf-8"))
                failures = self.check("v1.4.1")
                self.assertTrue(any(surface in failure and "transient" in failure for failure in failures), failures)

    def test_final_tag_refuses_transient_release_assertions_on_both_surfaces(self):
        for phrase in (
            "This is a source candidate.", "Publication pending #915.",
            "Publication and installed-package qualification remain pending [#915](https://example.org/915).",
            "This release is blocked on issue #915.", "The release depends on #915.",
            "Publication requires #915 to close.", "Release is final only after #915.",
            "The release is incomplete.", "The release is not yet complete.",
            "The release remains unpublished.", "v1.5.0 remains incomplete pending #915.",
            "Not yet published.", "PUBLICATION\n**PENDING** #915.",
            "This is a source-candidate.",
        ):
            for surface in ("README.md", "CHANGELOG.md"):
                with self.subTest(phrase=phrase, surface=surface):
                    self.write_release("1.5.0")
                    path = self.repo / surface
                    text = path.read_text(encoding="utf-8")
                    if surface == "README.md":
                        text = text.replace("## TestPyPI", phrase + "\n\n## TestPyPI")
                    else:
                        text = text.replace("- The release includes", phrase + "\n\n- The release includes")
                    self.write(surface, text)
                    self.assertTrue(any("transient" in p for p in self.check()), self.check())

    def test_candidate_instructions_and_unrelated_incomplete_work_remain_valid(self):
        path = self.repo / "CHANGELOG.md"
        self.write("CHANGELOG.md", path.read_text().replace(
            "- The release includes", "- Fix incomplete metadata.\n"
            "- Optional hosted canary pending #951 is outside this version's scope.\n"
            "- TestPyPI qualification instructions describe candidate rehearsals.\n"
            "- The release includes"))
        self.assertEqual(self.check(), [])

    def test_wrong_missing_duplicate_and_stale_changelog_headings_fail(self):
        for heading in ("1.4.9", "1.5.00", "1.5.0rc1", "Unreleased", "[1.5.0]",
                        "1.5.0\n\n## 1.5.0", "1.6.0\n\n## 1.5.0"):
            with self.subTest(heading=heading):
                self.write("CHANGELOG.md", f"# Changelog\n\n## {heading}\n\nChanges.\n")
                self.assertTrue(any("CHANGELOG.md" in p for p in self.check()))

    def test_tag_form_changelog_heading_is_supported(self):
        self.write("CHANGELOG.md", "## v1.5.0 — 2026-09-17\n\nChanges.\n")
        self.assertEqual(self.check(), [])

    def test_heading_cannot_present_a_final_tag_as_an_unfinished_release(self):
        for state in ("candidate", "pending", "unpublished", "unreleased", "incomplete", "draft"):
            with self.subTest(state=state):
                self.write("CHANGELOG.md", f"## 1.5.0 — {state}\n\nChanges.\n")
                self.assertTrue(any("transient" in p for p in self.check()))

    def test_empty_changelog_heading_fails_without_crashing(self):
        self.write("CHANGELOG.md", "##   \n\nChanges.\n")
        self.assertTrue(any("CHANGELOG.md" in p for p in self.check()))

    def test_readme_requires_one_exact_tag_and_package_spec_in_the_opening_statement(self):
        baseline = versioning.public_baseline_sentence("1.5.0")
        for text in (
            baseline.replace("v1.5.0", "v1.4.9"),
            baseline.replace("code-mower==1.5.0", "code-mower==1.5.1"),
            baseline.replace("code-mower==1.5.0", "code-mower==1.5.0rc1"),
            baseline.replace("1.5.0", "1.5.00"),
            "No release statement.", baseline + "\n\n" + baseline,
            "## Instructions\n\n" + baseline,
        ):
            with self.subTest(text=text):
                self.write("README.md", text)
                self.assertTrue(any("README.md" in p for p in self.check()))

    def test_package_metadata_must_match_the_tag(self):
        for path, text in (
            ("pyproject.toml", '[project]\nname = "code-mower"\nversion = "1.5.1"\nreadme = "README.md"'),
            ("pyproject.toml", "not toml"),
            ("pyproject.toml", 'project = "invalid"'),
            ("src/code_mower/__init__.py", '__version__ = "1.5.1"'),
            ("src/code_mower/__init__.py", '__version__ = "1.5.0"\n__version__ = "1.5.1"'),
            ("src/code_mower/__init__.py", '__version__ = "1.5.0"\n__version__ = compute_version()'),
            ("src/code_mower/__init__.py", '__version__ = compute_version()'),
        ):
            with self.subTest(path=path, text=text):
                self.write_release("1.5.0")
                self.write(path, text)
                self.assertTrue(any(path in p for p in self.check()))

    def test_missing_surfaces_fail_closed(self):
        for path in ("pyproject.toml", "src/code_mower/__init__.py", "README.md", "CHANGELOG.md"):
            with self.subTest(path=path):
                self.write_release("1.5.0")
                (self.repo / path).unlink()
                self.assertTrue(any(path in p for p in self.check()))

    def test_malformed_tags_are_never_normalized(self):
        for tag in ("", "1.5.0", "v1.5", "v01.5.0", "v1.05.0", "v1.5.00", "v1.5.0\n",
                    " v1.5.0", "refs/tags/v1.5.0", "main", "v1.5.0+local", "v1.5.0rc1",
                    "v1.5.0-rc.01", "v1.5.0-preview.1", "v1.5.0;true"):
            with self.subTest(tag=tag):
                self.assertTrue(any("release tag" in p for p in self.check(tag)))

    def test_canonical_prerelease_tags_bind_the_pep440_package_version(self):
        for tag, version in (("v1.5.0-alpha.1", "1.5.0a1"), ("v1.5.0-beta.2", "1.5.0b2"),
                             ("v1.5.0-rc.1", "1.5.0rc1")):
            with self.subTest(tag=tag):
                self.write_release(version)
                path = self.repo / "README.md"
                self.write("README.md", path.read_text().replace("## TestPyPI", "Source candidate.\n\n## TestPyPI"))
                self.assertEqual(self.check(tag), [])
                self.assertTrue(self.check("v1.5.0"))


class ReleaseIdentityWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())

    def test_readiness_checks_the_publish_wiring(self):
        jobs = self.workflow["jobs"]
        self.assertTrue(release_readiness._public_identity_gate_holds(jobs))
        for mutation in ("conditional", "allowed-failure", "missing-validator", "unpinned-build", "unbound-ref"):
            with self.subTest(mutation=mutation):
                broken = copy.deepcopy(jobs)
                step = broken["release-identity"]["steps"][-1]
                if mutation == "conditional":
                    step["if"] = "${{ github.event_name == 'workflow_dispatch' }}"
                elif mutation == "allowed-failure":
                    step["continue-on-error"] = True
                elif mutation == "missing-validator":
                    step["run"] = "true"
                elif mutation == "unpinned-build":
                    broken["build-distributions"]["steps"][0]["with"]["ref"] = "main"
                else:
                    step["run"] = step["run"].replace('test "$ACTUAL_REF" = "refs/tags/$RELEASE_TAG"', "true")
                self.assertFalse(release_readiness._public_identity_gate_holds(broken))

    def test_same_validator_runs_for_dispatch_and_release_with_no_publish_bypass(self):
        jobs = self.workflow["jobs"]
        self.assertNotIn("if", jobs["release-identity"])
        self.assertNotIn("if", jobs["release-identity"]["steps"][-1])
        for publisher in ("publish-testpypi", "publish-pypi"):
            self.assertIn("release-identity", jobs[publisher]["needs"])
        self.assertEqual(jobs["build-distributions"]["needs"], "release-identity")
        self.assertNotIn("v1.4.2", (ROOT / ".github/workflows/release.yml").read_text())

    def test_release_step_checks_checkout_ref_and_text_before_succeeding(self):
        fixture = ReleaseIdentityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        repo = fixture.repo
        shutil.copy(ROOT / "src/code_mower/release_identity.py", repo / "src/code_mower/release_identity.py")
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=Release Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "Release fixture")
        sha = git("rev-parse", "HEAD")
        script = self.workflow["jobs"]["release-identity"]["steps"][-1]["run"]
        for tag, ref, actual, success in (
            ("v1.5.0", "refs/tags/v1.5.0", sha, True),
            ("v1.5.0", "refs/heads/v1.5.0", sha, False),
            ("v1.5.0", "refs/tags/v1.5.1", sha, False),
            ("v1.5.0", "refs/tags/v1.5.0", "b" * 40, False),
            ("v1.5.1", "refs/tags/v1.5.1", sha, False),
            ("v1.5.00", "refs/tags/v1.5.00", sha, False),
        ):
            with self.subTest(tag=tag, ref=ref, actual=actual):
                result = subprocess.run(["bash", "-c", script], cwd=repo, text=True, capture_output=True,
                                        env={**os.environ, "RELEASE_TAG": tag, "ACTUAL_REF": ref, "ACTUAL_SHA": actual})
                self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        fixture.write("CHANGELOG.md", "## 1.5.0 — source candidate (publication pending #915)\n")
        result = subprocess.run([sys.executable, str(repo / "src/code_mower/release_identity.py"),
                                 "--tag", "v1.5.0", "--repo", str(repo)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("transient", result.stdout)
