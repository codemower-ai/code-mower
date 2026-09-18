"""Published-identity and Board-boundary regressions for the v1.4.2 release.

Release #952 is closed: v1.4.2 was published from release commit
``55339bf1acf76d33be5937e80bdaad772e0b2bf5`` under the annotated ``v1.4.2`` tag.
These tests protect the *published* identity in current-facing documentation.
They deliberately assert on facts -- version pins, evidence identifiers, link
shape, packaged-template agreement -- rather than on sentence wording, so
ordinary editorial passes do not break them.
"""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from code_mower import __version__, release_readiness, versioning
from code_mower import package as package_module

ROOT = Path(__file__).resolve().parents[1]
RELEASE_COMMIT = "55339bf1acf76d33be5937e80bdaad772e0b2bf5"
AUDITED_HEAD = "32706cf5a01af5863d6c713b83dfb196efde6d4c"
WHEEL_SHA256 = "f8bf24dd8a982ed5ab28302e837cd5d2aeece6d984ed1c44fcb4688c3fb7a522"
SDIST_SHA256 = "aff202eea9748ab3734ea6b90ba3b48ea5aea1e21ae87fba03b77b64d5cdec42"

#: Current-facing pages a new user or agent may follow. Historical release
#: notes, qualification records for earlier versions, and archived transcripts
#: are deliberately excluded: they are preserved, not reconciled.
CURRENT_FACING_DOCS = (
    "README.md",
    "docs/install.md",
    "docs/quickstart.md",
    "docs/try-in-10-minutes.md",
    "docs/current-state-and-roadmap.md",
    "docs/release-history.md",
    "docs/public-release-checklist.md",
    "docs/oss-v1-checklist.md",
    "docs/early-adopter-invite-runbook.md",
    "docs/early-adopter-v05.md",
    "docs/friendly-user-rollout-v05.md",
    "docs/first-user-install-rehearsal.md",
    "docs/sessions.md",
    "docs/github-setup.md",
    "docs/builders-grok-cursor.md",
    "docs/graphify-setup.md",
    "docs/v142-release-notes.md",
    "docs/v142-qualification.md",
)

#: Wording that describes v1.4.2 as unpublished. Any of these in a
#: current-facing page is a stale-candidate regression.
STALE_CANDIDATE_PHRASES = (
    "source candidate",
    "not yet published",
    "publication pending",
    "pending #952",
    "pending [#952]",
    "apply after publication",
)


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


class PublishedIdentityTests(unittest.TestCase):
    def test_current_docs_do_not_describe_v142_as_an_unpublished_candidate(self):
        offenders = []
        for relative in CURRENT_FACING_DOCS:
            lowered = _read(relative).lower()
            for phrase in STALE_CANDIDATE_PHRASES:
                if phrase in lowered:
                    offenders.append(f"{relative}: {phrase!r}")
        self.assertEqual(offenders, [], "\n" + "\n".join(offenders))

    def test_current_docs_do_not_still_call_v141_the_current_release(self):
        # v1.4.1 must stay nameable as history, but no current-facing page may
        # present it as the entrypoint a reader should install.
        for relative in CURRENT_FACING_DOCS:
            with self.subTest(doc=relative):
                text = " ".join(_read(relative).split())
                self.assertNotIn("current published package-index release entrypoint is `code-mower==1.4.1`", text)
                self.assertNotIn("The current package-index release baseline is `v1.4.1`", text)

    def test_shared_baseline_sentence_matches_the_published_version(self):
        sentence = versioning.public_baseline_sentence(__version__)
        self.assertIn("`v1.5.0`", sentence)
        self.assertIn("`code-mower==1.5.0`", sentence)
        for relative in ("README.md", "docs/current-state-and-roadmap.md",
                         "docs/friendly-user-rollout-v05.md"):
            with self.subTest(doc=relative):
                self.assertIn(sentence, " ".join(_read(relative).split()))

    def test_release_records_bind_the_exact_published_evidence(self):
        for relative in ("docs/v142-release-notes.md", "docs/v142-qualification.md"):
            with self.subTest(doc=relative):
                text = _read(relative)
                self.assertIn(RELEASE_COMMIT, text)
                self.assertIn(AUDITED_HEAD, text)
                self.assertIn(WHEEL_SHA256, text)
                self.assertIn(SDIST_SHA256, text)
                self.assertIn("35189302150", text)

    def test_release_records_point_readers_past_the_immutable_tag_snapshots(self):
        # The v1.4.2 tag carries the prepublication copies of both pages and is
        # never rewritten, so each page on main has to say so.
        for relative in ("docs/v142-release-notes.md", "docs/v142-qualification.md"):
            with self.subTest(doc=relative):
                text = _read(relative)
                self.assertIn("tag", text)
                self.assertIn("prepublication", text)

    def test_951_stays_an_open_unclaimed_boundary(self):
        for relative in ("README.md", "docs/v142-release-notes.md",
                         "docs/v142-qualification.md",
                         "docs/current-state-and-roadmap.md"):
            with self.subTest(doc=relative):
                text = _read(relative)
                self.assertIn("#951", text)
                self.assertIn("not claimed", " ".join(text.split()).lower())

    def test_no_current_doc_claims_the_hosted_canary_ran(self):
        for relative in CURRENT_FACING_DOCS:
            with self.subTest(doc=relative):
                text = " ".join(_read(relative).split()).lower()
                for claim in ("hosted devin canary passed",
                              "hosted canary passed",
                              "canary completed"):
                    self.assertNotIn(claim, text)

    def test_release_triggered_verification_skip_is_explained_not_reported_as_failure(self):
        qualification = " ".join(_read("docs/v142-qualification.md").split())
        self.assertIn("35189721623", qualification)
        self.assertIn("intentionally skipped", qualification)


