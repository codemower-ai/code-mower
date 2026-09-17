"""Version-identity and Board-boundary regressions for the v1.4.2 release, #952."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from code_mower import __version__, release_readiness
from code_mower import package as package_module

ROOT = Path(__file__).resolve().parents[1]


class PublicReleaseChecklistCandidateStatusTests(unittest.TestCase):
    def test_current_entrypoint_is_141_and_142_is_the_target_not_current(self):
        checklist = (ROOT / "docs/public-release-checklist.md").read_text(encoding="utf-8")
        self.assertIn(
            "The current published package-index release entrypoint is\n"
            "  `code-mower==1.4.1` (GitHub tag `v1.4.1`)",
            checklist,
        )
        self.assertIn(
            "The target package-index entrypoint after\n"
            "  v1.4.2's acceptance is `code-mower==1.4.2` (GitHub tag `v1.4.2`)",
            checklist,
        )
        # Never re-introduce the ambiguous "current entrypoint is v1.4.2"
        # framing while v1.4.2 is still an unpublished candidate.
        self.assertNotIn("current package-index release entrypoint is `code-mower==1.4.2`", checklist)
        self.assertNotIn("The corresponding GitHub tag is\n  `v1.4.2`", checklist)


class RoadmapDocFactsTests(unittest.TestCase):
    def test_role_policy_and_effective_authority_are_recorded_as_shipped(self):
        roadmap = (ROOT / "docs/current-state-and-roadmap.md").read_text(encoding="utf-8")
        self.assertIn(
            "These are main-line\nstabilization changes that shipped in `v1.4.1`.",
            roadmap,
        )
        self.assertNotIn("awaiting the next package", roadmap)
        self.assertIn("both are part of the\npublished `v1.4.1` artifact", roadmap)

    def test_graphify_915_closeout_is_recorded_complete_not_pending(self):
        roadmap = (ROOT / "docs/current-state-and-roadmap.md").read_text(encoding="utf-8")
        self.assertIn(
            "completing the release-specific comparative scorecard, campaign,\n"
            "Board, and fresh aggregate evidence as part of that closeout",
            roadmap,
        )
        self.assertNotIn("remain separately tracked release-specific follow-ups", roadmap)
        self.assertNotIn("remain pending", roadmap.partition("Release #915")[2][:200])

    def test_board_section_names_the_merging_prs_not_just_issues(self):
        roadmap = (ROOT / "docs/current-state-and-roadmap.md").read_text(encoding="utf-8")
        self.assertIn("via\n[PR #1001](https://github.com/codemower-ai/code-mower/pull/1001)", roadmap)
        self.assertIn("via\n[PR #1003](https://github.com/codemower-ai/code-mower/pull/1003)", roadmap)
        self.assertIn("#961 via PR #1001", roadmap)
        self.assertNotIn("are drafts behind\nmain that need refreshing", roadmap)
        self.assertNotIn("Board work is underway", roadmap)


class ChangelogAndRunbookInclusionTests(unittest.TestCase):
    def test_changelog_v142_section_lists_the_actually_shipping_board_work(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        v142_section = changelog.partition("## 1.4.2")[2].partition("\n## 1.4.1")[0]
        unreleased_section = changelog.partition("## Unreleased")[2]
        self.assertIn("code-mower board service", v142_section)
        self.assertIn("code-mower board stop --repo OWNER/REPO", v142_section)
        self.assertIn("#999", v142_section)
        self.assertIn("#1003", v142_section)
        # Work that actually ships in 1.4.2 is not left double-booked under
        # Unreleased.
        self.assertNotIn("code-mower board service` manages", unreleased_section)
        self.assertNotIn("board stop --repo OWNER/REPO", unreleased_section)

    def test_changelog_v141_section_is_marked_published_not_pending(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## 1.4.1 — published", changelog)
        self.assertNotIn("## 1.4.1 — source candidate", changelog)

    def test_current_runbook_names_the_actual_v142_required_inclusion(self):
        runbook = (ROOT / "docs/pypi-release.md").read_text(encoding="utf-8")
        self.assertIn("#999/#1000/#1001/#1002/#1003", runbook)
        self.assertNotIn("including #876", runbook)


class UpgradeRehearsalTests(unittest.TestCase):
    def test_runbook_binds_a_real_141_to_142_upgrade_with_preserved_state(self):
        runbook = (ROOT / "docs/pypi-release.md").read_text(encoding="utf-8")
        step = runbook.partition(
            "### 17. Rehearse the 1.4.1-to-1.4.2 upgrade in place, preserving existing state"
        )[2]
        step = step.partition("\n## Cache Bypass")[0]
        self.assertTrue(step, "step 17 is missing from the current runbook")
        # Hashing is portable to headless Linux, not macOS-only shasum.
        self.assertNotIn("shasum -a 256", step)
        self.assertIn("import hashlib", step)
        # v1.4.1 is downloaded and digest-bound before install, not just
        # resolved from the index.
        self.assertIn("code-mower==1.4.1", step)
        self.assertIn('V141_WHEEL="$V141_DOWNLOAD_DIR/code_mower-1.4.1-py3-none-any.whl"', step)
        self.assertIn("V141_WHEEL_SHA256=", step)
        self.assertIn('"${V141_WHEEL}[coworker]"', step)
        self.assertIn('test "$("$UPGRADE_ENV/bin/code-mower" --version)" = "code-mower 1.4.1"', step)
        # Creates preserved state before upgrading, and hashes it both sides.
        self.assertIn("PRESERVED_CONFIG_SHA256_BEFORE=", step)
        # The upgrade installs the exact wheel step 9 already digest-verified,
        # never a fresh `code-mower==1.4.2` index re-resolution.
        self.assertIn(
            'V142_WHEEL="$PYPI_DOWNLOAD_DIR/code_mower-1.4.2-py3-none-any.whl"', step
        )
        self.assertIn('--upgrade "${V142_WHEEL}[coworker]"', step)
        self.assertNotIn("--upgrade 'code-mower[coworker]==1.4.2'", step)
        self.assertIn('test "$("$UPGRADE_ENV/bin/code-mower" --version)" = "code-mower 1.4.2"', step)
        self.assertIn("PRESERVED_CONFIG_SHA256_AFTER=", step)
        self.assertIn(
            'test "$PRESERVED_CONFIG_SHA256_AFTER" = "$PRESERVED_CONFIG_SHA256_BEFORE"',
            step,
        )
        # Runs doctor against the preserved config after the upgrade.
        self.assertIn('"$UPGRADE_ENV/bin/code-mower" doctor', step)
        # A cold install cannot substitute for having actually upgraded.
        self.assertIn("do not record upgrade coverage as passed on a\ncold-install substitute", step)

    def test_release_notes_and_qualification_claim_upgrade_coverage_that_exists(self):
        release_notes = (ROOT / "docs/v142-release-notes.md").read_text(encoding="utf-8")
        qualification = (ROOT / "docs/v142-qualification.md").read_text(encoding="utf-8")
        runbook = (ROOT / "docs/pypi-release.md").read_text(encoding="utf-8")
        self.assertIn("1.4.1-to-1.4.2 upgrade rehearsal", release_notes)
        self.assertIn("upgrade from v1.4.1", qualification)
        # The claim in the candidate docs must point at a runbook step that
        # actually exists, not an unimplemented promise.
        self.assertIn("### 17. Rehearse the 1.4.1-to-1.4.2 upgrade in place", runbook)


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


STALE_BOARD_COUNT_PHRASES = (
    "three existing Board",
    "currently three",
    "all three Board",
    "three-service",
    "three Boards",
)


class BoardRestartBoundaryTests(unittest.TestCase):
    def test_qualification_doc_names_the_verified_two_service_inventory(self):
        qualification = (ROOT / "docs/v142-qualification.md").read_text(encoding="utf-8")
        # A read-only `board list --json` verified exactly two live local
        # Board services pre-release: 5332 (the public repo) plus one
        # additional private-repository port. Posture (managed vs transient)
        # is classified from `board service status`, never assumed.
        self.assertIn("observed\ntwo live local Board services", qualification)
        self.assertIn("port 5332", qualification)
        self.assertIn("board service status", qualification)
        for phrase in STALE_BOARD_COUNT_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, qualification)
        self.assertIn("serving ==", qualification)
        self.assertIn("1.4.2", qualification)

    def test_release_notes_do_not_claim_951_hosted_canary_or_close_951(self):
        release_notes = (ROOT / "docs/v142-release-notes.md").read_text(encoding="utf-8")
        self.assertIn("bounded hosted Devin canary is still pending", release_notes)
        self.assertIn("does not claim the hosted result or close", release_notes)

    def test_release_notes_name_the_verified_two_service_inventory(self):
        release_notes = (ROOT / "docs/v142-release-notes.md").read_text(encoding="utf-8")
        self.assertIn("two\nobserved local Board processes", release_notes)
        self.assertIn("port 5332", release_notes)
        for phrase in STALE_BOARD_COUNT_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, release_notes)

    def test_current_runbook_and_hygiene_use_reconciled_board_heading(self):
        runbook = (ROOT / "docs/pypi-release.md").read_text(encoding="utf-8")
        hygiene = (ROOT / "tests/test_release_hygiene.py").read_text(encoding="utf-8")
        self.assertIn(
            "### 15. Restart the reconciled Board inventory from the release",
            runbook,
        )
        self.assertIn(
            'runbook.partition("### 15. Restart the reconciled Board inventory")',
            hygiene,
        )
        for phrase in STALE_BOARD_COUNT_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, runbook)
        # v1.4.0's own historical runbook is immutable and out of scope here.
        self.assertTrue((ROOT / "docs/v140-release-runbook.md").is_file())


class InstalledPromptPackTests(unittest.TestCase):
    def test_literal_starter_and_explicit_config_walkthrough(self):
        """Exercise installed 1.4.2 code, with no provider login or network doctor probes."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            supplied = os.environ.get("CODE_MOWER_QUALIFICATION_WHEEL")
            if supplied:
                wheel = Path(supplied)
                self.assertTrue(wheel.is_absolute() and wheel.is_file())
            else:
                built = subprocess.run(
                    [sys.executable, "-m", "pip", "wheel", "--no-deps",
                     "--wheel-dir", str(root / "wheels"), str(ROOT)],
                    cwd=root, capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                wheel, = (root / "wheels").glob("*.whl")
            installed = root / "installed"
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps", "--no-compile",
                 "--target", str(installed), str(wheel)],
                cwd=root, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # Only the harness comes from this file. Product imports resolve to
            # the downloaded/built wheel, never to checkout modules.
            program = r'''
import io, json, os, shutil, sys
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
sys.meta_path = [f for f in sys.meta_path if '__editable__' not in str(f)]
sys.path.insert(0, sys.argv[1])
import code_mower
from code_mower import cli, package
from code_mower.config import load_config
assert Path(code_mower.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
assert code_mower.__version__ == '1.4.2'
empty_store = Path.cwd() / 'empty-provider-store'
empty_store.mkdir()
def run(args, doctor=False):
    if doctor:
        args += ['--provider-config-dir', str(empty_store)]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = cli.main(args)
    if doctor:
        # Explicitly selected hosted transport has no credentials in this
        # fixture. Configuration/remediation is tested, not live readiness.
        assert status in (0, 1), (args, status, err.getvalue())
        assert not err.getvalue(), err.getvalue()
    else:
        assert status == 0, (args, status, err.getvalue(), out.getvalue())
    if doctor and '--json' in args:
        report = json.loads(out.getvalue())
        for check in report['checks']:
            assert sys.argv[1] not in str(check.get('remediation', ''))
    return out.getvalue()
repo = Path.cwd() / 'fresh'
repo.mkdir()
previous = Path.cwd()
os.chdir(repo)
try:
    profile = 'deep_review'
    source = Path('code-mower.yml')
    selector = ['--packaged-starter']
    before = source.read_bytes() if source.exists() else None
    discovery = json.loads(run(['doctor', *selector, '--profile', profile, '--devin', '--json'], True))
    assert discovery['mode'] == 'doctor'
    for mode in ('--dry-run', '--apply'):
        command = ['init', *selector, '--profile', profile, '--set-transport', 'devin=devin_api_v3', mode, '--json']
        if mode == '--apply':
            command += ['--output-dir', '.code-mower.generated', '--skip-actionlint', '--skip-github-labels']
        payload = json.loads(run(command))
        if mode == '--dry-run':
            assert payload['profile']['id'] == profile
        else:
            staged_plan = json.loads(Path('.code-mower.generated/code-mower-init-plan.json').read_text())
            assert staged_plan['profile']['id'] == profile
        assert (source.read_bytes() if source.exists() else None) == before
    shutil.copyfile('.code-mower.generated/code-mower.yml', source)
    config = load_config(source)
    assert config['session_defaults']['transports']['devin'] == 'devin_api_v3'
    rendered = run(['doctor', str(source), '--profile', profile, '--devin'], True)
    assert f'doctor {source} --profile {profile} --devin' in rendered, rendered
    assert '--packaged-starter' not in rendered, rendered
finally:
    os.chdir(previous)
'''
            result = subprocess.run(
                [sys.executable, "-I", "-c", program, str(installed)], cwd=root,
                env={"PATH": os.defpath}, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
