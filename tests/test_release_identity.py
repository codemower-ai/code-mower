"""The final-tag text contract, including the immutable v1.4.1 contradiction."""

from __future__ import annotations

import copy
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

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
        for surface, fixture in (("README.md", "v141-readme.txt"),
                                 ("CHANGELOG.md", "v141-changelog.txt")):
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
        for mutation in (
            "conditional", "allowed-failure", "missing-validator", "unpinned-build",
            "event-sha-build", "event-sha-identity", "short-history", "unbound-ref",
            "unresolved-tag", "unbound-head", "unbound-dispatch", "wrong-event",
            "missing-output", "wrong-job-output", "conditional-checkout",
        ):
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
                elif mutation == "event-sha-build":
                    broken["build-distributions"]["steps"][0]["with"]["ref"] = "${{ github.sha }}"
                elif mutation == "event-sha-identity":
                    broken["release-identity"]["steps"][1]["with"]["ref"] = "${{ github.sha }}"
                elif mutation == "short-history":
                    broken["release-identity"]["steps"][1]["with"]["fetch-depth"] = 1
                elif mutation == "conditional-checkout":
                    broken["release-identity"]["steps"][1]["if"] = "false"
                elif mutation == "unbound-ref":
                    step["run"] = step["run"].replace('test "$ACTUAL_REF" = "refs/tags/$RELEASE_TAG"', "true")
                elif mutation == "unresolved-tag":
                    step["run"] = step["run"].replace('refs/tags/$RELEASE_TAG^{commit}', "HEAD")
                elif mutation == "unbound-head":
                    step["run"] = step["run"].replace('test "$(git rev-parse HEAD)" = "$RESOLVED_SHA"', "true")
                elif mutation == "unbound-dispatch":
                    step["run"] = step["run"].replace('test "$RESOLVED_SHA" = "$EXPECTED_SHA"', "true")
                elif mutation == "wrong-event":
                    step["env"]["EVENT_NAME"] = "release"
                elif mutation == "missing-output":
                    step["run"] = step["run"].split("printf 'resolved-sha=")[0]
                elif mutation == "wrong-job-output":
                    broken["release-identity"]["outputs"]["resolved-sha"] = "${{ github.sha }}"
                self.assertFalse(release_readiness._public_identity_gate_holds(broken))

    def test_same_validator_runs_for_dispatch_and_release_with_no_publish_bypass(self):
        jobs = self.workflow["jobs"]
        self.assertNotIn("if", jobs["release-identity"])
        self.assertNotIn("if", jobs["release-identity"]["steps"][-1])
        for publisher in ("publish-testpypi", "publish-pypi"):
            self.assertIn("release-identity", jobs[publisher]["needs"])
        self.assertEqual(jobs["build-distributions"]["needs"], "release-identity")
        self.assertNotIn("v1.4.2", (ROOT / ".github/workflows/release.yml").read_text())
        self.assertNotIn("github.sha", (ROOT / ".github/workflows/release.yml").read_text())
        for publisher in ("publish-testpypi", "publish-pypi"):
            steps = jobs[publisher]["steps"]
            self.assertTrue(steps[0]["uses"].startswith("actions/download-artifact@"))
            self.assertEqual(steps[0]["with"]["name"], "code-mower-dist")
            self.assertFalse(any("checkout@" in step.get("uses", "") or "run" in step for step in steps))

    def make_repo(self, *, annotated=False):
        fixture = ReleaseIdentityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.repo = fixture.repo
        shutil.copy(ROOT / "src/code_mower/release_identity.py", self.repo / "src/code_mower/release_identity.py")
        self.git("init", "-qb", "main")
        for key, value in (("user.name", "Release Fixture"), ("user.email", "fixture@example.invalid"),
                           ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
            self.git("config", key, value)
        self.git("add", ".")
        self.git("commit", "-qm", "Release fixture")
        self.tag_sha = self.git("rev-parse", "HEAD")
        self.git("tag", *( ["-a", "-m", "Release fixture"] if annotated else []), "v1.5.0")
        if annotated:
            self.assertNotEqual(self.git("rev-parse", "refs/tags/v1.5.0"), self.tag_sha)
        fixture.write_release("1.5.1")
        self.git("add", ".")
        self.git("commit", "-qm", "Default branch advances past the release")
        self.default_sha = self.git("rev-parse", "HEAD")
        self.assertNotEqual(self.default_sha, self.tag_sha)
        # An unqualified checkout could select this branch instead of the tag.
        self.git("branch", "v1.5.0")
        self.output = self.repo / "github-output"
        return fixture

    def git(self, *args, cwd=None):
        return subprocess.check_output(["git", *args], cwd=cwd or self.repo, text=True, stderr=subprocess.PIPE).strip()

    def context(self, event="release", *, tag="v1.5.0", ref=None, expected=None):
        return {
            "github": {"event_name": event, "sha": self.default_sha,
                       "ref": ref or f"refs/tags/{tag}", "ref_name": tag,
                       "event": {"action": "published", "release": {"tag_name": tag}} if event == "release" else {}},
            "inputs": {"expected_sha": self.tag_sha if expected is None else expected},
        }

    def render(self, text, context):
        """Evaluate only the context lookups/OR used by this workflow's refs and env."""
        def value(match):
            for alternative in match[1].split("||"):
                result = context
                for key in alternative.strip().split("."):
                    result = result.get(key, "") if isinstance(result, dict) else ""
                if result:
                    return str(result)
            return ""
        return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", value, text)

    def run_step(self, step, context):
        # Running a venv's pytest entry point directly does not activate that
        # venv or put its bin directory on PATH. Workflow snippets intentionally
        # call plain `python` after actions/setup-python, so mirror that contract
        # with the interpreter running this test instead of an ambient system
        # Python that may be older than Code Mower supports.
        test_runtime_path = os.pathsep.join(
            value
            for value in (str(Path(sys.executable).parent), os.environ.get("PATH", ""))
            if value
        )
        return subprocess.run(
            ["bash", "-c", step["run"]], cwd=self.repo, text=True, capture_output=True,
            env={**os.environ, "GITHUB_OUTPUT": str(self.output),
                 "GITHUB_SHA": context["github"]["sha"],
                 "PATH": test_runtime_path,
                 **{key: self.render(value, context) for key, value in step.get("env", {}).items()}},
            timeout=30,
        )

    def validate(self, context, *, checkout=True):
        self.output.write_text("")
        steps = self.workflow["jobs"]["release-identity"]["steps"]
        if context["github"]["event_name"] == "workflow_dispatch":
            result = self.run_step(steps[0], context)
            if result.returncode:
                return result
        if checkout:
            self.git("checkout", "--detach", self.render(steps[1]["with"]["ref"], context))
        return self.run_step(steps[-1], context)

    def test_published_release_validates_and_builds_the_tag_even_when_event_sha_differs(self):
        for annotated in (False, True):
            with self.subTest(annotated=annotated):
                self.make_repo(annotated=annotated)
                context = self.context()
                result = self.validate(context)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.git("rev-parse", "HEAD"), self.tag_sha)
                self.assertEqual(self.output.read_text(), f"resolved-sha={self.tag_sha}\n")
                jobs = self.workflow["jobs"]
                context["steps"] = {"identity": {"outputs": dict(
                    line.split("=", 1) for line in self.output.read_text().splitlines())}}
                context["needs"] = {"release-identity": {"outputs": {
                    key: self.render(value, context) for key, value in jobs["release-identity"]["outputs"].items()}}}
                # A tag moving after validation must not change the downstream build.
                self.git("tag", "-f", "v1.5.0", self.default_sha)
                build_repo = self.repo / "build-checkout"
                self.git("clone", "--quiet", "--no-hardlinks", str(self.repo), str(build_repo))
                build_ref = self.render(jobs["build-distributions"]["steps"][0]["with"]["ref"], context)
                self.git("checkout", "--detach", build_ref, cwd=build_repo)
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=build_repo), self.tag_sha)
                # Package the downstream checkout, and inspect its actual wheel contents.
                wheels = self.repo / "wheels"
                built = subprocess.run(
                    [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheels), str(build_repo)],
                    capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                wheel, = wheels.glob("*.whl")
                with zipfile.ZipFile(wheel) as artifact:
                    self.assertEqual(artifact.read("code_mower/__init__.py").decode(), '__version__ = "1.5.0"\n')
                    metadata = artifact.read("code_mower-1.5.0.dist-info/METADATA").decode()
                    self.assertIn(versioning.public_baseline_sentence("1.5.0"), metadata)

    def test_manual_dispatch_compares_the_resolved_tag_commit_with_expected_sha(self):
        for annotated in (False, True):
            with self.subTest(annotated=annotated):
                self.make_repo(annotated=annotated)
                for expected in ("", "not-a-sha", "a" * 39, self.default_sha, self.tag_sha):
                    with self.subTest(expected=expected):
                        result = self.validate(self.context("workflow_dispatch", expected=expected))
                        self.assertEqual(result.returncode == 0, expected == self.tag_sha, result.stdout + result.stderr)
                        self.assertEqual(bool(self.output.read_text()), expected == self.tag_sha)

    def test_release_step_rejects_wrong_ref_missing_tag_and_wrong_checkout(self):
        self.make_repo()
        for event in ("release", "workflow_dispatch"):
            for ref in ("refs/heads/v1.5.0", "refs/tags/v1.5.1"):
                with self.subTest(event=event, ref=ref):
                    result = self.validate(self.context(event, ref=ref))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.output.read_text(), "")
        self.git("checkout", "--detach", self.default_sha)
        result = self.validate(self.context(), checkout=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(), "")
        self.git("tag", "-d", "v1.5.0")
        result = self.validate(self.context(), checkout=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_text(), "")

    def test_tag_version_and_public_text_failures_never_emit_a_build_sha(self):
        fixture = self.make_repo()
        for tag in ("v1.5.1", "v1.5.00"):
            with self.subTest(tag=tag):
                self.git("tag", tag, self.tag_sha)
                result = self.validate(self.context(tag=tag))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.output.read_text(), "")
        self.git("checkout", "--detach", self.tag_sha)
        fixture.write("CHANGELOG.md", "## 1.5.0 — source candidate (publication pending #915)\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Unfinished tagged public text")
        self.git("tag", "-f", "v1.5.0")
        for event in ("release", "workflow_dispatch"):
            with self.subTest(event=event):
                result = self.validate(self.context(event, expected=self.git("rev-parse", "HEAD")))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("transient", result.stdout)
                self.assertEqual(self.output.read_text(), "")