class ReadmeLinkTests(unittest.TestCase):
    """The README is the PyPI long description; relative links do not resolve there."""

    LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
    BLOB_PREFIX = "https://github.com/codemower-ai/code-mower/blob/main/"

    def test_readme_has_no_relative_links(self):
        readme = _read("README.md")
        relative = [
            destination
            for destination in self.LINK_RE.findall(readme)
            if not destination.startswith(("http://", "https://", "mailto:", "#"))
        ]
        self.assertEqual(relative, [], "README links must be absolute for PyPI")

    def test_readme_repository_links_point_at_files_that_exist(self):
        readme = _read("README.md")
        missing = []
        checked = 0
        for destination in self.LINK_RE.findall(readme):
            if not destination.startswith(self.BLOB_PREFIX):
                continue
            checked += 1
            relative = destination[len(self.BLOB_PREFIX):].partition("#")[0]
            if not (ROOT / relative).exists():
                missing.append(relative)
        self.assertGreater(checked, 20, "expected the README doc index to be absolute")
        self.assertEqual(missing, [])

    def test_pyproject_still_ships_the_readme_as_the_long_description(self):
        pyproject = _read("pyproject.toml")
        self.assertIn('readme = "README.md"', pyproject)


class PackagedTemplateConsistencyTests(unittest.TestCase):
    LANE_README = "templates/lanes/README.md"
    PACKAGED_LANE_README = "src/code_mower/templates/lanes/README.md"

    def test_repo_and_packaged_lane_readme_are_identical(self):
        self.assertEqual(_read(self.LANE_README), _read(self.PACKAGED_LANE_README))

    def test_both_lane_readmes_document_the_supported_never_expiry(self):
        for relative in (self.LANE_README, self.PACKAGED_LANE_README):
            with self.subTest(template=relative):
                self.assertIn("`YYYY-MM-DD`, or `never` for a non-expiring token.",
                              _read(relative))

    def test_never_expiry_is_what_init_actually_advertises(self):
        init_source = _read("src/code_mower/init.py")
        self.assertIn("(YYYY-MM-DD or never)", init_source)


class PublicReleaseChecklistTests(unittest.TestCase):
    def test_checklist_names_v150_as_the_published_entrypoint(self):
        checklist = " ".join(_read("docs/public-release-checklist.md").split())
        self.assertIn(
            "The current package-index release entrypoint is "
            "`code-mower==1.5.0` (GitHub tag `v1.5.0`)",
            checklist,
        )


class RoadmapDocFactsTests(unittest.TestCase):
    def test_role_policy_and_effective_authority_are_recorded_as_shipped(self):
        roadmap = _read("docs/current-state-and-roadmap.md")
        self.assertIn(
            "These are main-line\nstabilization changes that shipped in `v1.4.1`.",
            roadmap,
        )
        self.assertNotIn("awaiting the next package", roadmap)
        self.assertIn("both are part of the\npublished `v1.4.1` artifact", roadmap)

    def test_graphify_915_closeout_is_recorded_complete_not_pending(self):
        roadmap = _read("docs/current-state-and-roadmap.md")
        self.assertIn(
            "completing the release-specific comparative scorecard, campaign,\n"
            "Board, and fresh aggregate evidence as part of that closeout",
            roadmap,
        )
        self.assertNotIn("remain separately tracked release-specific follow-ups", roadmap)

    def test_all_three_v14_releases_are_recorded_as_shipped(self):
        roadmap = " ".join(_read("docs/current-state-and-roadmap.md").split())
        self.assertIn("`v1.4.0`, `v1.4.1` and `v1.4.2` have all shipped", roadmap)
        self.assertIn("Board shipped as `v1.4.2` from release commit `55339bf`", roadmap)
        self.assertNotIn("Board work is underway", roadmap)
        self.assertNotIn("Board implementation is accepted on `main`, not underway", roadmap)

    def test_v150_slack_is_the_active_phase_and_no_longer_deferred(self):
        roadmap = " ".join(_read("docs/current-state-and-roadmap.md").split())
        self.assertIn("active, `v1.5.0`", roadmap)
        self.assertIn("This is the current roadmap phase.", roadmap)
        self.assertNotIn(
            "This runtime work is deferred until the sequence above is complete.",
            roadmap,
        )

    def test_board_prs_are_linked_as_pulls_and_952_is_not_called_a_pr(self):
        roadmap = _read("docs/current-state-and-roadmap.md")
        for number in (999, 1000, 1001, 1002, 1003, 1006):
            with self.subTest(pull=number):
                self.assertIn(
                    f"https://github.com/codemower-ai/code-mower/pull/{number}",
                    roadmap,
                )
                self.assertNotIn(
                    f"https://github.com/codemower-ai/code-mower/issues/{number}",
                    roadmap,
                )
        # #952 and #961 are issues, not pull requests.
        for number in (951, 952, 961):
            with self.subTest(issue=number):
                self.assertNotIn(
                    f"https://github.com/codemower-ai/code-mower/pull/{number}",
                    roadmap,
                )
        self.assertNotIn("the release PR #952", roadmap)


class ChangelogAndRunbookInclusionTests(unittest.TestCase):
    def test_unreleased_section_is_first(self):
        changelog = _read("CHANGELOG.md")
        self.assertLess(
            changelog.index("## Unreleased"),
            changelog.index("## 1.4.2"),
            "Unreleased belongs above the released sections",
        )

    def test_changelog_v142_section_is_marked_published_and_lists_the_board_work(self):
        changelog = _read("CHANGELOG.md")
        self.assertIn("## 1.4.2 — published", changelog)
        self.assertNotIn("## 1.4.2 — source candidate", changelog)
        v142_section = changelog.partition("## 1.4.2")[2].partition("\n## 1.4.1")[0]
        self.assertIn("code-mower board service", v142_section)
        self.assertIn("code-mower board stop --repo OWNER/REPO", v142_section)
        self.assertIn("#999", v142_section)
        self.assertIn("#1003", v142_section)
        self.assertIn(RELEASE_COMMIT, v142_section)

    def test_unreleased_section_does_not_double_book_released_work(self):
        changelog = _read("CHANGELOG.md")
        unreleased = changelog.partition("## Unreleased")[2].partition("\n## 1.4.2")[0]
        self.assertNotIn("code-mower board service` manages", unreleased)
        self.assertNotIn("board stop --repo OWNER/REPO", unreleased)

    def test_unreleased_carries_the_merged_graphify_compatibility_work(self):
        """PR #1007 merged to main after v1.4.2 was published.

        Its entry belongs under Unreleased -- on main, in no published package
        -- and must not be folded into the immutable 1.4.2 section.
        """
        changelog = _read("CHANGELOG.md")
        unreleased = changelog.partition("## Unreleased")[2].partition("\n## 1.4.2")[0]
        v142_section = changelog.partition("## 1.4.2")[2].partition("\n## 1.4.1")[0]
        collapsed = " ".join(unreleased.split())
        self.assertIn("16 MiB", collapsed)
        self.assertIn("provider-manifest reader", collapsed)
        self.assertIn("doc_ref", collapsed)
        self.assertIn("__tests__", collapsed)
        self.assertNotIn("16 MiB", " ".join(v142_section.split()))
        self.assertNotIn("doc_ref", v142_section)

    def test_changelog_v141_section_is_marked_published_not_pending(self):
        changelog = _read("CHANGELOG.md")
        self.assertIn("## 1.4.1 — published", changelog)
        self.assertNotIn("## 1.4.1 — source candidate", changelog)

    def test_current_runbook_names_the_actual_v142_required_inclusion(self):
        runbook = _read("docs/pypi-release.md")
        self.assertIn("#999/#1000/#1001/#1002/#1003", runbook)
        self.assertNotIn("including #876", runbook)


class UpgradeRehearsalTests(unittest.TestCase):
    def test_runbook_binds_a_real_141_to_142_upgrade_with_preserved_state(self):
        runbook = _read("docs/pypi-release.md")
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

    def test_release_records_claim_upgrade_coverage_that_exists(self):
        release_notes = _read("docs/v142-release-notes.md")
        qualification = _read("docs/v142-qualification.md")
        runbook = _read("docs/pypi-release.md")
        self.assertIn("1.4.1-to-1.4.2 upgrade", release_notes)
        self.assertIn("upgrade from v1.4.1", qualification)
        # The claim in the release docs must point at a runbook step that
        # actually exists, not an unimplemented promise.
        self.assertIn("### 17. Rehearse the 1.4.1-to-1.4.2 upgrade in place", runbook)


class VersionIdentityTests(unittest.TestCase):
    def test_source_version_is_1_5_0(self):
        self.assertEqual(__version__, "1.5.0")

    def test_committed_manifest_version_matches_source(self):
        manifest = package_module.generate_committed_package_manifest(ROOT)
        self.assertEqual(manifest["package"]["version"], __version__)

    def test_release_tag_for_current_version(self):
        self.assertEqual(release_readiness._release_tag_for_version(__version__), "v1.5.0")


class RunbookIdentityTests(unittest.TestCase):
    def test_pypi_release_doc_carries_the_v142_runbook_heading(self):
        doc = _read("docs/pypi-release.md")
        self.assertIn(
            f"## v1.4.2 {release_readiness.POST_MERGE_RUNBOOK_HEADING}",
            doc,
        )

    def test_release_notes_and_qualification_docs_exist_for_v142(self):
        release_notes = _read("docs/v142-release-notes.md")
        qualification = _read("docs/v142-qualification.md")
        self.assertIn("# Code Mower v1.4.2 Release Notes", release_notes)
        self.assertIn("v1.4.2 qualification and evidence matrix", qualification)
        # v1.4.1's own historical documents must remain present.
        self.assertTrue((ROOT / "docs/v141-release-notes.md").is_file())
        self.assertTrue((ROOT / "docs/v141-qualification.md").is_file())

    def test_preserved_v141_candidate_docs_carry_a_completed_release_banner(self):
        for relative in ("docs/v141-release-notes.md", "docs/v141-qualification.md"):
            with self.subTest(doc=relative):
                text = " ".join(_read(relative).split())
                self.assertIn("v1.4.1 is a completed release", text)
                self.assertIn("releases/tag/v1.4.1", text)

    def test_release_history_orders_v142_before_v141_before_v131(self):
        release_history = _read("docs/release-history.md")
        self.assertLess(
            release_history.index("[v1.4.2 release notes](v142-release-notes.md)"),
            release_history.index("[v1.4.1 release notes](v141-release-notes.md)"),
        )
        self.assertLess(
            release_history.index("[v1.4.1 release notes](v141-release-notes.md)"),
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
        qualification = _read("docs/v142-qualification.md")
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
        self.assertIn("serving == installed == 1.4.2", qualification)

    def test_release_notes_record_the_verified_two_service_restart(self):
        release_notes = _read("docs/v142-release-notes.md")
        self.assertIn("port 5332", release_notes)
        self.assertIn("private-repository Board", release_notes)
        for phrase in STALE_BOARD_COUNT_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, release_notes)

    def test_board_service_lifecycle_table_has_no_stranded_row(self):
        lifecycle = _read("docs/board-service-lifecycle.md")
        refusals = lifecycle.partition("## Fail-closed refusals")[2]
        stranded = [
            line
            for index, line in enumerate(refusals.splitlines())
            if line.startswith("| `")
            and index
            and not refusals.splitlines()[index - 1].startswith("|")
        ]
        self.assertEqual(stranded, [], "a table row is stranded outside its table")
        # delayed_health_failed is decided after the apply, so it belongs with
        # delayed health rather than with the no-state-change refusals.
        delayed = lifecycle.partition("## Delayed health")[2].partition("## Fail-closed")[0]
        self.assertIn("delayed_health_failed", delayed)

    def test_current_runbook_and_hygiene_use_reconciled_board_heading(self):
        runbook = _read("docs/pypi-release.md")
        hygiene = _read("tests/test_release_hygiene.py")
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


class BoardAndGraphifyDiscoverabilityTests(unittest.TestCase):
    def test_readme_surfaces_the_persistent_board_service_and_its_platform_boundary(self):
        readme = " ".join(_read("README.md").split())
        self.assertIn("code-mower board service", readme)
        self.assertIn("launchd", readme)
        self.assertIn("every other platform refuses", readme)
        self.assertIn("board-service-lifecycle.md", readme)

    def test_readme_navigates_to_graphify_setup_lifecycle_and_queries(self):
        readme = _read("README.md")
        for target in ("docs/graphify-setup.md",
                       "docs/context-graph-lifecycle.md",
                       "docs/context-graph-queries.md"):
            with self.subTest(target=target):
                self.assertIn(target, readme)

    def test_graphify_setup_documents_the_ramp_up_flow_and_its_exclusions(self):
        setup = _read("docs/graphify-setup.md")
        for command in ("context-graph doctor",
                        "context-graph build",
                        "context-graph status",
                        "context-graph connect",
                        "context-graph connection-status",
                        "context-graph query",
                        "context-graph refresh",
                        "context-graph disconnect",
                        "context-graph remove"):
            with self.subTest(command=command):
                self.assertIn(command, setup)
        collapsed = " ".join(setup.split())
        for exclusion in ("Untracked and\n  ignored files", "Symlinks and submodules",
                          "Committed private state"):
            with self.subTest(exclusion=exclusion):
                self.assertIn(" ".join(exclusion.split()), collapsed)
        self.assertIn("Nothing watches the working tree", collapsed)

    def test_graphify_evaluation_is_framed_as_a_dated_historical_record(self):
        evaluation = " ".join(_read("docs/graphify-evaluation.md").split())
        self.assertIn("Historical record. Graphify has since shipped.", evaluation)
        self.assertIn("2026-09-12", evaluation)
        self.assertIn("graphify-setup.md", evaluation)
        # The recorded benchmark evidence is preserved, not rewritten.
        self.assertIn("Clean-room experiment", evaluation)

    def test_graphify_docs_separate_the_published_package_from_current_main(self):
        """v1.4.2 history stays distinct from the v1.5.0 compatibility additions."""
        setup = " ".join(_read("docs/graphify-setup.md").split())
        self.assertIn("v1.5.0 compatibility and existing generations", setup)
        self.assertIn("/pull/1007", setup)
        # The boundary is stated in both directions.
        self.assertIn("merged to `main`", setup)
        self.assertIn("none of it is in the historical `v1.4.2` package", setup)
        # An upgrade alone does not repair a generation built earlier.
        self.assertIn("does not repair a generation you already built", setup)
        self.assertIn("context-graph refresh", setup)

        roadmap = " ".join(_read("docs/current-state-and-roadmap.md").split())
        self.assertIn("/pull/1007", roadmap)
        self.assertIn("the historical `v1.4.2` package does not contain them", roadmap)

    def test_graphify_setup_marks_v150_additions_and_preserves_acquisition_guidance(self):
        raw = _read("docs/graphify-setup.md")
        setup = " ".join(raw.split())
        self.assertIn("v1.5.0 includes the compatibility and readiness additions", setup)
        self.assertIn("The historical `v1.4.2` package", setup)
        self.assertIn("The next two paragraphs are included in v1.5.0", setup)
        acquisition = raw.split("## Separate acquisition environment", 1)[1]
        acquisition = acquisition.split("## Separate contained offline build", 1)[0]
        self.assertIn("Install any required language extras", acquisition)
        self.assertIn("If runtime ownership checks refuse", acquisition)

    def test_rebuild_guidance_is_scoped_to_generations_the_1007_gaps_affected(self):
        """Not every generation built before the next release needs a rebuild --
        only one the #1007 compatibility gaps left partial."""
        pages = {
            "docs/graphify-setup.md": _read("docs/graphify-setup.md"),
            "README.md": _read("README.md"),
            "docs/current-state-and-roadmap.md": _read(
                "docs/current-state-and-roadmap.md"
            ),
        }
        # Wording that tells every reader to rebuild regardless of state.
        overclaims = (
            "a generation built before that release has to be rebuilt explicitly",
            "a generation built before that future release must be rebuilt",
            "every generation built before",
            "all generations built before",
            "any generation built before that release must be rebuilt",
        )
        for relative, raw in pages.items():
            collapsed = " ".join(raw.split()).lower()
            for phrase in overclaims:
                with self.subTest(doc=relative, phrase=phrase):
                    self.assertNotIn(phrase, collapsed)
            with self.subTest(doc=relative, requirement="partial-scoped"):
                # The rebuild is tied to the partial state, not to a build date.
                self.assertIn("partial", collapsed)
                self.assertIn("frontend generation", collapsed)
            with self.subTest(doc=relative, requirement="usable-is-exempt"):
                # A generation status already reports usable is left alone.
                self.assertIn("context-graph status --json", collapsed)
                self.assertIn("already reports usable", collapsed)

        setup = " ".join(pages["docs/graphify-setup.md"].split())
        self.assertIn("This is not a blanket rebuild", setup)
        self.assertIn("is unaffected and needs no rebuild", setup)
        self.assertIn("If it reports `partial`", setup)

    def test_no_current_doc_calls_1007_open_or_unmerged(self):
        """#1007 merged at b863e638. Nothing current may still call it open."""
        stale = (
            "separate open pull request",
            "it is not merged",
            "is not merged and not released",
            "#1007 is open",
            "#1007 remains open",
            "#1007 stays open",
            "pending #1007",
        )
        for relative in CURRENT_FACING_DOCS + (
            "docs/graphify-evaluation.md",
            "docs/context-graph-lifecycle.md",
            "docs/context-graph-queries.md",
        ):
            collapsed = " ".join(_read(relative).split()).lower()
            for phrase in stale:
                with self.subTest(doc=relative, phrase=phrase):
                    self.assertNotIn(phrase, collapsed)

    def test_graphify_provider_pin_is_unchanged_by_the_compatibility_work(self):
        """#1007 is a Code Mower fix, not a provider upgrade."""
        setup = " ".join(_read("docs/graphify-setup.md").split())
        self.assertIn("graphifyy", setup)
        self.assertIn("0.9.58", setup)
        self.assertIn(
            "e239803288e91c723d6e30540860bd6d5a1dc3f0914b9fc1104b0233e98aaeb8", setup
        )
        self.assertIn("not a Graphify upgrade", setup)
        for other in ("0.9.59", "0.9.60", "0.10.", "1.0.0"):
            with self.subTest(version=other):
                self.assertNotIn(f"graphifyy=={other}", setup)

    def test_board_demo_does_not_claim_serve_opens_a_browser(self):
        demo = " ".join(_read("examples/board-demo/README.md").split())
        self.assertIn("It does not open a browser.", demo)
        self.assertIn("--open", demo)
        self.assertNotIn("To open the local browser Board", demo)


class InstalledPromptPackTests(unittest.TestCase):
    def test_literal_starter_and_explicit_config_walkthrough(self):
        """Exercise installed 1.5.0 code, with no provider login or network doctor probes."""
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
assert code_mower.__version__ == '1.5.0'
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
